"""Read a frame trace back and say what happened in it.

``--trace`` writes JSON Lines, and until this module nothing read them. A trace
attached to a bug report was thousands of lines a maintainer had to scroll by
hand. This turns one into the handful of facts that bug reports are
actually about: did the agent speak, when did it start, were its marks echoed,
did it ask for a ``clear``, and did anything arrive malformed or out of order.

Named ``tracereport`` rather than ``inspect`` so it cannot be confused with the
standard library module of that name.

**Pure.** No network, no clock. A trace is a file, and everything here is a
function of its lines, the same rule ``protocol.py`` and ``audio.py`` follow.

**A trace is untrusted input.** It exists to be attached to an issue and opened
by someone else. So lines are read one at a time and never all at once,
over-long lines are skipped without being parsed, anything that is not a JSON
object is counted rather than trusted, and the number of lines examined is
capped. Each limit is reported when it is hit instead of being hit silently.

**None is not zero.** An agent that never sent audio has no first-audio time,
and the summary says ``none``. A gap is only measured between two frames that
both exist.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .trace import MAX_TRACE_RECORDS

__all__ = [
    "DEFAULT_GAP_MS",
    "MAX_LINES",
    "MAX_LINE_BYTES",
    "TraceSummary",
    "read",
    "render",
    "summarise",
]

#: Longest line parsed. A trace with ``--trace-payloads`` holds base64 audio,
#: and gate 4's flooding endpoint sent frames of ~533 KB, so real lines can be
#: big. One MiB takes all of those and still stops a hostile file from making
#: ``json.loads`` build a huge object.
MAX_LINE_BYTES = 1024 * 1024

#: Most lines examined. The writer never produces more than
#: ``MAX_TRACE_RECORDS`` plus a truncation note, so a file longer than this
#: did not come from streamdouble as written. That gets reported.
MAX_LINES = MAX_TRACE_RECORDS + 16

#: Agent audio silence long enough to report. Agents batch their output, so
#: short gaps are normal. A second with nothing from an agent that was
#: mid-reply is worth a look.
DEFAULT_GAP_MS = 1000.0

#: Most individual anomalies kept per list. Counts stay exact past this.
_MAX_LISTED = 50


@dataclass
class TraceSummary:
    """What a trace shows. Times are seconds on the trace's own clock."""

    lines: int = 0
    records: int = 0
    #: Lines that were not a JSON object, or were too long to parse.
    malformed_lines: int = 0
    oversized_lines: int = 0
    #: The line cap was reached and the rest of the file was not read.
    stopped_early: bool = False
    #: Frames the agent sent that streamdouble itself could not parse.
    unparseable_frames: int = 0
    events: Counter = field(default_factory=Counter)
    first_media_out_t: float | None = None
    first_media_in_t: float | None = None
    last_media_in_t: float | None = None
    #: Marks the agent sent, and the ones this process echoed back.
    marks_sent: list[str] = field(default_factory=list)
    marks_echoed: list[str] = field(default_factory=list)
    clear_times: list[float] = field(default_factory=list)
    #: (start_t, length_ms) of silences in agent audio longer than the gap.
    media_gaps: list[tuple[float, float]] = field(default_factory=list)
    #: (direction, previous, current) sequence numbers that did not go up by one.
    sequence_breaks: list[tuple[str, int, int]] = field(default_factory=list)
    sequence_break_count: int = 0
    #: The writer's own truncation note, if present.
    truncated: dict[str, Any] | None = None

    @property
    def spoke(self) -> bool:
        return self.first_media_in_t is not None

    @property
    def first_audio_after_first_send_ms(self) -> float | None:
        """Agent's first media minus this process's first media, or None.

        None if either side never sent media. A negative value means the agent
        spoke first, for example with a greeting, and it is reported as is.
        """
        if self.first_media_in_t is None or self.first_media_out_t is None:
            return None
        return (self.first_media_in_t - self.first_media_out_t) * 1000.0

    @property
    def unechoed_marks(self) -> list[str]:
        remaining = Counter(self.marks_echoed)
        missing = []
        for name in self.marks_sent:
            if remaining[name]:
                remaining[name] -= 1
            else:
                missing.append(name)
        return missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "lines": self.lines,
            "records": self.records,
            "malformed_lines": self.malformed_lines,
            "oversized_lines": self.oversized_lines,
            "stopped_early": self.stopped_early,
            "unparseable_frames": self.unparseable_frames,
            "events": dict(sorted(self.events.items())),
            "spoke": self.spoke,
            "first_media_out_t": self.first_media_out_t,
            "first_media_in_t": self.first_media_in_t,
            "last_media_in_t": self.last_media_in_t,
            "first_audio_after_first_send_ms": self.first_audio_after_first_send_ms,
            "marks_sent": len(self.marks_sent),
            "marks_echoed": len(self.marks_echoed),
            "unechoed_marks": self.unechoed_marks[:_MAX_LISTED],
            "clear_times": self.clear_times[:_MAX_LISTED],
            "media_gaps": [
                {"t": t, "ms": round(ms, 1)} for t, ms in self.media_gaps[:_MAX_LISTED]
            ],
            "sequence_break_count": self.sequence_break_count,
            "sequence_breaks": [
                {"dir": d, "previous": p, "current": c} for d, p, c in self.sequence_breaks
            ],
            "truncated": self.truncated,
        }


def _number(value: Any) -> float | None:
    # bool is an int subclass; a hostile `"t": true` is not a time.
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return float(value)


def _sequence(value: Any) -> int | None:
    # Twilio sends sequenceNumber as a string; accept both, trust neither.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 12:
        return int(value)
    return None


