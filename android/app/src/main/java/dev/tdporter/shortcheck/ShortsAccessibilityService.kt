package dev.tdporter.shortcheck

import android.accessibilityservice.AccessibilityService
import android.graphics.Rect
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

/**
 * Watches YouTube, TikTok and Instagram for a reel on screen and reports
 * WHICH one, as a reel key (`app|channel|title`) fed to [ReelTracker].
 *
 * v2 change: this class no longer fact-checks anything itself. v1 fired a
 * check per Short from here; now it only says "reel X is on screen" / "no
 * reel", the tracker turns that into dwell events, and the capture/upload side
 * acts on those. Nothing is checked until the user taps the ✱.
 *
 * YouTube detection, below, is unchanged from v1 and device-verified:
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

    private val tracker = ReelTracker()
    private val main = Handler(Looper.getMainLooper())

    /** Hints for the key about to be passed to the tracker; read on Started. */
    private var pendingHints: ReelHints? = null

    /** Title of whatever is on screen right now, even if not yet identified. */
    private var lastVisibleTitle: String? = null

    /** Consecutive ticks on which the active window was not a watched app. */
    private var offAppTicks = 0
    private var ticking = false
    private var lastTreeDump = 0L

    /**
     * Accessibility events only arrive while something on screen changes, and
     * a reel that just plays emits none -- so the dwell clock needs its own
     * timer. It also catches leaving the app: packageNames in the config means
     * we get NO event when the launcher or another app comes up, so without
     * this check a reel would "stay on screen" (and recording) after the user
     * went home.
     */
    private val ticker = object : Runnable {
        override fun run() {
            val pkg = runCatching { rootInActiveWindow?.packageName?.toString() }.getOrNull()
            // Null counts as "not ours" too: whether the platform filters the
            // root by packageNames or just has no window yet, either way we
            // can't see a reel. Three misses (~750 ms) so a transient null
            // mid-transition does not end the reel.
            offAppTicks = if (pkg != null && pkg in WATCHED) 0 else offAppTicks + 1
            if (offAppTicks >= 3) {
                lastVisibleTitle = null
                noReel()
            } else {
                dispatch(tracker.tick(SystemClock.elapsedRealtime()))
            }
            if (tracker.currentKey != null) main.postDelayed(this, TICK_MS) else ticking = false
        }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        val pkg = event?.packageName?.toString() ?: return

        when (pkg) {
            in TIKTOK_PKGS -> { handleTikTok(); return }
            INSTAGRAM_PKG -> { handleInstagram(); return }
            YOUTUBE_PKG -> {}
            else -> return
        }
        val root = rootInActiveWindow ?: return

        val reel = findByIdSuffix(root, REEL_CONTAINER) ?: findByIdSuffix(root, REEL_RECYCLER)
        if (reel == null) {
            if (tracker.currentKey != null || lastVisibleTitle != null) {
                lastVisibleTitle = null
                noReel()
            }
            return
        }

        val labels = collectDescriptions(reel)

        // Detect that the Short CHANGED before trying to identify it. The
        // early returns below (no handle yet, no title yet) fire constantly
        // mid-swipe, and every one of them used to leave the previous video's
        // verdict on screen -- so the badge showed stats for a Short the user
        // had already scrolled past, with nothing signalling it was stale.
        // In v2 the same moment matters more: ending the reel here is what
        // stops recording and drops the previous reel's buffer.
        val visible = pickTitles(labels).firstOrNull()
        if (visible != null && visible != lastVisibleTitle) {
            lastVisibleTitle = visible
            stuckSince = 0L
            val cur = tracker.currentKey
            if (cur != null && !cur.endsWith("|$visible")) noReel()
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
            ?: run { giveUpIfStuck(visible); return }
        val titles = pickTitles(labels)
        if (titles.isEmpty()) { giveUpIfStuck(null); return }
        val title = titles.first()

        // Same key again is just another content-change event; the tracker
        // treats it as a tick. A new key is the swipe.
        val key = "youtube|$handle|$title"
        if (key != tracker.currentKey && !ReelTracker.sameReel(tracker.currentKey, key)) {
            Log.i(TAG, "short: @$handle | $title")
        }
        reel(key, ReelHints("youtube", titles, "@$handle"))
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
            noReel()
            return
        }
        val creator = findByIdSuffix(root, TT_CREATOR)?.let {
            (it.text ?: it.contentDescription)?.toString()
        }?.trim()?.removePrefix("@")?.trim().orEmpty()
        val caption = findByIdSuffix(root, TT_CAPTION)?.let {
            (it.text ?: it.contentDescription)?.toString()
        }?.trim().orEmpty()
        if (creator.isBlank() || caption.length < 10) return

        val key = "tiktok|$creator|$caption"
        if (key != tracker.currentKey) Log.i(TAG, "tiktok: $creator | $caption")
        reel(key, ReelHints("tiktok", listOf(caption), creator))
    }

    /**
     * Instagram Reels. UNVERIFIED: none of this has been checked against a
     * live Instagram view tree yet -- unlike the YouTube and TikTok paths
     * above, which were built from device dumps. Everything here is a
     * structural guess, kept deliberately conservative, plus a debug-only
     * tree dump (BuildConfig.DUMP_VIEW_TREE) so the guesses can be replaced
     * with measured ids on a real phone.
     *
     * Guesses, in order of confidence:
     *  1. Instagram's internal name for Reels is "clips", so a resource id
     *     containing `clips_viewer` / `clips_video` likely marks the player.
     *     UNVERIFIED: the exact ids and whether they survive obfuscation.
     *  2. Structural fallback: a scrollable pager/recycler covering ~the whole
     *     window, with some visible label mentioning "Reel". UNVERIFIED.
     *  3. Identity: an author node (id containing `username`) and a caption
     *     node (id containing `caption`), else the longest non-chrome label
     *     -- the same longest-wins rule that works for YouTube. UNVERIFIED.
     */
    private fun handleInstagram() {
        val root = rootInActiveWindow ?: return
        if (BuildConfig.DUMP_VIEW_TREE) dumpTree(root)

        val container = findByIdContaining(root, IG_CONTAINER_HINTS) ?: findFullScreenPager(root)
        if (container == null) { noReel(); return }
        val labels = collectDescriptions(container)
        // UNVERIFIED structural gate: a full-screen pager alone is also the
        // feed and Stories, so require something that says "reel". Skipped
        // when the id-based match hit, which is the stronger signal.
        if (findByIdContaining(root, IG_CONTAINER_HINTS) == null &&
            labels.none { it.contains("reel", ignoreCase = true) }) { noReel(); return }

        val handle = (findByIdContaining(container, listOf("author_username", "username"))
            ?.let { (it.text ?: it.contentDescription)?.toString() }
            ?: labels.firstNotNullOfOrNull { IG_HANDLE.find(it)?.groupValues?.get(1) })
            ?.trim()?.removePrefix("@")?.trim().orEmpty()
        val caption = (findByIdContaining(container, listOf("caption"))
            ?.let { (it.text ?: it.contentDescription)?.toString() }
            ?: labels.filter { l -> IG_CHROME.none { it.containsMatchIn(l) } && l.length >= 12 }
                .maxByOrNull { it.length })
            ?.trim().orEmpty()
        // Without a caption-ish string two reels by one author share a key,
        // and a swipe between them would not register -- merging their audio.
        // Better to not track at all than to do that.
        if (caption.length < 10) return

        val key = "instagram|$handle|$caption"
        if (key != tracker.currentKey) Log.i(TAG, "instagram: $handle | $caption")
        reel(key, ReelHints("instagram", listOf(caption), handle))
    }

    private var stuckSince = 0L

    /**
     * If we cannot read the channel handle within a couple of seconds, fall
     * back to a title-only key rather than not tracking at all. In v1 the
     * handle was essential (it resolved the video); in v2 the captured audio
     * is the content and the handle is only a cache hint. Without a title
     * there is no stable identity, so a swipe could not be told apart from
     * the same reel -- in that case we track nothing.
     */
    private fun giveUpIfStuck(title: String?) {
        if (tracker.currentKey != null) return    // already tracking this reel
        val now = SystemClock.elapsedRealtime()
        if (stuckSince == 0L) { stuckSince = now; return }
        if (now - stuckSince > 2500) {
            stuckSince = 0L
            if (title != null) reel("youtube||$title", ReelHints("youtube", listOf(title), ""))
        }
    }

    // --- tracker plumbing --------------------------------------------------

    private fun reel(key: String, hints: ReelHints) {
        pendingHints = hints
        offAppTicks = 0
        dispatch(tracker.onReel(key, SystemClock.elapsedRealtime()))
        // The tracker kept an older key for this same reel (a title-only key
        // from giveUpIfStuck, now that the handle has rendered). Pass on the
        // fuller hints without restarting the view.
        tracker.currentKey?.let { cur -> if (cur != key && hints.channel.isNotBlank()) Uploader.updateHints(cur, hints) }
        if (!ticking) { ticking = true; main.postDelayed(ticker, TICK_MS) }
    }

    private fun noReel() = dispatch(tracker.onNoReel(SystemClock.elapsedRealtime()))

    private fun dispatch(events: List<ReelEvent>) {
        for (e in events) when (e) {
            is ReelEvent.Started -> {
                // Recording starts here, but nothing is sent: the buffer stays
                // in memory until DwellReached, and is dropped on Ended.
                CaptureService.instance?.beginReel(e.key)
                Uploader.begin(e.key, pendingHints ?: ReelHints(e.key.substringBefore('|'), emptyList(), ""))
            }
            is ReelEvent.DwellReached -> {
                Uploader.uploadDue(e.key)
                Overlay.showButton(this) { Uploader.onTap(this) }
            }
            is ReelEvent.ChunkDue -> Uploader.uploadDue(e.key)
            is ReelEvent.Ended -> {
                CaptureService.instance?.endReel()
                Uploader.end(e.key)
                Overlay.hide(this)
            }
        }
    }

    override fun onInterrupt() {}

    override fun onDestroy() {
        super.onDestroy()
        main.removeCallbacks(ticker)
        ticking = false
        noReel()
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

    private fun findByIdContaining(node: AccessibilityNodeInfo, parts: List<String>): AccessibilityNodeInfo? {
        node.viewIdResourceName?.let { id -> if (parts.any { id.contains(it) } && node.isVisibleToUser) return node }
        for (i in 0 until node.childCount) {
            val hit = node.getChild(i)?.let { findByIdContaining(it, parts) }
            if (hit != null) return hit
        }
        return null
    }

    /** UNVERIFIED Instagram fallback: a scrollable pager covering >= 90% of the window. */
    private fun findFullScreenPager(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val win = Rect().also(root::getBoundsInScreen)
        if (win.isEmpty) return null
        val b = Rect()
        fun walk(n: AccessibilityNodeInfo): AccessibilityNodeInfo? {
            if (!n.isVisibleToUser) return null
            val cls = n.className?.toString().orEmpty()
            if (n.isScrollable && (cls.endsWith("ViewPager") || cls.endsWith("ViewPager2") ||
                    cls.endsWith("RecyclerView"))) {
                n.getBoundsInScreen(b)
                if (b.width() >= win.width() * 0.9 && b.height() >= win.height() * 0.9) return n
            }
            for (i in 0 until n.childCount) n.getChild(i)?.let(::walk)?.let { return it }
            return null
        }
        return walk(root)
    }

    /**
     * Debug-only: log the Instagram view tree (ids, classes, labels, bounds)
     * so the UNVERIFIED heuristics above can be replaced with what a real
     * build exposes -- the same method that produced the YouTube findings.
     * Throttled; a full dump on every content-change event floods logcat.
     */
    private fun dumpTree(root: AccessibilityNodeInfo) {
        val now = SystemClock.elapsedRealtime()
        if (now - lastTreeDump < 3_000) return
        lastTreeDump = now
        val b = Rect()
        fun walk(n: AccessibilityNodeInfo, depth: Int) {
            if (depth > 40) return
            n.getBoundsInScreen(b)
            val label = (n.text ?: n.contentDescription)?.toString()?.replace('\n', ' ')?.take(80)
            Log.d(TREE_TAG, "${"  ".repeat(depth)}${n.className?.toString()?.substringAfterLast('.')}" +
                " id=${n.viewIdResourceName?.substringAfter(":id/")} vis=${n.isVisibleToUser}" +
                " scroll=${n.isScrollable} $b" + (label?.let { " \"$it\"" } ?: ""))
            for (i in 0 until n.childCount) n.getChild(i)?.let { walk(it, depth + 1) }
        }
        Log.d(TREE_TAG, "---- instagram tree ----")
        walk(root, 0)
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
        val screen = Rect()
        node.getBoundsInScreen(screen)
        val bounds = Rect()
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
        private const val TREE_TAG = "ShortCheckTree"
        private const val TICK_MS = 250L
        private const val YOUTUBE_PKG = "com.google.android.youtube"
        // The TikTok ids below were measured on com.zhiliaoapp.musically.
        // com.ss.android.ugc.trill is the same app built for other regions;
        // that it exposes the same ids is UNVERIFIED.
        private val TIKTOK_PKGS = setOf("com.zhiliaoapp.musically", "com.ss.android.ugc.trill")
        private const val INSTAGRAM_PKG = "com.instagram.android"
        private val WATCHED = TIKTOK_PKGS + YOUTUBE_PKG + INSTAGRAM_PKG

        // UNVERIFIED Instagram heuristics; see handleInstagram().
        private val IG_CONTAINER_HINTS = listOf("clips_viewer", "clips_video")
        private val IG_HANDLE = Regex("""^@?([a-z0-9._]{2,30})$""")
        private val IG_CHROME = listOf(
            Regex("""^(Like|Comment|Share|Send|Save|More|Follow|Following|Reels|Audio|Remix)$""", RegexOption.IGNORE_CASE),
            Regex("""^Original audio""", RegexOption.IGNORE_CASE),
            Regex("""^Profile picture""", RegexOption.IGNORE_CASE),
            Regex("""^Liked by """, RegexOption.IGNORE_CASE),
            Regex("""^View all \d+ comments""", RegexOption.IGNORE_CASE),
            Regex("""^\d[\d,.]*[KkMm]?$"""),
        )
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
