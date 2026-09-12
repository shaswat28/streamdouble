"""Saving a run, and comparing a later run against it.

An absolute gate cannot see a regression inside its own limit. An agent that
answered in 300 ms and now answers in 700 ms has got more than twice as slow
and still passes `--max-first-audio-ms 800`, which is the shape of regression
teams actually care about and the one thing this tool was blind to.

Three rules, each of which makes the check refuse to answer more often than a
naive version would:

**It refuses to compare incomparable runs.** A baseline records which clip was
sent, which impairments were applied and with what seed, and how many runs went
into it. Comparing a series recorded with one clip against a series recorded
with another is not a regression check, it is two different experiments being
subtracted -- and it would report a "regression" the first time someone swaps
their test audio. That exits `EXIT_USAGE`, because it is a mistake in how the
check was set up rather than a finding about the agent.

**A regression must clear two bars, not one.** Percentage alone makes a 40%
worse 5 ms figure a build failure; milliseconds alone lets a 400 ms number
drift to 440 ms forever. Both, so the check fires on changes that a caller
would actually notice. The single exception is a baseline of zero, where no
percentage exists -- there the absolute bar decides alone, because requiring a
percentage that can never be computed would make that metric uncatchable for
the life of the baseline.

**A metric that was missing in either series is incomparable, never an
improvement.** "None is not zero" is the rule the whole project turns on, and
deltas are where it is easiest to violate: an agent that stopped speaking
entirely has `None` where it used to have 400 ms, and subtracting those must
not produce a cheerful "-400 ms, improved". A missing figure on either side is
reported as such, and if it is missing *now* but present before, that is a
regression of the most serious kind.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .aggregate import RunSeries, summarise
from .metrics import SCHEMA_VERSION

__all__ = [
    "DEFAULT_TOLERANCE_MS",
    "DEFAULT_TOLERANCE_PCT",
    "DEFAULT_TOLERANCE_RATIO",
    "BaselineError",
    "Comparison",
    "MetricDelta",
    "compare",
    "fingerprint",
    "load",
    "save",
]

#: A regression must exceed both the percentage and the absolute floor for
#: its unit. See the module docstring.
DEFAULT_TOLERANCE_PCT = 25.0
DEFAULT_TOLERANCE_MS = 50.0

#: The absolute floor for a dimensionless ratio.
#:
#: Separate from the millisecond floor because they are not the same quantity,
#: and conflating them is not a rounding error: ``delivery_ratio`` runs from
#: about 1 to about 12, so a 50 ms floor applied to it meant the metric could
#: never be flagged as a regression however far it moved. The barge-in blind
#: spot this project is known for finding is made of exactly that ratio
#: growing, so silently exempting it would have been the worst possible
#: metric to lose.
#:
#: 0.2 is about a fifth of real-time delivery -- large enough not to fire on
#: noise, small enough to catch an agent that starts front-loading.
DEFAULT_TOLERANCE_RATIO = 0.2

#: Metrics where a bigger number is worse. Everything tracked is a duration
#: except the delivery ratio, where a bigger number means the agent front-loads
#: its audio harder -- also worse, and the thing the barge-in blind spot is
#: made of.
#:
#: `audio_received_ms` is the exception and is deliberately not compared for
#: regression: more audio is not worse, it is a different reply.
_NOT_A_REGRESSION_METRIC = frozenset({"audio_received_ms"})


class BaselineError(Exception):
    """The baseline cannot be used -- unreadable, or not comparable."""


@dataclass(frozen=True)
class MetricDelta:
    """One metric, before and after."""

    name: str
    label: str
    before: float | None
    after: float | None
    tolerance_pct: float
    #: The absolute floor in this metric's own unit, already resolved by
    #: :func:`compare` -- milliseconds for a duration, ratio points for a
    #: ratio. Named without a unit because it carries whichever applies.
    tolerance_abs: float

    @property
    def comparable(self) -> bool:
        """Both sides have a number to compare."""
        return self.before is not None and self.after is not None

    @property
    def lost_measurement(self) -> bool:
        """It was measured before and is not now.

        Its own state rather than a kind of incomparability, because it is the
        most serious outcome a comparison can report: the agent used to answer
        and has stopped. Treating that as "no data, skip it" would let the
        worst regression there is pass quietly.
        """
        return self.before is not None and self.after is None

    @property
    def delta(self) -> float | None:
        if not self.comparable:
            return None
        return self.after - self.before  # type: ignore[operator]

    @property
    def delta_pct(self) -> float | None:
        """Proportional change, or ``None`` when there is no proportion.

        A baseline of zero has no percentage: every increase is infinite. That
        is a real property of the arithmetic, not a missing value -- see
        :attr:`regressed` for why the difference matters.
        """
        if not self.comparable or self.before == 0:
            return None
        return (self.after - self.before) / self.before * 100.0  # type: ignore[operator]

    @property
    def regressed(self) -> bool:
        """Worse by more than both tolerances, or no longer measured at all."""
        if self.name in _NOT_A_REGRESSION_METRIC:
            return False
        if self.lost_measurement:
            return True
        if not self.comparable:
            return False
        delta = self.delta or 0.0
        delta_pct = self.delta_pct

        if delta_pct is None:
            # A baseline of zero. Declining to judge here would make the metric
            # permanently uncatchable: a gap recorded as 0.0 against a fast
            # local stub could climb to half a second against a real agent and
            # never register, because the percentage bar can never be cleared.
            # The absolute bar still means something, so it decides alone.
            return delta > self.tolerance_abs

        return delta > self.tolerance_abs and delta_pct > self.tolerance_pct

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "before": self.before,
            "after": self.after,
            "delta": _round(self.delta),
            "delta_pct": _round(self.delta_pct),
            "comparable": self.comparable,
            "lost_measurement": self.lost_measurement,
            "regressed": self.regressed,
        }


@dataclass(frozen=True)
class Comparison:
    """A whole series compared against a stored one."""

    deltas: tuple[MetricDelta, ...]

    @property
    def regressions(self) -> tuple[MetricDelta, ...]:
        return tuple(delta for delta in self.deltas if delta.regressed)

    @property
    def passed(self) -> bool:
        return not self.regressions

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "regressions": [delta.name for delta in self.regressions],
            "metrics": [delta.to_dict() for delta in self.deltas],
        }


def fingerprint(
    *,
    audio_path: str | Path | None,
    frames: int,
    chaos_seed: int,
    impairments: str,
) -> dict[str, Any]:
    """Describe what is being measured, so a later run can check it matches.

    The clip is identified by a hash of its *contents*, not its filename. Two
    people with different `hello.wav` files would otherwise compare cleanly and
    get a meaningless answer, and the same file renamed would refuse to compare
    for no reason.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "audio_sha256": _file_digest(audio_path) if audio_path else None,
        "frames": frames,
        "chaos_seed": chaos_seed,
        "impairments": impairments,
    }


