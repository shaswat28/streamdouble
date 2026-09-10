"""End-to-end signal integrity: does audio survive the whole pipeline intact?

Review gate 1 calls for decoding a ``media.payload`` back to a WAV and listening
to it, on the grounds that tests can pass while the audio is garbage. That is
correct, and the by-ear check is still worth doing -- but most of what an ear
catches can be stated precisely, and what can be stated precisely should be a
test that runs on every commit rather than a ritual someone performs once.

These tests take audio all the way through the real path -- WAV, resample,
mu-law, framing, base64, JSON, and back -- and assert on the recovered signal
rather than on the bytes. They are built to fail loudly for the specific
failure modes that produce "it sounds like static":

* scrambled or misordered bytes -> the tone is no longer at its own frequency
* wrong sample rate -> the tone comes back transposed
* double base64 -> the payload does not decode to audio at all
* zero-padding instead of silence-padding -> a burst of noise at the tail
* sign or endianness errors -> correlation with the source collapses

What they cannot cover is subjective quality on real speech. That check stays
with a human, on a real recording.
"""

from __future__ import annotations

import base64
import json

import numpy as np
import pytest

from streamdouble import audio, g711
from streamdouble.protocol import MediaStreamEncoder, parse_outbound


def dominant_frequency(signal: np.ndarray, rate: int) -> float:
    """Frequency of the strongest spectral peak, in Hz."""
    windowed = signal.astype(np.float64) * np.hanning(signal.size)
    spectrum = np.abs(np.fft.rfft(windowed))
    freqs = np.fft.rfftfreq(signal.size, 1 / rate)
    return float(freqs[int(np.argmax(spectrum))])


def through_the_wire(frames: list[bytes]) -> np.ndarray:
    """Push frames through the full protocol path and recover the audio.

    Builds real JSON media frames, serialises them, parses them back with the
    real parser, and decodes the result -- the same path a live session takes,
    with the socket removed.
    """
    encoder = MediaStreamEncoder()
    encoder.start()

    recovered = bytearray()
    for media_frame in frames:
        wire = json.dumps(encoder.media(media_frame))
        parsed = parse_outbound(wire, expected_stream_sid=encoder.identity.stream_sid)
        recovered.extend(parsed.payload)

    return g711.decode(bytes(recovered))


def test_a_tone_comes_back_at_its_own_frequency(fixture_dir):
    """A 440 Hz tone is still 440 Hz after a full round trip.

    The single most informative check in this file. Byte scrambling, frame
    misordering, an off-by-one in framing, a wrong sample rate, or a
    sign-convention error all move or destroy this peak. If the tone comes back
    at 440 Hz with a clean spectrum, the pipeline is carrying audio, not noise.
    """
    frames = audio.wav_to_ulaw_frames(fixture_dir / "tone_440_8k.wav")
    recovered = through_the_wire(frames)

    peak = dominant_frequency(recovered, audio.SAMPLE_RATE)
    assert abs(peak - 440) < 10, f"tone came back at {peak:.0f} Hz, expected 440 Hz"


def test_the_tone_is_clean_not_merely_present(fixture_dir):
    """Most of the recovered energy sits at the fundamental.

    A peak at the right frequency is necessary but not sufficient: heavily
    distorted audio can still peak in the right place. Requiring the
    fundamental to dominate rules out the "recognisable but awful" middle
    ground that mu-law should not produce.
    """
    frames = audio.wav_to_ulaw_frames(fixture_dir / "tone_440_8k.wav")
    recovered = through_the_wire(frames).astype(np.float64)

    windowed = recovered * np.hanning(recovered.size)
    spectrum = np.abs(np.fft.rfft(windowed)) ** 2
    freqs = np.fft.rfftfreq(recovered.size, 1 / audio.SAMPLE_RATE)

    fundamental = spectrum[(freqs > 420) & (freqs < 460)].sum()
    assert fundamental / spectrum.sum() > 0.9


def test_speech_survives_with_high_correlation(speech_8k_path, speech_8k_samples):
    """Recovered speech correlates almost perfectly with the source.

    Correlation, unlike SNR, is insensitive to overall level but extremely
    sensitive to sample reordering, sign inversion and timing shifts. Above 0.99
    the waveform is genuinely the same waveform.
    """
    frames = audio.wav_to_ulaw_frames(speech_8k_path)
    recovered = through_the_wire(frames)[: speech_8k_samples.size].astype(np.float64)
    original = speech_8k_samples.astype(np.float64)

    correlation = float(
        np.corrcoef(original - original.mean(), recovered - recovered.mean())[0, 1]
    )
    assert correlation > 0.99, f"correlation of {correlation:.4f} is too low"


