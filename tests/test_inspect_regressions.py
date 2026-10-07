"""Regression tests for `streamdouble inspect`.

Each test covers a behaviour the command used to get wrong.

1. Clear and gap counts were read off lists capped at 50, so 120 clears
   rendered as "clears 50" and gaps had no total at all.
2. Times were raw perf_counter readings: "first caller audio 87842.778s".
3. A file with no frame records -- empty, or the wrong file entirely -- read
   as "agent spoke no" and exited 0: missing data presented as a verdict.
"""

from __future__ import annotations

import json
from pathlib import Path

from streamdouble import cli, tracereport


def _lines(records):
    return [json.dumps(r) for r in records]


# 1 -------------------------------------------------------------------------


def test_clears_past_the_listing_cap_are_all_counted():
    records = [{"t": 100.0 + i, "dir": "in", "event": "clear"} for i in range(120)]
    summary = tracereport.summarise(_lines(records))

    assert summary.clear_count == 120
    assert "clears              120" in tracereport.render(summary)
    assert summary.to_dict()["clear_count"] == 120


def test_a_clear_without_a_usable_time_is_still_counted():
    summary = tracereport.summarise(_lines([{"t": "late", "dir": "in", "event": "clear"}]))
    assert summary.clear_count == 1


def test_gaps_past_the_listing_cap_are_all_counted():
    # 80 frames two seconds apart: 79 gaps, more than the 50 listed.
    records = [{"t": 2.0 * i, "dir": "in", "event": "media"} for i in range(80)]
    summary = tracereport.summarise(_lines(records))

    assert summary.media_gap_count == 79
    assert len(summary.media_gaps) == 50
    assert summary.to_dict()["media_gap_count"] == 79
    assert "agent audio gaps    79" in tracereport.render(summary)


# 2 -------------------------------------------------------------------------


def test_times_are_shown_relative_to_the_first_record():
    summary = tracereport.summarise(_lines([
        {"t": 87842.0, "dir": "out", "event": "connected"},
        {"t": 87842.5, "dir": "out", "event": "media"},
        {"t": 87842.75, "dir": "in", "event": "media"},
        {"t": 87845.0, "dir": "in", "event": "media"},
    ]))
    rendered = tracereport.render(summary)

    assert "87842" not in rendered
    assert "first caller audio  0.500s" in rendered
    assert "first agent audio   0.750s" in rendered
    assert "after 0.750s" in rendered

    data = summary.to_dict()
    assert data["first_media_out_s"] == 0.5
    assert data["media_gaps"][0]["after_s"] == 0.75


# 3 -------------------------------------------------------------------------


def test_an_empty_file_is_not_a_silent_agent(tmp_path: Path, capsys):
    path = tmp_path / "t.jsonl"
    path.write_text("", encoding="utf-8")

    assert cli.main(["inspect", str(path)]) == cli.EXIT_USAGE
    captured = capsys.readouterr()
    assert "agent spoke" not in captured.out
    assert "no trace records" in captured.err


def test_the_wrong_file_is_not_a_silent_agent(tmp_path: Path, capsys):
    path = tmp_path / "reply.wav"
    path.write_bytes(b"RIFF\x24\x08\x00\x00WAVEfmt " + bytes(range(256)) * 4)

    assert cli.main(["inspect", "--json", str(path)]) == cli.EXIT_USAGE
    assert json.loads(capsys.readouterr().out)["spoke"] is None


def test_a_real_silent_agent_still_reads_as_silent():
    """The fix must not swallow the case it was confused with."""
    summary = tracereport.summarise(_lines([{"t": 1.0, "dir": "out", "event": "media"}]))
    assert not summary.empty
    assert summary.to_dict()["spoke"] is False
    assert "agent spoke         no" in tracereport.render(summary)
