"""
The focus check counts captures since auto scanning was started (station/scanner.py:
auto_capture_enabled, _count_capture): stopping and starting again begins a new count, the focus
itself is not touched, and a capture still counts one whichever way it was made.

    venv/bin/python -m unittest discover tests -p test_focus_count.py
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

from card_scanner.station.scanner import CardScanner  # noqa: E402


class FocusCount(unittest.TestCase):
    def setUp(self):
        self.scanner = CardScanner.__new__(CardScanner)
        self.scanner._auto_capture_enabled = False
        self.scanner.captures_since_focus = 0
        self.scanner.refocus_every = 20
        self.scanner.focus_locked_value = 440
        self.scanner.camera_type = 'usb'
        self.scanner.focus_range = (0, 1000)
        self.scanner.focus_sweep_running = False
        self.scanner.focus_probe_running = False

    def test_starting_auto_scanning_starts_the_count_again(self):
        self.scanner.auto_capture_enabled = True
        with mock.patch('threading.Thread') as thread:
            for _ in range(12):
                self.scanner._count_capture()
            self.scanner.auto_capture_enabled = False
            self.assertEqual(self.scanner.captures_since_focus, 12)       # stopping keeps it (nothing is lost)
            self.scanner.auto_capture_enabled = True
            self.assertEqual(self.scanner.captures_since_focus, 0)        # starting begins again
            for _ in range(19):
                self.scanner._count_capture()
            thread.assert_not_called()                                   # 19 cards: no check yet
            self.scanner._count_capture()
            self.assertEqual(thread.call_count, 1)                       # the 20th: the check

    def test_setting_it_to_the_same_value_does_not_reset(self):
        self.scanner.auto_capture_enabled = True
        self.scanner.captures_since_focus = 7
        self.scanner.auto_capture_enabled = True                         # a repeated message from the server
        self.assertEqual(self.scanner.captures_since_focus, 7)

    def test_the_focus_is_not_touched(self):
        self.scanner.auto_capture_enabled = True
        self.scanner.auto_capture_enabled = False
        self.scanner.auto_capture_enabled = True
        self.assertEqual(self.scanner.focus_locked_value, 440)


if __name__ == '__main__':
    unittest.main()
