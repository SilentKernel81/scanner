#!/usr/bin/env python3
"""
The server as a program on somebody's computer (server/program.py): who may reach it, quit
and restart, and the camera station that runs beside it.

Run:  venv/bin/python tests/test_program.py        (or with the others: -m unittest discover tests)
"""
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from card_scanner.config import Config  # noqa: E402
from card_scanner.server import program  # noqa: E402
import card_scanner.server.startup  # noqa: E402,F401 - the pages' routes exist once their modules are imported
from card_scanner.server.state import app, services  # noqa: E402


class Settings:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


class Reach(unittest.TestCase):
    def setUp(self):
        self.saved = (services.app_mode, services.app_settings, Config.HOST)

    def tearDown(self):
        services.app_mode, services.app_settings, Config.HOST = self.saved

    def reach(self, host='auto', app_mode=False, **saved):
        Config.HOST, services.app_mode, services.app_settings = host, app_mode, Settings(**saved)
        return program.network_access(), program.listen_host()

    def test_a_server_listens_on_the_whole_network(self):
        self.assertEqual(self.reach(), (True, '0.0.0.0'))

    def test_a_desktop_program_listens_on_this_computer_only(self):
        self.assertEqual(self.reach(app_mode=True), (False, '127.0.0.1'))

    def test_the_switch_in_settings_wins(self):
        self.assertEqual(self.reach(app_mode=True, network_access=True), (True, '0.0.0.0'))
        self.assertEqual(self.reach(network_access=False), (False, '127.0.0.1'))

    def test_an_address_in_config_stays(self):
        self.assertEqual(self.reach(host='192.168.1.5'), (True, '192.168.1.5'))
        self.assertEqual(self.reach(host='127.0.0.1'), (False, '127.0.0.1'))
        self.assertEqual(self.reach(host='127.0.0.1', network_access=True), (True, '0.0.0.0'))  # as the switch says


class Routes(unittest.TestCase):
    def setUp(self):
        self.saved = (services.app_mode, services.app_settings, services.restart_requested)
        services.app_settings = Settings()
        self.client = app.test_client()
        self.timer = mock.patch.object(program, '_stop_soon')
        self.stop = self.timer.start()

    def tearDown(self):
        self.timer.stop()
        services.app_mode, services.app_settings, services.restart_requested = self.saved

    def test_quit_is_for_a_desktop_program_on_this_computer(self):
        services.app_mode = False
        self.assertEqual(self.client.post('/api/quit', base_url='http://localhost').status_code, 403)
        services.app_mode = True
        self.assertEqual(self.client.post('/api/quit', base_url='http://localhost',
                                          environ_overrides={'REMOTE_ADDR': '192.168.1.9'}).status_code, 403)
        self.assertEqual(self.client.post('/api/quit', base_url='http://localhost',
                                          environ_overrides={'REMOTE_ADDR': '127.0.0.1'}).status_code, 200)
        self.assertEqual(self.stop.call_count, 1)

    def test_restart_asks_for_a_new_start(self):
        services.restart_requested = False
        self.assertEqual(self.client.post('/api/restart', base_url='http://localhost').status_code, 200)
        self.assertTrue(services.restart_requested)

    def test_the_switch_is_saved(self):
        answer = self.client.post('/api/network_access', json={'enabled': True}, base_url='http://localhost').get_json()
        self.assertEqual((answer['enabled'], services.app_settings.get('network_access')), (True, True))

    def test_the_settings_drawer_has_the_program_section(self):
        services.app_mode = True
        page = self.client.get('/collection', base_url='http://localhost').get_data(as_text=True)
        self.assertIn('Quit Card Scanner', page)
        self.assertIn('toggle-network-access', page)
        services.app_mode = False
        page = self.client.get('/collection', base_url='http://localhost').get_data(as_text=True)
        self.assertNotIn('Quit Card Scanner', page)
        self.assertIn('Restart Card Scanner', page)


class Shutdown(unittest.TestCase):
    """Asked twice (two tabs, a repeated SIGTERM), the program stops - and restarts - once"""

    def setUp(self):
        self.saved = (program._stop_scheduled, services.app_mode, services.restart_requested)
        program._stop_scheduled = False
        program._shutting_down.clear()

    def tearDown(self):
        program._stop_scheduled, services.app_mode, services.restart_requested = self.saved
        program._shutting_down.clear()

    def test_one_stop_however_often_it_is_asked(self):
        with mock.patch.object(program.threading, 'Timer') as timer:
            program._stop_soon()
            program._stop_soon()
            program._stop_soon()
        self.assertEqual(timer.call_count, 1)

    def test_a_second_sigterm_during_the_cleanup_is_ignored(self):
        with self.assertRaises(KeyboardInterrupt):
            program.stop_on_signal(15, None)
        program.stop_on_signal(15, None)  # main is cleaning up: must not raise inside it
        self.assertTrue(program._shutting_down.is_set())

    def test_a_quit_asked_after_a_restart_wins_until_the_stop_began(self):
        services.app_mode, services.restart_requested = True, True
        with mock.patch.object(program, '_stop_soon'):
            app.test_client().post('/api/quit', base_url='http://localhost',
                                   environ_overrides={'REMOTE_ADDR': '127.0.0.1'})
        self.assertFalse(services.restart_requested)


