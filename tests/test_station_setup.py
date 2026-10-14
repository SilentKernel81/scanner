"""
A camera station's setup page (station_setup.py): which of command line, environment and
saved file wins; what a "Save" from the page may contain; that the file survives being
written and read - and the page's password (station_login.py): how it is kept, checked and
remembered by a browser. No camera and no network are used, and the files are temporary ones.

    venv/bin/python -m unittest discover tests
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

from card_scanner.station import station_login  # noqa: E402
from card_scanner.station import station_setup  # noqa: E402
from card_scanner.config import Config  # noqa: E402

NO_ARGUMENTS = {'server': None, 'id': None, 'name': None, 'token': None}


class CleanEnvironment(unittest.TestCase):
    """Each test starts without SCANNER_* variables and with a settings file of its own"""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        file_patch = mock.patch.object(station_setup, 'SETTINGS_FILE', Path(self.folder.name) / 'station.json')
        file_patch.start()
        self.addCleanup(file_patch.stop)
        # mock.patch.dict puts os.environ back as it was when the test ends
        environment = mock.patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        for variable in station_setup.ENVIRONMENT.values():
            os.environ.pop(variable, None)
        os.environ.pop(station_login.PASSWORD_VARIABLE, None)
        login_patch = mock.patch.object(station_login, 'LOGIN_FILE', Path(self.folder.name) / 'station_login.json')
        login_patch.start()
        self.addCleanup(login_patch.stop)


class WhoWins(CleanEnvironment):
    def test_nothing_set_is_setup_mode(self):
        values, fixed = station_setup.resolve(NO_ARGUMENTS, {}, 'desk-pc')
        self.assertEqual(values['server'], '')  # no server: station_client only serves the page
        self.assertEqual((values['id'], values['name']), ('desk-pc', 'desk-pc'))
        self.assertEqual(fixed, {})

    def test_saved_file_is_used(self):
        saved = {'server': 'http://10.0.0.5:5000/', 'name': 'Desk camera', 'camera': 'picamera'}
        values, fixed = station_setup.resolve(NO_ARGUMENTS, saved, 'desk-pc')
        self.assertEqual(values['server'], 'http://10.0.0.5:5000')  # no slash at the end
        self.assertEqual((values['id'], values['name'], values['camera']), ('desk-pc', 'Desk camera', 'picamera'))
        self.assertEqual(fixed, {})

    def test_environment_wins_over_the_file_and_is_fixed(self):
        os.environ['SCANNER_SERVER'] = 'http://from-env:5000'
        values, fixed = station_setup.resolve(NO_ARGUMENTS, {'server': 'http://from-file:5000', 'name': 'Saved'}, 'pc')
        self.assertEqual(values['server'], 'http://from-env:5000')
        self.assertEqual(values['name'], 'Saved')  # not in the environment: the file's
        self.assertEqual(list(fixed), ['server'])

    def test_command_line_wins_over_the_environment(self):
        os.environ['SCANNER_SERVER'] = 'http://from-env:5000'
        values, fixed = station_setup.resolve(dict(NO_ARGUMENTS, server='http://from-cli:5000'), {}, 'pc')
        self.assertEqual(values['server'], 'http://from-cli:5000')
        self.assertEqual(fixed['server'], 'the command line')

    def test_an_empty_variable_counts_as_not_set(self):
        # docker-compose passes every variable, empty when it is not in .env
        os.environ['SCANNER_SERVER'] = ''
        values, fixed = station_setup.resolve(NO_ARGUMENTS, {'server': 'http://from-file:5000'}, 'pc')
        self.assertEqual(values['server'], 'http://from-file:5000')
        self.assertEqual(fixed, {})


class SavingTheForm(CleanEnvironment):
    def test_server_address_is_tidied(self):
        self.assertEqual(station_setup.checked({'server': ' 192.168.1.20:5000/ '}, {})['server'], 'http://192.168.1.20:5000')
        self.assertEqual(station_setup.checked({'server': 'https://scanner.example'}, {})['server'], 'https://scanner.example')
        self.assertEqual(station_setup.checked({'server': ''}, {})['server'], '')

    def test_bad_values_are_refused_with_a_sentence(self):
        for form in ({'id': 'two words'}, {'id': 'x' * 41}, {'camera': 'webcam'}, {'crop': [0.1, 0.1, 0.12, 0.9]},
                     {'crop': ['a', 0, 1, 1]}, {'camera': 'usb', 'camera_index': '/dev/null'},
                     {'server': 'http://server:bad'}, {'server': 'http://:5000'}):
            with self.assertRaises(ValueError, msg=form):
                station_setup.checked(form, {})

    def test_token_is_kept_unless_typed_or_removed(self):
        current = {'token': 'secret'}
        self.assertEqual(station_setup.checked({'token': ''}, current)['token'], 'secret')  # the page never has it
        self.assertEqual(station_setup.checked({'token': 'new'}, current)['token'], 'new')
        self.assertEqual(station_setup.checked({'remove_token': True}, current)['token'], '')

    def test_fields_not_sent_keep_their_value(self):
        current = {'server': 'http://a:5000', 'name': 'Desk', 'camera': 'usb', 'camera_index': '2', 'crop': [0.1, 0.1, 0.9, 0.9]}
        values = station_setup.checked({'name': 'Shelf'}, current)
        self.assertEqual(values['name'], 'Shelf')
        self.assertEqual({key: values[key] for key in current if key != 'name'},
                         {key: current[key] for key in current if key != 'name'})

    def test_crop(self):
        self.assertEqual(station_setup.checked_crop([0.7, 0.9, 0.3, 0.1]), [0.3, 0.1, 0.7, 0.9])  # put in order
        self.assertEqual(station_setup.checked_crop([-0.2, 0, 1.5, 1]), None)  # clipped to the image = all of it
        self.assertEqual(station_setup.checked_crop(None), None)

    def test_file_round_trip(self):
        values = station_setup.checked({'server': '10.0.0.5:5000', 'name': 'Desk', 'token': 'secret',
                                        'camera': 'picamera', 'crop': [0.36, 0.16, 0.66, 0.88]}, {})
        station_setup.save(values)
        self.assertEqual(station_setup.load(), values)
        self.assertEqual(oct(station_setup.SETTINGS_FILE.stat().st_mode & 0o777), '0o600')  # it holds the token
        self.assertFalse(station_setup.SETTINGS_FILE.with_suffix('.json.tmp').exists())

    def test_a_broken_file_starts_as_not_set_up(self):
        station_setup.SETTINGS_FILE.write_text('{"server": "http://a:50')
        self.assertEqual(station_setup.load(), {})
        station_setup.SETTINGS_FILE.write_text(json.dumps(["not", "settings"]))
        self.assertEqual(station_setup.load(), {})


class TheCameraInConfig(CleanEnvironment):
    def setUp(self):
        super().setUp()
        # apply_camera changes Config: put it back for the other tests
        for name in ('CAMERA_TYPE', 'USB_CAMERA_SOURCE', 'USB_CAMERA_INDEX', 'CAMERA_CROP'):
            patcher = mock.patch.object(Config, name, getattr(Config, name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_saved_camera_is_applied(self):
        station_setup.apply_camera({'camera': 'picamera', 'camera_index': '4', 'crop': [0.1, 0.2, 0.8, 0.9]}, {})
        self.assertEqual((Config.CAMERA_TYPE, Config.USB_CAMERA_INDEX, Config.CAMERA_CROP),
                         ('picamera', 4, [0.1, 0.2, 0.8, 0.9]))

    def test_the_environment_s_camera_is_left_alone(self):
        before = (Config.CAMERA_TYPE, Config.USB_CAMERA_INDEX)
        station_setup.apply_camera({'camera': 'picamera', 'camera_index': '4', 'crop': None},
                                   {'camera': 'SCANNER_CAMERA in the environment', 'camera_index': 'SCANNER_CAMERA_INDEX in the environment'})
        self.assertEqual((Config.CAMERA_TYPE, Config.USB_CAMERA_INDEX), before)

    def test_a_saved_camera_that_is_unplugged_is_waited_for(self):
        # It is not replaced by another camera: it has no number now, and the scanner keeps
        # looking for that same one
        gone = '/dev/v4l/by-id/usb-unplugged-video-index0'
        station_setup.apply_camera({'camera': 'usb', 'camera_index': gone}, {})
        self.assertEqual((Config.USB_CAMERA_SOURCE, Config.USB_CAMERA_INDEX), (gone, None))

    def test_a_camera_named_by_its_path_is_found_when_it_is_there(self):
        with tempfile.TemporaryDirectory() as folder:
            link = Path(folder) / 'usb-Some_Camera-video-index0'
            link.symlink_to('/dev/video7')  # what /dev/v4l/by-id holds: links to /dev/videoN
            from card_scanner.config import camera_index_or_none
            self.assertEqual(camera_index_or_none(str(link)), 7)
            link.unlink()
            self.assertIsNone(camera_index_or_none(str(link)))  # unplugged: no error
        self.assertEqual(camera_index_or_none(3), 3)
        self.assertEqual(camera_index_or_none('3'), 3)


class Answer:
    """What requests.get() returns, as far as the connection test looks at it"""

    def __init__(self, status_code, body):
        self.status_code, self.body = status_code, body

    def json(self):
        if self.body is None:
            raise ValueError("not JSON")
        return self.body


class TestConnection(CleanEnvironment):
    """The page's "Test connection" (SetupPage.test), with requests.get replaced by a stand-in"""

    def run_test(self, form, *answers, saved_token=''):
        """Call the test with `answers` handed out one per request; returns (result, the requests made)"""
        page = station_setup.SetupPage(None, {'id': 'desk', 'token': saved_token}, {}, restart=lambda: None)
        asked = []

        def get(url, timeout=None, headers=None):
            asked.append((url, (headers or {}).get('X-Station-Token')))
            answer = answers[len(asked) - 1]
            if isinstance(answer, Exception):
                raise answer
            return answer

        with mock.patch.object(station_setup.requests, 'get', get):
            return page.test(form), asked

    OPEN = Answer(200, {'stations': [{}, {}], 'token_required': False})
    WANTS_TOKEN = Answer(200, {'stations': [], 'token_required': True})
    NO_SUCH_CAPTURE = Answer(404, {'success': False})   # what the right token gets
    WRONG_TOKEN = Answer(401, {'success': False})

    def test_a_server_without_a_token(self):
        result, asked = self.run_test({'server': '10.0.0.5:5000'}, self.OPEN)
        self.assertTrue(result['ok'])
        self.assertEqual(result['address'], 'http://10.0.0.5:5000')
        self.assertEqual(len(asked), 1)  # nothing to check a token against

    def test_a_server_that_wants_a_token_and_none_is_given(self):
        result, _ = self.run_test({'server': 'http://s:5000'}, self.WANTS_TOKEN)
        self.assertFalse(result['ok'])
        self.assertIn('asks for a station token', result['message'])

    def test_a_wrong_token_is_not_reported_as_working(self):
        # The list of stations is given to anyone: only the second request tells
        result, asked = self.run_test({'server': 'http://s:5000', 'token': 'wrong'}, self.WANTS_TOKEN, self.WRONG_TOKEN)
        self.assertFalse(result['ok'])
        self.assertIn('does not accept', result['message'])
        self.assertEqual(asked[1], ('http://s:5000/api/stations/desk/captures/0', 'wrong'))

    def test_the_right_token(self):
        result, _ = self.run_test({'server': 'http://s:5000', 'token': 'right'}, self.WANTS_TOKEN, self.NO_SUCH_CAPTURE)
        self.assertTrue(result['ok'])
        self.assertIn('accepted', result['message'])

    def test_the_saved_token_is_checked_when_none_is_typed(self):
        result, asked = self.run_test({'server': 'http://s:5000'}, self.WANTS_TOKEN, self.NO_SUCH_CAPTURE, saved_token='saved')
        self.assertTrue(result['ok'])
        self.assertEqual(asked[1][1], 'saved')

    def test_an_address_that_cannot_be_requested(self):
        # Refused before any request ...
        result, asked = self.run_test({'server': 'http://server:bad'})
        self.assertFalse(result['ok'])
        self.assertEqual(asked, [])
        # ... and if requests itself refuses one: an answer for the page, not an exception
        # (InvalidURL is both a "request failed" error and a ValueError)
        import requests
        result, _ = self.run_test({'server': 'http://s:5000'}, requests.exceptions.InvalidURL("no"))
        self.assertFalse(result['ok'])
        self.assertIn('No answer', result['message'])

    def test_no_answer_and_an_answer_that_is_not_a_scanner_server(self):
        import requests
        result, _ = self.run_test({'server': 'http://s:5000'}, requests.ConnectionError("refused"))
        self.assertIn('No answer', result['message'])
        for other in (Answer(200, None), Answer(200, ['a list']), Answer(200, {'stations': 'text'}), Answer(404, {'stations': []})):
            result, _ = self.run_test({'server': 'http://s:5000'}, other)
            self.assertFalse(result['ok'])
            self.assertIn('not as a scanner server', result['message'])

    def test_the_token_check_itself_failing(self):
        import requests
        result, _ = self.run_test({'server': 'http://s:5000', 'token': 't'}, self.WANTS_TOKEN, requests.Timeout("slow"))
        self.assertFalse(result['ok'])
        self.assertIn('could not be checked', result['message'])


