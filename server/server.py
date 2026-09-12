"""HTTP front end for the fact-check agent.

The Android app sends exactly what its accessibility service can read -- the
reel's content-desc title and the subscribe button's label -- and gets a badge
back. Nothing else is transmitted: no screenshots, no audio, no screen content
beyond the public title of a public video.

Run:
    ./.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000

The phone reaches it over Tailscale at http://100.82.117.61:8000 -- same
tailnet, no tunnel, no ngrok, and it never leaves the private network.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

# yt-dlp needs a JS runtime for YouTube extraction; deno lives in ~/.deno.
os.environ["PATH"] = os.path.expanduser("~/.deno/bin") + os.pathsep + os.environ.get("PATH", "")

import factcheck
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


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "model": factcheck.MODEL, "cached_videos": len(_cache)}
