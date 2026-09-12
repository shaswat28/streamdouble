"""WAV <-> mu-law conversion and 20 ms frame chunking.

Twilio Media Streams carries ``audio/x-mulaw`` at 8000 Hz, mono. One mu-law byte
is one sample, so a 20 ms frame is exactly 160 bytes. Twilio does not document a
mandatory frame size, but 20 ms is what it sends and what every implementation
expects; sending a different size is one of the ways a simulator stops being a
faithful one.

Pure functions and file I/O only -- nothing here touches the network. If a bug
in this module needs a WebSocket to reproduce, the layering is wrong.
"""

from __future__ import annotations

import wave
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from . import g711

__all__ = [
    "FRAME_BYTES",
    "FRAME_MS",
    "SAMPLES_PER_FRAME",
    "SAMPLE_RATE",
    "AudioError",
    "frame_ulaw",
    "load_wav",
    "resample",
    "silence_frame",
    "to_mono",
    "ulaw_to_wav",
    "wav_to_ulaw_frames",
]

#: Twilio Media Streams sample rate, in Hz. Not configurable at the protocol level.
SAMPLE_RATE = 8000

#: Duration of one media frame, in milliseconds.
FRAME_MS = 20

#: Samples in one 20 ms frame at 8 kHz.
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000  # 160

#: Bytes in one 20 ms mu-law frame. One byte per sample.
FRAME_BYTES = SAMPLES_PER_FRAME  # 160


class AudioError(Exception):
    """Raised for unusable audio input: bad WAV, unsupported encoding, etc."""


def load_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """Read a PCM WAV file into int16 samples.

    Args:
        path: Path to a PCM (uncompressed) WAV file.

    Returns:
        ``(samples, sample_rate)`` where ``samples`` is 2-D with shape
        ``(n_frames, n_channels)`` and dtype int16.

    Raises:
        AudioError: The file is not readable as PCM WAV, or uses a sample width
            this module does not handle.
    """
    path = Path(path)
    try:
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            n_frames = wav.getnframes()
            raw = wav.readframes(n_frames)
    except wave.Error as exc:
        raise AudioError(f"{path}: not a readable PCM WAV file ({exc})") from exc
    except FileNotFoundError as exc:
        raise AudioError(f"{path}: no such file") from exc

    if channels < 1:
        raise AudioError(f"{path}: reports {channels} channels")
    if rate < 1:
        raise AudioError(f"{path}: reports a sample rate of {rate} Hz")

    # Check we actually got the audio the header promised, before handing the
    # bytes to numpy. A truncated file otherwise fails in one of two bad ways:
    # an odd byte count raises a bare ValueError out of np.frombuffer, breaking
    # this module's promise that bad input arrives as AudioError; and an even
    # truncation is worse still, decoding silently to less audio than the caller
    # asked for, which looks downstream like the agent stopped talking.
    # Interrupted downloads and killed recordings both produce exactly this.
    expected_bytes = n_frames * channels * width
    if len(raw) != expected_bytes:
        raise AudioError(
            f"{path}: truncated. Header declares {n_frames} frames "
            f"({expected_bytes} bytes) but the data chunk holds {len(raw)}"
        )

    samples = _decode_pcm(raw, width, path)

    if samples.size % channels:
        raise AudioError(
            f"{path}: sample count {samples.size} is not divisible by {channels} channels"
        )
    return samples.reshape(-1, channels), rate


def _decode_pcm(raw: bytes, width: int, path: Path) -> np.ndarray:
    """Convert raw WAV frame bytes of the given sample width to int16."""
    if width == 1:
        # 8-bit WAV is unsigned, offset by 128. Everything else is signed.
        u8 = np.frombuffer(raw, dtype=np.uint8).astype(np.int16)
        return ((u8 - 128) << 8).astype(np.int16)
    if width == 2:
        return np.frombuffer(raw, dtype="<i2").copy()
    if width == 3:
        # 24-bit little-endian packed. Widen to int32 by placing the three bytes
        # in the *high* 24 bits, which sign-extends for free, then shift down.
        b = np.frombuffer(raw, dtype=np.uint8)
        if b.size % 3:
            raise AudioError(f"{path}: 24-bit data length {b.size} is not a multiple of 3")
        b = b.reshape(-1, 3).astype(np.int32)
        i32 = (b[:, 0] << 8) | (b[:, 1] << 16) | (b[:, 2] << 24)
        return (i32 >> 16).astype(np.int16)
    if width == 4:
        return (np.frombuffer(raw, dtype="<i4") >> 16).astype(np.int16)
    raise AudioError(f"{path}: unsupported sample width of {width} bytes")


def to_mono(samples: np.ndarray) -> np.ndarray:
    """Downmix ``(n, channels)`` int16 samples to a 1-D mono int16 array.

    Averages in int32 so that summing channels cannot overflow.
    """
    if samples.ndim == 1:
        return samples.astype(np.int16, copy=False)
    if samples.shape[1] == 1:
        return samples[:, 0].astype(np.int16, copy=False)
    return samples.astype(np.int32).mean(axis=1).round().astype(np.int16)


