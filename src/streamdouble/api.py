"""The public Python API.

``streamdouble`` began as a command, and for its first six phases the only way
to use it was to run one and parse the JSON. That is a fine interface for a
shell script and a poor one for a test suite, which is where the people this
tool is for actually work.

This module is the supported surface. Two rules shape it:

**The CLI is a renderer over this, not a parallel implementation.** Everything
``cli.py`` knows about running a call now lives here; ``cli.py`` is left with
argument parsing, human formatting and exit. That is not tidiness for its own
sake -- ``streamdouble scenario`` parsed correctly and then died with "unknown
command" for its whole life in a public repository precisely because the
command line had a code path of its own that nothing exercised. Two ways to
place a call are two behaviours that drift.

**A report answers questions; it does not decide what they mean.** ``exit_code``
is computed here because the CLI and a CI harness should agree on it, but
nothing in this module prints, exits, or raises on a slow agent. A failed
threshold is a fact on the report, and the caller decides what a fact is worth.

The measurement rules from ``metrics.py`` carry over unchanged. In particular
``time_to_first_audio_ms`` is ``None`` -- never ``0`` -- when the agent never
spoke, and every consumer of this API inherits the obligation not to render
that as a fast reply.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import audio
from .metrics import Metrics, Threshold, ThresholdResult, compute, evaluate_thresholds
from .scenario import Scenario
from .scenario import load as load_scenario
from .session import Session, SessionConfig, SessionResult
from .trace import Trace, TraceConfig

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

__all__ = [
    "CallError",
    "CallReport",
    "ConnectionFailed",
    "TraceConfig",
    "call",
    "call_sync",
    "run_scenario",
    "run_scenario_sync",
    "save_reply",
]

#: Exit codes, duplicated from nowhere -- this is their definition, and
#: ``cli.py`` imports them. They are part of the documented contract in the
#: README, so they are stable and a new one is a breaking change.
EXIT_OK = 0
EXIT_ASSERTION_FAILED = 1
EXIT_TIMEOUT = 2
EXIT_PROTOCOL_VIOLATION = 3
EXIT_USAGE = 4
EXIT_CONNECTION_FAILED = 5


class CallError(Exception):
    """Base class for the errors this API raises."""


class ConnectionFailed(CallError):
    """The agent was not reachable at that URL.

    Covers a refused connection, an unresolvable host and a handshake that
    never completed. From the caller's point of view these are one situation,
    and distinguishing them would invite handling that cannot be tested.
    """


@dataclass
class CallReport:
    """Everything one simulated call produced.

    Deliberately a record rather than a verdict. ``exit_code`` is present so a
    harness can match the CLI's judgement exactly, but a caller is free to
    ignore it and assert on the numbers directly -- which is the normal thing
    to do in a test.
    """

    result: SessionResult
    metrics: Metrics
    thresholds: list[ThresholdResult] = field(default_factory=list)
    #: Where the frame trace was written, if one was requested.
    trace_path: Path | None = None

    # ------------------------------------------------------------------
    # The handful of things people actually assert on, forwarded so a test
    # reads as `report.time_to_first_audio_ms` rather than
    # `report.metrics.time_to_first_audio_ms`.
    # ------------------------------------------------------------------

    @property
    def time_to_first_audio_ms(self) -> float | None:
        """Milliseconds from stream start to the agent's first audio byte.

        ``None`` when the agent never spoke. Not ``0``: silence is the worst
        outcome, not the fastest one, and a comparison against ``None`` raising
        a ``TypeError`` is a better failure than a green build.
        """
        return self.metrics.time_to_first_audio_ms

    @property
    def violations(self) -> list[str]:
        """Protocol violations the agent committed, as codes."""
        return list(self.result.violations)

    @property
    def audio_received(self) -> bytes:
        """The agent's reply, as raw mu-law bytes."""
        return bytes(self.result.audio_received)

    @property
    def spoke(self) -> bool:
        """Whether the agent produced any audio at all."""
        return bool(self.result.audio_received)

    @property
    def stream_sid(self) -> str:
        return self.result.identity.stream_sid

    @property
    def passed(self) -> bool:
        """True when nothing this call measured counts as a failure."""
        return self.exit_code == EXIT_OK

    @property
    def exit_code(self) -> int:
        """The code the CLI would exit with for this report.

        Ordered by severity, most definite first. A protocol violation is a
        certain bug; a timeout is a specific, informative outcome; a failed
        threshold is a judgement about a number that was measured successfully.
        Reporting the vaguest of the three when a more specific one applies
        would lose information a pipeline could have acted on.
        """
        if self.result.violations:
            return EXIT_PROTOCOL_VIOLATION
        if self.result.timed_out:
            return EXIT_TIMEOUT
        if self.result.failed_expectations:
            return EXIT_ASSERTION_FAILED
        if any(not outcome.passed for outcome in self.thresholds):
            return EXIT_ASSERTION_FAILED
        return EXIT_OK

    def to_dict(self) -> dict[str, Any]:
        """The ``--json`` payload.

        This is the definition of that format. ``cli.py`` prints what this
        returns and adds nothing, so the documented JSON and the API cannot
        describe different calls.
        """
        payload = self.metrics.to_dict()
        payload["stream_sid"] = self.stream_sid
        payload["frames_dropped"] = self.result.frames_dropped
        payload["expectations"] = [
            {"what": what, "passed": passed} for what, passed in self.result.expectations
        ]
        payload["thresholds"] = [
            {
                "name": outcome.threshold.name,
                "limit_ms": outcome.threshold.limit,
                "value_ms": None if outcome.value is None else round(outcome.value, 1),
                "passed": outcome.passed,
            }
            for outcome in self.thresholds
        ]
        payload["exit_code"] = self.exit_code
        return payload


