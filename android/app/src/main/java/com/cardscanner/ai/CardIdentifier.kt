package com.cardscanner.ai

import android.graphics.Bitmap
import android.util.Base64
import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.util.concurrent.TimeUnit

/** What the AI read from a card */
data class CardReading(val name: String, val collectorNumber: String, val setCode: String)

enum class Foil { FOIL, NON_FOIL, UNKNOWN }

/**
 * Tokens used by the requests for one card, as the providers report them. `thinking` is part of
 * `output` (billed as output). `cost` in USD when the provider reports it (OpenRouter).
 */
data class Usage(val input: Int = 0, val output: Int = 0, val thinking: Int = 0, val cost: Double? = null) {
    operator fun plus(o: Usage) = Usage(input + o.input, output + o.output, thinking + o.thinking,
        if (cost == null && o.cost == null) null else (cost ?: 0.0) + (o.cost ?: 0.0))
}

enum class Provider(val id: String, val label: String, val needsKey: Boolean, val models: List<String>) {
    // First model of each list is the default (lists from card_identifier.py; Gemini's default differs)
    // Flash-Lite first: on 10 cards it read the same as Flash for ~1/4 of the cost in half the
    // time (Flash spends ~90% of its output thinking; 2026-09-26: $0.79 vs $3.22 per 1000 cards)
    GEMINI("gemini", "Google Gemini", true, listOf(
        "gemini-flash-lite-latest", "gemini-flash-latest", "gemini-pro-latest",
        "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro")),
    OPENAI("openai", "OpenAI", true, listOf("gpt-4.1-mini", "gpt-4.1", "gpt-4o", "gpt-4o-mini")),
    ANTHROPIC("anthropic", "Anthropic Claude", true, listOf("claude-sonnet-5", "claude-haiku-4-5-20251001", "claude-opus-5-5")),
    // OpenRouter: one key for many providers' models; ":free" models cost nothing (rate limited)
    OPENROUTER("openrouter", "OpenRouter", true, listOf(
        "qwen/qwen3.8-27b:free", "google/gemma-4-31b-it:free", "google/gemma-4-26b-a4b-it:free",
        "google/gemini-flash-latest", "openai/gpt-4.1-mini")),
    LOCAL("local", "Local server (Ollama)", false, listOf("qwen3-vl:4b", "qwen3-vl:8b", "llava:7b", "moondream"));

    companion object {
        fun of(id: String?) = entries.firstOrNull { it.id == id } ?: GEMINI
    }
}

/**
 * Identifies Magic cards with a vision AI - a port of card_identifier.py (providers, parser,
 * foil marker check). `localUrl` is the local server's base URL (http://host:11434).
 */
