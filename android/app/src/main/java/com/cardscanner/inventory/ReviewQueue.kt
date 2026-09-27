package com.cardscanner.inventory

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import com.cardscanner.ai.Foil
import java.io.File

/** A captured card waiting to be checked: what the AI read and the best match found, if any */
data class ReviewItem(
    val id: Long,
    val created: String,
    val imageFile: File,
    val name: String,
    val number: String,
    val setCode: String,
    val foil: Foil,
    /** Scryfall id of the suggested printing (not confirmed), or null */
    val suggestedId: String?,
    /** Why it is here: "Check: matched by name", "Not found on Scryfall", "The AI could not read the card", ... */
    val reason: String,
) {
    fun image(): Bitmap? = BitmapFactory.decodeFile(imageFile.path)
}

/**
 * Cards to review (review.py): while adding automatically, anything uncertain - printing not
 * confirmed, not found, nothing read - is queued with a copy of its photo, and scanning goes on.
 * Oldest first; kept across restarts.
 */
class ReviewQueue(context: Context) : SQLiteOpenHelper(context, "review.db", null, 1) {
    private val imageDir = File(context.filesDir, "review").apply { mkdirs() }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("""
            CREATE TABLE review_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created TEXT NOT NULL,
                image TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                number TEXT NOT NULL DEFAULT '',
                set_code TEXT NOT NULL DEFAULT '',
                foil TEXT NOT NULL DEFAULT 'UNKNOWN',
                suggested_id TEXT,
                reason TEXT NOT NULL DEFAULT ''
            )""")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {}

    @Synchronized
    fun add(photo: Bitmap, name: String, number: String, setCode: String, foil: Foil, suggestedId: String?, reason: String): Long {
        val file = File(imageDir, "${System.currentTimeMillis()}_${(0..9999).random()}.jpg")
        file.outputStream().use { photo.compress(Bitmap.CompressFormat.JPEG, 90, it) }
        return writableDatabase.insertOrThrow("review_queue", null, ContentValues().apply {
            put("created", Inventory.now())
            put("image", file.name)
            put("name", name)
            put("number", number)
            put("set_code", setCode)
            put("foil", foil.name)
            put("suggested_id", suggestedId)
            put("reason", reason)
        })
    }

    /** Resolve an item (added or skipped): it and its photo go */
    @Synchronized
    fun remove(id: Long) {
        val db = writableDatabase
        db.rawQuery("SELECT image FROM review_queue WHERE id = ?", arrayOf(id.toString())).use { c ->
            if (c.moveToFirst()) File(imageDir, c.getString(0)).delete()
        }
        db.delete("review_queue", "id = ?", arrayOf(id.toString()))
    }

    /** All items, oldest first */
    @Synchronized
    fun all(): List<ReviewItem> =
        readableDatabase.rawQuery("SELECT * FROM review_queue ORDER BY id", null).use { c ->
            buildList {
                while (c.moveToNext()) {
                    fun text(column: String) = c.getString(c.getColumnIndexOrThrow(column)).orEmpty()
                    add(ReviewItem(
                        id = c.getLong(c.getColumnIndexOrThrow("id")),
                        created = text("created"),
                        imageFile = File(imageDir, text("image")),
                        name = text("name"),
                        number = text("number"),
                        setCode = text("set_code"),
                        foil = runCatching { Foil.valueOf(text("foil")) }.getOrDefault(Foil.UNKNOWN),
                        suggestedId = c.getString(c.getColumnIndexOrThrow("suggested_id")),
                        reason = text("reason"),
                    ))
                }
            }
        }
}
