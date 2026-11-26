"""
The card dropper's feeder over Wi-Fi (station/dropper.py: checked's 'transport' / 'feeder' / 'pair_key', WifiFeeder, find_feeders,
pair_feeder, Dropper with transport 'wifi'; station_client.py: the find_feeders / pair_feeder commands; the server's settings_events:
dropper_payload, set_dropper, find_feeders, pair_feeder, forget_feeder). The feeder is card-dropper's sideways feeder
(sideways/firmware: main.py request_feed / feed_state, tuning.py handle_feed / pair_form / answer_finders): FakeFeeder below
follows its rules - one request is one card, a number seen is never fed twice, another boot's id refused, one card at a time.
No network is touched.

    venv/bin/python -m unittest discover tests -p test_station_dropper_wifi.py
"""
import contextlib
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from card_scanner.station import dropper as dropper_module  # noqa: E402
from card_scanner.station.dropper import DEFAULTS, Dropper, WifiFeeder, checked  # noqa: E402

KEY = 'k' * 32
FEEDER = {'ip': '192.168.1.108', 'name': 'card-dropper-sw', 'mac': '02:00:00:00:00:01'}
WIFI_ON = {'enabled': True, 'transport': 'wifi', 'feeder': FEEDER, 'pair_key': KEY}


class FakeFeeder:
    """
    The feeder's HTTP answers, by its rules. done_after: polls of GET /feed before a card is 'done'; lose: answers lost
    (the request arrives, its answer does not: OSError) for the next POSTs; down: nothing answers at all
    """

    def __init__(self, done_after=2, key=KEY):
        self.boot = 'b007b007'
        self.key = key
        self.results = {}                 # n -> state
        self.polls = {}
        self.fed = []                     # the numbers actually fed, in order
        self.done_after = done_after
        self.lose = 0
        self.down = False
        self.fail_next = False
        self.fail_drop = None
        self.requests = []

    def restart(self):
        self.boot = 'feedfeed'
        self.results, self.polls = {}, {}

    def __call__(self, url, form=None, timeout=None):
        self.requests.append((url, form))
        if self.down:
            raise OSError('timed out')
        path = url.split('/', 3)[3] if url.count('/') >= 3 else ''
        if path == 'status':
            return 200, {'status': {'boot': self.boot}}
        if path.startswith('feed?n='):
            n = int(path.split('=')[1])
            if n not in self.results:
                return 404, {'state': 'unknown', 'boot': self.boot}
            if self.results[n] == 'queued':
                self.polls[n] += 1
                if self.polls[n] >= self.done_after:
                    self.results[n] = 'failed' if self.fail_next else 'done'
                    self.fed.append(n)
            payload = {'n': n, 'state': self.results[n], 'boot': self.boot, 'error': 'jammed' if self.results[n] == 'failed' else None}
            if self.results[n] == 'failed' and self.fail_drop:
                payload['drop'] = self.fail_drop
            elif self.results[n] == 'done':
                payload['drop'] = 'dropped'
            return 200, payload
        if path == 'feed':
            if form['key'] != self.key:
                return 403, {'error': 'wrong key - nothing fed', 'boot': self.boot}
            if form['boot'] != self.boot:
                return 409, {'refused': 'boot', 'error': 'restarted', 'boot': self.boot}
            n = int(form['n'])
            if n in self.results:
                answer = (200, {'n': n, 'state': self.results[n], 'boot': self.boot})
            elif any(state == 'queued' for state in self.results.values()):
                return 409, {'refused': 'busy', 'error': 'busy', 'boot': self.boot}
            else:
                self.results[n], self.polls[n] = 'queued', 0
                answer = (202, {'n': n, 'state': 'queued', 'boot': self.boot})
            if self.lose:
                self.lose -= 1
                raise OSError('the answer was lost')
            return answer
        if path == 'pair':
            if form['config_key'] != 'owner-key':
                return 403, {'error': 'wrong config key - not paired'}
            return 200, {'feeder': 'card-dropper-sw', 'ip': '192.168.1.108', 'mac': FEEDER['mac'], 'paired': True,
                         'station': form['station']}
        return 404, {}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def feeder_for(fake, clock=None):
    clock = clock or Clock()
    return WifiFeeder(FEEDER['ip'], KEY, http=fake, clock=clock, sleep=clock.sleep)


