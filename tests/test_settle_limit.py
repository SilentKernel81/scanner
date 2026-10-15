"""
The stillness test of auto-capture (station/scanner.py: _is_card_settled): how far a card's
outline may jitter between frames and still count as a card at rest. The limit is
auto_capture.max_movement (1.5%; it was 1%, which kept cards waiting 5-15 s on a tall sleeved
pile once the corner network drew the outline - Pi 5, 2026-10-15). A card that really moves
must still not count.

A fake frame with a sharp pattern and a fake camera object: no camera, no network.

    venv/bin/python -m unittest discover tests -p test_settle_limit.py
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

from card_scanner.config import Config  # noqa: E402
from card_scanner.station import scanner as scanner_module  # noqa: E402

SIZE = 400.0   # the card's diagonal in pixels is about 500: 1% = ~5 px
CARD = np.array([[300, 100], [600, 100], [600, 500], [300, 500]], np.float32)


def sharp_frame():
    rng = np.random.default_rng(1)
    return rng.integers(0, 255, (720, 1280, 3), dtype=np.uint8)   # noise: very sharp


class Settled(unittest.TestCase):
    def setUp(self):
        self.camera = scanner_module.CardScanner.__new__(scanner_module.CardScanner)
        for name, value in dict(previous_card_points=None, previous_sharpness=None, previous_thumbnail=None,
                                stable_frames=0, settle_anchor=None, card_in_focus=False,
                                card_disturbed=False).items():
            setattr(self.camera, name, value)
        self.frame = sharp_frame()

    def after(self, shift):
        """_is_card_settled for a card that moves by `shift` pixels (all four corners) after a first frame"""
        self.camera._is_card_settled(self.frame, CARD)                 # the first frame only notes the card
        return self.camera._is_card_settled(self.frame, CARD + np.float32(shift))

    def diagonal(self):
        return float(np.linalg.norm(CARD[2] - CARD[0]))

    def test_the_default_limit_is_one_and_a_half_percent(self):
        self.assertAlmostEqual(Config.AUTO_CAPTURE_MAX_MOVEMENT, 0.015)

    def test_a_jitter_of_one_to_one_and_a_half_percent_is_still(self):
        shift = self.diagonal() * 0.0125 / np.sqrt(2)                  # 1.25% along the diagonal
        self.assertTrue(self.after(shift))

    def test_two_percent_is_not_still(self):
        shift = self.diagonal() * 0.02 / np.sqrt(2)
        self.assertFalse(self.after(shift))

    def test_the_limit_can_be_set(self):
        shift = self.diagonal() * 0.0125 / np.sqrt(2)
        with mock.patch.object(Config, 'AUTO_CAPTURE_MAX_MOVEMENT', 0.01):
            self.assertFalse(self.after(shift))                        # as before, at 1%


if __name__ == '__main__':
    unittest.main()
