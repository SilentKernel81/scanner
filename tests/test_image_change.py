"""
The stillness / new-card test's picture comparison (station/scanner.py: _thumbnail_difference,
IMAGE_CHANGE_LIMIT): a still card whose outline wobbles by 1-1.5% of its size must not look like
a different card, a really different card must. Synthetic textured cards - no camera.

    venv/bin/python -m unittest discover tests -p test_image_change.py
"""
import sys
import unittest
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

from card_scanner.station.scanner import CardScanner  # noqa: E402

LIMIT = CardScanner.IMAGE_CHANGE_LIMIT


def card_picture(seed, size=(640, 900)):
    """A detailed 'card': noise at several scales, as a busy illustration and its text"""
    rng = np.random.default_rng(seed)
    image = np.zeros(size[::-1], np.float32)
    for scale, weight in ((4, 1.0), (16, 0.8), (64, 0.6)):
        small = rng.normal(size=(size[1] // scale, size[0] // scale)).astype(np.float32)
        image += weight * cv2.resize(small, size, interpolation=cv2.INTER_CUBIC)
    return image


def thumbnail(picture, shift=(0.0, 0.0), zoom=1.0):
    """What CardScanner._card_thumbnail makes of the card: warped by the outline, 32 x 45, normalised"""
    height, width = picture.shape
    matrix = np.float32([[zoom, 0, shift[0] * width + (1 - zoom) * width / 2],
                         [0, zoom, shift[1] * height + (1 - zoom) * height / 2]])
    moved = cv2.warpAffine(picture, matrix, (width, height), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    small = cv2.resize(moved, (32, 45), interpolation=cv2.INTER_AREA)
    return (small - small.mean()) / (small.std() + 1e-6)


class ImageChange(unittest.TestCase):
    def test_a_wobbling_outline_of_the_same_card_is_not_a_change(self):
        picture = card_picture(1)
        base = thumbnail(picture)
        for shift, zoom in (((0.012, 0.0), 1.0), ((0.0, 0.012), 1.0), ((0.01, -0.01), 1.012), ((-0.012, 0.008), 0.99)):
            wobbled = thumbnail(picture, shift, zoom)
            smooth = CardScanner._thumbnail_difference(base, wobbled, smooth=True)
            self.assertLess(smooth, LIMIT, f"shift {shift}, zoom {zoom}: {smooth:.2f}")

    def test_smoothing_lowers_the_wobble(self):
        picture = card_picture(2)
        base, wobbled = thumbnail(picture), thumbnail(picture, (0.012, 0.01), 1.012)
        raw = CardScanner._thumbnail_difference(base, wobbled)
        smooth = CardScanner._thumbnail_difference(base, wobbled, smooth=True)
        self.assertLess(smooth, raw)

    def test_a_different_card_is_a_change(self):
        a, b = thumbnail(card_picture(3)), thumbnail(card_picture(4))
        self.assertGreater(CardScanner._thumbnail_difference(a, b, smooth=True), LIMIT)

    def test_identical_thumbnails_are_no_change_and_missing_ones_are_none(self):
        a = thumbnail(card_picture(5))
        self.assertAlmostEqual(CardScanner._thumbnail_difference(a, a, smooth=True), 0.0, places=5)
        self.assertEqual(CardScanner._thumbnail_difference(a, None, smooth=True), 0.0)


if __name__ == '__main__':
    unittest.main()
