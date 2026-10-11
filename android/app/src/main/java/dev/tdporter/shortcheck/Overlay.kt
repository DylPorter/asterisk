package dev.tdporter.shortcheck

import android.content.Context
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.view.animation.DecelerateInterpolator
import android.view.animation.OvershootInterpolator
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import kotlin.math.abs

/**
 * A draggable puck that walks through hidden -> ✱ button -> checking ->
 * verdict card, and expands into a detail card.
 *
 * v1 showed a badge for every Short automatically. v2 checks nothing until the
 * user taps: the ✱ only appears once the reel has passed the dwell gate, and
 * the tap is what sends it to the fact-checker.
 *
 * The window is deliberately two different sizes. Collapsed it is a ~56dp box,
 * because a touchable overlay any larger would swallow the vertical swipe that
 * moves to the next Short -- the one gesture we must never break. Expanded it
 * becomes full-screen so a tap anywhere outside the card dismisses it.
 */
object Overlay {

    private const val SIZE_DP = 56

    private var host: FrameLayout? = null
    private var puck: TextView? = null
    private var scrim: View? = null
    private var card: LinearLayout? = null
    private var cardBody: LinearLayout? = null
    private var lp: WindowManager.LayoutParams? = null
    private var expanded = false
    private var last: Verdict? = null

    /** Set while the puck is the ✱ "tap to check" button; consumed by one tap. */
    private var onTap: (() -> Unit)? = null

    private var posX = -1
    private var posY = -1

    private data class Tier(val glyph: String, val color: Int, val label: String)

    /**
     * Five clearly separated steps, green -> red.
     *
     * Tier 4 previously sat at a near-green lime with the label "Mostly true",
     * so a video containing a real factual error (Nintendo described as 150+
     * years old; it is ~135) read as a pass at a glance. A verified
     * inaccuracy must not look like a clean bill of health -- 4 is now amber
     * and named for the error, not for the majority that was fine.
     */
    private fun tier(score: Int) = when (score) {
        5 -> Tier("★", 0xFF13A150.toInt(), "Accurate")
        4 -> Tier("!", 0xFFCFA212.toInt(), "Minor inaccuracy")
        3 -> Tier("—", 0xFFE8730C.toInt(), "Mixed")
        2 -> Tier("✕", 0xFFD9480F.toInt(), "Mostly wrong")
        else -> Tier("👎", 0xFFC01919.toInt(), "False")
    }

    private val CHECKING = Tier("✳", 0xFF455A64.toInt(), "Checking")
    private val TAP = Tier("✱", 0xFF1F6FEB.toInt(), "Tap to check")
    private val UNKNOWN = Tier("?", 0xFF6B7A86.toInt(), "Can't check")
    private val BROKEN = Tier("⚡", 0xFF8E44AD.toInt(), "Error")

    private fun wm(ctx: Context) = ctx.getSystemService(Context.WINDOW_SERVICE) as WindowManager
    private fun Context.px(v: Int) = (v * resources.displayMetrics.density).toInt()

    // --- construction ----------------------------------------------------

    private fun ensure(ctx: Context) {
        if (host != null) return

        val frame = FrameLayout(ctx)

        scrim = View(ctx).apply {
            setBackgroundColor(0xB3000000.toInt())
            visibility = View.GONE
            alpha = 0f
            setOnClickListener { collapse(ctx) }
        }
        frame.addView(
            scrim,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT
            )
        )

