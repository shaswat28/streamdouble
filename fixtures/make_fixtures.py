"""Generate the deterministic WAV fixtures used by the test suite.

Run from the repository root::

    python fixtures/make_fixtures.py

The generated files are committed, so this script only needs re-running if a
fixture definition changes. Everything is seeded and derived from closed-form
signals, so regenerating produces byte-identical output on any platform -- which
is what lets ``tests/test_golden.py`` pin exact frame bytes.

Why synthetic rather than recorded speech: a golden-file test needs bit-exact
reproducibility, and a recording cannot be regenerated if the file is lost.
The signals below are speech-*like* (pitch pulses shaped by formant resonances,
syllable-rate envelope) so that they exercise the codec across a realistic
amplitude and spectral range rather than sitting in one quiet corner of it.

Recorded speech is still worth adding for the by-ear check that no automated
test replaces -- drop real WAVs alongside these; nothing here depends on their
absence.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

FIXTURES = Path(__file__).parent

#: Fixed seed for every noise source, so output is reproducible.
SEED = 20260910


def deterministic_noise(count: int, seed: int) -> np.ndarray:
    """Reproducible pseudo-random values in [-1, 1), from integer arithmetic only.

    Deliberately not ``np.random.default_rng``. NumPy guarantees ``Generator``
    stream-compatibility only for the same seed, the same call sequence, the
    same NumPy build, the same environment, the same machine *and* the same CPU,
    and it explicitly reserves the right to change the stream on any X.Y feature
    release -- the documentation cites re-implementing ``standard_normal`` as an
    acceptable such change.

    That is not a foundation a golden-file test can stand on. ``tests/golden``
    pins these fixtures by SHA-256 and CI regenerates them on both Linux and
    Windows to check they come out identical, so a fixture that shifts under a
    NumPy upgrade would light up every byte-exactness assertion in the suite and
    read exactly like a codec regression.

    This is xorshift64*, which is all integer operations on a fixed-width
    unsigned type, so it produces the same bytes on every platform and every
    NumPy version. The distribution is uniform rather than Gaussian; nothing
    here needs a normal distribution, only reproducible jitter.
    """
    mask = (1 << 64) - 1
    multiplier = 0x2545F4914F6CDD1D
    scale = float(1 << 53)

    # Plain Python integers rather than np.uint64: NumPy treats the wraparound
    # that xorshift depends on as an overflow and warns about it, and the test
    # suite runs with warnings as errors. Python's unbounded ints with an
    # explicit mask give the same arithmetic with no platform-specific
    # behaviour to depend on.
    # Seed through the splitmix64 finaliser. xorshift64* needs a non-zero state,
    # and the obvious way to guarantee that -- nudging even seeds up by one --
    # makes every adjacent even/odd pair collide, so seeds 42 and 43 would
    # produce byte-identical streams. That is not hypothetical: the fixtures use
    # SEED and SEED + 1, which is exactly such a pair, and it silently gave two
    # different-looking fixtures the same jitter sequence.
    state = (seed + 0x9E3779B97F4A7C15) & mask
    state ^= state >> 30
    state = (state * 0xBF58476D1CE4E5B9) & mask
    state ^= state >> 27
    state = (state * 0x94D049BB133111EB) & mask
    state ^= state >> 31
    if state == 0:
        state = 0x9E3779B97F4A7C15

    values = np.empty(count, dtype=np.float64)

    for index in range(count):
        state ^= state >> 12
        state ^= (state << 25) & mask
        state ^= state >> 27
        # Take the top 53 bits: exactly the mantissa width of a float64, so the
        # conversion to a float is itself exact and platform-independent.
        values[index] = ((state * multiplier & mask) >> 11) / scale

    return values * 2.0 - 1.0


def write_wav(
    path: Path,
    samples: np.ndarray,
    rate: int,
    *,
    channels: int = 1,
    width: int = 2,
) -> None:
    """Write int16-valued samples to a PCM WAV file at the given sample width.

    Args:
        samples: 1-D for mono, or ``(n, channels)`` for multi-channel. Values are
            int16-ranged regardless of the target width; wider formats are
            produced by shifting up, so the audible content is identical across
            widths and tests can compare them directly.
    """
    interleaved = samples if samples.ndim == 1 else samples.reshape(-1)

    if width == 2:
        raw = interleaved.astype("<i2").tobytes()
    elif width == 1:
        # 8-bit WAV is unsigned with a 128 offset.
        raw = ((interleaved.astype(np.int32) >> 8) + 128).astype(np.uint8).tobytes()
    elif width == 3:
        # 24-bit packed little-endian. The shift is by 16, not 8: a 24-bit file
        # holding the same audible content stores the 16-bit value scaled up to
        # 24-bit full scale (value << 8), not the 16-bit value sitting in the
        # low 24 bits. Getting this wrong makes the fixture 48 dB too quiet.
        i32 = interleaved.astype(np.int32) << 16
        raw = np.stack(
            [(i32 >> 8) & 0xFF, (i32 >> 16) & 0xFF, (i32 >> 24) & 0xFF], axis=1
        ).astype(np.uint8).tobytes()
    elif width == 4:
        raw = (interleaved.astype(np.int32) << 16).astype("<i4").tobytes()
    else:
        raise ValueError(f"unsupported width {width}")

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(raw)


def _to_int16(signal: np.ndarray, peak: float = 0.8) -> np.ndarray:
    """Normalise a float signal to a given peak and quantise to int16."""
    largest = np.max(np.abs(signal))
    if largest > 0:
        signal = signal / largest
    return np.round(signal * peak * 32767).astype(np.int16)


def speech_like(duration_s: float, rate: int, seed: int) -> np.ndarray:
    """A speech-like signal: pitch pulses through drifting formant resonances.

    Not intelligible speech, but it shares the properties that matter for codec
    testing -- a harmonic-rich excitation, energy concentrated in the 300-3400 Hz
    telephone band, a syllable-rate amplitude envelope, and pauses.
    """
    n = int(duration_s * rate)
    t = np.arange(n) / rate

    # Glottal excitation: a pulse train with slow pitch drift and light jitter.
    f0 = 120.0 + 15.0 * np.sin(2 * np.pi * 0.7 * t)
    phase = np.cumsum(2 * np.pi * f0 / rate)
    excitation = np.zeros(n)
    # Sum a few harmonics rather than an ideal impulse train: band-limited, so
    # the 44.1 kHz and 8 kHz variants stay spectrally comparable.
    for harmonic in range(1, 12):
        excitation += np.sin(harmonic * phase) / harmonic

    # Three formants, drifting as if the vocal tract were moving.
    signal = np.zeros(n)
    for base, depth, weight in ((700.0, 120.0, 1.0), (1220.0, 200.0, 0.55), (2600.0, 150.0, 0.25)):
        centre = base + depth * np.sin(2 * np.pi * 0.9 * t + base)
        signal += weight * np.sin(2 * np.pi * centre * t) * excitation

    # Syllable-rate envelope, with the troughs pushed to true silence so the
    # fixture contains genuine pauses rather than merely quiet stretches.
    envelope = np.clip(np.sin(2 * np.pi * 3.5 * t) * 0.6 + 0.55, 0.0, 1.0)
    envelope *= 0.9 + 0.1 * deterministic_noise(n, seed)

    return signal * envelope


def main() -> None:
    """Write every fixture. Existing files are overwritten."""
    # --- 8 kHz mono, the native Media Streams format -----------------------
    silence = np.zeros(8000, dtype=np.int16)
    write_wav(FIXTURES / "silence_8k.wav", silence, 8000)

    speech = _to_int16(speech_like(2.0, 8000, SEED))
    write_wav(FIXTURES / "speech_8k.wav", speech, 8000)

    noise = deterministic_noise(speech.size, SEED + 7) * 0.18 * 32767
    noisy = np.clip(speech.astype(np.float64) + noise, -32768, 32767).astype(np.int16)
    write_wav(FIXTURES / "noisy_8k.wav", noisy, 8000)

    long_utterance = _to_int16(speech_like(10.0, 8000, SEED + 1))
    write_wav(FIXTURES / "long_8k.wav", long_utterance, 8000)

    # A tone at full scale: drives the codec into its top segment, where the
    # saturation branch lives.
    t = np.arange(8000) / 8000
    loud = _to_int16(np.sin(2 * np.pi * 440 * t), peak=1.0)
    write_wav(FIXTURES / "tone_440_8k.wav", loud, 8000)

    # Deliberately not a whole number of 20 ms frames: 1000 samples is 6.25
    # frames, so the last frame needs padding.
    write_wav(FIXTURES / "partial_frame_8k.wav", speech[:1000], 8000)

    # --- Sample-width coverage, same audible content -----------------------
    for width in (1, 3, 4):
        write_wav(FIXTURES / f"speech_8k_{width * 8}bit.wav", speech, 8000, width=width)

    # --- Resampling and downmix -------------------------------------------
    # Two different signals, one per channel, so a downmix test can tell an
    # actual average apart from "took the left channel and called it mono".
    left = _to_int16(speech_like(2.0, 44100, SEED))
    right = _to_int16(speech_like(2.0, 44100, SEED + 2))
    write_wav(
        FIXTURES / "speech_44k_stereo.wav",
        np.stack([left, right], axis=1),
        44100,
        channels=2,
    )

    # A pure 6 kHz tone at 44.1 kHz, and nothing else. 6 kHz sits above the
    # 4 kHz Nyquist limit of an 8 kHz stream, so a correct band-limited
    # resampler removes it almost entirely (~46 dB down), while a naive
    # decimator passes it through at full strength, aliased into the speech
    # band. Isolating the tone in its own fixture is what makes this decisive:
    # mixed into speech, the alias is buried under legitimate energy at the
    # same frequency and the test proves nothing.
    t44 = np.arange(int(44100 * 0.5)) / 44100
    write_wav(
        FIXTURES / "alias_bait_44k.wav",
        _to_int16(np.sin(2 * np.pi * 6000 * t44)),
        44100,
    )

    # Mono 16 kHz: the common "downsample by an integer factor" case.
    write_wav(FIXTURES / "speech_16k.wav", _to_int16(speech_like(2.0, 16000, SEED)), 16000)

    for path in sorted(FIXTURES.glob("*.wav")):
        print(f"{path.name:<28} {path.stat().st_size:>8} bytes")


if __name__ == "__main__":
    main()
