#!/usr/bin/env python3
"""
The reading pipeline in each of its four configurations, on a REHEARSAL server (a copy of the data in a second container - never the real one): the same 39 pictures
(rehearse_local.py's: 15 Deadpool phone captures, 12 ordinary and 12 hard evaluation pictures) with OCR on / off and the vision AI on / off, set through the Settings page's own
events (toggle_ocr, toggle_ai, set_reading_mode). Each answer is scored against the known one: an evaluation picture's stored printing (name and number), a Deadpool capture's card by
name (#1037-1051 are five cards three times over). Prints, for each configuration: who read the cards, what was added, what went to review, and what was added WRONG.

    venv/bin/python scripts/experiments/image_match/rehearse_matrix.py http://localhost:5002
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import socketio

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import printings  # noqa: E402

BASE = sys.argv[1]
P = printings()
DEADPOOL = ['Deadly Dispute', 'Lightning Bolt', 'Lightning Greaves', 'Sol Ring', 'Thrill of Possibility']


def set_switches(ocr, ai):
    client = socketio.Client()
    client.connect(BASE)
    client.emit('toggle_ocr', {'enabled': ocr})
    client.emit('toggle_ai', {'enabled': ai})
    client.emit('set_reading_mode', {'mode': 'evidence', 'accept_image_alone': True})
    time.sleep(2.5)
    client.disconnect()


def right(row, n):
    """True / False for an added card against what it should be; None when there is no answer to score against"""
    if row['status'] != 'added':
        return None
    card = row['card'] or ''
    if row['kind'] == 'deadpool':
        return card.lower().startswith(DEADPOOL[(n - 1) % 5].lower())
    truth = P.get(row['truth'])
    if not truth:
        return None
    name = re.split(r' // ', truth['name'])[0].lower()
    return card.lower().startswith(name) and f"#{truth['number']}" in card.replace('#0', '#')


for ocr, ai in ((True, True), (True, False), (False, True), (False, False)):
    set_switches(ocr, ai)
    out = HERE.parents[2] / 'data' / 'logs' / f'matrix_{int(ocr)}{int(ai)}.json'
    subprocess.run([sys.executable, str(HERE / 'rehearse_local.py'), BASE, '--mode', 'default', '--out', str(out)], capture_output=True, text=True)
    results = json.loads(out.read_text())['results']
    readers, scored = {}, {'right': 0, 'wrong': [], 'unscored': 0}
    for n, r in enumerate(results, 1):
        readers[r['reader'] or '(none)'] = readers.get(r['reader'] or '(none)', 0) + 1
        verdict = right(r, int(r['picture'].split('_')[1]) - 1036 if r['kind'] == 'deadpool' else n)
        if verdict is True:
            scored['right'] += 1
        elif verdict is False:
            scored['wrong'].append((r['picture'], r['card']))
        elif r['status'] == 'added':
            scored['unscored'] += 1
    seconds = sorted(r['seconds'] for r in results)
    added = sum(r['status'] == 'added' for r in results)
    print(f"OCR {'on ' if ocr else 'off'} | AI {'on ' if ai else 'off'}: added {added:2}, review {len(results) - added:2} | right {scored['right']:2}, WRONG {len(scored['wrong'])}, unscorable {scored['unscored']} "
          f"| median {seconds[len(seconds) // 2]:.2f} s, max {seconds[-1]:.1f} s | readers {readers}")
    for w in scored['wrong']:
        print('     WRONG:', w)
