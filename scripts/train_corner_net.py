#!/usr/bin/env python3
"""
Trains and scores our own card corner detector on the labelled camera frames (data/frames/,
labelled with scripts/label_frames.py). Trained only from those labels: no other model's
weights or outputs are used.

    venv/bin/python scripts/train_corner_net.py --frames <folder> --train rpi-scanner --val laptop-try
    venv/bin/python scripts/train_corner_net.py --frames <folder> --train all --export corner_net.onnx

What it is
  A small fully convolutional network (about 0.4 M parameters, ~1.5 MB) that takes a 192 x 320
  window and returns the four corners of the card in it (top left, top right, bottom right,
  bottom left, in the window's own pixels) plus a "there is a card here" score. The corners are
  read as the expected position under a softmax over each corner's map - sub-pixel and
  differentiable. Only operations OpenCV's dnn module runs are used (convolution, ReLU,
  nearest-neighbour upsampling, add, global pooling), so the exported ONNX file runs on a
  station and in the Android app without a new library.

How a frame is read (two stages, as `find` does)
  1. the whole frame is letterboxed into a 192 x 320 window: the card is small there, so this
     only finds where it is;
  2. a window of 1.45 x the card's box (the same shape) is cut from the full-resolution frame
     and read again: the card fills most of it and the corners come out precisely.
  The network is trained on windows of both kinds (the card filling 12% - 80% of the window),
  with shifts, tilts, flips, colour changes, blur, noise and cut-outs, and on windows without a
  card so the score means something.

Validation is by whole session (a station): train on one, score on another. Whole-frame
scoring uses the intersection over union (IoU) with the label, as scripts/score_detector.py.
"""
import argparse
import json
import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # score_detector (labelled frame loading, IoU)

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from score_detector import iou, load

W, H = 192, 320          # the network's window
STRIDE = 4               # the corner maps are W/4 x H/4
ZOOM_OUT = 1.45          # stage 2: the window is this much larger than the card's box


# ----------------------------------------------------------------------------------------
# The network
# ----------------------------------------------------------------------------------------

def block(cin, cout, stride):
    return nn.Sequential(nn.Conv2d(cin, cout, 3, stride, 1, bias=False), nn.BatchNorm2d(cout), nn.ReLU(inplace=True))


class CornerNet(nn.Module):
    """A small encoder / decoder: four corner maps at 1/4 of the window and one presence logit"""

    def __init__(self):
        super().__init__()
        self.s1 = nn.Sequential(block(3, 16, 2), block(16, 24, 2), block(24, 24, 1))        # 1/4
        self.s2 = nn.Sequential(block(24, 48, 2), block(48, 48, 1))                         # 1/8
        self.s3 = nn.Sequential(block(48, 80, 2), block(80, 80, 1))                         # 1/16
        self.s4 = nn.Sequential(block(80, 128, 2), block(128, 128, 1), block(128, 128, 1))  # 1/32
        self.l3, self.l2, self.l1 = nn.Conv2d(80, 80, 1), nn.Conv2d(48, 48, 1), nn.Conv2d(24, 24, 1)
        self.d3, self.d2, self.d1 = block(128 + 80, 80, 1), block(80 + 48, 48, 1), block(48 + 24, 32, 1)
        self.head = nn.Conv2d(32, 4, 1)
        self.presence = nn.Conv2d(128, 1, 1)

    def forward(self, x):
        c1 = self.s1(x); c2 = self.s2(c1); c3 = self.s3(c2); c4 = self.s4(c3)
        up = lambda t, ref: F.interpolate(t, size=ref.shape[-2:], mode='nearest')
        d3 = self.d3(torch.cat([up(c4, c3), self.l3(c3)], 1))
        d2 = self.d2(torch.cat([up(d3, c2), self.l2(c2)], 1))
        d1 = self.d1(torch.cat([up(d2, c1), self.l1(c1)], 1))
        maps = self.head(d1)                                       # (B, 4, H/4, W/4)
        presence = self.presence(c4).mean(dim=(2, 3))              # (B, 1)
        return maps, presence


