# CLAUDE.md

Guidance for Claude Code when working in this repository.

## The directory name is wrong

This folder is called `dialtone`, but the project is **`streamdouble`**. The
package was renamed on 2026-09-10 — `dialtone` turned out to be an active
package on PyPI. The folder kept its old name; nothing else did.

## Read PLAN.md first

`PLAN.md` is the authority on what this project is, how it is built, and where
it currently stands. Its **Progress** section is written as a handover: what
exists, what each review gate found, what is owed, and what to do next. Start
there rather than inferring state from the code.

## How work proceeds here

The project is built in phases, with a **mandatory review gate between each
one** (`/code-review high`, plus `/security-review` at gate 4). Gates are not
optional and are not to be skipped or deferred — every gate so far has found
real bugs, including two that were introduced by the previous gate's own fixes.

When a gate finds something, fix it *and* write a regression test that fails
against the old behaviour. `tests/test_review_gate_1.py` and
`tests/test_review_gate_2.py` follow that pattern.

## What this project is unusually strict about

These are not stylistic preferences; each one exists because of a specific bug
that reached the repo.

- **Verify protocol details against the live Twilio docs, never from memory.**
  The original plan's protocol table was wrong in three ways. Cite the source in
  a docstring.
- **`None` is not zero.** An agent that never spoke has
  `time_to_first_audio = None`. Rendering that as "0 ms" turns the worst outcome
  into the best one on screen.
- **Measurement honesty over clean-looking numbers.** Pacing lateness is
  reported alongside the latency it could have distorted. A latency tool that
  reports wrong latency is worse than no tool, because people trust it.
- **Warnings are errors** (`filterwarnings = ["error"]`). Several real bugs
  surfaced first as warnings. The few ignores are third-party and each is
  annotated with why.
- **The golden file pins exact bytes.** Regenerate it (`UPDATE_GOLDEN=1`) only
  deliberately, with a reason in the commit message. Nothing feeding it may
  depend on `np.random` — NumPy does not guarantee `Generator` streams across
  builds, machines or CPUs.
- **`protocol.py` and `audio.py` stay pure.** No network, no clock. If a bug
  there needs a WebSocket to reproduce, the layering is wrong.
- **Time is `time.perf_counter`.** It must be monotonic — an NTP step mid-call
  would otherwise produce a negative latency reported with a straight face —
  *and* high resolution. `time.monotonic` looks right and is not: on Windows it
  is `GetTickCount64` at 15.625 ms, coarser than the 20 ms frame being measured.
  Gate 3 found it quantising every latency and turning the pacer's lateness
  statistics into a readout of the clock.

## Commands

```bash
pip install -e ".[dev]"
pytest                      # the session tests use a real socket
ruff check .
python fixtures/make_fixtures.py    # only if a fixture definition changed
```

Run the example agent and call it:

```bash
python examples/echo_agent.py --port 8000
streamdouble call ws://localhost:8000/media-stream --audio fixtures/speech_8k.wav --out reply.wav
```

The echo agent takes query parameters to misbehave on purpose: `mode=silent`,
`delay_ms=`, `hangup_after=`, `garbage_after=`, `clear_after=`.

## Testing against a real agent

The example agent is enough for most work, but it is *our* code, written from
the same reading of the Twilio docs as `protocol.py` -- a shared misreading
would leave every test green. Validating against an implementation we did not
write is what catches that, and it has already found things the example agent
structurally could not.

Point streamdouble at the agent's own `/media-stream`. Agents commonly
authenticate on `start.customParameters` rather than the URL, so
`--param key=...` is usually what is needed.

**Disable whatever the agent touches on hangup first** -- CRM pushes, webhooks,
billing, notifications. A simulated call is a real call to everything downstream
of the socket. The first time this was done for real, a completed test call
wrote records to a live CRM for a made-up phone number.

Two things a real agent taught us that the example agent could not, both in
PLAN.md: agents batch outbound audio into frames of roughly 8000 bytes rather
than 160, and mark echo is load-bearing for hangup.
