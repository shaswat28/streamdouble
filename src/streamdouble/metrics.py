"""Latency and conformance metrics, derived from a session's event log.

A latency tool that reports wrong latency is worse than no tool, because people
will trust it. Three decisions here follow from that, and each one costs
something:

**Everything is derived from the recorded event log, not measured inline.**
The session records timestamped facts; this module does arithmetic on them. That
makes every figure reproducible from data, testable against a synthetic log with
no socket in sight, and traceable -- a surprising number can be walked back to
the events that produced it instead of being taken on trust.

**Nothing is inferred that was not observed.** An agent that never spoke has a
time-to-first-audio of ``None``, not zero. A percentile over three samples is
not reported as a percentile. Where a figure cannot be computed honestly, the
answer is its absence, because a plausible wrong number does more damage than a
missing one.

**The tool's own overhead is stated, not hidden.** Pacing lateness is reported
alongside the latency it could have distorted, so a reader can tell a slow agent
from a slow measurement. Suppressing that would make the numbers look cleaner
and mean less.

A note on what "time to first audio" means here: it is the interval from the
instant the stream started -- the pacer's origin, when frame 0 was due -- to the
arrival of the first byte of the agent's audio at this process. It is not decode
time, and it does not include however long the audio then takes to play. That
choice is stated because both alternatives are defensible and differ by a full
frame, and an unstated convention is how a 20 ms error survives review.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from . import audio
from .session import SessionResult

__all__ = [
    "SCHEMA_VERSION",
    "Metrics",
    "Threshold",
    "ThresholdResult",
    "compute",
    "evaluate_thresholds",
]

#: The widely cited threshold for conversational flow: a reply that begins
#: within about 800 ms feels like a conversation rather than a wait.
#:
#: The provenance of these two numbers is different, and worth keeping straight
#: rather than blurring into one confident-sounding claim:
#:
#: * **200 ms is research.** Stivers et al., "Universals and cultural variation
#:   in turn-taking in conversation", PNAS 106(26), 2009
#:   (https://www.pnas.org/doi/10.1073/pnas.0903616106) sampled ten
#:   typologically diverse languages and found a modal between-turn offset of
#:   0-200 ms, with cross-language means falling inside a 250 ms band. Human
#:   turn-taking really is that fast, and really is that consistent.
#: * **800 ms is an industry rule of thumb**, not a finding. It is where voice
#:   AI practitioners report users start noticing a delay, with 300-500 ms
#:   considered comfortable and 1500 ms described as broken. It is a reasonable
#:   default gate, but it is convention, and this docstring says so rather than
#:   dressing it up as the PNAS result.
#:
#: Note that P95 matters far more than the mean: an agent that is fast on
#: average and occasionally takes three seconds feels broken, not fast.
CONVERSATIONAL_FLOW_MS = 800

#: Fewest samples before a percentile is worth reporting. Below this, the
#: "P95" of a handful of values is just the maximum wearing a statistical hat.
MIN_SAMPLES_FOR_PERCENTILE = 20

#: Version of the ``--json`` payload's shape.
#:
#: Shipped before anything consumes it, deliberately. Phase 8 stores a run as a
#: baseline and compares a later run against it, and the comparison has to know
#: whether the two describe the same thing. Adding this field *after* baselines
#: exist in the wild is the release where you discover that the files you need
#: to interpret are the ones that do not say what they are.
#:
#: Bump it when a field changes meaning or disappears. Adding a field is not a
#: bump: a reader that ignores unknown keys is unaffected, and treating every
#: addition as breaking trains people to ignore the number.
SCHEMA_VERSION = 1


def percentile(values: list[float], fraction: float) -> float | None:
    """Linear-interpolated percentile, or None if there is not enough data.

    Returns ``None`` rather than a number when the sample is too small. A P95
    computed from four values is the maximum with a misleading label, and a
    misleading label on a latency figure is precisely how a tool like this
    starts doing harm.
    """
    if len(values) < MIN_SAMPLES_FOR_PERCENTILE:
        return None
    if not 0 <= fraction <= 1:
        raise ValueError(f"fraction must be in [0, 1], got {fraction}")

    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


@dataclass
class Metrics:
    """Everything measurable about one call.

    Durations are milliseconds. ``None`` means "not observed", never zero.
    """

    # --- the headline ------------------------------------------------------
    #: Stream start to the first byte of agent audio. None if the agent never spoke.
    time_to_first_audio_ms: float | None = None

    # --- what was exchanged ------------------------------------------------
    frames_sent: int = 0
    media_frames_received: int = 0
    audio_sent_ms: float = 0.0
    audio_received_ms: float = 0.0
    call_duration_ms: float = 0.0

    # --- how the reply was delivered ---------------------------------------
    #: Gaps between consecutive inbound media frames, in milliseconds.
    inbound_gaps_ms: list[float] = field(default_factory=list)
    mean_inbound_gap_ms: float | None = None
    p95_inbound_gap_ms: float | None = None
    max_inbound_gap_ms: float | None = None
    #: Silences long enough to be audible as a stall mid-reply.
    stalls: list[float] = field(default_factory=list)

    # --- how the agent paces its own speech --------------------------------
    #: Wall-clock time the agent took to send its audio, in milliseconds.
    delivery_duration_ms: float | None = None
    #: Audio duration divided by the time taken to send it. 1.0 means the agent
    #: streams at the rate the caller hears; 10.0 means it dumps ten seconds of
    #: speech in one.
    delivery_ratio: float | None = None
    #: How long the caller is still listening after the agent has stopped
    #: sending -- audio sitting in Twilio's playback buffer.
    playback_tail_ms: float | None = None

    # --- turn taking -------------------------------------------------------
    marks_received: int = 0
    marks_echoed: int = 0
    clears_received: int = 0

    # --- conformance -------------------------------------------------------
    violations: list[str] = field(default_factory=list)
    unknown_events: list[str] = field(default_factory=list)
    timed_out: bool = False
    closed_early: bool = False

    # --- the tool's own honesty --------------------------------------------
    #: How far pacing diverged from the schedule. Reported because it bounds how
    #: much the figures above can be trusted.
    pacing_drift_ms: float = 0.0
    pacing_late_frames: int = 0
    pacing_max_lateness_ms: float = 0.0
    pacing_mean_lateness_ms: float = 0.0

    @property
    def barge_in_blind_spot_ms(self) -> float | None:
        """How long the agent is deaf to interruption, if it tracks speech by sending.

        Twilio buffers outbound audio and plays it at the caller's pace, which
        is why ``mark`` exists at all. An agent that streams TTS faster than
        real time therefore finishes *sending* long before the caller finishes
        *hearing*, and any state it derives from send-completion -- most
        commonly an ``is_speaking`` flag guarding barge-in -- goes false while
        the caller is still being spoken to.

        For that whole window the caller cannot interrupt: they talk, the agent
        does not stop, because as far as it knows it is not talking. This is the
        same number as :attr:`playback_tail_ms`, named for what it costs.

        An agent that gates barge-in on the ``mark`` echo instead does not have
        this problem, and the figure is harmless for it -- so this is a
        diagnostic to interpret, not a verdict.
        """
        return self.playback_tail_ms

    @property
    def measurement_is_reliable(self) -> bool:
        """Whether pacing was good enough for the latency figures to mean much.

        If the sender could not hold its schedule, audio reached the agent late,
        and every latency derived from it is correspondingly overstated. Better
        to say so than to publish a confident number built on a slipping clock.

        Judged on *mean* lateness, not the maximum. The maximum is a single
        worst frame across the whole call, so on a 60-second run one unrelated
        GC pause at frame 2400 would condemn a time-to-first-audio measured
        around frame 10, forty-eight seconds earlier and entirely unaffected. A
        warning that fires on healthy runs is one users learn to ignore, and
        then it is not there when pacing genuinely degrades.

        The threshold is a quarter of a frame rather than a whole one, because
        mean lateness is a much tighter statistic than the max: averaging 5 ms
        late across every frame is real, sustained slippage, not a hiccup.
        """
        return self.pacing_mean_lateness_ms < audio.FRAME_MS / 4

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serialisable summary for CI consumption.

        The full gap list is deliberately excluded: it is unbounded in call
        length, and a CI artefact that grows without limit stops being read.
        Its summary statistics are here instead.
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "time_to_first_audio_ms": _round(self.time_to_first_audio_ms),
            "frames_sent": self.frames_sent,
            "media_frames_received": self.media_frames_received,
            "audio_sent_ms": _round(self.audio_sent_ms),
            "audio_received_ms": _round(self.audio_received_ms),
            "call_duration_ms": _round(self.call_duration_ms),
            "mean_inbound_gap_ms": _round(self.mean_inbound_gap_ms),
            "p95_inbound_gap_ms": _round(self.p95_inbound_gap_ms),
            "max_inbound_gap_ms": _round(self.max_inbound_gap_ms),
            "stall_count": len(self.stalls),
            "longest_stall_ms": _round(max(self.stalls)) if self.stalls else None,
            "delivery_duration_ms": _round(self.delivery_duration_ms),
            "delivery_ratio": _round(self.delivery_ratio, 2),
            "playback_tail_ms": _round(self.playback_tail_ms),
            "marks_received": self.marks_received,
            "marks_echoed": self.marks_echoed,
            "clears_received": self.clears_received,
            "violations": list(self.violations),
            "unknown_events": list(self.unknown_events),
            "timed_out": self.timed_out,
            "closed_early": self.closed_early,
            "pacing": {
                "drift_ms": _round(self.pacing_drift_ms),
                "late_frames": self.pacing_late_frames,
                "max_lateness_ms": _round(self.pacing_max_lateness_ms),
                "mean_lateness_ms": _round(self.pacing_mean_lateness_ms, 2),
                "measurement_is_reliable": self.measurement_is_reliable,
            },
        }


def _round(value: float | None, places: int = 1) -> float | None:
    return None if value is None else round(value, places)


#: A gap between inbound frames longer than this counts as a stall. Twilio-paced
#: audio arrives every 20 ms; agents commonly batch, so a threshold near the
#: frame interval would flag every well-behaved batching agent. 250 ms is past
#: the point a listener notices a break in speech.
STALL_THRESHOLD_MS = 250


def compute(result: SessionResult) -> Metrics:
    """Derive metrics from a completed session.

    Pure: takes the recorded log and returns numbers. No clock is read here, so
    the same result always produces the same metrics, and tests can feed it a
    handwritten log.
    """
    metrics = Metrics(
        frames_sent=result.frames_sent,
        media_frames_received=result.media_frames_received,
        audio_sent_ms=result.frames_sent * audio.FRAME_MS,
        audio_received_ms=result.audio_duration_s * 1000,
        marks_received=len(result.marks_received),
        marks_echoed=len(result.marks_echoed),
        clears_received=result.clears_received,
        violations=[violation.code for violation in result.violations],
        unknown_events=sorted(set(result.unknown_events)),
        timed_out=result.timed_out,
        closed_early=result.closed_early,
        pacing_drift_ms=result.pacing.drift_ms,
        pacing_late_frames=result.pacing.late_frames,
        pacing_max_lateness_ms=result.pacing.max_lateness_ms,
        pacing_mean_lateness_ms=result.pacing.mean_lateness_ms,
    )

    time_to_first = result.time_to_first_audio_s
    if time_to_first is not None:
        metrics.time_to_first_audio_ms = time_to_first * 1000

    metrics.call_duration_ms = _call_duration_ms(result)

    arrivals = [event.at for event in result.events_of("media_received")]
    # pairwise, not zip(arrivals, arrivals[1:], strict=True): those two differ
    # in length by one by construction, so strict=True raises ValueError on
    # every call that received any audio at all.
    metrics.inbound_gaps_ms = [
        (later - earlier) * 1000 for earlier, later in pairwise(arrivals)
    ]

    if len(arrivals) >= 2:
        # How long the agent took to hand over its audio, against how long that
        # audio lasts. Twilio buffers and plays at the caller's pace, so the
        # difference is time the caller is still listening to an agent that has
        # already moved on.
        delivery_s = arrivals[-1] - arrivals[0]
        metrics.delivery_duration_ms = delivery_s * 1000
        if delivery_s > 0:
            metrics.delivery_ratio = result.audio_duration_s / delivery_s
        metrics.playback_tail_ms = max(0.0, (result.audio_duration_s - delivery_s) * 1000)

    if metrics.inbound_gaps_ms:
        gaps = metrics.inbound_gaps_ms
        metrics.mean_inbound_gap_ms = sum(gaps) / len(gaps)
        metrics.p95_inbound_gap_ms = percentile(gaps, 0.95)
        metrics.max_inbound_gap_ms = max(gaps)
        metrics.stalls = [gap for gap in gaps if gap > STALL_THRESHOLD_MS]

    return metrics


def _call_duration_ms(result: SessionResult) -> float:
    """Stream start to the last recorded event.

    Measured from ``stream_started`` rather than from the first event, so that
    connection setup -- which is not the agent's doing and varies with the
    network -- is excluded from a figure about the call.
    """
    started = result.events_of("stream_started")
    if not started or not result.events:
        return 0.0
    return (result.events[-1].at - started[0].at) * 1000


# --------------------------------------------------------------------------
# Thresholds
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Threshold:
    """One assertion about a metric, for use as a CI gate."""

    name: str
    limit: float
    #: Attribute on :class:`Metrics` to compare.
    attribute: str
    #: What to do when the metric was not observed at all.
    #:
    #: True means a missing value fails the threshold. That is the right default
    #: for latency: an agent that never spoke has not met an 800 ms target, and
    #: treating "no data" as a pass is how a broken agent slips through a green
    #: build.
    missing_fails: bool = True


@dataclass(frozen=True)
class ThresholdResult:
    threshold: Threshold
    value: float | None
    passed: bool

    def describe(self) -> str:
        if self.value is None:
            return f"{self.threshold.name}: not measured (limit {self.threshold.limit:g}ms)"
        verdict = "ok" if self.passed else "FAIL"
        return (
            f"{self.threshold.name}: {self.value:.0f}ms "
            f"(limit {self.threshold.limit:g}ms) {verdict}"
        )


def evaluate_thresholds(
    metrics: Metrics, thresholds: list[Threshold]
) -> list[ThresholdResult]:
    """Check each threshold against the metrics."""
    results = []
    for threshold in thresholds:
        value = getattr(metrics, threshold.attribute)
        passed = not threshold.missing_fails if value is None else value <= threshold.limit
        results.append(ThresholdResult(threshold=threshold, value=value, passed=passed))
    return results