def decode(maps):
    """Expected position of each corner under a softmax of its map: (B, 4, 2) in window pixels"""
    b, k, h, w = maps.shape
    prob = F.softmax(maps.reshape(b, k, h * w), dim=-1).reshape(b, k, h, w)
    ys = (torch.arange(h, dtype=maps.dtype, device=maps.device) + 0.5) * STRIDE
    xs = (torch.arange(w, dtype=maps.dtype, device=maps.device) + 0.5) * STRIDE
    return torch.stack([(prob.sum(2) * xs).sum(-1), (prob.sum(3) * ys).sum(-1)], dim=-1)


# ----------------------------------------------------------------------------------------
# Windows: cutting, augmenting
# ----------------------------------------------------------------------------------------

MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)


def to_tensor(window_rgb):
    return torch.from_numpy(((window_rgb.astype(np.float32) / 255 - MEAN) / STD).transpose(2, 0, 1).copy())


def window_matrix(cx, cy, height, angle=0.0):
    """Affine matrix (frame -> window) for the window of this height (frame pixels) centred on (cx, cy)"""
    scale = H / height
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    # rotate about the centre, scale, move the centre to the middle of the window
    m = np.array([[c * scale, -s * scale, 0], [s * scale, c * scale, 0]], np.float64)
    m[:, 2] = np.array([W / 2, H / 2]) - m[:, :2] @ np.array([cx, cy])
    return m


def cut(rgb, m):
    return cv2.warpAffine(rgb, m, (W, H), flags=cv2.INTER_AREA if abs(m[0, 0]) < 1 else cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)


def augment_colour(window, rng):
    img = window.astype(np.float32)
    img = img * rng.uniform(0.6, 1.4) + rng.uniform(-35, 35)                       # contrast, brightness
    img = img * rng.uniform(0.9, 1.1, size=3)                                      # colour cast
    if rng.random() < 0.5:
        k = rng.choice([3, 5, 7]); img = cv2.GaussianBlur(img, (k, k), 0)          # a soft focus
    if rng.random() < 0.5:
        img = img + rng.normal(0, rng.uniform(1, 8), img.shape)                    # sensor noise
    if rng.random() < 0.4:                                                         # glare / a cut-out
        x, y = rng.integers(0, W - 20), rng.integers(0, H - 20)
        w, h = rng.integers(10, 60), rng.integers(10, 60)
        img[y:y + h, x:x + w] = img[y:y + h, x:x + w] * rng.uniform(0.4, 1.6) + rng.uniform(-40, 40)
    return np.clip(img, 0, 255).astype(np.uint8)


class Frames:
    """Labelled frames kept in memory, shrunk so that a window's worth is cheap to cut"""

    def __init__(self, rows, longest=1600):
        self.items = []
        for stem, note, label, picture in rows:
            bgr = cv2.imread(str(picture))
            if bgr is None:
                continue
            scale = min(1.0, longest / max(bgr.shape[:2]))
            if scale < 1:
                bgr = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            self.items.append({'rgb': cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), 'scale': scale,
                               'quad': np.array(label['corners'], np.float64) * scale,
                               'station': note['station'], 'number': note.get('number'), 'stem': stem})

    def sample(self, rng, train_negative=0.15):
        """One training window: (tensor, corners in window pixels (4, 2), card present, mask)"""
        it = self.items[rng.integers(len(self.items))]
        quad = it['quad']
        centre = quad.mean(axis=0)
        card_h = np.linalg.norm(quad[3] - quad[0]) + np.linalg.norm(quad[2] - quad[1])
        card_h /= 2
        if rng.random() < train_negative:       # a window with no card in it: the score's negatives
            for _ in range(20):
                fh = rng.uniform(0.4, 1.0) * it['rgb'].shape[0]
                cx = rng.uniform(0, it['rgb'].shape[1]); cy = rng.uniform(0, it['rgb'].shape[0])
                m = window_matrix(cx, cy, fh)
                inv = cv2.invertAffineTransform(m)
                box = np.array([[0, 0], [W, 0], [W, H], [0, H]], np.float64) @ inv[:, :2].T + inv[:, 2]
                lo, hi = quad.min(0), quad.max(0)
                if box[:, 0].max() < lo[0] or box[:, 0].min() > hi[0] or box[:, 1].max() < lo[1] or box[:, 1].min() > hi[1]:
                    win = augment_colour(cut(it['rgb'], m), rng)
                    return to_tensor(win), np.zeros((4, 2), np.float32), 0.0, 0.0
        # the card fills 12% - 80% of the window's height: wide windows teach stage 1, tight ones stage 2
        if rng.random() < 0.35:
            fill = math.exp(rng.uniform(math.log(0.12), math.log(0.45)))
        else:
            fill = rng.uniform(0.55, 0.8)
        height = card_h / fill
        jitter = height * 0.12 * (1 - fill) + card_h * 0.06
        cx, cy = centre + rng.uniform(-jitter, jitter, 2)
        m = window_matrix(cx, cy, height, angle=rng.uniform(-8, 8))
        win = cut(it['rgb'], m)
        pts = quad @ m[:, :2].T + m[:, 2]
        if rng.random() < 0.5:                  # a mirror image: the corners swap sides
            win = win[:, ::-1]
            pts = np.stack([W - pts[:, 0], pts[:, 1]], 1)[[1, 0, 3, 2]]
        return to_tensor(augment_colour(np.ascontiguousarray(win), rng)), pts.astype(np.float32), 1.0, 1.0


