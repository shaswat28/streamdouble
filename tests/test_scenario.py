"""Tests for scenario parsing and execution.

Parsing is checked hard because a scenario is validated up front, before a
socket is opened. That is the point: a typo should not fail halfway through a
call with the agent left mid-sentence and the operator unsure what ran.

Error messages get their own assertions. A scenario file is written by a person
who is not reading this source, so "unknown step 'wiat'" earns its keep in a way
that a bare exception type does not.
"""

from __future__ import annotations

import pytest

from streamdouble import audio, scenario
from streamdouble.scenario import (
    Dtmf,
    Expect,
    Hangup,
    Say,
    ScenarioError,
    Wait,
    WaitFor,
)

CLIP = "fixtures/speech_8k.wav"


def parse(steps, **kwargs):
    return scenario.parse({"name": "test", "steps": steps}, **kwargs)


def one(step_dict):
    """Parse a single step and return it."""
    return parse([step_dict]).steps[0]


# --------------------------------------------------------------------------
# Document shape
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["just a string", ["a", "list"], 42, None])
def test_a_scenario_must_be_a_mapping(raw):
    with pytest.raises(ScenarioError, match="mapping"):
        scenario.parse(raw)


@pytest.mark.parametrize("steps", [[], None, "say hello", {}])
def test_steps_must_be_a_non_empty_list(steps):
    with pytest.raises(ScenarioError, match="non-empty list"):
        scenario.parse({"steps": steps})


def test_the_name_defaults_to_the_filename(tmp_path):
    path = tmp_path / "interrupts.yaml"
    path.write_text("steps:\n  - wait: 1\n")
    assert scenario.load(path).name == "interrupts"


def test_an_explicit_name_wins():
    assert parse([{"wait": 1}]).name == "test"


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


def test_say_loads_the_audio_at_parse_time():
    """The clip is decoded during validation, not during the call.

    A missing or corrupt WAV should stop the run before it starts rather than
    halfway through, when the agent is already talking.
    """
    step = one({"say": CLIP})
    assert isinstance(step, Say)
    assert len(step.frames) == 100
    assert step.duration_s == pytest.approx(2.0)
    assert all(len(frame) == audio.FRAME_BYTES for frame in step.frames)


def test_say_reports_a_missing_clip_with_its_path():
    with pytest.raises(ScenarioError, match="no such file"):
        one({"say": "definitely-not-here.wav"})


def test_relative_clips_resolve_against_the_scenario_file(tmp_path):
    """A scenario and its audio can be committed together and moved together.

    Resolving against the working directory instead would make a scenario work
    only when run from one place, which is exactly the kind of thing that works
    for its author and fails in CI.
    """
    import shutil

    clips = tmp_path / "clips"
    clips.mkdir()
    shutil.copy(CLIP, clips / "hello.wav")
    path = tmp_path / "s.yaml"
    path.write_text("steps:\n  - say: clips/hello.wav\n")

    step = scenario.load(path).steps[0]
    assert len(step.frames) == 100


@pytest.mark.parametrize("value", [0, -1, "soon", True, None])
def test_wait_needs_a_positive_number(value):
    with pytest.raises(ScenarioError):
        one({"wait": value})


def test_wait_accepts_fractional_seconds():
    assert one({"wait": 1.5}).seconds == 1.5


def test_wait_for_accepts_the_known_events():
    for event in sorted(scenario.WAITABLE):
        assert one({"wait_for": event}).event == event


def test_wait_for_rejects_an_unknown_event_and_lists_the_real_ones():
    with pytest.raises(ScenarioError) as excinfo:
        one({"wait_for": "the postman"})
    message = str(excinfo.value)
    assert "the postman" in message
    assert "mark" in message  # tells the author what is available


def test_wait_for_takes_an_optional_timeout():
    step = one({"wait_for": {"event": "mark", "timeout": 2.5}})
    assert isinstance(step, WaitFor)
    assert step.timeout_s == 2.5


def test_wait_for_rejects_a_nonsense_timeout():
    with pytest.raises(ScenarioError, match="timeout"):
        one({"wait_for": {"event": "mark", "timeout": -1}})


def test_dtmf_accepts_every_real_phone_key():
    assert one({"dtmf": "0123456789*#"}).digits == "0123456789*#"


def test_dtmf_accepts_a_number_written_without_quotes():
    """YAML turns an unquoted 1234 into an int; that is a person pressing keys."""
    assert one({"dtmf": 1234}).digits == "1234"


