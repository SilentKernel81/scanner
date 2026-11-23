# ============================================================================
# FILE: core/support.py
# The support file: one zip of plain text a person can read and then send to whoever helps them.
#
# Three rules decide everything below.
#   1. It must work when the program does not. So only the standard library, paths.py and diagnostics.py: no Config (a broken config.yaml is a reason to need
#      this), no server module, no database manager (they migrate when created; here a database is only ever opened read-only). `app.py --support-bundle`
#      runs this before the server is even imported, and takes no lock, so it also works beside a running server.
#   2. What goes in is ALLOWED by name, never "everything except". A new log file, setting or table is not in the bundle until it is named here.
#   3. Secrets are removed three ways: their exact values, read from where the program keeps them (RedactionMap); the shapes of keys and credentials; and the
#      finished zip is searched again for every exact value - if one survived, there is no file (SupportError).
#
# What it cannot hide, and the page says so: card names, station names and what was read from the cards remain, so a support file shows part of a collection.
# No generic "Bearer <word>" rule either: "Standard Bearer" is a card.
# Nothing here sends anything anywhere: the person downloads the file and decides.
# ============================================================================
import getpass
import importlib.metadata
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from card_scanner import diagnostics
from card_scanner.paths import TOP, USER_DIR, tool

DATA_DIR = USER_DIR / 'data'
LOG_DIR = DATA_DIR / 'logs'
BUNDLE_DIR = DATA_DIR / 'support'       # where the command line leaves its file
KEEP_BUNDLES = 3                        # ... and how many it keeps
LOG_TAIL_BYTES = 2 * 1024 * 1024        # of each log, the newest part (from its rotated copy too when the file itself is short)
MIN_SECRET_LENGTH = 8                   # a shorter "secret" would be replaced everywhere it happens to occur

# Rule 2: what may go in
LOG_FILES = ('errors.log', 'crash-server.log', 'crash-station.log', 'app.log', 'station.log', 'ai.log', 'ocr.log', 'database.log', 'requests.log',
             'scanned_cards.log', 'reading.jsonl', 'printing_check.jsonl')
SETTINGS_SHOWN = ('ai_provider', 'ai_model', 'ai_enabled', 'ocr_first', 'reading_mode', 'reading_accept_image_alone', 'reading_extend_index', 'debug_mode',
                  'network_access', 'price_source', 'backup_every_hours', 'backup_keep', 'cleanup_days', 'capture_copy_size', 'keep_frames',
                  'card_data_every_hours', 'image_index_every_hours', 'prices_every_hours')
STATION_FIELDS = ('name', 'camera', 'created', 'last_seen', 'captures')
STATION_SETTINGS = ('focus_value', 'camera_rotation', 'refocus_every', 'refocus_on_drop', 'fixed_area_enabled', 'debug_trace')
DROPPER_FIELDS = ('enabled', 'transport')
LIBRARIES = ('flask', 'flask-socketio', 'python-socketio', 'python-engineio', 'werkzeug', 'numpy', 'opencv-python', 'opencv-python-headless', 'onnxruntime',
             'requests', 'pyyaml', 'pillow')
DATABASES = ('cards_database.db', 'scan_inventory.db', 'arena_inventory.db', 'pending_captures.db', 'others.db', 'decks.db', 'web_cache.db')

SECRET_NAME = re.compile(r'(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD)$', re.I)     # API_KEY, STATION_TOKEN - not KEYBOARD
SECRET_FIELD = re.compile(r'key|token|secret|password|hash|salt', re.I)      # in a JSON file of the program's

README = """Card Scanner support file

Everything in this zip is plain text: open any file to see what you would be sending.

manifest.json   which build this is, the operating system, libraries, GPU
settings.json   the program's choices (no keys: only whether each one is set)
stations.json   the cameras and their focus settings
databases.json  sizes and row counts of the databases, not their contents
logs/           the newest part of each log

Removed: API keys, passwords, tokens, your home folder, network addresses and this
computer's name (shown as aliases such as private-ip-1 and host-1).
Still there: card names, camera names, and what was read from your cards, so this
file shows part of your collection.

Nothing was sent anywhere. You decide whether and to whom to send this file.
"""


class SupportError(Exception):
    """The support file could not be made safely. Its message is shown to the person."""