def batch(frames, size, rng):
    xs, ps, pres, mask = zip(*[frames.sample(rng) for _ in range(size)])
    return torch.stack(xs), torch.from_numpy(np.stack(ps)), torch.tensor(pres), torch.tensor(mask)


# ----------------------------------------------------------------------------------------
# Using it on a whole frame (two stages)
# ----------------------------------------------------------------------------------------

@torch.no_grad()
def read_window(model, rgb, cx, cy, height):
    m = window_matrix(cx, cy, height)
    win = cut(rgb, m)
    maps, presence = model(to_tensor(win)[None])
    pts = decode(maps)[0].numpy().astype(np.float64)
    inv = cv2.invertAffineTransform(m)
    return pts @ inv[:, :2].T + inv[:, 2], float(torch.sigmoid(presence)[0, 0])


def find(model, rgb):
    """The card's four corners in the frame (top left, top right, bottom right, bottom left), and a score"""
    h, w = rgb.shape[:2]
    # stage 1: the whole frame in one window (letterboxed: the window is taller than any frame)
    height = max(h, w * H / W)
    quad, score = read_window(model, rgb, w / 2, h / 2, height)
    # stage 2: a window around that quad, twice
    for _ in range(2):
        xs, ys = quad[:, 0], quad[:, 1]
        card_h = max(ys.max() - ys.min(), (xs.max() - xs.min()) * H / W * 0.6)
        quad, score2 = read_window(model, rgb, (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2, card_h * ZOOM_OUT)
        score = max(score, score2)
    return quad, score


def evaluate(model, frames, title):
    model.eval()
    rows = []
    for it in frames.items:
        t = time.perf_counter()
        quad, score = find(model, it['rgb'])
        ms = (time.perf_counter() - t) * 1000
        rows.append((it['station'], iou(quad, it['quad'], it['rgb'].shape[:2]), score, ms))
    if not rows:
        return {}
    values = np.array([r[1] for r in rows])
    print(f"  {title}: {len(rows)} frames  mean IoU {values.mean():.3f}  median {np.median(values):.3f}  "
          f"worst {values.min():.3f}  >=0.95: {(values >= .95).sum()}  >=0.90: {(values >= .90).sum()}  "
          f"{np.mean([r[3] for r in rows]):.0f} ms/frame")
    return {'mean': float(values.mean()), 'worst': float(values.min())}


# ----------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Train / score our card corner detector on labelled frames")
    parser.add_argument('--frames', default='data/frames')
    parser.add_argument('--train', default='all', help="comma separated stations, or all")
    parser.add_argument('--val', default='', help="comma separated stations held out for scoring")
    parser.add_argument('--steps', type=int, default=3000)
    parser.add_argument('--batch', type=int, default=24)
    parser.add_argument('--lr', type=float, default=2e-3)
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--export', help="write the trained network as ONNX to this file")
    parser.add_argument('--save', help="write the weights (PyTorch) to this file")
    parser.add_argument('--load', help="start from / score these weights; with --steps 0 only scores")
    args = parser.parse_args()

    rows = load(args.frames)
    train_names = None if args.train == 'all' else set(args.train.split(','))
    val_names = set(args.val.split(',')) if args.val else set()
    train_rows = [r for r in rows if (train_names is None or r[1]['station'] in train_names) and r[1]['station'] not in val_names]
    val_rows = [r for r in rows if r[1]['station'] in val_names]
    print(f"{len(rows)} labelled frames: training on {len(train_rows)} "
          f"({sorted({r[1]['station'] for r in train_rows})}), scoring on {len(val_rows)} ({sorted(val_names)})")
    torch.set_num_threads(max(1, torch.get_num_threads()))
    rng = np.random.default_rng(args.seed); random.seed(args.seed); torch.manual_seed(args.seed)

    model = CornerNet()
    if args.load:
        model.load_state_dict(torch.load(args.load))
    print(f"network: {sum(p.numel() for p in model.parameters()) / 1e6:.2f} M parameters")
    train, val = Frames(train_rows), Frames(val_rows)

    if args.steps > 0 and train.items:
        opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
        sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=args.lr, total_steps=args.steps, pct_start=0.1)
        started = time.time()
        for step in range(1, args.steps + 1):
            model.train()
            x, pts, pres, mask = batch(train, args.batch, rng)
            maps, presence = model(x)
            pred = decode(maps)
            # corners: L1 in window pixels (about 1 px at 1/4 resolution matters) where a card is present
            corner_loss = (torch.abs(pred - pts).mean(dim=(1, 2)) * mask).sum() / mask.sum().clamp(min=1)
            # a sharp map: the target's Gaussian as a cross-entropy keeps each corner's map unimodal
            b = pts.shape[0]
            gy = (torch.arange(maps.shape[2]).float() + 0.5) * STRIDE
            gx = (torch.arange(maps.shape[3]).float() + 0.5) * STRIDE
            dx2 = (gx[None, None, None, :] - pts[:, :, 0, None, None]) ** 2
            dy2 = (gy[None, None, :, None] - pts[:, :, 1, None, None]) ** 2
            target = torch.exp(-(dx2 + dy2) / (2 * 6.0 ** 2))
            target = target / target.sum(dim=(2, 3), keepdim=True).clamp(min=1e-8)
            logp = F.log_softmax(maps.reshape(b, 4, -1), dim=-1).reshape_as(target)
            heat_loss = ((-(target * logp).sum(dim=(2, 3))).mean(1) * mask).sum() / mask.sum().clamp(min=1)
            presence_loss = F.binary_cross_entropy_with_logits(presence[:, 0], pres)
            loss = corner_loss * 0.05 + heat_loss * 0.2 + presence_loss
            opt.zero_grad(); loss.backward(); opt.step(); sched.step()
            if step % 250 == 0 or step == args.steps:
                print(f"  step {step:5d}  loss {loss.item():.3f} (corners {corner_loss.item():.1f}px, map {heat_loss.item():.2f}, "
                      f"score {presence_loss.item():.2f})  {time.time() - started:.0f}s")
            if val.items and step % 1000 == 0 and step != args.steps:
                evaluate(model, val, f"held out @ step {step}")
        if args.save:
            torch.save(model.state_dict(), args.save)

    print("final:")
    if val.items:
        evaluate(model, val, "held out")
    evaluate(model, train, "training frames")
    if args.export:
        model.eval()
        torch.onnx.export(model, torch.zeros(1, 3, H, W), args.export, input_names=['image'],
                          output_names=['maps', 'presence'], opset_version=13, dynamo=False)
        print("exported", args.export, f"({Path(args.export).stat().st_size / 1e6:.2f} MB)")


if __name__ == '__main__':
    main()
