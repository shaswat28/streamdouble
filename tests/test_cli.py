"""Tests for the command line interface.

The CLI is the product, so its exit codes are a contract: CI pipelines will
branch on them, and changing what one means is a breaking change even though no
function signature moved. These pin each one against a real call.

The argument-parsing and formatting tests need no server; the exit-code tests
reuse the module-scoped echo agent, because an exit code that is right in
principle and wrong against a live agent is not worth much.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from conftest import free_port
from streamdouble import audio, cli
from streamdouble.metrics import Metrics, Threshold, evaluate_thresholds
from streamdouble.protocol import ProtocolViolation, StreamIdentity
from streamdouble.session import SessionResult

# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def test_version_is_reported():
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0


def test_a_missing_subcommand_is_a_usage_error():
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code != 0


def test_params_are_parsed_into_a_dict():
    assert cli.parse_params(["a=1", "b=two"]) == {"a": "1", "b": "two"}


def test_a_param_may_contain_equals_signs():
    """Only the first ``=`` separates; the rest belong to the value.

    Base64 and URLs both routinely contain ``=``, and splitting on all of them
    would silently truncate the value.
    """
    assert cli.parse_params(["token=abc=def=="]) == {"token": "abc=def=="}


def test_an_empty_param_value_is_allowed():
    assert cli.parse_params(["flag="]) == {"flag": ""}


@pytest.mark.parametrize("bad", ["novalue", "=novalue"])
def test_a_malformed_param_is_rejected(bad):
    """A parameter without a key is refused rather than dropped.

    Silently skipping it would surface much later as an agent routing bug, with
    nothing pointing back at the command line.
    """
    with pytest.raises(ValueError, match="KEY=VALUE"):
        cli.parse_params([bad])


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def make_result(**overrides) -> SessionResult:
    defaults = {
        "identity": StreamIdentity(),
        "events": [],
        "frames_sent": 100,
        "media_frames_received": 80,
        "audio_received": b"\xff" * 12800,
    }
    return SessionResult(**{**defaults, **overrides})


def render(result: SessionResult) -> str:
    buffer = io.StringIO()
    cli.report(result, stream=buffer)
    return buffer.getvalue()


def test_silence_is_reported_as_never_not_as_zero():
    """An agent that never spoke must not read as one that replied instantly.

    Formatting ``None`` as 0 ms would turn the worst outcome into the best one
    on screen, and it is the kind of thing that survives review because the
    number looks plausible.
    """
    output = render(make_result(first_audio_at=None))
    assert "never" in output
    assert "0 ms" not in output


def test_a_measured_latency_is_shown_in_milliseconds():
    output = render(make_result(started_at=100.0, first_audio_at=100.35))
    assert "350 ms" in output


def test_violations_are_listed_with_their_codes():
    result = make_result(
        violations=[
            ProtocolViolation("bad_base64", "media.payload is not valid base64"),
            ProtocolViolation("missing_stream_sid", "'clear' frame is missing a streamSid"),
        ]
    )
    output = render(result)

    assert "2 protocol violation(s)" in output
    assert "[bad_base64]" in output
    assert "[missing_stream_sid]" in output


def test_a_timeout_is_called_out():
    assert "no audio" in render(make_result(timed_out=True))


def test_an_early_close_is_called_out():
    assert "closed the connection" in render(make_result(closed_early=True))


# --------------------------------------------------------------------------
# Exit codes
# --------------------------------------------------------------------------


def test_exit_code_is_zero_for_a_clean_call():
    assert cli.exit_code_for(make_result()) == cli.EXIT_OK


def test_a_violation_outranks_a_timeout():
    """Severity ordering, pinned.

    A malformed frame is a definite bug; a timeout may only be a slow agent. A
    run with both should report the definite one.
    """
    result = make_result(
        timed_out=True,
        violations=[ProtocolViolation("bad_base64", "bad")],
    )
    assert cli.exit_code_for(result) == cli.EXIT_PROTOCOL_VIOLATION


def test_a_timeout_alone_is_exit_two():
    assert cli.exit_code_for(make_result(timed_out=True)) == cli.EXIT_TIMEOUT


def test_exit_codes_are_distinct():
    """No two outcomes share a code.

    They are a contract for CI, so a collision would make two different
    situations indistinguishable to a pipeline.
    """
    codes = [
        cli.EXIT_OK,
        cli.EXIT_ASSERTION_FAILED,
        cli.EXIT_TIMEOUT,
        cli.EXIT_PROTOCOL_VIOLATION,
        cli.EXIT_USAGE,
        cli.EXIT_CONNECTION_FAILED,
    ]
    assert len(set(codes)) == len(codes)


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


@pytest.mark.timeout(60)
def test_a_healthy_call_exits_zero_and_writes_the_reply(server, tmp_path, capsys):
    out = tmp_path / "reply.wav"
    code = cli.main(
        ["call", server, "--audio", "fixtures/speech_8k.wav",
         "--out", str(out), "--quiet-period", "0.3"]
    )

    assert code == cli.EXIT_OK
    assert out.exists()

    # The written file is a real 8 kHz mono WAV, not merely a file that exists.
    with wave.open(str(out), "rb") as wav:
        assert wav.getframerate() == audio.SAMPLE_RATE
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getnframes() > 0


@pytest.mark.timeout(60)
def test_a_silent_agent_exits_two(server, tmp_path):
    code = cli.main(
        ["call", f"{server}?mode=silent", "--audio", "fixtures/speech_8k.wav",
         "--response-timeout", "1", "--quiet"]
    )
    assert code == cli.EXIT_TIMEOUT


@pytest.mark.timeout(60)
def test_a_misbehaving_agent_exits_three(server):
    code = cli.main(
        ["call", f"{server}?garbage_after=12", "--audio", "fixtures/speech_8k.wav",
         "--quiet-period", "0.3", "--quiet"]
    )
    assert code == cli.EXIT_PROTOCOL_VIOLATION


@pytest.mark.timeout(60)
def test_an_unreachable_agent_exits_five():
    port = free_port()  # nothing is listening
    code = cli.main(
        ["call", f"ws://127.0.0.1:{port}/media-stream",
         "--audio", "fixtures/speech_8k.wav", "--quiet"]
    )
    assert code == cli.EXIT_CONNECTION_FAILED


@pytest.mark.timeout(60)
def test_a_missing_audio_file_exits_four(server):
    assert cli.main(["call", server, "--audio", "does-not-exist.wav"]) == cli.EXIT_USAGE


@pytest.mark.timeout(60)
def test_no_output_file_is_written_when_nothing_was_said(server, tmp_path, capsys):
    """A timed-out call leaves no WAV behind.

    Writing an empty or silent file would be worse than writing nothing: it
    looks like a successful recording, and the next person to open it concludes
    the agent produced silence rather than nothing at all.
    """
    out = tmp_path / "should-not-exist.wav"
    code = cli.main(
        ["call", f"{server}?mode=silent", "--audio", "fixtures/speech_8k.wav",
         "--response-timeout", "1", "--out", str(out)]
    )

    assert code == cli.EXIT_TIMEOUT
    assert not out.exists()
    assert "not written" in capsys.readouterr().err


# --------------------------------------------------------------------------
# CI gates and JSON output
# --------------------------------------------------------------------------


def parse_args(argv: list[str]):
    return cli.build_parser().parse_args(argv)


BASE = ["call", "ws://x/y", "--audio", "a.wav"]


def test_no_thresholds_are_set_by_default():
    """A plain call reports; it does not assert.

    Thresholds have to be asked for, so that adding metrics did not silently
    turn every existing invocation into a pass/fail check.
    """
    assert cli.thresholds_from(parse_args(BASE)) == []


def test_a_latency_threshold_is_built_from_the_flag():
    [threshold] = cli.thresholds_from(parse_args([*BASE, "--max-first-audio-ms", "800"]))

    assert threshold.attribute == "time_to_first_audio_ms"
    assert threshold.limit == 800
    assert threshold.missing_fails


def test_thresholds_fail_on_missing_data_unless_told_otherwise():
    """The default is strict, and the escape hatch is explicit.

    An agent that said nothing has not met an 800 ms target. Defaulting the
    other way is how a completely broken agent gets a green build.
    """
    strict = cli.thresholds_from(parse_args([*BASE, "--max-first-audio-ms", "800"]))
    lenient = cli.thresholds_from(
        parse_args([*BASE, "--max-first-audio-ms", "800", "--allow-no-audio"])
    )

    assert strict[0].missing_fails
    assert not lenient[0].missing_fails


def test_both_gate_flags_can_be_used_together():
    thresholds = cli.thresholds_from(
        parse_args([*BASE, "--max-first-audio-ms", "800", "--max-gap-ms", "500"])
    )
    assert {t.attribute for t in thresholds} == {
        "time_to_first_audio_ms",
        "max_inbound_gap_ms",
    }


def test_a_failed_threshold_exits_one():
    """Exit 1 is reserved for a measured value that missed its target."""
    metrics = Metrics(time_to_first_audio_ms=1200)
    outcomes = evaluate_thresholds(
        metrics,
        [Threshold(name="first audio", limit=800, attribute="time_to_first_audio_ms")],
    )
    assert cli.exit_code_for(make_result(), outcomes) == cli.EXIT_ASSERTION_FAILED


def test_a_timeout_outranks_a_failed_threshold():
    """Severity ordering: the more specific outcome wins.

    A timeout says exactly what went wrong. A failed threshold on a value that
    was never measured says the same thing less precisely, so reporting the
    latter would discard information a pipeline could act on.
    """
    outcomes = evaluate_thresholds(
        Metrics(time_to_first_audio_ms=None),
        [Threshold(name="first audio", limit=800, attribute="time_to_first_audio_ms")],
    )
    assert not outcomes[0].passed
    assert cli.exit_code_for(make_result(timed_out=True), outcomes) == cli.EXIT_TIMEOUT


def test_a_violation_outranks_everything():
    outcomes = evaluate_thresholds(
        Metrics(time_to_first_audio_ms=9999),
        [Threshold(name="first audio", limit=800, attribute="time_to_first_audio_ms")],
    )
    result = make_result(timed_out=True, violations=[ProtocolViolation("bad_base64", "x")])
    assert cli.exit_code_for(result, outcomes) == cli.EXIT_PROTOCOL_VIOLATION


def test_threshold_outcomes_appear_in_the_human_report():
    outcomes = evaluate_thresholds(
        Metrics(time_to_first_audio_ms=1200),
        [Threshold(name="first audio", limit=800, attribute="time_to_first_audio_ms")],
    )
    buffer = io.StringIO()
    cli.report(make_result(), Metrics(time_to_first_audio_ms=1200), outcomes, stream=buffer)

    output = buffer.getvalue()
    assert "first audio" in output
    assert "FAIL" in output


def test_unreliable_pacing_is_called_out_in_the_report():
    """The tool says when its own timing was too poor to trust.

    Reported rather than suppressed: a confident latency figure built on a
    slipping clock is the failure mode this project treats as worse than having
    no tool at all.
    """
    # Sustained slippage, not a single outlier: the verdict keys off mean
    # lateness, so one bad frame in a long call no longer condemns the run.
    metrics = Metrics(
        time_to_first_audio_ms=400,
        pacing_mean_lateness_ms=12.0,
        pacing_max_lateness_ms=60.0,
    )
    buffer = io.StringIO()
    cli.report(make_result(), metrics, [], stream=buffer)

    assert "pacing slipped" in buffer.getvalue()


def run_cli(args: list[str]) -> subprocess.CompletedProcess:
    """Invoke the installed console script in its own process.

    These three tests have to be subprocesses rather than in-process calls.
    The echo agent runs inside the test process, and it prints a connection
    trace to stdout -- so capsys sees the agent's chatter interleaved with the
    JSON and the parse fails, even though the real CLI's stdout is clean.

    Testing the actual entry point is the better check anyway: it exercises the
    console script, the argument parsing and the process exit code exactly as a
    CI pipeline would, rather than a Python function that resembles them.
    """
    return subprocess.run(
        [sys.executable, "-m", "streamdouble.cli", *args],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=Path(__file__).resolve().parent.parent,
    )


@pytest.mark.timeout(120)
def test_json_output_is_the_only_thing_on_stdout(server):
    """stdout carries JSON and nothing else, so a pipeline can pipe it straight.

    A human preamble on stdout would force every consumer to strip it, and the
    ones that forget would fail confusingly.
    """
    completed = run_cli(
        ["call", server, "--audio", "fixtures/speech_8k.wav",
         "--quiet-period", "0.3", "--json"]
    )
    assert completed.returncode == cli.EXIT_OK, completed.stderr

    payload = json.loads(completed.stdout)
    assert payload["media_frames_received"] > 0
    assert payload["exit_code"] == cli.EXIT_OK
    assert "stream_sid" in payload


@pytest.mark.timeout(120)
def test_json_reports_a_failed_threshold(server):
    completed = run_cli(
        ["call", server, "--audio", "fixtures/speech_8k.wav",
         "--quiet-period", "0.3", "--max-first-audio-ms", "1", "--json"]
    )
    assert completed.returncode == cli.EXIT_ASSERTION_FAILED, completed.stderr

    payload = json.loads(completed.stdout)
    [threshold] = payload["thresholds"]
    assert threshold["passed"] is False
    assert threshold["limit_ms"] == 1
    assert payload["exit_code"] == cli.EXIT_ASSERTION_FAILED


@pytest.mark.timeout(120)
def test_json_renders_silence_as_null(server):
    """None survives to the JSON a CI pipeline actually reads."""
    completed = run_cli(
        ["call", f"{server}?mode=silent", "--audio", "fixtures/speech_8k.wav",
         "--response-timeout", "1", "--json"]
    )
    assert completed.returncode == cli.EXIT_TIMEOUT, completed.stderr

    payload = json.loads(completed.stdout)
    assert payload["time_to_first_audio_ms"] is None
    assert payload["timed_out"] is True


# --------------------------------------------------------------------------
# The scenario subcommand
# --------------------------------------------------------------------------


def test_every_subcommand_has_a_runner():
    """The parser and the dispatch table agree.

    `streamdouble scenario` parsed happily and then died with "unknown command"
    for its whole life in a public repository, because every scenario test drove
    `Session(scenario=...)` rather than the CLI. The feature was in the README
    and unreachable from the command line.

    Asserted as an invariant between the two rather than as one test per
    subcommand, so the next subcommand added without a runner fails here instead
    of shipping.
    """
    [subcommands] = [
        action for action in cli.build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    assert set(subcommands.choices) == set(cli.RUNNERS)


@pytest.mark.timeout(120)
def test_the_scenario_subcommand_runs_a_scenario(server, tmp_path):
    """End to end through the CLI, not through the Python API.

    The regression test for the dispatch bug: this fails with exit 4 and
    "unknown command 'scenario'" against the broken version.
    """
    scenario_file = tmp_path / "quick.yaml"
    scenario_file.write_text(
        "name: quick\n"
        "steps:\n"
        f"  - say: {Path('fixtures/speech_8k.wav').resolve().as_posix()}\n"
        "  - wait: 0.2\n"
    )

    completed = run_cli(
        ["scenario", str(scenario_file), server, "--quiet-period", "0.3", "--json"]
    )
    assert completed.returncode == cli.EXIT_OK, completed.stderr

    payload = json.loads(completed.stdout)
    assert payload["frames_sent"] > 0


@pytest.mark.timeout(60)
def test_a_broken_scenario_is_rejected_before_connecting(server, tmp_path):
    """A bad scenario exits 4 without opening a socket.

    Validation happens up front precisely so a typo cannot fail halfway through
    a call with the agent left mid-sentence.
    """
    scenario_file = tmp_path / "broken.yaml"
    scenario_file.write_text("steps:\n  - wiat: 1\n")

    completed = run_cli(["scenario", str(scenario_file), server, "--quiet"])
    assert completed.returncode == cli.EXIT_USAGE
    assert "wiat" in completed.stderr


@pytest.mark.timeout(60)
def test_a_failed_expectation_exits_one(server, tmp_path):
    """A scenario is a CI check: a missed `expect` fails the run."""
    scenario_file = tmp_path / "expects.yaml"
    scenario_file.write_text(
        "steps:\n"
        f"  - say: {Path('fixtures/speech_8k.wav').resolve().as_posix()}\n"
        "  - expect: clear\n"
    )

    completed = run_cli(
        ["scenario", str(scenario_file), server, "--quiet-period", "0.3", "--json"]
    )
    assert completed.returncode == cli.EXIT_ASSERTION_FAILED, completed.stderr

    payload = json.loads(completed.stdout)
    assert payload["expectations"] == [{"what": "expect clear", "passed": False}]


def test_argparse_failures_use_the_documented_usage_code():
    """A bad command line exits 4, not argparse's own 2.

    2 is EXIT_TIMEOUT here -- a documented code that means the agent never
    answered. Leaving argparse on its default told anyone who mistyped a flag
    that their agent was slow, and gave CI a typo dressed up as a test result.
    The README has promised 4 for usage errors since the first release.
    """
    for argv in (
        ["call", "ws://localhost:1", "--audio", "x.wav", "stray-argument"],
        ["call", "ws://localhost:1", "--no-such-flag"],
        ["call", "ws://localhost:1"],  # --audio is required
        ["no-such-subcommand"],
        [],  # a subcommand is required
    ):
        with pytest.raises(SystemExit) as exit_info:
            cli.main(argv)
        assert exit_info.value.code == cli.EXIT_USAGE, (
            f"{argv} exited {exit_info.value.code}, expected EXIT_USAGE"
        )


def test_help_and_version_still_exit_zero():
    """The usage-code override must not swallow the successful exits.

    `--help` and `--version` go through the same `parser.exit`, and a parser
    that returned 4 for them would break every `command --version` check in a
    packaging script.
    """
    for argv in (["--help"], ["--version"], ["call", "--help"], ["scenario", "--help"]):
        with pytest.raises(SystemExit) as exit_info:
            cli.main(argv)
        assert exit_info.value.code == 0, f"{argv} exited {exit_info.value.code}, expected 0"
