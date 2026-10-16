"""
The local AI endpoint must be on the person's own network (core/api_keys.py: check_local_endpoint),
and the route that lists its models does not tell a caller why it failed. Temporary files only.

    venv/bin/python -m unittest discover tests -p test_local_endpoint.py
"""
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from card_scanner.core import api_keys  # noqa: E402


# What the stand-in resolver says for the names the tests use (the real one would need a network)
NAMES = {'ai-server': '192.168.1.23', 'ai.local': '192.168.1.9', 'box.lan': '10.0.0.7', 'host.docker.internal': '172.17.0.1',
         'public.example.com': '93.184.216.34', 'tricky.lan': '8.8.8.8', 'rebind.local': '127.0.0.1',
         'mixed.example': ['192.168.1.5', '8.8.4.4'], 'v6-private.local': 'fd00::5', 'v6-public.example': '2606:4700::1111',
         'mapped.example': '::ffff:8.8.8.8'}


def resolver(host, port, *args, **kwargs):
    """socket.getaddrinfo for the tests: literal and numeric forms are read by the real one, names come from NAMES"""
    if host in NAMES:
        answers = NAMES[host] if isinstance(NAMES[host], list) else [NAMES[host]]
        return [(2, 1, 6, '', (address, 0)) for address in answers]
    return real_getaddrinfo(host, port, *args, **kwargs)


real_getaddrinfo = socket.getaddrinfo


class Endpoint(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, {}, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop('ALLOW_PUBLIC_AI_ENDPOINT', None)
        names = mock.patch.object(api_keys.socket, 'getaddrinfo', side_effect=resolver)
        names.start()
        self.addCleanup(names.stop)

    def refused(self, value):
        with self.assertRaises(ValueError):
            api_keys.check_local_endpoint(value)

    def test_the_persons_own_network_is_allowed(self):
        for value in ('http://localhost:11434/v1/chat/completions', 'http://127.0.0.1:11434', 'http://[::1]:11434',
                      'http://192.168.1.23:11434/v1/chat/completions', 'http://10.0.0.5:1234', 'http://172.20.1.1',
                      'http://169.254.10.10', 'http://100.101.102.103:11434',   # Tailscale
                      'http://[fd00::5]:11434', 'http://127.1:11434',            # shortened loopback
                      'http://ai-server:11434', 'http://ai.local:11434', 'https://box.lan/v1', 'http://host.docker.internal:11434',
                      'http://v6-private.local'):
            api_keys.check_local_endpoint(value)

    def test_other_machines_are_refused(self):
        for value in ('http://8.8.8.8/v1', 'https://api.example.com/v1/chat/completions', 'http://93.184.216.34:80',
                      'http://172.32.0.1', 'http://evil.example.org', 'http://[2606:4700::1111]/v1'):
            self.refused(value)

    def test_other_ways_to_write_a_public_address_are_refused(self):
        # What the outside review found: a name without dots to a reader, a public address to the resolver
        for value in ('http://134744072/v1/chat/completions',      # 8.8.8.8 as one decimal number
                      'http://0x08080808/v1', 'http://0x8.0x8.0x8.0x8', 'http://010.8.8.8',  # hexadecimal, octal-looking
                      'http://8.8.8/v1', 'http://8.526344/v1'):    # shortened forms
            self.refused(value)
        # ... and the real resolver agrees about the first one (no mock): it IS 8.8.8.8
        self.assertEqual({info[4][0] for info in real_getaddrinfo('134744072', None, proto=socket.IPPROTO_TCP)}, {'8.8.8.8'})

    def test_a_name_counts_by_what_it_resolves_to_not_by_how_it_ends(self):
        self.refused('http://tricky.lan:11434')             # .lan, public address
        self.refused('http://public.example.com')
        self.refused('http://mixed.example')                # one private and one public address: refused
        self.refused('http://v6-public.example')
        self.refused('http://mapped.example')               # an IPv4 address written as IPv6
        api_keys.check_local_endpoint('http://rebind.local')   # a private name stays allowed

    def test_a_name_that_does_not_resolve_is_refused(self):
        with mock.patch.object(api_keys.socket, 'getaddrinfo', side_effect=socket.gaierror('no such host')):
            self.refused('http://no-such-box:11434')

    def test_the_form_of_the_address_is_checked(self):
        for value in ('ftp://192.168.1.2', 'file:///etc/passwd', '192.168.1.2:11434', 'http://', 'http://user:pw@192.168.1.2',
                      'http://192.168.1.2:notaport', 'javascript:alert(1)'):
            self.refused(value)

    def test_the_environment_can_allow_another_address(self):
        os.environ['ALLOW_PUBLIC_AI_ENDPOINT'] = '1'
        api_keys.check_local_endpoint('https://api.example.com/v1')
        os.environ['ALLOW_PUBLIC_AI_ENDPOINT'] = 'true'                # only exactly 1
        self.refused('https://api.example.com/v1')

    def test_saving_a_public_endpoint_is_refused_and_leaves_the_file_alone(self):
        with tempfile.TemporaryDirectory() as folder:
            keys = Path(folder) / 'api_keys.env'
            with mock.patch.object(api_keys, 'KEYS_FILE', keys), mock.patch.object(api_keys.Config, 'DATA_DIR', Path(folder)):
                with self.assertRaises(ValueError):
                    api_keys.save_credential('local', 'https://api.example.com/v1')
                self.assertFalse(keys.exists())
                api_keys.save_credential('local', 'http://192.168.1.23:11434/v1/chat/completions')
                self.assertIn('192.168.1.23', keys.read_text())
        os.environ.pop('LOCAL_AI_ENDPOINT', None)


class ModelsRoute(unittest.TestCase):
    def test_a_failure_is_not_described_to_the_caller(self):
        import app as launcher  # noqa: F401 - the server's routes
        from card_scanner.server.state import app
        import requests
        with mock.patch.object(requests, 'get', side_effect=RuntimeError('internal detail 10.9.8.7 refused')):
            answer = app.test_client().get('/api/local_ai_models', base_url='http://localhost').get_json()
        self.assertFalse(answer['success'])
        self.assertNotIn('10.9.8.7', str(answer))
        self.assertNotIn('internal detail', str(answer))


if __name__ == '__main__':
    unittest.main()
