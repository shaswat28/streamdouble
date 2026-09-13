"""Regression tests for review gate 10 (`--junit`).

Each test fails against the code as it stood at b5ea49b.

1. A --save-baseline refusal exited 4 after the report had already been
   written from calls that all passed, so the XML showed green next to a
   red job.
2. A connection failure (or usage error) wrote no report at all, leaving the
   previous run's green file in place to be published.
3. Expectation testcases were named "expect expect clear".
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from conftest import free_port
from streamdouble import cli, junit
from streamdouble.api import CallReport
from streamdouble.metrics import compute
from streamdouble.protocol import StreamIdentity
from streamdouble.session import SessionResult

GREEN = (
    '<?xml version="1.0"?><testsuites tests="1" failures="0">'
    '<testsuite name="old" tests="1" failures="0"><testcase name="verdict"/></testsuite>'
    "</testsuites>"
)


def failures(path: Path) -> int:
    return int(ET.parse(path).getroot().get("failures"))


# 1 -------------------------------------------------------------------------


@pytest.mark.timeout(120)
def test_a_refused_baseline_does_not_leave_a_green_report(server, tmp_path: Path):
    # A silent fork is correct behaviour, so every run passes, but first audio
    # is never measured -- which is exactly what the refusal exists to catch.
    report = tmp_path / "junit.xml"
    code = cli.main([
        "call", server.replace("/media-stream", "/media-stream-fork"), "--fork",
        "--audio", "fixtures/speech_8k.wav",
        "--repeat", "3", "--save-baseline", str(tmp_path / "b.json"),
        "--quiet-period", "0.3", "--max-drain", "3", "--response-timeout", "3",
        "--quiet", "--junit", str(report),
    ])

    assert code == cli.EXIT_USAGE
    assert failures(report) > 0


# 2 -------------------------------------------------------------------------


@pytest.mark.timeout(60)
def test_a_connection_failure_replaces_a_stale_green_report(tmp_path: Path):
    report = tmp_path / "junit.xml"
    report.write_text(GREEN, encoding="utf-8")

    code = cli.main([
        "call", f"ws://127.0.0.1:{free_port()}/media-stream",
        "--audio", "fixtures/speech_8k.wav", "--quiet", "--junit", str(report),
    ])

    assert code == cli.EXIT_CONNECTION_FAILED
    assert failures(report) == 1
    assert "could not connect" in ET.parse(report).getroot().find(".//failure").get("message")


def test_a_usage_error_replaces_a_stale_green_report(tmp_path: Path):
    report = tmp_path / "junit.xml"
    report.write_text(GREEN, encoding="utf-8")

    code = cli.main([
        "call", "ws://localhost:1/x", "--audio", str(tmp_path / "missing.wav"),
        "--junit", str(report),
    ])

    assert code == cli.EXIT_USAGE
    assert failures(report) == 1


# 3 -------------------------------------------------------------------------


def test_expectation_testcases_are_named_once():
    result = SessionResult(
        identity=StreamIdentity(), events=[], frames_sent=1, media_frames_received=1,
        audio_received=b"\xff" * 160,
        expectations=[("expect clear", True), ("expect no silence", False)],
    )
    report = CallReport(result=result, metrics=compute(result))
    root = junit.build([report]).getroot()

    names = [case.get("name") for case in root.iter("testcase")]
    assert "expect clear" in names
    assert "expect no silence" in names
    assert not any(name.startswith("expect expect") for name in names)
    assert next(root.iter("failure")).get("message") == "failed: expect no silence"
