# Changelog

## 0.3.0 — 2026-09-29

Phases 10-12: one new subcommand, one new flag, a timer change on Windows, and
the fixes from gates 12-13 and a full-codebase review. Phases 7-9 shipped
together in 0.2.0.

Two behaviour changes a CI pipeline may notice: an agent that hangs up without
speaking now exits 2 rather than 0, and `scenario` refuses `--repeat`,
`--baseline` and `--save-baseline` (exit 4) rather than silently ignoring them.

- `streamdouble inspect TRACE [--json] [--gap-ms MS]` summarises a `--trace`
  file: whether the agent spoke and when, marks and their echoes, clears, gaps
  in the agent's audio, sequence breaks, and malformed or truncated input. It
  opens no socket and reads the file as untrusted input.
- `--junit PATH` on `call` and `scenario`, including `--repeat` series and
  baseline comparisons, writes a JUnit XML report. Its `verdict` testcase fails
  whenever the exit code is non-zero. An allowed unmeasured threshold is
  skipped, never passed. The GitHub Action gains a `junit` input.
- On Windows, calls request a 1 ms system timer for their duration, released
  afterwards. On real calls, measured mean pacing lateness fell from 4.3 ms to
  1.3 ms and max lateness from 28.8 ms to 6.7 ms, with time to first audio
  unchanged. The JSON's `pacing.timer` records whether the timer was `raised`,
  `refused`, or `not needed`.
- **Fixed:** an agent that hangs up without ever sending audio now exits 2,
  "the agent never spoke", as documented. It used to exit 0. The JSON gains
  `closed_by` (`agent`, `caller`, or `streamdouble` when the websockets
  library closed it, e.g. for an oversized frame) and `closed_before_audio`;
  `--junit` gains a `no close before audio` testcase.
- **Fixed:** a hangup that arrives after the caller's last frame is reported as
  a hangup. It used to be reported as a response timeout, after waiting the
  whole timeout out on a closed socket. A scenario's own `hangup` no longer
  waits it out either.
- **Fixed:** a scenario `expect` step that the call ended before reaching is
  reported as failed ("not reached"). It used to be left out, so an agent that
  hung up before a failing expectation could exit 0.
- **Fixed:** `scenario` no longer accepts `--repeat`, `--baseline` or
  `--save-baseline`. It took them and ignored them -- one call, no baseline
  written, no comparison -- so a scenario regression gate could never fail.
  They are now a usage error (exit 4); they remain on `call`.
- **Fixed:** on a two-track fork, each track's `media.timestamp` now advances
  20 ms per frame. The tracks shared one clock, so each ran at 40 ms per frame.
  `chunk` and `sequenceNumber` are still shared; the Twilio docs do not say,
  and that reading is unchanged (see `protocol.py`). Within each 20 ms tick
  the agent's frame is now sent before the caller's.
- **Fixed:** on a two-track fork with `--loss` or `--jitter-ms`, the agent's
  track is no longer dropped or delayed along with the caller's. Impairments
  model the caller's leg only.
- **Fixed:** malformed scenario steps are refused before the call:
  `wait_for` with a list or mapping raised a TypeError, and `wait: .nan` or
  `.inf` passed validation and crashed mid-call, as did a huge finite one;
  a step now lasts at most a day. `timeout: true` is refused rather than read
  as one second.
- **Fixed:** `streamdouble inspect` measures times from the first frame in the
  trace, parseable or not, rather than the first well-formed one.

## 0.2.0 — 2026-09-12

Three phases of work in one release. They were planned as one arc and built as
one, so they ship as one rather than as three versions nobody had a reason to
install separately.

### streamdouble is now a library as well as a command

```python
from streamdouble import api

report = await api.call("ws://localhost:8000/media-stream", audio_path="hello.wav")
assert report.time_to_first_audio_ms < 800
```

The CLI is a renderer over exactly this, so `--json` and the API cannot
describe different calls.

**A pytest plugin**, registered by installing the package — no `pytest_plugins`
line:

```python
async def test_the_agent_answers_quickly(simulated_call):
    report = await simulated_call(AGENT_URL, audio="hello.wav")
    assert report.spoke
    assert report.time_to_first_audio_ms < 800
```

Your suite needs `asyncio_mode = auto` and
`asyncio_default_fixture_loop_scope = function`. See
[docs/python-api.md](docs/python-api.md).

### Catch a regression an absolute threshold cannot see

An agent that answered in 300 ms and now answers in 700 ms is more than twice
as slow and still passes `--max-first-audio-ms 800`.

```bash
streamdouble call ws://... --audio hello.wav -n 20 --save-baseline baseline.json
streamdouble call ws://... --audio hello.wav -n 20 --baseline baseline.json
```

The comparison refuses to answer more often than you might expect, and each
refusal is deliberate: it will not compare runs that used a different clip,
seed or impairments; it will not fire unless a change clears both a percentage
and an absolute floor; and it will not treat a measurement that has gone
missing as an improvement.

`-n` is a distribution, not a retry. There is no `--retries`.

### Fork mode — the other half of Media Streams

`<Connect><Stream>` is the bidirectional call an agent answers. `<Start><Stream>`
is a one-way fork, which is what transcription and compliance-recording apps
consume.

```bash
streamdouble call ws://.../media-stream-fork --audio hello.wav \
  --fork --track both --agent-audio reply.wav
```

No mark echo and no `clear`, because Twilio sends marks only on bidirectional
streams. An app that sends media back gets a warning rather than a failure:
the Twilio docs state the bidirectional case and are silent on this one, so
treating it as a violation is streamdouble's inference. `--strict-fork` opts in.

### Also new

- **`--trace out.jsonl`** — every frame, both directions, with timings. The
  artefact to attach when you think the *simulation* is wrong. Payloads
  excluded and `customParameters` redacted by default.
- **`--record-stereo call.wav`** — caller left, agent right, with the agent's
  audio placed at the instants it arrived. A front-loading agent looks
  front-loaded rather than smooth.
- **A GitHub Action** (`action.yml`).
- **`schema_version`** in the JSON output.

### Fixed

- `streamdouble` exits **4** for a bad command line, not argparse's 2 — which
  in this package means `EXIT_TIMEOUT`, so a mistyped flag used to report that
  your agent had failed to respond.
- The README's CI-gate example had a line continuation written as the two
  characters `\` and `n`, so it copy-pasted as a broken command.

### Compatibility

No breaking changes to the CLI or its exit codes. The `api` module and the
pytest plugin are new surface; everything that worked in 0.1.0 still works.

---

## 0.1.0 — 2026-09-11

First public release. Simulates the Twilio Media Streams protocol against a
voice agent's WebSocket endpoint: μ-law framing, 20 ms pacing, mark echo,
scenario files, seeded network impairments, and latency metrics usable as a CI
gate.

0.1.1 was tagged in the repository and never published.
