"""Real-time frame scheduling with drift correction.

A Twilio media stream delivers one 20 ms frame every 20 ms of wall clock. The
naive implementation of that is a loop that sleeps 20 ms per frame, and it is
wrong in a way that gets worse the longer the call runs::

    while frames:                 # do not do this
        send(frames.pop(0))
        await asyncio.sleep(0.02)

Each iteration sleeps *at least* 20 ms and then adds however long the send took
plus the scheduler's own wake-up latency. The error is one-directional, so it
accumulates: at a realistic 1-2 ms of overshoot per frame, a 30-second call runs
1.5 to 3 seconds long, and every latency figure measured against it is wrong by
a growing amount. Worse, it is wrong in the flattering direction -- audio
arrives later than it should, so the agent looks faster than it is.

The fix is to schedule against absolute deadlines derived from a fixed origin
rather than accumulating sleeps. Frame ``n`` is due at ``origin + n * interval``,
computed from the frame index every time, so a late frame does not push its
successors late -- it just gets a shorter sleep. Error stays bounded by the
scheduler's granularity instead of growing without limit.

The clock is ``time.perf_counter``: monotonic *and* high resolution. Both
properties are load-bearing, and picking the obvious-looking ``time.monotonic``
instead quietly breaks the second one on Windows.

Monotonic matters because wall clock can step backwards during an NTP
adjustment or a DST transition, which would surface as a negative latency
measurement or a very long sleep mid-call.

Resolution matters because of what CPython actually implements these with. On
Windows ``time.monotonic`` is ``GetTickCount64``, whose resolution is 15.625 ms
-- comparable to the entire 20 ms frame interval this module exists to schedule.
Measured with that clock, a frame released perfectly on time reads as either
0 ms or 15.6 ms late depending on where the tick boundary falls, so the lateness
statistics below would be reporting the clock rather than the pacing.
``time.perf_counter`` is ``QueryPerformanceCounter`` there, with a resolution of
100 ns, and is equally fine on Linux. ``time.get_clock_info`` confirms both are
monotonic.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

__all__ = ["Pacer", "PacingStats"]

#: How far behind schedule a frame must be before it is counted as late. One
#: millisecond is well beyond the sub-100-microsecond jitter of a healthy event
#: loop, and well below the 20 ms that would mean an actually dropped frame.
LATE_THRESHOLD_S = 0.001


@dataclass
class PacingStats:
    """How well the pacer held its schedule.

    Reported so that a suspicious latency measurement can be checked against the
    pacing that produced it. If the pacer could not keep up, the numbers derived
    from it are not trustworthy, and the user needs to know that rather than
    being handed a confident wrong answer.
    """

    frames: int = 0
    late_frames: int = 0
    total_lateness_s: float = 0.0
    max_lateness_s: float = 0.0
    #: Wall-clock duration from the schedule origin to the most recent frame.
    elapsed_s: float = 0.0
    #: Where the most recent frame sat on the schedule, in seconds from origin.
    #: This is ``(frames - 1) * interval``, not ``frames * interval``: frame 0 is
    #: due at the origin itself, so N frames span N-1 intervals of sending time
    #: even though they carry N intervals of audio.
    scheduled_s: float = 0.0

    @property
    def mean_lateness_ms(self) -> float:
        return (self.total_lateness_s / self.frames * 1000) if self.frames else 0.0

    @property
    def max_lateness_ms(self) -> float:
        return self.max_lateness_s * 1000

    @property
    def drift_ms(self) -> float:
        """How far the run diverged from the duration it should have taken.

        Positive means the run took longer than the audio it sent. This is the
        single number that shows whether pacing was honest: a naive sleep loop
        makes it grow with call length, while deadline scheduling keeps it flat.
        """
        return (self.elapsed_s - self.scheduled_s) * 1000

    def summary(self) -> str:
        return (
            f"{self.frames} frames in {self.elapsed_s:.2f}s "
            f"(drift {self.drift_ms:+.1f}ms, "
            f"{self.late_frames} late, max {self.max_lateness_ms:.1f}ms)"
        )


class Pacer:
    """Schedules frames against absolute deadlines from a fixed origin.

    The clock and sleep function are injectable so that pacing logic can be
    tested deterministically. Testing a scheduler by actually waiting is slow and
    flaky; testing it against a fake clock that can be advanced by hand is
    neither, and it makes it possible to assert on behaviour -- such as what
    happens when a frame is 200 ms late -- that is impractical to provoke for
    real.

    Args:
        interval_s: Seconds between frames. 0.02 for Twilio.
        clock: Monotonic, high-resolution time source, in seconds.
        sleep: Awaitable sleep.
    """

    def __init__(
        self,
        interval_s: float,
        *,
        clock: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if interval_s <= 0:
            raise ValueError(f"interval_s must be positive, got {interval_s}")

        self.interval_s = interval_s
        self._clock = clock
        self._sleep = sleep

        self._origin: float | None = None
        self._index = 0
        self.stats = PacingStats()

    @property
    def started(self) -> bool:
        return self._origin is not None

    @property
    def origin(self) -> float:
        """The instant frame 0 was due. Raises if pacing has not started."""
        if self._origin is None:
            raise RuntimeError("pacer has not started; call start() first")
        return self._origin

    def start(self) -> float:
        """Fix the schedule origin at the current instant.

        Called explicitly rather than lazily on the first ``wait``, because the
        origin is also the reference point for latency measurement -- it needs to
        be the moment the stream began, not the moment the first frame happened
        to be ready.
        """
        self._origin = self._clock()
        self._index = 0
        self.stats = PacingStats()
        return self._origin

    def deadline_for(self, index: int) -> float:
        """When frame ``index`` is due.

        Always recomputed from the index against a fixed origin, never
        accumulated. That is the whole mechanism: drift cannot build up in a
        value that is derived fresh every time.
        """
        return self.origin + index * self.interval_s

    async def wait(self) -> float:
        """Sleep until the next frame is due; return how late it is, in seconds.

        A positive return means the frame is being released after its deadline --
        the caller was too slow, the event loop was busy, or the sleep itself
        overshot. Zero means on time. The value is never negative: this returns
        lateness, not slack, so that callers accumulating it cannot have real
        lateness cancelled out by frames that happened to be early.

        A frame that is already overdue does not sleep at all, and does not
        delay its successors, because their deadlines were never a function of
        this one.

        Lateness is measured *after* the sleep rather than before it. Measuring
        before would report zero for every frame that slept, which hides the
        most common real imperfection: ``asyncio.sleep`` guarantees a minimum
        duration, not an exact one, so a frame routinely lands a fraction of a
        millisecond past its deadline. Phase 3's latency figures are only as
        honest as this number, so it reports when the frame was actually
        released.
        """
        deadline = self.deadline_for(self._index)
        self._index += 1

        remaining = deadline - self._clock()
        if remaining > 0:
            await self._sleep(remaining)

        released_at = self._clock()
        lateness = max(0.0, released_at - deadline)

        if lateness:
            self.stats.total_lateness_s += lateness
            self.stats.max_lateness_s = max(self.stats.max_lateness_s, lateness)
            if lateness >= LATE_THRESHOLD_S:
                self.stats.late_frames += 1

        self.stats.frames = self._index
        self.stats.scheduled_s = (self._index - 1) * self.interval_s
        self.stats.elapsed_s = released_at - self.origin

        return lateness

    def reset(self) -> None:
        """Forget the origin and statistics, as if never started."""
        self._origin = None
        self._index = 0
        self.stats = PacingStats()
