"""The public Python API.

These tests exist for two reasons beyond covering `api.py`'s own behaviour.

The first is that this API is now the *only* implementation. `cli.py` renders
what it returns and adds nothing, so a bug here is a bug in both front ends,
and `test_review_gate_6.py` proves that coupling by mutation.

The second is that `streamdouble scenario` parsed correctly and then exited
"unknown command" for its entire life in a public repository because every
scenario test drove the Python layer while the command line had a path of its
own. The fix was one implementation and two front ends; these tests hold the
Python half of that to the same standard `test_cli.py` holds the other.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from streamdouble import api
from streamdouble.metrics import Threshold
from streamdouble.session import SessionConfig

FAST = SessionConfig(response_timeout_s=3.0, quiet_period_s=0.3, max_drain_s=3.0)

#: Share one event loop across this module rather than building a fresh one per
#: test.
#:
#: Not a performance tweak. On Windows, asyncio's ProactorEventLoop holds a
#: self-pipe socketpair, and tearing down a loop per test leaks one often
#: enough that `filterwarnings = ["error"]` turns it into a failure -- reported
#: against whichever *later* test happened to trigger the collection, which is
#: how a leak here first showed up as an error in `test_cli.py`. This file
#: opens more real sockets in more loops than any other, so it is where the
#: effect appears first. The shipped code is not implicated: `Session.run`
#: closes its connection in a `finally`, and the full suite is clean without
#: this module.
pytestmark = pytest.mark.asyncio(loop_scope="module")


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_call_returns_a_report_of_a_real_call(server, speech_8k_path):
    report = await api.call(server, audio_path=speech_8k_path, config=FAST)

    assert report.spoke
    assert report.exit_code == api.EXIT_OK
    assert report.passed
    assert report.violations == []
    assert report.stream_sid.startswith("MZ")
    assert report.time_to_first_audio_ms is not None
    assert report.time_to_first_audio_ms > 0


async def test_call_accepts_pre_encoded_frames(server, speech_8k_path):
    """The alternative input, for callers who already hold mu-law frames."""
    from streamdouble import audio

    frames = audio.wav_to_ulaw_frames(speech_8k_path)
    report = await api.call(server, frames=frames, config=FAST)

    assert report.spoke
    assert report.metrics.frames_sent == len(frames)


async def test_run_scenario_accepts_a_path_and_a_loaded_scenario(server, tmp_path, speech_8k_path):
    text = (
        "name: api test\n"
        "steps:\n"
        f"  - say: {speech_8k_path.as_posix()}\n"
        "  - wait: 0.2\n"
    )
    path = tmp_path / "s.yaml"
    path.write_text(text, encoding="utf-8")

    from_path = await api.run_scenario(server, path, config=FAST)
    assert from_path.spoke

    from streamdouble.scenario import load

    from_object = await api.run_scenario(server, load(path), config=FAST)
    assert from_object.spoke


# ---------------------------------------------------------------------------
# Measurement honesty, carried across the API boundary
# ---------------------------------------------------------------------------


async def test_a_silent_agent_reports_none_not_zero(server, speech_8k_path):
    """The rule the whole project turns on, asserted at the new surface.

    An agent that never spoke has no time-to-first-audio. Reporting 0 would
    turn the worst outcome into the best one, and it is the API -- not the
    renderer -- that must refuse to do it, because a test harness reads this
    value directly and never sees the CLI's formatting.
    """
    report = await api.call(f"{server}?mode=silent", audio_path=speech_8k_path, config=FAST)

    assert not report.spoke
    assert report.time_to_first_audio_ms is None
    assert report.to_dict()["time_to_first_audio_ms"] is None


async def test_a_silent_agent_fails_a_latency_threshold(server, speech_8k_path):
    """Missing data fails its gate; it does not pass by absence."""
    report = await api.call(
        f"{server}?mode=silent",
        audio_path=speech_8k_path,
        config=FAST,
        thresholds=[Threshold(name="first audio", limit=800.0, attribute="time_to_first_audio_ms")],
    )

    assert report.thresholds
    assert not report.thresholds[0].passed
    assert report.thresholds[0].value is None
    # The timeout outranks the failed threshold: a more specific outcome wins.
    assert report.exit_code == api.EXIT_TIMEOUT


async def test_a_report_does_not_raise_on_a_failed_threshold(server, speech_8k_path):
    """A failed gate is a fact on the report, not an exception.

    The caller decides what a slow agent is worth. Raising would make the
    common case -- measure it, then decide -- need a try/except.
    """
    report = await api.call(
        server,
        audio_path=speech_8k_path,
        config=FAST,
        thresholds=[Threshold(name="first audio", limit=0.001, attribute="time_to_first_audio_ms")],
    )

    assert report.spoke
    assert not report.passed
    assert report.exit_code == api.EXIT_ASSERTION_FAILED


# ---------------------------------------------------------------------------
# Exit-code parity with the CLI
# ---------------------------------------------------------------------------


async def test_a_misbehaving_agent_is_reported_as_a_violation(server, speech_8k_path):
    report = await api.call(f"{server}?garbage_after=5", audio_path=speech_8k_path, config=FAST)

    assert report.violations
    assert report.exit_code == api.EXIT_PROTOCOL_VIOLATION


async def test_the_json_payload_is_the_documented_shape(server, speech_8k_path):
    """`to_dict` is the definition of `--json`, so its keys are a contract."""
    report = await api.call(server, audio_path=speech_8k_path, config=FAST)
    payload = report.to_dict()

    for key in (
        "time_to_first_audio_ms",
        "frames_sent",
        "media_frames_received",
        "violations",
        "pacing",
        "stream_sid",
        "frames_dropped",
        "expectations",
        "thresholds",
        "exit_code",
    ):
        assert key in payload, f"{key} missing from the JSON payload"

    assert payload["exit_code"] == report.exit_code


# ---------------------------------------------------------------------------
# Failure modes of the API itself
# ---------------------------------------------------------------------------


async def test_an_unreachable_agent_raises_connection_failed(speech_8k_path):
    from conftest import free_port

    port = free_port()
    with pytest.raises(api.ConnectionFailed) as failure:
        await api.call(f"ws://127.0.0.1:{port}/nope", audio_path=speech_8k_path, config=FAST)

    assert "could not connect" in str(failure.value)


async def test_call_needs_some_audio(server):
    with pytest.raises(ValueError, match="audio_path or frames"):
        await api.call(server, config=FAST)


async def test_call_refuses_both_kinds_of_audio(server, speech_8k_path):
    with pytest.raises(ValueError, match="not both"):
        await api.call(server, audio_path=speech_8k_path, frames=[b"\xff" * 160], config=FAST)


async def test_the_sync_wrappers_refuse_to_run_inside_a_loop(server, speech_8k_path):
    """A sentence naming the fix, rather than a failure three frames down.

    `asyncio.run` inside a running loop raises from deep in asyncio, which
    reads as a bug in this library rather than as the wrong function. Under
    `pytest-asyncio` in auto mode -- how this suite is configured, and how most
    users' suites are -- every test is already inside a loop, so this is the
    likely mistake rather than an exotic one.
    """
    with pytest.raises(RuntimeError, match=r"await call\(\) instead"):
        api.call_sync(server, audio_path=speech_8k_path, config=FAST)

    with pytest.raises(RuntimeError, match=r"await run_scenario\(\) instead"):
        api.run_scenario_sync(server, "irrelevant.yaml", config=FAST)


async def test_the_sync_wrapper_works_outside_a_loop(server, speech_8k_path, tmp_path: Path):
    """The supported use of `call_sync`: a plain script, no loop anywhere.

    Run in a subprocess rather than in this process, and not because it is
    tidier. This suite is `asyncio_mode = "auto"`, so pytest-asyncio owns a
    loop for the session; calling `asyncio.run` from a *sync* test inside that
    arrangement leaves its bookkeeping unhappy and emits a ResourceWarning
    about an unclosed loop that belongs to the harness, not to this package.
    With `filterwarnings = ["error"]` that fails the test for a reason that
    has nothing to do with the code under test -- which is the shape of
    problem this project's rules exist to avoid, not to create.

    Verified separately that `call_sync` is clean on its own: the subprocess
    below runs under `-W error::ResourceWarning`, so a leak in *our* code
    still fails this test. That is a stronger check than the in-process
    version was, because it exercises the arrangement a user actually has.

    Declared `async` only so the module-wide asyncio marker applies cleanly;
    everything it does is synchronous, and the loop it runs in is irrelevant
    because the code under test runs in a separate process with no loop at all.
    """
    script = tmp_path / "sync_use.py"
    script.write_text(
        "import sys\n"
        "from streamdouble import api\n"
        "from streamdouble.session import SessionConfig\n"
        "report = api.call_sync(\n"
        f"    {server!r},\n"
        f"    audio_path={str(speech_8k_path)!r},\n"
        "    config=SessionConfig(response_timeout_s=3.0, quiet_period_s=0.3, max_drain_s=3.0),\n"
        ")\n"
        "assert report.spoke, 'the agent said nothing'\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, "-W", "error::ResourceWarning", str(script)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, (
        f"call_sync failed in a plain script:\n{completed.stdout}\n{completed.stderr}"
    )


# ---------------------------------------------------------------------------
# save_reply
# ---------------------------------------------------------------------------


async def test_save_reply_writes_a_wav(server, speech_8k_path, tmp_path: Path):
    report = await api.call(server, audio_path=speech_8k_path, config=FAST)
    out = tmp_path / "reply.wav"

    assert api.save_reply(report, out) is True
    assert out.exists()
    assert out.stat().st_size > 44  # bigger than a bare WAV header


async def test_save_reply_writes_nothing_when_the_agent_was_silent(
    server, speech_8k_path, tmp_path: Path
):
    """No file beats a zero-length one that looks like recorded silence."""
    report = await api.call(f"{server}?mode=silent", audio_path=speech_8k_path, config=FAST)
    out = tmp_path / "reply.wav"

    assert api.save_reply(report, out) is False
    assert not out.exists()
