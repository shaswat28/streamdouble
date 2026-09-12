"""Regression tests for what review gate 8 found.

Four findings, and all four are one root cause seen from different angles: the
two-track interleave was driven by the *caller's* frame list, so anything about
the agent's track that did not line up one-for-one with the caller's was lost.

That is worth stating because it is the argument against fixing symptoms. Three
of the four could each have been patched where they surfaced -- a cursor here, a
padding rule there, a counter increment -- and the fourth would still have been
waiting. Moving the outbound track onto its own cursor with its own drain phase
fixes all of them at once, and makes the fifth variant that nobody has thought
of yet less likely.

Every test here was verified to fail against the behaviour it describes.
"""

from __future__ import annotations

import pathlib

import pytest

from streamdouble import api, audio
from streamdouble.protocol import TRACK_INBOUND, TRACK_OUTBOUND
from streamdouble.session import Session, SessionConfig

ASYNC = pytest.mark.asyncio(loop_scope="module")

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent


def frames(count: int, marker: int = 0xFF) -> list[bytes]:
    """Distinguishable frames, so a replay is visible rather than inferred."""
    return [bytes([(marker + i) % 256]) * audio.FRAME_BYTES for i in range(count)]


def fork_config(agent_frames, **kw) -> SessionConfig:
    return SessionConfig(
        response_timeout_s=3.0,
        quiet_period_s=0.3,
        max_drain_s=2.0,
        fork=True,
        tracks=[TRACK_INBOUND, TRACK_OUTBOUND],
        agent_frames=list(agent_frames),
        **kw,
    )


def fork_url(server: str, suffix: str = "") -> str:
    return server.replace("/media-stream", "/media-stream-fork") + suffix


# ---------------------------------------------------------------------------
# Finding 1 -- the agent's track restarted on every scenario step
# ---------------------------------------------------------------------------


@ASYNC
async def test_the_outbound_track_does_not_restart_between_steps(
    server, speech_8k_path, tmp_path
):
    """A cursor on the session, not on the send loop.

    The old code paired by the loop's own index, and every scenario step began
    a fresh loop -- so `say` then `wait` forked the agent's opening words, then
    forked those same opening words again as the 'silence' step. An app under
    test heard the agent say the same thing twice in one call, which for a
    transcription consumer is a transcript of something that never happened.

    **Driven through a real multi-step scenario, and that matters.** The first
    version of this test called `_next_outbound_frame` directly, which walks
    the cursor correctly no matter where the cursor lives -- so it passed
    against a mutation that reset the cursor at the top of `_stream_frames`,
    which is the bug. Testing the helper instead of the path is the same
    mistake that let `streamdouble scenario` ship unreachable, and mutation
    testing is the only reason it was caught here.

    The agent's track is shorter than the caller's, so a replay shows up as
    *more* outbound frames than the agent had to give.
    """
    scenario = tmp_path / "s.yaml"
    scenario.write_text(
        "name: two steps\nsteps:\n"
        f"  - say: {speech_8k_path.as_posix()}\n"
        "  - wait: 0.5\n",
        encoding="utf-8",
    )

    agent = frames(20, marker=0x10)
    report = await api.run_scenario(
        fork_url(server), scenario, config=fork_config(agent)
    )

    assert report.result.outbound_frames_sent == len(agent), (
        f"{report.result.outbound_frames_sent} outbound frames were sent from "
        f"a track of {len(agent)}: the cursor restarted and replayed the "
        "agent's audio"
    )


def test_the_cursor_is_not_shared_between_sessions():
    """Two calls must not resume each other's agent track."""
    agent = frames(3, marker=0x20)
    config = fork_config(agent)

    first = Session("ws://unused", frames(1), config=config)
    first._next_outbound_frame()

    second = Session("ws://unused", frames(1), config=config)
    assert second._next_outbound_frame()[0] == 0x20, "the cursor leaked across sessions"


# ---------------------------------------------------------------------------
# Finding 2 -- scenario accepted the fork flags and ignored them
# ---------------------------------------------------------------------------


def test_both_subcommands_validate_the_fork_options():
    """One validation, reachable from both.

    The fork flags live on the *shared* option set, so `scenario` accepted
    every one of them -- and then called `config_from` without the agent's
    frames, declaring an outbound track in its start frame and sending nothing
    on it for the whole call.
    """
    from streamdouble.cli import UsageError, agent_frames_from, build_parser

    parser = build_parser()

    for argv in (
        ["call", "ws://x", "--audio", "a.wav", "--fork", "--track", "both"],
        ["scenario", "s.yaml", "ws://x", "--fork", "--track", "both"],
    ):
        args = parser.parse_args(argv)
        with pytest.raises(UsageError, match="needs --agent-audio"):
            agent_frames_from(args)


