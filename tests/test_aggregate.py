"""Statistics across runs, and the honesty rules they are most likely to break.

Every test here exists because averaging makes a specific dishonesty feel
natural. They are unit tests over synthetic payloads on purpose: the arithmetic
is what is under test, and placing twenty real calls to check a median would be
slow and would add noise to a question that has an exact answer.
"""

from __future__ import annotations

from streamdouble.aggregate import TRACKED, MetricSeries, summarise
from streamdouble.metrics import MIN_SAMPLES_FOR_PERCENTILE


def payload(first_audio: float | None, exit_code: int = 0, **extra) -> dict:
    return {"time_to_first_audio_ms": first_audio, "exit_code": exit_code, **extra}


# ---------------------------------------------------------------------------
# A run that produced nothing is counted, not dropped
# ---------------------------------------------------------------------------


def test_a_missing_run_is_counted_separately_from_a_measured_one():
    """`n` and `measured` are different numbers and both are published.

    Dropping a `None` silently reports the mean of the *successful* runs as
    though it were the mean of all runs -- so an agent that answers nineteen
    times out of twenty and one that answers twenty out of twenty produce the
    same headline figure. That is the single most natural way to lie with this
    module.
    """
    series = summarise([payload(400.0), payload(None, exit_code=2), payload(420.0)])
    metric = series.metric("time_to_first_audio_ms")

    assert metric is not None
    assert metric.n == 3
    assert len(metric.measured) == 2
    assert metric.missing == 1


def test_a_missing_run_does_not_drag_the_median_to_zero():
    """The other direction: coercion.

    Treating a silent run as 0 ms would make the worst outcome a voice agent
    can have improve the headline number.
    """
    series = summarise([payload(400.0), payload(None), payload(420.0)])
    metric = series.metric("time_to_first_audio_ms")

    assert metric.median == 410.0
    assert 0.0 not in metric.measured


def test_every_run_being_silent_gives_no_median_rather_than_zero():
    series = summarise([payload(None), payload(None)])
    metric = series.metric("time_to_first_audio_ms")

    assert metric.median is None
    assert metric.minimum is None
    assert metric.maximum is None
    assert metric.stddev is None
    assert metric.missing == 2


def test_the_raw_values_keep_their_gaps():
    """A reader can see the shape rather than trusting the summary."""
    series = summarise([payload(400.0), payload(None), payload(420.0)])
    metric = series.metric("time_to_first_audio_ms")

    assert metric.values == (400.0, None, 420.0)
    assert metric.to_dict()["values"] == [400.0, None, 420.0]


# ---------------------------------------------------------------------------
# A percentile needs samples
# ---------------------------------------------------------------------------


def test_p95_is_withheld_below_the_sample_floor():
    """A P95 over five runs is the maximum wearing a statistical hat.

    The same rule and the same function as within-call percentiles, reused
    rather than re-derived -- a second implementation of "is this enough data"
    is a second one to get wrong.
    """
    series = summarise([payload(float(400 + i)) for i in range(5)])
    metric = series.metric("time_to_first_audio_ms")

    assert metric.p95 is None
    assert series.percentiles_withheld is True


def test_p95_is_reported_once_there_are_enough_samples():
    series = summarise([payload(float(400 + i)) for i in range(MIN_SAMPLES_FOR_PERCENTILE)])
    metric = series.metric("time_to_first_audio_ms")

    assert metric.p95 is not None
    assert series.percentiles_withheld is False
    # A percentile must lie inside the sample it came from.
    assert metric.minimum <= metric.p95 <= metric.maximum


def test_the_sample_floor_counts_measurements_not_attempts():
    """Twenty calls of which half were silent is ten samples, not twenty.

    The floor exists to stop a percentile being computed from too little data.
    Counting attempts rather than measurements would defeat it exactly when
    the agent is misbehaving.
    """
    values = [payload(float(400 + i)) for i in range(10)] + [payload(None)] * 10
    metric = summarise(values).metric("time_to_first_audio_ms")

    assert len(metric.measured) == 10
    assert metric.p95 is None


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_failures_are_counted_from_exit_codes():
    series = summarise(
        [payload(400.0), payload(None, exit_code=2), payload(400.0, exit_code=3)]
    )
    assert series.n == 3
    assert series.failures == 2


def test_every_tracked_metric_has_a_unit():
    """The unit decides which absolute tolerance applies in a comparison.

    A missing or wrong unit means a metric is compared against a floor in the
    wrong quantity, which is how `delivery_ratio` briefly became impossible to
    flag as a regression: a 50 *millisecond* floor on a ratio that ranges from
    about 1 to about 12.
    """
    series = summarise([payload(400.0)])
    for metric in series.metrics:
        assert metric.unit in {"ms", "ratio"}, f"{metric.name} has unit {metric.unit!r}"

    assert {name for name, _, _ in TRACKED} == {m.name for m in series.metrics}


def test_booleans_are_not_mistaken_for_measurements():
    """`isinstance(True, int)` is true in Python.

    A stray boolean silently becoming 1.0 would be indistinguishable from a
    real measurement of one millisecond.
    """
    metric = summarise([payload(True), payload(400.0)]).metric("time_to_first_audio_ms")

    assert metric.values == (None, 400.0)
    assert metric.median == 400.0


def test_stddev_needs_two_measurements():
    one = MetricSeries(name="x", label="x", values=(5.0,))
    assert one.stddev is None

    two = MetricSeries(name="x", label="x", values=(5.0, 7.0))
    assert two.stddev is not None
