"""Scenario files: a call as a sequence of things the caller does.

A single clip of audio is one test. Real bugs live in the timing *between*
things -- the caller interrupting mid-sentence, saying nothing at all, pressing
a key, hanging up early. Those are sequences, and a sequence belongs in a file
that can be committed and replayed rather than in a shell invocation.

Steps are pure data. This module parses and validates them; :mod:`session`
executes them. Keeping the two apart means a malformed scenario is rejected
before a socket is opened, and the whole step vocabulary is testable with no
network.

A worked example::

    name: interrupt the greeting
    steps:
      - wait: 1.2                  # let the agent get going
      - say: fixtures/speech_8k.wav
      - expect: clear              # a correct agent stops talking
      - wait_for: mark
      - hangup

**``wait`` streams silence; it does not stop sending.** That distinction is the
whole reason this module exists in the shape it does. A real call carries audio
in both directions continuously for its entire duration -- when the caller is
silent, Twilio still sends 20 ms frames of near-silence, every 20 ms, until the
call ends. An agent's voice-activity detection and endpointing are built on
that: transcription services generally need the stream to keep flowing to decide
an utterance has finished. A simulator that simply stops sending after the clip
looks, to the agent, like a caller whose line went dead, and the resulting
"my agent never finalises the transcript" is a bug in the test tool rather than
in the agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import audio

__all__ = [
    "Dtmf",
    "Expect",
    "Hangup",
    "Say",
    "Scenario",
    "ScenarioError",
    "Step",
    "Wait",
    "WaitFor",
    "load",
    "parse",
]


class ScenarioError(Exception):
    """A scenario file is malformed, or refers to something that is not there."""


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One thing the simulated caller does."""

    def describe(self) -> str:  # pragma: no cover - overridden by every subclass
        return type(self).__name__.lower()


@dataclass(frozen=True)
class Say(Step):
    """Stream a WAV file as the caller's voice."""

    path: Path
    frames: list[bytes] = field(repr=False, default_factory=list)

    @property
    def duration_s(self) -> float:
        return len(self.frames) * audio.FRAME_MS / 1000

    def describe(self) -> str:
        return f"say {self.path.name} ({self.duration_s:.2f}s)"


@dataclass(frozen=True)
class Wait(Step):
    """Stream silence for a fixed time.

    Not a pause in sending -- see the module docstring. The caller is quiet;
    the call is not.
    """

    seconds: float

    def describe(self) -> str:
        return f"wait {self.seconds:g}s (streaming silence)"


@dataclass(frozen=True)
class WaitFor(Step):
    """Stream silence until the agent does something, or until a timeout.

    Recognised events:
        ``audio``  the agent's first byte of speech
        ``mark``   any mark echoed back
        ``clear``  the agent asking for buffered audio to be discarded
        ``quiet``  the agent stopping speaking for ``quiet_period``
    """

    event: str
    timeout_s: float = 10.0

    def describe(self) -> str:
        return f"wait for {self.event} (up to {self.timeout_s:g}s)"


@dataclass(frozen=True)
class Dtmf(Step):
    """Press one or more keys."""

    digits: str

    def describe(self) -> str:
        return f"press {self.digits}"


@dataclass(frozen=True)
class Expect(Step):
    """Assert something happened by this point in the call.

    Evaluated in order, against what the agent has done *so far*. A failed
    expectation does not end the call -- the rest of the scenario still runs, so
    one run reports every problem rather than only the first.
    """

    what: str
    #: Set when the assertion is that something did NOT happen.
    negated: bool = False

    def describe(self) -> str:
        return f"expect {'no ' if self.negated else ''}{self.what}"


@dataclass(frozen=True)
class Hangup(Step):
    """Close the socket abruptly, without sending ``stop``.

    What a dropped call looks like from the agent's side: no clean shutdown, no
    warning. Distinct from the end of a scenario, which hangs up politely.
    """

    def describe(self) -> str:
        return "hang up abruptly"


#: Events :class:`WaitFor` understands.
WAITABLE = frozenset({"audio", "mark", "clear", "quiet"})

#: Conditions :class:`Expect` understands.
EXPECTABLE = frozenset({"audio", "mark", "clear", "silence", "no_violations"})


@dataclass(frozen=True)
class Scenario:
    """A named sequence of steps."""

    name: str
    steps: list[Step]
    #: Where it was loaded from, for error messages. None if built in memory.
    source: Path | None = None

    @property
    def audio_duration_s(self) -> float:
        """Total caller audio, excluding waits."""
        return sum(step.duration_s for step in self.steps if isinstance(step, Say))

    def describe(self) -> str:
        lines = [f"scenario: {self.name}"]
        lines += [f"  {index + 1}. {step.describe()}" for index, step in enumerate(self.steps)]
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def load(path: str | Path) -> Scenario:
    """Read and validate a scenario from a YAML file.

    Raises:
        ScenarioError: The file is missing, malformed, or refers to audio that
            does not exist. Every failure is raised here rather than at
            execution time, so a bad scenario cannot fail halfway through a call
            with the agent left mid-sentence.
    """
    # PyYAML is a declared dependency, so this import does not normally fail.
    # The guard stays for the broken-install case, where a clear sentence beats
    # a bare ImportError from three frames down.
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ScenarioError(
            "scenario files need PyYAML, which should have come with "
            "streamdouble. Try reinstalling: pip install --force-reinstall streamdouble"
        ) from exc

    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScenarioError(f"{path}: no such file") from exc
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path}: not valid YAML ({exc})") from exc

    return parse(raw, source=path, base_dir=path.parent)


