# The Python API

`streamdouble` is a library as well as a command. If you test in pytest — and
most people testing voice agents do — this is the interface you want.

```python
from streamdouble import api

report = await api.call("ws://localhost:8000/media-stream", audio_path="hello.wav")
assert report.time_to_first_audio_ms < 800
```

The CLI is a renderer over exactly this. It parses arguments, formats a report
and picks an exit code, and computes nothing of its own — so the numbers you
assert on here and the numbers `--json` prints cannot describe different calls.

---

## The pytest plugin

Installing the package registers it. There is no `pytest_plugins` line to add.

```python
async def test_the_agent_answers_quickly(simulated_call):
    report = await simulated_call(AGENT_URL, audio="fixtures/hello.wav")
    assert report.spoke
    assert report.time_to_first_audio_ms < 800
```

### Configure it once for your suite

`streamdouble_config` is an ordinary fixture. Override it in `conftest.py` to
carry whatever your agent needs — most commonly the secret it authenticates on,
which real agents read from `start.customParameters` rather than the URL:

```python
import os
import pytest
from streamdouble.session import SessionConfig

@pytest.fixture
def streamdouble_config():
    return SessionConfig(
        custom_parameters={"authToken": os.environ["AGENT_TOKEN"]},
        response_timeout_s=5.0,
    )
```

### You need pytest-asyncio configured

```ini
[pytest]
asyncio_mode = auto
asyncio_default_fixture_loop_scope = function
```

**The second line is not optional.** Without it pytest-asyncio emits a
deprecation warning during collection, and a suite running warnings as errors
turns that into an `INTERNALERROR` before a single test runs. This is stated
here because it cost time to diagnose once already.

### When the agent says nothing

```python
assert report.time_to_first_audio_ms < 800
```

against a silent agent raises `TypeError: '<' not supported between instances
of 'NoneType' and 'int'`. That is correct and deliberate: `time_to_first_audio_ms`
is `None` when the agent produced no audio, never `0`. Silence is the worst
outcome a voice agent can have, and reporting it as the fastest possible reply
is how a broken agent passes a green build.

The plugin attaches an explanation to that failure. To handle it yourself:

```python
assert report.spoke, "the agent never answered"
assert report.time_to_first_audio_ms < 800
```

---

## `CallReport`

What every call returns. A record, not a verdict — nothing here raises because
your agent was slow.

| Attribute | Meaning |
|---|---|
| `spoke` | Whether any audio came back at all |
| `time_to_first_audio_ms` | Stream start to first audio byte. `None` if silent |
| `violations` | Protocol violations the agent committed, as codes |
| `audio_received` | The reply, as raw μ-law bytes |
| `stream_sid` | The `MZ…` id used for this call |
| `passed` | True when nothing measured counts as a failure |
| `exit_code` | What the CLI would exit with — 0–5, same contract |
| `metrics` | The full `Metrics` object: gaps, pacing, delivery ratio |
| `result` | The raw `SessionResult`, including the event log |
| `trace_path` | Where the frame trace went, if one was requested |
| `to_dict()` | The `--json` payload, verbatim |

Save the reply to listen to it:

```python
from streamdouble import api
api.save_reply(report, "reply.wav")   # returns False if the agent was silent
```

It writes nothing rather than leaving a zero-length WAV that looks like a
successful recording of silence.

---

## Outside pytest

```python
from streamdouble import api

report = api.call_sync("ws://localhost:8000/media-stream", audio_path="hello.wav")
```

`call_sync` refuses to run inside a running event loop, with a message naming
the async version. Under `asyncio_mode = auto` every test is already inside a
loop, so that is the likely mistake rather than an exotic one.

---

## Scenarios

```python
report = await api.run_scenario(URL, "scenarios/barge_in.yaml")
assert not report.result.failed_expectations
```

A failed `expect:` lands on `report.result.failed_expectations` and makes
`exit_code` 1. Nothing raises. Scenario files are validated before the socket
opens, so a typo cannot fail halfway through with your agent mid-sentence.

---

## Tracing a call

When you need to know what actually went over the wire — and when you are
filing a bug saying the *simulation* is wrong, this is the artefact to attach:

```python
from streamdouble.trace import TraceConfig

report = await api.call(URL, audio_path=CLIP, trace=TraceConfig(path="call.jsonl"))
```

From the plugin, `trace=True` writes into pytest's `tmp_path`:

```python
report = await simulated_call(URL, audio=CLIP, trace=True)
```

One JSON object per frame, both directions:

```json
{"t": 141108.717832, "dir": "out", "event": "media", "seq": "2", "chunk": "1",
 "timestamp_ms": "0", "track": "inbound", "bytes": 216, "sha256_8": "22bb1432"}
```

Three defaults worth knowing:

- **Nothing is written during the call.** Records accumulate in memory and
  flush once the socket closes. File I/O inside the receive loop would delay
  frame handling and inflate the very latency the tool reports — a trace that
  changes the measurement is not a diagnostic.
- **Payloads are excluded.** A minute of audio is ~30 MB of base64 nobody
  reads. A SHA-256 prefix is kept so two traces stay comparable.
  `TraceConfig(payloads=True)` opts in.
- **`customParameters` values are redacted.** That is where agents' shared
  secrets live, and a trace exists to be sent to someone else.
  `TraceConfig(secrets=True)` opts out.

---

## Thresholds, if you want the CLI's judgement

```python
from streamdouble.metrics import Threshold

report = await api.call(
    URL, audio_path=CLIP,
    thresholds=[Threshold("first audio", 800, "time_to_first_audio_ms")],
)
assert report.passed
```

A missing measurement **fails** its threshold by default. Pass
`missing_fails=False` if you disagree, knowing what you are turning off.

---

## Errors

| Exception | When |
|---|---|
| `api.ConnectionFailed` | The agent was not reachable — refused, unresolvable, or a handshake that never completed |
| `ValueError` | No audio given, both `audio_path` and `frames` given, or an empty WAV |
| `audio.AudioError` | The WAV could not be read |

A slow agent, a silent agent and a protocol-violating agent all return normally.
They are facts on the report, and what they are worth is your call.

---

## Before pointing this at a real agent

A simulated call is a real call to everything behind the socket. The first time
this was run against a production agent, the call completed and then wrote
records to a live CRM for a made-up phone number. Disable whatever a completed
call touches — CRM pushes, webhooks, billing, notifications — before the first
run. See the README.
