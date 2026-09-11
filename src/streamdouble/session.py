"""WebSocket session lifecycle: the live half of the simulator.

Orchestrates the pure layers -- :mod:`protocol` builds and parses frames,
:mod:`audio` converts them, :mod:`pacer` decides when -- and adds the one thing
none of them have: a socket, and two things happening at once.

The concurrency shape is deliberately small. There are exactly three concurrent
activities, and each one has a single, stated reason to exist:

* **Sending**, which paces media frames in real time.
* **Receiving**, which cannot be folded into sending because the agent replies
  while the caller is still talking -- that overlap *is* barge-in, and a
  request/response loop could not represent it.
* **Mark echoing**, which fires on a timer rather than in response to a message,
  because a mark comes back when the audio finishes playing, not when it arrives.

They run under an :class:`asyncio.TaskGroup`, so a failure in any one of them
propagates rather than vanishing into a task nobody awaited. A silently dead
receive task would look exactly like an agent that stopped responding, and that
is the single most misleading failure this module could have.

Each task ends on its own rather than by cancellation: the receiver exits when
the socket closes, the echoer when the closing event is set. Cancellation is
harder to reason about and tends to leave the last few frames unaccounted for.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets

from . import audio
from .chaos import Impairments, Network
from .pacer import Pacer, PacingStats
from .protocol import (
    InboundClear,
    InboundMark,
    InboundMedia,
    MediaStreamEncoder,
    ProtocolViolation,
    StreamIdentity,
    UnknownFrame,
    parse_outbound,
)
from .scenario import (
    Dtmf,
    Expect,
    Hangup,
    Say,
    Scenario,
    Step,
    Wait,
    WaitFor,
)
from .trace import Trace

__all__ = [
    "Session",
    "SessionConfig",
    "SessionEvent",
    "SessionResult",
    "run_call",
]

#: How long a DTMF keypress occupies the line. Real tones are 40-100 ms;
#: 80 ms sits in the middle and is long enough for an agent to register.
_DTMF_TONE_S = 0.08

#: Bytes of mu-law per second. One byte is one 8 kHz sample.
BYTES_PER_SECOND = audio.SAMPLE_RATE

#: Largest inbound WebSocket frame accepted, in bytes.
#:
#: Generous -- a legitimate 20 ms payload is 160 bytes, and even an agent that
#: batches a whole utterance into one frame stays far below this -- but finite.
#: Passing max_size=None instead would let a runaway or hostile endpoint buffer
#: a frame of unbounded size before the protocol layer ever gets to reject it,
#: and the URL is user-supplied and may well be remote.
MAX_INBOUND_FRAME_BYTES = 8 * 1024 * 1024

#: Most audio retained from one call, in bytes. 60 MB is over two hours of
#: mu-law at 8 kHz -- far beyond any real call, and finite, which the previous
#: arrangement was not.
#:
#: The per-frame cap does not help here: it is per frame, while the buffer is
#: per call. Measured against a server streaming 400 KB frames as fast as the
#: socket accepted them, an unbounded buffer reached 206 MB of audio and 436 MB
#: of peak process memory in about five seconds; at the default 30 s drain that
#: is gigabytes, and --out then tries to write all of it to disk.
MAX_AUDIO_BYTES = 60 * 1024 * 1024

#: Most per-frame events retained. Aggregate counters stay exact past this --
#: only the individual event objects stop being kept -- so the reported totals
#: remain correct on a call long enough to hit it. 200k frames is over an hour
#: of 20 ms audio.
MAX_EVENTS = 200_000

#: Most protocol violations and distinct unknown events retained. An agent
#: misbehaving on every single frame is one bug reported thousands of times;
#: past this the counts continue but the detail stops accumulating.
MAX_REPORTED_ISSUES = 1000

#: How long to wait for the final stop frame to reach the peer.
#:
#: websockets applies no send timeout of its own, so a peer that has stopped
#: reading stalls the send on flow control for as long as the socket stays open.
#: Because this send happens during shutdown, an unbounded wait there hangs the
#: whole session -- on the error path as well as the success one -- and defeats
#: max_drain_s entirely.
STOP_SEND_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class SessionEvent:
    """One timestamped thing that happened during a call.

    The session records; it does not interpret. Every measurement Phase 3
    reports is derived from this log rather than computed inline, for two
    reasons: the arithmetic becomes testable against a synthetic log with no
    socket involved, and a surprising latency figure can be traced back to the
    events that produced it instead of being taken on trust.

    ``at`` is ``time.perf_counter`` seconds, from the same clock the pacer uses.
    Monotonic, so an NTP step mid-call cannot produce a negative latency that
    the tool then reports with a straight face -- and high resolution, which
    ``time.monotonic`` is not: on Windows that is GetTickCount64 at 15.625 ms,
    which would quantise every figure in this log to a grid nearly as coarse as
    the 20 ms frame interval being measured.
    """

    at: float
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionResult:
    """Everything one simulated call produced."""

    identity: StreamIdentity
    events: list[SessionEvent]

    frames_sent: int = 0
    #: Frames the simulated network lost before they reached the agent.
    frames_dropped: int = 0
    media_frames_received: int = 0
    audio_received: bytes = b""

    marks_received: list[str] = field(default_factory=list)
    marks_echoed: list[str] = field(default_factory=list)
    clears_received: int = 0
    unknown_events: list[str] = field(default_factory=list)
    violations: list[ProtocolViolation] = field(default_factory=list)
    #: Total violations seen, including any past the retention cap.
    violation_count: int = 0
    #: Set when the recording hit MAX_AUDIO_BYTES and stopped growing.
    audio_truncated: bool = False
    #: Set when the event log hit MAX_EVENTS. Aggregate counters remain exact.
    events_truncated: bool = False

    pacing: PacingStats = field(default_factory=PacingStats)
    #: The impairment layer that shaped this call, carrying its own statistics.
    network: Network | None = None
    #: Results of any `expect` steps, as (description, passed) pairs.
    expectations: list[tuple[str, bool]] = field(default_factory=list)

    @property
    def failed_expectations(self) -> list[str]:
        return [what for what, passed in self.expectations if not passed]

    #: Monotonic instant the stream began -- the pacer's origin, and the
    #: reference point for every latency figure.
    started_at: float = 0.0
    #: Monotonic instant the first byte of agent audio arrived, if any.
    first_audio_at: float | None = None
    #: Set when the agent produced no audio within the response timeout.
    timed_out: bool = False
    #: Set when the agent closed the socket before the call was finished.
    closed_early: bool = False

    @property
    def audio_duration_s(self) -> float:
        """Duration of the audio the agent sent, in seconds."""
        return len(self.audio_received) / BYTES_PER_SECOND

    @property
    def time_to_first_audio_s(self) -> float | None:
        """Seconds from stream start to the first byte of agent audio.

        The single most important voice-agent metric. ``None`` means the agent
        never spoke, which is a distinct outcome from "spoke immediately" and
        must not collapse to 0.
        """
        if self.first_audio_at is None:
            return None
        return self.first_audio_at - self.started_at

    def events_of(self, kind: str) -> list[SessionEvent]:
        return [event for event in self.events if event.kind == kind]


@dataclass
class SessionConfig:
    """Knobs for one simulated call.

    Defaults are chosen to behave like a patient caller: long enough to let a
    slow agent finish, short enough that a hung agent does not stall CI.
    """

    #: Seconds to wait for the agent's first audio before giving up.
    response_timeout_s: float = 10.0
    #: After audio stops arriving, how long to wait before deciding the agent
    #: has finished speaking.
    quiet_period_s: float = 1.0
    #: Hard cap on the post-send drain, however chatty the agent is.
    max_drain_s: float = 30.0
    #: Seconds to wait for the WebSocket handshake.
    connect_timeout_s: float = 10.0
    #: Hard ceiling on the whole call, sending included.
    #:
    #: Every other wait in a session is bounded -- response, drain, connect,
    #: the final stop send -- but the send phase was not, so a scenario with a
    #: long `wait` could hold a call open indefinitely. The tool is built to run
    #: unattended, where a job that hangs costs more than one that fails.
    max_call_s: float = 600.0
    #: Echo marks back after the corresponding audio would have played.
    #: On by default because Twilio does it, and agents gate turn-taking on it.
    echo_marks: bool = True
    #: TwiML ``<Parameter>`` values to put on the start frame.
    custom_parameters: dict[str, str] = field(default_factory=dict)
    #: Stop the call as soon as the agent's first mark is echoed. Useful for
    #: single-turn tests that would otherwise sit through the whole quiet period.
    stop_after_first_mark: bool = False
    #: Network conditions to simulate on the caller's audio. Default is a
    #: perfect connection.
    impairments: Impairments = field(default_factory=Impairments)
    #: Seed for the impairment decisions. Fixed by default so a chaos run is
    #: reproducible: when a bad network finds a bug, the seed is the repro.
    chaos_seed: int = 0
    #: Where to record every frame, in both directions. ``None`` disables it.
    #:
    #: The trace is accumulated in memory and written once the socket is
    #: closed, never during the call -- see ``trace.py`` for why that is not
    #: merely an optimisation.
    trace: Trace | None = None


class Session:
    """One simulated Twilio call against a WebSocket endpoint."""

    def __init__(
        self,
        url: str,
        frames: Sequence[bytes] | None = None,
        *,
        scenario: Scenario | None = None,
        config: SessionConfig | None = None,
        identity: StreamIdentity | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if frames is None and scenario is None:
            raise ValueError("a session needs either frames or a scenario")

        self.url = url
        self.frames = list(frames or [])
        # A plain clip is the one-step scenario "say this". Modelling it that
        # way rather than as a separate code path means the simple case and the
        # scripted case cannot drift apart in behaviour.
        self.scenario = scenario
        self._hung_up = False
        # Set once the stream starts; until then there is no call to bound.
        self._call_deadline = float("inf")
        self.config = config or SessionConfig()
        self.clock = clock

        self.encoder = MediaStreamEncoder(identity)
        self.pacer = Pacer(audio.FRAME_MS / 1000, clock=clock)
        self.network = Network(self.config.impairments, seed=self.config.chaos_seed)
        self.result = SessionResult(identity=self.encoder.identity, events=[])

        self._closing = asyncio.Event()
        self._first_audio = asyncio.Event()
        self._last_audio_at: float | None = None

        # Accumulate into a bytearray, not by concatenating bytes. `buffer +=
        # payload` on an immutable bytes object copies the whole buffer every
        # frame, which is quadratic: measured at 0.01s for 40s of audio but
        # 18.6s for 640s. That cost lands inside the receive loop on the event
        # loop, so it does not merely make long calls slow -- it delays frame
        # handling and inflates the very latency figures this tool reports,
        # progressively and plausibly.
        self._audio_received = bytearray()

        # Marks waiting for their audio to finish playing: (due_at, name).
        self._pending_marks: list[tuple[float, str]] = []
        self._marks_changed = asyncio.Event()
        self._first_mark_echoed = asyncio.Event()

        # When the audio the agent has sent so far would finish playing to the
        # caller. Twilio echoes a mark at this instant, not on arrival.
        self._playback_ends_at: float | None = None

        # Set when the agent asks for buffered audio to be discarded, so a
        # scenario can wait on barge-in having actually taken effect.
        self._clear_seen = asyncio.Event()
        self._mark_echoed_names: list[str] = []

    # ----------------------------------------------------------------------
    # Recording
    # ----------------------------------------------------------------------

    def _record(self, kind: str, at: float | None = None, **detail: Any) -> SessionEvent:
        """Append an event, optionally at an instant the caller already captured.

        Passing ``at`` explicitly is what keeps a single frame to a single
        timestamp. Reading the clock afresh inside each _record call meant one
        arriving frame was stamped three separate times -- into first_audio_at,
        into the first_audio event, and into the media_received event after the
        buffering and playback-clock work -- so time-to-first-audio and the
        inter-frame gaps ended up derived from different instants for the same
        frame, with the tool's own bookkeeping folded into the gaps.
        """
        event = SessionEvent(at=self.clock() if at is None else at, kind=kind, detail=detail)
        if len(self.result.events) < MAX_EVENTS:
            self.result.events.append(event)
        elif not self.result.events_truncated:
            self.result.events_truncated = True
        return event

    # ----------------------------------------------------------------------
    # Entry point
    # ----------------------------------------------------------------------

    async def run(self) -> SessionResult:
        """Place the call and return everything it produced.

        Raises:
            OSError: The endpoint could not be reached.
            TimeoutError: The WebSocket handshake did not complete in time.
        """
        self._record("connecting", url=self.url)

        try:
            connection = await asyncio.wait_for(
                websockets.connect(self.url, max_size=MAX_INBOUND_FRAME_BYTES),
                timeout=self.config.connect_timeout_s,
            )
        except TimeoutError:
            self._record("connect_timeout")
            raise
        except OSError as exc:
            self._record("connect_failed", error=str(exc))
            raise

        self._record("connected")

        try:
            await self._converse(connection)
        except* websockets.ConnectionClosed:
            # The agent hung up mid-call. A real outcome worth reporting, not an
            # error to propagate -- an agent that closes early is exactly the
            # kind of bug this tool exists to catch.
            #
            # except* because _converse runs a TaskGroup, which wraps whatever
            # its children raise in an ExceptionGroup. A plain except clause
            # here would not match, and the close would surface as an unhandled
            # ExceptionGroup instead of a reported outcome.
            self.result.closed_early = True
            self._record("closed_by_peer")
        finally:
            # Explicit close rather than `async with connection`: the object
            # returned by awaiting websockets.connect() is not an async context
            # manager in every supported version of the library. Closing twice
            # is harmless, so _finish having already closed is fine.
            await connection.close()

        self.result.audio_received = bytes(self._audio_received)
        self.result.pacing = self.pacer.stats
        self.result.network = self.network
        self._record("finished")
        return self.result

    async def _converse(self, connection: Any) -> None:
        """Run the three concurrent activities to completion."""
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._receive_loop(connection), name="receive")
            if self.config.echo_marks:
                tasks.create_task(self._mark_echo_loop(connection), name="mark-echo")

            try:
                await self._send_stream(connection)
                await self._drain()
            finally:
                # Always shut down cleanly, even if sending raised: leaving the
                # receiver blocked on a socket nobody will close turns a clear
                # error into a hang.
                self._closing.set()
                self._marks_changed.set()
                await self._finish(connection)

    async def _finish(self, connection: Any) -> None:
        """Send ``stop`` and close, so the receive loop can end.

        Runs from a ``finally``, so it must not raise: an exception here would
        replace whatever actually went wrong, and the real cause would be lost.

        The ``started`` check matters more than it looks. If the send loop failed
        before ``start`` reached the peer, the encoder's ordering guard refuses
        to build a ``stop`` -- correctly, since Twilio would never send one --
        and that FrameSequenceError would escape from this ``finally``,
        discarding the ConnectionClosed that was the actual problem. The user
        would then see an internal frame-ordering error instead of "the agent
        closed the connection", pointing debugging at streamdouble rather than
        at their agent.
        """
        if self.encoder.started and not self.encoder.stopped:
            with contextlib.suppress(websockets.ConnectionClosed, TimeoutError):
                await asyncio.wait_for(
                    self._send(connection, self.encoder.stop()),
                    timeout=STOP_SEND_TIMEOUT_S,
                )
                self._record("sent_stop")

        # close() is bounded by the library's own close_timeout.
        with contextlib.suppress(websockets.ConnectionClosed):
            await connection.close()

    # ----------------------------------------------------------------------
    # Sending
    # ----------------------------------------------------------------------

    async def _send_stream(self, connection: Any) -> None:
        """Send ``connected``, ``start``, then run the caller's script."""
        await self._send(connection, self.encoder.connected())
        await self._send(connection, self.encoder.start(**_start_kwargs(self.config)))

        self.result.started_at = self.pacer.start()
        self._record("stream_started", impairments=self.network.impairments.describe())

        self._call_deadline = self.result.started_at + self.config.max_call_s

        steps = self.scenario.steps if self.scenario else [Say(Path("-"), self.frames)]
        for step in steps:
            if self._hung_up:
                break
            if self.clock() >= self._call_deadline:
                self._record("max_call_reached", limit_s=self.config.max_call_s)
                break
            await self._run_step(connection, step)

        self._record(
            "sent_all_media",
            frames=self.result.frames_sent,
            dropped=self.result.frames_dropped,
        )

    async def _run_step(self, connection: Any, step: Step) -> None:
        """Execute one scenario step."""
        if isinstance(step, Say):
            await self._stream_frames(connection, step.frames)

        elif isinstance(step, Wait):
            await self._stream_silence(connection, step.seconds)

        elif isinstance(step, WaitFor):
            await self._stream_silence_until(connection, step)

        elif isinstance(step, Dtmf):
            for digit in step.digits:
                await self._send(connection, self.encoder.dtmf(digit))
                self._record("sent_dtmf", digit=digit)
                # A real keypress occupies the line for a moment rather than
                # arriving instantaneously alongside the next audio frame.
                await self._stream_silence(connection, _DTMF_TONE_S)

        elif isinstance(step, Expect):
            self._check_expectation(step)

        elif isinstance(step, Hangup):
            self._hung_up = True
            self._record("caller_hung_up")
            await connection.close()

    async def _stream_frames(self, connection: Any, frames: Sequence[bytes]) -> None:
        """Send frames in real time, through the impairment layer."""
        for frame in frames:
            await self.pacer.wait()

            if self.clock() >= self._call_deadline:
                self._record("max_call_reached", limit_s=self.config.max_call_s)
                self._hung_up = True
                return

            if self.network.should_drop():
                # Never goes on the wire. Presentation time still advances, so
                # the agent sees a media.timestamp discontinuity -- the only
                # signal it can get, since the WebSocket leg is TCP and cannot
                # lose a frame in transit. See chaos.py.
                self.encoder.skip_frame()
                self.result.frames_dropped += 1
                continue

            delay = self.network.delay_s()
            if delay:
                # Delays this frame's release without moving any other frame's
                # deadline, so jitter perturbs spacing without accumulating --
                # the pacer's absolute deadlines absorb it, exactly as a real
                # jitter buffer would.
                await asyncio.sleep(delay)

            try:
                await self._send(connection, self.encoder.media(frame))
            except websockets.ConnectionClosed:
                self._hung_up = True
                raise
            self.result.frames_sent += 1

    async def _stream_silence(self, connection: Any, seconds: float) -> None:
        """Stream silence for a fixed duration.

        Not a pause in sending. A real call carries 20 ms frames continuously
        for its whole duration, and an agent's endpointing depends on that: stop
        sending and the agent sees a dead line, not a quiet caller.
        """
        frame_count = max(1, round(seconds / (audio.FRAME_MS / 1000)))
        await self._stream_frames(connection, [audio.silence_frame()] * frame_count)

    async def _stream_silence_until(self, connection: Any, step: WaitFor) -> None:
        """Stream silence until the agent does something, or time runs out."""
        waiters = {
            "audio": self._first_audio,
            "mark": self._first_mark_echoed,
            "clear": self._clear_seen,
        }

        deadline = self.clock() + step.timeout_s
        silence = audio.silence_frame()

        while self.clock() < deadline:
            if step.event == "quiet":
                since = self.clock() - (self._last_audio_at or 0)
                if self.result.first_audio_at is not None and since >= self.config.quiet_period_s:
                    break
            elif waiters[step.event].is_set():
                break
            await self._stream_frames(connection, [silence])
        else:
            self._record("wait_for_timeout", event=step.event, waited=step.timeout_s)
            return

        self._record("wait_for_satisfied", event=step.event)

    def _check_expectation(self, step: Expect) -> None:
        """Evaluate one assertion against what has happened so far.

        Recorded rather than raised. A failed expectation lets the rest of the
        scenario run, so a single execution reports every problem instead of
        stopping at the first -- the same reasoning as protocol violations.
        """
        observed = {
            "audio": self.result.first_audio_at is not None,
            "mark": bool(self.result.marks_received),
            "clear": self.result.clears_received > 0,
            "silence": self.result.first_audio_at is None,
            "no_violations": not self.result.violations,
        }[step.what]

        passed = (not observed) if step.negated else observed
        self.result.expectations.append((step.describe(), passed))
        self._record("expectation", what=step.describe(), passed=passed)

    # ----------------------------------------------------------------------
    # Receiving
    # ----------------------------------------------------------------------

    async def _receive_loop(self, connection: Any) -> None:
        """Consume frames from the agent until the socket closes.

        Runs for the whole call, concurrently with sending, because an agent may
        start replying before the caller has stopped talking.
        """
        try:
            async for message in connection:
                self._handle_message(message)
        except websockets.ConnectionClosed:
            # Normal termination: either we closed, or the agent did. Which of
            # the two is recorded by the caller, which knows the difference.
            pass

    async def _send(self, connection: Any, frame: dict[str, Any]) -> None:
        """Serialise, trace and send one frame.

        Every outbound frame goes through here so that tracing cannot miss one.
        The trace call is a list append -- deliberately not a write -- because
        this runs on the event loop alongside the pacer.

        The frame is stamped before the send and recorded after it. Stamping
        first keeps the trace on the same timeline as the metrics; recording
        after means a frame whose send raised is not written down as though it
        went out. Gate 6 found the earlier version claiming exactly that, and
        the frame it lied about was the last one before a disconnect -- which
        is the one someone opens a trace to look at.
        """
        at = self.clock()
        await connection.send(_dumps(frame))
        if self.config.trace is not None:
            self.config.trace.note_out(frame, at)

    def _handle_message(self, message: str | bytes) -> None:
        """Parse and account for one frame from the agent."""
        # Stamp the arrival before parsing, not after. parse_outbound runs
        # json.loads and base64 decoding, which cost 0.004 ms on a 160-byte
        # payload but 0.32 ms on a 64 KB one -- so timestamping afterwards
        # charges the agent for this tool's decode work, in proportion to how
        # much audio it batches per frame. Small in absolute terms, but a
        # systematic bias correlated with agent behaviour, and it contradicts
        # the convention metrics.py documents: arrival, not decode completion.
        arrived_at = self.clock()

        try:
            parsed = parse_outbound(message, expected_stream_sid=self.encoder.identity.stream_sid)
        except ProtocolViolation as violation:
            # Recorded, not raised. One malformed frame should not end the call:
            # the user wants the whole picture, and a tool that aborts on the
            # first violation reports one problem where there may be twenty.
            if len(self.result.violations) < MAX_REPORTED_ISSUES:
                self.result.violations.append(violation)
            self.result.violation_count += 1
            self._record(
                "violation", at=arrived_at, code=violation.code, message=str(violation)
            )
            # Traced from the raw message, because a frame that failed to parse
            # is the single most valuable thing a trace can hold and one that
            # recorded only well-formed frames would omit exactly the case
            # being reported.
            if self.config.trace is not None:
                self.config.trace.note_in(message, arrived_at)
            return

        # Hand the already-parsed frame to the trace rather than letting it
        # run json.loads a second time. Gate 6 found the duplicate: real agents
        # batch outbound audio into ~8000-byte frames, JSON decode of a large
        # payload was measured at 0.32 ms back at gate 3, and doubling that
        # lands inside the receive loop next to the pacer -- the precise
        # "observer perturbs the observed" cost this design exists to avoid.
        if self.config.trace is not None:
            self.config.trace.note_in(message, arrived_at, parsed=parsed.raw)

        if isinstance(parsed, InboundMedia):
            self._handle_media(parsed, arrived_at)
        elif isinstance(parsed, InboundMark):
            self._handle_mark(parsed, arrived_at)
        elif isinstance(parsed, InboundClear):
            self._handle_clear(arrived_at)
        elif isinstance(parsed, UnknownFrame):
            if len(self.result.unknown_events) < MAX_REPORTED_ISSUES:
                self.result.unknown_events.append(parsed.event)
            self._record("unknown_event", at=arrived_at, event=parsed.event)

    def _handle_media(self, media: InboundMedia, now: float) -> None:
        """Account for one inbound media frame that arrived at ``now``.

        ``now`` is the arrival instant captured by the caller, and every
        timestamp derived from this frame uses it -- the field, both events, and
        the playback clock. One frame, one instant.
        """
        if self.result.first_audio_at is None:
            self.result.first_audio_at = now
            self._first_audio.set()
            self._record("first_audio", at=now, bytes=len(media.payload))

        self.result.media_frames_received += 1
        self._last_audio_at = now

        # Bounded. The frame count and duration below stay exact; only the
        # audio itself stops being kept, so a runaway agent costs a truncated
        # recording rather than the process.
        if len(self._audio_received) < MAX_AUDIO_BYTES:
            self._audio_received += media.payload
            if len(self._audio_received) >= MAX_AUDIO_BYTES and not self.result.audio_truncated:
                self.result.audio_truncated = True
                self._record("audio_truncated", at=now, limit_bytes=MAX_AUDIO_BYTES)

        # Advance the playback clock. Audio queues behind whatever is already
        # playing, so the buffer drains from whichever is later: now, or the end
        # of what is already queued.
        duration = len(media.payload) / BYTES_PER_SECOND
        start_from = max(now, self._playback_ends_at or now)
        self._playback_ends_at = start_from + duration

        self._record("media_received", at=now, bytes=len(media.payload))

    def _handle_mark(self, mark: InboundMark, now: float) -> None:
        """Queue a mark to be echoed when its audio would finish playing.

        Twilio returns a mark once the audio queued *before* it has finished
        playing to the caller, which is generally well after the mark arrives.
        Echoing immediately would be easier and would misrepresent the timing
        agents use for turn-taking -- an agent would think its greeting had
        finished while most of it was still to be heard.
        """
        self.result.marks_received.append(mark.name)

        due_at = self._playback_ends_at or now
        self._pending_marks.append((due_at, mark.name))
        self._marks_changed.set()

        self._record("mark_received", at=now, name=mark.name, echo_in=due_at - now)

    def _handle_clear(self, now: float) -> None:
        """Discard buffered audio, and the marks that were waiting on it.

        A ``clear`` is the agent reacting to barge-in. Twilio drops the unplayed
        buffer, and the marks scheduled behind that audio never come back --
        their audio is never going to play. An implementation that echoed them
        anyway would tell the agent an utterance completed that the caller
        actually interrupted.
        """
        self.result.clears_received += 1
        self._clear_seen.set()
        dropped = [name for _, name in self._pending_marks]
        self._pending_marks.clear()
        self._playback_ends_at = now
        self._marks_changed.set()

        self._record("clear_received", at=now, dropped_marks=dropped)

    # ----------------------------------------------------------------------
    # Mark echo
    # ----------------------------------------------------------------------

    async def _mark_echo_loop(self, connection: Any) -> None:
        """Send each queued mark back when its audio would have finished."""
        while not self._closing.is_set():
            if not self._pending_marks:
                await self._wait_for_marks()
                continue

            due_at, name = min(self._pending_marks)
            delay = due_at - self.clock()

            if delay > 0:
                # Wake early if a clear arrives, so a cancelled mark is not sent
                # after the audio it belonged to has been discarded.
                self._marks_changed.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._marks_changed.wait(), timeout=delay)
                continue

            self._pending_marks.remove((due_at, name))
            try:
                await self._send(connection, self.encoder.mark(name))
            except websockets.ConnectionClosed:
                return

            self.result.marks_echoed.append(name)
            self._record("mark_echoed", name=name)
            self._first_mark_echoed.set()

    async def _wait_for_marks(self) -> None:
        """Block until a mark is queued or the call is ending."""
        self._marks_changed.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._marks_changed.wait(), timeout=0.05)

    # ----------------------------------------------------------------------
    # Drain
    # ----------------------------------------------------------------------

    async def _drain(self) -> None:
        """Wait for the agent to finish speaking, then stop.

        Three ways this ends, and they are genuinely different outcomes that the
        caller needs to tell apart:

        * The agent never spoke -> ``timed_out``.
        * The agent spoke and went quiet -> a normal completed turn.
        * The agent never stopped -> the drain cap, so CI cannot hang.
        """
        if not await self._await_first_audio():
            return

        drain_started = self.clock()

        while self.clock() - drain_started < self.config.max_drain_s:
            if self._closing.is_set():
                return
            if self.config.stop_after_first_mark and self._first_mark_echoed.is_set():
                self._record("drain_complete", reason="first_mark_echoed")
                return

            since_audio = self.clock() - (self._last_audio_at or drain_started)
            # Do not stop while a mark is still waiting on audio to finish
            # playing: cutting the call there would deny the agent the echo it
            # is waiting for, and make a correct agent look like it hung.
            if since_audio >= self.config.quiet_period_s and not self._pending_marks:
                self._record("drain_complete", reason="quiet", quiet_for=since_audio)
                return

            await asyncio.sleep(0.02)

        self._record("drain_complete", reason="max_drain_reached")

    async def _await_first_audio(self) -> bool:
        """Wait for the agent's first audio. False if it never came."""
        if self._first_audio.is_set():
            return True
        try:
            await asyncio.wait_for(
                self._first_audio.wait(), timeout=self.config.response_timeout_s
            )
        except TimeoutError:
            self.result.timed_out = True
            self._record("response_timeout", waited=self.config.response_timeout_s)
            return False
        return True


def _start_kwargs(config: SessionConfig) -> dict[str, Any]:
    return {"custom_parameters": config.custom_parameters} if config.custom_parameters else {}


def _dumps(frame: dict[str, Any]) -> str:
    """Serialise a frame for the wire.

    Separate function so that every send goes through one place. ``Session._send``
    is the chokepoint that uses it, and the trace hook lives there.
    """
    import json

    return json.dumps(frame)


async def run_call(
    url: str,
    frames: Sequence[bytes],
    *,
    config: SessionConfig | None = None,
    identity: StreamIdentity | None = None,
) -> SessionResult:
    """Place one simulated call. The convenience entry point over :class:`Session`."""
    return await Session(url, frames, config=config, identity=identity).run()
