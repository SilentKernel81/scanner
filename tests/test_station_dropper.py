"""
The card dropper (station/dropper.py: checked, Dropper; station/station_client.py: when a card is released; the server's
settings_events: set_dropper / test_dropper; core/remote_scanner.py: the setting saved and sent). A stand-in for the GPIO pin,
the camera and the server's station - no GPIO, no camera, no network is touched.

    venv/bin/python -m unittest discover tests -p test_station_dropper.py
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from card_scanner.station import dropper as dropper_module  # noqa: E402
from card_scanner.station.dropper import DEFAULTS, Dropper, checked  # noqa: E402
from card_scanner.station.station_client import Station  # noqa: E402

ON = {'enabled': True, 'pin': 17, 'pulse_ms': 150}


class FakePin:
    """The GPIO line: what was set, in order, and whether it was let go"""
    opened = []

    def __init__(self, chip, pin, active_low):
        self.chip, self.pin, self.active_low = chip, pin, active_low
        self.values = []
        self.closed = False
        FakePin.opened.append(self)

    def set(self, active):
        self.values.append(active)

    def close(self):
        self.closed = True


def a_dropper(config=ON, open_pin=None):
    FakePin.opened = []
    slept = []
    dropper = Dropper(open_pin=open_pin or FakePin, sleep=slept.append)
    dropper.configure(config)
    return dropper, slept


class TheSettings(unittest.TestCase):
    """checked: the one place the rules are written - the server before saving, the station before using"""

    def test_never_set_is_off(self):
        self.assertEqual(checked(None), DEFAULTS)
        self.assertFalse(checked(None)['enabled'])

    def test_a_good_set_keeps_its_values_and_fills_the_rest(self):
        self.assertEqual(checked({'enabled': True, 'pin': 17}), dict(DEFAULTS, enabled=True, pin=17))

    def test_wrong_values_are_refused_by_name(self):
        for wrong, says in (({'pin': True}, 'pin'),                    # a switch's value is not GPIO 1
                            ({'pin': 64}, 'pin'), ({'pin': -1}, 'pin'), ({'pin': '17'}, 'pin'),
                            ({'pulse_ms': 5}, 'pulse'), ({'pulse_ms': 5000}, 'pulse'), ({'pulse_ms': 150.5}, 'pulse'),
                            ({'chip': '/etc/passwd'}, 'chip'), ({'chip': '/dev/gpiochip0/../x'}, 'chip'), ({'chip': '/dev/gpiochip'}, 'chip'),
                            ({'enabled': 'yes'}, 'enabled'), ({'active_low': 1}, 'active_low'),
                            ({'enabled': True}, 'pin'),                  # on without a pin
                            ({'speed': 3}, 'Unknown'), (['enabled'], 'settings')):
            with self.assertRaises(ValueError, msg=wrong) as caught:
                checked(wrong)
            self.assertIn(says.lower(), str(caught.exception).lower(), wrong)


class OnePulse(unittest.TestCase):
    """Dropper.pulse: the pin active for the pulse, then inactive - one card"""

    def test_a_pulse_sets_the_pin_active_for_its_length_then_inactive(self):
        dropper, slept = a_dropper(dict(ON, pulse_ms=250))
        self.assertTrue(dropper.pulse('test'))
        pin = FakePin.opened[0]
        self.assertEqual((pin.chip, pin.pin, pin.active_low), ('/dev/gpiochip0', 17, False))
        self.assertEqual(pin.values, [True, False])
        self.assertEqual(slept, [0.25])
        self.assertEqual(dropper.pulses, 1)

    def test_off_nothing_is_opened_or_pulsed(self):
        dropper, _ = a_dropper(None)
        self.assertFalse(dropper.pulse('test'))
        self.assertEqual(FakePin.opened, [])

    def test_the_pin_is_opened_once_and_let_go_when_it_changes_or_the_dropper_goes_off(self):
        dropper, _ = a_dropper()
        dropper.pulse('one')
        dropper.pulse('two')
        self.assertEqual(len(FakePin.opened), 1)
        first = FakePin.opened[0]
        dropper.configure(dict(ON, pulse_ms=300))           # the same pin: kept
        self.assertFalse(first.closed)
        dropper.configure(dict(ON, pin=27))                 # another pin: the first let go
        self.assertTrue(first.closed)
        dropper.pulse('three')
        self.assertEqual(FakePin.opened[-1].pin, 27)
        dropper.configure({'enabled': False, 'pin': 27})
        self.assertTrue(FakePin.opened[-1].closed)

    def test_wrong_settings_leave_the_old_ones(self):
        dropper, _ = a_dropper()
        with self.assertRaises(ValueError):
            dropper.configure({'enabled': True, 'pin': 99})
        self.assertEqual(dropper.config['pin'], 17)

    def test_a_pin_that_cannot_be_opened_is_an_error_said_once_never_an_exception(self):
        def refuse(chip, pin, active_low):
            raise RuntimeError('/dev/gpiochip0 does not exist on this machine')
        dropper, _ = a_dropper(open_pin=refuse)
        with self.assertLogs('station', level='ERROR') as logged:
            self.assertFalse(dropper.pulse('one'))
            self.assertFalse(dropper.pulse('two'))
        self.assertEqual(len(logged.records), 1)            # said once, not at every card
        self.assertIn('does not exist', dropper.error)
        self.assertEqual(dropper.pulses, 0)

    def test_a_pulse_that_fails_half_way_still_sets_the_pin_inactive(self):
        class Stuck(FakePin):
            def set(self, active):
                self.values.append(active)
                if active:
                    raise OSError('the line went away')
        dropper, _ = a_dropper(open_pin=Stuck)
        with self.assertLogs('station', level='ERROR'):
            self.assertFalse(dropper.pulse('one'))
        self.assertEqual(FakePin.opened[0].values, [True, False])   # inactive again: never left holding the mechanism open
        self.assertTrue(FakePin.opened[0].closed)

    def test_a_second_pulse_while_one_is_under_way_is_refused_not_queued(self):
        release, started = threading.Event(), threading.Event()

        def slow_sleep(_seconds):
            started.set()
            release.wait(5)
        FakePin.opened = []
        dropper = Dropper(open_pin=FakePin, sleep=slow_sleep)
        dropper.configure(ON)
        first = threading.Thread(target=dropper.pulse, args=('first',))
        first.start()
        self.assertTrue(started.wait(5))
        with self.assertLogs('station', level='WARNING'):
            self.assertFalse(dropper.pulse('second'))
        release.set()
        first.join(5)
        self.assertEqual(dropper.pulses, 1)

    def test_switched_off_between_the_check_and_the_lock_nothing_is_activated(self):
        # A Codex review of 2026-10-22, its interleaving: pulse() looked at `enabled` before taking the lock configure() uses - switched
        # off in between, the pin was opened and activated all the same. Here the switch-off is made exactly in that gap
        dropper, _ = a_dropper()
        real_lock = dropper._lock

        class GapLock:
            """The dropper's lock, with the switch-off made the moment pulse() comes to take it (after its first check)"""
            switched = False

            def acquire(self, blocking=True):
                if not GapLock.switched:
                    GapLock.switched = True
                    dropper.configure({'enabled': False, 'pin': 17})     # takes the real lock: pulse() has not got it yet
                return real_lock.acquire(blocking)

            def release(self):
                real_lock.release()

            def __enter__(self):
                real_lock.acquire()
                return self

            def __exit__(self, *exc):
                real_lock.release()
        dropper._lock = GapLock()
        self.assertFalse(dropper.pulse('the test button'))
        self.assertTrue(GapLock.switched)
        self.assertEqual(FakePin.opened, [])          # nothing opened, nothing activated
        self.assertEqual(dropper.pulses, 0)

    def test_the_real_pin_without_the_library_says_so(self):
        # A PC station: gpiod is not there - the dropper reports why, the station goes on
        with mock.patch.dict(sys.modules, {'gpiod': None, 'gpiod.line': None}):
            with self.assertRaises(RuntimeError) as caught:
                dropper_module.GpiodPin('/dev/gpiochip0', 17, False)
        self.assertIn('not installed', str(caught.exception))


