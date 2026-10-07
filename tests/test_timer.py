"""The Windows timer request: matched, scoped, and reported honestly.

The Windows path is tested on every platform through an injected fake winmm,
because the paths that matter most -- a refusal, an exception mid-call -- are
ones a healthy Windows machine never takes. One test uses the real winmm, and
only on Windows.
"""

from __future__ import annotations

import sys

import pytest

from streamdouble import api, timer
from streamdouble.session import SessionConfig

FAST = SessionConfig(response_timeout_s=3.0, quiet_period_s=0.3, max_drain_s=3.0)


class FakeWinmm:
    def __init__(self, begin_result: int = 0) -> None:
        self.begin_result = begin_result
        self.begun: list[int] = []
        self.ended: list[int] = []

    def timeBeginPeriod(self, period: int) -> int:
        self.begun.append(period)
        return self.begin_result

    def timeEndPeriod(self, period: int) -> int:
        self.ended.append(period)
        return 0


def test_a_granted_request_is_matched_with_the_same_period():
    fake = FakeWinmm()
    with timer.high_resolution_timer(platform="win32", winmm=fake) as status:
        assert status == timer.RAISED
        assert fake.ended == []  # held for the whole block, not released early
    assert fake.begun == [1]
    assert fake.ended == [1]


def test_the_period_is_released_even_when_the_call_raises():
    fake = FakeWinmm()
    with pytest.raises(RuntimeError), timer.high_resolution_timer(platform="win32", winmm=fake):
        raise RuntimeError("the agent hung up")
    assert fake.ended == [1]


def test_a_refused_request_is_reported_and_not_undone():
    """An unmatched timeEndPeriod would be a bug of its own."""
    fake = FakeWinmm(begin_result=97)  # TIMERR_NOCANDO
    with timer.high_resolution_timer(platform="win32", winmm=fake) as status:
        assert status == timer.REFUSED
    assert fake.ended == []


def test_a_missing_winmm_is_a_refusal_not_a_crash():
    class Broken:
        def timeBeginPeriod(self, period):
            raise OSError("no winmm here")

    with timer.high_resolution_timer(platform="win32", winmm=Broken()) as status:
        assert status == timer.REFUSED


def test_other_platforms_never_touch_winmm():
    fake = FakeWinmm()
    with timer.high_resolution_timer(platform="linux", winmm=fake) as status:
        assert status == timer.NOT_NEEDED
    assert fake.begun == [] and fake.ended == []


#: What a real Windows machine may legitimately answer. REFUSED is correct on a
#: Windows without winmm.dll (e.g. Nano Server), and these tests used to
#: fail there while the product behaved exactly as designed.
WINDOWS_OUTCOMES = {timer.RAISED, timer.REFUSED}


@pytest.mark.skipif(sys.platform != "win32", reason="real winmm exists only on Windows")
def test_the_real_request_is_answered_on_windows():
    with timer.high_resolution_timer() as status:
        assert status in WINDOWS_OUTCOMES


async def test_a_call_reports_which_timer_its_pacing_ran_on(server, speech_8k_path):
    report = await api.call(server, audio_path=speech_8k_path, config=FAST)

    published = report.to_dict()["pacing"]["timer"]
    # Never "not requested": that would mean the session skipped the request,
    # which is the wiring this test exists to catch.
    expected = WINDOWS_OUTCOMES if sys.platform == "win32" else {timer.NOT_NEEDED}
    assert published in expected
    assert report.result.pacing.timer == published
