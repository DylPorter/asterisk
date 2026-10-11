package dev.tdporter.shortcheck

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.media.projection.MediaProjectionConfig
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.Gravity
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

/**
 * Setup screen. Two Settings permissions (overlay, accessibility) plus a
 * Start/Stop for audio capture.
 *
 * v1 had no start button: it only read titles, so it could run ambiently.
 * v2 listens to app audio, and Android rightly makes that an explicit,
 * per-session act -- the screen-capture consent dialog, then a persistent
 * notification for as long as it runs. The button maps onto that one-to-one.
 */
class MainActivity : Activity() {

    private lateinit var status: TextView
    private lateinit var captureButton: Button

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val dp = resources.displayMetrics.density
        fun px(v: Int) = (v * dp).toInt()

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(px(24), px(48), px(24), px(24))
            setBackgroundColor(0xFF0E1116.toInt())
        }

        root.addView(TextView(this).apply {
            text = "ShortCheck"
            setTextColor(Color.WHITE); textSize = 26f
        })
        root.addView(TextView(this).apply {
            text = "Fact-checks YouTube Shorts, TikToks and Instagram Reels when you tap ✱.\n\n" +
                "While capture is on, it hears only those three apps, and only while a reel is on screen. " +
                "Audio stays in memory on the phone until you have watched for 5 seconds; swipe sooner " +
                "and it is thrown away. After 5 seconds the audio is sent to the ShortCheck server, which " +
                "has it transcribed by a third-party speech-to-text provider (Google Gemini by default); " +
                "the server keeps no audio and drops transcripts after 10 minutes. " +
                "Never the microphone, never screenshots."
            setTextColor(0xFF90A4AE.toInt()); textSize = 13f
            setPadding(0, px(12), 0, px(24))
        })

        status = TextView(this).apply {
            setTextColor(0xFFB0BEC5.toInt()); textSize = 13f
            setPadding(0, px(16), 0, px(16))
        }

        root.addView(button("1 · Allow drawing over other apps") {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                startActivity(
                    Intent(
                        Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                        Uri.parse("package:$packageName"),
                    )
                )
            }
        })
        root.addView(button("2 · Enable the ShortCheck service") { discloseThenOpenAccessibility() })
        captureButton = button("3 · Start audio check") { toggleCapture() }
        root.addView(captureButton)
        root.addView(status)
        root.addView(TextView(this).apply {
            text = "Server: ${BuildConfig.SERVER}"
            setTextColor(0xFF546E7A.toInt()); textSize = 11f
        })

        setContentView(root)
    }

    override fun onResume() {
        super.onResume()
        // CaptureService can stop on its own (revoked from the status-bar chip).
        CaptureService.listener = { runOnUiThread { refresh() } }
        refresh()
    }

    override fun onPause() {
        super.onPause()
        CaptureService.listener = null
    }

    /**
     * Prominent disclosure before the accessibility deep link. ShortCheck is
     * not an accessibility tool, so Play policy requires the user to see what
     * the service reads and what leaves the device, and to accept it, BEFORE
     * being sent to enable it. Shown on every press rather than remembered:
     * the button is pressed once or twice in the app's lifetime, and a stale
     * "accepted" flag is not consent to text that may have changed since.
     * Only the explicit Accept button proceeds; back, outside-tap and
     * "No thanks" all do nothing.
     */
    private fun discloseThenOpenAccessibility() {
        AlertDialog.Builder(this)
            .setTitle(R.string.a11y_disclosure_title)
            .setMessage(R.string.a11y_disclosure_body)
            .setPositiveButton(R.string.a11y_disclosure_accept) { _, _ ->
                startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
            }
            .setNegativeButton(R.string.a11y_disclosure_decline, null)
            .show()
    }

    // --- capture start/stop ------------------------------------------------

    private fun toggleCapture() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) { refresh(); return }
        if (CaptureService.running) {
            CaptureService.stop(this)
            status.postDelayed({ refresh() }, 300)
            return
        }
        // RECORD_AUDIO is required by the platform for playback capture even
        // though no mic source is ever opened. Notifications so the "listening"
        // notice is actually visible on Android 13+.
        val wanted = buildList {
            add(Manifest.permission.RECORD_AUDIO)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) add(Manifest.permission.POST_NOTIFICATIONS)
        }.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (wanted.isNotEmpty()) requestPermissions(wanted.toTypedArray(), REQ_PERMS)
        else requestProjection()
    }

    override fun onRequestPermissionsResult(code: Int, perms: Array<out String>, results: IntArray) {
        super.onRequestPermissionsResult(code, perms, results)
        if (code != REQ_PERMS) return
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            requestProjection()
        } else refresh()
    }

    private fun requestProjection() {
        val mpm = getSystemService(MediaProjectionManager::class.java)
        // Force the whole-screen option on 14+: in the single-app mode the
        // projection is bound to one app, which defeats capturing whichever
        // of the three the user scrolls next. No pixels are captured either way.
        val intent = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE)
            mpm.createScreenCaptureIntent(MediaProjectionConfig.createConfigForDefaultDisplay())
        else mpm.createScreenCaptureIntent()
        @Suppress("DEPRECATION")
        startActivityForResult(intent, REQ_PROJECTION)
    }

    @Deprecated("Activity result API needs androidx.activity; this app is plain Activity")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION")
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQ_PROJECTION) return
        // The token in `data` is single-use on Android 14+: hand it straight
        // to the service, never keep it for a later restart.
        if (resultCode == RESULT_OK && data != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            CaptureService.start(this, resultCode, data)
            status.postDelayed({ refresh() }, 500)
        } else refresh()
    }

    private fun refresh() {
        val canOverlay = Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.canDrawOverlays(this)
        val enabled = Settings.Secure.getString(
            contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
        )?.contains(packageName) == true
        val supported = Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q
        val capturing = supported && CaptureService.running
        val mic = checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        val installed = if (supported) CaptureService.targetUids(this).size else 0
        captureButton.text = if (capturing) "3 · Stop audio check" else "3 · Start audio check"
        captureButton.isEnabled = supported
        status.text = buildString {
            append(if (canOverlay) "✓ Overlay allowed" else "✗ Overlay not allowed")
            append("\n")
            append(if (enabled) "✓ Service enabled" else "✗ Service not enabled")
            append("\n")
            append(
                when {
                    !supported -> "✗ Audio check needs Android 10+ (YouTube title-only fallback still works)"
                    capturing -> "✓ Audio check running — open Shorts, TikTok or Reels"
                    !mic -> "✗ Audio check off (needs the audio-capture permission)"
                    installed == 0 && !BuildConfig.CAPTURE_ALL_APPS -> "✗ None of YouTube, TikTok or Instagram is installed"
                    else -> "✗ Audio check off"
                }
            )
            if (BuildConfig.CAPTURE_ALL_APPS) append("\n⚠ debug: capturing ALL apps' media audio")
        }
    }

    private fun button(label: String, onClick: () -> Unit) = Button(this).apply {
        text = label
        gravity = Gravity.START or Gravity.CENTER_VERTICAL
        setOnClickListener { onClick() }
    }

    private companion object {
        const val REQ_PERMS = 1
        const val REQ_PROJECTION = 2
    }
}