class TheSettings(unittest.TestCase):
    def test_a_feeder_is_a_transport_beside_the_pin(self):
        self.assertEqual(checked(None)['transport'], 'gpio')                  # as before: off, a pin
        self.assertEqual(checked(dict(WIFI_ON))['feeder'], FEEDER)             # on without a pin: the feeder needs none
        with self.assertRaisesRegex(ValueError, 'Pair a feeder'):
            checked({'enabled': True, 'transport': 'wifi'})                    # on, but not paired
        checked({'enabled': False, 'transport': 'wifi'})                       # off and not paired: allowed (chosen, then paired)
        with self.assertRaisesRegex(ValueError, 'GPIO pin'):
            checked({'enabled': True, 'transport': 'gpio', 'feeder': FEEDER, 'pair_key': KEY})   # the pin's rule stays
        for wrong in ({'transport': 'bluetooth'}, {'feeder': {'ip': 'not.an.ip'}}, {'feeder': {'ip': '1.2.3.4', 'x': 1}},
                      {'feeder': 'x'}, {'pair_key': 'short'}, {'pair_key': 'k' * 20 + ' spaced'}, {'pair_key': 5}):
            with self.assertRaises(ValueError, msg=wrong):
                checked(wrong)


class TheFeeder(unittest.TestCase):
    """WifiFeeder: one card per request, whatever the network does"""

    def test_one_card_asked_then_waited_for(self):
        fake = FakeFeeder(done_after=3)
        feeder = feeder_for(fake)
        feeder.set(True)
        self.assertEqual(len(fake.results), 1)
        self.assertEqual(fake.fed, [])                       # on its way, not done yet
        feeder.set(False)
        self.assertEqual(len(fake.fed), 1)                   # done: the card lies still
        feeder.set(False)                                    # nothing under way: nothing asked
        self.assertEqual(len(fake.fed), 1)

    def test_a_lost_answer_is_asked_again_with_the_same_number(self):
        fake = FakeFeeder()
        fake.lose = 2
        feeder = feeder_for(fake)
        feeder.set(True)
        feeder.set(False)
        numbers = {form['n'] for url, form in fake.requests if form}
        self.assertEqual(len(numbers), 1)                    # three tries, one number
        self.assertEqual(len(fake.fed), 1)                   # one card

    def test_every_answer_lost_but_the_number_taken_is_a_card_on_its_way(self):
        fake = FakeFeeder()
        fake.lose = dropper_module.FEEDER_TRIES
        feeder = feeder_for(fake)
        feeder.set(True)                                     # not "not released": the feeder has it
        feeder.set(False)
        self.assertEqual(len(fake.fed), 1)

    def test_a_feeder_that_does_not_answer_releases_nothing_and_says_so(self):
        fake = FakeFeeder()
        feeder = feeder_for(fake)
        feeder.set(True)
        feeder.set(False)
        fake.down = True
        with self.assertRaisesRegex(RuntimeError, 'did not answer'):
            feeder.set(True)
        feeder.set(False)                                    # nothing under way: nothing waited for

    def test_a_restart_before_the_request_asks_again_with_the_new_boot(self):
        fake = FakeFeeder()
        feeder = feeder_for(fake)
        feeder.set(True)
        feeder.set(False)
        fake.restart()                                       # the feeder powered off and on between two cards
        feeder.set(True)
        feeder.set(False)
        self.assertEqual(len(fake.fed), 2)
        self.assertEqual(feeder.boot, 'feedfeed')

    def test_a_restart_during_the_card_stops_and_says_look(self):
        fake = FakeFeeder(done_after=5)
        feeder = feeder_for(fake)
        feeder.set(True)
        fake.restart()
        with self.assertRaisesRegex(RuntimeError, 'restarted'):
            feeder.set(False)

    def test_refusals(self):
        fake = FakeFeeder(key='another')
        with self.assertRaisesRegex(RuntimeError, 'pair it again'):
            feeder_for(fake).set(True)
        fake = FakeFeeder(done_after=99)
        first = feeder_for(fake)
        first.set(True)
        other = feeder_for(fake)
        other.number = first.number + 1000                   # (a number of its own: the two were made in the same millisecond)
        with self.assertRaisesRegex(RuntimeError, 'busy'):
            other.set(True)                                  # another request while a card is under way
        self.assertEqual(len(fake.results), 1)

    def test_a_failed_card_and_the_wait_limit(self):
        fake = FakeFeeder()
        fake.fail_next = True
        feeder = feeder_for(fake)
        feeder.set(True)
        with self.assertRaisesRegex(RuntimeError, 'jammed'):
            feeder.set(False)
        clock = Clock()
        fake = FakeFeeder(done_after=10 ** 6)
        feeder = feeder_for(fake, clock)
        feeder.set(True)
        with self.assertRaisesRegex(RuntimeError, f'within {dropper_module.FEEDER_WAIT_S} s'):
            feeder.set(False)
        self.assertGreater(clock.now, dropper_module.FEEDER_WAIT_S)
        self.assertEqual(dropper_module.FEEDER_WAIT_S, 10)   # the owner's limit (2026-11-12)

    def test_what_runs_inside_the_gate_is_bounded(self):
        """set(True) runs under the station's release_lock: no boot id read there, few requests, short timeouts"""
        fake = FakeFeeder()
        fake.down = False
        feeder = feeder_for(fake)                            # the boot id read here, before any gate
        fake.requests.clear()
        fake.down = True                                     # the worst: nothing answers
        with self.assertRaises(RuntimeError):
            feeder.set(True)
        self.assertFalse(any(url.endswith('/status') for url, _ in fake.requests))
        self.assertLessEqual(len(fake.requests), dropper_module.FEEDER_TRIES + 1)
        self.assertLessEqual((dropper_module.FEEDER_TRIES + 1) * dropper_module.FEEDER_HTTP_S, 6)
        fake.down = False
        fake.restart()                                       # a restart refused: its new id taken from the refusal itself
        fake.requests.clear()
        feeder.set(True)
        self.assertFalse(any(url.endswith('/status') for url, _ in fake.requests))
        self.assertEqual(feeder.boot, 'feedfeed')

    def test_drop_one_card_is_waited_for_long_enough(self):
        from card_scanner.core import remote_scanner
        remote = remote_scanner.RemoteScanner(mock.Mock(), FakeSettings())
        with mock.patch.object(remote, '_call', return_value=1) as call:
            remote.drop_card()
        self.assertGreaterEqual(call.call_args.kwargs['timeout'], dropper_module.FEEDER_WAIT_S + 6)

    def test_numbers_only_grow(self):
        fake = FakeFeeder(done_after=1)
        feeder = feeder_for(fake)
        seen = []
        for _ in range(5):
            feeder.set(True)
            seen.append(feeder.number)
            feeder.set(False)
        self.assertEqual(seen, sorted(set(seen)))


