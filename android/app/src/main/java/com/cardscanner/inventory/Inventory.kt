package com.cardscanner.inventory

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import com.cardscanner.scryfall.Printing
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter

/** Finishes of a Magic card (keys as in games/mtg.py: Magic.finishes) */
enum class Finish(val key: String, val label: String) {
    REGULAR("regular", "Regular"), FOIL("foil", "Foil"), SURGE("surge", "Surge foil");

    companion object {
        fun of(key: String?) = entries.firstOrNull { it.key == key } ?: REGULAR
    }
}

val CONDITIONS = listOf("Near Mint", "Lightly Played", "Moderately Played", "Heavily Played", "Damaged")

/** One inventory row: copies of one card + set + number + condition + finish */
data class InventoryEntry(
    val id: Long,
    val cardId: String?,
    val name: String,
    val setName: String,
    val setCode: String,
    val number: String,
    val rarity: String,
    val typeLine: String,
    val manaCost: String,
    val colors: String,
    val colorIdentity: String,
    val priceUsd: Double,
    val quantity: Int,
    val condition: String,
    val finish: Finish,
    val timestamp: String,
    val imageUrl: String?,
)

/**
 * The card inventory, in a SQLite database in the app's storage - the same table as the Python
 * scanner's (inventory.py: INVENTORY_TABLE, merged on game + name + set + number + condition +
 * finish), plus the Scryfall image. Magic only for now (game = 'mtg').
 */
class Inventory(context: Context) : SQLiteOpenHelper(context, "inventory.db", null, 1) {

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("""
            CREATE TABLE inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game TEXT NOT NULL DEFAULT 'mtg',
                card_id TEXT,
                card_name TEXT NOT NULL,
                set_name TEXT NOT NULL,
                set_code TEXT,
                card_number TEXT NOT NULL DEFAULT '',
                rarity TEXT,
                type_line TEXT,
                mana_cost TEXT,
                colors TEXT,
                color_identity TEXT,
                price_usd REAL,
                quantity INTEGER NOT NULL DEFAULT 1,
                condition TEXT NOT NULL DEFAULT 'Near Mint',
                finish TEXT NOT NULL DEFAULT 'regular',
                timestamp TEXT NOT NULL,
                image_url TEXT,
                UNIQUE(game, card_name, set_name, card_number, condition, finish)
            )""")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {}

    /**
     * Add copies of a printing - merged with an existing entry for the same card, set, number,
     * condition and finish. Returns the entry's id.
     */
    @Synchronized
    fun add(printing: Printing, finish: Finish, condition: String = "Near Mint", quantity: Int = 1, timestamp: String = now()): Long {
        // Foil and surge foil copies are priced as foils; fall back to the other price if missing
        val prices = if (finish == Finish.REGULAR) listOf(printing.usd, printing.usdFoil) else listOf(printing.usdFoil, printing.usd)
        val price = prices.firstNotNullOfOrNull { it?.toDoubleOrNull() } ?: 0.0
        val db = writableDatabase
        // Upsert by hand: ON CONFLICT ... DO UPDATE needs SQLite 3.24 (Android 11); minSdk 26 has 3.18
        db.beginTransaction()
        try {
            val existing = findId(db, printing.name, printing.setName, printing.collectorNumber, condition, finish.key)
            val id = if (existing != null) {
                db.execSQL("""UPDATE inventory SET quantity = quantity + ?, timestamp = MAX(timestamp, ?), price_usd = ?,
                              card_id = COALESCE(?, card_id), set_code = COALESCE(?, set_code),
                              image_url = COALESCE(?, image_url) WHERE id = ?""",
                    arrayOf<Any?>(quantity, timestamp, price, printing.id, printing.setCode, printing.imageUrl, existing))
                existing
            } else {
                db.insertOrThrow("inventory", null, ContentValues().apply {
                    put("game", "mtg")
                    put("card_id", printing.id)
                    put("card_name", printing.name)
                    put("set_name", printing.setName)
                    put("set_code", printing.setCode)
                    put("card_number", printing.collectorNumber)
                    put("rarity", printing.rarity)
                    put("type_line", printing.typeLine)
                    put("mana_cost", printing.manaCost)
                    put("colors", printing.colors.joinToString(", ").ifEmpty { "Colorless" })
                    put("color_identity", colorIdentity(printing.colors))
                    put("price_usd", price)
                    put("quantity", quantity)
                    put("condition", condition)
                    put("finish", finish.key)
                    put("timestamp", timestamp)
                    put("image_url", printing.imageUrl)
                })
            }
            db.setTransactionSuccessful()
            return id
        } finally {
            db.endTransaction()
        }
    }

    /** Take back copies added by a scan (Undo): fewer copies, or the entry goes */
    @Synchronized
    fun remove(id: Long, quantity: Int = 1) {
        val db = writableDatabase
        db.execSQL("UPDATE inventory SET quantity = quantity - ? WHERE id = ?", arrayOf<Any?>(quantity, id))
        db.execSQL("DELETE FROM inventory WHERE id = ? AND quantity <= 0", arrayOf<Any?>(id))
    }

    @Synchronized
    fun delete(id: Long) {
        writableDatabase.execSQL("DELETE FROM inventory WHERE id = ?", arrayOf<Any?>(id))
    }

