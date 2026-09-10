"""Tests for Twilio Media Streams frame construction and parsing.

These assert the frame shapes against the documented examples field-for-field,
including the JSON types. That precision is the point of the package: an agent
tested against a simulator whose ``sequenceNumber`` is an int will break the
first time it meets the real thing.

Documentation reference:
https://www.twilio.com/docs/voice/media-streams/websocket-messages
"""

from __future__ import annotations

import base64
import binascii
import json

import pytest

from streamdouble import audio, protocol
from streamdouble.protocol import (
    InboundClear,
    InboundMark,
    InboundMedia,
    MediaStreamEncoder,
    ProtocolViolation,
    StreamIdentity,
    UnknownFrame,
    parse_outbound,
)


@pytest.fixture
def identity() -> StreamIdentity:
    """Fixed SIDs, so assertions can name exact values."""
    return StreamIdentity(
        stream_sid="MZ00000000000000000000000000000001",
        account_sid="AC00000000000000000000000000000002",
        call_sid="CA00000000000000000000000000000003",
    )


@pytest.fixture
def encoder(identity: StreamIdentity) -> MediaStreamEncoder:
    return MediaStreamEncoder(identity)


def frame(payload_byte: int = 0xFF) -> bytes:
    """One 160-byte mu-law frame of a single repeated byte."""
    return bytes([payload_byte]) * audio.FRAME_BYTES


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_generated_sids_look_like_twilio_sids():
    """SIDs are a two-letter prefix and 32 hex digits.

    Agents do validate and log these. A SID of the wrong shape is the kind of
    thing that works locally and fails against production.
    """
    generated = StreamIdentity()
    for sid, prefix in (
        (generated.stream_sid, "MZ"),
        (generated.account_sid, "AC"),
        (generated.call_sid, "CA"),
    ):
        assert sid.startswith(prefix)
        assert len(sid) == 34
        assert all(character in "0123456789abcdef" for character in sid[2:])


def test_each_identity_is_distinct():
    """SIDs are randomised per session.

    Reusing a SID across runs hides bugs in agents that key state by streamSid --
    the second run would silently reuse the first run's state.
    """
    assert StreamIdentity().stream_sid != StreamIdentity().stream_sid


# --------------------------------------------------------------------------
# Frame shapes
# --------------------------------------------------------------------------


def test_connected_frame_matches_the_documented_shape(encoder):
    """``connected`` carries exactly three fields, and no streamSid."""
    assert encoder.connected() == {
        "event": "connected",
        "protocol": "Call",
        "version": "1.0.0",
    }


def test_connected_does_not_consume_a_sequence_number(encoder):
    """The documented ``start`` frame is sequenceNumber "1", so connected has none."""
    encoder.connected()
    assert encoder.start()["sequenceNumber"] == "1"


def test_start_frame_matches_the_documented_shape(encoder, identity):
    start = encoder.start(custom_parameters={"FirstName": "Jane"})

    assert start == {
        "event": "start",
        "sequenceNumber": "1",
        "start": {
            "accountSid": identity.account_sid,
            "streamSid": identity.stream_sid,
            "callSid": identity.call_sid,
            "tracks": ["inbound"],
            "mediaFormat": {
                "encoding": "audio/x-mulaw",
                "sampleRate": 8000,
                "channels": 1,
            },
            "customParameters": {"FirstName": "Jane"},
        },
        "streamSid": identity.stream_sid,
    }


def test_start_carries_custom_parameters_verbatim(encoder):
    """``customParameters`` reaches the agent unchanged.

    Agents routinely route on TwiML ``<Parameter>`` values, so this is what
    makes the simulator usable against a real deployment rather than only
    against a toy.
    """
    params = {"tenant": "acme", "locale": "en-GB"}
    assert encoder.start(custom_parameters=params)["start"]["customParameters"] == params


def test_start_does_not_alias_its_arguments(encoder):
    """Mutating the caller's dict afterwards does not change the built frame."""
    params = {"a": "1"}
    tracks = ["inbound"]
    start = encoder.start(tracks=tracks, custom_parameters=params)

    params["a"] = "mutated"
    tracks.append("outbound")

    assert start["start"]["customParameters"] == {"a": "1"}
    assert start["start"]["tracks"] == ["inbound"]


