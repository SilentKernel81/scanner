# ============================================================================
# FILE: station_setup.py
# A camera station's own setup page: which server it sends its cards to, what
# it is called there, and which camera it uses.
#
# Why it exists: a station used to be set up with environment variables and
# config.yaml only, which means logging into the machine. With this page a
# station is started (the Docker image, or `python src/station_client.py`) and set
# up from a browser: http://<the station's address>:8080
#
# What is here, top to bottom:
#   1. The saved settings (data/station.json) and who wins when the same
#      thing is set in more than one place (resolve).
#   2. Finding the cameras plugged into this machine (list_cameras) and taking
#      one picture from any of them (take_picture).
#   3. The little web server and its routes (SetupPage).
# The page is behind a password: station_login.py has how it is kept and checked.
# The page itself - HTML, CSS and JavaScript in one file - is station_setup.html.
#
# What is NOT here: everything about scanning. Focus, rotation, fixed area and
# "add automatically" are set on the camera's page on the server and saved
# there (remote_scanner.py: CAMERA_SETTINGS). This page only covers what a
# station has to know before it can reach the server at all.
#
# Like the rest of the station it must not import the server's modules, and it
# uses Python's built-in web server so that the station needs no Flask.
# ============================================================================
import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
import requests

import station_login
from config import Config, camera_index_from, camera_index_or_none

logger = logging.getLogger('station')

SETTINGS_FILE = Config.DATA_DIR / 'station.json'
PAGE_FILE = Path(__file__).with_name('station_setup.html')
DEFAULT_PORT = 8080
ID_PATTERN = r'[A-Za-z0-9_-]{1,40}'  # the same rule the server has for a station's id
CAMERA_KINDS = ('auto', 'usb', 'picamera')

# The settings this page knows, with the value each has when nobody set it.
#   server        http://<address>:5000 of the scanner server
#   id            how the server tells stations apart; None = this machine's name
#   name          what the server shows; None = the id
#   token         the server's station token, if it asks for one
#   camera        'auto' (a USB camera if there is one, else a Pi camera), 'usb' or 'picamera'
#   camera_index  which USB camera: a number (N of /dev/videoN) or a /dev/v4l/by-id/... path
#   crop          Pi camera only: the part of the sensor's image to use, [x1, y1, x2, y2] as
#                 fractions of the whole image; None = what config.yaml says (normally: all)
DEFAULTS = {'server': '', 'id': None, 'name': None, 'token': '', 'camera': None, 'camera_index': None, 'crop': None}

# The environment variable that sets each of them. A variable that is set wins over the saved
# file, so a station configured the old way (.env, docker-compose) behaves exactly as before -
# and the page shows such a field as fixed instead of pretending it can change it.
ENVIRONMENT = {'server': 'SCANNER_SERVER', 'id': 'SCANNER_STATION_ID', 'name': 'SCANNER_STATION_NAME',
               'token': 'SCANNER_STATION_TOKEN', 'camera': 'SCANNER_CAMERA', 'camera_index': 'SCANNER_CAMERA_INDEX'}


# ----------------------------------------------------------------------------
# 1. Saved settings
# ----------------------------------------------------------------------------

