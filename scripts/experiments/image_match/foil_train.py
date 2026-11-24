#!/usr/bin/env python3
"""
Can a small network read the foil marker where the OCR could not?

    ~/.cache/mtg-image-match/venv-gpu/bin/python -I foil_train.py

Trains on out/foil_crops.npz (foil_crops.py): a 32 x 128 grey crop of the set line -> foil / not foil. The split is by CARD
(a printing is wholly in training or wholly in the test), so it is not tested on cards it has seen. Reported on the test cards:
  - all of them, and only those where the marker is NEEDED (the printing exists in both finishes),
  - only those where the OCR left the marker unread (the cases the AI's foil check handles today): the number that matters,
  - accuracy against how many it answers when it is allowed to say "not sure" (confidence threshold).
Labels are the finish stored with each picture: for the pictures whose marker the OCR read, that finish came from the marker
itself; for the unread ones from the AI's foil check or from the person - not independent, and I say so in the report.
"""
import hashlib
import json
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

OUT = Path.home() / '.cache' / 'mtg-image-match' / 'out'
device = 'cuda' if torch.cuda.is_available() else 'cpu'
torch.manual_seed(0)
np.random.seed(0)

d = np.load(OUT / 'foil_crops.npz')
crops, label, truth, kind, read_marker, both = (d[k] for k in ('crops', 'label', 'truth', 'kind', 'read_marker', 'both'))
bucket = np.array([int(hashlib.md5(t.encode()).hexdigest(), 16) % 5 for t in truth])
test = bucket == 0          # whole cards held out: what is reported below (and what SURE is chosen from) - never used to train or to pick a checkpoint
val = bucket == 1           # whole cards held out of training to pick the best checkpoint by; picking it on `test` would make the test numbers optimistic
print(f'{len(crops)} crops; train {int((~test & ~val).sum())}, validation {int(val.sum())}, test {int(test.sum())}; foil share {label.mean():.2f}')


def net():
    def block(i, o):
        return nn.Sequential(nn.Conv2d(i, o, 3, padding=1, bias=False), nn.BatchNorm2d(o), nn.ReLU(inplace=True), nn.MaxPool2d(2))
    return nn.Sequential(block(1, 16), block(16, 32), block(32, 64), block(64, 96), nn.AdaptiveAvgPool2d(1), nn.Flatten(), nn.Dropout(0.3), nn.Linear(96, 2))


def _affine_jitter(x):
    """Per-sample shift (±3 px), rotation (±2.5°), and scale (0.92-1.08x) with border padding (no wrap-around)."""
    n, _, h, w = x.shape
    angles = torch.empty(n).uniform_(-2.5, 2.5) * (np.pi / 180.0)
    scales = torch.empty(n).uniform_(0.92, 1.08)
    tx = torch.empty(n).uniform_(-3.0, 3.0) * (2.0 / w)
    ty = torch.empty(n).uniform_(-3.0, 3.0) * (2.0 / h)
    c, s = torch.cos(angles) / scales, torch.sin(angles) / scales
    theta = torch.zeros(n, 2, 3, dtype=x.dtype)
    theta[:, 0, 0] = c
    theta[:, 0, 1] = -s * (h / w)
    theta[:, 0, 2] = tx
    theta[:, 1, 0] = s * (w / h)
    theta[:, 1, 1] = c
    theta[:, 1, 2] = ty
    grid = F.affine_grid(theta, x.size(), align_corners=False)
    return F.grid_sample(x, grid, mode='bilinear', padding_mode='border', align_corners=False)


def _glare_speckles(x):
    """Synthetic sleeve glare / confetti-foil bright spots on ~35% of crops."""
    n, _, h, w = x.shape
    ys = torch.arange(h, dtype=x.dtype).view(1, h, 1)
    xs = torch.arange(w, dtype=x.dtype).view(1, 1, w)
    out = x.clone()
    for i in range(n):
        if np.random.rand() >= 0.35:
            continue
        for _ in range(np.random.randint(1, 4)):
            cx = float(np.random.uniform(0, w))
            cy = float(np.random.uniform(0, h))
            sx = float(np.random.uniform(1.5, 4.5))
            sy = float(np.random.uniform(1.0, 3.0))
            amp = float(np.random.uniform(-0.2, 0.35))
            blob = torch.exp(-(((xs - cx) / sx) ** 2 + ((ys - cy) / sy) ** 2) * 0.5)
            out[i] = (out[i] + amp * blob).clamp(0.0, 1.0)
    return out


