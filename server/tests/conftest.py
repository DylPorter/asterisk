"""Shared fixtures. Every network call (ASR, LLM, search, yt-dlp) is faked.

There are no API keys on the dev machine and tests must never need one: the
LLM is replaced at the OpenAI-client boundary so the real agent loop,
validation, grounding and reconcile all still run.
"""

from __future__ import annotations

import io
import json
import math
import sys
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import factcheck  # noqa: E402


def make_wav(seconds: float = 1.0, dbfs: float | None = -20.0, rate: int = 16000,
             width: int = 2, freq: float = 440.0) -> bytes:
    """A mono sine at roughly `dbfs` RMS, or digital silence when dbfs is None."""
    n = int(seconds * rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(width)
        w.setframerate(rate)
        if dbfs is None:
            w.writeframes(b"\x00" * n * width)
        else:
            # RMS of a sine is peak / sqrt(2).
            peak = 32767 * (10 ** (dbfs / 20)) * math.sqrt(2)
            from array import array
            a = array("h", (int(peak * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)))
            w.writeframes(a.tobytes())
    return buf.getvalue()


@pytest.fixture
def wav():
    return make_wav


class FakeLLM:
    """Stands in for openai.OpenAI. First call: no tool call (agent done
    searching). Second call: the forced, schema-bound answer."""

    def __init__(self, verdict: dict[str, Any]):
        self.verdict = verdict
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def with_options(self, **_: Any) -> "FakeLLM":
        return self

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        content = json.dumps(self.verdict) if kwargs.get("tool_choice") == "none" else "Enough."
        msg = SimpleNamespace(content=content, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


@pytest.fixture
def fake_llm(monkeypatch):
    """Install a FakeLLM returning `verdict`; returns the instance for asserts."""
    def install(verdict: dict[str, Any]) -> FakeLLM:
        llm = FakeLLM(verdict)
        monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-key")
        monkeypatch.setenv("EXA_API_KEY", "test-not-a-key")
        monkeypatch.setattr(factcheck, "MODEL", "gemini-test")
        monkeypatch.setattr(factcheck, "OpenAI", lambda **_: llm)
        monkeypatch.setattr(factcheck, "Exa", lambda **_: SimpleNamespace())
        return llm
    return install


def claim(quote: str, rating: str = "supported", text: str = "A claim") -> dict[str, Any]:
    return {"quote": quote, "claim": text, "rating": rating, "note": "n", "sources": []}


def verdict(*claims: dict[str, Any], **kw: Any) -> dict[str, Any]:
    v = {"score": 5, "overall": "clear", "headline": "Holds up",
         "statements_checked": len(claims), "statements_wrong": 0,
         "factual_pct": 100, "claims": list(claims), "skipped": 0}
    v.update(kw)
    return v
