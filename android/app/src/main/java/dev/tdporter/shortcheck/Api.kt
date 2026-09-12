package dev.tdporter.shortcheck

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

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
 * We send only the reel's title and the channel handle -- both already public
 * metadata of a public video. No screenshots, no audio, no other screen
 * content ever leaves the device, which is what keeps an accessibility-service
 * app on the right side of the privacy line.
 */
object Api {

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

        val conn = (URL("${BuildConfig.SERVER}/check").openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            doOutput = true
            connectTimeout = 5_000
            readTimeout = 30_000
            setRequestProperty("Content-Type", "application/json")
        }

        try {
            conn.outputStream.use { it.write(body.toByteArray()) }
            if (conn.responseCode !in 200..299) {
                return@withContext CheckResult.Failed("HTTP ${conn.responseCode}", true)
            }
            val json = JSONObject(conn.inputStream.bufferedReader().readText())
            // The server decides whether our own failures are shown. A release
            // build flips this off so users never see a debug glyph.
            val debug = json.optBoolean("debug", false)

            // "no_transcript" is a real answer, not a failure: ~25% of Shorts
            // have no captions at all. Surface it honestly rather than
            // inventing a verdict from the title.
            when (json.optString("status")) {
                "no_transcript" -> return@withContext CheckResult.Unknown("No captions on this video")
                "ok" -> {}
                else -> return@withContext CheckResult.Failed(
                    json.optString("detail").ifBlank { "server error" }, debug
                )
            }

            val v = json.optJSONObject("verdict")
                ?: return@withContext CheckResult.Failed("missing verdict", debug)
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
            CheckResult.Ok(
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
            CheckResult.Failed("${e.javaClass.simpleName}: ${e.message}", true)
        } finally {
            conn.disconnect()
        }
    }
}
