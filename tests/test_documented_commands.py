"""Every command in the documentation is actually run.

`streamdouble scenario` was in the README, parsed correctly, and then exited
with "unknown command" for its whole life in a public repository. 364 tests did
not notice, because every one of them drove the Python API rather than the
command line a reader is told to type.

So this extracts the invocations out of the docs and runs them. Not for their
output -- that is covered elsewhere -- but to establish that each one is
well-formed and reaches a runner. A documented command that cannot start is the
worst kind of bug in a tool nobody has used before: it is the first thing they
try, and it tells them the project does not work.

The substitutions below are deliberately narrow. URLs, clip names and scenario
paths are placeholders in prose and have to become real things to run; every
flag, subcommand and argument order is left exactly as written, since those are
what the test exists to check.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from streamdouble import baseline as baseline_module
from streamdouble import cli
from streamdouble.aggregate import summarise

ROOT = Path(__file__).resolve().parent.parent

#: Documents whose bash blocks are contracts with the reader.
DOCUMENTS = ["README.md", "skill/SKILL.md"]

#: Placeholder clip names used in prose, all meaning "some WAV file".
PLACEHOLDER_CLIPS = {"hello.wav", "clip.wav", "fixtures/speech_8k.wav"}


def documented_commands() -> list[tuple[str, str]]:
    """Every `streamdouble ...` invocation in the docs, as (document, command)."""
    found = []
    for name in DOCUMENTS:
        text = (ROOT / name).read_text(encoding="utf-8")
        for block in re.findall(r"```bash\n(.*?)```", text, re.DOTALL):
            # Shell line continuations first, so a wrapped command is one command.
            joined = re.sub(r"\\\s*\n\s*", " ", block)
            for line in joined.splitlines():
                line = line.strip()
                if line.startswith("streamdouble "):
                    found.append((name, re.sub(r"\s+", " ", line)))
    return found


def test_no_bash_block_contains_a_literal_backslash_n():
    """A line continuation written as the two characters `\\` and `n`.

    The README's CI-gate example carried one for an entire published release.
    It reads as a wrapped command and copy-pastes as a broken one: the shell
    hands the program a stray argument `n`, and `shlex` does the same here --
    so that command *was* extracted and *was* run, and still did not fail,
    because `argparse` exited 2 and the only assertion checked for 4.

    Both halves are fixed. This is the half that catches it in the document,
    before anything has to run.
    """
    offenders = []
    for name in DOCUMENTS:
        text = (ROOT / name).read_text(encoding="utf-8")
        for block in re.findall(r"```bash\n(.*?)```", text, re.DOTALL):
            for number, line in enumerate(block.splitlines(), 1):
                if "\\n" in line:
                    offenders.append(f"{name} bash block line {number}: {line.strip()}")
    assert not offenders, "literal backslash-n where a line continuation belongs:\n" + "\n".join(
        offenders
    )


def test_the_docs_contain_commands_to_check():
    """Guards the extractor itself.

    A regex that silently matches nothing turns this whole file into a test that
    always passes, which is worse than not having it.
    """
    commands = documented_commands()
    assert len(commands) >= 8, f"only found {len(commands)} documented commands"
    assert any("scenario" in command for _, command in commands)
    assert any("--packet-loss" in command for _, command in commands)


@pytest.mark.timeout(120)
@pytest.mark.parametrize(
    ("document", "command"),
    documented_commands(),
    ids=lambda value: value[:40] if isinstance(value, str) else value,
)
def test_a_documented_command_runs(document, command, server, tmp_path):
    """Each documented invocation reaches a runner and is accepted.

    Asserting on the exit code rather than the output: 4 is the usage error,
    which is what an unknown subcommand, a misspelled flag or a missing required
    argument produce. Anything else means the command was understood and the
    call was attempted, which is all this is checking.
    """
    # A baseline for the documented --baseline command to compare against.
    #
    # Until output paths were redirected into tmp_path, that command passed
    # only because an *earlier* documented command had left a baseline.json in
    # the repository root -- so the test depended on its own litter, and
    # cleaning up the litter broke it. Built here explicitly, with a
    # fingerprint matching the clip the substitutions below use, because a
    # baseline recorded from a different clip is correctly refused.
    baseline_path = tmp_path / "baseline.json"
    clip = ROOT / "fixtures" / "speech_8k.wav"
    runs = [
        {"time_to_first_audio_ms": 180.0, "delivery_ratio": 1.1, "exit_code": 0}
        for _ in range(3)
    ]
    baseline_module.save(
        baseline_path,
        summarise(
            runs,
            baseline_module.fingerprint(
                audio_path=clip, frames=100, chaos_seed=0, impairments="none"
            ),
        ),
        runs,
    )

    scenario_file = tmp_path / "example.yaml"
    scenario_file.write_text(
        "name: documented\n"
        "steps:\n"
        f"  - say: {(ROOT / 'fixtures' / 'speech_8k.wav').as_posix()}\n"
        "  - wait: 0.2\n"
    )

    parts = shlex.split(command)
    assert parts[0] == "streamdouble"

    rewritten = []
    skip_next = False
    for index, part in enumerate(parts[1:]):
        if skip_next:
            skip_next = False
            continue

        if part.startswith("ws://") or part.startswith("wss://"):
            rewritten.append(server)
        elif part in {"--audio", "--agent-audio"}:
            # Both take a WAV that is a placeholder in prose. --agent-audio was
            # missed when fork mode was documented, and the README's fork
            # example then failed here on a file that never existed -- which is
            # this test working, but for the wrong reason.
            rewritten += [part, str(ROOT / "fixtures" / "speech_8k.wav")]
            skip_next = True
        elif part.endswith(".yaml"):
            rewritten.append(str(scenario_file))
        elif part == "--baseline":
            rewritten += ["--baseline", str(baseline_path)]
            skip_next = True
        elif part in {"-n", "--repeat"}:
            # The README honestly recommends 20 runs; running 20 real calls per
            # documented command would dominate the suite. The number is not
            # what this test checks -- that the command parses and runs is.
            rewritten += [part, "3"]
            skip_next = True
        elif part in {"--out", "--record-stereo", "--trace", "--save-baseline"}:
            # Output paths: redirected into tmp_path so a documented command
            # cannot litter the repository when the suite runs. call.jsonl and
            # baseline.json both got committed once before this existed.
            rewritten += [part, str(tmp_path / f"out{index}{Path(parts[index + 2]).suffix}")]
            skip_next = True
        elif part in PLACEHOLDER_CLIPS:
            rewritten.append(str(ROOT / "fixtures" / "speech_8k.wav"))
        else:
            rewritten.append(part)

    # Keep the runs short; correctness of the output is covered elsewhere.
    rewritten += ["--quiet-period", "0.3", "--max-drain", "3", "--response-timeout", "3"]

    completed = subprocess.run(
        [sys.executable, "-m", "streamdouble.cli", *rewritten],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=ROOT,
    )

    assert completed.returncode != cli.EXIT_USAGE, (
        f"{document} documents a command that does not run:\n"
        f"  {command}\n"
        f"  exit {completed.returncode}: {completed.stderr.strip()[:300]}"
    )

    # `argparse` used to exit 2 here rather than EXIT_USAGE, so the assertion
    # above could not see a bad flag or a stray argument at all -- it read them
    # as EXIT_TIMEOUT, which is to say as a slow agent. `_Parser` now raises
    # usage failures as EXIT_USAGE; this checks the stderr that goes with them,
    # so the two cannot drift apart again without a test noticing.
    assert "error: unrecognized arguments" not in completed.stderr, (
        f"{document} documents a command with a stray argument:\n"
        f"  {command}\n"
        f"  {completed.stderr.strip()[:300]}"
    )