class FakeScanner:
    """What the station reads of its CardScanner"""

    def __init__(self):
        self.auto_capture_enabled = False
        self.card_under_review = False
        self.capture_pending = False
        self.focus_sweep_running = False
        self.focus_probe_running = False
        self.fast_scan_mode = True
        self.camera = object()
        self.card_in_view = False          # what is_card_detected answers: a capture sets it, as a card lies there
        self.awaiting_new_card = False     # True once the card in view was captured (CardScanner._mark_captured)
        self.last_auto_capture_time = time.time()
        self.last_capture_frame = None
        # What Station.state() reports with every command's answer
        self.camera_error, self.camera_type, self.rotation, self.refocus_every, self.refocus_on_drop = None, 'usb', 0, 10, 0.35
        self.fixed_area, self.fixed_area_enabled, self.focus_locked_value, self.enable_detection = None, False, None, True

    def is_card_detected(self):
        return self.card_in_view

    def capture_card_image_only(self, number, settle=0):
        return f'/tmp/capture-{number}.jpg', None, None


class WhenACardIsReleased(unittest.TestCase):
    """station_client.Station: after the capture beep, after the review's answer, at the start of scanning - and never more"""

    def setUp(self):
        self.station = Station('http://server.test:5000', 'test-station', 'Test', '')   # not connected: emit and send_log do nothing
        self.scanner = FakeScanner()
        self.station.scanner = self.scanner
        FakePin.opened = []
        self.station.dropper = Dropper(open_pin=FakePin, sleep=lambda _s: None)
        self.station.dropper.configure(ON)
        self.station.dropper_synced = True     # as after its hello: connected and in step with the server
        self.station.HELLO_RETRY_DELAYS = (0.01,)
        # The uploads queued, with the id each capture was given (the card owed with auto-add off is owed for that id); a test may
        # hook in at the moment the upload is queued (on_queued) - the upload thread could send it, and the server answer, right then
        self.ids = []
        self.on_queued = None

        def queued(*_args, capture_id=None, **_kwargs):
            self.ids.append(capture_id)
            if self.on_queued:
                self.on_queued(capture_id)
        self.station.queue_upload = mock.Mock(side_effect=queued)

    def released(self, wait=3.0):
        """The cards released once the background releases have had their turn"""
        deadline = time.time() + wait
        time.sleep(0.02)
        while time.time() < deadline and self.station.release_busy:
            time.sleep(0.01)
        return self.station.dropper.pulses

    def capture(self, fast):
        self.scanner.auto_capture_enabled = True
        self.scanner.fast_scan_mode = fast
        self.scanner.card_under_review = True          # as _trigger_auto_capture leaves it
        self.scanner.card_in_view = self.scanner.awaiting_new_card = True     # the card lies there, marked captured
        self.station._auto_captured(self.scanner)

    def answer(self, status='pending'):
        """The server answers the last queued upload (accepted) with this status - what send_until_settled hands to after_upload"""
        call = self.station.queue_upload.call_args
        item = {'image': Path(call.args[0]), 'release_session': call.kwargs.get('release_session')}
        self.station.after_upload(item, status)

    def test_auto_add_on_the_next_card_comes_once_the_server_answered_the_upload(self):
        self.capture(fast=True)
        self.assertEqual(self.released(), 0)              # queued is not enough: the card-back check is the server's
        self.answer('pending')
        self.assertEqual(self.released(), 1)
        self.assertFalse(self.station.drop_owed)

    def test_a_card_back_releases_nothing(self):
        # A Codex review of 2026-10-22: released at queue time, the dropper buried the face-down card meant to stop the stack
        self.capture(fast=True)
        self.answer('card_back')
        self.assertEqual(self.released(), 0)

    def test_auto_add_off_the_next_card_waits_for_add_or_skip_of_that_capture(self):
        self.capture(fast=False)
        self.assertEqual(self.released(), 0)
        self.assertEqual(self.station.drop_owed[0], self.ids[0])     # owed for the id the upload carries
        self.station.on_review_done({'capture_id': 'another'})       # another capture (a manual one, an older one): nothing
        self.assertEqual(self.released(), 0)
        self.station.on_review_done({'capture_id': self.ids[0]})     # the page's Add or Skip of this one, through the server
        self.assertEqual(self.released(), 1)
        self.station.on_review_done({'capture_id': self.ids[0]})     # said again: nothing more
        self.assertEqual(self.released(), 1)

    def test_the_review_flag_cleared_by_an_error_or_a_stop_releases_nothing(self):
        # The server clears card_under_review when reading the card fails, when adding it fails, when the game changes...: none of
        # that is the person's Add or Skip (a Codex review of 2026-10-22)
        self.capture(fast=False)
        self.station.on_set({'card_under_review': False})
        self.assertEqual(self.released(), 0)
        self.assertEqual(self.station.drop_owed[0], self.ids[0])     # still owed: the real Add / Skip pays it

    def test_starting_auto_scanning_releases_the_first_card_and_stopping_forgets_what_is_owed(self):
        self.station.on_set({'auto_capture_enabled': True})
        self.assertEqual(self.released(), 1)
        self.station.on_set({'auto_capture_enabled': True})       # already on: nothing
        self.assertEqual(self.released(), 1)
        self.capture(fast=False)
        self.station.on_set({'auto_capture_enabled': False})
        self.assertIsNone(self.station.drop_owed)
        self.station.on_review_done({'capture_id': self.ids[-1]})     # scanning was stopped: nothing is owed any more
        self.assertEqual(self.released(), 1)

    def test_a_dropper_that_breaks_never_costs_the_card(self):
        # The capture's upload is queued whatever the dropper does, and its part after the answer cannot raise into the upload
        self.capture(fast=True)
        self.station.queue_upload.assert_called_once()
        with mock.patch.object(self.station, 'release_next_card', side_effect=RuntimeError('broken')):
            with self.assertLogs('station', level='ERROR'):
                self.answer('pending')                          # no exception out of after_upload

    def send(self, answers):
        """Send one auto-add capture through the real send_until_settled; answers: what the stand-in network does, in order"""
        self.capture(fast=True)
        call = self.station.queue_upload.call_args
        folder = Path(self.tempdir.name)
        image = folder / 'capture.jpg'
        image.write_bytes(b'jpeg')
        item = {'image': image, 'foil': None, 'mode': 'auto', 'foil_is_image': True, 'capture_id': call.kwargs['capture_id'],
                'release_session': call.kwargs['release_session']}
        replies = iter(answers)

        def post(*_args, **_kwargs):
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return mock.Mock(status_code=reply[0], json=mock.Mock(return_value=reply[1]))
        self.station.remove_capture = mock.Mock()
        self.station.send_until_settled(mock.Mock(post=post), item)

    def test_through_the_real_upload_a_card_back_answer_releases_nothing(self):
        import tempfile
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.send([(200, {'success': True, 'capture': 5, 'status': 'card_back', 'similarity': 0.9})])
        self.assertEqual(self.released(), 0)

    def test_while_the_server_is_away_nothing_is_released_until_it_answers(self):
        import tempfile
        import requests
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        seen = []
        real_release = self.station.release_next_card
        self.station.release_next_card = lambda *a, **k: (seen.append(a), real_release(*a, **k))
        # No answer first (the server away): retried, nothing released meanwhile; then the answer - one card
        self.send([requests.ConnectionError('server away'), (202, {'success': True, 'capture': 6, 'status': 'pending'})])
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.released(), 1)

    def test_a_capture_that_failed_releases_nothing(self):
        self.scanner.capture_card_image_only = lambda number, settle=0: (None, None, None)
        self.capture(fast=True)
        self.assertEqual(self.released(), 0)

    def test_a_release_waits_for_the_focus_to_settle(self):
        self.scanner.auto_capture_enabled = True
        self.scanner.focus_sweep_running = True
        self.station.release_next_card('test')
        time.sleep(0.2)
        self.assertEqual(self.station.dropper.pulses, 0)          # still waiting
        self.scanner.focus_sweep_running = False
        self.assertEqual(self.released(), 1)

    def test_scanning_stopped_or_the_camera_gone_while_waiting_releases_nothing(self):
        for stop in ('auto_capture_enabled', 'camera'):
            self.scanner.auto_capture_enabled = True
            self.scanner.camera = object()
            self.scanner.focus_probe_running = True
            self.station.release_next_card('test')
            setattr(self.scanner, stop, False if stop == 'auto_capture_enabled' else None)
            self.scanner.focus_probe_running = False
            self.assertEqual(self.released(), 0, stop)

    def test_one_release_asked_for_meanwhile_is_kept_a_third_is_refused(self):
        self.scanner.auto_capture_enabled = True
        self.scanner.focus_probe_running = True
        self.station.release_next_card('first')
        self.station.release_next_card('second')                    # kept for after the first
        with self.assertLogs('station', level='WARNING'):
            self.station.release_next_card('third')                 # one card per capture: refused
        self.scanner.focus_probe_running = False
        self.assertEqual(self.released(), 2)

    def test_a_card_captured_during_a_long_pulse_still_gets_its_next_card(self):
        # A Codex review of 2026-10-22: with a pulse of 2 s, the released card can settle and be captured before the pulse ends -
        # its release used to be refused and nothing retried it: the feeding stopped for good
        pulsing, finish = threading.Event(), threading.Event()

        def long_pulse(seconds):
            self.assertEqual(seconds, 2.0)
            pulsing.set()
            finish.wait(5)
        FakePin.opened = []
        self.station.dropper = Dropper(open_pin=FakePin, sleep=long_pulse)
        self.station.dropper.configure(dict(ON, pulse_ms=2000))
        self.station.on_set({'auto_capture_enabled': True})           # the first card
        self.assertTrue(pulsing.wait(5))
        self.capture(fast=True)                                       # ... lands and is captured while the pin is still active
        self.answer('pending')                                        # ... and the server has it
        finish.set()
        self.assertEqual(self.released(), 2)                          # its next card comes after the pulse
        self.assertFalse(self.station.release_busy)

    def test_stopping_and_starting_again_while_a_release_waits_releases_one_card_not_two(self):
        # A Codex review of 2026-10-22, its own reproduction: start during a focus probe (the release waits), stop, start again (the
        # new first card is kept behind the waiting one), the probe ends - the old session's release must not pulse beside the new one
        self.scanner.focus_probe_running = True
        self.station.on_set({'auto_capture_enabled': True})
        time.sleep(0.1)
        self.station.on_set({'auto_capture_enabled': False})
        self.station.on_set({'auto_capture_enabled': True})
        self.scanner.focus_probe_running = False
        self.assertEqual(self.released(), 1)

    def test_a_release_from_before_a_stop_is_refused_even_with_scanning_on_again(self):
        self.scanner.auto_capture_enabled = True
        self.scanner.focus_probe_running = True
        self.station.release_next_card('after capture #1')            # asked for in the first session
        with self.station.release_lock:
            self.station.scan_session += 1                            # stopped and started meanwhile (the start's own card not asked for)
        self.scanner.focus_probe_running = False
        self.assertEqual(self.released(), 0)

    def test_a_stop_and_restart_during_a_capture_never_releases_two_cards(self):
        # A Codex review of 2026-10-22, its reproduction: scanning stopped and started again WHILE a capture is being taken. The old
        # capture must not release a card beside the restart's; and the restart's first card must not drop onto the card being
        # captured. Both are refused: the feeding waits (documented - "Drop one card" goes on), never two cards
        real = self.scanner.capture_card_image_only

        def capture_with_a_restart(number, settle=0):
            self.station.on_set({'auto_capture_enabled': False})
            self.station.on_set({'auto_capture_enabled': True})       # the new session's first card: a capture is under way
            return real(number, settle)
        self.scanner.capture_card_image_only = capture_with_a_restart
        self.scanner.auto_capture_enabled = True
        self.scanner.capture_pending = True                           # as _trigger_auto_capture sets it
        self.scanner.card_in_view = self.scanner.awaiting_new_card = True
        self.station._auto_captured(self.scanner)                     # fast: its release belongs to the old session
        self.answer('pending')
        self.assertLessEqual(self.released(), 1)
        self.assertEqual(self.released(), 0)

    def test_a_capture_finishing_after_a_restart_owes_nothing(self):
        real = self.scanner.capture_card_image_only

        def capture_with_a_restart(number, settle=0):
            self.station.on_set({'auto_capture_enabled': False})
            self.station.on_set({'auto_capture_enabled': True})
            return real(number, settle)
        self.scanner.capture_card_image_only = capture_with_a_restart
        self.scanner.auto_capture_enabled = True
        self.scanner.fast_scan_mode = False
        self.scanner.card_in_view = True
        self.station._auto_captured(self.scanner)
        self.assertIsNone(self.station.drop_owed)

    def test_a_review_done_sent_while_the_station_was_away_comes_with_the_next_connection_once(self):
        # A Codex review of 2026-10-22: Add / Skip while the station is disconnected - review_done was sent to nobody, and the card
        # owed was owed for ever. The server repeats the last settled capture in hello; it pays once, and a repeat pays nothing
        self.capture(fast=False)
        self.station.sio = mock.Mock()
        self.station.sio.call.return_value = {'settings': {}, 'set': {'dropper': dict(ON)}, 'review_done': self.ids[0]}
        self.station.hello()                                          # reconnected
        self.assertEqual(self.released(), 1)
        self.station.hello()                                          # and again (another reconnection): nothing more
        self.assertEqual(self.released(), 1)
        self.assertIsNone(self.station.drop_owed)

    def test_a_review_done_that_comes_before_the_upload_is_queued_is_not_lost(self):
        # A Codex review of 2026-10-22, its reproduction: the server may answer as soon as the upload is sent - here at the moment it is
        # queued. The debt must be there already, or the answer is lost and the feeding stops for good
        self.on_queued = lambda capture_id: self.station.on_review_done({'capture_id': capture_id})
        self.capture(fast=False)
        self.assertEqual(self.released(), 1)
        self.assertIsNone(self.station.drop_owed)

    def test_switching_the_dropper_off_and_on_again_while_a_release_waits_releases_nothing(self):
        # A Codex review of 2026-10-22: off only forgot the debt - a release waiting for the focus pulsed once the dropper was on again
        self.scanner.auto_capture_enabled = True
        self.scanner.focus_probe_running = True
        self.station.release_next_card('waiting')
        self.station.release_next_card('kept')
        self.station.on_set({'dropper': {'enabled': False, 'pin': 17}})
        self.station.on_set({'dropper': dict(ON)})                     # on again before the focus settled
        self.scanner.focus_probe_running = False
        self.assertEqual(self.released(), 0)
        self.assertIsNone(self.station.release_pending)

    def test_another_pulse_length_while_it_stays_on_cancels_nothing(self):
        self.scanner.auto_capture_enabled = True
        self.scanner.focus_probe_running = True
        self.station.release_next_card('waiting')
        self.station.on_set({'dropper': dict(ON, pulse_ms=300)})       # still on: the card waiting for its release still comes
        self.scanner.focus_probe_running = False
        self.assertEqual(self.released(), 1)

    def test_a_stop_and_restart_while_the_line_is_being_opened_releases_only_the_new_sessions_card(self):
        # A Codex review of 2026-10-22: the session was checked, THEN the GPIO line opened, THEN the pin set - a stop and restart in
        # between let the old release pulse beside the new session's first card. Here the opening of the line is held while scanning
        # is stopped and started again; the check made atomically with the pin (the gate) refuses the old release
        opening, go_on = threading.Event(), threading.Event()
        pins = []

        def slow_open(chip, pin, active_low):
            if not pins:                                   # the first opening: held
                opening.set()
                go_on.wait(5)
            pins.append(FakePin(chip, pin, active_low))
            return pins[-1]
        self.station.dropper = Dropper(open_pin=slow_open, sleep=lambda _s: None)
        self.station.dropper.configure(ON)
        self.scanner.auto_capture_enabled = True
        self.station.release_next_card('the old session')
        self.assertTrue(opening.wait(5))                   # past every early check, opening the line
        self.station.on_set({'auto_capture_enabled': False})
        self.station.on_set({'auto_capture_enabled': True})   # the new session's first card: kept behind the old release
        go_on.set()
        self.assertEqual(self.released(), 1)               # the new session's only
        # One activation in all: the refused release never set the pin active (it only set it inactive on its way out, as it already was)
        self.assertEqual(pins[0].values.count(True), 1)
        self.assertEqual(pins[0].values[-1], False)        # and the pin is left inactive

    def pulses_by_reason(self):
        """The reasons of the pulses actually sent"""
        reasons = []
        real = self.station.dropper.pulse

        def pulse(reason='', gate=None):
            sent = real(reason, gate=gate)
            if sent:
                reasons.append(reason)
            return sent
        self.station.dropper.pulse = pulse
        return reasons

    def test_a_capture_starting_the_moment_scanning_switches_on_belongs_to_the_new_session(self):
        # A Codex review of 2026-10-22: scanning was switched on BEFORE the new session began - a capture in that instant took the old
        # session and its release was refused, and the start's card dropped onto the card being captured. Here the camera's thread
        # starts a capture the very moment the switch is made
        reasons = self.pulses_by_reason()
        station, scanner = self.station, self.scanner
        camera = []

        class Switching(FakeScanner):
            @property
            def auto_capture_enabled(self):
                return self.__dict__.get('_on', False)

            @auto_capture_enabled.setter
            def auto_capture_enabled(self, value):
                was = self.__dict__.get('_on', False)
                self.__dict__['_on'] = value
                if value and not was and not camera:      # the camera's thread: a card was already there, captured at once
                    self.card_in_view = self.awaiting_new_card = True
                    camera.append(threading.Thread(target=station._auto_captured, args=(self,)))
                    camera[0].start()
        switching = Switching()
        station.scanner = switching
        station.on_set({'auto_capture_enabled': True})
        camera[0].join(5)
        self.answer('pending')                                         # the server has that capture
        self.assertEqual(self.released(), 1)
        self.assertEqual(len(reasons), 1)
        self.assertNotEqual(reasons[0], 'scanning started')            # the capture's own release - not the start's onto it

    def test_a_card_captured_before_the_starts_card_goes_releases_one_card(self):
        # Start with the focus being checked (the start's card waits); meanwhile the card in view is captured and the server answers:
        # its release comes, the start's is refused - one card for one capture
        reasons = self.pulses_by_reason()
        self.scanner.focus_probe_running = True
        self.station.on_set({'auto_capture_enabled': True})           # the start's card: waiting for the focus
        self.capture(fast=True)                                        # the card already there, captured in this session
        self.answer('pending')                                         # its release: kept behind the start's
        self.scanner.focus_probe_running = False
        self.assertEqual(self.released(), 1)
        self.assertEqual(len(reasons), 1)
        self.assertNotEqual(reasons[0], 'scanning started')

    def reconnect(self, set_values=None):
        """The station connects again: the server's hello with these 'set' values"""
        self.station.sio = mock.Mock()
        self.station.sio.call.return_value = {'settings': {}, 'set': dict({'dropper': dict(ON)}, **(set_values or {})), 'review_done': None}
        self.station.hello()

    def test_during_an_outage_a_release_waits_for_the_server_and_goes_when_nothing_stopped(self):
        # A Codex review of 2026-10-22: while Socket.IO is down a stop cannot reach the station, though uploads may still get through
        self.capture(fast=True)
        self.station.on_disconnect()
        self.answer('pending')                                        # the upload answered over HTTP during the outage
        self.assertEqual(self.released(), 0)                          # kept, not pulsed
        self.assertIsNotNone(self.station.release_deferred)
        self.reconnect()                                              # nothing was stopped meanwhile
        self.assertEqual(self.released(), 1)

    def test_a_stop_pressed_during_the_outage_cancels_the_release_kept(self):
        self.capture(fast=True)
        self.station.on_disconnect()
        self.answer('pending')
        self.reconnect({'auto_capture_enabled': False})               # the server replays the stop pressed meanwhile
        self.assertEqual(self.released(), 0)
        self.assertFalse(self.scanner.auto_capture_enabled)
        self.assertIsNone(self.station.release_deferred)

    def test_a_release_waiting_for_the_focus_when_the_connection_goes_is_kept_too(self):
        self.scanner.auto_capture_enabled = True
        self.scanner.focus_probe_running = True
        self.station.release_next_card('waiting')
        self.station.on_disconnect()
        self.scanner.focus_probe_running = False
        self.assertEqual(self.released(), 0)
        self.reconnect()
        self.assertEqual(self.released(), 1)

    def test_a_lost_hello_reply_keeps_the_station_out_of_step_and_the_next_one_stops_it(self):
        # The reply to the first hello is lost: the station stays out of step (nothing released); the next hello carries the stop again
        self.capture(fast=True)
        self.station.on_disconnect()
        self.answer('pending')
        self.station.sio = mock.Mock()

        def lost(*_args, **_kwargs):
            self.station.on_disconnect()                               # the reply lost with the connection
            raise TimeoutError('the reply was lost')
        self.station.sio.call.side_effect = lost
        with self.assertLogs('station', level='ERROR'):
            self.station.hello()
        self.assertFalse(self.station.dropper_synced)
        self.assertEqual(self.released(), 0)
        self.reconnect({'auto_capture_enabled': False})                # the stop, repeated
        self.assertEqual(self.released(), 0)
        self.assertFalse(self.scanner.auto_capture_enabled)

    def test_a_disconnect_while_a_hello_is_applied_keeps_the_station_out_of_step(self):
        # A Codex review of 2026-10-22: hello set "in step" unconditionally at its end - a disconnect while its answer was applied was undone
        self.capture(fast=True)
        self.station.on_disconnect()
        self.answer('pending')                                         # kept (out of step)
        real_on_set = self.station.on_set

        def on_set_then_gone(values):
            real_on_set(values)
            self.station.on_disconnect()                               # the connection goes while the answer is being applied
        self.station.on_set = on_set_then_gone
        self.reconnect()
        self.assertFalse(self.station.dropper_synced)
        self.assertEqual(self.released(), 0)
        self.assertIsNotNone(self.station.release_deferred)            # still kept for the next hello

    def test_a_hello_that_times_out_is_asked_again_on_the_same_connection(self):
        # A Codex review of 2026-10-22: a hello that failed was not asked again - the feeding waited on a healthy connection for ever
        self.capture(fast=True)
        self.station.on_disconnect()
        self.answer('pending')
        self.station.sio = mock.Mock()
        self.station.sio.call.side_effect = [TimeoutError('slow'), {'settings': {}, 'set': {'dropper': dict(ON)}, 'review_done': None}]
        with self.assertLogs('station', level='ERROR'):
            self.station.hello()
        self.assertTrue(self.station.dropper_synced)
        self.assertEqual(self.released(), 1)                           # the release kept during the outage goes

    def test_a_stop_while_a_start_is_still_being_applied_wins(self):
        # A Codex review of 2026-10-22: the switch was written again by apply outside the lock - a start paused there undid a stop
        # that came meanwhile, switched scanning back on and released a card
        real_apply = self.station.apply
        calls = []

        def start_paused_here(scanner, values):
            calls.append(values)
            if len(calls) == 1:                                        # the start, before its apply: the whole stop happens now
                self.station.on_set({'auto_capture_enabled': False})
            real_apply(scanner, values)
        self.station.apply = start_paused_here
        self.station.on_set({'auto_capture_enabled': True})
        self.assertFalse(self.scanner.auto_capture_enabled)            # the stop came last: it stands
        self.assertEqual(self.released(), 0)                           # and the start's first card is stale

    def test_an_older_hello_answering_late_changes_nothing(self):
        # A Codex review of 2026-10-22: a stale hello applied its settings first and checked its connection after - its "dropper on"
        # undid the newer connection's "off", and the station, in step from the newer hello, went on pulsing
        self.station.on_disconnect()
        with self.station.release_lock:
            self.station.connection += 1                               # connection A
        older = self.station.connection

        def answered_late(*_args, **_kwargs):
            # While A's answer is on its way: A goes, B connects, and B's hello switches the dropper off
            self.station.on_disconnect()
            with self.station.release_lock:
                self.station.connection += 1
            self.station.sio = mock.Mock()
            self.station.sio.call.return_value = {'settings': {}, 'set': {'dropper': dict(ON, enabled=False)}, 'review_done': None}
            self.station.hello(self.station.connection)
            return {'settings': {}, 'set': {'dropper': dict(ON)}, 'review_done': None}      # A's answer: "on"
        self.station.sio = mock.Mock()
        self.station.sio.call.side_effect = answered_late
        with self.assertLogs('station', level='WARNING'):
            self.station.hello(older)
        self.assertFalse(self.station.dropper.enabled)                  # B's "off" stands
        self.assertTrue(self.station.dropper_synced)                    # in step from B's hello

    def test_a_live_change_is_not_undone_by_an_older_hello_on_the_same_connection(self):
        # A Codex review of 2026-10-22: the hello's answer, built before a live "dropper off", applied after it - and switched it on
        self.station.sio = mock.Mock()

        def answered_late(*_args, **_kwargs):
            self.station.on_set({'dropper': dict(ON, enabled=False), '_revision': ['start-1', 8]})    # the live "off", sent later
            return {'settings': {}, 'set': {'dropper': dict(ON), '_revision': ['start-1', 7]}, 'review_done': None}
        self.station.sio.call.side_effect = answered_late
        self.station.hello()
        self.assertFalse(self.station.dropper.enabled)

    def test_a_newer_value_of_one_setting_hides_no_other_setting(self):
        self.station.on_set({'dropper': dict(ON, enabled=False), '_revision': ['start-1', 8]})
        self.station.on_set({'auto_capture_enabled': True, '_revision': ['start-1', 7]})          # another setting, numbered earlier
        self.assertTrue(self.scanner.auto_capture_enabled)

    def test_a_restarted_server_counts_from_the_start_again(self):
        self.station.on_set({'dropper': dict(ON, enabled=False), '_revision': ['start-1', 50]})
        self.station.on_set({'dropper': dict(ON), '_revision': ['start-2', 1]})                   # a new start: applied
        self.assertTrue(self.station.dropper.enabled)

    def test_the_server_numbers_every_change_and_its_hello(self):
        from card_scanner.core import remote_scanner
        remote = remote_scanner.RemoteScanner(mock.Mock(), FakeSettings())
        remote.sid = 'sid-1'
        remote.dropper = dict(ON)
        sent = remote.socketio.emit.call_args[0][1]['_revision']
        remote.dropper = dict(ON, enabled=False)
        later = remote.socketio.emit.call_args[0][1]['_revision']
        self.assertEqual(sent[0], later[0])
        self.assertGreater(later[1], sent[1])
        self.assertEqual(remote.hello()['set']['_revision'], later)  # the hello: the newest change it includes

    def test_the_station_reports_the_scanning_change_it_applied(self):
        self.station.on_set({'auto_capture_enabled': True, '_revision': ['start-1', 4]})
        self.assertEqual(self.station.state()['auto_capture_revision'], ['start-1', 4])

    def test_connecting_never_starts_scanning(self):
        self.reconnect({'auto_capture_enabled': True})                # (the server sends none - refused all the same)
        self.assertFalse(self.scanner.auto_capture_enabled)
        self.assertEqual(self.released(), 0)

    def test_stopping_scanning_drops_a_release_kept_for_later(self):
        self.scanner.auto_capture_enabled = True
        self.scanner.focus_probe_running = True
        self.station.release_next_card('first')
        self.station.release_next_card('kept')
        self.station.on_set({'auto_capture_enabled': False})
        self.scanner.focus_probe_running = False
        self.assertEqual(self.released(), 0)                          # the first: scanning stopped meanwhile; the kept one: dropped

    def test_with_the_dropper_off_nothing_is_ever_released(self):
        self.station.dropper.configure({'enabled': False, 'pin': 17})
        self.capture(fast=True)
        self.station.on_set({'auto_capture_enabled': False})
        self.station.on_set({'auto_capture_enabled': True})
        self.assertEqual(self.released(), 0)
        self.assertEqual(FakePin.opened, [])

    def test_the_settings_come_from_the_server_and_wrong_ones_are_refused(self):
        self.station.on_set({'dropper': dict(ON, pin=22)})
        self.assertEqual(self.station.dropper.config['pin'], 22)
        with self.assertLogs('station', level='ERROR'):
            self.station.on_set({'dropper': {'enabled': True, 'pin': 'x'}})
        self.assertEqual(self.station.dropper.config['pin'], 22)            # the old ones stay

    def test_connecting_configures_but_never_releases_a_card(self):
        self.station.sio = mock.Mock()
        self.station.sio.call.return_value = {'settings': {}, 'set': {'dropper': dict(ON, pin=5), 'auto_capture_enabled': True}}
        self.station.hello()
        self.assertEqual(self.station.dropper.config['pin'], 5)
        self.assertEqual(self.released(), 0)

    def test_the_settings_pages_test_pulses_once_or_says_why_not(self):
        answer = self.station.on_command({'method': 'drop_card'})
        self.assertTrue(answer['ok'])
        self.assertEqual(answer['result'], 1)                                # the cards released so far
        self.assertEqual(answer['state']['dropper_state']['pulses'], 1)      # reported to the server with the answer
        self.station.dropper.configure({'enabled': False, 'pin': 17})
        answer = self.station.on_command({'method': 'drop_card'})
        self.assertFalse(answer['ok'])
        self.assertIn('off', answer['error'])



