package dev.tdporter.shortcheck

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioPlaybackCaptureConfiguration
import android.media.AudioRecord
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import androidx.annotation.RequiresApi
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

/**
 * Captures the audio the target apps are playing -- and nothing else.
 *
 * Why this shape:
 *  - MediaProjection is held only because Android gates playback capture
 *    behind it. No VirtualDisplay is ever created, so no screen pixels exist
 *    to leak.
 *  - The AudioRecord is built from an AudioPlaybackCaptureConfiguration, never
 *    from setAudioSource(MIC). The RECORD_AUDIO permission is still demanded
 *    by the platform for playback capture; holding it does not open the mic.
 *  - Matching is by UID of the target packages, so a podcast app or a call in
 *    the background is not captured even though it plays USAGE_MEDIA too.
 *  - Recording runs only between beginReel() and endReel(), which the
 *    accessibility service drives from "a reel is on screen". Between reels
 *    the AudioRecord is stopped, not just ignored.
 *
 * Audio lives only in the in-memory ring below; it is never written to disk.
 *
 * Playback capture is Android 10+. The class itself loads on older versions
 * (minSdk is 26) so callers need no version checks: there, onStartCommand
 * refuses to start and `instance` stays null.
 */
class CaptureService : Service() {

    private var projection: MediaProjection? = null
    private var record: AudioRecord? = null
    private var reader: Thread? = null
    private val main = Handler(Looper.getMainLooper())

    private val lock = Any()
    private val ring = PcmRing(RING_SECONDS * SAMPLE_RATE)
    private var reelKey: String? = null
    private var lastDrainEnd = 0L

    private val projectionCallback = object : MediaProjection.Callback() {
        // The user revoked capture from the status-bar chip, another app took
        // the projection, or the system stopped it. Any of these makes the
        // token useless (Android 14+ will not let us reuse it), so the only
        // clean answer is to shut down and let the user press Start again.
        override fun onStop() {
            Log.i(TAG, "projection stopped by system/user")
            main.post { shutdown() }
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) { shutdown(); return START_NOT_STICKY }
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) { shutdown(); return START_NOT_STICKY }

        val code = intent?.getIntExtra(EXTRA_RESULT_CODE, Activity.RESULT_CANCELED)
            ?: Activity.RESULT_CANCELED
        @Suppress("DEPRECATION")
        val data: Intent? = intent?.getParcelableExtra(EXTRA_RESULT_DATA)
        // Order is mandated on Android 14+: the service must already be in the
        // foreground with type mediaProjection BEFORE getMediaProjection(), or
        // it throws SecurityException. It also comes before the token check
        // below: leaving a startForegroundService() call without a matching
        // startForeground() crashes the app even if we stop right after. It
        // can itself throw when there is no consent behind it, hence the guard.
        if (runCatching { startInForeground() }.onFailure { Log.w(TAG, "startForeground", it) }.isFailure) {
            shutdown(); return START_NOT_STICKY
        }

        // A null intent is a system restart. The consent token is single-use
        // on Android 14+, so there is nothing valid to restart with.
        if (code != Activity.RESULT_OK || data == null) { shutdown(); return START_NOT_STICKY }

        // A second Start while running: the new token supersedes the old one.
        // Tear the old projection down rather than holding two.
        releaseCapture()

        val mpm = getSystemService(MediaProjectionManager::class.java)
        val mp = runCatching { mpm.getMediaProjection(code, data) }
            .onFailure { Log.w(TAG, "getMediaProjection failed", it) }
            .getOrNull()
        if (mp == null) { shutdown(); return START_NOT_STICKY }
        // Registered before anything else uses the projection; Android 14+
        // throws if a capture is started on a projection with no callback.
        mp.registerCallback(projectionCallback, main)
        projection = mp

