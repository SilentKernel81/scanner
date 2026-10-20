#!/usr/bin/env python3
"""
The shipped printing check (core/printing_check.PrintingCheck with the real data/image_index) on the Deadpool rescan (captures #1037-#1051, the
physical copies known: see rescan_eval.py), from both sides:
  - R = the printing the SERVER RESOLVED at scan time (what it added: Lightning Strike [Game Night 2019, 42], Worn Powerstone [Starter
    Commander Deck, 288]; the ten that went to review have no R): the check should DISAGREE;
  - R = the RIGHT printing (the Scryfall entry the physical copy is): the check should AGREE (15 of 15), the false-alarm side on clean pictures.
Also times one check on this machine.

    venv/bin/python -I scripts/experiments/image_match/check_rescan.py        (the repository's venv; needs data/image_index)
"""
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
from card_scanner.config import Config
from card_scanner.core.database import CardDatabase
from card_scanner.core.printing_check import PrintingCheck
from common import HOME, read

database = CardDatabase()
check = PrintingCheck(database, Config.DATA_DIR / 'image_index', Path('/nonexistent/never-written.jsonl'))
CARDS = ['Deadly Dispute', 'Lightning Bolt', 'Lightning Greaves', 'Sol Ring', 'Thrill of Possibility']
NUMBER = {'Deadly Dispute': 1, 'Lightning Bolt': 2, 'Thrill of Possibility': 3, 'Lightning Greaves': 4, 'Sol Ring': 5}
VERSIONS = ['regular', 'foil', 'confetti foil']
files = {int(f.name.split('_')[1]): f for f in (HOME / 'truth' / 'rescan_cards').iterdir() if f.name.startswith('phone-0240eb84_')}


def printing(set_code, number):
    row = database.conn.execute('SELECT id FROM cards WHERE set_code = ? AND collector_number = ?', (set_code, number)).fetchone()
    return row['id'] if row else None


def sld(number):
    return printing('sld', f'IFIYW-{number}')


# what the server added at scan time (the pasted capture list of 2026-10-20): captures with an R
RESOLVED = {1038: ('gn2', '42'), 1043: ('gn2', '42'), 1048: ('gn2', '42'), 1040: ('scd', '288'), 1045: ('scd', '288')}
print(f'{"#":>5} {"version":13} {"card":22} {"R = what the server added":28} {"verdict":9} {"kind":11} {"gap":>6} | {"R = the right printing":22} {"verdict":8} {"gap":>6}')
agree, disagree, timings = 0, 0, []
for k, number in enumerate(range(1037, 1052)):
    version, card = VERSIONS[k // 5], CARDS[k % 5]
    n = NUMBER[card]
    right = sld(n + 5 if version == 'confetti foil' else n)
    rgb = read(files[number])
    t0 = time.time()
    good = check.check(rgb, right)
    timings.append(time.time() - t0)
    agree += good['verdict'] == 'agree'
    left = ' ' * 28 + f' {"-":9} {"-":11} {"-":>6}'
    if number in RESOLVED:
        wrong_id = printing(*RESOLVED[number])
        bad = check.check(rgb, wrong_id)
        disagree += bad['verdict'] == 'disagree'
        name = database.get_card_by_id(wrong_id)
        left = f"{name['name'] + ' [' + name['set_code'] + ' ' + name['number'] + ']':28} {bad['verdict']:9} {str(bad['kind']):11} {bad['gap']:>6.3f}"
    print(f'{number:>5} {version:13} {card:22} {left} | {"IFIYW-" + str(n + 5 if version == "confetti foil" else n):22} {good["verdict"]:8} {good["gap"]:>6.3f}')
print(f'\nthe right printing agrees: {agree} of 15; what the server added disagrees: {disagree} of {len(RESOLVED)}; '
      f'one check takes {1000 * np.median(timings):.0f} ms (median), {1000 * np.percentile(timings, 95):.0f} ms (95th percentile) on this machine (other jobs may be running beside it)')
