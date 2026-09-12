"""Statistics across several calls.

One call is a sample, not a measurement. Time-to-first-audio moves by tens of
milliseconds between identical runs -- GC, scheduler, whatever the agent's LLM
was doing -- so a single number is an estimate with an error bar nobody drew.
This module draws it.

**This is where the project's honesty rules face their hardest test**, because
averaging is so natural that violating them does not feel like a decision:

*A run where the agent never spoke contributes ``None``.* Dropping it silently
reports the mean of the *successful* runs as though it were the mean of all
runs -- so an agent that answers nineteen times out of twenty and an agent that
answers twenty out of twenty produce the same headline number. Coercing it to
zero, or to the timeout value, is worse in the other direction. Both counts are
published: ``n`` is how many calls were placed, ``measured`` is how many
produced a figure, and they are not the same thing.

*A percentile needs samples.* ``metrics.percentile`` already withholds below
``MIN_SAMPLES_FOR_PERCENTILE``, and that rule is reused here rather than
re-derived. Across-run P95 of first-audio is the genuinely missing metric this
module adds, and it is precisely the one that needs twenty runs to mean
anything -- so ``-n 5`` reports a median and withholds the P95, and says which
it did.

There is no ``--retries``, and there will not be. Repeating a call is for
measuring a distribution; repeating it until it passes is for hiding a flaky
agent, and a tool that offers the second cannot be trusted about the first.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from .metrics import MIN_SAMPLES_FOR_PERCENTILE, percentile

__all__ = ["MetricSeries", "RunSeries", "summarise"]

#: Metrics worth tracking across runs, as (JSON key, human label, unit).
#:
#: Deliberately a short list. Every figure in a report could be aggregated, and
#: a table of forty rows is one nobody reads; these are the ones a regression
#: would show up in first.
#:
#: The unit is not decoration. A regression check needs an absolute floor as
#: well as a percentage, and "50" means something completely different for a
#: duration in milliseconds than for a dimensionless ratio. Applying a 50 ms
#: floor to ``delivery_ratio`` -- which ranges from about 1 to about 12 --
#: meant that metric could never be reported as a regression at all, however
#: far it moved. Found by running the thing and reading the output rather than
#: by writing the test, which is the uncomfortable half of how it was found.
TRACKED = (
    ("time_to_first_audio_ms", "first audio", "ms"),
    ("mean_inbound_gap_ms", "mean gap", "ms"),
    ("p95_inbound_gap_ms", "p95 gap", "ms"),
    ("max_inbound_gap_ms", "max gap", "ms"),
    ("audio_received_ms", "audio received", "ms"),
    ("delivery_ratio", "delivery ratio", "ratio"),
)


@dataclass(frozen=True)
class MetricSeries:
    """One metric's values across several runs."""

    name: str
    label: str
    #: Every run's value, in order, ``None`` where the run produced none. Kept
    #: whole so a reader can see the shape rather than trust the summary.
    values: tuple[float | None, ...]
    #: ``"ms"`` or ``"ratio"``. Decides which absolute tolerance applies when
    #: this metric is compared against a baseline.
    unit: str = "ms"

    @property
    def n(self) -> int:
        """How many calls were placed."""
        return len(self.values)

    @property
    def measured(self) -> tuple[float, ...]:
        """The values that exist. Not the same length as :attr:`values`."""
        return tuple(v for v in self.values if v is not None)

    @property
    def missing(self) -> int:
        """Runs that produced no value at all."""
        return self.n - len(self.measured)

    @property
    def median(self) -> float | None:
        """The middle value, or ``None`` if nothing was measured.

        The median rather than the mean, and not for elegance: one cold start
        in twenty runs drags a mean somewhere no individual call ever was,
        which is exactly the wrong behaviour for a figure a regression check
        will later compare against.
        """
        values = self.measured
        return statistics.median(values) if values else None

    @property
    def minimum(self) -> float | None:
        values = self.measured
        return min(values) if values else None

    @property
    def maximum(self) -> float | None:
        values = self.measured
        return max(values) if values else None

    @property
    def p95(self) -> float | None:
        """95th percentile, or ``None`` below the sample floor.

        Uses the same rule and the same function as within-call percentiles.
        A P95 over five runs is the maximum wearing a statistical hat, and a
        misleading label on a latency figure is how a tool like this starts
        doing harm.
        """
        return percentile(list(self.measured), 0.95)

    @property
    def stddev(self) -> float | None:
        """Spread, or ``None`` with fewer than two measurements.

        Published because a median alone cannot distinguish an agent that is
        reliably 400 ms from one that alternates 100 ms and 700 ms, and those
        are very different agents to be on the phone with.
        """
        values = self.measured
        return statistics.stdev(values) if len(values) >= 2 else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "unit": self.unit,
            "measured": len(self.measured),
            "missing": self.missing,
            "median": _round(self.median),
            "min": _round(self.minimum),
            "max": _round(self.maximum),
            "p95": _round(self.p95),
            "stddev": _round(self.stddev),
            "values": [_round(v) for v in self.values],
        }


@dataclass(frozen=True)
class RunSeries:
    """Several calls, summarised.

    ``fingerprint`` describes what was being measured -- which clip, which
    impairments, which seed. Phase 8's baseline comparison refuses to compare
    two series whose fingerprints differ, because a different clip is a
    different experiment rather than a regression.
    """

    metrics: tuple[MetricSeries, ...]
    #: Exit code of each run, in order.
    exit_codes: tuple[int, ...] = ()
    #: What was being measured. See :func:`streamdouble.baseline.fingerprint`.
    fingerprint: dict[str, Any] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.exit_codes)

    @property
    def failures(self) -> int:
        """Runs that did not exit 0."""
        return sum(1 for code in self.exit_codes if code != 0)

    def metric(self, name: str) -> MetricSeries | None:
        for series in self.metrics:
            if series.name == name:
                return series
        return None

    @property
    def percentiles_withheld(self) -> bool:
        """Whether P95 is being withheld for want of samples.

        Surfaced so the report can say *why* a column is empty. A blank cell
        that might mean "zero" or might mean "not enough data" is the kind of
        ambiguity this project treats as a defect.
        """
        return self.n < MIN_SAMPLES_FOR_PERCENTILE

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "failures": self.failures,
            "exit_codes": list(self.exit_codes),
            "percentiles_withheld": self.percentiles_withheld,
            "fingerprint": dict(self.fingerprint),
            "metrics": {series.name: series.to_dict() for series in self.metrics},
        }


def summarise(
    payloads: list[dict[str, Any]], fingerprint: dict[str, Any] | None = None
) -> RunSeries:
    """Turn several ``CallReport.to_dict()`` payloads into a series.

    Built from the JSON payloads rather than from the reports themselves, so
    that a series can be reconstructed from a saved baseline file with exactly
    the same code that built it live. Two code paths for "summarise these runs"
    would be two paths that drift, which is the lesson Phase 7 was about.
    """
    series = []
    for name, label, unit in TRACKED:
        values = tuple(_as_number(payload.get(name)) for payload in payloads)
        series.append(MetricSeries(name=name, label=label, values=values, unit=unit))

    return RunSeries(
        metrics=tuple(series),
        exit_codes=tuple(int(payload.get("exit_code", 0)) for payload in payloads),
        fingerprint=dict(fingerprint or {}),
    )


def _as_number(value: Any) -> float | None:
    """Keep ``None`` as ``None``; everything else becomes a float or ``None``.

    Booleans are rejected explicitly. ``isinstance(True, int)`` is true in
    Python, and a metric that silently became 1.0 would be indistinguishable
    from a real measurement.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)
