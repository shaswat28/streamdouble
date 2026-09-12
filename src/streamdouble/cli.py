"""Command line interface.

The product is the CLI and CI, so this file is a user-facing surface rather than
a thin wrapper: what it prints, and what it exits with, is most of what anyone
will ever see of this package.

It does not, however, know how to place a call. That lives in ``api.py``, and
this module parses arguments, renders a report and picks an exit code. The
split is deliberate: `streamdouble scenario` parsed correctly and then died
with "unknown command" for its whole life in a public repository because the
command line had a code path of its own that no test exercised. One
implementation, two front ends.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import __version__, api, audio
from . import baseline as baseline_module
from .aggregate import RunSeries
from .api import (
    EXIT_ASSERTION_FAILED,
    EXIT_CONNECTION_FAILED,
    EXIT_OK,
    EXIT_PROTOCOL_VIOLATION,
    EXIT_TIMEOUT,
    EXIT_USAGE,
    CallReport,
    ConnectionFailed,
)
from .baseline import BaselineError
from .chaos import Impairments
from .metrics import (
    CONVERSATIONAL_FLOW_MS,
    MIN_SAMPLES_FOR_PERCENTILE,
    Metrics,
    Threshold,
    compute,
)
from .scenario import ScenarioError
from .scenario import load as load_scenario
from .session import SessionConfig, SessionResult
from .trace import TraceConfig

#: Exit codes. Stable, and chosen so CI can distinguish outcomes without
#: parsing output. They are *defined* in ``api`` and re-exported here, because
#: they are part of the CLI's documented contract and ``cli.EXIT_TIMEOUT`` is
#: what existing callers and tests import -- but there must be exactly one
#: definition, or the API and the command line could disagree about what a run
#: meant.
#:
#: They are named in ``__all__`` so that a linter's unused-import pass sees a
#: re-export rather than dead code and removes them. That is not hypothetical:
#: it happened during the refactor that introduced this module, and every
#: exit-code test went red at once.
__all__ = [
    "EXIT_ASSERTION_FAILED",
    "EXIT_CONNECTION_FAILED",
    "EXIT_OK",
    "EXIT_PROTOCOL_VIOLATION",
    "EXIT_TIMEOUT",
    "EXIT_USAGE",
    "build_parser",
    "main",
]


def _add_shared_options(command: argparse.ArgumentParser) -> None:
    """Options every call-placing subcommand takes.

    Shared rather than duplicated: `call` and `scenario` differ only in where
    the caller's audio comes from, and two copies of a dozen flags is two
    copies that drift.
    """
    command.add_argument(
        "--out", type=Path, help="write the agent's reply to this WAV file"
    )
    command.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="TwiML <Parameter> value to put on the start frame; repeatable",
    )
    command.add_argument(
        "--response-timeout", type=float, default=10.0, metavar="SECONDS",
        help="how long to wait for the agent's first audio (default: %(default)s)",
    )
    command.add_argument(
        "--quiet-period", type=float, default=1.0, metavar="SECONDS",
        help="silence that means the agent has finished speaking (default: %(default)s)",
    )
    command.add_argument(
        "--max-drain", type=float, default=30.0, metavar="SECONDS",
        help="cap on waiting after the audio is sent (default: %(default)s)",
    )
    command.add_argument(
        "--no-echo-marks", action="store_true",
        help="do not echo marks back; Twilio does, and most agents expect it",
    )
    command.add_argument("--quiet", action="store_true", help="print only errors")
    command.add_argument(
        "--trace", type=Path, metavar="PATH",
        help=(
            "write every frame, both directions, as JSON Lines. This is the "
            "artefact to attach to a bug report about the simulation itself"
        ),
    )
    command.add_argument(
        "--trace-payloads", action="store_true",
        help=(
            "include the base64 audio in the trace. Off by default: a minute "
            "of audio is about 30 MB of base64 that nobody reads"
        ),
    )
    command.add_argument(
        "--trace-secrets", action="store_true",
        help=(
            "include start.customParameters values in the trace. Off by "
            "default, because --param is how agents are authenticated and a "
            "trace exists to be sent to someone else"
        ),
    )
    command.add_argument(
        "--json", action="store_true",
        help="emit metrics as JSON on stdout instead of a human summary",
    )

    chaos = command.add_argument_group(
        "network conditions",
        "Simulate a bad connection. Impairments are seeded, so a run that finds "
        "a bug can be replayed exactly -- pass the same --chaos-seed.",
    )
    chaos.add_argument(
        "--packet-loss", type=float, default=0.0, metavar="FRACTION",
        help=(
            "proportion of the caller's frames the network loses, e.g. 0.05. "
            "Modelled as audio Twilio never received, so the agent sees a "
            "media.timestamp jump rather than a missing frame"
        ),
    )
    chaos.add_argument(
        "--jitter", type=float, default=0.0, metavar="MS",
        help="per-frame timing deviation, drawn uniformly from [0, MS]",
    )
    chaos.add_argument(
        "--latency", type=float, default=0.0, metavar="MS",
        help="constant delay added to every frame",
    )
    chaos.add_argument(
        "--chaos-seed", type=int, default=0, metavar="N",
        help="seed for the impairment decisions (default: %(default)s)",
    )

    series = command.add_argument_group(
        "repeat runs",
        "One call is a sample, not a measurement. Repeating gives a "
        "distribution -- and a stored distribution is something a later run "
        "can be checked against, which an absolute threshold cannot do.",
    )
    series.add_argument(
        "-n", "--repeat", type=int, default=1, metavar="N",
        help=(
            "place N calls in sequence and summarise them. Sequential, never "
            "parallel: concurrent calls delay each other's frames and would "
            "corrupt the statistics being gathered (default: %(default)s)"
        ),
    )
    series.add_argument(
        "--save-baseline", type=Path, metavar="PATH",
        help="write this run's summary to PATH, to compare later runs against",
    )
    series.add_argument(
        "--baseline", type=Path, metavar="PATH",
        help=(
            "compare this run against a saved baseline and fail on a "
            "regression. Catches 300ms becoming 700ms, which no absolute "
            "threshold can see"
        ),
    )
    series.add_argument(
        "--tolerance-pct", type=float, default=baseline_module.DEFAULT_TOLERANCE_PCT,
        metavar="PCT",
        help=(
            "how much worse, proportionally, counts as a regression "
            "(default: %(default)s)"
        ),
    )
    series.add_argument(
        "--tolerance-ms", type=float, default=baseline_module.DEFAULT_TOLERANCE_MS,
        metavar="MS",
        help=(
            "how much worse, absolutely, counts as a regression. A change must "
            "exceed this AND --tolerance-pct, so a 40%% worse 5ms figure is not "
            "a build failure (default: %(default)s)"
        ),
    )

    gates = command.add_argument_group(
        "CI gates",
        "Thresholds turn a call into a pass/fail check. A failed threshold "
        "exits 1. A metric that was never measured fails its threshold: an "
        "agent that said nothing has not met an 800ms target, and treating "
        "missing data as a pass is how a broken agent gets a green build.",
    )
    gates.add_argument(
        "--max-first-audio-ms", type=float, metavar="MS",
        help=(
            "fail if the agent's first audio takes longer than this. "
            f"{CONVERSATIONAL_FLOW_MS} is the usual conversational-flow target; "
            "see the metrics module for where that number comes from"
        ),
    )
    gates.add_argument(
        "--max-gap-ms", type=float, metavar="MS",
        help="fail if any silence within the agent's reply exceeds this",
    )
    gates.add_argument(
        "--allow-no-audio", action="store_true",
        help="treat a missing measurement as passing rather than failing its threshold",
    )


class _Parser(argparse.ArgumentParser):
    """An ``ArgumentParser`` that fails with this package's own usage code.

    ``argparse`` exits 2 on a bad flag or an unrecognised argument, which here
    is ``EXIT_TIMEOUT`` -- a documented, meaningful code. So a reader who typed
    a command wrong was told their agent had failed to respond in time, and a
    CI job gating on exit codes read a typo as a slow agent. Everything else
    about ``argparse``'s behaviour, including exiting 0 for ``--help`` and
    ``--version``, is left alone.

    ``add_subparsers`` propagates ``parser_class``, so every subcommand
    inherits this without being told to.
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="streamdouble",
        description=(
            "Simulate a Twilio Media Stream against a voice agent's WebSocket "
            "endpoint. Not affiliated with Twilio."
        ),
    )
    parser.add_argument("--version", action="version", version=f"streamdouble {__version__}")

    subcommands = parser.add_subparsers(dest="command", required=True)

    call = subcommands.add_parser(
        "call",
        help="place one simulated call",
        description="Place one simulated call and record what the agent says back.",
    )
    call.add_argument(
        "url", help="WebSocket URL of the agent, e.g. ws://localhost:8000/media-stream"
    )
    call.add_argument(
        "--audio",
        type=Path,
        required=True,
        help="WAV file to stream as the caller's voice. Resampled to 8 kHz mono automatically.",
    )
    _add_shared_options(call)

    play = subcommands.add_parser(
        "scenario",
        help="run a scripted call from a YAML file",
        description=(
            "Run a scripted call: clips, waits, keypresses and assertions, in "
            "order. Scenarios are validated before the socket opens, so a typo "
            "cannot fail halfway through with the agent mid-sentence."
        ),
    )
    play.add_argument("file", type=Path, help="scenario YAML file")
    play.add_argument(
        "url", help="WebSocket URL of the agent, e.g. ws://localhost:8000/media-stream"
    )
    _add_shared_options(play)

    return parser