        val rec = buildRecord(mp)
        if (rec == null) { shutdown(); return START_NOT_STICKY }
        record = rec
        instance = this
        running = true
        Log.i(TAG, "capture ready (captureAll=${BuildConfig.CAPTURE_ALL_APPS})")
        return START_NOT_STICKY
    }

    @RequiresApi(Build.VERSION_CODES.Q)
    private fun startInForeground() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, "Audio check", NotificationManager.IMPORTANCE_LOW)
        )
        val stop = PendingIntent.getService(
            this, 0, Intent(this, CaptureService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE,
        )
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
        )
        val n = Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_lock_silent_mode_off)
            .setContentTitle("ShortCheck is listening to Shorts, TikTok and Reels")
            .setContentText("Only while a video is on screen. Audio stays on the phone until you've watched 5 s.")
            .setContentIntent(open)
            .addAction(Notification.Action.Builder(null, "Stop", stop).build())
            .setOngoing(true)
            // Android 14+ lets the user swipe away even an ongoing FGS
            // notification, and the service keeps running without it -- which
            // would leave capture on with nothing in the shade and the Stop
            // action gone. Swiping it away is read as what the user means by
            // it: stop listening.
            .setDeleteIntent(stop)
            .build()
        startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
    }

    /**
     * Resolve each target package to its UID. A package that is not installed
     * is skipped, not an error. If NONE are installed we refuse to build the
     * recorder at all: a playback-capture config with only usage rules would
     * capture every app's media, which is exactly what we promised not to do.
     */
    @RequiresApi(Build.VERSION_CODES.Q)
    @SuppressLint("MissingPermission")    // checked on the first line; MainActivity requests it
    private fun buildRecord(mp: MediaProjection): AudioRecord? {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "RECORD_AUDIO not granted; playback capture requires it")
            return null
        }
        val cfg = AudioPlaybackCaptureConfiguration.Builder(mp)
            .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
            .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN)
            .addMatchingUsage(AudioAttributes.USAGE_GAME)
        if (!BuildConfig.CAPTURE_ALL_APPS) {
            val uids = targetUids(this)
            if (uids.isEmpty()) {
                Log.w(TAG, "no target app installed; not capturing")
                return null
            }
            uids.forEach { cfg.addMatchingUid(it) }
        }
        val format = AudioFormat.Builder()
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .setSampleRate(SAMPLE_RATE)
            .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
            .build()
        val minBuf = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
        )
        return runCatching {
            AudioRecord.Builder()
                .setAudioFormat(format)
                .setBufferSizeInBytes(max(minBuf, SAMPLE_RATE * 2))   // >= 1 s of slack
                .setAudioPlaybackCaptureConfig(cfg.build())
                .build()
                .takeIf { it.state == AudioRecord.STATE_INITIALIZED }
        }.onFailure { Log.w(TAG, "AudioRecord build failed", it) }.getOrNull()
    }

    // --- reel lifecycle (called on the main thread by the a11y service) ----

    /** Start recording for a new reel, with an empty buffer. */
    fun beginReel(key: String) {
        val rec = record ?: return
        stopReader()
        synchronized(lock) { ring.clear(); reelKey = key; lastDrainEnd = 0L }
        runCatching { rec.startRecording() }.onFailure { Log.w(TAG, "startRecording", it); return }
        val t = Thread({ readLoop(rec, key) }, "shortcheck-capture")
        reader = t
        t.start()
    }

    /**
     * Stop recording and DROP the buffer. If the reel ended before the dwell
     * gate this is the moment its audio ceases to exist -- nothing was sent.
     */
    fun endReel() {
        stopReader()
        synchronized(lock) { ring.clear(); reelKey = null; lastDrainEnd = 0L }
    }

    private fun stopReader() {
        // stop() makes the blocking read() return, so the thread exits promptly.
        record?.let { r -> runCatching { if (r.recordingState == AudioRecord.RECORDSTATE_RECORDING) r.stop() } }
        reader?.let { runCatching { it.join(200) } }
        reader = null
    }

    private fun readLoop(rec: AudioRecord, key: String) {
        val buf = ShortArray(SAMPLE_RATE / 10)            // 100 ms
        while (rec.recordingState == AudioRecord.RECORDSTATE_RECORDING) {
            val n = rec.read(buf, 0, buf.size, AudioRecord.READ_BLOCKING)
            if (n <= 0) break
            synchronized(lock) {
                // A late read from the previous reel must not land in the next one's buffer.
                if (reelKey != key) return
                ring.write(buf, n)
            }
        }
    }

    /**
     * Samples recorded since [offsetSamples] (counted from beginReel). If the
     * ring overflowed, the oldest retained sample is returned first instead.
     * Pass [key] to get an empty result when the reel has changed since the
     * caller looked, which closes the race between a swipe and an upload.
     */
    fun drainSince(offsetSamples: Long, key: String? = null): ShortArray = synchronized(lock) {
        if (key != null && key != reelKey) return ShortArray(0)
        val d = ring.since(offsetSamples)
        lastDrainEnd = d.end
        d.samples
    }

    /** End offset of the most recent drainSince(), i.e. where the next one should start. */
    fun lastDrainEnd(): Long = synchronized(lock) { lastDrainEnd }

    fun rmsDb(samples: ShortArray): Double = pcmRmsDb(samples)

    // --- teardown ----------------------------------------------------------

    private fun releaseCapture() {
        stopReader()
        record?.let { runCatching { it.release() } }
        record = null
        projection?.let {
            runCatching { it.unregisterCallback(projectionCallback) }
            runCatching { it.stop() }
        }
        projection = null
        synchronized(lock) { ring.clear(); reelKey = null }
    }

    private fun shutdown() {
        running = false
        if (instance === this) instance = null
        releaseCapture()
        runCatching { stopForeground(STOP_FOREGROUND_REMOVE) }
        stopSelf()
        listener?.invoke()
    }

    override fun onDestroy() {
        running = false
        if (instance === this) instance = null
        releaseCapture()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "ShortCheck"
        const val SAMPLE_RATE = 16_000
        const val RING_SECONDS = 120
        private const val CHANNEL = "capture"
        private const val NOTIF_ID = 17
        const val ACTION_STOP = "dev.tdporter.shortcheck.STOP"
        const val EXTRA_RESULT_CODE = "resultCode"
        const val EXTRA_RESULT_DATA = "resultData"

        /**
         * YouTube, TikTok (global and the Asia "trill" build), Instagram.
         * Also the <queries> list in the manifest: without it Android 11+
         * package visibility hides these packages and every UID lookup fails.
         */
        val TARGET_PACKAGES = listOf(
            "com.google.android.youtube",
            "com.zhiliaoapp.musically",
            "com.ss.android.ugc.trill",
            "com.instagram.android",
        )

        /** The live service, if capture is running. Same process as the a11y service. */
        @Volatile var instance: CaptureService? = null
            private set

        @Volatile var running = false
            private set

        /** MainActivity refreshes its status line when capture stops itself. */
        @Volatile var listener: (() -> Unit)? = null

        fun targetUids(ctx: Context): List<Int> = TARGET_PACKAGES.mapNotNull { pkg ->
            runCatching { ctx.packageManager.getApplicationInfo(pkg, 0).uid }.getOrNull()
        }.distinct()

        fun start(ctx: Context, resultCode: Int, data: Intent) {
            val i = Intent(ctx, CaptureService::class.java)
                .putExtra(EXTRA_RESULT_CODE, resultCode)
                .putExtra(EXTRA_RESULT_DATA, data)
            ctx.startForegroundService(i)
        }

        fun stop(ctx: Context) {
            ctx.startService(Intent(ctx, CaptureService::class.java).setAction(ACTION_STOP))
        }
    }
}

