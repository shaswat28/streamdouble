"""Shared fixtures for the test suite."""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from streamdouble import audio

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"
EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"

# The example agent is not part of the installed package -- it is documentation
# that happens to be executable -- so it has to be put on the path explicitly.
sys.path.insert(0, str(EXAMPLES_DIR))


def free_port() -> int:
    """Ask the OS for an unused port.

    Binding to port 0 and reading back the assignment avoids the flakiness of
    hard-coded ports, which collide with whatever else the developer happens to
    be running and with parallel CI jobs on the same machine.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def server() -> str:
    """A real echo agent on a real port, shared by the whole test session.

    Session-scoped because starting uvicorn costs about a second and no test can
    affect another's connection -- each opens its own, and the agent keeps no
    state between connections.

    Deliberately a real server on a real socket rather than an in-process
    transport: the lifecycle failures worth testing (the peer vanishing, a
    half-closed socket, a receive task dying quietly) do not exist without one.
    """
    uvicorn = pytest.importorskip("uvicorn", reason="uvicorn serves the example agent")
    from echo_agent import app

    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not instance.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the echo agent did not start")
        time.sleep(0.02)

    yield f"ws://127.0.0.1:{port}/media-stream"

    instance.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def fixture_dir() -> Path:
    """Directory holding the committed WAV fixtures."""
    return FIXTURE_DIR


@pytest.fixture(scope="session")
def speech_8k_path() -> Path:
    """An 8 kHz mono speech-like WAV -- the default input for most tests."""
    return FIXTURE_DIR / "speech_8k.wav"


@pytest.fixture(scope="session")
def speech_8k_samples(speech_8k_path: Path) -> np.ndarray:
    """The speech fixture as a 1-D int16 mono array."""
    samples, rate = audio.load_wav(speech_8k_path)
    assert rate == audio.SAMPLE_RATE
    return audio.to_mono(samples)
