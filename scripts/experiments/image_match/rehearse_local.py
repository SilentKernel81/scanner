#!/usr/bin/env python3
"""
A rehearsal of the reading on a LOCAL COPY of a server, as the deploy plan's step 5 describes it for the real one: the pictures go to the server's station route as a station would
send them (a station id of its own, foil_is_image=1, the pictures are the finished cards) and what became of each is written down. The mode is switched with the Settings page's own
event (set_reading_mode), so the event, its guard and the next card taking it are part of what is tried.

    # a server on a COPY of the data (never the real data), on port 5012, then:
    venv/bin/python scripts/experiments/image_match/rehearse_local.py http://localhost:5012 --mode cascade --out data/logs/rehearsal_cascade.json
    venv/bin/python scripts/experiments/image_match/rehearse_local.py http://localhost:5012 --mode evidence --out data/logs/rehearsal_evidence.json --compare data/logs/rehearsal_cascade.json

The pictures: the 15 Deadpool Secret Lair captures, `--ordinary N` OCR-confirmed pictures and `--hard N` pictures the vision AI had to read (a seeded sample of the experiment's evaluation set).
Everything is added to the COPY's scanned cards and review queue. NEVER against a server with a real collection.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import requests
import socketio

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import HOME, OUT, TRUTH  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument('base')
ap.add_argument('--mode', choices=['cascade', 'evidence', 'default'], required=True, help="'default': do not touch the server's mode - see what it does by itself")
ap.add_argument('--alone', action='store_true', help='evidence mode: also add what the picture alone decided')
ap.add_argument('--ordinary', type=int, default=12)
ap.add_argument('--hard', type=int, default=12)
ap.add_argument('--out', required=True)
ap.add_argument('--compare')
ap.add_argument('--station', default='rehearsal')
arguments = ap.parse_args()
BASE = arguments.base.rstrip('/')
run = f"{arguments.mode}{'-alone' if arguments.alone else ''}-{int(time.time())}"

# ---- wait for the server
for _ in range(360):
    try:
        requests.get(BASE + '/', timeout=2)
        break
    except requests.RequestException:
        time.sleep(0.5)
else:
    sys.exit('the server did not answer')

# ---- the mode, through the Settings page's own event (or left alone: --mode default)
if arguments.mode == 'default':
    status = requests.get(BASE + '/api/scan_settings', timeout=10).json()['reading']
    print('the server, left as it is:', status)
    arguments.mode, arguments.alone = status['mode'], status['accept_image_alone']      # what it is doing, for the report
else:
    client = socketio.Client()
    answers = []
    client.on('reading_mode_changed', lambda data: answers.append(data))
    client.on('error', lambda data: answers.append({'error': data}))
    client.connect(BASE)
    client.emit('set_reading_mode', {'mode': arguments.mode, 'accept_image_alone': bool(arguments.alone)})
    for _ in range(50):
        if answers:
            break
        time.sleep(0.1)
    client.disconnect()
    print('the server says:', answers[-1] if answers else 'NO ANSWER')
    assert answers and answers[-1].get('mode') == arguments.mode, 'the mode was not taken'

# ---- the pictures
rows = json.loads((OUT / 'phash_rgb_card' / 'rows.json').read_text())
items = json.loads((OUT / 'phash_rgb_card' / 'strip_items.json').read_text())
ordered = list(zip([r for k in ('collection', 'scanned') for r in rows if r['kind'] == k], items))
frozen = json.loads((TRUTH / 'hard_truth.json').read_text())
rng = np.random.default_rng(11)
easy = [r for r, x in ordered if not x['hard']]
hard = [r for r, x in ordered if x['hard'] and frozen.get(Path(r['path']).name)]
pictures = [('deadpool', f, None) for f in sorted((HOME / 'truth' / 'rescan_cards').iterdir())
            if f.name.startswith('phone-0240eb84_10') and 1037 <= int(f.name.split('_')[1]) <= 1051]
pictures += [('ordinary', Path(easy[i]['path']), easy[i]['truth']) for i in rng.choice(len(easy), arguments.ordinary, replace=False)]
pictures += [('hard', Path(hard[i]['path']), frozen[Path(hard[i]['path']).name]) for i in rng.choice(len(hard), arguments.hard, replace=False)]

results = []
for n, (kind, path, truth) in enumerate(pictures, 1):
    started = time.time()
    with open(path, 'rb') as f:
        answer = requests.post(f'{BASE}/api/stations/{arguments.station}/captures', files={'image': (path.name, f, 'image/jpeg')},
                               data={'foil_is_image': '1', 'capture_id': f'{run}-{n}', 'name': 'Rehearsal', 'wait': '120'}, timeout=180)
    body = answer.json()
    card = body.get('card') or {}
    read = body.get('read') or {}
    results.append({'kind': kind, 'picture': path.name, 'http': answer.status_code, 'status': body.get('status'), 'reason': body.get('reason'),
                    'card': f"{card.get('name')} [{card.get('set')} #{card.get('number')}] {card.get('finish')}" if card else None,
                    'read': f"{read.get('name')} {read.get('set')} {read.get('number')} foil={read.get('foil')}" if read else None, 'reader': read.get('reader'),
                    'seconds': round(time.time() - started, 2), 'truth': truth})
    print(f"{n:2} {kind:8} {body.get('status') or answer.status_code!s:8} {results[-1]['reader'] or '-':14} {(results[-1]['card'] or results[-1]['read'] or '')[:62]}  ({results[-1]['seconds']} s)")
Path(arguments.out).write_text(json.dumps({'run': run, 'mode': arguments.mode, 'alone': arguments.alone, 'results': results}, indent=1))

def summary(label, rows_):
    counts = {}
    for r in rows_:
        counts[(r['kind'], r['status'])] = counts.get((r['kind'], r['status']), 0) + 1
    print(label, dict(sorted(counts.items())))

summary(f'\n{arguments.mode}{" + picture alone" if arguments.alone else ""}:', results)
readers = {}
for r in results:
    readers[r['reader']] = readers.get(r['reader'], 0) + 1
print('read by:', readers)
if arguments.compare:
    before = {r['picture']: r for r in json.loads(Path(arguments.compare).read_text())['results']}
    differ = [(before[r['picture']], r) for r in results if r['picture'] in before and (before[r['picture']]['status'], before[r['picture']]['card']) != (r['status'], r['card'])]
    print(f'\ndiffer from {arguments.compare}: {len(differ)} of {len(results)}')
    for old, new in differ:
        print(f"  {new['kind']:8} {new['picture'][-22:]}: {old['status']} {old['card'] or old['read']}  ->  {new['status']} {new['card'] or new['read']}   [{old['reader']} -> {new['reader']}]")