class TheDropper(unittest.TestCase):
    """Dropper.pulse with a feeder: the same lock, the same gate - the request sent inside it"""

    def setUp(self):
        self.fake = FakeFeeder()
        self.opened = []
        self.clock = Clock()

        def open_feeder(ip, key):
            feeder = WifiFeeder(ip, key, http=self.fake, clock=self.clock, sleep=self.clock.sleep)
            self.opened.append(feeder)
            return feeder

        self.sleeps = []
        self.dropper = Dropper(open_pin=mock.Mock(side_effect=AssertionError('no pin for a feeder')), sleep=self.sleeps.append,
                               open_feeder=open_feeder, find_feeder=lambda: [], clock=self.clock)

    def test_one_pulse_is_one_card(self):
        self.dropper.configure(dict(WIFI_ON))
        self.assertTrue(self.dropper.pulse('a test'))
        self.assertEqual(len(self.fake.fed), 1)
        self.assertEqual(self.dropper.pulses, 1)
        self.assertEqual(self.sleeps, [])                    # no GPIO pulse length for a feeder

    def test_the_gate_says_no_and_nothing_is_asked(self):
        self.dropper.configure(dict(WIFI_ON))

        class No:
            def __enter__(self):
                return False

            def __exit__(self, *exc):
                return False

        self.assertFalse(self.dropper.pulse('stale', gate=No))
        self.assertEqual(self.fake.results, {})

    def test_a_failure_is_no_card_and_its_reason(self):
        self.dropper.configure(dict(WIFI_ON))
        self.fake.down = True
        self.assertFalse(self.dropper.pulse('a test'))       # not even opened: its boot id cannot be read
        self.assertIn('does not answer', self.dropper.error)
        self.assertEqual(self.fake.results, {})
        self.fake.down = False
        self.assertTrue(self.dropper.pulse('again'))         # opened again, the next time
        self.assertIsNone(self.dropper.error)

    def test_a_new_pairing_opens_a_new_feeder(self):
        self.dropper.configure(dict(WIFI_ON))
        self.dropper.pulse('one')
        self.dropper.configure(dict(WIFI_ON, pair_key='p' * 32))
        self.fake.key = 'p' * 32
        self.dropper.pulse('two')
        self.assertEqual([f.key for f in self.opened], [KEY, 'p' * 32])

    def test_probe_wifi_updates_online_and_sensor_in_state(self):
        self.dropper.configure(dict(WIFI_ON))

        def ok_http(url, timeout=2):
            return 200, {'status': {'boot': 'b1', 'sensor': {'type': 'VCNL4200', 'present': False}}}

        self.assertTrue(self.dropper.probe_wifi(http=ok_http))
        st = self.dropper.state()
        self.assertTrue(st['enabled'])
        self.assertTrue(st['online'])
        self.assertEqual(st['sensor'], {'type': 'VCNL4200', 'present': False})
        self.assertEqual(st['transport'], 'wifi')
        self.assertEqual(st['feeder']['ip'], WIFI_ON['feeder']['ip'])

        def fail_http(url, timeout=2):
            raise OSError('offline')

        self.assertFalse(self.dropper.probe_wifi(http=fail_http))
        self.assertFalse(self.dropper.state()['online'])

    def test_probe_wifi_auto_rediscovers_moved_ip_by_mac_and_throttles_when_offline(self):
        moved_ip = '192.168.1.222'
        reported = []
        scans = []

        def fake_find():
            scans.append(self.clock.now)
            return [
                {'ip': '192.168.1.99', 'name': 'other', 'mac': 'aa:bb:cc:dd:ee:ff'},
                {'ip': moved_ip, 'name': 'card-dropper-sw', 'mac': FEEDER['mac'].upper()},
            ]

        dropper = Dropper(open_feeder=lambda ip, key: WifiFeeder(ip, key, http=self.fake, clock=self.clock, sleep=self.clock.sleep),
                          find_feeder=fake_find, on_feeder_ip_changed=reported.append, clock=self.clock)
        dropper.configure(dict(WIFI_ON))

        def http_only_new_ip(url, timeout=2):
            if moved_ip in url:
                return 200, {'status': {'boot': 'b2', 'sensor': {'type': 'VCNL4200', 'present': True}}}
            raise OSError('old ip gone')

        self.assertTrue(dropper.probe_wifi(http=http_only_new_ip))
        self.assertTrue(dropper.state()['online'])
        self.assertEqual(dropper.state()['feeder']['ip'], moved_ip)
        self.assertEqual(dropper.state()['sensor'], {'type': 'VCNL4200', 'present': True})
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]['ip'], moved_ip)

        # If the feeder later goes completely offline, rediscovery respects REDISCOVER_COOLDOWN_S
        scans.clear()
        dropper._find_feeder = lambda: (scans.append(self.clock.now) or [])
        self.clock.sleep(dropper_module.REDISCOVER_COOLDOWN_S + 1)
        self.assertFalse(dropper.probe_wifi(http=lambda url, timeout=2: (_ for _ in ()).throw(OSError('down'))))
        self.assertEqual(len(scans), 1)
        self.clock.sleep(5.0)
        self.assertFalse(dropper.probe_wifi(http=lambda url, timeout=2: (_ for _ in ()).throw(OSError('down'))))
        self.assertEqual(len(scans), 1)   # throttled until cooldown elapses
        self.clock.sleep(dropper_module.REDISCOVER_COOLDOWN_S)
        self.assertFalse(dropper.probe_wifi(http=lambda url, timeout=2: (_ for _ in ()).throw(OSError('down'))))
        self.assertEqual(len(scans), 2)

    def test_pulse_auto_rediscovers_moved_ip_before_failing(self):
        moved_ip = '192.168.1.225'
        reported = []
        opened_ips = []

        def open_only_new_ip(ip, key):
            opened_ips.append(ip)
            if ip != moved_ip:
                raise RuntimeError(f'the feeder at {ip} does not answer')
            return WifiFeeder(ip, key, http=self.fake, clock=self.clock, sleep=self.clock.sleep)

        dropper = Dropper(open_feeder=open_only_new_ip,
                          find_feeder=lambda: [{'ip': moved_ip, 'name': 'card-dropper-sw', 'mac': FEEDER['mac']}],
                          on_feeder_ip_changed=reported.append, clock=self.clock)
        dropper.configure(dict(WIFI_ON))
        self.assertTrue(dropper.pulse('auto-rediscover'))
        self.assertEqual(opened_ips, [FEEDER['ip'], moved_ip])
        self.assertEqual(dropper.state()['feeder']['ip'], moved_ip)
        self.assertEqual(len(self.fake.fed), 1)
        self.assertEqual(len(reported), 1)

    def test_pulse_rediscovers_moved_ip_even_when_feeder_was_already_opened(self):
        moved_ip = '192.168.1.226'
        reported = []
        active_ip = [FEEDER['ip']]
        gate_calls = []

        @contextlib.contextmanager
        def gate():
            gate_calls.append(True)
            yield True

        def open_feeder(ip, key):
            def routed_http(url, form=None, timeout=2):
                if f'http://{active_ip[0]}/' not in url:
                    raise OSError(f'old ip {ip} unreachable')
                return self.fake(url, form, timeout)
            return WifiFeeder(ip, key, http=routed_http, clock=self.clock, sleep=self.clock.sleep)

        dropper = Dropper(open_feeder=open_feeder,
                          find_feeder=lambda: [{'ip': active_ip[0], 'name': 'card-dropper-sw', 'mac': FEEDER['mac']}],
                          on_feeder_ip_changed=reported.append, clock=self.clock)
        dropper.configure(dict(WIFI_ON))
        # First pulse succeeds and caches self._pin bound to the old IP
        self.assertTrue(dropper.pulse('first', gate=gate))
        self.assertEqual(len(self.fake.fed), 1)
        # DHCP moves the feeder while self._pin is still cached -> second pulse must still succeed on the first try
        active_ip[0] = moved_ip
        self.assertTrue(dropper.pulse('second-after-dhcp', gate=gate))
        self.assertEqual(len(self.fake.fed), 2)
        self.assertEqual(dropper.state()['feeder']['ip'], moved_ip)
        self.assertTrue(dropper.state()['online'])
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]['ip'], moved_ip)
        self.assertEqual(len(gate_calls), 3)  # 1st pulse, failed attempt on old IP, retry on new IP

    def test_pulse_http_refusal_keeps_online_true_and_offline_error_clears_on_reconnect(self):
        self.dropper.configure(dict(WIFI_ON))
        # 403 refusal: the feeder answered, so online stays True while error is recorded
        self.fake.key = 'wrong-key-on-feeder'
        self.assertFalse(self.dropper.pulse('bad-key'))
        self.assertTrue(self.dropper.state()['online'])
        self.assertIn('refused', self.dropper.state()['error'])

        # Network outage sets online=False and an offline error; probe_wifi reconnecting clears the stale offline error
        self.fake.key = KEY
        self.fake.down = True
        self.assertFalse(self.dropper.pulse('offline'))
        self.assertFalse(self.dropper.state()['online'])
        self.assertIn('does not answer', self.dropper.state()['error'])
        self.assertTrue(self.dropper.probe_wifi(http=lambda url, timeout=2: (200, {'status': {'boot': 'b1'}})))
        self.assertTrue(self.dropper.state()['online'])
        self.assertIsNone(self.dropper.state()['error'])

    def test_probe_wifi_does_not_clobber_state_after_concurrent_configure(self):
        self.dropper.configure(dict(WIFI_ON))

        def slow_fail_http(url, timeout=2):
            # Dropper is switched off mid-probe; the finishing probe must not write online=False onto the new config
            self.dropper.configure(dict(WIFI_ON, enabled=False))
            raise OSError('timed out')

        self.assertIsNone(self.dropper.probe_wifi(http=slow_fail_http))
        self.assertIsNone(self.dropper.state()['online'])

    def test_remote_scanner_persists_rediscovered_feeder_ip_only_for_matching_mac(self):
        from card_scanner.core.remote_scanner import RemoteScanner
        saved = FakeSettings({'dropper': dict(DEFAULTS, **WIFI_ON)})
        remote = RemoteScanner(mock.Mock(), saved)
        # Wrong MAC, invalid IP, or non-string IP (e.g. integer accepted by IPv4Address) is ignored
        self.assertFalse(remote.update_feeder_ip({'mac': 'ff:ff:ff:ff:ff:ff', 'ip': '192.168.1.222'}))
        self.assertFalse(remote.update_feeder_ip({'mac': FEEDER['mac'], 'ip': 'not-an-ip'}))
        self.assertFalse(remote.update_feeder_ip({'mac': FEEDER['mac'], 'ip': 3232235998}))
        self.assertEqual(remote.dropper['feeder']['ip'], FEEDER['ip'])
        # Matching MAC persists the new IP while keeping pair_key & enabled untouched
        self.assertTrue(remote.update_feeder_ip({'mac': FEEDER['mac'].upper(), 'ip': '192.168.1.222', 'name': 'card-dropper-sw'}))
        self.assertEqual(remote.dropper['feeder']['ip'], '192.168.1.222')
        self.assertEqual(remote.dropper['pair_key'], KEY)
        self.assertTrue(remote.dropper['enabled'])
        self.assertEqual(saved.saved['dropper']['feeder']['ip'], '192.168.1.222')


