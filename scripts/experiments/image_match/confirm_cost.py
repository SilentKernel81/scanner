#!/usr/bin/env python3
"""
What the confirmation by OCR costs (a fourth Codex review of 2026-10-20: with OCR on, every decision by the picture alone must be confirmed by the full-card OCR reading the same name,
whatever the strip says). Every evaluation picture and the 15 Deadpool captures through live_read with the real OCR reader and the picture alone allowed; for each 'image' decision the
full-card OCR reads the card, and database.names_match says whether it read the chosen card's name. Counted against the truth: confirmed / refused, right / wrong.

    MTG_SCANNER_HOME=<repo> venv/bin/python -I scripts/experiments/image_match/confirm_cost.py [--easy 1500]
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(1, str(HERE))

from card_scanner.config import Config
from card_scanner.core import card_ocr, foil_marker
from card_scanner.core.database import CardDatabase
from card_scanner.core.printing_check import PrintingCheck, ocr_agrees
from common import HOME, OUT, TRUTH, read

ap = argparse.ArgumentParser()
ap.add_argument('--easy', type=int, default=1500)
arguments = ap.parse_args()
database = CardDatabase()
ocr = card_ocr.CardOcr()
ocr.warm_up()
net = foil_marker.load(True)
check = PrintingCheck(database, Config.DATA_DIR / 'image_index', Path('/nonexistent/x.jsonl'), 'off', 'off')
rows = json.loads((OUT / 'phash_rgb_card' / 'rows.json').read_text())
items = json.loads((OUT / 'phash_rgb_card' / 'strip_items.json').read_text())
ordered = list(zip([r for k in ('collection', 'scanned') for r in rows if r['kind'] == k], items))
frozen = json.loads((TRUTH / 'hard_truth.json').read_text())
rng = np.random.default_rng(17)
easy = [(r['path'], r['truth'], None) for r, x in ordered if not x['hard']]
pictures = [(r['path'], frozen[Path(r['path']).name], None) for r, x in ordered if x['hard'] and frozen.get(Path(r['path']).name)]
pictures += [easy[i] for i in rng.choice(len(easy), min(arguments.easy, len(easy)), replace=False)]
DEADPOOL = ['Deadly Dispute', 'Lightning Bolt', 'Lightning Greaves', 'Sol Ring', 'Thrill of Possibility']
rescan = {int(f.name.split('_')[1]): f for f in (HOME / 'truth' / 'rescan_cards').iterdir() if f.name.startswith('phone-0240eb84_')}
pictures += [(str(rescan[n]), None, DEADPOOL[(n - 1037) % 5]) for n in range(1037, 1052)]
counts, shown = {}, []
for path, truth, name in pictures:
    rgb = read(path)
    reading = check.live_read(rgb, None, ocr, net, True)
    if not reading or reading['path'] != 'image':
        continue
    card = reading['card']
    right = card['name'].lower().startswith(name.lower()) if name else card['id'] == truth
    read_info = ocr.read_card(rgb, 'mtg')
    confirmed = ocr_agrees(read_info, card, database.get_card_by_set_number)
    key = ('confirmed' if confirmed else 'refused', 'right' if right else 'wrong', 'deadpool' if name else 'evaluation')
    counts[key] = counts.get(key, 0) + 1
    if not confirmed:
        shown.append((Path(path).name, f"{card['name']} [{card['set_code']} {card['number']}]", read_info and f"{read_info.get('name')} [{read_info.get('set_code')} {read_info.get('collector_number')}]", 'right' if right else 'WRONG'))
total = sum(counts.values())
print(f'{len(pictures)} pictures; decisions by the picture alone: {total}')
for key in sorted(counts):
    print(f'   {key[0]:9} {key[1]:5} ({key[2]}): {counts[key]}')
for s in shown:
    print('   refused:', s)
ocr.stop()
