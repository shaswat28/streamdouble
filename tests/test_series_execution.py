"""Repeat runs and baseline comparison, against a real agent over a real socket.

`test_aggregate.py` and `test_baseline.py` prove the arithmetic given payloads.
They cannot prove the payloads describe reality, or that the whole thing holds
together over a socket -- so this file injects a known regression and checks it
is caught, and runs the clean case repeatedly and checks it is not.

**Both halves matter equally.** A detector that never fires is useless; one
that fires on noise gets `continue-on-error` added to it, after which it is
also useless but looks fine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from streamdouble import api
from streamdouble.cli import main
from streamdouble.session import SessionConfig

#: Marker applied per-test rather than per-module.
#:
#: The tests that drive the command line must be *sync*: `cli.main` calls
#: `asyncio.run`, which cannot run inside an already-running loop, and a module
#: -wide asyncio marker would put every test inside one. Sharing a loop across
#: the async tests is still worth doing -- see tests/test_api.py for why -- so
#: the marker goes on those individually.
ASYNC = pytest.mark.asyncio(loop_scope="module")

FAST = SessionConfig(response_timeout_s=5.0, quiet_period_s=0.3, max_drain_s=3.0)

#: Enough runs for a median to be steadier than one sample, few enough that
#: this file does not dominate the suite's runtime.
RUNS = 3

ROOT_DIR = Path(__file__).resolve().parent.parent


def cli_args(server: str, clip: Path, *extra: str) -> list[str]:
    return [
        "call", server,
        "--audio", str(clip),
        "--quiet-period", "0.3",
        "--max-drain", "3",
        "--response-timeout", "5",
        *extra,
    ]


# ---------------------------------------------------------------------------
# The series itself
# ---------------------------------------------------------------------------


@ASYNC
async def test_a_series_places_every_run_and_summarises_them(server, speech_8k_path):
    series, reports = await api.call_series(
        server, runs=RUNS, audio_path=speech_8k_path, config=FAST
    )

    assert len(reports) == RUNS
    assert series.n == RUNS
    assert series.failures == 0

    metric = series.metric("time_to_first_audio_ms")
    assert len(metric.measured) == RUNS
    assert metric.median is not None
    # Every run is a distinct call, so the stream ids must all differ.
    assert len({report.stream_sid for report in reports}) == RUNS


@ASYNC
async def test_a_series_of_one_is_still_a_series(server, speech_8k_path):
    series, reports = await api.call_series(
        server, runs=1, audio_path=speech_8k_path, config=FAST
    )
    assert series.n == 1
    assert len(reports) == 1


@ASYNC
async def test_zero_runs_is_refused(server, speech_8k_path):
    with pytest.raises(ValueError, match="at least 1"):
        await api.call_series(server, runs=0, audio_path=speech_8k_path, config=FAST)


@ASYNC
async def test_a_silent_agent_leaves_the_counts_honest(server, speech_8k_path):
    """`n` runs, zero measured -- and the median is absent, not zero."""
    series, _ = await api.call_series(
        f"{server}?mode=silent",
        runs=2,
        audio_path=speech_8k_path,
        config=SessionConfig(response_timeout_s=2.0, quiet_period_s=0.3, max_drain_s=2.0),
    )

    metric = series.metric("time_to_first_audio_ms")
    assert series.n == 2
    assert metric.missing == 2
    assert metric.median is None
    assert series.failures == 2


# ---------------------------------------------------------------------------
# Regression detection, end to end
# ---------------------------------------------------------------------------


def test_an_injected_delay_is_caught_as_a_regression(
    server, speech_8k_path, tmp_path: Path
):
    """Record a baseline, then make the agent measurably slower.

    400 ms against an agent that normally answers in about 180 ms is well past
    both tolerances, and is the kind of change a caller would hear.
    """
    baseline_path = tmp_path / "base.json"
    assert main(cli_args(server, speech_8k_path, "-n", str(RUNS),
                         "--save-baseline", str(baseline_path), "--quiet")) == 0
    assert baseline_path.exists()

    code = main(cli_args(f"{server}?delay_ms=400", speech_8k_path, "-n", str(RUNS),
                         "--baseline", str(baseline_path), "--quiet"))

    assert code == 1, "a 400 ms regression should fail the build"


def test_the_clean_case_does_not_fire(server, speech_8k_path, tmp_path: Path):
    """Run the unchanged agent against its own baseline, repeatedly.

    Three consecutive passes rather than one, because a detector that fails one
    run in three is one people learn to re-run -- and by this project's own
    standard that is worse than not having it.
    """
    baseline_path = tmp_path / "base.json"
    assert main(cli_args(server, speech_8k_path, "-n", str(RUNS),
                         "--save-baseline", str(baseline_path), "--quiet")) == 0

    for attempt in range(3):
        code = main(cli_args(server, speech_8k_path, "-n", str(RUNS),
                             "--baseline", str(baseline_path), "--quiet"))
        assert code == 0, f"false regression on attempt {attempt + 1}"


def test_a_baseline_from_a_different_clip_is_refused_before_any_call(
    server, speech_8k_path, fixture_dir: Path, tmp_path: Path, capsys
):
    """And refused *first*, without placing the calls.

    Everything comparability depends on is known before the first call, so
    discovering a mismatch afterwards means having spent a whole series to
    learn something that was already true. The same principle the scenario
    loader follows: validated before the socket opens.
    """
    baseline_path = tmp_path / "base.json"
    assert main(cli_args(server, speech_8k_path, "-n", "2",
                         "--save-baseline", str(baseline_path), "--quiet")) == 0
    capsys.readouterr()

    code = main(cli_args(server, fixture_dir / "tone_440_8k.wav", "-n", "5",
                         "--baseline", str(baseline_path), "--quiet"))
    captured = capsys.readouterr()

    assert code == 4, "an incomparable baseline is a usage error, not a regression"
    assert "different audio clip" in captured.err
    assert "run 1/5" not in captured.out, "calls were placed before the baseline was checked"


def test_a_timeout_outranks_a_regression(server, speech_8k_path, tmp_path: Path):
    """Severity ordering survives aggregation.

    A silent agent both regresses and times out. Reporting the regression would
    lose the more specific fact, which is the same ordering a single call uses.
    """
    baseline_path = tmp_path / "base.json"
    assert main(cli_args(server, speech_8k_path, "-n", "2",
                         "--save-baseline", str(baseline_path), "--quiet")) == 0

    code = main([
        "call", f"{server}?mode=silent",
        "--audio", str(speech_8k_path),
        "-n", "2",
        "--baseline", str(baseline_path),
        "--quiet",
        "--quiet-period", "0.3", "--max-drain", "2", "--response-timeout", "2",
    ])

    assert code == 2, "the timeout is the more specific outcome and should win"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def test_the_summary_says_why_the_percentile_is_missing(
    server, speech_8k_path, capsys
):
    """A blank cell that might mean zero is the ambiguity this project treats
    as a defect."""
    main(cli_args(server, speech_8k_path, "-n", "2"))
    out = capsys.readouterr().out

    assert "p95 withheld" in out
    assert "below the 20 needed" in out


def test_json_mode_emits_one_object_for_the_whole_series(server, speech_8k_path):
    """Run in a subprocess, and not for isolation's sake.

    The example agent runs uvicorn in a *thread of this process*, so its own
    per-call logging goes to the same stdout that `capsys` captures. In a real
    terminal the agent is a separate process and `--json` is alone on stdout --
    which is the property being tested, so testing it in-process would be
    testing the wrong arrangement and failing for the wrong reason.
    """
    import json as json_module
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, "-m", "streamdouble.cli",
         *cli_args(server, speech_8k_path, "-n", "2", "--json")],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT_DIR,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json_module.loads(completed.stdout)

    assert payload["series"]["n"] == 2
    assert len(payload["runs"]) == 2
    assert "exit_code" in payload
    assert payload["series"]["fingerprint"]["audio_sha256"]


def test_a_missing_figure_renders_as_a_dash_not_a_zero(
    server, speech_8k_path, capsys
):
    """The table is where "None is not zero" is easiest to lose."""
    main([
        "call", f"{server}?mode=silent",
        "--audio", str(speech_8k_path),
        "-n", "2",
        "--quiet-period", "0.3", "--max-drain", "2", "--response-timeout", "2",
    ])
    out = capsys.readouterr().out

    first_audio_row = next(line for line in out.splitlines() if "first audio" in line)
    assert "0.0" not in first_audio_row, (
        f"a missing measurement rendered as zero: {first_audio_row}"
    )
    assert "-" in first_audio_row
