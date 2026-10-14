# ============================================================================
# FILE: server/access.py
# Only the program's own pages (and its stations) may use the server. There is no login, and
# a browser sends any web page's requests to a local or network address: without these
# checks a page open in another tab could change the collection, delete cards or restore a
# backup. Needs nothing from the rest of the server (state.py installs it).
# ============================================================================
import ipaddress
import os
import secrets
import socket
from urllib.parse import urlsplit

from flask import jsonify, request

from card_scanner.config import Config

# Domains of a home or office network, which nobody can register on the internet: the
# computer's name is trusted alone and under these, nothing else (scanner.attacker.example
# is the attacker's name to hand out)
LOCAL_DOMAINS = ('local', 'lan', 'home', 'home.arpa', 'internal', 'localdomain')


def secret_key():
    """Signs Flask's cookies: flask.secret_key if set, else a random one kept in data/secret_key"""
    if Config.SECRET_KEY:
        return Config.SECRET_KEY
    key_file = Config.DATA_DIR / 'secret_key'
    try:
        key = key_file.read_text().strip()
    except OSError:
        key = ''
    if not key:
        key = secrets.token_hex(32)
        Config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f:
            f.write(key + '\n')
    return key


def own_host_names():
    """The exact names this computer is reached under, besides localhost and IP addresses"""
    full = socket.gethostname().lower().rstrip('.')
    short = full.split('.')[0]
    names = {full, short} | {f'{short}.{domain}' for domain in LOCAL_DOMAINS}
    names |= {str(allowed).lower().rstrip('.') for allowed in Config.ALLOWED_HOSTS}
    return names - {''}


def host_allowed(host):
    """
    Whether `host` (a Host header) is a name the server is reached under: localhost, an IP
    address, one of this computer's own names (own_host_names) or of flask.allowed_hosts.
    Another name that resolves here is a web page reaching in through its own domain (DNS
    rebinding).
    """
    name = (urlsplit(f'//{host}').hostname or '').lower().rstrip('.')
    if name == 'localhost' or name in own_host_names():
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def same_origin(origin, host):
    """Whether a request's Origin header is the page it was sent to (`host`: its Host header)"""
    return urlsplit(origin).netloc.lower() == (host or '').lower()


def socket_origin_allowed(origin, environ=None):
    """Engine.IO asks when a connection names its origin (browsers always do; stations send none)"""
    host = (environ or {}).get('HTTP_HOST', '')
    if not host_allowed(host):
        return False
    return not origin or same_origin(origin, host)


def reject_other_sites():
    if not host_allowed(request.host):
        return jsonify({'success': False, 'error': f'{request.host} is not a name of this Card Scanner '
                                                   'server (flask.allowed_hosts in config.yaml)'}), 403
    origin = request.headers.get('Origin')
    changes = request.method not in ('GET', 'HEAD', 'OPTIONS')
    # Sec-Fetch-Site covers the requests browsers send without an Origin (a GET from another site)
    if (changes and origin and not same_origin(origin, request.host)) or (
            request.path.startswith('/api/') and request.headers.get('Sec-Fetch-Site') == 'cross-site'):
        return jsonify({'success': False, 'error': 'Requests from other web pages are not accepted'}), 403


def protect(app):
    """
    Call after SocketIO(app) is made: Flask-SocketIO puts its own middleware around
    app.wsgi_app and answers /socket.io/ before Flask's before_request hooks run, and
    Engine.IO checks the origin only when a request carries one. The Host check is
    therefore also wrapped around the whole WSGI application, outermost, so no request
    reaches the transport under a name that is not the server's.
    """
    app.before_request(reject_other_sites)
    inner = app.wsgi_app

    def guarded(environ, start_response):
        if not host_allowed(environ.get('HTTP_HOST', '')):
            body = b'{"success": false, "error": "This name is not a name of this Card Scanner server (flask.allowed_hosts in config.yaml)"}'
            start_response('403 Forbidden', [('Content-Type', 'application/json'),
                                             ('Content-Length', str(len(body)))])
            return [body]
        return inner(environ, start_response)

    app.wsgi_app = guarded
