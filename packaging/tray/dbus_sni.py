"""
Pure-Python Linux system tray icon using StatusNotifierItem + DBusMenu over D-Bus.

Zero external dependencies (talks directly to the session D-Bus Unix socket), so it works
inside a portable AppImage on Wayland and X11 (Hyprland/Waybar, KDE Plasma, GNOME with
AppIndicator, XFCE, Cinnamon, Sway, Quickshell/Omarchy) without GTK, Qt, or libappindicator.

Standalone-only file (lives under packaging/tray/ so syncing from scanner-server never touches it).
"""
from __future__ import annotations

import logging
import os
import select
import socket
import struct
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger('card_scanner.tray')

# D-Bus message types
MSG_METHOD_CALL = 1
MSG_METHOD_RETURN = 2
MSG_ERROR = 3
MSG_SIGNAL = 4

# D-Bus header field codes
HDR_PATH = 1
HDR_INTERFACE = 2
HDR_MEMBER = 3
HDR_ERROR_NAME = 4
HDR_REPLY_SERIAL = 5
HDR_DESTINATION = 6
HDR_SENDER = 7
HDR_SIGNATURE = 8

# Basic type alignments and struct format characters
_BASIC_ALIGN = {
    'y': 1, 'b': 4, 'n': 2, 'q': 2, 'i': 4, 'u': 4,
    'x': 8, 't': 8, 'd': 8, 'h': 4, 's': 4, 'o': 4, 'g': 1,
}
_BASIC_FMT = {
    'y': 'B', 'b': 'I', 'n': 'h', 'q': 'H', 'i': 'i', 'u': 'I',
    'x': 'q', 't': 'Q', 'd': 'd', 'h': 'I',
}


# ---------------------------------------------------------------------------
# D-Bus wire protocol marshaling / unmarshaling
# ---------------------------------------------------------------------------

def split_signature(sig: str) -> list[str]:
    """Split a D-Bus type signature into its top-level complete type strings."""
    result: list[str] = []
    i = 0
    n = len(sig)
    while i < n:
        start = i
        while i < n and sig[i] == 'a':
            i += 1
        if i >= n:
            raise ValueError(f"Incomplete array signature: {sig!r}")
        ch = sig[i]
        if ch in ('(', '{'):
            close = ')' if ch == '(' else '}'
            depth = 1
            i += 1
            while i < n and depth > 0:
                if sig[i] == ch:
                    depth += 1
                elif sig[i] == close:
                    depth -= 1
                i += 1
            if depth != 0:
                raise ValueError(f"Unclosed container in signature: {sig!r}")
        else:
            i += 1
        result.append(sig[start:i])
    return result


def type_alignment(t: str) -> int:
    """Return the D-Bus byte alignment for a single complete type."""
    ch = t[0]
    if ch in _BASIC_ALIGN:
        return _BASIC_ALIGN[ch]
    if ch == 'a':
        return 4
    if ch in ('(', '{'):
        return 8
    if ch == 'v':
        return 1
    raise ValueError(f"Unknown D-Bus type: {t!r}")


def _pad(buf: bytearray, align: int) -> None:
    rem = len(buf) % align
    if rem:
        buf.extend(b'\x00' * (align - rem))


def marshal_value(buf: bytearray, typ: str, value: Any, endian: str = '<') -> None:
    """Append a single D-Bus value of type `typ` to `buf`."""
    ch = typ[0]
    if ch in _BASIC_FMT:
        _pad(buf, _BASIC_ALIGN[ch])
        if ch == 'b':
            value = 1 if value else 0
        buf.extend(struct.pack(endian + _BASIC_FMT[ch], value))
    elif ch in ('s', 'o'):
        _pad(buf, 4)
        encoded = value.encode('utf-8') if isinstance(value, str) else bytes(value)
        buf.extend(struct.pack(endian + 'I', len(encoded)))
        buf.extend(encoded)
        buf.append(0)
    elif ch == 'g':
        encoded = value.encode('ascii') if isinstance(value, str) else bytes(value)
        buf.append(len(encoded))
        buf.extend(encoded)
        buf.append(0)
    elif ch == 'v':
        # Variant represented as (signature, inner_value)
        inner_sig, inner_val = value
        marshal_value(buf, 'g', inner_sig, endian)
        marshal_value(buf, inner_sig, inner_val, endian)
    elif ch == '(':
        _pad(buf, 8)
        fields = split_signature(typ[1:-1])
        if len(fields) != len(value):
            raise ValueError(f"Struct {typ} expects {len(fields)} fields, got {len(value)}")
        for field_type, field_val in zip(fields, value):
            marshal_value(buf, field_type, field_val, endian)
    elif ch == '{':
        _pad(buf, 8)
        key_type, val_type = split_signature(typ[1:-1])
        k, v = value
        marshal_value(buf, key_type, k, endian)
        marshal_value(buf, val_type, v, endian)
    elif ch == 'a':
        elem_type = typ[1:]
        _pad(buf, 4)
        len_pos = len(buf)
        buf.extend(b'\x00\x00\x00\x00')
        _pad(buf, type_alignment(elem_type))
        start = len(buf)
        if elem_type == 'y' and isinstance(value, (bytes, bytearray, memoryview)):
            buf.extend(value)
        elif elem_type.startswith('{') and isinstance(value, dict):
            for item in value.items():
                marshal_value(buf, elem_type, item, endian)
        else:
            for item in value:
                marshal_value(buf, elem_type, item, endian)
        byte_len = len(buf) - start
        buf[len_pos:len_pos + 4] = struct.pack(endian + 'I', byte_len)
    else:
        raise ValueError(f"Unsupported D-Bus type: {typ!r}")


