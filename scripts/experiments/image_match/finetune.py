#!/usr/bin/env python3
"""
Fine-tunes the embedding network (ftmodel.FtNet) on the Scryfall pictures alone, with augmentation that imitates the cameras. ONE run,
no sweep (the pass/fail bar is written in docs/plans/IMAGE_MATCH_EXPERIMENT.md before the run). Runs on the GPU:

    ~/.cache/mtg-image-match/venv-gpu/bin/python -I finetune.py [--epochs 8] [--steps N]      (--steps: a short smoke test)

What is learned
  A class is one VISUAL printing: the same illustration + frame + border + frame effects + language. Printings that look the same (a reprint
  with the same art and frame) are one class, because no image can tell them apart (the collector number line does that). Each step takes
  ~96 classes, one printing of each; the two views of a class are a clean picture (what the gallery holds) and a degraded one
  (what a camera makes of it: dark, tinted, blurred, glared, shifted, sleeved, recompressed). Symmetric InfoNCE: a degraded picture must be
  nearest to its own clean one among the other classes of the batch. Batches are filled with whole groups of one card's different
  versions (borderless, showcase, other art), so telling those apart is practised.
Held out of training (their whole visual classes, so a reprint of the same art cannot leak in): every printing of the Spider-Man set `spm`
and a random 5% of the classes - the "new set" test. The gallery used for evaluation still contains them all.
Writes out/ft.pt (every epoch), out/ft_heldout.json (the held-out printing ids), out/finetune.log.
"""
import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cv2
import numpy as np
import torch
import torch.nn.functional as F
from common import IMAGES, OUT, gallery_ids, prepare, printings, read
from ftmodel import FtNet

MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)
BATCH_CLASSES = 48        # ~96 pictures a step; 96 classes ran out of the laptop GPU's memory (the desktop already holds 1.8 GB of 6)
TEMPERATURE = 0.07
PRE = (280, 390)          # the Scryfall picture is brought to this size first (augmenting at full size is slow)


def visual_key(p):
    """What makes two printings LOOK different: art, frame, border, frame effects, language"""
    return (p['illustration_id'] or p['oracle_id'], p['frame'], p['border'], tuple(sorted(p['frame_effects'] or [])), p['lang'])


def augment(rgb, rng):
    """A card picture as one of the cameras might have made it"""
    h, w = rgb.shape[:2]
    # the card a little smaller / larger / tilted inside the frame, a sleeve's grey or the box's black around it
    s = rng.uniform(0.94, 1.04)
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = src * s + (1 - s) / 2 * np.float32([w, h]) + rng.uniform(-0.025, 0.025, (4, 2)).astype(np.float32) * np.float32([w, h])
    border = int(rng.choice([0, 0, 50, 110]))
    out = cv2.warpPerspective(rgb, cv2.getPerspectiveTransform(src, dst.astype(np.float32)), (w, h),
                              borderMode=cv2.BORDER_CONSTANT, borderValue=(border,) * 3)
    f = out.astype(np.float32) / 255
    f = f ** rng.uniform(0.8, 1.9)                                          # darker (the cameras are dim)
    f = (f - 0.5) * rng.uniform(0.55, 1.05) + 0.5 + rng.uniform(-0.1, 0.06)  # flatter
    f = f * rng.uniform(0.85, 1.1, 3)                                         # a colour cast
    grey = f.mean(2, keepdims=True)
    f = grey + (f - grey) * rng.uniform(0.5, 1.1)                             # less saturated
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    if rng.random() < 0.5:                                                   # glare: a soft bright blob, sometimes a band
        cx, cy = rng.uniform(0, w), rng.uniform(0, h)
        sx, sy = rng.uniform(0.08, 0.5) * w, rng.uniform(0.05, 0.4) * h
        mask = np.exp(-(((xx - cx) / sx) ** 2 + ((yy - cy) / sy) ** 2)) * rng.uniform(0.1, 0.5)
        f = f * (1 - mask[..., None]) + mask[..., None]
    if rng.random() < 0.2:                                                   # a foil's rainbow sheen
        phase = rng.uniform(0, 6.3, 3)
        wave = np.stack([np.sin((xx + yy) / rng.uniform(40, 120) + p) for p in phase], 2) * rng.uniform(0.03, 0.12)
        f = f + wave
    if rng.random() < 0.6:
        f = cv2.GaussianBlur(f, (0, 0), rng.uniform(0.5, 2.5))
    f = f + rng.normal(0, rng.uniform(0, 0.03), f.shape).astype(np.float32)
    out = (np.clip(f, 0, 1) * 255).astype(np.uint8)
    if rng.random() < 0.5:
        ok, enc = cv2.imencode('.jpg', out, [cv2.IMWRITE_JPEG_QUALITY, int(rng.integers(45, 95))])
        if ok:
            out = cv2.imdecode(enc, cv2.IMREAD_COLOR)
    return out


