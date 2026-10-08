# v2 eval

The gate before the Play closed test. It runs real reel audio through the same
`asr` → `factcheck` → `grounding` modules the server uses, and it measures
whether v2 meets the bar in `docs/V2-ARCHITECTURE.md`.

| Metric | Bar | How it is measured |
|---|---|---|
| Transcript captured | ≥ 95% | The share of videos where `asr.transcribe` returned non-empty text across the 5 s chunks. |
| Grounded claims | 100% | After `factcheck.check` (which already calls `grounding.ground`), `run_eval` grounds the verdict again on its own. Any claim that this second pass drops was *leaked*: it would have reached the card without a matching quote. Leaked must be 0. |
| Verdicts correct | ≥ 80% | Graded by hand in `out/report.md`, then tallied with `--score`. |

The report also lists per-stage latency (median, p90, max) and an estimated
tap→verdict time. That estimate is the slowest single ASR chunk plus the fact-check
plus grounding, because by the time the user taps, the prefetch has already transcribed
everything except the last partial chunk.

## 1. Assemble the 30-video set

Use **10 YouTube Shorts, 10 Instagram Reels and 10 TikToks**. Within each app,
aim for a mix of about **4 news, 3 health and 3 finance**. These are the
categories where a wrong verdict does real harm and where claims are checkable.
Pick them with the bar in mind:

- **Real feed content, not hand-picked easy cases.** Take them from your own
  scroll. The set should include fast talkers, music beds, and a few videos
  where the claim is only partly spoken.
- **Know the answer before you run.** Write `expected` for each row (what is
  true, and what verdict you would accept) *before* you see the output. That
  keeps the hand grade honest.
- **Spread the verdicts.** Include some clearly false claims, some accurate
  ones and some mixed ones. A set that is all misinformation can't catch a
  model that marks everything misleading.
- **Include 1–2 videos with no speech** (music over text) per app. They should
  end up uncaptured or "nothing checkable". This tests the empty-transcript
  path. Note that they count against the capture rate, so leave them out
  of the 30 if you're grading capture strictly, or note them in `expected`.

Fetch the audio (dev-only, needs `yt-dlp` and `ffmpeg`):

```sh
cd server
# urls.txt: one URL per line; optionally TAB + expected notes; # comments ok
python eval/fetch_samples.py urls.txt --topic news
python eval/fetch_samples.py --url https://www.instagram.com/reel/XXXX/ \
    --topic health --expected "should be misleading: ..."
```

This writes 16 kHz mono WAV (the format `CaptureService` uploads) to
`eval/samples/` and merges a row per URL into `eval/manifest.json`. Running it
again is safe, and notes you have already written are kept. Instagram and some
TikToks need a logged-in cookie file (`--cookies path`). Don't commit it.

**Samples are other people's content.** `samples/` is gitignored. Keep the
files local and use them only for this eval.

You can also record the audio yourself (for example, playback captured on a
real device) and add the rows by hand. See `manifest.example.json`:

| Field | Required | Meaning |
|---|---|---|
| `file` | yes | The audio path, relative to the manifest. Any format ffmpeg reads; it is converted to 16 kHz mono WAV. |
| `app` | yes | `youtube`, `instagram` or `tiktok`, the same names the client sends. |
| `expected` | yes, for grading | What is true and which verdict is acceptable. Write it before running. |
| `title`, `channel` | no | The hints accessibility would pass. Use `--no-hints` to drop them. |
| `url`, `topic` | no | Provenance and category, shown in the report. |

## 2. Run

Needs the ASR, LLM and search keys in `server/.env` (see `.env.example`).
Nothing in `eval/` reads or prints them.

```sh
cd server
python eval/run_eval.py                      # all rows → eval/out/report.md + results.json
python eval/run_eval.py --app tiktok --limit 3   # smoke test
python eval/run_eval.py --no-hints           # as if accessibility saw nothing
python eval/run_eval.py --chunk-s 0          # one ASR call per file, for comparison
```

By default the audio is cut into 5 s chunks, capped at 120 s like the device
ring buffer, and the chunks are transcribed in parallel. This copies how
`sessions.py` prefetches. Words split across a chunk boundary are part of what
is being measured.

## 3. Grade

Open `eval/out/report.md`. For each row in **Hand grading**, compare the
verdict and claims with your `expected` and fill in **Correct**:

- `y` if the overall verdict is right *and* no claim on the card is wrong
  about the world.
- `n` if the overall verdict is wrong, a claim's rating is wrong, or the card
  checks something the video did not say.
- `-` to exclude a row that has no verdict (that failure is already counted under
  capture or errors).

```sh
python eval/run_eval.py --score eval/out/report.md   # prints the rate and PASS/FAIL against 80%
```

Each rerun overwrites `report.md`. To keep the grades from a run, copy the file first.
