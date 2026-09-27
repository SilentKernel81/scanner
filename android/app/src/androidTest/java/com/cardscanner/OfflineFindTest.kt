package com.cardscanner

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.cardscanner.ai.CardIdentifier
import com.cardscanner.scryfall.CardDatabase
import com.cardscanner.scryfall.Scryfall
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * The offline card data finds the same printings, with the same match tags, as Scryfall's API.
 * Needs the card data downloaded in the app (Settings) and the internet; results are written to
 * the app's external files dir as offline_find.txt.
 */
@RunWith(AndroidJUnit4::class)
class OfflineFindTest {
    @Test
    fun offlineMatchesOnline() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val http = CardIdentifier.httpClient()
        val local = CardDatabase(context, http)
        assumeTrue("download the card data in the app first", local.ready)
        val offline = Scryfall(http, local)
        val online = Scryfall(http)
        // AI readings: name, number, set (scanned cards, and misreads)
        val readings = listOf(
            Triple("Beorn the Fierce", "0230", "HOB"), Triple("Warstorm Surge", "0001", "PRM"),
            Triple("Supper for Spiders", "0300", "HOB"), Triple("Sauron, the Dark Lord", "0076", "HOC"),
            Triple("Bilbo, Thief in the Night", "0033", "MEE"), Triple("The Soul Stone", "0066", "SPM"),
            Triple("Dark Deed", "0003", "PRM"), Triple("Thranduil, Sindarin Liege", "0269", "MEE"),
            Triple("Lakeshore Apothecary", "0043", "HOB"), Triple("Fili the Pathfinder", "0014", "HOB"),
            Triple("Smaug, the Great Calamity", "0109", "HOB"), Triple("Lightning Blot", "", ""),
            Triple("Thanos", "", ""), Triple("Sol Ring", "", ""), Triple("Ori, Keeper of Songs", "0024", "HOB"),
        )
        val lines = readings.map { (name, number, set) ->
            val a = offline.find(name, number, set)
            val b = online.find(name, number, set)
            val same = a?.id == b?.id && a?.match == b?.match
            "${if (same) "SAME" else "DIFF"} | $name #$number [$set] | offline: ${a?.setCode} #${a?.collectorNumber} (${a?.match}) | online: ${b?.setCode} #${b?.collectorNumber} (${b?.match})"
        }
        File(context.getExternalFilesDir(null), "offline_find.txt").writeText(lines.joinToString("\n"))
    }
}
