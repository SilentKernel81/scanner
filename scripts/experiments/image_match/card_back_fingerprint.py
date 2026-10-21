#!/usr/bin/env python3
"""
The fingerprint of the standard Magic back, as core/card_back.py pins it (CARD_BACK_FINGERPRINT): Scryfall's picture of the back, fingerprinted by image_index.hash_card.
Run it again when image_index's fingerprint changes (DESCRIPTOR) and paste the hex it prints into card_back.py, with the new descriptor.

    venv/bin/python -I scripts/experiments/image_match/card_back_fingerprint.py [BACK.jpg]
Without a file it fetches the picture from Scryfall (backs.scryfall.io) once.
"""
import hashlib, sys, urllib.request
from pathlib import Path
import cv2, numpy as np
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'src'))
from card_scanner.core import card_back, image_index

URL = 'https://backs.scryfall.io/large/0/a/0aeebaf5-8c7d-4636-9e82-8c27447861f7.jpg'
if len(sys.argv) > 1:
    data = Path(sys.argv[1]).read_bytes()
else:
    data = urllib.request.urlopen(urllib.request.Request(URL, headers={'User-Agent': 'card-scanner/1.0'}), timeout=30).read()
rgb = cv2.cvtColor(cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
fingerprint = image_index.hash_card(rgb).hex()
print(f"picture: {rgb.shape[1]} x {rgb.shape[0]}, SHA-256 {hashlib.sha256(data).hexdigest()}")
print(f"descriptor: {image_index.DESCRIPTOR}")
print(f"fingerprint: {fingerprint}")
print("the same as card_back.CARD_BACK_FINGERPRINT" if fingerprint == card_back.CARD_BACK_FINGERPRINT.hex() else "DIFFERENT from card_back.CARD_BACK_FINGERPRINT")
