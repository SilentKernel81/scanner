package com.cardscanner.scryfall

import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.zip.GZIPInputStream

/**
 * Offline card data: Scryfall's "default cards" (every card in English, ~110k printings) in a
 * local SQLite file - the Python scanner's `cards` table, trimmed to what the app uses. Downloaded
 * on request (Settings) and replaced as a whole; lookups fall back to the online API without it.
 */
class CardDatabase(context: Context, private val http: OkHttpClient) {
    private val file = File(context.filesDir, "cards.db")
    @Volatile private var db: SQLiteDatabase? = open()

    private fun open(): SQLiteDatabase? = file.takeIf { it.exists() }?.let {
        runCatching { SQLiteDatabase.openDatabase(it.path, null, SQLiteDatabase.OPEN_READONLY) }.getOrNull()
    }

    val ready get() = db != null

    /** (printings, when Scryfall made the file) of the downloaded data, or null */
    fun info(): Pair<Int, String>? {
        val db = db ?: return null
        val count = db.rawQuery("SELECT COUNT(*) FROM cards", null).use { if (it.moveToFirst()) it.getInt(0) else 0 }
        val updated = db.rawQuery("SELECT value FROM info WHERE key = 'updated_at'", null).use { if (it.moveToFirst()) it.getString(0) else "" }
        return count to updated
    }

