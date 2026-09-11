"""Command line interface.

The product is the CLI and CI, so this file is a user-facing surface rather than
a thin wrapper: what it prints, and what it exits with, is most of what anyone
will ever see of this package.

Phase 2 scope is the ``call`` command. Threshold flags, ``--json``, and the full
exit-code contract arrive with the metrics layer in Phase 3; the exit codes
defined here are the subset that is already meaningful, and they keep their
meanings when the rest are added.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import __version__, audio
from .chaos import Impairments
from .metrics import (
    CONVERSATIONAL_FLOW_MS,
    Metrics,
    Threshold,
    compute,
    evaluate_thresholds,
)
from .scenario import ScenarioError
from .scenario import load as load_scenario
from .session import Session, SessionConfig, SessionResult

__all__ = ["build_parser", "main"]

#: Exit codes. Stable, and chosen so CI can distinguish outcomes without
#: parsing output. Reserved for their Phase 3 meanings from the start, so that
#: nothing which works today changes meaning later.
EXIT_OK = 0
EXIT_ASSERTION_FAILED = 1
EXIT_TIMEOUT = 2
EXIT_PROTOCOL_VIOLATION = 3
EXIT_USAGE = 4
EXIT_CONNECTION_FAILED = 5


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
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


def exit_code_for(result: SessionResult, outcomes: list | None = None) -> int:
    """Map a call outcome to an exit code.

    Ordered by severity, most definite first. A protocol violation is a certain
    bug; a timeout is a specific, informative outcome; a failed threshold is a
    judgement about a number that was measured successfully. Reporting the
    vaguest of the three when a more specific one applies would lose
    information a pipeline could have acted on.
    """
    if result.violations:
        return EXIT_PROTOCOL_VIOLATION
    if result.timed_out:
        return EXIT_TIMEOUT
    if result.failed_expectations:
        return EXIT_ASSERTION_FAILED
    if any(not outcome.passed for outcome in outcomes or []):
        return EXIT_ASSERTION_FAILED
    return EXIT_OK


async def place_call(args: argparse.Namespace, session: Session, banner: str) -> int:
    """Run a prepared session and report it. Shared by `call` and `scenario`."""
    if not args.quiet and not args.json:
        print(banner)

    try:
        result = await session.run()
    except (OSError, TimeoutError) as exc:
        # Covers a refused connection, an unresolvable host, and a handshake
        # that never completed -- from the user's point of view one situation:
        # the agent is not reachable at that URL.
        print(f"streamdouble: could not connect to {args.url}: {exc}", file=sys.stderr)
        return EXIT_CONNECTION_FAILED

    metrics = compute(result)
    outcomes = evaluate_thresholds(metrics, thresholds_from(args))

    if args.out:
        if not result.audio_received:
            print(
                f"streamdouble: no audio received, so {args.out} was not written",
                file=sys.stderr,
            )
        else:
            audio.ulaw_to_wav(result.audio_received, args.out)

    if args.json:
        # JSON goes to stdout alone, so a pipeline can consume it directly
        # without having to strip a human preamble. Everything else this
        # command says in --json mode goes to stderr.
        payload = metrics.to_dict()
        payload["stream_sid"] = result.identity.stream_sid
        payload["frames_dropped"] = result.frames_dropped
        payload["expectations"] = [
            {"what": what, "passed": passed} for what, passed in result.expectations
        ]
        payload["thresholds"] = [
            {
                "name": outcome.threshold.name,
                "limit_ms": outcome.threshold.limit,
                "value_ms": None if outcome.value is None else round(outcome.value, 1),
                "passed": outcome.passed,
            }
            for outcome in outcomes
        ]
        payload["exit_code"] = exit_code_for(result, outcomes)
        print(json.dumps(payload, indent=2))
    elif not args.quiet:
        report(result, metrics, outcomes)
        if args.out and result.audio_received:
            print(f"\nwrote {args.out} ({result.audio_duration_s:.2f}s)")

    return exit_code_for(result, outcomes)


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

    session = Session(args.url, frames, config=config_from(args, params))
    banner = (
        f"calling {args.url} with {len(frames)} frames "
        f"({len(frames) * audio.FRAME_MS / 1000:.2f}s of audio)"
    )
    return await place_call(args, session, banner)


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

    session = Session(args.url, scenario=script, config=config_from(args, params))
    return await place_call(args, session, script.describe())


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
