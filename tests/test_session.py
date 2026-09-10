"""Tests for the live WebSocket session.

These run against a real uvicorn server on a real socket, not an in-process
transport. That is deliberate and it is the point of this file: the failures
gate 2 is concerned with -- a receive task dying silently, a hang when the peer
disappears, cleanup that does not happen on the error path -- are exactly the
ones an in-process fake cannot reproduce, because it has no socket to close and
no second task to lose.

Every test here has a timeout. A session bug most often manifests as a hang
rather than an exception, and a suite that hangs is worse than one that fails:
it tells you nothing and it blocks CI until someone kills it.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import numpy as np
import pytest

from conftest import free_port
from streamdouble import audio
from streamdouble.session import Session, SessionConfig

#: Nothing here should take anywhere near this long. It exists so that a hang
#: fails the suite quickly instead of stalling it.
TIMEOUT = 30


@pytest.fixture(scope="module")
def frames() -> list[bytes]:
    """Half a second of audio. Long enough to be a real exchange, short enough to be fast."""
    return audio.wav_to_ulaw_frames(FIXTURES / "speech_8k.wav")[:25]


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def quick_config(**overrides) -> SessionConfig:
    """A config tuned for tests: short waits, so failures surface fast."""
    defaults = {
        "response_timeout_s": 5.0,
        "quiet_period_s": 0.3,
        "max_drain_s": 5.0,
        "connect_timeout_s": 5.0,
    }
    return SessionConfig(**{**defaults, **overrides})


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


@pytest.mark.timeout(TIMEOUT)
async def test_a_complete_call_round_trips_audio(server, frames):
    """The whole point of the project, in one test.

    Audio goes out over a real socket, a real server processes it, and the
    audio that comes back is the audio that was sent.
    """
    result = await Session(server, frames, config=quick_config()).run()

    assert result.frames_sent == len(frames)
    assert result.media_frames_received > 0
    assert not result.timed_out
    assert not result.closed_early
    assert result.violations == []

    # The echo agent returns exactly what it received, so the recovered audio
    # must match the source once through the codec.
    expected = audio.g711.decode(b"".join(frames))
    recovered = audio.g711.decode(result.audio_received)
    assert np.array_equal(recovered[: expected.size], expected[: recovered.size])


@pytest.mark.timeout(TIMEOUT)
async def test_the_call_is_paced_in_real_time(server, frames):
    """25 frames take about half a second, not as fast as the socket allows.

    Pacing is the difference between a simulator and a file transfer. An agent's
    voice-activity detection and turn-taking both depend on audio arriving at
    the rate a person speaks; blasting it through makes agents behave in ways
    they never would on a real call.
    """
    started = time.monotonic()
    result = await Session(server, frames, config=quick_config()).run()
    elapsed = time.monotonic() - started

    expected_send_s = (len(frames) - 1) * audio.FRAME_MS / 1000
    assert elapsed >= expected_send_s, "the stream was sent faster than real time"
    assert result.pacing.frames == len(frames)
    # Generous: CI runners are not real-time systems, and Windows' timer
    # granularity alone is most of a frame.
    assert abs(result.pacing.drift_ms) < 250


@pytest.mark.timeout(TIMEOUT)
async def test_measured_latency_matches_injected_latency(server, frames):
    """A deliberate 300 ms delay is reported as roughly 300 ms.

    The check that makes every other latency number believable. The agent waits
    for 10 frames (180 ms of send time) and then sleeps 300 ms, so first audio
    is due at about 480 ms. If this drifts, the tool is measuring something
    other than what it claims.
    """
    result = await Session(
        f"{server}?delay_ms=300", frames, config=quick_config()
    ).run()

    measured_ms = result.time_to_first_audio_s * 1000
    expected_ms = 9 * audio.FRAME_MS + 300
    assert abs(measured_ms - expected_ms) < 150, (
        f"measured {measured_ms:.0f}ms, expected about {expected_ms:.0f}ms"
    )


@pytest.mark.timeout(TIMEOUT)
async def test_custom_parameters_are_delivered(server, frames):
    result = await Session(
        server, frames, config=quick_config(custom_parameters={"tenant": "acme"})
    ).run()
    assert result.media_frames_received > 0


# --------------------------------------------------------------------------
# Marks
# --------------------------------------------------------------------------


@pytest.mark.timeout(TIMEOUT)
async def test_marks_are_echoed_back(server, frames):
    """An agent's mark comes back, which is what unblocks its turn-taking.

    An agent that never receives its mark echo waits forever for audio it
    believes is still playing. A simulator that silently drops marks would make
    every such agent look broken.
    """
    result = await Session(server, frames, config=quick_config()).run()

    assert result.marks_received == ["reply-complete"]
    assert result.marks_echoed == ["reply-complete"]


@pytest.mark.timeout(TIMEOUT)
async def test_a_mark_is_echoed_after_its_audio_would_have_played(server, frames):
    """The echo is delayed by the audio ahead of it, not sent on arrival.

    Twilio returns a mark when the audio queued before it finishes playing. The
    agent sends 10 frames (200 ms) and then a mark, so the echo must lag the
    mark's arrival by roughly that much. Echoing immediately would be simpler
    and would misreport the timing agents use to decide when to listen again.
    """
    result = await Session(server, frames, config=quick_config()).run()

    [received] = result.events_of("mark_received")
    [echoed] = result.events_of("mark_echoed")

    lag_ms = (echoed.at - received.at) * 1000
    assert lag_ms > 100, f"mark was echoed after only {lag_ms:.0f}ms; audio was still playing"


@pytest.mark.timeout(TIMEOUT)
async def test_mark_echo_can_be_disabled(server, frames):
    """Opt out, for agents that do not expect echoes."""
    result = await Session(server, frames, config=quick_config(echo_marks=False)).run()

    assert result.marks_received == ["reply-complete"]
    assert result.marks_echoed == []


@pytest.mark.timeout(TIMEOUT)
async def test_a_clear_drops_marks_waiting_on_discarded_audio(server, frames):
    """Barge-in cancels the marks queued behind the discarded audio.

    When the caller interrupts, Twilio throws away the unplayed buffer. The
    marks scheduled behind that audio never come back, because their audio is
    never going to play. Echoing them anyway would tell the agent an utterance
    finished that the caller actually cut off.
    """
    result = await Session(
        f"{server}?clear_after=15", frames, config=quick_config()
    ).run()

    assert result.clears_received >= 1
    clears = result.events_of("clear_received")
    assert clears, "no clear was recorded"


# --------------------------------------------------------------------------
# Failure modes
# --------------------------------------------------------------------------


@pytest.mark.timeout(TIMEOUT)
async def test_a_silent_agent_times_out_rather_than_hanging(server, frames):
    """No response is a reported outcome, with a bounded wait.

    The most common real failure -- an agent whose transcription never fires --
    and the one where a hang would be least helpful.
    """
    started = time.monotonic()
    result = await Session(
        f"{server}?mode=silent", frames, config=quick_config(response_timeout_s=1.0)
    ).run()
    elapsed = time.monotonic() - started

    assert result.timed_out
    assert result.first_audio_at is None
    assert result.time_to_first_audio_s is None, "no audio must not be reported as 0 ms"
    assert result.media_frames_received == 0
    assert elapsed < 10, f"took {elapsed:.1f}s; the timeout did not bound the wait"


@pytest.mark.timeout(TIMEOUT)
async def test_an_abrupt_hangup_is_reported_not_raised(server, frames):
    """The agent closing mid-stream ends the call cleanly.

    A dropped call is a normal thing to test for, so it must come back as a
    result rather than as a traceback -- and critically, the send loop must not
    keep writing into a socket that is gone.
    """
    result = await Session(
        f"{server}?hangup_after=5", frames, config=quick_config()
    ).run()

    assert result.closed_early
    assert result.frames_sent <= len(frames)


@pytest.mark.timeout(TIMEOUT)
async def test_malformed_frames_are_recorded_and_the_call_continues(server, frames):
    """Three bad frames are all reported, and the call survives them.

    Aborting on the first violation would report one problem where there are
    three, and would hide anything that happens afterwards. The user wants the
    whole picture in one run.
    """
    result = await Session(
        f"{server}?garbage_after=12", frames, config=quick_config()
    ).run()

    codes = {violation.code for violation in result.violations}
    assert codes == {"malformed_json", "missing_stream_sid", "bad_base64"}
    # The call carried on: real audio still arrived after the garbage.
    assert result.media_frames_received > 0


@pytest.mark.timeout(TIMEOUT)
async def test_connecting_to_a_closed_port_fails_clearly(frames):
    """An unreachable endpoint raises OSError rather than hanging or timing out.

    The most common first-run mistake is pointing the tool at a server that is
    not running. It should say so immediately.
    """
    port = free_port()  # nothing is listening here
    session = Session(f"ws://127.0.0.1:{port}/media-stream", frames, config=quick_config())

    with pytest.raises(OSError):
        await session.run()

    assert session.result.events_of("connect_failed")


# --------------------------------------------------------------------------
# Lifecycle and cleanup
# --------------------------------------------------------------------------


@pytest.mark.timeout(TIMEOUT)
async def test_no_tasks_are_left_running_after_a_call(server, frames):
    """Every task the session started has finished by the time run() returns.

    A leaked task keeps a socket open and can log after the call is over. In a
    long CI run they accumulate until something runs out of file descriptors,
    far from the code that caused it.
    """
    before = {task for task in asyncio.all_tasks() if not task.done()}
    await Session(server, frames, config=quick_config()).run()
    await asyncio.sleep(0.1)  # let anything mid-shutdown settle

    after = {task for task in asyncio.all_tasks() if not task.done()}
    assert after - before == set()


@pytest.mark.timeout(TIMEOUT)
async def test_cleanup_still_happens_when_the_agent_disappears(server, frames):
    """The hangup path leaks nothing either.

    Cleanup on the happy path is easy. This is the path where a `finally` is
    usually missing.
    """
    before = {task for task in asyncio.all_tasks() if not task.done()}
    await Session(f"{server}?hangup_after=3", frames, config=quick_config()).run()
    await asyncio.sleep(0.1)

    after = {task for task in asyncio.all_tasks() if not task.done()}
    assert after - before == set()


@pytest.mark.timeout(TIMEOUT)
async def test_concurrent_calls_do_not_interfere(server, frames):
    """Three simultaneous calls each get their own audio back.

    Sessions share no state, and each has its own streamSid. If they did share
    state, this is where it would show -- as audio from one call appearing in
    another's recording, or as a streamSid mismatch violation.
    """
    results = await asyncio.gather(
        *(Session(server, frames, config=quick_config()).run() for _ in range(3))
    )

    sids = {result.identity.stream_sid for result in results}
    assert len(sids) == 3, "sessions shared a streamSid"

    for result in results:
        assert result.violations == []
        assert result.media_frames_received > 0


@pytest.mark.timeout(TIMEOUT)
async def test_events_are_recorded_in_monotonic_order(server, frames):
    """The event log is ordered and uses a monotonic clock.

    Every Phase 3 measurement is derived from this log, so time running
    backwards in it would produce negative latencies reported with confidence.
    """
    result = await Session(server, frames, config=quick_config()).run()

    times = [event.at for event in result.events]
    assert times == sorted(times)

    kinds = [event.kind for event in result.events]
    assert kinds[0] == "connecting"
    assert kinds[-1] == "finished"
    assert "stream_started" in kinds
    assert "sent_stop" in kinds


@pytest.mark.timeout(TIMEOUT)
async def test_an_empty_clip_still_completes_the_handshake(server):
    """Zero frames of audio is a valid, if pointless, call.

    An empty or all-silence input file is a plausible mistake, and it should
    produce a clean "the agent said nothing" rather than an exception from
    somewhere deep in the send loop.
    """
    result = await Session(
        f"{server}?mode=silent", [], config=quick_config(response_timeout_s=0.5)
    ).run()

    assert result.frames_sent == 0
    assert result.timed_out
    assert result.events_of("stream_started")
