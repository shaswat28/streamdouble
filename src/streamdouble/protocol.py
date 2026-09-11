"""Twilio Media Streams frame construction and parsing.

Pure functions and pure state machines. Nothing in this module performs I/O, and
nothing here knows a WebSocket exists -- it turns audio and counters into JSON
frames, and JSON frames back into typed objects.

All frame shapes below are transcribed from the Twilio documentation at
https://www.twilio.com/docs/voice/media-streams/websocket-messages (verified
2026-09-10). Getting these exactly right is the entire value of this package, so
the field-level gotchas are called out where they occur rather than left to be
rediscovered:

* ``sequenceNumber``, ``chunk`` and ``timestamp`` are JSON **strings**, even
  though every one of them holds a number. ``sampleRate`` and ``channels``, in
  the same payload, are JSON **numbers**. An agent that does
  ``msg["media"]["chunk"] + 1`` against a real Twilio stream gets a TypeError,
  and a simulator that emits them as ints would hide that bug.
* ``dtmf.track`` is the literal string ``"inbound_track"`` -- note the suffix.
  It does *not* match the ``"inbound"``/``"outbound"`` values used by
  ``media.track`` and ``start.tracks``.
* The ``connected`` frame carries neither ``streamSid`` nor ``sequenceNumber``.
* ``media.timestamp`` is "Presentation Timestamp in Milliseconds from the start
  of the stream" -- stream time, not wall-clock time, and not a per-frame delta.
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
from dataclasses import dataclass, field
from typing import Any

from .audio import FRAME_BYTES, FRAME_MS

__all__ = [
    "DTMF_TRACK",
    "MEDIA_FORMAT",
    "PROTOCOL_NAME",
    "PROTOCOL_VERSION",
    "FrameSequenceError",
    "InboundClear",
    "InboundMark",
    "InboundMedia",
    "MediaStreamEncoder",
    "ParsedFrame",
    "ProtocolViolation",
    "StreamIdentity",
    "UnknownFrame",
    "build_connected",
    "build_dtmf",
    "build_mark",
    "build_media",
    "build_start",
    "build_stop",
    "parse_outbound",
]

#: Value of ``protocol`` in the ``connected`` frame.
PROTOCOL_NAME = "Call"

#: Value of ``version`` in the ``connected`` frame.
PROTOCOL_VERSION = "1.0.0"

#: ``start.mediaFormat``. Fixed by the protocol; mu-law 8 kHz mono is the only
#: format Media Streams carries.
MEDIA_FORMAT = {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}

#: ``dtmf.track``. Deliberately not "inbound" -- see the module docstring.
DTMF_TRACK = "inbound_track"

#: How much of an offending frame to retain on a violation, so that error
#: messages stay useful without pulling a multi-megabyte payload into a report.
_RAW_EXCERPT_CHARS = 200


class FrameSequenceError(Exception):
    """streamdouble was asked to build frames in an order Twilio never produces.

    Distinct from :class:`ProtocolViolation`, which describes something the
    *app* did wrong. This one means the simulator itself is about to be
    unfaithful -- media before ``start``, or anything after ``stop`` -- and the
    resulting agent-side failure would be blamed on the agent. The encoder owns
    the counters, so it is the right place to refuse.
    """


class ProtocolViolation(Exception):
    """The peer sent something the Twilio protocol does not permit.

    Carries a machine-readable ``code`` so callers can distinguish violation
    classes without string-matching the message.
    """

    def __init__(self, code: str, message: str, *, raw: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.raw = raw


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def _sid(prefix: str) -> str:
    """Generate a Twilio-shaped SID: a two-letter prefix and 32 hex digits."""
    return prefix + secrets.token_hex(16)


@dataclass(frozen=True)
class StreamIdentity:
    """The SIDs that identify a simulated call.

    Shaped like real Twilio SIDs so that agents which validate or log them see
    something plausible. Randomised per session, because reusing a SID across
    runs masks bugs in agents that key state by ``streamSid``.
    """

    stream_sid: str = field(default_factory=lambda: _sid("MZ"))
    account_sid: str = field(default_factory=lambda: _sid("AC"))
    call_sid: str = field(default_factory=lambda: _sid("CA"))


# --------------------------------------------------------------------------
# Builders (Twilio -> app)
# --------------------------------------------------------------------------


def build_connected() -> dict[str, Any]:
    """The first frame on the socket. No streamSid, no sequenceNumber."""
    return {"event": "connected", "protocol": PROTOCOL_NAME, "version": PROTOCOL_VERSION}


def build_start(
    identity: StreamIdentity,
    sequence_number: int,
    *,
    tracks: list[str] | None = None,
    custom_parameters: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The ``start`` frame, sent once after ``connected``.

    Args:
        identity: SIDs for this simulated call.
        sequence_number: Position in the frame sequence, rendered as a string.
        tracks: Tracks being streamed. ``["inbound"]`` is what a bidirectional
            ``<Connect><Stream>`` sends -- the caller's audio.
        custom_parameters: ``<Parameter>`` values from the TwiML. Agents commonly
            read routing or tenant information out of here, so being able to set
            it is what makes the simulator usable against a real deployment.
    """
    return {
        "event": "start",
        "sequenceNumber": str(sequence_number),
        "start": {
            "accountSid": identity.account_sid,
            "streamSid": identity.stream_sid,
            "callSid": identity.call_sid,
            "tracks": list(tracks) if tracks is not None else ["inbound"],
            "mediaFormat": dict(MEDIA_FORMAT),
            "customParameters": dict(custom_parameters or {}),
        },
        "streamSid": identity.stream_sid,
    }