def marshal_body(signature: str, values: tuple[Any, ...] | list[Any], endian: str = '<') -> bytes:
    buf = bytearray()
    types = split_signature(signature)
    if len(types) != len(values):
        raise ValueError(f"Signature {signature!r} expects {len(types)} values, got {len(values)}")
    for typ, val in zip(types, values):
        marshal_value(buf, typ, val, endian)
    return bytes(buf)


def _align_pos(pos: int, align: int) -> int:
    rem = pos % align
    return pos + (align - rem) if rem else pos


def unmarshal_value(data: bytes, pos: int, typ: str, endian: str = '<') -> tuple[Any, int]:
    """Decode one D-Bus value of type `typ` starting at `pos`; return `(value, new_pos)`."""
    ch = typ[0]
    if ch in _BASIC_FMT:
        pos = _align_pos(pos, _BASIC_ALIGN[ch])
        fmt = endian + _BASIC_FMT[ch]
        size = struct.calcsize(fmt)
        (val,) = struct.unpack_from(fmt, data, pos)
        if ch == 'b':
            val = bool(val)
        return val, pos + size
    if ch in ('s', 'o'):
        pos = _align_pos(pos, 4)
        (length,) = struct.unpack_from(endian + 'I', data, pos)
        pos += 4
        val = data[pos:pos + length].decode('utf-8')
        return val, pos + length + 1
    if ch == 'g':
        length = data[pos]
        pos += 1
        val = data[pos:pos + length].decode('ascii')
        return val, pos + length + 1
    if ch == 'v':
        inner_sig, pos = unmarshal_value(data, pos, 'g', endian)
        inner_val, pos = unmarshal_value(data, pos, inner_sig, endian)
        return (inner_sig, inner_val), pos
    if ch == '(':
        pos = _align_pos(pos, 8)
        items = []
        for field_type in split_signature(typ[1:-1]):
            val, pos = unmarshal_value(data, pos, field_type, endian)
            items.append(val)
        return tuple(items), pos
    if ch == '{':
        pos = _align_pos(pos, 8)
        key_type, val_type = split_signature(typ[1:-1])
        k, pos = unmarshal_value(data, pos, key_type, endian)
        v, pos = unmarshal_value(data, pos, val_type, endian)
        return (k, v), pos
    if ch == 'a':
        elem_type = typ[1:]
        pos = _align_pos(pos, 4)
        (byte_len,) = struct.unpack_from(endian + 'I', data, pos)
        pos += 4
        pos = _align_pos(pos, type_alignment(elem_type))
        end = pos + byte_len
        if elem_type == 'y':
            return data[pos:end], end
        if elem_type.startswith('{'):
            result_dict: dict[Any, Any] = {}
            while pos < end:
                (k, v), pos = unmarshal_value(data, pos, elem_type, endian)
                result_dict[k] = v
            return result_dict, end
        result_list: list[Any] = []
        while pos < end:
            item, pos = unmarshal_value(data, pos, elem_type, endian)
            result_list.append(item)
        return result_list, end
    raise ValueError(f"Unsupported D-Bus type: {typ!r}")


def unmarshal_body(signature: str, data: bytes, endian: str = '<') -> list[Any]:
    if not signature:
        return []
    pos = 0
    out = []
    for typ in split_signature(signature):
        val, pos = unmarshal_value(data, pos, typ, endian)
        out.append(val)
    return out


