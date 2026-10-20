#!/usr/bin/env python3
"""
Step 5: does reading ONLY the bottom-left strip (collector number, set code, foil marker) settle which
printing an image match found? Uses the project's own OCR reader (light-ocr) on a small crop.

    venv/bin/python -I strip.py <embedding folder name> [--sample 1500]      (the REPOSITORY's venv: it
                                                                             has what card_scanner needs)

For a random sample of the real pictures (collection photos and scanned pictures) it
  1. takes the five nearest printings of the image match (evaluate.py's rows.json),
  2. crops the bottom-left strip of the picture, upscales it, reads it with light-ocr and parses
     number / set / foil marker with the SAME regexes the full-card parser uses,
  3. picks, among the five, the nearest one whose set and number agree with what was read (number alone
     if no set was read); with no agreement, the image's own top-1.
Reported against the image alone, the strip alone (exact set + number lookup) and the whole pipeline.
Caveat: the 'scanned' truth was itself read by light-ocr from the whole card, so its strip reading is
not independent of it; 'collection' truth (what the user accepted) is the cleaner test.
"""
import argparse
import collections
import json
import random
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2
import numpy as np
from card_scanner.core import card_ocr
from common import HOME, OUT, printings, read

STRIP = (0.86, 1.0, 0.0, 0.62)     # y from, y to, x from, x to (fractions of the card)


def crop(rgb):
    h, w = rgb.shape[:2]
    y0, y1, x0, x1 = STRIP
    part = rgb[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]
    # a small strip is upscaled to the size the reader was tuned on (the card is ~1000 px high)
    scale = max(1.0, 1000 / h)
    return cv2.resize(part, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)


