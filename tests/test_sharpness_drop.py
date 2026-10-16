"""
The station refocuses by itself when the last 3 captured cards were far less sharp than the ones
before them (station/scanner.py: _check_sharpness_drop), with a locked focus only, not more than
once a minute, and never on a soft baseline. No camera: a scanner object with the fields it reads.

    venv/bin/python -m unittest discover tests -p test_sharpness_drop.py
"""
import sys
import time
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

from card_scanner.config import Config  # noqa: E402
from card_scanner.station.scanner import CardScanner  # noqa: E402


class SharpnessDrop(unittest.TestCase):
    def setUp(self):
        self.scanner = CardScanner.__new__(CardScanner)
        s = self.scanner
        s.capture_sharpness = deque(maxlen=24)
        s.previous_sharpness = None
        s.focus_locked_value = 430
        s.focus_sweep_running = False
        s.focus_probe_running = False
        s.camera_type = 'usb'
        s.focus_range = (300, 650)
        s.last_focus_sweep = 0.0
        s.refocus_on_drop = 0.35
        s.log = mock.Mock()
        s.refocus = mock.Mock(return_value=True)

    def capture(self, sharpness):
        self.scanner.previous_sharpness = sharpness
        self.scanner._check_sharpness_drop()

    def test_three_blurry_cards_after_sharp_ones_refocus(self):
        for value in (1500, 1800, 1400, 1700, 1600, 1500, 1900, 1450):
            self.capture(value)
        self.capture(480)
        self.capture(450)
        self.scanner.refocus.assert_not_called()          # two are not enough
        self.capture(400)
        self.scanner.refocus.assert_called_once_with("sharpness dropped")
        self.assertEqual(len(self.scanner.capture_sharpness), 0)   # judged against the new focus from here

    def test_one_plain_card_does_not(self):
        for value in (1500, 1800, 1400, 1700, 1600, 1500, 1900, 1450, 450, 1600, 1500):
            self.capture(value)
        self.scanner.refocus.assert_not_called()

    def test_a_run_of_plain_cards_after_a_run_of_plain_cards_does_not(self):
        for value in [420, 450, 400, 430, 410, 440, 420, 430, 415, 425]:
            self.capture(value)                           # a soft baseline (a pile of tokens): nothing to compare with
        self.scanner.refocus.assert_not_called()

    def test_not_with_the_cameras_own_autofocus_nor_too_soon_nor_when_switched_off(self):
        s = self.scanner
        base = (1500, 1800, 1400, 1700, 1600, 1500, 1900, 1450)
        s.focus_locked_value = None                       # continuous autofocus: the camera's own business
        for value in base + (400, 400, 400):
            self.capture(value)
        s.refocus.assert_not_called()
        s.focus_locked_value = 430
        s.last_focus_sweep = time.time() - 10             # swept ten seconds ago
        self.capture(400)
        s.refocus.assert_not_called()
        s.last_focus_sweep = time.time() - 120
        s.refocus_on_drop = 0
        self.capture(400)
        s.refocus.assert_not_called()
        s.refocus_on_drop = 0.35
        self.capture(400)                                 # the same cards, allowed: now it does
        s.refocus.assert_called_once()


if __name__ == '__main__':
    unittest.main()
