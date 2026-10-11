from __future__ import annotations

import threading
import time

import pytest

import sessions
from conftest import make_wav

# Distinct audible chunks; the fake transcriber maps bytes -> text.
CHUNKS = [make_wav(0.2, dbfs=-20, freq=200 + 100 * i) for i in range(5)]
SILENT = make_wav(0.2, dbfs=None)


class FakeASR:
    def __init__(self, delays=None, fail=(), block=None):
        self.delays = delays or {}
        self.fail = set(fail)
        self.block = block or {}
        self.calls = 0

    def __call__(self, b: bytes) -> str:
        self.calls += 1
        i = CHUNKS.index(b)
        if i in self.block:
            self.block[i].wait(5)
        time.sleep(self.delays.get(i, 0))
        if i in self.fail:
            raise RuntimeError("provider down")
        return f"part{i}"


def store(asr_fn, **kw):
    return sessions.SessionStore(asr_fn, **kw)


def test_joins_in_seq_order_even_when_finished_out_of_order():
    # seq 0 is slowest, so it completes last.
    s = store(FakeASR(delays={0: 0.2, 1: 0.05, 2: 0.0}))
    for i in (2, 0, 1):
        assert s.add_segment("a", i, "youtube", CHUNKS[i]) == sessions.PENDING
    assert s.transcript("a", 2, timeout=3) == "part0 part1 part2"


def test_final_seq_bounds_the_transcript():
    s = store(FakeASR())
    for i in range(3):
        s.add_segment("a", i, "youtube", CHUNKS[i])
    assert s.transcript("a", 1, timeout=3) == "part0 part1"


def test_one_based_numbering_does_not_block():
    s = store(FakeASR())
    s.add_segment("a", 1, "tiktok", CHUNKS[1])
    s.add_segment("a", 2, "tiktok", CHUNKS[2])
    t0 = time.monotonic()
    assert s.transcript("a", 2, timeout=3) == "part1 part2"
    assert time.monotonic() - t0 < 1.0


def test_waits_for_a_final_chunk_that_has_not_arrived_yet():
    # The tap uploads the last partial chunk then calls check; the two
    # requests can land in either order.
    s = store(FakeASR())
    s.add_segment("a", 0, "youtube", CHUNKS[0])
    threading.Timer(0.2, s.add_segment, ("a", 1, "youtube", CHUNKS[1])).start()
    assert s.transcript("a", 1, timeout=3) == "part0 part1"


def test_timeout_returns_the_finished_part():
    gate = threading.Event()
    s = store(FakeASR(block={1: gate}))
    s.add_segment("a", 0, "youtube", CHUNKS[0])
    s.add_segment("a", 1, "youtube", CHUNKS[1])
    t0 = time.monotonic()
    assert s.transcript("a", 1, timeout=0.3) == "part0"
    assert 0.25 <= time.monotonic() - t0 < 1.5
    gate.set()


def test_gap_is_waited_for_until_timeout():
    s = store(FakeASR())
    s.add_segment("a", 0, "youtube", CHUNKS[0])
    s.add_segment("a", 2, "youtube", CHUNKS[2])  # seq 1 lost in transit
    t0 = time.monotonic()
    assert s.transcript("a", 2, timeout=0.3) == "part0 part2"
    assert time.monotonic() - t0 >= 0.25


def test_silent_chunks_skip_asr_and_mark_the_session():
    asr_fn = FakeASR()
    s = store(asr_fn)
    assert s.add_segment("a", 0, "instagram", SILENT) == sessions.SILENT
    assert s.add_segment("a", 1, "instagram", SILENT) == sessions.SILENT
    assert asr_fn.calls == 0
    assert s.get("a").captured_silent
    assert s.transcript("a", 1, timeout=1) == ""


def test_one_audible_chunk_means_capture_works():
    s = store(FakeASR())
    s.add_segment("a", 0, "youtube", SILENT)
    s.add_segment("a", 1, "youtube", CHUNKS[1])
    assert not s.get("a").captured_silent
    assert s.transcript("a", 1, timeout=3) == "part1"


def test_failed_chunk_is_excluded_then_retried_on_reupload():
    asr_fn = FakeASR(fail={1})
    s = store(asr_fn)
    s.add_segment("a", 0, "youtube", CHUNKS[0])
    s.add_segment("a", 1, "youtube", CHUNKS[1])
    assert s.transcript("a", 1, timeout=3) == "part0"
    assert s.get("a").segments[1].state == sessions.FAILED
    assert "provider down" in s.get("a").segments[1].error

    asr_fn.fail.clear()
    assert s.add_segment("a", 1, "youtube", CHUNKS[1]) == sessions.PENDING
    assert s.transcript("a", 1, timeout=3) == "part0 part1"