def test_dtmf_rejects_things_that_are_not_on_a_phone():
    with pytest.raises(ScenarioError, match="not a phone key"):
        one({"dtmf": "12x"})


def test_expect_parses_conditions_and_their_negations():
    assert one({"expect": "clear"}) == Expect(what="clear", negated=False)
    assert one({"expect": "no clear"}) == Expect(what="clear", negated=True)


def test_expect_rejects_an_unknown_condition():
    with pytest.raises(ScenarioError, match="cannot assert"):
        one({"expect": "the best"})


def test_hangup_works_as_a_bare_string_and_as_a_mapping():
    assert isinstance(one("hangup"), Hangup)
    assert isinstance(one({"hangup": None}), Hangup)


def test_a_bare_string_that_is_not_hangup_explains_itself():
    with pytest.raises(ScenarioError) as excinfo:
        one("wait")
    assert "takes a value" in str(excinfo.value)


def test_an_unknown_step_lists_the_known_ones():
    with pytest.raises(ScenarioError) as excinfo:
        one({"wiat": 1})
    message = str(excinfo.value)
    assert "wiat" in message
    assert "wait" in message


def test_a_step_with_two_keys_is_rejected():
    """`- say: a.wav` and `wait: 1` on one entry is a YAML indentation mistake."""
    with pytest.raises(ScenarioError, match=r"single key"):
        one({"say": CLIP, "wait": 1})


def test_errors_name_the_step_number():
    with pytest.raises(ScenarioError, match=r"step 3"):
        parse([{"wait": 1}, {"wait": 2}, {"wait": -1}])


def test_errors_name_the_file(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("steps:\n  - wait: -1\n")
    with pytest.raises(ScenarioError, match=r"broken\.yaml"):
        scenario.load(path)


# --------------------------------------------------------------------------
# Whole scenarios
# --------------------------------------------------------------------------


def test_a_realistic_scenario_parses_end_to_end():
    parsed = parse(
        [
            {"wait_for": "audio"},
            {"wait": 1.5},
            {"say": CLIP},
            {"expect": "clear"},
            {"dtmf": "1"},
            {"wait_for": {"event": "mark", "timeout": 5}},
            "hangup",
        ]
    )

    assert [type(step) for step in parsed.steps] == [
        WaitFor, Wait, Say, Expect, Dtmf, WaitFor, Hangup
    ]
    assert parsed.audio_duration_s == pytest.approx(2.0)


def test_describe_is_readable():
    described = parse([{"wait": 1.5}, {"say": CLIP}, "hangup"]).describe()
    assert "1. wait 1.5s" in described
    assert "streaming silence" in described  # the distinction that matters
    assert "2. say speech_8k.wav" in described


def test_loading_a_missing_file_is_a_scenario_error(tmp_path):
    with pytest.raises(ScenarioError, match="no such file"):
        scenario.load(tmp_path / "absent.yaml")


def test_invalid_yaml_is_a_scenario_error(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("steps:\n  - wait: [unclosed\n")
    with pytest.raises(ScenarioError, match="not valid YAML"):
        scenario.load(path)


def test_every_shipped_scenario_is_valid():
    """The examples in the repository parse.

    A broken example is worse than no example: it is the first thing anyone
    copies. Scenarios needing generated speech are skipped rather than failed,
    since `fixtures/speech/` is gitignored -- it holds synthesised audio that is
    regenerable in seconds and not ours to redistribute.
    """
    import pathlib

    shipped = sorted(pathlib.Path("scenarios").glob("*.yaml"))
    assert shipped, "no example scenarios found"

    checked = 0
    for path in shipped:
        try:
            loaded = scenario.load(path)
        except ScenarioError as exc:
            if "speech" in str(exc) and "no such file" in str(exc):
                # Needs `python fixtures/make_speech.py` first.
                continue
            raise
        assert loaded.steps
        checked += 1

    assert checked, "every scenario was skipped; none could be validated"


def test_the_barge_in_scenario_asserts_on_clear():
    """The example that demonstrates barge-in actually asserts barge-in.

    Checked by reading the file rather than loading it, so this holds whether or
    not the generated speech clips are present.
    """
    import pathlib

    text = pathlib.Path("scenarios/barge_in.yaml").read_text(encoding="utf-8")
    assert "expect: clear" in text
    assert "make_speech.py" in text, "it should say the clips must be generated first"