def thresholds_from(args: argparse.Namespace) -> list[Threshold]:
    """Build the threshold list from the command line."""
    missing_fails = not args.allow_no_audio
    thresholds = []
    if args.max_first_audio_ms is not None:
        thresholds.append(
            Threshold(
                name="first audio",
                limit=args.max_first_audio_ms,
                attribute="time_to_first_audio_ms",
                missing_fails=missing_fails,
            )
        )
    if args.max_gap_ms is not None:
        thresholds.append(
            Threshold(
                name="longest gap",
                limit=args.max_gap_ms,
                attribute="max_inbound_gap_ms",
                missing_fails=missing_fails,
            )
        )
    return thresholds


def trace_from(args: argparse.Namespace) -> TraceConfig | None:
    """Build the trace configuration, or None when --trace was not given."""
    if not args.trace:
        return None
    return TraceConfig(
        path=args.trace,
        payloads=args.trace_payloads,
        secrets=args.trace_secrets,
    )


def impairments_from(args: argparse.Namespace) -> Impairments:
    """The network conditions this run asked for."""
    return Impairments(
        loss=args.packet_loss,
        jitter_ms=args.jitter,
        latency_ms=args.latency,
    )


def config_from(args: argparse.Namespace, params: dict[str, str]) -> SessionConfig:
    """Build a SessionConfig from parsed arguments."""
    return SessionConfig(
        response_timeout_s=args.response_timeout,
        quiet_period_s=args.quiet_period,
        max_drain_s=args.max_drain,
        echo_marks=not args.no_echo_marks,
        custom_parameters=params,
        impairments=Impairments(
            loss=args.packet_loss,
            jitter_ms=args.jitter,
            latency_ms=args.latency,
        ),
        chaos_seed=args.chaos_seed,
    )


