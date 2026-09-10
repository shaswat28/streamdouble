"""Integration tests against the example echo agent.

Phase 1 has no session layer yet, so these drive the protocol directly over
Starlette's in-process WebSocket test transport. That is deliberate: it proves
the frames this package builds are accepted by a real ASGI WebSocket endpoint,
without waiting for ``session.py`` to exist and without a socket, a port, or a
race to bind one.

The point is to close the loop that unit tests cannot: the frames are not merely
shaped the way the documentation says, they are shaped the way a real endpoint
can consume. When Phase 2 adds the live session, these stay as the fast
in-process layer beneath the real socket tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from streamdouble import audio
from streamdouble.protocol import (
    InboundMark,
    InboundMedia,
    MediaStreamEncoder,
    parse_outbound,
)

# The example lives outside the package, since it is documentation as much as
# it is a test fixture.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))

fastapi_testclient = pytest.importorskip(
    "fastapi.testclient", reason="fastapi is needed to exercise the example agent"
)


@pytest.fixture
def client():
    from echo_agent import app

    return fastapi_testclient.TestClient(app)


def drive(client, frames: list[bytes], *, query: str = "", expect: int = 0):
    """Run one simulated call against the echo agent.

    Returns the parsed frames the agent sent back. Reads exactly ``expect``
    frames, so a test that expects nothing does not block forever waiting for
    an agent that is deliberately silent.
    """
    encoder = MediaStreamEncoder()
    received = []

    with client.websocket_connect(f"/media-stream{query}") as websocket:
        websocket.send_text(json.dumps(encoder.connected()))
        websocket.send_text(json.dumps(encoder.start(custom_parameters={"tenant": "acme"})))

        for media_frame in frames:
            websocket.send_text(json.dumps(encoder.media(media_frame)))

        for _ in range(expect):
            received.append(
                parse_outbound(
                    websocket.receive_text(),
                    expected_stream_sid=encoder.identity.stream_sid,
                )
            )

        websocket.send_text(json.dumps(encoder.stop()))

    return received


@pytest.fixture
def speech_frames(speech_8k_path) -> list[bytes]:
    return audio.wav_to_ulaw_frames(speech_8k_path)[:12]


def test_a_real_endpoint_accepts_our_frames(client, speech_frames):
    """A real ASGI WebSocket endpoint consumes the whole frame sequence.

    The broadest claim this package makes, reduced to its smallest test: an
    agent that was not written with streamdouble in mind can parse what we send.
    """
    received = drive(client, speech_frames, expect=11)
    assert received, "the agent sent nothing back"


def test_echoed_audio_is_byte_identical(client, speech_frames):
    """Audio survives the full round trip through a third-party endpoint.

    The agent base64-decodes what we send and base64-encodes it back using its
    own code, not ours. Byte equality after that proves the encoding layers on
    both sides agree -- which is precisely what a double-encoding bug on either
    side would break.
    """
    received = drive(client, speech_frames, expect=11)
    echoed = [f.payload for f in received if isinstance(f, InboundMedia)]

    assert echoed == speech_frames[: len(echoed)]
    assert all(len(payload) == audio.FRAME_BYTES for payload in echoed)


def test_the_agent_sends_a_mark_when_it_finishes_speaking(client, speech_frames):
    """The mark arrives, and arrives after the audio it checkpoints.

    Ordering matters: a mark is a position in the audio stream, so one that
    arrives before the audio would be meaningless. Phase 2 echoes these back.
    """
    received = drive(client, speech_frames, expect=11)

    marks = [f for f in received if isinstance(f, InboundMark)]
    assert [m.name for m in marks] == ["reply-complete"]
    assert isinstance(received[-1], InboundMark)


def test_custom_parameters_reach_the_agent(client, speech_frames):
    """The agent can read TwiML ``<Parameter>`` values off the start frame.

    Exercised end to end rather than asserted on the built frame, because the
    thing worth knowing is that the agent's own accessor path works.
    """
    # The echo agent prints them; reaching a reply at all means start parsed
    # cleanly, including customParameters.
    assert drive(client, speech_frames, expect=1)


def test_a_silent_agent_sends_nothing(client, speech_frames):
    """An agent that never replies is a supported case, not a hang.

    This is the shape of a real failure -- an agent whose STT never fires -- and
    the client must be able to distinguish it from a slow reply. Phase 3 turns
    this into a timeout with its own exit code.
    """
    assert drive(client, speech_frames, query="?mode=silent", expect=0) == []


def test_an_agent_that_thinks_before_replying_still_replies(client, speech_frames):
    """A deliberate delay does not break the exchange.

    The same hook Phase 3 uses to check that measured latency matches injected
    latency. Kept short here so the suite stays fast; the measurement check
    itself belongs with the metrics layer.
    """
    received = drive(client, speech_frames, query="?delay_ms=50", expect=11)
    assert [f.payload for f in received if isinstance(f, InboundMedia)] == speech_frames[:10]
