"""In-memory store of per-reel-view transcripts, filled ahead of the tap.

The client uploads a 5 s chunk as soon as a reel passes the dwell gate and
another every 5 s after. Each chunk is transcribed in the background as it
arrives, so by the time the user taps the transcript is already done and only
the fact-check is left on the critical path. That prefetch is the whole point
of this module.

Privacy: audio bytes are handed to the transcriber and never stored -- a
segment keeps only its text. Sessions, and with them every transcript, are
dropped TTL_S after creation. Nothing here touches disk.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

import asr

PENDING = "pending"
SILENT = "silent"
FAILED = "failed"
DONE = "done"

TTL_S = 600.0  # 10 min, per the privacy constraints in docs/V2-ARCHITECTURE.md

# Bounds on what one client -- buggy, looping, or anyone on the tailnet -- can
# make the server hold or spend. None of these is measured against real
# traffic; they are sized from the client's own behaviour so a well-behaved
# phone never reaches them.
#
# Highest seq accepted. The client sends one chunk per 5 s after the dwell
# (ReelTracker.CHUNK_MS), so 40 is ~200 s on one reel -- past the 120 s ring
# buffer and past any Short. A reel left looping longer has already been
# heard in full; later chunks are refused, and /v2/check uses what came before.
MAX_SEQ = 40
# Chunks waiting for, or in, a transcription call, across all sessions. Each
# holds its WAV bytes (~160 KB for 5 s, server cap 4 MB) until its call
# returns, and each is a paid ASR call. 4 workers x 4 queued keeps the worst
# case at 64 MB instead of whatever an unbounded executor queue grows to.
MAX_PENDING = 16
# Live sessions. One per reel view, alive for TTL_S; a phone scrolling one
# reel every 6 s for the whole TTL makes ~100. Text only, so cheap, but an
# unbounded dict keyed by client-chosen ids is still unbounded.
MAX_SESSIONS = 500


class Busy(Exception):
    """A cap above is full. The endpoint maps this to 503; the client retries."""


@dataclass
class Segment:
    state: str
    text: str = ""
    error: str | None = None


@dataclass
class Session:
    app: str
    created: float
    hints: dict[str, Any] = field(default_factory=dict)
    segments: dict[int, Segment] = field(default_factory=dict)

    @property
    def captured_silent(self) -> bool:
        """Every chunk received was below the silence floor.

        This is how an app that opts out of playback capture shows up: the
        capture runs, the samples are all zero. One audible chunk is enough to
        prove capture works for this reel.
        """
        return bool(self.segments) and all(s.state == SILENT for s in self.segments.values())


class SessionStore:
    def __init__(
        self,
        transcribe: Callable[[bytes], str] | None = None,
        *,
        ttl_s: float = TTL_S,
        max_workers: int = 4,
        clock: Callable[[], float] = time.monotonic,
    ):
        # Resolve asr.transcribe at call time, not import time, so a test that
        # monkeypatches the module attribute reaches the module-level store.
        self._transcribe = transcribe or (lambda b: asr.transcribe(b))
        self._ttl = ttl_s
        self._clock = clock
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="asr")
        self._sessions: dict[str, Session] = {}
        self._pending = 0   # chunks submitted to the pool and not yet finished
        self._cv = threading.Condition()

    # --- lifecycle ----------------------------------------------------------

    def sweep(self) -> int:
        """Drop expired sessions. Called on every access, so no timer thread."""
        now = self._clock()
        with self._cv:
            dead = [k for k, s in self._sessions.items() if now - s.created >= self._ttl]
            for k in dead:
                del self._sessions[k]
        return len(dead)

    def get(self, session_id: str) -> Session | None:
        self.sweep()
        with self._cv:
            return self._sessions.get(session_id)

    def set_hints(self, session_id: str, **hints: Any) -> None:
        with self._cv:
            s = self._sessions.get(session_id)
            if s is not None:
                s.hints.update({k: v for k, v in hints.items() if v})

    def __len__(self) -> int:
        with self._cv:
            return len(self._sessions)

    # --- ingest -------------------------------------------------------------

    def add_segment(self, session_id: str, seq: int, app: str, wav_bytes: bytes) -> str:
        """Register one chunk and start transcribing it. Returns its initial state.

        Raises ValueError if the bytes are not a PCM16 WAV -- that is checked
        here, synchronously, so the client gets a 400 rather than a chunk that
        silently fails later.

        Re-uploading a seq that already succeeded or is in flight is a no-op:
        the client retries on network errors, and transcribing the same chunk
        twice would only race two writers on one slot. A FAILED seq is retried.

        Raises ValueError for a seq outside 0..MAX_SEQ, and Busy when
        MAX_SESSIONS or MAX_PENDING is full -- checked before anything is
        queued, so a refused chunk costs neither memory nor an ASR call.
        """
        if not 0 <= seq <= MAX_SEQ:
            raise ValueError(f"seq must be 0..{MAX_SEQ}")
        silent = asr.is_silent(wav_bytes)
        self.sweep()
        with self._cv:
            s = self._sessions.get(session_id)
            existing = s.segments.get(seq) if s is not None else None
            if existing is not None and existing.state != FAILED:
                return existing.state
            # Both caps are checked before the session is created, so a
            # refused first chunk does not leave an empty session behind for
            # /v2/check to wait on.
            if s is None and len(self._sessions) >= MAX_SESSIONS:
                raise Busy("too many live sessions")
            if not silent and self._pending >= MAX_PENDING:
                raise Busy("transcription queue full")
            if s is None:
                s = self._sessions[session_id] = Session(app=app, created=self._clock())
            if silent:
                s.segments[seq] = Segment(SILENT)
                self._cv.notify_all()
                return SILENT
            self._pending += 1
            s.segments[seq] = Segment(PENDING)
        self._pool.submit(self._run, s, seq, wav_bytes)
        return PENDING

    def _run(self, s: Session, seq: int, wav_bytes: bytes) -> None:
        try:
            text, err = self._transcribe(wav_bytes) or "", None
        except Exception as e:  # never let a provider error kill the worker
            text, err = "", f"{type(e).__name__}: {e}"
        # The audio is referenced only by this call's arguments; once it
        # returns, nothing in the store holds it.
        with self._cv:
            # The session may have been swept while we were transcribing. It
            # holds the segment dict by reference, so writing is harmless, but
            # the object is unreachable and goes with it.
            s.segments[seq] = Segment(FAILED, error=err) if err else Segment(DONE, text=text)
            self._pending -= 1
            self._cv.notify_all()

    # --- read ---------------------------------------------------------------

    def _expected(self, s: Session, final_seq: int) -> range:
        # Wait for every seq from the lowest one we have seen up to final_seq.
        # The lower bound comes from the data rather than a fixed 0 so the
        # server does not care whether the client numbers from 0 or 1; a gap
        # in the middle (a lost upload) is still waited for until the timeout.
        lo = min([k for k in s.segments if k <= final_seq], default=final_seq)
        return range(lo, final_seq + 1)

    def transcript(self, session_id: str, final_seq: int, timeout: float) -> str:
        """Join segments up to and including final_seq, in seq order.

        Blocks until each of them is transcribed or `timeout` runs out. On
        timeout the segments that are done are joined anyway: a partial
        transcript still grounds a verdict on what was really said, while
        returning nothing would throw away the prefetch. Raises KeyError for an
        unknown or expired session.
        """
        deadline = self._clock() + max(timeout, 0.0)
        s = self.get(session_id)
        if s is None:
            raise KeyError(session_id)
        if final_seq < 0:
            return ""
        with self._cv:
            while True:
                want = self._expected(s, final_seq)
                if all(k in s.segments and s.segments[k].state != PENDING for k in want):
                    break
                left = deadline - self._clock()
                if left <= 0:
                    break
                self._cv.wait(timeout=min(left, 0.25))
            return " ".join(
                seg.text for k, seg in sorted(s.segments.items())
                if k <= final_seq and seg.state == DONE and seg.text
            )

    def outstanding(self, session_id: str, final_seq: int) -> list[int]:
        """Seqs up to final_seq that are still transcribing or never arrived.

        transcript() returns "" both when nobody spoke and when it timed out
        before any chunk finished; this is how a caller tells the two apart.
        Saying "no speech" for a talking-head video whose transcription was
        merely slow is a false statement to the user.
        """
        s = self.get(session_id)
        if s is None or final_seq < 0:
            return []
        with self._cv:
            return [k for k in self._expected(s, final_seq)
                    if k not in s.segments or s.segments[k].state == PENDING]


store = SessionStore()

# Module-level names matching the spec, so callers do not need the instance.
add_segment = store.add_segment
transcript = store.transcript
get = store.get
outstanding = store.outstanding
set_hints = store.set_hints
sweep = store.sweep