    /**
     * Change an entry's quantity, condition or finish. If that makes it the same card + condition
     * + finish as another entry, the two are merged (inventory.py: update_card).
     */
    @Synchronized
    fun update(id: Long, quantity: Int, condition: String, finish: Finish) {
        val db = writableDatabase
        val entry = get(id) ?: return
        if (quantity <= 0) { delete(id); return }
        db.beginTransaction()
        try {
            val other = findId(db, entry.name, entry.setName, entry.number, condition, finish.key)
            if (other != null && other != id) {
                db.execSQL("UPDATE inventory SET quantity = quantity + ?, timestamp = ? WHERE id = ?", arrayOf<Any?>(quantity, now(), other))
                db.execSQL("DELETE FROM inventory WHERE id = ?", arrayOf<Any?>(id))
            } else {
                db.update("inventory", ContentValues().apply {
                    put("quantity", quantity)
                    put("condition", condition)
                    put("finish", finish.key)
                }, "id = ?", arrayOf(id.toString()))
            }
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    /**
     * Correct an entry's printing (a misread card): its card fields become the printing's, priced
     * for its finish. If that makes it the same card + condition + finish as another entry, the two
     * are merged. Returns the id the copies ended up in.
     */
    @Synchronized
    fun changePrinting(id: Long, printing: Printing): Long? {
        val entry = get(id) ?: return null
        val prices = if (entry.finish == Finish.REGULAR) listOf(printing.usd, printing.usdFoil) else listOf(printing.usdFoil, printing.usd)
        val price = prices.firstNotNullOfOrNull { it?.toDoubleOrNull() } ?: 0.0
        val db = writableDatabase
        db.beginTransaction()
        try {
            val other = findId(db, printing.name, printing.setName, printing.collectorNumber, entry.condition, entry.finish.key)
            val result = if (other != null && other != id) {
                // Already have that printing (same condition and finish): the copies join it
                db.execSQL("UPDATE inventory SET quantity = quantity + ?, timestamp = ? WHERE id = ?", arrayOf<Any?>(entry.quantity, now(), other))
                db.execSQL("DELETE FROM inventory WHERE id = ?", arrayOf<Any?>(id))
                other
            } else {
                db.update("inventory", ContentValues().apply {
                    put("card_id", printing.id)
                    put("card_name", printing.name)
                    put("set_name", printing.setName)
                    put("set_code", printing.setCode)
                    put("card_number", printing.collectorNumber)
                    put("rarity", printing.rarity)
                    put("type_line", printing.typeLine)
                    put("mana_cost", printing.manaCost)
                    put("colors", printing.colors.joinToString(", ").ifEmpty { "Colorless" })
                    put("color_identity", colorIdentity(printing.colors))
                    put("price_usd", price)
                    put("image_url", printing.imageUrl)
                }, "id = ?", arrayOf(id.toString()))
                id
            }
            db.setTransactionSuccessful()
            return result
        } finally {
            db.endTransaction()
        }
    }

    @Synchronized
    fun clear() {
        writableDatabase.execSQL("DELETE FROM inventory")
    }

    @Synchronized
    fun get(id: Long): InventoryEntry? =
        readableDatabase.rawQuery("SELECT * FROM inventory WHERE id = ?", arrayOf(id.toString())).use { c ->
            if (c.moveToFirst()) c.entry() else null
        }

    /** All entries, newest first */
    @Synchronized
    fun all(): List<InventoryEntry> =
        readableDatabase.rawQuery("SELECT * FROM inventory WHERE game = 'mtg' ORDER BY timestamp DESC, id DESC", null).use { c ->
            buildList { while (c.moveToNext()) add(c.entry()) }
        }

    private fun findId(db: SQLiteDatabase, name: String, setName: String, number: String, condition: String, finish: String): Long? =
        db.rawQuery("""SELECT id FROM inventory WHERE game = 'mtg' AND card_name = ? AND set_name = ?
                       AND card_number = ? AND condition = ? AND finish = ?""",
            arrayOf(name, setName, number, condition, finish)).use { c -> if (c.moveToFirst()) c.getLong(0) else null }

    private fun Cursor.entry(): InventoryEntry {
        fun text(column: String) = getString(getColumnIndexOrThrow(column)).orEmpty()
        return InventoryEntry(
            id = getLong(getColumnIndexOrThrow("id")),
            cardId = getString(getColumnIndexOrThrow("card_id")),
            name = text("card_name"),
            setName = text("set_name"),
            setCode = text("set_code"),
            number = text("card_number"),
            rarity = text("rarity"),
            typeLine = text("type_line"),
            manaCost = text("mana_cost"),
            colors = text("colors"),
            colorIdentity = text("color_identity"),
            priceUsd = getDouble(getColumnIndexOrThrow("price_usd")),
            quantity = getInt(getColumnIndexOrThrow("quantity")),
            condition = text("condition"),
            finish = Finish.of(getString(getColumnIndexOrThrow("finish"))),
            timestamp = text("timestamp"),
            imageUrl = getString(getColumnIndexOrThrow("image_url")),
        )
    }

    companion object {
        private val COLOR_NAMES = mapOf("W" to "White", "U" to "Blue", "B" to "Black", "R" to "Red", "G" to "Green")

        /** games/mtg.py: color_identity */
        fun colorIdentity(colors: List<String>) = when (colors.size) {
            0 -> "Colorless"
            1 -> COLOR_NAMES[colors[0]] ?: colors[0]
            else -> "Multicolor"
        }

        /** Timestamp like the Python scanner's (inventory.py: now) */
        fun now(): String = LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss"))
    }
}