@dataclass
class DBusMessage:
    msg_type: int
    serial: int
    flags: int = 0
    path: str | None = None
    interface: str | None = None
    member: str | None = None
    error_name: str | None = None
    reply_serial: int | None = None
    destination: str | None = None
    sender: str | None = None
    signature: str = ''
    body: list[Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.body is None:
            self.body = []


def encode_message(
    msg_type: int,
    serial: int,
    *,
    flags: int = 0,
    path: str | None = None,
    interface: str | None = None,
    member: str | None = None,
    error_name: str | None = None,
    reply_serial: int | None = None,
    destination: str | None = None,
    signature: str = '',
    body: tuple[Any, ...] | list[Any] = (),
) -> bytes:
    body_bytes = marshal_body(signature, body) if signature else b''
    fields: list[tuple[int, tuple[str, Any]]] = []
    if path is not None:
        fields.append((HDR_PATH, ('o', path)))
    if interface is not None:
        fields.append((HDR_INTERFACE, ('s', interface)))
    if member is not None:
        fields.append((HDR_MEMBER, ('s', member)))
    if error_name is not None:
        fields.append((HDR_ERROR_NAME, ('s', error_name)))
    if reply_serial is not None:
        fields.append((HDR_REPLY_SERIAL, ('u', reply_serial)))
    if destination is not None:
        fields.append((HDR_DESTINATION, ('s', destination)))
    if signature:
        fields.append((HDR_SIGNATURE, ('g', signature)))

    buf = bytearray(struct.pack('<BBBBII', ord('l'), msg_type, flags, 1, len(body_bytes), serial))
    marshal_value(buf, 'a(yv)', fields, '<')
    _pad(buf, 8)
    buf.extend(body_bytes)
    return bytes(buf)


def try_decode_message(buffer: bytearray) -> DBusMessage | None:
    """Decode one complete D-Bus message from the start of `buffer`, consuming its bytes."""
    if len(buffer) < 16:
        return None
    endian_byte = buffer[0]
    if endian_byte == ord('l'):
        endian = '<'
    elif endian_byte == ord('B'):
        endian = '>'
    else:
        raise ValueError(f"Invalid D-Bus endian byte: {endian_byte!r}")

    _, msg_type, flags, _version, body_len, serial, fields_len = struct.unpack_from(
        endian + 'BBBBIII', buffer, 0
    )
    header_end = _align_pos(16 + fields_len, 8)
    total_len = header_end + body_len
    if len(buffer) < total_len:
        return None

    raw = bytes(buffer[:total_len])
    del buffer[:total_len]

    fields_list, _ = unmarshal_value(raw, 12, 'a(yv)', endian)
    hdr: dict[int, Any] = {code: val[1] for code, val in fields_list}
    sig = hdr.get(HDR_SIGNATURE, '')
    body_data = raw[header_end:total_len]
    body = unmarshal_body(sig, body_data, endian) if sig else []
    return DBusMessage(
        msg_type=msg_type,
        serial=serial,
        flags=flags,
        path=hdr.get(HDR_PATH),
        interface=hdr.get(HDR_INTERFACE),
        member=hdr.get(HDR_MEMBER),
        error_name=hdr.get(HDR_ERROR_NAME),
        reply_serial=hdr.get(HDR_REPLY_SERIAL),
        destination=hdr.get(HDR_DESTINATION),
        sender=hdr.get(HDR_SENDER),
        signature=sig,
        body=body,
    )


# ---------------------------------------------------------------------------
# D-Bus Unix socket connection
# ---------------------------------------------------------------------------

def parse_dbus_addresses(address: str | None = None) -> list[str | bytes]:
    """Return candidate Unix socket addresses from DBUS_SESSION_BUS_ADDRESS."""
    raw = address if address is not None else os.environ.get('DBUS_SESSION_BUS_ADDRESS', '')
    if not raw:
        runtime = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
        raw = f'unix:path={runtime}/bus'
    targets: list[str | bytes] = []
    for entry in raw.split(';'):
        entry = entry.strip()
        if not entry.startswith('unix:'):
            continue
        params: dict[str, str] = {}
        for pair in entry[5:].split(','):
            if '=' in pair:
                k, v = pair.split('=', 1)
                params[k] = v
        if 'path' in params:
            targets.append(urllib.parse.unquote(params['path']))
        elif 'abstract' in params:
            targets.append(b'\x00' + urllib.parse.unquote_to_bytes(params['abstract']))
        elif params.get('runtime') == 'yes':
            runtime = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
            targets.append(f'{runtime}/bus')
    return targets


class DBusConnection:
    """Minimal blocking/select-friendly session D-Bus client connection."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._recv_buf = bytearray()
        self._serial = 0
        self._send_lock = threading.Lock()
        self.unique_name: str = ''

    @classmethod
    def connect_session(cls, address: str | None = None, timeout: float = 5.0) -> DBusConnection:
        last_err: OSError | None = None
        for target in parse_dbus_addresses(address):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            try:
                sock.connect(target)
                conn = cls(sock)
                conn._authenticate()
                conn.unique_name = conn.hello()
                sock.settimeout(None)
                return conn
            except OSError as e:
                last_err = e
                sock.close()
        raise ConnectionError(f"Could not connect to session D-Bus: {last_err}")

    def _authenticate(self) -> None:
        uid_hex = str(os.geteuid()).encode('ascii').hex().encode('ascii')
        self.sock.sendall(b'\x00AUTH EXTERNAL ' + uid_hex + b'\r\n')
        line = self._read_auth_line()
        if not line.startswith(b'OK '):
            self.sock.sendall(b'AUTH ANONYMOUS\r\n')
            line = self._read_auth_line()
            if not line.startswith(b'OK '):
                raise ConnectionError(f"D-Bus SASL authentication failed: {line!r}")
        self.sock.sendall(b'BEGIN\r\n')

    def _read_auth_line(self) -> bytes:
        while True:
            idx = self._recv_buf.find(b'\r\n')
            if idx != -1:
                line = bytes(self._recv_buf[:idx])
                del self._recv_buf[:idx + 2]
                return line
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("D-Bus closed connection during authentication")
            self._recv_buf.extend(chunk)

    def next_serial(self) -> int:
        with self._send_lock:
            self._serial += 1
            return self._serial

    def send_raw(self, data: bytes) -> None:
        with self._send_lock:
            self.sock.sendall(data)

    def call_async(
        self,
        destination: str,
        path: str,
        interface: str,
        member: str,
        signature: str = '',
        body: tuple[Any, ...] | list[Any] = (),
    ) -> int:
        serial = self.next_serial()
        raw = encode_message(
            MSG_METHOD_CALL, serial,
            destination=destination, path=path, interface=interface, member=member,
            signature=signature, body=body,
        )
        self.send_raw(raw)
        return serial

    def send_reply(
        self,
        in_reply_to: DBusMessage,
        signature: str = '',
        body: tuple[Any, ...] | list[Any] = (),
    ) -> None:
        if in_reply_to.flags & 0x1:  # NO_REPLY_EXPECTED
            return
        serial = self.next_serial()
        raw = encode_message(
            MSG_METHOD_RETURN, serial,
            destination=in_reply_to.sender,
            reply_serial=in_reply_to.serial,
            signature=signature,
            body=body,
        )
        self.send_raw(raw)

    def send_error(self, in_reply_to: DBusMessage, error_name: str, message: str) -> None:
        if in_reply_to.flags & 0x1:
            return
        serial = self.next_serial()
        raw = encode_message(
            MSG_ERROR, serial,
            destination=in_reply_to.sender,
            reply_serial=in_reply_to.serial,
            error_name=error_name,
            signature='s',
            body=(message,),
        )
        self.send_raw(raw)

    def emit_signal(
        self,
        path: str,
        interface: str,
        member: str,
        signature: str = '',
        body: tuple[Any, ...] | list[Any] = (),
    ) -> None:
        serial = self.next_serial()
        raw = encode_message(
            MSG_SIGNAL, serial,
            path=path, interface=interface, member=member,
            signature=signature, body=body,
        )
        self.send_raw(raw)

    def read_messages(self) -> list[DBusMessage]:
        """Read available bytes from the socket and return any complete decoded messages."""
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("D-Bus connection closed")
        self._recv_buf.extend(chunk)
        messages: list[DBusMessage] = []
        while True:
            msg = try_decode_message(self._recv_buf)
            if msg is None:
                break
            messages.append(msg)
        return messages

    def call_sync(
        self,
        destination: str,
        path: str,
        interface: str,
        member: str,
        signature: str = '',
        body: tuple[Any, ...] | list[Any] = (),
        timeout: float = 5.0,
    ) -> DBusMessage:
        serial = self.call_async(destination, path, interface, member, signature, body)
        old_timeout = self.sock.gettimeout()
        self.sock.settimeout(timeout)
        try:
            while True:
                msg = try_decode_message(self._recv_buf)
                if msg is None:
                    chunk = self.sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("D-Bus closed while waiting for reply")
                    self._recv_buf.extend(chunk)
                    continue
                if msg.reply_serial == serial:
                    return msg
        finally:
            self.sock.settimeout(old_timeout)

    def hello(self) -> str:
        reply = self.call_sync(
            'org.freedesktop.DBus', '/org/freedesktop/DBus',
            'org.freedesktop.DBus', 'Hello',
        )
        if reply.msg_type != MSG_METHOD_RETURN or not reply.body:
            raise ConnectionError(f"D-Bus Hello failed: {reply}")
        return str(reply.body[0])

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Icon pixmap generation (fallback for trays that don't load IconThemePath SVG)
# ---------------------------------------------------------------------------

def render_card_icon_argb(size: int = 48) -> tuple[int, int, bytes]:
    """
    Render the Card Scanner icon (matching packaging/mtg-scanner.svg: purple rounded square
    with a white card outline and two lines) as big-endian ARGB32 bytes for SNI IconPixmap.
    Uses Pillow when installed (always true in the AppImage), else a tiny stdlib renderer.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return _render_card_icon_stdlib(size)

    # Render at 4x supersampling for crisp anti-aliased edges
    scale = 4
    s = size * scale
    img = Image.new('RGBA', (s, s), (0, 0, 0, 0))

    # Diagonal purple gradient (#7c6cff -> #b06cff) masked to a rounded square
    grad = Image.new('RGBA', (s, s))
    pixels = grad.load()
    denom = max(1, 2 * (s - 1))
    for y in range(s):
        for x in range(s):
            t = (x + y) / denom
            r = int(0x7c + (0xb0 - 0x7c) * t)
            pixels[x, y] = (r, 0x6c, 0xff, 255)

    mask = Image.new('L', (s, s), 0)
    mdraw = ImageDraw.Draw(mask)
    pad = round(16 * s / 256)
    radius = round(56 * s / 256)
    mdraw.rounded_rectangle([pad, pad, s - pad, s - pad], radius=radius, fill=255)
    img.paste(grad, (0, 0), mask)

    # White card outline and two inner horizontal bars (scaled from 256x256 viewBox)
    draw = ImageDraw.Draw(img)
    stroke = max(1, round(14 * s / 256))
    x0, y0 = round(80 * s / 256), round(52 * s / 256)
    x1, y1 = round(176 * s / 256), round(204 * s / 256)
    card_r = round(14 * s / 256)
    draw.rounded_rectangle([x0, y0, x1, y1], radius=card_r, outline=(255, 255, 255, 255), width=stroke)

    lx0, lx1 = round(108 * s / 256), round(148 * s / 256)
    for ly in (round(88 * s / 256), round(168 * s / 256)):
        draw.line([(lx0, ly), (lx1, ly)], fill=(255, 255, 255, 255), width=stroke)
        cap_r = stroke // 2
        draw.ellipse([lx0 - cap_r, ly - cap_r, lx0 + cap_r, ly + cap_r], fill=(255, 255, 255, 255))
        draw.ellipse([lx1 - cap_r, ly - cap_r, lx1 + cap_r, ly + cap_r], fill=(255, 255, 255, 255))

    img = img.resize((size, size), Image.Resampling.LANCZOS)
    rgba = img.tobytes()
    # Convert RGBA -> big-endian ARGB32 as required by org.kde.StatusNotifierItem.IconPixmap
    argb = bytearray(len(rgba))
    argb[0::4] = rgba[3::4]
    argb[1::4] = rgba[0::4]
    argb[2::4] = rgba[1::4]
    argb[3::4] = rgba[2::4]
    return (size, size, bytes(argb))


def _render_card_icon_stdlib(size: int) -> tuple[int, int, bytes]:
    """Minimal fallback icon without Pillow."""
    out = bytearray(size * size * 4)
    margin = max(1, size // 12)
    cx0, cx1 = size * 80 // 256, size * 176 // 256
    cy0, cy1 = size * 52 // 256, size * 204 // 256
    thick = max(1, size * 14 // 256)
    for y in range(size):
        for x in range(size):
            idx = (y * size + x) * 4
            if x < margin or x >= size - margin or y < margin or y >= size - margin:
                continue
            on_card = (
                (cx0 <= x <= cx1 and (cy0 <= y < cy0 + thick or cy1 - thick < y <= cy1))
                or (cy0 <= y <= cy1 and (cx0 <= x < cx0 + thick or cx1 - thick < x <= cx1))
            )
            if on_card:
                out[idx:idx + 4] = b'\xff\xff\xff\xff'
            else:
                out[idx:idx + 4] = bytes((255, 0x8e, 0x6c, 0xff))
    return (size, size, bytes(out))


# ---------------------------------------------------------------------------
# Introspection XML
# ---------------------------------------------------------------------------

_ROOT_INTROSPECT = """<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
"http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <node name="StatusNotifierItem"/>
  <node name="MenuBar"/>
</node>"""

_SNI_INTROSPECT = """<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
"http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect"><arg name="data" type="s" direction="out"/></method>
  </interface>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get">
      <arg name="interface" type="s" direction="in"/>
      <arg name="property" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="GetAll">
      <arg name="interface" type="s" direction="in"/>
      <arg name="properties" type="a{sv}" direction="out"/>
    </method>
  </interface>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="u" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
</node>"""

_MENU_INTROSPECT = """<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
"http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect"><arg name="data" type="s" direction="out"/></method>
  </interface>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get">
      <arg name="interface" type="s" direction="in"/>
      <arg name="property" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="GetAll">
      <arg name="interface" type="s" direction="in"/>
      <arg name="properties" type="a{sv}" direction="out"/>
    </method>
  </interface>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="LayoutUpdated">
      <arg name="revision" type="u"/>
      <arg name="parent" type="i"/>
    </signal>
  </interface>
</node>"""


# ---------------------------------------------------------------------------
# StatusNotifierItem + DBusMenu tray icon
# ---------------------------------------------------------------------------

@dataclass
class MenuItem:
    id: int
    label: str = ''
    separator: bool = False
    callback: Callable[[], None] | None = None
    enabled: bool = True

    def properties(self) -> dict[str, tuple[str, Any]]:
        if self.separator:
            return {'type': ('s', 'separator'), 'visible': ('b', True)}
        return {
            'label': ('s', self.label),
            'enabled': ('b', self.enabled),
            'visible': ('b', True),
        }


class TrayIcon:
    """
    Hosts an org.kde.StatusNotifierItem + com.canonical.dbusmenu service on the user's session
    D-Bus in a background daemon thread. Re-registers automatically if the panel/watcher restarts.
    """

    SNI_IFACE = 'org.kde.StatusNotifierItem'
    MENU_IFACE = 'com.canonical.dbusmenu'
    WATCHER_NAME = 'org.kde.StatusNotifierWatcher'
    WATCHER_PATH = '/StatusNotifierWatcher'

    def __init__(
        self,
        *,
        app_id: str = 'mtg-scanner',
        title: str = 'Card Scanner',
        tooltip: str = 'Running — click to open',
        icon_name: str = 'mtg-scanner',
        icon_theme_path: str | Path = '',
        on_activate: Callable[[], None] | None = None,
        menu_items: list[MenuItem] | None = None,
    ) -> None:
        self.app_id = app_id
        self.title = title
        self.tooltip = tooltip
        self.icon_name = icon_name
        self.icon_theme_path = str(icon_theme_path) if icon_theme_path else ''
        self.on_activate = on_activate
        self.menu_items = list(menu_items or [])
        self._menu_by_id = {item.id: item for item in self.menu_items}
        self._revision = 1
        self._pixmaps = [render_card_icon_argb(32), render_card_icon_argb(64)]

        self.bus_name = f'org.kde.StatusNotifierItem-{os.getpid()}-1'
        self._conn: DBusConnection | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake_r: int | None = None
        self._wake_w: int | None = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        """Connect to session D-Bus, register the tray icon, and start serving in a background thread."""
        try:
            conn = DBusConnection.connect_session()
        except Exception as e:
            logger.debug(f"System tray unavailable (no session D-Bus): {e}")
            return False

        self._conn = conn
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)

        # Acquire a well-known name and subscribe to watcher restarts
        try:
            conn.call_sync(
                'org.freedesktop.DBus', '/org/freedesktop/DBus',
                'org.freedesktop.DBus', 'RequestName',
                'su', (self.bus_name, 0),
            )
            match_rule = (
                "type='signal',sender='org.freedesktop.DBus',"
                "interface='org.freedesktop.DBus',member='NameOwnerChanged',"
                f"arg0='{self.WATCHER_NAME}'"
            )
            conn.call_sync(
                'org.freedesktop.DBus', '/org/freedesktop/DBus',
                'org.freedesktop.DBus', 'AddMatch',
                's', (match_rule,),
            )
        except Exception as e:
            logger.debug(f"Could not configure D-Bus tray name/match: {e}")
            self.stop()
            return False

        self._register_with_watcher()

        self._thread = threading.Thread(target=self._serve_loop, name='tray-dbus', daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """Disconnect cleanly from D-Bus (removes the tray icon immediately) and stop the thread."""
        self._stop_event.set()
        if self._wake_w is not None:
            try:
                os.write(self._wake_w, b'q')
            except OSError:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        for fd in (self._wake_r, self._wake_w):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._wake_r = self._wake_w = None

    def set_tooltip(self, tooltip: str) -> None:
        """Update the tray tooltip and notify the desktop panel."""
        with self._lock:
            self.tooltip = tooltip
            conn = self._conn
        if conn is not None:
            try:
                conn.emit_signal('/StatusNotifierItem', self.SNI_IFACE, 'NewToolTip')
            except OSError:
                pass

    def _register_with_watcher(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.call_async(
                self.WATCHER_NAME, self.WATCHER_PATH, self.WATCHER_NAME,
                'RegisterStatusNotifierItem', 's', (self.bus_name,),
            )
        except OSError as e:
            logger.debug(f"RegisterStatusNotifierItem failed: {e}")

    def _serve_loop(self) -> None:
        assert self._conn is not None and self._wake_r is not None
        sock = self._conn.sock
        while not self._stop_event.is_set():
            try:
                readable, _, _ = select.select([sock, self._wake_r], [], [], 1.0)
            except (OSError, ValueError):
                break
            if self._wake_r in readable:
                try:
                    os.read(self._wake_r, 64)
                except OSError:
                    pass
                if self._stop_event.is_set():
                    break
            if sock in readable:
                try:
                    messages = self._conn.read_messages()
                except Exception as e:
                    logger.debug(f"Tray D-Bus loop ended: {e}")
                    break
                for msg in messages:
                    self._handle_message(msg)

    def _handle_message(self, msg: DBusMessage) -> None:
        if msg.msg_type == MSG_SIGNAL:
            if (
                msg.interface == 'org.freedesktop.DBus'
                and msg.member == 'NameOwnerChanged'
                and len(msg.body) >= 3
                and msg.body[0] == self.WATCHER_NAME
                and msg.body[2]
            ):
                # Panel / StatusNotifierWatcher started or restarted — re-register
                self._register_with_watcher()
            return

        if msg.msg_type != MSG_METHOD_CALL or self._conn is None:
            return

        try:
            self._dispatch_call(msg)
        except Exception as e:
            logger.debug(f"Error handling D-Bus call {msg.interface}.{msg.member}: {e}")
            try:
                self._conn.send_error(msg, 'org.freedesktop.DBus.Error.Failed', str(e))
            except OSError:
                pass

    def _dispatch_call(self, msg: DBusMessage) -> None:
        assert self._conn is not None
        path = msg.path or ''
        iface = msg.interface or ''
        member = msg.member or ''

        # Introspectable
        if member == 'Introspect' and iface in ('org.freedesktop.DBus.Introspectable', ''):
            xml = _SNI_INTROSPECT if path == '/StatusNotifierItem' else (
                _MENU_INTROSPECT if path == '/MenuBar' else _ROOT_INTROSPECT
            )
            self._conn.send_reply(msg, 's', (xml,))
            return

        # Properties
        if iface == 'org.freedesktop.DBus.Properties':
            if member == 'GetAll':
                target_iface = msg.body[0] if msg.body else ''
                self._conn.send_reply(msg, 'a{sv}', (self._properties_for(path, target_iface),))
                return
            if member == 'Get' and len(msg.body) >= 2:
                props = self._properties_for(path, msg.body[0])
                name = msg.body[1]
                if name in props:
                    self._conn.send_reply(msg, 'v', (props[name],))
                else:
                    self._conn.send_error(
                        msg, 'org.freedesktop.DBus.Error.InvalidArgs', f"Unknown property: {name}"
                    )
                return

        # StatusNotifierItem methods
        if path == '/StatusNotifierItem' and iface in (self.SNI_IFACE, ''):
            if member == 'Activate':
                self._conn.send_reply(msg)
                self._run_callback(self.on_activate)
                return
            if member in ('SecondaryActivate', 'ContextMenu', 'Scroll'):
                self._conn.send_reply(msg)
                return

        # DBusMenu methods
        if path == '/MenuBar' and iface in (self.MENU_IFACE, ''):
            if member == 'GetLayout' and len(msg.body) >= 3:
                parent_id, depth, prop_names = int(msg.body[0]), int(msg.body[1]), list(msg.body[2])
                layout = self.build_menu_layout(parent_id, depth, prop_names)
                self._conn.send_reply(msg, 'u(ia{sv}av)', (self._revision, layout))
                return
            if member == 'GetGroupProperties' and len(msg.body) >= 2:
                ids, prop_names = list(msg.body[0]), list(msg.body[1])
                self._conn.send_reply(msg, 'a(ia{sv})', (self.get_group_properties(ids, prop_names),))
                return
            if member == 'GetProperty' and len(msg.body) >= 2:
                item_id, name = int(msg.body[0]), str(msg.body[1])
                props = self._item_properties(item_id)
                if name in props:
                    self._conn.send_reply(msg, 'v', (props[name],))
                else:
                    self._conn.send_error(
                        msg, 'org.freedesktop.DBus.Error.InvalidArgs', f"Unknown menu property: {name}"
                    )
                return
            if member == 'Event' and len(msg.body) >= 2:
                item_id, event_id = int(msg.body[0]), str(msg.body[1])
                self._conn.send_reply(msg)
                if event_id == 'clicked':
                    item = self._menu_by_id.get(item_id)
                    if item and item.enabled:
                        self._run_callback(item.callback)
                return
            if member == 'EventGroup' and msg.body:
                self._conn.send_reply(msg, 'ai', ([],))
                for ev in msg.body[0]:
                    item_id, event_id = int(ev[0]), str(ev[1])
                    if event_id == 'clicked':
                        item = self._menu_by_id.get(item_id)
                        if item and item.enabled:
                            self._run_callback(item.callback)
                return
            if member == 'AboutToShow':
                self._conn.send_reply(msg, 'b', (False,))
                return
            if member == 'AboutToShowGroup':
                self._conn.send_reply(msg, 'aiai', ([], []))
                return

        self._conn.send_error(
            msg, 'org.freedesktop.DBus.Error.UnknownMethod',
            f"No such method {iface}.{member} on {path}",
        )

    def _properties_for(self, path: str, iface: str) -> dict[str, tuple[str, Any]]:
        if path == '/StatusNotifierItem' and iface in (self.SNI_IFACE, ''):
            with self._lock:
                tooltip = self.tooltip
            return {
                'Category': ('s', 'ApplicationStatus'),
                'Id': ('s', self.app_id),
                'Title': ('s', self.title),
                'Status': ('s', 'Active'),
                'WindowId': ('u', 0),
                'IconName': ('s', self.icon_name),
                'IconThemePath': ('s', self.icon_theme_path),
                'IconPixmap': ('a(iiay)', self._pixmaps),
                'OverlayIconName': ('s', ''),
                'OverlayIconPixmap': ('a(iiay)', []),
                'AttentionIconName': ('s', ''),
                'AttentionIconPixmap': ('a(iiay)', []),
                'AttentionMovieName': ('s', ''),
                'ToolTip': ('(sa(iiay)ss)', ('', [], self.title, tooltip)),
                'ItemIsMenu': ('b', False),
                'Menu': ('o', '/MenuBar'),
            }
        if path == '/MenuBar' and iface in (self.MENU_IFACE, ''):
            paths = [self.icon_theme_path] if self.icon_theme_path else []
            return {
                'Version': ('u', 3),
                'TextDirection': ('s', 'ltr'),
                'Status': ('s', 'normal'),
                'IconThemePath': ('as', paths),
            }
        return {}

    def _item_properties(self, item_id: int) -> dict[str, tuple[str, Any]]:
        if item_id == 0:
            return {'children-display': ('s', 'submenu')}
        item = self._menu_by_id.get(item_id)
        return item.properties() if item else {}

    @staticmethod
    def _filter_props(props: dict[str, tuple[str, Any]], names: list[str]) -> dict[str, tuple[str, Any]]:
        if not names:
            return props
        wanted = set(names)
        return {k: v for k, v in props.items() if k in wanted}

    def build_menu_layout(
        self, parent_id: int, recursion_depth: int, property_names: list[str]
    ) -> tuple[int, dict[str, tuple[str, Any]], list[tuple[str, Any]]]:
        props = self._filter_props(self._item_properties(parent_id), property_names)
        children: list[tuple[str, Any]] = []
        if parent_id == 0 and recursion_depth != 0:
            for item in self.menu_items:
                item_props = self._filter_props(item.properties(), property_names)
                children.append(('(ia{sv}av)', (item.id, item_props, [])))
        return (parent_id, props, children)

    def get_group_properties(
        self, ids: list[int], property_names: list[str]
    ) -> list[tuple[int, dict[str, tuple[str, Any]]]]:
        target_ids = ids if ids else [0] + [item.id for item in self.menu_items]
        out: list[tuple[int, dict[str, tuple[str, Any]]]] = []
        for item_id in target_ids:
            props = self._filter_props(self._item_properties(item_id), property_names)
            if props:
                out.append((item_id, props))
        return out

    @staticmethod
    def _run_callback(callback: Callable[[], None] | None) -> None:
        if callback is None:
            return
        threading.Thread(target=callback, name='tray-action', daemon=True).start()
