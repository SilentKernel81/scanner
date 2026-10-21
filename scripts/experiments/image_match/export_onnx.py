#!/usr/bin/env python3
"""
Step 2/4: export an embedding model to ONNX and check that OpenCV's dnn module (what the Android app
has, 4.14) gives the same vectors as torch - the parity test corner_net also had.

    venv/bin/python -I export_onnx.py --model mnv3|dino [--weights finetuned.pt]
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cv2
import numpy as np
import torch
from common import OUT
from embed import build


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', required=True)
    ap.add_argument('--weights')
    a = ap.parse_args()
    net, (w, h) = build(a.model)
    if a.weights:
        net.load_state_dict(torch.load(a.weights))
    for parameter in net.parameters():     # frozen: the exporter cannot take weights that require grad
        parameter.requires_grad_(False)
    x = torch.randn(1, 3, h, w)
    path = OUT / f'{a.model}.onnx'
    torch.onnx.export(net, x, str(path), input_names=['image'], output_names=['embedding'], opset_version=17, dynamo=False)
    print('written', path, round(path.stat().st_size / 1e6, 1), 'MB')
    with torch.no_grad():
        want = net(x).numpy()
    try:
        dnn = cv2.dnn.readNetFromONNX(str(path))
        dnn.setInput(x.numpy())
        got = dnn.forward()
        t0 = time.time()
        for _ in range(10):
            dnn.setInput(x.numpy())
            dnn.forward()
        ms = (time.time() - t0) * 100
        cos = float((want * got).sum() / (np.linalg.norm(want) * np.linalg.norm(got)))
        print(f'OpenCV {cv2.__version__} dnn: cosine to torch {cos:.6f}, max abs diff {np.abs(want - got).max():.2e}, {ms:.0f} ms per picture (this machine, CPU)')
    except cv2.error as e:
        print('OpenCV dnn CANNOT run this model:', str(e)[:300])


if __name__ == '__main__':
    main()
