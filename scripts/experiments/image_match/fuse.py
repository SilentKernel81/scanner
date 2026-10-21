#!/usr/bin/env python3
"""
Do the colour hash and HOG complement each other? Joins their embeddings into ONE vector whose dot product is the weighted sum of
the two similarities:   w * (hash similarity) + (1 - w) * (HOG cosine)
(the hash bits as +-1 divided by sqrt(bits), HOG as it is, each scaled by sqrt(weight)). Writes out/fused_w<percent>_card/ in
the layout evaluate.py reads, so evaluate.py and strip.py work on it unchanged.

    venv/bin/python -I fuse.py 50 70      (the weights of the hash, in percent)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
from common import OUT

HASH, HOG = 'phash_rgb_card', 'hog_card'


def bits(v):
    """+-1 floats from packed hash bits, scaled so that a dot product is the share of agreeing bits (in [-1, 1])"""
    return (np.unpackbits(v, axis=1).astype(np.float32) * 2 - 1) / np.sqrt(v.shape[1] * 8)


def main():
    hash_g, hog_g = np.load(OUT / HASH / 'gallery.npz'), np.load(OUT / HOG / 'gallery.npz')
    hash_e, hog_e = np.load(OUT / HASH / 'eval.npz'), np.load(OUT / HOG / 'eval.npz')
    ids_hash, ids_hog = list(hash_g['ids']), list(hog_g['ids'])
    common = [i for i in ids_hash if i in set(ids_hog)]
    row_hash = {i: n for n, i in enumerate(ids_hash)}
    row_hog = {i: n for n, i in enumerate(ids_hog)}
    gh, gg = hash_g['vectors'][[row_hash[i] for i in common]], hog_g['vectors'][[row_hog[i] for i in common]]
    ok = hash_e['ok'] & hog_e['ok']
    for percent in map(int, sys.argv[1:]):
        w = percent / 100
        name = f'fused_w{percent}_card'
        (OUT / name).mkdir(exist_ok=True)
        gallery = np.concatenate([np.sqrt(w) * bits(gh), np.sqrt(1 - w) * gg], axis=1).astype(np.float32)
        queries = np.concatenate([np.sqrt(w) * bits(hash_e['vectors']), np.sqrt(1 - w) * hog_e['vectors']], axis=1).astype(np.float32)
        np.savez(OUT / name / 'gallery.npz', ids=np.array(common), vectors=gallery)
        np.savez(OUT / name / 'eval.npz', vectors=queries, ok=ok)
        print('written', name, gallery.shape)


if __name__ == '__main__':
    main()
