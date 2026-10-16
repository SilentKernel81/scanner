# ============================================================================
# FILE: server/lock.py
# The settings password: an optional lock on the few actions that can hurt
# beyond one card - the AI keys and endpoint, restarting or stopping the server,
# who may reach it, debug mode, and the backups (restore, upload, download, delete).
#
# The pages have no login by design (the cameras, the phone and the other
# computers just work), so anyone who can reach the server could do these.
# With a settings password set, they ask for it first; scanning, the
# collection and the decks stay open. (Outside code review, 2026-10-16.)
#
#   - No password set: nothing is locked (what the server always did).
#   - The password is chosen on the Settings page (Server), or fixed from outside with
#     SCANNER_SETTINGS_PASSWORD (Docker). Stored only as an scrypt hash in
#     data/settings_login.json - the same mechanism as a camera station's setup page
#     (station/station_login.py), with its own file, cookie and a 12-hour session.
#   - Unlocking sets a cookie (HttpOnly, SameSite=Strict). A guarded route answers 401
#     {"locked": true}; common.js asks for the password and repeats the request.
#     A guarded Socket.IO event answers an `error` event with locked: true; the connection
#     was made before the unlock, so the page reconnects after it.
#   - Wrong passwords wait 1 s, 2 s, 4 s ... up to 30 s (Login.check).
#
# Not protected: the page is plain HTTP, so the password crosses the network unencrypted.
# It keeps out the curious and the mistaken on a home or shop network, not someone who can
# listen on it. Forgotten password: delete data/settings_login.json and restart.
# ============================================================================
import functools

from flask import jsonify, request
from flask_socketio import emit

from card_scanner.config import Config
from card_scanner.station.station_login import Login, session_from

from card_scanner.server.state import app
from card_scanner.server.logs import logger

COOKIE = 'settings_session'
SESSION_SECONDS = 12 * 3600
LOCKED_MESSAGE = 'This needs the settings password'

login = Login(file=Config.DATA_DIR / 'settings_login.json', variable='SCANNER_SETTINGS_PASSWORD',
              cookie=COOKIE, session_seconds=SESSION_SECONDS)


def unlocked():
    """Whether the request in hand may do a guarded action: no password is set, or its cookie is a valid session"""
    return not login.has_password() or login.valid_session(session_from(request.headers.get('Cookie'), COOKIE))


def guarded(function):
    """A route that needs the settings password (when one is set): 401 {"locked": true} without it"""
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        if not unlocked():
            return jsonify({'success': False, 'error': LOCKED_MESSAGE, 'locked': True}), 401
        return function(*args, **kwargs)
    return wrapper


def guarded_event(function):
    """The same for a Socket.IO event (put it below @socketio.on): the page is told with an `error` event"""
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        if not unlocked():
            emit('error', {'message': LOCKED_MESSAGE, 'locked': True})
            return None
        return function(*args, **kwargs)
    return wrapper


def status():
    return {'success': True, 'has_password': login.has_password(), 'fixed': login.fixed, 'unlocked': unlocked()}


def with_cookie(answer, value):
    """A JSON answer carrying the session cookie ('' removes it)"""
    response = jsonify(answer)
    response.headers.add('Set-Cookie', login.cookie_header(value))
    return response


@app.route('/api/settings_lock', methods=['GET'])
def settings_lock_status():
    """Whether a settings password is set, and whether this browser has unlocked the guarded actions"""
    return jsonify(status())


@app.route('/api/settings_lock', methods=['POST'])
def settings_unlock():
    """Unlock with the password (JSON: password): sets the session cookie"""
    try:
        right = login.check((request.get_json(silent=True) or {}).get('password'))
    except ValueError as e:                      # locked after wrong attempts
        return jsonify({'success': False, 'error': str(e)}), 429
    if not right:
        logger.warning(f"Wrong settings password from {request.remote_addr}")
        return jsonify({'success': False, 'error': 'Wrong password'}), 403
    return with_cookie({**status(), 'unlocked': True}, login.new_session())


@app.route('/api/settings_lock', methods=['DELETE'])
def settings_lock():
    """Lock again (this browser's cookie is removed)"""
    return with_cookie({**status(), 'unlocked': not login.has_password()}, '')


@app.route('/api/settings_password', methods=['POST'])
def settings_password():
    """
    Choose, change or (with an empty password) remove the settings password (JSON: password).
    The first password can be set by anyone - nothing was locked before; after that this needs
    the current one (the unlocked cookie). Setting a password logs this browser in; the others
    must unlock with it.
    """
    if not unlocked():
        return jsonify({'success': False, 'error': LOCKED_MESSAGE, 'locked': True}), 401
    password = str((request.get_json(silent=True) or {}).get('password') or '')
    try:
        if not password:
            if login.fixed:
                raise ValueError("The password is set by SCANNER_SETTINGS_PASSWORD in the environment - remove it there")
            login.remove_password()
            logger.info("Settings password removed")
            return with_cookie(status(), '')
        login.set_password(password)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    logger.info("Settings password set")
    return with_cookie({**status(), 'unlocked': True}, login.new_session())