        card = LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE
            setPadding(ctx.px(18), ctx.px(16), ctx.px(18), ctx.px(16))
            background = GradientDrawable().apply {
                cornerRadius = ctx.px(20).toFloat()
                setColor(0xFF161B22.toInt())
                setStroke(ctx.px(1), 0x1AFFFFFF)
            }
            // Absorb taps so they don't fall through to the dismiss scrim.
            isClickable = true
        }
        cardBody = LinearLayout(ctx).apply { orientation = LinearLayout.VERTICAL }
        val scroll = ScrollView(ctx).apply {
            isVerticalScrollBarEnabled = false
            addView(cardBody)
        }
        card!!.addView(
            scroll,
            LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply { }
        )
        frame.addView(
            card,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.CENTER,
            ).apply { marginStart = ctx.px(20); marginEnd = ctx.px(20) }
        )

        puck = TextView(ctx).apply {
            gravity = Gravity.CENTER
            textSize = 22f
            setTextColor(Color.WHITE)
        }
        frame.addView(
            puck,
            FrameLayout.LayoutParams(ctx.px(SIZE_DP), ctx.px(SIZE_DP), Gravity.TOP or Gravity.START)
        )

        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        else @Suppress("DEPRECATION") WindowManager.LayoutParams.TYPE_PHONE

        lp = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            type,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            x = if (posX >= 0) posX else ctx.px(14)
            y = if (posY >= 0) posY else ctx.px(150)
        }

        attachDrag(ctx)
        runCatching { wm(ctx).addView(frame, lp) }.onSuccess { host = frame }
    }

    private fun attachDrag(ctx: Context) {
        val slop = 8 * ctx.resources.displayMetrics.density
        var downX = 0f; var downY = 0f; var startX = 0; var startY = 0; var dragged = false

        puck?.setOnTouchListener { _, e ->
            when (e.action) {
                MotionEvent.ACTION_DOWN -> {
                    downX = e.rawX; downY = e.rawY
                    startX = lp!!.x; startY = lp!!.y; dragged = false
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    if (expanded) return@setOnTouchListener true
                    val dx = e.rawX - downX; val dy = e.rawY - downY
                    if (abs(dx) > slop || abs(dy) > slop) dragged = true
                    if (dragged) {
                        lp!!.x = startX + dx.toInt(); lp!!.y = startY + dy.toInt()
                        posX = lp!!.x; posY = lp!!.y
                        runCatching { wm(ctx).updateViewLayout(host, lp) }
                    }
                    true
                }
                // A drag must not also register as a tap, or the card opens
                // every time the badge is repositioned.
                // While it is the ✱ button, a tap starts the check instead of
                // opening a card (there is no card yet). Consumed once, so a
                // double tap cannot fire two checks.
                MotionEvent.ACTION_UP -> {
                    if (!dragged) {
                        val tap = onTap
                        if (tap != null && last == null) { onTap = null; tap() } else expand(ctx)
                    }
                    true
                }
                else -> false
            }
        }
    }

    // --- expand / collapse ----------------------------------------------

    private fun expand(ctx: Context) {
        val v = last ?: return
        if (expanded) return
        expanded = true
        buildCard(ctx, v)

        // Hide the puck BEFORE resizing the window. The puck sits at
        // TOP|START inside the frame, so the instant the window grows to
        // full-screen at x=0,y=0 it renders in the corner -- that was the
        // "teleports to the top left on press" flicker. Order matters; this is
        // not a cosmetic reshuffle.
        puck?.visibility = View.GONE
        scrim?.apply { visibility = View.VISIBLE; alpha = 1f }
        card?.apply {
            visibility = View.VISIBLE
            alpha = 1f; scaleX = 1f; scaleY = 1f; translationY = 0f
        }

        lp?.apply {
            width = WindowManager.LayoutParams.MATCH_PARENT
            height = WindowManager.LayoutParams.MATCH_PARENT
            x = 0; y = 0
        }
        runCatching { wm(ctx).updateViewLayout(host, lp) }
    }

    private fun collapse(ctx: Context) {
        if (!expanded) return
        expanded = false
        scrim?.visibility = View.GONE
        card?.visibility = View.GONE
        lp?.apply {
            width = WindowManager.LayoutParams.WRAP_CONTENT
            height = WindowManager.LayoutParams.WRAP_CONTENT
            x = if (posX >= 0) posX else ctx.px(14)
            y = if (posY >= 0) posY else ctx.px(150)
        }
        runCatching { wm(ctx).updateViewLayout(host, lp) }
        puck?.visibility = View.VISIBLE
    }

    // --- card content ----------------------------------------------------

    private fun label(ctx: Context, text: String, size: Float, color: Int, bold: Boolean = false) =
        TextView(ctx).apply {
            this.text = text
            textSize = size
            setTextColor(color)
            if (bold) setTypeface(typeface, Typeface.BOLD)
        }

    /**
     * Structured fields, not a paragraph. "1 of 5 statements wrong" tells a
     * viewer something a prose summary cannot: whether the video is broadly
     * sound with one slip, or wrong throughout. That distinction was exactly
     * what a single "mixed" label was destroying.
     */
    private fun buildCard(ctx: Context, v: Verdict) {
        val body = cardBody ?: return
        body.removeAllViews()

        // "Can't check" is not a score of 3. Rendering it through the verdict
        // layout showed a 3/5 "Mixed" badge with metric tiles for a video we
        // never actually evaluated -- a fabricated middling verdict, which is
        // worse than an empty state. Non-results get their own card.
        if (v.score == 0) {
            val t = if (v.overall == "error") BROKEN else UNKNOWN
            val head = LinearLayout(ctx).apply {
                orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            }
            head.addView(TextView(ctx).apply {
                text = t.glyph; textSize = 26f; setTextColor(Color.WHITE); gravity = Gravity.CENTER
                background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(t.color) }
                layoutParams = LinearLayout.LayoutParams(ctx.px(46), ctx.px(46))
            })
            head.addView(label(ctx, t.label, 17f, Color.WHITE, bold = true).apply {
                setPadding(ctx.px(12), 0, 0, 0)
            })
            body.addView(head)
            body.addView(label(ctx, v.headline, 12.5f, 0xFF98A6B3.toInt()).apply {
                setPadding(0, ctx.px(12), 0, 0)
            })
            body.addView(label(ctx, "No verdict was produced for this video.",
                10f, 0xFF4A5764.toInt()).apply { setPadding(0, ctx.px(10), 0, 0) })
            return
        }

        val t = tier(v.score)

        val header = LinearLayout(ctx).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        header.addView(TextView(ctx).apply {
            text = t.glyph; textSize = 26f; setTextColor(Color.WHITE); gravity = Gravity.CENTER
            background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(t.color) }
            layoutParams = LinearLayout.LayoutParams(ctx.px(46), ctx.px(46))
        })
        header.addView(LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(ctx.px(12), 0, 0, 0)
            addView(label(ctx, t.label, 17f, Color.WHITE, bold = true))
            addView(label(ctx, "score ${v.score}/5", 12f, 0xFF8B98A5.toInt()))
        })
        body.addView(header)

        body.addView(label(ctx, v.headline, 14f, 0xFFD6DEE6.toInt()).apply {
            setPadding(0, ctx.px(14), 0, ctx.px(12))
        })

        // Metric row: counts first, because they are read fastest.
        val metrics = LinearLayout(ctx).apply { orientation = LinearLayout.HORIZONTAL }
        fun metric(value: String, cap: String, color: Int) = LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            addView(label(ctx, value, 19f, color, bold = true))
            addView(label(ctx, cap, 10f, 0xFF6B7A86.toInt()))
        }
        if (v.factualPct >= 0) metrics.addView(metric("${v.factualPct}%", "FACTUAL", t.color))
        metrics.addView(metric("${v.statementsChecked}", "CHECKED", 0xFFD6DEE6.toInt()))
        metrics.addView(
            metric(
                "${v.statementsWrong}", "WRONG",
                if (v.statementsWrong > 0) 0xFFE8730C.toInt() else 0xFF1B9E4B.toInt()
            )
        )
        if (v.skipped > 0) metrics.addView(metric("${v.skipped}", "SKIPPED", 0xFF6B7A86.toInt()))
        body.addView(metrics, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT
        ).apply { bottomMargin = ctx.px(14) })

        for (c in v.claims) {
            val glyph = when (c.rating) {
                "supported" -> "✓" to 0xFF1B9E4B.toInt()
                "contradicted" -> "✕" to 0xFFD02828.toInt()
                "misleading" -> "~" to 0xFFE8730C.toInt()
                else -> "?" to 0xFF6B7A86.toInt()
            }
            val row = LinearLayout(ctx).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(ctx.px(10), ctx.px(9), ctx.px(10), ctx.px(9))
                background = GradientDrawable().apply {
                    cornerRadius = ctx.px(10).toFloat(); setColor(0xFF1C2430.toInt())
                }
            }
            row.addView(label(ctx, "${glyph.first}  ${c.claim}", 12.5f, glyph.second, bold = true))
            row.addView(label(ctx, c.note, 11.5f, 0xFF98A6B3.toInt()).apply {
                setPadding(0, ctx.px(4), 0, 0)
            })
            c.sources.firstOrNull()?.let {
                row.addView(label(ctx, it, 10f, 0xFF5A6B7A.toInt()).apply {
                    setPadding(0, ctx.px(4), 0, 0); maxLines = 1
                })
            }
            body.addView(row, LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply { bottomMargin = ctx.px(8) })
        }

        body.addView(label(
            ctx,
            if (v.cached) "cached result · tap outside to close"
            else "checked in %.0fs · tap outside to close".format(v.seconds),
            10f, 0xFF4A5764.toInt(),
        ).apply { setPadding(0, ctx.px(6), 0, 0) })
    }

    // --- public API ------------------------------------------------------

    private fun renderPuck(ctx: Context, t: Tier) {
        ensure(ctx)
        puck?.background = GradientDrawable().apply {
            shape = GradientDrawable.OVAL
            setColor(t.color)
            setStroke(ctx.px(2), 0x66FFFFFF)
        }
        puck?.text = t.glyph
        host?.visibility = View.VISIBLE
    }

    private fun stub(kind: String, headline: String) = Verdict(
        score = 0, overall = kind, headline = headline,
        statementsChecked = 0, statementsWrong = 0, factualPct = -1,
        claims = emptyList(), skipped = 0, cached = false, seconds = 0.0,
    )

    /** The reel passed the dwell gate: offer the check, don't run it. */
    fun showButton(ctx: Context, tap: () -> Unit) {
        if (expanded) collapse(ctx)
        last = null
        onTap = tap
        stopSpin()
        renderPuck(ctx, TAP)
    }

    fun showChecking(ctx: Context, title: String) {
        if (expanded) collapse(ctx)
        onTap = null
        last = null                       // nothing to expand into yet
        renderPuck(ctx, CHECKING)
        spin()
    }

    /** The asterisk spins while checking, then stops on the verdict glyph. */
    private fun spin() {
        puck?.apply {
            clearAnimation()
            animate().cancel()
            rotation = 0f
            animate().rotationBy(360f).setDuration(1100)
                .setInterpolator(android.view.animation.LinearInterpolator())
                .withEndAction { if (last == null) spin() }
                .start()
        }
    }

    private fun stopSpin() {
        puck?.apply { animate().cancel(); clearAnimation(); rotation = 0f }
    }

    /**
     * `open` = expand straight into the card. The v2 flow passes true: the
     * user just tapped and is waiting for exactly this answer, so making them
     * tap a second time to read it would be friction with no purpose.
     */
    fun showUnknown(ctx: Context, why: String, open: Boolean = false) {
        onTap = null
        last = stub("unverifiable", why)
        stopSpin()
        renderPuck(ctx, UNKNOWN)
        if (open) expand(ctx)
    }

    fun showError(ctx: Context, detail: String, open: Boolean = false) {
        onTap = null
        last = stub("error", detail)
        stopSpin()
        renderPuck(ctx, BROKEN)
        if (open) expand(ctx)
    }

    fun showVerdict(ctx: Context, v: Verdict, open: Boolean = false) {
        onTap = null
        stopSpin()
        // "Nothing was checkable" is not a score. The server encodes that case
        // as score=3 + overall=unverifiable, and score 3 is also tier "Mixed"
        // (orange) -- so a motivational clip with 0 claims checked rendered as
        // though half of it were wrong. Opinion content must read grey, not
        // orange: we have no finding, which is different from a mixed finding.
        // Grey only when there is genuinely NO finding. Including
        // overall=="unverifiable" here was too broad: the server sets that
        // whenever coverage is low (skipped > checked), so videos that DID
        // surface real claims were greyed out for having skipped more than
        // they checked. A finding with poor coverage is still a finding --
        // it is reported at a capped score, not erased.
        val noFinding = v.claims.isEmpty() ||
            v.claims.all { it.rating == "unverifiable" }
        if (noFinding) {
            last = stub("unverifiable", "Nothing factual to check in this one")
            renderPuck(ctx, UNKNOWN)
            if (open) expand(ctx)
            return
        }
        last = v
        renderPuck(ctx, tier(v.score))
        if (open) expand(ctx)
    }

    fun hide(ctx: Context) {
        host?.let { runCatching { wm(ctx).removeView(it) } }
        host = null; puck = null; scrim = null; card = null; cardBody = null; lp = null
        expanded = false; last = null; onTap = null
    }
}
