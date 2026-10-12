#!/usr/bin/env python3
"""
Walks through a running scanner server and writes down what it answers - to compare a server
before and after a change that must not alter its behaviour (a refactoring, a move of code):

    venv/bin/python scripts/walk_server.py http://localhost:5001 before.json
    ... change the code, start the server again on a FRESH copy of the same data ...
    venv/bin/python scripts/walk_server.py http://localhost:5001 after.json
    venv/bin/python scripts/walk_server.py --compare before.json after.json

NEVER against a server that holds real data: the walk adds, changes and deletes - an inventory
entry, a deck, a trade, a backup, a station, a captured card. Run the server from a copy of
the checkout with copies of the databases in a temporary folder (the server keeps its data
in data/ beside its code, so a copy of the folder is a server with its own data), and give it
a station token of its own if you like.

What it does, in order:
  1. every GET route that needs nothing filled in (from tests/app_surface.json), except
     those that ask other sites or stream;
  2. routes that need an id, with ids found in the answers of step 1;
  3. a fixed list of actions: edit an inventory entry and put it back, a deck (create, add
     cards, export, duplicate, delete), a trade, a backup, settings;
  4. a station: a capture uploaded as a station would (a recorded picture, if one is given
     with --capture), its outcome, undo;
  5. the scanner page's Socket.IO events: connect, a search, a few toggles.

For each step it records the status code and the answer with everything that differs between
two runs removed (times, ids of things created during the walk, timings). Two walks over the
same data then give the same file, and --compare shows the steps that differ.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests
import socketio

SURFACE = Path(__file__).resolve().parent.parent / 'tests' / 'app_surface.json'
# Not walked: they ask other sites (answers change and it is not ours to load-test them), or
# never end (the camera's stream)
SKIP = {'/video_feed', '/api/decks/popular', '/api/precons', '/api/local_ai_models', '/api/ai_models'}
# Keys whose values differ from run to run. Their presence is still recorded.
VOLATILE = re.compile(r'(time|date|_at$|^at$|created|modified|updated|last_seen|elapsed|duration|seconds|'
                      r'timestamp|uptime|^id$|_id$|backup|file|path|uid|capture$|number$|image|^url$)', re.I)


def tidy(value, key=''):
    """An answer with the run-dependent parts replaced by '*', so that two runs compare equal"""
    if isinstance(value, dict):
        return {k: ('*' if VOLATILE.search(k) and not isinstance(v, (dict, list)) else tidy(v, k))
                for k, v in sorted(value.items())}
    if isinstance(value, list):
        items = [tidy(v, key) for v in value]
        return items[:30] + ([f"... {len(items) - 30} more"] if len(items) > 30 else [])  # long lists: the start and the length
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + f"... ({len(value)} characters)"
    return value


class Walk:
    def __init__(self, server):
        self.server = server.rstrip('/')
        self.http = requests.Session()
        self.steps = []  # [{'step': ..., 'status': ..., 'answer': ...}]

    def call(self, step, method, path, **kwargs):
        """One request; its tidied answer is recorded under `step`. Returns the parsed JSON (or None)."""
        try:
            response = self.http.request(method, self.server + path, timeout=kwargs.pop('timeout', 60), **kwargs)
        except requests.RequestException as e:
            self.steps.append({'step': step, 'status': f"no answer ({type(e).__name__})"})
            return None
        kind = response.headers.get('Content-Type', '').split(';')[0]
        try:
            body = response.json()
            answer = tidy(body)
        except ValueError:
            # Not JSON (a page, a download): its kind and roughly its size say enough
            body, answer = None, f"{kind}, {round(len(response.content), -2)} bytes"
            if kind.startswith('text/html'):
                answer = f"{kind}: {re.search(r'<title>(.*?)</title>', response.text, re.S).group(1).strip() if '<title>' in response.text else '?'}"
        self.steps.append({'step': step, 'status': response.status_code, 'answer': answer})
        return body

    # ------------------------------------------------------------------------

    def plain_gets(self):
        for rule, methods, _endpoint in json.loads(SURFACE.read_text())['routes']:
            if 'GET' in methods and '<' not in rule and rule not in SKIP:
                self.call(f"GET {rule}", 'GET', rule)
        self.call("GET /api/inventory?area=scan", 'GET', '/api/inventory?area=scan')
        self.call("GET /api/cards/search?q=bolt", 'GET', '/api/cards/search?q=bolt')
        self.call("GET /api/cards/printings?name=Lightning Bolt", 'GET', '/api/cards/printings?name=Lightning%20Bolt')
        self.call("GET /api/stats?station=walk", 'GET', '/api/stats?station=walk')

    def with_ids(self):
        inventory = self.http.get(self.server + '/api/inventory', timeout=60).json()
        rows = inventory.get('cards') or inventory.get('inventory') or []
        self.row = rows[0] if rows else None
        if self.row:
            self.call("GET an entry's printings", 'GET', f"/api/inventory/{self.row['id']}/printings")
        decks = self.http.get(self.server + '/api/decks', timeout=60).json().get('decks') or []
        if decks:
            deck_id = decks[0]['id']
            self.call("GET a deck", 'GET', f"/api/decks/{deck_id}")
            self.call("GET a deck's export (text)", 'GET', f"/api/decks/{deck_id}/export/text")
        games = self.http.get(self.server + '/api/games', timeout=60).json()
        for fmt in (games.get('export_formats') or games.get('formats') or [])[:3]:
            key = fmt.get('id') if isinstance(fmt, dict) else fmt
            self.call(f"GET /api/export_inventory/{key}", 'GET', f"/api/export_inventory/{key}")
        self.call("GET a capture's outcome that does not exist", 'GET', '/api/stations/walk/captures/0')
        self.call("GET a page for a camera", 'GET', '/scan/walk')
        self.call("GET a missing thumbnail", 'GET', '/captures/none.jpg')

    def actions(self):
        if self.row:
            row_id, quantity = self.row['id'], int(self.row.get('entry_quantity') or self.row.get('quantity') or 1)
            self.call("inventory: one more copy", 'POST', f"/api/inventory/update/{row_id}", json={'quantity': quantity + 1})
            self.call("inventory: back to what it was", 'POST', f"/api/inventory/update/{row_id}", json={'quantity': quantity})
            self.call("inventory: a tag on one entry", 'POST', '/api/inventory/bulk', json={'ids': [row_id], 'action': 'add_tag', 'value': 'walk'})
            self.call("inventory: the tag off again", 'POST', '/api/inventory/bulk', json={'ids': [row_id], 'action': 'remove_tag', 'value': 'walk'})
        self.call("deck: create without a format (refused)", 'POST', '/api/decks', json={'name': 'Walk test deck'})
        deck = self.call("deck: create", 'POST', '/api/decks', json={'name': 'Walk test deck', 'format': 'modern'})
        deck_id = ((deck or {}).get('deck') or {}).get('id')
        if deck_id:
            self.call("deck: add cards", 'POST', f"/api/decks/{deck_id}/cards",
                      json={'cards': [{'name': 'Lightning Bolt', 'change': 4}, {'name': 'Mountain', 'change': 20}]})
            self.call("deck: import a list", 'POST', f"/api/decks/{deck_id}/import", json={'text': "2 Shock\n1 Not A Real Card Name"})
            self.call("deck: read", 'GET', f"/api/decks/{deck_id}")
            self.call("deck: export", 'GET', f"/api/decks/{deck_id}/export/text")
            copy = self.call("deck: duplicate", 'POST', f"/api/decks/{deck_id}/duplicate")
            copy_id = ((copy or {}).get('deck') or {}).get('id')
            if copy_id:
                self.call("deck: delete the copy", 'DELETE', f"/api/decks/{copy_id}")
            self.call("deck: rename", 'PUT', f"/api/decks/{deck_id}", json={'name': 'Walk test deck 2'})
            self.call("deck: delete", 'DELETE', f"/api/decks/{deck_id}")
        if self.row:
            trade = self.call("trade: set one entry aside", 'POST', '/api/trades', json={'name': 'Walk trade', 'ids': [self.row['id']]})
            trades = self.http.get(self.server + '/api/trades', timeout=60).json().get('trades') or []
            mine = [t for t in trades if t.get('name') == 'Walk trade']
            if mine:
                self.call("trade: rename", 'PUT', f"/api/trades/{mine[0]['id']}", json={'name': 'Walk trade 2'})
                self.call("trade: cancel", 'DELETE', f"/api/trades/{mine[0]['id']}")
        self.call("backup: create", 'POST', '/api/backups', json={'note': 'walk'})
        backups = (self.http.get(self.server + '/api/backups', timeout=60).json().get('backups') or [])
        walk_backups = [b for b in backups if b.get('note') == 'walk']
        if walk_backups:
            self.call("backup: delete", 'DELETE', f"/api/backups/{walk_backups[0]['id']}")
        self.call("backup: schedule as it is", 'POST', '/api/backups/schedule', json={})
        self.call("settings: sound", 'POST', '/api/sound', json={'volume': 10})
        self.call("settings: scan location", 'POST', '/api/scan_location?station=walk', json={'location': ''})
        self.call("scanned cards: what would move", 'GET', '/api/scan_inventory/to_collection')

    def station(self, picture):
        self.call("station: rename before it exists", 'PUT', '/api/stations/walk', json={'name': 'Walk camera'})
        if picture:
            data = Path(picture).read_bytes()
            form = {'name': 'Walk camera', 'camera': '1', 'mode': 'auto', 'wait': '45', 'capture_id': 'walk-capture-1', 'foil_is_image': '1'}
            self.call("station: a capture", 'POST', '/api/stations/walk/captures', files={'image': ('card.jpg', data, 'image/jpeg')}, data=form, timeout=120)
            self.call("station: the same capture again (counts once)", 'POST', '/api/stations/walk/captures',
                      files={'image': ('card.jpg', data, 'image/jpeg')}, data=form, timeout=120)
            self.call("station: stats after it", 'GET', '/api/stats?station=walk')
            self.call("station: its scanned cards", 'GET', '/api/inventory?area=scan&station=walk')
            self.call("station: undo", 'POST', '/api/stations/walk/undo')
        self.call("station: an upload without a picture", 'POST', '/api/stations/walk/captures', data={'name': 'Walk camera'})
        self.call("station: list", 'GET', '/api/stations')
        self.call("station: clear its scanned cards", 'POST', '/api/clear_inventory?area=scan&station=walk', json={})
        self.call("station: forget", 'DELETE', '/api/stations/walk')

    def page_events(self):
        """The scanner page's side: what the server sends back for a few events"""
        received = []
        page = socketio.Client()
        page.on('*', lambda event, data=None: received.append((event, data)))
        try:
            page.connect(f"{self.server}?station=walk", wait_timeout=10)
        except Exception as e:
            self.steps.append({'step': 'page: connect', 'status': f"no connection ({type(e).__name__})"})
            return

        def send(step, event, data, wait=1.5):
            received.clear()
            page.emit(event, data)
            time.sleep(wait)
            # Names of what came back, and the tidied content - logs carry times in their text
            answer = sorted({name for name, _ in received})
            content = {name: tidy(data) for name, data in received if name not in ('log',)}
            self.steps.append({'step': step, 'status': 'events', 'answer': {'events': answer, 'content': content}})

        time.sleep(1.5)
        self.steps.append({'step': 'page: on connect', 'status': 'events', 'answer': sorted({name for name, _ in received})})
        send("page: search a card", 'search_card', {'card_name': 'Lightning Bolt'}, wait=2.5)
        send("page: search with a number", 'search_card', {'card_name': 'Shire Terrace', 'collector_number': '261', 'set_code': 'LTR'}, wait=2.5)
        send("page: dismiss the card", 'dismiss_card', {})
        send("page: search nothing", 'search_card', {'card_name': ''})
        send("page: debug trace on", 'toggle_debug_trace', {'enabled': True})
        send("page: debug trace off", 'toggle_debug_trace', {'enabled': False})
        send("page: undo with nothing to undo", 'undo_last_add', {})
        send("page: capture without a camera", 'capture_card', {'card_number': 1})
        send("page: detection off and on", 'toggle_detection', {'enabled': True})
        page.disconnect()

    def run(self, picture):
        self.plain_gets()
        self.with_ids()
        self.actions()
        self.station(picture)
        self.page_events()
        self.call("at the end: stats", 'GET', '/api/stats')
        return self.steps


