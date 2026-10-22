# foil_marker.py
# The foil marker - the star (foil) or dot (regular) between a card's set code and its language
# ("HOB ★ EN" against "HOB • EN") - read by a small network, with no vision AI.
#
# Why it exists: the full-card OCR (card_ocr.py) misses that one small symbol on about a third of
# the foils, and then the finish used to default to "regular" (Game.suggested_finish) unless the
# AI's foil check answered. This reads the symbol from the set line itself.
#
# What it does, in the order training did it (scripts/experiments/image_match/foil_crops.py,
# foil_train.py - written out again here, and every step must stay what training did, or the
# numbers below no longer apply):
#   1. the bottom-left STRIP of the flat card (lowest 14%, left 62%) is cut out and, if the card
#      image is smaller than ~1000 px high, enlarged to that size;
#   2. the OCR reader reads the strip again (a second, small request: ~50-80 ms) and the line
#      the set code is on is found with the same rules card_ocr.parse_magic uses (the collector
#      number line first, then the set line);
#   3. that line, with a margin of a quarter of its height, is cut out, turned to grey and
#      scaled to 32 x 128 pixels;
#   4. a four-block convolutional network (about 100k parameters, foil_net.onnx beside this file,
#      run with OpenCV's dnn module like corner_net) says how likely it is a foil.
#   Before step 3: if that second OCR request read the symbol itself (a star or a dot in the set
#   line's text), that is the answer and the network is not asked - a symbol OCR read was never
#   wrong (99.9%), and the network alone was wrong on confetti foils. The network only fills in
#   what the OCR still did not read.
#
# Measured (2026-10-20, docs/plans/IMAGE_MATCH_EXPERIMENT.md; 10,501 set lines of 13 recent sets,
# tested on the cards it was NOT trained on): where the OCR had left the marker unread (381
# cards, 97.1% on the collection photos the owner confirmed) it was right 97.6%; the whole
# answer - the OCR's marker when read, else this at SURE - answered 99.6% of the cards that need
# one and was right 99.5%. A guess from the set alone got 48.8% on those cards, so it reads the
# symbol and not the set. NOT measured: other sets and older cards (which print no marker at
# all), other cameras, a Raspberry Pi's speed. The labels were not independent: for the cards
# whose marker the OCR read, the stored finish came from that marker. On a confetti-foil
# Secret Lair card it was wrong twice in five (the confetti pattern confuses it), which is why
# the OCR's own marker is always asked first and this only fills in what OCR did not read.

import logging
import threading
from pathlib import Path

import cv2
import numpy as np

from card_scanner.core import card_ocr

logger = logging.getLogger('ai')

# The exported network, lying beside this module (paths.py's one exception: a file that names
# another lying beside it - the server image and the AppImage copy the whole package)
MODEL_FILE = Path(__file__).with_name('foil_net.onnx')

# Where the collector line and the set line are on a flat card: (y from, y to, x from, x to) as
# fractions of its height / width. Training cut exactly this.
STRIP = (0.86, 1.0, 0.0, 0.62)
STRIP_CARD_HEIGHT = 1000          # a card image smaller than this is enlarged to it first
CROP_W, CROP_H = 128, 32          # the network's input: the set line, grey
MARGIN = 0.25                     # of the line's height, added on every side of it
SURE = 0.9                        # the probability one way or the other that is believed (below: 'unknown')


def strip_of(card_rgb):
    """The bottom-left strip of a flat card (RGB), enlarged when the card image is small"""
    height, width = card_rgb.shape[:2]
    y0, y1, x0, x1 = STRIP
    strip = card_rgb[int(height * y0):int(height * y1), int(width * x0):int(width * x1)]
    scale = max(1.0, STRIP_CARD_HEIGHT / height)
    if scale == 1.0:
        return strip
    return cv2.resize(strip, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)


def set_line(lines):
    """
    The line the set code is on, out of the strip's OCR lines (card_ocr.read_lines:
    [{'text', 'confidence', 'box'}]), or None. The rules are parse_magic's: lines from the top
    down; the first that reads as the collector number is skipped, and the first line after it
    (or a line before it, if none matched yet) that reads as 'SET . LANGUAGE' is the set line.
    """
    number_seen = False
    for line in sorted(lines, key=lambda l: l['box'][0][1]):
        text = line['text'].strip()
        if not number_seen:
            match = card_ocr.MAGIC_NUMBER.match(text)
            digits = match and (match.group(1) or match.group(2))
            if (digits and sum(c.isdigit() for c in digits) >= 2) or card_ocr.MAGIC_NUMBER_OF_TOTAL.match(text):
                number_seen = True
                continue
        if card_ocr.MAGIC_SET.match(text):
            return line
    return None


def set_line_box(lines):
    """The box of the set line (see set_line), or None"""
    line = set_line(lines)
    return line['box'] if line else None


def symbol_read(line):
    """
    The marker the OCR itself read on a set line: 'foil' (a star), 'non-foil' (a dot), or 'unknown'
    (no symbol came out between the set code and the language). The same rule as parse_magic: only
    a symbol that was actually read counts, a missing one says nothing.
    """
    match = card_ocr.MAGIC_SET.match(line['text'].strip())
    symbol = (match.group(2) or '') if match else ''
    if symbol and symbol in card_ocr.STARS:
        return 'foil'
    if symbol and symbol in card_ocr.DOTS:
        return 'non-foil'
    return 'unknown'


