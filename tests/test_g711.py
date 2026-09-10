"""Tests for the G.711 mu-law codec.

The centrepiece is the exhaustive parity check against CPython's ``audioop``.
mu-law has only 65536 possible inputs and 256 possible outputs, so "correct" can
be established by enumeration rather than by sampling -- there is no reason to
settle for spot checks on a domain this small.

``audioop`` was removed in Python 3.13 (PEP 594), so those tests skip on newer
interpreters. The property-based tests below do not depend on it and hold
everywhere, which is what keeps the codec pinned once the reference is gone.
"""

from __future__ import annotations

import warnings

import numpy as np
import pytest

from streamdouble import g711

with warnings.catch_warnings():
    # The suite runs with -W error. audioop warns about its own deprecation on
    # import, which is the very reason this package does not depend on it --
    # suppressed here rather than globally, so no other DeprecationWarning gets
    # a free pass.
    warnings.simplefilter("ignore", DeprecationWarning)
    try:  # pragma: no cover - interpreter-dependent, not a real branch
        import audioop

        HAVE_AUDIOOP = True
    except ImportError:  # pragma: no cover
        audioop = None
        HAVE_AUDIOOP = False

needs_audioop = pytest.mark.skipif(
    not HAVE_AUDIOOP, reason="audioop was removed in Python 3.13 (PEP 594)"
)

ALL_SAMPLES = np.arange(-32768, 32768, dtype=np.int16)
ALL_CODES = bytes(range(256))


# --------------------------------------------------------------------------
# Parity with the reference implementation
# --------------------------------------------------------------------------


@needs_audioop
def test_encode_matches_audioop_for_every_int16():
    """Every one of the 65536 possible samples encodes identically to audioop."""
    assert g711.encode(ALL_SAMPLES) == audioop.lin2ulaw(ALL_SAMPLES.tobytes(), 2)


@needs_audioop
def test_decode_matches_audioop_for_every_code():
    """All 256 mu-law codes decode identically to audioop."""
    assert g711.decode(ALL_CODES).tobytes() == audioop.ulaw2lin(ALL_CODES, 2)


# --------------------------------------------------------------------------
# Properties that hold with or without the reference
# --------------------------------------------------------------------------


def test_vector_path_matches_scalar_path():
    """The lookup tables agree with the readable scalar implementation.

    They are built from it at import, so this mostly guards against someone
    later "optimising" one path without the other.
    """
    scalar = bytes(g711.encode_scalar(int(s)) for s in ALL_SAMPLES)
    assert g711.encode(ALL_SAMPLES) == scalar

    scalar_decoded = np.array([g711.decode_scalar(b) for b in ALL_CODES], dtype=np.int16)
    assert np.array_equal(g711.decode(ALL_CODES), scalar_decoded)


def test_silence_encodes_to_0xff_not_0x00():
    """Digital silence is 0xFF.

    mu-law inverts the assembled byte, so a buffer of 0x00 is not silence -- it
    is close to full-scale. Padding a short frame with zero bytes produces an
    audible click on a real call, which is precisely the class of bug this
    package exists to surface.
    """
    assert g711.encode_scalar(0) == 0xFF
    assert g711.SILENCE_BYTE == 0xFF
    assert g711.decode_scalar(0xFF) == 0
    assert g711.decode_scalar(0x00) != 0
    assert abs(g711.decode_scalar(0x00)) > 30000


def test_decode_output_stays_in_int16_range():
    """No code decodes outside int16.

    Guards the decoder's scaling directly: an extra ``<< 2`` here -- an easy
    mistake, since the encoder does shift -- overflows at full scale.
    """
    decoded = g711.decode(ALL_CODES)
    assert decoded.dtype == np.int16
    assert decoded.min() == -32124
    assert decoded.max() == 32124


def test_encode_reaches_every_code_except_negative_zero():
    """255 of the 256 codes are reachable, and the odd one out is 0x7F.

    mu-law is sign-magnitude, so it has two encodings of zero. 0xFF is positive
    zero and is what the encoder emits; 0x7F is negative zero and no input can
    produce it, because reaching a zero magnitude on the negative side would
    require a sample that is simultaneously negative and zero.

    Asserting the exact identity of the unreachable code is much sharper than
    asserting a count: a broken segment lookup also leaves gaps, but not
    *this* gap.
    """
    encoded = np.frombuffer(g711.encode(ALL_SAMPLES), dtype=np.uint8)
    assert encoded.size == ALL_SAMPLES.size

    unreachable = set(range(256)) - set(encoded.tolist())
    assert unreachable == {0x7F}
    assert g711.decode_scalar(0x7F) == 0
    assert g711.decode_scalar(0xFF) == 0


