# Six ways to get Twilio Media Streams audio wrong

Notes from building [streamdouble](https://github.com/shaswat28/streamdouble), a
simulator that has to be indistinguishable from Twilio at the wire. Every one of
these was found by a test that failed, and most of them produce output that
looks fine until you listen to it.

Not affiliated with Twilio. Everything here is from the public documentation and
from measurement.

---

## 1. μ-law is a 14-bit format, and it is not symmetric

G.711 μ-law is usually described as an 8-bit encoding of 16-bit audio, which is
true and misleading. The algorithm is defined on **14-bit** linear samples. A
16-bit sample is arithmetic-shifted right by two before encoding.

That shift rounds toward negative infinity, and the consequence surfaces
immediately: `+1` and `-1` do not encode to mirror-image values.

```python
encode(1)   # 0xFF
encode(-1)  # 0x7E
```

If your codec is symmetric about zero, it is wrong. This is the first thing to
check when audio is subtly distorted rather than obviously broken, and it is
easy to "fix" in the wrong direction because symmetry looks like correctness.

The decoder needs no matching shift. It subtracts the bias *after* the segment
shift, which lands back in 16-bit range on its own — adding a `<< 2` there
overflows at full scale. That bug is caught instantly by an overflow, which is
the good outcome.

## 2. Digital silence is `0xFF`, not `0x00`

μ-law inverts the assembled byte before transmitting. So a buffer of zero bytes
is not silence — it is close to full scale.

```python
decode(0xFF)  #     0   silence
decode(0x00)  # 32124   nearly full scale
```

Where this bites: padding. Any clip that is not an exact multiple of 20 ms needs
its final frame padded, and `bytes(160)` is the obvious way to do it. That
appends a burst of loud noise to the end of most clips. It sounds like a click
and it is easy to blame on something else.

## 3. There is a code no encoder ever produces

μ-law is sign-magnitude, so it has two representations of zero. `0xFF` is
positive zero, and it is what encoders emit. `0x7F` is negative zero, and
nothing can produce it — reaching a zero magnitude on the negative side would
need a sample that is simultaneously negative and zero.

So `0x7F` decodes to 0 and re-encodes to `0xFF`. If you write a round-trip test
asserting that every code survives unchanged, it fails on exactly one of 256
values, and the failure is correct. Assert the specific exception rather than
loosening the test — a count-based assertion ("255 reachable codes") passes for
the wrong reasons too, since a broken segment lookup also leaves gaps.

## 4. Twilio's frame fields are strings, except when they are numbers

From a real `media` frame:

```json
{
  "event": "media",
  "sequenceNumber": "3",
  "media": { "track": "inbound", "chunk": "1", "timestamp": "5", "payload": "..." },
  "streamSid": "MZ..."
}
```

`sequenceNumber`, `chunk` and `timestamp` are **JSON strings**, though every one
of them holds a number. In the `start` frame, `sampleRate` and `channels` in the
same payload are **numbers**.

`msg["media"]["chunk"] + 1` therefore raises a `TypeError` against real Twilio.
Worth knowing when writing a mock: emitting them as ints is tidier and hides
this class of bug rather than surfacing it.

One more: `dtmf.track` is the literal string `"inbound_track"`. Every other
track field uses `"inbound"` or `"outbound"`. An agent matching on `"inbound"`
silently never sees a keypress.

## 5. "Finished sending" is not "finished speaking"

The one that causes real bugs in production agents rather than merely wrong
audio.

Twilio buffers the audio you send it and plays it to the caller at normal speed.
Your TTS does not run at normal speed — measured on a production agent, 9.52
seconds of speech handed over in 0.85 seconds, about 11x real time.

So an agent that tracks whether it is speaking by when its send loop finishes
believes it stopped talking roughly nine seconds before the caller stopped
hearing it. If barge-in is gated on that flag, the caller can talk over the
agent for the whole difference and nothing happens. In this case 8.67 seconds
out of 9.52 — most of every utterance.

The symptom is nasty because everything looks healthy: transcripts are perfect,
latency is fine, and callers just say the bot talks over them.

This is what `mark` is for. Twilio echoes a mark back on the inbound side only
once the audio queued before it has actually finished playing. Gate on the echo,
not on the send loop. If you also want a local bound — a lost mark should not
stall a turn forever — μ-law at 8 kHz is one byte per sample, so bytes ÷ 8000 is
seconds, and you can compute when playback should end without asking anyone.

## 6. Packet loss cannot mean what you think on a TCP transport

If you are simulating a bad connection, this one shapes the whole design.

The Twilio↔app leg is a WebSocket, which is TCP. TCP retransmits. A frame
cannot vanish in transit and cannot arrive out of order. Whatever packet loss
means for a voice call, it does not mean a missing WebSocket frame.

The loss happens *upstream*, on the carrier's RTP leg, which is UDP over a
mobile network. By the time Twilio comes to build a frame, that audio was never
received. So a lost frame is one Twilio never sends — and what the app sees is
not a gap in `sequenceNumber`, because Twilio numbers what it sends, but a
`media.timestamp` that jumps by more than one frame interval.

That discontinuity is the only signal an agent gets. The documentation does not
cover this, so it is an inference; the alternative is that Twilio substitutes
silence, which would be undetectable. Worth deciding deliberately and writing
down which you assumed.

---

## A bonus that is not about audio at all

`time.monotonic` on Windows is `GetTickCount64`, with a resolution of **15.625
ms** — coarser than the 20 ms frame you are trying to measure.

Used as a timing source, it quantises every latency onto that grid while your
code prints them to a tenth of a millisecond. It also turns pacing statistics
into a readout of the clock: a frame released perfectly on time reads as either
0 ms or 15.6 ms late depending on where the tick boundary falls.

`time.perf_counter` is monotonic *and* high resolution on every platform, and is
what you want. `time.get_clock_info('monotonic')` will tell you which you have.

---

The tests for all of the above are in
[streamdouble](https://github.com/shaswat28/streamdouble), including an
exhaustive check of the codec against CPython's `audioop` across all 65536
possible samples — which is worth doing while `audioop` still exists, since it
was removed in Python 3.13.
