#!/usr/bin/env python3
"""
camera.usb_index "auto": the first USB camera that is not a laptop's own (config.py:
auto_camera_index), read from /sys without opening anything.

Run:  venv/bin/python tests/test_camera_auto.py        (or with the others: -m unittest discover tests)
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from card_scanner import config  # noqa: E402


class FakeSys:
    """A /sys/class/video4linux with cameras: add(number, name, usb=True, index='0')"""

    def __init__(self, root):
        self.root = Path(root)
        (self.root / 'devices').mkdir()

    def add(self, number, name, usb=True, index='0'):
        node = self.root / f'video{number}'
        node.mkdir()
        (node / 'index').write_text(index + '\n')
        (node / 'name').write_text(name + '\n')
        parent = self.root / 'devices' / (f'usb{number}' if usb else f'platform{number}')
        parent.mkdir()
        (node / 'device').symlink_to(parent)


class AutoCamera(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.sys = FakeSys(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def pick(self):
        return config.auto_camera_index(self.dir.name)

    def test_none_without_a_camera(self):
        self.assertIsNone(self.pick())

    def test_a_webcam_before_the_laptops_own(self):
        self.sys.add(0, 'Integrated Camera: Integrated C')
        self.sys.add(1, 'Integrated Camera: Integrated C', index='1')
        self.sys.add(2, 'HD Pro Webcam C920')
        self.assertEqual(self.pick(), 2)

    def test_the_laptops_own_when_it_is_the_only_one(self):
        self.sys.add(0, 'Integrated Camera')
        self.assertEqual(self.pick(), 0)

    def test_what_is_not_usb_is_not_a_camera(self):
        self.sys.add(0, 'bcm2835-codec-decode', usb=False)
        self.sys.add(3, 'Logitech BRIO')
        self.assertEqual(self.pick(), 3)
        self.assertEqual(sorted(p.name for p in Path(self.dir.name).glob('video*')), ['video0', 'video3'])

    def test_the_second_node_of_a_webcam_is_skipped(self):
        self.sys.add(4, 'Webcam', index='1')
        self.assertIsNone(self.pick())

    def test_numbers_are_compared_as_numbers(self):
        self.sys.add(10, 'Cam B')
        self.sys.add(2, 'Cam A')
        self.assertEqual(self.pick(), 2)

    def test_auto_resolves_when_asked_and_no_camera_is_not_a_crash(self):
        from unittest import mock
        with mock.patch.object(config, 'auto_camera_index', return_value=None):
            self.assertIsNone(config.camera_index_or_none('auto'))   # the scanner waits and asks again
            with self.assertRaises(ValueError):
                config.camera_index_from('auto')
        with mock.patch.object(config, 'auto_camera_index', return_value=6):
            self.assertEqual(config.camera_index_from('auto'), 6)
            self.assertEqual(config.camera_index_or_none('AUTO'), 6)


if __name__ == '__main__':
    unittest.main()
