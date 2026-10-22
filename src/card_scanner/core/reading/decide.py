# decide.py
# The decision, as a pure function of plain data (evidence.py): no database, no index, no picture, no clock, no log. Given what the picture says
# and what the strip says, which printing is it - or is this a card for review?
#
# Rule A2 of the experiment (docs/plans/IMAGE_MATCH_EXPERIMENT.md, section 4), exactly as it was measured, and as PrintingCheck.lookup applied
# it before this module existed (the regression cases in tests/data/reading_regression_cases.json were captured from that code):
#   'strong'  the strip names a printing (its number AND its set) that is one of the picture's candidates, the first such one nearest first,
#             and whose similarity is less than `agree_below` under the best match's: it looks as much like the picture as the best match does
#             (a reprint with the same art has almost the same fingerprint, so the strip decides among those). That printing.
#             If the first candidate the strip names looks different from the best match, the strip is NOT believed (a Secret Lair card prints
#             another printing's set and number): it is not looked for further down, and the rule goes on to the next step;
#   'image'   else the best match, when it leads the second by at least `image_lead`;
#   'review'  else: the evidence does not decide.
# Anything beyond A2 (a printing newer than the index, the finish) is a separate rule with its own function and its own field in the log; it never
# changes what A2 answers, because the exit criteria for dropping the vision AI were written against A2's numbers.
#
# Equal-to-the-threshold counts as the larger side for the lead (>=) and as not agreeing for the gap (< is the test), as in the code it replaces.

from typing import Optional

AGREE_BELOW = 0.02
IMAGE_LEAD = 0.05
THRESHOLDS = {'agree_below': AGREE_BELOW, 'image_lead': IMAGE_LEAD}

# The finish (phase 2b) has its own threshold, separate from rule A2's: how sure the foil network must be, one way or the other, to be believed
# (core/foil_marker.SURE: 0.9 - the number its 99.5% was measured at)
FINISH_SURE = 0.9
FINISH_THRESHOLDS = {'sure': FINISH_SURE}

# The guards of a decision (step 5 of docs/plans/READING_REVIEW_2026-10-21_PLAN.md): rule A2 says which printing the evidence points to; these say whether the
# server may ADD it on that evidence. Their own functions (accept_printing, confirm_by_ocr) and their own case file (tests/data/reading_guard_cases.json) -
# never a change to A2's answer. The names are what the reading log writes in 'refused' (core/reading/record.py imports them from here), what the case file
# expects, and what the Android app's Decide.kt answers: one spelling for all three.
IMAGE_ALONE_OFF = 'image_alone_off'              # the picture alone decided, and the person has not allowed that (reading.accept_image_alone)
INDEX_INCOMPLETE = 'index_incomplete'            # ... allowed, but the index lacks printings of the card data (PrintingCheck.missing_printings > 0, or not known)
CARD_NOT_IN_DATA = 'card_not_in_data'            # the decided printing is in the index and no longer in the card data
BELOW_IMAGE_FLOOR = 'below_image_floor'          # the picture alone, and it looks too little like its best match (image_min_similarity)
BELOW_STRONG_FLOOR = 'below_strong_floor'        # the strip named the printing, and the picture looks too little like it (strong_min_similarity)
STRIP_NAMES_UNINDEXED = 'strip_names_unindexed'  # the picture alone, and the strip names a printing the card data has and the index lacks (or the lookup failed)
STRIP_NAMES_UNKNOWN = 'strip_names_unknown'      # the picture alone, and the strip names a printing the card data does not have
OCR_DID_NOT_CONFIRM = 'ocr_did_not_confirm'      # the picture alone, with OCR on, and the full-card OCR did not confirm that printing (confirm_by_ocr)
GUARDS = (IMAGE_ALONE_OFF, INDEX_INCOMPLETE, CARD_NOT_IN_DATA, BELOW_IMAGE_FLOOR, BELOW_STRONG_FLOOR, STRIP_NAMES_UNINDEXED, STRIP_NAMES_UNKNOWN,
          OCR_DID_NOT_CONFIRM)

