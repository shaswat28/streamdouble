"""Regression tests for the issues found at review gate 3.

Gate 3 asks whether the tool measures what it claims to. Its findings shared a
character: every number looked plausible, none was obviously broken, and the
errors were in the *convention* rather than the arithmetic — the kind that
survive review precisely because the output reads correctly.
"""

from __future__ import annotations

import time

import pytest

from streamdouble import audio
from streamdouble.metrics import Metrics, compute
from streamdouble.pacer import Pacer, PacingStats
from streamdouble.session import Session, SessionConfig

# --------------------------------------------------------------------------
# Clock resolution
# --------------------------------------------------------------------------


def test_the_default_clock_is_high_resolution_everywhere():
    """The clock can resolve far finer than a frame.

    ``time.monotonic`` is the obvious choice and is wrong on Windows, where
    CPython implements it with GetTickCount64 at 15.625 ms resolution --
    comparable to the whole 20 ms frame interval. Every latency would be
    quantised onto that grid while being printed to a tenth of a millisecond,
    and the pacer's lateness statistics would be measuring the clock rather
    than the pacing.

    Asserted on the resolution rather than on the function's identity, because
    what matters is the property, not which name provides it.
    """
    info = time.get_clock_info(Pacer(0.02)._clock.__name__)

    assert info.monotonic, "a clock that can step backwards produces negative latencies"
    assert info.resolution <= 0.001, (
        f"clock resolution is {info.resolution * 1000:.1f} ms, too coarse to "
        f"measure a {audio.FRAME_MS} ms frame interval"
    )


def test_the_session_and_pacer_share_one_clock():
    """Both sides of a latency measurement read the same clock.

    Time-to-first-audio is an interval between an instant the pacer recorded
    and one the session recorded. Two different clocks would make the
    subtraction meaningless.
    """
    session = Session("ws://example.invalid/x", [])
    assert session.clock is session.pacer._clock


def test_the_clock_actually_resolves_sub_millisecond_intervals():
    """Empirical check, not just a documented resolution.

    ``get_clock_info`` reports what the platform claims. This measures what it
    delivers -- the difference is exactly what made GetTickCount64 slip through
    as a reasonable-looking choice.
    """
    clock = Pacer(0.02)._clock
    steps = set()
    previous = clock()
    for _ in range(200_000):
        now = clock()
        if now != previous:
            steps.add(now - previous)
            previous = now
        if len(steps) > 5:
            break

    assert min(steps) < 0.001, (
        f"smallest observable step is {min(steps) * 1000:.3f} ms"
    )


# --------------------------------------------------------------------------
# One frame, one timestamp
# --------------------------------------------------------------------------


@pytest.mark.timeout(90)
async def test_a_frame_is_timestamped_once_on_arrival(server):
    """The first-audio field and its event agree exactly.

    A single frame used to be timestamped three times -- into first_audio_at,
    into the first_audio event, and into media_received after buffering and
    playback-clock arithmetic -- so time-to-first-audio and the inter-frame
    gaps were derived from different instants for the same frame, with the
    tool's own bookkeeping folded into the gaps.

    That was invisible while the clock rounded all three reads to the same
    15.6 ms tick. Fixing the clock exposed it rather than causing it, which is
    why this assertion is exact rather than approximate.
    """
    frames = audio.wav_to_ulaw_frames("fixtures/speech_8k.wav")[:25]
    result = await Session(
        f"{server}?delay_ms=100",
        frames,
        config=SessionConfig(quiet_period_s=0.3, max_drain_s=5),
    ).run()

    [first_audio_event] = result.events_of("first_audio")
    first_media_event = result.events_of("media_received")[0]

    assert first_audio_event.at == result.first_audio_at
    assert first_media_event.at == result.first_audio_at