def set_line_crop(strip, box):
    """The set line (box: four corner points in the strip's pixels) as the network's input: grey, 32 x 128 - or None"""
    xs, ys = [p[0] for p in box], [p[1] for p in box]
    margin = MARGIN * (max(ys) - min(ys))
    part = strip[max(int(min(ys) - margin), 0):int(max(ys) + margin), max(int(min(xs) - margin), 0):int(max(xs) + margin)]
    if part.size == 0:
        return None
    return cv2.resize(cv2.cvtColor(part, cv2.COLOR_RGB2GRAY), (CROP_W, CROP_H), interpolation=cv2.INTER_AREA)


# Where the set line is on a flat card when nothing can say so - the OCR is switched off: the MEDIAN of its box (y from, y to, x from, x to; fractions of the card's height /
# width) over 1,487 cards of the evaluation set (scripts/experiments/image_match/foil_fixed_crop.py). The line's centre varies by +/-0.73% of the card's height (std) and the line is
# 2.4% high, so 90% of the centres lie within half a line of this box. Measured on 1,453 held-out cards where the marker is needed, with the same network and the same SURE: cut at the
# OCR's own box it answered 97.8% and was right 99.6%; cut here, 95.0% and 98.9%; on the 313 whose marker the OCR did not read, 95.8% and 99.0%; on the 15 Deadpool phone captures
# (confetti foils) 10 answered, 9 right (at the OCR's box: 12, 11). A taller / wider box was clearly worse (82% answered, 96% right; 4 of 10 on the Deadpool captures): do not widen it.
# NOT measured: other cameras than the evaluation set's and the phone's, cards of older frames (no marker printed), a card the warp cut loosely.
FIXED_BOX = (0.9460, 0.9698, 0.0627, 0.3970)


def fixed_crop(card_rgb):
    """The network's input cut at FIXED_BOX of a flat card (RGB): through the same strip as training (enlarged when small) and the same margin - or None"""
    if card_rgb is None or min(card_rgb.shape[:2]) < 2:
        return None                                   # nothing to cut out of
    strip = strip_of(card_rgb)
    strip_height, strip_width = strip.shape[:2]
    y0, y1, x0, x1 = FIXED_BOX
    top, bottom = [(y - STRIP[0]) / (STRIP[1] - STRIP[0]) * strip_height for y in (y0, y1)]
    left, right = [x / STRIP[3] * strip_width for x in (x0, x1)]
    return set_line_crop(strip, [(left, top), (right, top), (right, bottom), (left, bottom)])


def answer_for(probability):
    """('foil' | 'non-foil' | 'unknown', probability): the probability that is believed one way or the other (SURE), else 'unknown'"""
    if probability >= SURE:
        return 'foil', probability
    if probability <= 1 - SURE:
        return 'non-foil', probability
    return 'unknown', probability


class FoilMarker:
    """The network, loaded once. One instance serves all threads (a lock: a dnn network is not safe to run from two at once)."""

    def __init__(self, path=MODEL_FILE):
        self.net = cv2.dnn.readNetFromONNX(str(path))
        self._lock = threading.Lock()

    def probability(self, crop):
        """How likely a set-line crop (uint8, 32 x 128, grey) shows a foil's star: 0..1"""
        x = crop.astype(np.float32) / 255.0
        # The contrast of each crop is normalised on its own (as in training): the cameras differ in
        # brightness and a sleeve's glare shifts it, what counts is the shape
        x = (x - x.mean()) / (x.std(ddof=1) + 1e-3)      # ddof=1: torch's std, which training used
        with self._lock:
            self.net.setInput(np.ascontiguousarray(x[None, None]))
            logits = self.net.forward().reshape(-1)
        exp = np.exp(logits - logits.max())          # softmax; index 1 is 'foil' (training's label)
        return float(exp[1] / exp.sum())

    def read(self, card_rgb, ocr):
        """
        The marker of a flat card: ('foil' | 'non-foil' | 'unknown', probability or None).
        ocr: the OCR reader (card_ocr.CardOcr) - asked once more, on the strip alone.
        A symbol the OCR itself read on the strip (a star or a dot) stands, and the network is not
        asked: it is never wrong when read (99.9%), the network on its own was wrong twice in five on
        confetti-foil cards. Probability is None for such an answer. Otherwise the network is asked
        about the set line: 'unknown' when the strip has no set line, or it is not sure either way (SURE).
        """
        strip = strip_of(card_rgb)
        lines = ocr.read_lines(strip)
        if not lines:
            return 'unknown', None
        line = set_line(lines)
        if line is None:
            return 'unknown', None
        symbol = symbol_read(line)
        if symbol != 'unknown':
            return symbol, None
        crop = set_line_crop(strip, line['box'])
        if crop is None:
            return 'unknown', None
        return answer_for(self.probability(crop))

    def read_fixed(self, card_rgb):
        """
        The marker of a flat card with NO OCR: the network asked about the crop at FIXED_BOX, ('foil' | 'non-foil' | 'unknown', probability). There is no symbol the OCR read to
        stand first, so on a confetti foil (a pattern the network confuses) it can be wrong where read() would have kept the OCR's symbol: it is for when the OCR is off, where the
        alternative is the vision AI's check.
        """
        crop = fixed_crop(card_rgb)
        if crop is None:
            return 'unknown', None
        return answer_for(self.probability(crop))


def load(enabled=True):
    """
    The network, or None - when it is switched off (config.yaml ocr.foil_marker), its file is
    missing or this OpenCV cannot read it. The server then works as before: the OCR's marker,
    then the AI's foil check.
    """
    if not enabled:
        return None
    if not MODEL_FILE.is_file():
        logger.warning(f"Foil marker network {MODEL_FILE.name} not found - the foil marker is read by OCR and the AI only")
        return None
    try:
        return FoilMarker(MODEL_FILE)
    except Exception as e:   # not worth stopping the server for
        logger.warning(f"Foil marker network could not be loaded ({e}) - the foil marker is read by OCR and the AI only")
        return None