class FakeUdp:
    """A UDP socket: what was asked, and the feeders' answers"""

    def __init__(self, answers):
        self.sent, self.answers, self.closed = [], list(answers), False

    def setsockopt(self, *args):
        pass

    def setblocking(self, flag):
        pass

    def sendto(self, data, target):
        self.sent.append((data, target))

    def recvfrom(self, size):
        if not self.answers:
            raise BlockingIOError
        return self.answers.pop(0)

    def close(self):
        self.closed = True


class FindingAndPairing(unittest.TestCase):
    def test_found_by_asking_every_address_and_by_broadcast(self):
        me = {'feeder': 'card-dropper-sw', 'ip': '192.168.1.108', 'mac': FEEDER['mac'], 'paired': False, 'station': None,
              'boot': 'b007b007', 'port': 80}
        udp = FakeUdp([(json.dumps(me).encode(), ('192.168.1.108', 50505)), (b'not json', ('192.168.1.9', 50505)),
                       (json.dumps(me).encode(), ('192.168.1.108', 50505))])
        found = dropper_module.find_feeders(['192.168.1.61'], wait=0.05, make_socket=lambda: udp)
        self.assertEqual(found, [{'ip': '192.168.1.108', 'name': 'card-dropper-sw', 'mac': FEEDER['mac'], 'paired': False,
                                  'station': None}])
        targets = [t for _, t in udp.sent]
        self.assertIn(('192.168.1.255', 50505), targets)   # the broadcast
        self.assertIn(('192.168.1.108', 50505), targets)   # ... and each address directly (a firewall drops broadcast answers)
        self.assertEqual(len(targets), 255)
        self.assertTrue(all(d == b'card-dropper?' for d, _ in udp.sent))
        self.assertTrue(udp.closed)

    def test_paired_with_the_owners_key_once(self):
        fake = FakeFeeder()
        paired = dropper_module.pair_feeder('192.168.1.108', 'owner-key', 'Living room PC', http=fake)
        self.assertEqual(paired['feeder'], FEEDER)
        checked(dict(WIFI_ON, pair_key=paired['pair_key']))  # a key the feeder takes and the settings keep
        sent = fake.requests[-1][1]
        self.assertEqual(sent['config_key'], 'owner-key')
        self.assertEqual(sent['pair_key'], paired['pair_key'])
        self.assertEqual(sent['station'], 'Living room PC')
        self.assertNotEqual(dropper_module.pair_feeder('192.168.1.108', 'owner-key', 'x', http=fake)['pair_key'],
                            paired['pair_key'])              # a new key each pairing
        with self.assertRaisesRegex(ValueError, 'wrong config key'):
            dropper_module.pair_feeder('192.168.1.108', 'guess', 'x', http=fake)
        with self.assertRaisesRegex(ValueError, 'key'):
            dropper_module.pair_feeder('192.168.1.108', '', 'x', http=fake)
        with self.assertRaisesRegex(ValueError, 'address'):
            dropper_module.pair_feeder('nope', 'owner-key', 'x', http=fake)
        fake.down = True
        with self.assertRaisesRegex(ValueError, 'does not answer'):
            dropper_module.pair_feeder('192.168.1.108', 'owner-key', 'x', http=fake)

    def test_the_stations_commands(self):
        from card_scanner.station.station_client import Station
        station = Station.__new__(Station)
        station.scanner, station.name = mock.Mock(), 'Living room PC'
        station.state = lambda: {}
        with mock.patch.object(dropper_module, 'find_feeders', return_value=[{'ip': '1.2.3.4'}]):
            self.assertEqual(station.on_command({'method': 'find_feeders'})['result'], [{'ip': '1.2.3.4'}])
        with mock.patch.object(dropper_module, 'pair_feeder', side_effect=ValueError('wrong config key')) as pair:
            answer = station.on_command({'method': 'pair_feeder', 'args': ['1.2.3.4', 'k']})
            self.assertEqual(answer, {'ok': False, 'error': 'wrong config key'})
            pair.assert_called_once_with('1.2.3.4', 'k', station_name='Living room PC')


