"""
A webcam whose focus control crashes it (station/scanner.py: _focus_reset_the_camera): the first
"Protocol error" on a lens position is remembered for that camera and its focus is left alone from
then on; a saved focus lock that does not fit the camera's range is not applied. No camera is
opened: v4l2-ctl and OpenCV are replaced, the quirks file is a temporary one.

    venv/bin/python -m unittest discover tests -p test_camera_quirks.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

from card_scanner.config import Config  # noqa: E402
from card_scanner.station import scanner as scanner_module  # noqa: E402
from card_scanner.station.station_client import ServerSettings  # noqa: E402

IDENTITY = '208e:2cc9 USB 2.0 Camera'


class FakeCamera:
    def isOpened(self):
        return True

    def set(self, *_args):
        return True

    def read(self):
        return True, np.zeros((480, 640, 3), np.uint8)

    def release(self):
        pass


class Quirks(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        patch = mock.patch.object(Config, 'DATA_DIR', Path(self.folder.name))
        patch.start()
        self.addCleanup(patch.stop)
        self.logs = []
        # A bare scanner: building a CardScanner would look for cameras (and this machine has some)
        self.scanner = scanner_module.CardScanner.__new__(scanner_module.CardScanner)
        self.scanner.log_callback = lambda message, level='info': self.logs.append(message)
        self.scanner.settings = ServerSettings({'focus_value': 440}, lambda *_: None)
        self.scanner.camera = None
        self.scanner.focus_range = None
        self.scanner.focus_locked_value = 440
        self.scanner.focus_writes_blocked = False
        # the camera is "the LX one" - until it resets: then it is gone from /sys, as on the real one -
        # and v4l2-ctl is a recorded stand-in
        self.reset = False
        self.scanner._camera_identity = lambda: None if self.reset else IDENTITY
        self.scanner._v4l2_ctl = lambda: '/usr/bin/v4l2-ctl'
        self.commands = []

    def v4l2(self, answers):
        """Replace subprocess.run: answers maps a text in the command to (returncode, stderr)"""
        def run(command, **_kwargs):
            self.commands.append(' '.join(command))
            for text, (code, error) in answers.items():
                if text in ' '.join(command):
                    self.reset = 'Protocol error' in error
                    return SimpleNamespace(returncode=code, stderr=error, stdout='')
            return SimpleNamespace(returncode=0, stderr='', stdout='')
        return mock.patch.object(scanner_module.subprocess, 'run', run)

    def noted(self):
        file = Path(self.folder.name) / 'camera_quirks.json'
        return json.loads(file.read_text()) if file.exists() else {}

    def test_a_protocol_error_on_the_lens_is_remembered_and_the_lens_is_left_alone(self):
        self.scanner.focus_range = (0, 15)
        self.scanner.focus_locked_value = 10
        with self.v4l2({'focus_absolute': (1, 'focus_absolute: Protocol error')}):
            self.assertFalse(self.scanner._run_v4l2_command('-c', 'focus_absolute=10'))
            self.assertEqual((self.scanner.focus_range, self.scanner.focus_locked_value), (None, None))
            self.assertIn(IDENTITY, self.noted()['focus_resets_camera'])
            before = len(self.commands)
            self.assertFalse(self.scanner._run_v4l2_command('-c', 'focus_absolute=11'))   # not even tried
            self.assertEqual(len(self.commands), before)
            self.assertTrue(self.scanner._run_v4l2_command('-c', 'sharpness=50'))          # other controls still are

    def test_other_errors_are_not_taken_for_a_crash(self):
        self.scanner.focus_range = (0, 15)
        with self.v4l2({'focus_absolute': (1, 'VIDIOC_S_EXT_CTRLS: failed: Permission denied')}):
            self.assertFalse(self.scanner._run_v4l2_command('-c', 'focus_absolute=10'))
        self.assertEqual((self.scanner.focus_range, self.noted()), ((0, 15), {}))

    def test_a_camera_known_to_reset_has_no_focus_control_after_a_restart(self):
        (Path(self.folder.name) / 'camera_quirks.json').write_text(json.dumps({'focus_resets_camera': {IDENTITY: 'then'}}))
        with self.v4l2({}):
            self.assertIsNone(self.scanner._query_focus_range())
        self.assertEqual(self.commands, [])                                  # it was not even asked
        # ... and another camera is not affected
        self.scanner._camera_identity = lambda: '291a:3369 Anker PowerConf C200'
        with self.v4l2({'--list-ctrls': (0, '')}):
            self.scanner._query_focus_range()
        self.assertEqual(len(self.commands), 1)

    def test_a_saved_lock_outside_the_cameras_range_is_not_applied(self):
        listing = ('focus_absolute 0x009a090a (int)    : min=0 max=15 step=1 default=0 value=0 flags=inactive\n')
        self.scanner.focus_locked_value = 440           # the Anker's position, saved for the station
        with mock.patch.object(scanner_module.cv2, 'VideoCapture', return_value=FakeCamera()), \
                mock.patch.object(scanner_module.time, 'sleep'), \
                mock.patch.object(self.scanner, '_usb_camera_present', return_value=True), \
                mock.patch.object(scanner_module.subprocess, 'run', lambda command, **kw: (
                    self.commands.append(' '.join(command)),
                    SimpleNamespace(returncode=0, stderr='', stdout=listing if '--list-ctrls' in command else ''))[1]):
            self.scanner._initialize_usb_camera()
        self.assertEqual(self.scanner.focus_range, (0, 15))
        self.assertIsNone(self.scanner.focus_locked_value)
        self.assertFalse([c for c in self.commands if 'focus_absolute=' in c])   # the lens was never set
        self.assertTrue([c for c in self.commands if 'focus_automatic_continuous=1' in c])
        self.assertTrue(any('outside this camera' in message for message in self.logs))


if __name__ == '__main__':
    unittest.main()
