# card_back.py
# Is a capture the BACK of a Magic card? (docs/plans/CARD_BACK_STOP_PLAN.md) A face-down card dropped on the pile is a stop sign: the server switches that camera's
# auto-capture off and throws the capture away (server/captures.py: stop_at_card_back). This module only answers the question - it changes nothing.
#
# How: the same 96-byte fingerprint the picture index makes of every printing (image_index.hash_card), of the capture as given and turned 180 degrees (a back is not
# symmetric, and a card lies either way up), compared with the fingerprint of the one standard Magic back, pinned below. No database, no index, no OCR, no file to
# ship, nothing downloaded at run time: a few milliseconds a picture.
#
# What it does NOT recognise (said in the docs): a sleeved card face down shows the SLEEVE's back; other products' own backs; a double-faced card's back, which is a card face.
# Those are read as cards, as before - the safe failure (they land in review).

import numpy as np

from card_scanner.core import image_index

# The fingerprint of Scryfall's picture of the standard Magic back, https://backs.scryfall.io/large/0/a/0aeebaf5-8c7d-4636-9e82-8c27447861f7.jpg (672 x 936,
# SHA-256 f9535ae9...a1e1222, fetched 2026-10-21), as image_index.hash_card makes it. Recompute it with scripts/experiments/image_match/card_back_fingerprint.py -
# and whenever image_index's fingerprint changes (DESCRIPTOR): a test pins the descriptor it was made with.
FINGERPRINT_DESCRIPTOR = 'phash_rgb_v1'
CARD_BACK_FINGERPRINT = bytes.fromhex(
    '8000aa5a2fc1c62b7c0393a470fc38d472b16e2362d3d7b373de9ad4536ece9e'
    '8000a9593ff1962e3c0696a5703fc1c161f9c0f862d4787c57d63d5747676f17'
    'c100655b5efa3a3e3a049ec76d4bc7e161f9c5f830bc383c96866c03c7c36b63')

# From this similarity on, a capture is a back. PROVISIONAL (2026-10-21) until real captures of backs are measured (step 1 of the plan:
# scripts/experiments/image_match/card_back_measure.py). What is measured so far: the 16,881 saved captures of card FRONTS on the development machine reach at most
# 0.219 (99.9% below 0.177); Scryfall's back degraded on purpose - blurred, darkened, tinted, JPEG 30 - stays above 0.92, a glare spot or a 3% perspective error gives
# about 0.71, but a 5% crop of its edge 0.35 and everything together 0.42: how a real camera frames the card matters most, and only real captures can say.
CARD_BACK_MIN_SIMILARITY = 0.35


def card_back_similarity(rgb):
    """-1..1: how much a picture (RGB uint8, the flat card filling it) looks like the Magic back - the better of the picture as given and turned 180 degrees"""
    back = np.frombuffer(CARD_BACK_FINGERPRINT, np.uint8)
    best = -1.0
    for picture in (rgb, np.ascontiguousarray(rgb[::-1, ::-1])):        # [::-1, ::-1]: turned 180 degrees, as cv2.rotate(ROTATE_180) does
        differing = np.unpackbits(np.bitwise_xor(back, np.frombuffer(image_index.hash_card(picture), np.uint8))).sum()
        best = max(best, float(image_index.similarity(differing)))
    return best


def is_card_back(rgb, min_similarity=None):
    """Whether a picture is the back of a Magic card: card_back_similarity at least CARD_BACK_MIN_SIMILARITY. A picture that cannot be fingerprinted is not one (False)"""
    try:
        return card_back_similarity(rgb) >= (CARD_BACK_MIN_SIMILARITY if min_similarity is None else min_similarity)
    except Exception:
        return False
