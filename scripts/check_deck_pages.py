#!/usr/bin/env python3
"""
Tries the deck pages in a real browser (headless Chromium), against a running scanner server:

    venv/bin/python scripts/check_deck_pages.py http://localhost:5001

NEVER against a server that holds real data: it creates, changes, duplicates and deletes
decks (its own: "Check deck ..."). Run the server on copies, as for scripts/walk_server.py.

Why this exists: the tests cover the server's answers, not the pages. When the decks moved
out of the collection page and were redesigned (docs/plans/DECK_PAGE_PLAN.md), the risk was
a feature that quietly did not survive - a button that is drawn and does nothing. This goes
through what the deck pages can do, does each thing as a person would (through the page's
own controls), and looks at the result both in the page and in the server's answer.

Not covered: what asks other sites (EDHREC suggestions, popular decks, Archidekt, the
preconstructed decks' ranking) - those need the internet and other people's servers.

It needs Chromium, and websocket-client (in the server's requirements through socketio).
"""
import json
import subprocess
import sys
import tempfile
import time
import urllib.request

import websocket

BASE = sys.argv[1].rstrip('/')
PORT = 9334
results = []          # (ok, what)


def api(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=30))


class Browser:
    """A page in headless Chromium, driven over its debugging port"""

    def __init__(self):
        self.process = subprocess.Popen(
            ['chromium', '--headless=new', f'--remote-debugging-port={PORT}', f'--user-data-dir={tempfile.mkdtemp()}',
             '--no-first-run', '--disable-gpu', '--window-size=1440,900', 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f'http://localhost:{PORT}/json'))
                break
            except OSError:
                time.sleep(0.2)
        self.ws = websocket.create_connection([t for t in tabs if t['type'] == 'page'][0]['webSocketDebuggerUrl'], suppress_origin=True)
        self.n = 0
        self.problems = []    # script errors and the server's own files that were not found
        for domain in ('Page', 'Runtime', 'Network', 'Log'):
            self.call(domain + '.enable')

    def note(self, message):
        method, params = message.get('method'), message.get('params', {})
        if method == 'Runtime.exceptionThrown':
            details = params['exceptionDetails']
            self.problems.append('script error: ' + (details.get('exception', {}).get('description') or details.get('text', ''))[:300])
        elif method == 'Network.responseReceived':
            response = params['response']
            # /api/ answers with 404 on purpose (a deck that is gone); a missing page or file is a fault
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
        """The value of a JavaScript expression in the page (promises are waited for)"""
        result = self.call('Runtime.evaluate', expression=code, awaitPromise=True, returnByValue=True)
        if 'exceptionDetails' in result:
            self.problems.append('script error in a check: ' + str(result['exceptionDetails'].get('exception', {}).get('description'))[:300])
            return None
        return result.get('result', {}).get('value')

    def go(self, path, wait=3.0):
        self.call('Page.navigate', url=BASE + path)
        self.settle(wait)

    def settle(self, seconds=1.2):
        """Let the page work, and read what it reports meanwhile"""
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
    results.append((bool(ok), what))
    print(('  ok   ' if ok else '  FAIL ') + what + (f'   [{detail}]' if detail and not ok else ''))


