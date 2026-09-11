# Contributing

The most valuable contribution to this project is **telling us where the
simulation is wrong.**

`streamdouble` claims that an agent cannot tell it from Twilio. That claim is
only as good as our reading of the protocol, and the Twilio documentation does
not cover everything — several behaviours here are inferences, marked as such in
the code. If you have watched a real Twilio call do something this tool does
not, that is the bug report we most want.

## Reporting a protocol difference

Include what Twilio actually did. A frame capture, a log line, a screenshot of
Twilio's debugger — anything that shows real behaviour beats a description of
it, because the whole question is what the real thing does.

If you cannot share a capture, say what you observed and how you observed it.
"Our agent works on real calls but hangs against streamdouble" is a useful
report even without a capture; it tells us where to look.

Known inferences, each documented at the code that makes them:

- **Packet loss** is modelled as audio Twilio never received, so a lost frame is
  never sent and `media.timestamp` jumps. Twilio might instead substitute
  silence. See `chaos.py`.
- **Mark echo timing** is derived from a playback clock we maintain. Twilio's
  real timing may differ under load. See `session.py`.
- **Frame size**: we always send 160 bytes. Twilio does too, as far as we know,
  but this is not documented as a guarantee. See `audio.py`.

## Working on the code

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

The session tests start a real server on a real socket; they are slower than the
rest and that is deliberate — the lifecycle bugs worth catching do not exist
without one.

### Things this project is strict about

Each of these exists because of a specific bug that reached the repository, and
each is written up in `PLAN.md` with the story attached.

- **Verify protocol details against the live Twilio docs, never from memory.**
  The original plan's protocol table was wrong in three ways.
- **`None` is not zero.** An agent that never spoke has no latency measurement.
  Rendering that as `0 ms` turns the worst outcome into the best one.
- **Measurement honesty over clean-looking numbers.** Pacing lateness is
  reported alongside the latency it could have distorted. A latency tool that
  reports wrong latency is worse than no tool, because people trust it.
- **Time is `time.perf_counter`.** `time.monotonic` is `GetTickCount64` on
  Windows, at 15.6 ms — coarser than the frame being measured. This was found
  quantising every number the tool produced.
- **Warnings are errors.** The few ignores are third-party and each is annotated
  with why.
- **`protocol.py` and `audio.py` stay pure.** No network, no clock.

### Tests

A fix needs a test that fails against the old behaviour. If the bug was in
timing or concurrency, prefer a test that reproduces the condition over one that
asserts on structure — `tests/test_review_gate_*.py` are written that way, and
each names the failure it prevents.

Property tests and mutation checks are welcome. There is a mutation-testing
habit in this repository rather than a tool: deliberately break something, and
confirm a test written for it fails. Several fixes here have been verified that
way, and one of them found that a fix had introduced a new bug.

## Adding a scenario

Scenarios in `scenarios/` are examples as much as tests. Keep clips small, and
write the comments for someone who has not read the source.

If the scenario depends on the agent *understanding* the caller — barge-in,
anything answering a question — it needs real speech. Use
`python fixtures/make_speech.py`, which synthesises transcribable clips from the
OS's own TTS into the gitignored `fixtures/speech/`. The committed synthetic
fixtures are speech-like and no transcription service will turn them into words,
so a scenario driven by one fails for reasons that have nothing to do with the
agent under test.

## What is out of scope

Stated to save you the work, not to be unwelcoming:

- LLM-as-judge or transcript scoring
- Multi-turn conversation simulation
- A web UI or dashboard
- Being a voice agent framework

The [comparison table](README.md#how-this-compares) says who does those well.
`streamdouble` does one thing, and the narrowness is the point.

## Not affiliated with Twilio

This project is not affiliated with, endorsed by, or sponsored by Twilio. It
simulates the publicly documented Twilio Media Streams protocol. Please do not
contribute anything obtained under an NDA or from a private beta.
