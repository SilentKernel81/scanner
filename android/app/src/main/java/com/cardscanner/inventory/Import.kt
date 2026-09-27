package com.cardscanner.inventory

/** One card row of an imported CSV file */
data class ImportRow(
    val name: String,
    /** Set code ("HOB") or set name ("The Hobbit"), as the file has it */
    val set: String,
    val number: String,
    val quantity: Int,
    val condition: String,
    val finish: Finish,
    /** When it was added (the file's Timestamp column), or null */
    val timestamp: String? = null,
)

/**
 * Read an inventory CSV: this app's / the Python scanner's export (Card Name, Set, Card Number,
 * Quantity, Condition, Foil, Surge - or a Finish column), or Moxfield's (Count, Name,
 * Edition / Set Code, Collector Number, Condition, Foil). Columns are found by name.
 * Throws IllegalArgumentException for a file in neither format.
 */
fun parseInventoryCsv(text: String): List<ImportRow> {
    val rows = parseCsv(text.removePrefix("﻿"))  // Excel writes a byte order mark
    if (rows.isEmpty()) return emptyList()
    val header = rows.first().map { it.trim().lowercase() }
    fun column(vararg names: String) = names.firstNotNullOfOrNull { n -> header.indexOf(n).takeIf { it >= 0 } }
    val name = column("card name", "name") ?: throw IllegalArgumentException("No \"Card Name\" or \"Name\" column")
    val set = column("set code", "edition", "set")
    val number = column("card number", "collector number")
    val quantity = column("quantity", "count")
    val condition = column("condition")
    val foil = column("foil")
    val surge = column("surge")
    val finish = column("finish")
    val timestamp = column("timestamp")

    return rows.drop(1).mapNotNull { row ->
        fun value(index: Int?) = index?.let { row.getOrNull(it) }?.trim().orEmpty()
        val cardName = value(name).ifEmpty { return@mapNotNull null }
        val foilValue = value(foil).lowercase()
        val cardFinish = when {
            finish != null -> Finish.of(value(finish).lowercase())
            value(surge).equals("yes", true) -> Finish.SURGE
            foilValue in setOf("yes", "foil", "etched", "true", "1") -> Finish.FOIL
            else -> Finish.REGULAR
        }
        ImportRow(cardName, value(set), value(number), value(quantity).toIntOrNull()?.coerceAtLeast(1) ?: 1,
            normalizeCondition(value(condition)), cardFinish,
            value(timestamp).takeIf { Regex("""\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}""").matches(it) })
    }
}

/** Condition names and Moxfield's short forms ("NM", "LP", ...) to this app's names */
private fun normalizeCondition(value: String): String = when (value.lowercase().replace("_", " ")) {
    "", "nm", "m", "mint", "near mint" -> "Near Mint"
    "lp", "sp", "lightly played", "slightly played", "excellent" -> "Lightly Played"
    "mp", "moderately played", "played", "good" -> "Moderately Played"
    "hp", "heavily played" -> "Heavily Played"
    "d", "dmg", "damaged", "poor" -> "Damaged"
    else -> CONDITIONS.firstOrNull { it.equals(value, true) } ?: "Near Mint"
}

/** RFC 4180 CSV: quoted fields with commas, doubled quotes and line breaks */
fun parseCsv(text: String): List<List<String>> {
    val rows = ArrayList<List<String>>()
    var row = ArrayList<String>()
    val field = StringBuilder()
    var quoted = false
    var i = 0
    while (i < text.length) {
        val c = text[i]
        if (quoted) {
            if (c == '"') {
                if (i + 1 < text.length && text[i + 1] == '"') { field.append('"'); i++ } else quoted = false
            } else field.append(c)
        } else when (c) {
            '"' -> quoted = true
            ',' -> { row.add(field.toString()); field.clear() }
            '\r' -> {}
            '\n' -> { row.add(field.toString()); field.clear(); if (row.any { it.isNotEmpty() }) rows.add(row); row = ArrayList() }
            else -> field.append(c)
        }
        i++
    }
    if (field.isNotEmpty() || row.isNotEmpty()) { row.add(field.toString()); if (row.any { it.isNotEmpty() }) rows.add(row) }
    return rows
}