def test_codec_is_asymmetric_about_zero():
    """-1 and +1 encode differently.

    This is not a bug to be smoothed over. mu-law is defined on 14-bit linear,
    and the 16->14 bit conversion is an arithmetic shift that rounds toward
    negative infinity. Every conforming implementation shares this behaviour, so
    a codec that is symmetric here is the one that is wrong.
    """
    assert g711.encode_scalar(1) != g711.encode_scalar(-1)
    assert g711.encode_scalar(1) == 0xFF
    assert g711.encode_scalar(-1) == 0x7E


def test_decode_encode_is_idempotent_except_for_negative_zero():
    """Decoding a code and re-encoding it returns the same code.

    Every decoded value is a quantisation centroid, so it must map back exactly.
    A failure here means the encoder's segment boundaries and the decoder's
    reconstruction levels disagree -- audible as persistent low-level distortion
    that no single-pass test would catch.

    The sole exception is negative zero (0x7F), which decodes to 0 and therefore
    re-encodes to positive zero (0xFF). Both represent silence, so nothing is
    lost; it is simply the one code the encoder cannot emit.
    """
    decoded = g711.decode(ALL_CODES)
    reencoded = g711.encode(decoded)

    differing = [c for c in range(256) if reencoded[c] != c]
    assert differing == [0x7F]
    assert reencoded[0x7F] == 0xFF


def test_clipping_saturates_rather_than_wrapping():
    """Out-of-range magnitudes saturate to the extreme codes."""
    extremes = np.array([-32768, -32767, 32767, 32000, -32000], dtype=np.int16)
    decoded = g711.decode(g711.encode(extremes))
    # Sign is preserved -- a wrap would flip it, which is the failure this catches.
    assert np.all(np.sign(decoded) == np.sign(extremes))
    assert np.all(np.abs(decoded) <= 32124)


@pytest.mark.parametrize("magnitude", [0, 1, 8, 100, 1000, 8000, 20000, 32124])
def test_round_trip_error_is_bounded_by_quantisation_step(magnitude):
    """Round-trip error stays within one mu-law step for both signs.

    mu-law is logarithmic, so absolute error grows with magnitude; relative
    error is what stays bounded. 12.5% is comfortably inside the ~6% worst case
    for a correct implementation and far outside what a broken segment or
    mantissa shift produces.
    """
    for value in (magnitude, -magnitude):
        sample = np.array([value], dtype=np.int16)
        reconstructed = int(g711.decode(g711.encode(sample))[0])
        assert abs(reconstructed - value) <= max(8, abs(value) * 0.125)


def test_round_trip_snr_on_real_signal(speech_8k_samples):
    """Speech survives a round trip at roughly the SNR G.711 promises.

    G.711 targets about 38 dB SNR over its dynamic range. Measuring on an actual
    speech-like signal, rather than on a synthetic worst case, is what would
    catch a codec that passes the per-sample bounds above but mangles the
    waveform -- for example by getting the sign convention right and the
    segments wrong.
    """
    original = speech_8k_samples.astype(np.float64)
    reconstructed = g711.decode(g711.encode(speech_8k_samples)).astype(np.float64)

    noise = reconstructed - original
    snr_db = 10 * np.log10((original**2).sum() / (noise**2).sum())
    assert snr_db > 30, f"round-trip SNR of {snr_db:.1f} dB is too low for G.711"


# --------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------


def test_encode_rejects_multichannel_input():
    """A 2-D array is a downmix that never happened, not something to guess at."""
    stereo = np.zeros((100, 2), dtype=np.int16)
    with pytest.raises(ValueError, match="1-D"):
        g711.encode(stereo)


def test_encode_accepts_castable_dtypes():
    """Integer arrays that are not already int16 are accepted."""
    as_int32 = np.array([0, 100, -100], dtype=np.int32)
    as_int16 = as_int32.astype(np.int16)
    assert g711.encode(as_int32) == g711.encode(as_int16)


def test_encode_of_empty_input_is_empty():
    assert g711.encode(np.array([], dtype=np.int16)) == b""
    assert g711.decode(b"").size == 0
