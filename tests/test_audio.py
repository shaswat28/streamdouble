"""Tests for WAV loading, downmixing, resampling and 20 ms framing."""

from __future__ import annotations

import wave

import numpy as np
import pytest

from streamdouble import audio, g711


def rms(signal: np.ndarray) -> float:
    """Root-mean-square level of a signal, as a float."""
    return float(np.sqrt((signal.astype(np.float64) ** 2).mean())) if signal.size else 0.0


def band_energy(signal: np.ndarray, rate: int, low: float, high: float) -> float:
    """Total spectral energy in ``[low, high)`` Hz."""
    windowed = signal.astype(np.float64) * np.hanning(signal.size)
    spectrum = np.abs(np.fft.rfft(windowed))
    freqs = np.fft.rfftfreq(signal.size, 1 / rate)
    selected = (freqs >= low) & (freqs < high)
    return float((spectrum[selected] ** 2).sum())


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------


def test_frame_geometry_matches_the_protocol():
    """20 ms at 8 kHz is 160 samples, and mu-law is one byte per sample.

    Pinned as a test because every other size in the package is derived from
    these, and a frame that is not 160 bytes is the single most consequential
    way this tool could be unfaithful to Twilio.
    """
    assert audio.SAMPLE_RATE == 8000
    assert audio.FRAME_MS == 20
    assert audio.SAMPLES_PER_FRAME == 160
    assert audio.FRAME_BYTES == 160


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def test_load_wav_returns_shape_and_rate(speech_8k_path):
    samples, rate = audio.load_wav(speech_8k_path)
    assert rate == 8000
    assert samples.dtype == np.int16
    assert samples.ndim == 2
    assert samples.shape[1] == 1
    assert samples.shape[0] == 16000  # 2 seconds at 8 kHz


@pytest.mark.parametrize("width_bits", [8, 24, 32])
def test_load_wav_handles_every_sample_width(fixture_dir, speech_8k_samples, width_bits):
    """8-, 24- and 32-bit WAVs load to the same audio as the 16-bit original.

    The fixtures hold identical content at each width, so any difference is a
    decoding error. 8-bit is checked loosely because it genuinely loses the low
    byte; the wider formats must be exact, since widening and narrowing back is
    lossless. This catches the classic 24-bit sign-extension bug, which a
    tolerance-only assertion would let through as mild distortion.
    """
    samples, rate = audio.load_wav(fixture_dir / f"speech_8k_{width_bits}bit.wav")
    mono = audio.to_mono(samples)
    assert rate == 8000
    assert mono.size == speech_8k_samples.size

    if width_bits == 8:
        # Genuinely lossy: 8-bit PCM keeps only the high byte, which measures
        # around 29 dB SNR on this fixture. The threshold sits below that and
        # far above the ~0 dB a sign or offset error would produce -- 8-bit WAV
        # is unsigned with a 128 offset, and forgetting that inverts the signal.
        error = mono.astype(np.float64) - speech_8k_samples.astype(np.float64)
        noise_rms = float(np.sqrt((error**2).mean()))
        snr_db = 20 * np.log10(rms(speech_8k_samples) / max(noise_rms, 1e-9))
        assert snr_db > 25, f"8-bit SNR of {snr_db:.1f} dB"
    else:
        assert np.array_equal(mono, speech_8k_samples)


def test_load_wav_reports_a_missing_file_clearly(tmp_path):
    with pytest.raises(audio.AudioError, match="no such file"):
        audio.load_wav(tmp_path / "absent.wav")


def test_load_wav_rejects_a_non_wav_file(tmp_path):
    """A non-WAV file fails as AudioError, not as a bare wave.Error.

    Callers should be able to catch one exception type for all bad input.
    """
    bogus = tmp_path / "not-audio.wav"
    bogus.write_bytes(b"this is not a RIFF file at all")
    with pytest.raises(audio.AudioError, match="not a readable PCM WAV"):
        audio.load_wav(bogus)


def test_load_wav_rejects_unsupported_sample_width(tmp_path):
    """A width this module cannot decode is refused rather than misread."""
    path = tmp_path / "odd-width.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * 10)
    # Rewrite the sample-width field in the fmt chunk to an unsupported value.
    raw = bytearray(path.read_bytes())
    raw[34] = 5 * 8  # bits per sample -> 40
    path.write_bytes(bytes(raw))

    with pytest.raises(audio.AudioError, match="unsupported sample width"):
        audio.load_wav(path)


# --------------------------------------------------------------------------
# Downmixing
# --------------------------------------------------------------------------


