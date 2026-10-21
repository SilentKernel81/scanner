#!/usr/bin/env python3
"""
Step 2: embed the gallery (Scryfall pictures) and the evaluation pictures with one model.

    venv/bin/python -I embed.py --model mnv3 [--crop card|art] [--no-normalise] [--only eval|gallery]

Models:  phash  - perceptual hash baseline (16x16 DCT hash, 256 bits, compared by Hamming distance)
         mnv3   - torchvision MobileNetV3-Large, ImageNet weights, global-pooled features (960-d)
         dino   - DINOv2 ViT-S/14, CLS token (384-d)
Output:  out/<model>_<crop>[_raw]/gallery.npz (ids, vectors float32 L2-normalised; bits for phash) and eval.npz
Resumable per file: a gallery that grows while Scryfall's pictures are still downloading is
extended, not redone.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cv2
import numpy as np
import torch
from common import IMAGES, OUT, W, H, gallery_ids, prepare, read

DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)


def build(model):
    """A torch module taking a (n, 3, h, w) normalised batch and returning (n, d); plus the input size"""
    if model == 'mnv3':
        import torchvision
        net = torchvision.models.mobilenet_v3_large(weights='IMAGENET1K_V2')

        class Features(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.f, self.p = net.features, torch.nn.AdaptiveAvgPool2d(1)

            def forward(self, x):
                return self.p(self.f(x)).flatten(1)
        return Features().eval(), (W, H)
    if model == 'ft':       # the fine-tuned network (finetune.py), its weights in out/ft.pt
        from ftmodel import FtNet, INPUT_W, INPUT_H
        net = FtNet(pretrained=False)
        net.load_state_dict(torch.load(OUT / 'ft.pt', map_location='cpu'))
        return net.eval(), (INPUT_W, INPUT_H)
    if model == 'dino':
        net = torch.hub.load('facebookresearch/dinov2', 'dinov2_vits14', verbose=False)

        class Cls(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.net = net

            def forward(self, x):
                return self.net(x)    # the CLS token (after the final norm)
        model = Cls().eval()
        with torch.no_grad():     # the input size is fixed: the position embedding for it is a constant, so the
            # interpolation (a Resize layer OpenCV's dnn cannot read) is done once, here
            fixed = net.interpolate_pos_encoding(torch.zeros(1, 1 + 22 * 16, 384), 224, 308).clone()
        net.interpolate_pos_encoding = lambda x, w, h: fixed
        return model, (224, 308)       # multiples of the patch size 14
    raise SystemExit(f'unknown model {model}')


def dct_bits(plane, side):
    """The lowest side x side DCT frequencies of a 64 x 64 plane, each above / below their median: side*side bits"""
    low = cv2.dct(plane)[:side, :side]
    return np.packbits((low > np.median(low.flatten()[1:])).flatten())


def phash_bits(rgb, model='phash'):
    """
    The hashes compared against the networks (a few bytes per card, compared by Hamming distance):
      phash      16 x 16 DCT of the grey picture: 256 bits (32 bytes)
      phash32    32 x 32 DCT of the grey picture: 1024 bits (128 bytes)
      phash_rgb  16 x 16 DCT of each colour channel: 768 bits (96 bytes) - keeps the colour a grey hash drops
    """
    small = cv2.resize(rgb, (64, 64), interpolation=cv2.INTER_AREA)
    if model == 'phash_rgb':
        return np.concatenate([dct_bits(small[..., c].astype(np.float32), 16) for c in range(3)])
    grey = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY).astype(np.float32)
    return dct_bits(grey, 32 if model == 'phash32' else 16)


_hog = {}


def hog_vector(rgb, model):
    """
    Histogram of oriented gradients (OpenCV's own HOGDescriptor) of the grey card, L2-normalised: compared by cosine similarity.
      hog    64 x 96 window, 8 px cells, 16 px blocks, stride 8, 9 bins:  7 x 11 blocks x 36 = 2772 floats
      hog_s  32 x 48 window, same cells and blocks:                       3 x 5 blocks x 36 = 540 floats
    """
    if model not in _hog:
        size = (64, 96) if model == 'hog' else (32, 48)
        _hog[model] = (cv2.HOGDescriptor(size, (16, 16), (8, 8), (8, 8), 9), size)
    descriptor, size = _hog[model]
    grey = cv2.cvtColor(cv2.resize(rgb, size, interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2GRAY)
    v = descriptor.compute(grey).flatten().astype(np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


class Pictures(torch.utils.data.Dataset):
    def __init__(self, items, a):
        self.items, self.a = items, a

    def __len__(self):
        return len(self.items)

    def __getitem__(self, i):
        it = self.items[i]
        rgb = read(it['path'])
        if rgb is None:
            return torch.zeros(3, H, W), False
        if 'corners' in it:       # a camera frame: cut the card out with the station's corners first
            from build_eval import warp
            rgb = warp(rgb, it['corners'])
        if getattr(self.a, 'rotate180', False):      # the orientation test: the card is upside down
            rgb = cv2.rotate(rgb, cv2.ROTATE_180)
        rgb = prepare(rgb, self.a.crop, not self.a.no_normalise)
        if self.a.model.startswith('phash'):
            return torch.from_numpy(phash_bits(rgb, self.a.model)), True
        if self.a.model.startswith('hog'):
            return torch.from_numpy(hog_vector(rgb, self.a.model)), True
        if self.a.model == 'dino':
            rgb = rgb[2:-2]       # 312 -> 308 rows
        x = (rgb.astype(np.float32) / 255 - MEAN) / STD
        return torch.from_numpy(x.transpose(2, 0, 1).copy()), True


def embed(items, a, net):
    loader = torch.utils.data.DataLoader(Pictures(items, a), batch_size=64, num_workers=6)
    vectors, ok, t0 = [], [], time.time()
    with torch.no_grad():
        for n, (x, good) in enumerate(loader):
            if a.model.startswith(('phash', 'hog')):
                v = x.numpy()
            else:     # on the GPU when there is one (the CPU run of DINOv2 alone took 2.7 hours)
                v = torch.nn.functional.normalize(net(x.to(DEVICE)).float(), dim=1).cpu().numpy()
            vectors.append(v)
            ok.append(good.numpy())
            if n % 20 == 0:
                print(f'  {min((n + 1) * 64, len(items))}/{len(items)}  {time.time() - t0:.0f}s', flush=True)
    return np.concatenate(vectors), np.concatenate(ok)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--model', required=True)
    p.add_argument('--crop', default='card')
    p.add_argument('--no-normalise', action='store_true')
    p.add_argument('--only', choices=('eval', 'gallery'))
    p.add_argument('--rotate180', action='store_true', help='orientation test: embed the evaluation pictures upside down, '
                   'into <name>_rot180/ next to a link to the normal gallery')
    a = p.parse_args()
    torch.set_num_threads(8)
    name = f"{a.model}_{a.crop}{'_raw' if a.no_normalise else ''}"
    folder = OUT / name
    folder.mkdir(exist_ok=True)
    if a.rotate180:
        a.only = 'eval'
        folder = OUT / f'{name}_rot180'
        folder.mkdir(exist_ok=True)
        if not (folder / 'gallery.npz').exists():
            (folder / 'gallery.npz').symlink_to(OUT / name / 'gallery.npz')
    net = None if a.model.startswith(('phash', 'hog')) else build(a.model)[0].to(DEVICE)
    print('device', DEVICE, flush=True)

    if a.only != 'gallery':
        items = json.loads((OUT / 'eval.json').read_text())
        v, ok = embed(items, a, net)
        np.savez(folder / 'eval.npz', vectors=v, ok=ok)
    if a.only != 'eval':
        have = set(np.load(folder / 'gallery.npz')['ids']) if (folder / 'gallery.npz').exists() else set()
        old = np.load(folder / 'gallery.npz') if have else None
        ids = sorted(i for i in gallery_ids() if (IMAGES / f'{i}.jpg').exists() and i not in have)
        print(len(have), 'already embedded,', len(ids), 'new')
        if ids:
            v, ok = embed([{'path': str(IMAGES / f'{i}.jpg')} for i in ids], a, net)
            ids = [i for i, g in zip(ids, ok) if g]
            v = v[ok]
            if old is not None:
                ids, v = list(old['ids']) + ids, np.concatenate([old['vectors'], v])
            np.savez(folder / 'gallery.npz', ids=np.array(ids), vectors=v)


if __name__ == '__main__':
    main()