def parse(
    raw: Any, *, source: Path | None = None, base_dir: Path | None = None
) -> Scenario:
    """Validate an already-loaded scenario document.

    Args:
        raw: The parsed YAML.
        source: Where it came from, for error messages.
        base_dir: Directory that relative audio paths are resolved against.
            Defaults to the current directory. Resolving relative to the
            scenario file means a scenario and its clips can be committed
            together and stay working wherever the repository is checked out.
    """
    where = f"{source}: " if source else ""

    if not isinstance(raw, dict):
        raise ScenarioError(f"{where}a scenario must be a mapping with a 'steps' list")

    name = raw.get("name") or (source.stem if source else "scenario")
    if not isinstance(name, str):
        raise ScenarioError(f"{where}'name' must be a string")

    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ScenarioError(f"{where}'steps' must be a non-empty list")

    base = Path(base_dir) if base_dir else Path()
    steps = [
        _parse_step(entry, index, where, base) for index, entry in enumerate(raw_steps)
    ]
    return Scenario(name=name, steps=steps, source=source)


def _parse_step(entry: Any, index: int, where: str, base: Path) -> Step:
    position = f"{where}step {index + 1}: "

    # A bare string is shorthand for the steps that take no argument.
    if isinstance(entry, str):
        if entry == "hangup":
            return Hangup()
        raise ScenarioError(
            f"{position}{entry!r} is not a step. Bare strings are only 'hangup'; "
            "everything else takes a value, e.g. '- wait: 1.5'"
        )

    if not isinstance(entry, dict) or len(entry) != 1:
        raise ScenarioError(
            f"{position}each step is a single key/value pair, e.g. '- say: hello.wav'"
        )

    (verb, value), = entry.items()

    if verb == "say":
        return _parse_say(value, position, base)
    if verb == "wait":
        return _parse_wait(value, position)
    if verb == "wait_for":
        return _parse_wait_for(value, position)
    if verb == "dtmf":
        return _parse_dtmf(value, position)
    if verb == "expect":
        return _parse_expect(value, position)
    if verb == "hangup":
        return Hangup()

    known = "say, wait, wait_for, dtmf, expect, hangup"
    raise ScenarioError(f"{position}unknown step {verb!r}. Known steps: {known}")


def _parse_say(value: Any, position: str, base: Path) -> Say:
    """Resolve and load a clip.

    A scenario file is *trusted input*: it names paths this process then reads
    and streams to the endpoint under test. Scenarios look like configuration
    rather than code, and are exactly the sort of thing pasted from an issue
    tracker, so a relative path is confined to somewhere the user plausibly
    meant: the scenario's own directory, or the directory the tool was run from.

    Both are needed. `scenarios/x.yaml` referring to `../fixtures/clip.wav` is
    an ordinary project layout, not an attack, and confining strictly to the
    scenario's directory rejects it. Reaching `../../../../etc` is neither.

    An absolute path is honoured as written -- that is an unambiguous
    instruction from whoever wrote the file, not an accident of `../..`.
    """
    if not isinstance(value, str):
        raise ScenarioError(f"{position}'say' takes a path to a WAV file")

    path = Path(value)
    if not path.is_absolute():
        candidate = (base / path).resolve()
        allowed = {base.resolve(), Path.cwd().resolve()}
        if not any(candidate.is_relative_to(root) for root in allowed):
            raise ScenarioError(
                f"{position}{value!r} escapes both the scenario's directory and "
                "the working directory. Keep clips within the project, or give "
                "an absolute path if you really mean somewhere else."
            )
        path = candidate

    try:
        frames = audio.wav_to_ulaw_frames(path)
    except audio.AudioError as exc:
        raise ScenarioError(f"{position}{exc}") from exc

    if not frames:
        raise ScenarioError(f"{position}{path} contains no audio")
    return Say(path=path, frames=frames)


def _parse_wait(value: Any, position: str) -> Wait:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ScenarioError(f"{position}'wait' takes a number of seconds")
    if value <= 0:
        raise ScenarioError(f"{position}'wait' must be positive, got {value}")
    return Wait(seconds=float(value))


def _parse_wait_for(value: Any, position: str) -> WaitFor:
    timeout = 10.0
    if isinstance(value, dict):
        event = value.get("event")
        timeout = value.get("timeout", timeout)
        if not isinstance(timeout, int | float) or timeout <= 0:
            raise ScenarioError(f"{position}'timeout' must be a positive number")
    else:
        event = value

    if event not in WAITABLE:
        raise ScenarioError(
            f"{position}cannot wait for {event!r}. "
            f"Known events: {', '.join(sorted(WAITABLE))}"
        )
    return WaitFor(event=event, timeout_s=float(timeout))


def _parse_dtmf(value: Any, position: str) -> Dtmf:
    digits = str(value)
    allowed = set("0123456789*#ABCD")
    bad = sorted(set(digits) - allowed)
    if not digits:
        raise ScenarioError(f"{position}'dtmf' needs at least one digit")
    if bad:
        raise ScenarioError(
            f"{position}{''.join(bad)!r} is not a phone key. "
            "Valid keys are 0-9, * and #, plus A-D."
        )
    return Dtmf(digits=digits)


def _parse_expect(value: Any, position: str) -> Expect:
    if not isinstance(value, str):
        raise ScenarioError(f"{position}'expect' takes a condition name")

    what, negated = value, False
    if value.startswith("no "):
        what, negated = value[3:].strip(), True

    if what not in EXPECTABLE:
        raise ScenarioError(
            f"{position}cannot assert {what!r}. "
            f"Known conditions: {', '.join(sorted(EXPECTABLE))}"
        )
    return Expect(what=what, negated=negated)
