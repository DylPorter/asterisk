package dev.tdporter.shortcheck

import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.Gravity
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

/**
 * Setup screen. Two permissions, both user-granted in Settings, both revocable.
 *
 * There is deliberately no "start" button: once permissions are granted the
 * service runs whenever YouTube is foregrounded, which is the whole point of
 * an ambient agent.
 */
class MainActivity : Activity() {

    private lateinit var status: TextView

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
            text = "Fact-checks YouTube Shorts while you watch.\n\n" +
                "It reads only the title of the Short on screen and the channel handle — " +
                "both already public. No screenshots, no audio, nothing else."
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
        root.addView(button("2 · Enable the ShortCheck service") {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        })
        root.addView(status)
        root.addView(TextView(this).apply {
            text = "Server: ${BuildConfig.SERVER}"
            setTextColor(0xFF546E7A.toInt()); textSize = 11f
        })

        setContentView(root)
    }

    override fun onResume() {
        super.onResume()
        val canOverlay = Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.canDrawOverlays(this)
        val enabled = Settings.Secure.getString(
            contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
        )?.contains(packageName) == true
        status.text = buildString {
            append(if (canOverlay) "✓ Overlay allowed" else "✗ Overlay not allowed")
            append("\n")
            append(if (enabled) "✓ Service enabled — open YouTube Shorts" else "✗ Service not enabled")
        }
    }

    private fun button(label: String, onClick: () -> Unit) = Button(this).apply {
        text = label
        gravity = Gravity.START or Gravity.CENTER_VERTICAL
        setOnClickListener { onClick() }
    }
}