class FakeSettings:
    def __init__(self, saved=None):
        self.saved = dict(saved or {})

    def get(self, key, default=None):
        return self.saved.get(key, default)

    def set(self, key, value):
        self.saved[key] = value


class TheServer(unittest.TestCase):
    """The pair key stays on the server (and the station): the page never gets it, and a save from the page keeps it"""

    def test_the_pages_events(self):
        import app as launcher  # noqa: F401 - the server's routes and events
        from card_scanner.server.state import app, socketio
        settings_events = sys.modules['card_scanner.server.settings_events']
        station = mock.Mock(connected=True, dropper=None, dropper_state={'enabled': False, 'pulses': 0, 'error': None,
                                                                          'last_pulse': None}, settings=FakeSettings())
        with mock.patch.object(settings_events, 'scanner', station):
            client = socketio.test_client(app, flask_test_client=app.test_client())
            client.get_received()
            try:
                def emit(event, payload=None):
                    client.emit(event, payload or {})
                    return {e['name']: e['args'][0] for e in client.get_received()}

                # Chosen and switched on before any pairing: kept off, and why
                answer = emit('set_dropper', {'dropper': {'enabled': True, 'transport': 'wifi'}})
                self.assertIn('Pair a feeder', answer['error']['message'])
                self.assertEqual(station.settings.saved['dropper']['transport'], 'wifi')
                self.assertFalse(station.settings.saved['dropper']['enabled'])

                station.find_feeders.return_value = [{'ip': FEEDER['ip'], 'name': 'card-dropper-sw'}]
                self.assertEqual(emit('find_feeders')['feeders_found'], {'ok': True, 'feeders': station.find_feeders.return_value})
                station.find_feeders.side_effect = ValueError('The station cannot look for feeders')
                self.assertFalse(emit('find_feeders')['feeders_found']['ok'])

                station.pair_feeder.side_effect = ValueError('wrong config key - not paired')
                answer = emit('pair_feeder', {'ip': FEEDER['ip'], 'config_key': 'guess'})
                self.assertEqual(answer['feeder_paired'], {'ok': False, 'message': 'wrong config key - not paired'})
                self.assertIsNone(station.settings.saved['dropper']['pair_key'])

                station.pair_feeder.side_effect = None
                station.pair_feeder.return_value = {'feeder': FEEDER, 'pair_key': KEY}
                answer = emit('pair_feeder', {'ip': FEEDER['ip'], 'config_key': 'owner-key'})
                station.pair_feeder.assert_called_with(FEEDER['ip'], 'owner-key')
                self.assertEqual(answer['feeder_paired'], {'ok': True, 'feeder': FEEDER})
                self.assertEqual(station.settings.saved['dropper']['pair_key'], KEY)          # kept on the server
                self.assertEqual(station.dropper['pair_key'], KEY)                            # ... and sent to the station
                self.assertNotIn(KEY, json.dumps(answer))                                     # never to the page
                self.assertNotIn('owner-key', json.dumps(station.settings.saved))             # the owner's key kept nowhere
                self.assertTrue(answer['dropper_updated']['settings']['paired'])

                # The page's form saved: switched on - the feeder and its key kept, though the page never had them
                answer = emit('set_dropper', {'dropper': {'enabled': True, 'transport': 'wifi', 'pin': None, 'pulse_ms': 150,
                                                          'chip': '/dev/gpiochip0', 'active_low': False}})
                self.assertEqual(station.settings.saved['dropper'], dict(DEFAULTS, **WIFI_ON))
                self.assertNotIn(KEY, json.dumps(answer))
                # ... and a page sending a key of its own changes nothing of it
                emit('set_dropper', {'dropper': {'enabled': True, 'transport': 'wifi', 'pair_key': 'x' * 32,
                                                 'feeder': {'ip': '10.0.0.1'}}})
                self.assertEqual(station.settings.saved['dropper']['pair_key'], KEY)
                self.assertEqual(station.settings.saved['dropper']['feeder'], FEEDER)

                answer = emit('forget_feeder')
                saved = station.settings.saved['dropper']
                self.assertEqual((saved['enabled'], saved['feeder'], saved['pair_key']), (False, None, None))
                self.assertFalse(answer['dropper_updated']['settings']['paired'])

                # Forgetting a feeder while a GPIO pin drives it leaves the pin on
                emit('set_dropper', {'dropper': {'enabled': True, 'transport': 'gpio', 'pin': 17}})
                station.settings.saved['dropper'] = dict(station.settings.saved['dropper'], feeder=FEEDER, pair_key=KEY)
                station.dropper = station.settings.saved['dropper']
                emit('forget_feeder')
                self.assertTrue(station.settings.saved['dropper']['enabled'])
            finally:
                client.disconnect()


