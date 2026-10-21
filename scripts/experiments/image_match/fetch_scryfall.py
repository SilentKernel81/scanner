#!/usr/bin/env python3
"""
Step 1 of the image-match experiment: Scryfall's default_cards bulk file and every printing's
card picture, cached OUTSIDE the repository (default ~/.cache/mtg-image-match/).

    ~/.cache/mtg-image-match/venv/bin/python scripts/experiments/image_match/fetch_scryfall.py [--limit N]

Scryfall's rules (https://scryfall.com/docs/api): a User-Agent and an Accept header on every
request, 50-100 ms between requests. Resumable: a picture already on disk is skipped. The
printings the user has actually scanned (truth/captures.json) and their reprints come first, so
a partly finished download is already useful.
"""
import argparse
import gzip
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

HOME = Path.home() / '.cache' / 'mtg-image-match'
HEADERS = {'User-Agent': 'mtg-scanner-experiment/0.1 (personal, noncommercial)', 'Accept': 'application/json;q=0.9,*/*;q=0.8'}
GAP = 0.1  # seconds between two requests


STOPPED = threading.Event()   # set when Scryfall keeps saying no: every worker then gives up
_gate = threading.Lock()
_next_start = [0.0]


def get(url, **kw):
    # Requests may overlap, but no two START less than GAP apart (Scryfall asks for 50-100 ms)
    with _gate:
        wait = _next_start[0] - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _next_start[0] = time.monotonic() + GAP
    for attempt in range(4):
        if STOPPED.is_set():
            raise requests.RequestException('stopped: Scryfall is refusing requests')
        r = requests.get(url, headers=HEADERS, timeout=60, **kw)
        if r.status_code in (429, 403):
            # Refused (a rate limit, perhaps a daily one): wait as long as asked and try again; if it
            # persists, stop the whole run - hammering a server that says no risks a ban
            wait = int(r.headers.get('Retry-After', 0) or 0) or 60 * (attempt + 1)
            print(f'HTTP {r.status_code} from Scryfall, waiting {wait}s', flush=True)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r
    STOPPED.set()
    print('Scryfall keeps refusing: the download stops here (run it again later, it resumes)', flush=True)
    raise requests.RequestException('refused')


def bulk(folder):
    """default_cards as a list of printings (the fields we need), cached as printings.json"""
    out = folder / 'printings.json'
    if out.exists():
        return json.loads(out.read_text())
    listing = get('https://api.scryfall.com/bulk-data/default-cards').json()
    raw = get(listing.get('jsonl_download_uri') or listing['download_uri']).content
    if raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)
    rows = []
    for card in (json.loads(raw) if raw[:1] == b'[' else (json.loads(l) for l in raw.splitlines() if l.strip())):
        uris = card.get('image_uris') or (card.get('card_faces') or [{}])[0].get('image_uris')
        if not uris or not uris.get('normal'):
            continue
        rows.append({'id': card['id'], 'oracle_id': card.get('oracle_id') or (card.get('card_faces') or [{}])[0].get('oracle_id'),
                     'name': card['name'], 'set': card['set'], 'number': card['collector_number'], 'lang': card.get('lang'),
                     'frame': card.get('frame'), 'layout': card.get('layout'), 'border': card.get('border_color'),
                     'frame_effects': card.get('frame_effects', []), 'promo': card.get('promo'), 'finishes': card.get('finishes', []),
                     'illustration_id': card.get('illustration_id') or (card.get('card_faces') or [{}])[0].get('illustration_id'),
                     'image': uris['normal']})
    out.write_text(json.dumps(rows))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--home', default=str(HOME))
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--english-only', action='store_true', help='skip non-English printings')
    a = ap.parse_args()
    home = Path(a.home)
    images = home / 'images'
    images.mkdir(parents=True, exist_ok=True)
    rows = bulk(home)
    if a.english_only:
        rows = [r for r in rows if r['lang'] == 'en']
    truth = home / 'truth' / 'captures.json'
    mine = {c['card_id'] for c in json.loads(truth.read_text())} if truth.exists() else set()
    oracles = {r['oracle_id'] for r in rows if r['id'] in mine}
    rows.sort(key=lambda r: (r['id'] not in mine, r['oracle_id'] not in oracles))
    todo = [r for r in rows if not (images / f"{r['id']}.jpg").exists()]
    if a.limit:
        todo = todo[:a.limit]
    counts = {'done': 0, 'failed': 0}

    def one(r):
        path = images / f"{r['id']}.jpg"
        try:
            data = get(r['image']).content
        except requests.RequestException as e:
            counts['failed'] += 1
            print('failed', r['id'], e, flush=True)
            return
        path.with_suffix('.part').write_bytes(data)
        path.with_suffix('.part').rename(path)  # whole or not at all
        counts['done'] += 1
        if counts['done'] % 500 == 0:
            print(f"{counts['done']} downloaded, {counts['failed']} failed", flush=True)

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(one, todo))
    done, skipped = counts['done'], counts['failed']
    print('finished', done, skipped)


if __name__ == '__main__':
    main()