def test_media_frame_matches_the_documented_shape(encoder, identity):
    encoder.start()
    media = encoder.media(frame(0x01))

    assert media["event"] == "media"
    assert media["streamSid"] == identity.stream_sid
    assert set(media["media"]) == {"track", "chunk", "timestamp", "payload"}
    assert media["media"]["track"] == "inbound"


def test_stop_frame_matches_the_documented_shape(encoder, identity):
    encoder.start()
    assert encoder.stop() == {
        "event": "stop",
        "sequenceNumber": "2",
        "stop": {"accountSid": identity.account_sid, "callSid": identity.call_sid},
        "streamSid": identity.stream_sid,
    }


def test_mark_frame_matches_the_documented_shape(encoder, identity):
    encoder.start()
    assert encoder.mark("my label") == {
        "event": "mark",
        "sequenceNumber": "2",
        "streamSid": identity.stream_sid,
        "mark": {"name": "my label"},
    }


def test_dtmf_track_is_inbound_track_not_inbound(encoder, identity):
    """``dtmf.track`` is "inbound_track", which nothing else in the protocol uses.

    Every other track field is "inbound"/"outbound". An agent that matches on
    "inbound" here silently never sees a keypress, and against a simulator that
    emitted the wrong value it would appear to work.
    """
    encoder.start()
    dtmf = encoder.dtmf("7")

    assert dtmf["dtmf"] == {"track": "inbound_track", "digit": "7"}
    assert dtmf["dtmf"]["track"] != "inbound"
    assert dtmf["streamSid"] == identity.stream_sid


# --------------------------------------------------------------------------
# JSON types
# --------------------------------------------------------------------------


def test_counter_fields_serialise_as_json_strings(encoder):
    """sequenceNumber, chunk and timestamp are strings; sampleRate is a number.

    This mixture is genuinely what Twilio sends. Emitting the counters as ints
    would be tidier and would hide a real class of agent bug -- arithmetic on a
    field that arrives as a string.
    """
    start = json.loads(json.dumps(encoder.start()))
    assert isinstance(start["sequenceNumber"], str)
    assert isinstance(start["start"]["mediaFormat"]["sampleRate"], int)
    assert isinstance(start["start"]["mediaFormat"]["channels"], int)

    media = json.loads(json.dumps(encoder.media(frame())))
    assert isinstance(media["sequenceNumber"], str)
    assert isinstance(media["media"]["chunk"], str)
    assert isinstance(media["media"]["timestamp"], str)
    assert isinstance(media["media"]["payload"], str)


def test_every_frame_is_json_serialisable(encoder):
    """Nothing leaks a non-serialisable value such as bytes."""
    encoder.start()
    for built in (
        encoder.connected(),
        encoder.media(frame()),
        encoder.mark("m"),
        encoder.dtmf("1"),
        encoder.stop(),
    ):
        json.loads(json.dumps(built))


# --------------------------------------------------------------------------
# Base64 layering
# --------------------------------------------------------------------------


def test_payload_is_base64_encoded_exactly_once(encoder):
    """The payload decodes in one step back to the original bytes.

    Double-encoding is the signature bug of this layer: it still looks like
    valid base64, still decodes without error, and produces audio that sounds
    like static. Decoding twice must fail.
    """
    encoder.start()
    raw = bytes(range(160))
    payload = encoder.media(raw)["media"]["payload"]

    assert base64.b64decode(payload, validate=True) == raw
    with pytest.raises(binascii.Error):
        base64.b64decode(base64.b64decode(payload, validate=True), validate=True)


def test_payload_has_no_wav_header(encoder, speech_8k_path):
    """The payload is raw mu-law, with no container.

    Twilio documents the payload as "encoded audio... without headers". Sending
    a RIFF header inline would put 44 bytes of ASCII through the agent's
    decoder as if it were audio.
    """
    encoder.start()
    frames = audio.wav_to_ulaw_frames(speech_8k_path)
    decoded = base64.b64decode(encoder.media(frames[0])["media"]["payload"], validate=True)

    assert not decoded.startswith(b"RIFF")
    assert len(decoded) == audio.FRAME_BYTES


def test_payload_decodes_to_exactly_160_bytes(encoder):
    encoder.start()
    payload = encoder.media(frame())["media"]["payload"]
    assert len(base64.b64decode(payload, validate=True)) == 160


# --------------------------------------------------------------------------
# Counters
# --------------------------------------------------------------------------


