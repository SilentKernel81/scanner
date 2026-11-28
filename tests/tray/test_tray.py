#!/usr/bin/env python3
"""
Tests for the standalone system tray module (packaging/tray/dbus_sni.py and launch.py).

Run:  venv/bin/python -m unittest discover -s tests/tray -t tests/tray

Standalone-only file (lives under tests/tray/ so syncing from scanner-server never touches it).
"""
import os
import signal
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

TOP = Path(__file__).resolve().parent.parent.parent
TRAY_DIR = TOP / 'packaging' / 'tray'
if str(TRAY_DIR) not in sys.path:
    sys.path.insert(0, str(TRAY_DIR))

import dbus_sni  # noqa: E402
import launch  # noqa: E402


class TestDBusWireProtocol(unittest.TestCase):
    """D-Bus signature splitting, marshaling, unmarshaling, and message framing."""

    def test_split_signature_nested_containers(self):
        self.assertEqual(dbus_sni.split_signature('u(ia{sv}av)'), ['u', '(ia{sv}av)'])
        self.assertEqual(dbus_sni.split_signature('(sa(iiay)ss)'), ['(sa(iiay)ss)'])
        self.assertEqual(dbus_sni.split_signature('a(isvu)'), ['a(isvu)'])
        self.assertEqual(dbus_sni.split_signature('aiai'), ['ai', 'ai'])

    def test_roundtrip_sni_and_dbusmenu_payloads(self):
        layout = (
            0,
            {'children-display': ('s', 'submenu')},
            [
                ('(ia{sv}av)', (1, {'label': ('s', 'Open Card Scanner'), 'enabled': ('b', True), 'visible': ('b', True)}, [])),
                ('(ia{sv}av)', (2, {'type': ('s', 'separator'), 'visible': ('b', True)}, [])),
                ('(ia{sv}av)', (3, {'label': ('s', 'Restart'), 'enabled': ('b', True), 'visible': ('b', True)}, [])),
                ('(ia{sv}av)', (4, {'label': ('s', 'Quit'), 'enabled': ('b', True), 'visible': ('b', True)}, [])),
            ],
        )
        raw = dbus_sni.encode_message(
            dbus_sni.MSG_METHOD_RETURN,
            serial=7,
            destination=':1.42',
            reply_serial=3,
            signature='u(ia{sv}av)',
            body=(1, layout),
        )
        buf = bytearray(raw)
        decoded = dbus_sni.try_decode_message(buf)
        self.assertIsNotNone(decoded)
        self.assertEqual(len(buf), 0)
        assert decoded is not None
        self.assertEqual(decoded.msg_type, dbus_sni.MSG_METHOD_RETURN)
        self.assertEqual(decoded.serial, 7)
        self.assertEqual(decoded.reply_serial, 3)
        self.assertEqual(decoded.destination, ':1.42')
        self.assertEqual(decoded.signature, 'u(ia{sv}av)')
        self.assertEqual(decoded.body[0], 1)
        self.assertEqual(decoded.body[1][0], 0)
        self.assertEqual(decoded.body[1][1], {'children-display': ('s', 'submenu')})
        self.assertEqual(len(decoded.body[1][2]), 4)
        self.assertEqual(decoded.body[1][2][0][1][1]['label'], ('s', 'Open Card Scanner'))

    def test_empty_struct_array_alignment(self):
        # Empty a(iiay) must still align after its 4-byte length to 8 bytes per the D-Bus spec
        body = dbus_sni.marshal_body('sa(iiay)ss', ('', [], 'Title', 'Subtitle'))
        values = dbus_sni.unmarshal_body('sa(iiay)ss', body)
        self.assertEqual(values, ['', [], 'Title', 'Subtitle'])

    def test_big_endian_decode(self):
        # Verify decoding a big-endian message works cleanly
        body = dbus_sni.marshal_body('si', ('hello', 42), endian='>')
        header = bytearray(struct.pack('>BBBBII', ord('B'), dbus_sni.MSG_METHOD_CALL, 0, 1, len(body), 9))
        dbus_sni.marshal_value(
            header, 'a(yv)',
            [(dbus_sni.HDR_MEMBER, ('s', 'Ping')), (dbus_sni.HDR_SIGNATURE, ('g', 'si'))],
            endian='>',
        )
        while len(header) % 8:
            header.append(0)
        buf = bytearray(header + body)
        msg = dbus_sni.try_decode_message(buf)
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.member, 'Ping')
        self.assertEqual(msg.body, ['hello', 42])

    def test_parse_dbus_addresses(self):
        addrs = dbus_sni.parse_dbus_addresses(
            'unix:path=/run/user/1000/bus,guid=abc;unix:abstract=/tmp/dbus-foo%20bar'
        )
        self.assertEqual(addrs, ['/run/user/1000/bus', b'\x00/tmp/dbus-foo bar'])

    def test_icon_pixmap_format(self):
        w, h, data = dbus_sni.render_card_icon_argb(32)
        self.assertEqual((w, h), (32, 32))
        self.assertEqual(len(data), 32 * 32 * 4)
        # Corners are transparent (rounded square), center is opaque
        self.assertEqual(data[0], 0)
        center_alpha = data[((16 * 32) + 16) * 4]
        self.assertEqual(center_alpha, 255)


