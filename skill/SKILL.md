---
name: streamdouble
description: >
  Test a Twilio voice agent by placing simulated calls against its
  /media-stream WebSocket endpoint - no phone, no ngrok, no Twilio account.
  Use when asked to test, call, or debug a voice agent; to measure how fast it
  answers; to check how it behaves when the caller interrupts, says nothing,
  hangs up, or is on a bad connection; or when a voice agent works on real
  calls but not locally (or the reverse). Also for diagnosing barge-in that
  does not fire, audio that sounds like static, and mu-law or framing problems.
---

# streamdouble

Places a simulated Twilio Media Stream against a voice agent's WebSocket
endpoint and reports what came back.

## Before the first call: what a test call touches

**A simulated call is a real call to everything downstream of the socket.**
streamdouble removes the phone, the tunnel and the Twilio charge. It does not
remove anything the agent does *after* its handler runs.

The first time this was pointed at a production agent, the call completed
normally and then wrote records to a live CRM for a made-up phone number,
because that agent pushes call outcomes on hangup. Correct behaviour by the
agent; wrong outcome for a test.

So before the first call against an agent that is not a throwaway, find what a
completed call triggers - CRM pushes, database writes, webhooks, notifications,
billing - and disable it. Usually that is a few environment variables:

```bash
CRM_API_KEY="" WEBHOOK_URL="" python -m uvicorn app.main:app --port 8000
```

If you cannot tell what a call touches, say so and ask before calling.

## Placing a call

```bash
streamdouble call ws://localhost:8000/media-stream --audio clip.wav --out reply.wav
```

`--audio` is any WAV; it is resampled to 8 kHz mono automatically. `--out`
writes what the agent said, so it can be listened to or measured.

Many agents authenticate on TwiML `<Parameter>` values rather than the URL,
delivered in the `start` frame. Pass those with `--param`:

```bash
streamdouble call ws://localhost:8000/media-stream --audio clip.wav \
  --param key=SECRET --param to=+15555550100
```

If a call is rejected immediately, missing `--param` is the first thing to
check - the endpoint reads them off `start.customParameters`.

## Reading the result

Use `--json` when the numbers matter; it is stdout-clean and pipeable.

Fields worth knowing:

- **`time_to_first_audio_ms`** - the headline metric. `null` means the agent
  never spoke, which is *not* zero. Never report a `null` as fast.
- **`delivery_ratio`** and **`playback_tail_ms`** - how much faster than real
  time the agent handed over its audio, and how long the caller is still
  listening after it stopped sending. See below; this one catches a real bug.
- **`violations`** - protocol errors, by code. Any entry is a definite bug in
  the agent.
- **`pacing.measurement_is_reliable`** - whether streamdouble's own timing was
  good enough for the latency figures to mean anything. If false, report the
  latency as an upper bound rather than a measurement.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Clean |
| 1 | A threshold failed |
| 2 | The agent never spoke |
| 3 | The agent violated the protocol |
| 4 | Usage error |
| 5 | Could not connect |

## Common tasks

**Is it fast enough?**

```bash
streamdouble call ws://localhost:8000/media-stream --audio clip.wav \
  --max-first-audio-ms 800 --json
```

800 ms is the usual conversational-flow target. Human turn-taking gaps cluster
at 0-200 ms; 800 ms is an industry rule of thumb rather than a research
finding, so treat it as a default, not a law.

**Does it handle a bad connection?**

```bash
streamdouble call ws://localhost:8000/media-stream --audio clip.wav \
  --packet-loss 0.05 --jitter 40 --latency 120 --chaos-seed 7
```

Always report the seed with any failure. Impairments are seeded precisely so a
failing run can be replayed, and a chaos failure without its seed is a story
rather than a bug report.

**Does it stop talking when interrupted?**

```yaml
# barge_in.yaml
name: caller interrupts
steps:
  - wait_for: audio
  - wait: 1.5
  - say: clip.wav
  - expect: clear
  - wait: 1.0
```

```bash
streamdouble scenario barge_in.yaml ws://localhost:8000/media-stream
```

**Real speech is required for this test.** Barge-in in most agents is triggered
by their transcription service producing words. Synthetic or tone-like audio
will not transcribe, the agent will not react, and the test fails for a reason
that has nothing to do with the agent. If the only clips available are
synthetic, say so rather than reporting a barge-in bug.

**Does it survive the caller vanishing?**

```yaml
steps:
  - wait_for: audio
  - hangup
```

## Diagnosing what comes back

**No audio at all (`exit 2`, `time_to_first_audio_ms: null`).** The handshake
worked, so the failure is downstream of the socket - the agent's LLM or TTS.
Check its logs before suspecting the protocol. A decommissioned model returning
404 looks exactly like this.

**Protocol violations (`exit 3`).** Read the codes. `bad_base64` usually means
double-encoding. `missing_stream_sid` means the agent is not echoing the
`streamSid` it was given.

**Barge-in does not fire, but the transcript is correct.** Look at
`delivery_ratio` first. Agents commonly stream TTS much faster than real time -
10x or more - while Twilio buffers that audio and plays it at the caller's
pace. An agent that clears its "am I speaking" flag when *sending* finishes is
then deaf to interruption for the whole of `playback_tail_ms`. This is a real
bug on real calls and has been found in the wild. The fix is to gate on the
`mark` echo, which Twilio returns only once the audio has actually played.

Report this as a hypothesis to check, not a verdict: an agent that already
gates on the mark echo has the same `delivery_ratio` and no blind spot.

**Audio comes back as static.** Decode it with `--out` and listen. Then suspect
double base64, wrong frame size, or a sample-rate mismatch, in that order.

## What this cannot tell you

It says nothing about whether the agent is any *good* - no transcript scoring,
no personas, no judgement about what was said. It tests the transport: framing,
timing, protocol conformance, and behaviour under bad conditions. If the
question is about conversation quality, say so and point at tools built for
that rather than stretching this one.
