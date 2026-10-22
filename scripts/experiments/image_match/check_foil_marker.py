#!/usr/bin/env python3
"""
The SHIPPED module (card_scanner.core.foil_marker) with the REAL OCR reader on real pictures - not the experiment's own copy of the steps:
  - the 15 Deadpool rescan pictures (finish known from the physical cards),
  - a sample of the collection / scanned pictures the network was NOT trained on (cards whose hash split put them in the test 20%),
    with the finish stored for them, only where the printing exists in both finishes (the marker is needed).
Reports how many it answers, how many of those are right, and the time of one read.

    venv/bin/python -I check_foil_marker.py [sample size, default 400]        (the REPOSITORY's venv: the OCR reader)
"""
import hashlib
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2
import numpy as np
from card_scanner.config import Config
from card_scanner.core import card_ocr, foil_marker
from card_scanner.core.database import CardDatabase
from card_scanner.core.printing_check import PrintingCheck
from common import HOME, OUT, printings, read

P = printings()
sample_size = int(sys.argv[1]) if len(sys.argv) > 1 else 400
marker = foil_marker.load(True)
checker = PrintingCheck(CardDatabase(), Config.DATA_DIR / 'image_index', Path('/nonexistent/x.jsonl'), 'off', 'off')
ocr = card_ocr.CardOcr()
ocr.warm_up()


def run(pictures, title, fixed=False, turn=False):
    answered = right = 0
    seconds = []
    for path, finish in pictures:
        t0 = time.time()
        # fixed: the OCR switched off - the set line is cut at its fixed place (foil_marker.FIXED_BOX), no OCR is asked
        picture = read(path)
        if turn:
            picture = cv2.rotate(picture, cv2.ROTATE_180)          # a card put in upside down
        if turn or fixed == 'oriented':
            # the shipped path with OCR off: the picture search says which way up the card is (None: cannot say -> unknown, the AI's check follows)
            picture = checker.upright(picture, needs_marker=True)
            foil, probability = marker.read_fixed(picture) if picture is not None else ('unknown', None)
        else:
            foil, probability = marker.read_fixed(picture) if fixed else marker.read(picture, ocr)
        seconds.append(time.time() - t0)
        if foil != 'unknown':
            answered += 1
            right += (foil == 'foil') == (finish != 'regular')
    n = len(pictures)
    print(f'{title}: {n} pictures; answered {answered} ({100 * answered / n:.1f}%), right {right} ({100 * right / max(answered, 1):.1f}% of those); '
          f'a read takes {1000 * np.median(seconds):.0f} ms (median), {1000 * np.percentile(seconds, 95):.0f} ms (95th percentile)')


# the rescan: #1037-1041 regular, 1042-1051 foil / confetti foil
rescan = {int(f.name.split('_')[1]): f for f in (HOME / 'truth' / 'rescan_cards').iterdir() if f.name.startswith('phone-0240eb84_')}
run([(rescan[n], 'regular' if n < 1042 else 'foil') for n in range(1037, 1052)], 'Deadpool rescan')
run([(rescan[n], 'regular' if n < 1042 else 'foil') for n in range(1037, 1052)], 'Deadpool rescan, OCR off (fixed place, orientation from the picture search)', fixed='oriented')

# held-out cards (the test side of foil_train.py's split by card), where the printing exists in both finishes
items = [i for i in json.loads((OUT / 'eval.json').read_text())
         if i['kind'] in ('collection', 'scanned') and i['finish'] in ('regular', 'foil')
         and int(hashlib.md5(i['truth'].encode()).hexdigest(), 16) % 5 == 0
         and 'nonfoil' in P[i['truth']]['finishes'] and {'foil', 'etched'} & set(P[i['truth']]['finishes'])]
rng = np.random.default_rng(11)
chosen = [items[i] for i in rng.choice(len(items), min(sample_size, len(items)), replace=False)]
run([(i['path'], i['finish']) for i in chosen], 'held-out cards')
run([(i['path'], i['finish']) for i in chosen], 'held-out cards, OCR off (fixed place, orientation from the picture search)', fixed='oriented')
run([(i['path'], i['finish']) for i in chosen], 'the same cards put in UPSIDE DOWN, OCR off (what a Codex review probed)', turn=True)
ocr.stop()
