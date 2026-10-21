#!/usr/bin/env python3
"""Prints the experiment's results (out/*/result.json, index_bench.json, strip.json) as compact tables"""
import json
from pathlib import Path

OUT = Path.home() / '.cache' / 'mtg-image-match' / 'out'
NAMES = ['fused_w50_card', 'fused_w70_card', 'phash_rgb_card', 'phash_card', 'phash32_card', 'hog_s_card', 'hog_card', 'phash_art', 'phash_card_raw', 'mnv3_card', 'mnv3_art', 'mnv3_card_raw', 'dino_card']


def load(name, file):
    path = OUT / name / file
    return json.loads(path.read_text()) if path.exists() else None


print('== accuracy of the nearest Scryfall picture (percent) ==')
print(f"{'embedding':15} {'gallery':>7} {'kind':10} {'n':>5} {'top1':>5} {'top5':>5} {'oracle':>6} {'same_art':>8} {'other_art':>9} {'wrong':>5}")
for name in NAMES:
    r = load(name, 'result.json')
    if not r:
        continue
    for kind, v in r['by_kind'].items():
        if v:
            print(f"{name:15} {r['gallery']:>7} {kind:10} {v['n']:>5} {v['top1']:>5} {v['top5']:>5} {v['oracle']:>6} {v['same_art']:>8} {v['other_art']:>9} {v['wrong_card']:>5}")
    print(f"{'':15} brute force {r['brute_force_ms_per_query']} ms/query; skipped {r['skipped']}")

for name in ('mnv3_card', 'dino_card'):
    r = load(name, 'result.json')
    print(f'\n== {name}: by finish / reader / station (collection+scanned, percent) ==')
    for group in ('by_finish', 'by_reader', 'by_station'):
        for k, v in r[group].items():
            if v:
                print(f"{group:11} {k:24} n={v['n']:>5} top1={v['top1']:>5} top5={v['top5']:>5} oracle={v['oracle']:>5} same_art={v['same_art']:>4} other_art={v['other_art']:>4} wrong={v['wrong_card']:>4}")

for name in ('mnv3_card', 'dino_card'):
    b = load(name, 'index_bench.json')
    print(f'\n== {name}: index ==')
    print({k: v for k, v in b.items() if k != 'hnsw'})
    for k, v in b['hnsw'].items():
        print(' ', k, v)

for name in ('mnv3_card', 'dino_card'):
    s = load(name, 'strip.json')
    print(f'\n== {name}: strip reading + no-AI ==')
    print(json.dumps(s, indent=1))