def parse_strip(lines, height):
    """number, set code, foil marker out of the strip's lines - the loop of card_ocr.parse_magic"""
    number = set_code = ''
    foil = 'unknown'
    for line in sorted(lines, key=lambda l: l['box'][0][1]):
        text = line['text'].strip()
        if not number:
            m = card_ocr.MAGIC_NUMBER.match(text)
            digits = m and (m.group(1) or m.group(2))
            if digits and sum(c.isdigit() for c in digits) >= 2:
                number = digits.translate(card_ocr.DIGIT_LOOKALIKES)
                continue
            m = card_ocr.MAGIC_NUMBER_OF_TOTAL.match(text)
            if m:
                number = m.group(1)
                continue
        if not set_code:
            m = card_ocr.MAGIC_SET.match(text)
            if m:
                set_code = m.group(1)
                marker = m.group(2) or ''
                foil = 'foil' if marker and marker in card_ocr.STARS else 'non-foil' if marker and marker in card_ocr.DOTS else 'unknown'
    return number.lstrip('0') or '', set_code.lower(), foil


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('name')
    ap.add_argument('--sample', type=int, default=0, help='pictures per kind; 0 = all of them')
    a = ap.parse_args()
    P = printings()
    rows = json.loads((OUT / a.name / 'rows.json').read_text())
    random.seed(7)
    by_kind = {k: [r for r in rows if r['kind'] == k] for k in ('collection', 'scanned')}
    sample = [r for k, v in by_kind.items() for r in (random.sample(v, min(a.sample, len(v))) if a.sample else v)]
    by_key = collections.defaultdict(list)
    for p in P.values():
        by_key[(p['set'], p['number'].lstrip('0') or '0')].append(p['id'])

    ocr = card_ocr.CardOcr()
    ocr.warm_up()
    t_ocr, results = [], []
    for n, r in enumerate(sample):
        rgb = read(r['path'])
        if rgb is None:
            continue
        t0 = time.time()
        lines = ocr.read_lines(crop(rgb))
        t_ocr.append(time.time() - t0)
        # an unreadable strip is a result too (nothing read), not a picture to leave out
        number, set_code, foil = parse_strip(lines or [], rgb.shape[0])
        top5 = r['top5']
        agree = [c for c in top5 if (not number or (P[c]['number'].lstrip('0') or '0') == number) and (not set_code or P[c]['set'] == set_code) and (number or set_code)]
        # the strong agreement: the strip gave BOTH number and set and one of the image's candidates has both
        both = [c for c in top5 if number and set_code and (P[c]['number'].lstrip('0') or '0') == number and P[c]['set'] == set_code]
        piped = agree[0] if agree else top5[0]
        strip_only = by_key.get((set_code, number), [])
        sims = r.get('sims') or [0, 0]
        results.append({'kind': r['kind'], 'finish': r['finish'], 'cls': r['class'], 'read_number': bool(number), 'read_set': bool(set_code),
                        'image': r['got'] == r['truth'], 'pipeline': piped == r['truth'], 'in_top5': r['truth'] in top5,
                        'strip_only': len(strip_only) == 1 and strip_only[0] == r['truth'],
                        # the strip's own printing (set + number looked up), and whether the image's candidates are that CARD
                        'strip_unique': len(strip_only) == 1,
                        'strip_card_in_top5': len(strip_only) == 1 and P[strip_only[0]]['oracle_id'] in {P[c]['oracle_id'] for c in top5},
                        'strip_card_is_top1': len(strip_only) == 1 and P[strip_only[0]]['oracle_id'] == P[top5[0]]['oracle_id'],
                        # the strip names a printing; does it have the ART the image sees? (Secret Lair cards print the base
                        # printing's set and number: the strip points at another printing, with other art, than the one in hand)
                        'both_art_is_top1_art': bool(both) and bool(P[both[0]]['illustration_id']) and P[both[0]]['illustration_id'] == P[top5[0]]['illustration_id'],
                        'strip_art_is_top1_art': len(strip_only) == 1 and bool(P[strip_only[0]]['illustration_id']) and P[strip_only[0]]['illustration_id'] == P[top5[0]]['illustration_id'],
                        'truth': r['truth'], 'station': r.get('station'), 'reader': r.get('reader'),
                        # a printing that exists in foil AND non-foil: only the marker (or a person) can say which this one is
                        'finishes_both': 'nonfoil' in P[r['truth']]['finishes'] and bool({'foil', 'etched'} & set(P[r['truth']]['finishes'])),
                        'foil_read': foil, 'foil_true': r['finish'], 'hard': r['reader'] not in (None, 'light-ocr') and r['kind'] == 'scanned',
                        'sim1': sims[0], 'margin': round(sims[0] - sims[1], 4), 'both': bool(both), 'both_right': bool(both) and both[0] == r['truth']})
        if n % 100 == 0:
            print(n, len(sample), flush=True)
    out = {'ocr_ms_per_strip_median': round(1000 * float(np.median(t_ocr)), 1), 'n': len(results)}
    for kind in ('collection', 'scanned'):
        rs = [x for x in results if x['kind'] == kind]
        if not rs:
            continue
        pct = lambda f, s=rs: round(100 * sum(f(x) for x in s) / max(len(s), 1), 1)
        same = [x for x in rs if x['cls'] == 'same_art']
        out[kind] = {'n': len(rs), 'image_only_top1': pct(lambda x: x['image']), 'top5_has_truth': pct(lambda x: x['in_top5']),
                     'strip_only': pct(lambda x: x['strip_only']), 'pipeline': pct(lambda x: x['pipeline']),
                     'number_read': pct(lambda x: x['read_number']), 'set_read': pct(lambda x: x['read_set']),
                     'same_art_n': len(same), 'same_art_resolved_by_strip': pct(lambda x: x['pipeline'], same) if same else None,
                     'foil_marker_read': pct(lambda x: x['foil_read'] != 'unknown'),
                     'foil_marker_right_when_read': round(100 * sum((x['foil_read'] == 'foil') == (x['foil_true'] != 'regular') for x in rs if x['foil_read'] != 'unknown')
                                                          / max(sum(x['foil_read'] != 'unknown' for x in rs), 1), 1)}
    # --- no AI: when does the pipeline add a card by itself, and how often is it right? ---
    # auto-add if the strip and an image candidate agree on number AND set (strong), or else if the image's
    # nearest printing leads by `margin` (weak: a same-art reprint can hide there); anything else goes to review.
    # Same-art reprints leave the weak path wrong, which is why the margin alone is not enough for them.
    def decide(x, margin):
        if x['both']:
            return 'auto', x['both_right']
        if x['margin'] >= margin and x['sim1'] > 0:
            return 'auto', x['image']
        return 'review', None

    out['no_ai'] = {}
    groups = {'collection': [x for x in results if x['kind'] == 'collection'], 'scanned': [x for x in results if x['kind'] == 'scanned'],
              'hard_cases (AI-read captures)': [x for x in results if x['hard']]}
    for title, rs in groups.items():
        if not rs:
            continue
        table = {}
        for margin in (None, 0.0, 0.02, 0.05, 0.1):      # None: only the strong path, the rest to review
            made = [decide(x, 9 if margin is None else margin) for x in rs]
            auto = [ok for kind, ok in made if kind == 'auto']
            table['strong_only' if margin is None else f'margin>={margin}'] = {
                'auto_pct': round(100 * len(auto) / len(rs), 1), 'auto_accuracy_pct': round(100 * sum(auto) / max(len(auto), 1), 1),
                'to_review_pct': round(100 * (len(rs) - len(auto)) / len(rs), 1)}
        out['no_ai'][title] = {'n': len(rs), **table}
    (OUT / a.name / 'strip_items.json').write_text(json.dumps(results))
    (OUT / a.name / 'strip.json').write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))
    ocr.stop()


if __name__ == '__main__':
    main()