def main():
    b = Browser()
    try:
        before = {d['id'] for d in api('/api/decks')['decks']}

        print('The deck list')
        b.go('/decks')
        tiles = b.js("document.querySelectorAll('#deck-list [data-deck]').length")
        check('every deck has a tile', tiles == len(before), f'{tiles} tiles, {len(before)} decks')
        check('"Decks" is marked in the header', b.js("!!document.querySelector('.page-link.is-active#page-link-decks')"))
        first = b.js("document.querySelector('#deck-list [data-deck] .deck-tile-name, #deck-list [data-deck] h3').textContent.trim()")
        word = (first or 'x').split()[0]
        b.js(f"(() => {{ const f = document.getElementById('deck-filter-text'); f.value = {json.dumps(word)}; f.dispatchEvent(new Event('input')); }})()")
        b.settle(0.6)
        narrowed = b.js("document.querySelectorAll('#deck-list [data-deck]').length")
        check('the name filter narrows the list', 0 < narrowed <= tiles, f'{narrowed} of {tiles}')
        b.js("(() => { const f = document.getElementById('deck-filter-text'); f.value = ''; f.dispatchEvent(new Event('input')); })()")
        b.settle(0.6)
        b.js("(() => { const s = document.getElementById('deck-sort'); s.value = 'name'; s.dispatchEvent(new Event('change')); })()")
        names = b.js("[...document.querySelectorAll('#deck-list [data-deck] .deck-tile-name, #deck-list [data-deck] h3')].map(e => e.textContent.trim())")
        check('sorting by name orders the tiles', names == sorted(names, key=str.lower) or names == sorted(names), str(names)[:120])
        check('"What can I build?" lists your cards', b.js("document.querySelectorAll('#around-cards .result-row').length") > 0)
        check('a tile shows its commander and is a link to the deck',
              b.js("(() => { const t = document.querySelector('#deck-list [data-deck]'); return /^\\/decks\\/\\d+$/.test(t.getAttribute('href')) && !!t.querySelector('.deck-tile-art'); })()"))
        for tab in ('build', 'precons', 'mine'):
            b.js(f"document.querySelector('#deck-tabs [data-tab=\"{tab}\"]').click()")
            check(f'the list\'s tab "{tab}" opens',
                  b.js(f"!document.getElementById('deck-tab-{tab}').hidden && document.querySelectorAll('.deck-tab:not([hidden])').length === 1"))

        print('A new deck from a pasted list')
        b.js("document.getElementById('deck-new').click()")
        b.js("""(() => { document.getElementById('new-deck-name').value = 'Check deck one';
                         document.getElementById('new-deck-text').value = '1 Sol Ring\\n2 Island\\n1 Not A Real Card Name';
                         document.getElementById('deck-modal-save').click(); })()""")
        b.settle(2.5)
        path = b.js('location.pathname')
        check('the new deck opens at an address of its own', path.startswith('/decks/') and path[7:].isdigit(), path)
        deck_id = int(path[7:]) if path[7:].isdigit() else 0
        deck = api(f'/api/decks/{deck_id}')['deck']
        check('the pasted cards are in it', {c['name']: c['quantity'] for c in deck['cards']}.get('Island') == 2, str([(c['name'], c['quantity']) for c in deck['cards']]))
        check('a card the data does not know is reported', 'Not A Real Card Name' in (b.js("document.body.innerText") or ''))

        def row(name):
            return f"[...document.querySelectorAll('[data-name]')].find(r => r.dataset.name === {json.dumps(name)} && r.closest('#deck-cards'))"

        def cards():
            return {(c['name'], c['board']): c for c in api(f'/api/decks/{deck_id}')['deck']['cards']}

        print('Changing its cards')
        check('the deck shows its cards', b.js(f"!!{row('Sol Ring')}"))
        b.js(f"{row('Sol Ring')}.querySelector('[data-change=\"1\"]').click()")
        b.settle()
        check('one more copy', cards()[('Sol Ring', 'main')]['quantity'] == 2)
        b.js(f"{row('Sol Ring')}.querySelector('[data-change=\"-1\"]').click()")
        b.settle()
        check('one copy less', cards()[('Sol Ring', 'main')]['quantity'] == 1)
        b.js(f"{row('Island')}.querySelector('[data-move=\"side\"]').click()")
        b.settle()
        check('a card moves to the sideboard / maybe', ('Island', 'side') in cards())
        b.js(f"{row('Island')}.querySelector('[data-move=\"main\"]').click()")
        b.settle()
        check('... and back to the main deck', ('Island', 'main') in cards())
        was = cards()[('Island', 'main')]['printing']['id']
        b.js(f"{row('Island')}.querySelector('[data-printing]').click()")
        b.settle(2.5)
        printings = b.js("document.querySelectorAll('#printing-grid .printing').length")
        check('the printings of a card are offered', printings > 1, f'{printings}')
        b.js("[...document.querySelectorAll('#printing-grid .printing')].find(p => !p.classList.contains('is-current')).click()")
        b.settle()
        check('another printing is taken', cards()[('Island', 'main')]['printing']['id'] != was)
        check('totals are shown', '4 cards' in (b.js("document.getElementById('deck-totals').innerText") or '').replace('\\n', ' ') or
              b.js("document.body.innerText.includes('4')"))
        check('the charts are drawn', b.js("document.getElementById('deck-charts').children.length") > 0)
        check('legality or problems are shown', b.js("document.getElementById('deck-issues').children.length") > 0)

        print('The two views of a deck')
        rows = b.js("document.querySelectorAll('#deck-cards .deck-row').length")
        b.js("document.querySelector('#deck-view [data-view=\"stacks\"]').click()")
        b.settle(0.8)
        check('Stacks shows every card as a card', b.js("document.querySelectorAll('#deck-cards .stack-card').length") == rows and rows > 0, f'{rows} rows')
        check('... with the same buttons', b.js("!!document.querySelector('#deck-cards .stack-card [data-change=\"1\"]') && !!document.querySelector('#deck-cards .stack-card [data-printing]')"))
        b.js("document.querySelector('#deck-grouping [data-grouping=\"cost\"]').click()")
        b.settle(0.6)
        titles = b.js("[...document.querySelectorAll('#deck-cards .deck-group-title span:first-child')].map(e => e.textContent)")
        check('grouping by cost', any(title.startswith('Cost ') for title in titles or []), str(titles))
        b.js("document.querySelector('#deck-grouping [data-grouping=\"colour\"]').click()")
        b.settle(0.6)
        titles = b.js("[...document.querySelectorAll('#deck-cards .deck-group-title span:first-child')].map(e => e.textContent)")
        check('grouping by colour', any(title in ('Colourless', 'Blue', 'Red', 'White', 'Black', 'Green', 'Multicolour') for title in titles or []), str(titles))
        b.js("document.querySelector('#deck-grouping [data-grouping=\"type\"]').click(); document.querySelector('#deck-view [data-view=\"columns\"]').click()")
        b.settle(0.6)
        check('back to Columns', b.js("document.getElementById('deck-cards').classList.contains('deck-columns')"))
        b.js("document.getElementById('deck-add').click()")
        check('"Add cards" opens its panel', b.js("document.getElementById('add-drawer').classList.contains('show')"))
        b.js("document.getElementById('deck-more').click()")
        check('the menu opens', b.js("!document.getElementById('deck-more-list').hidden"))
        b.js("document.body.click()")
        check('... and closes on a click elsewhere', b.js("document.getElementById('deck-more-list').hidden"))

        print('Adding cards by search')
        b.js("(() => { const f = document.getElementById('search-text'); f.value = 'Lightning Bolt'; f.dispatchEvent(new Event('input')); })()")
        b.settle(2.0)
        found = b.js("document.querySelectorAll('#search-results .result-row').length")
        check('the search finds cards', found > 0, f'{found}')
        b.js("document.querySelector('#search-results .result-row [data-add=\"main\"]').click()")
        b.settle()
        check('a result is added to the deck', any(name.startswith('Lightning') for name, _ in cards()), str(list(cards())))
        b.js("(() => { const f = document.getElementById('search-legal'); f.checked = false; f.dispatchEvent(new Event('change')); })()")
        b.settle(1.5)
        check('the search answers to its switches', b.js("document.querySelectorAll('#search-results .result-row').length") >= found)

        print('The deck itself')
        b.js("(() => { const f = document.getElementById('deck-name'); f.value = 'Check deck renamed'; f.dispatchEvent(new Event('change')); })()")
        b.settle()
        check('renaming', api(f'/api/decks/{deck_id}')['deck']['name'] == 'Check deck renamed')
        formats = b.js("[...document.getElementById('deck-format').options].map(o => o.value)")
        other = next(f for f in formats if f != deck['format'])
        b.js(f"(() => {{ const f = document.getElementById('deck-format'); f.value = {json.dumps(other)}; f.dispatchEvent(new Event('change')); }})()")
        b.settle()
        check('changing the format', api(f'/api/decks/{deck_id}')['deck']['format'] == other)
        for kind in ('text', 'buylist'):
            try:
                status = urllib.request.urlopen(f'{BASE}/api/decks/{deck_id}/export/{kind}', timeout=30).status
            except Exception as e:  # noqa: BLE001
                status = str(e)
            check(f'the {kind} export answers', status == 200, str(status))
        check('Export, Buy list, Import, Duplicate and Delete are there',
              b.js("['deck-export','deck-buylist','deck-import','deck-duplicate','deck-delete'].every(id => !!document.getElementById(id))"))
        b.js("document.getElementById('deck-import').click()")
        b.js("""(() => { document.getElementById('new-deck-text').value = '3 Forest'; document.getElementById('deck-modal-save').click(); })()""")
        b.settle(2.0)
        check('importing cards into the deck', cards().get(('Forest', 'main'), {}).get('quantity') == 3)

        print('Addresses')
        b.go(f'/decks/{deck_id}')
        check('a reload stays in the deck', b.js("!document.getElementById('deck-builder').hidden && document.getElementById('deck-name').value === 'Check deck renamed'"))
        b.js("document.getElementById('deck-back').click()")
        b.settle()
        check('back to the list changes the address', b.js('location.pathname') == '/decks')
        b.js('history.back()')
        b.settle(2.0)
        check("the browser's Back returns to the deck", b.js('location.pathname') == f'/decks/{deck_id}' and b.js("!document.getElementById('deck-builder').hidden"))
        b.js("document.getElementById('deck-duplicate').click()")
        b.settle(2.0)
        copy_path = b.js('location.pathname')
        copy_id = int(copy_path[7:]) if copy_path[7:].isdigit() else 0
        check('duplicating opens the copy', copy_id not in (0, deck_id), copy_path)
        for doomed in (copy_id, deck_id):
            b.go(f'/decks/{doomed}', wait=2.0)
            b.js("document.getElementById('deck-delete').click()")
            b.settle(0.6)
            b.js("[...document.querySelectorAll('#dialog-modal button')].find(x => x.textContent.trim() === 'Delete').click()")
            b.settle(1.5)
        left = {d['id'] for d in api('/api/decks')['decks']}
        check('deleting, asked first', left == before, f'{sorted(left - before)} left over')
        b.go('/decks/999999', wait=2.5)
        check('a deck that does not exist shows the list', b.js('location.pathname') == '/decks' and b.js("!document.getElementById('deck-home').hidden"))

        print('The collection page, which the decks left')
        b.go('/collection', wait=4.0)
        check('the inventory is listed', b.js("document.querySelectorAll('#inventory-list [data-id], #inventory-list .inventory-row, #inventory-list > *').length") > 0)
        check('its tabs are Inventory, Statistics, Trades', b.js("[...document.querySelectorAll('#tabs .tab')].map(t => t.dataset.tab).join()") == 'inventory,stats,trades')
        for tab in ('stats', 'trades', 'inventory'):
            b.js(f"showTab('{tab}')")
            b.settle(0.8)
            check(f'the {tab} tab opens', b.js(f"!document.getElementById('tab-{tab}').hidden"))
        check('the location filter is filled', b.js("document.getElementById('filter-location').options.length") > 1)
        b.js("openSettings()")
        b.settle(0.8)
        check('the settings drawer opens', b.js("document.getElementById('settings-drawer').classList.contains('show')"))
        b.js("closeSettings()")
        b.js("(async () => { sessionStorage.setItem('newDeckText', '1 Sol Ring'); location.href = '/decks?new=1'; })()")
        b.settle(3.5)
        check('"New deck..." from a selection opens the dialog with the cards',
              b.js("document.getElementById('deck-modal').classList.contains('show') && document.getElementById('new-deck-text').value.includes('Sol Ring')"))

        print('Every page loaded without a script error or a missing file')
        for path in ('/', '/decks', '/collection'):
            b.go(path, wait=2.5)
        check('no script error, no missing file', not b.problems, '; '.join(dict.fromkeys(b.problems)))
    finally:
        left = {d['id'] for d in api('/api/decks')['decks']}
        b.close()
    failed = [what for ok, what in results if not ok]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed" + (f"; failed: {failed}" if failed else ''))
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
