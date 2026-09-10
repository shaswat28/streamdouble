"""Network impairment: what a bad mobile connection does to a call.

The happy path is not where voice agents break. They break when the caller is on
a train, and the tool is only worth having if it can reproduce that.

Pure and deterministic: this module decides *whether* to drop a frame and *how
long* to delay one, and nothing else. It performs no I/O, reads no clock, and
given the same seed makes the same decisions every run. A chaos feature that
cannot be replayed is not a test tool, it is a random number generator that
occasionally fails your build -- when a bad seed finds a real bug, you need to
be able to hand someone the seed.

A note on what impairment can and cannot mean here, because it shapes every
decision below and the Twilio documentation does not address it.

**The Twilio-to-app leg is a WebSocket, which is TCP.** TCP retransmits, so a
frame cannot simply vanish in transit and cannot arrive out of order. Whatever
"packet loss" means for a voice call, it does not mean a missing WebSocket
frame. The loss happens *upstream* of Twilio -- on the carrier's RTP leg, which
is UDP over a mobile network -- and by the time Twilio comes to build a frame,
that audio was never received.

So the model here is: a dropped frame is one Twilio never sends, because it
never had the audio. The app sees no gap in ``sequenceNumber`` (Twilio numbers
what it sends), but it does see ``media.timestamp`` jump by more than one frame
interval, because presentation time keeps running while the audio does not.

**This is an inference, not documentation.** Twilio does not say whether it
skips the frame or substitutes silence; both are plausible implementations. The
skip model was chosen because it is the one an agent can actually detect -- a
timestamp discontinuity is a signal, silence substitution is invisible -- and a
test tool should surface the harder case. If Twilio turns out to substitute
silence, this is the knob to change, and the docstring is here so that whoever
finds out knows where to look.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

__all__ = ["Impairments", "Network"]


@dataclass(frozen=True)
class Impairments:
    """How badly to treat the caller's audio.

    Defaults are a perfect connection, so an unconfigured session behaves
    exactly as it did before this module existed.
    """

    #: Probability in [0, 1] that any given frame is never sent.
    loss: float = 0.0

    #: Maximum timing deviation, in milliseconds, applied per frame. Drawn
    #: uniformly from [0, jitter_ms] -- never negative, because a frame cannot
    #: be delivered before the audio in it was spoken.
    jitter_ms: float = 0.0

    #: Constant delay added to every frame, in milliseconds. Models distance
    #: rather than instability: a caller on the other side of the world.
    latency_ms: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.loss <= 1.0:
            raise ValueError(f"loss must be a probability in [0, 1], got {self.loss}")
        if self.jitter_ms < 0:
            raise ValueError(f"jitter_ms cannot be negative, got {self.jitter_ms}")
        if self.latency_ms < 0:
            raise ValueError(f"latency_ms cannot be negative, got {self.latency_ms}")

    @property
    def active(self) -> bool:
        """Whether anything at all is being impaired."""
        return bool(self.loss or self.jitter_ms or self.latency_ms)

    def describe(self) -> str:
        if not self.active:
            return "none"
        parts = []
        if self.loss:
            parts.append(f"{self.loss:.1%} loss")
        if self.jitter_ms:
            parts.append(f"{self.jitter_ms:g}ms jitter")
        if self.latency_ms:
            parts.append(f"{self.latency_ms:g}ms latency")
        return ", ".join(parts)


class Network:
    """Decides, per frame, whether it is lost and how late it is.

    Seeded explicitly rather than left to global randomness. When a chaos run
    finds a bug, the seed is the reproduction: without it the failure is a story
    about something that happened once.

    ``random.Random`` is used rather than ``numpy.random.Generator`` for the same
    reason the fixtures avoid it -- CPython documents the Mersenne Twister
    stream as stable across versions, while NumPy explicitly reserves the right
    to change ``Generator`` on a feature release.
    """

    def __init__(self, impairments: Impairments | None = None, *, seed: int = 0) -> None:
        self.impairments = impairments or Impairments()
        self.seed = seed
        self._random = random.Random(seed)

        self.frames_considered = 0
        self.frames_dropped = 0
        self.total_delay_s = 0.0

    @property
    def active(self) -> bool:
        return self.impairments.active

    def should_drop(self) -> bool:
        """Whether this frame is lost before Twilio ever sees it."""
        self.frames_considered += 1
        if not self.impairments.loss:
            return False
        dropped = self._random.random() < self.impairments.loss
        if dropped:
            self.frames_dropped += 1
        return dropped

    def delay_s(self) -> float:
        """Extra delay for this frame, in seconds, on top of its schedule.

        Constant latency plus a per-frame jitter draw. Always non-negative:
        pulling a frame *earlier* than its deadline would mean delivering audio
        before it was spoken, which no network does.

        Jitter is applied to a frame's release without moving any other frame's
        deadline, so it perturbs spacing without accumulating -- the pacer's
        absolute-deadline scheduling absorbs it, which is exactly the behaviour a
        real jitter buffer has.
        """
        delay = self.impairments.latency_ms / 1000
        if self.impairments.jitter_ms:
            delay += self._random.uniform(0, self.impairments.jitter_ms / 1000)
        self.total_delay_s += delay
        return delay

    def summary(self) -> str:
        if not self.active:
            return "no impairment"
        loss_pct = (
            self.frames_dropped / self.frames_considered * 100
            if self.frames_considered
            else 0.0
        )
        mean_delay_ms = (
            self.total_delay_s / self.frames_considered * 1000
            if self.frames_considered
            else 0.0
        )
        return (
            f"{self.impairments.describe()} (seed {self.seed}): "
            f"dropped {self.frames_dropped}/{self.frames_considered} "
            f"({loss_pct:.1f}%), mean added delay {mean_delay_ms:.1f}ms"
        )