# The guards' floors, as measured (their measurements are documented beside core/printing_check.IMAGE_MIN_SIMILARITY / STRONG_MIN_SIMILARITY, which the server passes at
# call time; tests/test_reading_decide.py pins the two equal)
IMAGE_MIN_SIMILARITY = 0.5
STRONG_MIN_SIMILARITY = 0.30
ACCEPT_THRESHOLDS = {'image_min_similarity': IMAGE_MIN_SIMILARITY, 'strong_min_similarity': STRONG_MIN_SIMILARITY}

# How far ahead of the second printing the picture's best match must be for the full-card OCR's NAME to be no longer needed to confirm a decision by the picture
# alone (confirm_by_ocr): the collector line is still checked. Measured 2026-10-21 (scripts/experiments/image_match/lead_waiver.py, 428 real pictures with known truth):
# with every printing of the card removed from the index - a card newer than the card data, what the name check is there for - the picture's best match led the second
# by a median of 0.005 and never by more than 0.052 (best similarity never above 0.58): 0 of 428 such pictures would be added as another card at 0.08 or above. With the
# index complete, 257 of 386 right picture-alone decisions lead by 0.10 or more. 0.10 is about twice the highest lead a missing card reached. The live case it is for: a
# Secret Lair that prints ANOTHER card's name (the Deadpool Sol Ring prints "Worn Powerstone"), which no OCR read can confirm - six live scans, leads 0.12 to 0.19.
# What it does NOT cover: which printing, when the right one is missing from the index (the picture then picks a same-art reprint with a high lead) - that is the
# collector line's job, which stays.
LEAD_WAIVES_NAME = 0.10
CONFIRM_THRESHOLDS = {'lead_waives_name': LEAD_WAIVES_NAME}

# What the strip's whole line names, as the caller looked it up (facts['strip_names']); and what the full-card OCR's line names (ocr['line_names'])
STRIP_NAMES = (None, 'chosen', 'indexed_other', 'unindexed', 'unknown', 'lookup_failed')
LINE_NAMES = (None, 'chosen', 'same_card', 'other_card', 'unknown', 'lookup_failed')


def plain_number(number):
    """A collector number as the strip's parse writes it: no leading zeros ('0172' -> '172'); '0' stays '0' for a card numbered 0"""
    return str(number or '').strip().lstrip('0') or '0'


def decide_printing(picture, strip, thresholds: Optional[dict] = None):
    """
    Rule A2. picture: {'candidates': [{'id', 'similarity', 'set', 'number', ...}, nearest first], ...}; strip: {'number', 'set', ...} ('' for what was not read).
    Returns {'path': 'strong' | 'image' | 'review', 'printing_id', 'similarity', 'lead'} (evidence.PrintingDecision). The inputs are not changed.
    """
    thresholds = thresholds or THRESHOLDS
    candidates = (picture or {}).get('candidates') or []
    if not candidates:
        return {'path': 'review', 'printing_id': None, 'similarity': None, 'lead': None}
    best = candidates[0]['similarity']
    lead = best - candidates[1]['similarity'] if len(candidates) > 1 else None
    number, set_code = (strip or {}).get('number') or '', (strip or {}).get('set') or ''
    if number and set_code:
        for candidate in candidates:                      # nearest first: the first that names the strip's printing
            if plain_number(candidate.get('number')) == number and (candidate.get('set') or '').lower() == set_code:
                if best - candidate['similarity'] < thresholds['agree_below']:      # it looks as much like the picture as the best match does
                    return {'path': 'strong', 'printing_id': candidate['id'], 'similarity': candidate['similarity'], 'lead': lead}
                break                                     # one that names it but looks different: the strip is not believed
    if lead is not None and lead >= thresholds['image_lead']:
        return {'path': 'image', 'printing_id': candidates[0]['id'], 'similarity': best, 'lead': lead}
    return {'path': 'review', 'printing_id': None, 'similarity': None, 'lead': lead}


