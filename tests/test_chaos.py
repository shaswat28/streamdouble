"""Tests for network impairment.

Chaos that cannot be replayed is not testing, it is a random number generator
that occasionally fails your build. Most of these assert determinism in one form
or another, because that is the property the feature lives or dies on: when a
bad network finds a real bug, the seed has to be the reproduction.
"""

from __future__ import annotations

import pytest

from streamdouble.chaos import Impairments, Network

# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_the_default_is_a_perfect_connection():
    """An unconfigured session behaves as though this module did not exist."""
    network = Network()
    assert not network.active
    assert not any(network.should_drop() for _ in range(200))
    assert network.delay_s() == 0.0


@pytest.mark.parametrize("loss", [-0.1, 1.1, 2])
def test_loss_must_be_a_probability(loss):
    with pytest.raises(ValueError, match="probability"):
        Impairments(loss=loss)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [({"jitter_ms": -1}, "jitter"), ({"latency_ms": -5}, "latency")],
)
def test_timings_cannot_be_negative(kwargs, match):
    """Negative delay would mean delivering audio before it was spoken."""
    with pytest.raises(ValueError, match=match):
        Impairments(**kwargs)


def test_impairments_describe_themselves_readably():
    assert Impairments().describe() == "none"
    described = Impairments(loss=0.05, jitter_ms=30, latency_ms=80).describe()
    assert "5.0% loss" in described
    assert "30ms jitter" in described
    assert "80ms latency" in described


# --------------------------------------------------------------------------
# Determinism -- the property the feature rests on
# --------------------------------------------------------------------------


def decisions(seed: int, count: int = 300) -> list[bool]:
    network = Network(Impairments(loss=0.2), seed=seed)
    return [network.should_drop() for _ in range(count)]


def test_the_same_seed_drops_the_same_frames():
    """A chaos failure is reproducible from its seed alone.

    Without this the feature is unusable: a run that finds a bug cannot be
    handed to anyone, and a fix cannot be shown to work.
    """
    assert decisions(1234) == decisions(1234)


def test_different_seeds_drop_different_frames():
    assert decisions(1) != decisions(2)


def test_delays_are_reproducible_too():
    def draws(seed: int) -> list[float]:
        network = Network(Impairments(jitter_ms=50), seed=seed)
        return [network.delay_s() for _ in range(100)]

    assert draws(99) == draws(99)
    assert draws(99) != draws(100)


def test_the_seed_is_recorded_for_the_report():
    """The summary names the seed, so a failing run is self-describing."""
    network = Network(Impairments(loss=0.1), seed=4242)
    for _ in range(50):
        network.should_drop()
    assert "4242" in network.summary()


# --------------------------------------------------------------------------
# Loss
# --------------------------------------------------------------------------


def test_loss_rate_is_roughly_what_was_asked_for():
    """Over many frames the drop rate approaches the configured probability.

    Loose bounds: this is checking the probability is wired through at all, not
    re-testing the standard library's uniform generator.
    """
    network = Network(Impairments(loss=0.25), seed=7)
    dropped = sum(network.should_drop() for _ in range(4000))
    assert 0.20 < dropped / 4000 < 0.30


def test_zero_loss_drops_nothing():
    network = Network(Impairments(jitter_ms=100), seed=1)
    assert not any(network.should_drop() for _ in range(500))


def test_total_loss_drops_everything():
    network = Network(Impairments(loss=1.0), seed=1)
    assert all(network.should_drop() for _ in range(100))


def test_dropped_frames_are_counted():
    network = Network(Impairments(loss=0.5), seed=3)
    for _ in range(200):
        network.should_drop()

    assert network.frames_considered == 200
    assert 0 < network.frames_dropped < 200
    assert f"{network.frames_dropped}/200" in network.summary()


# --------------------------------------------------------------------------
# Delay
# --------------------------------------------------------------------------


def test_latency_is_a_constant_addition():
    network = Network(Impairments(latency_ms=120), seed=1)
    assert [network.delay_s() for _ in range(10)] == [pytest.approx(0.12)] * 10


def test_jitter_varies_within_its_bound():
    network = Network(Impairments(jitter_ms=40), seed=5)
    draws = [network.delay_s() for _ in range(500)]

    assert all(0 <= draw <= 0.04 for draw in draws)
    assert len(set(draws)) > 100, "jitter should actually vary"


def test_jitter_is_never_negative():
    """A frame cannot arrive before the audio in it was spoken.

    Symmetric jitter around zero would be the obvious implementation and would
    model something physically impossible.
    """
    network = Network(Impairments(jitter_ms=100), seed=11)
    assert all(network.delay_s() >= 0 for _ in range(500))


def test_latency_and_jitter_compose():
    """Constant distance plus variable instability, added together."""
    network = Network(Impairments(jitter_ms=20, latency_ms=100), seed=2)
    draws = [network.delay_s() for _ in range(200)]

    assert all(0.10 <= draw <= 0.12 for draw in draws)
    assert min(draws) < max(draws)


def test_summary_reports_the_realised_impairment_not_the_requested_one():
    """What actually happened, which on a short call differs from the setting.

    Asking for 10% loss over 20 frames does not give exactly two drops, and
    reporting the configured figure as though it were the outcome would be a
    small lie in a tool whose whole value is not telling those.
    """
    network = Network(Impairments(loss=0.1), seed=0)
    for _ in range(20):
        network.should_drop()

    summary = network.summary()
    assert "10.0% loss" in summary  # what was asked for
    assert f"dropped {network.frames_dropped}/20" in summary  # what happened
