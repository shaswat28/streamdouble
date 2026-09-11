"""A minimal voice agent that speaks the Twilio Media Streams protocol.

Load-bearing in three ways:

1. **CI target.** The integration suite needs something to talk to that does not
   require Twilio credentials, an ngrok tunnel, or a phone.
2. **Onboarding example.** Someone who does not yet own a voice agent can still
   try streamdouble in one command, and can read this file to see what the
   protocol looks like from the agent's side.
3. **Measurement reference.** ``--delay-ms`` injects a known, deliberate think
   time. A latency tool that reports the wrong latency is worse than no tool,
   because people trust it -- so the metrics layer is checked against a server
   whose true latency is known by construction, not merely estimated.

Run it directly::

    python examples/echo_agent.py --port 8000

Then point streamdouble at ``ws://localhost:8000/media-stream``.

This is deliberately a hand-rolled FastAPI endpoint rather than one built on a
voice framework, because that is what a large share of Twilio voice agents
actually are -- and it is the shape of endpoint no existing tool exercises.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from typing import Any

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
except ImportError:  # pragma: no cover - a setup problem, not a code path
    # A bare ModuleNotFoundError here is a bad first impression: this is the
    # very first command someone with no voice agent of their own runs, and
    # `pip install streamdouble` deliberately does not pull in a web framework
    # just to serve an example.
    raise SystemExit(
        "examples/echo_agent.py needs FastAPI and uvicorn, which streamdouble "
        "does not install on its own.\n\n"
        "    pip install 'streamdouble[example]'\n"
    ) from None

app = FastAPI(title="streamdouble echo agent")

#: Audio the agent has to receive before it starts replying, in "echo" mode.
#: Roughly a fifth of a second, which is long enough to look like a real agent
#: waiting for a phrase and short enough to keep tests fast.
DEFAULT_TRIGGER_FRAMES = 10


class EchoSession:
    """Per-connection state for one simulated call.

    Kept as a class rather than as locals in the handler so that the reply task
    and the receive loop share one obvious place to look for state -- which is
    the part of async WebSocket code that most often goes wrong.
    """

    def __init__(
        self,
        websocket: WebSocket,
        mode: str,
        delay_ms: int,
        *,
        hangup_after: int = 0,
        garbage_after: int = 0,
        clear_after: int = 0,
    ) -> None:
        self.websocket = websocket
        self.mode = mode
        self.delay_ms = delay_ms
        self.hangup_after = hangup_after
        self.garbage_after = garbage_after
        self.clear_after = clear_after

        self.stream_sid: str | None = None
        self.received_frames: list[bytes] = []
        self.replied = False
        self.stream_started_at: float | None = None
        self.marks_sent = 0

    async def send(self, message: dict[str, Any]) -> None:
        await self.websocket.send_text(json.dumps(message))

    async def send_media(self, payload: bytes) -> None:
        """Send audio to be played to the caller.

        Note the shape: the app's outbound media frame carries only ``event``,
        ``streamSid`` and ``media.payload``. It does not echo back the counters
        Twilio sends inbound -- that asymmetry is real, and an agent that sends
        a full inbound-shaped frame here is doing extra work for nothing.
        """
        await self.send(
            {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": base64.b64encode(payload).decode("ascii")},
            }
        )

    async def send_mark(self, name: str) -> None:
        """Send a playback checkpoint.

        Twilio echoes this back once the audio queued before it has finished
        playing. Agents commonly use that echo to know when it is their turn to
        listen again, so a simulator that never echoes marks will hang them --
        which is exactly why streamdouble echoes them.
        """
        self.marks_sent += 1
        await self.send({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}})

    async def send_garbage(self) -> None:
        """Send frames a correct agent never would.

        Three distinct violations, so a test can check that a client reports all
        of them rather than aborting on the first: unparseable JSON, a media
        frame with no streamSid, and a payload that is not valid base64.
        """
        print("  garbage sending three malformed frames")
        await self.websocket.send_text("{not json at all")
        await self.send({"event": "media", "media": {"payload": "//8="}})
        await self.send(
            {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": "!!!! not base64 !!!!"},
            }
        )

    async def handle_start(self, message: dict[str, Any]) -> None:
        self.stream_sid = message["start"]["streamSid"]
        self.stream_started_at = time.monotonic()
        params = message["start"].get("customParameters", {})
        print(f"  start   streamSid={self.stream_sid} customParameters={params}")

    async def handle_media(self, message: dict[str, Any]) -> None:
        payload = base64.b64decode(message["media"]["payload"])
        self.received_frames.append(payload)

        # Misbehaviours, for tests that need an agent to fail in a specific way.
        # They live in the example rather than in a separate mock because a
        # simulator has to be tested against the things real agents actually do
        # wrong, and this file is already the thing tests talk to.
        if self.hangup_after and len(self.received_frames) >= self.hangup_after:
            print(f"  hangup  closing abruptly after {len(self.received_frames)} frames")
            await self.websocket.close()
            return

        if self.garbage_after and len(self.received_frames) == self.garbage_after:
            await self.send_garbage()

        if self.clear_after and len(self.received_frames) == self.clear_after:
            print("  clear   sending clear (simulated barge-in)")
            await self.send({"event": "clear", "streamSid": self.stream_sid})

        if self.mode == "silent":
            return

        if self.replied:
            # Steady state: echo each frame as it arrives, so the agent keeps
            # talking for as long as the caller does. Replying only once would
            # make the agent go permanently silent after its first utterance,
            # which is a strange thing for an echo to do and would deadlock any
            # client that waits for a reply to the whole clip.
            await self.send_media(payload)
            return

        if len(self.received_frames) < DEFAULT_TRIGGER_FRAMES:
            return

        self.replied = True
        await self.reply()

    async def reply(self) -> None:
        """Wait out the configured think time, then speak.

        The delay is measured from the moment the reply is triggered, so the
        time to first audio byte that a client observes is
        ``delay_ms`` plus the time it took to send the trigger frames. Tests
        that assert on measured latency account for both.
        """
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000)

        # Echo the audio received so far, in 20 ms frames, exactly as it came in.
        # A real agent would be sending TTS output here; echoing is what makes
        # this testable, since the client already knows what it sent.
        for payload in list(self.received_frames):
            await self.send_media(payload)

        await self.send_mark("reply-complete")
        elapsed_ms = (time.monotonic() - (self.stream_started_at or time.monotonic())) * 1000
        print(f"  replied {len(self.received_frames)} frames at t+{elapsed_ms:.0f}ms")

    async def handle_mark(self, message: dict[str, Any]) -> None:
        """Twilio echoing a mark back once our audio finished playing."""
        print(f"  mark    echoed back: {message['mark']['name']!r}")

    async def handle_dtmf(self, message: dict[str, Any]) -> None:
        print(f"  dtmf    digit={message['dtmf']['digit']!r} track={message['dtmf']['track']!r}")


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket) -> None:
    """A Twilio Media Streams endpoint.

    Query parameters, for testing:
        mode: "echo" (default) replies with the audio it received;
            "silent" never replies, which is how a client's no-response
            timeout gets exercised.
        delay_ms: think time before the first reply frame, in milliseconds.
            Used to check that a latency measurement reports what actually
            happened.
        hangup_after: close the socket abruptly after this many media frames,
            without sending stop. What a dropped call looks like.
        garbage_after: send three malformed frames at this frame number.
        clear_after: send a clear at this frame number, as if the caller had
            interrupted.
    """
    mode = websocket.query_params.get("mode", "echo")
    delay_ms = int(websocket.query_params.get("delay_ms", "0"))
    hangup_after = int(websocket.query_params.get("hangup_after", "0"))
    garbage_after = int(websocket.query_params.get("garbage_after", "0"))
    clear_after = int(websocket.query_params.get("clear_after", "0"))

    await websocket.accept()
    print(f"connection opened (mode={mode}, delay_ms={delay_ms})")

    session = EchoSession(
        websocket,
        mode=mode,
        delay_ms=delay_ms,
        hangup_after=hangup_after,
        garbage_after=garbage_after,
        clear_after=clear_after,
    )
    handlers = {
        "start": session.handle_start,
        "media": session.handle_media,
        "mark": session.handle_mark,
        "dtmf": session.handle_dtmf,
    }

    try:
        while True:
            message = json.loads(await websocket.receive_text())
            event = message.get("event")

            if event == "connected":
                print(f"  connected protocol={message.get('protocol')} v{message.get('version')}")
            elif event == "stop":
                print(f"  stop    after {len(session.received_frames)} frames")
                break
            else:
                handler = handlers.get(event)
                if handler is None:
                    print(f"  ?       ignoring unknown event {event!r}")
                else:
                    await handler(message)
    except WebSocketDisconnect:
        # The caller hung up. Expected, not an error -- this is what an abrupt
        # hangup looks like from the agent's side.
        print(f"  closed  abruptly after {len(session.received_frames)} frames")
    finally:
        print("connection closed")


@app.get("/health")
async def health() -> dict[str, str]:
    """Readiness probe, so tests can wait for the server without guessing."""
    return {"status": "ok"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    import uvicorn

    print(f"echo agent listening on ws://{args.host}:{args.port}/media-stream")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
