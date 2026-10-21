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
