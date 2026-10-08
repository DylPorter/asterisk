from __future__ import annotations

import base64
import io
import json
import urllib.error

import pytest

import asr
from conftest import make_wav


def test_read_wav_roundtrip():
    samples, rate = asr.read_wav(make_wav(0.5))
    assert rate == 16000 and len(samples) == 8000


def test_digital_silence_is_silent():
    samples, _ = asr.read_wav(make_wav(1.0, dbfs=None))
    assert asr.rms_dbfs(samples) == float("-inf")
    assert asr.is_silent(make_wav(1.0, dbfs=None))


@pytest.mark.parametrize("dbfs,silent", [(-70, True), (-58, True), (-50, False), (-20, False)])
def test_silence_threshold(dbfs, silent):
    assert asr.is_silent(make_wav(0.5, dbfs=dbfs)) is silent


def test_rms_level_is_accurate():
    samples, _ = asr.read_wav(make_wav(1.0, dbfs=-20))
    assert asr.rms_dbfs(samples) == pytest.approx(-20, abs=0.2)


@pytest.mark.parametrize("data", [b"", b"not a wav at all", b"RIFF\x00\x00"])
def test_non_wav_raises_value_error(data):
    with pytest.raises(ValueError):
        asr.is_silent(data)


def test_8bit_wav_rejected():
    with pytest.raises(ValueError, match="16-bit"):
        asr.read_wav(make_wav(0.1, width=1, dbfs=None))


def _no_network(*a, **k):
    raise AssertionError("provider must not be called")


def test_silence_never_reaches_the_provider(monkeypatch):
    monkeypatch.setattr(asr.urllib.request, "urlopen", _no_network)
    assert asr.transcribe(make_wav(1.0, dbfs=None)) == ""


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _gemini_reply(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setattr(asr, "PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-key")
    seen = {}

    def install(reply):
        def fake_urlopen(req, timeout):
            seen["req"], seen["timeout"] = req, timeout
            if isinstance(reply, Exception):
                raise reply
            return _Resp(json.dumps(reply).encode())
        monkeypatch.setattr(asr.urllib.request, "urlopen", fake_urlopen)
        return seen
    return install


def test_gemini_request_shape(gemini):
    seen = gemini(_gemini_reply("  Amazon made\n574 billion.  "))
    audio = make_wav(0.5)
    assert asr.transcribe(audio) == "Amazon made 574 billion."

    req = seen["req"]
    assert req.full_url.endswith(f"/models/{asr.GEMINI_MODEL}:generateContent")
    assert "key=" not in req.full_url  # key goes in a header, not the URL
    assert req.get_header("X-goog-api-key") == "test-not-a-key"
    part = json.loads(req.data)["contents"][0]["parts"][1]["inline_data"]
    assert part["mime_type"] == "audio/wav"
    assert base64.b64decode(part["data"]) == audio


@pytest.mark.parametrize("reply", ["NO_SPEECH", "NO_SPEECH.", " no_speech "])
def test_no_speech_marker_maps_to_empty(gemini, reply):
    gemini(_gemini_reply(reply))
    assert asr.transcribe(make_wav(0.5)) == ""


def test_empty_candidates_is_no_speech_not_an_error(gemini):
    gemini({"candidates": []})
    assert asr.transcribe(make_wav(0.5)) == ""


def test_http_error_raises_asr_error(gemini):
    gemini(urllib.error.HTTPError("u", 503, "busy", {}, None))
    with pytest.raises(asr.AsrError, match="503"):
        asr.transcribe(make_wav(0.5))


def test_missing_key_raises_asr_error(monkeypatch):
    monkeypatch.setattr(asr, "PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(asr.urllib.request, "urlopen", _no_network)
    with pytest.raises(asr.AsrError, match="GEMINI_API_KEY"):
        asr.transcribe(make_wav(0.5))


def test_unknown_provider(monkeypatch):
    monkeypatch.setattr(asr, "PROVIDER", "nope")
    with pytest.raises(asr.AsrError):
        asr.transcribe(make_wav(0.5))
