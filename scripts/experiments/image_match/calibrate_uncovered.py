#!/usr/bin/env python3
"""
A printing that is NOT in the image index (newer than the index, or a stale one): what does evidence mode do with it? Every add of such a picture is a wrong add - the right
printing is not among the candidates. Per real picture (the 128 AI-read ones and a seeded 300 OCR-confirmed ones) the live decision is made twice: with the index as it is
(the picture is COVERED: it must still be added right) and with the picture's own printing removed from the index (UNCOVERED: it must not be added), under four rules:
    A  the decision as it is (the lead over the second printing only)
    B  A + the best match must be at least GATE similar in absolute terms (the "absolute gate" a Codex review suggested)
    C  A + the strip names a printing the card database has but the index lacks: the picture cannot be trusted for it - no picture-alone add
    D  B + C
    MTG_SCANNER_HOME=<repo> venv/bin/python -I scripts/experiments/image_match/calibrate_uncovered.py
"""
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(1, str(HERE))

import cv2
from card_scanner.config import Config
from card_scanner.core import card_ocr, image_index, printing_check
from card_scanner.core.database import CardDatabase
from card_scanner.core.reading.decide import decide_printing
from common import OUT, TRUTH, read

database = CardDatabase()
index = image_index.ImageIndex.load(Config.DATA_DIR / 'image_index')
ocr = card_ocr.CardOcr()
ocr.warm_up()
rows = json.loads((OUT / 'phash_rgb_card' / 'rows.json').read_text())
items = json.loads((OUT / 'phash_rgb_card' / 'strip_items.json').read_text())
ordered = list(zip([r for k in ('collection', 'scanned') for r in rows if r['kind'] == k], items))
frozen = json.loads((TRUTH / 'hard_truth.json').read_text())
pictures = [(r['path'], frozen.get(Path(r['path']).name)) for r, x in ordered if x['hard'] and frozen.get(Path(r['path']).name)]
rng = np.random.default_rng(31)
easy = [(r['path'], r['truth']) for r, x in ordered if not x['hard']]
pictures += [easy[i] for i in rng.choice(len(easy), 300, replace=False)]


def decide(distances, strip, truth_row, removed):
    d = distances.copy()
    if removed:
        d[truth_row] = d.max() + 1                                   # the printing is not in the index
    k = 5
    top = np.argpartition(d, k - 1)[:k]
    top = top[np.argsort(d[top], kind='stable')]
    sims = image_index.similarity(d[top])
    candidates = []
    for row, sim in zip(top, sims):
        card = database.get_card_by_id(index.ids[int(row)]) or {}
        candidates.append({'id': index.ids[int(row)], 'similarity': float(sim), 'set': card.get('set_code'), 'number': card.get('number')})
    return decide_printing({'candidates': candidates}, strip)


def named_but_not_indexed(strip):
    """The strip names a printing the card database has and the index lacks (here: also the printing removed from it)"""
    if not (strip['number'] and strip['set']):
        return None
    card = database.get_card_by_set_number(strip['set'], strip['number'])
    return card['id'] if card else None


# S: as shipped since the second Codex review - only the decisions the strip confirms (the picture alone goes to the AI): [n, added, added right]
strong = {'covered': [0, 0, 0], 'uncovered': [0, 0, 0]}
results = {}
for path, truth in pictures:
    rgb = read(path)
    truth_row = index.row(truth)
    if truth_row is None:
        continue
    up = index.distances(image_index.hash_card(rgb))
    down = index.distances(image_index.hash_card(cv2.rotate(rgb, cv2.ROTATE_180)))
    rotated = down.min() < up.min()
    distances = down if rotated else up
    strip = printing_check.read_strip_evidence(ocr, cv2.rotate(rgb, cv2.ROTATE_180) if rotated else rgb)
    strip = {'number': strip['number'], 'set': strip['set']}
    named = named_but_not_indexed(strip)
    for removed in (False, True):
        decision = decide(distances, strip, truth_row, removed)
        for gate in (None, 0.5, 0.6, 0.7):
            for c_rule in (False, True):
                accepted = decision['path'] in ('strong', 'image')
                if accepted and decision['path'] == 'image':
                    if gate is not None and decision['similarity'] < gate:
                        accepted = False
                    # rule C: the strip names a printing that the INDEX lacks. Covered: the index lacks only what it lacks anyway; uncovered: also the removed one
                    if c_rule and named is not None and (index.row(named) is None or (removed and named == truth)):
                        accepted = False
                if gate is None and not c_rule:
                    t = strong['uncovered' if removed else 'covered']
                    t[0] += 1
                    t[1] += decision['path'] == 'strong'
                    t[2] += decision['path'] == 'strong' and decision['printing_id'] == truth
                key = (removed, gate, c_rule)
                tally = results.setdefault(key, {'n': 0, 'added': 0, 'right': 0})
                tally['n'] += 1
                tally['added'] += accepted
                tally['right'] += bool(accepted and decision['printing_id'] == truth)

print('rule                         COVERED: added right (of n)        UNCOVERED: still ADDED (all wrong)')
for gate in (None, 0.5, 0.6, 0.7):
    for c_rule in (False, True):
        cov, unc = results[(False, gate, c_rule)], results[(True, gate, c_rule)]
        name = ('A' if gate is None else f'B{gate}') if not c_rule else ('C' if gate is None else f'D{gate}')
        print(f"{name:6} gate {str(gate):5} strip-rule {str(c_rule):5}   {cov['right']:3}/{cov['n']} ({100 * cov['right'] / cov['n']:.1f}%), wrong {cov['added'] - cov['right']}      "
              f"{unc['added']:3}/{unc['n']} ({100 * unc['added'] / unc['n']:.1f}%)")
print(f"S  strong only (shipped default)   {strong['covered'][2]:3}/{strong['covered'][0]} covered added right, wrong {strong['covered'][1] - strong['covered'][2]}      {strong['uncovered'][1]:3}/{strong['uncovered'][0]} ({100 * strong['uncovered'][1] / strong['uncovered'][0]:.1f}%) uncovered still ADDED")
ocr.stop()
