"""A frame-by-frame record of a call, as JSON Lines.

What this is for. When someone reports "streamdouble does not work against my
agent", the useful artefact is not a summary -- it is what actually went over
the wire, in order, with timings. This project's issue template asks people to
say where the simulation is wrong, and a trace is how they show it.

Three decisions, each of which costs something:

**Nothing is written during the call.** Records are appended to a list in
memory and flushed once, after the socket closes. File I/O inside the receive
loop is the exact shape of the bug gate 2 found -- work on the event loop that
delays frame handling and inflates the very latency this tool exists to report,
worse the chattier the agent, and plausible at every point. A trace that
changes the measurement is not a diagnostic, it is a heisenbug generator.
`tests/test_review_gate_6.py` asserts a traced run and an untraced run report
the same figures.

**Payloads are excluded by default.** A minute of audio is about 30 MB of
base64 that nobody reads. What a reader needs is the *shape*: order, sizes,
timings, sequence numbers. A SHA-256 prefix of each payload is kept so two
traces can be compared for "did the same bytes go out" without carrying the
bytes. ``--trace-payloads`` opts in when the bytes themselves are the question.

**Custom parameters are redacted.** ``--param`` is how agents authenticate --
the project's own documentation says to use it for shared secrets -- and the
``start`` frame carries those values. Since the entire purpose of a trace is to
be attached to a bug report, writing credentials into it by default would turn
a diagnostic into a disclosure. Keys are kept, values become ``"<redacted>"``,
and ``--trace-secrets`` opts out for someone tracing their own local stub.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["MAX_TRACE_PAYLOAD_BYTES", "MAX_TRACE_RECORDS", "Trace", "TraceConfig"]

#: How many hex characters of the payload digest to keep. Eight is enough to
#: tell two frames apart in a diff and short enough to stay readable; this is
#: not a security boundary, it is an identifier for eyeballing.
_DIGEST_CHARS = 8

#: What a redacted custom parameter looks like. Deliberately not the empty
#: string: a reader must be able to tell "this was withheld" from "this was
#: absent", because those are different bug reports.
REDACTED = "<redacted>"

#: Most frames to retain. Beyond this the trace stops growing and says so.
#:
#: Gate 4 measured a flooding endpoint driving 206 MB of buffered audio and
#: 436 MB of peak memory in a five-second call, and capped both the audio and
#: the event log in response. The trace arrived afterwards with no cap at all
#: and reopened the same vector: a record is roughly a kilobyte, so a ten
#: minute call accumulates tens of megabytes, and with ``payloads=True``
#: against that same endpoint each record carries a ~533 KB base64 string.
#:
#: 100_000 frames is over half an hour of a call in both directions, which is
#: past the point where anyone reads a trace line by line anyway.
MAX_TRACE_RECORDS = 100_000

#: Most payload bytes to retain in total, when ``payloads=True``.
#:
#: Separate from the record cap because the record cap does not bound this:
#: gate 4's endpoint sends few frames and enormous ones, so a count-based limit
#: alone lets a handful of records carry hundreds of megabytes.
MAX_TRACE_PAYLOAD_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class TraceConfig:
    """What to write, and what to leave out."""

    path: Path
    #: Include the base64 audio payloads themselves.
    payloads: bool = False
    #: Include ``start.customParameters`` values rather than redacting them.
    secrets: bool = False


@dataclass
class Trace:
    """An in-memory frame log, flushed to disk once the call is over."""

    config: TraceConfig
    records: list[dict[str, Any]] = field(default_factory=list)
    #: Frames seen after the cap was reached. Counted exactly even though they
    #: are not retained, because "the trace stops at 100,000" and "your agent
    #: sent 4 million frames" are different facts and the second one is the
    #: interesting one.
    dropped: int = 0
    #: Payload bytes retained so far, against MAX_TRACE_PAYLOAD_BYTES.
    _payload_bytes: int = 0

    def _room_for_another(self) -> bool:
        if len(self.records) >= MAX_TRACE_RECORDS:
            self.dropped += 1
            return False
        return True

    def note_out(self, frame: dict[str, Any], at: float) -> None:
        """Record a frame this process sent to the agent.

        ``at`` is the caller's clock reading, passed in rather than taken here,
        so the trace never introduces a second notion of when something
        happened. Every timestamp in a trace comes from the same clock as the
        metrics, which is what makes the two comparable.
        """
        if self._room_for_another():
            self.records.append(self._describe(frame, "out", at))

    def note_in(self, raw: str | bytes, at: float, *, parsed: dict[str, Any] | None = None) -> None:
        """Record a frame the agent sent to this process.

        ``raw`` is accepted so that a frame which failed to parse is still
        traced -- a malformed frame is the single most valuable thing a trace
        can contain, and one that only logged well-formed frames would omit
        exactly the case someone is reporting.
        """
        if parsed is None:
            parsed = _try_parse(raw)

        if not self._room_for_another():
            return

        if parsed is None:
            self.records.append(
                {
                    "t": round(at, 6),
                    "dir": "in",
                    "event": None,
                    "unparseable": True,
                    "bytes": len(raw),
                    "excerpt": _excerpt(raw),
                }
            )
            return

        self.records.append(self._describe(parsed, "in", at))

    def _describe(self, frame: dict[str, Any], direction: str, at: float) -> dict[str, Any]:
        record: dict[str, Any] = {
            "t": round(at, 6),
            "dir": direction,
            "event": frame.get("event"),
        }

        sequence = frame.get("sequenceNumber")
        if sequence is not None:
            record["seq"] = sequence

        media = frame.get("media")
        if isinstance(media, dict):
            record["chunk"] = media.get("chunk")
            record["timestamp_ms"] = media.get("timestamp")
            if (track := media.get("track")) is not None:
                record["track"] = track
            payload = media.get("payload")
            if isinstance(payload, str):
                record["bytes"] = len(payload)
                record["sha256_8"] = _digest(payload)
                if self.config.payloads:
                    # The record cap does not bound this on its own: gate 4's
                    # endpoint sends few frames and enormous ones, so a
                    # count-based limit lets a handful of records carry
                    # hundreds of megabytes. The digest above is kept either
                    # way, so a truncated trace stays comparable.
                    if self._payload_bytes + len(payload) <= MAX_TRACE_PAYLOAD_BYTES:
                        record["payload"] = payload
                        self._payload_bytes += len(payload)
                    else:
                        record["payload_omitted"] = "payload cap reached"

        if isinstance(mark := frame.get("mark"), dict):
            record["mark"] = mark.get("name")

        if isinstance(dtmf := frame.get("dtmf"), dict):
            record["digit"] = dtmf.get("digit")

        # `start` nests its fields one level down, which is easy to get wrong
        # in exactly the direction that matters: reading `customParameters`
        # from the top level finds nothing, so the redaction appears to work
        # while actually never running. `tests/test_trace.py` asserts the keys
        # are present *and* the values are hidden, because only the first half
        # of that distinguishes a redaction from a lookup that missed.
        if isinstance(start := frame.get("start"), dict):
            record["tracks"] = start.get("tracks")
            if (params := start.get("customParameters")) is not None:
                record["customParameters"] = self._parameters(params)

        return record

    def _parameters(self, params: Any) -> Any:
        if self.config.secrets or not isinstance(params, dict):
            return params
        return dict.fromkeys(params, REDACTED)

    def flush(self) -> int:
        """Write the trace and return how many records were written.

        Called once, after the socket is closed, so the cost lands where it
        cannot affect a measurement.

        Raises whatever the filesystem raises. :func:`streamdouble.api.call`
        is responsible for not letting that destroy a call -- see the note
        there about why a diagnostic must never be able to do that.
        """
        if self.dropped:
            self.records.append(
                {
                    "dir": "note",
                    "truncated": True,
                    "retained": len(self.records),
                    "dropped": self.dropped,
                }
            )
        self.config.path.parent.mkdir(parents=True, exist_ok=True)
        with self.config.path.open("w", encoding="utf-8", newline="\n") as handle:
            for record in self.records:
                handle.write(json.dumps(record) + "\n")
        return len(self.records)


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("ascii", "replace")).hexdigest()[:_DIGEST_CHARS]


def _try_parse(raw: str | bytes) -> dict[str, Any] | None:
    try:
        loaded = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _excerpt(raw: str | bytes, limit: int = 120) -> str:
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    return text[:limit]
