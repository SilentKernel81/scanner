#!/usr/bin/env python3
"""
Corrects the Deadpool Secret Lair entries of the REAL collection, through the server's own web API (the route Edit card uses), after the
owner's go-ahead on 2026-10-20 ("You can fix it").

What is wrong: the owner has one regular, one foil and one confetti-foil copy of each of five cards. The collection holds, per card, the
regular copy right (IFIYW-n, quantity 1) but BOTH foil copies under the foil-only entry (IFIYW-n+5, quantity 2): the plain foil copy belongs
to IFIYW-n (foil). The pictures agree (the photo of one of the two looks like n, see deadpool_entries.py; the rescan of the three versions
separated them 15 of 15).
The fix, per card: move ONE copy of "IFIYW-(n+5), foil" to "IFIYW-n, foil" (POST /api/inventory/update/<id> with card_id and split_quantity 1:
the app moves that copy and keeps the photos with the copies).

    venv/bin/python -I deadpool_fix.py            # dry run: reads the live state, checks it is what is expected, writes NOTHING
    venv/bin/python -I deadpool_fix.py --apply    # backup first (the app's own), then the five moves, then checks the result

To undo one: move a copy of "IFIYW-n, foil" back to "IFIYW-(n+5)" with Edit card (or restore the backup named below).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import requests
from common import printings

SERVER = 'http://192.168.1.23:5000'
APPLY = '--apply' in sys.argv
P = printings()
SLD = {p['number']: p for p in P.values() if p['set'] == 'sld' and p['number'].startswith('IFIYW')}
CARDS = {'Deadly Dispute': 1, 'Lightning Bolt': 2, 'Thrill of Possibility': 3, 'Lightning Greaves': 4, 'Sol Ring': 5}
BACKUP_NOTE = 'before the Deadpool fix (image-match experiment, 2026-10-20)'


def deadpool_entries():
    cards = requests.get(f'{SERVER}/api/inventory', timeout=60).json()['cards']
    # the API's own field names: name, number, set_code, finish, quantity (not the table's card_name / card_number)
    return [c for c in cards if str(c.get('number', '')).startswith('IFIYW') and (c.get('set_code') or '').lower() == 'sld']


def show(entries, title):
    print(title)
    for c in sorted(entries, key=lambda c: (c['name'], int(c['number'].split('-')[1]), c['finish'])):
        print(f"   id {c['id']:>5}  {c['name']:22} {c['number']:9} {c['finish']:8} x{c['quantity']}")


def find(entries, name, number, finish):
    return [c for c in entries if c['name'] == name and c['number'] == number and c['finish'] == finish]


entries = deadpool_entries()
show(entries, f'The collection now ({len(entries)} Deadpool entries):')

# the state the fix is made for: per card, n regular x1, n+5 foil x2, no n foil
plan = []
for name, n in CARDS.items():
    regular, foil_only, plain_foil = (find(entries, name, f'IFIYW-{n}', 'regular'), find(entries, name, f'IFIYW-{n + 5}', 'foil'),
                                      find(entries, name, f'IFIYW-{n}', 'foil'))
    if len(regular) == 1 and regular[0]['quantity'] == 1 and len(foil_only) == 1 and foil_only[0]['quantity'] == 2 and not plain_foil:
        plan.append((name, foil_only[0]['id'], SLD[f'IFIYW-{n}']['id'], f'IFIYW-{n + 5}', f'IFIYW-{n}'))
    else:
        print(f'\nNOT what the fix expects for {name}: regular {len(regular)}, foil-only {[(c["quantity"]) for c in foil_only]}, plain foil {len(plain_foil)}'
              f' - nothing will be changed for it')
print('\nPlanned moves (one copy each):')
for name, entry_id, target, source_number, target_number in plan:
    print(f'   {name:22} entry {entry_id}: one copy of {source_number} foil -> {target_number} foil')

if not APPLY:
    print('\nDry run: nothing was written. Run with --apply to make the backup and the moves.')
    sys.exit(0)
if not plan:
    sys.exit('Nothing to do.')

backup = requests.post(f'{SERVER}/api/backups', json={'note': BACKUP_NOTE}, timeout=300).json()
if not backup.get('success'):
    sys.exit(f'The backup failed, nothing was changed: {backup}')
print(f"\nBackup made: {backup['backup']}")
for name, entry_id, target, source_number, target_number in plan:
    answer = requests.post(f'{SERVER}/api/inventory/update/{entry_id}', json={'card_id': target, 'split_quantity': 1}, timeout=60).json()
    print(f"   {name}: {answer.get('message') or answer}")
    if not answer.get('success'):
        sys.exit(f'Stopped at {name}: {answer}. The rest were not changed; the backup above holds the state before.')

after = deadpool_entries()
show(after, '\nThe collection after:')
ok = all(len(find(after, name, f'IFIYW-{n}', 'regular')) == 1 and find(after, name, f'IFIYW-{n}', 'regular')[0]['quantity'] == 1
         and len(find(after, name, f'IFIYW-{n}', 'foil')) == 1 and find(after, name, f'IFIYW-{n}', 'foil')[0]['quantity'] == 1
         and len(find(after, name, f'IFIYW-{n + 5}', 'foil')) == 1 and find(after, name, f'IFIYW-{n + 5}', 'foil')[0]['quantity'] == 1
         for name, n in CARDS.items())
print('\nEvery card now has regular x1, foil x1, confetti foil x1:', 'YES' if ok else 'NO - check the list above')