# ---------------------------------------------------------------------------------------------
# What must not leave
# ---------------------------------------------------------------------------------------------

def _read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _env_values(path):
    """The values in a KEY=value file whose name says they are secret"""
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return
    for line in lines:
        name, separator, value = line.strip().removeprefix('export ').partition('=')
        if separator and not name.startswith('#') and SECRET_NAME.search(name.strip()):
            yield value.strip().strip('\'"')


def _json_secrets(value, field='', everything=False):
    """String values of a JSON document under a field that names a secret (everything: all of them - a login file holds nothing else worth showing)"""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield from _json_secrets(inner, str(key), everything)
    elif isinstance(value, list):
        for inner in value:
            yield from _json_secrets(inner, field, everything)
    elif isinstance(value, str) and (everything or SECRET_FIELD.search(field)):
        yield value


def secret_values():
    """Every secret this installation holds, by value, longest first (so that one containing another is replaced whole). Files that are not there are skipped"""
    found = set()
    for path in (USER_DIR / '.env', TOP / '.env', DATA_DIR / 'api_keys.env'):
        found.update(_env_values(path))
    found.update(value for name, value in os.environ.items() if SECRET_NAME.search(name))
    for name in ('settings_login.json', 'station_login.json'):
        found.update(_json_secrets(_read_json(DATA_DIR / name), everything=True))
    for name in ('settings.json', 'stations.json', 'station.json'):
        found.update(_json_secrets(_read_json(DATA_DIR / name)))
    try:
        found.add((DATA_DIR / 'secret_key').read_text(errors='replace').strip())
    except OSError:
        pass
    for path in (USER_DIR / 'config.yaml', TOP / 'config.yaml'):
        try:
            found.update(re.findall(r'(?im)^\s*(?:\w+_)?(?:token|key|secret|password)\s*:\s*["\']?([^\s"\'#]+)', path.read_text(errors='replace')))
        except OSError:
            pass
    return sorted((value for value in found if value and len(value) >= MIN_SECRET_LENGTH and value.lower() not in ('none', 'null')), key=len, reverse=True)


_IPV4 = re.compile(r'(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])')
_MAC = re.compile(r'(?<![0-9A-Fa-f:])[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}(?![0-9A-Fa-f:])')
_EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
_URL_LOGIN = re.compile(r'(\b[a-z][a-z0-9+.-]*://)[^/\s\'"@]+@', re.I)
_URL_QUERY = re.compile(r'(\bhttps?://[^\s\'"?<>]+)\?[^\s\'"<>]*', re.I)
_AUTH_HEADER = re.compile(r'(?i)\b(authorization|x-api-key|x-goog-api-key|x-station-token)(["\']?\s*[:=]\s*["\']?)(?:(?:bearer|basic)\s+)?[^\s"\',}]+')
_KEY_SHAPES = re.compile(r'\bsk-[A-Za-z0-9_-]{16,}|\bAIza[0-9A-Za-z_-]{20,}')
_HOME_UNIX = re.compile(r'/(?:home|Users)/[^/\s\'":]+')
_HOME_WINDOWS = re.compile(r'[A-Za-z]:\\Users\\[^\\\s\'"]+')
_URL_HOST = re.compile(r'(\bhttps?://(?:<credentials>@)?)([^/:\s\'"<>?]+)', re.I)
_POOL_HOST = re.compile(r'(\bhost=[\'"])([^\'"]+)')       # how requests names a machine in a connection error
LOCAL_DOMAINS = ('.local', '.lan', '.home', '.internal', '.localdomain', '.home.arpa')
_CAPTURE_KEY = re.compile(r'"capture_key":\s*"[^"]*"')       # what a station removes a copy by: of no use to read, and not to be handed around


