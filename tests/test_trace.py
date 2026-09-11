"""The frame trace: what it records, and what it refuses to record.

The redaction tests here are shaped deliberately. Each asserts that the
parameter *key* is present and that its *value* is hidden -- not merely that
the secret is absent from the file. Absence alone is satisfied by a trace that
never found the field at all, which is exactly the bug this file was written
after: `customParameters` is nested inside `start`, the first implementation
read it from the top level, and so it wrote no parameters and ran no redaction
while a "does the secret appear" check passed cleanly.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import median

import pytest

from streamdouble import api
from streamdouble.session import SessionConfig
from streamdouble.trace import REDACTED, Trace, TraceConfig

pytestmark = pytest.mark.asyncio(loop_scope="module")

FAST = SessionConfig(response_timeout_s=3.0, quiet_period_s=0.3, max_drain_s=3.0)

SECRET = "hunter2-do-not-write-this-down"

#: Calls per side in the parity test. Three is enough for a median to be
#: steadier than a single sample without making the test slow.
RUNS_PER_SIDE = 3


def read_trace(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


async def test_a_trace_records_both_directions(server, speech_8k_path, tmp_path: Path):
    out = tmp_path / "t.jsonl"
    report = await api.call(
        server, audio_path=speech_8k_path, config=FAST, trace=TraceConfig(path=out)
    )

    records = read_trace(out)
    assert records, "the trace is empty"

    directions = {record["dir"] for record in records}
    assert directions == {"out", "in"}, f"expected both directions, got {directions}"

    events_out = {r["event"] for r in records if r["dir"] == "out"}
    assert {"connected", "start", "media"} <= events_out

    # Every frame this process sent is accounted for, plus connected/start/stop.
    sent_media = sum(1 for r in records if r["dir"] == "out" and r["event"] == "media")
    assert sent_media == report.metrics.frames_sent

    assert report.trace_path == out


async def test_the_trace_path_is_reported_on_the_report(server, speech_8k_path, tmp_path: Path):
    out = tmp_path / "nested" / "deeper" / "t.jsonl"
    await api.call(server, audio_path=speech_8k_path, config=FAST, trace=TraceConfig(path=out))
    assert out.exists(), "the trace did not create its parent directories"


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------


async def test_payloads_are_excluded_by_default(server, speech_8k_path, tmp_path: Path):
    out = tmp_path / "t.jsonl"
    await api.call(server, audio_path=speech_8k_path, config=FAST, trace=TraceConfig(path=out))

    media = [r for r in read_trace(out) if r.get("event") == "media"]
    assert media
    assert all("payload" not in r for r in media), "base64 audio leaked into the trace"
    # The digest stands in for the bytes, so two traces remain comparable.
    assert all(len(r["sha256_8"]) == 8 for r in media)


async def test_payloads_can_be_opted_into(server, speech_8k_path, tmp_path: Path):
    out = tmp_path / "t.jsonl"
    await api.call(
        server,
        audio_path=speech_8k_path,
        config=FAST,
        trace=TraceConfig(path=out, payloads=True),
    )

    media = [r for r in read_trace(out) if r.get("event") == "media"]
    assert media
    assert all("payload" in r for r in media)


# ---------------------------------------------------------------------------
# Redaction -- see the module docstring for why these assert presence too
# ---------------------------------------------------------------------------


async def test_custom_parameters_are_redacted_by_default(server, speech_8k_path, tmp_path: Path):
    out = tmp_path / "t.jsonl"
    await api.call(
        server,
        audio_path=speech_8k_path,
        config=SessionConfig(
            response_timeout_s=3.0,
            quiet_period_s=0.3,
            max_drain_s=3.0,
            custom_parameters={"authToken": SECRET, "tenant": "acme"},
        ),
        trace=TraceConfig(path=out),
    )

    start = next(r for r in read_trace(out) if r.get("event") == "start")

    # Presence: the trace really did look at the parameters.
    assert "customParameters" in start, "the trace never recorded customParameters at all"
    assert set(start["customParameters"]) == {"authToken", "tenant"}

    # And having looked, it hid the values.
    assert start["customParameters"]["authToken"] == REDACTED
    assert start["customParameters"]["tenant"] == REDACTED

    assert SECRET not in out.read_text(encoding="utf-8")


async def test_secrets_can_be_opted_into(server, speech_8k_path, tmp_path: Path):
    out = tmp_path / "t.jsonl"
    await api.call(
        server,
        audio_path=speech_8k_path,
        config=SessionConfig(
            response_timeout_s=3.0,
            quiet_period_s=0.3,
            max_drain_s=3.0,
            custom_parameters={"authToken": SECRET},
        ),
        trace=TraceConfig(path=out, secrets=True),
    )

    start = next(r for r in read_trace(out) if r.get("event") == "start")
    assert start["customParameters"]["authToken"] == SECRET


# ---------------------------------------------------------------------------
# The cases a trace exists for
# ---------------------------------------------------------------------------


async def test_an_unparseable_frame_is_still_traced(server, speech_8k_path, tmp_path: Path):
    """The most valuable line in a trace is the one that did not parse.

    A trace that recorded only well-formed frames would omit precisely the
    frame someone is filing a bug about.
    """
    out = tmp_path / "t.jsonl"
    await api.call(
        f"{server}?garbage_after=5",
        audio_path=speech_8k_path,
        config=FAST,
        trace=TraceConfig(path=out),
    )

    records = read_trace(out)
    unparseable = [r for r in records if r.get("unparseable")]
    assert unparseable, "a malformed frame was dropped from the trace"
    assert "excerpt" in unparseable[0]


async def test_a_trace_survives_a_call_that_ended_badly(server, speech_8k_path, tmp_path: Path):
    """Flushed from a `finally`.

    A call that ended badly is the one whose trace is worth having, so a
    diagnostic that only survives success is no diagnostic at all.
    """
    out = tmp_path / "t.jsonl"
    with pytest.raises(api.ConnectionFailed):
        await api.call(
            "ws://127.0.0.1:1/nope",
            audio_path=speech_8k_path,
            config=FAST,
            trace=TraceConfig(path=out),
        )

    assert out.exists(), "no trace was written for a failed call"


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


async def test_a_shared_config_is_not_mutated(server, speech_8k_path, tmp_path: Path):
    """Two calls sharing one SessionConfig must not share one trace file.

    `SessionConfig` is routinely defined once and reused -- this suite does it
    at module level -- so attaching the recorder to the caller's object would
    make the second call write into the first call's trace.
    """
    shared = SessionConfig(response_timeout_s=3.0, quiet_period_s=0.3, max_drain_s=3.0)

    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"

    await api.call(server, audio_path=speech_8k_path, config=shared, trace=TraceConfig(path=first))
    await api.call(server, audio_path=speech_8k_path, config=shared, trace=TraceConfig(path=second))

    assert shared.trace is None, "the caller's config was mutated"
    assert first.exists() and second.exists()

    # Each holds one call, not one empty and one doubled.
    first_starts = sum(1 for r in read_trace(first) if r.get("event") == "start")
    second_starts = sum(1 for r in read_trace(second) if r.get("event") == "start")
    assert first_starts == 1
    assert second_starts == 1


async def test_flush_reports_how_many_records_it_wrote(tmp_path: Path):
    """A pure unit check -- no socket, because none is needed.

    Declared `async` only so the module-wide asyncio marker applies; nothing
    in it awaits anything.
    """
    recorder = Trace(config=TraceConfig(path=tmp_path / "t.jsonl"))
    recorder.note_out({"event": "connected"}, 1.0)
    recorder.note_out({"event": "start", "start": {"customParameters": {"k": "v"}}}, 2.0)

    assert recorder.flush() == 2
    assert len(read_trace(tmp_path / "t.jsonl")) == 2


# ---------------------------------------------------------------------------
# The observer must not perturb the observed
# ---------------------------------------------------------------------------


async def test_tracing_does_not_change_the_measurement(server, speech_8k_path, tmp_path: Path):
    """A traced run and an untraced run must report the same latency.

    This is the design claim `trace.py` is built around, and the reason nothing
    is written to disk during a call. It is also a bug this project has already
    had once in a different guise: gate 2 found quadratic audio accumulation
    running inside the receive loop, which did not merely make long calls slow
    -- it delayed frame handling and inflated the very latency figures the tool
    exists to report, worse the longer the call, and plausible at every point.

    Medians of several runs each way, not one call against one call. The first
    version of this test compared a single pair and was flaky: two calls over a
    real socket differ by tens of milliseconds for reasons that have nothing to
    do with tracing, so a single pair is a noisy estimator of a systematic
    effect. It passed alone and failed in sequence, which is the worst way for
    a test to behave -- and by this project's own standard a flaky detector is
    worse than none, because people learn to re-run it.

    What would still fail here is what the test is for: a per-frame cost that
    shifts the median rather than one sample.
    """
    plain: list[float] = []
    traced: list[float] = []

    for run in range(RUNS_PER_SIDE):
        bare = await api.call(server, audio_path=speech_8k_path, config=FAST)
        assert bare.time_to_first_audio_ms is not None
        plain.append(bare.time_to_first_audio_ms)

        recorded = await api.call(
            server,
            audio_path=speech_8k_path,
            config=FAST,
            trace=TraceConfig(path=tmp_path / f"t{run}.jsonl"),
        )
        assert recorded.time_to_first_audio_ms is not None
        traced.append(recorded.time_to_first_audio_ms)

    difference = abs(median(traced) - median(plain))
    assert difference < 100, (
        f"tracing shifted median time-to-first-audio by {difference:.1f} ms "
        f"(untraced {median(plain):.1f}, traced {median(traced):.1f}; "
        f"untraced runs {[round(v, 1) for v in plain]}, "
        f"traced runs {[round(v, 1) for v in traced]}) -- the trace is being "
        "written inside the loop it is measuring"
    )


async def test_nothing_is_written_until_the_call_is_over(server, speech_8k_path, tmp_path: Path):
    """The file does not exist mid-call, which is the mechanism behind the above.

    Asserting the mechanism as well as the effect, because the timing test has
    a loose tolerance by necessity and could pass a small per-frame write. This
    one cannot: either the file appears during the call or it does not.
    """
    trace_path = tmp_path / "t.jsonl"
    seen_during_call: list[bool] = []

    original = Trace.note_out

    def spy(self, frame, at):
        seen_during_call.append(trace_path.exists())
        return original(self, frame, at)

    Trace.note_out = spy
    try:
        await api.call(
            server, audio_path=speech_8k_path, config=FAST, trace=TraceConfig(path=trace_path)
        )
    finally:
        Trace.note_out = original

    assert seen_during_call, "no frames were sent, so this proved nothing"
    assert not any(seen_during_call), "the trace file existed while the call was still running"
    assert trace_path.exists(), "the trace was never written at all"