class CardIdentifier(
    private val provider: Provider,
    private val model: String,
    private val apiKey: String?,
    localUrl: String,
    private val http: OkHttpClient,
) {
    /** Tokens used by this identifier's requests so far (one identifier serves one card) */
    var usage = Usage()
        private set

    private val localBase = localUrl.trim().trimEnd('/').removeSuffix("/v1/chat/completions").trimEnd('/')

    /** Identify a card from its perspective-corrected image; returns (raw answer, reading) */
    fun identify(card: Bitmap, maxDimension: Int = 1024): Pair<String, CardReading?> {
        val answer = ask(toBase64(card, maxDimension), Prompts.identify, maxTokens = 100)
        return answer to parseResponse(answer)
    }

    /**
     * Check the star/dot foil marker in the bottom-left corner of a flat, portrait, tightly
     * cropped card image.
     */
    fun readFoilSymbol(card: Bitmap): Foil {
        // Bottom 14% x left 45%: tall enough to still hold the set line when the detected
        // outline also takes in the edge of the card underneath
        val top = (card.height * 0.86).toInt()
        val corner = Bitmap.createBitmap(card, 0, top, (card.width * 0.45).toInt(), card.height - top)
        val zoomed = Bitmap.createScaledBitmap(corner, corner.width * 3, corner.height * 3, true)
        val answer = try {
            ask(toBase64(zoomed, 2048), Prompts.foil, maxTokens = 10).lowercase()
        } catch (e: Exception) {
            Log.w(TAG, "Foil check failed: $e")
            return Foil.UNKNOWN
        }
        return when {
            "star" in answer -> Foil.FOIL
            "dot" in answer -> Foil.NON_FOIL
            else -> Foil.UNKNOWN
        }
    }

    private fun toBase64(image: Bitmap, maxDimension: Int): String {
        var bitmap = image
        val longest = maxOf(image.width, image.height)
        if (longest > maxDimension) {
            val scale = maxDimension.toDouble() / longest
            bitmap = Bitmap.createScaledBitmap(image, (image.width * scale).toInt(), (image.height * scale).toInt(), true)
        }
        val buffer = ByteArrayOutputStream()
        // High quality: the collector number and set code are small print
        bitmap.compress(Bitmap.CompressFormat.JPEG, 95, buffer)
        return Base64.encodeToString(buffer.toByteArray(), Base64.NO_WRAP)
    }

    // ------------------------------------------------------------------------
    // Provider requests: send one image + prompt, return the response text
    // ------------------------------------------------------------------------

    private fun ask(image: String, prompt: String, maxTokens: Int): String = when (provider) {
        Provider.GEMINI -> askGemini(image, prompt)
        Provider.OPENAI -> askOpenAi(image, prompt, maxTokens)
        Provider.ANTHROPIC -> askAnthropic(image, prompt, maxTokens)
        Provider.OPENROUTER -> askOpenRouter(image, prompt, maxTokens)
        Provider.LOCAL -> askLocal(image, prompt, maxTokens)
    }

    /** Add the token counts of an OpenAI-style response (OpenAI, OpenRouter, OpenAI-compatible servers) */
    private fun countOpenAiUsage(json: JSONObject) {
        val u = json.optJSONObject("usage") ?: return
        usage += Usage(u.optInt("prompt_tokens"), u.optInt("completion_tokens"),
            u.optJSONObject("completion_tokens_details")?.optInt("reasoning_tokens") ?: 0,
            if (u.has("cost")) u.optDouble("cost") else null)
    }

    private fun post(url: String, body: JSONObject, headers: Map<String, String> = emptyMap()): JSONObject {
        val request = Request.Builder().url(url).post(body.toString().toRequestBody(JSON))
        headers.forEach { (name, value) -> request.header(name, value) }
        http.newCall(request.build()).execute().use { response ->
            val text = response.body.string()
            if (!response.isSuccessful) throw IOException("HTTP ${response.code}: ${text.take(300)}")
            return JSONObject(text)
        }
    }

    private fun requireKey(): String = apiKey?.takeIf { it.isNotBlank() }
        ?: throw IOException("No API key for ${provider.label} - add it in Settings")

    private fun askGemini(image: String, prompt: String): String {
        val body = JSONObject().put("contents", JSONArray().put(JSONObject().put("parts", JSONArray()
            .put(JSONObject().put("text", prompt))
            .put(JSONObject().put("inline_data", JSONObject().put("mime_type", "image/jpeg").put("data", image))))))
        val json = post("https://generativelanguage.googleapis.com/v1beta/models/$model:generateContent", body,
            mapOf("x-goog-api-key" to requireKey()))
        json.optJSONObject("usageMetadata")?.let { u ->
            // candidatesTokenCount excludes the thinking; both are billed as output
            val thoughts = u.optInt("thoughtsTokenCount")
            usage += Usage(u.optInt("promptTokenCount"), u.optInt("candidatesTokenCount") + thoughts, thoughts)
        }
        val parts = json.getJSONArray("candidates").getJSONObject(0).getJSONObject("content").getJSONArray("parts")
        return (0 until parts.length()).joinToString("") { parts.getJSONObject(it).optString("text") }.trim()
    }

    private fun openAiMessages(image: String, prompt: String) = JSONArray().put(JSONObject()
        .put("role", "user")
        .put("content", JSONArray()
            .put(JSONObject().put("type", "text").put("text", prompt))
            .put(JSONObject().put("type", "image_url").put("image_url", JSONObject().put("url", "data:image/jpeg;base64,$image")))))

    private fun askOpenAi(image: String, prompt: String, maxTokens: Int): String {
        val body = JSONObject().put("model", model).put("messages", openAiMessages(image, prompt)).put("max_tokens", maxTokens)
        val json = post("https://api.openai.com/v1/chat/completions", body, mapOf("Authorization" to "Bearer ${requireKey()}"))
        countOpenAiUsage(json)
        return json.getJSONArray("choices").getJSONObject(0).getJSONObject("message").getString("content").trim()
    }

    private fun askAnthropic(image: String, prompt: String, maxTokens: Int): String {
        val body = JSONObject().put("model", model).put("max_tokens", maxTokens).put("messages", JSONArray().put(JSONObject()
            .put("role", "user")
            .put("content", JSONArray()
                .put(JSONObject().put("type", "image").put("source", JSONObject()
                    .put("type", "base64").put("media_type", "image/jpeg").put("data", image)))
                .put(JSONObject().put("type", "text").put("text", prompt)))))
        val json = post("https://api.anthropic.com/v1/messages", body,
            mapOf("x-api-key" to requireKey(), "anthropic-version" to "2023-06-01"))
        json.optJSONObject("usage")?.let { usage += Usage(it.optInt("input_tokens"), it.optInt("output_tokens")) }
        return json.getJSONArray("content").getJSONObject(0).getString("text").trim()
    }

    /**
     * OpenRouter (OpenAI-compatible). Reasoning off: reasoning tokens count against max_tokens
     * and would leave an empty answer; the budget still leaves room for models that reason anyway.
     */
    private fun askOpenRouter(image: String, prompt: String, maxTokens: Int): String {
        val body = JSONObject().put("model", model).put("messages", openAiMessages(image, prompt))
            .put("max_tokens", maxOf(maxTokens, 1000))
            .put("temperature", 0)
            .put("reasoning", JSONObject().put("effort", "none"))
            .put("usage", JSONObject().put("include", true))  // report the cost
        val json = post("https://openrouter.ai/api/v1/chat/completions", body, mapOf(
            "Authorization" to "Bearer ${requireKey()}",
            "X-Title" to "Card Scanner",  // app name in OpenRouter's activity list
        ))
        json.optJSONObject("error")?.let { throw IOException("OpenRouter: ${it.optString("message")}") }
        countOpenAiUsage(json)
        return json.getJSONArray("choices").getJSONObject(0).getJSONObject("message").optString("content").trim()
    }

    /** Local server: Ollama's native /api/chat first, then the OpenAI-compatible endpoint (vLLM, LM Studio) */
    private fun askLocal(image: String, prompt: String, maxTokens: Int): String {
        if (localBase.isBlank()) throw IOException("No local AI server address - set it in Settings")
        val native = try {
            val body = JSONObject()
                .put("model", model)
                .put("messages", JSONArray().put(JSONObject().put("role", "user").put("content", prompt).put("images", JSONArray().put(image))))
                .put("stream", false)
                // Thinking models otherwise spend the token budget reasoning and return no answer
                .put("think", false)
                .put("keep_alive", OLLAMA_KEEP_ALIVE)
                // num_predict caps the answer: a model reasoning aloud would write hundreds of tokens
                .put("options", JSONObject().put("temperature", 0).put("num_predict", maxOf(maxTokens, 50)))
            val json = post("$localBase/api/chat", body)
            usage += Usage(json.optInt("prompt_eval_count"), json.optInt("eval_count"))
            json.getJSONObject("message").getString("content").trim()
        } catch (e: Exception) {
            Log.w(TAG, "Ollama native API failed, trying the OpenAI-compatible endpoint: $e")
            null
        }
        if (!native.isNullOrBlank()) return native
        val body = JSONObject().put("model", model).put("messages", openAiMessages(image, prompt))
            .put("max_tokens", maxOf(maxTokens, 150)).put("temperature", 0.1)
        return post("$localBase/v1/chat/completions", body).also { countOpenAiUsage(it) }
            .getJSONArray("choices").getJSONObject(0).getJSONObject("message").getString("content").trim()
    }

    /** Preload the local model so the first card isn't slowed down by loading it (Ollama unloads idle models) */
    fun warmUp() {
        if (provider != Provider.LOCAL || localBase.isBlank()) return
        try {
            // An empty message list just loads the model
            post("$localBase/api/chat", JSONObject().put("model", model).put("messages", JSONArray()).put("keep_alive", OLLAMA_KEEP_ALIVE))
        } catch (e: Exception) {
            Log.w(TAG, "Could not preload local model: $e")
        }
    }

    companion object {
        private const val TAG = "CardIdentifier"
        private val JSON = "application/json".toMediaType()
        // How long Ollama keeps the model loaded after a request (its default is 5 minutes)
        private const val OLLAMA_KEEP_ALIVE = "30m"

        fun httpClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .build()

        /** OpenRouter models that read images (public list), free ones first */
        fun openRouterModels(http: OkHttpClient): List<String> {
            http.newCall(Request.Builder().url("https://openrouter.ai/api/v1/models").build()).execute().use { response ->
                if (!response.isSuccessful) throw IOException("HTTP ${response.code}")
                val data = JSONObject(response.body.string()).getJSONArray("data")
                val models = (0 until data.length()).map { data.getJSONObject(it) }.filter { m ->
                    val inputs = m.optJSONObject("architecture")?.optJSONArray("input_modalities")
                    inputs != null && (0 until inputs.length()).any { inputs.getString(it) == "image" }
                }
                fun free(m: JSONObject) = m.optJSONObject("pricing")?.let {
                    it.optString("prompt").toDoubleOrNull() == 0.0 && it.optString("completion").toDoubleOrNull() == 0.0
                } ?: false
                return models.sortedWith(compareBy({ !free(it) }, { it.getString("id") })).map { it.getString("id") }
            }
        }

        /** Models installed on a local Ollama server (GET /api/tags) */
        fun localModels(localUrl: String, http: OkHttpClient): List<String> {
            val base = localUrl.trim().trimEnd('/').removeSuffix("/v1/chat/completions").trimEnd('/')
            http.newCall(Request.Builder().url("$base/api/tags").build()).execute().use { response ->
                if (!response.isSuccessful) throw IOException("HTTP ${response.code}")
                val models = JSONObject(response.body.string()).getJSONArray("models")
                return (0 until models.length()).map { models.getJSONObject(it).getString("name") }.sorted()
            }
        }

        private val LABEL_LINE = Regex("""(NAME|NUMBER|SET)\s*:""", RegexOption.IGNORE_CASE)
        private val NUMBER_WITH_TOTAL = Regex("""[A-Za-z]{0,4}\d{1,4}\s*/\s*[A-Za-z]{0,4}\d{1,4}""")
        private val SET_CODE = Regex("""([A-Za-z0-9]{2,5})(?:\s+[A-Za-z]{2})?""")

        /**
         * Parse a 'NAME: ... / NUMBER: ... / SET: ...' answer from any provider (a port of
         * card_identifier.py:_parse_response). Returns null if no name was found.
         */
        fun parseResponse(responseText: String): CardReading? {
            // Some models answer in Markdown with comments: "- **NAME**: Riolu (The card is ...)"
            val text = responseText.replace("\r\n", "\n").replace("**", "").replace("__", "")
            val values = HashMap<String, String>()
            for (label in listOf("NAME", "NUMBER", "SET")) {
                Regex("""$label:\s*(.+?)(?:\n|$)""", RegexOption.IGNORE_CASE).find(text)?.let {
                    values[label] = cleanValue(it.groupValues[1])
                }
            }

            // Some models drop the labels (all of them, or only the first one), and sometimes a
            // line: "Mirkwood / 0188 / HOB", "Mirkwood / NUMBER: 0188 / SET: HOB", "Mirkwood / HOB"
            if ("NAME" !in values) {
                val lines = text.lines().map { it.trim() }.filter { it.isNotEmpty() }
                val bare = lines.filter { !LABEL_LINE.matchesAt(it, 0) }
                if (bare.isNotEmpty() && lines.size <= 3) {
                    values["NAME"] = cleanValue(bare[0])
                    for (raw in bare.drop(1)) {
                        val line = cleanValue(raw)
                        if ("NUMBER" !in values && looksLikeNumber(line)) values["NUMBER"] = line
                        else if ("SET" !in values && Regex("[A-Za-z0-9]{2,5}").matches(line)) values["SET"] = line
                    }
                }
            }

            var name = values["NAME"].orEmpty()
            val number = values["NUMBER"].orEmpty()
            val withTotal = NUMBER_WITH_TOTAL.find(number)
            var collectorNumber = cleanNumber(withTotal?.value?.replace(" ", "") ?: number)
            // Set codes are 2-5 letters/digits ("HOB", "M21", "PLST"), maybe followed by the
            // language code ("PAL EN"); anything else (e.g. "Unknown") is ignored
            val setCode = SET_CODE.matchEntire(values["SET"].orEmpty())?.groupValues?.get(1)?.uppercase().orEmpty()

            if (name.equals("unknown", ignoreCase = true)) name = ""
            if (collectorNumber.equals("unknown", ignoreCase = true)) collectorNumber = ""
            return if (name.isNotEmpty()) CardReading(name, collectorNumber, setCode) else null
        }

        /** 'Riolu (The card ...)' -> 'Riolu', '103/202 ★' -> '103/202' */
        private fun cleanValue(value: String): String =
            value.trim().replace(Regex("""\s+\(.*$"""), "").trim(' ', '\t', '"', '\'', '.', '*', '★', '•', '☆')

        /** A collector number, maybe with its rarity letter and misread digits ("L 018B"), or "016/131" */
        private fun looksLikeNumber(text: String): Boolean {
            val t = text.trim()
            if (Regex("""[A-Za-z]{0,4}\d{1,4}\s*/\s*[A-Za-z]{0,4}\d{1,4}""").matches(t)) return true
            val match = Regex("""(?:[A-Za-z]\s+)?([0-9OoDBIlSZ]{1,4})[a-z]?""").matchEntire(t) ?: return false
            val core = match.groupValues[1]
            return core.all { it.isDigit() } || (core.length >= 3 && core.count { it.isDigit() } >= 2)
        }

        // Letters a model reads for digits in small print: "018B" is 0188
        private val DIGIT_LOOKALIKES = mapOf('O' to '0', 'o' to '0', 'D' to '0', 'B' to '8', 'I' to '1', 'l' to '1', 'S' to '5', 'Z' to '2')

        /** Fix letters read for digits in a mostly-digit collector number ("018B" -> "0188") */
        private fun cleanNumber(text: String): String =
            Regex("""\b[0-9OoDBIlSZ]{3,4}\b""").replace(text) { m ->
                if (m.value.count { it.isDigit() } >= 2) m.value.map { DIGIT_LOOKALIKES[it] ?: it }.joinToString("") else m.value
            }
    }
}
