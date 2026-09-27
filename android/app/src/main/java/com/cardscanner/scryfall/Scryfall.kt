package com.cardscanner.scryfall

import android.util.Log
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.IOException
import java.text.Normalizer

/** One printing of a card, as Scryfall describes it */
data class Printing(
    /** Scryfall's id of the printing */
    val id: String,
    val name: String,
    val setCode: String,
    val setName: String,
    val collectorNumber: String,
    val rarity: String,
    val typeLine: String,
    val manaCost: String,
    /** Color letters (W, U, B, R, G); empty for colorless */
    val colors: List<String>,
    val finishes: List<String>,
    /** Surge foil printings (promo_types "surgefoil"): their foil finish is a surge foil */
    val surgeFoil: Boolean,
    val imageUrl: String?,
    val scryfallUrl: String,
    val usd: String?,
    val usdFoil: String?,
    val eur: String?,
    val eurFoil: String?,
    /** How the printing was found - see Scryfall.CONFIRMED */
    val match: String,
) {
    val confirmed get() = match in Scryfall.CONFIRMED
}

/**
 * Finds the exact printing on the Scryfall API - the online counterpart of
 * database.py:search_card_exact (the app has no local card database yet).
 *
 * Order: set code + collector number (checked against the name), name + collector number,
 * name + set, name only. Results are tagged like the Python search (`match`).
 */
class Scryfall(private val http: OkHttpClient) {

    fun find(name: String, collectorNumber: String, setCode: String): Printing? {
        val numbers = numberVariants(collectorNumber)
        val set = setCode.lowercase()

        // 1. Set code + collector number identify the printing exactly
        var setNumberCard: JSONObject? = null
        if (set.isNotEmpty() && numbers.isNotEmpty()) {
            setNumberCard = numbers.firstNotNullOfOrNull { get("/cards/$set/$it") }
            if (setNumberCard != null && namesMatch(name, setNumberCard)) return printing(setNumberCard, "set_number")
            if (setNumberCard != null) Log.w(TAG, "$setCode #$collectorNumber is '${setNumberCard.optString("name")}', not '$name' - searching by name")
        }

        // The card's real name: exact, or Scryfall's fuzzy match for a misread name
        val named = get("/cards/named", "fuzzy" to name)
            ?: return setNumberCard?.let { printing(it, "set_number_unverified") }
        val exactName = named.getString("name")

        // 2. Name + collector number
        if (numbers.isNotEmpty()) {
            search("!\"$exactName\" cn:${numbers.first()}")?.let { return printing(it, "name_number") }
        }
        // 3. Name + set (a number read one digit off: the closest number in that set)
        if (set.isNotEmpty()) {
            get("/cards/named", "exact" to exactName, "set" to set)?.let { inSet ->
                val wanted = numbers.firstOrNull()?.toIntOrNull()
                val found = leadingNumber(inSet.optString("collector_number"))
                val oneDigitOff = wanted != null && found != null && wanted != found && digitsDifferByOne(wanted, found)
                return printing(inSet, if (oneDigitOff) "name_set_digit" else "name_set")
            }
        }
        // 4. Name only (Scryfall's default printing)
        return printing(named, "name")
    }

    /** A printing by its Scryfall id (a review item's suggestion) */
    fun byId(id: String): Printing? = get("/cards/$id")?.let { printing(it, "chosen") }

    /**
     * Printings to choose from in a review: set + number gives that printing; otherwise all
     * printings of the name (Scryfall's fuzzy match for a misread name), newest first, those of
     * the set (if given) first.
     */
    fun printings(name: String, setCode: String = "", number: String = ""): List<Printing> {
        val set = setCode.trim().lowercase()
        val numbers = numberVariants(number)
        if (set.isNotEmpty() && numbers.isNotEmpty()) {
            numbers.firstNotNullOfOrNull { get("/cards/$set/$it") }?.let { card ->
                if (name.isBlank() || namesMatch(name, card)) return listOf(printing(card, "chosen"))
            }
        }
        if (name.isBlank()) return emptyList()
        val exactName = get("/cards/named", "fuzzy" to name)?.getString("name") ?: return emptyList()
        val found = get("/cards/search", "q" to "!\"$exactName\"", "unique" to "prints", "include_extras" to "true", "order" to "released")
            ?.optJSONArray("data") ?: return emptyList()
        val all = (0 until found.length()).map { printing(found.getJSONObject(it), "chosen") }
        return all.sortedBy { if (set.isNotEmpty() && it.setCode.equals(set, ignoreCase = true)) 0 else 1 }
    }

    private fun get(path: String, vararg query: Pair<String, String>): JSONObject? {
        val url = "$API$path".toHttpUrl().newBuilder().apply { query.forEach { addQueryParameter(it.first, it.second) } }.build()
        // Scryfall asks for a User-Agent and an Accept header on every request
        val request = Request.Builder().url(url).header("User-Agent", USER_AGENT).header("Accept", "application/json").build()
        http.newCall(request).execute().use { response ->
            if (response.code == 404) return null
            if (!response.isSuccessful) throw IOException("Scryfall: HTTP ${response.code}")
            return JSONObject(response.body.string())
        }
    }

    private fun search(query: String): JSONObject? =
        get("/cards/search", "q" to query, "unique" to "prints", "include_extras" to "true")
            ?.optJSONArray("data")?.optJSONObject(0)

