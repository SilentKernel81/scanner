#!/usr/bin/env python3
"""
Step 4a: the evaluation set - real pictures with a known printing. Three kinds, kept apart:

  collection  data/captures_full/ of the server (the photo kept with each collection entry); truth = the
              entry's printing, which the user accepted (clean, but only the cards that were kept)
  scanned     scanned_cards/ (what the server received) joined with settled_captures; truth = what the
              server settled (set + number read by OCR/AI, so a misread is in the truth: see label errors)
              and WHICH reader decided it (light-ocr / AI / review)
  frame       the whole camera frame behind a 'scanned' capture, warped here with the station's own corners
              (the detector + warp part of the chain); same truth as 'scanned'

    ~/.cache/mtg-image-match/venv/bin/python -I build_eval.py
"""
import collections
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cv2
import numpy as np
from common import CARD_DB, OUT, TRUTH, gallery_ids


def warp(frame, corners, size=(488, 680)):
    """Corners (tl, tr, br, bl) of the card in the frame to an upright card, as the station does"""
    src = np.array(corners, np.float32)
    dst = np.array([[0, 0], [size[0], 0], [size[0], size[1]], [0, size[1]]], np.float32)
    return cv2.warpPerspective(frame, cv2.getPerspectiveTransform(src, dst), size)


def by_set_number():
    db = sqlite3.connect(f'file:{CARD_DB}?mode=ro', uri=True)
    table = collections.defaultdict(list)
    for id_, set_code, number in db.execute('select id, lower(set_code), collector_number from cards'):
        table[(set_code, str(number).lstrip('0') or '0')].append(id_)
    return table


def main():
    gallery = gallery_ids()
    table = by_set_number()
    items, problems = [], collections.Counter()

    # 1. the collection's photos
    for c in json.loads((TRUTH / 'captures.json').read_text()):
        path = TRUTH / 'captures_full' / c['file']
        if path.exists() and c['card_id'] in gallery:
            items.append({'kind': 'collection', 'path': str(path), 'truth': c['card_id'], 'finish': c['finish'], 'station': c['station']})

    # 2. + 3. scanned pictures and frames, through settled_captures
    scanned = collections.defaultdict(list)
    for f in (TRUTH / 'scanned_cards').iterdir():
        m = re.match(r'(.+)_(\d+)_(\d+)\.jpg$', f.name)
        if m:
            scanned[(m[1], int(m[2]))].append((int(m[3]), f))
    for s in json.loads((TRUTH / 'settled.json').read_text()):
        out = s['outcome']
        card = out.get('card')
        if out.get('status') != 'added' or not card:
            problems['not added (review)'] += 1
            continue
        read = out.get('read') or {}
        # set and number of the SETTLED card: its own name for the set, so use what was read
        key = ((read.get('set') or '').lower(), (read.get('number') or '').lstrip('0') or '0')
        ids = table.get(key, [])
        if len(ids) != 1:   # none (read differently from the database) or several (language / promo variants)
            problems['no unique printing for set+number' if not ids else 'several printings for set+number'] += 1
            continue
        meta = {'truth': ids[0], 'finish': (card.get('finish') or '').lower(), 'station': s['sender'],
                'reader': read.get('reader') or 'review', 'capture_id': s['capture_id'], 'number': s['number']}
        files = scanned.get((s['sender'], s['number']), [])
        if len(files) == 1:        # a repeated number after a reset is ambiguous: left out
            items.append({'kind': 'scanned', 'path': str(files[0][1]), **meta})
        else:
            problems['scanned file missing or ambiguous'] += 1
        note = TRUTH / 'frames' / f"{s['capture_id']}.json"
        if note.exists():
            n = json.loads(note.read_text())
            if n.get('corners') and (TRUTH / 'frames' / f"{s['capture_id']}.jpg").exists():
                items.append({'kind': 'frame', 'path': str(TRUTH / 'frames' / f"{s['capture_id']}.jpg"), 'corners': n['corners'], **meta})
    (OUT / 'eval.json').write_text(json.dumps(items))
    print(collections.Counter(i['kind'] for i in items))
    print('unresolved:', dict(problems))
    print('distinct printings per kind:', {k: len({i['truth'] for i in items if i['kind'] == k}) for k in ('collection', 'scanned', 'frame')})
    print('readers (scanned):', collections.Counter(i['reader'] for i in items if i['kind'] == 'scanned'))


if __name__ == '__main__':
    main()
