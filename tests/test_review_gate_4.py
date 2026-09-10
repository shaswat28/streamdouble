"""Regression tests for the issues found at review gate 4.

Gate 4 pairs a code review with a security review, and its findings share a
shape: nothing here misbehaves against a well-behaved agent. They only appear
when the peer is hostile, broken, or merely much faster than expected — which
is the situation this tool exists to be pointed at.
"""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path

import pytest
import websockets

from conftest import free_port
from streamdouble import audio, scenario
from streamdouble import session as session_module
from streamdouble.scenario import Say, ScenarioError, Wait
from streamdouble.session import Session, SessionConfig


def quick(**overrides) -> SessionConfig:
    defaults = {"response_timeout_s": 3.0, "quiet_period_s": 0.3, "max_drain_s": 3.0}
    return SessionConfig(**{**defaults, **overrides})


# --------------------------------------------------------------------------
# Resource bounds
# --------------------------------------------------------------------------


def test_every_retention_limit_is_finite_and_generous():
    """The caps exist, and are far above any real call.

    A cap that a legitimate call can hit is a bug of its own, so these assert
    both halves: bounded, and bounded well beyond plausible use. 60 MB of
    mu-law is over two hours; 200k frames is over an hour.
    """
    assert 0 < session_module.MAX_AUDIO_BYTES <= 512 * 1024 * 1024
    assert session_module.MAX_AUDIO_BYTES >= 30 * 1024 * 1024

    assert 0 < session_module.MAX_EVENTS <= 10_000_000
    assert session_module.MAX_EVENTS >= 100_000

    assert 0 < session_module.MAX_REPORTED_ISSUES <= 100_000


@pytest.mark.timeout(120)
async def test_a_flooding_agent_cannot_exhaust_memory():
    """A peer streaming as fast as the socket allows is bounded.

    Measured before the fix: 400 KB frames -- comfortably under the 8 MiB
    per-frame cap -- streamed flat out produced 206 MB of buffered audio and
    436 MB of peak process memory in about five seconds. The per-frame limit
    does not help, because it is per frame while the buffer is per call. At the
    default 30 s drain that is gigabytes, and `--out` then tries to write all
    of it to disk; a CI runner OOMs and it reads as flaky infrastructure.

    The cap is checked here at a deliberately small setting so the test is fast
    and does not itself allocate hundreds of megabytes.
    """
    port = free_port()
    payload = base64.b64encode(b"\xff" * 200_000).decode()

    async def flood(websocket, *args):
        stream_sid = None
        async for raw in websocket:
            message = json.loads(raw)
            if message.get("event") == "start":
                stream_sid = message["start"]["streamSid"]
                break
        with pytest.raises(Exception):  # noqa: B017 - any close ends the flood
            while True:
                await websocket.send(
                    json.dumps(
                        {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": payload},
                        }
                    )
                )

    server = await websockets.serve(flood, "127.0.0.1", port, max_size=None)
    original = session_module.MAX_AUDIO_BYTES
    session_module.MAX_AUDIO_BYTES = 2 * 1024 * 1024  # 2 MB, to keep this quick
    try:
        result = await Session(
            f"ws://127.0.0.1:{port}/media-stream",
            [audio.silence_frame()] * 10,
            config=quick(max_drain_s=1.0),
        ).run()

        assert result.audio_truncated
        # Bounded by the cap, with one frame's slack for the frame in flight.
        assert len(result.audio_received) <= session_module.MAX_AUDIO_BYTES + 200_000
        # The counters stay exact even though the recording stopped growing.
        assert result.media_frames_received > 0
    finally:
        session_module.MAX_AUDIO_BYTES = original
        server.close()
        await server.wait_closed()


@pytest.mark.timeout(60)
async def test_truncation_is_reported_rather_than_silent(server):
    """Hitting the cap is recorded, not quietly absorbed.

    A truncated recording that claims to be complete is worse than the
    unbounded buffer it replaced: the WAV looks fine and is not.
    """
    original = session_module.MAX_AUDIO_BYTES
    session_module.MAX_AUDIO_BYTES = 1600  # a fifth of a second
    try:
        result = await Session(
            server, audio.wav_to_ulaw_frames("fixtures/speech_8k.wav")[:40], config=quick()
        ).run()

        assert result.audio_truncated
        assert result.events_of("audio_truncated")
    finally:
        session_module.MAX_AUDIO_BYTES = original