class RedactionMap:
    """
    Cleans text. The same address, device or computer gets the same alias in every file of one bundle (private-ip-1, mac-1, host-1), so a problem between two
    machines can still be followed; the aliases mean nothing outside that bundle.
    """

    def __init__(self):
        self.secrets = secret_values()
        self.aliases = {}
        self.counts = {}
        try:
            home = str(Path.home())
        except (RuntimeError, OSError):      # a container's user may have no home at all
            home = ''
        # Whole paths only: in Docker the data folder is /app, which must not change /application or /usr/app. Longest first
        folders = sorted({(str(USER_DIR), '<data folder>'), (home, '~')}, key=lambda pair: len(pair[0]), reverse=True)
        self.folders = [(re.compile(r'(?<![\w/.-])' + re.escape(folder) + r'(?![\w.-])'), shown) for folder, shown in folders if len(folder) > 1]
        names = {socket.gethostname(), socket.gethostname().split('.')[0]}
        try:
            names.add(Path('/etc/host_hostname').read_text().strip())       # the machine's own name, seen from inside a container
        except OSError:
            pass
        names = {name for name in names if len(name) >= 3 and name.lower() != 'localhost'}
        self.hosts = re.compile(r'(?<![\w-])(?:' + '|'.join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r')(?![\w-])') if names else None
        try:
            user = getpass.getuser()
        except (OSError, KeyError):
            user = ''
        self.user = re.compile(r'(?<![\w-])' + re.escape(user) + r'(?![\w-])') if len(user) >= 3 and user not in ('root', 'user', 'app') else None

    def _alias(self, kind, value):
        key = (kind, value)
        if key not in self.aliases:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            self.aliases[key] = f'{kind}-{self.counts[kind]}'
        return self.aliases[key]

    def _ip(self, match):
        try:
            address = ipaddress.ip_address(match.group())
        except ValueError:
            return match.group()                       # 999.1.2.3: not an address
        if address.is_loopback or address.is_unspecified:
            return match.group()                       # says something, reveals nothing
        return self._alias('private-ip' if address.is_private else 'ip', str(address))

    def _named_host(self, match):
        """A machine named in a URL or a connection error: one of the person's own (no dot, .local, .lan, ...) gets an alias; a public service's name is useful and stays"""
        host = match.group(2)
        own = '.' not in host or host.lower().endswith(LOCAL_DOMAINS)
        if not own or host.lower() == 'localhost' or _IPV4.fullmatch(host):
            return match.group()
        return match.group(1) + self._alias('host', host)

    def clean(self, text, addresses=True):
        """The text with what must not leave replaced. addresses=False: for what holds no address but four-part version numbers, which look like one"""
        for secret in self.secrets:
            if secret in text:
                text = text.replace(secret, '<secret>')
        text = _URL_QUERY.sub(r'\1?<removed>', text)            # first: a URL's end is found by '<', which the next rule writes into it
        text = _URL_LOGIN.sub(r'\1<credentials>@', text)        # before the e-mail rule: user:pass@host looks like one
        text = _AUTH_HEADER.sub(r'\1\2<secret>', text)
        text = _KEY_SHAPES.sub('<secret>', text)
        text = _CAPTURE_KEY.sub('"capture_key": "<removed>"', text)
        text = _EMAIL.sub('<email>', text)
        for folder, shown in self.folders:
            text = folder.sub(lambda match: shown, text)
        text = _HOME_UNIX.sub('/home/<user>', text)             # another machine's home, in a line a station sent
        text = _HOME_WINDOWS.sub(r'C:\\Users\\<user>', text)
        text = _URL_HOST.sub(self._named_host, text)
        text = _POOL_HOST.sub(self._named_host, text)
        if addresses:
            text = _IPV4.sub(self._ip, text)
        text = _MAC.sub(lambda match: self._alias('mac', match.group().lower()), text)
        if self.hosts is not None:
            text = self.hosts.sub(lambda match: self._alias('host', match.group()), text)
        if self.user is not None:
            text = self.user.sub('<user>', text)
        return text


# ---------------------------------------------------------------------------------------------
# What goes in
# ---------------------------------------------------------------------------------------------

def _tail(path, limit=LOG_TAIL_BYTES):
    """The newest `limit` bytes of a log as text, whole lines only: from the file, then from its rotated copy (<name>.1) if the file is shorter than that"""
    parts, left = [], limit
    for candidate in (path, path.with_name(path.name + '.1')):
        if left <= 0:
            break
        try:
            with open(candidate, 'rb') as handle:
                size = handle.seek(0, os.SEEK_END)
                start = max(0, size - left)
                handle.seek(start)
                data = handle.read(left)
        except OSError:
            continue
        if start:                                       # began inside a line
            data = data[data.find(b'\n') + 1:]
        parts.insert(0, data)
        left -= len(data)
    return b''.join(parts).decode('utf-8', errors='replace')


