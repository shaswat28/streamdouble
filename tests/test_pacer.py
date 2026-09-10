"""Tests for real-time frame pacing.

Driven by a fake clock rather than by actually waiting. Testing a scheduler with
real sleeps is slow, flaky, and cannot provoke the interesting cases -- a frame
that arrives 200 ms late, or a run long enough for drift to become visible. A
controllable clock makes all of that deterministic and instant.

The central claim under test is that error does not accumulate. That is the
difference between a pacer and a sleep loop, and it is what every latency number
this tool reports ultimately rests on.
"""

from __future__ import annotations

import pytest

from streamdouble.pacer import Pacer

FRAME_INTERVAL = 0.02


class FakeClock:
    """A monotonic clock advanced only by sleeping.

    Models a perfect scheduler: every sleep takes exactly as long as requested.
    Deviations are introduced explicitly by tests via ``advance``, so each test
    controls precisely the imperfection it is about.
    """

    def __init__(self, start: float = 1000.0, overshoot: float = 0.0) -> None:
        self.now = start
        self.sleeps: list[float] = []
        # How much longer than requested each sleep actually takes. asyncio.sleep
        # guarantees a minimum, never an exact duration, so a real sleep always
        # overshoots by some amount.
        self.overshoot = overshoot

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds + self.overshoot

    def advance(self, seconds: float) -> None:
        """Move time forward without sleeping -- simulates work taking time."""
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def pacer(clock: FakeClock) -> Pacer:
    return Pacer(FRAME_INTERVAL, clock=clock, sleep=clock.sleep)


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------


@pytest.mark.parametrize("interval", [0, -0.02])
def test_interval_must_be_positive(interval):
    with pytest.raises(ValueError, match="must be positive"):
        Pacer(interval)


def test_deadline_requires_a_start(pacer):
    """Using the schedule before fixing its origin is an error, not an implicit start.

    The origin doubles as the reference point for latency measurement, so
    starting it lazily on first use would silently anchor every measurement to
    the wrong instant.
    """
    assert not pacer.started
    with pytest.raises(RuntimeError, match="has not started"):
        pacer.deadline_for(0)


# --------------------------------------------------------------------------
# Deadlines
# --------------------------------------------------------------------------


def test_deadlines_are_absolute_multiples_of_the_interval(pacer, clock):
    origin = pacer.start()
    assert origin == clock.now
    for index in (0, 1, 2, 50, 1500):
        assert pacer.deadline_for(index) == pytest.approx(origin + index * FRAME_INTERVAL)


def test_deadlines_do_not_depend_on_when_they_are_asked_for(pacer, clock):
    """A deadline is a function of the index alone.

    This is the property that prevents drift. If deadlines were derived from
    "now" at any point, a delay would push every later frame back by the same
    amount, permanently.
    """
    pacer.start()
    before = pacer.deadline_for(100)
    clock.advance(5.0)
    assert pacer.deadline_for(100) == before


# --------------------------------------------------------------------------
# The core claim: no accumulated drift
# --------------------------------------------------------------------------


async def test_a_perfect_run_has_no_drift(pacer, clock):
    """1500 frames of a 30-second call land exactly on schedule."""
    pacer.start()
    for _ in range(1500):
        assert await pacer.wait() == 0.0

    assert pacer.stats.frames == 1500
    # Frame 0 is due at the origin, so 1500 frames span 1499 intervals of
    # sending time even though they carry 1500 intervals of audio.
    assert pacer.stats.elapsed_s == pytest.approx(1499 * FRAME_INTERVAL)
    # abs= is required: pytest.approx(0.0) falls back to a 1e-12 absolute
    # tolerance, which 1500 accumulated float additions will exceed.
    assert pacer.stats.drift_ms == pytest.approx(0.0, abs=1e-6)
    assert pacer.stats.late_frames == 0


async def test_per_frame_overhead_does_not_accumulate(pacer, clock):
    """Work between frames shortens the next sleep instead of delaying the run.

    The regression test for the naive sleep loop. Each frame here costs 1 ms of
    processing -- a realistic figure for encoding and a socket write. A loop
    that slept a fixed 20 ms would finish 1.5 seconds late over a 30-second
    call; deadline scheduling absorbs it entirely.
    """
    pacer.start()
    for _ in range(1500):
        clock.advance(0.001)  # the work of sending a frame
        await pacer.wait()

    assert pacer.stats.elapsed_s == pytest.approx(1499 * FRAME_INTERVAL, abs=0.002)
    assert abs(pacer.stats.drift_ms) < 2.0
    # Sleeps shrank to compensate rather than staying at a fixed interval.
    assert all(s == pytest.approx(FRAME_INTERVAL - 0.001) for s in clock.sleeps[1:])
    # Frame 0 is the exception: the 1 ms of work happened before its deadline
    # had any slack to give, so it alone is released late. Asserted by measured
    # lateness rather than by the late-frame count, because 1 ms sits exactly on
    # LATE_THRESHOLD_S and float arithmetic puts it a fraction under -- a knife
    # edge that would make this test's verdict arbitrary.
    assert pacer.stats.max_lateness_ms == pytest.approx(1.0, abs=0.01)
    assert pacer.stats.total_lateness_s == pytest.approx(0.001, abs=1e-6)