def test_agent_audio_without_fork_is_refused_on_both():
    from streamdouble.cli import UsageError, agent_frames_from, build_parser

    parser = build_parser()
    for argv in (
        ["call", "ws://x", "--audio", "a.wav", "--agent-audio", "b.wav"],
        ["scenario", "s.yaml", "ws://x", "--agent-audio", "b.wav"],
    ):
        args = parser.parse_args(argv)
        with pytest.raises(UsageError, match="only means something with --fork"):
            agent_frames_from(args)


def test_a_scenario_fork_carries_the_agent_track_through_the_cli(
    server, speech_8k_path, tmp_path
):
    """Through the real entry point, because that is where the bug was.

    The validation was fine and the session was fine; `run_scenario_command`
    simply dropped the loaded frames on the floor. A test that calls
    `api.run_scenario` with a config already holding `agent_frames` cannot see
    that -- the first version of this test did exactly that and passed against
    the mutation that removed the wiring.

    Run in a subprocess rather than through `cli.main` in-process. `main` calls
    `asyncio.run`, and doing that between the async tests in this module
    disturbs the loop they share: the second version of this test turned four
    unrelated tests red without touching them. A subprocess also exercises the
    installed console entry point, which is what a user actually runs.

    Asserted through `--trace`, since the command line returns an exit code
    rather than a report, and the trace is the only place it publishes what
    went on the wire.
    """
    import json
    import subprocess
    import sys

    scenario = tmp_path / "s.yaml"
    scenario.write_text(
        "name: fork\nsteps:\n"
        f"  - say: {speech_8k_path.as_posix()}\n"
        "  - wait: 0.3\n",
        encoding="utf-8",
    )
    trace = tmp_path / "t.jsonl"

    completed = subprocess.run(
        [
            sys.executable, "-m", "streamdouble.cli",
            "scenario", str(scenario), fork_url(server),
            "--fork", "--track", "both",
            "--agent-audio", str(speech_8k_path),
            "--trace", str(trace),
            "--quiet",
            "--quiet-period", "0.3", "--max-drain", "2", "--response-timeout", "3",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT_DIR,
    )
    assert completed.returncode == 0, completed.stderr

    records = [json.loads(line) for line in trace.read_text().splitlines()]
    outbound = [
        r for r in records
        if r.get("dir") == "out"
        and r.get("event") == "media"
        and r.get("track") == "outbound"
    ]

    assert outbound, (
        "the scenario declared an outbound track in its start frame and then "
        "sent nothing on it"
    )


# ---------------------------------------------------------------------------
# Finding 3 -- a longer agent track was truncated
# ---------------------------------------------------------------------------


@ASYNC
async def test_agent_audio_longer_than_the_caller_is_not_truncated(
    server, speech_8k_path
):
    """A short question and a long answer is the normal shape of a call.

    Driving the interleave from the caller's frames cut the agent off the
    moment the caller fell silent and discarded the rest without a word. For a
    compliance-recording consumer that means archiving a call that was never
    made.
    """
    caller = audio.wav_to_ulaw_frames(speech_8k_path)
    agent = caller * 2  # the agent talks for twice as long

    report = await api.call(
        fork_url(server), frames=caller, config=fork_config(agent)
    )

    assert report.result.outbound_frames_sent == len(agent), (
        f"{report.result.outbound_frames_sent} of {len(agent)} agent frames "
        "were forked; the rest were dropped when the caller stopped"
    )


@ASYNC
async def test_a_shorter_agent_track_is_still_not_padded(server, speech_8k_path):
    """The other direction was already right and must stay right.

    A fork carries whatever each side actually produced. Padding the agent's
    track with silence to match the caller would invent audio.
    """
    caller = audio.wav_to_ulaw_frames(speech_8k_path)
    agent = caller[:10]

    report = await api.call(
        fork_url(server), frames=caller, config=fork_config(agent)
    )

    assert report.result.outbound_frames_sent == 10


# ---------------------------------------------------------------------------
# Finding 4 -- the frame count
# ---------------------------------------------------------------------------


@ASYNC
async def test_frames_sent_counts_both_tracks(server, speech_8k_path):
    """150 frames on the wire reported as 100 is a number someone reconciles.

    Both totals are published, because "the caller sent 2.00s of audio" and
    "150 frames went over the wire" are different questions and one number
    cannot answer both.
    """
    caller = audio.wav_to_ulaw_frames(speech_8k_path)
    agent = caller[: len(caller) // 2]

    report = await api.call(
        fork_url(server), frames=caller, config=fork_config(agent)
    )

    assert report.result.outbound_frames_sent == len(agent)
    assert report.metrics.frames_sent == len(caller) + len(agent), (
        "frames_sent should count every media frame that went out"
    )


@ASYNC
async def test_a_bidirectional_call_reports_no_outbound_frames(server, speech_8k_path):
    """The new counter must stay zero on the path everyone actually uses."""
    report = await api.call(
        server,
        audio_path=speech_8k_path,
        config=SessionConfig(response_timeout_s=3.0, quiet_period_s=0.3, max_drain_s=2.0),
    )

    assert report.result.outbound_frames_sent == 0
    assert report.metrics.frames_sent > 0
