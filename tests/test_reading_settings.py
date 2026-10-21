"""
The Settings page's switch for how cards are read (server/settings_events.py: set_reading_mode, reading_status; core/identification.py: reading_mode, accept_image_alone):
the event is guarded like the other settings that decide what is added, refuses what it does not know, remembers what it was told and answers with the status the page
shows - and what is remembered is what Identification reads on the next card, no restart. Socket.IO's test client with stand-ins; nothing real is touched.

    venv/bin/python -m unittest discover tests -p test_reading_settings.py
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as launcher  # noqa: E402,F401 - the server's routes and events
from card_scanner.core import identification  # noqa: E402
from card_scanner.server import lock  # noqa: E402
from card_scanner.server.state import app, socketio  # noqa: E402
from card_scanner.station.station_login import Login  # noqa: E402

settings_events = sys.modules['card_scanner.server.settings_events']


class FakeSettings:
    def __init__(self, saved=None):
        self.saved = dict(saved or {})

    def get(self, key, default=None):
        return self.saved.get(key, default)

    def set(self, key, value):
        self.saved[key] = value


def make_identification(settings, ocr_enabled=True):
    """The real Identification properties on an object made without __init__ (no OCR process, no AI)"""
    ident = identification.Identification.__new__(identification.Identification)
    ident.settings = settings
    ident.ocr_enabled = ocr_enabled
    ident.evidence = None
    return ident


class TheEvent(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.settings = FakeSettings()
        self.ident = make_identification(self.settings)
        self.index = mock.MagicMock()
        self.index.__len__.return_value = 112765
        self.check = mock.Mock(index=mock.Mock(return_value=self.index))
        self.log = mock.Mock()
        self.login = Login(file=Path(self.folder.name) / 'settings_login.json', variable='TEST_SETTINGS_PASSWORD', cookie=lock.COOKIE,
                           session_seconds=lock.SESSION_SECONDS)
        for name, value in (('app_settings', self.settings), ('identification', self.ident), ('printing_check', self.check), ('log_to_client', self.log)):
            patcher = mock.patch.object(settings_events, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(lock, 'login', self.login)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = socketio.test_client(app, flask_test_client=app.test_client())
        self.client.get_received()
        self.addCleanup(self.client.disconnect)

    def emit(self, payload):
        self.client.emit('set_reading_mode', payload)
        return {e['name']: e['args'][0] for e in self.client.get_received()}

    def test_the_default_is_evidence_and_the_picture_alone_is_on(self):
        # (on since 2026-10-20: the picture comes first and a clear one settles a card - the person can still turn it off, and what they chose is what is kept)
        status = settings_events.reading_status()
        self.assertEqual((status['mode'], status['accept_image_alone']), ('evidence', True))
        self.settings.saved['reading_accept_image_alone'] = False
        self.assertFalse(settings_events.reading_status()['accept_image_alone'])           # a stored choice wins over the default

    def test_evidence_is_remembered_and_the_next_card_takes_it(self):
        got = self.emit({'mode': 'evidence'})
        self.assertEqual(self.settings.saved, {'reading_mode': 'evidence'})
        self.assertEqual(got['reading_mode_changed'], {'mode': 'evidence', 'accept_image_alone': True, 'index_printings': 112765, 'index_missing': None, 'ocr_enabled': True})
        self.assertEqual(self.ident.reading_mode, 'evidence')                       # no restart: Identification reads the setting
        self.assertTrue(self.ident.accept_image_alone)                              # the default (the picture comes first)
        self.emit({'mode': 'cascade'})
        self.assertEqual(self.ident.reading_mode, 'cascade')
        self.assertEqual(self.settings.saved['reading_mode'], 'cascade')           # turning it OFF is remembered too: the default would turn it on again

    def test_the_picture_alone_is_its_own_switch_and_only_true_is_true(self):
        self.emit({'accept_image_alone': True})
        self.assertTrue(self.ident.accept_image_alone)
        self.assertNotIn('reading_mode', self.settings.saved)                      # one switch does not set the other
        for value in ('yes', 1, None):
            self.settings.saved['reading_accept_image_alone'] = value
            self.assertFalse(self.ident.accept_image_alone, repr(value))           # a hand-edited file cannot turn it on by accident

    def test_what_it_does_not_know_is_refused_and_nothing_is_saved(self):
        for payload in ({'mode': 'both'}, {'mode': 3}, {'accept_image_alone': 'yes'}, {'accept_image_alone': 1}):
            got = self.emit(payload)
            self.assertIn('error', got, payload)
            self.assertNotIn('reading_mode_changed', got)
        self.assertEqual(self.settings.saved, {})
        self.client.emit('set_reading_mode')                                       # no payload at all
        self.assertEqual(self.settings.saved, {})

    def test_the_status_says_what_the_mode_needs(self):
        self.check.index.return_value = None
        self.ident.ocr_enabled = False
        status = settings_events.reading_status()
        self.assertEqual((status['index_printings'], status['ocr_enabled']), (None, False))
        self.emit({'mode': 'evidence'})
        warnings = [call for call in self.log.call_args_list if call.kwargs.get('level') == 'warning']
        self.assertEqual(len(warnings), 1)                                         # said once: it needs the index and OCR
        self.check.index.side_effect = RuntimeError('unreadable index')
        self.assertIsNone(settings_events.reading_status()['index_printings'])     # a failing index is "none", not an error on the page

    def test_the_page_gets_the_status_with_the_other_scan_settings(self):
        data = app.test_client().get('/api/scan_settings', base_url='http://localhost').get_json()
        self.assertEqual(set(data['reading']), {'mode', 'accept_image_alone', 'index_printings', 'index_missing', 'ocr_enabled'})

    # ----- the vision AI's switch (toggle_ai): like the OCR's, open, remembered, and what Identification reads on the next card
    def toggle_ai(self, enabled):
        self.client.emit('toggle_ai', {'enabled': enabled})
        return {e['name']: e['args'][0] for e in self.client.get_received()}

    def test_the_ai_is_on_by_default_and_the_page_is_told(self):
        self.ident.card_identifier = object()
        data = app.test_client().get('/api/scan_settings', base_url='http://localhost').get_json()
        self.assertEqual((data['ai_enabled'], data['ai_available']), (True, True))

    def test_without_a_provider_there_is_no_ai_to_switch(self):
        data = app.test_client().get('/api/scan_settings', base_url='http://localhost').get_json()
        self.assertEqual((data['ai_enabled'], data['ai_available']), (True, False))      # the page disables the switch and says so

    def test_switching_the_ai_off_is_remembered_answered_and_logged(self):
        self.ident.card_identifier = object()
        got = self.toggle_ai(False)
        self.assertEqual(got['ai_toggled'], {'enabled': False, 'available': True})
        self.assertEqual(self.settings.saved['ai_enabled'], False)
        self.assertFalse(self.ident.ai_enabled)                                               # what the next card reads: no restart
        self.assertIn('off', self.log.call_args[0][0])
        self.assertEqual(self.toggle_ai(True)['ai_toggled']['enabled'], True)
        self.assertTrue(self.ident.ai_enabled)
        self.assertEqual(self.settings.saved['ai_enabled'], True)

    def test_the_ai_switch_does_not_need_the_settings_password_like_the_ocr_switch(self):
        # it adds nothing, it only stops asking: what is added is still decided by the same rules
        self.login.set_password('correct horse')
        stranger = socketio.test_client(app, flask_test_client=app.test_client())
        stranger.get_received()
        stranger.emit('toggle_ai', {'enabled': False})
        got = {e['name']: e['args'][0] for e in stranger.get_received()}
        stranger.disconnect()
        self.assertNotIn('error', got)
        self.assertEqual(got['ai_toggled']['enabled'], False)

    def test_with_a_settings_password_a_stranger_is_locked_out(self):
        self.login.set_password('correct horse')
        stranger = socketio.test_client(app, flask_test_client=app.test_client())
        stranger.get_received()
        stranger.emit('set_reading_mode', {'mode': 'evidence', 'accept_image_alone': True})
        errors = [e for e in stranger.get_received() if e['name'] == 'error']
        stranger.disconnect()
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0]['args'][0].get('locked'))
        self.assertEqual(self.settings.saved, {})


if __name__ == '__main__':
    unittest.main()