def _label(value: Any) -> str:
    return value[:80] if isinstance(value, str) else repr(value)[:80]


def summarise(lines: Iterable[str | bytes], *, gap_ms: float = DEFAULT_GAP_MS) -> TraceSummary:
    """Summarise trace lines. Never raises on content, only counts it."""
    summary = TraceSummary()
    last_seq: dict[str, int] = {}

    for raw in lines:
        if summary.lines >= MAX_LINES:
            summary.stopped_early = True
            break
        summary.lines += 1

        if len(raw) > MAX_LINE_BYTES:
            summary.oversized_lines += 1
            continue
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            summary.malformed_lines += 1
            continue
        if not isinstance(record, dict):
            summary.malformed_lines += 1
            continue

        summary.records += 1
        direction = record.get("dir")

        if direction == "note":
            if record.get("truncated") is True:
                summary.truncated = {
                    "retained": _sequence(record.get("retained")),
                    "dropped": _sequence(record.get("dropped")),
                }
            continue
        if direction not in ("in", "out"):
            summary.malformed_lines += 1
            summary.records -= 1
            continue

        if record.get("unparseable") is True:
            summary.unparseable_frames += 1
            continue

        event = record.get("event")
        summary.events[f"{direction}:{_label(event)}"] += 1
        t = _number(record.get("t"))

        seq = _sequence(record.get("seq"))
        if seq is not None:
            previous = last_seq.get(direction)
            if previous is not None and seq != previous + 1:
                summary.sequence_break_count += 1
                if len(summary.sequence_breaks) < _MAX_LISTED:
                    summary.sequence_breaks.append((direction, previous, seq))
            last_seq[direction] = seq

        if event == "media" and t is not None:
            if direction == "out":
                if summary.first_media_out_t is None:
                    summary.first_media_out_t = t
            else:
                if summary.first_media_in_t is None:
                    summary.first_media_in_t = t
                elif summary.last_media_in_t is not None:
                    silence_ms = (t - summary.last_media_in_t) * 1000.0
                    if silence_ms > gap_ms and len(summary.media_gaps) < _MAX_LISTED:
                        summary.media_gaps.append((summary.last_media_in_t, silence_ms))
                summary.last_media_in_t = t
        elif event == "mark":
            name = _label(record.get("mark"))
            target = summary.marks_sent if direction == "in" else summary.marks_echoed
            if len(target) < MAX_LINES:
                target.append(name)
        elif (
            event == "clear"
            and direction == "in"
            and t is not None
            and len(summary.clear_times) < _MAX_LISTED
        ):
            summary.clear_times.append(t)

    return summary


def read(path: str | Path, *, gap_ms: float = DEFAULT_GAP_MS) -> TraceSummary:
    """Summarise the trace at ``path``, reading one line at a time.

    Opened in binary so that a line is measured before it is decoded, and a
    non-UTF-8 line is counted as malformed instead of raising.
    """
    with Path(path).open("rb") as handle:
        return summarise(_bounded_lines(handle), gap_ms=gap_ms)


def _bounded_lines(handle) -> Iterable[bytes]:
    # readline(limit) never buffers more than the limit, so a file with no
    # newlines at all cannot be pulled into memory in one go.
    while True:
        chunk = handle.readline(MAX_LINE_BYTES + 1)
        if not chunk:
            return
        if len(chunk) > MAX_LINE_BYTES and not chunk.endswith(b"\n"):
            # Consume the rest of the long line without keeping it.
            while True:
                rest = handle.readline(MAX_LINE_BYTES)
                if not rest or rest.endswith(b"\n"):
                    break
        yield chunk


def render(summary: TraceSummary) -> str:
    """The human-readable form."""

    def when(t: float | None) -> str:
        return "none" if t is None else f"{t:.3f}s"

    first_ms = summary.first_audio_after_first_send_ms
    lines = [
        f"records             {summary.records} ({summary.lines} lines)",
        f"agent spoke         {'yes' if summary.spoke else 'no'}",
        f"first caller audio  {when(summary.first_media_out_t)}",
        f"first agent audio   {when(summary.first_media_in_t)}",
        "agent after caller  " + ("none" if first_ms is None else f"{first_ms:.0f} ms"),
        f"marks               {len(summary.marks_sent)} sent by agent, "
        f"{len(summary.marks_echoed)} echoed",
        f"clears              {len(summary.clear_times)}",
    ]
    if unechoed := summary.unechoed_marks:
        lines.append(f"  unechoed marks: {', '.join(unechoed[:10])}")
    for t, ms in summary.media_gaps[:10]:
        lines.append(f"  agent audio gap of {ms:.0f} ms after {t:.3f}s")
    if summary.sequence_break_count:
        lines.append(f"sequence breaks     {summary.sequence_break_count}")
        for direction, previous, current in summary.sequence_breaks[:10]:
            lines.append(f"  {direction}: {previous} -> {current}")
    if summary.unparseable_frames:
        lines.append(f"unparseable frames  {summary.unparseable_frames} (sent by the agent)")
    if summary.malformed_lines or summary.oversized_lines:
        lines.append(
            f"malformed lines     {summary.malformed_lines}, "
            f"oversized {summary.oversized_lines} (skipped)"
        )
    if summary.truncated is not None:
        lines.append(
            f"TRUNCATED           the trace kept {summary.truncated['retained']} records "
            f"and dropped {summary.truncated['dropped']}"
        )
    if summary.stopped_early:
        lines.append(f"STOPPED             read the first {MAX_LINES} lines only")
    return "\n".join(lines)
