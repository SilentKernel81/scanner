# printing_check.py
# Does the picture look like the printing the server resolved? A second opinion that does not go
# through the OCR or the AI - and, in SHADOW MODE (the only mode there is for now), one that only
# writes down where it disagrees and changes nothing.
#
# What it is for (docs/plans/IMAGE_MATCH_EXPERIMENT.md): the collector line does not always name the
# printing (a Secret Lair card prints the set and number of the card it is made from, so the server
# adds the base printing), and the vision AI misreads at least 12% of the cards it handles (a name and
# a number that agree with each other and with nothing in the picture).
#
# The rule: the capture and every printing in the image index are compared by their 96-byte fingerprints
# (image_index.py), in both orientations (a card upside down would otherwise match a wrong card with a
# clear lead: 2% of them did in the experiment). With R the printing the server resolved:
#       gap = similarity(best match) - similarity(R)
#   gap below AGREE_BELOW        'agree'     R looks like the picture as well as the best match does (a reprint with
#                                            the same art has almost the same fingerprint, so it agrees too)
#   gap from DISAGREE_FROM       'disagree'  the picture looks like something else: kind 'other_card' (the best match
#                                            is another card: a misread) or 'same_card' (another printing of the
#                                            same card: the printed line named the wrong printing)
#   between                      'weak'      written down, not counted as a disagreement
#   R not in the index           'not_indexed'  (a printing newer than the index)
# The two thresholds come from the experiment's data (scripts/experiments/image_match/calibrate_gap.py):
# among 10,677 pictures with labels not known to be wrong the gap was 0 for 95%, and 0.036 at the 99th
# percentile; the 20 labels known to be wrong all had a gap of 0.089 or more. At 0.06, 0.68% of the right
# labels would have been flagged (some of those are probably labels that were wrong too). 20 wrong cases
# is a small sample: that is what shadow mode is for. Measure on live scans before it changes anything.
#
# THE LOOKUP (since 2026-10-20, also shadow only) is the second half: what the picture and the strip ALONE would have
# answered, with no vision AI - to compare on live scans with what the AI answered before anything is switched off
# (the exit criteria are in docs/PROGRAM_DOCUMENTATION.md, "Printing check"). The rule is the experiment's rule A2:
# the printing the strip names (number AND set) if it is one of the picture's five nearest and looks as much like the
# picture as the best match; else the best match if it leads the second by IMAGE_LEAD; else it would send the card to
# review. By default it runs for the captures the AI read and for those nothing was read for ('ai_cases'): the cards the
# AI would no longer be needed for, ~3% of the captures; 'all' adds the OCR-confirmed ones at one more small OCR request each.
#
# Shadow mode writes one JSON line per card it looked at to data/logs/printing_check.jsonl, and a line in
# the log for each 'disagree'. `python scripts/printing_check_report.py` sums them up. The work is done by
# one background thread behind a small queue: a capture never waits for it, and nothing it does can fail one.

import json
import logging
import queue
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from card_scanner.core import card_ocr, foil_marker, image_index
# The decision itself is a pure function of plain data (core/reading/decide.py, docs/plans/READING_REFACTOR_PLAN.md); this module GATHERS the evidence
# for it. AGREE_BELOW and IMAGE_LEAD are defined there and kept here under the names this module always exported.
from card_scanner.core.reading.decide import AGREE_BELOW, IMAGE_LEAD, decide_finish, decide_printing, plain_number

logger = logging.getLogger('ai')

DISAGREE_FROM = 0.06
# Guards for a decision made by the picture ALONE (live_read, reading.mode 'evidence'). The lead over the second printing is relative: with an index that
# lacks the card's printing (newer than the index, or a stale index) an unrelated best match can still lead. Measured with
# scripts/experiments/image_match/calibrate_uncovered.py on 428 real pictures, each also with its own printing removed from the index (every add is then wrong):
# the lead alone still added 36%; the strip rule below brought that to 5.4% at no cost to covered cards; adding this floor, 4.0% at -1.1 points of covered
# cards (they go to the AI). A higher floor costs more than it helps: a covered camera picture is only 0.66 similar to Scryfall's scan (median), and the
# same-art reprints of an uncovered one stay in the index and score high.
IMAGE_MIN_SIMILARITY = 0.5
# A 'strong' decision (the strip names a printing the picture agrees with) needs a floor of its own, far lower: a Codex review of 2026-10-20 showed that without one a collector line
# naming ANY of the five nearest printings was enough, even when every candidate looked unlike the capture (a probe gave 'strong' at a similarity of 0.10) - a degraded capture with a
# misread collector line would then bypass the AI and be added. Measured (scripts/experiments/image_match/calibrate_strong.py, 1,517 'strong' decisions on real pictures, none wrong):
# the printings they pick have a similarity of at least 0.245 (1st percentile 0.355, median 0.68); 0.30 refuses 5 of them (0.33%, they go to the AI) and everything near the 0.10 of
# the probe. A backstop calibrated against the correct ones - no wrong 'strong' decision exists in the data to calibrate against.
STRONG_MIN_SIMILARITY = 0.30
EVIDENCE_READER = 'picture+strip'     # what a card read by live_read is said to be read by (route_identified's `reader`, the scanned-cards log)
LOOKUP_MODES = ('ai_cases', 'all', 'off')
QUEUE_SIZE = 200                  # captures waiting to be looked at: more are dropped (it is only a second opinion)
LOG_LIMIT = 10 * 1024 * 1024      # the log is renamed to .1 beyond this (the old .1 goes)


