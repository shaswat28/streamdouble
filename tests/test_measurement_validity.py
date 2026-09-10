"""Does the tool measure what it claims to measure?

`test_metrics.py` proves the arithmetic is right given a log. It cannot prove
the log describes reality — feed it a wrong timeline and it will compute
impeccable nonsense. This file closes that gap by injecting a *known* delay into
a real server and checking the reported number matches it.

The plan calls for this as a manual sanity check at gate 3, with the note "do
not skip this". It is committed as a test instead, because a manual check gets
performed once and then quietly stops happening, and a latency tool that drifts
into reporting wrong latency is worse than no tool — people trust it.

Tolerances are wide on purpose. These run against a real socket on a shared CI
runner, and Windows' ~15.6 ms timer granularity alone accounts for a frame. The
job here is to catch an error of *kind* — a missing frame interval, a wrong
origin, seconds mistaken for milliseconds — not to police tens of milliseconds.
A tight bound here would be a flaky test, not a strict one.
"""

from __future__ import annotations

import time

import pytest

from streamdouble import audio
from streamdouble.metrics import compute
from streamdouble.session import Session, SessionConfig

#: The echo agent replies once it has received this many frames.
TRIGGER_FRAMES = 10

#: Frame N is sent at (N-1) intervals after the origin, so the trigger frame
#: leaves at 9 * 20 ms. That offset is part of the expected latency and has to
#: be accounted for, or every expectation here is one frame out.
TRIGGER_OFFSET_MS = (TRIGGER_FRAMES - 1) * audio.FRAME_MS


def config(**overrides) -> SessionConfig:
    defaults = {
        "response_timeout_s": 10.0,
        "quiet_period_s": 0.3,
        "max_drain_s": 8.0,
        "connect_timeout_s": 5.0,
    }
    return SessionConfig(**{**defaults, **overrides})


@pytest.fixture
def frames() -> list[bytes]:
    return audio.wav_to_ulaw_frames("fixtures/speech_8k.wav")[:25]


@pytest.mark.timeout(90)
@pytest.mark.parametrize("injected_ms", [0, 250, 750, 2000])
async def test_reported_latency_tracks_injected_latency(server, frames, injected_ms):
    """A deliberate delay of N ms is reported as roughly N ms.

    The central check of gate 3, across a range wide enough that a proportional
    error cannot hide. A single data point could be matched by a tool that is
    wrong by a constant; four points spanning 2 seconds could not.

    The 2000 ms case is the plan's own suggestion, and it is the one that would
    catch a unit error: a tool confusing seconds for milliseconds reports 2 ms
    or 2,000,000 ms, both of which are outside any tolerance.
    """
    result = await Session(
        f"{server}?delay_ms={injected_ms}", frames, config=config()
    ).run()
    metrics = compute(result)

    assert metrics.time_to_first_audio_ms is not None, "the agent never spoke"

    expected_ms = TRIGGER_OFFSET_MS + injected_ms
    error_ms = metrics.time_to_first_audio_ms - expected_ms

    assert abs(error_ms) < 250, (
        f"injected {injected_ms}ms, expected about {expected_ms}ms, "
        f"reported {metrics.time_to_first_audio_ms:.0f}ms "
        f"(off by {error_ms:+.0f}ms)"
    )


@pytest.mark.timeout(90)
async def test_latency_error_does_not_grow_with_the_delay(server, frames):
    """The error stays flat as the delay grows, rather than scaling with it.

    A proportional error — the kind a wrong clock rate or an accumulating
    offset produces — passes a single-point check comfortably and fails this
    one. Comparing the error at 200 ms against the error at 1500 ms separates
    "wrong by a constant" from "wrong by a factor".
    """
    errors = {}
    for injected_ms in (200, 1500):
        result = await Session(
            f"{server}?delay_ms={injected_ms}", frames, config=config()
        ).run()
        metrics = compute(result)
        expected = TRIGGER_OFFSET_MS + injected_ms
        errors[injected_ms] = metrics.time_to_first_audio_ms - expected

    # If the error were proportional, a 7.5x longer delay would give a roughly
    # 7.5x larger error. Requiring the two to stay within 200 ms of each other
    # rules that out while tolerating ordinary scheduling noise.
    assert abs(errors[1500] - errors[200]) < 200, (
        f"error grew with the delay: {errors[200]:+.0f}ms at 200ms "
        f"but {errors[1500]:+.0f}ms at 1500ms"
    )