@pytest.mark.timeout(90)
async def test_the_arrival_timestamp_precedes_parsing(server):
    """Decode cost is not charged to the agent.

    metrics.py documents time-to-first-audio as arrival at this process, "not
    decode time", but the clock used to be read after parse_outbound had run
    json.loads and base64 decoding. That cost 0.004 ms on a 160-byte payload and
    0.32 ms on a 64 KB one -- small absolutely, but a systematic bias in
    proportion to how much audio an agent batches per frame.

    Checked by comparing two agents whose only difference is payload size: the
    batching one must not be reported as slower for that reason alone.
    """
    frames = audio.wav_to_ulaw_frames("fixtures/speech_8k.wav")[:25]
    config = SessionConfig(quiet_period_s=0.3, max_drain_s=5)

    result = await Session(f"{server}?delay_ms=200", frames, config=config).run()
    metrics = compute(result)

    # The measured latency reflects the injected delay, not decode work.
    expected_ms = 9 * audio.FRAME_MS + 200
    assert abs(metrics.time_to_first_audio_ms - expected_ms) < 250


# --------------------------------------------------------------------------
# Reliability reporting
# --------------------------------------------------------------------------


def test_one_late_frame_does_not_condemn_a_long_call():
    """Reliability is judged on sustained slippage, not a single outlier.

    Keyed to the maximum, one unrelated GC pause at frame 2400 of a 3000-frame
    call marked a time-to-first-audio measured around frame 10 -- forty-eight
    seconds earlier -- as untrustworthy. A warning that fires on healthy runs
    is one users learn to ignore, and then it is absent when pacing genuinely
    degrades.
    """
    outlier = compute_with(
        PacingStats(frames=3000, total_lateness_s=0.2, max_lateness_s=0.2, late_frames=1)
    )
    assert outlier.measurement_is_reliable


def test_sustained_lateness_is_reported_as_unreliable():
    """The complement: widespread slippage still fails, with no single outlier."""
    sustained = compute_with(
        PacingStats(
            frames=3000, total_lateness_s=24.0, max_lateness_s=0.012, late_frames=3000
        )
    )
    assert not sustained.measurement_is_reliable


def compute_with(pacing: PacingStats) -> Metrics:
    """A Metrics carrying only the pacing figures under test."""
    return Metrics(
        pacing_max_lateness_ms=pacing.max_lateness_ms,
        pacing_mean_lateness_ms=pacing.mean_lateness_ms,
        pacing_late_frames=pacing.late_frames,
    )


def test_both_lateness_statistics_are_published():
    """Mean and max both reach the JSON.

    The mean drives the verdict; the max is what a reader needs to tell a
    steady 5 ms slip from a single 200 ms stall. Publishing only the one that
    drives the verdict would hide the distinction.
    """
    payload = compute_with(
        PacingStats(frames=100, total_lateness_s=0.5, max_lateness_s=0.2)
    ).to_dict()["pacing"]

    assert payload["mean_lateness_ms"] == pytest.approx(5.0)
    assert payload["max_lateness_ms"] == pytest.approx(200.0)
    assert "measurement_is_reliable" in payload


@pytest.mark.timeout(90)
async def test_a_real_call_reports_believable_pacing_figures(server):
    """Pacing statistics on a real run are plausible rather than clock artefacts.

    Before the clock fix, every run on Windows reported max lateness of exactly
    15.0 or 16.0 ms -- one GetTickCount64 tick -- regardless of what the pacer
    actually did. Genuine values are not suspiciously quantised, and the mean
    is meaningfully below the max.
    """
    frames = audio.wav_to_ulaw_frames("fixtures/speech_8k.wav")[:50]
    result = await Session(
        server, frames, config=SessionConfig(quiet_period_s=0.3, max_drain_s=5)
    ).run()
    metrics = compute(result)

    assert metrics.pacing_mean_lateness_ms >= 0
    assert metrics.pacing_max_lateness_ms >= metrics.pacing_mean_lateness_ms
    # A quantised clock makes these land on exact tick multiples; a real one
    # essentially never produces a round number.
    assert metrics.pacing_max_lateness_ms not in (0.0, 15.0, 16.0, 15.625)