def parse_params(pairs: list[str]) -> dict[str, str]:
    """Parse repeated ``KEY=VALUE`` arguments.

    Raises:
        ValueError: A pair has no ``=``. Reported rather than skipped, because a
            silently-dropped parameter shows up as an agent routing bug much
            later and with no clue pointing here.
    """
    params: dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key:
            raise ValueError(f"--param must be KEY=VALUE, got {pair!r}")
        params[key] = value
    return params


def report(
    result: SessionResult,
    metrics: Metrics | None = None,
    outcomes: list | None = None,
    stream=sys.stdout,
) -> None:
    """Print a human-readable summary of the call."""
    metrics = metrics if metrics is not None else compute(result)
    outcomes = outcomes or []

    def write(line: str = "") -> None:
        print(line, file=stream)

    write()
    write(f"  stream        {result.identity.stream_sid}")
    write(f"  sent          {metrics.frames_sent} frames "
          f"({metrics.audio_sent_ms / 1000:.2f}s)")
    write(f"  received      {metrics.media_frames_received} media frames "
          f"({metrics.audio_received_ms / 1000:.2f}s)")

    if metrics.time_to_first_audio_ms is None:
        # Deliberately not "0 ms". Silence is a different outcome from an
        # instant reply, and collapsing them would hide the more serious one.
        write("  first audio   never")
    else:
        write(f"  first audio   {metrics.time_to_first_audio_ms:.0f} ms")

    if metrics.max_inbound_gap_ms is not None:
        gaps = f"  gaps          mean {metrics.mean_inbound_gap_ms:.0f} ms"
        if metrics.p95_inbound_gap_ms is not None:
            gaps += f", p95 {metrics.p95_inbound_gap_ms:.0f} ms"
        gaps += f", max {metrics.max_inbound_gap_ms:.0f} ms"
        write(gaps)

    if metrics.delivery_ratio is not None and metrics.delivery_ratio > 1.5:
        # Worth surfacing unprompted. An agent that front-loads its audio
        # finishes sending long before the caller finishes hearing, and
        # anything it derives from send-completion is wrong for that whole
        # window -- most often an is_speaking flag guarding barge-in.
        write(f"  delivery      {metrics.delivery_ratio:.1f}x faster than real time "
              f"({metrics.delivery_duration_ms / 1000:.2f}s to send "
              f"{metrics.audio_received_ms / 1000:.2f}s of audio)")
        write(f"  ! caller still hears {metrics.playback_tail_ms / 1000:.2f}s after "
              "the agent stops sending. If it tracks \"am I speaking\" by when "
              "sending finishes, barge-in is blind for that window; gating on the "
              "mark echo instead avoids it")

    if metrics.stalls:
        write(f"  stalls        {len(metrics.stalls)}, "
              f"longest {max(metrics.stalls):.0f} ms")

    if metrics.marks_received:
        write(f"  marks         {metrics.marks_received} received, "
              f"{metrics.marks_echoed} echoed")
    if metrics.clears_received:
        write(f"  clears        {metrics.clears_received}")
    if metrics.unknown_events:
        write(f"  unknown       {', '.join(metrics.unknown_events)}")

    write(f"  pacing        {result.pacing.summary()}")

    if not metrics.measurement_is_reliable:
        # Said plainly rather than buried. Audio that reached the agent late
        # makes every latency above it overstated, and a confident number off a
        # slipping clock is worse than no number.
        write()
        write("  ! pacing slipped by more than a frame, so the timings above "
              "are an upper bound")

    if result.frames_dropped:
        write(f"  dropped       {result.frames_dropped} caller frames "
              f"({result.network.impairments.describe() if result.network else ''})")

    if result.expectations:
        write()
        for what, passed in result.expectations:
            write(f"  {what}: {'ok' if passed else 'FAIL'}")

    if outcomes:
        write()
        for outcome in outcomes:
            write(f"  {outcome.describe()}")

    if metrics.violations:
        write()
        write(f"  {len(metrics.violations)} protocol violation(s):")
        for violation in result.violations:
            write(f"    [{violation.code}] {violation}")

    if result.timed_out:
        write()
        write("  the agent sent no audio before the response timeout")
    if result.closed_early:
        write()
        write("  the agent closed the connection before the call finished")


