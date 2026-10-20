#!/usr/bin/env python3
"""
Step 4: how often does the nearest Scryfall picture have the right printing?

    venv/bin/python -I evaluate.py <embedding folder name, e.g. mnv3_card> [--require-full-gallery]

For every evaluation picture (out/eval.json) the gallery is searched by cosine similarity (Hamming for
phash) and the top-1 is put in one of four classes:
    correct         the exact printing
    same_art        another printing of the same card with the same illustration (indistinguishable by
                    image; the collector number / set code line has to decide)
    other_art       the same card, a different illustration (a different frame / showcase / borderless: an
                    image should tell these apart, so these are real misses)
    wrong_card      a different card
Reported per kind (collection / scanned / frame), and for scanned by reader, finish and station.
'oracle' is the accuracy of the card ignoring printing (correct + same_art + other_art).
"top-5 candidate recall" is how often the right printing is among the five nearest.
"""
import argparse
import collections
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
from common import OUT, printings


def load(name):
    g = np.load(OUT / name / 'gallery.npz')
    e = np.load(OUT / name / 'eval.npz')
    return list(g['ids']), g['vectors'], e['vectors'], e['ok']


def as_float(v):
    """phash bytes -> +-1 floats (dot product = 256 - 2 x Hamming distance); real vectors as they are"""
    if v.dtype == np.uint8:
        return np.unpackbits(v, axis=1).astype(np.float32) * 2 - 1
    return v.astype(np.float32)


def search(gallery, queries, k=5, chunk=256):
    """Top k gallery rows for each query: brute force, matrix product in chunks"""
    top, sims = [], []
    for s in range(0, len(queries), chunk):
        sim = queries[s:s + chunk] @ gallery.T
        part = np.argpartition(-sim, k, axis=1)[:, :k]
        order = np.argsort(-np.take_along_axis(sim, part, 1), axis=1)
        rows = np.take_along_axis(part, order, 1)
        top.append(rows)
        sims.append(np.take_along_axis(sim, rows, 1))
    return np.concatenate(top), np.concatenate(sims)


def classify(truth, got, P):
    if got == truth:
        return 'correct'
    t, g = P[truth], P[got]
    if t['oracle_id'] != g['oracle_id']:
        return 'wrong_card'
    return 'same_art' if t['illustration_id'] and t['illustration_id'] == g['illustration_id'] else 'other_art'


def report(rows, title):
    n = len(rows)
    if not n:
        return None
    c = collections.Counter(r['class'] for r in rows)
    pct = lambda x: round(100 * x / n, 1)
    return {'n': n, 'top1': pct(c['correct']), 'top5': pct(sum(r['in_top5'] for r in rows)),
            'oracle': pct(c['correct'] + c['same_art'] + c['other_art']), 'oracle_top5': pct(sum(r['oracle_in_top5'] for r in rows)),
            'same_art': pct(c['same_art']), 'other_art': pct(c['other_art']), 'wrong_card': pct(c['wrong_card'])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('name')
    ap.add_argument('--require-full-gallery', action='store_true')
    a = ap.parse_args()
    P = printings()
    ids, gallery, queries, ok = load(a.name)
    items = json.loads((OUT / 'eval.json').read_text())
    if a.require_full_gallery and len(ids) < 112000:
        raise SystemExit(f'the gallery has only {len(ids)} printings: wait for the download')
    have = set(ids)
    gallery, queries = as_float(gallery), as_float(queries)
    t0 = time.time()
    top, sims = search(gallery, queries)
    if np.abs(gallery).min() == 1:    # a hash: +-1 vectors, scale the dot product (bits - 2 x Hamming) to [-1, 1]
        sims = sims / gallery.shape[1]
    per_query_ms = (time.time() - t0) * 1000 / len(queries)
    rows = []
    for it, q, sim, good in zip(items, top, sims, ok):
        if not good or it['truth'] not in have:     # unreadable picture / printing not downloaded yet
            continue
        got = [ids[j] for j in q]
        oracle = P[it['truth']]['oracle_id']
        rows.append({**{k: it.get(k) for k in ('kind', 'finish', 'station', 'reader', 'path')}, 'truth': it['truth'], 'got': got[0], 'top5': got,
                     'sims': [round(float(x), 4) for x in sim], 'class': classify(it['truth'], got[0], P), 'in_top5': it['truth'] in got,
                     'oracle_in_top5': any(P[g]['oracle_id'] == oracle for g in got)})
    result = {'gallery': len(ids), 'evaluated': len(rows), 'skipped': len(items) - len(rows), 'brute_force_ms_per_query': round(per_query_ms, 2),
              'by_kind': {k: report([r for r in rows if r['kind'] == k], k) for k in ('collection', 'scanned', 'frame')}}
    sc = [r for r in rows if r['kind'] in ('scanned', 'collection')]
    result['by_finish'] = {f: report([r for r in sc if r['finish'] == f], f) for f in ('regular', 'foil')}
    result['by_reader'] = {k: report([r for r in rows if r['kind'] == 'scanned' and r['reader'] == k], k)
                           for k in sorted({r['reader'] for r in rows if r['kind'] == 'scanned'})}
    result['by_station'] = {k: report([r for r in rows if r['kind'] == 'scanned' and r['station'] == k], k)
                            for k in sorted({r['station'] for r in rows if r['kind'] == 'scanned' and r['station']})}
    held_file = OUT / 'ft_heldout.json'
    if held_file.exists():
        # the fine-tune's held-out cards (the Spider-Man set + 5% of the visual classes, never trained on): scored for EVERY embedding,
        # so the hash is judged on the same split
        held = set(json.loads(held_file.read_text()))
        result['by_heldout'] = {'unseen by the fine-tune': report([r for r in rows if r['truth'] in held], 'unseen'),
                                'seen by the fine-tune': report([r for r in rows if r['truth'] not in held], 'seen')}
    (OUT / a.name / 'result.json').write_text(json.dumps(result, indent=1))
    (OUT / a.name / 'rows.json').write_text(json.dumps(rows))
    print(json.dumps(result, indent=1))


if __name__ == '__main__':
    main()