class Address(unittest.TestCase):
    """The station, the browser and --quit reach the server where it listens"""

    def setUp(self):
        self.saved = (services.app_mode, services.app_settings, Config.HOST)

    def tearDown(self):
        services.app_mode, services.app_settings, Config.HOST = self.saved

    def use(self, host, app_mode=True, **saved):
        Config.HOST, services.app_mode, services.app_settings = host, app_mode, Settings(**saved)

    def test_loopback_for_what_listens_on_loopback_or_everywhere(self):
        self.use('auto')
        self.assertEqual(program.local_url(5000), 'http://localhost:5000')
        self.use('auto', app_mode=False)
        self.assertEqual(program.local_url(5000), 'http://localhost:5000')  # 0.0.0.0 answers on loopback

    def test_the_address_of_its_own_when_that_is_where_it_listens(self):
        self.use('192.168.1.5', network_access=True)
        self.assertEqual(program.local_url(5000), 'http://192.168.1.5:5000')
        self.assertEqual(program.station_command(5000)[-1], 'http://192.168.1.5:5000')

    def test_an_address_of_its_own_with_the_switch_off_listens_on_loopback(self):
        self.use('192.168.1.5', network_access=False)
        self.assertEqual(program.listen_host(), '127.0.0.1')
        self.assertEqual(program.local_url(5000), 'http://localhost:5000')

    def test_ipv6_is_bracketed(self):
        self.use('fe80::1', network_access=True)
        self.assertEqual(program.local_url(5000), 'http://[fe80::1]:5000')

    def test_quit_and_a_second_start_try_both_before_the_settings_are_read(self):
        self.use('192.168.1.5')
        self.assertEqual(program.candidate_urls(5000), ['http://localhost:5000', 'http://192.168.1.5:5000'])
        self.use('auto')
        self.assertEqual(program.candidate_urls(5000), ['http://localhost:5000'])


class Station(unittest.TestCase):
    def setUp(self):
        self.saved = (Config.CAMERA_TYPE, program.STATION_STOP_SECONDS)

    def tearDown(self):
        Config.CAMERA_TYPE, program.STATION_STOP_SECONDS = self.saved
        program.stop_station()

    def test_the_command_connects_to_this_server(self):
        command = program.station_command(5000)
        self.assertEqual(command[-2:], ['--server', 'http://localhost:5000'])
        self.assertTrue(Path(command[1]).name == 'station.py' and Path(command[1]).is_file())

    def test_no_station_for_a_machine_without_a_camera(self):
        Config.CAMERA_TYPE = 'remote'
        with mock.patch.object(subprocess, 'Popen') as popen:
            self.assertIsNone(program.start_station(5000))
            popen.assert_not_called()

    def test_a_station_that_does_not_stop_is_killed_and_the_server_does_not_hang(self):
        program.STATION_STOP_SECONDS = 0.6
        # Ignores SIGINT, as a station stuck in a camera call would
        stuck = subprocess.Popen([sys.executable, '-c',
                                  'import signal, time; signal.signal(signal.SIGINT, signal.SIG_IGN); '
                                  'print(1, flush=True); time.sleep(60)'], stdout=subprocess.PIPE)
        stuck.stdout.readline()  # the handler is in place
        self.addCleanup(stuck.stdout.close)
        services.station_process = stuck
        started = time.time()
        program.stop_station()
        self.assertLess(time.time() - started, 5)
        self.assertIsNotNone(stuck.poll())
        self.assertIsNone(services.station_process)

    def test_a_station_that_stops_by_itself_is_not_killed(self):
        quick = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])  # SIGINT ends it
        services.station_process = quick
        program.stop_station()
        self.assertEqual(quick.poll(), -2)  # ended by the interrupt, not killed (-9)


class Arguments(unittest.TestCase):
    def test_options(self):
        args = program.parse_arguments(['--app', '--no-browser', '--no-station'])
        self.assertEqual((args.app, args.no_browser, args.no_station, args.quit), (True, True, True, False))
        self.assertTrue(program.parse_arguments(['--quit']).quit)
        self.assertFalse(program.parse_arguments([]).app)


if __name__ == '__main__':
    unittest.main()