class FakeSettings:
    def __init__(self, saved=None):
        self.saved = dict(saved or {})

    def get(self, key, default=None):
        return self.saved.get(key, default)

    def set(self, key, value):
        self.saved[key] = value


class TheServer(unittest.TestCase):
    """The server's side: the setting saved per camera and sent to the station (RemoteScanner), the Settings page's two events"""

    def test_the_setting_is_a_cameras_and_reaches_the_station_when_it_connects(self):
        from card_scanner.core import remote_scanner
        self.assertIn('dropper', remote_scanner.CAMERA_SETTINGS)
        remote = remote_scanner.RemoteScanner(mock.Mock(), FakeSettings({'dropper': dict(ON)}))
        self.assertEqual(remote.dropper, ON)
        self.assertEqual(remote.hello()['set']['dropper'], ON)          # sent with the others on connect
        self.assertIsNone(remote.dropper_state)                          # nothing reported while no station is connected

    def test_the_settings_pages_events(self):
        import app as launcher  # noqa: F401 - the server's routes and events
        from card_scanner.server.state import app, socketio
        settings_events = sys.modules['card_scanner.server.settings_events']
        station = mock.Mock(connected=True, dropper=None, dropper_state={'enabled': True, 'pulses': 3, 'error': None, 'last_pulse': None},
                            settings=FakeSettings())
        station.drop_card.return_value = 4
        with mock.patch.object(settings_events, 'scanner', station):
            client = socketio.test_client(app, flask_test_client=app.test_client())
            client.get_received()
            try:
                def emit(event, payload=None):
                    client.emit(event, payload or {})
                    return {e['name']: e['args'][0] for e in client.get_received()}

                answer = emit('set_dropper', {'dropper': {'enabled': True, 'pin': 17, 'pulse_ms': 200}})
                self.assertEqual(station.settings.saved['dropper'], dict(DEFAULTS, enabled=True, pin=17, pulse_ms=200))
                self.assertEqual(station.dropper, station.settings.saved['dropper'])        # sent to the station
                self.assertEqual(answer['dropper_updated']['settings']['pin'], 17)

                saved = dict(station.settings.saved)
                answer = emit('set_dropper', {'dropper': {'enabled': True, 'pin': True}})  # refused whole, nothing saved
                self.assertIn('pin', answer['error']['message'].lower())
                self.assertEqual(station.settings.saved, saved)
                self.assertIn('dropper_updated', answer)                                    # the page goes back

                answer = emit('test_dropper')
                self.assertEqual(answer['dropper_tested'], {'ok': True, 'pulses': 4})
                station.drop_card.side_effect = ValueError('The card dropper is off')
                self.assertEqual(emit('test_dropper')['dropper_tested'], {'ok': False, 'message': 'The card dropper is off'})

                page = settings_events.dropper_payload()        # the dropper's part of /api/scan_settings
                self.assertEqual(page['state']['pulses'], 3)
                self.assertTrue(page['connected'])
                station.connected = False                        # a station that is away: its last report is not shown
                self.assertIsNone(settings_events.dropper_payload()['state'])
                station.dropper = {'enabled': True, 'pin': 999}  # data/stations.json edited by hand: shown as off, never trusted
                self.assertFalse(settings_events.dropper_payload()['settings']['enabled'])
            finally:
                client.disconnect()