def read_strip(ocr, card_rgb):
    """
    (number, set_code) the OCR reader (card_ocr.CardOcr) reads from the bottom-left strip of a flat card - the collector line and the
    set line, enlarged when the card image is small (foil_marker.strip_of): ('', '') when it read neither. One more small request.
    """
    lines = ocr.read_lines(foil_marker.strip_of(card_rgb))
    return card_ocr.parse_strip(lines) if lines else ('', '')


def read_strip_evidence(ocr, card_rgb, net=None):
    """
    The strip's evidence in full (phase 2b): {'number', 'set', 'symbol', 'net'} from ONE read of the bottom-left strip. symbol: the foil symbol the OCR read in the
    set line ('foil', 'non-foil', 'unknown'); net: the foil network's probability of a foil - asked only when the OCR read no symbol and there is a network (a
    foil_marker.FoilMarker; a separate instance from the live path's) - else None. Uses foil_marker's helpers, and leaves foil_marker.read (the live path) alone.
    """
    strip = foil_marker.strip_of(card_rgb)
    lines = ocr.read_lines(strip)
    if not lines:
        return {'number': '', 'set': '', 'symbol': 'unknown', 'net': None}
    number, set_code = card_ocr.parse_strip(lines)
    line = foil_marker.set_line(lines)
    symbol = foil_marker.symbol_read(line) if line else 'unknown'
    probability = None
    if symbol == 'unknown' and net is not None and line is not None:
        crop = foil_marker.set_line_crop(strip, line['box'])
        if crop is not None:
            probability = net.probability(crop)
    return {'number': number, 'set': set_code, 'symbol': symbol, 'net': probability}


class StripReader:
    """
    The lookup's strip reader: a light-ocr reader OF ITS OWN, never the live one. A second light-ocr process, started on the first strip it is
    asked for and stopped when it has been idle for `idle_stop` seconds (the lookup is for ~3% of the captures: the models need not stay in
    memory), one request at a time. Why not the live reader (a review of 2026-10-20): `CardOcr.read_lines` holds that reader's request lock
    while it waits, so a slow shadow request would hold up the live OCR worker for up to its 10 s time-out, and a time-out stops the shared
    process and starts its 60 s cool-down - after which the live captures go to the AI. Shadow mode must not be able to do that: here a stalled or
    failed read can only spoil this reader.
    Called with a flat card (RGB) it returns (number, set_code), ('', '') for what was not read or while OCR is off (ocr_enabled(): the person's
    choice, and it would start a reader); an exception from the reader goes to the caller (PrintingCheck, which goes on by the picture alone).
    `read` gives the strip's evidence in full (read_strip_evidence: also the foil symbol and, with `foil_net`, the network's probability) from the same one read.
    """

    IDLE_STOP = 120

    def __init__(self, ocr_enabled=lambda: True, make_reader=None, idle_stop=IDLE_STOP, foil_net=None):
        self.ocr_enabled = ocr_enabled
        self._make = make_reader or card_ocr.CardOcr
        self._idle_stop = idle_stop
        self._net = foil_net                          # a foil_marker.FoilMarker of its own (not the live path's), or None
        self._reader = None
        self._last_used = 0.0
        self._timer = None
        self._lock = threading.Lock()                 # one request at a time; also guards starting and stopping

    def read(self, rgb):
        """The strip's evidence in full ({'number', 'set', 'symbol', 'net'}; read_strip_evidence), or nothing while OCR is off"""
        if not self.ocr_enabled():
            return {'number': '', 'set': '', 'symbol': 'unknown', 'net': None}
        with self._lock:
            if self._reader is None:
                self._reader = self._make()
            self._last_used = time.monotonic()
            self._arm()
            return read_strip_evidence(self._reader, rgb, self._net)

    def __call__(self, rgb):
        evidence = self.read(rgb)
        return evidence['number'], evidence['set']

    def _arm(self):
        """(Re)start the idle timer: the reader stops idle_stop seconds after the last strip"""
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self._idle_stop, self._idle)
        self._timer.daemon = True
        self._timer.start()

    def _idle(self):
        with self._lock:                              # waits for a request in progress
            if time.monotonic() - self._last_used >= self._idle_stop:    # not used again since this timer was set
                self._stop()

    def _stop(self):
        if self._reader is not None:
            try:
                self._reader.stop()
            except Exception:
                pass
            self._reader = None

    def close(self):
        """Stops the reader now (the server's shutdown)"""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._stop()


