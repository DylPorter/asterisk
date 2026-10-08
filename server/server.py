"""HTTP front end for the fact-check agent.

Two paths, and they send different things. Stated exactly, because the
difference matters:

v1, POST /check -- the Android accessibility service sends what it can read
off the screen: the reel's content-desc title and the subscribe button's
label. No audio and no screenshots. The server finds the video by title,
fetches its public captions, and caches the verdict by video ID in
out/verdict_cache.json.

v2, POST /v2/segments + POST /v2/check -- the app ALSO sends audio:
  - What: the playback audio of the reel on screen, as 16 kHz mono PCM16 WAV
    chunks of about 5 s. Captured only from the YouTube, TikTok and Instagram
    apps' own playback (AudioPlaybackCaptureConfiguration), never the
    microphone, never screen pixels.
  - When: only after the reel has been on screen for 5 s. A reel swiped away
    sooner uploads nothing; its buffer is discarded on the device.
  - With it: the app name, a random per-reel-view session UUID and a chunk
    number. At tap, /v2/check adds the same title/channel hints as v1.
  - Where it goes: each chunk is forwarded to the ASR provider (Gemini by
    default, see asr.py) to be transcribed. That is a third party seeing the
    audio; nothing else does.
  - What is kept: the audio is never written to disk and is not retained after
    its transcription call returns. Transcripts are held in memory per session
    and dropped 10 min after the session starts (sessions.py). v2 verdicts --
    which include quotes of at most 25 words per claim -- are cached in memory
    only, never in the on-disk cache, and v2 failures are logged without the
    model's raw output so no transcript text reaches out/failures.jsonl.

Run:
    ./.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000

The phone reaches it over Tailscale at http://100.82.117.61:8000 -- same
tailnet, no tunnel, no ngrok, and it never leaves the private network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

# yt-dlp needs a JS runtime for YouTube extraction; deno lives in ~/.deno.
os.environ["PATH"] = os.path.expanduser("~/.deno/bin") + os.pathsep + os.environ.get("PATH", "")

import asr
import factcheck
import grounding
import sessions
import transcript as tx

app = FastAPI(title="shortcheck")

# Verdicts are pure functions of a video ID, so cache them. This is what makes
# a re-watch instant, and it means a viral Short is checked once for everyone
# rather than once per viewer. Prefetching the *next* Short is not available to
# us -- unlike a browser extension reading a preloaded feed from the DOM, the
# accessibility tree only ever exposes the Short currently on screen.
CACHE_FILE = os.path.join(os.path.dirname(__file__), "out", "verdict_cache.json")


def _load_cache() -> dict[str, Any]:
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


_cache: dict[str, dict[str, Any]] = _load_cache()
_inflight: dict[str, asyncio.Task] = {}


class CheckRequest(BaseModel):
    # The app sends several candidate strings, best guess first. Picking the
    # title from the accessibility tree by heuristic is unreliable -- live
    # failures included a search-suggestion chip ('Search "jeff bezos ambani
    # wedding"') and a remix label. Rather than enumerate every UI string
    # YouTube will ever render, try each candidate against the channel and let
    # resolution decide: either a video with that title exists there, or it
    # does not. Costs one extra ~0.5s lookup on a miss.
    platform: str = "youtube"   # "youtube" | "tiktok"
    titles: list[str] = []
    title: str = ""
    channel: str = ""          # '@Handle' or the raw 'Subscribe to @Handle.' label
    deadline_s: float = 20.0


# Flip to "0" for a user-facing release: the app then renders our failures as
# a neutral grey "can't check" instead of a debug glyph nobody can act on.
DEBUG_BADGES = os.environ.get("SHORTCHECK_DEBUG_BADGES", "1") == "1"


class CheckResponse(BaseModel):
    debug: bool = DEBUG_BADGES
    status: str                # "ok" | "no_transcript" | "error"
    video_id: str | None = None
    cached: bool = False
    seconds: float = 0.0
    verdict: dict[str, Any] | None = None
    queries: list[str] = []
    detail: str | None = None


FAILLOG = os.path.join(os.path.dirname(__file__), "out", "failures.jsonl")


def _record_failure(stage, title, channel, vid, error, raw):
    """Append every failure with enough context to group by SHAPE later."""
    try:
        os.makedirs(os.path.dirname(FAILLOG), exist_ok=True)
        with open(FAILLOG, "a") as f:
            f.write(json.dumps({
                "ts": time.time(), "stage": stage, "title": title,
                "channel": channel, "video_id": vid, "error": error, "raw": raw,
            }) + "\n")
    except Exception:
        pass


def _pipeline(titles: list[str], channel: str, deadline_s: float,
              platform: str = "youtube") -> CheckResponse:
    t0 = time.perf_counter()
    t, title = None, titles[0] if titles else ""
    for cand in titles[:3]:
        attempt = (tx.from_tiktok(cand, channel) if platform == "tiktok"
                   else tx.from_title_channel(cand, channel))
        if attempt.video_id:
            t, title = attempt, cand
            break
    if t is None or not t.video_id:
        _record_failure("resolve", " | ".join(titles[:3]), channel, None,
                        "could not resolve video id", None)
        return CheckResponse(status="error", detail="could not resolve video id",
                             seconds=time.perf_counter() - t0)
    if t.video_id in _cache:
        c = _cache[t.video_id]
        return CheckResponse(status="ok", video_id=t.video_id, cached=True,
                             seconds=time.perf_counter() - t0, verdict=c["verdict"],
                             queries=c["queries"])
    if not t.text:
        # ~25% of Shorts: no captions at all, or text-on-screen only. Say so
        # rather than inventing a verdict from the title.
        return CheckResponse(status="no_transcript", video_id=t.video_id,
                             seconds=time.perf_counter() - t0)

    r = factcheck.check(t.text, title, channel, deadline_s=deadline_s)
    if not r.verdict:
        _record_failure("agent", title, channel, t.video_id, r.error, r.raw)
        return CheckResponse(status="error", video_id=t.video_id, detail=r.error,
                             seconds=time.perf_counter() - t0, queries=r.queries)

    payload = {"verdict": r.verdict.model_dump(), "queries": r.queries}
    _cache[t.video_id] = payload
    # Persist immediately. The cache is what makes a demo survive a YouTube
    # rate-limit: a cached verdict never touches yt-dlp again. Keeping it only
    # in memory meant a server restart threw away every pre-warmed video.
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(_cache, f)
    except Exception:
        pass
    return CheckResponse(status="ok", video_id=t.video_id, verdict=payload["verdict"],
                         queries=r.queries, seconds=time.perf_counter() - t0)


@app.post("/check", response_model=CheckResponse)
async def check(req: CheckRequest) -> CheckResponse:
    """One badge for one Short.

    Deliberately NOT cancelled when the client disconnects. The user swiping
    away should detach the UI, not throw away work that is nearly done -- the
    result still lands in the cache and makes a swipe-back instant.
    """
    cands = [c for c in ([*req.titles, req.title]) if c and c.strip()]
    res = await run_in_threadpool(_pipeline, cands, req.channel, req.deadline_s, req.platform)
    # Log the outcome: a bare 200 hid every failure reason during testing.
    print(f"[check] {res.status:<14} {req.channel:<20} {(cands[0] if cands else '')[:46]!r} "
          f"id={res.video_id} {res.seconds:.1f}s {res.detail or ''}", flush=True)
    return res


# --- v2: on-device capture -------------------------------------------------

# 120 s of 16 kHz mono PCM16 is 3.84 MB -- the client's whole ring buffer. A
# 5 s chunk is ~160 KB. Anything near the cap is a bug or abuse, not a reel.
MAX_SEGMENT_BYTES = 4 * 1024 * 1024

# How long /v2/check waits for chunks still being transcribed. The prefetch
# means usually only the final partial chunk is outstanding at tap time.
# Not measured yet -- a guess at one Gemini audio call; eval/ reports latency.
TRANSCRIPT_WAIT_S = float(os.environ.get("TRANSCRIPT_WAIT_S", "8"))
# Share of the client's deadline the transcript may take when NOTHING is
# transcribed yet. The short wait above is right when earlier chunks are done
# and only the tail is late -- a partial transcript still checks. With no text
# at all, giving up at 8 s while an ASR call may legitimately take
# asr.TIMEOUT_S (15) just turns a slow call into a wasted tap. 0.6 of the
# phone's 25 s deadline is 15 s, leaving 10 s for the fact-check.
EMPTY_WAIT_SHARE = 0.6

# Character budget for the transcript sent to the LLM. ~12k chars is roughly
# 2,000 words, far more than the MAX_SEQ chunks of speech a reel produces,
# so a real reel never hits it; it caps the prompt (and the bill) if
# something upstream misbehaves. Grounding sees the same truncated text, so a
# quote from the cut part is dropped rather than half-checked.
MAX_TRANSCRIPT_CHARS = 12_000

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_APP = re.compile(r"^[a-z]{1,20}$")

# v2 verdicts stay in memory. They carry transcript quotes, and the privacy
# constraint is that transcripts are not persisted -- so they never go into
# CACHE_FILE. Bounded so a long-running server cannot grow without limit.
_V2_CACHE_MAX = 2000
_v2_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()


class V2CheckRequest(BaseModel):
    session_id: str
    app: str = "youtube"        # "youtube" | "tiktok" | "instagram"
    titles: list[str] = []
    channel: str = ""
    final_seq: int = -1         # last chunk the client uploaded; -1 = none
    deadline_s: float = 20.0


class V2CheckResponse(CheckResponse):
    # v1's status vocabulary (ok | no_transcript | error) plus one:
    # "captured_silent" -- the app blocked capture and there is no v1 route.
    # Kept distinct from no_transcript, which the phone renders as "No speech
    # in this video"; that would be false for a reel we simply could not hear.
    session_id: str | None = None
    source: str = "audio"       # "audio" | "v1_fallback"
    captured_silent: bool = False


def _hint_key(app_name: str, channel: str, titles: list[str]) -> str | None:
    """`app|channel|normalised title`, or None without a title.

    Caveat carried over from v1: titles[0] is a heuristic pick from the
    accessibility tree and has been a search-suggestion chip before. Two reels
    from one channel that both got a generic chip would share a key. The
    transcript-hash key below has no such failure; it only misses more often.
    """
    title = grounding.normalise(titles[0]) if titles else ""
    if not title:
        return None
    handle = tx.handle_from_subscribe_label(channel) or channel
    return f"{app_name}|{handle.strip().casefold()}|{title}"


def _transcript_key(text: str) -> str:
    return "sha256:" + hashlib.sha256(grounding.normalise(text).encode()).hexdigest()


# Hint keys are client-supplied, so a hint-key hit alone proves nothing: a
# client could send a viral reel's title with arbitrary audio and have that
# verdict served to everyone who checks the real reel (cache poisoning). A hit
# is served only when the requester's own audio says the same thing as the
# audio the verdict was made from. Compared as hashed 4-word shingles, so the
# cache holds no recoverable transcript, and Jaccard tolerates the word-level
# drift between two ASR passes over the same reel.
_SHINGLE_N = 4
SAME_AUDIO_JACCARD = float(os.environ.get("SAME_AUDIO_JACCARD", "0.5"))


def _shingles(text: str) -> frozenset[str]:
    words = grounding.normalise(text).split()
    if len(words) < _SHINGLE_N:
        words = words + [""] * (_SHINGLE_N - len(words))
    return frozenset(
        hashlib.sha256(" ".join(words[i:i + _SHINGLE_N]).encode()).hexdigest()[:16]
        for i in range(len(words) - _SHINGLE_N + 1))


def _same_audio(a: frozenset[str], b: frozenset[str]) -> bool:
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= SAME_AUDIO_JACCARD


def _v2_cached(key: str | None, shingles: frozenset[str] | None = None) -> dict[str, Any] | None:
    if key and key in _v2_cache:
        hit = _v2_cache[key]
        if shingles is not None and not _same_audio(hit["shingles"], shingles):
            return None
        _v2_cache.move_to_end(key)
        return hit
    return None


def _v2_store(key: str, payload: dict[str, Any]) -> None:
    _v2_cache[key] = payload
    _v2_cache.move_to_end(key)
    while len(_v2_cache) > _V2_CACHE_MAX:
        _v2_cache.popitem(last=False)


def _v2_pipeline(req: V2CheckRequest) -> V2CheckResponse:
    t0 = time.perf_counter()
    sid = req.session_id
    titles = [t for t in req.titles if t and t.strip()]

    def done(**kw: Any) -> V2CheckResponse:
        kw.setdefault("seconds", time.perf_counter() - t0)
        return V2CheckResponse(session_id=sid, **kw)

    # No cache lookup before the audio is heard: the hint key is client-supplied
    # (see _same_audio). The transcript is normally prefetched by tap time, so
    # this costs little.
    hint_key = _hint_key(req.app, req.channel, titles)

    sess = sessions.get(sid)
    silent = sess is not None and sess.captured_silent
    if sess is None or silent:
        # Silent means the app opted out of playback capture. YouTube still
        # has the v1 route (title -> captions); the others have nothing to
        # check. An unknown session (expired, or every upload lost) is
        # treated the same way: for YouTube v1 is still the best answer.
        if req.app == "youtube":
            r = _pipeline(titles, req.channel, req.deadline_s, "youtube")
            return done(**{**r.model_dump(), "seconds": time.perf_counter() - t0},
                        source="v1_fallback", captured_silent=silent)
        if silent:
            return done(status="captured_silent", captured_silent=True,
                        detail="can't check this video: the app blocks audio capture")
        return done(status="error", detail="unknown or expired session")

    sessions.set_hints(sid, titles=titles, channel=req.channel)
    wait = min(TRANSCRIPT_WAIT_S, req.deadline_s * 0.4)
    try:
        text = sessions.transcript(sid, req.final_seq, wait)
        if not text.strip() and sessions.outstanding(sid, req.final_seq):
            # Nothing usable yet and something is still in flight: a partial
            # transcript is not an option, so spend more of the deadline
            # rather than give up on a call that has not had its full time.
            extra = min(asr.TIMEOUT_S, req.deadline_s * EMPTY_WAIT_SHARE) - wait
            if extra > 0:
                text = sessions.transcript(sid, req.final_seq, extra)
        pending = sessions.outstanding(sid, req.final_seq)
    except KeyError:
        return done(status="error", detail="unknown or expired session")

    if not text.strip():
        failed = [s.error for s in sess.segments.values() if s.state == sessions.FAILED]
        if failed:
            return done(status="error", detail=f"transcription failed: {failed[-1]}")
        if pending:
            # Timed out, not empty. "No speech in this video" would be a
            # false statement about a video we simply had not heard yet.
            return done(status="error", detail="still transcribing; try again")
        # Capture worked but nobody spoke: music, ambience, a silent clip.
        return done(status="no_transcript", detail="no speech in the audio")
    text = text[:MAX_TRANSCRIPT_CHARS]

    shingles = _shingles(text)
    tkey = _transcript_key(text)
    for k in (tkey, hint_key):
        if (c := _v2_cached(k, shingles)) is not None:
            return done(status="ok", cached=True, verdict=c["verdict"], queries=c["queries"])

    left = max(req.deadline_s - (time.perf_counter() - t0), 2.0)
    r = factcheck.check(text, titles[0] if titles else "", req.channel, deadline_s=left)
    if not r.verdict:
        # raw=None: the model's raw output quotes the transcript, and the
        # failure log is on disk.
        _record_failure("agent_v2", " | ".join(titles[:3]), req.channel, None, r.error, None)
        return done(status="error", detail=r.error, queries=r.queries)

    payload = {"verdict": r.verdict.model_dump(), "queries": r.queries, "shingles": shingles}
    _v2_store(tkey, payload)
    # First writer keeps a hint key unless the new verdict is from the same
    # audio: otherwise a client sending a viral reel's hints with other audio
    # could evict the real entry, even though it can no longer be served it.
    if hint_key and (hint_key not in _v2_cache
                     or _same_audio(_v2_cache[hint_key]["shingles"], shingles)):
        _v2_store(hint_key, payload)
    return done(status="ok", verdict=payload["verdict"], queries=r.queries)


@app.post("/v2/segments")
async def v2_segments(
    request: Request,
    session_id: str,
    seq: int = Query(ge=0, le=sessions.MAX_SEQ),
    app_name: str = Query(alias="app"),
) -> dict[str, Any]:
    """One ~5 s WAV chunk of a reel past the dwell gate. Transcribed in the background."""
    if not _ID.match(session_id):
        raise HTTPException(400, "bad session_id")
    if not _APP.match(app_name):
        raise HTTPException(400, "bad app")
    # Enforced while reading, not after: request.body() buffers the whole
    # upload first, and neither uvicorn nor Starlette caps body size, so a
    # check on len(body) would run only after a multi-GB body was in RAM.
    # Content-Length is refused up front; the running total catches chunked
    # uploads that send none, or a header that lies.
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_SEGMENT_BYTES:
                raise HTTPException(413, "segment too large")
        except ValueError:
            raise HTTPException(400, "bad content-length")
    buf = bytearray()
    async for part in request.stream():
        buf += part
        if len(buf) > MAX_SEGMENT_BYTES:
            raise HTTPException(413, "segment too large")
    body = bytes(buf)
    try:
        # The RMS pass over ~80k samples is pure Python; keep it off the loop.
        state = await run_in_threadpool(sessions.add_segment, session_id, seq, app_name, body)
    except sessions.Busy as e:
        # The client retries a failed upload once and then reuses the seq on
        # its next chunk, so a 503 delays a chunk rather than losing the reel.
        raise HTTPException(503, str(e), headers={"Retry-After": "5"})
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "session_id": session_id, "seq": seq, "state": state}


@app.post("/v2/check", response_model=V2CheckResponse)
async def v2_check(req: V2CheckRequest) -> V2CheckResponse:
    """The tap. Waits for any chunk still transcribing, then fact-checks.

    Like /check, not cancelled on client disconnect: the verdict still lands
    in the cache.
    """
    if not _ID.match(req.session_id):
        raise HTTPException(400, "bad session_id")
    res = await run_in_threadpool(_v2_pipeline, req)
    print(f"[v2]    {res.status:<14} {req.app:<9} {req.channel:<20} src={res.source} "
          f"silent={res.captured_silent} {res.seconds:.1f}s {res.detail or ''}", flush=True)
    return res


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "model": factcheck.MODEL, "cached_videos": len(_cache),
            "asr": asr.PROVIDER, "sessions": len(sessions.store),
            "v2_cached": len(_v2_cache)}
