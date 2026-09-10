"""Tests for the metrics layer.

Almost everything here is driven by a handwritten event log rather than a real
call. That is the payoff of having the session record facts and this module do
arithmetic on them: timing behaviour that would be impossible to provoke
reliably over a socket -- a 2-second stall, a percentile over 200 samples, an
agent that replies before it should -- becomes a literal list of timestamps.

The one thing a synthetic log cannot prove is that the numbers describe reality.
That is `test_measurement_validity.py`, which injects a known delay into a real
server and checks the tool reports it.
"""

from __future__ import annotations

import json

import pytest

from streamdouble import metrics as metrics_module
from streamdouble.metrics import (
    CONVERSATIONAL_FLOW_MS,
    Metrics,
    Threshold,
    compute,
    evaluate_thresholds,
    percentile,
)
from streamdouble.pacer import PacingStats
from streamdouble.protocol import ProtocolViolation, StreamIdentity
from streamdouble.session import SessionEvent, SessionResult

#: All synthetic logs start here, at an arbitrary non-zero monotonic instant.
#: Non-zero on purpose: a metric that accidentally uses an absolute timestamp
#: instead of a difference looks correct when the origin is 0.
ORIGIN = 1000.0


def build_result(
    *,
    media_at: list[float] | None = None,
    first_audio_at: float | None = None,
    frames_sent: int = 0,
    audio_bytes: int = 0,
    marks_received: list[str] | None = None,
    marks_echoed: list[str] | None = None,
    clears: int = 0,
    violations: list[str] | None = None,
    unknown: list[str] | None = None,
    timed_out: bool = False,
    closed_early: bool = False,
    pacing: PacingStats | None = None,
    finished_at: float | None = None,
) -> SessionResult:
    """Assemble a SessionResult from a handwritten timeline.

    ``media_at`` are absolute monotonic instants of inbound media arrivals. The
    stream always starts at ORIGIN, so a metric that confuses "since stream
    start" with "since epoch" fails loudly.
    """
    media_at = media_at or []
    events = [SessionEvent(at=ORIGIN, kind="stream_started")]
    events += [SessionEvent(at=at, kind="media_received") for at in media_at]
    events.append(SessionEvent(at=finished_at or (media_at[-1] if media_at else ORIGIN),
                               kind="finished"))

    if first_audio_at is None and media_at:
        first_audio_at = media_at[0]

    return SessionResult(
        identity=StreamIdentity(),
        events=events,
        frames_sent=frames_sent,
        media_frames_received=len(media_at),
        audio_received=b"\xff" * audio_bytes,
        marks_received=marks_received or [],
        marks_echoed=marks_echoed or [],
        clears_received=clears,
        unknown_events=unknown or [],
        violations=[ProtocolViolation(code, code) for code in (violations or [])],
        pacing=pacing or PacingStats(),
        started_at=ORIGIN,
        first_audio_at=first_audio_at,
        timed_out=timed_out,
        closed_early=closed_early,
    )


# --------------------------------------------------------------------------
# Time to first audio -- the headline number
# --------------------------------------------------------------------------


def test_time_to_first_audio_is_measured_from_stream_start():
    """The interval is first-audio minus stream-start, not an absolute time.

    ORIGIN is deliberately far from zero, so a metric that returned the raw
    timestamp would be off by 1000 seconds rather than plausibly wrong.
    """
    result = build_result(media_at=[ORIGIN + 0.42])
    assert compute(result).time_to_first_audio_ms == pytest.approx(420.0)


def test_silence_gives_none_not_zero():
    """An agent that never spoke has no measurement, and that is not 0 ms.

    Collapsing them turns the worst outcome into the best one, and a zero here
    would sail through every latency threshold in the tool.
    """
    metrics = compute(build_result(timed_out=True))
    assert metrics.time_to_first_audio_ms is None
    assert metrics.time_to_first_audio_ms != 0


def test_connection_setup_is_excluded_from_the_call_duration():
    """Call duration starts at ``stream_started``, not at the first event.

    Connecting is the network's doing, not the agent's, and folding it in would
    make a figure about the agent vary with DNS.
    """
    result = build_result(media_at=[ORIGIN + 0.5], finished_at=ORIGIN + 1.0)
    # A connection attempt well before the stream began.
    result.events.insert(0, SessionEvent(at=ORIGIN - 5.0, kind="connecting"))

    assert compute(result).call_duration_ms == pytest.approx(1000.0)