_plain_number = plain_number      # the name this module had for it; the function now lives in core/reading/decide.py


def verdict_for(gap):
    """'agree' / 'weak' / 'disagree' for a gap (best match's similarity minus the resolved printing's)"""
    if gap < AGREE_BELOW:
        return 'agree'
    return 'disagree' if gap >= DISAGREE_FROM else 'weak'


class PrintingCheck:
    """
    Looks at the cards the server resolved (the check) and at what the picture and the strip alone would have answered (the lookup).
    mode: 'shadow' (log only) or 'off'.
    lookup: which captures also get the lookup - 'ai_cases' (those the vision AI read, or nothing read: the cards the AI would no longer
    be needed for), 'all' (every capture: the OCR-confirmed ones show how often the picture agrees on easy cards, at the price of one
    more small OCR request for each) or 'off'.
    strip_reader: a function (RGB flat card) -> (number, set_code) for the lookup (read_strip with the server's OCR reader), or None:
    the lookup then goes by the picture alone.
    """

    def __init__(self, database, index_folder, log_file, mode='shadow', lookup='ai_cases', strip_reader=None, finish_for=None):
        self.database = database
        self.index_folder = Path(index_folder)
        self.log_file = Path(log_file)
        self.mode = mode if mode in ('shadow', 'off') else 'shadow'
        self.lookup_mode = lookup if lookup in LOOKUP_MODES else 'ai_cases'
        self.strip_reader = strip_reader
        # finish_for(card, foil): the finish the SERVER chose for a card it added ('regular', 'foil', 'surge'; Game.suggested_finish), given the marker
        # it read; the server hands it in so that core/ needs no game code. None: the finish evidence is logged without a comparison.
        self.finish_for = finish_for
        self._index = None
        self._index_stamp = 'not looked at yet'   # meta.json's modification time when the index was loaded (None: no index)
        self._queue = queue.Queue(QUEUE_SIZE)
        self._thread = None
        self._lock = threading.Lock()       # starting the thread, loading the index
        self._said_no_index = False
        self._failures = 0

    # ------------------------------------------------------------------ the index

    def index(self):
        """The image index, loaded when first needed and again when it was rebuilt; None when there is none (said once in the log)"""
        meta = self.index_folder / image_index.MANIFEST      # replaced whenever a new generation is published
        try:
            stamp = meta.stat().st_mtime_ns
        except OSError:
            stamp = None
        with self._lock:
            if stamp != self._index_stamp:     # first call, or the index was rebuilt (an unusable one is not tried again until it changes)
                self._index = image_index.ImageIndex.load(self.index_folder) if stamp is not None else None
                self._index_stamp = stamp
                if self._index is not None:
                    logger.info(f"Printing check: image index loaded ({len(self._index)} printings, built {self._index.built})")
            if self._index is None and not self._said_no_index:
                self._said_no_index = True
                logger.info("Printing check: no image index (build one with scripts/build_image_index.py): nothing is checked")
            return self._index

    # ------------------------------------------------------------------ the check

    def _search(self, rgb):
        """
        The capture against every printing, in both orientations (the better one counts): the index, the distance to every row, whether the
        card was upside down, and the five nearest rows with their similarities (nearest first). None without an index.
        """
        index = self.index()
        if index is None or not len(index):
            return None
        upright = index.distances(image_index.hash_card(rgb))
        turned = index.distances(image_index.hash_card(cv2.rotate(rgb, cv2.ROTATE_180)))
        rotated = int(turned.min()) < int(upright.min())      # the card was put in upside down
        distances = turned if rotated else upright
        k = min(5, len(distances))
        top = np.argpartition(distances, k - 1)[:k]
        top = top[np.argsort(distances[top], kind='stable')]
        return {'index': index, 'distances': distances, 'rotated': rotated, 'top': top, 'sims': image_index.similarity(distances[top])}

    def check(self, rgb, printing_id, search=None):
        """
        The verdict for a capture (RGB array) and the printing the server resolved for it: a dict, or None without an index.
        {'verdict', 'kind', 'gap', 'lead', 'rotated', 'best': {'id', 'similarity'}, 'resolved_similarity'}
        search: what _search gave for this picture, when the caller has it already.
        """
        search = search or self._search(rgb)
        if search is None:
            return None
        index, distances, top, sims = search['index'], search['distances'], search['top'], search['sims']
        result = {'rotated': search['rotated'], 'best': {'id': index.ids[int(top[0])], 'similarity': round(float(sims[0]), 4)},
                  'lead': round(float(sims[0] - sims[1]), 4) if len(top) > 1 else None}
        row = index.row(printing_id)
        if row is None:
            return {**result, 'verdict': 'not_indexed', 'kind': None, 'gap': None, 'resolved_similarity': None}
        resolved_similarity = float(image_index.similarity(distances[row]))
        gap = float(sims[0]) - resolved_similarity
        result.update(gap=round(gap, 4), resolved_similarity=round(resolved_similarity, 4), verdict=verdict_for(gap), kind=None)
        if result['verdict'] != 'agree':
            result['kind'] = self._kind(printing_id, index.ids[int(top[0])])
        return result

    def _strip(self, rgb):
        """(number, set_code) the strip reader gives, or ('', '') without one or when it fails: the lookup then goes by the picture alone"""
        if self.strip_reader is None:
            return '', ''
        try:
            return self.strip_reader(rgb)
        except Exception as e:
            logger.debug(f"Printing check: the strip could not be read: {e}")
            return '', ''

    def _strip_evidence(self, rgb, search):
        """
        The strip's evidence in full - {'number', 'set', 'symbol', 'net'} - from ONE read, of the card the right way up (a card the search found upside down is turned
        back first). A strip reader that has `read` (StripReader) gives it all; a plain function gives (number, set) and the foil evidence is 'unknown' / None.
        """
        upright = cv2.rotate(rgb, cv2.ROTATE_180) if search['rotated'] else rgb
        try:
            if self.strip_reader is None:
                return {'number': '', 'set': '', 'symbol': 'unknown', 'net': None}
            if hasattr(self.strip_reader, 'read'):
                return self.strip_reader.read(upright)
            number, set_code = self.strip_reader(upright)
            return {'number': number, 'set': set_code, 'symbol': 'unknown', 'net': None}
        except Exception as e:
            logger.debug(f"Printing check: the strip could not be read: {e}")
            return {'number': '', 'set': '', 'symbol': 'unknown', 'net': None}

    def lookup(self, rgb, resolved_id=None, search=None, strip=None):
        """
        What the picture and the strip ALONE would have answered, with no OCR of the whole card and no vision AI - the answer that would
        replace the AI's. The rule is the experiment's rule A2 (docs/plans/IMAGE_MATCH_EXPERIMENT.md, section 4):
          'strong'  the strip names a printing (number AND set) that is one of the picture's five nearest and looks as much like the
                    picture as the best match does (a reprint with the same art has almost the same fingerprint: the strip decides
                    among those) - that printing;
          'image'   else, the picture's best match when it leads the second by IMAGE_LEAD (0.05);
          'review'  else: it would not decide.
        The answer is compared with the printing the server resolved (resolved_id, from the OCR or the AI) when there is one:
        'against_resolved' is 'same_printing', 'same_card' (another printing of the card), 'other_card', 'picture_review' (it would not
        have decided) or, with no resolved printing, 'unresolved'. None without an index.
        {'path', 'printing': {'id', 'name', 'set', 'number', 'similarity'} or None, 'strip': {'number', 'set'}, 'lead', 'rotated', 'against_resolved'}
        """
        search = search or self._search(rgb)
        if search is None:
            return None
        index, top, sims = search['index'], search['top'], search['sims']
        # The strip is at the bottom left of the card the right way up: a card that was put in upside down (the search found its
        # fingerprint turned) is turned back first, or the strip cut would be the title area and the collector lines would be missed.
        # `strip`: the evidence the caller has read already (the shadow worker reads it once for the lookup and the finish)
        strip = strip or self._strip_evidence(rgb, search)
        number, set_code = strip['number'], strip['set']
        # The evidence as plain data (core/reading/evidence.py), every lookup done here - and then a pure function decides
        picture, cards = self._picture_evidence(search)
        decision = decide_printing(picture, {'number': number, 'set': set_code})
        printing = None
        if decision['printing_id'] is not None:
            card = cards[decision['printing_id']]
            printing = {'id': decision['printing_id'], 'name': card.get('name'), 'set': card.get('set_code'), 'number': card.get('number'),
                        'similarity': round(decision['similarity'], 4)}
        lead = decision['lead']
        return {'path': decision['path'], 'printing': printing, 'strip': {'number': number, 'set': set_code} if number or set_code else None,
                'lead': round(lead, 4) if lead is not None else None, 'rotated': search['rotated'],
                'against_resolved': self._against(resolved_id, printing)}

    def _picture_evidence(self, search):
        """
        What the picture says, as plain data (evidence.Picture), and the card dicts behind its candidates ({printing id: card}; {} for a
        printing the card database does not have). Every database read the decision needs is done here.
        """
        index = search['index']
        cards, candidates = {}, []
        for row, similarity in zip(search['top'], search['sims']):
            printing_id = index.ids[int(row)]
            card = cards[printing_id] = self.database.get_card_by_id(printing_id) or {}
            candidates.append({'id': printing_id, 'similarity': float(similarity), 'set': card.get('set_code'), 'number': card.get('number'),
                               'oracle_id': card.get('oracle_id'), 'finishes': card.get('finishes') or []})
        return {'rotated': bool(search['rotated']), 'candidates': candidates}, cards

    # ------------------------------------------------------------------ the live reading (reading.mode: evidence)

    def live_read(self, image, card_image, ocr, foil_net=None, accept_image_alone=False):
        """
        The reading of a capture by its picture and its strip - the live counterpart of lookup, used by Identification only in reading.mode
        'evidence' (docs/plans/READING_REFACTOR_PLAN.md, phase 3), for a card the full-card OCR did not confirm, in place of asking the vision AI.
        image: the capture (RGB); card_image: the flat card (the perspective-corrected one when there is one, else image). BOTH the fingerprint and
        the strip are made of the flat card: a fixed area's `image` is the area as drawn, not a card, and the index was built from whole-card pictures
        (the shadow log hashes the saved capture file, so its numbers for such a station carry that difference). ocr: the LIVE reader (here the read IS the live path - the shadow lookup's isolated reader exists to protect
        the live one from shadow work); foil_net: the foil network, asked only where the OCR read no symbol.
        Returns None when the picture cannot speak (no index, nothing found, or a candidate the card database does not have) and for a decision
        of 'review' - the caller then goes on as it always did; or {'path', 'card' (the database's card of the decided printing), 'strip',
        'finish' (decide_finish's answer), 'similarity', 'lead'}. 'image' (the picture alone; OFF unless asked for: a Codex review of 2026-10-20 - a printing newer than the index with an unreadable strip can still be added by it) is returned only with accept_image_alone.
        Raises nothing the caller must handle except what the OCR reader raises.
        """
        card_image = image if card_image is None else card_image
        search = self._search(card_image)
        if search is None:
            return None
        upright = cv2.rotate(card_image, cv2.ROTATE_180) if search['rotated'] else card_image
        strip = read_strip_evidence(ocr, upright, foil_net)
        picture, cards = self._picture_evidence(search)
        decision = decide_printing(picture, {'number': strip['number'], 'set': strip['set']})
        if decision['path'] == 'review' or (decision['path'] == 'image' and not accept_image_alone):
            return None
        card = cards.get(decision['printing_id'])
        if not card:                                   # an indexed printing the card database no longer has: not a card to add
            return None
        if decision['similarity'] < (IMAGE_MIN_SIMILARITY if decision['path'] == 'image' else STRONG_MIN_SIMILARITY):
            return None                                # a picture that looks little like the printing decides nothing, not even with a collector line that names it
        if decision['path'] == 'image' and self._names_unindexed(search['index'], strip):
            return None                                # the picture alone is not enough for a card the index may not have (see IMAGE_MIN_SIMILARITY)
        return {'path': decision['path'], 'card': card, 'strip': {'number': strip['number'], 'set': strip['set']},
                'finish': decide_finish(card.get('finishes'), strip['symbol'], strip['net']),
                'similarity': decision['similarity'], 'lead': decision['lead']}

    def _names_unindexed(self, index, strip):
        """
        Whether the strip names a printing the card database has and the image index LACKS: then the picture cannot have seen that printing, and its best
        match is another card's (or another printing's) - no add by the picture alone. Fails closed: if the card database cannot say, True.
        """
        if not (strip['number'] and strip['set']):
            return False
        try:
            named = self.database.get_card_by_set_number(strip['set'], strip['number'])
        except Exception as e:
            logger.debug(f"Printing check: the strip's printing could not be looked up: {e}")
            return True
        return bool(named) and index.row(named['id']) is None

    def candidates(self, image_path):
        """
        The printings a capture's picture looks like, nearest first (at most five), for the review screen: [{'id', 'name', 'set', 'set_code', 'number', 'image_uri',
        'finishes', 'similarity'}] - plain Python values, ready for JSON; [] without an index, without the picture, or for a printing the card database
        lacks (left out). Display only: a person picks. The picture is the file as given: for a station that sends a fixed area it is the area as drawn,
        not a flat card (the flat copy is not kept for a review item), so the candidates of such a capture are poorer.
        """
        image = cv2.imread(str(image_path)) if image_path else None
        if image is None:
            return []
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        search = self._search(rgb)
        if search is None:
            return []
        picture, cards = self._picture_evidence(search)
        found = []
        for candidate in picture['candidates']:
            card = cards.get(candidate['id'])
            if card:
                found.append({'id': card['id'], 'name': card.get('name'), 'set': card.get('set'), 'set_code': card.get('set_code'), 'number': card.get('number'),
                              'image_uri': card.get('image_uri'), 'finishes': list(card.get('finishes') or []), 'similarity': round(float(candidate['similarity']), 4)})
        return found

    def contradicts(self, image, printing_id):
        """
        Whether the picture clearly looks like ANOTHER PRINTING OF THE SAME CARD than printing_id: the 'disagree' verdict of the check with kind
        'same_card' - the Secret Lair case (the card prints the set and number of the card it is made from; the OCR reads the printed line and
        confirms the wrong printing, silently). Only that kind: a 'disagree' for another card is, on correctly read cards, mostly a foil's glare or a
        showcase background (0.78% of the OCR-confirmed pictures, 76 of 82 of them other cards, three of the four looked at false alarms) - the
        same_card ones were 6 of 10,568, all genuine (scripts/experiments/image_match/measure_contradiction.py). False without an index.
        """
        result = self.check(image, printing_id)
        return bool(result and result['verdict'] == 'disagree' and result['kind'] == 'same_card')

    def verdict_of(self, image, printing_id):
        """
        'agree' / 'weak' / 'disagree' / 'not_indexed' for a capture and a printing the server was told (the vision AI's answer, looked up),
        or None without an index. The check of an AI answer against the picture in reading.mode 'evidence'.
        """
        result = self.check(image, printing_id)
        return result['verdict'] if result else None

    def _against(self, resolved_id, printing):
        """How the lookup's printing stands to the one the server resolved (see lookup)"""
        if resolved_id is None:
            return 'unresolved'
        if printing is None:
            return 'picture_review'
        if printing['id'] == resolved_id:
            return 'same_printing'
        return self._kind(resolved_id, printing['id']) or 'other_card'

    def _kind(self, resolved_id, best_id):
        """'same_card' when the best match is another printing of the resolved card, else 'other_card'"""
        try:
            resolved, best = self.database.get_card_by_id(resolved_id), self.database.get_card_by_id(best_id)
            same = bool(resolved and best and resolved.get('oracle_id') and resolved.get('oracle_id') == best.get('oracle_id'))
        except Exception:
            return None
        return 'same_card' if same else 'other_card'

    # ------------------------------------------------------------------ shadow mode

    def wants_lookup(self, reader, resolved):
        """Whether a capture gets the lookup: reader is what read the card ('light-ocr' or the AI's name), resolved the card found or None"""
        if self.lookup_mode == 'all':
            return True
        if self.lookup_mode == 'off':
            return False
        # 'ai_cases': the vision AI read it, or no card was resolved (whoever read it: an OCR read the database does not know is a card
        # for review too) - the cards the AI is, or would be, needed for
        return resolved is None or reader != card_ocr.READER

    def observe(self, image_path, card, station=None, reader=None, foil=None):
        """
        Shadow mode: look at a capture and the card the server resolved for it (a card dict with 'id'; None when nothing was read or found
        - the cases where the AI gave no answer), in the background. The check compares the picture with that card; the lookup (for the
        captures wants_lookup says) works out what the picture and the strip alone would have answered. reader: what read the card.
        foil: the marker the server read for the capture ('foil', 'non-foil', 'unknown'; None = not given), for the comparison of the finish.
        Returns at once and never raises: the capture goes on whatever happens here.
        """
        if reader == EVIDENCE_READER:
            return          # the picture and the strip chose this printing: comparing the picture with it would only compare it with itself
        resolved = card if card and card.get('id') else None
        lookup = self.wants_lookup(reader, resolved)
        if self.mode != 'shadow' or not image_path or (resolved is None and not lookup):
            return
        try:
            self._start()
            self._queue.put_nowait((str(image_path), dict(resolved) if resolved else None, station, reader, lookup, foil))
        except queue.Full:
            pass                          # busy: this one is not looked at
        except Exception as e:
            logger.debug(f"Printing check could not queue a capture: {e}")

    def close(self):
        """The server's shutdown: lets go of the strip reader's process, if it has one (a plain function has nothing to close)"""
        close = getattr(self.strip_reader, 'close', None)
        if close:
            close()

    def _start(self):
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, daemon=True, name="Printing-check")
                self._thread.start()

    def _work(self):
        while True:
            job = self._queue.get()
            try:
                self._look(*job)
            except Exception as e:        # a bad picture, a full disk: written down, never raised (a few times, then silent)
                self._failures += 1
                if self._failures <= 5:
                    logger.warning(f"Printing check failed on {Path(job[0]).name}: {e}")
            finally:
                self._queue.task_done()

    def wait(self, timeout=30):
        """Until what is queued has been looked at (tests, scripts); False if it takes longer than timeout"""
        done = threading.Event()
        threading.Thread(target=lambda: (self._queue.join(), done.set()), daemon=True).start()
        return done.wait(timeout)

    def _look(self, image_path, card, station, reader, lookup, foil=None):
        image = cv2.imread(image_path)
        if image is None:                 # the picture is gone (cleaned up, or a path that does not exist)
            return
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        search = self._search(rgb)
        if search is None:
            return
        record = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'station': station, 'capture': Path(image_path).name, 'reader': reader,
                  'verdict': None, 'kind': None, 'gap': None, 'lead': None, 'rotated': search['rotated'], 'resolved': None, 'best': None}
        result = self.check(rgb, card['id'], search) if card else None
        best = {}
        if result:
            best = self.database.get_card_by_id(result['best']['id']) or {}
            record.update(verdict=result['verdict'], kind=result['kind'], gap=result['gap'], lead=result['lead'],
                          resolved={'id': card['id'], 'name': card.get('name'), 'set': card.get('set_code'), 'number': card.get('number'),
                                    'similarity': result['resolved_similarity']},
                          best={'id': result['best']['id'], 'name': best.get('name'), 'set': best.get('set_code'),
                                'number': best.get('number'), 'similarity': result['best']['similarity']})
        if lookup:
            strip = self._strip_evidence(rgb, search)       # ONE read of the strip serves the lookup and the finish
            record['lookup'] = self.lookup(rgb, card['id'] if card else None, search, strip)
            record['finish'] = self._finish(card, record['lookup'], strip, foil)
        self._append(record)
        if result and result['verdict'] == 'disagree':
            logger.info(f"Printing check (shadow, nothing changed): {card.get('name')} [{card.get('set_code')} {card.get('number')}] was added, "
                        f"the picture looks like {best.get('name')} [{best.get('set_code')} {best.get('number')}] "
                        f"({result['kind']}, gap {result['gap']:.3f}, {Path(image_path).name})")
        answer = record.get('lookup')
        if answer and answer['against_resolved'] in ('same_card', 'other_card'):
            p, name = answer['printing'], card.get('name') if card else None
            logger.info(f"Printing check (shadow, nothing changed): read by {reader or 'the AI'} as {name} [{card.get('set_code')} {card.get('number')}], "
                        f"the picture and the strip would say {p['name']} [{p['set']} {p['number']}] ({answer['path']}, "
                        f"{answer['against_resolved']}, {Path(image_path).name})")

    def _finish(self, card, answer, strip, foil):
        """
        The finish evidence of a capture, in a field of its own (never part of the lookup's answer). The shadow finish is worked out for the printing the LOOKUP
        selected - never the server's: with the server's printing, a wrongly resolved printing that exists in one finish only would force both finishes to agree and
        hide the disagreement this evidence is there to show. Beside it, the finish the server chose for the card it added (finish_for; the server's own printing)
        and whether the two agree ('same' is None when either side does not say: an unread marker is 'unknown', not 'regular'). When the lookup selected no printing
        (review) there is no shadow finish: 'decision' is None. None without a strip.
        """
        if not strip:
            return None
        printing = None
        if answer and answer.get('printing'):
            printing = self.database.get_card_by_id(answer['printing']['id'])
        decision = decide_finish(printing.get('finishes'), strip['symbol'], strip['net']) if printing else None
        chosen = None
        if card is not None and self.finish_for is not None:
            try:
                chosen = self.finish_for(card, foil or 'unknown')
            except Exception as e:
                logger.debug(f"Printing check: the server's finish could not be worked out: {e}")
        server = None if chosen is None else ('regular' if chosen == 'regular' else 'foil')     # a surge foil is a foil here
        shadow = decision['finish'] if decision else None
        return {'printing': answer['printing']['id'] if printing else None,
                'decision': shadow, 'source': decision['source'] if decision else None, 'symbol': strip['symbol'],
                'net': None if strip['net'] is None else round(strip['net'], 4), 'server': chosen,
                'same': None if server is None or shadow in (None, 'unknown') else server == shadow}

    def _append(self, record):
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.log_file.stat().st_size > LOG_LIMIT:
                self.log_file.replace(self.log_file.with_name(self.log_file.name + '.1'))
        except OSError:
            pass
        with self.log_file.open('a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')