// --- pure helpers (no android.*; unit-tested on the JVM) ---------------------

/**
 * Fixed-capacity ring of PCM16 samples, addressed by absolute offset from the
 * last clear(). 120 s at 16 kHz mono is 3.84 MB -- a hard memory ceiling even
 * if the uploader stalls, and long enough that a normal 5 s cadence never
 * overflows it.
 */
class PcmRing(private val capacity: Int) {
    private val buf = ShortArray(capacity)
    /** Total samples written since clear(); the absolute end offset. */
    var written = 0L
        private set

    data class Drained(val samples: ShortArray, val end: Long)

    fun clear() { written = 0L }

    fun write(src: ShortArray, n: Int = src.size) {
        // Only the newest `capacity` samples of an oversized write can survive.
        val skip = max(0, n - capacity)
        written += skip
        var i = skip
        while (i < n) {
            val pos = (written % capacity).toInt()
            val run = min(n - i, capacity - pos)
            System.arraycopy(src, i, buf, pos, run)
            i += run
            written += run
        }
    }

    fun since(offset: Long): Drained {
        val start = max(max(offset, 0L), written - capacity)
        if (start >= written) return Drained(ShortArray(0), written)
        val len = (written - start).toInt()
        val out = ShortArray(len)
        var copied = 0
        while (copied < len) {
            val pos = ((start + copied) % capacity).toInt()
            val run = min(len - copied, capacity - pos)
            System.arraycopy(buf, pos, out, copied, run)
            copied += run
        }
        return Drained(out, written)
    }
}

/**
 * RMS level in dBFS (0 = full scale). Empty input is -inf, i.e. silent.
 * The silence fallback compares this against about -55 dBFS: an app that opts
 * out of playback capture hands us digital zeros, not quiet audio.
 */
fun pcmRmsDb(samples: ShortArray): Double {
    if (samples.isEmpty()) return Double.NEGATIVE_INFINITY
    var sum = 0.0
    for (s in samples) { val v = s / 32768.0; sum += v * v }
    val rms = sqrt(sum / samples.size)
    return if (rms <= 0.0) Double.NEGATIVE_INFINITY else 20 * log10(rms)
}

/** Canonical 44-byte RIFF/WAVE header followed by little-endian PCM16. */
fun encodeWav(samples: ShortArray, sampleRate: Int = CaptureService.SAMPLE_RATE): ByteArray {
    val dataLen = samples.size * 2
    val bb = ByteBuffer.allocate(44 + dataLen).order(ByteOrder.LITTLE_ENDIAN)
    bb.put("RIFF".toByteArray(Charsets.US_ASCII)).putInt(36 + dataLen)
    bb.put("WAVE".toByteArray(Charsets.US_ASCII))
    bb.put("fmt ".toByteArray(Charsets.US_ASCII)).putInt(16)
    bb.putShort(1)                       // PCM
    bb.putShort(1)                       // mono
    bb.putInt(sampleRate)
    bb.putInt(sampleRate * 2)            // byte rate
    bb.putShort(2)                       // block align
    bb.putShort(16)                      // bits per sample
    bb.put("data".toByteArray(Charsets.US_ASCII)).putInt(dataLen)
    for (s in samples) bb.putShort(s)
    return bb.array()
}
