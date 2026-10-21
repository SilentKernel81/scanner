#!/usr/bin/env python3
"""Does this venv's torch see the GPU, and how much faster than the CPU is MobileNetV3 on it?"""
import time

import torch
import torchvision

print('torch', torch.__version__, 'cuda build', torch.version.cuda, 'available', torch.cuda.is_available())
if torch.cuda.is_available():
    print(torch.cuda.get_device_name(0), round(torch.cuda.get_device_properties(0).total_memory / 1e9, 1), 'GB')
    net = torchvision.models.mobilenet_v3_large(weights=None).features.eval()
    x = torch.randn(64, 3, 312, 224)
    for device in ('cpu', 'cuda'):
        m, y = net.to(device), x.to(device)
        with torch.no_grad():
            m(y)
            if device == 'cuda':
                torch.cuda.synchronize()
            t0 = time.time()
            for _ in range(5):
                m(y)
            if device == 'cuda':
                torch.cuda.synchronize()
        print(device, round(64 * 5 / (time.time() - t0)), 'pictures/s (forward only)')
