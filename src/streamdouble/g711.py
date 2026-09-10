"""G.711 mu-law codec.

Implemented from the ITU-T G.711 specification rather than the stdlib
``audioop`` module, which was removed in Python 3.13 (PEP 594). Twilio Media
Streams carries ``audio/x-mulaw`` at 8000 Hz, so this codec sits on the hot path
for every frame in both directions and its correctness underpins everything else
in the package.

This is byte-for-byte identical to ``audioop.lin2ulaw`` / ``audioop.ulaw2lin``
for all 65536 possible 16-bit inputs and all 256 mu-law bytes;
``tests/test_g711.py`` asserts that exhaustively on interpreters where
``audioop`` is still importable.

Reference: ITU-T Recommendation G.711 (11/88). The implementation follows the
CCITT reference sources as distributed by Sun Microsystems (``g711.c``, public
domain), which is also what CPython's ``audioop`` derives from.

Two details account for essentially every bug people hit here:

1. **mu-law is a 14-bit format.** A 16-bit sample is arithmetic-shifted right
   by 2 before encoding. The shift is *arithmetic*, so it rounds toward negative
   infinity, which is why the codec is not symmetric about zero: -1 encodes
   differently from +1. The decoder needs no matching shift -- it subtracts BIAS
   *after* the segment shift, which puts its output back in 16-bit range on its
   own. Adding a ``<< 2`` there overflows at full scale.
2. **The assembled byte is inverted** (XOR with 0xFF for positives, 0x7F for
   negatives -- the sign bit is folded into the same mask). This is why digital
   silence is 0xFF rather than 0x00, which matters when padding a short frame.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "BIAS",
    "CLIP",
    "SILENCE_BYTE",
    "decode",
    "decode_scalar",
    "encode",
    "encode_scalar",
]

#: Added to the 14-bit magnitude before segment lookup, per G.711.
BIAS = 0x84

#: Largest 14-bit magnitude representable before the top segment saturates.
CLIP = 8159

#: mu-law encoding of a zero sample. Digital silence is 0xFF, not 0x00.
SILENCE_BYTE = 0xFF

_QUANT_MASK = 0x0F
_SEG_MASK = 0x70
_SEG_SHIFT = 4
_SIGN_BIT = 0x80

# Upper bound of each of the 8 mu-law segments, in the biased 14-bit domain.
_SEG_END = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)


def _segment(value: int) -> int:
    """Index of the mu-law segment containing ``value``; 8 means saturated."""
    for i, end in enumerate(_SEG_END):
        if value <= end:
            return i
    return 8


def encode_scalar(sample: int) -> int:
    """Encode one 16-bit PCM sample to one mu-law byte.

    Reference implementation, kept readable on purpose: the vectorised tables
    below are built from this function, so the fast path cannot drift from it.
    """
    # 16-bit -> 14-bit. Arithmetic shift; the rounding toward -inf here is what
    # makes the codec asymmetric about zero, and matching it is what makes this
    # agree with every other G.711 implementation in the wild.
    pcm = sample >> 2

    # Split into sign and magnitude. The sign is carried in the output mask
    # rather than a separate variable, because the final step inverts the byte.
    if pcm < 0:
        pcm = -pcm
        mask = 0x7F
    else:
        mask = 0xFF

    if pcm > CLIP:
        pcm = CLIP
    pcm += BIAS >> 2

    segment = _segment(pcm)
    if segment >= 8:
        # Saturated: the largest-magnitude code, sign applied via the mask.
        return 0x7F ^ mask
    return ((segment << 4) | ((pcm >> (segment + 1)) & _QUANT_MASK)) ^ mask


def decode_scalar(byte: int) -> int:
    """Decode one mu-law byte to a 16-bit PCM sample."""
    byte = ~byte & 0xFF

    magnitude = ((byte & _QUANT_MASK) << 3) + BIAS
    magnitude <<= (byte & _SEG_MASK) >> _SEG_SHIFT

    return BIAS - magnitude if byte & _SIGN_BIT else magnitude - BIAS


# Lookup tables, built once at import from the scalar reference above.
# _ENCODE_TABLE is indexed by (sample + 32768) so a plain non-negative index
# covers the whole int16 range.
_ENCODE_TABLE = np.array([encode_scalar(s) for s in range(-32768, 32768)], dtype=np.uint8)
_DECODE_TABLE = np.array([decode_scalar(b) for b in range(256)], dtype=np.int16)


def encode(samples: np.ndarray) -> bytes:
    """Encode an array of 16-bit PCM samples to mu-law bytes.

    Args:
        samples: 1-D array of int16, or anything castable to it.

    Returns:
        One mu-law byte per input sample.
    """
    samples = np.asarray(samples)
    if samples.ndim != 1:
        raise ValueError(f"expected a 1-D sample array, got shape {samples.shape}")
    if samples.dtype != np.int16:
        samples = samples.astype(np.int16)
    # int32 intermediate: the +32768 offset would overflow int16.
    return _ENCODE_TABLE[samples.astype(np.int32) + 32768].tobytes()


def decode(payload: bytes) -> np.ndarray:
    """Decode mu-law bytes to a 1-D int16 PCM array."""
    return _DECODE_TABLE[np.frombuffer(payload, dtype=np.uint8)]
