"""
A capture is never lost without a word, and a failure never leaves the station stuck
(station/scanner.py: capture_card_image_only, _run_focus_probe, _serve_release_request;
station/station_client.py: auto_captured). From the outside review of 2026-10-16.

    venv/bin/python -m unittest discover tests -p test_capture_failures.py
"""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

from card_scanner.config import Config  # noqa: E402
from card_scanner.station import scanner as scanner_module  # noqa: E402
from card_scanner.station import station_client  # noqa: E402
from card_scanner.station.scanner import CardScanner  # noqa: E402


def bare_scanner():
    s = CardScanner.__new__(CardScanner)
    s.debug_trace_enabled = False
    s.stable_frames, s.required_stable_frames = 6, 6
    s.last_capture_frame = None
    s.last_new_card = None
    s._previous_capture_at = None
    s.log = mock.Mock()
    s._mark_captured = mock.Mock()
    s._count_capture = mock.Mock()
    s.get_detected_card = mock.Mock(return_value=(np.zeros((120, 80, 3), np.uint8), 'a card', None))
    return s


class DiskFull(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        patch = mock.patch.object(Config, 'STATION_CAPTURES_DIR', Path(self.folder.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_failed_write_is_not_a_capture(self):
        s = bare_scanner()
        with mock.patch.object(scanner_module.cv2, 'imwrite', return_value=False):
            result = s.capture_card_image_only(1, settle=0)
        self.assertEqual(result, (None, None, None))
        messages = [call.args[0] for call in s.log.call_args_list]
        self.assertTrue(any('could not be saved' in m for m in messages), messages)
        self.assertFalse(any('Card captured' in m for m in messages), messages)   # no false "captured"

    def test_a_good_write_still_works(self):
        s = bare_scanner()
        path, image, _ = s.capture_card_image_only(1, settle=0)
        self.assertTrue(path.is_file())


class StationLoop(unittest.TestCase):
    def station(self, scanner):
        st = station_client.Station.__new__(station_client.Station)
        st.scanner = scanner
        st.capture_number = 0
        st.send_log = mock.Mock()
        st.emit = mock.Mock()
        st.queue_upload = mock.Mock()
        return st

    def scanner(self):
        s = mock.Mock()
        s.is_card_detected.return_value = True
        s.capture_pending = True
        s.card_under_review = True
        s.focus_probe_running = False
        s.fast_scan_mode = True
        s.last_auto_capture_time = 0
        return s

    def test_an_error_in_the_capture_frees_the_scanner(self):
        s = self.scanner()
        s.capture_card_image_only.side_effect = RuntimeError('the frame is gone')
        st = self.station(s)
        st.auto_captured()                                # must not raise: it runs in its own thread
        self.assertFalse(s.capture_pending)
        self.assertFalse(s.card_under_review)
        self.assertIn('failed', st.send_log.call_args.args[0])
        st.queue_upload.assert_not_called()

    def test_a_failed_save_tells_the_person_and_frees_the_scanner(self):
        s = self.scanner()
        s.capture_card_image_only.return_value = (None, None, None)
        st = self.station(s)
        st.auto_captured()
        self.assertFalse(s.capture_pending)
        self.assertFalse(s.card_under_review)
        self.assertIn('not captured', st.send_log.call_args.args[0])
        st.emit.assert_not_called()                       # no beep for a card that was not taken

    def test_a_good_capture_beeps_and_queues_the_upload(self):
        s = self.scanner()
        s.capture_card_image_only.return_value = (Path('card.jpg'), np.zeros((2, 2, 3), np.uint8), None)
        st = self.station(s)
        st.auto_captured()
        st.emit.assert_called_once_with('captured', {'number': 1})
        st.queue_upload.assert_called_once()
        self.assertFalse(s.capture_pending)


class FocusAndCameraRequests(unittest.TestCase):
    def test_the_probe_frees_its_flag_whatever_goes_wrong_first(self):
        s = bare_scanner()
        s.focus_range = None                              # unpacking this raises TypeError before the old try
        s.focus_locked_value = 430
        s.focus_probe_direction = 1
        s.focus_probe_running = True
        s.last_focus_move = 0
        s.log = mock.Mock()
        s._run_focus_probe()
        self.assertFalse(s.focus_probe_running)

    def test_a_request_for_the_camera_is_always_answered(self):
        s = bare_scanner()
        s._close_camera = mock.Mock(side_effect=OSError('busy'))   # failed outside the old try
        s._open_camera = mock.Mock()
        done = threading.Event()
        s.release_request = {'action': lambda: 'x', 'done': done, 'result': None, 'error': None}
        s._serve_release_request()
        self.assertTrue(done.is_set())                    # the caller is not left waiting
        self.assertIsInstance(s.release_request, type(None))
        s._open_camera.assert_called_once()               # and scanning goes on

    def test_a_camera_that_will_not_reopen_does_not_end_the_thread(self):
        s = bare_scanner()
        s._close_camera = mock.Mock()
        s._open_camera = mock.Mock(side_effect=RuntimeError('gone'))
        done = threading.Event()
        s.release_request = {'action': lambda: 'ok', 'done': done, 'result': None, 'error': None}
        s._serve_release_request()                        # raises nothing
        self.assertTrue(done.is_set())


if __name__ == '__main__':
    unittest.main()
