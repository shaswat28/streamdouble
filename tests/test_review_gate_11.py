"""Regression tests for review gate 11 (the Windows timer).

1. Two tests demanded "raised" on any Windows, so on a Windows without
   winmm.dll -- where "refused" is the designed answer -- the suite failed
   while the product was right. The first test below drives exactly that
   machine through a real call; the old assertion in test_timer.py would have
   rejected its outcome.
2. The "not requested" default was a string literal in pacer.py and
   metrics.py as well as a constant in timer.py. The second test fails against
   614d618, where the literal appears three times.
"""

from __future__ import annotations

import re
from pathlib import Path

from streamdouble import api, timer
from streamdouble.metrics import Metrics
from streamdouble.pacer import PacingStats
from streamdouble.session import SessionConfig

FAST = SessionConfig(response_timeout_s=3.0, quiet_period_s=0.3, max_drain_s=3.0)
SRC = Path(__file__).resolve().parent.parent / "src" / "streamdouble"


# 1 -------------------------------------------------------------------------


async def test_a_windows_without_winmm_still_calls_and_reports_refused(
    server, speech_8k_path, monkeypatch
):
    def missing():
        raise OSError("winmm.dll not found")

    monkeypatch.setattr(timer.sys, "platform", "win32")
    monkeypatch.setattr(timer, "_load_winmm", missing)

    report = await api.call(server, audio_path=speech_8k_path, config=FAST)

    assert report.passed
    assert report.to_dict()["pacing"]["timer"] == timer.REFUSED


# 2 -------------------------------------------------------------------------


def test_the_not_requested_spelling_is_defined_once():
    # Assignments only. A comment listing the possible values documents the
    # constant rather than redefining it, and must not count.
    assigned = re.compile(rf'=\s*"{re.escape(timer.NOT_REQUESTED)}"')
    holders = sorted(
        p.name for p in SRC.glob("*.py") if assigned.search(p.read_text(encoding="utf-8"))
    )
    assert holders == ["timer.py"]


def test_the_defaults_are_the_constant():
    assert PacingStats().timer == timer.NOT_REQUESTED
    assert Metrics().pacing_timer == timer.NOT_REQUESTED