def to_tensor(rgb):
    """prepare() is what the lookup does to every picture (edge cut, 224 x 312, per-channel stretch)"""
    x = (prepare(rgb, 'card', True).astype(np.float32) / 255 - MEAN) / STD
    return torch.from_numpy(x.transpose(2, 0, 1).copy())


class Classes(torch.utils.data.Dataset):
    """One item = one visual class; two views of one of its printings: (clean or lightly degraded, degraded)"""

    def __init__(self, classes):
        self.classes = classes            # list of lists of printing ids

    def __len__(self):
        return len(self.classes)

    def __getitem__(self, i):
        rng = np.random.default_rng(torch.initial_seed() % 2 ** 32 + random.randrange(2 ** 31))
        pid = random.choice(self.classes[i])
        rgb = read(IMAGES / f'{pid}.jpg')
        rgb = cv2.resize(rgb, PRE, interpolation=cv2.INTER_AREA)
        first = rgb if rng.random() < 0.7 else augment(rgb, rng)
        return to_tensor(first), to_tensor(augment(rgb, rng))


def batches(oracle_groups, rng):
    """Batches of ~BATCH_CLASSES classes made of whole groups (all the visual versions of a card, up to 6)"""
    order = list(oracle_groups)
    rng.shuffle(order)
    batch = []
    for group in order:
        batch.extend(rng.sample(group, min(len(group), 6)))
        if len(batch) >= BATCH_CLASSES:
            yield batch
            batch = []


class BatchSampler:
    def __init__(self, oracle_groups, seed):
        self.groups, self.rng = oracle_groups, random.Random(seed)

    def __iter__(self):
        return batches(self.groups, self.rng)

    def __len__(self):
        return sum(len(g) for g in self.groups) // BATCH_CLASSES


