"""Offline eval for the v2 pipeline: local audio -> asr -> factcheck -> grounding.

    cd server
    python eval/run_eval.py                          # eval/manifest.json -> eval/out/
    python eval/run_eval.py --limit 3 --app youtube  # quick smoke
    python eval/run_eval.py --score eval/out/report.md   # tally hand grades

This is the gate before the Play closed test (docs/V2-ARCHITECTURE.md):
on 30 real videos, transcript captured >= 95%, grounded claims = 100%,
verdicts judged correct by hand >= 80%. The first two are measured here; the
third needs a human, so the report carries a blank column for it and --score
reads it back.

Needs real ASR + LLM + search keys in server/.env to produce verdicts. Nothing
here prints or reads them; the server modules do.
"""

from __future__ import annotations

import argparse
import array
import copy
import io
import json
import math
import re
import shutil
import statistics
import subprocess
import sys
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent
# The server modules are flat files in server/, not a package. Put that on the
# path so this runs from any cwd, and so `import asr` resolves to the same
# module the API process uses rather than anything on site-packages.
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

SAMPLE_RATE = 16_000        # What CaptureService records: 16 kHz mono PCM16.
CHUNK_S = 5.0               # Uploader posts one segment per 5 s of dwell.
MAX_AUDIO_S = 120.0         # CaptureService ring buffer cap; nothing older reaches the server.
SILENCE_DBFS = -55.0        # Below this on every chunk the session is `captured_silent`.
ASR_WORKERS = 4             # sessions.py transcribes segments on a pool; mirror the overlap.

BAR_CAPTURE = 0.95
BAR_GROUNDED = 1.0
BAR_CORRECT = 0.80


# --- Audio -----------------------------------------------------------------

