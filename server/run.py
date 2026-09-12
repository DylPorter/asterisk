"""End-to-end probe: Short -> transcript -> agentic fact-check -> timings.

    ./.venv/bin/python run.py https://www.youtube.com/shorts/VIDEO_ID
    ./.venv/bin/python run.py --title "..." --channel "..."

The second form is what the Android side will actually call: accessibility
gives us title + channel, never a video ID.
"""

from __future__ import annotations

import argparse
import re
import sys
import time

import factcheck
import transcript as tx

BADGE = {
    "clear": "\033[32m● CLEAR\033[0m",
    "caution": "\033[33m▲ CAUTION\033[0m",
    "misleading": "\033[31m✖ MISLEADING\033[0m",
    "unverifiable": "\033[90m? UNVERIFIABLE\033[0m",
}
RATING = {
    "supported": "\033[32m✓\033[0m",
    "contradicted": "\033[31m✗\033[0m",
    "misleading": "\033[31m~\033[0m",
    "unverifiable": "\033[90m?\033[0m",
}


def video_id(s: str) -> str | None:
    m = re.search(r"(?:shorts/|watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})", s)
    return m.group(1) if m else (s if re.fullmatch(r"[A-Za-z0-9_-]{11}", s) else None)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("url", nargs="?", help="Shorts URL or bare video ID")
    p.add_argument("--title")
    p.add_argument("--channel", default="")
    p.add_argument("--deadline", type=float, default=10.0)
    p.add_argument("--model", default=None)
    args = p.parse_args()

    wall = time.perf_counter()

    if args.title:
        print(f"resolving  {args.title!r} / {args.channel!r} …")
        t = tx.from_title_channel(args.title, args.channel)
        title, channel = args.title, args.channel
    elif args.url:
        vid = video_id(args.url)
        if not vid:
            print("could not parse a video ID from that", file=sys.stderr)
            return 2
        t = tx.fetch(vid)
        title, channel = args.url, ""
    else:
        p.print_help()
        return 2

    print(f"transcript {t.source:<8} {len(t.text):>5} chars  {t.seconds:.2f}s")
    if not t.text:
        print("\nno transcript — this is the ~25% case. Needs OCR, not ASR.")
        return 1
    print(f"           {t.text[:160]}…\n")

    r = factcheck.check(
        t.text, title, channel, deadline_s=args.deadline, model=args.model
    )

    if r.error:
        print(f"\033[31merror\033[0m      {r.error}")
    if r.verdict:
        v = r.verdict
        print(f"{BADGE.get(v.overall, v.overall)}  {v.headline}\n")
        for c in v.claims:
            print(f"  {RATING.get(c.rating, '?')} {c.claim}")
            print(f"    {c.note}")
            for url in c.sources:
                print(f"    \033[90m{url}\033[0m")
        if v.skipped:
            print(f"\n  \033[90mskipped {v.skipped} claim(s) as not worth the budget\033[0m")

    print(
        f"\n--- {r.seconds:.2f}s agent "
        f"({r.llm_calls} llm calls, {len(r.queries)} searches / {r.search_seconds:.2f}s) "
        f"| {time.perf_counter() - wall:.2f}s total"
    )
    for q in r.queries:
        print(f"    \033[90mq: {q}\033[0m")
    return 0 if r.verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