def build_media(
    identity: StreamIdentity,
    sequence_number: int,
    chunk: int,
    timestamp_ms: int,
    payload: bytes,
    *,
    track: str = "inbound",
) -> dict[str, Any]:
    """A ``media`` frame carrying one 20 ms mu-law payload.

    Args:
        payload: Raw mu-law bytes. Base64 is applied here, exactly once -- the
            caller must pass raw bytes, never an already-encoded string.
    """
    return {
        "event": "media",
        "sequenceNumber": str(sequence_number),
        "media": {
            "track": track,
            "chunk": str(chunk),
            "timestamp": str(timestamp_ms),
            "payload": base64.b64encode(payload).decode("ascii"),
        },
        "streamSid": identity.stream_sid,
    }


def build_mark(identity: StreamIdentity, sequence_number: int, name: str) -> dict[str, Any]:
    """A ``mark`` frame echoed back to the app.

    Twilio returns a mark to the app once the audio queued before it has
    finished playing to the caller. Agents commonly gate turn-taking on this
    echo, so a simulator that swallows marks will hang them.
    """
    return {
        "event": "mark",
        "sequenceNumber": str(sequence_number),
        "streamSid": identity.stream_sid,
        "mark": {"name": name},
    }


def build_dtmf(identity: StreamIdentity, sequence_number: int, digit: str) -> dict[str, Any]:
    """A ``dtmf`` frame -- the caller pressed a key."""
    return {
        "event": "dtmf",
        "streamSid": identity.stream_sid,
        "sequenceNumber": str(sequence_number),
        "dtmf": {"track": DTMF_TRACK, "digit": digit},
    }


def build_stop(identity: StreamIdentity, sequence_number: int) -> dict[str, Any]:
    """The ``stop`` frame, sent once when the stream ends."""
    return {
        "event": "stop",
        "sequenceNumber": str(sequence_number),
        "stop": {"accountSid": identity.account_sid, "callSid": identity.call_sid},
        "streamSid": identity.stream_sid,
    }


# --------------------------------------------------------------------------
# Encoder: owns the counters so callers cannot get them subtly wrong
# --------------------------------------------------------------------------