def test_to_mono_averages_channels_rather_than_dropping_one(fixture_dir):
    """Stereo downmix is a real average of both channels.

    The fixture carries a different signal per channel precisely so that
    "returned the left channel and called it mono" fails here.
    """
    samples, _ = audio.load_wav(fixture_dir / "speech_44k_stereo.wav")
    assert samples.shape[1] == 2

    mono = audio.to_mono(samples)
    expected = samples.astype(np.int32).mean(axis=1).round().astype(np.int16)

    assert mono.ndim == 1
    assert np.array_equal(mono, expected)
    assert not np.array_equal(mono, samples[:, 0])
    assert not np.array_equal(mono, samples[:, 1])


def test_to_mono_does_not_overflow_on_loud_stereo():
    """Summing two full-scale channels must not wrap.

    Averaging in int16 would overflow here and flip the sign, turning a loud
    passage into a loud passage of the opposite polarity.
    """
    loud = np.full((100, 2), 32767, dtype=np.int16)
    assert np.all(audio.to_mono(loud) == 32767)

    quiet = np.full((100, 2), -32768, dtype=np.int16)
    assert np.all(audio.to_mono(quiet) == -32768)


def test_to_mono_passes_through_one_dimensional_input(speech_8k_samples):
    assert np.array_equal(audio.to_mono(speech_8k_samples), speech_8k_samples)


# --------------------------------------------------------------------------
# Resampling
# --------------------------------------------------------------------------


def test_resample_is_a_no_op_at_the_target_rate(speech_8k_samples):
    result = audio.resample(speech_8k_samples, 8000, 8000)
    assert result is speech_8k_samples