# --------------------------------------------------------------------------
# Inbound gaps
# --------------------------------------------------------------------------


def test_gaps_are_differences_between_consecutive_arrivals():
    result = build_result(media_at=[ORIGIN + 1.0, ORIGIN + 1.02, ORIGIN + 1.05])
    gaps = compute(result).inbound_gaps_ms

    assert gaps == pytest.approx([20.0, 30.0])


def test_n_arrivals_produce_n_minus_one_gaps():
    """The off-by-one that a `zip(..., strict=True)` once turned into a crash."""
    for count in (1, 2, 5, 50):
        result = build_result(media_at=[ORIGIN + i * 0.02 for i in range(count)])
        assert len(compute(result).inbound_gaps_ms) == count - 1


def test_a_single_arrival_yields_no_gaps_and_no_statistics():
    """One frame is not enough to describe delivery, so nothing is invented."""
    metrics = compute(build_result(media_at=[ORIGIN + 0.5]))

    assert metrics.inbound_gaps_ms == []
    assert metrics.mean_inbound_gap_ms is None
    assert metrics.max_inbound_gap_ms is None
    assert metrics.stalls == []


def test_stalls_are_gaps_a_listener_would_notice():
    """Only genuinely audible breaks count, not ordinary batching.

    Agents commonly send audio in bursts rather than every 20 ms, so a
    threshold near the frame interval would flag every well-behaved agent and
    the number would stop meaning anything.
    """
    result = build_result(
        media_at=[ORIGIN, ORIGIN + 0.02, ORIGIN + 0.10, ORIGIN + 2.0, ORIGIN + 2.02]
    )
    metrics = compute(result)

    # 20ms, 80ms and 20ms are unremarkable; the 1.9s silence is not.
    assert len(metrics.stalls) == 1
    assert metrics.stalls[0] == pytest.approx(1900.0)
    assert metrics.max_inbound_gap_ms == pytest.approx(1900.0)


def test_batched_delivery_is_not_reported_as_stalling():
    """An agent sending 200 ms bursts is normal, not stalled."""
    arrivals = [ORIGIN + i * 0.2 for i in range(10)]
    assert compute(build_result(media_at=arrivals)).stalls == []


# --------------------------------------------------------------------------
# Percentiles
# --------------------------------------------------------------------------


def test_a_percentile_needs_enough_samples_to_mean_anything():
    """Below the minimum, the answer is None rather than a dressed-up maximum.

    A "P95" over four values is the largest of them wearing a statistical hat,
    and a misleading label on a latency figure is how a tool like this starts
    doing harm.
    """
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.95) is None
    assert percentile([], 0.5) is None

    enough = [float(n) for n in range(metrics_module.MIN_SAMPLES_FOR_PERCENTILE)]
    assert percentile(enough, 0.95) is not None


def test_percentile_interpolates_between_ranks():
    values = [float(n) for n in range(1, 101)]  # 1..100
    assert percentile(values, 0.0) == 1.0
    assert percentile(values, 1.0) == 100.0
    assert percentile(values, 0.5) == pytest.approx(50.5)
    assert percentile(values, 0.95) == pytest.approx(95.05)


def test_percentile_does_not_care_about_input_order():
    ascending = [float(n) for n in range(30)]
    shuffled = ascending[15:] + ascending[:15]
    assert percentile(shuffled, 0.95) == percentile(ascending, 0.95)


@pytest.mark.parametrize("fraction", [-0.1, 1.5])
def test_percentile_rejects_a_fraction_outside_zero_to_one(fraction):
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        percentile([float(n) for n in range(30)], fraction)


def test_p95_is_reported_once_a_call_is_long_enough():
    arrivals = [ORIGIN + i * 0.02 for i in range(60)]
    metrics = compute(build_result(media_at=arrivals))

    assert metrics.p95_inbound_gap_ms is not None
    assert metrics.p95_inbound_gap_ms == pytest.approx(20.0, abs=0.01)


# --------------------------------------------------------------------------
# Measurement honesty
# --------------------------------------------------------------------------


