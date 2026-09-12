package dev.tdporter.shortcheck

import android.accessibilityservice.AccessibilityService
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Watches the YouTube app for a Short on screen and reports which one.
 *
 * Verified against YouTube 21.36.45 on Android 17 (Pixel 7) by dumping the
 * live view hierarchy. Two findings shape this code:
 *
 *  - The container ids survive (`reel_player_page_container`, `reel_recycler`),
 *    but every *text* id the public blocker repos match on is gone on this
 *    build: no `reel_main_title`, no `elements_top_channel_bar_container`, no
 *    `reel_dyn_*`. So detection is by container id and extraction is
 *    structural -- scoped to the reel subtree, filtered by shape.
 *  - Swipes emit TYPE_WINDOW_CONTENT_CHANGED and never TYPE_VIEW_SCROLLED,
 *    so scroll-based heuristics would never fire here.
 */
class ShortsAccessibilityService : AccessibilityService() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var inFlight: Job? = null

    /** Identity of the Short currently being shown, so we ignore repeat events. */
    private var currentKey: String? = null

    /** Title of whatever is on screen right now, even if not yet identified. */
    private var lastVisibleTitle: String? = null

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        val pkg = event?.packageName?.toString() ?: return

        if (pkg == TIKTOK_PKG) { handleTikTok(); return }

        if (pkg != YOUTUBE_PKG) return
        val root = rootInActiveWindow ?: return

        val reel = findByIdSuffix(root, REEL_CONTAINER) ?: findByIdSuffix(root, REEL_RECYCLER)
        if (reel == null) {
            if (currentKey != null || lastVisibleTitle != null) {
                currentKey = null
                lastVisibleTitle = null
                inFlight?.cancel()
                Overlay.hide(this)
            }
            return
        }

        val labels = collectDescriptions(reel)

        // Detect that the Short CHANGED before trying to identify it. The
        // early returns below (no handle yet, no title yet) fire constantly
        // mid-swipe, and every one of them used to leave the previous video's
        // verdict on screen -- so the badge showed stats for a Short the user
        // had already scrolled past, with nothing signalling it was stale.
        val visible = pickTitles(labels).firstOrNull()
        if (visible != null && visible != lastVisibleTitle) {
            lastVisibleTitle = visible
            stuckSince = 0L
            if (currentKey != null && !currentKey!!.endsWith("|$visible")) {
                inFlight?.cancel()
                currentKey = null
                Overlay.showChecking(this, visible)
            }
        }
        // trimEnd('.') matters: the label is a sentence ("Subscribe to
        // @BBCNews.") and the handle charset legitimately contains dots, so
        // the capture swallows the full stop. Measured on device -- it
        // produced "@BBCNews." and a 404 on the channel URL.
        val handle = labels
            .firstNotNullOfOrNull { l ->
                HANDLE_PATTERNS.firstNotNullOfOrNull { it.find(l)?.groupValues?.get(1) }
            }
            ?.trimEnd('.')
            ?.takeIf { it.isNotBlank() }
            ?: run { giveUpIfStuck(); return }
        val titles = pickTitles(labels)
        if (titles.isEmpty()) { giveUpIfStuck(); return }
        val title = titles.first()

        val key = "$handle|$title"
        if (key == currentKey) return          // same Short, just another content-change event
        currentKey = key

        // The swipe is the cancel signal. Drop the previous request's UI
        // immediately -- the server deliberately lets the old job finish and
        // cache, so a swipe-back is instant rather than wasted.
        inFlight?.cancel()
        Log.i(TAG, "short: @$handle | $title")
        Overlay.showChecking(this, title)

        inFlight = scope.launch {
            val result = runCatching { Api.check(titles, "@$handle") }
                .getOrElse { CheckResult.Failed(it.javaClass.simpleName, true) }
            withContext(Dispatchers.Main) {
                val ctx = this@ShortsAccessibilityService
                when (result) {
                    is CheckResult.Ok -> Overlay.showVerdict(ctx, result.verdict)
                    is CheckResult.Unknown -> Overlay.showUnknown(ctx, result.why)
                    is CheckResult.Failed -> {
                        Log.w(TAG, "check failed: ${result.detail}")
                        // Hidden from users unless the server enables debug
                        // badges -- an error glyph they can't act on is noise.
                        if (result.showToUser) Overlay.showError(ctx, result.detail)
                        else Overlay.showUnknown(ctx, "Couldn't check this one")
                    }
                }
            }
        }
    }

    /**
     * TikTok has what YouTube does not: stable, semantic resource ids that
     * survived R8 obfuscation, so the creator and caption can be read from
     * named nodes instead of guessed at by string length.
     *
     * `id/title` holds the creator (a DISPLAY name -- "Tesla Owners Silicon
     * Valley" -- not the URL handle), and `id/desc` the caption, truncated
     * with an ellipsis and a "more" affordance. The server resolves both.
     */
    private fun handleTikTok() {
        val root = rootInActiveWindow ?: return
        if (findByIdSuffix(root, TT_PAGER) == null && findByIdSuffix(root, TT_VIDEO) == null) {
            if (currentKey != null) { currentKey = null; inFlight?.cancel(); Overlay.hide(this) }
            return
        }
        val creator = findByIdSuffix(root, TT_CREATOR)?.let {
            (it.text ?: it.contentDescription)?.toString()
        }?.trim()?.removePrefix("@")?.trim().orEmpty()
        val caption = findByIdSuffix(root, TT_CAPTION)?.let {
            (it.text ?: it.contentDescription)?.toString()
        }?.trim().orEmpty()
        if (creator.isBlank() || caption.length < 10) return

        val key = "tt|$creator|$caption"
        if (key == currentKey) return
        currentKey = key
        inFlight?.cancel()
        Log.i(TAG, "tiktok: $creator | $caption")
        Overlay.showChecking(this, caption)

        inFlight = scope.launch {
            val result = runCatching { Api.check(listOf(caption), creator, "tiktok") }
                .getOrElse { CheckResult.Failed(it.javaClass.simpleName, true) }
            withContext(Dispatchers.Main) {
                val ctx = this@ShortsAccessibilityService
                when (result) {
                    is CheckResult.Ok -> Overlay.showVerdict(ctx, result.verdict)
                    is CheckResult.Unknown -> Overlay.showUnknown(ctx, result.why)
                    is CheckResult.Failed -> {
                        Log.w(TAG, "tiktok check failed: ${result.detail}")
                        if (result.showToUser) Overlay.showError(ctx, result.detail)
                        else Overlay.showUnknown(ctx, "Couldn't check this one")
                    }
                }
            }
        }
    }

    private var stuckSince = 0L

    /**
     * A spinner with nothing behind it is worse than an honest blank. If we
     * cannot identify the Short within a couple of seconds -- no handle node,
     * no usable title -- say so instead of spinning indefinitely, which is
     * what happened on channels lacking a "Subscribe to @x" label.
     */
    private fun giveUpIfStuck() {
        if (currentKey != null) return            // a check is genuinely running
        val now = System.currentTimeMillis()
        if (stuckSince == 0L) { stuckSince = now; return }
        if (now - stuckSince > 2500) {
            stuckSince = 0L
            lastVisibleTitle = null
            Overlay.showUnknown(this, "Couldn't identify this video")
        }
    }

    override fun onInterrupt() {}

    override fun onDestroy() {
        super.onDestroy()
        scope.cancel()
        Overlay.hide(this)
    }

    // --- extraction ------------------------------------------------------

    private fun findByIdSuffix(node: AccessibilityNodeInfo, id: String): AccessibilityNodeInfo? {
        node.viewIdResourceName?.let { if (it.endsWith(id)) return node }
        for (i in 0 until node.childCount) {
            val hit = node.getChild(i)?.let { findByIdSuffix(it, id) }
            if (hit != null) return hit
        }
        return null
    }

    /**
     * Text from the Short that is ACTUALLY ON SCREEN.
     *
     * The visibility filter is load-bearing, not a tidy-up. `reel_recycler` is
     * a RecyclerView and keeps adjacent pages alive, so a naive walk collects
     * the neighbouring Shorts' titles too. Measured failure: while watching
     * "The Chain That Shapes Your Entire Life" it picked up a sibling's title
     * and resolved to a different video entirely -- then reported success,
     * because that title is real and does resolve. A wrong verdict shown
     * confidently is worse than no verdict, so identification must come only
     * from the page occupying the screen.
     */
    private fun collectDescriptions(node: AccessibilityNodeInfo): List<String> {
        val screen = android.graphics.Rect()
        node.getBoundsInScreen(screen)
        val bounds = android.graphics.Rect()
        val out = mutableListOf<String>()

        fun walk(n: AccessibilityNodeInfo) {
            if (!n.isVisibleToUser) return          // prunes recycled/off-screen pages
            n.getBoundsInScreen(bounds)
            // An adjacent page can still report isVisibleToUser mid-swipe, so
            // require the node's centre to sit inside the container. A node
            // with empty bounds is not rendered at all.
            val onThisPage = !bounds.isEmpty && bounds.centerY() in screen.top..screen.bottom
            if (onThisPage) {
                n.contentDescription?.toString()?.takeIf { it.isNotBlank() }?.let(out::add)
                n.text?.toString()?.takeIf { it.isNotBlank() }?.let(out::add)
            }
            for (i in 0 until n.childCount) n.getChild(i)?.let(::walk)
        }
        walk(node)
        return out.distinct()
    }

    /**
     * The reel subtree has no labelled title node on this build, so pick by
     * shape: drop everything that matches a known chrome pattern, then take
     * the longest survivor. Real example that must win:
     * "American airports consider security changes. #BBCNews" over
     * "BBC is a British public broadcast service." and
     * "like this video along with 123 other people".
     */
    private fun pickTitles(labels: List<String>): List<String> =
        labels.filter { l -> CHROME.none { it.containsMatchIn(l) } && l.length >= 12 }
            .sortedByDescending { it.length }
            .take(3)

    companion object {
        private const val TAG = "ShortCheck"
        private const val YOUTUBE_PKG = "com.google.android.youtube"
        private const val TIKTOK_PKG = "com.zhiliaoapp.musically"
        private const val TT_PAGER = "id/viewpager"
        private const val TT_VIDEO = "id/long_press_layout"
        private const val TT_CREATOR = "id/title"
        private const val TT_CAPTION = "id/desc"
        private const val REEL_CONTAINER = "id/reel_player_page_container"
        private const val REEL_RECYCLER = "id/reel_recycler"

        // The Subscribe button is not always present -- on a channel the user
        // already follows it renders differently, and with no "Subscribe to
        // @x" label the handle could not be read at all. Several nodes carry
        // it; accept any of them.
        private val HANDLE_PATTERNS = listOf(
            Regex("""Subscribe to @([\w.\-]+)"""),
            Regex("""Go to channel @([\w.\-]+)"""),
            Regex("""^@([\w.\-]+)$"""),
        )

        /** Chrome seen in live dumps; anything matching is not the title. */
        private val CHROME = listOf(
            Regex("""^Subscribe to """),
            Regex("""^like this video"""),
            Regex("""^View \d+ comment"""),
            Regex("""\b(is a|is an)\b.*\b(service|channel|network|broadcaster)\b"""),
            Regex("""^\d+\s*(hr|min|sec|day|week|month|year)s?\s+ago$"""),
            Regex("""^(Home|Shorts|Search|Create|Subscriptions|You|Share|Remix|Save|More actions|Navigate up)$"""),
            Regex("""^Feature not available"""),
            Regex("""^Tap to watch live$"""),
            Regex("""^New content"""),
            // The sound-attribution row. Measured failure: on @MapWarden this
            // beat the real title ("Elon Musk's Biggest Risk", 26 chars) on
            // length alone (32), so longest-wins picked it and resolution got
            // a query that matches nothing.
            Regex("""videos using this sound"""),
            Regex("""^(Original audio|Use this sound|Sound|Music)\b"""),
            Regex("""^Add (a )?comment"""),
            Regex("""^Dislike"""),
            Regex("""^(Not interested|Report|Undo)$"""),
            // Both seen as live resolve failures during a scroll session: a
            // search-suggestion chip and the remix attribution row.
            Regex("""^Search ["']"""),
            // Picked as a title on subscribed channels, and it even resolved
            // to a real video id -- so this junk query got fact-checked.
            Regex("""^Go to channel"""),
            Regex("""^(Subscribed|Following|Join)$"""),
            Regex("""Remix this Short"""),
            Regex("""^(Shuffle|Autoplay|Live|Sponsored|Promoted)$""")
        )
    }
}