def test_duplicate_upload_is_a_noop():
    asr_fn = FakeASR()
    s = store(asr_fn)
    s.add_segment("a", 0, "youtube", CHUNKS[0])
    s.transcript("a", 0, timeout=3)
    assert s.add_segment("a", 0, "youtube", CHUNKS[0]) == sessions.DONE
    assert asr_fn.calls == 1


def test_bad_wav_is_rejected_synchronously():
    s = store(FakeASR())
    with pytest.raises(ValueError):
        s.add_segment("a", 0, "youtube", b"garbage")
    assert s.get("a") is None


def test_ttl_sweep_drops_sessions():
    now = [1000.0]
    s = store(FakeASR(), ttl_s=600, clock=lambda: now[0])
    s.add_segment("old", 0, "youtube", SILENT)
    now[0] += 300
    s.add_segment("new", 0, "youtube", SILENT)
    now[0] += 301  # old is 601 s old, new is 301 s
    assert s.get("old") is None
    assert s.get("new") is not None
    with pytest.raises(KeyError):
        s.transcript("old", 0, timeout=0)


def test_unknown_session_raises():
    with pytest.raises(KeyError):
        store(FakeASR()).transcript("nope", 0, timeout=0)


def test_no_audio_is_retained():
    s = store(FakeASR())
    s.add_segment("a", 0, "youtube", CHUNKS[0])
    s.transcript("a", 0, timeout=3)
    sess = s.get("a")

    def walk(o):
        if isinstance(o, (bytes, bytearray, memoryview)):
            yield o
        elif isinstance(o, dict):
            for v in o.values():
                yield from walk(v)
        elif hasattr(o, "__dict__"):
            yield from walk(vars(o))

    assert list(walk(sess)) == []


# --- bounds -------------------------------------------------------------------

def test_outstanding_tells_a_timeout_from_an_empty_result():
    gate = threading.Event()
    s = store(FakeASR(block={0: gate}))
    s.add_segment("a", 0, "youtube", CHUNKS[0])
    assert s.transcript("a", 1, timeout=0.2) == ""
    assert s.outstanding("a", 1) == [0, 1]       # 0 in flight, 1 never arrived
    gate.set()
    assert s.transcript("a", 0, timeout=3) == "part0"
    assert s.outstanding("a", 0) == []


@pytest.mark.parametrize("seq", [-1, sessions.MAX_SEQ + 1])
def test_seq_outside_the_cap_is_refused(seq):
    s = store(FakeASR())
    with pytest.raises(ValueError):
        s.add_segment("a", seq, "youtube", CHUNKS[0])
    assert s.get("a") is None


def test_pending_cap_refuses_without_queueing(monkeypatch):
    monkeypatch.setattr(sessions, "MAX_PENDING", 2)
    gate = threading.Event()
    asr_fn = FakeASR(block={0: gate, 1: gate})
    s = store(asr_fn)
    s.add_segment("a", 0, "youtube", CHUNKS[0])
    s.add_segment("b", 1, "youtube", CHUNKS[1])
    with pytest.raises(sessions.Busy):
        s.add_segment("c", 2, "youtube", CHUNKS[2])
    assert s.get("c") is None                    # no empty session left behind
    # Silence costs no ASR call, so it is never refused for a full queue.
    assert s.add_segment("a", 3, "youtube", SILENT) == sessions.SILENT
    gate.set()
    s.transcript("a", 0, timeout=3)
    s.transcript("b", 1, timeout=3)
    assert s.add_segment("c", 2, "youtube", CHUNKS[2]) == sessions.PENDING
    s.transcript("c", 2, timeout=3)
    assert asr_fn.calls == 3


def test_session_cap(monkeypatch):
    monkeypatch.setattr(sessions, "MAX_SESSIONS", 2)
    s = store(FakeASR())
    s.add_segment("a", 0, "youtube", SILENT)
    s.add_segment("b", 0, "youtube", SILENT)
    with pytest.raises(sessions.Busy):
        s.add_segment("c", 0, "youtube", SILENT)
    # An existing session can still add chunks.
    assert s.add_segment("a", 1, "youtube", SILENT) == sessions.SILENT
