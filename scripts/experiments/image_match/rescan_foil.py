#!/usr/bin/env python3
"""
The Deadpool rescan, second half: for the 15 pictures (captures #1037-#1051, versions known from the physical cards - see rescan_eval.py),
what do the strip OCR and the foil classifier say about the finish, and what would the whole no-AI pipeline add?

    venv/bin/python -I rescan_foil.py        (the REPOSITORY's venv: it has the OCR reader; the classifier runs on the CPU)
The pipeline here: the colour hash's nearest printing decides WHICH printing when the strip's printing has other art (the Secret Lair
cards print the base printing's set and number); the finish comes from the OCR's marker if it read one, else the foil classifier.
Truth: regular -> non-foil, foil and confetti foil -> foil.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2
import numpy as np
import torch
import torch.nn as nn
from card_scanner.core import card_ocr
from common import HOME, IMAGES, OUT, prepare, printings, read
from embed import phash_bits
from foil_crops import set_line_box
from strip import crop, parse_strip

P = printings()
CARDS = ['Deadly Dispute', 'Lightning Bolt', 'Lightning Greaves', 'Sol Ring', 'Thrill of Possibility']
NUMBER = {'Deadly Dispute': 1, 'Lightning Bolt': 2, 'Thrill of Possibility': 3, 'Lightning Greaves': 4, 'Sol Ring': 5}
VERSIONS = ['regular', 'foil', 'confetti foil']
by_number = {p['number']: p['id'] for p in P.values() if p['set'] == 'sld' and p['number'].startswith('IFIYW')}
files = {int(f.name.split('_')[1]): f for f in (HOME / 'truth' / 'rescan_cards').iterdir() if f.name.startswith('phone-0240eb84_')}


def foil_net():
    """The classifier of foil_train.py (same layers, so its saved weights load)"""
    def block(i, o):
        return nn.Sequential(nn.Conv2d(i, o, 3, padding=1, bias=False), nn.BatchNorm2d(o), nn.ReLU(inplace=True), nn.MaxPool2d(2))
    return nn.Sequential(block(1, 16), block(16, 32), block(32, 64), block(64, 96), nn.AdaptiveAvgPool2d(1), nn.Flatten(), nn.Dropout(0.3), nn.Linear(96, 2))


model = foil_net()
model.load_state_dict(torch.load(OUT / 'foil_net.pt', map_location='cpu'))
model.eval()

g = np.load(OUT / 'phash_rgb_card' / 'gallery.npz')
ids = list(g['ids'])
gallery = (np.unpackbits(g['vectors'], axis=1).astype(np.float32) * 2 - 1) / np.sqrt(768)
ocr = card_ocr.CardOcr()
ocr.warm_up()

print(f'{"#":>5} {"version":13} {"card":22} {"strip read":24} {"marker":9} {"foil prob":>9} {"image's pick":14} {"finish said":11} {"truth":8} printing  finish')
printing_right = finish_right = 0
for k, number in enumerate(range(1037, 1052)):
    version, card = VERSIONS[k // 5], CARDS[k % 5]
    n = NUMBER[card]
    truth_id = by_number[f'IFIYW-{n + 5 if version == "confetti foil" else n}']
    truth_finish = 'regular' if version == 'regular' else 'foil'
    rgb = read(files[number])
    # printing: the image's nearest (the strip names the base printing, whose art differs)
    v = (np.unpackbits(phash_bits(prepare(rgb, 'card', True), 'phash_rgb')).astype(np.float32) * 2 - 1) / np.sqrt(768)
    pick = ids[int(np.argmax(gallery @ v))]
    # finish: the OCR's marker, else the classifier on the set line
    lines = ocr.read_lines(crop(rgb)) or []
    num, set_code, marker = parse_strip(lines, rgb.shape[0])
    box = set_line_box(lines)
    prob = None
    if box is not None:
        strip = crop(rgb)
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        pad = 0.25 * (max(ys) - min(ys))
        part = strip[max(int(min(ys) - pad), 0):int(max(ys) + pad), max(int(min(xs) - pad), 0):int(max(xs) + pad)]
        grey = cv2.resize(cv2.cvtColor(part, cv2.COLOR_RGB2GRAY), (128, 32), interpolation=cv2.INTER_AREA)
        x = torch.from_numpy(grey).float().div(255)[None, None]
        x = (x - x.mean((2, 3), keepdim=True)) / (x.std((2, 3), keepdim=True) + 1e-3)
        with torch.no_grad():
            prob = float(torch.softmax(model(x), 1)[0, 1])
    said = {'foil': 'foil', 'non-foil': 'regular'}.get(marker) or (None if prob is None else ('foil' if prob > 0.5 else 'regular'))
    printing_right += pick == truth_id
    finish_right += said == truth_finish
    print(f'{number:>5} {version:13} {card:22} {(set_code + " " + num).strip():24} {marker:9} {"-" if prob is None else f"{prob:.2f}":>9} '
          f'{P[pick]["number"]:14} {str(said):11} {truth_finish:8} {"right" if pick == truth_id else "WRONG":8} {"right" if said == truth_finish else "WRONG"}')
print(f'\nprinting right {printing_right} of 15, finish right {finish_right} of 15')
ocr.stop()