def test_pacing_trouble_marks_the_measurement_unreliable():
    """If the sender could not hold its schedule, it says so.

    Audio that reached the agent late makes every latency derived from it
    overstated. Publishing a confident number off a slipping clock is precisely
    the failure this project treats as worse than having no tool.
    """
    # 1 ms late on average across 100 frames: ordinary scheduling noise.
    good = compute(
        build_result(pacing=PacingStats(frames=100, total_lateness_s=0.1))
    )
    assert good.measurement_is_reliable

    # 10 ms late on average: sustained slippage, half a frame every frame.
    bad = compute(build_result(pacing=PacingStats(frames=100, total_lateness_s=1.0)))
    assert not bad.measurement_is_reliable


def test_one_slow_frame_does_not_condemn_the_whole_call():
    """A single outlier late in a long call leaves the verdict intact.

    Reliability is judged on mean lateness rather than the maximum. Keying it to
    the max meant one unrelated GC pause at frame 2400 of a 3000-frame call
    marked a time-to-first-audio measured around frame 10 as untrustworthy --
    and a warning that fires on healthy runs is one users learn to ignore, so it
    is not there when pacing genuinely degrades.
    """
    metrics = compute(
        build_result(
            pacing=PacingStats(
                frames=3000,
                # One frame 200 ms late, everything else on time.
                total_lateness_s=0.2,
                max_lateness_s=0.2,
                late_frames=1,
            )
        )
    )

    assert metrics.pacing_max_lateness_ms == pytest.approx(200.0)
    assert metrics.measurement_is_reliable, "one hiccup should not condemn 3000 frames"


def test_sustained_slippage_is_still_caught():
    """Widespread lateness fails, even with no dramatic single outlier.

    The complement of the test above: the mean must not be so forgiving that
    genuinely bad pacing passes.
    """
    metrics = compute(
        build_result(
            pacing=PacingStats(
                frames=3000,
                total_lateness_s=24.0,  # 8 ms late on every frame
                max_lateness_s=0.012,
                late_frames=3000,
            )
        )
    )
    assert not metrics.measurement_is_reliable


def test_pacing_statistics_are_carried_through():
    pacing = PacingStats(
        frames=100, late_frames=7, max_lateness_s=0.016, elapsed_s=2.0, scheduled_s=1.98
    )
    metrics = compute(build_result(pacing=pacing))

    assert metrics.pacing_late_frames == 7
    assert metrics.pacing_max_lateness_ms == pytest.approx(16.0)
    assert metrics.pacing_drift_ms == pytest.approx(20.0)


# --------------------------------------------------------------------------
# Conformance passthrough
# --------------------------------------------------------------------------


def test_violations_are_reported_by_code():
    metrics = compute(build_result(violations=["bad_base64", "missing_stream_sid"]))
    assert metrics.violations == ["bad_base64", "missing_stream_sid"]


def test_unknown_events_are_deduplicated_and_sorted():
    """A chatty agent sending the same unknown event 500 times reports it once."""
    metrics = compute(build_result(unknown=["zzz", "aaa", "zzz", "aaa", "mmm"]))
    assert metrics.unknown_events == ["aaa", "mmm", "zzz"]


def test_turn_taking_counts_are_carried_through():
    metrics = compute(
        build_result(marks_received=["a", "b"], marks_echoed=["a"], clears=3)
    )
    assert metrics.marks_received == 2
    assert metrics.marks_echoed == 1
    assert metrics.clears_received == 3


def test_audio_durations_are_derived_from_byte_counts():
    """8000 mu-law bytes is one second, in both directions."""
    metrics = compute(build_result(frames_sent=50, audio_bytes=8000))

    assert metrics.audio_sent_ms == pytest.approx(1000.0)  # 50 frames x 20 ms
    assert metrics.audio_received_ms == pytest.approx(1000.0)


# --------------------------------------------------------------------------
# Purity
# --------------------------------------------------------------------------


def test_compute_is_deterministic():
    """The same log always produces the same numbers.

    compute() reads no clock, which is what makes every figure reproducible
    from data and traceable back to the events behind it.
    """
    result = build_result(media_at=[ORIGIN + 0.5, ORIGIN + 0.52], frames_sent=25)
    assert compute(result).to_dict() == compute(result).to_dict()


def test_compute_does_not_mutate_the_result():
    result = build_result(media_at=[ORIGIN + 0.5], frames_sent=10)
    before = (len(result.events), result.frames_sent, result.audio_received)

    compute(result)

    assert (len(result.events), result.frames_sent, result.audio_received) == before


# --------------------------------------------------------------------------
# JSON output
# --------------------------------------------------------------------------


