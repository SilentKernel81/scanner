#!/usr/bin/env python3
"""
Tries the review panel of the scanner page in a real browser (headless Chromium): the printings the capture's picture looks like (phase 4 of docs/plans/READING_REFACTOR_PLAN.md),
the "foil marker was not read" hint, and that a tile picks the printing. It gives the page's own renderReview a made-up review item - nothing of the server's answers is needed, only
its page and its scripts - and clicks as a person would.

    venv/bin/python scripts/check_review_page.py http://localhost:5011 <station id> [screenshot.png]

NEVER against a server that holds real data (it only reads and renders, but the rule of the other check scripts stands): run the server on a temporary data folder.
The station must exist (the page is a 404 for an unknown one). Needs Chromium and websocket-client.
"""
import base64
import json
import subprocess
import sys
import tempfile
import time
import urllib.request

import websocket

BASE = sys.argv[1].rstrip('/')
STATION = sys.argv[2]
SHOT = sys.argv[3] if len(sys.argv) > 3 else None
PORT = 9335
results = []


class Browser:
    def __init__(self):
        self.process = subprocess.Popen(
            ['chromium', '--headless=new', f'--remote-debugging-port={PORT}', f'--user-data-dir={tempfile.mkdtemp()}',
             '--no-first-run', '--disable-gpu', '--window-size=1200,1400', 'about:blank'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f'http://localhost:{PORT}/json'))
                break
            except OSError:
                time.sleep(0.2)
        self.ws = websocket.create_connection([t for t in tabs if t['type'] == 'page'][0]['webSocketDebuggerUrl'], suppress_origin=True)
        self.n = 0
        self.problems = []
        for domain in ('Page', 'Runtime', 'Network', 'Log'):
            self.call(domain + '.enable')

    def note(self, message):
        method, params = message.get('method'), message.get('params', {})
        if method == 'Runtime.exceptionThrown':
            details = params['exceptionDetails']
            self.problems.append('script error: ' + (details.get('exception', {}).get('description') or details.get('text', ''))[:300])
        elif method == 'Network.responseReceived':
            response = params['response']
            if response['status'] >= 400 and response['url'].startswith(BASE) and '/api/' not in response['url']:
                self.problems.append(f"{response['status']} {response['url']}")

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({'id': self.n, 'method': method, 'params': params}))
        while True:
            message = json.loads(self.ws.recv())
            self.note(message)
            if message.get('id') == self.n:
                return message.get('result', {})

    def js(self, code):
        result = self.call('Runtime.evaluate', expression=code, awaitPromise=True, returnByValue=True)
        if 'exceptionDetails' in result:
            self.problems.append('script error in a check: ' + str(result['exceptionDetails'].get('exception', {}).get('description'))[:300])
            return None
        return result.get('result', {}).get('value')

    def settle(self, seconds=1.0):
        end = time.time() + seconds
        self.ws.settimeout(0.2)
        while time.time() < end:
            try:
                self.note(json.loads(self.ws.recv()))
            except (websocket.WebSocketTimeoutException, TimeoutError):
                pass
        self.ws.settimeout(None)

    def close(self):
        self.process.terminate()


def check(what, ok, detail=''):
    results.append(bool(ok))
    print(('  ok   ' if ok else '  FAIL ') + what + (f'   [{detail}]' if detail and not ok else ''))


CARD = {'id': 'c2', 'name': 'Card Two', 'set': 'Test Set', 'set_code': 'tst', 'number': '2', 'rarity': 'common', 'type': 'Creature', 'price': 0.5, 'price_foil': 1.0,
        'image_uri': None, 'treatments': [], 'finishes': ['nonfoil', 'foil'], 'oracle_text': '', 'mana_cost': '', 'colors': [], 'color_identity': []}
CANDIDATES = [{'id': f'c{n}', 'name': f'Card {n}', 'set': 'Test Set', 'set_code': 'tst', 'number': str(n), 'image_uri': None, 'finishes': ['nonfoil'], 'similarity': 0.9 - n / 10}
              for n in range(1, 6)]


def item(candidates, card=None, foil='unknown', name=''):
    return {'id': 7, 'total': 3, 'image_url': None, 'ai': {'name': name, 'number': '', 'set': '', 'foil': foil}, 'captured_at': '2026-10-20 12:00:00', 'station': None,
            'card': card, 'candidates': candidates}


def main():
    # Wait for the server
    for _ in range(240):
        try:
            urllib.request.urlopen(BASE + '/', timeout=2)
            break
        except OSError:
            time.sleep(0.5)
    b = Browser()
    try:
        b.call('Page.navigate', url=f'{BASE}/scan/{STATION}')
        b.settle(3.0)
        check('the scanner page loads and has the review panel', b.js("typeof renderReview === 'function' && !!document.getElementById('review-panel')"))
        b.js("window.__emitted = []; socket.emit = (...args) => { window.__emitted.push(args); };")

        print('A review item with candidates, one of them what was read')
        b.js(f"renderReview({json.dumps(item(CANDIDATES, CARD))})")
        b.settle(0.5)
        check('five tiles are shown', b.js("document.querySelectorAll('.review-candidates .review-candidate').length") == 5)
        check('the heading says what they are', b.js("document.querySelector('.review-candidates .field-label').textContent") == 'The picture looks like')
        tags = b.js("[...document.querySelectorAll('.review-candidate')].map(t => (t.querySelector('.review-candidate-tag') || {}).textContent || '')")
        check('the first is "closest", the one that was read says so, the others have no tag', tags == ['closest', 'what was read', '', '', ''], str(tags))
        check('the one that was read is marked', b.js("document.querySelectorAll('.review-candidate.is-suggested').length") == 1
              and b.js("document.querySelector('.review-candidate.is-suggested').dataset.printing") == 'c2')
        check('the foil hint shows when the marker was not read', b.js("!!document.querySelector('.review-finish-hint')"))
        b.js("document.querySelectorAll('.review-candidate')[3].click()")
        emitted = b.js("window.__emitted.filter(e => e[0] === 'select_printing')")
        check('a click on a tile picks that printing', emitted == [['select_printing', {'id': 'c4'}]], str(emitted))
        if SHOT:
            shot = b.call('Page.captureScreenshot', format='png')
            open(SHOT, 'wb').write(base64.b64decode(shot['data']))

        print('A review item the picture could not help with')
        b.js(f"renderReview({json.dumps(item([], None, foil='foil'))})")
        b.settle(0.4)
        check('no candidates: no section at all', b.js("document.querySelectorAll('.review-candidates').length") == 0)
        check('a marker that was read: no hint', b.js("document.querySelectorAll('.review-finish-hint').length") == 0)
        b.js("renderReview(" + json.dumps({k: v for k, v in item([], None).items() if k != 'candidates'}) + ")")
        check('an item from an older server (no candidates field) still renders', b.js("!!document.querySelector('.review-body')"))

        check('the page raised no script error and no file was missing', not b.problems, '; '.join(b.problems)[:300])
    finally:
        b.close()
    failed = results.count(False)
    print(f"\n{len(results) - failed} of {len(results)} checks passed")
    sys.exit(1 if failed else 0)


main()