def decide_finish(finishes, symbol='unknown', net=None, thresholds: Optional[dict] = None):
    """
    The finish of a card whose printing is known (phase 2b: its own rule, answered in its own field of the log - never part of rule A2's answer).
    finishes: what the printing exists in ('nonfoil', 'foil', 'etched'); symbol: the foil symbol the OCR read in the set line ('foil', 'non-foil' or
    'unknown'); net: the foil network's probability of a foil, or None (it is asked only when the symbol was not read).
    In this order:
      1. a printing that exists in one finish only is certain: 'foil' (foil / etched only) or 'regular' (non-foil only) - source 'printing';
      2. the symbol the OCR read - source 'symbol';
      3. the network, when it is sure either way (>= sure for a foil, <= 1 - sure for a regular card) - source 'network';
      4. otherwise 'unknown', source 'none' - NOT 'regular': an unread marker says nothing (the live rule, Game.suggested_finish, files it as regular).
    Returns {'finish': 'foil' | 'regular' | 'unknown', 'source': 'printing' | 'symbol' | 'network' | 'none'}. A surge foil is 'foil' here: the kind of foil is not decided.
    """
    sure = (thresholds or FINISH_THRESHOLDS)['sure']
    finishes = finishes or []
    has_foil = 'foil' in finishes or 'etched' in finishes
    has_nonfoil = 'nonfoil' in finishes
    if has_foil and not has_nonfoil:
        return {'finish': 'foil', 'source': 'printing'}
    if has_nonfoil and not has_foil:
        return {'finish': 'regular', 'source': 'printing'}
    if symbol == 'foil':
        return {'finish': 'foil', 'source': 'symbol'}
    if symbol == 'non-foil':
        return {'finish': 'regular', 'source': 'symbol'}
    if net is not None:
        if net >= sure:
            return {'finish': 'foil', 'source': 'network'}
        if net <= 1 - sure:
            return {'finish': 'regular', 'source': 'network'}
    return {'finish': 'unknown', 'source': 'none'}


def accept_printing(decision, facts, thresholds: Optional[dict] = None):
    """
    May the server add the printing rule A2 decided on (decision: decide_printing's answer), on the evidence of the picture and the strip? The guards, in the order the
    server has always applied them:
      1. 'review' is no decision: not accepted, and no guard refused it (refused None);
      2. the picture alone ('image') needs facts['accept_image_alone'] - else image_alone_off - and an index that covers the whole card data (facts['index_covers'] True;
         False or None: index_incomplete);
      3. the decided printing must be in the card data (facts['in_data']) - else card_not_in_data;
      4. its similarity must reach the floor of its path - 'image': thresholds['image_min_similarity'] (below_image_floor), 'strong': ['strong_min_similarity'] (below_strong_floor);
      5. the picture alone, when the strip read a whole line (facts['strip_names'], looked up by the caller: None no whole line, 'chosen' / 'indexed_other' a printing in the
         index, 'unindexed' one the card data has and the index lacks, 'unknown' one the card data lacks, 'lookup_failed' the card data could not say): 'unindexed' or
         'lookup_failed' -> strip_names_unindexed (fails closed), 'unknown' -> strip_names_unknown.
    Returns {'accepted': bool, 'refused': None | the guard's name, 'needs_ocr': bool}. needs_ocr: a decision by the picture alone that passed all of these, with OCR on
    (facts['ocr_on']), is accepted only once the full-card OCR confirms it - confirm_by_ocr, the second half (a separate function because the OCR is read only then, and only
    for such a card). A program deciding the same way composes the two: accept_printing, then, if needs_ocr, confirm_by_ocr.
    """
    thresholds = thresholds or ACCEPT_THRESHOLDS
    path = (decision or {}).get('path')
    if path not in ('strong', 'image'):
        return {'accepted': False, 'refused': None, 'needs_ocr': False}
    alone = path == 'image'
    refuse = lambda guard: {'accepted': False, 'refused': guard, 'needs_ocr': False}
    if alone and not facts.get('accept_image_alone'):
        return refuse(IMAGE_ALONE_OFF)
    if alone and facts.get('index_covers') is not True:
        return refuse(INDEX_INCOMPLETE)
    if not facts.get('in_data'):
        return refuse(CARD_NOT_IN_DATA)
    if decision['similarity'] < thresholds['image_min_similarity' if alone else 'strong_min_similarity']:
        return refuse(BELOW_IMAGE_FLOOR if alone else BELOW_STRONG_FLOOR)
    if alone:
        named = facts.get('strip_names')
        if named in ('unindexed', 'lookup_failed'):
            return refuse(STRIP_NAMES_UNINDEXED)
        if named == 'unknown':
            return refuse(STRIP_NAMES_UNKNOWN)
    needs_ocr = alone and bool(facts.get('ocr_on'))
    return {'accepted': not needs_ocr, 'refused': None, 'needs_ocr': needs_ocr}


