# Changelog

## 0.2.0 — 2026-09-12

Three phases of work in one release. They were planned as one arc and built as
one, so they ship as one rather than as three versions nobody had a reason to
install separately. `PLAN.md` numbers those phases 0.2.0, 0.3.0 and 0.4.0 to
describe what each one's code *is*; on PyPI they are this.

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
