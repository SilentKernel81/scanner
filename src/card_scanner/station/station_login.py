# ============================================================================
# FILE: station_login.py
# The password of a camera station's setup page (station_setup.py).
#
# Without it, anyone on the network could open the page and point the station
# at another server. With it, the page asks for a password first - one
# password for the page, no user names.
#
# How the password is kept: NOT the password itself, and not encrypted either,
# but hashed. The difference matters:
#   - encrypted text can be turned back into the password by whoever has the
#     key - and the key would have to lie on this same machine, next to it;
#   - a hash cannot be turned back. To check a password, the same hash is made
#     from what was typed and the two are compared.
# The hash is made with scrypt, which is slow and memory-hungry on purpose:
# someone who copies the file can try guesses only a few at a time. A random
# "salt" is mixed in, so the same password gives a different hash on every
# station and no ready-made table of hashes helps.
#
# How a browser stays logged in: after the right password it gets a cookie
# holding an expiry time and a signature of that time, made with a secret key
# that only this station has (HMAC). The station keeps no list of who is
# logged in - it checks the signature - so a login survives the station's
# restart after "Save and restart".
#
# Everything is in data/station_login.json (readable by this user only).
# Forgotten password: delete that file and restart the station; the page then
# asks for a new one.
#
# What this does NOT protect against: the page is plain HTTP, so the password
# crosses the network unencrypted when it is typed. It keeps out the curious
# on a home or shop network, not someone who can listen on that network.
# ============================================================================
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from card_scanner.config import Config

LOGIN_FILE = Config.DATA_DIR / 'station_login.json'
PASSWORD_VARIABLE = 'SCANNER_SETUP_PASSWORD'  # set it to fix the password from outside (Docker)
COOKIE = 'station_session'
SESSION_SECONDS = 30 * 24 * 3600  # a login lasts 30 days: the page is opened rarely
MINIMUM_LENGTH = 8

# scrypt's cost: N (work and memory, a power of two), r (block size), p (passes).
# 2**14, 8, 1 uses 16 MB and takes about 0.1 s on a Raspberry Pi 4 - nothing for one login,
# a lot for a million guesses. They are saved with the hash, so they can be raised later
# without making old files unreadable.
SCRYPT = {'n': 2 ** 14, 'r': 8, 'p': 1}


def hash_password(password, salt, cost=None):
    """The scrypt hash of a password with this salt, as hexadecimal text"""
    cost = cost or SCRYPT
    return hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt),
                          n=cost['n'], r=cost['r'], p=cost['p'], dklen=32).hex()


