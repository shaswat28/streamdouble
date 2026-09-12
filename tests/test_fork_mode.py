"""`<Start><Stream>` fork mode -- the other half of Media Streams.

A fork is one-way: Twilio streams audio to the app and the app has no channel
back. Two consequences drive everything here, and both are documented facts
rather than inferences:

* ``start.tracks`` can carry an outbound track, and on a fork *streamdouble*
  must supply that audio, because on a real fork Twilio is copying audio it
  generated.
* "Twilio sends the ``mark`` event only during bidirectional Streams", verbatim.
  So a fork has no mark echo and no ``clear``.

The one thing that is *not* documented -- whether an app sending media back on
a fork is committing a violation -- is treated as an inference throughout, and
these tests pin that distinction rather than the verdict.
"""

from __future__ import annotations

import pytest

from streamdouble import api
from streamdouble.protocol import (
    TRACK_INBOUND,
    TRACK_OUTBOUND,
    ProtocolViolation,
    parse_outbound,
)
from streamdouble.session import SessionConfig

ASYNC = pytest.mark.asyncio(loop_scope="module")

FAST = SessionConfig(response_timeout_s=3.0, quiet_period_s=0.3, max_drain_s=2.0)


def fork_config(server_tracks: list[str], agent_frames=None, **kw) -> SessionConfig:
    return SessionConfig(
        response_timeout_s=3.0,
        quiet_period_s=0.3,
        max_drain_s=2.0,
        fork=True,
        tracks=server_tracks,
        agent_frames=list(agent_frames or []),
        **kw,
    )


def fork_url(server: str, suffix: str = "") -> str:
    return server.replace("/media-stream", "/media-stream-fork") + suffix


# ---------------------------------------------------------------------------
# The parse-level decision, with no socket in sight
# ---------------------------------------------------------------------------


def test_a_fork_violation_is_off_unless_asked_for():
    """`protocol.py` stays pure, and the flag is explicit at every call."""
    import json

    frame = json.dumps(
        {"event": "media", "streamSid": "MZ1", "media": {"payload": "//8="}}
    )

    # Bidirectional: perfectly legal.
    assert parse_outbound(frame, expected_stream_sid="MZ1") is not None

    with pytest.raises(ProtocolViolation) as raised:
        parse_outbound(frame, expected_stream_sid="MZ1", unidirectional=True)
    assert raised.value.code == "unidirectional_stream"


def test_the_violation_message_says_it_is_inferred():
    """Anyone acting on this must be able to see it is not a documented rule.

    The same treatment `chaos.py` gives its packet-loss model. A reader who
    believes the Twilio docs say this will go looking for a sentence that does
    not exist.
    """
    import json

    frame = json.dumps({"event": "mark", "streamSid": "MZ1", "mark": {"name": "x"}})
    with pytest.raises(ProtocolViolation) as raised:
        parse_outbound(frame, expected_stream_sid="MZ1", unidirectional=True)

    assert "infers" in str(raised.value)


def test_only_frames_an_app_sends_are_affected():
    """A fork still parses everything else normally."""
    import json

    unknown = json.dumps({"event": "somethingElse", "streamSid": "MZ1"})
    parsed = parse_outbound(unknown, expected_stream_sid="MZ1", unidirectional=True)
    assert parsed.event == "somethingElse"


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


@ASYNC
async def test_a_fork_declares_its_tracks(server, speech_8k_path):
    from streamdouble import audio

    agent = audio.wav_to_ulaw_frames(speech_8k_path)
    report = await api.call(
        fork_url(server),
        audio_path=speech_8k_path,
        config=fork_config([TRACK_INBOUND, TRACK_OUTBOUND], agent),
    )

    assert report.exit_code == api.EXIT_OK
    assert not report.violations


@ASYNC
async def test_a_silent_fork_is_success_not_a_timeout(server, speech_8k_path):
    """The failure found by running it, not by testing it.

    A fork consumer *cannot* send audio back. Treating its silence as a
    response timeout failed every correctly-written fork consumer for doing
    exactly the right thing -- a clean fork call exited 2 with "the agent sent
    no audio before the response timeout".
    """
    report = await api.call(
        fork_url(server), audio_path=speech_8k_path, config=fork_config([TRACK_INBOUND])
    )

    assert not report.spoke, "a fork consumer has no channel to speak on"
    assert not report.result.timed_out, "silence on a fork is not a timeout"
    assert report.exit_code == api.EXIT_OK


@ASYNC
async def test_marks_are_not_echoed_on_a_fork(server, speech_8k_path):
    """Twilio sends marks only on bidirectional streams -- verbatim from the docs.

    Echoing one here would be the simulator inventing a message real Twilio
    never sends, which is the opposite of the job.
    """
    config = fork_config([TRACK_INBOUND])
    assert config.echo_marks is True, "the default is on; the session must override it"

    report = await api.call(
        fork_url(server, "?mark_back=10"), audio_path=speech_8k_path, config=config
    )

    assert report.metrics.marks_echoed == 0


@ASYNC
async def test_the_session_turns_mark_echo_off_and_does_not_ask(server, speech_8k_path):
    """A caller who left `echo_marks` at its default has asked for nothing wrong."""
    from streamdouble.session import Session

    session = Session("ws://unused", [b"\xff" * 160], config=fork_config([TRACK_INBOUND]))
    assert session.config.echo_marks is False


# ---------------------------------------------------------------------------
# Inference versus fact, at the session level
# ---------------------------------------------------------------------------


@ASYNC
async def test_an_app_talking_back_is_a_warning_by_default(server, speech_8k_path):
    """A warning, not a violation, and the build still passes.

    Failing someone's build on this project's reading of a *silence* in the
    documentation is not something to do without being asked. `--strict-fork`
    is the asking.
    """
    report = await api.call(
        fork_url(server, "?talk_back=15"),
        audio_path=speech_8k_path,
        config=fork_config([TRACK_INBOUND]),
    )

    assert report.violations == [], "an inference must not be reported as a violation"
    assert report.result.warnings, "but it must still be reported"
    assert any("one-way" in note for note in report.result.warnings)
    assert report.exit_code == api.EXIT_OK


@ASYNC
async def test_strict_fork_turns_the_warning_into_a_violation(server, speech_8k_path):
    report = await api.call(
        fork_url(server, "?talk_back=15"),
        audio_path=speech_8k_path,
        config=fork_config([TRACK_INBOUND], strict_fork=True),
    )

    assert report.violations, "--strict-fork should make this fail"
    assert report.exit_code == api.EXIT_PROTOCOL_VIOLATION


@ASYNC
async def test_a_bidirectional_call_is_unaffected(server, speech_8k_path):
    """The default path must not have changed at all.

    A fork flag that quietly altered ordinary calls would be the worst possible
    outcome of this phase, since ordinary calls are what everyone runs.
    """
    report = await api.call(server, audio_path=speech_8k_path, config=FAST)

    assert report.spoke
    assert report.metrics.marks_echoed >= 1, "bidirectional calls still echo marks"
    assert report.result.warnings == []
    assert report.exit_code == api.EXIT_OK
