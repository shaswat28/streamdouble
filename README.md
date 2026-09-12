# streamdouble

**A framework-agnostic Twilio Media Streams simulator.** Test your voice agent's
WebSocket endpoint locally, at full protocol fidelity, without placing a real call.

![streamdouble placing a simulated Twilio call](https://raw.githubusercontent.com/shaswat28/streamdouble/main/docs/demo.svg)

> **Status: early, but it works.** Places real calls, records the reply,
> simulates bad networks, runs scripted scenarios, and gates on latency in CI.
> Validated against a production voice agent, where it found a real bug.
> Expect the API to move before 1.0.

---

## The problem

To test a one-line change in a Twilio voice agent today, you start your app,
start an ngrok tunnel, trigger a call, **make a real phone ring**, talk to your
own bot until you reach the code path you changed, and get billed for the minute.

It is slow, it costs money, it cannot run in CI, and the Twilio↔app seam — where
audio format mismatches, framing errors and silent packet drops actually live —
is exactly the seam no existing tool exercises.

`streamdouble` does one thing: it lies to your WebSocket endpoint convincingly
enough that your agent cannot tell it isn't Twilio.

**It says nothing about whether your agent is any good.** No transcript scoring,
no personas, no LLM-as-judge, no conversation simulation. It tests the transport
underneath all of that: framing, timing, protocol conformance, and what happens
on a bad line. If you want to know whether your agent handles an angry customer,
[other tools do that better](#how-this-compares) — this is the layer beneath
them.

## Try it

No Twilio account, no tunnel, no phone.

```bash
pip install streamdouble
```

Point it at your agent, with any WAV file as the caller's voice:

```bash
streamdouble call ws://localhost:8000/media-stream --audio hello.wav --out reply.wav
```

`reply.wav` is what your agent said back — open it and listen.

```
  stream        MZdcedc700bccdb1f93f6b3334c79dcfa2
  sent          100 frames (2.00s)
  received      100 media frames (2.00s)
  first audio   437 ms
  gaps          mean 16 ms, p95 34 ms, max 46 ms
  marks         1 received, 1 echoed
  pacing        100 frames in 1.97s (drift -8.4ms, 51 late, max 15.9ms)

wrote reply.wav (2.00s)
```

**first audio** is the one to watch: how long the caller waits before hearing
anything. **marks** are Twilio's playback checkpoints — an agent sends one to
ask "has the caller actually heard this yet?", and a reply that never comes back
is why some agents hang. **pacing** is streamdouble reporting on itself, so you
can tell a slow agent from a slow measurement.

<details>
<summary><b>No voice agent to point it at yet?</b></summary>

This repository ships one, along with the audio to feed it. You need the clone
for the example agent, the fixtures and the scenarios; the published package is
just the tool itself.

```bash
git clone https://github.com/shaswat28/streamdouble
cd streamdouble
pip install -e ".[example]"
```

Then, in one terminal:

```bash
python examples/echo_agent.py --port 8000
```

and in another:

```bash
streamdouble call ws://localhost:8000/media-stream \
  --audio fixtures/speech_8k.wav --out reply.wav
```

The example agent can misbehave on purpose, which is how you see what a failure
looks like. Add `?mode=silent` to the URL for an agent that never answers, or
`?delay_ms=2000` for a slow one; `hangup_after=`, `garbage_after=` and
`clear_after=` are also there.

</details>

## Use it as a CI gate

```bash
streamdouble call ws://localhost:8000/media-stream --audio hello.wav \
  --max-first-audio-ms 800 --json
```

```json
{
  "schema_version": 1,
  "time_to_first_audio_ms": 440.7,
  "mean_inbound_gap_ms": 15.6,
  "p95_inbound_gap_ms": 31.7,
  "max_inbound_gap_ms": 60.1,
  "stall_count": 0,
  "violations": [],
  "timed_out": false,
  "pacing": {
    "drift_ms": 7.6,
    "late_frames": 51,
    "max_lateness_ms": 25.9,
    "mean_lateness_ms": 4.41,
    "measurement_is_reliable": true
  },
  "thresholds": [
    { "name": "first audio", "limit_ms": 800.0, "value_ms": 440.7, "passed": true }
  ],
  "exit_code": 0
}
```

Exit codes: `0` clean, `1` a threshold failed, `2` the agent never spoke,
`3` the agent violated the protocol, `4` usage error, `5` could not connect.

Two things that behaviour turns on, both deliberate:

- **An agent that said nothing fails its latency threshold.** Its
  `time_to_first_audio_ms` is `null`, not `0` — silence is the worst outcome,
  not the fastest one, and treating missing data as a pass is how a broken
  agent gets a green build. Use `--allow-no-audio` if you disagree.
- **`measurement_is_reliable` reports on the tool itself.** If pacing slipped
  persistently, audio reached your agent late and the latency above is an upper
  bound. A latency tool that reports wrong latency is worse than no tool, so it
  says so rather than quietly looking precise. Judged on *mean* lateness, so one
  unrelated stall does not condemn a measurement taken thousands of frames
  earlier — `max_lateness_ms` is published alongside for the full picture.

Where 800 ms comes from: human turn-taking gaps cluster at
[0–200 ms across languages](https://www.pnas.org/doi/10.1073/pnas.0903616106)
(Stivers et al., PNAS 2009). The 800 ms figure itself is an industry rule of
thumb for voice agents rather than a research finding — `metrics.py` says so
too, rather than borrowing the paper's authority for it.

## Use it from pytest

Most people testing a voice agent already have a pytest suite. Installing the
package registers a fixture, so a call is one line in it:

```python
async def test_the_agent_answers_quickly(simulated_call):
    report = await simulated_call(AGENT_URL, audio="hello.wav")
    assert report.spoke
    assert report.time_to_first_audio_ms < 800
```

No shelling out, no parsing JSON. The CLI is a renderer over the same API, so
what you assert on here and what `--json` prints cannot disagree.

An agent that never spoke has `time_to_first_audio_ms` of `None`, and the
plugin explains that failure rather than leaving you with a bare `TypeError`.
Full reference, including per-suite configuration and the pytest-asyncio
setting you need: **[docs/python-api.md](https://github.com/shaswat28/streamdouble/blob/main/docs/python-api.md)**.

## Catch a regression an absolute threshold cannot see

An agent that answered in 300 ms and now answers in 700 ms has got more than
twice as slow — and still passes `--max-first-audio-ms 800`. That is the
regression teams actually care about, and it needs a *baseline* rather than a
limit.

```bash
# once, on a known-good build
streamdouble call ws://localhost:8000/media-stream --audio hello.wav \
  -n 20 --save-baseline baseline.json

# in CI, from then on
streamdouble call ws://localhost:8000/media-stream --audio hello.wav \
  -n 20 --baseline baseline.json
```

```
  20 runs, 0 failed

                      median       min       max       p95   stddev  n
  first audio          181.7     166.4     194.1     193.2      9.2  20/20
  mean gap              18.2      18.1      18.3      18.3      0.1  20/20
  delivery ratio         1.1       1.1       1.2       1.2      0.0  20/20

  against the baseline:
    first audio      181.7 -> 585.8 (+404.1, +222%)  REGRESSION
```

A regression exits 1, same as a failed threshold — it is the same kind of fact.

**`-n` is a distribution, not a retry.** There is no `--retries` and there will
not be: repeating a call to measure its spread is useful, repeating it until it
passes hides a flaky agent, and a tool that offers the second cannot be trusted
about the first. Runs are sequential, never parallel — concurrent calls delay
each other's frames and would corrupt the statistics being gathered.

Three things the check refuses to do:

- **Compare incomparable runs.** The baseline records a hash of the clip's
  *contents*, the chaos seed and the impairments. Swap your test audio and it
  says so and exits 4, rather than reporting a "regression" that is really two
  different experiments being subtracted. It refuses before placing a single
  call, since everything it needs to know is known beforehand.
- **Fire on noise.** A change must exceed *both* a percentage and an absolute
  floor. 5 ms becoming 8 ms is 60% worse and inaudible; 2000 ms becoming
  2040 ms is 40 ms and nobody notices. Neither fails your build.
- **Treat a missing measurement as an improvement.** An agent that has stopped
  speaking has `null` where it had 400 ms. Subtracting those would report a
  cheerful "−400 ms"; instead it reads `was 181.7, now never measured` and
  counts as the most serious regression there is.

Below 20 runs the P95 is withheld and the output says why, because a P95 over
five samples is the maximum wearing a statistical hat.

### In GitHub Actions

```yaml
- uses: shaswat28/streamdouble@main
  with:
    url: ws://localhost:8000/media-stream
    audio: fixtures/hello.wav
    repeat: "20"
    baseline: baseline.json
    max-first-audio-ms: "800"
```

The JSON results and the frame trace are uploaded as an artifact on every run,
including failures — a failing run is the one whose trace you want.

## Script a whole call

A single clip is one test. The bugs live in the timing *between* things — the
caller interrupting, saying nothing, pressing a key, hanging up early.

```yaml
# scenarios/barge_in.yaml
name: caller interrupts the greeting
steps:
  - wait_for: audio        # let the agent get going
  - wait: 1.5              # streaming silence, not stopping
  - say: ../fixtures/speech/interrupt.wav
  - expect: clear          # a correct agent stops talking
  - wait: 1.0
```

Scenarios and their clips live next to each other, so this one runs from a
clone of this repository:

```bash
python fixtures/make_speech.py     # once: real speech, from your OS's own TTS
streamdouble scenario scenarios/barge_in.yaml ws://localhost:8000/media-stream
```

For your own project, write the YAML wherever you like and keep the clips
beside it — `say:` paths resolve relative to the scenario file.

**Barge-in needs real speech, and this is the trap.** Most agents trigger
barge-in when their transcription service produces words. The synthetic
fixtures in this repo are speech-*like* — right for codec tests, and no STT will
ever turn them into words. Drive a barge-in test with one and the agent never
reacts, the test fails, and it looks like an agent bug. It is not.

`fixtures/make_speech.py` generates transcribable clips using the TTS already on
your machine — SAPI on Windows, `say` on macOS, `espeak-ng` on Linux. No
recording, no API cost. Verified end to end: a real streaming transcription
service returned the exact sentence, and the agent's barge-in fired on it. The
output is gitignored, so generate your own (or drop in real recordings, which
are better — real callers have accents and trail off mid-sentence).

A failed `expect` exits 1, so a scenario is a CI check. Scenarios are validated
before the socket opens — a typo cannot fail halfway through with your agent
mid-sentence.

**`wait` streams silence; it does not stop sending.** A real call carries 20 ms
frames continuously for its whole duration, and voice-activity detection depends
on that. A simulator that goes quiet looks like a dead line rather than a quiet
caller, and the resulting "my agent never finalises the transcript" is a bug in
the test tool.

## See what actually went over the wire

```bash
streamdouble call ws://localhost:8000/media-stream --audio hello.wav \
  --trace call.jsonl
```

One JSON object per frame, both directions, with timings. This is the artefact
to attach when you think the *simulation* is wrong rather than your agent --
which is the most valuable bug report this project can receive.

Audio payloads are left out by default, because a minute of a call is about
30 MB of base64 nobody reads; a hash of each is kept so two traces stay
comparable. `--trace-payloads` includes them. `customParameters` values are
redacted, because `--param` is how agents are authenticated and a trace exists
to be sent to someone else; `--trace-secrets` opts out.

Nothing is written while the call is running. A trace that changed the latency
it was recording would not be a diagnostic.

## Simulate a bad connection

```bash
streamdouble call ws://localhost:8000/media-stream --audio hello.wav \
  --packet-loss 0.05 --jitter 40 --latency 120 --chaos-seed 7
```

Impairments are seeded, so a run that finds a bug replays exactly — pass the
same `--chaos-seed` and you get the same call. A chaos feature you cannot replay
is not a test tool, it is a random number generator that occasionally fails your
build.

Packet loss is modelled as audio Twilio never received, so the agent sees
`media.timestamp` jump rather than a frame go missing. The Twilio↔app leg is a
WebSocket, which is TCP: a frame *cannot* vanish in transit. `chaos.py` explains
the reasoning, and flags it as an inference the docs do not cover.

## Hear both sides of the call

```bash
streamdouble call ws://localhost:8000/media-stream --audio hello.wav \
  --record-stereo call.wav
```

Caller on the left, agent on the right, in one file.

**The agent's channel is placed at the instants its audio arrived**, with real
silence in the gaps — not concatenated. That distinction is the whole point.
Agents batch their outbound audio: the bug this tool is best known for finding
was an agent sending 9.5 seconds of speech in 0.85 seconds, leaving the caller
listening for another 8.7 seconds while the agent believed it had finished.
Concatenating those frames would render that as a smooth, perfectly-timed reply
— a picture of the opposite of the bug. Placed at arrival times, you can hear
the burst and the silence after it.

## Fork mode: the other half of Media Streams

`<Connect><Stream>` is the bidirectional call an agent answers, and it is what
everything above simulates. `<Start><Stream>` is the other half — a one-way
fork, which is what transcription and compliance-recording apps consume.

```bash
streamdouble call ws://localhost:8000/media-stream-fork --audio hello.wav \
  --fork --track both --agent-audio agent-reply.wav
```

A fork is one-way, and two things follow:

- **No mark echo, and no `clear`.** The Twilio docs say verbatim that "Twilio
  sends the `mark` event only during bidirectional Streams", so a simulator
  that echoed marks here would be inventing a message real Twilio never sends.
- **`--agent-audio` is required for an outbound track.** On a real fork Twilio
  copies the agent's *own* audio to the app, so streamdouble has to supply both
  halves. Streaming silence instead would not be a simpler simulation, it would
  be a wrong one.

An app that sends media back on a fork gets a **warning**, and the run still
passes. The Twilio docs state the bidirectional case affirmatively and say
nothing about this one, so treating it as a violation is streamdouble's
inference rather than a documented rule — and this project does not fail your
build on an inference unless you ask. `--strict-fork` is the asking.

## How this compares

Everything below does something streamdouble does not, and most of them are the
thing you actually want first. `streamdouble` tests one seam that none of them
test. Read this as "which problem do you have", not as a ranking.

| | What it is | Where it's stronger | Where streamdouble is |
|---|---|---|---|
| **[Pipecat Evals](https://docs.pipecat.ai/pipecat/evals/overview)** | Describe a conversation and the expected behaviour; it runs against your real agent | Semantic correctness — did the agent say the right thing, take the right action. First-party and free | Only works on Pipecat agents. Does not exercise the Twilio wire format |
| **[LiveKit testing](https://docs.livekit.io/agents/start/testing/)** | pytest-based agent tests, text mode by default, optional audio simulation | Fast, deterministic, cheap in text mode. Excellent if you're on LiveKit | Tied to LiveKit agents. Text mode skips the audio path entirely |
| **Hamming / Cekura / TestMu** | Hosted QA: simulated callers, personas, LLM-judged scoring, production monitoring | Conversation quality at scale, regression suites, dashboards, monitoring real traffic | Paid, hosted, and aimed at what the agent *says* rather than how audio reaches it |
| **Twilio CLI** | Official tooling, including webhook tunnelling | Everything else Twilio — TwiML, calls, numbers, HTTP webhooks | Doesn't simulate Media Streams. Webhooks are HTTP; this is the WebSocket leg |
| **streamdouble** | Speaks the Twilio Media Streams protocol at your endpoint | The transport seam: μ-law framing, 20 ms pacing, mark echo, timing, packet loss. Framework-agnostic, local, free, no account | **Says nothing about whether your agent is any good.** No transcript scoring, no personas, no judgement |

**The honest summary:** if you want to know whether your agent handles an angry
customer, use one of the others. If you want to know whether your agent handles
a caller on a bad connection who interrupts mid-sentence — or whether it handles
Twilio's wire format correctly at all — that is the gap this fills.

They compose. streamdouble is the layer underneath: it makes the *transport*
trustworthy so that when a behavioural test fails, you know it failed for a
behavioural reason.

**What it found in practice.** Pointed at a real production voice agent for the
first time, it found a barge-in blind spot: the agent streamed 9.5 s of speech in
0.85 s, cleared its "am I speaking" flag when *sending* finished, and was
therefore deaf to interruption for the 8.7 s the caller was still listening. No
transcript-scoring tool would have seen that, because every transcript was
correct.

## A simulated call is still a real call

streamdouble removes the phone, the tunnel and the Twilio charge. It does not
remove anything that happens *after* your agent's WebSocket handler runs.

The first time this was pointed at a production voice agent, the call completed
normally and then wrote three records to a live CRM for a made-up phone number,
because the agent pushes call outcomes on hangup. That is correct behaviour by
the agent. It is just not what you want from a test.

Before pointing streamdouble at a real agent, check what a completed call
touches — CRM pushes, databases, webhooks, notification sends, billing — and
disable them:

```bash
TWENTY_API_KEY="" CRM_BASE_URL="" python -m uvicorn app.main:app --port 8000
```

The rule of thumb: everything downstream of the socket cannot tell the
difference, and it is not supposed to be able to. That is the whole point of the
tool, and it is exactly why this needs saying.

## Scope

**What it does:** speaks the Twilio Media Streams protocol — `connected`,
`start`, `media`, `dtmf`, `stop` outbound; `media`, `mark`, `clear` inbound —
with correct μ-law encoding, 20 ms framing, real-time pacing, and mark echo.

**Marks are echoed, not just logged.** Twilio returns a mark once the audio
queued before it has finished playing to the caller, and many agents gate
turn-taking on that echo — so `streamdouble` models a playback clock and echoes
each mark when its audio would have drained. A `clear` discards the buffer *and*
the marks queued behind it, because that audio is never going to play.

## Development

```bash
pip install -e ".[dev]"
pytest        # the session tests run against a real socket
ruff check .
```

Fixtures are generated and committed; regenerate with
`python fixtures/make_fixtures.py` only if a fixture definition changes.

CI runs the suite on Linux and Windows across Python 3.11, 3.12 and 3.13.
3.13 matters specifically: `audioop` was removed there, so that run proves the
μ-law codec stands on its own rather than leaning on the stdlib.

### A note on timing accuracy

Pacing schedules against absolute deadlines from a fixed origin, so error does
not accumulate over a long call.

Timing uses `time.perf_counter`, not `time.monotonic`. The distinction is not
pedantry: on Windows CPython implements `monotonic` with `GetTickCount64`, whose
resolution is **15.625 ms** — comparable to the entire 20 ms frame being
measured. Using it quantised every reported latency onto that grid while
printing it to a tenth of a millisecond, and made the pacer's own lateness
statistics measure the clock instead of the pacing. Every run reported max
lateness of exactly 15.0 or 16.0 ms, which is one tick.

With `perf_counter` (100 ns), the two effects separate. Windows' scheduler
granularity is genuinely coarse — a real run shows max lateness around 15.5 ms —
but mean lateness is closer to 4 ms, and that distinction was invisible before.
`streamdouble` publishes both, and derives `measurement_is_reliable` from the
mean, so one unrelated stall does not condemn a measurement taken thousands of
frames earlier.

`PLAN.md` records the build plan, the decisions taken, and what each review gate
found.

## Use it from Claude Code

`skill/SKILL.md` is a Claude Code skill wrapper. Copy it into `.claude/skills/`
and Claude can drive streamdouble directly — "test this agent with a caller who
interrupts", "is it answering fast enough", "why does barge-in not fire".

It carries the judgement that matters as much as the commands: disable whatever
your agent touches on hangup before the first call, never report a `null`
latency as fast, and do not diagnose barge-in with synthetic audio, because a
transcription service will not transcribe it and the test fails for the wrong
reason.

## Notes

[Six ways to get Twilio Media Streams audio wrong](https://github.com/shaswat28/streamdouble/blob/main/docs/mulaw-gotchas.md) —
what building this turned up, including why μ-law silence is `0xFF`, why one
code exists that no encoder emits, and why "finished sending" is not "finished
speaking".

## Changelog

[What changed in each release](https://github.com/shaswat28/streamdouble/blob/main/CHANGELOG.md).

## Not affiliated with Twilio

This project is not affiliated with, endorsed by, or sponsored by Twilio. It
simulates the publicly documented Twilio Media Streams protocol for testing
purposes. "Twilio" is a trademark of Twilio Inc.

## License

Apache-2.0. See [LICENSE](https://github.com/shaswat28/streamdouble/blob/main/LICENSE).