def load():
    """The settings saved by the page ({} when there is no file yet, or it cannot be read)"""
    try:
        saved = json.loads(SETTINGS_FILE.read_text())
        return saved if isinstance(saved, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        # A broken file must not stop the station from starting: it starts as "not set up"
        # and the page can save a good file over it
        logger.warning(f"{SETTINGS_FILE} could not be read ({e}) - starting without it")
        return {}


def save(values):
    """
    Write the settings file. First to a temporary file, then renamed over the real one: a
    rename either happens or does not, so a power cut in the middle (a Pi that is unplugged)
    leaves the old file or the new one, never half a file.
    """
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_FILE.with_suffix('.json.tmp')
    temporary.write_text(json.dumps({key: values.get(key) for key in DEFAULTS}, indent=2))
    os.replace(temporary, SETTINGS_FILE)
    try:
        os.chmod(SETTINGS_FILE, 0o600)  # it can hold the server's token: this user only
    except OSError:
        pass


def resolve(arguments, saved, machine_name):
    """
    What the station really runs with, and which of it the page may not change.

    Each setting is looked for in this order, the first place that has it wins:
        1. the command line          (python src/station_client.py --server ...)
        2. the environment           (SCANNER_SERVER=..., as the Docker image is given them)
        3. the file the page saved   (data/station.json)
        4. the default
    Returns (values, fixed): `fixed` maps the names decided by 1 or 2 to where they came
    from - the page shows those as read-only.
    """
    values, fixed = {}, {}
    for key, default in DEFAULTS.items():
        variable = ENVIRONMENT.get(key)
        if arguments.get(key):
            values[key], fixed[key] = arguments[key], "the command line"
        elif variable and os.getenv(variable):
            values[key], fixed[key] = os.getenv(variable), f"{variable} in the environment"
        elif saved.get(key) not in (None, ''):
            values[key] = saved[key]
        else:
            values[key] = default
    values['server'] = (values['server'] or '').rstrip('/')
    values['id'] = values['id'] or machine_name
    values['name'] = values['name'] or values['id']
    return values, fixed


def apply_camera(values, fixed):
    """
    Put the chosen camera into Config, where CardScanner reads it. Must run before the
    scanner is created (it opens the camera when it is created).

    config.py has already taken the camera from config.yaml and the environment when it was
    imported. So only what the page saved is applied here, and only where the environment did
    not decide (`fixed`): the order of resolve() stays true for the camera too.
    """
    if 'camera' not in fixed and values.get('camera') in CAMERA_KINDS:
        Config.CAMERA_TYPE = values['camera']
    if 'camera_index' not in fixed and values.get('camera_index') not in (None, ''):
        # The camera as it was chosen (a number, or its by-id path), and its number right now.
        # A saved camera that is unplugged has none: the scanner then says "no camera" and
        # looks for that same camera again every few seconds (scanner._open_camera) - it does
        # not quietly use another one.
        chosen = str(values['camera_index'])
        Config.USB_CAMERA_SOURCE = int(chosen) if chosen.isdigit() else chosen
        Config.USB_CAMERA_INDEX = camera_index_or_none(Config.USB_CAMERA_SOURCE)
    if values.get('crop'):
        Config.CAMERA_CROP = values['crop']


def checked(data, current):
    """
    The values of a "Save" from the page, checked and tidied - or ValueError with a sentence
    the page shows as it is. `current` is what is saved now: a field the page did not send
    keeps its value (the token is never sent to the browser, so it comes back empty unless
    it was typed anew).
    """
    values = {key: current.get(key, default) for key, default in DEFAULTS.items()}

    if 'server' in data:
        server = str(data['server'] or '').strip().rstrip('/')
        if server and not re.match(r'https?://', server):
            server = 'http://' + server  # "192.168.1.20:5000" is what people type
        try:
            # .port is read to check it: "http://server:bad" has a host name, but urlparse
            # raises ValueError for its port only when the port is asked for
            usable = bool(urlparse(server).hostname) and (urlparse(server).port or True)
        except ValueError:
            usable = False
        if server and not usable:
            raise ValueError("The server address is not an address, e.g. http://192.168.1.20:5000")
        values['server'] = server

    if 'id' in data:
        station_id = str(data['id'] or '').strip()
        if station_id and not re.fullmatch(ID_PATTERN, station_id):
            raise ValueError("The id must be 1-40 letters, digits, - or _")
        values['id'] = station_id or None

    if 'name' in data:
        values['name'] = str(data['name'] or '').strip()[:60] or None

    # The token: changed only when one was typed, removed only when asked
    if data.get('remove_token'):
        values['token'] = ''
    elif data.get('token'):
        values['token'] = str(data['token']).strip()

    if 'camera' in data:
        if data['camera'] not in CAMERA_KINDS:
            raise ValueError("Unknown kind of camera")
        values['camera'] = data['camera']
        index = str(data.get('camera_index') or '').strip()
        values['camera_index'] = index or None
        if values['camera'] == 'usb' and index:
            camera_index_from(index, source="The camera")  # raises ValueError if it is not a camera

    if 'crop' in data:
        values['crop'] = checked_crop(data['crop'])
    return values


def checked_crop(crop):
    """[x1, y1, x2, y2] as fractions, in order and inside the image - or None for "all of it" """
    if not crop:
        return None
    try:
        x1, y1, x2, y2 = (min(1.0, max(0.0, float(v))) for v in crop)
    except (TypeError, ValueError):
        raise ValueError("The crop must be four numbers")
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    if x2 - x1 < 0.05 or y2 - y1 < 0.05:
        raise ValueError("The crop is too small - drag a larger area")
    if (x1, y1, x2, y2) == (0.0, 0.0, 1.0, 1.0):
        return None
    return [round(v, 4) for v in (x1, y1, x2, y2)]


# ----------------------------------------------------------------------------
# 2. Cameras
# ----------------------------------------------------------------------------

def list_cameras():
    """
    The cameras this machine has, for the page's list:
        [{'kind': 'usb' | 'picamera', 'index': text for camera_index, 'label': text shown}]
    """
    return usb_cameras() + pi_cameras()


def usb_cameras():
    """
    USB webcams, read from /sys (Linux's own description of its devices) - no camera is
    opened for this, so it also works while the scanner is using one.

    /sys/class/video4linux has one entry per /dev/videoN. Two things are filtered out:
      - devices that are not USB: a Raspberry Pi has a dozen /dev/videoN of its own (its
        image processor, its video codecs), none of them a camera to choose;
      - the second node of a webcam: every webcam appears twice, as index 0 (the pictures)
        and index 1 (data about the pictures).
    """
    cameras = []
    nodes = sorted(Path('/sys/class/video4linux').glob('video*'), key=lambda node: int(node.name[5:] or 0))
    for node in nodes:
        try:
            if '/usb' not in os.path.realpath(node / 'device'):
                continue
            if (node / 'index').read_text().strip() != '0':
                continue
            name = (node / 'name').read_text().strip()
        except OSError:
            continue  # unplugged while we were looking
        device = f"/dev/{node.name}"
        cameras.append({'kind': 'usb', 'index': stable_path(device) or node.name[5:],
                        'label': f"{name}  ({device})"})
    return cameras


def stable_path(device):
    """
    The /dev/v4l/by-id/... name of a /dev/videoN, if it has one. The number N can change
    when cameras are plugged in a different order or the machine restarts; the by-id name
    is made from the camera's maker, model and serial number and stays the same camera.
    """
    for link in sorted(Path('/dev/v4l/by-id').glob('*')):
        if os.path.realpath(link) == device:
            return str(link)
    return None


def pi_cameras():
    """
    Raspberry Pi camera modules, asked from picamera2 (which is only installed on a Pi - on
    any other machine the import fails and the list is empty). libcamera also lists USB
    webcams; those are left to usb_cameras(), which knows their /dev/videoN.
    """
    try:
        from picamera2 import Picamera2
        found = Picamera2.global_camera_info()
    except Exception:
        return []
    return [{'kind': 'picamera', 'index': '', 'label': f"Raspberry Pi camera  ({info.get('Model', '?')})"}
            for info in found if 'usb' not in str(info.get('Id', '')).lower()]


def take_picture(kind, index=None):
    """
    Open a camera, take one picture, close it again: the picture as JPEG bytes. For the page's
    "Show picture" - to see which camera is which, and for a Pi camera to draw the crop on.
    It is the camera's whole image, not rotated and not cropped.

    The camera must be free: only one program part can hold it. SetupPage.picture() sees to
    that when the scanner is using this very camera.
    """
    if kind == 'picamera':
        frame = pi_camera_picture()
    else:
        frame = usb_camera_picture(camera_index_from(index if index not in (None, '') else Config.USB_CAMERA_SOURCE,
                                                     source="The camera"))
    ok, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    if not ok:
        raise RuntimeError("The picture could not be encoded")
    return jpeg.tobytes()


def usb_camera_picture(number):
    """One picture (BGR, as OpenCV has them) from /dev/video<number>"""
    camera = cv2.VideoCapture(number, cv2.CAP_V4L2)
    try:
        if not camera.isOpened():
            raise RuntimeError(f"/dev/video{number} could not be opened (in use by another program?)")
        # Webcams only give large pictures as MJPEG; 1280x720 is plenty for a look
        camera.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        frame = None
        for _ in range(8):  # the first pictures after opening are dark: exposure is still adjusting
            ok, picture = camera.read()
            if ok:
                frame = picture
        if frame is None:
            raise RuntimeError(f"/dev/video{number} opened but sends no pictures")
        return frame
    finally:
        camera.release()


def pi_camera_picture(width=1152):
    """One picture (BGR) of everything a Pi camera's sensor sees, scaled down to `width`"""
    from picamera2 import Picamera2
    camera = Picamera2()
    try:
        sensor_width, sensor_height = camera.sensor_resolution
        size = (width, int(width * sensor_height / sensor_width) // 2 * 2)
        # "RGB888" is libcamera's name for blue, green, red in memory - OpenCV's order
        camera.configure(camera.create_still_configuration(main={'size': size, 'format': 'RGB888'}))
        camera.start()
        time.sleep(1.5)  # exposure, white balance and autofocus settle
        return camera.capture_array()
    finally:
        camera.stop()
        camera.close()


def machine_temperature():
    """The processor's temperature in °C (a Raspberry Pi and most Linux machines), or None"""
    try:
        return round(int(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000, 1)
    except (OSError, ValueError):
        return None


# ----------------------------------------------------------------------------
# 3. The web server
# ----------------------------------------------------------------------------

class SetupPage:
    """
    The setup page's web server. It runs in its own thread beside the station; `station` is
    the running Station, or None while the station has no server address yet ("setup mode").

    Routes (all answer JSON except the page and the picture):
        GET  /              the page (its HTML holds no settings, so it needs no login)
        POST /api/login     {password} - or {new_password} while no password has been chosen yet
        POST /api/logout
      and, only for a browser that is logged in (anything else is answered 401):
        GET  /api/state     settings, what is fixed, the cameras found, how the station is doing
        GET  /api/picture   ?kind=usb|picamera&index=...  one JPEG from that camera
        POST /api/test      {server, token}  can that server be reached, and does it take the token?
        POST /api/save      the settings; the station then restarts with them
        POST /api/restart   restart without changing anything
        POST /api/password  {password, new_password}  change the page's password
        POST /api/held      {action: 'send' | 'delete'}  captures taken for another server
                            (station_client.py: "Captures that are not on the server yet")
    """

    def __init__(self, station, values, fixed, restart, port=DEFAULT_PORT):
        self.station = station
        self.values = values      # what the station runs with now (resolve)
        self.fixed = fixed        # the names the command line / environment decided
        self.restart = restart    # call it to restart the station (station_client.request_restart)
        self.port = port
        self.picture_lock = threading.Lock()  # one picture at a time: a camera opens only once
        self.login = station_login.Login()    # the page's password and who is logged in

    def start(self):
        """Start serving in a background thread. False when the port cannot be used."""
        page = self

        class Handler(BaseHTTPRequestHandler):
            """One instance per request; do_GET / do_POST are called by Python's web server"""

            def log_message(self, *_args):
                pass  # no line per request in the station's log

            def logged_in(self):
                """True if this request carries a valid session cookie; else it is answered 401"""
                if page.login.valid_session(station_login.session_from(self.headers.get('Cookie'))):
                    return True
                # The page shows its login form on this answer - "create" while no password
                # exists yet, so that it asks to choose one instead
                self.send_json({'error': 'Log in first', 'login': 'needed' if page.login.has_password() else 'create'}, 401)
                return False

            def do_GET(self):
                url = urlparse(self.path)
                if url.path == '/':
                    self.send(200, PAGE_FILE.read_bytes(), 'text/html; charset=utf-8')
                elif not self.logged_in():
                    pass  # answered by logged_in()
                elif url.path == '/api/state':
                    self.send_json(page.state())
                elif url.path == '/api/picture':
                    query = parse_qs(url.query)
                    try:
                        jpeg = page.picture(query.get('kind', ['usb'])[0], query.get('index', [''])[0])
                        self.send(200, jpeg, 'image/jpeg')
                    except Exception as e:
                        self.send_json({'error': str(e)}, 500)
                else:
                    self.send_json({'error': 'Not found'}, 404)

            def do_POST(self):
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                    if length > 65536:  # a settings form is a few hundred bytes
                        return self.send_json({'error': 'Too large'}, 413)
                    data = json.loads(self.rfile.read(length) or b'{}')
                    if not isinstance(data, dict):
                        raise ValueError("Not a settings form")
                except ValueError:
                    return self.send_json({'error': 'Not understood'}, 400)
                if self.path == '/api/login':
                    return self.log_in(data)
                if self.path == '/api/logout':
                    return self.send_json({'ok': True}, headers=[('Set-Cookie', page.login.cookie_header(''))])
                if not self.logged_in():
                    return
                if self.path == '/api/password':
                    try:
                        if not page.login.check(data.get('password')):
                            raise ValueError("The current password is wrong")
                        page.login.set_password(data.get('new_password'))
                    except ValueError as e:
                        return self.send_json({'error': str(e)}, 400)
                    # The old cookie was signed with the old key: hand out one for the new
                    return self.send_json({'ok': True}, headers=[('Set-Cookie', page.login.cookie_header(page.login.new_session()))])
                if self.path == '/api/test':
                    self.send_json(page.test(data))
                elif self.path == '/api/save':
                    try:
                        self.send_json(page.save(data))
                    except ValueError as e:
                        self.send_json({'error': str(e)}, 400)
                elif self.path == '/api/held':
                    self.send_json(page.held(data.get('action')))
                elif self.path == '/api/restart':
                    self.send_json({'ok': True})
                    page.restart()
                else:
                    self.send_json({'error': 'Not found'}, 404)

            def log_in(self, data):
                """
                POST /api/login. While no password exists the first visitor chooses one
                ({new_password}); after that only the right password logs in. Either way the
                answer carries the session cookie.
                """
                try:
                    if not page.login.has_password():
                        page.login.set_password(data.get('new_password'))
                        logger.info("Setup page: a password was chosen")
                    elif not page.login.check(data.get('password')):
                        raise ValueError("Wrong password")
                except ValueError as e:
                    return self.send_json({'error': str(e)}, 403)
                self.send_json({'ok': True}, headers=[('Set-Cookie', page.login.cookie_header(page.login.new_session()))])

            def send_json(self, data, status=200, headers=()):
                self.send(status, json.dumps(data).encode(), 'application/json', headers)

            def send(self, status, body, content_type, headers=()):
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')  # always the current state and picture
                self.end_headers()
                self.wfile.write(body)

        try:
            # '' = every network address of this machine. ThreadingHTTPServer answers each
            # request in its own thread, so a slow one (a picture) does not hold up the rest
            server = ThreadingHTTPServer(('', self.port), Handler)
        except OSError as e:
            logger.warning(f"The setup page could not start on port {self.port} ({e})")
            return False
        threading.Thread(target=server.serve_forever, daemon=True, name="SetupPage").start()
        return True

    # ------------------------------------------------------------------------

    def state(self):
        """Everything the page shows. The token itself is never sent: only whether there is one."""
        station, scanner = self.station, getattr(self.station, 'scanner', None)
        settings = {key: self.values.get(key) for key in DEFAULTS if key != 'token'}
        settings['camera'] = settings['camera'] or Config.CAMERA_TYPE
        if settings['camera_index'] in (None, ''):
            settings['camera_index'] = str(Config.USB_CAMERA_SOURCE)
        settings['crop'] = settings['crop'] or Config.CAMERA_CROP
        return {
            'settings': settings,
            'has_token': bool(self.values.get('token')),
            'fixed': self.fixed,
            'password_fixed': self.login.fixed,  # set from the environment: not changeable here
            'cameras': list_cameras(),
            'status': {
                'set_up': station is not None,
                'connected': bool(station and station.connected()),
                # None until the scanner exists: it is created when the server first answers
                'camera_type': getattr(scanner, 'camera_type', None),
                'camera_error': getattr(scanner, 'camera_error', None) if scanner else None,
                'scanning': bool(scanner and scanner.camera is not None),
                'frames_per_second': round(scanner.frame_rate, 1) if scanner and scanner.camera is not None else None,
                'waiting_uploads': station.pending_count() if station else 0,
                # Captures an earlier run took for another server or station id: kept until
                # the page says what to do with them ({count, server, station, oldest} or None)
                'held_uploads': station.held_summary() if station else None,
                'temperature': machine_temperature(),
            },
        }

    def picture(self, kind, index):
        """
        One JPEG from the camera the page asks for - which may be the one the scanner is
        using right now:
          - a USB camera in use: the scanner's current frame is that picture already;
          - a Pi camera in use: the scanner shows only its crop, and the page needs the whole
            image to draw a crop on. The scanner lets go of the camera for a moment
            (with_camera_released), the picture is taken, and scanning goes on;
          - any other camera: it is free, and is simply opened.
        """
        if kind not in ('usb', 'picamera'):
            raise ValueError("Unknown kind of camera")
        scanner = getattr(self.station, 'scanner', None)
        with self.picture_lock:
            if scanner is not None and scanner.camera is not None and scanner.camera_type == kind:
                if kind == 'picamera':
                    return scanner.with_camera_released(lambda: take_picture('picamera'))
                if camera_index_or_none(index or Config.USB_CAMERA_SOURCE) == Config.USB_CAMERA_INDEX:
                    frame = scanner.get_full_frame()
                    if frame is None:
                        raise RuntimeError("The camera has no picture yet")
                    ok, jpeg = cv2.imencode('.jpg', cv2.cvtColor(frame, cv2.COLOR_RGB2BGR),
                                            [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                    return jpeg.tobytes()
            return take_picture(kind, index)

    def test(self, data):
        """
        "Test connection", in two steps, and a sentence for the page about what came back:

        1. Is a scanner server at the address? It is asked for its list of stations -
           something only a scanner server answers. That answer also says whether the server
           wants a station token.
        2. If it wants one: does it accept the token typed in (or the saved one)? The list
           of stations is given to anyone, with or without a token, so it proves nothing
           about the token. The server is therefore asked something it only answers to a
           station with the right token - what became of this station's capture number 0.
           No such capture exists, and nothing is changed by asking: the right token gets
           "no such capture" (404), a wrong one "wrong or missing station token" (401).
        """
        try:
            server = checked({'server': data.get('server')}, {})['server']
        except ValueError as e:
            return {'ok': False, 'message': str(e)}
        if not server:
            return {'ok': False, 'message': "Type the server's address first"}
        token = str(data.get('token') or '') or self.values.get('token') or ''
        headers = {'X-Station-Token': token} if token else {}

        # Step 1. The request and the reading of its answer are kept apart on purpose: an
        # address that cannot be requested at all raises an error that is both a "request
        # failed" and a ValueError, and treating it as "answered, but not with JSON" went on
        # to use an answer that was never received.
        try:
            answer = requests.get(f"{server}/api/stations", timeout=5, headers=headers)
        except (requests.RequestException, ValueError) as e:
            return {'ok': False, 'message': f"No answer from {server} ({type(e).__name__})"}
        try:
            body = answer.json()
        except ValueError:
            body = None  # something answered, but not with JSON: a router's page, another web server
        stations = body.get('stations') if isinstance(body, dict) else None
        if answer.status_code != 200 or not isinstance(stations, list):
            return {'ok': False, 'message': f"{server} answers, but not as a scanner server"}
        message = f"Scanner server found ({len(stations)} station{'s' if len(stations) != 1 else ''} known)"
        if not body.get('token_required'):
            return {'ok': True, 'address': server, 'message': message}
        if not token:
            return {'ok': False, 'address': server, 'message': message + " - it asks for a station token"}

        # Step 2
        try:
            check = requests.get(f"{server}/api/stations/{self.values['id']}/captures/0", timeout=5, headers=headers)
        except requests.RequestException as e:
            return {'ok': False, 'address': server, 'message': message + f" - but the token could not be checked ({type(e).__name__})"}
        if check.status_code == 401:
            return {'ok': False, 'address': server, 'message': message + " - but it does not accept this station token"}
        return {'ok': True, 'address': server, 'message': message + " - the station token is accepted"}

    def held(self, action):
        """
        What to do with captures that were taken for another server or under another station
        id: 'send' them to the server this station has now, or 'delete' them. Only a person
        can know which is right - the old server may have moved to a new address (send), or
        the station may have been pointed at a different collection (don't).
        """
        if self.station is None or action not in ('send', 'delete'):
            return {'error': 'Nothing to do'}
        count = self.station.send_held() if action == 'send' else self.station.delete_held()
        logger.info(f"Setup page: {count} held captures {'queued for this server' if action == 'send' else 'deleted'}")
        return {'ok': True, 'count': count}

    def save(self, data):
        """Check and save the form, then restart the station so that it runs with it"""
        # Fields that the environment decides are not saved: they would only be ignored at the
        # next start, and the file would tell a different story than the station
        data = {key: value for key, value in data.items() if key not in self.fixed}
        if 'camera' in self.fixed or 'camera_index' in self.fixed:
            data.pop('camera', None)
            data.pop('camera_index', None)
        values = checked(data, load())
        save(values)
        logger.info("Settings saved from the setup page - restarting with them")
        self.restart()
        return {'ok': True}
