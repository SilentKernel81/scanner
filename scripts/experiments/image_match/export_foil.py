#!/usr/bin/env python3
"""
Exports the foil-marker classifier (trained by foil_train.py on 80% of the cards, tested on the other 20%: the numbers in
docs/plans/IMAGE_MATCH_EXPERIMENT.md are THESE weights') to src/card_scanner/core/foil_net.onnx, and checks that OpenCV's dnn module gives
the answers torch gives, on every crop of out/foil_crops.npz.

    ~/.cache/mtg-image-match/venv/bin/python -I scripts/experiments/image_match/export_foil.py
(the experiment venv: torch and OpenCV 4.14, the version of the Android app)
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cv2
import numpy as np
import torch
import torch.nn as nn
from common import OUT

TARGET = HERE.parents[2] / 'src' / 'card_scanner' / 'core' / 'foil_net.onnx'


def foil_net():
    """The classifier of foil_train.py (same layers, so its saved weights load)"""
    def block(i, o):
        return nn.Sequential(nn.Conv2d(i, o, 3, padding=1, bias=False), nn.BatchNorm2d(o), nn.ReLU(inplace=True), nn.MaxPool2d(2))
    return nn.Sequential(block(1, 16), block(16, 32), block(32, 64), block(64, 96), nn.AdaptiveAvgPool2d(1), nn.Flatten(), nn.Dropout(0.3), nn.Linear(96, 2))


def normalise(crops):
    """foil_train.py's per-crop contrast normalisation, for an array (n, 32, 128) of uint8 -> (n, 1, 32, 128) float32"""
    x = torch.from_numpy(crops).float().div(255).unsqueeze(1)
    return ((x - x.mean((2, 3), keepdim=True)) / (x.std((2, 3), keepdim=True) + 1e-3)).numpy()


model = foil_net()
model.load_state_dict(torch.load(OUT / 'foil_net.pt', map_location='cpu'))
model.eval()
for parameter in model.parameters():
    parameter.requires_grad_(False)
torch.onnx.export(model, torch.zeros(1, 1, 32, 128), str(TARGET), input_names=['crop'], output_names=['logits'], opset_version=17, dynamo=False)
print('written', TARGET, round(TARGET.stat().st_size / 1024, 1), 'KB')

net = cv2.dnn.readNetFromONNX(str(TARGET))
crops = np.load(OUT / 'foil_crops.npz')['crops']
x = normalise(crops)
with torch.no_grad():
    want = torch.softmax(model(torch.from_numpy(x)), 1)[:, 1].numpy()
got = np.empty(len(x), np.float32)
for i in range(len(x)):
    net.setInput(np.ascontiguousarray(x[i:i + 1]))
    logits = net.forward().reshape(-1)
    e = np.exp(logits - logits.max())
    got[i] = e[1] / e.sum()
print(f'OpenCV {cv2.__version__} dnn against torch on {len(x)} crops: largest difference in the foil probability {np.abs(got - want).max():.2e}, '
      f'different answers at 0.5: {int(((got > 0.5) != (want > 0.5)).sum())}, at 0.9 / 0.1: {int(((got > 0.9) != (want > 0.9)).sum() + ((got < 0.1) != (want < 0.1)).sum())}')
