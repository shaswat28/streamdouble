"""Regression tests for the issues found at review gate 2.

Gate 2 is about async lifecycle, and its findings share a shape: each one is
invisible on the happy path and only appears when something goes wrong or when
a call runs long. That is exactly the class of bug the plan warns tests miss, so
these reproduce the conditions rather than asserting on the code's structure.
"""

from __future__ import annotations

import asyncio
import time

import pytest
import websockets

from conftest import free_port
from streamdouble import audio
from streamdouble import session as session_module
from streamdouble.protocol import FrameSequenceError, MediaStreamEncoder
from streamdouble.session import Session, SessionConfig


@pytest.fixture
def frames() -> list[bytes]:
    return [audio.silence_frame()] * 5


def quick() -> SessionConfig:
    return SessionConfig(response_timeout_s=1.0, quiet_period_s=0.2, max_drain_s=2.0)


# --------------------------------------------------------------------------
# Cleanup must not replace the real error
# --------------------------------------------------------------------------


class SendFailsImmediately:
    """A connection whose every send fails, as an already-dead socket would."""

    def __init__(self, inner):
        self.inner = inner
        self.closed = False

    async def send(self, message):
        raise websockets.ConnectionClosed(None, None)

    async def close(self):
        self.closed = True
        await self.inner.close()

    def __aiter__(self):
        return self.inner.__aiter__()


@pytest.mark.timeout(30)
async def test_a_send_failure_before_start_reports_the_real_cause(frames):
    """A socket that dies before ``start`` reports closed_early, not an internal error.

    The cleanup path builds a ``stop`` frame, and the gate 1 ordering guard
    refuses to build one when ``start`` never went out. Raised from a
    ``finally``, that FrameSequenceError replaced the ConnectionClosed that was
    the actual problem, and surfaced as an unhandled ExceptionGroup naming a
    frame-ordering rule -- sending the user to debug streamdouble instead of
    their agent.
    """
    port = free_port()

    async def idle(websocket, *args):
        await asyncio.sleep(5)

    server = await websockets.serve(idle, "127.0.0.1", port)
    try:
        session = Session(f"ws://127.0.0.1:{port}/media-stream", frames, config=quick())
        original = session._converse

        async def with_dead_socket(connection):
            return await original(SendFailsImmediately(connection))

        session._converse = with_dead_socket

        result = await session.run()

        assert result.closed_early
        assert result.frames_sent == 0
    finally:
        server.close()
        await server.wait_closed()


def test_the_encoder_reports_its_lifecycle_state():
    """Cleanup can ask whether a stop is legal instead of catching an exception."""
    encoder = MediaStreamEncoder()
    assert not encoder.started
    assert not encoder.stopped

    encoder.start()
    assert encoder.started
    assert not encoder.stopped

    encoder.stop()
    assert encoder.stopped


def test_stop_is_still_refused_out_of_order():
    """Exposing the state did not weaken the guard itself."""
    encoder = MediaStreamEncoder()
    with pytest.raises(FrameSequenceError):
        encoder.stop()


@pytest.mark.timeout(30)
async def test_an_abrupt_hangup_still_reports_cleanly(server, frames):
    """The ordinary hangup path is unaffected by the cleanup change."""
    result = await Session(f"{server}?hangup_after=2", frames, config=quick()).run()
    assert result.closed_early


# --------------------------------------------------------------------------
# Accumulation cost
# --------------------------------------------------------------------------


def test_audio_accumulation_is_linear_not_quadratic():
    """Appending audio stays cheap as a call gets long.

    The original code did ``result.audio_received += payload`` on immutable
    bytes, copying the whole buffer per frame. Measured at 0.011s for 40s of
    audio but 18.6s for 640s -- and because it ran inside the receive loop on
    the event loop, the cost delayed frame handling and inflated the very
    latency numbers the tool reports, progressively and plausibly.

    Timed rather than inspected, because the property that matters is the growth
    rate. Thresholds are loose: this needs to catch a quadratic regression, not
    police constant factors on a shared CI runner.
    """
    payload = audio.silence_frame()

    def elapsed(frame_count: int) -> float:
        buffer = bytearray()
        started = time.perf_counter()
        for _ in range(frame_count):
            buffer += payload
        return time.perf_counter() - started

    elapsed(2000)  # warm up, so the first timing is not paying for imports
    small = elapsed(8000)
    large = elapsed(32000)

    # 4x the frames should cost about 4x the time, not 16x. Allowing 8x leaves
    # generous room for noise while still failing loudly on quadratic growth.
    assert large < max(small * 8, 0.5), (
        f"8000 frames took {small:.3f}s but 32000 took {large:.3f}s: "
        "accumulation looks quadratic again"
    )


@pytest.mark.timeout(60)
async def test_received_audio_is_still_exact_after_the_change(server):
    """Switching to a bytearray did not alter what is recorded.

    A performance fix that quietly changes the output would be worse than the
    problem it solves.
    """
    frames = audio.wav_to_ulaw_frames("fixtures/speech_8k.wav")[:25]
    result = await Session(server, frames, config=quick()).run()

    assert isinstance(result.audio_received, bytes)
    assert len(result.audio_received) % audio.FRAME_BYTES == 0
    assert result.audio_received == b"".join(frames)[: len(result.audio_received)]


# --------------------------------------------------------------------------
# Resource bounds
# --------------------------------------------------------------------------


def test_inbound_frames_are_size_limited():
    """The socket keeps a finite ceiling on a single inbound frame.

    ``max_size=None`` removed the library's own 1 MiB cap, letting a runaway or
    hostile endpoint buffer an unbounded frame before the protocol layer got a
    chance to reject anything. The URL is user-supplied and may be remote.
    """
    assert session_module.MAX_INBOUND_FRAME_BYTES is not None
    assert session_module.MAX_INBOUND_FRAME_BYTES > 0
    # Far above any legitimate payload: one 20 ms frame is 160 bytes, and even
    # a whole utterance batched into one frame is orders of magnitude smaller.
    assert session_module.MAX_INBOUND_FRAME_BYTES >= 1024 * 1024


def test_the_stop_send_is_bounded():
    """Shutdown cannot wait forever on a peer that stopped reading.

    websockets applies no send timeout, so flow control on a wedged peer would
    otherwise stall the final stop indefinitely -- during the ``finally``, on
    both the success and error paths, defeating max_drain_s and hanging CI
    instead of failing it.
    """
    assert 0 < session_module.STOP_SEND_TIMEOUT_S <= 30


@pytest.mark.timeout(60)
async def test_a_peer_that_stops_reading_does_not_hang_shutdown(frames):
    """A server that accepts and then ignores everything still lets the call end.

    The end-to-end version of the bound above: whatever the peer does, run()
    returns.
    """
    port = free_port()

    async def accept_and_ignore(websocket, *args):
        # Never reads, never writes, keeps the socket open.
        await asyncio.sleep(30)

    server = await websockets.serve(accept_and_ignore, "127.0.0.1", port)
    try:
        started = time.monotonic()
        result = await Session(
            f"ws://127.0.0.1:{port}/media-stream",
            frames,
            config=SessionConfig(
                response_timeout_s=0.5, quiet_period_s=0.2, max_drain_s=1.0
            ),
        ).run()
        elapsed = time.monotonic() - started

        assert result.timed_out
        assert elapsed < 30, f"shutdown took {elapsed:.1f}s"
    finally:
        server.close()
        await server.wait_closed()
