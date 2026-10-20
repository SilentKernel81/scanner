#!/usr/bin/env python3
"""
Step 3: brute-force cosine against HNSW (hnswlib), and what an index of 100k printings weighs.

    venv/bin/python -I index_bench.py <embedding folder name, e.g. mnv3_card>

Measured on the real gallery vectors and the real evaluation pictures (so recall is against the
brute-force answer, and so is the accuracy of each quantisation). Sizes for 100,000 printings are
computed from the vector size (they are plain arithmetic), the speeds are measured on THIS machine
(8 cores x86, not a phone) with one query at a time, as a scan would.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hnswlib
import numpy as np
from common import OUT
from evaluate import as_float, load


def timed(function, queries, repeat=1):
    t0 = time.perf_counter()
    for _ in range(repeat):
        for q in queries:
            function(q)
    return (time.perf_counter() - t0) * 1000 / (len(queries) * repeat)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('name')
    a = ap.parse_args()
    ids, gallery, queries, ok = load(a.name)
    gallery, queries = as_float(gallery), as_float(queries)[ok][:2000]
    n, d = gallery.shape
    out = {'gallery': n, 'dimensions': d}

    truth = np.argsort(-(queries @ gallery.T), axis=1)[:, :5]      # brute force fp32 = the reference

    # --- brute force, one query at a time ---
    out['brute_fp32_ms'] = round(timed(lambda q: np.argpartition(-(gallery @ q), 5)[:5], queries[:300]), 2)
    half = gallery.astype(np.float16)
    out['brute_fp16_ms'] = round(timed(lambda q: np.argpartition(-(half @ q.astype(np.float16)), 5)[:5], queries[:300]), 2)
    scale = np.abs(gallery).max()
    q8 = np.round(gallery / scale * 127).astype(np.int8)
    out['brute_int8_ms'] = round(timed(lambda q: np.argpartition(-(q8.astype(np.float32) @ q), 5)[:5], queries[:100]), 2)

    # --- quantisation: does the answer change? (top-1 and top-5 overlap with fp32) ---
    def overlap(vectors, qs):
        top = np.argsort(-(qs @ vectors.T.astype(np.float32)), axis=1)[:, :5]
        return round(100 * np.mean(top[:, 0] == truth[:, 0]), 2), round(100 * np.mean([len(set(t) & set(r)) / 5 for t, r in zip(top, truth)]), 2)
    out['fp16_top1_same_as_fp32_pct'], out['fp16_top5_overlap_pct'] = overlap(half, queries)
    out['int8_top1_same_as_fp32_pct'], out['int8_top5_overlap_pct'] = overlap(q8.astype(np.float32) * scale / 127, queries)

    # --- HNSW ---
    out['hnsw'] = {}
    for M, efc in ((16, 200), (32, 200)):
        index = hnswlib.Index(space='ip', dim=d)
        t0 = time.time()
        index.init_index(max_elements=n, ef_construction=efc, M=M)
        index.add_items(gallery, np.arange(n), num_threads=8)
        build = time.time() - t0
        path = OUT / a.name / f'hnsw_M{M}.bin'
        index.save_index(str(path))
        for ef in (32, 64, 128):
            index.set_ef(ef)
            labels = np.array([index.knn_query(q, k=5)[0][0] for q in queries])
            out['hnsw'][f'M{M}_efc{efc}_ef{ef}'] = {
                'build_s': round(build, 1), 'file_mb': round(path.stat().st_size / 1e6, 1),
                'ms_per_query': round(timed(lambda q: index.knn_query(q, k=5), queries[:500]), 3),
                'top1_same_as_brute_pct': round(100 * np.mean(labels[:, 0] == truth[:, 0]), 2),
                'top5_overlap_pct': round(100 * np.mean([len(set(l) & set(t)) / 5 for l, t in zip(labels, truth)]), 2)}

    # --- size of 100k printings ---
    out['size_100k_mb'] = {'fp32': round(1e5 * d * 4 / 1e6, 1), 'fp16': round(1e5 * d * 2 / 1e6, 1), 'int8': round(1e5 * d / 1e6, 1),
                           'ids_uuid_16_bytes': 1.6,
                           'hnsw_links_M16_approx': round(1e5 * (16 * 2 * 4 + 16 * 4) / 1e6, 1)}
    (OUT / a.name / 'index_bench.json').write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == '__main__':
    main()
