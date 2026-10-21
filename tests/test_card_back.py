"""
Is a capture the back of a Magic card? (core/card_back.py, docs/plans/CARD_BACK_STOP_PLAN.md) On synthetic pictures only: the pinned fingerprint is swapped for one of a
synthetic "back" (no picture of the real back is shipped), so these pin the arithmetic - as given and turned, degraded, another picture, a broken one - and that the
pinned fingerprint was made with the fingerprint the index uses today. Where real backs and fronts fall is measured by scripts/experiments/image_match/card_back_measure.py.

    venv/bin/python -m unittest discover tests -p test_card_back.py
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))                 # the helpers of test_printing_check.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from card_scanner.core import card_back, image_index  # noqa: E402
from test_printing_check import degraded, synthetic_card  # noqa: E402


class ThePinnedFingerprint(unittest.TestCase):
    def test_it_was_made_with_the_fingerprint_the_index_uses(self):
        # image_index changes DESCRIPTOR with its fingerprint: the back must then be fingerprinted again (card_back_fingerprint.py)
        self.assertEqual(card_back.FINGERPRINT_DESCRIPTOR, image_index.DESCRIPTOR)
        self.assertEqual(len(card_back.CARD_BACK_FINGERPRINT), len(image_index.hash_card(synthetic_card(0))))


class OnASyntheticBack(unittest.TestCase):
    def setUp(self):
        self.back = synthetic_card(41)
        patcher = mock.patch.object(card_back, 'CARD_BACK_FINGERPRINT', image_index.hash_card(self.back))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_back_itself_and_turned_is_a_back(self):
        self.assertEqual(card_back.card_back_similarity(self.back), 1.0)
        self.assertEqual(card_back.card_back_similarity(cv2.rotate(self.back, cv2.ROTATE_180)), 1.0)    # a card lies either way up
        self.assertTrue(card_back.is_card_back(cv2.rotate(self.back, cv2.ROTATE_180)))

    def test_a_camera_picture_of_it_is_a_back(self):
        self.assertGreaterEqual(card_back.card_back_similarity(degraded(self.back)), card_back.CARD_BACK_MIN_SIMILARITY)
        self.assertTrue(card_back.is_card_back(degraded(self.back)))

    def test_another_picture_is_not(self):
        for seed in range(10):
            self.assertLess(card_back.card_back_similarity(degraded(synthetic_card(seed))), card_back.CARD_BACK_MIN_SIMILARITY, seed)
            self.assertFalse(card_back.is_card_back(synthetic_card(seed)))

    def test_the_threshold_can_be_given(self):
        self.assertFalse(card_back.is_card_back(degraded(self.back), min_similarity=1.01))

    def test_a_blank_or_broken_picture_is_not_a_back_and_never_raises(self):
        self.assertFalse(card_back.is_card_back(np.zeros((680, 488, 3), np.uint8)))
        self.assertFalse(card_back.is_card_back(np.zeros((0, 0, 3), np.uint8)))           # cannot be fingerprinted: not a back
        self.assertFalse(card_back.is_card_back(None))


if __name__ == '__main__':
    unittest.main()