async def _run(
    session: Session,
    url: str,
    thresholds: Sequence[Threshold] | None,
    trace: Trace | None = None,
) -> CallReport:
    """Run a prepared session and turn it into a report.

    The trace is flushed in a ``finally``: a call that ended badly is the one
    whose trace is worth having, and a diagnostic that only survives success is
    no diagnostic at all.

    That ``finally`` must not be able to raise, and getting this wrong once is
    how gate 6 found it. ``flush`` writes a file, so it raises for a typo in
    the path, a read-only CI workspace or a full disk -- and from a ``finally``
    that exception *replaces* whatever the call was actually reporting. Two
    failures, both reproduced: a perfectly good call lost its entire report
    because a side-file could not be written, and a call to an unreachable
    agent reported ``PermissionError`` instead of ``ConnectionFailed``,
    pointing the user at their filesystem while their agent was down.

    This is gate 2's finding wearing different clothes -- there, a ``finally``
    that raised discarded the ``ConnectionClosed`` that was the real problem.
    The rule that came out of it is the rule here: cleanup does not get to
    decide how a call ended.
    """
    trace_failure: OSError | None = None
    try:
        try:
            result = await session.run()
        except (OSError, TimeoutError) as exc:
            raise ConnectionFailed(f"could not connect to {url}: {exc}") from exc
    finally:
        if trace is not None:
            try:
                trace.flush()
            except OSError as exc:
                trace_failure = exc

    if trace_failure is not None:
        # Reported, not raised, and not silent either. The call succeeded; the
        # user's numbers are real and they should get them. What they must not
        # get is a report that implies a trace exists when it does not.
        print(f"streamdouble: could not write the trace: {trace_failure}", file=sys.stderr)

    metrics = compute(result)
    return CallReport(
        result=result,
        metrics=metrics,
        thresholds=evaluate_thresholds(metrics, list(thresholds or [])),
        trace_path=(
            trace.config.path if trace is not None and trace_failure is None else None
        ),
    )