    /** Where Scryfall's current default cards file is, and when it was made */
    private fun bulkInfo(): Pair<String, String> {
        val request = Request.Builder().url("https://api.scryfall.com/bulk-data/default-cards")
            .header("User-Agent", Scryfall.USER_AGENT).header("Accept", "application/json").build()
        http.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw IOException("Scryfall: HTTP ${response.code}")
            val json = JSONObject(response.body.string())
            return (json.optString("jsonl_download_uri").ifEmpty { throw IOException("No JSON Lines file on Scryfall") }) to
                json.optString("updated_at")
        }
    }

    /** Is there newer card data on Scryfall than the downloaded one? (null: can't tell) */
    fun updateAvailable(): Boolean? = runCatching { bulkInfo().second != info()?.second }.getOrNull()

    /**
     * Download Scryfall's default cards (gzipped JSON Lines, ~80 MB) into a new file, one card at a
     * time, then swap it in. `progress` gets status lines.
     */
    fun download(progress: (String) -> Unit) {
        val (url, updatedAt) = bulkInfo()
        val temp = File(file.path + ".tmp")
        temp.delete()
        val out = SQLiteDatabase.openOrCreateDatabase(temp, null)
        try {
            out.execSQL("""CREATE TABLE cards (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT, front_key TEXT, flavor_key TEXT,
                set_code TEXT, set_name TEXT, number TEXT, rarity TEXT, type_line TEXT, mana_cost TEXT, colors TEXT,
                finishes TEXT, surge INTEGER, image_url TEXT, scryfall_uri TEXT,
                usd TEXT, usd_foil TEXT, eur TEXT, eur_foil TEXT, released TEXT)""")
            out.execSQL("CREATE TABLE info (key TEXT PRIMARY KEY, value TEXT)")
            val insert = out.compileStatement("""INSERT OR REPLACE INTO cards VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""")
            val request = Request.Builder().url(url).header("User-Agent", Scryfall.USER_AGENT).build()
            http.newCall(request).execute().use { response ->
                if (!response.isSuccessful) throw IOException("Download: HTTP ${response.code}")
                val size = response.body.contentLength()
                val counting = CountingStream(response.body.byteStream())
                var count = 0
                out.beginTransaction()
                GZIPInputStream(counting, 1 shl 16).bufferedReader().useLines { lines ->
                    for (line in lines) {
                        if (line.isBlank()) continue
                        val card = JSONObject(line)
                        if (card.optString("layout") == "art_series") continue
                        val p = Scryfall.printingOf(card, "offline")
                        val flavor = card.optString("flavor_name")
                        val values = listOf(p.id, p.name, Scryfall.searchKey(p.name), Scryfall.searchKey(p.name.split(" // ")[0]),
                            Scryfall.searchKey(flavor), p.setCode, p.setName, p.collectorNumber, p.rarity, p.typeLine, p.manaCost,
                            p.colors.joinToString(","), p.finishes.joinToString(","), if (p.surgeFoil) "1" else "0", p.imageUrl,
                            p.scryfallUrl, p.usd, p.usdFoil, p.eur, p.eurFoil, card.optString("released_at"))
                        insert.clearBindings()
                        values.forEachIndexed { i, v -> if (v == null) insert.bindNull(i + 1) else insert.bindString(i + 1, v) }
                        insert.executeInsert()
                        if (++count % 5000 == 0) {
                            out.setTransactionSuccessful(); out.endTransaction(); out.beginTransaction()
                            val percent = if (size > 0) " (${counting.count * 100 / size}%)" else ""
                            progress("Reading card data: $count printings$percent…")
                        }
                    }
                }
                out.setTransactionSuccessful()
                out.endTransaction()
                progress("Indexing $count printings…")
                out.execSQL("INSERT INTO info VALUES ('updated_at', ?)", arrayOf(updatedAt))
                out.execSQL("CREATE INDEX idx_set_number ON cards (set_code, number)")
                out.execSQL("CREATE INDEX idx_name ON cards (name_key)")
                out.execSQL("CREATE INDEX idx_front ON cards (front_key)")
                out.execSQL("CREATE INDEX idx_flavor ON cards (flavor_key)")
            }
            out.close()
            synchronized(this) {
                db?.close()
                if (!temp.renameTo(file)) throw IOException("Could not replace the card data")
                File(temp.path + "-journal").delete()
                db = open()
                sets = null
            }
        } catch (e: Exception) {
            if (out.isOpen) { if (out.inTransaction()) out.endTransaction(); out.close() }
            temp.delete()
            throw e
        }
    }

    fun delete() = synchronized(this) {
        db?.close()
        db = null
        sets = null
        file.delete()
    }

    // ------------------------------------------------------------------------
    // Lookups (the local counterpart of Scryfall's API calls)
    // ------------------------------------------------------------------------

    private fun query(where: String, args: Array<String>, order: String = "released DESC", limit: Int = 200): List<Printing> {
        val db = db ?: return emptyList()
        return db.rawQuery("SELECT * FROM cards WHERE $where ORDER BY $order LIMIT $limit", args).use { c ->
            buildList { while (c.moveToNext()) add(c.printing()) }
        }
    }

    fun byId(id: String): Printing? = query("id = ?", arrayOf(id)).firstOrNull()

    /** The printing with this set code and one of these collector numbers */
    fun bySetNumber(setCode: String, numbers: List<String>): Printing? = numbers.firstNotNullOfOrNull { number ->
        query("set_code = ? AND number = ?", arrayOf(setCode.uppercase(), number)).firstOrNull()
    }

    /**
     * The card's exact name for a name read from a card: same name / front face / flavor name,
     * a shortened legendary name ("Thanos" -> "Thanos, the Mad Titan"), or the closest spelling
     * (similarity >= 0.8 among names starting with the same letter). Null if nothing close.
     */
    fun exactName(name: String): String? {
        val key = Scryfall.searchKey(name) ?: return null
        query("name_key = ? OR front_key = ? OR flavor_key = ?", arrayOf(key, key, key), limit = 1).firstOrNull()?.let { return it.name }
        query("name_key LIKE ? OR front_key LIKE ?", arrayOf("$key%", "$key%"), order = "length(name), released DESC", limit = 1)
            .firstOrNull()?.let { return it.name }
        val db = db ?: return null
        val candidates = db.rawQuery("SELECT DISTINCT name, front_key FROM cards WHERE front_key LIKE ?", arrayOf("${key.first()}%")).use { c ->
            buildList { while (c.moveToNext()) add(c.getString(0) to c.getString(1)) }
        }
        return candidates.map { it.first to Scryfall.similarity(key, it.second) }.filter { it.second >= 0.8 }.maxByOrNull { it.second }?.first
    }

    /** All printings of a card (its exact name), newest first */
    fun printingsOf(exactName: String): List<Printing> = query("name = ?", arrayOf(exactName))

    /** (set codes, set name key -> code) of the data, read once */
    @Volatile private var sets: Pair<Set<String>, Map<String, String>>? = null

    /** Set code of a set code or set name, from the sets in the data; null if unknown */
    fun resolveSet(value: String): String? {
        val db = db ?: return null
        val (codes, names) = sets ?: db.rawQuery("SELECT DISTINCT set_code, set_name FROM cards", null).use { c ->
            val codes = HashSet<String>(); val names = HashMap<String, String>()
            while (c.moveToNext()) { codes.add(c.getString(0)); Scryfall.searchKey(c.getString(1))?.let { names[it] = c.getString(0) } }
            (codes to names).also { sets = it }
        }
        return value.trim().uppercase().takeIf { it in codes } ?: names[Scryfall.searchKey(value)]
    }

    private fun Cursor.printing(): Printing {
        fun text(column: String): String? = getString(getColumnIndexOrThrow(column))
        return Printing(
            id = text("id")!!, name = text("name")!!, setCode = text("set_code").orEmpty(), setName = text("set_name").orEmpty(),
            collectorNumber = text("number").orEmpty(), rarity = text("rarity").orEmpty(), typeLine = text("type_line").orEmpty(),
            manaCost = text("mana_cost").orEmpty(), colors = text("colors").orEmpty().split(",").filter { it.isNotEmpty() },
            finishes = text("finishes").orEmpty().split(",").filter { it.isNotEmpty() }, surgeFoil = text("surge") == "1",
            imageUrl = text("image_url"), scryfallUrl = text("scryfall_uri").orEmpty(),
            usd = text("usd"), usdFoil = text("usd_foil"), eur = text("eur"), eurFoil = text("eur_foil"), match = "offline",
        )
    }

    /** Counts the compressed bytes read, for the download's percentage */
    private class CountingStream(private val inner: java.io.InputStream) : java.io.FilterInputStream(inner) {
        var count = 0L
        override fun read(): Int = super.read().also { if (it >= 0) count++ }
        override fun read(b: ByteArray, off: Int, len: Int): Int = super.read(b, off, len).also { if (it > 0) count += it }
    }
}
