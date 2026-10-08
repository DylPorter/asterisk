# Asterisk v2: tap-to-check with on-device capture

v1 found out which video was playing by searching for its title. That caused both of its quality failures:
- **The wrong video, about half the time.** `transcript.resolve_video_id` matches a title and channel handle against a channel search.
- **Only ~30% of Shorts covered.** It depends on captions, and yt-dlp is IP-blocked.

v2 stops searching for the video. It captures the audio that is actually playing, on the device, and checks that. A wrong-video result becomes impossible by construction.

## User flow
1. The user starts Asterisk once per session (Start → Android screen-capture consent). A foreground notification stays visible while it runs.
2. The user scrolls YouTube Shorts, TikTok or Instagram Reels as normal.
3. While a reel is on screen, its audio is buffered **locally in memory only**. Swiping away before **5 s** throws the buffer away and uploads nothing.
4. At 5 s of dwell, the client uploads the buffered audio, then a new chunk every 5 s. The server transcribes each chunk as it arrives (the prefetch).
5. A small ✱ button appears. When the user taps it, the client uploads the last partial chunk and calls `/v2/check`. The transcript is already done, so only the fact-check runs.
6. The verdict card opens (claims, verdicts, sources, coverage).

The always-on badge from v1 is removed. Nothing is checked unless the user taps.

## Privacy constraints (non-negotiable)
- Capture only the audio of the three target apps. Use `AudioPlaybackCaptureConfiguration.addMatchingUid` with the UIDs of `com.google.android.youtube`, `com.zhiliaoapp.musically` / `com.ss.android.ugc.trill`, and `com.instagram.android`, resolved through `PackageManager`.
- Never use the microphone. Never capture screen pixels. MediaProjection is used only because playback capture requires it.
- `AudioRecord` runs **only while a reel is on screen** in a target app, according to the accessibility service. It stops otherwise.
- Audio from before the dwell gate never leaves the device. The server does not save audio to disk. It keeps transcripts in memory for 10 min per session, then drops them.

## Android (`android/app/src/main/java/dev/tdporter/shortcheck/`)
| File | Role |
|---|---|
| `CaptureService.kt` (new) | A foreground service with type `mediaProjection`. It owns the `MediaProjection` and an `AudioRecord` (16 kHz, mono, PCM16) built with playback capture matching the target UIDs. It keeps an in-memory **ring buffer, max 120 s**, starting from `beginReel(key)`, and exposes `beginReel(key)`, `endReel()`, `drainSince(offsetSamples): ShortArray` and `rmsDb(samples)`. Encoding is WAV (44-byte header plus PCM16). |
| `ReelTracker.kt` (new) | **Pure Kotlin with no Android imports**, so it can be unit tested. It is a dwell state machine: `onReel(key, nowMs)`, `onNoReel(nowMs)`, `tick(nowMs)`. It emits `Started(key)`, `DwellReached(key)` at ≥5000 ms, `ChunkDue(key, seq)` every 5000 ms after that, and `Ended(key)`. A key change (a swipe) ends the current reel. |
| `ShortsAccessibilityService.kt` | Keeps the existing YouTube and TikTok detection and adds Instagram Reels (best effort, see below). It now produces only a **reel key** (`app|channel|title`, or an app-specific fallback) for `ReelTracker`. It does no network checks on its own. |
| `Uploader.kt` (new) | On `DwellReached` and `ChunkDue`, it drains the new samples since the last upload, encodes WAV, and POSTs to `/v2/segments`. On tap, it uploads the last partial chunk and calls `/v2/check`. `session_id` is a new UUID for each reel view. |
| `Overlay.kt` | The always-on badge becomes: hidden → ✱ button (after dwell) → "checking…" → verdict card. The existing card rendering is reused. |
| `Api.kt` | Adds the v2 calls. The v1 `/check` stays as the YouTube fallback when the captured audio is silent (an app opted out of capture). |
| `MainActivity.kt` | Adds Start/Stop capture (projection consent via `MediaProjectionManager.createScreenCaptureIntent()`) and status for each permission: overlay, accessibility, capture. |
| Manifest | Adds `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_MEDIA_PROJECTION`, `POST_NOTIFICATIONS`, the `CaptureService` declaration with `foregroundServiceType="mediaProjection"`, and an accessibility-config update to watch the Instagram and TikTok packages. |

**Silence fallback:** if every uploaded chunk is below about -55 dBFS RMS, the app is assumed to block capture. Mark the session `captured_silent`. On tap, YouTube falls back to the v1 path. Other apps return "can't check this video".

**Instagram detection has NOT been verified on a device.** Detect the `com.instagram.android` foreground window plus a full-screen video/reel container through structural heuristics. Derive the key from the author handle and caption content descriptions. Log the view tree under a debug flag so the heuristics can be fixed on a real phone.

## Server (`server/`)
| File | Role |
|---|---|
| `asr.py` (new) | Exposes `transcribe(wav_bytes) -> str`. The provider is pluggable. The default is Gemini (`GEMINI_API_KEY`) through its native audio input, called over REST with `httpx` or `urllib` (no new SDK if avoidable). Groq or OpenAI Whisper is optional and selected by env var. It returns `""` for silence or music-only audio. |
| `sessions.py` (new) | An in-memory store: `session_id → {app, hints, segments{seq: text\|pending\|silent}, created}`. `add_segment` starts transcription in a background thread pool. `transcript(session_id, final_seq, timeout)` waits for every segment up to and including `final_seq`, then joins them in order. A TTL sweep drops sessions after 10 min. |
| `grounding.py` (new) | Exposes `ground(verdict, transcript) -> verdict`. Each claim must carry a `quote`. The quote is normalised (case, punctuation, whitespace) and must fuzzy-match the transcript, using difflib `SequenceMatcher` against sliding windows. Claims that don't match are **dropped and counted** in `ungrounded_dropped`. This is enforced in code, not in the prompt. |
| `factcheck.py` | Restores a required short `quote` field on each claim (verbatim from the transcript, ≤ 25 words). Calls `grounding.ground` before returning. Keeps the existing coverage logic. |
| `server.py` | Adds `POST /v2/segments` (query: `session_id`, `seq`, `app`; body: `audio/wav`) and `POST /v2/check` (JSON: `session_id`, `app`, `titles`, `channel`, `final_seq`, `deadline_s`). The cache key is `app\|channel\|normalised title` when hints exist, otherwise a hash of the transcript. A silent session on `app=youtube` falls back to the v1 `_pipeline`. `/check` and `/health` are kept. |
| `eval/` (new) | `run_eval.py` runs local audio files from a manifest (`eval/manifest.json`: file, app, expected notes) through `asr` → `factcheck` → `grounding`. It reports transcript-capture rate, grounded-claim rate and latency, and writes a markdown report for hand-grading verdicts. `fetch_samples.py` is a dev-only helper that uses yt-dlp to pull the audio of a list of URLs for the eval set. |
| `tests/` (new) | pytest covering grounding (match, near-match, fabricated quote dropped), session ordering and timeouts, WAV and silence handling, the endpoint flow with ASR and the LLM mocked, and the silent→v1 fallback. |

## Quality bar before the Play closed test
On 30 real videos (10 each from Shorts, Reels and TikTok):
- transcript captured ≥ 95%
- grounded claims = 100% (code-enforced)
- verdicts judged correct by hand ≥ 80%

## Out of scope for v2
iOS, the shared cross-user cache service, audio fingerprinting, share cards, Play Store listing assets.
