package com.cardscanner

import com.cardscanner.inventory.Finish
import com.cardscanner.inventory.ImportRow
import com.cardscanner.inventory.InventoryEntry
import com.cardscanner.inventory.parseInventoryCsv
import com.cardscanner.inventory.writeCsv
import com.cardscanner.inventory.writeMoxfield
import org.junit.Assert.assertEquals
import org.junit.Test

class ImportTest {
    private fun entry(name: String, set: String, code: String, number: String, qty: Int, condition: String, finish: Finish) =
        InventoryEntry(0, null, name, set, code, number, "rare", "Instant", "{U}", "U", "Blue", 1.5, qty, condition, finish, "2026-09-26 10:00:00", null)

    private val entries = listOf(
        entry("Thorin, King of Durin's Folk", "The Hobbit Eternal", "HOC", "3", 1, "Near Mint", Finish.REGULAR),
        entry("The \"Quoted\" Card", "Test Set", "TST", "12a", 3, "Lightly Played", Finish.FOIL),
        entry("Smaug", "The Hobbit", "HOB", "109", 2, "Damaged", Finish.SURGE),
    )

    @Test
    fun csvExportReadsBack() {
        assertEquals(listOf(
            ImportRow("Thorin, King of Durin's Folk", "The Hobbit Eternal", "3", 1, "Near Mint", Finish.REGULAR, "2026-09-26 10:00:00"),
            ImportRow("The \"Quoted\" Card", "Test Set", "12a", 3, "Lightly Played", Finish.FOIL, "2026-09-26 10:00:00"),
            ImportRow("Smaug", "The Hobbit", "109", 2, "Damaged", Finish.SURGE, "2026-09-26 10:00:00"),
        ), parseInventoryCsv(writeCsv(entries)))
    }

    @Test
    fun moxfieldExportReadsBack() {
        // Surge foils go to Moxfield as foil
        assertEquals(listOf(
            ImportRow("Thorin, King of Durin's Folk", "hoc", "3", 1, "Near Mint", Finish.REGULAR),
            ImportRow("The \"Quoted\" Card", "tst", "12a", 3, "Lightly Played", Finish.FOIL),
            ImportRow("Smaug", "hob", "109", 2, "Damaged", Finish.FOIL),
        ), parseInventoryCsv(writeMoxfield(entries)))
    }

    @Test
    fun moxfieldCollectionExport() {
        // Moxfield's own export: extra columns, "Set Code", short conditions, byte order mark
        val csv = "﻿\"Count\",\"Tradelist Count\",\"Name\",\"Set Code\",\"Condition\",\"Language\",\"Foil\",\"Collector Number\"\n" +
            "\"4\",\"0\",\"Lightning Bolt\",\"m11\",\"NM\",\"English\",\"\",\"149\"\n" +
            "\"1\",\"0\",\"Sol Ring\",\"c21\",\"LP\",\"English\",\"etched\",\"263\"\n"
        assertEquals(listOf(
            ImportRow("Lightning Bolt", "m11", "149", 4, "Near Mint", Finish.REGULAR),
            ImportRow("Sol Ring", "c21", "263", 1, "Lightly Played", Finish.FOIL),
        ), parseInventoryCsv(csv))
    }
}