class Login:
    """
    The setup page's password and sessions. One instance, shared by all requests.

    The defaults are the station's setup page's. The server's settings password
    (server/lock.py) is the same mechanism with a file, an environment variable and a cookie of
    its own: a different `file`, `variable`, `cookie` and a shorter `session_seconds`.
    """

    def __init__(self, file=None, variable=PASSWORD_VARIABLE, cookie=COOKIE, session_seconds=SESSION_SECONDS):
        self.file = file or LOGIN_FILE
        self.variable = variable
        self.cookie = cookie
        self.session_seconds = session_seconds
        self.lock = threading.Lock()   # requests come in several threads
        self.failures = 0              # wrong passwords in a row
        self.locked_until = 0.0        # no attempt is accepted before this time
        self.fixed = bool(os.getenv(self.variable))
        self.record = self._load()
        if self.fixed:
            self._adopt_environment_password(os.getenv(self.variable))

    def _load(self):
        """The saved record, or None when no password has been chosen yet"""
        try:
            record = json.loads(self.file.read_text())
            if all(isinstance(record.get(name), str) for name in ('salt', 'hash', 'key')):
                return record
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def _adopt_environment_password(self, password):
        """
        The password comes from the environment (SCANNER_SETUP_PASSWORD). It is treated like
        one chosen on the page: a record with a random salt, its scrypt hash and a random
        cookie key is kept in the file - never the password itself.

        The cookie key must NOT be made from the password. An earlier version did that (one
        SHA-256 of it), which let anyone test a password guess without ever asking the
        station to log in: make the key from the guess, sign a cookie with it, see if the
        station accepts the cookie - millions of guesses, past scrypt and past the lock after
        wrong attempts. Two stations with the same password also accepted each other's
        cookies. A random key that only this station's file holds allows neither.

        At each start the saved hash says whether the environment's password is still the
        one the record was made for. If it is, the record - and with it every login - is
        kept; if it changed, a new record with a new key is made and everyone logs in again.
        """
        record = self.record
        if record is not None and hmac.compare_digest(hash_password(password, record['salt'], record.get('cost')), record['hash']):
            return
        self.record = self._new_record(password)
        try:
            self._write(self.record)
        except OSError:
            pass  # data/ cannot be written: the record lives until this program ends, then logins are asked again

    @staticmethod
    def _new_record(password):
        """A fresh record for a password: a new random salt and a new random cookie key"""
        salt = secrets.token_hex(16)  # 16 random bytes; secrets = random numbers fit for security
        return {'salt': salt, 'cost': dict(SCRYPT), 'hash': hash_password(password, salt), 'key': secrets.token_hex(32)}

    def _write(self, record):
        """Save a record: whole or not at all (as station_setup.save), readable by this user only"""
        self.file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.file.with_suffix('.json.tmp')
        # Created private from the start (not written first and restricted after)
        with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as file:
            json.dump(record, file, indent=2)
        os.replace(temporary, self.file)

    def has_password(self):
        return self.record is not None

    def set_password(self, password):
        """
        Choose or change the password. A new salt and a new cookie key are made each time, so
        every browser that was logged in has to log in again with the new password.
        """
        if self.fixed:
            raise ValueError(f"The password is set by {self.variable} in the environment - change it there")
        password = str(password or '')
        if len(password) < MINIMUM_LENGTH:
            raise ValueError(f"The password needs at least {MINIMUM_LENGTH} characters")
        record = self._new_record(password)
        self._write(record)
        with self.lock:
            self.record, self.failures, self.locked_until = record, 0, 0.0

    def remove_password(self):
        """No password any more (the server's settings lock): the file goes, every login with it"""
        if self.fixed:
            raise ValueError(f"The password is set by {self.variable} in the environment - remove it there")
        try:
            self.file.unlink()
        except FileNotFoundError:
            pass
        with self.lock:
            self.record, self.failures, self.locked_until = None, 0, 0.0

    def check(self, password):
        """
        True for the right password. Raises ValueError with a sentence for the page while
        attempts are locked: after each wrong password the next attempt has to wait - 1 s,
        then 2, 4 ... up to 30 s - which makes guessing through the page hopeless. (It also
        means a stranger typing wrong passwords can make the owner wait up to 30 s.)
        """
        with self.lock:
            record = self.record
            wait = self.locked_until - time.time()
            if wait > 0:
                raise ValueError(f"Too many wrong passwords - wait {int(wait) + 1} s")
            if record is None:
                return False
            typed = hash_password(str(password or ''), record['salt'], record.get('cost'))
            # compare_digest takes the same time wherever the first difference is, so the
            # time an answer takes says nothing about how close a guess was
            right = hmac.compare_digest(typed, record['hash'])
            if right:
                self.failures = 0
            else:
                self.failures += 1
                self.locked_until = time.time() + min(30, 2 ** (self.failures - 1))
            return right

    # ------------------------------------------------------------------------
    # Sessions: "<expiry time>.<signature of it>" in a cookie
    # ------------------------------------------------------------------------

    def _signature(self, expiry):
        return hmac.new(bytes.fromhex(self.record['key']), str(expiry).encode(), hashlib.sha256).hexdigest()

    def new_session(self):
        """The cookie value for a browser that just logged in"""
        expiry = int(time.time()) + self.session_seconds
        return f"{expiry}.{self._signature(expiry)}"

    def valid_session(self, value):
        """True if a cookie value was made by new_session() with the current key and has not expired"""
        if self.record is None or not value or '.' not in value:
            return False
        expiry, signature = value.split('.', 1)
        if not expiry.isdigit() or int(expiry) < time.time():
            return False
        return hmac.compare_digest(signature, self._signature(int(expiry)))

    def cookie_header(self, value):
        """
        The Set-Cookie header for a session value ('' logs the browser out).
          HttpOnly         the page's script cannot read it (nothing to steal for a script)
          SameSite=Strict  it is only sent on requests made from this page itself, so another
                           site open in the same browser cannot act on the station with it
        """
        age = self.session_seconds if value else 0
        return f"{self.cookie}={value}; Path=/; Max-Age={age}; HttpOnly; SameSite=Strict"


def session_from(cookie_header, cookie=COOKIE):
    """The session value in a request's Cookie header ("a=1; station_session=...; b=2"), or ''"""
    for part in (cookie_header or '').split(';'):
        name, _, value = part.strip().partition('=')
        if name == cookie:
            return value
    return ''
