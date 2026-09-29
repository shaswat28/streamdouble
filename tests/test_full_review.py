"""Regression tests for the full-codebase review after phase 12b.

Four read-only reviewers covered the whole tree; these are the findings that
survived checking. Each test fails against 34adbe4.

1. ``scenario`` accepted ``--repeat``, ``--baseline`` and ``--save-baseline``
   and ignored all three: one call, no baseline written, no comparison made.
2. On a two-track fork, a dropped or delayed caller frame also dropped or
   delayed the agent's frame for that instant, although the impairments model
   the caller's leg only.
3. Both tracks of a fork shared one presentation clock, so each track's
   ``media.timestamp`` advanced 40 ms per 20 ms frame. Found while fixing 2.
4. ``wait_for: [audio, mark]`` raised TypeError instead of ScenarioError, and
   ``wait: .nan`` / ``.inf`` passed validation and crashed mid-call.
5. ``streamdouble inspect`` took its time origin from the first *parseable*
   frame, so garbage at the start of a trace shifted every time shown.
"""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path

import pytest
import websockets

from conftest import free_port
from streamdouble import audio, cli, tracereport
from streamdouble.chaos import Impairments
from streamdouble.protocol import TRACK_INBOUND, TRACK_OUTBOUND
from streamdouble.scenario import ScenarioError
from streamdouble.scenario import parse as parse_scenario
from streamdouble.session import Session, SessionConfig

ROOT = Path(__file__).resolve().parent.parent


# 1 -------------------------------------------------------------------------


@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    "flags",
    [["--repeat", "3"], ["--save-baseline", "b.json"], ["--baseline", "b.json"]],
)
def test_scenario_refuses_the_flags_it_cannot_honour(server, tmp_path: Path, flags):
    flags = [str(tmp_path / f) if f.endswith(".json") else f for f in flags]
    # A parse error, so argparse exits rather than main() returning. Before
    # the fix this returned 0 after placing one call.
    with pytest.raises(SystemExit) as exited:
        cli.main([
            "scenario", str(ROOT / "scenarios" / "hangup.yaml"), server, "--quiet", *flags,
        ])

    assert exited.value.code == cli.EXIT_USAGE
    assert not (tmp_path / "b.json").exists()


# 2 and 3 -------------------------------------------------------------------


async def _fork_capture(impairments: Impairments) -> list[dict]:
    """Run a two-track fork and return every media frame the app received."""
    port = free_port()
    received: list[dict] = []

    async def app(websocket, *args):
        async for raw in websocket:
            message = json.loads(raw)
            if message.get("event") == "media":
                received.append(message["media"])

    frames = [audio.silence_frame()] * 50
    server = await websockets.serve(app, "127.0.0.1", port)
    try:
        await Session(
            f"ws://127.0.0.1:{port}/fork",
            frames,
            config=SessionConfig(
                response_timeout_s=1.0, quiet_period_s=0.2, max_drain_s=1.0,
                fork=True, tracks=[TRACK_INBOUND, TRACK_OUTBOUND],
                agent_frames=list(frames),
                impairments=impairments, chaos_seed=7,
            ),
        ).run()
    finally:
        server.close()
        await server.wait_closed()
    return received


@pytest.mark.timeout(30)
async def test_caller_side_loss_does_not_hold_back_the_agent_track():
    received = await _fork_capture(Impairments(loss=0.3))
    tracks = [m["track"] for m in received]
    last_inbound = max(i for i, t in enumerate(tracks) if t == TRACK_INBOUND)
    trailing_outbound = tracks[last_inbound + 1:].count(TRACK_OUTBOUND)
    # Ticks after the last caller frame that got through: its presentation
    # time says which of the 50 ticks it was, dropped ticks included.
    last_tick = int(received[last_inbound]["timestamp"]) // audio.FRAME_MS
    trailing_ticks = 49 - last_tick

    assert tracks.count(TRACK_OUTBOUND) == 50
    assert tracks.count(TRACK_INBOUND) < 50, "precondition: some caller frames dropped"
    # One agent frame per tick, on schedule. The old code deferred one per
    # dropped caller frame and flushed them all after the caller finished.
    # The agent's frame for a tick goes out just before the caller's (gate 13
    # fixed the order), so only the ticks after the last caller frame trail it.
    assert trailing_outbound == trailing_ticks


@pytest.mark.timeout(30)
async def test_each_track_of_a_fork_keeps_its_own_presentation_time():
    received = await _fork_capture(Impairments())
    by_track = {
        track: [int(m["timestamp"]) for m in received if m["track"] == track]
        for track in (TRACK_INBOUND, TRACK_OUTBOUND)
    }

    for track, stamps in by_track.items():
        assert stamps[0] == 0, track
        steps = {b - a for a, b in pairwise(stamps)}
        assert steps == {audio.FRAME_MS}, f"{track} advanced by {steps}"


# 4 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "step",
    [
        {"wait_for": ["audio", "mark"]},
        {"wait_for": {"event": {"nested": 1}}},
        {"wait": float("nan")},
        {"wait": float("inf")},
        {"wait_for": {"event": "audio", "timeout": float("nan")}},
        {"wait_for": {"event": "audio", "timeout": True}},
    ],
)
def test_a_malformed_step_is_a_scenario_error(step):
    with pytest.raises(ScenarioError):
        parse_scenario({"name": "bad", "steps": [step]})


# 5 -------------------------------------------------------------------------


def test_the_time_origin_is_the_first_frame_even_if_unparseable():
    lines = [
        json.dumps({"t": 1.0, "dir": "in", "unparseable": True, "bytes": 5, "excerpt": "x"}),
        json.dumps({"t": 1.5, "dir": "out", "event": "start"}),
    ]
    assert tracereport.summarise(lines).origin_t == 1.0
