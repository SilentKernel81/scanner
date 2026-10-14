#!/usr/bin/env python3
"""
Who may use the server: its own pages and stations, not another web page in the browser
(server/access.py).

Run:  venv/bin/python tests/test_access.py        (or with the others: -m unittest discover tests)
"""
import sys
import unittest
from pathlib import Path

TOP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOP / 'src'))

from flask import Flask  # noqa: E402

from card_scanner.config import Config  # noqa: E402
from card_scanner.server import access  # noqa: E402


class Hosts(unittest.TestCase):
    def test_local_names_and_addresses(self):
        for host in ('localhost:5000', '127.0.0.1:5000', '192.168.1.20:5000', '[::1]:5000'):
            self.assertTrue(access.host_allowed(host), host)

    def test_another_domain_is_refused_even_under_the_computers_name(self):
        short = access.socket.gethostname().split('.')[0].lower()
        self.assertTrue(access.host_allowed(f'{short}.local:5000'))
        self.assertFalse(access.host_allowed(f'{short}.attacker.example:5000'))
        self.assertFalse(access.host_allowed('evil.example'))

    def test_allowed_hosts_adds_names(self):
        self.assertFalse(access.host_allowed('cards.example.org'))
        Config.ALLOWED_HOSTS = ['cards.example.org']
        try:
            self.assertTrue(access.host_allowed('cards.example.org:443'))
        finally:
            Config.ALLOWED_HOSTS = []


class Requests(unittest.TestCase):
    def setUp(self):
        app = Flask('t')
        access.protect(app)
        app.add_url_rule('/api/x', 'x', lambda: 'ok', methods=['GET', 'POST'])
        app.add_url_rule('/page', 'page', lambda: 'ok')
        self.client = app.test_client()

    def test_own_page_and_station_pass(self):
        self.assertEqual(self.client.post('/api/x', headers={'Origin': 'http://localhost'},
                                          base_url='http://localhost').status_code, 200)
        self.assertEqual(self.client.post('/api/x', base_url='http://192.168.1.20:5000').status_code, 200)

    def test_other_site_is_refused(self):
        self.assertEqual(self.client.post('/api/x', headers={'Origin': 'http://evil.example'},
                                          base_url='http://localhost').status_code, 403)
        self.assertEqual(self.client.get('/api/x', headers={'Sec-Fetch-Site': 'cross-site'},
                                         base_url='http://localhost').status_code, 403)
        self.assertEqual(self.client.get('/page', base_url='http://evil.example').status_code, 403)

    def test_a_refusal_says_why_where_a_client_looks(self):
        # The phone app shows `message` of a 4xx answer; the pages' scripts read `error`
        for reply in (self.client.get('/page', base_url='http://evil.example'),
                      self.client.post('/api/x', headers={'Origin': 'http://evil.example'}, base_url='http://localhost')):
            body = reply.get_json()
            self.assertEqual(reply.status_code, 403)
            self.assertTrue(body['message'] and body['message'] == body['error'], body)

    def test_socket_origin(self):
        env = {'HTTP_HOST': 'localhost:5000'}
        self.assertTrue(access.socket_origin_allowed('http://localhost:5000', env))
        self.assertTrue(access.socket_origin_allowed('', env))  # a station sends no Origin
        self.assertFalse(access.socket_origin_allowed('http://evil.example', env))
        self.assertFalse(access.socket_origin_allowed('http://evil.example', {'HTTP_HOST': 'evil.example'}))


class SocketTransport(unittest.TestCase):
    """The Socket.IO transport answers before Flask's hooks and skips its origin check when a
    request has no Origin: the Host check must still be in front of it"""

    def setUp(self):
        from flask_socketio import SocketIO
        app = Flask('t')
        SocketIO(app, cors_allowed_origins=access.socket_origin_allowed, async_mode='threading')
        access.protect(app)  # after SocketIO, as server/state.py does
        self.client = app.test_client()

    def test_polling_handshake_by_host(self):
        url = '/socket.io/?EIO=4&transport=polling'
        self.assertEqual(self.client.get(url, base_url='http://evil.example:5000').status_code, 403)
        self.assertEqual(self.client.get(url, base_url='http://evil.example:5000',
                                         headers={'Origin': 'http://evil.example:5000'}).status_code, 403)
        self.assertEqual(self.client.get(url, base_url='http://localhost:5000').status_code, 200)

    def test_the_servers_own_app_is_guarded_the_same_way(self):
        from card_scanner.server import state
        reply = state.app.test_client().get('/socket.io/?EIO=4&transport=polling', base_url='http://evil.example')
        self.assertEqual(reply.status_code, 403)


if __name__ == '__main__':
    unittest.main()
