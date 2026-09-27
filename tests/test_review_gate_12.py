"""Regression tests for review gate 12 (phase 12b, a hangup is a hangup).

Each test fails against 4648e6a, the phase 12b commit the gate reviewed.

1. An agent closing between scenario steps set ``_hung_up``, so the remaining
   steps were skipped -- including ``expect`` steps, which then never appeared
   in the results. A failed expectation became exit 0. (A hangup mid-step
   already skipped them before phase 12b; this closes both.)
2. Any close the session did not start was blamed on the agent, including the
   ones the websockets library starts itself, such as 1009 for an oversized
   frame.
3. After a scenario's own ``hangup`` the session still sat out the whole
   response timeout on the socket it had just closed.
4. The JUnit case for a close before audio existed only when it failed, so CI
   history could never show it as fixed.

The gate's fifth finding, three overlapping close flags, is the reshaping that
fixed 2 and 3 -- one ``_socket_closed`` event and a ``closed_by`` cause -- and
has no behaviour of its own to test.
"""

from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import websockets

from conftest import free_port
from streamdouble import audio, cli
from streamdouble import session as session_module
from streamdouble.api import EXIT_ASSERTION_FAILED, CallReport
from streamdouble.metrics import compute
from streamdouble.scenario import parse as parse_scenario
from streamdouble.session import Session, SessionConfig

RESPONSE_TIMEOUT_S = 5.0


def config(**kw) -> SessionConfig:
    return SessionConfig(
        response_timeout_s=RESPONSE_TIMEOUT_S, quiet_period_s=0.2, max_drain_s=2.0, **kw
    )


def scenario(*steps):
    return parse_scenario({"name": "gate 12", "steps": list(steps)})


# 1 -------------------------------------------------------------------------


@pytest.mark.timeout(60)
async def test_an_expectation_the_call_never_reached_fails(server):
    # The echo agent answers within a few frames, then hangs up at frame 20,
    # halfway through a one-second wait. `expect: no clear` would have passed
    # had it run; it did not run, and that must not read as a pass.
    result = await Session(
        f"{server}?hangup_after=20",
        scenario=scenario({"wait": 1.0}, {"expect": "no clear"}),
        config=config(),
    ).run()

    assert result.first_audio_at is not None, "precondition: the agent spoke"
    assert [passed for _, passed in result.expectations] == [False]
    assert "not reached" in result.expectations[0][0]
    assert result.closed_by == "agent"

    report = CallReport(result=result, metrics=compute(result), thresholds=[])
    assert report.exit_code == EXIT_ASSERTION_FAILED


# 2 -------------------------------------------------------------------------


@pytest.mark.timeout(30)
async def test_a_close_the_library_started_is_not_blamed_on_the_agent(monkeypatch):
    port = free_port()

    async def oversized_reply(websocket, *args):
        for _ in range(2):  # connected, start
            await websocket.recv()
        await websocket.send(json.dumps({"event": "media", "pad": "x" * 4096}))
        async for _ in websocket:
            pass

    monkeypatch.setattr(session_module, "MAX_INBOUND_FRAME_BYTES", 1024)
    server = await websockets.serve(oversized_reply, "127.0.0.1", port, max_size=None)
    try:
        result = await Session(
            f"ws://127.0.0.1:{port}/media-stream",
            [audio.silence_frame()] * 25,
            config=config(),
        ).run()
    finally:
        server.close()
        await server.wait_closed()

    assert not result.closed_early
    assert result.closed_by == "streamdouble"
    assert result.closed_before_audio
    assert any("1009" in warning for warning in result.warnings)


# 3 -------------------------------------------------------------------------


@pytest.mark.timeout(30)
async def test_the_callers_hangup_does_not_wait_out_the_response_timeout(server):
    started = time.perf_counter()
    result = await Session(
        f"{server}?mode=silent",
        scenario=scenario({"wait": 0.1}, "hangup"),
        config=config(),
    ).run()
    elapsed = time.perf_counter() - started

    assert elapsed < RESPONSE_TIMEOUT_S / 2
    assert result.closed_by == "caller"
    assert not result.closed_early
    assert not result.timed_out
    # Still exit 2: the agent never spoke, whoever ended the call. Before this
    # it was also exit 2, via the timeout it no longer waits for -- so not
    # waiting must not turn it green.
    assert result.closed_before_audio


# 4 -------------------------------------------------------------------------


@pytest.mark.timeout(60)
def test_the_close_before_audio_testcase_is_present_when_it_passes(server, tmp_path: Path):
    report = tmp_path / "junit.xml"
    code = cli.main([
        "call", server, "--audio", "fixtures/speech_8k.wav",
        "--quiet", "--junit", str(report),
    ])

    assert code == cli.EXIT_OK
    cases = {
        case.get("name"): case.find("failure")
        for case in ET.parse(report).getroot().iter("testcase")
    }
    assert "no close before audio" in cases
    assert cases["no close before audio"] is None