def test_to_dict_is_json_serialisable():
    metrics = compute(
        build_result(
            media_at=[ORIGIN + 0.5, ORIGIN + 0.52],
            frames_sent=25,
            violations=["bad_base64"],
            unknown=["somethingNew"],
        )
    )
    reloaded = json.loads(json.dumps(metrics.to_dict()))
    assert reloaded["media_frames_received"] == 2


def test_json_keeps_none_as_null_not_zero():
    """Silence survives serialisation as null.

    JSON is what a CI pipeline actually reads, so the None-is-not-zero rule has
    to hold here too or it holds nowhere that matters.
    """
    payload = compute(build_result(timed_out=True)).to_dict()
    assert payload["time_to_first_audio_ms"] is None
    assert json.loads(json.dumps(payload))["time_to_first_audio_ms"] is None


def test_json_omits_the_unbounded_gap_list():
    """Summary statistics travel; the raw per-frame list does not.

    The gap list grows with call length, and a CI artefact that grows without
    limit stops being read.
    """
    arrivals = [ORIGIN + i * 0.02 for i in range(500)]
    payload = compute(build_result(media_at=arrivals)).to_dict()

    assert "inbound_gaps_ms" not in payload
    assert payload["mean_inbound_gap_ms"] is not None
    assert len(json.dumps(payload)) < 2000


def test_json_reports_measurement_reliability():
    payload = compute(
        build_result(pacing=PacingStats(frames=10, total_lateness_s=0.5))
    ).to_dict()
    assert payload["pacing"]["measurement_is_reliable"] is False
    assert payload["pacing"]["mean_lateness_ms"] == pytest.approx(50.0)


# --------------------------------------------------------------------------
# Thresholds
# --------------------------------------------------------------------------


def latency_threshold(limit: float = CONVERSATIONAL_FLOW_MS, **kwargs) -> Threshold:
    return Threshold(
        name="first audio", limit=limit, attribute="time_to_first_audio_ms", **kwargs
    )


def test_a_value_under_the_limit_passes():
    metrics = Metrics(time_to_first_audio_ms=400)
    [outcome] = evaluate_thresholds(metrics, [latency_threshold()])
    assert outcome.passed


def test_a_value_over_the_limit_fails():
    metrics = Metrics(time_to_first_audio_ms=1200)
    [outcome] = evaluate_thresholds(metrics, [latency_threshold()])
    assert not outcome.passed


def test_the_limit_itself_passes():
    """The boundary is inclusive, so a threshold of 800 accepts exactly 800."""
    metrics = Metrics(time_to_first_audio_ms=800)
    [outcome] = evaluate_thresholds(metrics, [latency_threshold(800)])
    assert outcome.passed


def test_a_missing_measurement_fails_by_default():
    """An agent that never spoke has not met an 800 ms target.

    Treating "no data" as a pass is how a completely broken agent slips through
    a green build -- the exact outcome a CI gate exists to prevent.
    """
    metrics = Metrics(time_to_first_audio_ms=None)
    [outcome] = evaluate_thresholds(metrics, [latency_threshold()])

    assert not outcome.passed
    assert outcome.value is None


def test_a_missing_measurement_can_be_allowed_explicitly():
    metrics = Metrics(time_to_first_audio_ms=None)
    [outcome] = evaluate_thresholds(metrics, [latency_threshold(missing_fails=False)])
    assert outcome.passed


def test_threshold_descriptions_are_readable():
    passing, failing, missing = evaluate_thresholds(
        Metrics(time_to_first_audio_ms=400), [latency_threshold()]
    ), evaluate_thresholds(
        Metrics(time_to_first_audio_ms=1200), [latency_threshold()]
    ), evaluate_thresholds(
        Metrics(time_to_first_audio_ms=None), [latency_threshold()]
    )

    assert "ok" in passing[0].describe()
    assert "FAIL" in failing[0].describe()
    assert "not measured" in missing[0].describe()
    assert "0 ms" not in missing[0].describe()


def test_several_thresholds_are_evaluated_independently():
    metrics = Metrics(time_to_first_audio_ms=400, max_inbound_gap_ms=900)
    outcomes = evaluate_thresholds(
        metrics,
        [
            latency_threshold(),
            Threshold(name="max gap", limit=500, attribute="max_inbound_gap_ms"),
        ],
    )

    assert [outcome.passed for outcome in outcomes] == [True, False]