def quick_validation(model, device, held, train_ids, P):
    """Early signal: degraded pictures of 400 held-out printings against a gallery of 3,000 held-out + 3,000 training printings"""
    rng = np.random.default_rng(1)
    held = sorted(held)
    gallery_ids_ = held[:3000] + sorted(train_ids)[:3000]
    queries = [held[i] for i in rng.choice(len(held), min(400, len(held)), replace=False)]

    def embed(rgbs):
        out = []
        with torch.no_grad(), torch.autocast('cuda', dtype=torch.float16):
            for s in range(0, len(rgbs), 100):
                out.append(F.normalize(model(torch.stack(rgbs[s:s + 100]).to(device)).float(), dim=1).cpu())
        return torch.cat(out)
    model.eval()
    g = embed([to_tensor(cv2.resize(read(IMAGES / f'{i}.jpg'), PRE, interpolation=cv2.INTER_AREA)) for i in gallery_ids_])
    q = embed([to_tensor(augment(cv2.resize(read(IMAGES / f'{i}.jpg'), PRE, interpolation=cv2.INTER_AREA), rng)) for i in queries])
    model.train()
    top = (q @ g.T).argmax(1).numpy()
    same_class = [visual_key(P[gallery_ids_[t]]) == visual_key(P[qid]) for t, qid in zip(top, queries)]
    return float(np.mean(same_class))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--epochs', type=int, default=8)
    ap.add_argument('--steps', type=int, default=0, help='stop after this many steps (a smoke test)')
    a = ap.parse_args()
    torch.manual_seed(0)
    random.seed(0)
    device = 'cuda'
    P = printings()
    ids = [i for i in gallery_ids() if i in P and (IMAGES / f'{i}.jpg').exists()]
    keys = {i: visual_key(P[i]) for i in ids}
    by_key = {}
    for i in ids:
        by_key.setdefault(keys[i], []).append(i)
    all_keys = sorted(by_key, key=repr)
    held_keys = {keys[i] for i in ids if P[i]['set'] == 'spm'} | set(random.Random(0).sample(all_keys, int(0.05 * len(all_keys))))
    held = [i for k in held_keys for i in by_key[k]]
    train_keys = [k for k in all_keys if k not in held_keys]
    (OUT / 'ft_heldout.json').write_text(json.dumps(sorted(held)))
    print(f'{len(ids)} printings, {len(all_keys)} visual classes; held out {len(held_keys)} classes ({len(held)} printings, all of spm included); '
          f'training on {len(train_keys)} classes', flush=True)

    classes = [by_key[k] for k in train_keys]
    oracle_of = {}
    for n, k in enumerate(train_keys):
        oracle_of.setdefault(P[by_key[k][0]]['oracle_id'], []).append(n)
    loader = torch.utils.data.DataLoader(Classes(classes), batch_sampler=BatchSampler(list(oracle_of.values()), 0), num_workers=7,
                                         pin_memory=True, persistent_workers=True, prefetch_factor=4)

    model = FtNet().to(device).train()
    backbone = list(model.features.parameters())
    head = [p for n, p in model.named_parameters() if not n.startswith('features.')]
    steps_per_epoch = len(loader.batch_sampler)
    total = a.steps or a.epochs * steps_per_epoch
    opt = torch.optim.AdamW([{'params': backbone, 'lr': 3e-4}, {'params': head, 'lr': 1e-3}], weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, [3e-4, 1e-3], total_steps=total, pct_start=0.1)
    scaler = torch.amp.GradScaler()
    train_ids = [i for k in train_keys for i in by_key[k]]
    step, t0, running = 0, time.time(), []
    print(f'{steps_per_epoch} steps per epoch, {total} steps in all', flush=True)
    while step < total:
        for first, second in loader:
            first, second = first.to(device, non_blocking=True), second.to(device, non_blocking=True)
            with torch.autocast('cuda', dtype=torch.float16):
                za = F.normalize(model(first).float(), dim=1)
                zb = F.normalize(model(second).float(), dim=1)
            logits = za @ zb.T / TEMPERATURE
            labels = torch.arange(len(za), device=device)
            loss = (F.cross_entropy(logits, labels) + F.cross_entropy(logits.T, labels)) / 2
            opt.zero_grad(set_to_none=True)
            scaler.scale(loss).backward()
            scaler.step(opt)
            scaler.update()
            sched.step()
            step += 1
            running.append(loss.item())
            if step % 50 == 0:
                print(f'step {step}/{total}  loss {np.mean(running[-50:]):.3f}  {time.time() - t0:.0f}s', flush=True)
            if step % steps_per_epoch == 0 or step >= total:
                torch.save(model.state_dict(), OUT / 'ft.pt')
                print(f'== step {step}: saved; held-out quick check (degraded query -> same visual class in 6,000 pictures): '
                      f'{100 * quick_validation(model, device, held, train_ids, P):.1f}%', flush=True)
            if step >= total:
                break


if __name__ == '__main__':
    main()
