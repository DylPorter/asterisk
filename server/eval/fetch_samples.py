"""Dev-only: pull the audio of a list of URLs into the eval set.

    cd server
    python eval/fetch_samples.py urls.txt            # one URL per line, # comments ok
    python eval/fetch_samples.py --url https://www.youtube.com/shorts/ID --expected "..."

Writes 16 kHz mono WAV to eval/samples/ and adds an entry per video to
eval/manifest.json (merged by URL, so re-running is safe and keeps any notes
already written). Never runs in the server: v2 exists precisely so production
does not depend on yt-dlp, which is IP-blocked on datacenter ranges.

Audio in eval/samples/ is other people's content, kept locally for testing
only. It is gitignored; do not commit or redistribute it.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

# The app names the client sends as `app` on /v2/segments, so manifest rows
# and live sessions report under the same keys.
APP_BY_HOST = (
    (re.compile(r"(?:^|\.)(?:youtube\.com|youtu\.be)$"), "youtube"),
    (re.compile(r"(?:^|\.)tiktok\.com$"), "tiktok"),
    (re.compile(r"(?:^|\.)instagram\.com$"), "instagram"),
)


def app_for(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url.strip())
    host = m.group(1).lower() if m else ""
    for pat, app in APP_BY_HOST:
        if pat.search(host):
            return app
    return "unknown"


def slug(app: str, info: dict[str, Any]) -> str:
    vid = re.sub(r"[^A-Za-z0-9_-]", "", str(info.get("id") or "x"))[:40]
    return f"{app}_{vid}"


def read_urls(path: Path) -> list[tuple[str, str]]:
    """Lines of `URL` or `URL<TAB or two spaces>expected notes`. # starts a comment."""
    out = []
    for line in path.read_text().splitlines():
        line = line.split(" #", 1)[0].strip() if not line.lstrip().startswith("#") else ""
        if not line:
            continue
        parts = re.split(r"\t|\s{2,}", line, maxsplit=1)
        url, expected = parts[0], (parts[1].strip() if len(parts) > 1 else "")
        out.append((url, expected))
    return out


def fetch(url: str, dest: Path, cookies: str | None) -> dict[str, Any]:
    """Download one URL's audio as dest/<app>_<id>.wav and return its manifest row."""
    from yt_dlp import YoutubeDL  # dev-only dependency, imported on use

    app = app_for(url)
    opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": str(dest / f"{app}_%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # Convert straight to what the device uploads, so run_eval's ffmpeg
        # step is a no-op and the file on disk is what the ASR will hear.
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
        "postprocessor_args": {"extractaudio": ["-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le"]},
    }
    if cookies:
        opts["cookiefile"] = cookies
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    name = slug(app, info) + ".wav"
    if not (dest / name).exists():
        raise RuntimeError(f"yt-dlp finished but {name} is missing")
    return {
        "file": f"samples/{name}",
        "app": app,
        "url": url,
        # Hints as accessibility would read them. run_eval --no-hints drops
        # them to mimic a Reel where the overlay gets nothing usable.
        "title": info.get("title") or (info.get("description") or "")[:120],
        "channel": info.get("uploader_id") or info.get("channel") or info.get("uploader") or "",
        "duration_s": info.get("duration"),
        "topic": "",
        "expected": "",
    }


def merge(manifest: Path, rows: list[dict[str, Any]]) -> int:
    """Add rows by URL. Hand-written fields (expected, topic) on existing rows win."""
    data = json.loads(manifest.read_text()) if manifest.exists() else []
    existing: list[dict[str, Any]] = data.get("items", []) if isinstance(data, dict) else data
    by_url = {r.get("url"): r for r in existing if r.get("url")}
    added = 0
    for row in rows:
        old = by_url.get(row["url"])
        if old is None:
            existing.append(row)
            by_url[row["url"]] = row
            added += 1
        else:
            for k, v in row.items():
                if not old.get(k):
                    old[k] = v
    manifest.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n")
    return added


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("urls_file", nargs="?", type=Path)
    p.add_argument("--url", action="append", default=[], help="a single URL (repeatable)")
    p.add_argument("--expected", default="", help="expected notes for --url entries")
    p.add_argument("--topic", default="", help="news / health / finance / ... for every row fetched")
    p.add_argument("--manifest", type=Path, default=HERE / "manifest.json")
    p.add_argument("--dest", type=Path, default=HERE / "samples")
    p.add_argument("--cookies", help="Netscape cookies file passed to yt-dlp (path only; never read here)")
    args = p.parse_args(argv)

    todo = [(u, args.expected) for u in args.url]
    if args.urls_file:
        todo += read_urls(args.urls_file)
    if not todo:
        p.print_help()
        return 2
    if not shutil.which("ffmpeg"):
        print("ffmpeg is required to convert audio to 16 kHz mono WAV", file=sys.stderr)
        return 2

    args.dest.mkdir(parents=True, exist_ok=True)
    rows, failed = [], 0
    for url, expected in todo:
        try:
            row = fetch(url, args.dest, args.cookies)
        except Exception as e:
            failed += 1
            print(f"FAIL {url}  {type(e).__name__}: {str(e)[:160]}")
            continue
        row["expected"] = expected
        row["topic"] = args.topic
        rows.append(row)
        print(f"ok   {row['app']:<9} {row['file']}  {row['title'][:60]!r}")

    added = merge(args.manifest, rows)
    print(f"\n{len(rows)} fetched, {failed} failed, {added} new rows in {args.manifest}")
    return 1 if failed and not rows else 0


if __name__ == "__main__":
    raise SystemExit(main())