class ThePagePassword(CleanEnvironment):
    def test_no_password_until_one_is_chosen(self):
        login = station_login.Login()
        self.assertFalse(login.has_password())
        self.assertFalse(login.check('anything'))
        self.assertFalse(login.valid_session('9999999999.abc'))

    def test_too_short_is_refused(self):
        with self.assertRaises(ValueError):
            station_login.Login().set_password('seven77')
        self.assertFalse(station_login.LOGIN_FILE.exists())

    def test_chosen_password_is_checked_and_survives_a_restart(self):
        station_login.Login().set_password('correct horse')
        login = station_login.Login()  # a new start reads the file
        self.assertTrue(login.has_password())
        self.assertTrue(login.check('correct horse'))

    def test_the_file_holds_no_password_and_is_private(self):
        station_login.Login().set_password('correct horse')
        text = station_login.LOGIN_FILE.read_text()
        self.assertNotIn('correct horse', text)
        self.assertEqual(oct(station_login.LOGIN_FILE.stat().st_mode & 0o777), '0o600')
        # The same password, chosen again, is saved differently: a new salt each time
        first = json.loads(text)
        station_login.Login().set_password('correct horse')
        second = json.loads(station_login.LOGIN_FILE.read_text())
        self.assertNotEqual((first['salt'], first['hash']), (second['salt'], second['hash']))

    def test_wrong_passwords_lock_the_next_attempts(self):
        login = station_login.Login()
        login.set_password('correct horse')
        with mock.patch.object(station_login.time, 'time', return_value=1000.0):
            self.assertFalse(login.check('guess one'))
            with self.assertRaises(ValueError):  # at once: locked, even with the right password
                login.check('correct horse')
        with mock.patch.object(station_login.time, 'time', return_value=1001.5):  # the 1 s is over
            self.assertFalse(login.check('guess two'))
        with mock.patch.object(station_login.time, 'time', return_value=1003.0):  # now it is 2 s
            with self.assertRaises(ValueError):
                login.check('correct horse')
        with mock.patch.object(station_login.time, 'time', return_value=1004.0):
            self.assertTrue(login.check('correct horse'))

    def test_session_cookie(self):
        login = station_login.Login()
        login.set_password('correct horse')
        session = login.new_session()
        self.assertTrue(login.valid_session(session))
        self.assertTrue(station_login.Login().valid_session(session))  # after a restart of the station
        expiry, signature = session.split('.')
        self.assertFalse(login.valid_session(f"{int(expiry) + 1}.{signature}"))  # a later expiry, same signature
        self.assertFalse(login.valid_session(f"{expiry}.{'0' * len(signature)}"))
        for junk in ('', 'abc', '.', '12.', '-5.abc'):
            self.assertFalse(login.valid_session(junk), junk)
        with mock.patch.object(station_login.time, 'time', return_value=int(expiry) + 1):
            self.assertFalse(login.valid_session(session))  # expired

    def test_changing_the_password_logs_everyone_out(self):
        login = station_login.Login()
        login.set_password('correct horse')
        session = login.new_session()
        login.set_password('another one 2')
        self.assertFalse(login.valid_session(session))
        self.assertFalse(login.check('correct horse'))

    def test_password_from_the_environment(self):
        os.environ[station_login.PASSWORD_VARIABLE] = 'from the compose file'
        login = station_login.Login()
        self.assertTrue(login.fixed and login.has_password())
        self.assertTrue(login.check('from the compose file'))
        with self.assertRaises(ValueError):  # not changeable on the page
            login.set_password('a new password')
        # Kept like a chosen password: a hash in the private file, never the password
        self.assertNotIn('from the compose file', station_login.LOGIN_FILE.read_text())
        self.assertEqual(oct(station_login.LOGIN_FILE.stat().st_mode & 0o777), '0o600')
        # The same password at the next start: the same record, so logins survive a restart
        self.assertTrue(station_login.Login().valid_session(login.new_session()))

    def test_a_cookie_cannot_be_made_from_the_environment_s_password(self):
        # What an earlier version allowed: its cookie key was SHA-256 of the password, so a
        # guess could be tested by signing a cookie with it - no login, no scrypt, no lock.
        import hashlib
        import hmac
        import time
        password = 'from the compose file'
        os.environ[station_login.PASSWORD_VARIABLE] = password
        login = station_login.Login()
        expiry = int(time.time()) + 3600
        for guessed_key in (hashlib.sha256(b'station-login:' + password.encode()).hexdigest()[32:],
                            hashlib.sha256(password.encode()).hexdigest()):
            forged = hmac.new(bytes.fromhex(guessed_key), str(expiry).encode(), hashlib.sha256).hexdigest()
            self.assertFalse(login.valid_session(f"{expiry}.{forged}"))

    def test_two_stations_with_the_same_password_do_not_share_logins(self):
        os.environ[station_login.PASSWORD_VARIABLE] = 'the same everywhere'
        first = station_login.Login()
        session = first.new_session()
        station_login.LOGIN_FILE.unlink()  # another station: its own, empty data folder
        second = station_login.Login()
        self.assertTrue(second.check('the same everywhere'))
        self.assertFalse(second.valid_session(session))
        self.assertNotEqual(first.record['key'], second.record['key'])

    def test_changing_the_environment_s_password_logs_everyone_out(self):
        os.environ[station_login.PASSWORD_VARIABLE] = 'the first one'
        session = station_login.Login().new_session()
        os.environ[station_login.PASSWORD_VARIABLE] = 'the second one'
        login = station_login.Login()
        self.assertFalse(login.valid_session(session))
        self.assertTrue(login.check('the second one'))
        self.assertFalse(login.check('the first one'))

    def test_the_environment_s_password_replaces_one_chosen_on_the_page(self):
        station_login.Login().set_password('chosen on the page')
        os.environ[station_login.PASSWORD_VARIABLE] = 'from the compose file'
        login = station_login.Login()
        self.assertTrue(login.check('from the compose file'))

    def test_cookie_header_is_read_back(self):
        login = station_login.Login()
        login.set_password('correct horse')
        session = login.new_session()
        header = login.cookie_header(session)
        self.assertIn('HttpOnly', header)
        self.assertIn('SameSite=Strict', header)
        self.assertEqual(station_login.session_from(f"other=1; {header.split(';')[0]}; more=2"), session)
        self.assertEqual(station_login.session_from(None), '')


if __name__ == '__main__':
    unittest.main()
