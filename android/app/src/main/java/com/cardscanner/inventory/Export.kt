package com.cardscanner.inventory

import java.util.Locale

/** Export formats of the inventory (games/mtg.py: Magic.export_formats) */
enum class ExportFormat(val label: String, val filePrefix: String) {
    CSV("CSV", "card_inventory_export"),
    MOXFIELD("Moxfield", "moxfield_export");

    fun write(entries: List<InventoryEntry>): String = when (this) {
        CSV -> writeCsv(entries)
        MOXFIELD -> writeMoxfield(entries)
    }
}

/** Same columns as the Python scanner's CSV export (games/mtg.py: CSV_COLUMNS, which import_csv reads back) */
fun writeCsv(entries: List<InventoryEntry>): String = csv(
    listOf("Card Name", "Set", "Card Number", "Rarity", "Type", "Mana Cost", "Colors", "Color Identity",
        "Price (USD)", "Quantity", "Condition", "Foil", "Surge", "Timestamp"),
    entries.map {
        listOf(it.name, it.setName, it.number, it.rarity, it.typeLine, it.manaCost, it.colors, it.colorIdentity,
            "$" + String.format(Locale.US, "%.2f", it.priceUsd), it.quantity.toString(), it.condition,
            if (it.finish == Finish.FOIL) "Yes" else "No", if (it.finish == Finish.SURGE) "Yes" else "No", it.timestamp)
    })

/**
 * Moxfield collection import. Edition is the set code: Moxfield matches it against its sets and,
 * for a value it doesn't know (e.g. the set name), takes the alphabetically first set of the card.
 */
fun writeMoxfield(entries: List<InventoryEntry>): String = csv(
    listOf("Count", "Name", "Edition", "Condition", "Language", "Foil", "Collector Number", "Purchase Price", "Tag"),
    entries.map {
        // A surge foil is Scryfall's "foil" finish of a surge foil printing (Moxfield's finishes:
        // foil, etched); the printing itself is given by set + collector number
        val foil = if (it.finish == Finish.REGULAR) "" else "foil"
        listOf(it.quantity.toString(), it.name, it.setCode.lowercase(), it.condition, "English", foil, it.number, "0", "")
    })

/** RFC 4180 CSV like Python's csv module: fields with commas, quotes or line breaks are quoted */
private fun csv(header: List<String>, rows: List<List<String>>): String = buildString {
    for (row in listOf(header) + rows) {
        append(row.joinToString(",") { field ->
            if (field.any { it == ',' || it == '"' || it == '\n' || it == '\r' }) "\"" + field.replace("\"", "\"\"") + "\"" else field
        })
        append("\r\n")
    }
}
