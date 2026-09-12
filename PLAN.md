# streamdouble — Build Plan

> A framework-agnostic Twilio Media Streams simulator. Test your voice agent's
> WebSocket endpoint locally, at full protocol fidelity, without placing a real call.

**Status:** All six phases complete, gates 1-4 passed. Public and published on
PyPI. Gate 5's clean-install half is done -- the package was installed from
PyPI into an empty virtualenv and exercised; what is still owed is someone who
is not the author reading the README cold. Phases 7-9 are planned -- see
the [post-launch roadmap](#3b-phases-7-9--the-post-launch-roadmap).
**License:** Apache-2.0
**Language:** Python 3.11+
**Name:** `streamdouble` — `dialtone` was taken on PyPI. See [Progress](#progress).
**Repo:** [shaswat28/streamdouble](https://github.com/shaswat28/streamdouble)

> See [Progress](#progress) at the end for what is built, what each review gate
> found, and what the next session should pick up.

---

## The one-line pitch

Pipecat tests Pipecat. LiveKit tests LiveKit. Twilio mocks its REST API and its
webhooks — but not Media Streams. `dialtone` tests **anything that speaks the
Twilio Media Streams protocol**, including the hand-rolled FastAPI endpoint that
a large share of voice-agent developers actually have.

## The problem, concretely

To test a one-line change in a Twilio voice agent today you must:

1. Start your app server
2. Start an ngrok tunnel (public URL, changes unless reserved)
3. Trigger an outbound call
4. **A real phone rings a real person**
5. Talk to your own bot to reach the code path you changed
6. Get billed for the minute

This is slow, costs money, cannot run in CI, and is impossible to run in a
tight loop. Worse, the Twilio↔app seam is where the nastiest bugs live —
audio format mismatches, framing errors, silent packet drops — and it is
exactly the seam no existing tool exercises.

## Non-goals (v1)

Stating these up front because each one is a crowded market on its own:

- **LLM-as-judge / transcript scoring** — Hamming, Cekura, LangWatch, Maxim own this
- **Multi-turn conversation simulation** — LiveKit ships this first-party
- **A web UI or dashboard** — CLI and CI are the product
- **Being a voice agent framework** — Pipecat and LiveKit exist and are good
- **Replacing ngrok for webhook testing** — Twilio's CLI webhook plugin does that

`dialtone` does one thing: it lies to your WebSocket endpoint convincingly
enough that your agent cannot tell it isn't Twilio.

---

## 0. Naming & prerequisites

**Before writing any code:**

- [x] ~~Check `dialtone` availability on PyPI and GitHub.~~ **Taken.** `dialtone`
      0.0.39 on PyPI is an active SDK for the Dialtone API (Apache-2.0, not
      abandoned). Renamed to **`streamdouble`**, verified free on PyPI (404).
- [ ] **Do not** name the package `twilio-*`. Twilio's trademark policy restricts
      names implying official affiliation. Use a neutral name and describe
      compatibility in the README ("simulates the Twilio Media Streams protocol")
- [x] ~~Confirm the protocol details in §2 against the current Twilio docs.~~
      **Done, and §2 below was wrong in three ways.** Corrections applied and
      cited in `protocol.py`'s docstring — see [Progress](#progress).
- [x] ~~Capture 3–4 short WAV fixtures.~~ **Twelve generated fixtures**, built
      by `fixtures/make_fixtures.py` from closed-form signals so they are
      bit-reproducible (a recording cannot be regenerated if lost, and the
      golden-file test pins exact bytes). Real recorded speech is still worth
      adding for the by-ear check — drop WAVs alongside them, nothing depends
      on their absence.

**Prerequisites:** access to a working `/media-stream` endpoint belonging to a
real voice agent. That is the reference implementation to validate against, and
it is a significant head start — most new tools have nothing real to test against.

> ✅ **Done, 2026-09-10.** streamdouble drove a real production voice
> agent's `/media-stream` endpoint end to end and recovered 11.89 s of
> genuine synthesised speech — no phone, no ngrok, no Twilio. See
> [Validated against a real voice agent](#validated-against-a-real-voice-agent).

---

## 1. Architecture

```
┌─────────────────┐         WebSocket          ┌──────────────────────┐
│    dialtone     │ ─────────────────────────► │  Your voice agent    │
│                 │   connected / start /      │  (FastAPI, Node,     │
│  ┌───────────┐  │   media / stop             │   anything)          │
│  │ WAV → μlaw│  │                            │                      │
│  │  encoder  │  │ ◄───────────────────────── │  STT → LLM → TTS     │
│  └───────────┘  │   media / mark / clear     │                      │
│  ┌───────────┐  │                            └──────────────────────┘
│  │  pacer    │  │
│  │ (20ms rt) │  │   Everything below is local. No tunnel, no phone,
│  └───────────┘  │   no Twilio account, no charges.
│  ┌───────────┐  │
│  │ recorder  │  │
│  │ + timing  │  │
│  └───────────┘  │
└─────────────────┘
```

**Module layout:**

| Module | Responsibility |
|---|---|
| `protocol.py` | Twilio frame construction/parsing. Pure functions, no I/O |
| `audio.py` | WAV ↔ μ-law 8kHz conversion, 20ms chunking |
| `pacer.py` | Real-time frame scheduling with drift correction |
| `session.py` | WebSocket lifecycle, orchestrates the above |
| `metrics.py` | Latency capture, timing report |
| `cli.py` | Argument parsing, output formatting, exit codes |

**Design rule:** `protocol.py` and `audio.py` must be pure and independently
testable with zero network. If a bug requires a live WebSocket to reproduce,
the layering is wrong.

---

## 2. Protocol reference (VERIFY BEFORE USE)

Twilio Media Streams, bidirectional (`<Connect><Stream>`).

**Inbound to your app** (what `dialtone` sends):

| Event | When | Key fields |
|---|---|---|
| `connected` | First message | `protocol`, `version` |
| `start` | Once, after connected | `streamSid`, `callSid`, `tracks`, `mediaFormat`, `customParameters` |
| `media` | Every 20ms | `streamSid`, `sequenceNumber`, `timestamp`, `media.payload` (base64) |
| `stop` | End of stream | `streamSid`, `accountSid`, `callSid` |

**Outbound from your app** (what `dialtone` must accept):

| Event | Purpose |
|---|---|
| `media` | Audio to play to caller — `{event, streamSid, media: {payload}}` |
| `mark` | Playback checkpoint — app sends, Twilio echoes back when audio finishes |
| `clear` | Flush buffered audio (barge-in / interruption) |

**Audio format:** `audio/x-mulaw`, 8000 Hz, mono. 20ms per frame = **160 bytes**
of μ-law per frame, base64-encoded in `media.payload`.

> **These specifics are the entire value of the tool.** Getting the frame size,
> pacing, or base64 layering wrong makes it a toy. Verify every field against
> the live docs in step 0, and write the citation into a docstring so future-you
> knows where it came from.

---

## 3. Phased build

Each phase ends in a working, committed, demonstrable state. Do not start a
phase before its predecessor's review gate passes.

### Phase 1 — Protocol core (no network) ✅ DONE

**Goal:** Construct and parse every Twilio frame type correctly, offline.

- Repo init: AGPL-3.0 LICENSE, README stub, `pyproject.toml`, ruff + pytest
- `protocol.py`: builders for `connected`/`start`/`media`/`stop`, parser for
  inbound `media`/`mark`/`clear`
- `audio.py`: WAV load → resample to 8kHz mono → μ-law encode → 160-byte frames;
  and the reverse for recording
- Unit tests: round-trip WAV → μ-law → WAV, assert acceptable loss;
  frame size exactly 160 bytes; base64 layering correct
- Golden-file test: a known WAV produces a byte-identical frame sequence

**Done when:** `pytest` passes and you can print a valid `start` frame that
matches the documented shape field-for-field.

> ### ✅ REVIEW GATE 1 — Protocol correctness — PASSED
>
> Ran, 5 findings, all fixed with regression tests in
> `tests/test_review_gate_1.py`. See [Progress](#progress).
> **The by-ear check is now done** (2026-09-11) -- see
> [The by-ear check](#the-by-ear-check-done-2026-09-11).
>
> <details><summary>Original gate text</summary>
>
> ### ⛔ REVIEW GATE 1 — Protocol correctness
> Run `/code-review high`. This is the highest-stakes review in the project:
> **every later phase inherits these bugs**, and protocol errors surface as
> "the audio sounds like static" three phases later.
> Focus: μ-law encoding correctness, off-by-one in framing, base64 double-encoding,
> endianness, resampling quality, silent exception swallowing.
> Additionally: hand-decode one `media.payload` back to a WAV and **listen to it**.
> Tests can pass while the audio is garbage.
>
> </details>

---

### Phase 2 — Live session against your own endpoint ✅ DONE (validated against a real agent)

**Goal:** A real WebSocket conversation with a real agent's `/media-stream`.

- [x] `session.py`: connect, send `connected` → `start`, stream frames, handle `stop`
- [x] `pacer.py`: absolute deadlines from a fixed origin, not accumulated sleeps
- [x] Receive loop: collect outbound `media`, decode, write to a WAV
- [x] **`mark` is echoed, not just logged** — moved forward from Phase 4. Twilio
      returns a mark once the audio queued before it has finished playing, and
      agents gate turn-taking on that echo; one that only logs would hang them.
      The session models a playback clock to time the echo, and a `clear`
      discards the marks queued behind the discarded audio.
- [x] CLI: `streamdouble call ws://localhost:8000/media-stream --audio hello.wav --out response.wav`

**Done when:** you run it against a real agent, and `response.wav` contains its
actual spoken reply. **This is the moment the project becomes real** —
if this works, everything after is refinement.

> ### ✅ REVIEW GATE 2 — Concurrency & lifecycle — PASSED
>
> Ran, 4 findings, all fixed and mutation-verified in
> `tests/test_review_gate_2.py`. See [Progress](#progress).
> **Caveat: initially closed against the echo agent alone, and re-closed
> later against a real agent.**
>
> <details><summary>Original gate text</summary>
>
> ### ⛔ REVIEW GATE 2 — Concurrency & lifecycle
> Run `/code-review high`. Async WebSocket code fails in ways tests miss.
> Focus: send/receive task coordination, unhandled task exceptions vanishing,
> cleanup on disconnect, resource leaks on error paths, timing drift under load,
> what happens when the server closes mid-stream or never responds.
>
> </details>

---

### Phase 3 — Metrics that justify the tool ✅ DONE

**Goal:** Report the numbers that matter for voice UX.

- [x] **Time to first audio byte** — `None` when the agent never spoke, never 0
- [x] Inter-frame gaps: mean, P95 (withheld below 20 samples), max, and stalls
- [x] Total response duration, frame counts sent/received
- [x] Protocol violations detected and reported by code, without ending the call
- [x] `--json` output for CI consumption, stdout-clean so it can be piped
- [x] Exit codes: `0` pass, `1` threshold failed, `2` timeout, `3` protocol
      violation, `4` usage, `5` could not connect

- [x] Threshold flags: `--max-first-audio-ms`, `--max-gap-ms`, `--allow-no-audio`

On the 800 ms figure: the plan asked for a citation, and getting one separated
two claims that are usually blurred together. **200 ms is research** — Stivers
et al., PNAS 2009, found a modal between-turn gap of 0–200 ms across ten
typologically diverse languages. **800 ms is an industry rule of thumb**, not a
finding. `metrics.py` says exactly that rather than borrowing the paper's
authority for the round number.

**Done when:** `dialtone call ... --max-first-audio-ms 800 --json` is usable
as a CI gate.

> ### ✅ REVIEW GATE 3 — Measurement validity — PASSED
>
> Ran, 4 findings, all fixed and mutation-verified in
> `tests/test_review_gate_3.py`. The delay-injection sanity check the gate
> asks for is committed as `tests/test_measurement_validity.py` rather than
> performed by hand, because a manual check gets done once and then quietly
> stops happening. See [Progress](#progress).
>
> <details><summary>Original gate text</summary>
>
> ### ⛔ REVIEW GATE 3 — Measurement validity
> Run `/code-review high`. A latency tool that reports wrong latency is worse
> than no tool, because people will trust it.
> Focus: are you measuring what you claim? Does your own processing time leak
> into the measurement? Monotonic vs wall clock? Is "first audio byte" the frame
> arrival or the decode completion? Off-by-one-frame (20ms) errors.
> **Sanity check:** inject a deliberate 2-second delay in a stub server and
> confirm the tool reports ~2s. Do not skip this.
>
> </details>

---

### Phase 4 — Realism ✅ DONE

**Goal:** Reproduce the failure modes that actually bite in production.

- [x] **Barge-in:** `expect: clear` in a scenario. Found a real bug in
      a real agent doing it — see [Progress](#progress).
- [x] **Network chaos:** `--packet-loss`, `--jitter`, `--latency`, all seeded so
      a failing run replays exactly.
- [x] **Silence / no-input:** and a fidelity bug fixed in the process — `wait`
      streams silence rather than stopping, because a real call never goes quiet.
- [x] **Abrupt hangup:** the `hangup` step, closing without `stop`.
- [x] **DTMF** — verified against the live docs: it *is* an inbound event on
      bidirectional streams, and `dtmf.track` is the literal `"inbound_track"`.
- [x] Scenario files (YAML), validated before the socket opens.

**Done when:** you can reproduce a real bug from a real agent.
That is the proof the tool has value beyond the happy path.

> ### ✅ REVIEW GATE 4 — Full review + security — PASSED
>
> Ran. 4 findings, all fixed with regression tests in
> `tests/test_review_gate_4.py`. The headline one was measured, not
> theorised: a flooding endpoint drove 206 MB of buffered audio and 436 MB
> of peak memory in a five-second call. See [Progress](#progress).
>
> <details><summary>Original gate text</summary>
>
> ### ⛔ REVIEW GATE 4 — Full review + security
> Run `/code-review high` and `/security-review`.
> Security surface is small but real: you accept a WebSocket URL and a file path
> from the CLI, decode untrusted base64 from a remote server, and write files to
> disk. Check for path traversal on `--out`, unbounded memory growth on a server
> that streams forever, and decompression/allocation bombs from malformed payloads.
>
> </details>

---

### Phase 5 — Ship it ✅ DONE (up to publishing)

**Goal:** Someone who isn't you can use it in under five minutes.

- [x] **README** — pitch above the fold, demo, quickstart, comparison table,
      not-affiliated line.
  - [x] The demo is an **animated SVG generated from a real run**
        (`tools/make_demo.py`), not a hand-typed mock-up. Sharper than a GIF, a
        fraction of the bytes, diffable, and it cannot drift from what the tool
        actually prints. Line timings are the real ones; the summary block
        cascades for legibility, which is stated in the generator.
  - [x] The comparison table says plainly what Pipecat Evals, LiveKit's test
        framework and the hosted QA vendors do better, and states outright that
        streamdouble "says nothing about whether your agent is any good".
- [x] `pipx`/pip install path verified: the wheel installs into a clean venv
      with only numpy, PyYAML and websockets, and the optional soxr path fails
      with a sentence naming the fix.
- [x] GitHub Actions running the test suite (added back in Phase 1).
- [x] `examples/echo_agent.py`, with deliberate misbehaviour modes.
- [x] CONTRIBUTING.md and issue templates. The protocol-difference template
      leads with "tell us where the simulation is wrong", because that is the
      most valuable report this project can receive.
- [x] **PyPI publish — done.** `.github/workflows/release.yml` stays
      manual-dispatch with `publish: false` by default, so no release happens
      by accident. 0.1.0.dev0 went out first; 0.1.0 is the real one.

> ### 🟡 REVIEW GATE 5 — Fresh-eyes pass — PARTLY DONE
>
> The clean-machine half is done and already earned its keep: building the
> wheel and installing it into an empty virtualenv is what proves the
> dependency list is honest, and CI caught PyYAML being used-but-undeclared
> exactly that way. Verified: installs with only numpy, PyYAML and
> websockets; `streamdouble --version` runs; every module imports; the
> optional soxr path fails with a sentence naming the fix.
>
> **Still owed:** installing *from PyPI* and following the README literally,
> which cannot happen until there is something on PyPI. And a human who is
> not the author reading it cold. A close read of the README has since found
> one shipped copy-paste failure and a test that could not see it -- see
> [Gate 5's first finding](#gate-5s-first-finding-from-reading-rather-than-fresh-eyes).
>
> <details><summary>Original gate text</summary>
>
> ### ⛔ REVIEW GATE 5 — Fresh-eyes pass
> Run `/code-review high` on the full diff, then do something no review can do:
> **install from PyPI on a clean machine and follow your own README literally.**
> Every stumble is a user you would have lost. Fix them before launch, not after.
>
> </details>

---

### Phase 6 — Distribution ✅ DONE

- [x] **Claude Code skill wrapper** — `skill/SKILL.md`. Carries the judgement
      as well as the commands: disable what the agent touches on hangup before
      the first call, never report a `null` latency as fast, and do not diagnose
      barge-in with synthetic audio.
- [x] **Launch posts drafted** — `docs/launch-drafts.md`, for Show HN, r/twilio,
      r/voiceai and the framework community channels. **Nothing posted.** Each
      leads with the barge-in bug rather than a feature list, because everyone
      claims their tool finds bugs and a specific one with numbers is different.
- [x] **The μ-law/framing write-up** — `docs/mulaw-gotchas.md`.
- [ ] ~~Comment on [LiveKit issue #3379](https://github.com/livekit/agents/issues/3379)~~
      **Recommended against, and the reason is worth keeping.** That issue is a
      WebSocket connecting through ngrok and then never delivering media.
      streamdouble replaces Twilio and runs locally, so there is no tunnel in
      the path — it cannot reproduce a tunnel fault. What it does is *bisect*
      one: run it against the same endpoint, watch media flow correctly, and the
      fault is isolated to the tunnel rather than the agent. That is genuinely
      useful and is not what the issue asked for. The issue is also closed, so a
      comment would be a resolved thread receiving what reads as promotion.

---

## 3b. Phases 7-9 — the post-launch roadmap

*Planned 2026-09-10, after 0.1.1. Phases 1-6 built a tool that works; these
three address the reason someone would keep using it after the first call.*

Three gaps motivate them:

1. **It is a command, not a dependency.** Every CI user shells out and parses
   JSON. Voice developers test in pytest, and streamdouble does not live there.
2. **A latency number is a reading, not a baseline.** An absolute
   `--max-first-audio-ms 800` gate cannot see 300 ms become 700 ms. That is the
   regression teams actually care about and the one the tool is blind to.
3. **It simulates half the protocol.** Only `<Connect><Stream>`. The
   `<Start><Stream>` fork -- what transcription and compliance-recording apps
   consume -- is unmodelled.

| Phase | Thesis | Version |
|---|---|---|
| **7 — Become a dependency** | streamdouble stops being a command you shell out to and becomes something you `import` | 0.2.0 |
| **8 — One call becomes a trend** | A latency number stops being a reading and becomes a baseline you can regress against | 0.3.0 |
| **9 — The other half of the protocol** | The `<Start><Stream>` fork, and both tracks rendered into one listenable file | 0.4.0 |

> **The version column is not a release schedule.** Decided 2026-09-11:
> **nothing is published to PyPI until Phase 9 is done.** Those numbers say
> what each phase's code *is* -- 0.2.0 adds an API, 0.4.0 changes bytes on the
> wire -- not that each one ships on completion. PyPI stays at 0.1.0 until the
> roadmap is finished, and the next upload is whatever version is current then.
> Version numbers on PyPI need not be contiguous, and a jump from 0.1.0 is
> normal and fine.
>
> This is worth stating because the table by itself reads as a release
> schedule, and a later session that bumps the version at the end of Phase 7
> and reaches for `release.yml` would be following the plan as written. It
> would also be irreversible: PyPI never allows a version number to be reused.

**Sequencing.** Phase 7 is the multiplier -- every later feature is worth more
with users. Phase 8 is strictly downstream of it: comparing baselines means
comparing a *structured result*, which is what Phase 7 promotes from an
implementation detail of `cli.py` into a versioned object. Phase 9 is last
because it is the only one with a hard external dependency -- a foreign
`<Start><Stream>` consumer to validate against -- and because it can use Phase
7's trace and Phase 8's repeat harness as instruments rather than growing its
own.

**The non-goals still hold.** Nothing here scores a transcript, simulates a
turn, or draws a dashboard. One guard worth writing down: once the pytest
plugin exists, the obvious next request is `assert_transcript_contains(...)`.
That is LLM-as-judge with extra steps, and it is the first non-goal on the list.

### Phase 7 — Become a dependency (0.2.0)

- `api.py`: `CallReport`, `async call()`, `async run_scenario()`, sync wrappers
  that refuse to run inside a live loop with a sentence naming the async form.
- `cli.py` refactored to *call* `api` rather than duplicate it. `place_call`
  currently interleaves running, computing, WAV writing, JSON rendering and
  exit coding. Two code paths for "place a call" are two paths that drift --
  which is exactly how `streamdouble scenario` shipped unreachable.
- `pytest_plugin.py` via `[project.entry-points.pytest11]`: a `simulated_call`
  fixture, a `streamdouble_config` fixture, and `pytest_assertrepr_compare` so
  a failed latency assertion prints the summary block rather than `None < 800`.
  **Not** in scope: starting the user's app. The plugin takes a URL.
- `trace.py` and `--trace out.jsonl`: one JSON object per frame each way.
  Closes the note in `session.py`. Payloads are opt-in (`--trace-payloads`) --
  a 60 s call is ~30 MB of base64 nobody reads.
- `Metrics.to_dict()` gains `schema_version`. It ships now and is consumed in
  Phase 8; adding it *after* baselines exist is the release where you find out
  you cannot.

> ### ✅ REVIEW GATE 6 — API extraction + observation cost — PASSED
>
> Ran. 4 findings, all fixed and mutation-verified in
> `tests/test_review_gate_6.py`. `/security-review` found nothing. Two of the
> four were repeats of earlier gates' findings, in new code written by someone
> who knew about them. See [Gate 6](#phase-7-and-gate-6).
>
> <details><summary>Original gate text</summary>
>
> ### ⛔ REVIEW GATE 6 — API extraction + observation cost
> `/code-review high` and `/security-review`.
> **Focused risk: the observer perturbing the observed, and the refactor
> forking the path it was meant to unify.**
> The trace writer is file I/O inside the receive loop -- the identical shape
> to gate 2's quadratic `audio_received +=` finding. Assert a traced and an
> untraced run report the same figures, extending the delay-injection harness
> in `tests/test_measurement_validity.py`.
> The trace is also a secret-exfiltration surface: `--param` is how agents
> authenticate, the trace records the `start` frame, and the issue template
> asks people to attach reproductions. Redact `customParameters` by default.
> Plus loop reentrancy in the plugin, and `filterwarnings = ["error"]` turning
> a plugin DeprecationWarning into a collection failure for every user.
> Verify the refactor by mutation: break `exit_code_for` and confirm the CLI
> test *and* the API test both go red. If only one does, the paths are still
> separate.
>
> </details>

### Phase 8 — One call becomes a trend (0.3.0)

- `-n / --repeat N`, sequential only. Concurrent calls contend for the event
  loop and corrupt the pacing statistics that are the tool's honesty claim.
- `aggregate.py`: per-metric `n`, `n_measured`, min, median, max, p95, stddev.
  `metrics.percentile` and `MIN_SAMPLES_FOR_PERCENTILE` are reused verbatim,
  not re-derived -- across-run first-audio P95 is the genuinely missing metric
  and it is exactly the one needing 20 runs to be honest, so `-n 5` publishes a
  median and withholds the P95, and says so.
- `--baseline` / `--save-baseline`. Refuses to compare incomparable runs: the
  baseline records `schema_version`, the clip's SHA-256, the chaos seed,
  impairments and `n`, because a different clip is a different experiment
  rather than a regression. A regression is a median exceeding the baseline by
  more than *both* `--tolerance-pct` (25%) and `--tolerance-ms` (50 ms), so a
  40%-worse 5 ms figure is not a build failure. A metric that was `None` in
  either series is incomparable, never an improvement -- "None is not zero"
  applies to deltas, and deltas are where it is easiest to get wrong.
  Regression exits 1; no sixth exit code is invented.
- `action.yml`, a composite GitHub Action, plus one copyable workflow.

> ### ⛔ REVIEW GATE 7 — Statistics, and a YAML-to-shell seam
> `/code-review high`, and `/security-review` for `action.yml` alone.
> **Focused risk: aggregation is where every honesty rule this project holds
> gets quietly violated, because averaging is so natural.** A run where the
> agent never spoke contributes `None` -- dropping it silently reports the mean
> of the *successful* runs as the mean of all runs, and coercing it to 0 or the
> timeout is worse. It must read `n=20, measured=17`. Is p95 over 5 runs the
> max wearing a statistical hat? Does a regression verdict survive being run
> twice -- a known 300 ms delta detected, and 20 clean pairs with zero false
> positives? And `${{ inputs.url }}` interpolated into a `run:` block is
> command injection into every consumer's CI runner.

### Phase 9 — The other half of the protocol (0.4.0)

**Protocol verification, done at planning time** against
https://www.twilio.com/docs/voice/media-streams/websocket-messages:

- Confirmed, verbatim: *"Twilio sends the `mark` event only during
  bidirectional Streams."* A fork therefore has **no mark echo and no
  `clear`**. That is the design pivot, not a detail.
- Confirmed: `media.track` is `"inbound"`/`"outbound"` and `start.tracks` is an
  array of those, while the **TwiML** attribute uses `inbound_track`,
  `outbound_track`, `both_tracks`. The same trap as `dtmf.track`, and it earns
  a fourth citation in `protocol.py`'s docstring.
- Confirmed: `<Connect><Stream>` is the bidirectional case, so today's
  `["inbound"]` default is correct and stays correct.
- **Not confirmed, and to be recorded as an inference rather than as fact:**
  the docs state the bidirectional case affirmatively but never say that a
  unidirectional app sending media commits a violation. Treating it as one is
  the useful behaviour -- a simulator that accepts outbound audio on a fork
  hides the bug it exists to find -- but it goes into `protocol.py` the way
  `chaos.py` writes up the packet-loss model: flagged as inference, with the
  reasoning, so whoever establishes the truth knows what to change. It is a
  warning by default and a violation only under `--strict-fork`.

- Fork mode: `--mode fork --track both|inbound|outbound`, with `--agent-audio`
  supplying the outbound track, because in a fork Twilio sends the app the
  audio *it* generated.
- `--record-stereo call.wav`: caller left, agent right. The agent's channel is
  placed at **arrival timestamps with real silence in the gaps**, never
  concatenated -- concatenating a batching agent's frames would render a 9.5 s
  reply delivered in 0.85 s as continuous audio, a picture of the *opposite* of
  the barge-in bug this project is known for finding.
- `examples/echo_agent.py` gains a fork endpoint that, in one mode, wrongly
  tries to talk back.

> ### ⛔ REVIEW GATE 8 — Fidelity, and the golden file
> `/code-review high`. Security review not warranted: no new untrusted input,
> no new network surface.
> **Focused risk: this is the first phase since Phase 1 that changes bytes on
> the wire, and the golden file is the only thing between a wrong byte and
> three phases of downstream damage.** Does adding `track` plumbing change the
> *default* media frame by a single byte? It must not, and a golden file that
> needs regenerating is a finding rather than a chore. Did fork logic that
> reads a clock or a socket land in `protocol.py`? The interleave is a
> scheduling decision and belongs in `session.py`; the violation is a parsing
> decision and belongs in `protocol.py`. Are the TwiML and WebSocket track
> literals ever confused? Does the stereo writer survive a batching agent --
> 9 s of audio sent in 0.9 s must span 9 s of file. Does fork mode hang waiting
> for a mark echo that will never come?

### Deliberately dropped, with the reasoning

**`--out` path confinement.** PLAN.md previously listed it as owed. It is
security theatre: the path is typed by the user on their own command line, in a
process with their privileges, and someone who can write
`--out ~/.ssh/authorized_keys` could have run `cat >` instead. Gate 4's
confinement of scenario `say:` paths is a genuinely different case and
`scenario.py` already states why -- **scenario files travel**, so a path inside
one is an instruction from a stranger rather than from the user.

The residual risk worth acting on is the mirror of it, and it is a schema
constraint rather than a check: **never add an output path to the scenario YAML
schema.** No `record:` step, no `out:` key. The day one is added, confinement
becomes owed.

---

## 4. Testing strategy

| Layer | What | Runs where |
|---|---|---|
| Unit | `protocol.py`, `audio.py` — pure, fast, no network | Every commit |
| Golden file | Known WAV → exact expected frame bytes | Every commit |
| Integration | Against `examples/echo_agent.py` — a stub that echoes audio back | Every commit, CI |
| Manual/ear | Decode output, listen to it | Each phase |
| Real-world | Against a real agent's `/media-stream` | Each phase |
| Adversarial | Malformed frames, hostile server behavior | Phase 4+ |

**The `examples/echo_agent.py` stub is load-bearing.** It makes CI possible
without credentials, and doubles as the onboarding example. Build it in Phase 2.

---

## 5. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Protocol details wrong | **High** | Verify against live docs; validate against real endpoint early (Phase 2) |
| Twilio ships this themselves | Medium | They've had Media Streams for years and haven't. Accept it |
| Audience too small | Medium | Real, and priced in. Target hundreds of stars, not thousands |
| Pipecat/LiveKit expand into it | Low | They have no incentive to test *outside* their framework — that's the whole wedge |
| Maintenance burden after launch | Medium | Keep scope tight. The non-goals list is a defense, use it |
| Twilio changes the protocol | Low | Versioned protocol; changes would be additive |

**The one that actually kills this project** is not competition — it is losing
interest after Phase 2 and leaving a half-finished repo. Phase 2 gives you
something personally useful; that is the checkpoint that sustains the rest.

---

## 6. Definition of done (v1.0)

- [ ] Replaces the real-phone-call dev loop for a real agent in daily use
- [ ] Runs in CI with a latency threshold gate
- [ ] A stranger installs it and gets a result in under five minutes
- [ ] Reproduces at least one real bug that a phone call would have been needed to find
- [ ] README has a demo GIF and an honest comparison table
- [x] Published to PyPI under Apache-2.0

---

## Appendix: review gate summary

| Gate | After | Command | Primary risk being caught |
|---|---|---|---|
| 1 | Protocol core | `/code-review high` + listen to decoded audio | Wrong bytes poisoning everything downstream |
| 2 | Live session | `/code-review high` | Async lifecycle, swallowed task exceptions |
| 3 | Metrics | `/code-review high` + delay-injection check | Measuring the wrong thing convincingly |
| 4 | Realism | `/code-review high` + `/security-review` | Untrusted input handling, resource exhaustion |
| 5 | Pre-launch | `/code-review high` + clean-machine install | Onboarding friction losing every new user |

---

## Progress

> **A note on the git history.** It was re-baselined to a single commit before
> this repository was made public. The project was validated against a private
> production voice agent, and the original commit messages described that
> system and its defects in detail. The engineering record lives here in
> PLAN.md instead, which is where it is more useful anyway.

*Updated 2026-09-10. Phases 1–3 complete, gates 1–3 passed. Phase 4 next.*

### Decisions taken

| Decision | Was | Is | Why |
|---|---|---|---|
| Package name | `dialtone` | **`streamdouble`** | `dialtone` 0.0.39 is an active PyPI package (Dialtone API SDK, Apache-2.0). `streamdouble` verified free |
| License | AGPL-3.0 | **Apache-2.0** | The tool is meant to run inside other teams' CI, where AGPL trips license allowlists. The network clause has no practical bite on a CLI |
| μ-law codec | `audioop` | **Hand-written G.711** | `audioop` was removed in Python 3.13 (PEP 594). Verified byte-identical to `audioop` across all 65536 samples and all 256 codes, and CI proves it on 3.13 where `audioop` is absent |
| Resampling | unspecified | **`soxr`, optional extra** | No stdlib resampler is acceptable; aliasing at 8 kHz lands in the speech band |
| Mark echo | Phase 4 | **Phase 2** | Agents gate turn-taking on the echo; a simulator that only logs marks hangs them |
| CI | Phase 5 | **Phase 1** | The golden-file test makes a cross-platform byte-exactness claim, so it needed proving from the start |
| Release cadence | A release per phase | **One release, after Phase 9** | Decided 2026-09-11. 0.1.1 is merged to `main` and deliberately unpublished. Publishing is irreversible -- a PyPI version can be yanked but never replaced -- and there is no user waiting on 0.1.1, so there is nothing to buy by shipping mid-roadmap and a burnt version number to lose |

### The protocol table in §2 was wrong

Verified against the live Twilio docs. Three corrections, each now cited in
`protocol.py`'s module docstring:

1. **`sequenceNumber`, `chunk` and `timestamp` are JSON strings**, while
   `sampleRate` and `channels` in the same payload are numbers. An agent doing
   arithmetic on `chunk` gets a TypeError against real Twilio — and a simulator
   emitting ints would hide that bug rather than surface it.
2. **`dtmf.track` is the literal `"inbound_track"`**, matching nothing else in
   the protocol (`media.track` and `start.tracks` use `"inbound"`/`"outbound"`).
3. **`timestamp` and `chunk` are nested inside `media`**, not top-level as §2 had
   them. `dtmf` and `mark` are also inbound events, which §2 omitted.

### What exists

```
src/streamdouble/
  g711.py       G.711 μ-law codec, from the ITU-T spec. No audioop.
  audio.py      WAV I/O, downmix, resample, 20 ms framing.
  protocol.py   Frame builders + parser. Pure. Enforces frame ordering.
  pacer.py      Absolute-deadline scheduling with drift correction.
  session.py    WebSocket lifecycle: send + receive + mark echo under a TaskGroup.
  cli.py        `streamdouble call ...`
  metrics.py    Latency and conformance metrics, derived from the event log.
examples/
  echo_agent.py FastAPI agent. Modes: echo, silent, hangup, garbage, clear.
fixtures/       12 generated WAVs + the deterministic generator.
tests/          277 passing, on Linux + Windows × Python 3.11/3.12/3.13.
```

### Review gate findings

**Gate 1 — 5 findings** (`tests/test_review_gate_1.py`):

- **Fixtures used `np.random.default_rng`.** NumPy guarantees that stream only
  for an identical seed, call sequence, build, environment, machine *and CPU*,
  and may change it on any feature release. The golden file pins those fixtures
  by SHA-256 and CI regenerates them on two OSes — a NumPy upgrade would have
  turned every byte-exactness assertion red while looking exactly like a codec
  regression. Replaced with xorshift64* over plain Python integers.
- **The replacement collided adjacent seeds.** Forcing the state odd made 42 and
  43 identical, and the fixtures use `SEED` and `SEED+1` — so `speech_8k` and
  `long_8k` had silently been sharing a jitter sequence. Found by the test
  written for the fix. Seeded through splitmix64 instead.
- **`load_wav` never validated length.** Odd truncation leaked a raw
  `ValueError`; even truncation silently loaded half the clip, which reads
  downstream as "the agent stopped talking".
- **`resample` accepted 2-D**, so skipping `to_mono` produced an error from
  `g711.encode` that appeared to blame the codec.
- **The encoder had no ordering guard** — it would emit media before `start`,
  the simulator committing the violation Phase 3 exists to detect in agents.

**Gate 2 — 4 findings** (`tests/test_review_gate_2.py`, all mutation-verified):

- **Quadratic audio accumulation, and self-concealing.** `result.audio_received
  += payload` on immutable bytes: 0.011s for 40s of audio, 18.6s for 640s. The
  real damage was not slowness — it ran inside the receive loop on the event
  loop, so a long call spent seconds blocking the pacer and inflating the very
  latency figures the tool exists to report, worse the longer the call and
  plausible at every point. Now a `bytearray`.
- **Cleanup replaced the real error.** `_finish` built a `stop` unconditionally,
  and gate 1's ordering guard refuses that when `start` never went out — so a
  socket dying before `start` turned a `ConnectionClosed` into a
  `FrameSequenceError` raised from a `finally`, escaping as an unhandled
  `ExceptionGroup`. **Gate 1's fix created this**: a guard is a new failure mode
  until its callers are checked.
- **`max_size=None`** removed the websockets 1 MiB inbound cap. Bounded at 8 MiB.
- **The final `stop` send had no timeout**, so a peer that stops reading stalls
  it on flow control — in the `finally`, on both success and error paths, so it
  could hang CI rather than fail it.

**Gate 3 — 4 findings** (`tests/test_review_gate_3.py`):

- **`time.monotonic` has 15.625 ms resolution on Windows.** The most
  consequential finding of the project so far, and it invalidated numbers that
  had been reported confidently for two phases. CPython implements `monotonic`
  with `GetTickCount64` there — coarser than the 20 ms frame being measured. So
  every latency was quantised onto a 15.6 ms grid while being printed to a
  tenth of a millisecond; the pacer's lateness statistics were reading the
  clock rather than the pacing (every run reported max lateness of *exactly*
  15.0 or 16.0 ms — one tick); and `measurement_is_reliable` was comparing
  clock noise against its threshold, flipping between identical runs. Confirmed
  by `time.get_clock_info` and by three independent clock reads with real work
  between them returning byte-identical timestamps. Now `time.perf_counter`
  (100 ns, monotonic on both platforms). The same call now reports
  **440.7 ms** where it used to report a quantised **422.0**, and gaps resolve
  to 0.007 ms.
- **The Windows caveat previously written into the README and PLAN was wrong in
  its explanation** — it blamed sleep granularity for what was mostly
  measurement granularity. Both are real, and only now can they be told apart:
  genuine max lateness is ~15.5 ms, but mean lateness is ~4 ms.
- **First-audio was timestamped after JSON parse and base64 decode**, though
  `metrics.py` explicitly documented it as arrival "not decode time". Costs
  0.004 ms on a 160-byte payload but 0.32 ms at 64 KB, so it penalised agents
  in proportion to how much they batched — small, systematic, and correlated
  with the thing being measured.
- **One frame carried three separate timestamps**, so time-to-first-audio and
  the inter-frame gaps were derived from different instants for the same
  arrival. Masked entirely by the coarse clock; fixing the clock exposed it.
- **`measurement_is_reliable` keyed off max lateness**, a single worst frame
  across the whole call, so one GC pause at frame 2400 condemned a measurement
  taken at frame 10. Now keyed to the mean, with the max still published.

### Verification worth keeping

- **Codec**: byte-identical to `audioop` for all 65536 samples / 256 codes.
- **Mutation testing**: 8 deliberate breakages (0x00 padding, 158-byte frames,
  double base64, dropped bit shifts, naive decimation, left-channel downmix,
  halved timestamps, unbounded reads) each caught by the test written for it.
- **End-to-end**: audio recovered over a real socket is byte-identical to the
  source through the codec.
- **Measurement**: `tests/test_measurement_validity.py` injects 0, 250, 750 and
  2000 ms delays into a real server and checks the reported figure tracks each
  one. The 2000 ms case is what would catch a seconds/milliseconds confusion.
  A companion test asserts the error does not *grow* with the delay, which
  separates "wrong by a constant" from "wrong by a factor" — a single data
  point cannot.
- **Seven measurement mutations** all caught: latency in seconds not
  milliseconds, silence as 0 instead of None, a wrong origin, an off-by-one
  frame, connect time folded into call duration, percentiles reported below the
  sample minimum, and missing data passing its threshold.

### Known issues and things owed

1. **Real-agent validation is owed.** Gate 2 closed against
   the echo agent and hostile stubs, which is weaker than the plan intends.
   *This is the single most important thing to do next.*
2. ~~**The by-ear check is owed.**~~ **Done, 2026-09-11.** A human listened to
   a clip before and after a full round trip and confirmed they sound the same,
   and that the words are intelligible. See
   [The by-ear check](#the-by-ear-check-done-2026-09-11). The objective
   substitutes in `tests/test_signal_integrity.py` (440 Hz returns at 440 Hz,
   correlation > 0.99, zero-lag cross-correlation peak) stand, and now have a
   pair of ears behind them.
3. **Windows scheduler granularity is genuinely ~15.6 ms**, so max lateness
   sits near a full frame even on healthy runs; mean lateness is ~4 ms.
   Cumulative drift stays near zero — deadline scheduling absorbs it. Worth
   considering a `winmm` timer-resolution request if it proves to matter.
4. **Real recorded speech fixtures** would strengthen the by-ear check.
5. **Path traversal on `--out`** is unhandled; the plan puts it in gate 4.

### Next session should

1. **Phase 6, distribution** — the Claude Code skill wrapper, and the launch
   posts. All of it presupposes a public repo, so it waits on that decision.
2. **Real recorded speech fixtures.** The synthetic ones are right for codec
   tests and insufficient for agent tests: a real STT service will not transcribe
   speech-*like* audio, so the first barge-in run failed for want of real words
   rather than for want of a working agent. A human voice saying a few
   sentences would unlock scenario testing against any STT-backed agent.
3. **Finish gate 5** once there is something to install from PyPI, and get
   someone who is not the author to read the README cold.

---

## Validated against a real voice agent

*2026-09-10. A production Twilio voice agent, driven locally against its own
`/media-stream` endpoint.*

The point of this exercise was that `examples/echo_agent.py` is **our** code,
written from the same reading of the Twilio docs as `protocol.py`. A shared
misreading would leave every test green. The validation target was an
implementation we did not write, built against real Twilio by someone with a
live account and a working STT/LLM/TTS pipeline behind it.

### What it confirmed

| Check | Result |
|---|---|
| `connected` then `start` ordering | Accepted. Its source comments that order as *"confirmed live, not just documented"* -- independent corroboration of section 2 |
| Auth via `start.customParameters` | Passed a `secrets.compare_digest` check on a shared secret |
| SID extraction | Both `streamSid` and `callSid` parsed out of our `start` frame |
| 100 paced media frames | Consumed and forwarded to a real streaming transcription service, zero violations |
| Reply captured | 11.89 s of real synthesised speech: 75.7% of energy in the 300-3400 Hz band, 0.7% near Nyquist, 23% quiet windows |
| Time to first audio | 1310 ms for the greeting -- LLM cold start plus TTS |

### What only a foreign implementation could have shown

- **Agents batch their outbound audio.** This one sends roughly 8000 bytes per
  `media` frame -- about a second of audio -- not 160-byte frames. Our parser
  accepts arbitrary inbound payload sizes by deliberate decision
  (`test_parse_accepts_a_media_payload_of_any_length`); had we enforced 160
  bytes symmetrically, which looks like the tidy choice, this call would have
  failed outright. The echo agent could never have surfaced it, because it
  echoes back exactly the frame sizes it receives.
- **Mark echo is load-bearing, as predicted.** Its receive loop sets the
  end-of-turn event only on the echoed mark, and the hangup path waits on that.
  A simulator that merely logged marks -- which the original plan proposed for
  Phase 2 -- would hang such an agent indefinitely.
- **`clear` on barge-in** matches the model in `session.py`.

### Cost of getting this wrong, recorded so it is not repeated

The first test call ran to completion and then wrote records to a **live CRM**
for a made-up phone number, because that agent pushes call outcomes on hangup.
Correct behaviour by the agent; wrong outcome for a test. The records were
deleted with the owner's explicit approval and verified gone.

**Any run against a real agent must first disable whatever it touches on
hangup.** A simulated call is a real call as far as everything downstream of the
socket is concerned -- storage, CRM pushes, webhooks, billing. That is a general
hazard of this tool rather than a quirk of one agent, which is why it is in the
README rather than only here.

## Phase 4 and gate 4

*2026-09-10.*

### The bug it was built to find

Phase 4's brief was "reproduce a real bug you previously hit". It found one
nobody had hit yet.

The barge-in scenario failed against a real agent: the caller talked over the
greeting, the transcription service transcribed it correctly, and no `clear`
came back. That agent gated barge-in on an "am I speaking" flag, and cleared
the flag when its TTS **send** loop finished.

But Twilio buffers outbound audio and plays it at the caller's pace. That is why
`mark` exists at all. So "finished sending" and "finished speaking" are different
instants, and streamdouble measured how different:

```
audio duration     9.52 s   what the caller hears
time to deliver    0.85 s   how long the agent took to send it
                   11.2x faster than real time
playback tail      8.67 s   caller still listening, agent already "done"
```

For those 8.67 seconds the flag is false while the caller is still being spoken
to, so barge-in cannot fire. On a cold-calling agent that is most of every
utterance during which the caller is unable to interrupt. It is a real bug on
real calls, not an artefact of simulation, and an agent gating on the `mark`
echo instead would not have it. Reported upstream and since fixed.

Now a first-class metric — `delivery_ratio`, `playback_tail_ms`,
`barge_in_blind_spot_ms` — reported unprompted whenever an agent front-loads its
audio, because the pattern is common and the consequence is invisible.

### A fidelity bug in this tool, found while building

Until now the sender stopped entirely once the clip ended. A real call carries
20 ms frames continuously for its whole duration, and endpointing depends on it:
transcription generally needs the stream flowing to decide an utterance is over.
Going quiet looks like a dead line, not a quiet caller — so "my agent never
finalises the transcript" would have been a bug in *this tool*, reported against
the agent. `wait` and every `wait_for` now stream silence.

### Modelling packet loss on a TCP transport

The Twilio-to-app leg is a WebSocket, so it is TCP: a frame cannot vanish in
transit or arrive out of order. Whatever packet loss means for a voice call, it
does not mean a missing WebSocket frame — it happens upstream on the carrier's
RTP leg, and by the time Twilio builds a frame, that audio was never received.

So a dropped frame is one Twilio never sends: `sequenceNumber` stays unbroken,
because Twilio numbers what it sends, while `media.timestamp` jumps by more than
one frame interval. That discontinuity is the only signal an agent gets.

Representing it meant decoupling presentation time from the chunk counter in the
encoder — they are the same number right up until audio goes missing. **The
Twilio docs say nothing about any of this**; the inference and its reasoning are
written into `chaos.py` so that whoever establishes the truth knows what to
change. Silence substitution is the plausible alternative, and it was rejected
because it would be undetectable, and a test tool should present the harder case.

### Gate 4 findings

- **Unbounded audio buffering.** Measured, not theorised: an endpoint streaming
  400 KB frames — under the 8 MiB per-frame cap — produced 206 MB of buffered
  audio and 436 MB of peak memory in a five-second call. The per-frame cap does
  not help, because it is per frame while the buffer is per call; at the default
  30 s drain that is gigabytes, and `--out` then writes all of it to disk. A CI
  runner OOMs and it reads as flaky infrastructure. Now capped at 60 MB, with
  truncation reported rather than silent.
- **Unbounded event log**, same vector, smaller constant — plus `violations` and
  `unknown_events`, where an agent broken on every frame is one bug reported
  thousands of times. Retention capped; counts stay exact.
- **No ceiling on the call itself.** Every other wait was bounded — response,
  drain, connect, the final stop send — but the send phase was not, so a
  scenario's `wait: 86400` would stream for a day. Added `max_call_s`.
- **Scenario clip paths were unconstrained.** Scenario files look like
  configuration and get shared around, but `say:` names a path this process
  reads. Now confined to the scenario's directory or the working directory —
  the first attempt confined it to the scenario's directory alone and
  immediately rejected the repository's own example, which is the useful kind of
  early failure: containment that forbids ordinary project layout gets disabled.

**Not a finding, having been checked:** `wss://` verifies certificates, because
`websockets` uses `ssl.create_default_context` and nothing here overrides it.

## The by-ear check, done 2026-09-11

*Owed since gate 1. Closed while rehearsing a demo, which is the only reason it
finally happened -- a demo needs someone to hear the audio, and the check needs
someone to hear the audio, and they turn out to be the same act.*

Gate 1 asked for a decoded payload to be listened to, on the grounds that tests
can pass while the audio is garbage. It was deferred, then carried as an owed
item through four more gates. Objective substitutes were written in the
meantime (`tests/test_signal_integrity.py`: a 440 Hz tone returns at 440 Hz,
correlation > 0.99, the cross-correlation peak sits at zero lag) and they are
good evidence, but none of them can tell you that speech is *intelligible*.

**What was checked.** Real TTS speech -- "How much would that cost per month?"
from `fixtures/make_speech.py` -- before and after a complete round trip:

    WAV -> 8 kHz mono -> mu-law -> 160-byte frames -> base64 -> JSON
        -> WebSocket -> echo agent -> back -> decode -> WAV

**Result: the two files sound the same, and the sentence is clearly
intelligible.** No muffling, no buzz, no pitch shift, no truncation.

**This is a stronger check than gate 1 asked for**, and the difference is worth
stating rather than letting the tick mark imply more or less than it should.
Gate 1 wanted a codec round trip listened to. What was actually listened to is
the codec *plus* framing, base64, the wire format, a real socket, a real agent
and the decode path -- every stage that could plausibly corrupt audio, end to
end. A codec-only check would not have caught a framing or base64 fault; this
one would.

**What it still does not cover.** The audio is desktop TTS, so it has no accent,
no background noise and no trailing off mid-sentence. Real recorded human speech
remains worth having, and remains listed above as an open item -- not because
this check was weak, but because it is the *input* that is synthetic, not the
measurement.

## Phase 7 and gate 6

*2026-09-11.*

### What Phase 7 built

`api.py` is now the only implementation of "place a call". `cli.py` parses
arguments, renders a report and picks an exit code, and knows nothing else --
`CallReport.to_dict()` is the definition of `--json`, which the CLI prints and
adds nothing to. `pytest_plugin.py` registers `simulated_call` and
`streamdouble_config` through `pytest11`, so installing the package is the
whole setup. `trace.py` records every frame in both directions as JSON Lines.
`metrics.SCHEMA_VERSION` ships now and is consumed in Phase 8.

Three things worth keeping from building it:

**The pytest hook everyone would reach for does not work.** Explaining a `None`
latency looks like a job for `pytest_assertrepr_compare`. It never fires:
`None < 800` raises `TypeError` while *evaluating* the comparison, so the
assertion never completes and pytest never asks a plugin to describe a mismatch
that did not happen. It is done from `pytest_runtest_makereport` instead.
Running a real pytest session through `pytester` is what revealed this --
calling the fixture directly would have missed it, which is the same shape of
mistake that let `streamdouble scenario` ship unreachable.

**The redaction shipped broken and a reasonable test passed anyway.**
`customParameters` is nested inside `start`; the first implementation read it
from the top level, found nothing, and therefore wrote no parameters and ran no
redaction -- while a "does the secret appear in the file" check passed cleanly.
Absence is satisfied by a lookup that missed. The tests now assert the keys are
**present** and the values **hidden**, because only the first half separates a
redaction from a bug.

**The suite was red for a reason that was not ours.** A
`PytestUnraisableExceptionWarning` about an unclosed `ProactorEventLoop` was
failing a random test per run. Measured before touching anything: twenty
consecutive in-process calls leave zero unclosed loops and no socket growth, and
the one surviving loop is the uvicorn test server's, still running. It is
pytest-asyncio building a fresh loop per test on Windows. Filtered narrowly --
two message shapes, verified narrow by raising a different unraisable and
watching it still fail -- and the detection the warning was accidentally
providing is now deliberate in `tests/test_no_resource_leaks.py`.

### Gate 6 findings

**4 findings** (`tests/test_review_gate_6.py`, all mutation-verified). The two
worst are repeats, which is the argument for the gates existing:

- **`trace.flush()` in a bare `finally`.** Reproduced both ways: a completely
  successful call lost its entire report -- metrics, thresholds, recorded audio
  -- because a side-file could not be opened; and a call to an unreachable agent
  raised `PermissionError` instead of `ConnectionFailed`, aiming debugging at
  the filesystem while the agent was down. **This is gate 2's finding exactly**:
  cleanup replacing the real error. A failing trace is now reported on stderr,
  `trace_path` stays `None` so no report claims a file that is not there, and
  the call's own outcome stands.
- **The trace had no cap at all**, reopening the vector gate 4 measured at
  206 MB buffered and 436 MB peak. Two caps, because one is not enough: records
  bounded by count, payloads by total bytes -- gate 4's endpoint sent *few*
  frames and *enormous* ones, so a count-based limit alone lets a handful of
  records carry hundreds of megabytes. Dropped counts stay exact.
- **Tracing re-parsed every inbound frame** that `parse_outbound` had just
  parsed, doubling JSON decode inside the receive loop -- the precise "observer
  perturbs the observed" cost the design exists to avoid, and invisible to the
  parity test because the echo agent sends 160-byte frames while real agents
  batch to ~8000. `InboundMedia`/`Mark`/`Clear` now carry the frame they came
  from, `compare=False` so that every existing test asserting on a whole parsed
  frame keeps working -- which is how the equality consequence was found.
- **Frames were recorded before the send was awaited**, so a frame whose send
  raised appeared as though it went out. The frame it lied about was the last
  one before a disconnect, which is the one someone opens a trace to look at.

**And a flaky test of my own making.** The trace parity check compared one
traced call against one untraced call -- a noisy estimator of a systematic
effect. It passed alone and failed in sequence. Medians of three runs each way
now, stable across three consecutive full runs. By this project's own standard
a flaky detector is worse than none, because people learn to re-run it.

**Security review: nothing found.** The one genuinely new surface is secrets in
the trace, and it was already handled. Worth recording what was checked and
cleared: scenario YAML still cannot set any output path (`_parse_step` enforces
a six-verb allowlist), which upholds the schema constraint written down when
`--out` confinement was dropped; `yaml.safe_load` throughout; no `eval`, `exec`,
`pickle` or `subprocess` anywhere in `src/`.

## Gate 5's first finding, from reading rather than fresh eyes

*2026-09-10.*

The README's CI-gate example wrote its line continuation as the two characters
`\` and `n`. It reads as a wrapped command and copy-pastes as a broken one --
the shell drops the backslash and hands `streamdouble` a stray argument `n`.
It shipped that way in 0.1.0 and 0.1.1.

**The typo is not the finding.** `tests/test_documented_commands.py` extracted
that command and ran it, exactly as designed, and it passed. Its only assertion
was `returncode != EXIT_USAGE`, and `argparse` never returns `EXIT_USAGE` --
`parser.error` exits **2**, which in this package is `EXIT_TIMEOUT`. So the
entire class of failure the test exists to catch (bad flag, unknown subcommand,
stray argument) was invisible to it, and a reader who mistyped a flag was told
their agent had failed to respond in time. A CI job gating on exit codes read a
typo as a slow agent. The README has promised `4` for usage errors since the
first release.

This is the same shape as the `scenario` subcommand bug that caused that test
file to exist: a check that looks like it covers the command line, keyed to a
value the command line never produces. Writing the test was not enough; the
test needed a failing case to be verified against, and it never had one.

Fixed by `_Parser`, which overrides `error()` to exit `EXIT_USAGE`.
`add_subparsers` propagates `parser_class`, so both subcommands inherit it, and
`--help` / `--version` still exit 0 -- asserted, because a parser that returned
4 for `--version` would break every packaging script that checks it.

Three regression tests, each verified failing against the old behaviour:

- no bash block in the docs may contain a literal `\n`, checked in the document
  before anything is run;
- the runner test also asserts on stderr for `unrecognized arguments`, so it
  holds independently of whatever the exit code happens to be;
- the exit code itself, across five malformed command lines and four
  well-formed ones.

**What this says about gate 5.** It was found by reading the README closely,
which is the cheap half of the fresh-eyes pass and evidently still productive.
It is not a substitute for the half that is still owed: a person who did not
write this following the quickstart literally would have hit the broken
copy-paste in their first two minutes, and would not have needed to reason
about exit codes to notice.