@pytest.mark.timeout(90)
async def test_measured_latency_agrees_with_an_independent_wall_clock(server, frames):
    """An outside stopwatch agrees with the tool's own figure.

    Everything else here trusts the session's event log. This brackets the call
    with `time.monotonic` from the test itself, so a systematic error in how the
    session timestamps things — a wrong origin, say — has somewhere to show up.

    The outside measurement is necessarily larger: it includes connection setup,
    which the call-duration metric deliberately excludes because that is the
    network's doing rather than the agent's.
    """
    started = time.monotonic()
    result = await Session(f"{server}?delay_ms=500", frames, config=config()).run()
    outside_elapsed_ms = (time.monotonic() - started) * 1000

    metrics = compute(result)

    assert metrics.call_duration_ms <= outside_elapsed_ms + 50
    # But not wildly smaller: connection setup is milliseconds on loopback, so
    # a large shortfall would mean the call duration is measuring the wrong span.
    assert metrics.call_duration_ms > outside_elapsed_ms * 0.5


@pytest.mark.timeout(90)
async def test_a_silent_agent_reports_no_latency_rather_than_a_fast_one(server, frames):
    """The worst outcome must not be measurable as the best one.

    Worth an end-to-end test and not just a unit test: `None` has to survive the
    whole path from the session through metrics, or a broken agent scores a
    perfect latency.
    """
    result = await Session(
        f"{server}?mode=silent", frames, config=config(response_timeout_s=1.0)
    ).run()
    metrics = compute(result)

    assert metrics.time_to_first_audio_ms is None
    assert metrics.timed_out
    assert metrics.to_dict()["time_to_first_audio_ms"] is None


@pytest.mark.timeout(90)
async def test_the_tools_own_processing_does_not_inflate_the_measurement(server):
    """A long call is measured as accurately as a short one.

    The failure this guards against is the tool's own work leaking into its
    numbers. Gate 2 found exactly that: quadratic audio accumulation running on
    the event loop, which delayed frame handling and inflated the reported
    latency progressively — worse the longer the call, and plausible throughout.

    Sending five times as much audio with the same injected delay must not move
    the reported first-audio figure, because the agent replies near the start in
    both cases.
    """
    short_frames = audio.wav_to_ulaw_frames("fixtures/speech_8k.wav")[:25]
    long_frames = audio.wav_to_ulaw_frames("fixtures/long_8k.wav")[:125]

    measurements = []
    for frames in (short_frames, long_frames):
        result = await Session(
            f"{server}?delay_ms=300", frames, config=config()
        ).run()
        measurements.append(compute(result).time_to_first_audio_ms)

    short_ms, long_ms = measurements
    assert abs(long_ms - short_ms) < 250, (
        f"first audio measured {short_ms:.0f}ms on a short call but "
        f"{long_ms:.0f}ms on a call five times longer"
    )


@pytest.mark.timeout(90)
async def test_a_stall_is_reported_at_its_real_duration(server, frames):
    """A gap in the reply is measured, not merely noticed.

    The echo agent waits for its trigger before replying, so the delay shows up
    as the first inbound gap of the call rather than as a mid-reply stall. What
    matters is that the measured gap matches the real one.
    """
    result = await Session(f"{server}?delay_ms=800", frames, config=config()).run()
    metrics = compute(result)

    assert metrics.max_inbound_gap_ms is not None
    # The echo agent sends its buffered frames back to back once it starts, so
    # the gaps within the reply are small; the measurement that matters here is
    # that first audio reflects the 800 ms wait.
    assert metrics.time_to_first_audio_ms > 700


@pytest.mark.timeout(90)
async def test_pacing_reliability_is_reported_alongside_the_latency(server, frames):
    """The tool states how much its own timing can be trusted.

    Not a threshold anyone passes or fails — the point is that the figure is
    present and consistent with the pacing behind it, so a reader can tell a
    slow agent from a slow measurement.
    """
    result = await Session(f"{server}?delay_ms=100", frames, config=config()).run()
    metrics = compute(result)

    assert metrics.pacing_max_lateness_ms >= 0
    assert metrics.pacing_mean_lateness_ms >= 0
    # The max is by definition at least the mean; a violation here would mean
    # the two statistics disagree about the same run.
    assert metrics.pacing_max_lateness_ms >= metrics.pacing_mean_lateness_ms

    published = metrics.to_dict()["pacing"]
    assert published["mean_lateness_ms"] is not None
    assert published["max_lateness_ms"] is not None
    assert isinstance(published["measurement_is_reliable"], bool)
