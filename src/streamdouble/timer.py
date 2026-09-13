"""Ask Windows for a 1 ms timer for the duration of a call.

**Why.** On Windows, asyncio's proactor loop waits for work with
``GetQueuedCompletionStatus`` and a millisecond timeout
(``math.ceil(timeout * 1e3)`` in ``asyncio/windows_events.py``). A wait like
that only ends on a tick of the system timer, which is 15.625 ms by default.
Every frame the pacer sleeps towards can therefore wake up to a whole tick
late, against a 20 ms frame.

Measured on this project's development machine (Windows 11, CPython 3.12.10),
median of 6 alternating runs each way. First, real calls against the echo
agent, with the request forced off for the default rows:

==================== =========== ========== ================
real call            mean late   max late   first audio
==================== =========== ========== ================
system default       4.28 ms     28.8 ms    183 ms
timeBeginPeriod(1)   1.34 ms     6.7 ms     182 ms
==================== =========== ========== ================

Time to first audio did not move. That is the right result: the change makes
the *pacing* precise, and it should not shift a latency that the pacing was
already honest about. A bare pacer loop with no socket shows the timer effect
alone, larger because nothing else wakes the loop between frames:

==================== =========== ========== ============
bare pacer, 300 fr.  mean late   max late   late frames
==================== =========== ========== ============
system default       8.21 ms     17.1 ms    287 / 300
timeBeginPeriod(1)   1.23 ms     3.2 ms     186 / 300
==================== =========== ========== ============

Only the bare loop crossed the quarter-frame (5 ms) line that
``Metrics.measurement_is_reliable`` draws. Real calls on that machine stayed
under it, so this sharpens figures that were already reported as reliable;
it did not rescue unreliable ones. A slower machine could sit nearer the line.

**What the docs say.** Source:
https://learn.microsoft.com/en-us/windows/win32/api/timeapi/nf-timeapi-timebeginperiod

- ``timeBeginPeriod`` returns ``TIMERR_NOERROR`` (0) on success, and every
  call must be matched by ``timeEndPeriod`` with the same value.
- Since Windows 10 version 2004 it affects only the calling process, not the
  whole system, so a short request scoped to one call is a polite one.
- Since Windows 11, a *window-owning* process that is occluded, minimised or
  invisible is not guaranteed the higher resolution. That is why this module
  reports what it asked for, not what it got, and why the lateness figures
  stay the real evidence.

**Honesty.** The outcome is recorded next to the pacing statistics it
affects: ``"raised"``, ``"refused"``, or ``"not needed"`` off Windows. A
lateness figure is only interpretable when you know which timer produced it.
A refusal is never an error, because a call without the finer timer is still
a valid call, just a less precise one, and the published lateness says so.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator
from typing import Any

__all__ = ["NOT_NEEDED", "NOT_REQUESTED", "RAISED", "REFUSED", "high_resolution_timer"]

RAISED = "raised"
REFUSED = "refused"
NOT_NEEDED = "not needed"
#: A pacer used directly, outside a session, never asked.
NOT_REQUESTED = "not requested"

_PERIOD_MS = 1
_TIMERR_NOERROR = 0


def _load_winmm() -> Any:
    import ctypes

    return ctypes.WinDLL("winmm")


@contextlib.contextmanager
def high_resolution_timer(
    *, platform: str | None = None, winmm: Any = None
) -> Iterator[str]:
    """Hold a 1 ms timer period for the ``with`` block and yield the outcome.

    ``platform`` and ``winmm`` are injectable so the Windows path can be tested
    everywhere, including the refusal and cleanup paths no real machine takes.
    """
    if (platform or sys.platform) != "win32":
        yield NOT_NEEDED
        return

    try:
        library = winmm if winmm is not None else _load_winmm()
        granted = library.timeBeginPeriod(_PERIOD_MS) == _TIMERR_NOERROR
    except (OSError, AttributeError):
        # No winmm, or a stripped-down Windows without it. Still a valid call.
        yield REFUSED
        return

    if not granted:
        # Nothing to undo: an unmatched timeEndPeriod would be a bug of its own.
        yield REFUSED
        return

    try:
        yield RAISED
    finally:
        library.timeEndPeriod(_PERIOD_MS)