class TheReviewIsDone(unittest.TestCase):
    """
    The server tells a station that the live capture shown for Add / Skip is settled (desks.review_done) - on Add of that card,
    on Skip, on its going to the review queue by itself - and never on an error, nor for an item of the review queue
    """

    def setUp(self):
        import app as launcher  # noqa: F401 - the server's routes and events
        from card_scanner.server.state import app, socketio
        self.app, self.socketio = app, socketio
        self.desks = sys.modules['card_scanner.server.desks']
        self.scan_events = sys.modules['card_scanner.server.scan_events']

    def settle(self, steps):
        """Run steps(desks) on a desk of its own; the capture ids sent to the station"""
        station, desk = mock.Mock(), mock.Mock(capture_ids={})
        with mock.patch.object(self.desks, 'desk', lambda: desk), mock.patch.object(self.desks, 'scanner', station):
            steps(self.desks)
        return [c.args[0] for c in station.review_done.call_args_list], desk

    def test_it_names_the_shown_capture_once(self):
        def steps(d):
            d.remember_capture_id('/cards/A.jpg', 'id-A')
            d.review_done('/cards/A.jpg')
            d.review_done('/cards/A.jpg')                              # already said: nothing more
        sent, desk = self.settle(steps)
        self.assertEqual(sent, ['id-A'])
        self.assertEqual(desk.capture_ids, {})

    def test_settling_the_shown_card_while_another_capture_is_read_settles_only_the_shown_one(self):
        # A Codex review of 2026-10-22: one shared field was replaced as soon as the next capture ARRIVED - settling the card still
        # shown then released the NEW capture's next card. Each capture's id is now under its own picture
        def steps(d):
            d.remember_capture_id('/cards/A.jpg', 'id-A')               # shown, waiting for Add / Skip
            d.remember_capture_id('/cards/B.jpg', 'id-B')               # arrived, being read
            d.review_done('/cards/A.jpg')                               # the person settles A
        sent, desk = self.settle(steps)
        self.assertEqual(sent, ['id-A'])
        self.assertEqual(desk.capture_ids, {'/cards/B.jpg': 'id-B'})   # B's stays for B

    def test_a_manual_capture_during_an_automatic_review_does_not_take_its_place(self):
        def steps(d):
            d.remember_capture_id('/cards/A.jpg', 'id-A')               # the automatic capture, shown
            d.remember_capture_id('/cards/M.jpg', 'id-M')               # a manual Capture meanwhile
            d.review_done('/cards/M.jpg')                               # the manual one settled: its own id (the station owes nothing for it)
            d.review_done('/cards/A.jpg')                               # the automatic one settled: still its own id
        sent, _ = self.settle(steps)
        self.assertEqual(sent, ['id-M', 'id-A'])

    def test_a_capture_that_failed_or_none_shown_sends_nothing(self):
        def steps(d):
            d.remember_capture_id('/cards/A.jpg', 'id-A')
            d.forget_capture_id('/cards/A.jpg')                         # reading it failed
            d.review_done('/cards/A.jpg')
            d.review_done(None)                                         # nothing shown
        sent, _ = self.settle(steps)
        self.assertEqual(sent, [])

    def test_captures_never_settled_are_let_go(self):
        def steps(d):
            for n in range(d.CAPTURE_IDS_KEPT + 5):
                d.remember_capture_id(f'/cards/{n}.jpg', f'id-{n}')
        _, desk = self.settle(steps)
        self.assertEqual(len(desk.capture_ids), self.desks.CAPTURE_IDS_KEPT)
        self.assertNotIn('/cards/0.jpg', desk.capture_ids)            # the oldest went first

    def test_a_settled_capture_is_repeated_to_a_station_that_connects(self):
        from card_scanner.core import remote_scanner
        socket = mock.Mock()
        remote = remote_scanner.RemoteScanner(socket, FakeSettings())
        self.assertIsNone(remote.hello()['review_done'])
        remote.review_done('capture-7')                               # the station is away: nothing can be sent ...
        socket.emit.assert_not_called()
        self.assertEqual(remote.hello()['review_done'], 'capture-7')  # ... so it comes with the next connection

    def test_a_stop_is_replayed_until_the_station_reports_it_is_off(self):
        # A Codex review of 2026-10-22: crossed off when the hello was built, a lost reply lost the stop and the feeding went on
        from card_scanner.core import remote_scanner
        remote = remote_scanner.RemoteScanner(mock.Mock(), FakeSettings())
        remote.auto_capture_enabled = False                           # the station away: nothing to send it on
        stop = remote.hello()['set']['_revision']
        self.assertIs(remote.hello()['set']['auto_capture_enabled'], False)
        self.assertIs(remote.hello()['set']['auto_capture_enabled'], False)   # the first reply lost: in the next hello again
        remote.status({'state': {'auto_capture_enabled': True, 'auto_capture_revision': None}})   # from before it applied the stop
        self.assertIs(remote.hello()['set']['auto_capture_enabled'], False)
        remote.status({'state': {'auto_capture_enabled': False, 'auto_capture_revision': stop}})  # off, the stop applied: confirmed
        self.assertNotIn('auto_capture_enabled', remote.hello()['set'])

    def test_a_start_after_the_stop_is_never_undone_by_it(self):
        from card_scanner.core import remote_scanner
        remote = remote_scanner.RemoteScanner(mock.Mock(), FakeSettings())
        remote.auto_capture_enabled = False
        remote.auto_capture_enabled = True                            # stopped, then started again: nothing to replay
        self.assertNotIn('auto_capture_enabled', remote.hello()['set'])

    def test_a_live_stop_is_repeated_too_until_confirmed(self):
        # Sent into a connection that was just dropping, a live stop could be lost as well
        from card_scanner.core import remote_scanner
        remote = remote_scanner.RemoteScanner(mock.Mock(), FakeSettings())
        remote.sid = 'sid-1'
        remote.auto_capture_enabled = False
        stop = remote.socketio.emit.call_args[0][1]['_revision']
        self.assertIs(remote.hello()['set']['auto_capture_enabled'], False)
        remote.status({'state': {'auto_capture_enabled': False, 'auto_capture_revision': stop}})
        self.assertNotIn('auto_capture_enabled', remote.hello()['set'])

    def test_an_off_report_from_before_a_start_confirms_no_stop(self):
        # A Codex review of 2026-10-22: an idle station's "off", delayed, arrived after the start and a lost stop - and crossed the
        # stop off, though the station was still scanning
        from card_scanner.core import remote_scanner
        remote = remote_scanner.RemoteScanner(mock.Mock(), FakeSettings())
        remote.sid = 'sid-1'
        remote.auto_capture_enabled = True                            # the start, delivered
        start = remote.socketio.emit.call_args[0][1]['_revision']
        remote.auto_capture_enabled = False                           # the stop - lost with the connection
        remote.status({'state': {'auto_capture_enabled': False, 'auto_capture_revision': None}})    # the idle report, delayed
        self.assertIs(remote.hello()['set']['auto_capture_enabled'], False)
        remote.status({'state': {'auto_capture_enabled': False, 'auto_capture_revision': start}})   # older than the stop too
        self.assertIs(remote.hello()['set']['auto_capture_enabled'], False)                       # still waiting: replayed

    def test_a_capture_queued_because_the_game_was_switched_pays_its_card(self):
        # A Codex review of 2026-10-22: that branch queued the capture without review_done - the dropper waited for ever
        from card_scanner.server import captures
        games = mock.Mock()
        games.active_id.return_value = 'another-game'
        with mock.patch.object(captures, 'games', games), mock.patch.object(captures, 'review') as review, \
                mock.patch.object(captures, 'desk', return_value=mock.MagicMock(id='station-1')), \
                mock.patch.object(captures, 'capture_key', return_value='uid-1'), mock.patch.object(captures, 'log_to_client'), \
                mock.patch.object(captures, 'scanner', mock.MagicMock()), mock.patch.object(captures, 'review_done') as review_done:
            outcome = captures.route_identified('/x/c.jpg', '', '', '', game_id='mtg', fast=True)
        self.assertEqual(outcome, {'status': 'review', 'reason': 'game switched'})
        review.add.assert_called_once()
        review_done.assert_called_once_with('/x/c.jpg')

    def test_the_station_is_sent_the_capture_it_owes_a_card_for(self):
        from card_scanner.core import remote_scanner
        socket = mock.Mock()
        remote = remote_scanner.RemoteScanner(socket, FakeSettings())
        remote.sid = 'sid-1'
        remote.review_done('capture-7')
        socket.emit.assert_called_with('review_done', {'capture_id': 'capture-7'}, to='sid-1', namespace=remote_scanner.NAMESPACE)

    def add(self, desk, inventory):
        """The page's Add, through Socket.IO, with stand-ins for what it reads and writes; whether review_done was called"""
        game = mock.Mock(default_finish='nonfoil', finishes={'nonfoil': 'Regular'}, id='mtg')
        game.inventory_fields.return_value = {}
        patches = [mock.patch.object(self.scan_events, name, value) for name, value in (
            ('desk', lambda: desk), ('scan_inventory', inventory), ('scanner', mock.Mock()), ('review_done', mock.Mock()),
            ('scan_stats', mock.Mock(return_value={})), ('added_payload', mock.Mock(return_value={})),
            ('start_price_update', mock.Mock()), ('scan_location', mock.Mock(return_value='')), ('set_pending_capture', mock.Mock()),
            ('review', mock.Mock(get=mock.Mock(return_value={'file': 'x.jpg', 'station': 's'}), count=mock.Mock(return_value=0))),
            ('handle_review_open', mock.Mock()), ('identification', None), ('log_to_client', mock.Mock()), ('emit_desk', mock.Mock()))]
        patches.append(mock.patch.object(self.scan_events.games, 'active', lambda: game))
        for patch in patches:
            patch.start()
        try:
            client = self.socketio.test_client(self.app, flask_test_client=self.app.test_client())
            client.emit('add_to_inventory', {'items': [{'finish': 'nonfoil', 'quantity': 1}]})
            client.disconnect()
            done = self.scan_events.review_done
            return done.call_args.args[0] if done.called else None
        finally:
            for patch in patches:
                patch.stop()

    def a_desk(self, review_id=None, capture='/cards/A.jpg', pending='/cards/A.jpg'):
        card = {'name': 'Lightning Bolt', 'id': 'p1'}
        if capture:
            card['capture'] = capture
        return mock.Mock(current_card_info=card, current_review_id=review_id,
                         pending_capture=pending, review_filter=None, review_candidates=[])

    def test_add_settles_the_shown_cards_own_capture(self):
        self.assertEqual(self.add(self.a_desk(), mock.Mock()), '/cards/A.jpg')

    def test_add_of_another_printing_picked_for_it_settles_the_same_capture(self):
        # Another printing picked on the page: the card has no picture of its own, the capture shown is still the camera's pending one
        self.assertEqual(self.add(self.a_desk(capture=None, pending='/cards/A.jpg'), mock.Mock()), '/cards/A.jpg')

    def test_add_of_an_item_of_the_review_queue_settles_nothing(self):
        self.assertIsNone(self.add(self.a_desk(review_id=42), mock.Mock()))

    def test_an_add_that_failed_settles_nothing(self):
        broken = mock.Mock()
        broken.add_card.side_effect = OSError('database is locked')
        self.assertIsNone(self.add(self.a_desk(), broken))

    def dismiss(self, payload):
        """The page's dismiss_card with this payload; what review_done was called with (None: not called)"""
        desk = mock.Mock(pending_capture='/cards/A.jpg')
        with mock.patch.object(self.scan_events, 'review_done') as done, mock.patch.object(self.scan_events, 'scanner', mock.Mock()), \
                mock.patch.object(self.scan_events, 'set_pending_capture', mock.Mock()), mock.patch.object(self.scan_events, 'desk', lambda: desk):
            client = self.socketio.test_client(self.app, flask_test_client=self.app.test_client())
            client.emit('dismiss_card', payload)
            client.disconnect()
        return done.call_args.args[0] if done.called else None

    def test_skip_settles_the_shown_capture(self):
        self.assertEqual(self.dismiss({}), '/cards/A.jpg')

    def test_not_found_dismissing_itself_settles_nothing(self):
        # A Codex review of 2026-10-22: scanner.js dismisses "not found" by itself (keep_capture) - that is not the person's Skip,
        # and the card dropper must not drop the next card onto the unresolved one
        self.assertIsNone(self.dismiss({'keep_capture': True}))

    def test_a_not_found_card_found_by_search_and_added_settles_its_capture(self):
        # After "not found" the picture stays the pending capture; the card found by a manual search has none of its own
        self.assertEqual(self.add(self.a_desk(capture=None, pending='/cards/A.jpg'), mock.Mock()), '/cards/A.jpg')


if __name__ == '__main__':
    unittest.main()