class MediaStreamEncoder:
    """Stateful frame builder that maintains Twilio's counters.

    ``sequenceNumber`` increments across every frame sent to the app.
    ``chunk`` increments only across ``media`` frames, and ``timestamp`` is
    stream time in milliseconds derived from the chunk index -- 20 ms per frame.

    Deriving the timestamp from the chunk index rather than from a clock is
    deliberate: ``media.timestamp`` is *presentation* time, so it must advance at
    exactly one frame per 20 ms regardless of how the pacer actually behaved.
    Real elapsed time belongs in the metrics layer, not in the protocol.

    Counters live here, not in the session, so that frame numbering is testable
    with no network at all. The encoder also enforces frame *ordering*: nothing
    before ``start``, nothing after ``stop``, and only one of each. Phase 3
    reports an agent that violates the protocol, so the simulator must not be
    capable of violating it first.
    """

    def __init__(self, identity: StreamIdentity | None = None) -> None:
        self.identity = identity or StreamIdentity()
        self._sequence = 0
        self._chunk = 0
        self._stream_time_ms = 0
        self._started = False
        self._stopped = False

    @property
    def started(self) -> bool:
        """Whether ``start`` has been sent.

        Exposed so that cleanup code can tell whether a ``stop`` is even legal
        without catching FrameSequenceError to find out.
        """
        return self._started

    @property
    def stopped(self) -> bool:
        """Whether ``stop`` has been sent."""
        return self._stopped

    @property
    def sequence_number(self) -> int:
        """Sequence number of the most recently built frame."""
        return self._sequence

    @property
    def chunk(self) -> int:
        """Chunk number of the most recently built media frame."""
        return self._chunk

    @property
    def stream_time_ms(self) -> int:
        """Presentation time of the *next* media frame, in milliseconds.

        Tracked separately from the chunk counter rather than derived from it,
        because the two come apart the moment audio is lost upstream. ``chunk``
        counts frames actually sent; presentation time counts elapsed audio,
        including audio that never arrived. See :meth:`skip_frame`.
        """
        return self._stream_time_ms

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _require_started(self, event: str) -> None:
        """Refuse to build a frame outside the stream's lifetime."""
        if self._stopped:
            raise FrameSequenceError(
                f"cannot send '{event}' after 'stop'; the stream has ended"
            )
        if not self._started:
            raise FrameSequenceError(
                f"cannot send '{event}' before 'start'; Twilio always sends "
                "'connected' then 'start' before anything else"
            )

    def connected(self) -> dict[str, Any]:
        """Build ``connected``. Does not consume a sequence number."""
        return build_connected()

    def start(
        self,
        *,
        tracks: list[str] | None = None,
        custom_parameters: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Build ``start``, consuming the next sequence number.

        Raises:
            FrameSequenceError: ``start`` was already sent, or the stream has
                already stopped. Twilio sends exactly one ``start`` per stream.
        """
        if self._stopped:
            raise FrameSequenceError("cannot send 'start' after 'stop'")
        if self._started:
            raise FrameSequenceError("'start' has already been sent for this stream")
        self._started = True
        return build_start(
            self.identity,
            self._next_sequence(),
            tracks=tracks,
            custom_parameters=custom_parameters,
        )

    def media(self, payload: bytes, *, track: str = "inbound") -> dict[str, Any]:
        """Build a ``media`` frame, advancing chunk and stream time.

        Raises:
            ValueError: The payload is not exactly one 20 ms frame. Twilio only
                ever sends 160 bytes; letting a wrong size through here would
                make the simulator unfaithful in the one way that matters most.
        """
        self._require_started("media")
        if len(payload) != FRAME_BYTES:
            raise ValueError(
                f"media payload must be exactly {FRAME_BYTES} bytes "
                f"({FRAME_MS} ms of mu-law), got {len(payload)}"
            )
        timestamp_ms = self._stream_time_ms
        self._chunk += 1
        self._stream_time_ms += FRAME_MS
        return build_media(
            self.identity,
            self._next_sequence(),
            self._chunk,
            timestamp_ms,
            payload,
            track=track,
        )

    def skip_frame(self) -> None:
        """Advance presentation time by one frame without sending anything.

        Models audio lost upstream of Twilio, on the carrier's RTP leg. Twilio
        can only send frames for audio it actually received, so a lost frame is
        one that never goes on the wire at all -- and because the Twilio-to-app
        leg is a TCP WebSocket, that is the *only* way audio can go missing.
        A frame cannot be dropped in transit; TCP would retransmit it.

        What the app sees, therefore, is not a hole in ``sequenceNumber`` -- Twilio
        numbers what it sends -- but a ``media.timestamp`` that jumps by more than
        one frame interval. That discontinuity is the only signal an agent gets,
        which is why this is modelled as a skip rather than as silence
        substitution: silence would be undetectable, and a test tool should
        present the case that is harder to handle.

        Raises:
            FrameSequenceError: Called outside the stream's lifetime.
        """
        self._require_started("media")
        self._stream_time_ms += FRAME_MS

    def mark(self, name: str) -> dict[str, Any]:
        """Build a ``mark`` echo, consuming the next sequence number."""
        self._require_started("mark")
        return build_mark(self.identity, self._next_sequence(), name)

    def dtmf(self, digit: str) -> dict[str, Any]:
        """Build a ``dtmf`` frame, consuming the next sequence number."""
        self._require_started("dtmf")
        return build_dtmf(self.identity, self._next_sequence(), digit)

    def stop(self) -> dict[str, Any]:
        """Build ``stop``, consuming the next sequence number."""
        self._require_started("stop")
        self._stopped = True
        return build_stop(self.identity, self._next_sequence())


# --------------------------------------------------------------------------
# Parsing (app -> Twilio)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InboundMedia:
    """Audio the app wants played to the caller."""

    stream_sid: str
    payload: bytes
    #: The frame exactly as it arrived, after JSON decoding.
    #:
    #: Carried so that a caller wanting the whole frame -- the trace does --
    #: does not have to parse the message a second time. Gate 6 found exactly
    #: that duplication costing a JSON decode per inbound frame inside the
    #: receive loop, and real agents batch audio into ~8000-byte frames.
    #:
    #: Defaults to an empty dict so that existing constructions and the many
    #: tests building these directly keep working unchanged, and is excluded
    #: from equality and repr: two media frames carrying the same payload for
    #: the same stream are the same frame, whatever dict they came out of.
    #: Without `compare=False` every existing test that asserts on a whole
    #: parsed frame breaks -- which is how this was found.
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class InboundMark:
    """A playback checkpoint the app expects echoed back once audio drains."""

    stream_sid: str
    name: str
    #: The frame as it arrived. See :attr:`InboundMedia.raw`.
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class InboundClear:
    """A request to discard buffered audio -- the caller interrupted."""

    stream_sid: str
    #: The frame as it arrived. See :attr:`InboundMedia.raw`.
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class UnknownFrame:
    """A well-formed JSON frame carrying an event Twilio does not define.

    Surfaced rather than raised: Twilio ignores unknown events from the app, so
    treating one as fatal would be *less* faithful than tolerating it. Callers
    can log or count these.
    """

    event: str
    raw: dict[str, Any]


ParsedFrame = InboundMedia | InboundMark | InboundClear | UnknownFrame


def parse_outbound(
    message: str | bytes, *, expected_stream_sid: str | None = None
) -> ParsedFrame:
    """Parse one frame sent by the app.

    Args:
        message: The raw WebSocket frame.
        expected_stream_sid: If given, frames carrying a different streamSid are
            rejected. Twilio would silently ignore them; surfacing it is more
            useful in a test tool, since it is nearly always an agent bug.

    Raises:
        ProtocolViolation: The frame is not usable as a Twilio outbound frame.
    """
    if isinstance(message, bytes):
        # Media Streams is a text protocol. A binary frame is a real bug --
        # typically an agent writing raw mu-law straight to the socket.
        raise ProtocolViolation(
            "binary_frame",
            f"received a binary WebSocket frame of {len(message)} bytes; "
            "Media Streams frames must be JSON text",
        )

    excerpt = message[:_RAW_EXCERPT_CHARS]

    try:
        data = json.loads(message)
    except json.JSONDecodeError as exc:
        raise ProtocolViolation(
            "malformed_json", f"frame is not valid JSON: {exc}", raw=excerpt
        ) from exc

    if not isinstance(data, dict):
        raise ProtocolViolation(
            "not_an_object",
            f"frame is a JSON {type(data).__name__}, expected an object",
            raw=excerpt,
        )

    event = data.get("event")
    if not isinstance(event, str):
        raise ProtocolViolation(
            "missing_event", "frame has no string 'event' field", raw=excerpt
        )

    stream_sid = data.get("streamSid")
    if event in {"media", "mark", "clear"}:
        if not isinstance(stream_sid, str) or not stream_sid:
            raise ProtocolViolation(
                "missing_stream_sid",
                f"'{event}' frame is missing a streamSid; Twilio would discard it",
                raw=excerpt,
            )
        if expected_stream_sid is not None and stream_sid != expected_stream_sid:
            raise ProtocolViolation(
                "wrong_stream_sid",
                f"'{event}' frame carries streamSid {stream_sid!r}, "
                f"expected {expected_stream_sid!r}",
                raw=excerpt,
            )

    if event == "media":
        return InboundMedia(
            stream_sid=stream_sid, payload=_decode_payload(data, excerpt), raw=data
        )
    if event == "mark":
        mark = data.get("mark")
        name = mark.get("name") if isinstance(mark, dict) else None
        if not isinstance(name, str):
            raise ProtocolViolation(
                "malformed_mark", "'mark' frame is missing mark.name", raw=excerpt
            )
        return InboundMark(stream_sid=stream_sid, name=name, raw=data)
    if event == "clear":
        return InboundClear(stream_sid=stream_sid, raw=data)

    return UnknownFrame(event=event, raw=data)


def _decode_payload(data: dict[str, Any], excerpt: str) -> bytes:
    """Extract and base64-decode ``media.payload`` from an outbound media frame."""
    media = data.get("media")
    if not isinstance(media, dict):
        raise ProtocolViolation(
            "malformed_media", "'media' frame has no media object", raw=excerpt
        )
    payload = media.get("payload")
    if not isinstance(payload, str):
        raise ProtocolViolation(
            "malformed_media", "'media' frame has no string media.payload", raw=excerpt
        )
    try:
        # validate=True rejects characters outside the base64 alphabet instead of
        # silently discarding them, which is how double-base64-encoded audio
        # slips through as plausible-looking garbage.
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProtocolViolation(
            "bad_base64",
            f"media.payload is not valid base64: {exc}",
            raw=payload[:_RAW_EXCERPT_CHARS],
        ) from exc