async def test_a_single_stall_does_not_shift_later_frames(pacer, clock):
    """After a 200 ms stall, the schedule re-converges instead of staying behind.

    A garbage collection pause or a slow agent response should cost the frames
    it actually delayed, not permanently offset the rest of the call.
    """
    pacer.start()
    for _ in range(10):
        await pacer.wait()

    clock.advance(0.200)  # a long stall
    late = await pacer.wait()
    assert late == pytest.approx(0.200 - FRAME_INTERVAL, abs=1e-9)

    # The next ten frames are consumed catching up, then the schedule is met
    # again -- the run ends on the original timeline, not 200 ms past it.
    for _ in range(20):
        await pacer.wait()

    assert pacer.stats.elapsed_s == pytest.approx(30 * FRAME_INTERVAL, abs=1e-9)
    assert pacer.stats.drift_ms == pytest.approx(0.0, abs=0.001)


async def test_catch_up_frames_do_not_sleep(pacer, clock):
    """Overdue frames are sent immediately rather than sleeping.

    Sleeping on an already-missed deadline would make the pacer fall further
    behind with every frame -- the opposite of catching up.
    """
    pacer.start()
    clock.advance(0.100)  # five frames' worth of stall before the first frame

    sleeps_before = len(clock.sleeps)
    for _ in range(5):
        assert await pacer.wait() > 0
    assert len(clock.sleeps) == sleeps_before, "an overdue frame should not sleep"


# --------------------------------------------------------------------------
# Lateness reporting
# --------------------------------------------------------------------------


async def test_lateness_is_never_negative(pacer, clock):
    """``wait`` reports lateness, not slack.

    An early frame returns 0.0 rather than a negative number, so that callers
    summing the return value cannot have real lateness cancelled out by frames
    that happened to be early.
    """
    pacer.start()
    for _ in range(5):
        assert await pacer.wait() >= 0.0
    assert pacer.stats.total_lateness_s == 0.0


async def test_late_frames_are_counted_and_measured(pacer, clock):
    pacer.start()
    await pacer.wait()

    clock.advance(0.050)
    await pacer.wait()

    assert pacer.stats.late_frames == 1
    assert pacer.stats.max_lateness_ms == pytest.approx(30.0, abs=0.001)
    assert pacer.stats.mean_lateness_ms == pytest.approx(15.0, abs=0.001)


async def test_sub_millisecond_jitter_is_not_reported_as_lateness():
    """Normal event-loop jitter does not inflate the late-frame count.

    Without a threshold, a healthy run reports hundreds of "late" frames from
    microsecond-scale scheduling noise, and the number stops meaning anything.

    The jitter is applied through the sleep, which is where it actually comes
    from -- asyncio.sleep guarantees a minimum, not an exact duration. Applying
    it before the wait instead would model something quite different: a caller
    that is progressively falling behind, which accumulates and *should* be
    reported.
    """
    clock = FakeClock(overshoot=0.0001)
    pacer = Pacer(FRAME_INTERVAL, clock=clock, sleep=clock.sleep)

    pacer.start()
    for _ in range(100):
        await pacer.wait()

    assert pacer.stats.late_frames == 0
    # Measured, just not flagged -- the overshoot is real and reported.
    assert pacer.stats.total_lateness_s > 0
    assert pacer.stats.max_lateness_ms == pytest.approx(0.1, abs=0.01)


async def test_sleep_overshoot_does_not_accumulate():
    """A sleep that always overshoots still does not compound.

    Each frame lands 0.1 ms late, and stays 0.1 ms late -- the deadline for
    frame N never moves, so the overshoot on frame N-1 is absorbed by a shorter
    sleep rather than being added to it. A sleep loop would turn the same
    overshoot into 100 ms of drift over these 1000 frames.
    """
    clock = FakeClock(overshoot=0.0001)
    pacer = Pacer(FRAME_INTERVAL, clock=clock, sleep=clock.sleep)

    pacer.start()
    for _ in range(1000):
        await pacer.wait()

    assert pacer.stats.drift_ms == pytest.approx(0.1, abs=0.01)
    assert pacer.stats.max_lateness_ms == pytest.approx(0.1, abs=0.01)


async def test_stats_summary_is_readable(pacer, clock):
    pacer.start()
    for _ in range(50):
        await pacer.wait()

    summary = pacer.stats.summary()
    assert "50 frames" in summary
    assert "drift" in summary


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


async def test_restarting_clears_stats_and_rebases_the_origin(pacer, clock):
    pacer.start()
    clock.advance(0.5)
    await pacer.wait()
    assert pacer.stats.frames == 1

    clock.advance(10.0)
    new_origin = pacer.start()

    assert new_origin == clock.now
    assert pacer.stats.frames == 0
    assert pacer.stats.late_frames == 0
    assert await pacer.wait() == 0.0


async def test_reset_returns_the_pacer_to_its_initial_state(pacer):
    pacer.start()
    await pacer.wait()
    pacer.reset()

    assert not pacer.started
    assert pacer.stats.frames == 0
    with pytest.raises(RuntimeError):
        pacer.deadline_for(0)


async def test_real_time_is_actually_spent(clock):
    """One test against the real clock, so the fake is not the only evidence.

    Everything else here runs on a controllable clock, which proves the
    arithmetic but not that the pacer waits at all. This paces ten frames for
    real and checks the elapsed time is in the right neighbourhood -- loose
    bounds, because CI runners are not real-time systems and a tight assertion
    here would be a flaky test rather than a strict one.
    """
    import time

    pacer = Pacer(FRAME_INTERVAL)
    started = time.monotonic()
    pacer.start()
    for _ in range(10):
        await pacer.wait()
    elapsed = time.monotonic() - started

    assert 0.15 < elapsed < 0.60, f"ten 20ms frames took {elapsed:.3f}s"