def report_series(series: RunSeries, write=print) -> None:
    """Print a summary of several runs."""
    write()
    write(f"  {series.n} runs, {series.failures} failed")
    write()
    write(f"  {'':<16}{'median':>10}{'min':>10}{'max':>10}{'p95':>10}{'stddev':>9}  n")
    for metric in series.metrics:
        measured = len(metric.measured)
        counts = f"{measured}/{metric.n}"
        write(
            f"  {metric.label:<16}"
            f"{_cell(metric.median):>10}{_cell(metric.minimum):>10}"
            f"{_cell(metric.maximum):>10}{_cell(metric.p95):>10}"
            f"{_cell(metric.stddev):>9}  {counts}"
        )

    if series.percentiles_withheld:
        write()
        write(
            f"  p95 withheld: {series.n} runs is below the {MIN_SAMPLES_FOR_PERCENTILE} "
            "needed for a percentile to mean anything"
        )

    missing = [m for m in series.metrics if m.missing]
    if missing:
        write()
        for metric in missing:
            write(
                f"  {metric.label}: {metric.missing} of {metric.n} runs produced no "
                "measurement, and are excluded from the figures above rather "
                "than counted as zero"
            )


def report_comparison(comparison, write=print) -> None:
    """Print a baseline comparison."""
    write()
    write("  against the baseline:")
    for delta in comparison.deltas:
        if delta.lost_measurement:
            write(
                f"    {delta.label:<16} was {delta.before:.1f}, now never measured  "
                "REGRESSION"
            )
        elif not delta.comparable:
            write(f"    {delta.label:<16} not comparable")
        else:
            arrow = "+" if (delta.delta or 0) >= 0 else ""
            verdict = "  REGRESSION" if delta.regressed else ""
            write(
                f"    {delta.label:<16} {delta.before:.1f} -> {delta.after:.1f} "
                f"({arrow}{delta.delta:.1f}, {arrow}{delta.delta_pct:.0f}%){verdict}"
            )


