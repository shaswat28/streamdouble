"""Generate real-speech fixtures using the operating system's own TTS.

    python fixtures/make_speech.py

Why this exists, and why it is separate from ``make_fixtures.py``.

The synthetic fixtures are right for codec and framing tests: they are
closed-form, bit-reproducible, and pin exact bytes in the golden file. They are
useless for testing an *agent*, because they are speech-*like* rather than
speech. No transcription service will turn a formant sweep into words, so an
agent driven by one never reacts -- and a barge-in test then fails for a reason
that has nothing to do with the agent. That mistake was made here first, and
briefly looked like a bug in a perfectly good agent.

Real speech is therefore needed for any scenario that depends on the agent
understanding the caller. Rather than asking everyone to record themselves, this
uses the TTS already installed on every desktop operating system: SAPI on
Windows, ``say`` on macOS, ``espeak-ng`` on Linux. Verified end to end -- a real
streaming transcription service returned the exact sentence synthesised here,
and the agent's barge-in fired on it.

**The output is not committed.** Partly because it is regenerable in seconds,
and partly because redistributing a vendor's synthesised voice in an
Apache-2.0 repository raises a licensing question nobody needs. Generate your
own; ``fixtures/speech/`` is gitignored.

Recorded human speech is still better if you have it. Real callers have accents,
background noise, and the habit of trailing off mid-sentence, none of which a
desktop TTS reproduces. Drop WAVs alongside these; nothing depends on their
absence.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent / "speech"

#: What to say, and what each clip is for. Chosen to be the things a scenario
#: actually needs rather than lorem ipsum: an interruption has to sound like an
#: interruption for an agent's turn-taking to treat it as one.
CLIPS = {
    "interrupt": "Sorry, can I stop you there for a second?",
    "greeting": "Hello, yes, this is she speaking.",
    "question": "How much would that cost per month?",
    "agree": "Yes, that sounds good to me.",
    "decline": "No thank you, I'm not interested right now.",
    "long": (
        "I am calling about the insurance quote you requested last week. "
        "I wanted to check whether you had any questions about the coverage, "
        "and whether now is a good time to talk it through."
    ),
}


class NoSynthesiser(RuntimeError):
    """No usable text-to-speech was found on this machine."""


def synthesise_windows(text: str, destination: Path) -> None:
    """Windows SAPI, via System.Speech. Built in, nothing to install.

    Written straight out at 8 kHz mono, which is what Media Streams carries --
    so the fixture needs no resampling and therefore no optional soxr
    dependency just to produce it.
    """
    script = f"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
    8000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)
$synth.SetOutputToWaveFile("{destination}", $format)
$synth.Speak("{text}")
$synth.SetOutputToNull()
$synth.Dispose()
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        capture_output=True,
    )


def synthesise_macos(text: str, destination: Path) -> None:
    """macOS `say`. Writes AIFF, so convert with afconvert."""
    with tempfile.TemporaryDirectory() as work:
        aiff = Path(work) / "speech.aiff"
        subprocess.run(["say", "-o", str(aiff), text], check=True, capture_output=True)
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@8000", "-c", "1",
             str(aiff), str(destination)],
            check=True,
            capture_output=True,
        )


def synthesise_linux(text: str, destination: Path) -> None:
    """espeak-ng. Robotic, but it transcribes, which is the whole requirement."""
    subprocess.run(
        ["espeak-ng", "-w", str(destination), "-s", "150", text],
        check=True,
        capture_output=True,
    )


def pick_synthesiser():
    system = platform.system()
    if system == "Windows" and shutil.which("powershell"):
        return synthesise_windows, "Windows SAPI"
    if system == "Darwin" and shutil.which("say") and shutil.which("afconvert"):
        return synthesise_macos, "macOS say"
    if shutil.which("espeak-ng"):
        return synthesise_linux, "espeak-ng"
    raise NoSynthesiser(
        "no text-to-speech found.\n"
        "  Windows: built in, nothing to do\n"
        "  macOS:   built in, nothing to do\n"
        "  Linux:   apt install espeak-ng  (or equivalent)\n"
        "Or record your own WAVs -- real human speech is better anyway."
    )


def main() -> None:
    try:
        synthesise, name = pick_synthesiser()
    except NoSynthesiser as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"synthesising with {name}\n")

    for label, text in CLIPS.items():
        destination = OUTPUT_DIR / f"{label}.wav"
        synthesise(text, destination)

        # Report what a caller would actually hear, so an unusable clip -- a
        # synthesiser that wrote silence, say -- is obvious here rather than
        # three layers down in a scenario that mysteriously does nothing.
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
            from streamdouble import audio

            samples, rate = audio.load_wav(destination)
            seconds = samples.shape[0] / rate
            detail = f"{seconds:5.2f}s at {rate} Hz"
        except Exception as exc:  # pragma: no cover - reporting only
            detail = f"unreadable: {exc}"

        print(f'  {destination.name:<14} {detail}   "{text[:52]}"')

    print(f"\nwrote {len(CLIPS)} clips to {OUTPUT_DIR.relative_to(Path.cwd())}/")
    print("Not committed -- regenerate with this script, or drop in your own recordings.")


if __name__ == "__main__":
    main()