class TestTrayIconDispatch(unittest.TestCase):
    """TrayIcon handles SNI properties, DBusMenu calls, Activate, and Event callbacks over a socketpair."""

    def _run_pair(self, tray: dbus_sni.TrayIcon, incoming: list[bytes]) -> list[dbus_sni.DBusMessage]:
        s_tray, s_peer = socket.socketpair()
        conn = dbus_sni.DBusConnection(s_tray)
        tray._conn = conn
        try:
            for raw in incoming:
                s_peer.sendall(raw)
            messages = conn.read_messages()
            for m in messages:
                tray._handle_message(m)
            s_peer.settimeout(0.5)
            out_buf = bytearray()
            replies: list[dbus_sni.DBusMessage] = []
            while True:
                try:
                    chunk = s_peer.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                out_buf.extend(chunk)
                while True:
                    decoded = dbus_sni.try_decode_message(out_buf)
                    if decoded is None:
                        break
                    replies.append(decoded)
            return replies
        finally:
            conn.close()
            s_peer.close()
            tray._conn = None

    def test_sni_properties_and_activate(self):
        activated = threading.Event()
        clicked = threading.Event()
        tray = dbus_sni.TrayIcon(
            icon_theme_path=TOP / 'packaging',
            on_activate=activated.set,
            menu_items=[
                dbus_sni.MenuItem(id=1, label='Open Card Scanner', callback=clicked.set),
                dbus_sni.MenuItem(id=2, separator=True),
                dbus_sni.MenuItem(id=3, label='Quit'),
            ],
        )
        calls = [
            dbus_sni.encode_message(
                dbus_sni.MSG_METHOD_CALL, 1,
                path='/StatusNotifierItem',
                interface='org.freedesktop.DBus.Properties',
                member='GetAll',
                signature='s',
                body=('org.kde.StatusNotifierItem',),
            ),
            dbus_sni.encode_message(
                dbus_sni.MSG_METHOD_CALL, 2,
                path='/StatusNotifierItem',
                interface='org.kde.StatusNotifierItem',
                member='Activate',
                signature='ii',
                body=(0, 0),
            ),
            dbus_sni.encode_message(
                dbus_sni.MSG_METHOD_CALL, 3,
                path='/MenuBar',
                interface='com.canonical.dbusmenu',
                member='GetLayout',
                signature='iias',
                body=(0, -1, []),
            ),
            dbus_sni.encode_message(
                dbus_sni.MSG_METHOD_CALL, 4,
                path='/MenuBar',
                interface='com.canonical.dbusmenu',
                member='Event',
                signature='isvu',
                body=(1, 'clicked', ('i', 0), 0),
            ),
        ]
        replies = self._run_pair(tray, calls)
        self.assertEqual([r.reply_serial for r in replies], [1, 2, 3, 4])

        # Check SNI GetAll properties
        props = replies[0].body[0]
        self.assertEqual(props['Id'], ('s', 'mtg-scanner'))
        self.assertEqual(props['Title'], ('s', 'Card Scanner'))
        self.assertEqual(props['Menu'], ('o', '/MenuBar'))
        self.assertEqual(props['IconName'], ('s', 'mtg-scanner'))
        self.assertTrue(len(props['IconPixmap'][1]) >= 1)

        # Check DBusMenu GetLayout
        revision, layout = replies[2].body
        self.assertEqual(revision, 1)
        self.assertEqual(len(layout[2]), 3)
        self.assertEqual(layout[2][0][1][1]['label'], ('s', 'Open Card Scanner'))

        # Callbacks run asynchronously on worker threads
        self.assertTrue(activated.wait(timeout=2.0))
        self.assertTrue(clicked.wait(timeout=2.0))


