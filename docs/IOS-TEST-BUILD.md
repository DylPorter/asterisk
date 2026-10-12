# iOS test build

The goal is one signed build on Alan's iPhone that answers three questions:
1. Does the broadcast extension hear TikTok, Reels and Shorts audio, and stay under its 50 MB cap?
2. Does a Back Tap reach the extension fast enough to feel instant?
3. Does Asterisk show up in Visual Intelligence when Alan screenshots a reel?

Everything that can be checked without a phone already has been:
- **Core logic** (`mobile/targets/_shared/`) is shared by every Xcode target and covered by `swift test`, which runs on Linux. Run it with `npm run test:core` (podman, `swift:6.2`).
- **Server:** pytest.
- **Swift compilation for all targets** is checked by `.github/workflows/ios.yml`, an unsigned `xcodebuild` on a free macOS runner (the repo is public).

## How it works
```
Start Asterisk (system broadcast picker, once per session)
        │
        ▼
broadcast extension (targets/broadcast/SampleHandler.swift)
   audioApp ─► Resampler ─► AudioRing (memory only, ≤120 s)
   video ─► caption-strip signature (≈150 numbers, no pixels kept) ─► SwipeDetector ─► ReelTracker (5 s dwell)
        ▲                                                                   │
        │ Darwin "check"                                                    ▼  prefetch on: chunks after dwell
Back Tap ─► Shortcut ─► CheckVideoIntent (targets/intents)          /v2/segments ─► /v2/check
        ◄─ last-result.json + Darwin "result" ◄─────────────────────────────┘
   verdict snippet over TikTok
```

## Privacy differences from Android
- **iOS can't limit capture to particular apps.** Android uses `addMatchingUid`; ReplayKit's `audioApp` carries every app's audio. So **prefetch defaults to off on iOS**: audio leaves the phone only when the user taps. The user can turn it on in the app. The server can't turn it on, and it can't lower the dwell below 3 s (`CaptureConfig.swift`, tested).
- **Frames are reduced to the caption-strip signature at once.** Only that signature crosses threads. No pixels are stored or uploaded.
- **Audio goes over HTTPS only.** The extension refuses to start with a non-https server.

## What Dylan needs to do
1. **Apple Developer account.** Use Alan's team if he has one, otherwise US$99/yr. Then add `ios.appleTeamId` to `mobile/app.json`.
2. **Server over HTTPS.** For a test run, `cloudflared tunnel --url http://localhost:8000` gives a temporary https URL. Put API keys in `server/.env` straight from the clipboard (`wl-paste` into the file), never echoed into a terminal.
3. **Signed development build.** From `mobile/`: `npx eas-cli@latest build -p ios --profile development`. EAS asks to register Alan's iPhone UDID through a link he opens on the phone. It also creates the App Group and the three bundle IDs (`dev.tdporter.asterisk`, `.broadcast`, `.intents`).
4. **On Alan's phone:**
   - Install the build.
   - Open Asterisk, enter the server URL and tap Start.
   - Settings → Accessibility → Touch → Back Tap → Double Tap → "Check video". If it isn't listed, make a one-action shortcut in the Shortcuts app.
5. **Run `npx expo start --dev-client` on the laptop.** JS changes then reach the phone without a rebuild. Native Swift changes need a new EAS build.

## Alan's 10-minute script
1. Start Asterisk. Scroll TikTok for 3 minutes, Reels for 3 and Shorts for 3. Stay on some videos for over 10 s and swipe past others quickly.
2. Double Back Tap on about 5 videos he's stayed on for 10 s or more. Do it once right after a swipe too, which should answer "Give it a few more seconds".
3. Screenshot one reel, open Visual Intelligence on the screenshot, and check whether "Fact-check this video" appears.

## Reading the results
Everything is in `server/out/diag.jsonl`, one JSON line per event. Here's what to check, then what to change if it's wrong:

| Question | Events | Fix without a rebuild |
|---|---|---|
| Hears app audio? | `audio_format` (once); `heartbeat.audio_bufs`/`samples16k` rising; `segment.db` well above −55 | none (no audio means the approach fails) |
| Under memory cap? | `heartbeat.mem_avail_mb` over the session | lower `ringSeconds` |
| Swipes detected? | `reel` events vs. what Alan did; `strip.d` values | tune `strip`, `changeThreshold`, `stableFrames` in `server/ios_config.json` |
| Tap latency | `check_signal` → `check_on_queue.lag_ms`; `intent_start` → `intent_done.ms` | — |
| Visual Intelligence | `vi_query` present? `labels`, `has_pixels` | build the screenshot path if yes |

`server/ios_config.json` is read on every broadcast start. Any field of `CaptureConfig` can go in it. Missing fields keep their defaults.

## Known gaps
- **App Intents deployment target is stuck at 17.0.** `@bacons/apple-targets` hardcodes it for app-intent targets, so the Visual Intelligence query is gated with `@available(iOS 26, *)`.
- **The swipe detector is a heuristic.** It's tested on synthetic frames only. Expanding a caption ("…more") will probably read as a swipe, and the first device run is where its thresholds get set.
- **The app can't tell which app is playing.** `app=ios` is sent, so the server caches by transcript only.