def save(path: str | Path, series: RunSeries, payloads: list[dict[str, Any]]) -> None:
    """Write a baseline.

    The individual run payloads are stored alongside the summary so a baseline
    can be re-summarised later. If the aggregation ever changes -- a different
    percentile convention, say -- a stored baseline holding only the summary
    would be frozen at the old arithmetic with no way to tell.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "fingerprint": series.fingerprint,
                "summary": series.to_dict(),
                "runs": payloads,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def load(path: str | Path) -> RunSeries:
    """Read a baseline back into a series.

    Re-summarised from the stored run payloads with the same function that
    built it live, rather than trusting the stored summary.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BaselineError(f"could not read the baseline {path}: {exc}") from exc
    except ValueError as exc:
        raise BaselineError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict) or "runs" not in raw:
        raise BaselineError(f"{path} is not a streamdouble baseline")

    stored = raw.get("schema_version")
    if stored != SCHEMA_VERSION:
        raise BaselineError(
            f"{path} was written with schema version {stored!r}, and this is "
            f"version {SCHEMA_VERSION}. The two describe different things, so "
            "comparing them would be meaningless. Record a fresh baseline."
        )

    return summarise(raw["runs"], raw.get("fingerprint"))


def check_comparable(before: RunSeries, after: RunSeries) -> None:
    """Refuse to compare two series that measured different things.

    Raises :class:`BaselineError` naming the field that differs. A different
    clip, a different chaos seed or different impairments make two series
    incommensurable -- subtracting them produces a number, and that number
    means nothing.
    """
    old, new = before.fingerprint, after.fingerprint
    if not old:
        # A baseline written before fingerprints existed, or by hand. Nothing
        # to check against; let it through rather than inventing a mismatch.
        return

    for key, human in (
        ("audio_sha256", "a different audio clip"),
        ("chaos_seed", "a different chaos seed"),
        ("impairments", "different network impairments"),
    ):
        if old.get(key) != new.get(key):
            raise BaselineError(
                f"this run used {human} than the baseline "
                f"({key}: {old.get(key)!r} then, {new.get(key)!r} now). That is a "
                "different experiment rather than a regression -- record a new "
                "baseline for it instead of comparing across."
            )


def compare(
    before: RunSeries,
    after: RunSeries,
    *,
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
    tolerance_ms: float = DEFAULT_TOLERANCE_MS,
    tolerance_ratio: float = DEFAULT_TOLERANCE_RATIO,
) -> Comparison:
    """Compare two series metric by metric, on medians.

    Each metric's absolute floor is chosen by its unit. Passing one number for
    everything is what made ``delivery_ratio`` uncomparable, and a check that
    silently never fires on a metric is worse than one that does not offer it.
    """
    deltas = []
    for series in after.metrics:
        older = before.metric(series.name)
        deltas.append(
            MetricDelta(
                name=series.name,
                label=series.label,
                before=older.median if older is not None else None,
                after=series.median,
                tolerance_pct=tolerance_pct,
                tolerance_abs=tolerance_ratio if series.unit == "ratio" else tolerance_ms,
            )
        )
    return Comparison(deltas=tuple(deltas))


def _file_digest(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()[:16]


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)