# ---------------------------------------------------------------------------------------------
# The report (scripts/printing_check_report.py)
# ---------------------------------------------------------------------------------------------

def report(log_file, show=15, out=print):
    """Sums up the shadow log: how many cards, how many agreed, and the latest disagreements (name, printing, what it looks like)"""
    log_file = Path(log_file)
    if not log_file.is_file():
        out(f"No {log_file.name} yet: shadow mode has looked at nothing (is there an image index, and has a card been scanned since?)")
        return
    records = []
    for line in log_file.read_text(encoding='utf-8').splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            pass
    out(f"{len(records)} captures looked at ({records[0]['time'] if records else '-'} to {records[-1]['time'] if records else '-'})")

    # --- the check: does the picture look like the printing the server resolved? ---
    checked = [r for r in records if r.get('verdict')]
    verdicts = {}
    for r in checked:
        verdicts[r['verdict']] = verdicts.get(r['verdict'], 0) + 1
    out(f"\nTHE CHECK - {len(checked)} resolved cards: " + ', '.join(f"{n} {v}" for v, n in sorted(verdicts.items(), key=lambda kv: -kv[1])))
    kinds = {}
    for r in checked:
        if r['verdict'] == 'disagree':
            kinds[r['kind']] = kinds.get(r['kind'], 0) + 1
    if kinds:
        out("  disagreements by kind: " + ', '.join(f"{n} {k}" for k, n in kinds.items())
            + "   (other_card: probably a misread; same_card: the printed line names another printing)")
    out(f"  the latest {show} disagreements:")
    for r in [r for r in checked if r['verdict'] == 'disagree'][-show:]:
        a, b = r['resolved'], r['best']
        out(f"    {r['time']} {r['capture']}: added {a['name']} [{a['set']} {a['number']}], the picture looks like {b['name']} [{b['set']} {b['number']}] "
            f"- {r['kind']}, gap {r['gap']:.3f}, lead {r['lead']}{', card upside down' if r['rotated'] else ''}")

    # --- the lookup: what the picture and the strip alone would have answered ---
    looked = [r for r in records if r.get('lookup')]
    if not looked:
        out("\nTHE LOOKUP - none yet (printing_check.lookup in config.yaml: ai_cases, all or off)")
        return
    percent = lambda n, whole: f"{100 * n / whole:.1f}%" if whole else '-'
    ai = [r for r in looked if r.get('reader') != card_ocr.READER]           # the AI read it, or nothing did: where the AI would go
    easy = [r for r in looked if r.get('reader') == card_ocr.READER]
    for title, group in (("cards the AI handled (read by the vision AI, or not read at all)", ai), ("cards the OCR confirmed", easy)):
        if not group:
            continue
        paths = {}
        for r in group:
            paths[r['lookup']['path']] = paths.get(r['lookup']['path'], 0) + 1
        decided = paths.get('strong', 0) + paths.get('image', 0)
        out(f"\nTHE LOOKUP - {title}: {len(group)} compared")
        out(f"  the picture and the strip would add by themselves {decided} ({percent(decided, len(group))}: {paths.get('strong', 0)} strong, "
            f"{paths.get('image', 0)} by the picture alone) and send {paths.get('review', 0)} ({percent(paths.get('review', 0), len(group))}) to review")
        against = {}
        for r in group:
            against[r['lookup']['against_resolved']] = against.get(r['lookup']['against_resolved'], 0) + 1
        out("  against what the server resolved: " + ', '.join(f"{n} {k}" for k, n in sorted(against.items(), key=lambda kv: -kv[1])))
        if against.get('unresolved'):
            unresolved = [r for r in group if r['lookup']['against_resolved'] == 'unresolved']
            added = sum(1 for r in unresolved if r['lookup']['path'] != 'review')
            out(f"  of the {len(unresolved)} nothing was resolved for, the picture and the strip would have added {added}")
    out(f"\n  exit criteria for dropping the AI (written beforehand): about 300 AI-handled cards compared - {len(ai)} so far; at least 85% added by "
        f"the picture and the strip - {percent(sum(1 for r in ai if r['lookup']['path'] != 'review'), len(ai))} so far; at most 0.5% wrong - "
        "that needs the disagreements below judged BY EYE (a disagreement with the AI is not an error of the picture: the AI misreads ~12%)")
    disagreements = [r for r in looked if r['lookup']['against_resolved'] in ('same_card', 'other_card')]
    out(f"  the latest {min(show, len(disagreements))} of the {len(disagreements)} disagreements between the AI / OCR and the picture + strip, to look at:")
    for r in disagreements[-show:]:
        a, p, s = r.get('resolved') or {}, r['lookup']['printing'], r['lookup']['strip']
        out(f"    {r['time']} {r['capture']} (read by {r.get('reader')}): {a.get('name')} [{a.get('set')} {a.get('number')}] -> the picture and the strip say "
            f"{p['name']} [{p['set']} {p['number']}], {r['lookup']['path']}, {r['lookup']['against_resolved']}"
            f"{', strip ' + s['set'] + ' ' + s['number'] if s else ', strip unread'}")


def main(argv=None):
    """scripts/printing_check_report.py [--show N]"""
    import argparse
    from card_scanner.config import Config
    parser = argparse.ArgumentParser(description="What the printing check (shadow mode) has found")
    parser.add_argument('--show', type=int, default=15, help="how many of the latest disagreements to list")
    report(Config.DATA_DIR / 'logs' / 'printing_check.jsonl', parser.parse_args(argv).show)


if __name__ == '__main__':
    main()
