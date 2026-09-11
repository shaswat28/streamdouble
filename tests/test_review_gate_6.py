"""Regression tests for what review gate 6 found.

Gate 6 covered the API extraction, the frame trace and the pytest plugin. Four
findings, and the two worst are repeats: this project had already found and
fixed both shapes in earlier gates, and they came back in new code written by
someone who knew about them.

That is the argument for the gates existing, so it is worth being explicit
about which is which:

* **Cleanup deciding how a call ended.** Gate 2 found a `finally` that raised
  and discarded the `ConnectionClosed` which was the real problem. Gate 6 found
  `trace.flush()` in a `finally` doing exactly the same thing.
* **Unbounded growth per call.** Gate 4 measured 206 MB of buffered audio and
  436 MB peak from a flooding endpoint, and capped the audio and the event log.
  The trace arrived afterwards with no cap at all.

Every test here was verified to fail against the behaviour it describes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from streamdouble import api
from streamdouble.session import SessionConfig
from streamdouble.trace import MAX_TRACE_RECORDS, Trace, TraceConfig

pytestmark = pytest.mark.asyncio(loop_scope="module")

FAST = SessionConfig(response_timeout_s=3.0, quiet_period_s=0.3, max_drain_s=3.0)


# ---------------------------------------------------------------------------
# Finding 1 -- a failing trace must not decide how the call ended
# ---------------------------------------------------------------------------


async def test_an_unwritable_trace_does_not_destroy_a_successful_call(
    server, speech_8k_path, tmp_path: Path, capsys
):
    """A diagnostic that cannot be written must not take the results with it.

    Reproduced before the fix: `--trace <an existing directory>` raised
    PermissionError out of the `finally`, and a call that had completely
    succeeded returned nothing at all. The user loses their latency figures,
    their threshold outcomes and their recorded audio because a side-file could
    not be opened.
    """
    a_directory = tmp_path / "somewhere"
    a_directory.mkdir()

    report = await api.call(
        server, audio_path=speech_8k_path, config=FAST, trace=TraceConfig(path=a_directory)
    )

    assert report.spoke, "the call itself should have succeeded"
    assert report.time_to_first_audio_ms is not None

    # And the failure is reported rather than swallowed: a report claiming a
    # trace exists when it does not would send someone looking for a file.
    assert report.trace_path is None
    assert "could not write the trace" in capsys.readouterr().err


async def test_an_unwritable_trace_does_not_mask_the_real_error(
    server, speech_8k_path, tmp_path: Path
):
    """The error the user sees must be the one that actually happened.

    Before the fix this raised PermissionError, pointing at the filesystem
    while the actual problem was an agent that was not running. That is gate
    2's finding exactly: cleanup replacing the real error, and debugging then
    aimed at streamdouble rather than at the thing that was wrong.
    """
    a_directory = tmp_path / "somewhere"
    a_directory.mkdir()

    with pytest.raises(api.ConnectionFailed) as failure:
        await api.call(
            "ws://127.0.0.1:1/nope",
            audio_path=speech_8k_path,
            config=FAST,
            trace=TraceConfig(path=a_directory),
        )

    assert "could not connect" in str(failure.value)


# ---------------------------------------------------------------------------
# Finding 2 -- the trace is bounded
# ---------------------------------------------------------------------------


async def test_the_trace_stops_growing_and_says_so(tmp_path: Path):
    """Bounded, and honest about it.

    A pure unit test: driving 100,000 frames over a real socket would take
    half an hour, and the property under test is about the container, not the
    network.

    The count of what was dropped stays exact, because "the trace stops at
    100,000" and "your agent sent four million frames" are different facts and
    the second is the interesting one.
    """
    recorder = Trace(config=TraceConfig(path=tmp_path / "t.jsonl"))
    overshoot = 250

    for index in range(MAX_TRACE_RECORDS + overshoot):
        recorder.note_out({"event": "media", "sequenceNumber": str(index)}, float(index))

    assert len(recorder.records) == MAX_TRACE_RECORDS
    assert recorder.dropped == overshoot

    written = recorder.flush()
    assert written == MAX_TRACE_RECORDS + 1, "the truncation note should be written too"

    lines = [json.loads(line) for line in (tmp_path / "t.jsonl").read_text().splitlines()]
    note = lines[-1]
    assert note["truncated"] is True
    assert note["dropped"] == overshoot


async def test_payload_bytes_are_capped_separately_from_record_count(tmp_path: Path):
    """The record cap does not bound memory on its own.

    Gate 4's endpoint sent few frames and enormous ones -- 400 KB each, under
    the per-frame limit, because that limit is per frame while the buffer is
    per call. A count-based cap alone would let a handful of records carry
    hundreds of megabytes, which is the same trap in a new place.
    """
    recorder = Trace(config=TraceConfig(path=tmp_path / "t.jsonl", payloads=True))

    big = "A" * (1024 * 1024)  # 1 MB of base64 per frame
    for index in range(64):
        recorder.note_out(
            {
                "event": "media",
                "sequenceNumber": str(index),
                "media": {"payload": big, "chunk": str(index), "timestamp": "0"},
            },
            float(index),
        )

    kept = [r for r in recorder.records if "payload" in r]
    omitted = [r for r in recorder.records if "payload_omitted" in r]

    assert omitted, "64 MB of payloads were all retained; the cap did nothing"
    assert len(kept) * len(big) <= 32 * 1024 * 1024 + len(big)

    # Every record still carries its digest, so a truncated trace stays usable
    # for "did the same bytes go out" comparisons.
    assert all("sha256_8" in r for r in recorder.records)


# ---------------------------------------------------------------------------
# Finding 3 -- the trace must not re-parse what was already parsed
# ---------------------------------------------------------------------------


async def test_inbound_frames_are_parsed_once_not_twice(server, speech_8k_path, tmp_path: Path):
    """Tracing must not double the JSON decode cost inside the receive loop.

    The first implementation handed `note_in` the raw message, which ran
    `json.loads` again on bytes `parse_outbound` had just decoded. Real agents
    batch audio into ~8000-byte frames and gate 3 measured JSON and base64 work
    at 0.32 ms on a large payload, so doubling it lands squarely next to the
    pacer -- the exact cost this design exists to avoid.

    Counting calls rather than timing them: a timing test for a sub-millisecond
    per-frame cost would be flaky, and the thing worth pinning is that the
    second parse does not happen at all.
    """
    import streamdouble.trace as trace_module

    calls: list[bool] = []
    original = trace_module._try_parse

    def counting(raw):
        calls.append(True)
        return original(raw)

    trace_module._try_parse = counting
    try:
        report = await api.call(
            server,
            audio_path=speech_8k_path,
            config=FAST,
            trace=TraceConfig(path=tmp_path / "t.jsonl"),
        )
    finally:
        trace_module._try_parse = original

    assert report.metrics.media_frames_received > 0, "the agent sent nothing to trace"
    assert not calls, (
        f"the trace re-parsed {len(calls)} inbound frame(s) that had already been "
        "parsed by parse_outbound"
    )


async def test_a_malformed_frame_is_still_traced_from_the_raw_message(
    server, speech_8k_path, tmp_path: Path
):
    """The one case that must still parse the raw message.

    Removing the duplicate parse must not remove the frame a trace exists for.
    A frame that failed to parse has no parsed form to hand over, so that path
    keeps working from the raw bytes.
    """
    out = tmp_path / "t.jsonl"
    await api.call(
        f"{server}?garbage_after=5",
        audio_path=speech_8k_path,
        config=FAST,
        trace=TraceConfig(path=out),
    )

    records = [json.loads(line) for line in out.read_text().splitlines()]
    assert any(r.get("unparseable") for r in records)


# ---------------------------------------------------------------------------
# Finding 4 -- do not record a frame that was never sent
# ---------------------------------------------------------------------------


async def test_a_frame_whose_send_failed_is_not_recorded_as_sent(
    server, speech_8k_path, tmp_path: Path
):
    """The last frame before a disconnect is the one a trace is opened for.

    The first implementation appended to the trace before awaiting the send, so
    a frame whose send raised appeared in the trace indistinguishable from one
    that went out. Someone reading it concludes the agent dropped audio that
    streamdouble never actually delivered -- the one place the trace was
    actively misleading rather than merely incomplete.

    Driven with an agent that hangs up mid-stream, so real sends really do
    fail.
    """
    out = tmp_path / "t.jsonl"
    report = await api.call(
        f"{server}?hangup_after=10",
        audio_path=speech_8k_path,
        config=FAST,
        trace=TraceConfig(path=out),
    )

    records = [json.loads(line) for line in out.read_text().splitlines()]
    traced_media = sum(1 for r in records if r["dir"] == "out" and r.get("event") == "media")

    # The trace must not claim more outbound media than the session counted as
    # sent. Before the fix it could exceed it by the frame that failed.
    assert traced_media <= report.metrics.frames_sent, (
        f"the trace records {traced_media} media frames sent but the session "
        f"counted {report.metrics.frames_sent}"
    )