def _cell(value: float | None) -> str:
    """A missing figure renders as a dash, never as 0.

    The whole project turns on that distinction, and a table is where it is
    easiest to lose: a blank or a zero in a column of numbers reads as a
    measurement.
    """
    return "-" if value is None else f"{value:.1f}"


def exit_code_for(result: SessionResult, outcomes: list | None = None) -> int:
    """Map a call outcome to an exit code.

    Kept as a function because tests and downstream code import it. The logic
    itself is :attr:`streamdouble.api.CallReport.exit_code` -- this builds the
    smallest report that can answer the question rather than reimplementing it,
    so the two can never disagree.
    """
    return CallReport(result=result, metrics=compute(result), thresholds=outcomes or []).exit_code


def render(args: argparse.Namespace, call_report: CallReport) -> int:
    """Render a finished call and return its exit code.

    Everything this function does is presentation. It computes nothing about
    the call -- the numbers, the threshold outcomes and the exit code all
    arrive already decided on the report -- so the CLI cannot report something
    the API would not.
    """
    result = call_report.result

    if args.out and not api.save_reply(call_report, args.out):
        # An agent that never spoke leaves no file at all, rather than a
        # zero-length WAV that looks like a successful recording of silence.
        print(
            f"streamdouble: no audio received, so {args.out} was not written",
            file=sys.stderr,
        )

    if args.json:
        # JSON goes to stdout alone, so a pipeline can consume it directly
        # without having to strip a human preamble. Everything else this
        # command says in --json mode goes to stderr.
        print(json.dumps(call_report.to_dict(), indent=2))
    elif not args.quiet:
        report(result, call_report.metrics, call_report.thresholds)
        if args.out and result.audio_received:
            print(f"\nwrote {args.out} ({result.audio_duration_s:.2f}s)")
        if call_report.trace_path:
            print(f"wrote {call_report.trace_path} (frame trace)")

    return call_report.exit_code


