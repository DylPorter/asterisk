package dev.tdporter.shortcheck

import android.content.Context
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.util.UUID

/**
 * Metadata read off the screen for a reel. Only a hint for the server's cache
 * key and for the v1 fallback -- v2 identifies content by the audio itself.
 */
data class ReelHints(val app: String, val titles: List<String>, val channel: String)

/**
 * Turns ReelTracker events into uploads, and the ✱ tap into a verdict.
 *
 * All uploads for a reel go through one mutex. That buys two things: chunks
 * reach the server in seq order, and the tap's final partial chunk is only
 * drained after every scheduled chunk has been, so no sample is sent twice or
 * skipped.
 */
object Uploader {

    private const val TAG = "ShortCheck"

    /** Below this every chunk is treated as "the app blocked capture" (spec: ~-55 dBFS). */
    const val SILENT_DB = -55.0

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val mutex = Mutex()

    private class Session(val key: String, @Volatile var hints: ReelHints) {
        /** New per reel VIEW: watching the same reel twice is two unlinked sessions. */
        val id: String = UUID.randomUUID().toString()
        var offset = 0L
        /** Last seq the server accepted; -1 = none. */
        var lastSeq = -1
        var loudestDb = Double.NEGATIVE_INFINITY
        /** Set by the ✱ tap. After it, no more audio is sent for this view. */
        @Volatile var tapped = false
        /**
         * Set by the first DwellReached. Until then nothing about this view may
         * leave the phone. Enforced here, not only by when the ✱ is shown: the
         * overlay is a separate component, and a stale button tapped across a
         * swipe would otherwise drain the new reel's pre-dwell buffer.
         */
        @Volatile var dwellPassed = false
    }

    @Volatile private var current: Session? = null

    fun begin(key: String, hints: ReelHints) {
        current = Session(key, hints)
    }

    /**
     * Better hints for the reel already being tracked under [key] -- the
     * channel handle that rendered after a title-only key was chosen. Only
     * affects what /v2/check sends (cache key, v1 fallback); the session id
     * and everything already uploaded stay as they are.
     */
    fun updateHints(key: String, hints: ReelHints) {
        current?.takeIf { it.key == key }?.hints = hints
    }

    fun end(key: String) {
        if (current?.key == key) current = null
    }

    /** DwellReached and ChunkDue both land here: upload whatever accrued since last time. */
    fun uploadDue(key: String) {
        val s = current?.takeIf { it.key == key && !it.tapped } ?: return
        s.dwellPassed = true
        scope.launch { mutex.withLock { uploadNext(s) } }
    }

    /**
     * Drain and upload the samples since the last upload. Must hold [mutex].
     *
     * seq only advances when the server accepted the chunk. If one upload
     * fails, the next attempt reuses its seq: the server waits for every seq
     * up to final_seq, so a hole would stall /v2/check until its timeout,
     * whereas a gap in the audio only costs a few words of transcript.
     */
    private suspend fun uploadNext(s: Session) {
        if (current !== s) return                        // swiped away while queued
        if (!s.dwellPassed) return                       // privacy gate; see Session
        val cap = CaptureService.instance ?: return
        val pcm = cap.drainSince(s.offset, s.key)
        if (pcm.isEmpty()) return
        s.offset = cap.lastDrainEnd()
        val db = pcmRmsDb(pcm)
        val seq = s.lastSeq + 1
        val ok = Api.postSegment(s.id, seq, s.hints.app, encodeWav(pcm)) ||
            Api.postSegment(s.id, seq, s.hints.app, encodeWav(pcm))   // one retry
        if (ok) {
            s.lastSeq = seq
            s.loudestDb = maxOf(s.loudestDb, db)
        } else {
            Log.w(TAG, "segment $seq upload failed; seq will be reused")
        }
    }

    /** The ✱ was tapped. Main thread. */
    fun onTap(ctx: Context) {
        val s = current ?: return
        if (s.tapped || !s.dwellPassed) return
        s.tapped = true
        Overlay.showChecking(ctx, s.hints.titles.firstOrNull().orEmpty())

        scope.launch {
            val result = runCatching { check(s) }
                .getOrElse { CheckResult.Failed(it.javaClass.simpleName, true) }
            withContext(Dispatchers.Main) {
                // The user swiped while we were checking: this verdict belongs
                // to a reel that is no longer on screen. Showing it would be
                // the v1 stale-badge bug all over again.
                if (current !== s) return@withContext
                when (result) {
                    is CheckResult.Ok -> Overlay.showVerdict(ctx, result.verdict, open = true)
                    is CheckResult.Unknown -> Overlay.showUnknown(ctx, result.why, open = true)
                    is CheckResult.Failed -> {
                        Log.w(TAG, "check failed: ${result.detail}")
                        if (result.showToUser) Overlay.showError(ctx, result.detail, open = true)
                        else Overlay.showUnknown(ctx, "Couldn't check this one", open = true)
                    }
                }
            }
        }
    }

    private suspend fun check(s: Session): CheckResult {
        val finalSeq = mutex.withLock {
            uploadNext(s)                                  // the last partial chunk
            s.lastSeq
        }
        // Silence fallback. Capture not running, nothing uploaded, or every
        // chunk at digital-zero level all mean the same thing: we have no
        // audio for this reel (most often the app opted out of playback
        // capture). YouTube can still be checked the v1 way, by captions;
        // the other apps have no such path.
        val silent = finalSeq < 0 || s.loudestDb < SILENT_DB
        if (silent) {
            Log.i(TAG, "session ${s.id} captured_silent (loudest ${"%.1f".format(s.loudestDb)} dBFS)")
            if (s.hints.app != "youtube") return CheckResult.Unknown("Can't check this video")
            if (s.hints.channel.isBlank() || s.hints.titles.isEmpty()) {
                return CheckResult.Unknown("Couldn't identify this video")
            }
            return Api.check(s.hints.titles, s.hints.channel)
        }
        return Api.checkV2(s.id, s.hints.app, s.hints.titles, s.hints.channel, finalSeq)
    }
}
