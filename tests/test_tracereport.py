"""`streamdouble inspect`: reading a trace back.

Two kinds of test. Round trips run a real call against the echo agent, trace it,
and check the summary against the report that call produced, so the reader
and the writer cannot drift apart unnoticed. Hostile-file tests hand-build the
traces a stranger might attach to an issue, because a trace is untrusted input
the moment it leaves the machine that wrote it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from streamdouble import api, cli, tracereport
from streamdouble.session import SessionConfig
from streamdouble.trace import TraceConfig

FAST = SessionConfig(response_timeout_s=3.0, quiet_period_s=0.3, max_drain_s=3.0)


def lines(*records: object) -> list[str]:
    return [json.dumps(r) for r in records]


# ---------------------------------------------------------------------------
# Round trips against a real call


@pytest.mark.asyncio(loop_scope="module")
async def test_a_real_trace_summarises_to_what_the_call_reported(
    server, speech_8k_path, tmp_path: Path
):
    out = tmp_path / "t.jsonl"
    report = await api.call(
        server, audio_path=speech_8k_path, config=FAST, trace=TraceConfig(path=out)
    )

    summary = tracereport.read(out)

    assert summary.malformed_lines == 0
    assert summary.events["out:media"] == report.metrics.frames_sent
    assert summary.spoke == report.spoke
    assert summary.first_media_in_t is not None
    assert summary.first_audio_after_first_send_ms is not None


@pytest.mark.asyncio(loop_scope="module")
async def test_a_silent_agent_reads_as_none_not_zero(server, speech_8k_path, tmp_path: Path):
    out = tmp_path / "t.jsonl"
    await api.call(
        f"{server}?mode=silent", audio_path=speech_8k_path, config=FAST,
        trace=TraceConfig(path=out),
    )

    summary = tracereport.read(out)

    assert not summary.spoke
    assert summary.first_media_in_t is None
    assert summary.first_audio_after_first_send_ms is None
    assert summary.to_dict()["first_audio_after_first_send_ms"] is None
    rendered = tracereport.render(summary)
    assert "agent after caller  none" in rendered
    assert "0 ms" not in rendered


# ---------------------------------------------------------------------------
# What the summary derives


def test_marks_are_matched_to_echoes_by_name():
    summary = tracereport.summarise(lines(
        {"t": 0.1, "dir": "in", "event": "mark", "mark": "a"},
        {"t": 0.2, "dir": "in", "event": "mark", "mark": "b"},
        {"t": 0.3, "dir": "out", "event": "mark", "mark": "a"},
    ))
    assert summary.unechoed_marks == ["b"]


def test_gaps_in_agent_audio_are_measured_between_frames_that_exist():
    summary = tracereport.summarise(lines(
        {"t": 1.0, "dir": "in", "event": "media"},
        {"t": 1.02, "dir": "in", "event": "media"},
        {"t": 3.02, "dir": "in", "event": "media"},
    ))
    assert len(summary.media_gaps) == 1
    t, ms = summary.media_gaps[0]
    assert t == pytest.approx(1.02)
    assert ms == pytest.approx(2000.0)


def test_an_agent_that_spoke_first_is_negative_not_clamped():
    summary = tracereport.summarise(lines(
        {"t": 0.5, "dir": "in", "event": "media"},
        {"t": 1.0, "dir": "out", "event": "media"},
    ))
    assert summary.first_audio_after_first_send_ms == pytest.approx(-500.0)


def test_sequence_breaks_are_reported_per_direction():
    summary = tracereport.summarise(lines(
        {"dir": "out", "event": "media", "seq": "1"},
        {"dir": "in", "event": "media", "seq": 7},
        {"dir": "out", "event": "media", "seq": "2"},
        {"dir": "out", "event": "media", "seq": "4"},
    ))
    assert summary.sequence_breaks == [("out", 2, 4)]


def test_the_writers_truncation_note_is_surfaced():
    summary = tracereport.summarise(lines(
        {"dir": "note", "truncated": True, "retained": 100000, "dropped": 42},
    ))
    assert summary.truncated == {"retained": 100000, "dropped": 42}
    assert "TRUNCATED" in tracereport.render(summary)


# ---------------------------------------------------------------------------
# Hostile traces


def test_garbage_is_counted_never_raised():
    summary = tracereport.summarise([
        "not json", "[1, 2]", "null", '"string"', "{", "",
        json.dumps({"dir": "sideways", "event": "media"}),
        json.dumps({"dir": "in", "event": "media", "t": True}),
        json.dumps({"dir": "in", "event": "media", "t": "soon"}),
        json.dumps({"dir": "in", "event": {"nested": 1}, "seq": "9" * 500}),
        "[" * 100_000,
    ])
    assert summary.malformed_lines == 7
    # A non-numeric time is not a time: nothing spoke *at* a moment.
    assert summary.first_media_in_t is None


def test_non_finite_times_are_ignored():
    summary = tracereport.summarise([
        '{"dir": "in", "event": "media", "t": NaN}',
        '{"dir": "in", "event": "media", "t": Infinity}',
    ])
    assert summary.first_media_in_t is None


def test_an_oversized_line_is_skipped_without_parsing(tmp_path: Path):
    path = tmp_path / "t.jsonl"
    huge = '{"dir": "in", "event": "media", "t": 1.0, "pad": "' + "x" * (
        tracereport.MAX_LINE_BYTES + 10
    ) + '"}'
    path.write_text(
        huge + "\n" + json.dumps({"dir": "in", "event": "media", "t": 2.0}) + "\n",
        encoding="utf-8",
    )
    summary = tracereport.read(path)
    assert summary.oversized_lines == 1
    assert summary.first_media_in_t == 2.0


def test_a_file_with_no_newlines_does_not_become_one_line(tmp_path: Path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(b"x" * (tracereport.MAX_LINE_BYTES * 3 + 5))
    summary = tracereport.read(path)
    assert summary.oversized_lines == 1
    assert summary.records == 0


def test_non_utf8_bytes_are_malformed_not_fatal(tmp_path: Path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(b"\xff\xfe\x00garbage\n")
    assert tracereport.read(path).malformed_lines == 1


def test_reading_stops_at_the_line_cap(monkeypatch):
    monkeypatch.setattr(tracereport, "MAX_LINES", 3)
    summary = tracereport.summarise(["{}"] * 10)
    assert summary.lines == 3
    assert summary.stopped_early


# ---------------------------------------------------------------------------
# The command


def test_inspect_prints_a_summary(tmp_path: Path, capsys):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(lines({"t": 0.0, "dir": "out", "event": "media"})), encoding="utf-8")
    assert cli.main(["inspect", str(path)]) == cli.EXIT_OK
    assert "agent spoke         no" in capsys.readouterr().out


def test_inspect_json_is_parseable(tmp_path: Path, capsys):
    path = tmp_path / "t.jsonl"
    path.write_text("", encoding="utf-8")
    assert cli.main(["inspect", "--json", str(path)]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["spoke"] is False


def test_inspect_a_missing_file_is_a_usage_error(tmp_path: Path, capsys):
    assert cli.main(["inspect", str(tmp_path / "nope.jsonl")]) == cli.EXIT_USAGE
    assert "cannot read" in capsys.readouterr().err


def test_inspect_rejects_a_nonsense_gap(tmp_path: Path):
    path = tmp_path / "t.jsonl"
    path.write_text("", encoding="utf-8")
    assert cli.main(["inspect", "--gap-ms", "nan", str(path)]) == cli.EXIT_USAGE