def batch(x, train):
    x = torch.from_numpy(x).float().div(255).unsqueeze(1)
    if train:       # what a camera does to the line: affine offset/tilt, contrast/gamma, glare speckles, blur, noise
        n = len(x)
        x = _affine_jitter(x)
        gamma = torch.empty(n, 1, 1, 1).uniform_(0.75, 1.35)
        x = x.clamp(1e-4, 1.0).pow(gamma)
        x = (x - 0.5) * torch.empty(n, 1, 1, 1).uniform_(0.55, 1.45) + 0.5 + torch.empty(n, 1, 1, 1).uniform_(-0.2, 0.2)
        x = x.clamp(0.0, 1.0)
        x = _glare_speckles(x)
        if np.random.rand() < 0.5:
            x = F.avg_pool2d(F.pad(x, (1, 1, 1, 1), mode='replicate'), 3, 1)
        x = x + torch.randn_like(x) * 0.03
    return (x - x.mean((2, 3), keepdim=True)) / (x.std((2, 3), keepdim=True) + 1e-3)     # per-crop contrast normalisation


model = net().to(device)
opt = torch.optim.AdamW(model.parameters(), 2e-3, weight_decay=1e-3)
tr = np.where(~test & ~val)[0]
va = np.where(val)[0]
epochs = 40
sched = torch.optim.lr_scheduler.OneCycleLR(opt, 4e-3, total_steps=epochs * (len(tr) // 128 + 1))
best_val_loss = float('inf')
best_state = None
for epoch in range(epochs):
    model.train()
    np.random.shuffle(tr)
    for s in range(0, len(tr), 128):
        idx = tr[s:s + 128]
        loss = F.cross_entropy(model(batch(crops[idx], True).to(device)), torch.from_numpy(label[idx]).to(device), label_smoothing=0.05)
        opt.zero_grad()
        loss.backward()
        opt.step()
        sched.step()
    if len(va):
        model.eval()
        with torch.no_grad():
            logits = torch.cat([model(batch(crops[va[s:s + 512]], False).to(device)).cpu() for s in range(0, len(va), 512)])
            val_loss = float(F.cross_entropy(logits, torch.from_numpy(label[va])))
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
if best_state is not None:
    model.load_state_dict(best_state)
    print(f'restored best validation checkpoint (val cross-entropy {best_val_loss:.4f})')
model.eval()
with torch.no_grad():
    prob = torch.cat([F.softmax(model(batch(crops[s:s + 512], False).to(device)), 1)[:, 1].cpu() for s in range(0, len(crops), 512)]).numpy()
pred = (prob > 0.5).astype(int)
conf = np.maximum(prob, 1 - prob)


def acc(mask, title):
    m = mask & test
    if m.sum():
        print(f'{title}: n={int(m.sum())}  classifier right {100 * (pred[m] == label[m]).mean():.1f}%')
    return m


unread = read_marker == 'unknown'
acc(np.ones(len(crops), bool), 'all test crops')
acc(both, 'marker needed (printing exists in both finishes)')
m = acc(both & unread, 'marker needed AND the OCR left it unread')
acc(both & ~unread, 'marker needed AND the OCR read it (labels come from it)')
for k in ('collection', 'scanned'):
    acc(both & unread & (kind == k), f'  unread, {k}')
print('\naccuracy against how many it answers, on the test crops where the marker is needed and unread:')
for t in (0.5, 0.7, 0.8, 0.9, 0.95, 0.99):
    sel = m & (conf >= t)
    if sel.sum():
        print(f'  confidence >= {t}: answers {100 * sel.sum() / m.sum():.0f}% of them, right {100 * (pred[sel] == label[sel]).mean():.1f}%')

# the whole foil answer without AI: the OCR's marker when it read one, else the classifier (when sure enough)
ocr_says = np.where(read_marker == 'foil', 1, np.where(read_marker == 'non-foil', 0, -1))
need = both & test
for t in (0.8, 0.9, 0.95):
    final = np.where(ocr_says >= 0, ocr_says, np.where(conf >= t, pred, -1))
    answered = need & (final >= 0)
    print(f'foil answer = OCR marker, else classifier at {t}: answers {100 * answered.sum() / need.sum():.1f}% of the cards that need it, '
          f'right {100 * (final[answered] == label[answered]).mean():.1f}%; {100 * (need & (final < 0)).sum() / need.sum():.1f}% left for a person')
torch.save(model.state_dict(), OUT / 'foil_net.pt')
(OUT / 'foil_result.json').write_text(json.dumps({'test_crops': int(test.sum())}))
