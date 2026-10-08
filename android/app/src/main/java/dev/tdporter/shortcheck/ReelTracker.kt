package dev.tdporter.shortcheck

/** What the dwell state machine tells the capture/upload side to do. */
sealed interface ReelEvent {
    val key: String

    /** A new reel is on screen. Start buffering locally; nothing leaves the device yet. */
    data class Started(override val key: String) : ReelEvent

    /** The user has stayed [ReelTracker.DWELL_MS]. Only now may buffered audio be uploaded. */
    data class DwellReached(override val key: String) : ReelEvent

    /** Another chunk interval has passed since the dwell; upload what accrued. `seq` starts at 1. */
    data class ChunkDue(override val key: String, val seq: Int) : ReelEvent

    /** The reel left the screen (swipe, app switch). Drop its buffer. */
    data class Ended(override val key: String) : ReelEvent
}

/**
 * Dwell state machine for "which reel is on screen, and for how long".
 *
 * Deliberately free of android.* imports and of any clock of its own: every
 * call takes `nowMs`. That keeps the privacy-critical rule -- a reel swiped
 * away before the dwell gate uploads nothing -- testable on the JVM instead of
 * only by scrolling a phone.
 *
 * Not thread-safe; the accessibility service drives it from the main thread.
 */
class ReelTracker(
    private val dwellMs: Long = DWELL_MS,
    private val chunkMs: Long = CHUNK_MS,
) {
    var currentKey: String? = null
        private set

    /** True once the current reel has passed the dwell gate. */
    var dwellReached: Boolean = false
        private set

    private var startedAt = 0L
    private var nextSeq = 1
    private var nextDueAt = 0L

    /**
     * The accessibility service reports the reel it sees. The same key again
     * is just another content-change event (they fire constantly while a reel
     * plays), so it only advances time. A different key is a swipe: the old
     * reel ends before the new one starts, in that order, so the capture side
     * never has two reels' audio in one buffer.
     */
    fun onReel(key: String, nowMs: Long): List<ReelEvent> {
        if (key == currentKey || sameReel(currentKey, key)) return tick(nowMs)
        val out = ArrayList<ReelEvent>(2)
        currentKey?.let { out += ReelEvent.Ended(it) }
        currentKey = key
        startedAt = nowMs
        dwellReached = false
        nextSeq = 1
        out += ReelEvent.Started(key)
        return out
    }

    /** No reel on screen any more (left the Shorts player, left the app). */
    fun onNoReel(@Suppress("UNUSED_PARAMETER") nowMs: Long): List<ReelEvent> {
        val key = currentKey ?: return emptyList()
        currentKey = null
        dwellReached = false
        return listOf(ReelEvent.Ended(key))
    }

    /**
     * Called on a timer. Emits at most one event per call.
     *
     * If the timer was starved (a long GC, the main thread busy) and several
     * chunk boundaries passed at once, it emits ONE ChunkDue and skips ahead
     * rather than a burst. The upload drains everything accrued anyway, so a
     * burst would only produce empty chunks; and keeping `seq` contiguous
     * matters because the server waits for every seq up to `final_seq`.
     */
    fun tick(nowMs: Long): List<ReelEvent> {
        val key = currentKey ?: return emptyList()
        if (!dwellReached) {
            if (nowMs - startedAt < dwellMs) return emptyList()
            dwellReached = true
            // Anchor on the scheduled boundary, not on when the tick happened
            // to land, so chunk cadence does not drift with timer jitter.
            nextDueAt = startedAt + dwellMs + chunkMs
            return listOf(ReelEvent.DwellReached(key))
        }
        if (nowMs < nextDueAt) return emptyList()
        while (nextDueAt <= nowMs) nextDueAt += chunkMs
        return listOf(ReelEvent.ChunkDue(key, nextSeq++))
    }

    companion object {
        const val DWELL_MS = 5_000L
        const val CHUNK_MS = 5_000L

        /**
         * Whether two `app|channel|title` keys name the same reel. Equal keys
         * do; so do keys that differ only in that one of them has no channel.
         *
         * The YouTube path tracks a reel under `youtube||title` when the
         * handle label has not rendered within 2.5 s, and then gets the full
         * `youtube|@h|title` key once it does -- often after the dwell, with
         * the ✱ already showing. Treated as a swipe, that would drop the
         * buffer, discard a session that may have uploaded chunks or have a
         * check in flight, hide the ✱ and restart the dwell clock. The
         * current key is KEPT (see [onReel]) so every event for this view
         * still carries the key the capture/upload side began with.
         */
        fun sameReel(current: String?, key: String): Boolean {
            if (current == null) return false
            val a = current.split('|', limit = 3)
            val b = key.split('|', limit = 3)
            if (a.size != 3 || b.size != 3) return false
            if (a[0] != b[0] || a[2] != b[2]) return false
            return a[1] == b[1] || a[1].isEmpty() || b[1].isEmpty()
        }
    }
}
