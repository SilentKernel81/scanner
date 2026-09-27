package com.cardscanner.ai

/**
 * Vision AI prompts for Magic cards - the built-in texts of prompts.py (BUILT_IN['mtg']).
 * Instructions + a fixed answer format the parser relies on. Keep in step with prompts.py.
 */
object Prompts {
    // No example values on purpose: on blurry cards, models copied them ("0367", "LTR")
    // instead of saying they could not read the card
    private const val IDENTIFY_INSTRUCTIONS = """This is a Magic: The Gathering card. Read three things:
- NAME: the card name at the top-left (for a double-faced card, the front face).
- NUMBER: the collector number at the bottom-left corner, first line: a letter and a number - give only the number, with its leading zeros. It is not the mana cost at the top-right.
- SET: the set code (3-4 letters or digits) at the start of the bottom-left second line, before the language code.
Copy exactly what is printed. If a value is blurry or unreadable, write Unknown - do not guess."""

    // With "Answer with exactly three lines" qwen3.5:9b often dropped the labels - asking for
    // the labels fixed it
    private const val IDENTIFY_ANSWER_FORMAT = """Always answer with all three lines, each with its label:
NAME: <card name>
NUMBER: <collector number, or Unknown>
SET: <set code, or Unknown>"""

    // Describing both shapes matters: asked only "star or dot?", qwen3.5:9b called 25 of 84
    // regular cards foil; with this wording none
    private const val FOIL_INSTRUCTIONS = "This is the bottom-left corner of a Magic: The Gathering card. Find the line with the set code " +
        "and the language code, like 'HOB•EN' or 'HOB★EN'. Look closely at the small symbol between " +
        "them: a dot is a plain round point; a star has five sharp points. Which is it? If that line is cut " +
        "off or not visible, answer unclear."

    private const val FOIL_ANSWER_FORMAT = "Answer with one word: star, dot, or unclear."

    val identify = "${IDENTIFY_INSTRUCTIONS.trim()}\n\n$IDENTIFY_ANSWER_FORMAT"
    val foil = "${FOIL_INSTRUCTIONS.trim()}\n\n$FOIL_ANSWER_FORMAT"
}
