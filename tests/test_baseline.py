"""Saving a baseline, and refusing to answer when a comparison is meaningless.

The tests that matter here are the refusals. A regression check that always
produces a verdict is easy to write and produces a false regression the first
time someone swaps their test clip -- after which people add `continue-on-error`
and the check stops meaning anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from streamdouble import baseline as baseline_module
from streamdouble.aggregate import RunSeries, summarise
from streamdouble.baseline import BaselineError, compare, fingerprint, load, save
from streamdouble.metrics import SCHEMA_VERSION


def payload(first_audio: float | None, ratio: float = 1.1, exit_code: int = 0) -> dict:
    return {
        "time_to_first_audio_ms": first_audio,
        "delivery_ratio": ratio,
        "exit_code": exit_code,
    }


def series_of(values, fp=None, **kwargs) -> RunSeries:
    return summarise([payload(v, **kwargs) for v in values], fp)


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_a_baseline_round_trips(tmp_path: Path, speech_8k_path):
    fp = fingerprint(audio_path=speech_8k_path, frames=100, chaos_seed=0, impairments="none")
    payloads = [payload(400.0), payload(420.0)]
    original = summarise(payloads, fp)

    path = tmp_path / "base.json"
    save(path, original, payloads)
    restored = load(path)

    assert restored.fingerprint == fp
    assert restored.metric("time_to_first_audio_ms").median == original.metric(
        "time_to_first_audio_ms"
    ).median


def test_a_baseline_is_resummarised_from_the_stored_runs(tmp_path: Path):
    """Not trusted from the stored summary.

    If the aggregation ever changes -- a different percentile convention, say
    -- a baseline holding only a summary would be frozen at the old arithmetic
    with nothing to say so.
    """
    payloads = [payload(400.0), payload(420.0)]
    path = tmp_path / "base.json"
    save(path, summarise(payloads, {}), payloads)

    raw = json.loads(path.read_text())
    assert raw["runs"] == payloads, "the individual runs must be stored, not just the summary"

    # Corrupt the stored summary; the loaded series should ignore it.
    raw["summary"]["metrics"]["time_to_first_audio_ms"]["median"] = 99999.0
    path.write_text(json.dumps(raw))

    assert load(path).metric("time_to_first_audio_ms").median == 410.0


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_different_clip_is_refused_rather_than_compared(tmp_path: Path, fixture_dir: Path):
    """A different clip is a different experiment, not a regression."""
    speech = fingerprint(
        audio_path=fixture_dir / "speech_8k.wav", frames=100, chaos_seed=0, impairments="none"
    )
    tone = fingerprint(
        audio_path=fixture_dir / "tone_440_8k.wav", frames=100, chaos_seed=0, impairments="none"
    )
    assert speech["audio_sha256"] != tone["audio_sha256"]

    with pytest.raises(BaselineError, match="different audio clip"):
        baseline_module.check_comparable(series_of([400.0], speech), series_of([400.0], tone))


def test_a_different_chaos_seed_is_refused(tmp_path: Path, speech_8k_path):
    one = fingerprint(audio_path=speech_8k_path, frames=100, chaos_seed=1, impairments="5% loss")
    two = fingerprint(audio_path=speech_8k_path, frames=100, chaos_seed=2, impairments="5% loss")

    with pytest.raises(BaselineError, match="different chaos seed"):
        baseline_module.check_comparable(series_of([400.0], one), series_of([400.0], two))


def test_different_impairments_are_refused(speech_8k_path):
    clean = fingerprint(audio_path=speech_8k_path, frames=100, chaos_seed=0, impairments="none")
    lossy = fingerprint(
        audio_path=speech_8k_path, frames=100, chaos_seed=0, impairments="5% loss"
    )

    with pytest.raises(BaselineError, match="different network impairments"):
        baseline_module.check_comparable(series_of([400.0], clean), series_of([400.0], lossy))


def test_a_mismatched_schema_version_is_refused(tmp_path: Path):
    path = tmp_path / "base.json"
    path.write_text(json.dumps({"schema_version": SCHEMA_VERSION + 99, "runs": []}))

    with pytest.raises(BaselineError, match="schema version"):
        load(path)


def test_a_file_that_is_not_a_baseline_is_refused(tmp_path: Path):
    path = tmp_path / "nope.json"
    path.write_text(json.dumps({"hello": "world"}))

    with pytest.raises(BaselineError, match="not a streamdouble baseline"):
        load(path)


def test_a_baseline_without_a_fingerprint_is_allowed_through():
    """Nothing to check against is not the same as a mismatch.

    A baseline written by hand, or by a version before fingerprints existed,
    should not be rejected for failing a check it never had the data for.
    """
    baseline_module.check_comparable(series_of([400.0], {}), series_of([400.0], {"x": 1}))


# ---------------------------------------------------------------------------
# What counts as a regression
# ---------------------------------------------------------------------------


def test_a_real_regression_is_caught():
    before = series_of([400.0, 400.0])
    after = series_of([800.0, 800.0])

    result = compare(before, after)
    assert not result.passed
    assert "time_to_first_audio_ms" in [d.name for d in result.regressions]


def test_a_large_percentage_on_a_tiny_number_is_not_a_regression():
    """Both bars, not either.

    5 ms becoming 8 ms is 60% worse and nobody can hear it. A check that fires
    on that trains people to ignore it.
    """
    result = compare(series_of([5.0]), series_of([8.0]))
    assert result.passed


def test_a_large_absolute_change_on_a_big_number_is_not_a_regression():
    """The mirror case: 2000 ms becoming 2040 ms is 40 ms and 2%."""
    result = compare(series_of([2000.0]), series_of([2040.0]))
    assert result.passed


def test_an_improvement_is_never_a_regression():
    result = compare(series_of([800.0]), series_of([200.0]))
    assert result.passed
    delta = next(d for d in result.deltas if d.name == "time_to_first_audio_ms")
    assert delta.delta < 0


def test_losing_a_measurement_entirely_is_the_worst_regression():
    """`None` where there used to be a number is not an improvement.

    This is the rule the whole project turns on, applied to deltas -- where it
    is easiest to get wrong, because subtracting a missing value is so easy to
    write as a very good result.
    """
    before = series_of([400.0, 400.0])
    after = series_of([None, None])

    result = compare(before, after)
    delta = next(d for d in result.deltas if d.name == "time_to_first_audio_ms")

    assert delta.lost_measurement is True
    assert delta.regressed is True
    assert delta.delta is None, "there is no delta to compute, and none must be invented"
    assert not result.passed


def test_a_metric_missing_from_the_baseline_is_not_a_regression():
    """Missing *before* is a gap in knowledge, not evidence of harm."""
    before = series_of([None])
    after = series_of([400.0])

    delta = next(
        d for d in compare(before, after).deltas if d.name == "time_to_first_audio_ms"
    )
    assert delta.comparable is False
    assert delta.lost_measurement is False
    assert delta.regressed is False


def test_more_audio_is_not_a_regression():
    """A longer reply is a different reply, not a worse one."""
    before = summarise([{"audio_received_ms": 1000.0, "exit_code": 0}])
    after = summarise([{"audio_received_ms": 9000.0, "exit_code": 0}])

    delta = next(d for d in compare(before, after).deltas if d.name == "audio_received_ms")
    assert delta.regressed is False


def test_the_delivery_ratio_is_compared_against_a_ratio_tolerance():
    """The barge-in metric must be able to regress at all.

    `delivery_ratio` runs from about 1 to about 12. Comparing it against the
    50 *millisecond* absolute floor meant it could never be flagged however far
    it moved -- and an agent whose delivery ratio is climbing is precisely the
    barge-in blind spot this project is known for finding. Silently exempting
    it would have been the worst possible metric to lose.
    """
    before = summarise([{"delivery_ratio": 1.1, "exit_code": 0}])
    after = summarise([{"delivery_ratio": 11.0, "exit_code": 0}])

    delta = next(d for d in compare(before, after).deltas if d.name == "delivery_ratio")
    assert delta.regressed is True, "a tenfold delivery ratio must register as a regression"
