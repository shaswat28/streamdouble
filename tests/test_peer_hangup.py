"""Regression tests for an agent hanging up, found by CI rather than a gate.

``test_review_gate_2::test_an_abrupt_hangup_still_reports_cleanly`` failed once
on Python 3.13 on Windows. The session only noticed a hangup when a later send
failed, and the receive loop ended silently on a clean close. If the close
arrived after the caller's last frame there was no later send, so:

1. The call was reported as ``timed_out`` -- "the agent sent no audio" -- after
   sitting out the whole response timeout on a socket that was already closed.
2. An agent that hung up *without ever speaking* exited 0 whenever the close
   was noticed, because ``closed_early`` never reached the exit code. README
   documents exit 2 as "the agent never spoke".

The three regression tests fail against 9f64564 on behaviour. They use
``hangup_after=`` at or past the number of frames sent, so the close lands
after the last send and nothing depends on a runner being slow. The three
guards at the end check the fix does not overreach; against 9f64564 they fail
only because ``closed_by`` did not exist, which proves nothing.

Gate 12 reshaped the fix (``hung_up_silent`` became ``closed_by`` plus
``closed_before_audio``); its own findings are in test_review_gate_12.py.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import websockets

from conftest import free_port
from streamdouble import audio, cli
from streamdouble.protocol import TRACK_INBOUND
from streamdouble.scenario import load as load_scenario
from streamdouble.session import Session, SessionConfig

FRAMES = 5
RESPONSE_TIMEOUT_S = 5.0


@pytest.fixture
def frames() -> list[bytes]:
    return [audio.silence_frame()] * FRAMES


def config(**kw) -> SessionConfig:
    return SessionConfig(
        response_timeout_s=RESPONSE_TIMEOUT_S, quiet_period_s=0.2, max_drain_s=2.0, **kw
    )


# 1 -------------------------------------------------------------------------


@pytest.mark.timeout(30)
async def test_a_hangup_after_the_last_frame_is_a_hangup_not_a_timeout(server, frames):
    result = await Session(
        f"{server}?hangup_after={FRAMES}", frames, config=config()
    ).run()

    assert result.closed_early
    assert not result.timed_out
    assert result.closed_by == "agent"
    assert result.closed_before_audio


@pytest.mark.timeout(30)
async def test_a_hangup_does_not_wait_out_the_response_timeout(server, frames):
    started = time.perf_counter()
    await Session(f"{server}?hangup_after={FRAMES}", frames, config=config()).run()
    elapsed = time.perf_counter() - started

    # Five frames take 0.1 s. Anything near the timeout means it was waited out.
    assert elapsed < RESPONSE_TIMEOUT_S / 2


# 2 -------------------------------------------------------------------------


@pytest.mark.timeout(60)
def test_an_agent_that_hangs_up_without_speaking_does_not_exit_0(server, tmp_path: Path):
    # Mid-clip, so this hangup *was* noticed -- closed_early was set -- and it
    # still exited 0, because nothing mapped closed_early to an exit code.
    report = tmp_path / "junit.xml"
    code = cli.main([
        "call", f"{server}?hangup_after=2&mode=silent",
        "--audio", "fixtures/speech_8k.wav",
        "--quiet", "--junit", str(report),
    ])

    assert code == cli.EXIT_TIMEOUT
    failed = [
        case.get("name")
        for case in ET.parse(report).getroot().iter("testcase")
        if case.find("failure") is not None
    ]
    assert "no close before audio" in failed


# Guards: not regressions, but the two ways the fix could overreach ----------


@pytest.mark.timeout(60)
def test_an_agent_that_spoke_then_hung_up_is_not_called_silent(server, tmp_path: Path):
    # The echo agent replies within ~200 ms, well before frame 60.
    code = cli.main([
        "call", f"{server}?hangup_after=60",
        "--audio", "fixtures/speech_8k.wav", "--quiet",
    ])
    assert code == cli.EXIT_OK


@pytest.mark.timeout(60)
async def test_the_callers_own_hangup_step_is_not_the_agent_closing(server, tmp_path):
    # A scenario's `hangup` closes the socket from our side. The receive loop
    # sees a close either way; mistaking this one for the agent's would report
    # every hangup scenario as the agent's fault.
    path = tmp_path / "hangup.yaml"
    path.write_text("name: hangup\nsteps:\n  - wait: 0.2\n  - hangup\n")

    result = await Session(server, scenario=load_scenario(path), config=config()).run()

    assert not result.closed_early
    assert result.closed_by == "caller"


@pytest.mark.timeout(30)
async def test_a_fork_consumer_closing_is_never_a_silent_hangup(frames):
    # A fork has no channel back, so no audio is the correct outcome there.
    port = free_port()

    async def read_three_then_close(websocket, *args):
        for _ in range(3):
            await websocket.recv()
        await websocket.close()

    server = await websockets.serve(read_three_then_close, "127.0.0.1", port)
    try:
        result = await Session(
            f"ws://127.0.0.1:{port}/fork", frames,
            config=config(fork=True, tracks=[TRACK_INBOUND]),
        ).run()
    finally:
        server.close()
        await server.wait_closed()

    assert result.closed_early
    assert not result.closed_before_audio
    assert not result.timed_out