def _is_device_wav(data: bytes) -> bool:
    """True when the bytes are already what the phone uploads (16 kHz mono PCM16)."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            return (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SAMPLE_RATE, 1, 2)
    except (wave.Error, EOFError):
        return False


def to_wav(path: Path, max_s: float = MAX_AUDIO_S) -> bytes:
    """Return `path` as 16 kHz mono PCM16 WAV, the exact format the device sends.

    Sample sets arrive as m4a/webm/mp3 from yt-dlp. Transcoding to the device's
    format matters for honesty: an ASR that does well on 44.1 kHz stereo AAC
    has not shown it does well on what CaptureService actually uploads.
    """
    data = path.read_bytes()
    if _is_device_wav(data):
        return _trim(data, max_s)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(f"{path.name} is not 16 kHz mono WAV and ffmpeg is not installed")
    proc = subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-t", str(max_s), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
         "-acodec", "pcm_s16le", "-f", "wav", "pipe:1"],
        capture_output=True, timeout=120,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"ffmpeg failed on {path.name}: {proc.stderr.decode(errors='replace')[-300:]}")
    # ffmpeg writing to a pipe can't seek back to fill in the RIFF sizes, so the
    # header says 0xFFFFFFFF. Re-wrap the PCM so `wave` and every ASR provider
    # see a well-formed file, same as the 44-byte header the device writes.
    return _wav_from_pcm(_pcm_after_data_chunk(proc.stdout))


def _pcm_after_data_chunk(raw: bytes) -> bytes:
    """Walk the RIFF chunks to `data` rather than searching for the bytes: ffmpeg
    puts a LIST/INFO chunk first, and its text could contain "data"."""
    i = 12
    while i + 8 <= len(raw):
        cid, size = raw[i:i + 4], int.from_bytes(raw[i + 4:i + 8], "little")
        if cid == b"data":
            pcm = raw[i + 8:]   # size is a placeholder on a pipe; take the rest
            return pcm[: len(pcm) - len(pcm) % 2]
        i += 8 + size + (size & 1)
    raise RuntimeError("ffmpeg output has no WAV data chunk")


def _wav_from_pcm(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def _pcm(wav_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        return w.readframes(w.getnframes())


def _trim(wav_bytes: bytes, max_s: float) -> bytes:
    pcm = _pcm(wav_bytes)
    limit = int(max_s * SAMPLE_RATE) * 2
    return wav_bytes if len(pcm) <= limit else _wav_from_pcm(pcm[:limit])


def chunks(wav_bytes: bytes, chunk_s: float) -> list[bytes]:
    """Split into the WAV segments Uploader would post. chunk_s <= 0 means one."""
    if chunk_s <= 0:
        return [wav_bytes]
    pcm = _pcm(wav_bytes)
    step = int(chunk_s * SAMPLE_RATE) * 2
    return [_wav_from_pcm(pcm[i:i + step]) for i in range(0, len(pcm), step)] or [wav_bytes]


def rms_dbfs(wav_bytes: bytes) -> float:
    """RMS level in dBFS, same measure CaptureService.rmsDb uses for the silence gate."""
    samples = array.array("h")
    samples.frombytes(_pcm(wav_bytes))
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return -math.inf
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    return 20 * math.log10(rms / 32768) if rms > 0 else -math.inf


def duration_s(wav_bytes: bytes) -> float:
    return len(_pcm(wav_bytes)) / 2 / SAMPLE_RATE


# --- One video ---------------------------------------------------------------

@dataclass
class Row:
    idx: int
    file: str
    app: str
    expected: str = ""
    url: str = ""
    topic: str = ""
    audio_s: float = 0.0
    chunks: int = 0
    silent: bool = False
    rms_db: float = 0.0
    transcript: str = ""
    captured: bool = False
    overall: str = ""
    score: int | None = None
    headline: str = ""
    claims: list[dict[str, Any]] = field(default_factory=list)
    claims_emitted: int = 0     # what the model returned, before any grounding
    dropped_in_check: int = 0   # removed by factcheck's own grounding.ground call
    leaked_ungrounded: int = 0  # survived factcheck but failed our re-ground: a bug
    skipped: int = 0
    queries: list[str] = field(default_factory=list)
    error: str = ""
    # Seconds per stage. `asr_tail_s` is the slowest single chunk: after a tap
    # only the final partial chunk is still transcribing, so this approximates
    # the ASR the user actually waits on, where `asr_wall_s` is the prefetch.
    convert_s: float = 0.0
    asr_wall_s: float = 0.0
    asr_tail_s: float = 0.0
    factcheck_s: float = 0.0
    grounding_s: float = 0.0

    @property
    def tap_to_verdict_s(self) -> float:
        return self.asr_tail_s + self.factcheck_s + self.grounding_s


def _timed_transcribe(asr: Any, wav: bytes) -> tuple[str, float, str]:
    """(text, seconds, error). One bad chunk must not sink the video."""
    t0 = time.perf_counter()
    try:
        text, err = asr.transcribe(wav) or "", ""
    except Exception as e:
        text, err = "", f"{type(e).__name__}: {e}"
    return text, time.perf_counter() - t0, err


def _claim_dict(c: Any) -> dict[str, Any]:
    if hasattr(c, "model_dump"):
        return c.model_dump()
    return dict(c) if isinstance(c, dict) else {"claim": str(c)}


def run_one(idx: int, item: dict[str, Any], base: Path, mods: dict[str, Any],
            args: argparse.Namespace) -> Row:
    asr, factcheck, grounding = mods["asr"], mods["factcheck"], mods["grounding"]
    path = (base / item["file"]).resolve()
    row = Row(idx=idx, file=item["file"], app=item.get("app", ""),
              expected=item.get("expected", ""), url=item.get("url", ""),
              topic=item.get("topic", ""))

    t0 = time.perf_counter()
    try:
        wav = to_wav(path)
    except Exception as e:
        row.error = f"audio: {e}"
        return row
    row.convert_s = time.perf_counter() - t0
    row.audio_s = round(duration_s(wav), 1)

    segs = chunks(wav, args.chunk_s)
    row.chunks = len(segs)
    levels = [rms_dbfs(s) for s in segs]
    row.rms_db = round(max(levels), 1) if levels else -math.inf
    # Same rule as the device: silent only if EVERY chunk is under the floor.
    # One quiet intro chunk is normal; a whole session under it means the app
    # opted out of playback capture.
    row.silent = all(lv < SILENCE_DBFS for lv in levels)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=ASR_WORKERS) as pool:
        results = list(pool.map(lambda s: _timed_transcribe(asr, s), segs))
    row.asr_wall_s = time.perf_counter() - t0
    row.asr_tail_s = max((dt for _, dt, _ in results), default=0.0)
    errs = [e for _, _, e in results if e]
    row.transcript = " ".join(t.strip() for t, _, _ in results if t.strip())
    row.captured = bool(row.transcript)
    if errs:
        row.error = f"asr ({len(errs)}/{len(segs)} chunks): {errs[0]}"
    if not row.captured:
        row.error = row.error or ("captured_silent" if row.silent else "empty transcript")
        return row

    title = "" if args.no_hints else item.get("title", "")
    channel = "" if args.no_hints else item.get("channel", "")
    t0 = time.perf_counter()
    try:
        res = factcheck.check(row.transcript, title, channel,
                              deadline_s=args.deadline, model=args.model)
    except Exception as e:  # check() promises not to raise; record it if it does
        row.error = f"factcheck raised {type(e).__name__}: {e}"
        return row
    row.factcheck_s = time.perf_counter() - t0
    row.queries = list(getattr(res, "queries", []) or [])
    v = getattr(res, "verdict", None)
    if getattr(res, "error", None):
        row.error = f"factcheck: {res.error}"
    if v is None:
        return row

    row.dropped_in_check = int(getattr(v, "ungrounded_dropped", 0) or 0)
    kept_by_check = len(v.claims)
    row.claims_emitted = kept_by_check + row.dropped_in_check

    # Re-ground independently. factcheck already calls grounding.ground, so on
    # a correct build this drops nothing. Anything it does drop is a claim that
    # would have reached the card without a matching quote -- the one failure
    # the 100% grounded bar exists to catch. Deep copy so a ground() that
    # mutates in place can't erase factcheck's own drop count.
    t0 = time.perf_counter()
    try:
        g = grounding.ground(copy.deepcopy(v), row.transcript)
    except Exception as e:
        row.error = f"grounding raised {type(e).__name__}: {e}"
        g = v
    row.grounding_s = time.perf_counter() - t0
    row.leaked_ungrounded = max(kept_by_check - len(g.claims), 0)

    row.overall = getattr(v, "overall", "")
    row.score = getattr(v, "score", None)
    row.headline = getattr(v, "headline", "")
    row.skipped = int(getattr(v, "skipped", 0) or 0)
    row.claims = [_claim_dict(c) for c in v.claims]
    return row


# --- Report ------------------------------------------------------------------

def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}% ({n}/{d})" if d else "n/a"


def _md(s: Any) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _stat(xs: list[float]) -> str:
    xs = sorted(x for x in xs if x > 0)
    if not xs:
        return "n/a"
    p90 = xs[min(len(xs) - 1, math.ceil(0.9 * len(xs)) - 1)]
    return f"{statistics.median(xs):.2f} / {p90:.2f} / {xs[-1]:.2f}"


def summarise(rows: list[Row]) -> dict[str, Any]:
    n = len(rows)
    captured = sum(r.captured for r in rows)
    kept = sum(len(r.claims) for r in rows)
    leaked = sum(r.leaked_ungrounded for r in rows)
    return {
        "videos": n,
        "captured": captured,
        "capture_rate": captured / n if n else 0.0,
        "verdicts": sum(bool(r.overall) for r in rows),
        "claims_kept": kept,
        "claims_emitted": sum(r.claims_emitted for r in rows),
        "dropped_in_check": sum(r.dropped_in_check for r in rows),
        "leaked_ungrounded": leaked,
        "grounded_rate": (kept - leaked) / kept if kept else 1.0,
    }


def write_report(rows: list[Row], out: Path, args: argparse.Namespace) -> Path:
    s = summarise(rows)
    ok = lambda good: "PASS" if good else "FAIL"  # noqa: E731
    lines: list[str] = []
    add = lines.append

    add("# Asterisk v2 eval\n")
    add(f"{time.strftime('%Y-%m-%d %H:%M')} · manifest `{args.manifest}` · "
        f"chunk {args.chunk_s:g}s · deadline {args.deadline:g}s · "
        f"model `{args.model or 'default'}` · hints {'off' if args.no_hints else 'on'}\n")

    add("## Quality bar\n")
    add("| Metric | Result | Bar | |")
    add("|---|---|---|---|")
    add(f"| Transcript captured | {_pct(s['captured'], s['videos'])} | ≥ 95% | {ok(s['capture_rate'] >= BAR_CAPTURE)} |")
    add(f"| Grounded claims | {_pct(s['claims_kept'] - s['leaked_ungrounded'], s['claims_kept'])} | 100% | "
        f"{ok(s['grounded_rate'] >= BAR_GROUNDED)} |")
    add("| Verdicts correct (hand) | fill the Correct column, then `run_eval.py --score` | ≥ 80% | — |")
    if len(rows) < 30:
        add(f"\n> {len(rows)} videos, not 30. The bar is defined on 10 Shorts + 10 Reels + 10 TikTok.")
    add(f"\nThe model emitted {s['claims_emitted']} claims; factcheck's grounding dropped "
        f"{s['dropped_in_check']} as unquotable. That is the fabrication rate the code "
        f"absorbs. A nonzero *leaked* count means a claim got past it.\n")

    add("## By app\n")
    add("| App | Videos | Captured | Verdicts | Claims kept | Leaked |")
    add("|---|---|---|---|---|---|")
    for app in sorted({r.app for r in rows}):
        sub = summarise([r for r in rows if r.app == app])
        add(f"| {app or '?'} | {sub['videos']} | {_pct(sub['captured'], sub['videos'])} | "
            f"{sub['verdicts']} | {sub['claims_kept']} | {sub['leaked_ungrounded']} |")

    add("\n## Latency (s): median / p90 / max\n")
    add("| Stage | |")
    add("|---|---|")
    for name, attr in (("convert", "convert_s"), ("asr prefetch (wall)", "asr_wall_s"),
                       ("asr tail (slowest chunk)", "asr_tail_s"), ("factcheck", "factcheck_s"),
                       ("grounding", "grounding_s"), ("tap → verdict (est.)", "tap_to_verdict_s")):
        add(f"| {name} | {_stat([getattr(r, attr) for r in rows])} |")
    add("\nTap → verdict assumes the prefetch finished before the tap, so the user waits on "
        "one chunk of ASR plus the fact-check.\n")

    add("## Hand grading\n")
    add("Put `y` or `n` in **Correct** (is the verdict right about this video?), "
        "`-` to exclude a row with no verdict. Then run `python eval/run_eval.py --score <this file>`.\n")
    add("| # | App | File | Expected | Verdict | Headline | Claims | Leaked | Tap s | Correct |")
    add("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        verdict = f"{r.overall} {r.score}" if r.overall else f"— {_md(r.error)[:40]}"
        add(f"| {r.idx} | {r.app} | {_md(r.file)} | {_md(r.expected)} | {verdict} | "
            f"{_md(r.headline)} | {len(r.claims)} | {r.leaked_ungrounded} | "
            f"{r.tap_to_verdict_s:.1f} |  |")

    add("\n## Detail\n")
    for r in rows:
        add(f"### {r.idx}. {r.file}\n")
        meta = [r.app]
        if r.chunks:
            meta += [f"{r.audio_s}s", f"{r.chunks} chunks", f"loudest chunk {r.rms_db} dBFS"]
        if r.topic:
            meta.append(r.topic)
        if r.url:
            meta.append(r.url)
        add("- " + " · ".join(meta))
        if r.expected:
            add(f"- **Expected:** {r.expected}")
        if r.error:
            add(f"- **Error:** `{_md(r.error)}`")
        if r.overall:
            add(f"- **Verdict:** {r.overall} ({r.score}/5) — {r.headline} · skipped {r.skipped}")
        for q in r.queries:
            add(f"  - search: {q}")
        for c in r.claims:
            note = f" — {c['note']}" if c.get("note") else ""
            add(f"  - **{c.get('rating', '?')}** {c.get('claim', '')}{note}")
            if c.get("quote"):
                add(f"    - quote: “{_md(c['quote'])}”")
            for src in c.get("sources") or []:
                add(f"    - {src}")
        if r.transcript:
            excerpt = r.transcript[:600] + ("…" if len(r.transcript) > 600 else "")
            add(f"\n<details><summary>Transcript ({len(r.transcript)} chars)</summary>\n\n{excerpt}\n\n</details>")
        add("")

    out.mkdir(parents=True, exist_ok=True)
    report = out / "report.md"
    report.write_text("\n".join(lines) + "\n")
    payload = {"summary": s, "rows": [asdict(r) | {"tap_to_verdict_s": r.tap_to_verdict_s} for r in rows]}
    (out / "results.json").write_text(json.dumps(payload, indent=2, default=str))
    return report


def score(report: Path) -> int:
    """Tally the hand-graded Correct column and check it against the 80% bar."""
    yes = no = 0
    in_table = False
    for line in report.read_text().splitlines():
        if line.startswith("| # |"):
            in_table = True
            continue
        if in_table and not line.startswith("|"):
            break
        if not in_table or line.startswith("|---"):
            continue
        cell = line.rstrip().rstrip("|").rsplit("|", 1)[-1].strip().lower()
        if cell in ("y", "yes", "✓", "1"):
            yes += 1
        elif cell in ("n", "no", "✗", "0"):
            no += 1
    graded = yes + no
    if not graded:
        print("no rows graded yet: put y/n in the Correct column")
        return 1
    rate = yes / graded
    print(f"correct {_pct(yes, graded)}  bar ≥ 80%  {'PASS' if rate >= BAR_CORRECT else 'FAIL'}")
    return 0 if rate >= BAR_CORRECT else 1


# --- CLI -----------------------------------------------------------------------

def load_manifest(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    items = data.get("items", []) if isinstance(data, dict) else data
    if not isinstance(items, list) or not all(isinstance(i, dict) and "file" in i for i in items):
        raise SystemExit(f"{path}: expected a list of objects with at least a 'file' key")
    return items


def load_modules() -> dict[str, Any]:
    # Imported late so --score and --help work without the server deps installed.
    import asr
    import factcheck
    import grounding
    return {"asr": asr, "factcheck": factcheck, "grounding": grounding}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--manifest", type=Path, default=HERE / "manifest.json")
    p.add_argument("--out", type=Path, default=HERE / "out")
    p.add_argument("--chunk-s", type=float, default=CHUNK_S,
                   help="split audio into device-sized segments (0 = whole file in one ASR call)")
    p.add_argument("--deadline", type=float, default=10.0, help="factcheck deadline_s")
    p.add_argument("--model", default=None, help="factcheck model override")
    p.add_argument("--no-hints", action="store_true",
                   help="pass empty title/channel, as for a Reel with no accessibility text")
    p.add_argument("--app", help="only run items for this app")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--score", type=Path, metavar="REPORT", help="tally hand grades in a report and exit")
    args = p.parse_args(argv)

    if args.score:
        return score(args.score)

    if not args.manifest.exists():
        print(f"no manifest at {args.manifest}; copy eval/manifest.example.json (see eval/README.md)",
              file=sys.stderr)
        return 2
    items = load_manifest(args.manifest)
    if args.app:
        items = [i for i in items if i.get("app") == args.app]
    if args.limit:
        items = items[: args.limit]

    mods = load_modules()
    base = args.manifest.resolve().parent
    rows: list[Row] = []
    for n, item in enumerate(items, 1):
        row = run_one(n, item, base, mods, args)
        rows.append(row)
        status = (f"{row.overall} {row.score}" if row.overall
                  else ("captured" if row.captured else "no transcript"))
        print(f"[{n:>2}/{len(items)}] {row.app:<9} {row.file[:40]:<40} {status:<14} "
              f"tap {row.tap_to_verdict_s:5.1f}s  {row.error[:60]}")

    report = write_report(rows, args.out, args)
    s = summarise(rows)
    print(f"\ncaptured {_pct(s['captured'], s['videos'])} · grounded "
          f"{_pct(s['claims_kept'] - s['leaked_ungrounded'], s['claims_kept'])} · report {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