def resample(samples: np.ndarray, src_rate: int, dst_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Resample 1-D int16 audio.

    Uses ``soxr`` for a proper band-limited resample. There is no acceptable
    stdlib resampler: naive decimation aliases badly, and aliasing at 8 kHz
    lands squarely in the speech band, so it would degrade exactly the audio the
    tool exists to test.

    Raises:
        AudioError: The input is not usable 1-D audio, a rate is not positive,
            or a rate change is needed but ``soxr`` is not installed.
    """
    # Validate at the boundary rather than letting the failure surface later.
    # Multi-channel input reaching here means the caller skipped to_mono; soxr
    # would happily resample it per-channel and hand back a 2-D array, and the
    # eventual complaint would come from g711.encode and appear to blame the
    # codec rather than the missing downmix.
    if samples.ndim != 1:
        raise AudioError(
            f"resample expects 1-D mono audio, got shape {samples.shape}. "
            "Pass it through to_mono() first."
        )
    if src_rate < 1 or dst_rate < 1:
        raise AudioError(
            f"sample rates must be positive, got src_rate={src_rate}, dst_rate={dst_rate}"
        )

    if src_rate == dst_rate:
        return samples
    try:
        import soxr
    except ImportError as exc:
        raise AudioError(
            f"input is {src_rate} Hz and must be resampled to {dst_rate} Hz, but the "
            "'soxr' package is not installed. Install it with "
            "'pip install streamdouble[resample]', or supply audio that is already "
            f"{dst_rate} Hz mono."
        ) from exc

    resampled = soxr.resample(samples.astype(np.float32), src_rate, dst_rate, quality="VHQ")
    # Clip before casting: a band-limited resampler can overshoot past full scale
    # on transients, and a bare astype would wrap that around to the opposite sign.
    return np.clip(np.round(resampled), -32768, 32767).astype(np.int16)


def frame_ulaw(payload: bytes, pad: bool = True) -> list[bytes]:
    """Split mu-law bytes into 160-byte frames.

    Args:
        payload: mu-law encoded audio.
        pad: If true, pad a short final frame to 160 bytes with mu-law silence.
            If false, drop it.

    Returns:
        A list of frames, each exactly ``FRAME_BYTES`` long.
    """
    frames = [payload[i : i + FRAME_BYTES] for i in range(0, len(payload), FRAME_BYTES)]
    if frames and len(frames[-1]) < FRAME_BYTES:
        if pad:
            short = frames[-1]
            frames[-1] = short + bytes([g711.SILENCE_BYTE]) * (FRAME_BYTES - len(short))
        else:
            frames.pop()
    return frames


def silence_frame() -> bytes:
    """One 20 ms frame of mu-law digital silence."""
    return bytes([g711.SILENCE_BYTE]) * FRAME_BYTES


def wav_to_ulaw_frames(path: str | Path, pad: bool = True) -> list[bytes]:
    """Load a WAV file and convert it to a list of 160-byte mu-law frames.

    Handles downmixing to mono and resampling to 8 kHz along the way.
    """
    samples, rate = load_wav(path)
    mono = to_mono(samples)
    resampled = resample(mono, rate, SAMPLE_RATE)
    return frame_ulaw(g711.encode(resampled), pad=pad)


def write_stereo_wav(
    left: bytes,
    right_segments: Sequence[tuple[float, bytes]],
    path: str | Path,
) -> None:
    """Write both sides of a call to one stereo WAV: caller left, agent right.

    ``left`` is continuous from the start of the call. ``right_segments`` is
    ``(offset_seconds, mu-law bytes)`` pairs, each placed at the instant that
    audio *arrived*, with real silence in the gaps between them.

    **Placing rather than concatenating is the entire point, and getting it
    wrong would invert the tool's most useful finding.** Agents batch their
    outbound audio: this project's own headline bug was an agent sending 9.5
    seconds of speech in 0.85 seconds, leaving the caller listening for another
    8.7 seconds while the agent believed it had finished. Concatenating those
    frames would produce a file in which the reply sounds continuous and
    perfectly timed -- a picture of the opposite of the bug. Placed at arrival
    times, the same data shows a burst followed by a long silence, which is
    what actually happened on the wire.

    A consequence worth stating: the right channel is a picture of *delivery*,
    not of playback. What the caller would have heard is the same audio
    stretched over its real duration. The two differ by exactly the
    ``playback_tail_ms`` that ``metrics.py`` reports, and neither rendering is
    wrong -- but only one of them shows the batching.

    Overlapping segments are summed rather than replacing one another, because
    dropping audio to make the arithmetic tidy would be the same class of
    dishonesty in miniature.
    """
    left_samples = g711.decode(left)

    latest_end = 0
    decoded: list[tuple[int, np.ndarray]] = []
    for offset_s, payload in right_segments:
        if not payload:
            continue
        start = max(0, round(offset_s * SAMPLE_RATE))
        samples = g711.decode(payload)
        decoded.append((start, samples))
        latest_end = max(latest_end, start + len(samples))

    length = max(len(left_samples), latest_end)

    # int32 while summing, so overlapping segments cannot wrap around before
    # they are clipped. int16 addition overflowing silently would turn loud
    # audio into loud noise of the opposite sign.
    right_samples = np.zeros(length, dtype=np.int32)
    for start, samples in decoded:
        right_samples[start : start + len(samples)] += samples.astype(np.int32)
    np.clip(right_samples, -32768, 32767, out=right_samples)

    padded_left = np.zeros(length, dtype=np.int16)
    padded_left[: len(left_samples)] = left_samples

    interleaved = np.empty(length * 2, dtype=np.int16)
    interleaved[0::2] = padded_left
    interleaved[1::2] = right_samples.astype(np.int16)

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(interleaved.tobytes())


def ulaw_to_wav(payload: bytes, path: str | Path) -> None:
    """Decode mu-law bytes and write them to an 8 kHz mono 16-bit WAV file."""
    samples = g711.decode(payload)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(samples.tobytes())
