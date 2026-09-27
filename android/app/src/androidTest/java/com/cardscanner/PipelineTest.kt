package com.cardscanner

import android.graphics.BitmapFactory
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.cardscanner.ai.CardIdentifier
import com.cardscanner.ai.Provider
import com.cardscanner.scryfall.Scryfall
import org.json.JSONObject
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * Card images (assets cards/, e.g. from scanned_cards/) -> local vision AI -> Scryfall, written to
 * the app's external files dir as pipeline.json. Runs only with a local AI server:
 * am instrument -e aiUrl http://host:11434 -e aiModel <model> ...
 */
@RunWith(AndroidJUnit4::class)
class PipelineTest {
    @Test
    fun identifyCards() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val args = InstrumentationRegistry.getArguments()
        val url = args.getString("aiUrl")
        val model = args.getString("aiModel")
        assumeTrue("needs -e aiUrl and -e aiModel", url != null && model != null)
        val http = CardIdentifier.httpClient()
        val identifier = CardIdentifier(Provider.LOCAL, model!!, null, url!!, http)
        val scryfall = Scryfall(http)
        val assets = instrumentation.context.assets
        val out = JSONObject()
        for (name in assets.list("cards")!!.sorted()) {
            val card = BitmapFactory.decodeStream(assets.open("cards/$name"))
            val start = System.nanoTime()
            val (answer, reading) = identifier.identify(card)
            val foil = identifier.readFoilSymbol(card)
            val printing = reading?.let { scryfall.find(it.name, it.collectorNumber, it.setCode) }
            out.put(name, JSONObject()
                .put("answer", answer)
                .put("reading", reading?.let { "${it.name} #${it.collectorNumber} [${it.setCode}]" })
                .put("foil", foil.name)
                .put("printing", printing?.let { "${it.name} ${it.setCode} #${it.collectorNumber} (${it.match}) finishes=${it.finishes}" })
                .put("seconds", (System.nanoTime() - start) / 1e9))
        }
        File(instrumentation.targetContext.getExternalFilesDir(null), "pipeline.json").writeText(out.toString(2))
    }
}
