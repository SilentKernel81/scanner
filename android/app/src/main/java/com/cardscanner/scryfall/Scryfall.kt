package com.cardscanner.scryfall

import android.util.Log
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
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
class Scryfall(private val http: OkHttpClient, private val local: CardDatabase? = null) {

    private val offline get() = local?.takeIf { it.ready }

    /**
     * The printing of a card read by the AI: from the offline card data when downloaded (a card
     * missing there - newer than the data - is looked up online), else online
     */
    fun find(name: String, collectorNumber: String, setCode: String): Printing? {
        offline?.let { db -> findOffline(db, name, collectorNumber, setCode)?.let { return it } }
        return findOnline(name, collectorNumber, setCode)
    }

    /** find() on the offline data: the same steps and match tags as online */
    private fun findOffline(db: CardDatabase, name: String, collectorNumber: String, setCode: String): Printing? {
        val numbers = numberVariants(collectorNumber)
        var setNumber: Printing? = null
        if (setCode.isNotEmpty() && numbers.isNotEmpty()) {
            setNumber = db.bySetNumber(setCode, numbers)
            if (setNumber != null && namesMatch(name, listOf(setNumber.name))) return setNumber.copy(match = "set_number")
        }
        val exactName = db.exactName(name) ?: return setNumber?.copy(match = "set_number_unverified")
        val all = db.printingsOf(exactName)
        if (numbers.isNotEmpty()) {
            all.firstOrNull { it.collectorNumber in numbers }?.let { return it.copy(match = "name_number") }
        }
        if (setCode.isNotEmpty()) {
            val inSet = all.filter { it.setCode.equals(setCode, true) }
            if (inSet.isNotEmpty()) {
                val wanted = numbers.firstOrNull()?.toIntOrNull()
                val oneOff = wanted?.let { w -> inSet.firstOrNull { p -> leadingNumber(p.collectorNumber)?.let { it != w && digitsDifferByOne(w, it) } == true } }
                return oneOff?.copy(match = "name_set_digit") ?: inSet.first().copy(match = "name_set")
            }
        }
        return all.firstOrNull()?.copy(match = "name")
    }

