"""Transcript acquisition for YouTube Shorts.

Accessibility gives us title + channel, never a video ID. So we resolve the ID
ourselves. The obvious route -- YouTube Data API search.list -- costs 100 quota
units per call against a 10k/day default, i.e. 100 lookups a day, which is not
a product. yt-dlp's own search has no quota, and we already depend on it for
captions, so it's one dependency doing two jobs.

Measured on 12 live news/health/science Shorts: ~75% have usable auto-captions,
p50 ~1.4s per fetch. The floor is YouTube's round-trip, not this code -- bare
timedtext and innertube /player are PO-token gated and return empty.
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import urllib.parse
import urllib.request
from yt_dlp import YoutubeDL

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# Point YT_COOKIES at a Netscape-format cookies.txt exported from a browser
# that IS logged into YouTube. This box has no GUI and no logged-in browser,
# but the file can be exported elsewhere and copied here -- which is the only
# route out of a "Sign in to confirm you're not a bot" block when the IP
# cannot be changed.
COOKIEFILE = os.environ.get("YT_COOKIES") or str(Path(__file__).parent / "cookies.txt")

_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "extract_flat": False,
    # YouTube IP-blocked this host mid-session ("Sign in to confirm you're not
    # a bot" / "The page needs to be reloaded"), and valid cookies alone did
    # not lift it. The client pair matters: `ios` gets past the block but
    # returns no caption tracks, while `web_embedded` supplies them -- neither
    # works alone. ignore_no_formats_error is required because we skip the
    # download, so format selection has nothing to match and would otherwise
    # raise after a successful extraction.
    "ignore_no_formats_error": True,
    "extractor_args": {"youtube": {"player_client": ["ios", "web_embedded"]}},
    **({"cookiefile": COOKIEFILE} if os.path.isfile(COOKIEFILE) else {}),
}


@dataclass
class Transcript:
    video_id: str | None
    text: str
    source: str  # "manual" | "auto" | "none"
    seconds: float


CHANNEL_SCAN_DEPTH = 40  # Newest-first; a busy news channel posts ~10-15/day.

_INVISIBLE = dict.fromkeys(map(ord, "​‌‍﻿ "), " ")


def _norm(s: str) -> str:
    """Normalize a title for comparison.

    The accessibility tree's content-desc is NOT byte-identical to the
    canonical title -- the live BBC Short came back as "...#BBCNews​"
    with a trailing zero-width space. Exact matching passes on yt-dlp metadata
    and then silently fails on real device data, so normalize both sides.
    """
    return " ".join(s.translate(_INVISIBLE).split()).casefold().strip(" .")


def handle_from_subscribe_label(label: str) -> str | None:
    """'Subscribe to @BBCNews.' -> '@BBCNews'.

    This is how the channel arrives from the accessibility tree; the display
    name ('BBC News') is not usable as a URL, the handle is.
    """
    m = re.search(r"@[\w.\-]+", label or "")
    return m.group(0).rstrip(".") if m else None


def resolve_video_id(title: str, handle: str) -> str | None:
    """title + channel handle -> video ID, free and unmetered.

    Searches within the channel. Two approaches were measured and discarded:

    - Global YouTube search on the exact title: 0/8. Ranking favours
      popularity, and news channels post many near-identical Shorts, so the
      target never reached the top 5.
    - Paging the channel's 40 newest Shorts: 8/8 on recent uploads but 0/3 on
      Shorts taken from a live feed. The feed is algorithmic and serves
      months-old videos, so "recent" is the wrong window. That first 8/8 was
      selection bias -- the test set was collected *because* it was fresh.

    Channel-scoped search is 3/3 on the feed cases and faster (~0.4s vs 0.7s).
    """
    handle = handle if handle.startswith("@") else f"@{handle}"
    want = _norm(title)

    def _match(entries: list[dict]) -> str | None:
        for e in entries:
            if _norm(e.get("title") or "") == want:
                return e["id"]
        # Truncation fallback: some surfaces elide long titles with an ellipsis.
        for e in entries:
            cand = _norm(e.get("title") or "")
            if cand and (cand.startswith(want[:40]) or want.startswith(cand[:40])):
                return e["id"]
        return None

    def _fetch(url: str, limit: int) -> list[dict]:
        try:
            with YoutubeDL({**_BASE_OPTS, "extract_flat": True, "playlistend": limit}) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:
            return []
        return [e for e in ((info or {}).get("entries") or []) if e and e.get("id")]

    # Channel-scoped search first. Paging the channel's newest Shorts only
    # works if the viewer happens to be watching a recent upload -- but the
    # Shorts feed is algorithmic and routinely serves months-old videos.
    # Measured on three real feed Shorts: newest-40 listing found 0/3,
    # channel search found 3/3, and was faster (0.4s vs 0.7s).
    q = urllib.parse.quote(title)
    hit = _match(_fetch(f"https://www.youtube.com/{handle}/search?query={q}", 10))
    if hit:
        return hit

    # Fallback for channels where search misbehaves (very new uploads can lag
    # the search index).
    return _match(_fetch(f"https://www.youtube.com/{handle}/shorts", CHANNEL_SCAN_DEPTH))


def _pick_track(info: dict) -> tuple[str, str] | None:
    """Return (url, source). Manual captions beat ASR when both exist."""
    for key, source in (("subtitles", "manual"), ("automatic_captions", "auto")):
        tracks = info.get(key) or {}
        for lang in list(tracks):
            if not (lang == "en" or lang.startswith("en")):
                continue
            # json3 is the cheapest to parse; fall back to any format offered.
            fmts = tracks[lang]
            best = next((f for f in fmts if f.get("ext") == "json3"), None) or fmts[0]
            if best.get("url"):
                return best["url"], source
    return None


def _strip(raw: str, ext_hint: str) -> str:
    """Flatten a caption payload to plain prose, de-duplicating rolling lines."""
    if raw.lstrip().startswith("{"):
        import json

        try:
            data = json.loads(raw)
        except Exception:
            return ""
        parts = []
        for event in data.get("events") or []:
            for seg in event.get("segs") or []:
                parts.append(seg.get("utf8", ""))
        text = "".join(parts)
    else:  # vtt / srt
        lines = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or "-->" in line or line.isdigit() or line.startswith("WEBVTT"):
                continue
            lines.append(re.sub(r"<[^>]+>", "", line))
        # Auto-caption VTT repeats each line as it rolls; drop consecutive dupes.
        deduped = [l for i, l in enumerate(lines) if i == 0 or l != lines[i - 1]]
        text = " ".join(deduped)

    text = re.sub(r"\[(music|applause|laughter)\]", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def fetch(video_id: str, *, timeout: float = 6.0) -> Transcript:
    started = time.perf_counter()
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with YoutubeDL({**_BASE_OPTS, "writeautomaticsub": True, "writesubtitles": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return Transcript(video_id, "", "none", time.perf_counter() - started)

    picked = _pick_track(info or {})
    if not picked:
        return Transcript(video_id, "", "none", time.perf_counter() - started)

    track_url, source = picked
    try:
        req = urllib.request.Request(track_url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception:
        return Transcript(video_id, "", "none", time.perf_counter() - started)

    text = _strip(raw, source)
    return Transcript(
        video_id=video_id,
        text=text,
        source=source if text else "none",
        seconds=time.perf_counter() - started,
    )


def from_title_channel(title: str, handle: str) -> Transcript:
    """The real entry point: exactly what the accessibility service gives us.

    `title` is the reel's content-desc; `handle` is either '@Name' or the raw
    'Subscribe to @Name.' label straight off the subscribe button.
    """
    started = time.perf_counter()
    handle = handle_from_subscribe_label(handle) or handle
    vid = resolve_video_id(title, handle)
    if not vid:
        return Transcript(None, "", "none", time.perf_counter() - started)
    t = fetch(vid)
    return Transcript(t.video_id, t.text, t.source, time.perf_counter() - started)


# --- TikTok -----------------------------------------------------------------

def _tiktok_caption(info: dict) -> str:
    for k in ("title", "description", "alt_title"):
        val = (info.get(k) or "").strip()
        if val:
            return val
    return ""


def from_tiktok(caption: str, creator: str, *, exa_key: str | None = None) -> Transcript:
    """TikTok caption + creator display name -> transcript.

    The accessibility tree exposes only a display name ("Tesla Owners Silicon
    Valley"), never the URL handle, so yt-dlp's user-listing route is closed.
    A domain-restricted web search recovers the handle (-> @teslaownerssv) and
    a candidate video URL in <1s.

    The match check below is NOT optional. Search reliably finds the right
    CREATOR and unreliably finds the right VIDEO -- the top hit for a live
    caption was a video from two years earlier. Fact-checking the wrong video
    while reporting success is the failure mode that actually destroys trust,
    so an unverified candidate is discarded rather than used.
    """
    import os

    started = time.perf_counter()
    key = exa_key or os.environ.get("EXA_API_KEY")
    if not key or not caption.strip():
        return Transcript(None, "", "none", time.perf_counter() - started)

    # The tree truncates the caption with an ellipsis + "more" affordance.
    raw_caption = caption.strip()
    truncated = bool(re.search(r"(?:…|\.\.\.)\s*more\s*$", raw_caption)) or raw_caption.endswith("…")
    stem = re.split(r"\s*(?:…|\.\.\.)\s*more\s*$", raw_caption)[0]
    # Strip emoji, hashtags and trailing punctuation before quoting. The stem
    # goes inside a quoted search phrase, so anything the indexer did not store
    # verbatim breaks the match: keeping "🤖🚀" returned a different video by
    # the same creator, which the oEmbed check then correctly rejected.
    # Strip ONLY emoji and invisible formatting -- never letters. An earlier
    # ASCII whitelist also deleted curly apostrophes and every accented
    # character, so "Poland's" became "Polands" and "c'etait" became "ctait":
    # the quoted search phrase was corrupted into something that matched
    # nothing. Every non-ASCII caption failed; the one ASCII caption worked.
    stem = re.sub(r"#\w+", " ", stem)
    stem = stem.replace("\u2019", "'").replace("\u2018", "'")
    stem = stem.replace("\u201c", '"').replace("\u201d", '"').replace("\u00a0", " ")
    stem = "".join(
        c for c in stem
        if unicodedata.category(c) not in ("So", "Sk", "Cf", "Cs", "Co")
    )
    stem = re.sub(r"\s+", " ", stem).strip(" .,!?-|")
    # A long quoted phrase is brittle; the first dozen words are distinctive
    # enough and far more likely to be indexed verbatim.
    # TikTok cuts the caption mid-WORD before "…more", so the final token is
    # often a fragment ("Not an of"). A quoted phrase ending in a partial word
    # matches nothing, and longer captions get cut earlier -- which is why the
    # long ones failed most. Drop the last token when we know it was truncated,
    # and keep the phrase short: a distinctive 8-word run is far likelier to be
    # indexed verbatim than a 20-word one.
    words = stem.split()
    if truncated and len(words) > 4:
        words = words[:-1]
    stem = " ".join(words[:8])

    # Resolve via DuckDuckGo, NOT Exa. Measured: Exa returns the right
    # creator but never the right video -- three query forms (quoted both,
    # quoted caption, unquoted) all surfaced the same 2023 uploads and never
    # the target. DuckDuckGo returned the exact video as hit #1. Search-engine
    # coverage of recent TikTok video pages differs by index; that difference,
    # not the query phrasing, was the blocker.
    try:
        from ddgs import DDGS
        rows = list(DDGS().text(f'"{creator}" tiktok "{stem}"', max_results=8))
    except Exception:
        return Transcript(None, "", "none", time.perf_counter() - started)

    urls, seen = [], set()
    for r in rows:
        m = re.search(r"tiktok\.com/@[\w.\-]+/video/\d+", r.get("href") or "")
        if m and m.group(0) not in seen:
            seen.add(m.group(0))
            urls.append("https://www." + m.group(0))

    want = _norm(stem)[:60]
    for url in urls[:3]:
        # oEmbed is unauthenticated, ~0.3s, and returns the FULL untruncated
        # caption plus author_unique_id (the real @handle). Cheap enough to
        # verify every candidate before committing to extraction, which is
        # what stops us fact-checking the wrong video.
        try:
            import urllib.parse as _up
            meta = json.loads(urllib.request.urlopen(
                "https://www.tiktok.com/oembed?url=" + _up.quote(url, safe=":/@?=&"),
                timeout=10).read().decode("utf-8", "replace"))
        except Exception:
            continue
        full = _norm(meta.get("title") or "")
        if not (full.startswith(want[:35]) or want[:35] in full):
            continue

        try:
            with YoutubeDL({**_BASE_OPTS, "writeautomaticsub": True, "writesubtitles": True}) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:
            continue
        if not info:
            continue
        vid = info.get("id")
        picked = _pick_track(info)
        if not picked:
            return Transcript(vid, "", "none", time.perf_counter() - started)
        track_url, source = picked
        try:
            req = urllib.request.Request(track_url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=8) as r:
                raw = r.read().decode("utf-8", "replace")
        except Exception:
            return Transcript(vid, "", "none", time.perf_counter() - started)
        text = _strip(raw, source)
        return Transcript(vid, text, source if text else "none",
                          time.perf_counter() - started)

    return Transcript(None, "", "none", time.perf_counter() - started)

    # Step 2: verify candidates. yt-dlp's TikTok user listing cannot be used
    # here -- it fails with "Unable to extract secondary user ID" for
    # tiktok.com/@handle URLs, needing an internal channel_id we have no way
    # to obtain. So we check the search hits directly and accept an honest
    # miss when the video is not indexed, rather than fact-checking whichever
    # video the search happened to rank first.
    want = _norm(stem)[:60]
    for url in [r.url for r in hits.results if "/video/" in (r.url or "")][:5]:
        try:
            with YoutubeDL({**_BASE_OPTS, "writeautomaticsub": True, "writesubtitles": True}) as ydl:
                info = ydl.extract_info(url.split("?")[0], download=False)
        except Exception:
            continue
        if not info:
            continue
        if not (_norm(_tiktok_caption(info)).startswith(want[:35]) or want[:35] in _norm(_tiktok_caption(info))):
            continue

        vid = info.get("id")
        picked = _pick_track(info)
        if not picked:
            return Transcript(vid, "", "none", time.perf_counter() - started)
        track_url, source = picked
        try:
            req = urllib.request.Request(track_url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=8) as r:
                raw = r.read().decode("utf-8", "replace")
        except Exception:
            return Transcript(vid, "", "none", time.perf_counter() - started)
        text = _strip(raw, source)
        return Transcript(vid, text, source if text else "none",
                          time.perf_counter() - started)

    return Transcript(None, "", "none", time.perf_counter() - started)