def _tail_size(path):
    """How many bytes of this log _tail would take, without reading it"""
    total = 0
    for candidate in (path, path.with_name(path.name + '.1')):
        try:
            total += candidate.stat().st_size
        except OSError:
            pass
    return min(total, LOG_TAIL_BYTES)


def _command_output(command, timeout=3):
    """First output of a small tool, or None when it is not there or fails"""
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return done.stdout.strip() if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, TypeError):
        return None


def _run_mode():
    if os.environ.get('APPIMAGE'):
        return 'desktop program (AppImage)'
    if Path('/.dockerenv').exists():
        return 'Docker'
    return 'checkout'


def _operating_system():
    try:
        return platform.freedesktop_os_release().get('PRETTY_NAME')
    except OSError:
        return None


def _memory_gb():
    try:
        kilobytes = int(re.search(r'MemTotal:\s+(\d+)', Path('/proc/meminfo').read_text()).group(1))
        return round(kilobytes / 1024 / 1024, 1)
    except (OSError, AttributeError, ValueError):
        return None


def _libraries():
    found = {}
    for name in LIBRARIES:
        try:
            found[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    return found


def _ocr_reader():
    """How the OCR reader last said it runs ('webgpu', 'cpu'), from its own log line"""
    lines = re.findall(r'light-ocr ready \(([^)]*)\)', _tail(LOG_DIR / 'ai.log', 512 * 1024))
    return lines[-1] if lines else None


def manifest():
    """Which build, on what. Nothing here names the machine: platform.uname() and sys.executable would"""
    try:
        usage = shutil.disk_usage(USER_DIR)
        disk = {'free_gb': round(usage.free / 1024 ** 3, 1), 'total_gb': round(usage.total / 1024 ** 3, 1)}
    except OSError:
        disk = None
    gpu = _command_output(['nvidia-smi', '--query-gpu=name,driver_version', '--format=csv,noheader'])
    return {'created': time.strftime('%Y-%m-%d %H:%M:%S %z'), 'build': diagnostics.build_info(), 'run_mode': _run_mode(),
            'python': platform.python_version(), 'os': {'system': platform.system(), 'release': platform.release(), 'name': _operating_system(),
                                                        'machine': platform.machine()},
            'cpus': os.cpu_count(), 'memory_gb': _memory_gb(), 'disk': disk, 'gpu': gpu, 'ocr_reader': _ocr_reader(),
            'node': _command_output([tool('node'), '--version']) if tool('node') else None, 'libraries': _libraries()}


def settings_summary():
    """The choices by name (rule 2), and for each key only whether it is there"""
    saved = _read_json(DATA_DIR / 'settings.json')
    saved = saved if isinstance(saved, dict) else {}
    keys = {}
    for path in (USER_DIR / '.env', DATA_DIR / 'api_keys.env'):
        try:
            lines = path.read_text(errors='replace').splitlines()
        except OSError:
            continue
        for line in lines:
            name, separator, value = line.strip().removeprefix('export ').partition('=')
            if separator and not name.startswith('#') and SECRET_NAME.search(name.strip()):
                keys[name.strip()] = bool(value.strip().strip('\'"')) or keys.get(name.strip(), False)
    login = (DATA_DIR / 'settings_login.json').exists()
    return {'settings': {name: saved[name] for name in SETTINGS_SHOWN if name in saved}, 'keys_present': keys, 'settings_password_set': login}


def stations_summary():
    """The cameras: fields by name (rule 2). No pairing key, no feeder address"""
    saved = _read_json(DATA_DIR / 'stations.json')
    if not isinstance(saved, dict):
        return {}
    shown = {}
    for station_id, station in saved.items():
        if not isinstance(station, dict):
            continue
        values = station.get('settings') if isinstance(station.get('settings'), dict) else {}
        entry = {name: station[name] for name in STATION_FIELDS if name in station}
        entry['settings'] = {name: values[name] for name in STATION_SETTINGS if name in values}
        if isinstance(values.get('dropper'), dict):
            entry['settings']['dropper'] = {name: values['dropper'][name] for name in DROPPER_FIELDS if name in values['dropper']}
        shown[station_id] = entry
    return shown


def databases_summary():
    """Size, tables with their row counts, migrations done. Opened read-only and never waited for: a database in use says so instead of holding this up"""
    summary = {}
    for name in DATABASES:
        path = DATA_DIR / name
        if not path.is_file():
            continue
        entry = {'bytes': path.stat().st_size}
        connection = None
        try:
            connection = sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True, timeout=1)
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
            # (names come from the file itself: quoted as identifiers)
            entry['rows'] = {table: connection.execute('SELECT COUNT(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0] for table in tables}
            if 'schema_migrations' in tables:
                entry['migrations'] = [row[0] for row in connection.execute('SELECT name FROM schema_migrations ORDER BY name')]
        except sqlite3.Error as e:
            entry['error'] = str(e)
        finally:
            if connection is not None:
                connection.close()
        summary[name] = entry
    return summary


def _section(function):
    """One part of the file as JSON text. A part that fails says why in its place: the rest is what someone needs most then"""
    try:
        return json.dumps(function(), indent=1, ensure_ascii=False, default=str) + '\n'
    except Exception as e:
        return json.dumps({'could_not_be_read': f'{type(e).__name__}: {e}'}) + '\n'


def logs_present():
    return [name for name in LOG_FILES if _tail_size(LOG_DIR / name)]


def preview():
    """What the file would hold, for the page to show before it is made: [{'name', 'bytes'}], nothing read"""
    fixed = [{'name': name, 'bytes': None} for name in ('README.txt', 'manifest.json', 'settings.json', 'stations.json', 'databases.json')]
    return fixed + [{'name': f'logs/{name}', 'bytes': _tail_size(LOG_DIR / name)} for name in logs_present()]


def collect():
    """Every file of the bundle as (name in the zip, cleaned text). Only the README is not cleaned: it is this file's own text"""
    redaction = RedactionMap()
    entries = [('README.txt', README)]
    for name, function in (('manifest.json', manifest), ('settings.json', settings_summary), ('stations.json', stations_summary),
                           ('databases.json', databases_summary)):
        # (the manifest is built from named fields and holds no address, but OpenCV's version has four parts)
        entries.append((name, redaction.clean(_section(function), addresses=name != 'manifest.json')))
    for name in logs_present():
        entries.append((f'logs/{name}', redaction.clean(_tail(LOG_DIR / name))))
    return entries, redaction.secrets


def write_bundle(out):
    """
    Writes the zip to `out` (a binary file, seekable) and returns the names in it. Afterwards the finished file is read back and searched for every secret: if one
    is found, SupportError - the caller must then throw the file away.
    """
    entries, secrets = collect()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, text in entries:
            archive.writestr(name, text)
    out.seek(0)
    with zipfile.ZipFile(out) as archive:
        for name in archive.namelist():
            text = archive.read(name).decode('utf-8', errors='replace')
            if any(secret in text for secret in secrets):
                raise SupportError('The support file was not made: a secret could not be removed from it')
    out.seek(0)
    return [name for name, _ in entries]


def bundle_name():
    return f"card-scanner-support-{time.strftime('%Y-%m-%d_%H-%M-%S')}.zip"


def command_line():
    """`app.py --support-bundle`: makes the file in data/support/ and says where. Readable by this user only until they decide to send it. Returns the exit code"""
    try:
        BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=BUNDLE_DIR, suffix='.tmp')        # mode 600
    except OSError as e:
        print(f"The support file could not be made: {e}", file=sys.stderr)
        return 1
    try:
        with os.fdopen(descriptor, 'w+b') as out:
            write_bundle(out)
        target = BUNDLE_DIR / bundle_name()
        os.replace(temporary, target)
    except Exception as e:
        Path(temporary).unlink(missing_ok=True)
        print(f"The support file could not be made: {e}", file=sys.stderr)
        return 1
    for old in sorted(BUNDLE_DIR.glob('card-scanner-support-*.zip'), reverse=True)[KEEP_BUNDLES:]:
        old.unlink(missing_ok=True)
    print(f"Support file: {target}")
    print("Open it to see what it holds. Nothing was sent: you decide whether and to whom to send it.")
    return 0
