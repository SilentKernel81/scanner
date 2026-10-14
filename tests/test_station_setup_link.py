#!/usr/bin/env python3
"""
The server knows where a station's setup page is (the station says its port when it connects,
the server sees the address it connected from) so that the pages can link to it.

Run:  venv/bin/python tests/test_station_setup_link.py        (or with the others: -m unittest discover tests)
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from flask import Flask  # noqa: E402
from flask_socketio import SocketIO  # noqa: E402

from card_scanner.core.remote_scanner import NAMESPACE, CameraHub  # noqa: E402


class FakeStations:
    def valid_id(self, station_id):
        return bool(station_id)

    def seen(self, station_id, name=None):
        return {'id': station_id, 'name': name or station_id}


class SetupLink(unittest.TestCase):
    def setUp(self):
        self.app = Flask('t')
        self.socketio = SocketIO(self.app, async_mode='threading')
        self.scanner = mock.MagicMock()
        self.hub = CameraHub(self.socketio, FakeStations(), lambda station_id: self.scanner)

    def connect(self, **auth):
        return self.socketio.test_client(self.app, namespace=NAMESPACE, auth={'id': 'desk-a', 'name': 'Desk', **auth})

    def test_the_port_and_the_address_it_connected_from_are_kept(self):
        client = self.connect(setup_port=8080)
        page = self.hub.setup_page('desk-a')
        self.assertEqual(page['port'], 8080)
        # Flask's test client has no peer address; a real connection has one (the page
        # takes a missing or loopback address to mean "the host this page was opened on")
        self.assertIn(page['address'], (None, '127.0.0.1'))
        client.disconnect(namespace=NAMESPACE)

    def test_no_or_a_wrong_port_is_no_link(self):
        for port in (None, 0, 'x', 70000, -1):
            self.hub._setup_pages.clear()
            self.connect(setup_port=port).disconnect(namespace=NAMESPACE)
            self.assertIsNone(self.hub.setup_page('desk-a'), port)

    def test_a_forgotten_station_has_no_link(self):
        self.connect(setup_port=8080).disconnect(namespace=NAMESPACE)
        self.hub.forget('desk-a')
        self.assertIsNone(self.hub.setup_page('desk-a'))


if __name__ == '__main__':
    unittest.main()
