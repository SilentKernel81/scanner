"""
A camera station's setup page (station_setup.py): which of command line, environment and
saved file wins; what a "Save" from the page may contain; that the file survives being
written and read. No camera and no network are used, and the settings file is a temporary one.

    venv/bin/python -m unittest discover tests
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import station_setup  # noqa: E402
from config import Config  # noqa: E402

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
                     {'crop': ['a', 0, 1, 1]}, {'camera': 'usb', 'camera_index': '/dev/null'}):
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
        for name in ('CAMERA_TYPE', 'USB_CAMERA_INDEX', 'CAMERA_CROP'):
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

    def test_a_saved_camera_that_is_gone_keeps_the_configured_one(self):
        before = Config.USB_CAMERA_INDEX
        station_setup.apply_camera({'camera': 'usb', 'camera_index': '/dev/v4l/by-id/usb-unplugged-video-index0'}, {})
        self.assertEqual(Config.USB_CAMERA_INDEX, before)


if __name__ == '__main__':
    unittest.main()
