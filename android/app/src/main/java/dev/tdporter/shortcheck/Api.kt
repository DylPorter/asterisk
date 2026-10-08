package dev.tdporter.shortcheck

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

data class Claim(val claim: String, val rating: String, val note: String, val sources: List<String>)

data class Verdict(
    val score: Int,
    val overall: String,
    val headline: String,
    val statementsChecked: Int,
    val statementsWrong: Int,
    val factualPct: Int,
    val claims: List<Claim>,
    val skipped: Int,
    val cached: Boolean,
    val seconds: Double,
)

/**
 * Three outcomes, deliberately distinct.
 *
 * "Can't check" (no captions, unresolvable) is a normal, expected answer for
 * roughly a quarter of Shorts and the user should see it. "Error" is our bug,
 * and it is hidden behind a server-controlled flag so a shipped build never
 * shows a purple "!" to someone who cannot act on it.
 */
sealed interface CheckResult {
    data class Ok(val verdict: Verdict) : CheckResult
    data class Unknown(val why: String) : CheckResult
    data class Failed(val detail: String, val showToUser: Boolean) : CheckResult
}

/**
 * Talks to the fact-check server over Tailscale.
 *
 * v1 (`/check`) sends only the reel's title and the channel handle -- both
 * already public metadata of a public video. It is kept as the YouTube
 * fallback for when captured audio is silent.
 *
 * v2 (`/v2/segments`, `/v2/check`) additionally sends the audio the target app
 * was playing, in 5 s WAV chunks, and only after the dwell gate. Never mic
 * audio, never screen pixels. `session_id` is a fresh UUID per reel view, so
 * nothing ties one view to the next.
 */
object Api {

    /**
     * Upload one chunk. The server starts transcribing it immediately, which
     * is the whole latency win: by the time the user taps, most of the
     * transcript already exists. Returns false on any failure; the caller
     * reuses the seq so the server never waits on a hole.
     */
    suspend fun postSegment(sessionId: String, seq: Int, app: String, wav: ByteArray): Boolean =
        withContext(Dispatchers.IO) {
            val q = "session_id=${enc(sessionId)}&seq=$seq&app=${enc(app)}"
            val conn = (URL("${BuildConfig.SERVER}/v2/segments?$q").openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                doOutput = true
                connectTimeout = 5_000
                readTimeout = 15_000
                setRequestProperty("Content-Type", "audio/wav")
                setFixedLengthStreamingMode(wav.size)
            }
            try {
                conn.outputStream.use { it.write(wav) }
                val ok = conn.responseCode in 200..299
                // Drain so the keep-alive connection can be reused.
                runCatching { (if (ok) conn.inputStream else conn.errorStream)?.use { it.readBytes() } }
                ok
            } catch (e: Exception) {
                false
            } finally {
                conn.disconnect()
            }
        }

    /** Fact-check a captured session. Titles/channel are hints for the cache key only. */
    suspend fun checkV2(sessionId: String, app: String, titles: List<String>, channel: String,
                        finalSeq: Int, deadlineS: Double = 25.0): CheckResult = withContext(Dispatchers.IO) {
        val body = JSONObject()
            .put("session_id", sessionId)
            .put("app", app)
            .put("titles", JSONArray(titles))
            .put("channel", channel)
            .put("final_seq", finalSeq)
            .put("deadline_s", deadlineS)
            .toString()
        postJson("/v2/check", body, readTimeoutMs = ((deadlineS + 10) * 1000).toInt(), v2 = true)
    }

    private fun enc(s: String) = URLEncoder.encode(s, "UTF-8")

    /** v1: identify the video by title + channel, fact-check its captions. */
    suspend fun check(titles: List<String>, handle: String,
                      platform: String = "youtube"): CheckResult = withContext(Dispatchers.IO) {
        // Several candidates, best guess first. The server tries each against
        // the channel until one resolves, so a heuristic miss costs one extra
        // lookup instead of failing the whole check.
        val body = JSONObject()
            .put("titles", JSONArray(titles))
            .put("channel", handle)
            .put("platform", platform)
            .put("deadline_s", 20.0)
            .toString()
        postJson("/check", body, readTimeoutMs = 30_000, v2 = false)
    }

    private fun postJson(path: String, body: String, readTimeoutMs: Int, v2: Boolean): CheckResult {
        val conn = (URL("${BuildConfig.SERVER}$path").openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            doOutput = true
            connectTimeout = 5_000
            readTimeout = readTimeoutMs
            setRequestProperty("Content-Type", "application/json")
        }

        try {
            conn.outputStream.use { it.write(body.toByteArray()) }
            if (conn.responseCode !in 200..299) {
                return CheckResult.Failed("HTTP ${conn.responseCode}", true)
            }
            val json = JSONObject(conn.inputStream.bufferedReader().readText())
            // The server decides whether our own failures are shown. A release
            // build flips this off so users never see a debug glyph.
            val debug = json.optBoolean("debug", false)

            // "no_transcript" is a real answer, not a failure: ~25% of Shorts
            // have no captions at all. Surface it honestly rather than
            // inventing a verdict from the title.
            when (json.optString("status")) {
                // v1 depends on captions; v2 on speech in the captured audio.
                // /v2/check can itself answer through v1 (source=v1_fallback,
                // when the server found the session silent or expired), and
                // then no_transcript means no captions, not no speech.
                "no_transcript" -> return CheckResult.Unknown(
                    if (v2 && json.optString("source") != "v1_fallback") "No speech in this video"
                    else "No captions on this video"
                )
                // The server's silent-session answer for non-YouTube apps: the
                // app blocked playback capture and there is no v1 path for it.
                "captured_silent", "cant_check" -> return CheckResult.Unknown(
                    json.optString("detail").ifBlank { "Can't check this video" }
                )
                "ok" -> {}
                else -> return CheckResult.Failed(
                    json.optString("detail").ifBlank { "server error" }, debug
                )
            }

            val v = json.optJSONObject("verdict")
                ?: return CheckResult.Failed("missing verdict", debug)
            val arr = v.optJSONArray("claims")
            val claims = buildList {
                for (i in 0 until (arr?.length() ?: 0)) {
                    val c = arr!!.getJSONObject(i)
                    val srcArr = c.optJSONArray("sources")
                    add(
                        Claim(
                            claim = c.optString("claim"),
                            rating = c.optString("rating"),
                            note = c.optString("note"),
                            sources = buildList {
                                for (j in 0 until (srcArr?.length() ?: 0)) add(srcArr!!.getString(j))
                            },
                        )
                    )
                }
            }
            return CheckResult.Ok(
                Verdict(
                    score = v.optInt("score", 3).coerceIn(1, 5),
                    overall = v.optString("overall", "unverifiable"),
                    headline = v.optString("headline"),
                    statementsChecked = v.optInt("statements_checked", claims.size),
                    statementsWrong = v.optInt("statements_wrong", 0),
                    factualPct = v.optInt("factual_pct", -1),
                    claims = claims,
                    skipped = v.optInt("skipped", 0),
                    cached = json.optBoolean("cached", false),
                    seconds = json.optDouble("seconds", 0.0),
                )
            )
        } catch (e: Exception) {
            return CheckResult.Failed("${e.javaClass.simpleName}: ${e.message}", true)
        } finally {
            conn.disconnect()
        }
    }
}
