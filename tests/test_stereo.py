"""Stereo recording: caller left, agent right, placed at arrival times.

The alignment convention is the whole content of this file. Concatenating the
agent's frames instead of placing them is easy, produces a file that sounds
fine, and would invert this project's best-known finding: an agent that sent
9.5 seconds of speech in 0.85 seconds would be rendered as a smooth, perfectly
timed reply -- a picture of the opposite of the bug.

So the tests here mostly assert *where* audio sits, not that audio exists.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from streamdouble import api, audio, g711
from streamdouble.session import SessionConfig

ASYNC = pytest.mark.asyncio(loop_scope="module")

FAST = SessionConfig(response_timeout_s=5.0, quiet_period_s=0.3, max_drain_s=3.0)


def read_stereo(path: Path) -> np.ndarray:
    with wave.open(str(path)) as wav:
        assert wav.getnchannels() == 2, "not a stereo file"
        assert wav.getframerate() == audio.SAMPLE_RATE
        assert wav.getsampwidth() == 2
        frames = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
    return frames.reshape(-1, 2)


def span(channel: np.ndarray) -> tuple[float, float]:
    """First and last non-silent instant, in seconds."""
    nonzero = np.nonzero(channel)[0]
    if not len(nonzero):
        return (0.0, 0.0)
    return (nonzero[0] / audio.SAMPLE_RATE, nonzero[-1] / audio.SAMPLE_RATE)


def tone(seconds: float, level: int = 8000) -> bytes:
    return g711.encode(np.full(int(seconds * audio.SAMPLE_RATE), level, dtype=np.int16))


# ---------------------------------------------------------------------------
# Placement -- unit level, where the arithmetic is exact
# ---------------------------------------------------------------------------


def test_audio_is_placed_at_its_arrival_instant(tmp_path: Path):
    out = tmp_path / "s.wav"
    audio.write_stereo_wav(tone(1.0), [(0.5, tone(0.2, 6000))], out)

    channels = read_stereo(out)
    start, end = span(channels[:, 1])

    assert start == pytest.approx(0.5, abs=0.01), "the agent's audio moved"
    assert end == pytest.approx(0.7, abs=0.01)


def test_gaps_between_segments_are_real_silence(tmp_path: Path):
    """Two bursts a second apart must stay a second apart.

    Concatenation would put them back to back, which is exactly the rendering
    that hides a front-loading agent.
    """
    out = tmp_path / "s.wav"
    audio.write_stereo_wav(
        tone(3.0), [(0.0, tone(0.2, 6000)), (2.0, tone(0.2, 6000))], out
    )

    right = read_stereo(out)[:, 1]
    middle = right[int(0.5 * audio.SAMPLE_RATE) : int(1.9 * audio.SAMPLE_RATE)]

    assert np.count_nonzero(middle) == 0, "the gap between bursts was not preserved"


def test_a_front_loading_agent_looks_front_loaded(tmp_path: Path):
    """The case this convention exists for.

    Nine seconds of audio delivered in under one. Placed at arrival times the
    file shows a burst near the start; concatenated it would show nine seconds
    of continuous speech and look entirely healthy.
    """
    out = tmp_path / "s.wav"
    burst = [(i * 0.02, tone(0.02, 6000)) for i in range(45)]  # 0.9s of arrivals
    audio.write_stereo_wav(tone(9.0), burst, out)

    right = read_stereo(out)[:, 1]
    _, end = span(right)

    assert end < 1.5, (
        f"the agent's audio spans {end:.2f}s of the file; delivered in 0.9s it "
        "should not look like a nine-second reply"
    )


def test_overlapping_segments_are_summed_not_dropped(tmp_path: Path):
    """Discarding audio to keep the arithmetic tidy is the same dishonesty small."""
    out = tmp_path / "s.wav"
    audio.write_stereo_wav(tone(1.0), [(0.0, tone(0.5, 4000)), (0.0, tone(0.5, 4000))], out)

    right = read_stereo(out)[:, 1]
    assert abs(int(right[100])) > 4000, "the overlap was dropped rather than summed"


def test_summing_does_not_wrap_around(tmp_path: Path):
    """int16 overflow would turn loud audio into loud noise of the wrong sign."""
    out = tmp_path / "s.wav"
    loud = tone(0.3, 30000)
    audio.write_stereo_wav(tone(1.0), [(0.0, loud), (0.0, loud), (0.0, loud)], out)

    right = read_stereo(out)[:, 1]
    assert right.max() > 0, "clipping produced a negative sample: the sum wrapped"
    assert right.max() <= 32767


def test_the_left_channel_is_padded_not_truncated(tmp_path: Path):
    """A long reply must not cut the caller's channel short."""
    out = tmp_path / "s.wav"
    audio.write_stereo_wav(tone(0.5), [(0.0, tone(3.0, 6000))], out)

    channels = read_stereo(out)
    assert len(channels) / audio.SAMPLE_RATE == pytest.approx(3.0, abs=0.01)
    assert span(channels[:, 0])[1] == pytest.approx(0.5, abs=0.01)


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


@ASYNC
async def test_a_real_call_records_both_sides(server, speech_8k_path, tmp_path: Path):
    report = await api.call(server, audio_path=speech_8k_path, config=FAST)
    caller = b"".join(audio.wav_to_ulaw_frames(speech_8k_path))

    out = tmp_path / "call.wav"
    assert api.save_stereo(report, caller, out) is True

    channels = read_stereo(out)
    assert np.count_nonzero(channels[:, 0]), "the caller's channel is empty"
    assert np.count_nonzero(channels[:, 1]), "the agent's channel is empty"


@ASYNC
async def test_the_agent_channel_starts_when_the_agent_did(
    server, speech_8k_path, tmp_path: Path
):
    """The recording and the reported latency must tell the same story.

    Two renderings of the same call that disagree about when the agent spoke
    would make both untrustworthy.
    """
    report = await api.call(
        f"{server}?delay_ms=600", audio_path=speech_8k_path, config=FAST
    )
    assert report.time_to_first_audio_ms is not None

    caller = b"".join(audio.wav_to_ulaw_frames(speech_8k_path))
    out = tmp_path / "call.wav"
    api.save_stereo(report, caller, out)

    start, _ = span(read_stereo(out)[:, 1])
    reported = report.time_to_first_audio_ms / 1000

    assert start == pytest.approx(reported, abs=0.1), (
        f"the recording has the agent starting at {start:.2f}s but the metrics "
        f"report {reported:.2f}s"
    )


@ASYNC
async def test_nothing_is_written_when_the_agent_was_silent(
    server, speech_8k_path, tmp_path: Path
):
    """A file containing only the caller looks like a broken recorder."""
    report = await api.call(
        f"{server}?mode=silent",
        audio_path=speech_8k_path,
        config=SessionConfig(response_timeout_s=2.0, quiet_period_s=0.3, max_drain_s=2.0),
    )

    out = tmp_path / "call.wav"
    assert api.save_stereo(report, b"", out) is False
    assert not out.exists()
