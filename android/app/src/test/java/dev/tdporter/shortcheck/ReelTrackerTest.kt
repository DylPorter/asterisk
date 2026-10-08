package dev.tdporter.shortcheck

import dev.tdporter.shortcheck.ReelEvent.ChunkDue
import dev.tdporter.shortcheck.ReelEvent.DwellReached
import dev.tdporter.shortcheck.ReelEvent.Ended
import dev.tdporter.shortcheck.ReelEvent.Started
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ReelTrackerTest {

    private val t = ReelTracker()

    /** Run ticks every 100 ms over [from, to] and collect everything emitted. */
    private fun ticks(from: Long, to: Long): List<ReelEvent> =
        (from..to step 100).flatMap { t.tick(it) }

    @Test fun `new reel emits Started only`() {
        assertEquals(listOf(Started("a")), t.onReel("a", 0))
        assertFalse(t.dwellReached)
    }

    @Test fun `swipe before dwell never reaches DwellReached`() {
        t.onReel("a", 0)
        assertEquals(emptyList<ReelEvent>(), ticks(100, 4_900))
        assertEquals(listOf(Ended("a"), Started("b")), t.onReel("b", 4_999))
        // The new reel's clock restarted: 5 s after "a" started is not 5 s of "b".
        assertEquals(emptyList<ReelEvent>(), t.tick(5_000))
    }

    @Test fun `dwell boundary is inclusive at exactly 5000 ms`() {
        t.onReel("a", 1_000)
        assertEquals(emptyList<ReelEvent>(), t.tick(5_999))
        assertEquals(listOf(DwellReached("a")), t.tick(6_000))
        assertTrue(t.dwellReached)
        assertEquals(emptyList<ReelEvent>(), t.tick(6_000))
    }

    @Test fun `chunks follow every 5 s after dwell with contiguous seq`() {
        t.onReel("a", 0)
        val events = ticks(100, 20_000)
        assertEquals(
            listOf(DwellReached("a"), ChunkDue("a", 1), ChunkDue("a", 2), ChunkDue("a", 3)),
            events,
        )
    }

    @Test fun `chunk times anchor on schedule not on late ticks`() {
        t.onReel("a", 0)
        assertEquals(listOf(DwellReached("a")), t.tick(5_700))   // late by 700 ms
        assertEquals(emptyList<ReelEvent>(), t.tick(9_999))
        assertEquals(listOf(ChunkDue("a", 1)), t.tick(10_000))
    }

    @Test fun `starved timer emits one chunk and keeps seq contiguous`() {
        t.onReel("a", 0)
        t.tick(5_000)
        assertEquals(listOf(ChunkDue("a", 1)), t.tick(26_000))   // 10k, 15k, 20k, 25k all passed
        assertEquals(emptyList<ReelEvent>(), t.tick(29_999))
        assertEquals(listOf(ChunkDue("a", 2)), t.tick(30_000))
    }

    @Test fun `repeat of the same key only advances time`() {
        t.onReel("a", 0)
        assertEquals(emptyList<ReelEvent>(), t.onReel("a", 3_000))
        assertEquals(listOf(DwellReached("a")), t.onReel("a", 5_000))
        assertEquals(listOf(ChunkDue("a", 1)), t.onReel("a", 10_000))
    }

    @Test fun `key change after dwell ends the old reel and resets seq`() {
        t.onReel("a", 0)
        ticks(100, 12_000)
        assertEquals(listOf(Ended("a"), Started("b")), t.onReel("b", 12_100))
        assertFalse(t.dwellReached)
        assertEquals(listOf(DwellReached("b")), t.tick(17_100))
        assertEquals(listOf(ChunkDue("b", 1)), t.tick(22_100))
    }

    @Test fun `onNoReel ends the current reel once`() {
        t.onReel("a", 0)
        assertEquals(listOf(Ended("a")), t.onNoReel(1_000))
        assertNull(t.currentKey)
        assertEquals(emptyList<ReelEvent>(), t.onNoReel(1_100))
        assertEquals(emptyList<ReelEvent>(), t.tick(50_000))
    }

    @Test fun `returning to the same reel after leaving restarts the dwell`() {
        t.onReel("a", 0)
        t.tick(5_000)
        t.onNoReel(6_000)
        assertEquals(listOf(Started("a")), t.onReel("a", 7_000))
        assertEquals(emptyList<ReelEvent>(), t.tick(11_999))
        assertEquals(listOf(DwellReached("a")), t.tick(12_000))
    }

    @Test fun `tick with no reel is a no-op`() {
        assertEquals(emptyList<ReelEvent>(), t.tick(0))
        assertEquals(emptyList<ReelEvent>(), t.tick(100_000))
    }

    @Test fun `custom intervals are honoured`() {
        val fast = ReelTracker(dwellMs = 1_000, chunkMs = 500)
        fast.onReel("a", 0)
        val out = (0L..2_000L step 50).flatMap { fast.tick(it) }
        assertEquals(
            listOf(DwellReached("a"), ChunkDue("a", 1), ChunkDue("a", 2)),
            out,
        )
    }

    @Test fun `handle appearing late on a title-only key is the same reel`() {
        t.onReel("youtube||Big Tech facts", 0)
        assertEquals(listOf(DwellReached("youtube||Big Tech facts")), t.tick(5_000))
        // The Subscribe label renders after the ✱ is already showing.
        assertEquals(emptyList<ReelEvent>(), t.onReel("youtube|TechGuy|Big Tech facts", 6_000))
        assertEquals("youtube||Big Tech facts", t.currentKey)
        assertTrue(t.dwellReached)
        assertEquals(listOf(ChunkDue("youtube||Big Tech facts", 1)), t.tick(10_000))
    }

    @Test fun `a different title or a different handle is still a swipe`() {
        t.onReel("youtube||A", 0)
        assertEquals(listOf(Ended("youtube||A"), Started("youtube|h|B")), t.onReel("youtube|h|B", 100))
        assertEquals(listOf(Ended("youtube|h|B"), Started("youtube|g|B")), t.onReel("youtube|g|B", 200))
        assertEquals(listOf(Ended("youtube|g|B"), Started("tiktok|g|B")), t.onReel("tiktok|g|B", 300))
    }

    @Test fun `sameReel ignores only an empty channel`() {
        assertTrue(ReelTracker.sameReel("youtube||T", "youtube|h|T"))
        assertTrue(ReelTracker.sameReel("youtube|h|T", "youtube||T"))
        assertFalse(ReelTracker.sameReel("youtube|h|T", "youtube|g|T"))
        assertFalse(ReelTracker.sameReel(null, "youtube||T"))
        assertFalse(ReelTracker.sameReel("a", "a|b"))
        // Titles may contain '|'; limit = 3 keeps them whole.
        assertTrue(ReelTracker.sameReel("youtube||x | y", "youtube|h|x | y"))
    }
}
