"""Regression tests for the issues found at review gate 1.

Kept in one file rather than scattered, so the findings and their fixes stay
legible together. Each test names the failure it prevents.
"""

from __future__ import annotations

import struct
import wave

import numpy as np
import pytest

from streamdouble import audio
from streamdouble.protocol import FrameSequenceError, MediaStreamEncoder


def write_wav(path, samples: bytes, *, channels=1, width=2, rate=8000) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(samples)


def truncate(path, by_bytes: int) -> None:
    """Chop bytes off the end of a file, leaving the header claiming the original size."""
    raw = path.read_bytes()
    path.write_bytes(raw[:-by_bytes])


# --------------------------------------------------------------------------
# Truncated input
# --------------------------------------------------------------------------


def test_odd_truncation_raises_audio_error_not_value_error(tmp_path):
    """A truncated WAV fails as AudioError, not as a bare numpy ValueError.

    Previously np.frombuffer raised ValueError('buffer size must be a multiple
    of element size') straight out of load_wav, breaking the module's promise
    that unusable input arrives as AudioError -- which the CLI's exit codes will
    depend on.
    """
    path = tmp_path / "odd.wav"
    write_wav(path, b"\x01\x02" * 100)
    truncate(path, 1)

    with pytest.raises(audio.AudioError, match="truncated"):
        audio.load_wav(path)


def test_even_truncation_is_reported_rather_than_silently_shortening(tmp_path):
    """Losing whole samples is an error, not a quietly shorter clip.

    The more dangerous half of the same bug: an evenly-truncated file used to
    load without complaint, returning half the audio. Downstream that looks
    like the agent stopped talking, and nothing points at the file.
    """
    path = tmp_path / "short.wav"
    write_wav(path, b"\x01\x02" * 1000)
    truncate(path, 1000)

    with pytest.raises(audio.AudioError) as excinfo:
        audio.load_wav(path)

    message = str(excinfo.value)
    assert "truncated" in message
    # The message must carry both numbers, or it does not help anyone debug.
    assert "1000 frames" in message
    assert "1000" in message


def test_an_intact_file_still_loads(tmp_path):
    """The truncation check does not reject valid files.

    Worth stating explicitly: an over-strict length check here would reject
    every real WAV, and the fixtures alone would not prove otherwise since they
    are all written by the same helper.
    """
    path = tmp_path / "intact.wav"
    write_wav(path, b"\x01\x02" * 500)
    samples, rate = audio.load_wav(path)
    assert samples.shape == (500, 1)
    assert rate == 8000


def test_stereo_length_check_accounts_for_channels(tmp_path):
    """The expected byte count multiplies by channel count as well as width."""
    path = tmp_path / "stereo.wav"
    write_wav(path, b"\x01\x02\x03\x04" * 250, channels=2)
    samples, _ = audio.load_wav(path)
    assert samples.shape == (250, 2)


# --------------------------------------------------------------------------
# Degenerate headers
# --------------------------------------------------------------------------


def test_zero_sample_rate_is_rejected_at_load(tmp_path):
    """A WAV declaring 0 Hz fails with a clear AudioError.

    It used to load happily and then raise ValueError('Sample rate should be
    over 0') from inside soxr -- a third-party message naming neither the file
    nor the field at fault.
    """
    path = tmp_path / "zero-rate.wav"
    write_wav(path, b"\x01\x02" * 100)

    raw = bytearray(path.read_bytes())
    raw[24:28] = struct.pack("<I", 0)  # fmt chunk sample rate
    path.write_bytes(bytes(raw))

    with pytest.raises(audio.AudioError, match="sample rate"):
        audio.load_wav(path)


@pytest.mark.parametrize(("src", "dst"), [(0, 8000), (8000, 0), (-1, 8000), (8000, -8000)])
def test_resample_rejects_non_positive_rates(src, dst):
    """Rate validation lives in resample too, not only at load."""
    with pytest.raises(audio.AudioError, match="positive"):
        audio.resample(np.zeros(100, dtype=np.int16), src, dst)


# --------------------------------------------------------------------------
# Shape validation
# --------------------------------------------------------------------------