def test_violations_are_counted_beyond_the_retention_limit():
    """An agent broken on every frame is one bug, not ten thousand reports.

    The retained list stops growing; the count does not, so the report still
    says how bad it actually was.
    """
    from streamdouble.protocol import ProtocolViolation, StreamIdentity
    from streamdouble.session import SessionResult

    result = SessionResult(identity=StreamIdentity(), events=[])
    for _ in range(session_module.MAX_REPORTED_ISSUES + 500):
        if len(result.violations) < session_module.MAX_REPORTED_ISSUES:
            result.violations.append(ProtocolViolation("bad_base64", "x"))
        result.violation_count += 1

    assert len(result.violations) == session_module.MAX_REPORTED_ISSUES
    assert result.violation_count == session_module.MAX_REPORTED_ISSUES + 500


# --------------------------------------------------------------------------
# Whole-call bound
# --------------------------------------------------------------------------


def test_the_call_has_a_ceiling_of_its_own():
    """max_call_s exists and is finite.

    Every other wait in a session was already bounded -- response, drain,
    connect, the final stop send -- but the send phase was not, so a scenario
    step could hold a call open indefinitely. In CI a job that hangs costs more
    than one that fails.
    """
    assert 0 < SessionConfig().max_call_s < float("inf")


@pytest.mark.timeout(60)
async def test_a_long_scenario_is_cut_off_at_the_ceiling(server):
    """A scenario that would stream for far too long stops at the limit."""
    started_frames = 0
    result = await Session(
        f"{server}?mode=silent",
        scenario=scenario.Scenario(name="forever", steps=[Wait(seconds=600)]),
        config=quick(max_call_s=1.0, response_timeout_s=0.5, max_drain_s=0.5),
    ).run()

    assert result.events_of("max_call_reached")
    # 600 seconds is 30,000 frames; a 1 second ceiling allows a tiny fraction.
    assert result.frames_sent < 500, f"sent {result.frames_sent} frames past the ceiling"
    assert started_frames == 0


# --------------------------------------------------------------------------
# Scenario path containment
# --------------------------------------------------------------------------


def test_a_scenario_cannot_reach_across_the_filesystem():
    """Relative clip paths stay within the project.

    Scenario files look like configuration rather than code and get shared
    around, but a `say:` step names a path this process reads and streams to
    the endpoint under test.
    """
    directory = Path(tempfile.mkdtemp())
    path = directory / "escape.yaml"
    path.write_text("steps:\n  - say: ../../../../../../etc/passwd\n")

    with pytest.raises(ScenarioError, match="escapes"):
        scenario.load(path)


def test_a_clip_beside_the_scenario_is_fine(tmp_path):
    import shutil

    shutil.copy("fixtures/speech_8k.wav", tmp_path / "hello.wav")
    path = tmp_path / "s.yaml"
    path.write_text("steps:\n  - say: hello.wav\n")

    assert isinstance(scenario.load(path).steps[0], Say)


def test_a_clip_elsewhere_in_the_project_is_fine():
    """The shipped scenario refers to ../fixtures, which is an ordinary layout.

    Confining strictly to the scenario's own directory rejected this, which is
    why the boundary is the scenario directory *or* the working directory --
    the containment has to permit normal project structure or it will simply be
    turned off.
    """
    loaded = scenario.load("scenarios/barge_in.yaml")
    assert any(isinstance(step, Say) for step in loaded.steps)


def test_an_absolute_path_is_still_honoured(tmp_path):
    """An absolute path is an explicit instruction, not an accident of `../..`."""
    import shutil

    clip = tmp_path / "hello.wav"
    shutil.copy("fixtures/speech_8k.wav", clip)
    path = tmp_path / "s.yaml"
    path.write_text(f"steps:\n  - say: {clip.as_posix()}\n")

    assert isinstance(scenario.load(path).steps[0], Say)


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


def test_inbound_frames_remain_size_limited():
    """The per-frame cap from gate 2 is still in place.

    It does not bound the call -- that is what MAX_AUDIO_BYTES is for -- but it
    is what stops a single frame being unbounded, and the two are easy to
    confuse for each other.
    """
    assert session_module.MAX_INBOUND_FRAME_BYTES is not None
    assert 0 < session_module.MAX_INBOUND_FRAME_BYTES <= 64 * 1024 * 1024


def test_wss_urls_use_verified_tls():
    """`wss://` verifies certificates, because the library's default does.

    Checked rather than assumed: a tool that talks to a remote endpoint and
    silently skipped verification would be a genuine hole, and "we never
    disabled it" is only reassuring if nobody ever passes an ssl argument.
    """
    import inspect

    source = inspect.getsource(session_module)
    assert "ssl=" not in source
    assert "CERT_NONE" not in source
    assert "verify_mode" not in source