    private fun printing(card: JSONObject, match: String): Printing {
        val images = card.optJSONObject("image_uris")
            ?: card.optJSONArray("card_faces")?.optJSONObject(0)?.optJSONObject("image_uris")
        val prices = card.optJSONObject("prices")
        val finishes = card.optJSONArray("finishes")?.let { list -> (0 until list.length()).map { list.getString(it) } }.orEmpty()
        fun price(key: String) = prices?.optString(key)?.takeIf { it.isNotEmpty() && it != "null" }
        fun strings(array: org.json.JSONArray?) = array?.let { list -> (0 until list.length()).map { list.getString(it) } }.orEmpty()
        val faces = card.optJSONArray("card_faces")?.let { list -> (0 until list.length()).map { list.getJSONObject(it) } }.orEmpty()
        // Double-faced cards keep mana cost and colors on their faces
        fun faced(key: String) = card.optString(key).ifEmpty { faces.map { it.optString(key) }.filter { it.isNotEmpty() }.joinToString(" // ") }
        return Printing(
            id = card.optString("id"),
            name = card.getString("name"),
            setCode = card.optString("set").uppercase(),
            setName = card.optString("set_name"),
            collectorNumber = card.optString("collector_number"),
            rarity = card.optString("rarity"),
            typeLine = faced("type_line"),
            manaCost = faced("mana_cost"),
            colors = if (card.has("colors")) strings(card.optJSONArray("colors")) else strings(faces.firstOrNull()?.optJSONArray("colors")),
            finishes = finishes,
            surgeFoil = "surgefoil" in strings(card.optJSONArray("promo_types")),
            imageUrl = images?.optString("normal"),
            scryfallUrl = card.optString("scryfall_uri"),
            usd = price("usd"), usdFoil = price("usd_foil"), eur = price("eur"), eurFoil = price("eur_foil"),
            match = match,
        )
    }

    companion object {
        private const val TAG = "Scryfall"
        private const val API = "https://api.scryfall.com"
        const val USER_AGENT = "CardScanner-Android/0.1"

        /** Matches that identify the printing for sure (database.py:CONFIRMED_MATCHES) */
        val CONFIRMED = setOf("set_number", "name_number", "name_set_digit")

        /** "0330" -> ["330", "0330", "330s"]: numbers a scanned number may be in Scryfall data */
        fun numberVariants(collectorNumber: String): List<String> {
            val digits = Regex("""\d+""").find(collectorNumber)?.value ?: return emptyList()
            val base = digits.trimStart('0').ifEmpty { "0" }
            return listOf(base, digits, base + "s").distinct()
        }

        private fun leadingNumber(number: String) = Regex("""\d+""").find(number)?.value?.toIntOrNull()

        private fun digitsDifferByOne(a: Int, b: Int): Boolean {
            val x = a.toString(); val y = b.toString()
            return x.length == y.length && x.indices.count { x[it] != y[it] } == 1
        }

        /** Lowercase, no accents ("Fíli" -> "fili", "Æther" -> "aether") */
        fun searchKey(text: String?): String? {
            if (text.isNullOrBlank()) return null
            val decomposed = Normalizer.normalize(text, Normalizer.Form.NFKD).replace(Regex("""\p{M}+"""), "")
            return decomposed.lowercase().replace("æ", "ae").trim()
        }

        /**
         * Loose check that a name read from a card matches a card: same name, a shortened
         * legendary name ("Thanos" / "Thanos, the Mad Titan"), a double-faced card's front face,
         * or a close spelling. Checks the flavor name too. (database.py:names_match)
         */
        fun namesMatch(query: String, card: JSONObject): Boolean {
            val queryKey = searchKey(query) ?: return false
            for (fullName in listOf(card.optString("name"), card.optString("flavor_name"))) {
                for (face in fullName.split(" // ")) {
                    val key = searchKey(face) ?: continue
                    if (key == queryKey || key.startsWith(queryKey) || queryKey.startsWith(key)) return true
                    if (similarity(queryKey, key) >= 0.6) return true
                    // A misread short name ("Thands") against a legendary name ("Thanos, the Mad Titan")
                    val shortName = key.split(",")[0]
                    if (shortName != key && similarity(queryKey.split(",")[0], shortName) >= 0.75) return true
                }
            }
            return false
        }

        /** Python's difflib.SequenceMatcher(None, a, b).ratio(): 2 * matching characters / total length */
        fun similarity(a: String, b: String): Double {
            if (a.isEmpty() && b.isEmpty()) return 1.0
            fun matches(aLo: Int, aHi: Int, bLo: Int, bHi: Int): Int {
                // Longest common substring in a[aLo:aHi], b[bLo:bHi] (earliest in a, then in b)
                var bestI = aLo; var bestJ = bLo; var bestSize = 0
                var lengths = IntArray(bHi - bLo + 1)
                for (i in aLo until aHi) {
                    val next = IntArray(bHi - bLo + 1)
                    for (j in bLo until bHi) {
                        if (a[i] == b[j]) {
                            val size = lengths[j - bLo] + 1
                            next[j - bLo + 1] = size
                            if (size > bestSize) { bestI = i - size + 1; bestJ = j - size + 1; bestSize = size }
                        }
                    }
                    lengths = next
                }
                if (bestSize == 0) return 0
                return bestSize + matches(aLo, bestI, bLo, bestJ) + matches(bestI + bestSize, aHi, bestJ + bestSize, bHi)
            }
            return 2.0 * matches(0, a.length, 0, b.length) / (a.length + b.length)
        }
    }
}
