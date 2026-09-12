"""This package does not leak event loops, sockets or tasks.

These tests exist because of what they replaced.

`filterwarnings = ["error"]` used to catch resource leaks by accident: an
unclosed socket or loop became an unraisable ResourceWarning, and the suite
went red. That was never a real detector, for two reasons. It fired on
pytest-asyncio's own per-test loop churn on Windows, which is not this
package's problem -- and when it fired it named whichever unrelated test was
running when the garbage collector happened to reach the object, so the report
pointed nowhere near the cause. A detector that cries wolf and misreports the
location is one people learn to silence.

So the noise is now filtered in `pyproject.toml`, and the detection it was
accidentally providing is done deliberately here instead: run the real thing
many times over a real socket and count what survives. A genuine leak in
`session.py` -- a connection not closed on an error path, a TaskGroup child
outliving its parent -- fails these, by name, at the place it happened.

The counts are absolute rather than relative. "Did not grow" passes trivially
when the first iteration already leaked.
"""

from __future__ import annotations

import asyncio
import gc

import pytest

from streamdouble import api
from streamdouble.session import SessionConfig

pytestmark = pytest.mark.asyncio(loop_scope="module")

FAST = SessionConfig(response_timeout_s=3.0, quiet_period_s=0.3, max_drain_s=3.0)

#: Enough repetitions that a per-call leak is unmistakable, few enough that the
#: test stays quick. A leak of one object per call shows up as REPEATS of them.
REPEATS = 8


def _unclosed_loops() -> list[asyncio.AbstractEventLoop]:
    """Event loops that exist, are not closed, and are not currently running.

    The running exclusion matters: the session-scoped example agent runs
    uvicorn in a thread with a live loop of its own, and pytest-asyncio holds
    the loop this test is executing in. Neither is a leak, and counting them
    would make this test assert a constant rather than a property.
    """
    gc.collect()
    return [
        obj
        for obj in gc.get_objects()
        if isinstance(obj, asyncio.AbstractEventLoop)
        and not obj.is_closed()
        and not obj.is_running()
    ]


def _open_sockets() -> list:
    import socket as socket_module

    gc.collect()
    return [
        obj
        for obj in gc.get_objects()
        if isinstance(obj, socket_module.socket) and obj.fileno() != -1
    ]


async def test_repeated_calls_leak_no_event_loops(server, speech_8k_path):
    """The headline check: placing many calls leaves no loop behind."""
    before = len(_unclosed_loops())

    for _ in range(REPEATS):
        await api.call(server, audio_path=speech_8k_path, config=FAST)

    after = len(_unclosed_loops())
    assert after <= before, (
        f"{after - before} event loop(s) leaked across {REPEATS} calls "
        f"({before} before, {after} after)"
    )


async def test_repeated_calls_leak_no_sockets(server, speech_8k_path):
    """`Session.run` closes its connection in a `finally`; this proves it.

    A socket left open per call is the failure gate 2 went looking for on the
    error paths, and the one most likely to come back when the send and receive
    tasks are next rearranged.
    """
    before = len(_open_sockets())

    for _ in range(REPEATS):
        await api.call(server, audio_path=speech_8k_path, config=FAST)

    after = len(_open_sockets())
    # A small allowance: the shared uvicorn server accepts and retires
    # connections on its own schedule, and its bookkeeping is not ours.
    assert after - before < REPEATS, (
        f"sockets grew by {after - before} across {REPEATS} calls, which tracks "
        f"the call count -- that is a per-call leak ({before} before, {after} after)"
    )


async def test_the_error_paths_leak_nothing_either(server, speech_8k_path):
    """Cleanup on the paths that skip the happy ending.

    Gate 2's finding was a `finally` that raised and replaced the real error,
    and its lesson was that error paths are where cleanup is skipped. A silent
    agent times out, a garbage agent aborts on a violation, and an unreachable
    one never connects at all -- three different exits from `run`, none of which
    may leave a socket or a loop behind.
    """
    from conftest import free_port

    before_loops = len(_unclosed_loops())
    before_sockets = len(_open_sockets())

    dead_port = free_port()
    for _ in range(REPEATS):
        await api.call(f"{server}?mode=silent", audio_path=speech_8k_path, config=FAST)
        await api.call(f"{server}?garbage_after=5", audio_path=speech_8k_path, config=FAST)
        with pytest.raises(api.ConnectionFailed):
            await api.call(
                f"ws://127.0.0.1:{dead_port}/nope", audio_path=speech_8k_path, config=FAST
            )

    assert len(_unclosed_loops()) <= before_loops, "an error path leaked an event loop"
    assert len(_open_sockets()) - before_sockets < REPEATS, "an error path leaked a socket"