class TestLauncher(unittest.TestCase):
    """Supervisor URL resolution, single-instance lock detection, restart, and quit."""

    def test_compute_local_url_defaults_and_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp) / 'app'
            user_dir = Path(tmp) / 'user'
            (app_dir).mkdir()
            (user_dir / 'data').mkdir(parents=True)
            (app_dir / 'config.yaml').write_text("flask:\n  host: auto\n  port: 5000\n", encoding='utf-8')
            self.assertEqual(launch.compute_local_url(app_dir, user_dir), ('http://localhost:5000', 'localhost', 5000))

            # User config overlay overrides port and host
            (user_dir / 'config.yaml').write_text("flask:\n  host: 192.168.1.50\n  port: 5123\n", encoding='utf-8')
            self.assertEqual(
                launch.compute_local_url(app_dir, user_dir),
                ('http://192.168.1.50:5123', '192.168.1.50', 5123),
            )

            # Saved network_access=False forces localhost
            (user_dir / 'data' / 'settings.json').write_text('{"network_access": false}', encoding='utf-8')
            self.assertEqual(
                launch.compute_local_url(app_dir, user_dir),
                ('http://localhost:5123', 'localhost', 5123),
            )

    def test_supervisor_restart_and_quit(self):
        """Supervisor spawns a dummy app.py, restarts it with --no-browser, then quits cleanly on SIGTERM."""
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp)
            marker = app_dir / 'runs.txt'
            # Dummy app.py that logs its argv and sleeps until SIGTERM
            (app_dir / 'app.py').write_text(
                "import signal, sys, time\n"
                f"with open({str(marker)!r}, 'a') as f:\n"
                "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
                "stop = False\n"
                "def _s(a, b):\n"
                "    global stop; stop = True\n"
                "signal.signal(signal.SIGTERM, _s)\n"
                "while not stop:\n"
                "    time.sleep(0.05)\n",
                encoding='utf-8',
            )
            (app_dir / 'config.yaml').write_text("flask:\n  port: 59991\n", encoding='utf-8')

            sup = launch.Supervisor(app_dir, app_dir, ['--no-station'], use_tray=False)

            def drive():
                # Wait for first run
                for _ in range(50):
                    if marker.is_file() and len(marker.read_text().splitlines()) >= 1:
                        break
                    time.sleep(0.05)
                sup.request_restart()
                # Wait for second run
                for _ in range(50):
                    if marker.is_file() and len(marker.read_text().splitlines()) >= 2:
                        break
                    time.sleep(0.05)
                sup.request_quit()

            driver = threading.Thread(target=drive, daemon=True)
            driver.start()
            with mock.patch.dict(os.environ, {'MTG_SCANNER_HOME': tmp}):
                sup.user_dir = app_dir
                rc = sup.run()
            driver.join(timeout=5.0)

            self.assertEqual(rc, 0)
            lines = marker.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0], '--app --no-station')
            self.assertEqual(lines[1], '--app --no-station --no-browser')


if __name__ == '__main__':
    unittest.main()
