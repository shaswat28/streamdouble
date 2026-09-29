"""Regression tests for review gate 13 (phase 12c, the full review's fixes).

Each test fails against bd0a92f, the commit the gate reviewed.

1. ``wait: .nan`` and ``.inf`` were refused, but a huge finite wait was not:
   ``wait: 1e308`` overflowed converting seconds to frames, mid-call.
2. The protocol docstrings still described one shared timestamp clock. No
   test; the text was corrected.
3. ``_stream_silence`` built a list of every frame of a wait up front. An
   efficiency fix with no behaviour to assert beyond 1 and the suite.
4. On a fork, wire order within a tick depended on whether any impairment was
   set: with ``--latency-ms`` the agent's frame went first, otherwise second.
5. ``MediaStreamEncoder.stream_time_ms`` came to mean the inbound track only,
   so an outbound-only stream read 0 however far it had run.
"""

from __future__ import annotations

import pytest

from streamdouble import audio
from streamdouble.chaos import Impairments
from streamdouble.protocol import TRACK_OUTBOUND, MediaStreamEncoder
from streamdouble.scenario import ScenarioError
from streamdouble.scenario import parse as parse_scenario
from test_full_review import _fork_capture

# 1 -------------------------------------------------------------------------


@pytest.mark.parametrize("seconds", [1e308, 1e10, 24 * 60 * 60 + 1])
def test_a_wait_longer_than_a_day_is_refused_before_the_call(seconds):
    with pytest.raises(ScenarioError):
        parse_scenario({"name": "long", "steps": [{"wait": seconds}]})


def test_a_day_is_still_allowed():
    parse_scenario({"name": "long", "steps": [{"wait": 24 * 60 * 60}]})


# 4 -------------------------------------------------------------------------


@pytest.mark.timeout(30)
async def test_fork_wire_order_does_not_depend_on_impairments():
    clean = [m["track"] for m in await _fork_capture(Impairments())]
    delayed = [m["track"] for m in await _fork_capture(Impairments(latency_ms=5))]

    assert clean == delayed
    assert clean[0] == TRACK_OUTBOUND


# 5 -------------------------------------------------------------------------


def test_stream_time_counts_any_track():
    encoder = MediaStreamEncoder()
    encoder.connected()
    encoder.start(tracks=[TRACK_OUTBOUND])
    encoder.media(audio.silence_frame(), track=TRACK_OUTBOUND)

    assert encoder.stream_time_ms == audio.FRAME_MS
