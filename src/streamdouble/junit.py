"""JUnit XML for CI systems that render it natively.

Every CI system worth using draws a test report from JUnit XML, and until this
module every consumer of ``--json`` wrote their own converter. This is that
converter, written once, from the same :class:`~streamdouble.api.CallReport`
the JSON comes from -- so the XML cannot describe a different call.

**The XML is never greener than the exit code.** Each threshold, expectation,
protocol check and timeout is its own ``<testcase>``, and a final ``verdict``
case fails whenever the exit code is non-zero. If a future exit-code rule
has no testcase of its own, the report still goes red rather than showing a
passing build next to a failed job.

**None is not a pass.** A threshold on a metric that was never measured fails
by default. Under ``--allow-no-audio`` it passes the gate, but it is written
as *skipped*, not passed: nothing was checked, and a report that counts it as
a success turns "the agent never spoke" into a green tick.

**Agent-controlled text is hostile.** Violation messages quote what the agent
sent. ``xml.etree`` escapes markup, but it does not remove characters that are
illegal in XML 1.0, such as NUL and most other control characters. One of
those in a frame would produce a file every JUnit parser rejects, which hides
the failing run it was meant to show. They are replaced before writing.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING

from .api import EXIT_OK

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .api import CallReport
    from .baseline import Comparison

__all__ = ["build", "write"]

#: Characters XML 1.0 does not allow anywhere, even escaped.
_ILLEGAL_XML = re.compile("[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]")

#: Longest message kept. A hostile agent can make a violation message very
#: long, and CI report viewers do not cope well with megabyte-sized cases.
_MAX_TEXT = 2000


def _clean(text: object) -> str:
    return _ILLEGAL_XML.sub("�", str(text))[:_MAX_TEXT]


class _Suite:
    def __init__(self, name: str) -> None:
        self.element = ET.Element("testsuite", name=_clean(name))
        self.tests = self.failures = self.skipped = 0

    def case(self, name: str, *, failure: str | None = None, skipped: str | None = None) -> None:
        case = ET.SubElement(
            self.element, "testcase", classname="streamdouble", name=_clean(name)
        )
        self.tests += 1
        if failure is not None:
            self.failures += 1
            ET.SubElement(case, "failure", message=_clean(failure)).text = _clean(failure)
        elif skipped is not None:
            self.skipped += 1
            ET.SubElement(case, "skipped", message=_clean(skipped))

    def finish(self) -> ET.Element:
        self.element.set("tests", str(self.tests))
        self.element.set("failures", str(self.failures))
        self.element.set("errors", "0")
        self.element.set("skipped", str(self.skipped))
        return self.element


def _call_suite(report: CallReport, name: str) -> ET.Element:
    suite = _Suite(name)
    result = report.result

    if result.violations:
        codes = ", ".join(sorted({v.code for v in result.violations}))
        detail = "; ".join(f"{v.code}: {v}" for v in result.violations[:10])
        suite.case(
            "protocol",
            failure=f"{result.violation_count or len(result.violations)} violation(s) "
            f"[{codes}]: {detail}",
        )
    else:
        suite.case("protocol")

    if result.timed_out:
        suite.case("no timeout", failure="the agent did not respond before the timeout")
    else:
        suite.case("no timeout")

    for what, passed in result.expectations:
        suite.case(f"expect {what}", failure=None if passed else f"expected {what}")

    for outcome in report.thresholds:
        label = f"threshold: {outcome.threshold.name}"
        if outcome.value is None:
            if outcome.passed:
                suite.case(label, skipped="not measured; allowed by --allow-no-audio")
            else:
                suite.case(label, failure=outcome.describe())
        else:
            suite.case(label, failure=None if outcome.passed else outcome.describe())

    code = report.exit_code
    suite.case(
        "verdict",
        failure=None if code == EXIT_OK else f"streamdouble would exit {code}",
    )
    return suite.finish()


def _comparison_suite(comparison: Comparison, exit_code: int) -> ET.Element:
    suite = _Suite("baseline comparison")
    for delta in comparison.deltas:
        label = f"baseline: {delta.label}"
        if delta.lost_measurement:
            suite.case(label, failure=f"was {delta.before:.1f}, now never measured")
        elif not delta.comparable:
            suite.case(label, skipped="not comparable: not measured in one of the series")
        elif delta.regressed:
            suite.case(label, failure=f"regressed {delta.before:.1f} -> {delta.after:.1f}")
        else:
            suite.case(label)
    suite.case(
        "verdict",
        failure=None if exit_code == EXIT_OK else f"streamdouble would exit {exit_code}",
    )
    return suite.finish()


def build(
    reports: list[CallReport],
    *,
    name: str = "streamdouble",
    comparison: Comparison | None = None,
    exit_code: int | None = None,
) -> ET.ElementTree:
    """One ``<testsuite>`` per call, plus one for a baseline comparison.

    ``exit_code`` is the series-level code; it gates the comparison suite's
    verdict, since a comparison can fail when every call passed.
    """
    root = ET.Element("testsuites", name=_clean(name))
    for index, report in enumerate(reports):
        suffix = f" run {index + 1}" if len(reports) > 1 else ""
        root.append(_call_suite(report, f"{name}{suffix}"))
    if comparison is not None:
        root.append(
            _comparison_suite(comparison, EXIT_OK if exit_code is None else exit_code)
        )
    totals = {"tests": 0, "failures": 0, "skipped": 0}
    for suite in root:
        for key in totals:
            totals[key] += int(suite.get(key, "0"))
    for key, value in totals.items():
        root.set(key, str(value))
    root.set("errors", "0")
    return ET.ElementTree(root)


def write(path: str | Path, tree: ET.ElementTree) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(tree)
    tree.write(target, encoding="utf-8", xml_declaration=True)
