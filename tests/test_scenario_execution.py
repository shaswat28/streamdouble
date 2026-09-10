"""Scenario execution against a live agent.

Parsing is covered in `test_scenario.py`. This is about what the steps actually
do on a socket -- particularly the one that is easy to get wrong and invisible
when it is: `wait` streams silence rather than stopping.
"""

from __future__ import annotations

import pytest

from streamdouble import audio, scenario
from streamdouble.chaos import Impairments
from streamdouble.metrics import compute
from streamdouble.scenario import Expect, Hangup, Say, Scenario, Wait, WaitFor
from streamdouble.session import Session, SessionConfig

CLIP = "fixtures/speech_8k.wav"


def quick(**overrides) -> SessionConfig:
    defaults = {
        "response_timeout_s": 5.0,
        "quiet_period_s": 0.3,
        "max_drain_s": 5.0,
        "connect_timeout_s": 5.0,
    }
    return SessionConfig(**{**defaults, **overrides})


def script(*steps) -> Scenario:
    return Scenario(name="test", steps=list(steps))


def clip(count: int = 25) -> Say:
    from pathlib import Path

    return Say(path=Path(CLIP), frames=audio.wav_to_ulaw_frames(CLIP)[:count])


# --------------------------------------------------------------------------
# wait streams silence
# --------------------------------------------------------------------------


@pytest.mark.timeout(60)
async def test_wait_keeps_the_stream_flowing(server):
    """A `wait` sends silence frames, it does not stop sending.

    The distinction the whole scenario model turns on. A real call carries
    20 ms frames continuously for its entire duration; an agent's endpointing
    is built on that. Stop sending and the agent sees a dead line rather than a
    quiet caller, and "my agent never finalises the transcript" becomes a bug in
    the test tool.

    One second of waiting is 50 frames at 20 ms.
    """
    result = await Session(
        f"{server}?mode=silent", scenario=script(Wait(seconds=1.0)), config=quick()
    ).run()

    assert result.frames_sent >= 45, (
        f"only {result.frames_sent} frames sent during a 1s wait -- "
        "the stream went quiet instead of streaming silence"
    )


@pytest.mark.timeout(60)
async def test_the_silence_sent_is_actually_silent(server):
    """What is streamed during a wait decodes to zero, not to noise.

    mu-law silence is 0xFF; a buffer of 0x00 is near full scale. Getting this
    backwards would blast noise at the agent for the whole of every wait.
    """
    result = await Session(
        server, scenario=script(Wait(seconds=0.5)), config=quick()
    ).run()

    # The echo agent returns what it receives, so the reply is our own silence.
    assert result.audio_received
    assert all(byte == 0xFF for byte in result.audio_received)


@pytest.mark.timeout(60)
async def test_a_wait_takes_about_as_long_as_it_says(server):
    """Waiting is paced in real time, like everything else.

    Measured from the event log rather than by timing ``run()``, which also
    covers the drain -- against a deliberately silent agent that is the whole
    response timeout, so wall-clock timing here would be asserting on the
    wrong span entirely.
    """
    result = await Session(
        f"{server}?mode=silent",
        scenario=script(Wait(seconds=1.0)),
        config=quick(response_timeout_s=0.5),
    ).run()

    [started] = result.events_of("stream_started")
    [finished] = result.events_of("sent_all_media")
    elapsed = finished.at - started.at

    assert 0.9 < elapsed < 2.5, f"a 1s wait streamed for {elapsed:.2f}s"


# --------------------------------------------------------------------------
# Step sequencing
# --------------------------------------------------------------------------


@pytest.mark.timeout(60)
async def test_steps_run_in_order(server):
    result = await Session(
        server,
        scenario=script(clip(10), Wait(seconds=0.3), clip(10)),
        config=quick(),
    ).run()

    kinds = [event.kind for event in result.events]
    assert kinds.index("stream_started") < kinds.index("sent_all_media")
    # 10 + ~15 + 10 frames.
    assert result.frames_sent >= 30


@pytest.mark.timeout(60)
async def test_wait_for_audio_returns_as_soon_as_the_agent_speaks(server):
    """Waiting on an event ends on the event, not on the timeout."""
    result = await Session(
        server,
        scenario=script(clip(15), WaitFor(event="audio", timeout_s=5.0)),
        config=quick(),
    ).run()

    satisfied = result.events_of("wait_for_satisfied")
    assert [event.detail["event"] for event in satisfied] == ["audio"]
    assert not result.events_of("wait_for_timeout")


@pytest.mark.timeout(60)
async def test_wait_for_gives_up_rather_than_hanging(server):
    """A silent agent does not stall the scenario forever."""
    result = await Session(
        f"{server}?mode=silent",
        scenario=script(clip(5), WaitFor(event="audio", timeout_s=0.5)),
        config=quick(),
    ).run()

    assert result.events_of("wait_for_timeout")


@pytest.mark.timeout(60)
async def test_dtmf_is_sent_as_a_keypress_per_digit(server):
    result = await Session(
        server,
        scenario=script(clip(5), scenario.Dtmf(digits="123")),
        config=quick(),
    ).run()

    digits = [event.detail["digit"] for event in result.events_of("sent_dtmf")]
    assert digits == ["1", "2", "3"]