def test_sequence_number_increments_across_all_frame_types(encoder):
    """One monotonic counter spans every frame sent to the app.

    ``connected`` is the only exception; it has no sequenceNumber at all.
    """
    encoder.connected()
    observed = [
        encoder.start()["sequenceNumber"],
        encoder.media(frame())["sequenceNumber"],
        encoder.media(frame())["sequenceNumber"],
        encoder.mark("m")["sequenceNumber"],
        encoder.dtmf("1")["sequenceNumber"],
        encoder.stop()["sequenceNumber"],
    ]
    assert observed == ["1", "2", "3", "4", "5", "6"]


def test_chunk_counts_only_media_frames(encoder):
    """``chunk`` is a per-track media counter, independent of sequenceNumber."""
    encoder.start()
    assert encoder.media(frame())["media"]["chunk"] == "1"
    encoder.mark("m")
    encoder.dtmf("1")
    assert encoder.media(frame())["media"]["chunk"] == "2"


def test_chunk_starts_at_one_not_zero(encoder):
    encoder.start()
    assert encoder.media(frame())["media"]["chunk"] == "1"


def test_timestamp_advances_20ms_per_frame_from_zero(encoder):
    """Presentation time is stream time: 0, 20, 40, ... milliseconds.

    Derived from the chunk index rather than a clock, so it stays exact however
    the pacer behaved. An off-by-one here shifts every latency measurement in
    Phase 3 by a full frame.
    """
    encoder.start()
    timestamps = [encoder.media(frame())["media"]["timestamp"] for _ in range(5)]
    assert timestamps == ["0", "20", "40", "60", "80"]


def test_timestamp_stays_exact_over_a_long_stream(encoder):
    """Ten thousand frames in, stream time is still exactly 200 seconds.

    Accumulating a float here would drift; integer arithmetic does not.
    """
    encoder.start()
    for _ in range(10_000):
        last = encoder.media(frame())
    assert last["media"]["timestamp"] == str((10_000 - 1) * 20)
    assert last["media"]["chunk"] == "10000"


def test_media_rejects_a_payload_that_is_not_one_frame(encoder):
    """Only 160 bytes is accepted. Twilio never sends anything else."""
    encoder.start()
    for size in (0, 159, 161, 320):
        with pytest.raises(ValueError, match="exactly 160 bytes"):
            encoder.media(b"\xff" * size)


# --------------------------------------------------------------------------
# Parsing valid frames
# --------------------------------------------------------------------------


def test_parse_media(identity):
    payload = bytes(range(160))
    message = json.dumps(
        {
            "event": "media",
            "streamSid": identity.stream_sid,
            "media": {"payload": base64.b64encode(payload).decode()},
        }
    )
    parsed = parse_outbound(message, expected_stream_sid=identity.stream_sid)
    assert parsed == InboundMedia(stream_sid=identity.stream_sid, payload=payload)


def test_parse_mark(identity):
    message = json.dumps(
        {"event": "mark", "streamSid": identity.stream_sid, "mark": {"name": "greeting-done"}}
    )
    parsed = parse_outbound(message, expected_stream_sid=identity.stream_sid)
    assert parsed == InboundMark(stream_sid=identity.stream_sid, name="greeting-done")


def test_parse_clear(identity):
    message = json.dumps({"event": "clear", "streamSid": identity.stream_sid})
    parsed = parse_outbound(message, expected_stream_sid=identity.stream_sid)
    assert parsed == InboundClear(stream_sid=identity.stream_sid)


def test_unknown_event_is_surfaced_not_raised():
    """An undocumented event is reported, not treated as fatal.

    Twilio ignores unknown events from the app, so raising would be less
    faithful than tolerating. Returning it rather than dropping it silently
    still lets the session log or count it.
    """
    message = json.dumps({"event": "somethingNew", "streamSid": "MZ1", "extra": 1})
    parsed = parse_outbound(message)
    assert isinstance(parsed, UnknownFrame)
    assert parsed.event == "somethingNew"
    assert parsed.raw["extra"] == 1


def test_parse_accepts_a_media_payload_of_any_length(identity):
    """Agents legitimately send audio in chunks other than 160 bytes.

    Unlike the inbound direction, Twilio does not constrain the app's payload
    size -- it buffers and repaces. Rejecting a non-160-byte payload here would
    fail correct agents.
    """
    for size in (1, 160, 1000):
        message = json.dumps(
            {
                "event": "media",
                "streamSid": identity.stream_sid,
                "media": {"payload": base64.b64encode(b"\xff" * size).decode()},
            }
        )
        parsed = parse_outbound(message, expected_stream_sid=identity.stream_sid)
        assert len(parsed.payload) == size


