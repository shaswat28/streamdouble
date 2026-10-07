"""`--junit`: the XML report, and the promises it makes.

The central promise is that the XML is never greener than the exit code. Tests
here check it three ways: over constructed reports, by mutation (an exit code
with no testcase of its own still turns the report red), and end to end against
the echo agent, where the XML and the process exit code come from one run.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from streamdouble import api, cli, junit
from streamdouble.api import CallReport
from streamdouble.metrics import Threshold, compute, evaluate_thresholds
from streamdouble.protocol import ProtocolViolation, StreamIdentity
from streamdouble.session import SessionResult


def make_result(**overrides) -> SessionResult:
    defaults = {
        "identity": StreamIdentity(),
        "events": [],
        "frames_sent": 100,
        "media_frames_received": 80,
        "audio_received": b"\xff" * 12800,
    }
    return SessionResult(**{**defaults, **overrides})


def make_report(result: SessionResult, thresholds: list[Threshold] | None = None) -> CallReport:
    metrics = compute(result)
    return CallReport(
        result=result,
        metrics=metrics,
        thresholds=evaluate_thresholds(metrics, thresholds or []),
    )


def parse(tree: ET.ElementTree) -> ET.Element:
    """Round-trip through bytes, as a CI parser would see it."""
    return ET.fromstring(ET.tostring(tree.getroot(), encoding="utf-8"))


def cases(root: ET.Element) -> dict[str, str]:
    """testcase name -> 'pass' | 'failure' | 'skipped'."""
    found = {}
    for case in root.iter("testcase"):
        if case.find("failure") is not None:
            found[case.get("name")] = "failure"
        elif case.find("skipped") is not None:
            found[case.get("name")] = "skipped"
        else:
            found[case.get("name")] = "pass"
    return found


LATENCY = Threshold(name="first audio", limit=800, attribute="time_to_first_audio_ms")


# ---------------------------------------------------------------------------
# Never greener than the exit code


@pytest.mark.parametrize(
    "result",
    [
        make_result(),
        make_result(timed_out=True),
        make_result(violations=[ProtocolViolation("bad_json", "nope")], violation_count=1),
        make_result(expectations=[("clear", False)]),
        make_result(audio_received=b"", media_frames_received=0),
    ],
    ids=["clean", "timeout", "violation", "expectation", "silent"],
)
def test_failures_exist_exactly_when_the_exit_code_is_non_zero(result):
    report = make_report(result, [LATENCY])
    root = parse(junit.build([report]))

    assert (int(root.get("failures")) > 0) == (report.exit_code != cli.EXIT_OK)


def test_an_exit_code_with_no_testcase_of_its_own_still_fails_the_report(monkeypatch):
    """Mutation: a future exit-code rule nobody gave a testcase to."""
    report = make_report(make_result())
    monkeypatch.setattr(CallReport, "exit_code", property(lambda self: cli.EXIT_ASSERTION_FAILED))

    found = cases(parse(junit.build([report])))

    assert found["verdict"] == "failure"
    assert found["protocol"] == "pass"


# ---------------------------------------------------------------------------
# None is not a pass


def test_an_unmeasured_threshold_fails():
    silent = make_result(audio_received=b"", media_frames_received=0)
    found = cases(parse(junit.build([make_report(silent, [LATENCY])])))
    assert found["threshold: first audio"] == "failure"


def test_an_allowed_unmeasured_threshold_is_skipped_not_passed():
    allowed = Threshold(
        name="first audio", limit=800, attribute="time_to_first_audio_ms", missing_fails=False
    )
    silent = make_result(audio_received=b"", media_frames_received=0)
    found = cases(parse(junit.build([make_report(silent, [allowed])])))
    assert found["threshold: first audio"] == "skipped"


# ---------------------------------------------------------------------------
# Hostile text


def test_illegal_xml_characters_from_the_agent_do_not_break_the_file(tmp_path: Path):
    hostile = 'nul\x00 bell\x07 </failure><x a="1"> ]]> ￾ end'
    result = make_result(violations=[ProtocolViolation("bad", hostile)], violation_count=1)
    path = tmp_path / "out" / "report.xml"

    junit.write(path, junit.build([make_report(result)]))

    root = ET.parse(path).getroot()  # raises if the file is not well-formed
    message = next(root.iter("failure")).get("message")
    assert "</failure>" in message  # escaped and kept, not interpreted
    assert "\x00" not in message and "\x07" not in message


def test_a_huge_message_is_bounded():
    result = make_result(violations=[ProtocolViolation("bad", "x" * 1_000_000)], violation_count=1)
    root = parse(junit.build([make_report(result)]))
    assert len(next(root.iter("failure")).get("message")) <= 2000


# ---------------------------------------------------------------------------
# Series and totals


def test_a_series_gets_one_suite_per_run_and_totals_add_up():
    reports = [make_report(make_result()), make_report(make_result(timed_out=True))]
    root = parse(junit.build(reports, name="agent"))

    suites = root.findall("testsuite")
    assert [s.get("name") for s in suites] == ["agent run 1", "agent run 2"]
    assert int(root.get("tests")) == sum(int(s.get("tests")) for s in suites)
    assert int(root.get("failures")) == sum(int(s.get("failures")) for s in suites)


# ---------------------------------------------------------------------------
# End to end


@pytest.mark.timeout(60)
def test_the_cli_writes_junit_that_agrees_with_its_exit_code(server, tmp_path: Path):
    path = tmp_path / "junit.xml"
    code = cli.main([
        "call", f"{server}?garbage_after=12", "--audio", "fixtures/speech_8k.wav",
        "--quiet-period", "0.3", "--quiet", "--junit", str(path),
    ])

    assert code == cli.EXIT_PROTOCOL_VIOLATION
    found = cases(ET.parse(path).getroot())
    assert found["protocol"] == "failure"
    assert found["verdict"] == "failure"


@pytest.mark.timeout(60)
def test_a_clean_call_writes_a_passing_report(server, tmp_path: Path):
    path = tmp_path / "junit.xml"
    code = cli.main([
        "call", server, "--audio", "fixtures/speech_8k.wav", "--quiet-period", "0.3",
        "--quiet", "--max-first-audio-ms", "5000", "--junit", str(path),
    ])

    assert code == cli.EXIT_OK
    root = ET.parse(path).getroot()
    assert root.get("failures") == "0"
    assert cases(root)["threshold: first audio"] == "pass"


@pytest.mark.timeout(60)
def test_an_unwritable_junit_path_does_not_destroy_the_call(server, tmp_path: Path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    code = cli.main([
        "call", server, "--audio", "fixtures/speech_8k.wav", "--quiet-period", "0.3",
        "--quiet", "--junit", str(blocker / "junit.xml"),
    ])

    assert code == cli.EXIT_OK
    assert "could not write" in capsys.readouterr().err


def test_the_action_passes_junit_through_the_environment():
    """No `${{ inputs.* }}` inside a run: block; pass inputs through the environment."""
    text = (Path(__file__).resolve().parent.parent / "action.yml").read_text(encoding="utf-8")
    assert "JUNIT: ${{ inputs.junit }}" in text
    assert '--junit "$JUNIT"' in text
    for block in text.split("run: |")[1:]:
        script = block.split("\n    - ")[0]
        assert "${{" not in script


async def test_api_reports_feed_junit_directly(server, speech_8k_path):
    report = await api.call(server, audio_path=speech_8k_path)
    root = parse(junit.build([report]))
    assert root.find("testsuite") is not None
