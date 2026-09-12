# Asterisk

Fact-checks YouTube Shorts while you watch them, as a floating badge over the
YouTube app. Built at AI Tinkerers Hong Kong, "Agents Everywhere".

An agent that lives in a place people already spend time — not a chatbox you
have to go to.

## How it works

1. **Accessibility service** reads the on-screen view tree and pulls two
   things: the video's title and the channel handle. No screenshots, no audio,
   no screen recording — only text that is already public.
2. **Resolver** turns title + handle into the actual video, then fetches that
   video's captions.
3. **Agent** (Gemini Flash + Exa search) reads the transcript, decides *which*
   claims are worth checking, spends a budget of three searches, and returns a
   structured verdict with sources.
4. **Badge** shows a 1–5 score over the video. Tap to expand: each claim, what
   the evidence said, and links.

The agentic part is the triage. It has seconds and three searches, so it has to
rank claims by whether being wrong would change how a viewer acts — and it
reports what it skipped.

## What it does not do

- Score a video it could not identify — it says so instead
- Score a video whose claims it could not check — opinion and motivational
  content returns "nothing factual to check"
- Certify a video it only partly examined — the percentage is hidden when
  coverage is low, and the claim counts tell the real story

Those are deliberate. A confidently wrong badge is worse than an honest blank.

## Layout

```
server/    FastAPI backend — resolution, captions, the agent
android/   The app — accessibility service + overlay
```

## Running it

```bash
cd server
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env        # add OPENROUTER_API_KEY / GEMINI_API_KEY / EXA_API_KEY
./.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
```

Build the app with `./gradlew :app:assembleDebug`, set `SERVER` in
`android/app/build.gradle.kts` to wherever the backend is reachable, then grant
the overlay and accessibility permissions on first launch.

YouTube rate-limits unauthenticated caption fetches at volume; export a
`cookies.txt` into `server/` if you hit it.

## Known limits

- ~25% of Shorts have no captions at all and cannot be checked
- The agent chooses its own searches, so the same video can yield different
  findings across runs
- TikTok support exists but resolves only videos indexed by search engines
- Instagram is not supported: Reels expose no retrievable caption track
