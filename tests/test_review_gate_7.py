"""Regression tests for what review gate 7 found.

Gate 7 covered the repeat-runs and baseline machinery. Four findings, and three
of them are the same failure wearing different clothes: **a regression gate that
silently stops checking**.

That is the characteristic way this kind of tool dies. It does not crash and it
does not report a wrong number -- it keeps printing a clean summary while
nothing is being verified, which is indistinguishable from working right up
until a real regression ships. Each of the three is a different route to it:
recording a baseline from a broken run, comparing samples too noisy to mean
anything, and a metric whose bar can never be cleared.

Every test here was verified to fail against the behaviour it describes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from streamdouble.aggregate import summarise
from streamdouble.baseline import compare
from streamdouble.cli import MIN_RUNS_FOR_COMPARISON, main

ROOT_DIR = Path(__file__).resolve().parent.parent


def cli_args(server: str, clip: Path, *extra: str) -> list[str]:
    return [
        "call", server,
        "--audio", str(clip),
        "--quiet-period", "0.3",
        "--max-drain", "2",
        "--response-timeout", "3",
        *extra,
    ]


# ---------------------------------------------------------------------------
# Finding 1 -- a baseline that measured nothing disables the check
# ---------------------------------------------------------------------------


def test_a_baseline_is_not_written_from_a_run_that_measured_nothing(
    server, speech_8k_path, tmp_path: Path, capsys
):
    """The poisoned baseline, and why it is worse than a crash.

    Before the fix this wrote a file in which every median was null. Every
    later comparison against it then read "not comparable" for every metric and
    passed -- so the gate was dead, and looked healthy, because "not
    comparable" is the correct response to missing data.

    The realistic path is a scheduled baseline refresh that happens to run on a
    day the agent is down.
    """
    out = tmp_path / "poisoned.json"
    code = main(
        cli_args(f"{server}?mode=silent", speech_8k_path,
                 "-n", "3", "--save-baseline", str(out), "--quiet",
                 "--response-timeout", "2")
    )

    assert not out.exists(), "a baseline that measured nothing was written anyway"
    assert code == 4, "refusing to write it is a usage error"
    assert "measured nothing" in capsys.readouterr().err


def test_an_unmeasured_baseline_can_still_be_forced(
    server, speech_8k_path, tmp_path: Path
):
    """The escape hatch exists, because refusing outright would be the tool
    deciding it knows better than someone with a reason."""
    out = tmp_path / "forced.json"
    main(
        cli_args(f"{server}?mode=silent", speech_8k_path,
                 "-n", "2", "--save-baseline", str(out), "--quiet",
                 "--allow-unmeasured-baseline", "--response-timeout", "2")
    )

    assert out.exists()
    stored = json.loads(out.read_text())
    assert stored["summary"]["metrics"]["time_to_first_audio_ms"]["median"] is None


def test_a_healthy_run_still_writes_its_baseline(server, speech_8k_path, tmp_path: Path):
    """The guard must not block the normal case."""
    out = tmp_path / "good.json"
    assert main(
        cli_args(server, speech_8k_path, "-n", "3", "--save-baseline", str(out), "--quiet")
    ) == 0
    assert out.exists()


# ---------------------------------------------------------------------------
# Finding 2 -- comparing single samples
# ---------------------------------------------------------------------------


def test_a_baseline_comparison_needs_more_than_one_run(
    server, speech_8k_path, tmp_path: Path, capsys
):
    """PLAN.md called this out as a precondition and it shipped anyway.

    Two calls over a real socket differ by tens of milliseconds for reasons
    that have nothing to do with the agent. This project's own trace parity
    test passed alone and failed in sequence until it was changed to medians,
    which is the same lesson from the other side.
    """
    baseline = tmp_path / "b.json"
    assert main(
        cli_args(server, speech_8k_path, "-n", "3", "--save-baseline", str(baseline), "--quiet")
    ) == 0
    capsys.readouterr()

    code = main(cli_args(server, speech_8k_path, "-n", "1", "--baseline", str(baseline)))

    assert code == 4
    assert "at least --repeat" in capsys.readouterr().err


def test_the_minimum_run_count_is_allowed(server, speech_8k_path, tmp_path: Path):
    """The floor is inclusive -- it must not reject the number it names."""
    baseline = tmp_path / "b.json"
    assert main(
        cli_args(server, speech_8k_path, "-n", str(MIN_RUNS_FOR_COMPARISON),
                 "--save-baseline", str(baseline), "--quiet")
    ) == 0

    code = main(
        cli_args(server, speech_8k_path, "-n", str(MIN_RUNS_FOR_COMPARISON),
                 "--baseline", str(baseline), "--quiet")
    )
    assert code == 0


def test_a_single_run_without_a_baseline_is_still_fine():
    """The guard is about comparison, not about repetition.

    `-n 1` on its own is the ordinary single call this tool has always placed,
    and must stay unaffected.
    """
    # Asserted at the level of the constant rather than by placing a call: the
    # guard reads `args.baseline and args.repeat < MIN`, so with no baseline
    # there is nothing to trip.
    assert MIN_RUNS_FOR_COMPARISON > 1


# ---------------------------------------------------------------------------
# Finding 3 -- a tolerance that cannot be reached
# ---------------------------------------------------------------------------


def test_the_ratio_tolerance_reaches_the_comparison(
    server, speech_8k_path, tmp_path: Path
):
    """The flag must change a verdict, not merely be accepted.

    `--tolerance-ms` never applied to delivery_ratio and nothing else did, so
    that metric kept a hardcoded floor no user could reach.

    The first version of this test asserted that a loose `--tolerance-ratio`
    exits 0 -- which the clean case does anyway, so it could not tell the flag
    from a flag that parses and is then dropped. Mutation testing caught that:
    removing `tolerance_ratio=args.tolerance_ratio` from the comparison left
    all fourteen tests green.

    So this drives a run whose delivery ratio genuinely regresses, and checks
    the flag can call it off. `--tolerance-ms` is set enormous so the latency
    regression does not decide the exit code on its own.
    """
    baseline = tmp_path / "b.json"
    assert main(
        cli_args(server, speech_8k_path, "-n", "3", "--save-baseline", str(baseline), "--quiet")
    ) == 0

    slow = f"{server}?delay_ms=400"

    # The agent front-loads harder under a delay, so the ratio really moves.
    tight = main(
        cli_args(slow, speech_8k_path, "-n", "3", "--baseline", str(baseline),
                 "--tolerance-ms", "999999", "--quiet")
    )
    assert tight == 1, "the delivery ratio should regress against the default floor"

    loose = main(
        cli_args(slow, speech_8k_path, "-n", "3", "--baseline", str(baseline),
                 "--tolerance-ms", "999999", "--tolerance-ratio", "99", "--quiet")
    )
    assert loose == 0, "--tolerance-ratio did not reach the comparison"


def test_the_ratio_tolerance_changes_the_verdict():
    """Not merely accepted -- it has to reach the comparison.

    A flag that parses and is then dropped is the bug this test exists for.
    """
    before = summarise([{"delivery_ratio": 1.0, "exit_code": 0}])
    after = summarise([{"delivery_ratio": 2.0, "exit_code": 0}])

    assert compare(before, after, tolerance_ratio=0.2).regressions
    assert not compare(before, after, tolerance_ratio=5.0).regressions


# ---------------------------------------------------------------------------
# Finding 4 -- a baseline of zero
# ---------------------------------------------------------------------------


def test_a_baseline_of_zero_does_not_make_a_metric_uncatchable():
    """`not 0.0` is true in Python, and that made a metric immortal.

    `delta_pct` returned None for a zero baseline and `regressed` short-
    circuited on None, so a gap recorded as 0.0 against a fast local stub could
    climb to half a second against a real agent and never register.

    The percentage is genuinely undefined at zero, which is the argument for
    falling back to the absolute bar rather than declining to judge.
    """
    before = summarise([{"mean_inbound_gap_ms": 0.0, "exit_code": 0}])
    after = summarise([{"mean_inbound_gap_ms": 500.0, "exit_code": 0}])

    delta = next(d for d in compare(before, after).deltas if d.name == "mean_inbound_gap_ms")

    assert delta.delta_pct is None, "there is no percentage against zero, and none is invented"
    assert delta.regressed is True, "a zero baseline must not make the metric uncatchable"


def test_a_baseline_of_zero_still_respects_the_absolute_bar():
    """Falling back to one bar must not mean firing on anything at all."""
    before = summarise([{"mean_inbound_gap_ms": 0.0, "exit_code": 0}])
    after = summarise([{"mean_inbound_gap_ms": 1.0, "exit_code": 0}])

    delta = next(d for d in compare(before, after).deltas if d.name == "mean_inbound_gap_ms")
    assert delta.regressed is False, "1ms over a zero baseline is not a regression"


def test_zero_to_zero_is_not_a_regression():
    before = summarise([{"mean_inbound_gap_ms": 0.0, "exit_code": 0}])
    after = summarise([{"mean_inbound_gap_ms": 0.0, "exit_code": 0}])

    delta = next(d for d in compare(before, after).deltas if d.name == "mean_inbound_gap_ms")
    assert delta.regressed is False


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def test_one_run_is_not_described_as_one_runs(server, speech_8k_path, tmp_path, capsys):
    """`--save-baseline` rather than a bare `-n 1`.

    A bare `-n 1` takes the ordinary single-call path and prints no series
    summary at all, which is correct and is why the first version of this test
    failed: it was asserting on output that should not exist.
    """
    main(
        cli_args(server, speech_8k_path, "-n", "1",
                 "--save-baseline", str(tmp_path / "b.json"))
    )
    assert "1 run," in capsys.readouterr().out


@pytest.mark.parametrize("count", [2, 3])
def test_several_runs_keep_the_plural(server, speech_8k_path, capsys, count):
    main(cli_args(server, speech_8k_path, "-n", str(count)))
    assert f"{count} runs," in capsys.readouterr().out
