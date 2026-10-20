"""Shared bits of the image-match experiment: where things are, the printings, the evaluation set"""
import json
import os
import sqlite3
from pathlib import Path

import cv2
import numpy as np

HOME = Path(os.environ.get('IMAGE_MATCH_HOME', Path.home() / '.cache' / 'mtg-image-match'))
IMAGES = HOME / 'images'          # Scryfall 'normal' pictures, <printing id>.jpg
TRUTH = HOME / 'truth'            # read-only copies from the server (captures, frames, settled outcomes)
OUT = HOME / 'out'                # embeddings, indexes, models, results
OUT.mkdir(parents=True, exist_ok=True)
CARD_DB = Path(os.environ.get('CARD_DB', Path(__file__).resolve().parents[3] / 'data' / 'cards_database.db'))

W, H = 224, 312                   # the size every picture is brought to (card aspect 63 x 88 mm)


def printings():
    """Scryfall's default_cards fields we keep, by printing id"""
    return {p['id']: p for p in json.loads((HOME / 'printings.json').read_text())}


def gallery_ids():
    """The printings the matcher can return: the ones in the server's card database"""
    db = sqlite3.connect(f'file:{CARD_DB}?mode=ro', uri=True)
    ids = {r[0] for r in db.execute('select id from cards')}
    db.close()
    return ids


def read(path):
    """A picture as RGB uint8, or None"""
    data = cv2.imread(str(path), cv2.IMREAD_COLOR)
    return None if data is None else cv2.cvtColor(data, cv2.COLOR_BGR2RGB)


def prepare(rgb, crop='card', normalise=True):
    """
    The picture both sides (Scryfall's and the camera's) go through before any embedding.
    crop: 'card' - the whole card, a few percent of the edge cut off (a camera card has a black rim
    and a sleeve edge, Scryfall's has the rounded corners); 'art' - the picture box only.
    normalise: per-channel stretch to the 2nd-98th percentile, so a dim, glare-washed camera picture
    and Scryfall's bright one meet halfway.
    """
    h, w = rgb.shape[:2]
    if crop == 'art':     # the art box of a regular frame: x 8-92 %, y 11-56 % of the card
        rgb = rgb[int(h * .11):int(h * .56), int(w * .08):int(w * .92)]
    else:
        rgb = rgb[int(h * .025):int(h * .975), int(w * .035):int(w * .965)]
    size = (W, H) if crop == 'card' else (W, int(W * 0.5))
    rgb = cv2.resize(rgb, size, interpolation=cv2.INTER_AREA)
    if normalise:
        out = np.empty_like(rgb)
        for c in range(3):
            lo, hi = np.percentile(rgb[..., c], (2, 98))
            out[..., c] = np.clip((rgb[..., c].astype(np.float32) - lo) * 255 / max(hi - lo, 1), 0, 255)
        rgb = out
    return rgb
