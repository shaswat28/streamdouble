"""The pytest plugin, exercised by running real pytest sessions.

`pytester` runs pytest in a subprocess against a generated test file. That is
heavier than calling the fixture function directly, and it is the only way to
test what actually matters here: that the entry point registers, that the
fixtures are discoverable without any `pytest_plugins` line, and that a failure
prints what it is supposed to print.

Calling the fixture directly would test the code and miss all three, which is
the same mistake that let `streamdouble scenario` ship unreachable -- every
test drove the layer underneath the one users touch.
"""

from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]


@pytest.fixture
def agent_test(pytester: pytest.Pytester, server: str, speech_8k_path):
    """Write a test file that talks to the shared echo agent."""

    def write(body: str, *, url_suffix: str = "") -> None:
        # asyncio_default_fixture_loop_scope is not decoration. Without it
        # pytest-asyncio emits a deprecation warning during collection, and a
        # suite running warnings-as-errors -- as this project does, and as many
        # do -- turns that into an INTERNALERROR before a single test runs.
        # Users of this plugin hit exactly the same thing, which is why
        # docs/python-api.md says so.
        pytester.makeini(
            "[pytest]\n"
            "asyncio_mode = auto\n"
            "asyncio_default_fixture_loop_scope = function\n"
        )
        pytester.makepyfile(
            "import pytest\n"
            f"URL = {server + url_suffix!r}\n"
            f"CLIP = {str(speech_8k_path)!r}\n"
            "\n"
            "@pytest.fixture\n"
            "def streamdouble_config():\n"
            "    from streamdouble.session import SessionConfig\n"
            "    return SessionConfig(\n"
            "        response_timeout_s=3.0, quiet_period_s=0.3, max_drain_s=3.0\n"
            "    )\n"
            "\n" + body
        )

    return write


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_the_fixtures_are_available_without_any_configuration(agent_test, pytester):
    """Installing the package is the whole setup."""
    agent_test(
        "async def test_it(simulated_call):\n"
        "    report = await simulated_call(URL, audio=CLIP)\n"
        "    assert report.spoke\n"
        "    assert report.time_to_first_audio_ms is not None\n"
    )
    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=1)


def test_the_plugin_can_be_disabled(agent_test, pytester):
    """`-p no:streamdouble` must not break collection.

    A plugin that cannot be turned off is one that has to be uninstalled when
    it misbehaves, and it is installed into other people's suites.
    """
    agent_test("def test_nothing():\n    assert True\n")
    result = pytester.runpytest("-p", "no:streamdouble", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


# ---------------------------------------------------------------------------
# The failure that matters
# ---------------------------------------------------------------------------


def test_a_silent_agent_fails_with_an_explanation_not_a_typeerror(agent_test, pytester):
    """The single most common first failure, and it must explain itself.

    Unexplained, this is `TypeError: '<' not supported between instances of
    'NoneType' and 'int'`, which describes Python rather than the agent. This
    test pins the explanation, not merely the failure.

    It also pins the *mechanism* by existing at all: the obvious hook,
    pytest_assertrepr_compare, never fires here, because the TypeError is
    raised while evaluating the comparison and the assertion never completes.
    Running a real pytest session is what revealed that; calling the fixture
    directly would not have.
    """
    agent_test(
        "async def test_it(simulated_call):\n"
        "    report = await simulated_call(URL, audio=CLIP)\n"
        "    assert report.time_to_first_audio_ms < 800\n",
        url_suffix="?mode=silent",
    )
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*measurement that was never taken*"])
    result.stdout.fnmatch_lines(["*reports `None`, not 0*"])


def test_a_normal_failure_is_left_alone(agent_test, pytester):
    """The hook must not editorialise comparisons it has nothing to say about.

    A plugin that rewrites every numeric assertion failure in someone else's
    suite is a plugin they remove.
    """
    agent_test(
        "async def test_it(simulated_call):\n"
        "    report = await simulated_call(URL, audio=CLIP)\n"
        "    assert report.time_to_first_audio_ms < 0.0001\n"
    )
    result = pytester.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    assert "measurement that was never taken" not in result.stdout.str()


# ---------------------------------------------------------------------------
# Fixture behaviour
# ---------------------------------------------------------------------------


def test_an_unrelated_suite_is_not_lectured(pytester):
    """A stranger's `None < 5` must not get a paragraph about voice agents.

    This plugin loads into every pytest run of anyone who installs the
    package. Without a check that the test actually placed a call, any
    unrelated None comparison anywhere in their code base gets an explanation
    about Twilio, which is how a plugin gets uninstalled.

    Found by installing the built wheel into a clean virtualenv and running a
    two-line test with nothing to do with streamdouble. The nearby test for
    this only asserted that ordinary *numeric* failures were left alone -- it
    never occurred to it that someone might compare None to a number for
    reasons of their own.
    """
    # The ini matters even here: without
    # asyncio_default_fixture_loop_scope, pytest-asyncio warns during
    # collection and the inner run produces no summary at all.
    pytester.makeini(
        "[pytest]\n"
        "asyncio_mode = auto\n"
        "asyncio_default_fixture_loop_scope = function\n"
    )
    pytester.makepyfile(
        "def test_unrelated():\n"
        "    value = None\n"
        "    assert value < 800\n"
    )
    result = pytester.runpytest("-p", "no:cacheprovider")

    result.assert_outcomes(failed=1)
    assert "measurement that was never taken" not in result.stdout.str(), (
        "a suite that never used streamdouble was given streamdouble's advice"
    )


def test_the_config_fixture_is_overridable(agent_test, pytester):
    """A suite's own conftest must be able to carry its agent's auth."""
    agent_test(
        "async def test_it(simulated_call, streamdouble_config):\n"
        "    assert streamdouble_config.response_timeout_s == 3.0\n"
        "    report = await simulated_call(URL, audio=CLIP)\n"
        "    assert report.spoke\n"
    )
    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=1)


def test_tracing_writes_into_tmp_path(agent_test, pytester):
    agent_test(
        "async def test_it(simulated_call, tmp_path):\n"
        "    report = await simulated_call(URL, audio=CLIP, trace=True)\n"
        "    assert report.trace_path is not None\n"
        "    assert report.trace_path.exists()\n"
        "    assert report.trace_path.parent == tmp_path\n"
    )
    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=1)


def test_scenario_and_audio_are_mutually_exclusive(agent_test, pytester):
    agent_test(
        "import pytest\n"
        "async def test_it(simulated_call):\n"
        "    with pytest.raises(ValueError, match='not both'):\n"
        "        await simulated_call(URL, audio=CLIP, scenario='x.yaml')\n"
    )
    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=1)