def test_resample_produces_the_expected_length(fixture_dir):
    samples, rate = audio.load_wav(fixture_dir / "speech_16k.wav")
    resampled = audio.resample(audio.to_mono(samples), rate, 8000)
    # Half the rate, so half the samples, give or take the resampler's edge handling.
    assert abs(resampled.size - samples.shape[0] // 2) <= 2
    assert resampled.dtype == np.int16


def test_resample_rejects_content_above_nyquist(fixture_dir):
    """A 6 kHz tone is removed on the way to 8 kHz, not aliased into the speech band.

    This is the test that distinguishes a real resampler from decimation. 6 kHz
    is above the 4 kHz Nyquist limit of an 8 kHz stream, so it cannot be
    represented. A band-limited resampler filters it out; naive decimation folds
    it back to 2 kHz, sitting squarely in the middle of the speech band where it
    would be audible as a whistle over every call.

    Measured rejection is around 46 dB; the 30 dB threshold leaves room for
    resampler version differences while staying far away from the ~0 dB that
    decimation gives.
    """
    samples, rate = audio.load_wav(fixture_dir / "alias_bait_44k.wav")
    tone = audio.to_mono(samples)
    assert rate == 44100

    resampled = audio.resample(tone, rate, 8000)
    rejection_db = 20 * np.log10(rms(tone) / max(rms(resampled), 1e-9))
    assert rejection_db > 30, f"only {rejection_db:.1f} dB of alias rejection"


def test_resample_preserves_in_band_speech(fixture_dir, speech_8k_samples):
    """Downsampling real content keeps the speech band intact.

    Alias rejection alone could be achieved by outputting silence; this is the
    other half of the claim.
    """
    samples, rate = audio.load_wav(fixture_dir / "speech_16k.wav")
    resampled = audio.resample(audio.to_mono(samples), rate, 8000)

    speech_band = band_energy(resampled, 8000, 300, 3400)
    total = band_energy(resampled, 8000, 0, 4000)
    assert speech_band / total > 0.5
    # Level is preserved to within a few dB of the natively-8 kHz fixture.
    assert abs(20 * np.log10(rms(resampled) / rms(speech_8k_samples))) < 6


def test_resample_does_not_wrap_on_overshoot():
    """Resampler overshoot clips instead of wrapping to the opposite sign.

    A band-limited resampler rings past full scale on sharp transients. Casting
    that straight to int16 wraps a positive peak to a large negative one, which
    is an audible crack rather than the mild clipping it should be.
    """
    rate = 44100
    t = np.arange(rate // 4) / rate
    # Full-scale square wave: maximal transient content, guaranteed overshoot.
    square = (np.sign(np.sin(2 * np.pi * 300 * t)) * 32767).astype(np.int16)

    resampled = audio.resample(square, rate, 8000)
    assert resampled.dtype == np.int16
    # Sign flips must track the source's own 300 Hz alternation, not exceed it
    # wildly, which is what wrapped samples would produce.
    flips = int(np.sum(np.diff(np.sign(resampled.astype(np.int32))) != 0))
    assert flips < 200


def test_resample_without_soxr_explains_itself(monkeypatch, speech_8k_samples):
    """The missing-dependency error names the package and the workaround.

    ``resample`` is the only place an optional dependency is required, and it is
    reached deep inside a call chain, so a bare ImportError would surface far
    from its cause.
    """
    import builtins

    real_import = builtins.__import__

    def fail_on_soxr(name, *args, **kwargs):
        if name == "soxr":
            raise ImportError("no soxr")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_on_soxr)

    with pytest.raises(audio.AudioError) as excinfo:
        audio.resample(speech_8k_samples, 16000, 8000)

    message = str(excinfo.value)
    assert "soxr" in message
    assert "streamdouble[resample]" in message


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


def test_frame_ulaw_splits_into_exact_frames():
    frames = audio.frame_ulaw(b"\x01" * 480)
    assert len(frames) == 3
    assert all(len(frame) == audio.FRAME_BYTES for frame in frames)


def test_frame_ulaw_pads_a_short_final_frame_with_silence():
    """A partial frame is padded with 0xFF, not 0x00.

    0x00 is near full-scale in mu-law, so padding with zero bytes appends a
    burst of loud noise to the end of every clip that is not an exact multiple
    of 20 ms -- which is most of them.
    """
    frames = audio.frame_ulaw(b"\x01" * 161)
    assert len(frames) == 2
    assert len(frames[1]) == audio.FRAME_BYTES
    assert frames[1][0] == 0x01
    assert set(frames[1][1:]) == {g711.SILENCE_BYTE}


def test_frame_ulaw_can_drop_a_short_final_frame_instead():
    frames = audio.frame_ulaw(b"\x01" * 161, pad=False)
    assert len(frames) == 1


def test_frame_ulaw_leaves_exact_multiples_alone():
    exact = b"\x01" * (audio.FRAME_BYTES * 2)
    frames = audio.frame_ulaw(exact)
    assert len(frames) == 2
    assert b"".join(frames) == exact


def test_frame_ulaw_of_empty_input_is_empty():
    assert audio.frame_ulaw(b"") == []


def test_silence_frame_is_one_frame_of_silence():
    frame = audio.silence_frame()
    assert len(frame) == audio.FRAME_BYTES
    assert set(frame) == {g711.SILENCE_BYTE}
    assert np.all(g711.decode(frame) == 0)


# --------------------------------------------------------------------------
# End-to-end conversion
# --------------------------------------------------------------------------


def test_wav_to_ulaw_frames_end_to_end(speech_8k_path):
    frames = audio.wav_to_ulaw_frames(speech_8k_path)
    # 2 seconds at 20 ms per frame.
    assert len(frames) == 100
    assert all(len(frame) == audio.FRAME_BYTES for frame in frames)


def test_wav_to_ulaw_frames_pads_a_partial_input(fixture_dir):
    """1000 samples is 6.25 frames, so the seventh frame is padded."""
    frames = audio.wav_to_ulaw_frames(fixture_dir / "partial_frame_8k.wav")
    assert len(frames) == 7
    assert frames[-1][-1] == g711.SILENCE_BYTE


def test_wav_to_ulaw_frames_resamples_and_downmixes(fixture_dir):
    """A 44.1 kHz stereo file becomes 8 kHz mono frames in one call."""
    frames = audio.wav_to_ulaw_frames(fixture_dir / "speech_44k_stereo.wav")
    assert len(frames) == 100
    assert all(len(frame) == audio.FRAME_BYTES for frame in frames)


def test_ulaw_to_wav_writes_a_readable_8k_mono_file(tmp_path, speech_8k_samples):
    """The recording path produces a file the loading path accepts."""
    payload = g711.encode(speech_8k_samples)
    out = tmp_path / "out.wav"
    audio.ulaw_to_wav(payload, out)

    samples, rate = audio.load_wav(out)
    assert rate == 8000
    assert samples.shape == (speech_8k_samples.size, 1)

    reloaded = audio.to_mono(samples)
    assert np.array_equal(reloaded, g711.decode(payload))


def test_full_round_trip_preserves_the_signal(tmp_path, speech_8k_path, speech_8k_samples):
    """WAV -> frames -> WAV survives at the SNR mu-law allows.

    The path a real session takes, end to end. Padding makes the output slightly
    longer than the input, so the comparison is over the original length.
    """
    frames = audio.wav_to_ulaw_frames(speech_8k_path)
    out = tmp_path / "round-trip.wav"
    audio.ulaw_to_wav(b"".join(frames), out)

    samples, rate = audio.load_wav(out)
    assert rate == 8000

    reconstructed = audio.to_mono(samples)[: speech_8k_samples.size].astype(np.float64)
    original = speech_8k_samples.astype(np.float64)

    snr_db = 10 * np.log10((original**2).sum() / (((reconstructed - original) ** 2).sum()))
    assert snr_db > 30, f"round-trip SNR of {snr_db:.1f} dB"