def compare(first, second):
    a, b = (json.loads(Path(p).read_text()) for p in (first, second))
    by_step = {s['step']: s for s in b}
    different = 0
    for step in a:
        other = by_step.pop(step['step'], None)
        if other != step:
            different += 1
            print(f"DIFFERENT  {step['step']}")
            print(f"   {first}: {json.dumps(step, sort_keys=True)[:600]}")
            print(f"   {second}: {json.dumps(other, sort_keys=True)[:600]}")
    for step in by_step:
        different += 1
        print(f"ONLY IN {second}: {step}")
    print(f"{len(a)} steps, {different} different")
    return different


def main():
    parser = argparse.ArgumentParser(description="Record what a scanner server answers, or compare two recordings")
    parser.add_argument('--compare', action='store_true', help="compare two recordings instead of walking")
    parser.add_argument('--capture', help="a picture of a card to upload as a station's capture")
    parser.add_argument('first', help="the server's address, or with --compare the first recording")
    parser.add_argument('second', help="the file to write, or with --compare the second recording")
    args = parser.parse_args()
    if args.compare:
        sys.exit(1 if compare(args.first, args.second) else 0)
    steps = Walk(args.first).run(args.capture)
    Path(args.second).write_text(json.dumps(steps, indent=1, sort_keys=True))
    statuses = {}
    for step in steps:
        statuses[str(step['status'])] = statuses.get(str(step['status']), 0) + 1
    print(f"{len(steps)} steps recorded in {args.second}: {statuses}")


if __name__ == '__main__':
    main()
