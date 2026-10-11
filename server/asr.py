"""Speech-to-text for one captured audio chunk.

v2 checks the audio that is actually playing instead of searching for the
video by title -- the search is what picked the wrong video about half the
time in v1. Audio arrives as WAV (16 kHz mono PCM16 from Android playback
capture, 5 s per chunk), is transcribed here, and is never written to disk.

Providers, selected by ASR_PROVIDER:
    gemini  (default)  native audio input over REST, stdlib urllib only
    groq               whisper-large-v3-turbo via the OpenAI-compatible API
    openai             whisper-1

Silence is decided locally before any provider call. Whisper in particular
hallucinates on silent input ("Thank you for watching."), and an app that
blocks playback capture hands us pure zeros -- paying a provider to invent
text from those is the worst of both.
"""

from __future__ import annotations

import base64
import io
import json
import math
import os
import urllib.error
import urllib.request
import wave
from array import array
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

PROVIDER = os.environ.get("ASR_PROVIDER", "gemini").lower()

# Default is a current Gemini Flash model with native audio input. The exact
# id has NOT been confirmed against the live API from this machine (no key
# here) -- confirm it with a models.list call before relying on the default.
# .env.example already pins gemini-3.5-flash-lite for the fact-check model,
# which is a safe fallback if this id 404s.
GEMINI_MODEL = os.environ.get("ASR_MODEL", "gemini-3.5-flash")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

WHISPER = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "whisper-large-v3-turbo"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", "whisper-1"),
}

# Same threshold the client uses to mark a session `captured_silent`. A
# capture-blocked app delivers digital silence (-inf dBFS); quiet speech under
# background music sits well above -40. -55 leaves room for dither and noise.
SILENCE_DBFS = float(os.environ.get("ASR_SILENCE_DBFS", "-55"))

TIMEOUT_S = float(os.environ.get("ASR_TIMEOUT_S", "15"))

NO_SPEECH = "NO_SPEECH"
PROMPT = (
    "Transcribe the spoken words in this audio verbatim, in the language spoken. "
    "Output only the transcript: no timestamps, speaker labels, descriptions or commentary. "
    f"If there is no speech -- silence, noise, or music only -- output exactly {NO_SPEECH}."
)


class AsrError(RuntimeError):
    """The provider failed. Distinct from "no speech", which returns ""."""


def read_wav(wav_bytes: bytes) -> tuple[array, int]:
    """Parse a PCM16 WAV into (samples, sample_rate). Raises ValueError if it isn't one.

    Stereo is accepted and folded into one channel for the RMS only; the
    provider still receives the original bytes.
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            if w.getsampwidth() != 2:
                raise ValueError(f"expected 16-bit PCM, got {8 * w.getsampwidth()}-bit")
            rate = w.getframerate()
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as e:
        raise ValueError(f"not a WAV file: {e}") from e
    samples = array("h")
    samples.frombytes(frames[: len(frames) - len(frames) % 2])
    return samples, rate


def rms_dbfs(samples: array) -> float:
    """RMS level relative to full scale. -inf for empty or all-zero input."""
    if not samples:
        return float("-inf")
    mean_sq = sum(s * s for s in samples) / len(samples)
    if mean_sq == 0:
        return float("-inf")
    return 20 * math.log10(math.sqrt(mean_sq) / 32768.0)


def is_silent(wav_bytes: bytes) -> bool:
    """True when the chunk is below SILENCE_DBFS. Raises ValueError on a non-WAV."""
    samples, _ = read_wav(wav_bytes)
    return rms_dbfs(samples) < SILENCE_DBFS


def _gemini(wav_bytes: bytes) -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise AsrError("missing in .env: GEMINI_API_KEY")
    body = {
        "contents": [{
            "parts": [
                {"text": PROMPT},
                {"inline_data": {"mime_type": "audio/wav",
                                 "data": base64.b64encode(wav_bytes).decode("ascii")}},
            ],
        }],
        # Transcription, not generation: no sampling creativity wanted.
        "generationConfig": {"temperature": 0},
    }
    req = urllib.request.Request(
        GEMINI_URL.format(model=GEMINI_MODEL),
        data=json.dumps(body).encode(),
        # Header, not ?key= -- a query-string key ends up in proxy and access logs.
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise AsrError(f"gemini HTTP {e.code}") from e
    except Exception as e:
        raise AsrError(f"gemini {type(e).__name__}: {e}") from e
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        # Blocked or empty candidate. Treat as no usable speech rather than an
        # outage, so one odd chunk does not fail the whole reel.
        return ""
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def _whisper(wav_bytes: bytes) -> str:
    from openai import OpenAI  # already a dependency for factcheck

    base_url, env, default_model = WHISPER[PROVIDER]
    key = os.environ.get(env)
    if not key:
        raise AsrError(f"missing in .env: {env}")
    client = OpenAI(base_url=base_url, api_key=key, timeout=TIMEOUT_S)
    try:
        r = client.audio.transcriptions.create(
            model=os.environ.get("ASR_MODEL", default_model),
            file=("chunk.wav", wav_bytes, "audio/wav"),
            response_format="text",
        )
    except Exception as e:
        raise AsrError(f"{PROVIDER} {type(e).__name__}: {e}") from e
    return r if isinstance(r, str) else getattr(r, "text", "")


def _clean(text: str) -> str:
    text = " ".join((text or "").split())
    return "" if text.strip(" .").upper() == NO_SPEECH else text


def transcribe(wav_bytes: bytes) -> str:
    """WAV bytes -> transcript. "" for silence or music-only audio.

    Raises ValueError for input that is not a PCM16 WAV and AsrError when the
    provider fails, so callers can tell "nothing was said" from "we could not
    find out".
    """
    if is_silent(wav_bytes):
        return ""
    if PROVIDER == "gemini":
        return _clean(_gemini(wav_bytes))
    if PROVIDER in WHISPER:
        return _clean(_whisper(wav_bytes))
    raise AsrError(f"unknown ASR_PROVIDER {PROVIDER!r}")
