#!/usr/bin/env python3
"""
Pins core/image_index.hash_card to the experiment's own code: the fingerprint of a deterministic synthetic card picture made by the
experiment's functions (common.prepare + embed.phash_bits, the code that built every number in the report) and by the shipped one.
Prints the hex both agree on, for tests/test_printing_check.py to compare against (a synthetic picture: no card art in the repo).

    venv/bin/python -I scripts/experiments/image_match/pin_hash.py        (the repository's venv: both pieces of code import there)
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2
import numpy as np
from card_scanner.core import image_index
from common import prepare
from embed import phash_bits


def synthetic_card(seed, width=488, height=680):
    """A deterministic card-shaped picture: coloured blocks, smoothly scaled up (tests/test_printing_check.py has the same function)"""
    blocks = np.random.default_rng(seed).integers(0, 256, (9, 6, 3), dtype=np.uint8)
    return cv2.resize(blocks, (width, height), interpolation=cv2.INTER_CUBIC)


for seed in (1234, 99):
    card = synthetic_card(seed)
    experiment = np.packbits(np.unpackbits(phash_bits(prepare(card, 'card', True), 'phash_rgb'))).tobytes()
    shipped = image_index.hash_card(card)
    print(f'seed {seed}: the same {experiment == shipped}; hex {shipped.hex()}')