    private fun findOnline(name: String, collectorNumber: String, setCode: String): Printing? {
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
    fun byId(id: String): Printing? =
        offline?.byId(id)?.copy(match = "chosen") ?: get("/cards/$id")?.let { printing(it, "chosen") }

    /**
     * Printings to choose from in a review: set + number gives that printing; otherwise all
     * printings of the name (Scryfall's fuzzy match for a misread name), newest first, those of
     * the set (if given) first.
     */
    fun printings(name: String, setCode: String = "", number: String = ""): List<Printing> {
        val set = setCode.trim().lowercase()
        val numbers = numberVariants(number)
        offline?.let { db ->
            if (set.isNotEmpty() && numbers.isNotEmpty()) {
                db.bySetNumber(set, numbers)?.takeIf { name.isBlank() || namesMatch(name, listOf(it.name)) }?.let { return listOf(it.copy(match = "chosen")) }
            }
            val all = db.exactName(name)?.let { db.printingsOf(it) }.orEmpty()
            if (all.isNotEmpty()) return all.map { it.copy(match = "chosen") }
                .sortedBy { if (set.isNotEmpty() && it.setCode.equals(set, ignoreCase = true)) 0 else 1 }
        }
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

    private var sets: Pair<Set<String>, Map<String, String>>? = null

    /**
     * The set code for a file's set column: a set code as is ("hob", "PW26"), or a set name's
     * code ("The Hobbit" -> "HOB"), from Scryfall's list of sets (read once); null if unknown.
     */
    fun resolveSet(value: String): String? {
        if (value.isBlank()) return null
        offline?.resolveSet(value)?.let { return it }
        val (codes, names) = sets ?: run {
            val data = get("/sets")?.optJSONArray("data") ?: return null
            val all = (0 until data.length()).map { data.getJSONObject(it) }
            (all.map { it.getString("code").uppercase() }.toSet() to
                all.associate { searchKey(it.getString("name"))!! to it.getString("code").uppercase() }).also { sets = it }
        }
        return value.trim().uppercase().takeIf { it in codes } ?: names[searchKey(value)]
    }

    /** What to look up in `collection`: a printing by set + number, or a card by name (+ set) */
    data class Identifier(val setCode: String = "", val number: String = "", val name: String = "")

    /**
     * Look up many cards at once (POST /cards/collection, 75 per request): the printing for each
     * identifier, or null when Scryfall doesn't know it. Results are tagged "imported".
     */
    fun collection(identifiers: List<Identifier>): List<Printing?> {
        val results = arrayOfNulls<Printing>(identifiers.size)
        // Offline data first; what it lacks goes online
        offline?.let { db ->
            identifiers.forEachIndexed { i, id ->
                results[i] = if (id.setCode.isNotEmpty() && id.number.isNotEmpty()) db.bySetNumber(id.setCode, numberVariants(id.number))
                else db.exactName(id.name)?.let { exact -> db.printingsOf(exact).let { all ->
                    all.firstOrNull { id.setCode.isNotEmpty() && it.setCode.equals(id.setCode, true) } ?: all.firstOrNull() } }
                results[i] = results[i]?.copy(match = "imported")
            }
            val missing = identifiers.indices.filter { results[it] == null }
            if (missing.isEmpty()) return results.toList()
            val online = runCatching { collectionOnline(missing.map { identifiers[it] }) }.getOrNull() ?: return results.toList()
            missing.forEachIndexed { k, i -> results[i] = online[k] }
            return results.toList()
        }
        return collectionOnline(identifiers)
    }

    private fun collectionOnline(identifiers: List<Identifier>): List<Printing?> {
        val results = arrayOfNulls<Printing>(identifiers.size)
        identifiers.withIndex().chunked(75).forEachIndexed { batch, chunk ->
            if (batch > 0) Thread.sleep(100)  // Scryfall asks for 50-100 ms between requests
            val body = JSONObject().put("identifiers", org.json.JSONArray().apply {
                chunk.forEach { (_, id) ->
                    put(JSONObject().apply {
                        if (id.setCode.isNotEmpty() && id.number.isNotEmpty()) {
                            put("set", id.setCode.lowercase()); put("collector_number", id.number)
                        } else {
                            put("name", id.name)
                            if (id.setCode.isNotEmpty()) put("set", id.setCode.lowercase())
                        }
                    })
                }
            })
            val request = Request.Builder().url("$API/cards/collection")
                .post(body.toString().toRequestBody("application/json".toMediaType()))
                .header("User-Agent", USER_AGENT).header("Accept", "application/json").build()
            val found = http.newCall(request).execute().use { response ->
                if (!response.isSuccessful) throw IOException("Scryfall: HTTP ${response.code}")
                JSONObject(response.body.string()).optJSONArray("data") ?: org.json.JSONArray()
            }
            // Results come back in request order, without the ones not found: match them up
            val cards = (0 until found.length()).map { found.getJSONObject(it) }
            for ((index, id) in chunk) {
                val card = cards.firstOrNull { c ->
                    if (id.setCode.isNotEmpty() && id.number.isNotEmpty())
                        c.optString("set").equals(id.setCode, true) && numberVariants(id.number).contains(c.optString("collector_number"))
                    else namesMatch(id.name, c) && (id.setCode.isEmpty() || c.optString("set").equals(id.setCode, true))
                }
                results[index] = card?.let { printing(it, "imported") }
            }
        }
        return results.toList()
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

    private fun printing(card: JSONObject, match: String): Printing = printingOf(card, match)

    companion object {
        /** A printing from Scryfall's card JSON (API answers and the bulk data file) */
        fun printingOf(card: JSONObject, match: String): Printing {
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
        fun namesMatch(query: String, card: JSONObject): Boolean =
            namesMatch(query, listOf(card.optString("name"), card.optString("flavor_name")))

        /** namesMatch against a card's name(s) - e.g. its name and flavor name */
        fun namesMatch(query: String, names: List<String>): Boolean {
            val queryKey = searchKey(query) ?: return false
            for (fullName in names) {
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
