package com.cardscanner

import com.cardscanner.ai.CardIdentifier
import com.cardscanner.scryfall.Scryfall
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * The AI answer parser and name similarity give the same results as the Python code
 * (card_identifier._parse_response, difflib.SequenceMatcher). parser_reference.json is made by
 * parser_reference.py (run from the repository root with the Python venv).
 */
class ParserParityTest {
    private val reference = JSONObject(javaClass.getResource("/parser_reference.json")!!.readText())

    @Test
    fun parseResponseMatchesPython() {
        val cases = reference.getJSONArray("cases")
        val parsed = reference.getJSONArray("parsed")
        for (i in 0 until cases.length()) {
            val expected = parsed.optJSONObject(i)
            val actual = CardIdentifier.parseResponse(cases.getString(i))
            val message = "case ${cases.getString(i)}"
            if (expected == null) {
                assertEquals(message, null, actual)
            } else {
                assertEquals(message, expected.getString("name"), actual?.name)
                assertEquals(message, expected.getString("collector_number"), actual?.collectorNumber)
                assertEquals(message, expected.getString("set_code"), actual?.setCode)
            }
        }
    }

    @Test
    fun similarityMatchesDifflib() {
        val pairs = reference.getJSONArray("pairs")
        val ratios = reference.getJSONArray("ratios")
        for (i in 0 until pairs.length()) {
            val pair = pairs.getJSONArray(i)
            assertEquals(pair.toString(), ratios.getDouble(i), Scryfall.similarity(pair.getString(0), pair.getString(1)), 1e-9)
        }
    }
}