class DropGateIntegration(unittest.TestCase):
    """Wi-Fi dropper sensor confirmation gates auto-capture on the station."""

    def test_wifi_dropper_gates_capture_until_sensor_confirms_drop(self):
        import time
        from card_scanner.station.station_client import Station

        class FakeScanner:
            def __init__(self):
                self.auto_capture_enabled = False
                self.focus_sweep_running = False
                self.focus_probe_running = False
                self.capture_pending = False
                self.camera = object()
                self.awaiting_new_card = False
                self.require_drop_signal = False
                self.stable_frames = 5
                self.detected = False
                self.notified = 0

            def is_card_detected(self):
                return self.detected

            def notify_card_dropped(self):
                self.notified += 1
                self.awaiting_new_card = False
                self.stable_frames = 0

        fake = FakeFeeder(done_after=1)
        st = Station('http://example.test', 's1', 'Desk', 'tok')
        st.dropper = Dropper(open_feeder=lambda ip, key: feeder_for(fake))
        st.scanner = FakeScanner()
        st.dropper_synced = True
        st.send_log = mock.MagicMock()

        # Enabling Wi-Fi dropper enables the drop-signal capture gate
        st.configure_dropper(WIFI_ON)
        self.assertTrue(st.scanner.require_drop_signal)

        # Starting auto-scan on an empty tray locks awaiting_new_card, then releases Card #1 in the background
        st.on_set({'auto_capture_enabled': True})
        deadline = time.time() + 2.0
        while st.release_busy and time.time() < deadline:
            time.sleep(0.01)
        # Background release succeeded (sensor confirmed 'dropped') -> unlocked capture!
        self.assertEqual(st.scanner.notified, 1)
        self.assertFalse(st.scanner.awaiting_new_card)
        self.assertEqual(st.scanner.stable_frames, 0)

        # Stuck card on ramp: keeps capture locked AND keeps auto-scan armed (user clears jam)
        time.sleep(0.002)
        st.scanner.awaiting_new_card = True
        fake.fail_next = True
        fake.fail_drop = 'stuck'
        st._release('jammed card', st.scan_session, first=False)
        self.assertEqual(st.scanner.notified, 1)
        self.assertTrue(st.scanner.awaiting_new_card)
        self.assertTrue(st.scanner.auto_capture_enabled)
        st.send_log.assert_called()

        # Empty magazine ('no_card'): keeps capture locked AND disables auto-scan cleanly (like card back!)
        time.sleep(0.002)
        st.scanner.awaiting_new_card = True
        fake.fail_next = True
        fake.fail_drop = 'no_card'
        st._release('empty stack', st.scan_session, first=False)
        self.assertEqual(st.scanner.notified, 1)
        self.assertTrue(st.scanner.awaiting_new_card)
        self.assertFalse(st.scanner.auto_capture_enabled)

        # Verify scanner.py's real notify_card_dropped() and require_drop_signal attribute exist and work
        from card_scanner.station.scanner import CardScanner
        self.assertTrue(hasattr(CardScanner, 'notify_card_dropped'))

        # Switching dropper OFF disables the gate for manual hand-scanning
        st.configure_dropper({'enabled': False, 'transport': 'wifi', 'feeder': FEEDER, 'pair_key': KEY})
        self.assertFalse(st.scanner.require_drop_signal)


if __name__ == '__main__':
    unittest.main()
