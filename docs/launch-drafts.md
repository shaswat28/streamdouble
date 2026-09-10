# Launch drafts

Nothing here has been posted. These are drafts to edit and send yourself.

A note on framing, because it decides how these land. The plan put it well:
*"Frame it as complementary, not competitive."* Pipecat and LiveKit maintainers
are the people most likely to amplify a tool that makes their users' agents
better, and least likely to amplify something positioned against them. "Test
your LiveKit agent's Twilio seam" is a friendly pitch. "LiveKit alternative" is
a fight that loses on the merits, because their testing tools genuinely do
things this one does not.

The strongest thing to lead with is the bug it found, not the feature list.
Everyone claims their tool finds bugs; a specific one with numbers is different.

---

## Show HN

**Title:** `Show HN: Streamdouble – Test a Twilio voice agent without placing a call`

Testing a Twilio voice agent usually means: start the app, start ngrok, trigger
a call, make a real phone ring, talk to your own bot until you reach the code
path you changed, and get billed for the minute. It cannot run in CI, and the
Twilio↔app seam — where audio format mismatches, framing errors and silent
packet drops actually live — is exactly the seam no existing tool exercises.

Streamdouble speaks the Twilio Media Streams protocol at your WebSocket
endpoint: μ-law framing, 20 ms real-time pacing, mark echo, DTMF, `clear` on
barge-in. Framework-agnostic, so it works against a hand-rolled FastAPI handler
as readily as against Pipecat or LiveKit.

The first time I pointed it at a production agent it found a barge-in bug I
would not have thought to look for. The agent streamed 9.5 seconds of speech in
0.85 seconds — TTS runs far faster than real time — and cleared its "am I
speaking" flag when *sending* finished. But Twilio buffers outbound audio and
plays it at the caller's pace, which is why `mark` exists. So for 8.7 of every
9.5 seconds it spoke, the caller could talk and the agent would not stop. Every
transcript was correct, so no transcript-scoring tool would have seen it.

What it deliberately does not do: judge whether your agent is any *good*. No
personas, no LLM-as-judge, no transcript scoring. Pipecat Evals, LiveKit's test
framework and the hosted QA vendors all do that, most of them better than I
would. This tests the transport underneath them.

Some of what it took to be honest about timing is written up in the repo — the
codec disagreeing with the reference implementation on negative samples, and
`time.monotonic` having 15.6 ms resolution on Windows, which had been quantising
every number the tool produced onto a grid coarser than the frame it was
measuring.

Apache-2.0. https://github.com/shaswat28/streamdouble

---

## r/twilio

**Title:** `Built a Media Streams simulator so I could stop calling my own bot`

If you have a `<Connect><Stream>` endpoint, you have probably done the loop:
ngrok, trigger a call, talk to your own bot, repeat. I got tired of it and wrote
a simulator that speaks the Media Streams protocol directly at the endpoint.

```
streamdouble call ws://localhost:8000/media-stream --audio hello.wav --out reply.wav
```

No account, no tunnel, no charge. `reply.wav` is what your agent said.

Three protocol details it turned out to be worth getting exactly right, in case
they save anyone else the trouble:

- `sequenceNumber`, `chunk` and `timestamp` arrive as **JSON strings**, while
  `sampleRate` and `channels` in the same payload are numbers.
- `dtmf.track` is the literal string `"inbound_track"` — not `"inbound"`, which
  is what every other track field uses.
- Twilio echoes a `mark` back only once the audio queued before it has finished
  **playing**, not when you finish sending. Agents that treat those as the same
  instant go deaf to barge-in for the difference, which can be most of an
  utterance.

Not affiliated with Twilio. Apache-2.0.
https://github.com/shaswat28/streamdouble

---

## r/voiceai

**Title:** `Your agent may be unable to hear interruptions for most of every sentence`

A pattern worth checking, which I found while building a Media Streams
simulator and then found in a real agent.

TTS streams much faster than real time. Measured on a production agent: 9.52
seconds of speech handed over in 0.85 seconds, about 11x. Twilio buffers that
audio and plays it to the caller at normal speed.

So "we finished sending" and "the caller finished hearing" are seconds apart. If
barge-in is gated on a flag cleared when the send loop finishes — which is the
obvious way to write it — the agent is deaf to interruption for the entire
remaining playback. In that case 8.67 seconds out of 9.52.

The symptom is subtle: transcripts are perfect, latency looks fine, and callers
just report that the bot talks over them. It is invisible to transcript scoring
because nothing about the transcript is wrong.

The fix is to gate on the `mark` echo instead, which Twilio returns only once
the audio has actually played. The tool reports `delivery_ratio` and
`playback_tail_ms` so you can see the gap on your own agent:
https://github.com/shaswat28/streamdouble

---

## Pipecat / LiveKit community channels

Shorter, and complementary in tone. These are the people whose users benefit,
and their own testing tools cover ground this one does not.

> Built a Twilio Media Streams simulator for testing the transport seam locally
> — μ-law framing, 20 ms pacing, mark echo, packet loss and jitter — without a
> phone or a tunnel. It is framework-agnostic, so it works against a
> {Pipecat,LiveKit} Twilio transport the same as anything else.
>
> It deliberately does no conversation testing: {Pipecat Evals,your test
> framework} already covers whether the agent says the right thing, and this
> sits underneath, on whether the audio reaches it correctly in the first place.
> Mostly useful for "works on a real call, breaks locally" and the reverse.
>
> https://github.com/shaswat28/streamdouble

---

## On livekit/agents#3379

**Recommendation: do not comment.**

The plan said to comment there *if the tool reproduces the issue*. It does not,
and the distinction matters.

That issue is a WebSocket connecting through ngrok and then never delivering
media. Streamdouble replaces Twilio and runs locally, so there is no tunnel in
the path — it cannot reproduce a tunnel fault. What it would have done is
**bisect** the problem: run it against the same endpoint, see media flow
correctly, and the fault is isolated to the tunnel rather than the agent, in
about a minute rather than a day.

That is a genuinely useful thing to be able to say, but it is not what the issue
asked for. The issue is also **closed**, so a comment would be a resolved thread
receiving what reads as promotion.

Better use of the same material: keep it as an example of the class of problem
this bisects — "works on a real call, breaks locally, and you cannot tell which
half is at fault" — in the README or a write-up, where it is illustrating a
point rather than advertising on someone's bug report.