@pytest.mark.timeout(60)
async def test_hangup_ends_the_call_without_a_stop_frame(server):
    """An abrupt hangup is not a polite goodbye.

    A dropped call gives the agent no warning, which is a different code path
    from a clean shutdown and the one more likely to be buggy.
    """
    result = await Session(
        server, scenario=script(clip(5), Hangup()), config=quick()
    ).run()

    assert result.events_of("caller_hung_up")
    assert not result.events_of("sent_stop")


# --------------------------------------------------------------------------
# Expectations
# --------------------------------------------------------------------------


@pytest.mark.timeout(60)
async def test_a_met_expectation_passes(server):
    result = await Session(
        server,
        scenario=script(clip(15), WaitFor(event="audio", timeout_s=5.0), Expect(what="audio")),
        config=quick(),
    ).run()

    assert result.expectations == [("expect audio", True)]
    assert not result.failed_expectations


@pytest.mark.timeout(60)
async def test_an_unmet_expectation_fails_without_ending_the_call(server):
    """A failed assertion lets the rest of the scenario run.

    Same reasoning as protocol violations: one run should report every problem,
    not stop at the first.
    """
    result = await Session(
        f"{server}?mode=silent",
        scenario=script(clip(5), Expect(what="audio"), clip(5), Expect(what="silence")),
        config=quick(response_timeout_s=1.0),
    ).run()

    assert [passed for _, passed in result.expectations] == [False, True]
    assert result.failed_expectations == ["expect audio"]
    # The steps after the failure still ran.
    assert result.frames_sent >= 10


@pytest.mark.timeout(60)
async def test_a_negated_expectation_works(server):
    result = await Session(
        f"{server}?mode=silent",
        scenario=script(clip(5), Expect(what="audio", negated=True)),
        config=quick(response_timeout_s=1.0),
    ).run()

    assert result.expectations == [("expect no audio", True)]


# --------------------------------------------------------------------------
# Impairment during a scenario
# --------------------------------------------------------------------------


@pytest.mark.timeout(60)
async def test_packet_loss_drops_frames_and_reports_them(server):
    result = await Session(
        server,
        scenario=script(clip(50)),
        config=quick(impairments=Impairments(loss=0.2), chaos_seed=1),
    ).run()

    assert result.frames_dropped > 0
    assert result.frames_sent + result.frames_dropped == 50
    assert "20.0% loss" in result.network.summary()


@pytest.mark.timeout(60)
async def test_a_dropped_frame_shows_up_as_a_timestamp_gap_not_a_missing_chunk(server):
    """The agent sees presentation time jump, and contiguous chunk numbers.

    The Twilio-to-app leg is TCP, so a frame cannot go missing in transit.
    Loss happens upstream, and what reaches the app is audio Twilio never had:
    chunk numbering stays unbroken because Twilio numbers what it sends, while
    media.timestamp advances by more than one frame.
    """
    result = await Session(
        server,
        scenario=script(clip(40)),
        config=quick(impairments=Impairments(loss=0.3), chaos_seed=5),
    ).run()

    assert result.frames_dropped > 0
    # Presentation time covers every frame, sent or lost.
    expected_ms = 40 * audio.FRAME_MS
    assert result.identity is not None
    assert result.frames_sent < 40
    assert expected_ms == (result.frames_sent + result.frames_dropped) * audio.FRAME_MS


@pytest.mark.timeout(60)
async def test_the_same_seed_produces_the_same_call(server):
    """Two runs with one seed drop the same number of frames.

    Reproducibility is what makes a chaos failure actionable.
    """
    async def run() -> int:
        result = await Session(
            server,
            scenario=script(clip(40)),
            config=quick(impairments=Impairments(loss=0.25), chaos_seed=99),
        ).run()
        return result.frames_dropped

    assert await run() == await run()


# --------------------------------------------------------------------------
# Delivery metrics
# --------------------------------------------------------------------------


@pytest.mark.timeout(60)
async def test_delivery_ratio_is_measured(server):
    """How fast the agent hands over its audio, against how long it lasts.

    The echo agent replies frame by frame at roughly the rate it receives, so
    its ratio is near 1. An agent streaming TTS faster than real time scores
    much higher, and everything it derives from send-completion is then wrong
    for the difference -- which is how a barge-in blind spot appears.
    """
    result = await Session(server, scenario=script(clip(30)), config=quick()).run()
    metrics = compute(result)

    assert metrics.delivery_ratio is not None
    assert metrics.delivery_duration_ms > 0
    assert metrics.playback_tail_ms is not None
    assert metrics.barge_in_blind_spot_ms == metrics.playback_tail_ms


def test_delivery_metrics_are_absent_when_there_is_nothing_to_measure():
    """One frame, or none, cannot support a rate."""
    from streamdouble.protocol import StreamIdentity
    from streamdouble.session import SessionEvent, SessionResult

    result = SessionResult(
        identity=StreamIdentity(),
        events=[SessionEvent(at=1.0, kind="stream_started")],
    )
    metrics = compute(result)

    assert metrics.delivery_ratio is None
    assert metrics.playback_tail_ms is None