def confirm_by_ocr(ocr, lead=None, thresholds: Optional[dict] = None):
    """
    Does the full-card OCR confirm the printing the picture alone chose? ocr: {'name_matches': the name it read is the chosen card's (the caller's names_match; False when it
    read nothing or failed), 'line_names': what the whole collector line it read names, looked up by the caller - None (no whole line), 'chosen' (the very printing),
    'other_card' (another card's printing: a Secret Lair prints one by design, or a misread number - no evidence about this card), 'same_card' (another printing of the SAME
    card, by oracle id), 'unknown' (a printing the card data lacks), 'lookup_failed' (the card data could not say)}.
    The name must match - unless the picture's best match leads the second by at least thresholds['lead_waives_name'] (lead: decide_printing's answer; None: not given): then
    the picture has said which CARD it is better than a name OCR read (a Secret Lair prints another card's name; a name is misread), and an OCR that read nothing or another
    name no longer refuses it. Nor is the name needed when the whole collector line it read (set AND number) names EXACTLY the printing the picture chose ('chosen'):
    two independent readings agree on the printing - and a Secret Lair cannot fake that, it prints ANOTHER card's line. Either way, a line naming another printing of the same card, a printing the card data lacks, or one that cannot be looked up refuses it - the
    printing is the line's to check, which the lead says nothing about.
    And neither stands in for the name when the name it read is ANOTHER card's that a whole collector line names too (ocr['name_of_other_line']: the OCR's line or the
    strip's - the caller looked it up; absent: False): two readings then agree on a different card, a contradiction no lead overrules (a Codex review of 2026-10-21) - the
    card goes on to the OCR path and the AI as before the waiver.
    And a STRIP whose whole line names another printing of the same card (ocr['strip_names'] == 'same_card', looked up by the caller like line_names; absent: None)
    refuses whatever the name and the lead, exactly as the OCR's own line does: the picture chose A, a readable collector line says B (a second Codex review of
    2026-10-21). accept_printing lets a strip naming another indexed printing through to here; this is where it is checked.
    Returns {'accepted': bool, 'refused': None | 'ocr_did_not_confirm'}.
    """
    ocr = ocr or {}
    waive = (thresholds or CONFIRM_THRESHOLDS)['lead_waives_name']
    if ocr.get('strip_names') == 'same_card':
        return {'accepted': False, 'refused': OCR_DID_NOT_CONFIRM}       # the strip names another printing of this card
    if not ocr.get('name_matches') and ocr.get('name_of_other_line'):
        return {'accepted': False, 'refused': OCR_DID_NOT_CONFIRM}       # the name and a line both name another card
    name_ok = bool(ocr.get('name_matches')) or (lead is not None and lead >= waive) or ocr.get('line_names') == 'chosen'
    if name_ok and ocr.get('line_names') in (None, 'chosen', 'other_card'):
        return {'accepted': True, 'refused': None}
    return {'accepted': False, 'refused': OCR_DID_NOT_CONFIRM}