def test_no_sample_offset_is_introduced(speech_8k_path, speech_8k_samples):
    """The recovered signal is aligned with the source, not shifted.

    A one-frame or one-sample offset still correlates well at lag zero for
    speech-like content, so this checks the peak of the cross-correlation
    directly. An offset here would put every Phase 3 latency measurement out by
    a constant, which is exactly the kind of error that gets trusted because it
    looks plausible.
    """
    frames = audio.wav_to_ulaw_frames(speech_8k_path)
    recovered = through_the_wire(frames)[: speech_8k_samples.size].astype(np.float64)
    original = speech_8k_samples.astype(np.float64)

    # Cross-correlate over a small lag window; the peak must be at lag 0.
    window = 50
    correlations = [
        float(np.dot(original[window:-window], np.roll(recovered, lag)[window:-window]))
        for lag in range(-window, window + 1)
    ]
    best_lag = int(np.argmax(correlations)) - window
    assert best_lag == 0, f"recovered audio is offset by {best_lag} samples"


def test_padding_is_silent_not_noisy(fixture_dir):
    """The padded tail of a partial frame is silence.

    ``partial_frame_8k.wav`` is 1000 samples, so the last frame is 60 samples of
    audio and 100 of padding. Padding with 0x00 instead of 0xFF would put a
    burst near full scale here -- a click at the end of every clip that is not
    an exact multiple of 20 ms.
    """
    frames = audio.wav_to_ulaw_frames(fixture_dir / "partial_frame_8k.wav")
    recovered = through_the_wire(frames)

    assert recovered.size == 7 * audio.SAMPLES_PER_FRAME
    tail = recovered[1000:]
    assert tail.size == 120
    assert np.all(tail == 0), "padding is not silent"


def test_silence_stays_silent_end_to_end(fixture_dir):
    """Silence in, silence out. No DC offset, no dither, no clicks."""
    frames = audio.wav_to_ulaw_frames(fixture_dir / "silence_8k.wav")
    recovered = through_the_wire(frames)
    assert np.all(recovered == 0)


def test_resampled_audio_survives_the_wire(fixture_dir):
    """A 44.1 kHz stereo source arrives as intelligible 8 kHz mono.

    Covers the longest path into the encoder: load, downmix, resample, encode,
    frame, base64, parse, decode.
    """
    frames = audio.wav_to_ulaw_frames(fixture_dir / "speech_44k_stereo.wav")
    recovered = through_the_wire(frames).astype(np.float64)

    # Energy is concentrated in the telephone band rather than smeared across
    # the spectrum, which is what scrambled bytes look like.
    windowed = recovered * np.hanning(recovered.size)
    spectrum = np.abs(np.fft.rfft(windowed)) ** 2
    freqs = np.fft.rfftfreq(recovered.size, 1 / audio.SAMPLE_RATE)

    in_band = spectrum[(freqs >= 100) & (freqs < 3400)].sum()
    assert in_band / spectrum.sum() > 0.8


def test_recovered_audio_is_writable_and_reloadable(tmp_path, speech_8k_path):
    """The decode-to-WAV path produces a file that loads back identically.

    This is the path a human uses for the by-ear check, so it needs to work
    before that check is meaningful.
    """
    frames = audio.wav_to_ulaw_frames(speech_8k_path)
    encoder = MediaStreamEncoder()
    encoder.start()

    payload = bytearray()
    for media_frame in frames:
        wire = json.loads(json.dumps(encoder.media(media_frame)))
        payload.extend(base64.b64decode(wire["media"]["payload"], validate=True))

    out = tmp_path / "recovered.wav"
    audio.ulaw_to_wav(bytes(payload), out)

    samples, rate = audio.load_wav(out)
    assert rate == audio.SAMPLE_RATE
    assert np.array_equal(audio.to_mono(samples), g711.decode(bytes(payload)))


@pytest.mark.parametrize("frequency", [200, 440, 1000, 2000, 3000])
def test_frequencies_across_the_telephone_band_are_preserved(frequency):
    """Tones across the usable band come back at the right frequency.

    A single tone could pass by coincidence. Sweeping the band rules out
    frequency-dependent errors -- a wrong sample rate, for instance, transposes
    everything by a constant ratio, which one tone cannot distinguish from a
    correct result at a different frequency.
    """
    duration_s = 0.5
    t = np.arange(int(audio.SAMPLE_RATE * duration_s)) / audio.SAMPLE_RATE
    tone = np.round(np.sin(2 * np.pi * frequency * t) * 0.8 * 32767).astype(np.int16)

    frames = audio.frame_ulaw(g711.encode(tone))
    recovered = through_the_wire(frames)

    peak = dominant_frequency(recovered, audio.SAMPLE_RATE)
    assert abs(peak - frequency) < 15, f"{frequency} Hz came back at {peak:.0f} Hz"
