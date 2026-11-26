"""
The focus check before a scanning run's first capture (station/scanner.py: _start_focus_check, _run_focus_check), and the strip's foil marker kept for a card the OCR
named but did not confirm (core/identification.py: _foil_of_unconfirmed_read; core/printing_check.py: finish_named_by_strip). Stand-ins only: no camera, OCR or data.

    venv/bin/python -m unittest discover tests -p test_first_card_focus.py
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

from card_scanner.core import identification, printing_check  # noqa: E402
from card_scanner.core.reading import capture as reading_capture  # noqa: E402
from card_scanner.station.scanner import CardScanner  # noqa: E402


class Lens(unittest.TestCase):
    """A scanner with a lens whose sharpness depends on where it is: `peak` is the sharpest position"""

    def setUp(self):
        s = self.scanner = CardScanner.__new__(CardScanner)
        s._auto_capture_enabled = False
        s.focus_check_due = False
        s.captures_since_focus = 5
        s.refocus_every = 10
        s.focus_locked_value = 400
        s.camera_type = 'usb'
        s.focus_range = (0, 1000)
        s.focus_sweep_running = s.focus_probe_running = s.disturbed_during_focus = s.card_under_review = s.awaiting_new_card = False
        s.capture_pending = False
        s.focus_probe_direction = 1
        s.last_focus_sweep = s.last_focus_move = 0.0
        s.stable_frames = 6
        s.required_stable_frames = 6
        s.settings = mock.Mock()
        s.log = mock.Mock()
        s.auto_capture_callback = mock.Mock()
        s.previous_thumbnail = s.area_thumb = None
        self.position, self.peak = 400, 410
        s._move_focus = mock.Mock(side_effect=lambda position, **kw: setattr(self, 'position', position))
        s._measure_focus_sharpness = lambda: 1000.0 / (1 + abs(self.position - self.peak))
        patcher = mock.patch('card_scanner.station.scanner.time.sleep')
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self):
        self.scanner.focus_probe_running = True
        self.scanner._run_focus_check()

    def test_starting_a_run_holds_the_first_capture_for_the_check(self):
        s = self.scanner
        s.auto_capture_enabled = True
        with mock.patch('card_scanner.station.scanner.threading.Thread') as thread:
            s._trigger_auto_capture()
            self.assertEqual(thread.call_args.kwargs['target'], s._run_focus_check)
            self.assertTrue(s.focus_probe_running)                  # the capture loop's condition: no capture until it ends
            self.assertFalse(s.awaiting_new_card)                   # the card is still the one to capture
            self.assertFalse(s.capture_pending)
            self.assertEqual(s.stable_frames, 0)
            s.focus_probe_running = False
            s.stable_frames = 6
            s._trigger_auto_capture()                               # once per run: now it captures
            self.assertEqual(thread.call_args.kwargs['target'], s.auto_capture_callback)
        self.assertTrue(s.awaiting_new_card)

    def test_stopping_withdraws_it_and_a_new_run_asks_again(self):
        s = self.scanner
        s.auto_capture_enabled = True
        s.auto_capture_enabled = False
        self.assertFalse(s.focus_check_due)
        s.auto_capture_enabled = True
        self.assertTrue(s.focus_check_due)

    def test_no_check_where_it_cannot_or_should_not_run(self):
        for change in ({'refocus_every': 0}, {'focus_locked_value': None}, {'camera_type': 'picamera'}, {'focus_range': None},
                       {'focus_sweep_running': True}):
            self.setUp()
            s = self.scanner
            s.focus_check_due = True
            for key, value in change.items():
                setattr(s, key, value)
            self.assertFalse(s._start_focus_check(), change)
            self.assertFalse(s.focus_check_due)

    def test_no_check_right_after_a_full_sweep(self):
        s = self.scanner
        s.focus_check_due = True
        s.last_focus_sweep = printing_check.time.time()
        self.assertFalse(s._start_focus_check())

    def test_it_finds_the_better_position_upwards(self):
        self.check()
        self.assertEqual((self.scanner.focus_locked_value, self.position), (410, 410))
        self.scanner.settings.set.assert_called_once_with('focus_value', 410)
        self.assertFalse(self.scanner.focus_probe_running)
        self.assertEqual(self.scanner.stable_frames, 0)

    def test_it_also_tries_the_other_way(self):
        # What a one-direction probe misses: the sharper side is the one it would not have tried
        self.peak = 390
        self.check()
        self.assertEqual((self.scanner.focus_locked_value, self.position, self.scanner.focus_probe_direction), (390, 390, -1))

    def test_it_follows_an_improvement_but_only_so_far(self):
        self.peak = 460
        self.check()
        self.assertEqual((self.scanner.focus_locked_value, self.position), (430, 430))     # three steps

    def test_a_sharp_lens_stays_where_it_is(self):
        self.peak = 400
        self.check()
        self.assertEqual((self.scanner.focus_locked_value, self.position), (400, 400))
        self.scanner.settings.set.assert_not_called()

    def test_a_card_moved_during_the_check_changes_nothing(self):
        s = self.scanner
        moved = mock.Mock(side_effect=lambda position, **kw: (setattr(self, 'position', position), setattr(s, 'disturbed_during_focus', True)))
        s._move_focus = moved
        self.check()
        self.assertEqual((s.focus_locked_value, self.position), (400, 400))
        s.settings.set.assert_not_called()

    def test_an_error_frees_the_flag_and_puts_the_lens_back(self):
        s = self.scanner
        real = s._measure_focus_sharpness
        calls = []

        def fails(*a):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('camera gone')
            return real()
        s._measure_focus_sharpness = fails
        self.check()
        self.assertFalse(s.focus_probe_running)
        self.assertEqual((s.focus_locked_value, self.position), (400, 400))


LTR = {'id': 'x', 'name': 'Galadhrim Bow', 'flavor_name': None, 'finishes': ['nonfoil', 'foil'], 'released_at': '2023-06-23', 'frame': '2015'}
OLD = {**LTR, 'released_at': '2010-01-01', 'frame': '2003'}


class StripFinish(unittest.TestCase):
    def setUp(self):
        self.check = printing_check.PrintingCheck.__new__(printing_check.PrintingCheck)
        self.card = LTR
        self.check.database = mock.Mock(get_card_by_set_number=lambda set_code, number, exact=False: self.card)

    def finish(self, name='Galadhrim Bow', **strip):
        return self.check.finish_named_by_strip({'number': '167', 'set': 'ltr', 'symbol': 'unknown', 'net': None, **strip}, name)

    def test_the_networks_sure_answer_is_kept(self):
        self.assertEqual(self.finish(net=0.0005), 'non-foil')             # capture 48 of the run of 2026-11-23
        self.assertEqual(self.finish(net=0.9995), 'foil')

    def test_a_symbol_read_stands(self):
        self.assertEqual(self.finish(symbol='foil', net=0.97), 'foil')
        self.assertEqual(self.finish(symbol='foil', net=0.5), 'foil')
        self.assertEqual(self.finish(symbol='foil'), 'foil')

    def test_a_symbol_against_a_sure_network_says_nothing(self):
        # (since 2026-11-25: the OCR reads a foil's star as a dot now and then - decide_finish's conflict)
        self.assertEqual(self.finish(symbol='foil', net=0.0), 'unknown')
        self.assertEqual(self.finish(symbol='non-foil', net=0.99), 'unknown')

    def test_a_modern_frame_printing_from_before_the_lord_of_the_rings_says_its_marker_too(self):
        self.card = {**LTR, 'released_at': '2019-07-12'}
        self.assertEqual(self.finish(symbol='non-foil'), 'non-foil')
        self.card = {**LTR, 'released_at': '2011-09-30', 'frame': '2003'}
        self.assertEqual(self.finish(symbol='non-foil'), 'unknown')

    def test_an_unsure_network_says_nothing(self):
        self.assertEqual(self.finish(net=0.28), 'unknown')                # capture 46
        self.assertEqual(self.finish(), 'unknown')

    def test_only_a_whole_line(self):
        self.assertEqual(self.finish(net=0.0005, number=''), 'unknown')
        self.assertEqual(self.finish(net=0.0005, set=''), 'unknown')

    def test_not_for_a_printing_of_another_name(self):
        self.assertEqual(self.finish(name='Shire Terrace', net=0.0005), 'unknown')
        self.assertEqual(self.finish(name='', net=0.0005), 'unknown')

    def test_not_for_a_printing_that_prints_no_marker(self):
        self.card = OLD
        self.assertEqual(self.finish(net=0.0005), 'unknown')

    def test_not_for_a_line_the_card_data_lacks_or_cannot_look_up(self):
        self.card = None
        self.assertEqual(self.finish(net=0.0005), 'unknown')
        self.check.database = mock.Mock(get_card_by_set_number=mock.Mock(side_effect=RuntimeError('locked')))
        self.assertEqual(self.finish(net=0.0005), 'unknown')


class InIdentification(unittest.TestCase):
    def setUp(self):
        self.ident = identification.Identification.__new__(identification.Identification)
        self.ident.log_callback = None
        self.ident.evidence = mock.Mock(finish_named_by_strip=mock.Mock(return_value='non-foil'))
        self.picture = object()
        self.strip = {'number': '167', 'set': 'ltr', 'symbol': 'unknown', 'net': 0.0005}
        self.read = {'name': 'Galadhrim Bow', 'foil': 'unknown'}

    def foil(self, keep=True):
        capture = reading_capture.CaptureEvidence()
        with reading_capture.using(capture):
            if keep:
                reading_capture.remember('strip', self.picture, self.strip)
            return self.ident._foil_of_unconfirmed_read(object(), self.picture, self.read)

    def test_the_strip_of_this_capture_is_used(self):
        self.assertEqual(self.foil(), 'non-foil')
        self.ident.evidence.finish_named_by_strip.assert_called_once_with(self.strip, 'Galadhrim Bow')

    def test_without_a_strip_nothing_is_read_again(self):
        self.assertEqual(self.foil(keep=False), 'unknown')
        self.ident.evidence.finish_named_by_strip.assert_not_called()

    def test_without_the_printing_check(self):
        self.ident.evidence = None
        self.assertEqual(self.foil(), 'unknown')

    def test_a_failure_never_loses_the_card(self):
        self.ident.evidence.finish_named_by_strip.side_effect = RuntimeError('no')
        self.assertEqual(self.foil(), 'unknown')


if __name__ == '__main__':
    unittest.main()