async def run_call_command(args: argparse.Namespace) -> int:
    try:
        params = parse_params(args.param)
    except ValueError as exc:
        print(f"streamdouble: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        frames = audio.wav_to_ulaw_frames(args.audio)
    except audio.AudioError as exc:
        print(f"streamdouble: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if not frames:
        print(f"streamdouble: {args.audio} contains no audio", file=sys.stderr)
        return EXIT_USAGE

    if args.repeat < 1:
        print("streamdouble: --repeat must be at least 1", file=sys.stderr)
        return EXIT_USAGE

    if not args.quiet and not args.json:
        suffix = f" x{args.repeat}" if args.repeat > 1 else ""
        print(
            f"calling {args.url} with {len(frames)} frames "
            f"({len(frames) * audio.FRAME_MS / 1000:.2f}s of audio){suffix}"
        )

    config = config_from(args, params)
    try:
        if args.repeat > 1 or args.save_baseline or args.baseline:
            return await run_series(args, frames, config)

        call_report = await api.call(
            args.url,
            frames=frames,
            config=config,
            thresholds=thresholds_from(args),
            trace=trace_from(args),
        )
    except ConnectionFailed as exc:
        print(f"streamdouble: {exc}", file=sys.stderr)
        return EXIT_CONNECTION_FAILED
    except BaselineError as exc:
        print(f"streamdouble: {exc}", file=sys.stderr)
        return EXIT_USAGE

    return render(args, call_report)


async def run_series(args: argparse.Namespace, frames: list[bytes], config) -> int:
    """Place several calls, summarise them, and compare against a baseline.

    A regression exits 1 -- the existing assertion-failed code -- rather than a
    new sixth one. The exit-code contract is documented and stable, and a
    regression is an assertion about a number that turned out false, which is
    exactly what 1 already means.
    """
    fingerprint = baseline_module.fingerprint(
        audio_path=args.audio if getattr(args, "audio", None) else None,
        frames=len(frames),
        chaos_seed=args.chaos_seed,
        impairments=impairments_from(args).describe(),
    )

    # Load and check the baseline *before* placing a single call. Everything
    # comparability depends on -- the clip's hash, the seed, the impairments --
    # is known now, so discovering a mismatch afterwards means having spent
    # twenty real calls to learn something that was true before the first one.
    #
    # The same principle the scenario loader already follows: validated before
    # the socket opens, so a typo cannot fail halfway through with the agent
    # mid-sentence.
    stored = None
    if args.baseline:
        stored = baseline_module.load(args.baseline)
        baseline_module.check_comparable(stored, RunSeries(metrics=(), fingerprint=fingerprint))

    def progress(index: int, report) -> None:
        if args.quiet or args.json or args.repeat < 2:
            return
        figure = report.time_to_first_audio_ms
        rendered = "no audio" if figure is None else f"{figure:.0f} ms"
        print(f"  run {index + 1}/{args.repeat}: {rendered}")

    series, reports = await api.call_series(
        args.url,
        runs=args.repeat,
        fingerprint=fingerprint,
        on_run=progress,
        frames=frames,
        config=config,
        thresholds=thresholds_from(args),
        trace=trace_from(args),
    )
    payloads = [report.to_dict() for report in reports]

    comparison = None
    if stored is not None:
        comparison = baseline_module.compare(
            stored,
            series,
            tolerance_pct=args.tolerance_pct,
            tolerance_ms=args.tolerance_ms,
        )

    if args.save_baseline:
        baseline_module.save(args.save_baseline, series, payloads)

    if args.json:
        payload = {
            "series": series.to_dict(),
            "runs": payloads,
        }
        if comparison is not None:
            payload["comparison"] = comparison.to_dict()
        payload["exit_code"] = _series_exit_code(reports, comparison)
        print(json.dumps(payload, indent=2))
    elif not args.quiet:
        report_series(series)
        if comparison is not None:
            report_comparison(comparison)
        if args.save_baseline:
            print()
            print(f"wrote {args.save_baseline} (baseline)")

    return _series_exit_code(reports, comparison)


def _series_exit_code(reports: list, comparison) -> int:
    """The worst outcome across the series wins.

    A regression is EXIT_ASSERTION_FAILED, the same as a failed threshold,
    because it is the same kind of fact: a number was measured and found
    wanting. A more definite outcome in any single run -- a protocol violation,
    a timeout -- outranks it, on the same severity ordering one call uses.
    """
    for code in (EXIT_PROTOCOL_VIOLATION, EXIT_TIMEOUT):
        if any(report.exit_code == code for report in reports):
            return code
    if comparison is not None and not comparison.passed:
        return EXIT_ASSERTION_FAILED
    if any(report.exit_code != EXIT_OK for report in reports):
        return EXIT_ASSERTION_FAILED
    return EXIT_OK


async def run_scenario_command(args: argparse.Namespace) -> int:
    try:
        params = parse_params(args.param)
    except ValueError as exc:
        print(f"streamdouble: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        script = load_scenario(args.file)
    except ScenarioError as exc:
        # Validation happens here, before a socket is opened: a typo should not
        # fail halfway through a call with the agent left mid-sentence.
        print(f"streamdouble: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if not args.quiet and not args.json:
        print(script.describe())

    try:
        call_report = await api.run_scenario(
            args.url,
            script,
            config=config_from(args, params),
            thresholds=thresholds_from(args),
            trace=trace_from(args),
        )
    except ConnectionFailed as exc:
        print(f"streamdouble: {exc}", file=sys.stderr)
        return EXIT_CONNECTION_FAILED

    return render(args, call_report)


#: Subcommand name to the coroutine that runs it.
#:
#: Module level so a test can check it against the parser's own subcommand
#: list. `streamdouble scenario` parsed happily and then died with "unknown
#: command" for its whole life in a public repository, because every scenario
#: test drove the Python API instead of the CLI. A documented feature was
#: unreachable from the command line and nothing noticed.
RUNNERS = {"call": run_call_command, "scenario": run_scenario_command}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    runner = RUNNERS.get(args.command)
    if runner is not None:
        try:
            return asyncio.run(runner(args))
        except KeyboardInterrupt:
            print("\nstreamdouble: interrupted", file=sys.stderr)
            return EXIT_USAGE

    parser.error(f"unknown command {args.command!r}")
    return EXIT_USAGE  # unreachable; parser.error exits


if __name__ == "__main__":
    sys.exit(main())
