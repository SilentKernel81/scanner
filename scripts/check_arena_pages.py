#!/usr/bin/env python3
"""
The digital collection (MTG Arena) on the pages, tried in a headless browser.

Run:  venv/bin/python scripts/check_arena_pages.py http://localhost:5001 <folder with the exporter's files>

ONLY against a server with copied data: it imports the export (which replaces the digital
collection and the decks Arena brought before), and it creates and deletes a deck. The
paper collection is only read - and compared before and after, which is the point: nothing
done in Arena view may reach it.

Like scripts/check_deck_pages.py (whose browser it uses): headless Chromium has no hovering
pointer, so what only shows on pointing is not looked at.
"""
import json
import sys
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_deck_pages as pages   # noqa: E402  (reads the server's address from sys.argv[1] itself)

BASE, EXPORT = sys.argv[1].rstrip('/'), Path(sys.argv[2])
api, check = pages.api, pages.check


def send(path, method='POST', body=None):
    request = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json'})
    try:
        return json.load(urllib.request.urlopen(request, timeout=60))
    except urllib.error.HTTPError as e:
        return json.load(e)


def import_export(names):
    boundary, body = uuid.uuid4().hex, b''
    for name in names:
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
                 'Content-Type: application/json\r\n\r\n').encode() + (EXPORT / name).read_bytes() + b'\r\n'
    body += f'--{boundary}--\r\n'.encode()
    request = urllib.request.Request(BASE + '/api/arena/import', data=body,
                                     headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    return json.load(urllib.request.urlopen(request, timeout=300))


def paper():
    """The paper collection as the server lists it: what must be the same at the end"""
    cards = api('/api/inventory')['cards']
    return sorted((c['id'], c['name'], c['quantity'], c['location'], c['tags']) for c in cards)


def main():
    paper_before = paper()
    imported = import_export(['mtga_collection.json', 'mtga_decks.json'])
    arena = imported['arena']
    print(f"Imported: {arena['cards']} cards in {arena['entries']} entries, {imported['decks']} decks")
    b = pages.Browser()
    made = None
    try:
        print('The collection page')
        b.go('/collection')
        b.js("localStorage.clear()")
        b.go('/collection')
        check('the Physical | Arena switch is shown, on Physical', b.js("!document.getElementById('collection-kind').hidden && document.querySelector('#collection-kind .is-active').dataset.collection === 'paper'"))
        check('Physical: no Arena bar, rows can be ticked and edited',
              b.js("document.getElementById('arena-bar').hidden && getComputedStyle(document.querySelector('#inventory-list .row-check')).display !== 'none' && getComputedStyle(document.querySelector('#inventory-list .btn-edit')).display !== 'none'"))
        paper_summary = b.js("document.getElementById('inventory-summary').textContent")
        b.js("document.querySelector('#collection-kind [data-collection=arena]').click()")
        b.settle(4)
        check('Arena: the list is the digital collection', b.js("inventory.length") == arena['entries'] and b.js("totalQuantity(inventory)") == arena['cards'],
              f"{b.js('inventory.length')} entries")
        check('Arena: the bar says when it is from and the wildcards', b.js("(t => !document.getElementById('arena-bar').hidden && /wildcards: 18 common, 30 uncommon, 1 rare, 0 mythic/.test(t) && /3061 cards/.test(t))(document.getElementById('arena-text').textContent)"),
              b.js("document.getElementById('arena-text').textContent"))
        # A price filter set for the paper cards (it is remembered) must not empty the Arena list,
        # where the fields are hidden and every card's price is 0
        b.js("(() => { const f = document.getElementById('filter-price-min'); f.value = '5'; f.dispatchEvent(new Event('input', {bubbles: true})); f.dispatchEvent(new Event('change', {bubbles: true})); filtersChanged(); })()")
        b.settle(0.5)
        check('Arena: a price filter of the paper view hides nothing and shows no chip',
              b.js("shown.length") == arena['entries'] and b.js("!/Price/.test(document.getElementById('filter-chips').textContent)"), f"{b.js('shown.length')} shown")
        b.js("document.querySelector('#collection-kind [data-collection=paper]').click()")
        b.settle(3)
        check('... and filters the paper list again', b.js("shown.length < inventory.length && shown.every(card => card.price >= 5) && /Price/.test(document.getElementById('filter-chips').textContent)"))
        b.js("(() => { document.getElementById('filter-price-min').value = ''; filtersChanged(); })()")
        b.js("document.querySelector('#collection-kind [data-collection=arena]').click()")
        b.settle(4)
        hidden = b.js("""['#inventory-list .row-check', '#inventory-list .btn-edit', '#inventory-list .btn-delete', '#inventory-list .btn-trade',
                         '#select-all-label', '#import-button', '#filter-location', '#filter-tag', '#filter-spare-label', '.tab[data-tab=trades]']
                         .filter(s => getComputedStyle(document.querySelector(s)).display !== 'none')""")
        check('Arena: nothing to tick, edit, delete, trade or import as paper', hidden == [], str(hidden))
        # Everything a person could still reach: the handlers themselves, called as the page calls them
        b.js("""(() => { const card = inventory[0];
                 toggleSelected(card.id, true); pick(inventory[1].id, true, true);
                 changeLater([card.id], 'x', {ids: [card.id], action: 'delete'});
                 setAsideForTrade(card);
                 document.body.dispatchEvent(new KeyboardEvent('keydown', {key: 'a', ctrlKey: true, bubbles: true}));
                 document.body.dispatchEvent(new KeyboardEvent('keydown', {key: 'Delete', bubbles: true})); })()""")
        b.settle(1)
        check('Arena: ticking, deleting and trading do nothing', b.js("selected.size === 0 && pendingChange === null && document.getElementById('bulk-bar').hidden && !document.querySelector('.modal.show')"))
        refused = b.js("fetch('/api/inventory/bulk' + areaQuery(), {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ids: [inventory[0].id], action: 'delete'})}).then(r => r.status)")
        check('Arena: a change sent anyway is refused by the server', refused == 400, str(refused))
        check('Arena: the list is still whole', api('/api/arena')['arena']['cards'] == arena['cards'])
        b.js("document.querySelector('.tab[data-tab=stats]').click()")
        b.settle(1)
        check('Arena: the statistics count the digital collection', b.js("document.querySelector('#stats-tiles .inventory-stat-value').textContent") == f"{arena['cards']:,}")
        b.js("document.querySelector('.tab[data-tab=inventory]').click()")
        b.go('/collection')
        check('the page comes back in Arena view', b.js("inArena() && document.body.classList.contains('arena-view')") and b.js("inventory.length") == arena['entries'])
        b.js("document.querySelector('#collection-kind [data-collection=paper]').click()")
        b.settle(3)
        check('back on Physical: the same list as before, editable again',
              b.js("document.getElementById('inventory-summary').textContent") == paper_summary and b.js("!document.body.classList.contains('arena-view')"))

        print('The deck list')
        decks = api('/api/decks')['decks']
        digital = [d for d in decks if d['kind'] == 'arena']
        b.go('/decks')
        check('every deck has a tile, Arena decks a badge',
              b.js("document.querySelectorAll('#deck-list [data-deck]').length") == len(decks)
              and b.js("document.querySelectorAll('#deck-list .deck-kind-badge').length") == len(digital))
        check('the kind switch is shown', b.js("!document.getElementById('deck-kind').hidden"))
        for kind, expected in (('arena', len(digital)), ('paper', len(decks) - len(digital)), ('all', len(decks))):
            b.js(f"document.querySelector('#deck-kind [data-kind={kind}]').click()")
            b.settle(0.5)
            shown = b.js("document.querySelectorAll('#deck-list [data-deck]').length")
            check(f'{kind}: {expected} decks', shown == expected, str(shown))

        print('An Arena deck')
        deck = max(digital, key=lambda d: d['cards'])
        b.go(f"/decks/{deck['id']}")
        totals = b.js("document.getElementById('deck-totals').textContent.replace(/\\s+/g, ' ')")
        check('its totals say Arena, and no money', 'Arena' in totals and 'in Arena' in totals and '$' not in totals, totals)
        check('owned marks come from the Arena collection', b.js("deck.kind === 'arena' && deck.cards.every(c => c.owned >= 1)"))
        # A card nobody has four of in Arena... any card not in the digital collection: found by the search
        b.js("showAdding(true)")
        b.js("(() => { const f = document.getElementById('search-text'); f.value = 'Black Lotus'; f.dispatchEvent(new Event('input')); })()")
        b.settle(2.5)
        check('the card search of an Arena deck asks about the Arena collection', b.js("performance.getEntriesByType('resource').some(e => /api\\/cards\\/search.*kind=arena/.test(e.name))"))

        print('A new Arena deck, made here')
        b.go('/decks')
        b.js("document.querySelector('#deck-kind [data-kind=arena]').click()")
        b.js("openDeckModal()")
        check('New deck offers the kind, starting on the one shown', b.js("!document.getElementById('deck-modal-kind-group').hidden && document.querySelector('#new-deck-kind .is-active').dataset.kind === 'arena'"))
        b.js("(() => { document.getElementById('new-deck-name').value = 'Arena page check'; document.getElementById('new-deck-format').value = 'historic'; document.getElementById('new-deck-text').value = '4 Black Lotus\\n4 Sheoldred, the Apocalypse\\n4 Llanowar Elves\\n10 Forest'; })()")
        b.js("saveDeckModal()")
        b.settle(3)
        made = b.js("deck && deck.id")
        check('it is an Arena deck, not one of Arena\'s', b.js("deck.kind === 'arena' && deck.from_arena === false && deck.format === 'historic'"))
        check('basic lands count as owned, a card not in Arena as missing',
              b.js("deck.cards.find(c => c.name === 'Forest').owned >= 10 && deck.cards.find(c => c.name === 'Black Lotus').owned === 0"))
        # Sheoldred is on Arena (a mythic): to craft. Black Lotus is not: a problem, and no wildcard's business
        check('what is missing and on Arena is counted in wildcards', b.js("/to craft: \\d mythic/.test(document.getElementById('deck-totals').textContent.replace(/\\s+/g, ' '))"),
              b.js("document.getElementById('deck-totals').textContent.replace(/\\s+/g, ' ')"))
        check('a card Arena does not have is a problem of the deck', b.js("deck.issues.some(i => i.message === 'Not on Arena' && i.cards.includes('Black Lotus') && !i.cards.includes('Sheoldred, the Apocalypse'))"))
        again = import_export(['mtga_collection.json', 'mtga_decks.json'])
        after = api('/api/decks')['decks']
        check('another import replaces Arena\'s decks and leaves this one',
              again['decks'] == len(digital) and any(d['id'] == made for d in after) and len(after) == len(decks) + 1)

        print('Can a paper deck be built in Arena?')
        physical = max((d for d in decks if d['kind'] == 'paper'), key=lambda d: d['cards'])
        b.go(f"/decks/{physical['id']}")
        check('a paper deck has no Arena badge and counts money', b.js("!document.querySelector('#deck-totals .deck-kind-badge') && /\\$/.test(document.getElementById('deck-totals').textContent)"))
        b.js("openArena()")
        b.settle(2)
        text = b.js("document.getElementById('arena-check').textContent.replace(/\\s+/g, ' ')")
        check('the Arena window answers it', b.js("!document.getElementById('arena-check').hidden") and 'Can you build it in Arena?' in text and 'in your Arena collection' in text, text[:200])
        answer = api(f"/api/decks/{physical['id']}/arena")
        counted = answer['check']['have'] + sum(c['copies'] for c in answer['check']['craft'])
        check('every copy on Arena is either had or to craft', counted == answer['cards'], f"{counted} of {answer['cards']}")
        print('   ', text[:260])

        check('no script error, no missing file', not b.problems, '; '.join(b.problems[:4]))
    finally:
        b.close()
        if made:
            send(f'/api/decks/{made}', 'DELETE')
    check('the paper collection is as it was', paper() == paper_before)
    failed = [what for ok, what in pages.results if not ok]
    print(f"\n{len(pages.results) - len(failed)} of {len(pages.results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