async def call(
    url: str,
    *,
    audio_path: str | Path | None = None,
    frames: Sequence[bytes] | None = None,
    config: SessionConfig | None = None,
    thresholds: Sequence[Threshold] | None = None,
    trace: TraceConfig | None = None,
) -> CallReport:
    """Place one simulated call and return what it produced.

    Args:
        url: The agent's WebSocket endpoint.
        audio_path: WAV file to stream as the caller's voice. Resampled to
            8 kHz mono mu-law automatically.
        frames: Pre-encoded 160-byte mu-law frames, as an alternative to
            ``audio_path`` for callers who already have them.
        config: Timeouts, custom parameters, impairments. See
            :class:`~streamdouble.session.SessionConfig`.
        thresholds: Gates to evaluate. Their outcomes land on the report;
            nothing is raised when one fails.

    Raises:
        ValueError: Neither ``audio_path`` nor ``frames`` was given, or the
            WAV contained no audio.
        ConnectionFailed: The agent was not reachable.
    """
    if audio_path is None and frames is None:
        raise ValueError("call() needs either audio_path or frames")
    if audio_path is not None and frames is not None:
        raise ValueError("call() takes audio_path or frames, not both")

    if audio_path is not None:
        frames = audio.wav_to_ulaw_frames(audio_path)
        if not frames:
            raise ValueError(f"{audio_path} contains no audio")

    config, recorder = _with_trace(config, trace)
    session = Session(url, frames, config=config)
    return await _run(session, url, thresholds, recorder)


async def run_scenario(
    url: str,
    scenario: Scenario | str | Path,
    *,
    config: SessionConfig | None = None,
    thresholds: Sequence[Threshold] | None = None,
    trace: TraceConfig | None = None,
) -> CallReport:
    """Run a scripted call and return what it produced.

    ``scenario`` may be a loaded :class:`~streamdouble.scenario.Scenario` or a
    path to a YAML file. A path is loaded and validated before the socket
    opens, so a typo cannot fail halfway through with the agent mid-sentence.

    A failed ``expect:`` lands on ``report.result.failed_expectations`` and
    makes ``exit_code`` 1. Nothing is raised.
    """
    if not isinstance(scenario, Scenario):
        scenario = load_scenario(scenario)

    config, recorder = _with_trace(config, trace)
    session = Session(url, scenario=scenario, config=config)
    return await _run(session, url, thresholds, recorder)


def _with_trace(
    config: SessionConfig | None, trace: TraceConfig | None
) -> tuple[SessionConfig, Trace | None]:
    """Attach a recorder to a copy of the config, never to the caller's own.

    ``SessionConfig`` is routinely shared between calls -- this project's own
    tests define one at module level and reuse it -- so mutating it here would
    make every later call write to the first call's trace file.
    """
    base = config or SessionConfig()
    if trace is None:
        return base, None

    recorder = Trace(config=trace)
    return replace(base, trace=recorder), recorder


def _refuse_inside_a_loop(sync_name: str, async_name: str) -> None:
    """Fail with a sentence naming the fix, rather than deep inside asyncio.

    ``asyncio.run`` inside a running loop raises "asyncio.run() cannot be
    called from a running event loop" several frames down, which reads as a
    bug in this library rather than as a wrong choice of function. Under
    ``pytest-asyncio`` in ``auto`` mode -- which is how this project's own
    suite is configured -- every test function is already inside a loop, so
    this is the *likely* mistake, not an exotic one.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        f"{sync_name}() cannot be used inside a running event loop; "
        f"await {async_name}() instead"
    )


def call_sync(url: str, **kwargs: Any) -> CallReport:
    """Blocking :func:`call`, for tests and scripts that are not async."""
    _refuse_inside_a_loop("call_sync", "call")
    return asyncio.run(call(url, **kwargs))


def run_scenario_sync(url: str, scenario: Scenario | str | Path, **kwargs: Any) -> CallReport:
    """Blocking :func:`run_scenario`."""
    _refuse_inside_a_loop("run_scenario_sync", "run_scenario")
    return asyncio.run(run_scenario(url, scenario, **kwargs))


def save_reply(report: CallReport, path: str | Path) -> bool:
    """Write the agent's reply to a WAV file.

    Returns False and writes nothing when the agent never spoke, rather than
    leaving a zero-length WAV that looks like a successful recording of
    silence. The caller decides whether that is worth reporting.
    """
    if not report.result.audio_received:
        return False
    audio.ulaw_to_wav(report.result.audio_received, path)
    return True
