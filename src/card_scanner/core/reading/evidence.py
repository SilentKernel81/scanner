# evidence.py
# The shapes of what is known about a capture, written down once. Everything here is PLAIN DATA - strings, numbers, lists, dictionaries -
# and nothing in it refers to the database, the index or a picture: the code that GATHERS evidence (printing_check.py, later the reading
# pipeline) does every lookup, so that `decide.decide_printing(picture, strip)` makes no call at all. That is what makes each test case a
# complete file (tests/data/reading_*.json) and what lets another program - the Android app - pass the same files.
# (docs/plans/READING_REFACTOR_PLAN.md: "Evidence is plain data".)
#
# The TypedDicts are documentation that a type checker can read; at run time the values are ordinary dicts, as they come out of json.loads.

from typing import List, Optional, TypedDict


class Candidate(TypedDict):
    """One of the printings the picture looks like (nearest first in a Picture)"""
    id: str                       # the Scryfall id of the printing
    similarity: float             # -1..1, 1 = the same fingerprint (image_index.similarity); full precision, never rounded
    set: Optional[str]            # the printing's set code, as the card database has it (None: unknown)
    number: Optional[str]         # its collector number, as the card database has it (leading zeros, letters and all)
    oracle_id: Optional[str]      # the same for every printing of a card
    finishes: List[str]           # 'nonfoil', 'foil', 'etched' - what the printing exists in


class Picture(TypedDict):
    """What the picture says: the nearest printings by fingerprint, in both orientations of the card (the better one counts)"""
    candidates: List[Candidate]   # nearest first; at most 5; empty when there is no image index
    rotated: bool                 # the card was found upside down (the strip is then read from the card turned upright)


class Strip(TypedDict, total=False):
    """What the bottom-left strip says (the collector line and the set line)"""
    number: str                   # the collector number, no leading zeros ('0172' -> '172'); '' when it was not read
    set: str                      # the set code, lower case; '' when it was not read
    symbol: str                   # (phase 2b) the foil symbol the OCR read in the set line: 'foil', 'non-foil' or 'unknown'
    net: Optional[float]          # (phase 2b) the foil network's probability of a foil, when the OCR read no symbol; None otherwise


class Thresholds(TypedDict):
    agree_below: float            # a printing looks like the picture "as much as the best match" when its similarity is less than this below it
    image_lead: float             # the picture alone is believed when its best match leads the second by at least this


class PrintingDecision(TypedDict):
    """What decide_printing answers (rule A2 of docs/plans/IMAGE_MATCH_EXPERIMENT.md)"""
    path: str                     # 'strong' (the strip's printing, confirmed by the picture), 'image' (the picture alone) or 'review'
    printing_id: Optional[str]    # None for 'review'
    similarity: Optional[float]   # of that printing; None for 'review'
    lead: Optional[float]         # the best match's lead over the second; None with fewer than two candidates


class FinishDecision(TypedDict):
    """What decide_finish answers (phase 2b); its own rule, never part of rule A2's answer"""
    finish: str                   # 'foil', 'regular' or 'unknown' (an unread marker is not 'regular')
    source: str                   # 'printing' (it exists in one finish), 'symbol' (the OCR read it), 'network' (the foil network was sure) or 'none'
