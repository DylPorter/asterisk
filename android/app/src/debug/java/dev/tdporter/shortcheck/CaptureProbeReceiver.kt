package dev.tdporter.shortcheck

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.util.Log

/**
 * DEBUG BUILDS ONLY (lives in src/debug). Emulator smoke test for playback
 * capture.
 *
 * The real trigger for recording is the accessibility service seeing a reel
 * in YouTube/TikTok/Instagram, none of which can be driven on an emulator. So
 * this stands in for it: begin a fake reel, record for a few seconds, log the
 * level, end the reel. It logs only the RMS level, never samples, and the
 * buffer is dropped by endReel() as usual. Nothing is uploaded.
 *
 *   adb shell am broadcast -n dev.tdporter.shortcheck/.CaptureProbeReceiver --ei ms 3000
 *
 * Guarded by the DUMP permission in the debug manifest: adb shell holds it,
 * third-party apps cannot, so a debug build on a real phone can't be told to
 * record by another app. Needs a debug build with -PcaptureAllApps=true to hear
 * a non-target player; otherwise it records only the three target apps.
 */
class CaptureProbeReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        val cap = CaptureService.instance
        if (cap == null) {
            Log.w(TAG, "probe: capture not running (press Start first)")
            return
        }
        val ms = intent.getIntExtra("ms", 3000).coerceIn(500, 10_000).toLong()
        val pending = goAsync()
        // beginReel/endReel are main-thread calls, as from the a11y service.
        cap.beginReel(KEY)
        Handler(Looper.getMainLooper()).postDelayed({
            val pcm = cap.drainSince(0, KEY)
            val db = pcmRmsDb(pcm)
            val verdict = if (db < Uploader.SILENT_DB) "SILENT" else "AUDIBLE"
            Log.i(
                TAG, "probe: captured ${pcm.size} samples (${pcm.size * 1000L / CaptureService.SAMPLE_RATE} ms), " +
                    "rms=${"%.1f".format(db)} dBFS, threshold=${Uploader.SILENT_DB} -> $verdict",
            )
            cap.endReel()
            pending.finish()
        }, ms)
    }

    private companion object {
        const val TAG = "ShortCheck"
        const val KEY = "debug|probe|probe"
    }
}
