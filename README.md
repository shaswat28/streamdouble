# streamdouble

**A framework-agnostic Twilio Media Streams simulator.** Test your voice agent's
WebSocket endpoint locally, at full protocol fidelity, without placing a real call.

![streamdouble placing a simulated Twilio call](docs/demo.svg)

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
streamdouble call ws://localhost:8000/media-stream --audio hello.wav \n  --max-first-audio-ms 800 --json
```

```json
{
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

[Six ways to get Twilio Media Streams audio wrong](docs/mulaw-gotchas.md) —
what building this turned up, including why μ-law silence is `0xFF`, why one
code exists that no encoder emits, and why "finished sending" is not "finished
speaking".

## Not affiliated with Twilio

This project is not affiliated with, endorsed by, or sponsored by Twilio. It
simulates the publicly documented Twilio Media Streams protocol for testing
purposes. "Twilio" is a trademark of Twilio Inc.

## License

Apache-2.0. See [LICENSE](LICENSE).
