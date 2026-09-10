"""Record a real streamdouble run and render it as an animated SVG.

Run from the repository root::

    python tools/make_demo.py

Generated from an actual call against ``examples/echo_agent.py``, not typed out
by hand. A mocked-up demo drifts from the tool the first time output changes,
and a README whose screenshot does not match what you see when you run it is
worse than no screenshot -- it is the first thing a visitor checks and the first
place trust goes.

An animated SVG rather than a GIF: sharp at any size, a fraction of the bytes,
readable as text in a diff, and it needs no recording tool installed. The
animation is pure CSS, which GitHub's image pipeline preserves.
"""

from __future__ import annotations

import html
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "demo.svg"

#: Terminal geometry, in characters.
COLUMNS = 84

#: A dark, neutral terminal that reads against both GitHub themes.
BACKGROUND = "#1c2128"
TITLE_FILL = "#2d333b"
FOREGROUND = "#d4d8de"
DIM = "#8b949e"
ACCENT = "#7ee787"
WARN = "#f0883e"
PROMPT = "#79c0ff"

CHAR_WIDTH = 8.4
LINE_HEIGHT = 21
PADDING = 18
TITLE_BAR = 30

#: Seconds the finished frame holds before the animation loops.
HOLD_S = 5.0

#: Stagger applied within a burst of lines printed at the same instant.
#:
#: The summary block is written in one go when the call finishes, so its lines
#: share a timestamp, and revealing fifteen at once is unreadable. This is
#: typesetting, not data: it changes nothing about *when the call completed*,
#: which is the only timing claim the demo makes, and the figures inside those
#: lines are the real measurements from the real run.
CASCADE_S = 0.04

#: Two lines printed closer together than this came from the same burst.
SAME_BURST_S = 0.01


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_echo_agent() -> int:
    """Run the example agent in this process and return its port."""
    sys.path.insert(0, str(ROOT / "examples"))
    import uvicorn

    from echo_agent import app

    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()

    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the echo agent did not start")
        time.sleep(0.02)
    return port


def tidy(text: str) -> str:
    """Strip test-rig detail from a line before it goes in the demo.

    The real URL carries a random port and a delay knob used to make the echo
    agent behave like a slower agent. Neither belongs in front of someone who
    has not read the source; the measurements they produce are untouched.
    """
    return re.sub(
        r"ws://127\.0\.0\.1:\d+/media-stream\?delay_ms=\d+",
        "ws://localhost:8000/media-stream",
        text,
    )


def record() -> list[tuple[float, str]]:
    """Run a real call and return (elapsed_seconds, line) pairs."""
    port = start_echo_agent()

    command = [
        sys.executable, "-m", "streamdouble.cli", "call",
        f"ws://127.0.0.1:{port}/media-stream?delay_ms=400",
        "--audio", "fixtures/speech_8k.wav",
        "--out", "reply.wav",
        "--quiet-period", "0.5",
        "--max-first-audio-ms", "800",
    ]

    lines: list[tuple[float, str]] = [
        (0.0, "$ streamdouble call ws://localhost:8000/media-stream \\"),
        (0.05, "      --audio hello.wav --out reply.wav --max-first-audio-ms 800"),
    ]

    # Unbuffered, or Python holds the child's stdout until it exits and every
    # line arrives at the same instant -- which loses the one thing the
    # animation is for.
    environment = {**os.environ, "PYTHONUNBUFFERED": "1"}

    started = time.perf_counter()
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=environment,
    )
    assert process.stdout is not None

    captured = [
        (time.perf_counter() - started, raw.rstrip("\n")) for raw in process.stdout
    ]
    process.wait()

    previous: float | None = None
    run = 0
    for at, text in captured:
        run = run + 1 if previous is not None and at - previous < SAME_BURST_S else 0
        previous = at
        lines.append((at + 0.4 + run * CASCADE_S, tidy(text)))

    tail = lines[-1][0]
    lines.append((tail + 0.25, ""))
    lines.append((tail + 0.45, "$ echo $?"))
    lines.append((tail + 0.60, str(process.returncode)))

    (ROOT / "reply.wav").unlink(missing_ok=True)
    return lines


def colour_for(line: str) -> str:
    """Pick a colour by what the line is, matching the CLI's own emphasis."""
    stripped = line.strip()
    if stripped.startswith("$"):
        return PROMPT
    if stripped.startswith("!"):
        return WARN
    if stripped.endswith(" ok") or stripped.startswith("wrote "):
        return ACCENT
    if stripped.startswith("calling "):
        return DIM
    return FOREGROUND


def render(lines: list[tuple[float, str]]) -> str:
    width = int(COLUMNS * CHAR_WIDTH + PADDING * 2)
    height = int(len(lines) * LINE_HEIGHT + PADDING * 2 + TITLE_BAR)
    total = lines[-1][0] + HOLD_S

    rows = [
        f'<text x="{PADDING}" y="{PADDING + TITLE_BAR + (index + 1) * LINE_HEIGHT}" '
        f'fill="{colour_for(text)}" style="animation-delay:{at:.2f}s">'
        f'{html.escape(text) or " "}</text>'
        for index, (at, text) in enumerate(lines)
    ]

    dots = "".join(
        f'<circle cx="{18 + offset}" cy="15" r="5" fill="{colour}"/>'
        for offset, colour in ((0, "#ff5f56"), (18, "#ffbd2e"), (36, "#27c93f"))
    )
    body = "\n  ".join(rows)

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"\n'
        f'     viewBox="0 0 {width} {height}"\n'
        f'     font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"\n'
        f'     font-size="13" role="img"\n'
        f'     aria-label="streamdouble placing a simulated Twilio call">\n'
        f"  <title>streamdouble placing a simulated Twilio call</title>\n"
        f"  <style>\n"
        f"    text {{ opacity: 0; animation: reveal {total:.2f}s linear infinite;"
        f" white-space: pre; }}\n"
        f"    @keyframes reveal {{\n"
        f"      0%   {{ opacity: 0; }}\n"
        f"      0.6% {{ opacity: 1; }}\n"
        f"      97%  {{ opacity: 1; }}\n"
        f"      100% {{ opacity: 0; }}\n"
        f"    }}\n"
        f"  </style>\n"
        f'  <rect width="{width}" height="{height}" rx="8" fill="{BACKGROUND}"/>\n'
        f'  <rect width="{width}" height="{TITLE_BAR}" rx="8" fill="{TITLE_FILL}"/>\n'
        f'  <rect y="{TITLE_BAR - 8}" width="{width}" height="8" fill="{TITLE_FILL}"/>\n'
        f"  {dots}\n"
        f'  <text x="{width / 2}" y="19" fill="{DIM}" font-size="11"'
        f' text-anchor="middle">streamdouble</text>\n'
        f"  {body}\n"
        f"</svg>\n"
    )


def main() -> None:
    lines = record()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render(lines), encoding="utf-8")

    print(f"wrote {OUTPUT.relative_to(ROOT)} ({OUTPUT.stat().st_size:,} bytes)")
    print(f"{len(lines)} lines over {lines[-1][0]:.1f}s")
    for at, text in lines:
        print(f"  {at:5.2f}s  {text}")


if __name__ == "__main__":
    main()
