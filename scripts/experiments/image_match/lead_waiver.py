#!/usr/bin/env python3
"""
Is the picture's LEAD (best match minus the second) a measure of how sure it is of the CARD, for a decision by the picture alone? (decide.LEAD_WAIVES_NAME, 2026-10-21)
The real pictures with known truth of verify_live_read.py (the 128 AI-read ones and a seeded 300 OCR-confirmed ones); the picture alone decides (no strip), once with the
index as it is (covered: an add can be right or wrong) and once with the truth taken out (every add is wrong):
    --missing card      EVERY printing of the card taken out - a card newer than the card data, what the full-card OCR's name check is there for
    --missing printing  only the picture's own printing taken out - a printing newer than the index (the picture then picks a same-art reprint)
Writes one record per decision to OUT (lead, best similarity, right, same_card) and prints the table by lead.

    MTG_SCANNER_HOME=<repo> venv/bin/python -I scripts/experiments/image_match/lead_waiver.py [--missing card|printing] [OUT.json]
Measured 2026-10-21 on the development machine's index (112,765 printings): --missing card: 0 of 428 added as another card at a lead of 0.08 or more (the most a missing card
reached: 0.052); --missing printing: 116 wrong printings at 0.05, 115 of them another printing of the same card; index complete: 257 of 386 right decisions lead by 0.10 or more.
"""
import json, sys
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parent; REPO = HERE.parents[2]
MODE = sys.argv[sys.argv.index('--missing') + 1] if '--missing' in sys.argv else 'card'
TARGET = next((a for a in sys.argv[1:] if a.endswith('.json')), str(REPO / 'data' / 'logs' / f'lead_waiver_{MODE}.json'))
sys.path.insert(0, str(REPO / 'src')); sys.path.insert(1, str(HERE))
from card_scanner.config import Config
from card_scanner.core import image_index
from card_scanner.core.database import CardDatabase
from card_scanner.core.printing_check import PrintingCheck
from common import OUT, TRUTH, read
db = CardDatabase()
check = PrintingCheck(db, Config.DATA_DIR / 'image_index', Path('/nonexistent/x.jsonl'), 'off', 'off')
rows = json.loads((OUT / 'phash_rgb_card' / 'rows.json').read_text())
items = json.loads((OUT / 'phash_rgb_card' / 'strip_items.json').read_text())
ordered = list(zip([r for k in ('collection', 'scanned') for r in rows if r['kind'] == k], items))
frozen = json.loads((TRUTH / 'hard_truth.json').read_text())
hard = [(r, frozen.get(Path(r['path']).name)) for r, x in ordered if x['hard'] and frozen.get(Path(r['path']).name)]
easy_rows = [r for r, x in ordered if not x['hard']]
rng = np.random.default_rng(31)
easy = [(easy_rows[i], easy_rows[i]['truth']) for i in rng.choice(len(easy_rows), 300, replace=False)]
out = []
import sqlite3
conn = sqlite3.connect(str(Config.DATA_DIR / 'cards_database.db'))
ORACLE = dict(conn.execute('select id, oracle_id from cards'))
ORACLE_ROWS = {}
for row, truth in hard + easy:
    s = check._search_now(read(row['path']))
    if s is None: continue
    index, dist = s['index'], s['distances'].astype(np.int64)
    sims = image_index.similarity(dist)
    truth_row = index.row(truth)
    for covered in (True, False):
        d = sims.copy()
        if not covered:
            if truth_row is None: continue
            if MODE == 'printing':
                d[truth_row] = -9                 # only this printing is missing from the index
            oracle = None if MODE == 'printing' else (db.get_card_by_id(truth) or {}).get('oracle_id')
            if MODE == 'card' and not oracle: continue
            if MODE == 'card':
              # EVERY printing of the card is missing (a card newer than the card data): rows whose printing has this oracle id
              if oracle not in ORACLE_ROWS:
                ORACLE_ROWS[oracle] = [i for i, pid in enumerate(index.ids) if (ORACLE.get(pid) == oracle)]
              d[ORACLE_ROWS[oracle]] = -9
        order = np.argsort(-d, kind='stable')[:2]
        best, second = float(d[order[0]]), float(d[order[1]])
        chosen = index.ids[int(order[0])]
        tc, cc = db.get_card_by_id(truth) or {}, db.get_card_by_id(chosen) or {}
        out.append({'covered': covered, 'lead': best - second, 'best': best, 'right': chosen == truth,
                    'same_card': bool(tc.get('oracle_id')) and tc.get('oracle_id') == cc.get('oracle_id')})
json.dump(out, open(TARGET, 'w'))
right = [x for x in out if x['covered'] and x['right']]
missing = [x for x in out if not x['covered']]
print(f"{len(out)} decisions; index complete: {len(right)} right; truth taken out ({MODE}): {len(missing)} (every add wrong)")
print("lead >=   right kept   | added with the truth taken out [another printing of the same card / another card]   (all need best >= 0.5)")
for t in (0.05, 0.08, 0.10, 0.12, 0.15, 0.20):
    ok = lambda x: x['lead'] >= t and x['best'] >= 0.5
    m = [x for x in missing if ok(x)]
    print(f"  {t:.2f}    {sum(map(ok, right)):4d}          {len(m):4d} [{sum(x['same_card'] for x in m)} / {sum(not x['same_card'] for x in m)}]")