def test_resample_rejects_multichannel_input():
    """Forgetting to_mono fails at resample, naming the actual mistake.

    It previously returned a 2-D array, and the complaint surfaced later from
    g711.encode, which made it look like a codec problem rather than a missing
    downmix.
    """
    stereo = np.zeros((100, 2), dtype=np.int16)
    with pytest.raises(audio.AudioError) as excinfo:
        audio.resample(stereo, 16000, 8000)

    message = str(excinfo.value)
    assert "1-D" in message
    assert "to_mono" in message, "the error should name the fix"


def test_resample_validates_shape_even_when_no_rate_change_is_needed():
    """The guard runs before the same-rate short circuit.

    Otherwise a 2-D array passes straight through whenever the input already
    happens to be 8 kHz, and the bug only appears for some inputs.
    """
    stereo = np.zeros((100, 2), dtype=np.int16)
    with pytest.raises(audio.AudioError, match="1-D"):
        audio.resample(stereo, 8000, 8000)


# --------------------------------------------------------------------------
# Frame ordering
# --------------------------------------------------------------------------


@pytest.mark.parametrize("event", ["media", "mark", "dtmf", "stop"])
def test_nothing_may_be_sent_before_start(event):
    """The encoder refuses to emit any frame before ``start``.

    Phase 3 reports agents that violate the protocol, so the simulator must not
    be able to violate it first -- an ordering bug in the session layer would
    otherwise produce an agent-side failure that gets blamed on the agent.
    """
    encoder = MediaStreamEncoder()
    call = {
        "media": lambda: encoder.media(audio.silence_frame()),
        "mark": lambda: encoder.mark("m"),
        "dtmf": lambda: encoder.dtmf("1"),
        "stop": encoder.stop,
    }[event]

    with pytest.raises(FrameSequenceError, match="before 'start'"):
        call()


@pytest.mark.parametrize("event", ["media", "mark", "dtmf", "stop"])
def test_nothing_may_be_sent_after_stop(event):
    """The stream is over once ``stop`` is sent."""
    encoder = MediaStreamEncoder()
    encoder.start()
    encoder.stop()

    call = {
        "media": lambda: encoder.media(audio.silence_frame()),
        "mark": lambda: encoder.mark("m"),
        "dtmf": lambda: encoder.dtmf("1"),
        "stop": encoder.stop,
    }[event]

    with pytest.raises(FrameSequenceError, match="after 'stop'"):
        call()


def test_start_may_only_be_sent_once():
    """Twilio sends exactly one start per stream."""
    encoder = MediaStreamEncoder()
    encoder.start()
    with pytest.raises(FrameSequenceError, match="already been sent"):
        encoder.start()


def test_connected_is_allowed_before_start():
    """``connected`` precedes ``start``, so it must not be caught by the guard."""
    encoder = MediaStreamEncoder()
    assert encoder.connected()["event"] == "connected"
    assert encoder.start()["sequenceNumber"] == "1"


def test_the_normal_sequence_is_unobstructed():
    """A well-formed stream passes through every guard untouched."""
    encoder = MediaStreamEncoder()
    encoder.connected()
    encoder.start()
    for _ in range(3):
        encoder.media(audio.silence_frame())
    encoder.mark("m")
    encoder.dtmf("1")
    assert encoder.stop()["event"] == "stop"


# --------------------------------------------------------------------------
# Fixture determinism
# --------------------------------------------------------------------------


def test_fixture_noise_is_reproducible_without_numpy_random():
    """Fixture noise comes from integer arithmetic, not np.random.

    NumPy guarantees Generator stream-compatibility only for the same seed,
    call sequence, NumPy build, environment, machine and CPU, and reserves the
    right to change the stream on any feature release. The golden file pins
    these fixtures by SHA-256 and CI regenerates them on two operating systems,
    so a NumPy upgrade could have turned every byte-exactness assertion red and
    looked exactly like a codec regression.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from fixtures.make_fixtures import deterministic_noise

    first = deterministic_noise(1000, 42)
    second = deterministic_noise(1000, 42)

    assert np.array_equal(first, second)
    assert not np.array_equal(first, deterministic_noise(1000, 43))
    assert first.min() >= -1.0
    assert first.max() < 1.0
    # Actually varies, rather than being a constant that would trivially pass.
    assert first.std() > 0.4
