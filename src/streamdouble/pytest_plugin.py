"""A pytest plugin, so voice-agent tests can live where tests already live.

Registered through ``[project.entry-points.pytest11]``, so installing this
package is enough -- there is no ``pytest_plugins`` line to add.

The whole point is that testing a voice agent should look like testing
anything else::

    async def test_the_agent_answers_quickly(simulated_call):
        report = await simulated_call(AGENT_URL, audio="hello.wav")
        assert report.time_to_first_audio_ms < 800

Two things this plugin deliberately does not do.

**It does not start your application.** The fixture takes a URL. Anything more
would mean knowing how to boot FastAPI, Flask, Node, and whatever else, and
that is a web-framework fixture library rather than a Twilio simulator. Start
your app however your suite already starts it.

**It does not judge your agent.** There is no ``assert_transcript_contains``,
and there will not be. That is LLM-as-judge, it is the first item on this
project's non-goals list, and other tools do it well. What you get here are the
transport facts -- timing, framing, protocol conformance -- and your own
assertions about them.

One piece of real work happens here beyond plumbing: an assertion on a
``None`` latency produces a useful failure instead of ``TypeError: '<' not
supported between instances of 'NoneType' and 'int'``. An agent that never
spoke is the single most common failure a new user hits, and the default
message tells them nothing about why.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from . import api
from .session import SessionConfig
from .trace import TraceConfig

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable

    from .api import CallReport

__all__ = ["simulated_call", "streamdouble_config"]


@pytest.fixture
def streamdouble_config() -> SessionConfig:
    """Defaults for every call in this suite. Override it in your conftest.

    The shipped defaults are tuned for a test suite rather than for a patient
    human: a real call waits 10 seconds for a reply and drains for 30, which is
    right at a terminal and wrong in CI, where a hung agent should fail the job
    rather than hold it open for most of a minute.

    Override it to carry whatever your agent needs -- most commonly the shared
    secret it authenticates on::

        @pytest.fixture
        def streamdouble_config():
            return SessionConfig(custom_parameters={"authToken": os.environ["TOKEN"]})
    """
    return SessionConfig(
        response_timeout_s=10.0,
        quiet_period_s=1.0,
        max_drain_s=15.0,
    )


@pytest.fixture
def simulated_call(
    streamdouble_config: SessionConfig,
    tmp_path: Path,
) -> Callable[..., Awaitable[CallReport]]:
    """Place a simulated Twilio call against a WebSocket endpoint.

    ``await simulated_call(url, audio="clip.wav")`` returns a
    :class:`~streamdouble.api.CallReport`.

    Keyword arguments:
        audio: WAV file to stream as the caller's voice.
        frames: Pre-encoded mu-law frames, instead of ``audio``.
        scenario: A scenario file or object, instead of ``audio``.
        config: Override ``streamdouble_config`` for this one call.
        trace: ``True`` writes a frame trace into pytest's ``tmp_path`` and
            names it in the failure output; a ``TraceConfig`` or a path puts it
            somewhere specific.

    Tracing is worth turning on for a test you are actively debugging. It is
    off by default because a suite that writes a trace per call accumulates
    files nobody reads.
    """

    async def place(
        url: str,
        *,
        audio: str | Path | None = None,
        frames: Any = None,
        scenario: Any = None,
        config: SessionConfig | None = None,
        thresholds: Any = None,
        trace: bool | str | Path | TraceConfig | None = None,
    ) -> CallReport:
        recorder = _trace_config(trace, tmp_path)
        effective = config or streamdouble_config

        if scenario is not None:
            if audio is not None or frames is not None:
                raise ValueError("pass scenario, or audio/frames -- not both")
            return await api.run_scenario(
                url, scenario, config=effective, thresholds=thresholds, trace=recorder
            )

        return await api.call(
            url,
            audio_path=audio,
            frames=frames,
            config=effective,
            thresholds=thresholds,
            trace=recorder,
        )

    return place


def _trace_config(
    trace: bool | str | Path | TraceConfig | None, tmp_path: Path
) -> TraceConfig | None:
    if trace is None or trace is False:
        return None
    if trace is True:
        return TraceConfig(path=tmp_path / "streamdouble-trace.jsonl")
    if isinstance(trace, TraceConfig):
        return trace
    return TraceConfig(path=Path(trace))


#: What to say when a test compares a measurement that was never taken.
#:
#: The wording matters more than it looks. This is the first failure most new
#: users see, and the moment they decide whether the tool explains anything.
#: The ``None`` is correct and deliberate -- silence is the worst outcome a
#: voice agent can have, not the fastest -- so the job is to say so, not to
#: apologise for it or soften it into a zero.
_NONE_COMPARISON_HELP = """\
This looks like a comparison against a measurement that was never taken.

streamdouble reports `None`, not 0, when the agent produced no audio at all.
There is no latency to compare because nothing was ever said. Reporting that
as a latency of zero would make the worst possible outcome -- total silence on
the line -- look like the fastest reply you have ever had, and sail through an
800 ms threshold.

Check that the agent spoke before asserting on how quickly it did:

    assert report.spoke, "the agent never answered"
    assert report.time_to_first_audio_ms < 800

Or let the threshold machinery handle it, which fails a missing measurement by
default:

    from streamdouble.metrics import Threshold
    report = await simulated_call(
        URL, audio=CLIP,
        thresholds=[Threshold("first audio", 800, "time_to_first_audio_ms")],
    )
    assert report.passed
"""

#: The TypeError CPython raises for `None < 5` and friends. Matched on text
#: because the exception carries no structured information about its operands.
_NONE_COMPARISON_MARKER = "not supported between instances of"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any) -> Any:
    """Attach an explanation when a test trips over a `None` measurement.

    Note the hook. The obvious choice, ``pytest_assertrepr_compare``, does not
    work and it took running one to find out: ``None < 800`` raises
    ``TypeError`` while *evaluating* the comparison, so the assertion never
    completes and pytest never reaches the point where it would ask a plugin to
    describe the mismatch. The failure a user sees is a raw
    ``TypeError: '<' not supported between instances of 'NoneType' and 'int'``,
    which describes Python rather than their agent.

    Reporting hooks run after the exception exists, so this one can see it.
    """
    outcome = yield
    report = outcome.get_result()

    if report.when != "call" or not report.failed:
        return
    if call.excinfo is None or not isinstance(call.excinfo.value, TypeError):
        return
    if _NONE_COMPARISON_MARKER not in str(call.excinfo.value):
        return
    if "NoneType" not in str(call.excinfo.value):
        return

    report.sections.append(("streamdouble", _NONE_COMPARISON_HELP))