# --------------------------------------------------------------------------
# Protocol violations
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "code"),
    [
        (b"\x00\x01\x02", "binary_frame"),
        ("not json at all", "malformed_json"),
        ("[1, 2, 3]", "not_an_object"),
        ('{"noEvent": true}', "missing_event"),
        ('{"event": 42}', "missing_event"),
        ('{"event": "media", "media": {"payload": "//8="}}', "missing_stream_sid"),
        ('{"event": "clear"}', "missing_stream_sid"),
        ('{"event": "mark", "streamSid": "MZ1"}', "malformed_mark"),
        ('{"event": "mark", "streamSid": "MZ1", "mark": {}}', "malformed_mark"),
        ('{"event": "media", "streamSid": "MZ1"}', "malformed_media"),
        ('{"event": "media", "streamSid": "MZ1", "media": {}}', "malformed_media"),
        ('{"event": "media", "streamSid": "MZ1", "media": {"payload": "!!!!"}}', "bad_base64"),
    ],
)
def test_violations_carry_a_machine_readable_code(message, code):
    """Each violation class has a stable code.

    The CLI needs to distinguish these for its exit codes without matching on
    English prose.
    """
    with pytest.raises(ProtocolViolation) as excinfo:
        parse_outbound(message)
    assert excinfo.value.code == code


def test_wrong_stream_sid_is_a_violation(identity):
    """A frame for a different stream is reported rather than ignored.

    Twilio would discard it silently. In a test tool that is nearly always an
    agent bug -- typically stale state from a previous call -- so surfacing it
    is more useful than fidelity here.
    """
    message = json.dumps({"event": "clear", "streamSid": "MZdifferent"})
    with pytest.raises(ProtocolViolation) as excinfo:
        parse_outbound(message, expected_stream_sid=identity.stream_sid)
    assert excinfo.value.code == "wrong_stream_sid"


def test_stream_sid_is_not_checked_when_not_supplied():
    """Without an expected SID, any non-empty SID is accepted."""
    message = json.dumps({"event": "clear", "streamSid": "MZanything"})
    assert parse_outbound(message) == InboundClear(stream_sid="MZanything")


def test_double_base64_encoded_payload_is_rejected_or_wrong_length(identity):
    """Doubly-encoded audio does not pass as valid 160-byte audio.

    Base64 of base64 is still valid base64, so it cannot always be caught by
    decoding alone -- but it inflates by 4/3, so it can never be mistaken for a
    correctly sized frame. This documents the detection that Phase 3's frame
    size checking relies on.
    """
    once = base64.b64encode(b"\xff" * 160)
    twice = base64.b64encode(once).decode()
    message = json.dumps(
        {"event": "media", "streamSid": identity.stream_sid, "media": {"payload": twice}}
    )
    parsed = parse_outbound(message, expected_stream_sid=identity.stream_sid)
    assert parsed.payload != b"\xff" * 160
    assert len(parsed.payload) == len(once)


def test_violation_retains_a_bounded_excerpt_of_the_frame():
    """Error reporting keeps enough context to debug, but not a whole payload."""
    huge = json.dumps(
        {"event": "media", "streamSid": "MZ1", "media": {"payload": "!" * 100_000}}
    )
    with pytest.raises(ProtocolViolation) as excinfo:
        parse_outbound(huge)
    assert excinfo.value.raw is not None
    assert len(excinfo.value.raw) <= 200


def test_violation_message_is_readable():
    """Messages name the problem in terms an agent author can act on."""
    with pytest.raises(ProtocolViolation) as excinfo:
        parse_outbound('{"event": "clear"}')
    assert "streamSid" in str(excinfo.value)


# --------------------------------------------------------------------------
# Module-level constants
# --------------------------------------------------------------------------


def test_media_format_constant_is_not_shared_between_frames(encoder):
    """Each frame gets its own mediaFormat dict.

    A shared reference would let a caller mutating one frame's format silently
    change every other frame's.
    """
    first = encoder.start()["start"]["mediaFormat"]
    second = MediaStreamEncoder().start()["start"]["mediaFormat"]

    assert first == second
    assert first is not second
    assert first is not protocol.MEDIA_FORMAT
