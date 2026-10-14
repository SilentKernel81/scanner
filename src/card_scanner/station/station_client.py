#!/usr/bin/env python3
"""
Camera station: runs next to the camera, on any Linux machine. It finds the card, captures it
once it is still and keeps the focus (CardScanner - the same code and thresholds as ever), and
sends each captured card to the scanner server, which reads it, matches it and keeps the
collection. The server's scanner page shows this camera's live view and controls it.

    python station.py --server http://192.168.1.20:5000      (station.py, in the top folder, starts this)
    (or SCANNER_SERVER / SCANNER_STATION_ID / SCANNER_STATION_NAME / SCANNER_STATION_TOKEN)

or without any of that: the station then serves its setup page (station_setup.py,
http://<this machine>:8080), where the server, the station's name and the camera are chosen
and saved in data/station.json.

Little else is kept here: camera settings (focus, rotation, fixed area) are saved on the
server, and a captured image is deleted once the server has it. Until then it lies in
station_captures/ with a note beside it (<image>.upload.json) saying what it is and where it
goes, so that a capture taken while the server was away is still sent after this program -
or the whole machine - has been restarted. Resolution and the capture thresholds are in
config.yaml. See remote_scanner.py for the messages.
"""

import argparse
import json
import logging
import os
import queue
import re
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import cv2
import requests
import signal
import socketio

from card_scanner.station import station_setup
from card_scanner.config import Config

NAMESPACE = '/station'
STATUS_INTERVAL = 0.2    # seconds between status reports
PREVIEW_INTERVAL = 0.08  # at most ~12 preview frames a second, while the page is watched
NOTE_SUFFIX = '.upload.json'  # the note beside a capture that is not on the server yet
# What the server may call and set (remote_scanner.py: METHODS, DECIDED, SENT)
METHODS = ('set_rotation', 'set_refocus_every', 'set_fixed_area', 'set_detection_enabled',
           'set_continuous_autofocus', 'reset_focus', 'detected_area')
SETTABLE = ('fast_scan_mode', 'required_stable_frames', 'auto_capture_delay', 'debug_trace_enabled',
            'auto_capture_enabled', 'card_under_review', 'capture_pending', 'stable_frames')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('station')
# CardScanner's lines for the page are logged here (send_log), not a second time by its own
# logger; what it writes to the log only (why a card waits, focus probes, saved debug frames)
# has no other way to this console
logging.getLogger('scanner').propagate = False
scanner_console = logging.StreamHandler()
scanner_console.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s', datefmt='%H:%M:%S'))
scanner_console.addFilter(lambda record: record.funcName != 'log')
logging.getLogger('scanner').addHandler(scanner_console)


class ServerSettings:
    """The camera's settings as CardScanner uses them (get / set): kept by the server"""

    def __init__(self, values, on_change):
        self.values = dict(values)
        self.on_change = on_change

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value
        self.on_change(key, value)


class Station:
    def __init__(self, server, station_id, name, token):
        self.server = server.rstrip('/')
        self.id = station_id
        self.name = name
        self.token = token
        self.setup_port = 0  # the port of this station's setup page (0: none): told to the server, which links to it
        self.scanner = None
        self.capture_number = 0
        self.preview_wanted = False
        self.stopping = False  # Set to leave run(): the setup page saved new settings (request_restart)
        self.uploads = queue.Queue()
        self.upload_idle = threading.Event()  # Clear while a capture is being sent (wait_for_upload)
        self.upload_idle.set()
        # Captures found at the start that were taken for another server or under another
        # station id: kept, not sent, until the setup page says what to do with them
        self.held = []
        self.held_lock = threading.Lock()
        self.sio = socketio.Client(reconnection=True, reconnection_delay=1, reconnection_delay_max=5)
        self.sio.on('connect', self.on_connect, namespace=NAMESPACE)
        self.sio.on('disconnect', self.on_disconnect, namespace=NAMESPACE)
        self.sio.on('command', self.on_command, namespace=NAMESPACE)
        self.sio.on('set', self.on_set, namespace=NAMESPACE)

    # ------------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------------

    def run(self):
        self.load_pending()  # captures an earlier run could not send, before any new one
        threading.Thread(target=self.upload_loop, daemon=True, name="Uploads").start()
        while not self.stopping:
            try:
                self.sio.connect(self.server, namespaces=[NAMESPACE], wait_timeout=10,
                                 auth={'id': self.id, 'name': self.name, 'token': self.token,
                                       'setup_port': self.setup_port})
                break
            except socketio.exceptions.ConnectionError as e:
                logger.warning(f"Server {self.server} not reachable ({e}) - trying again in 3 s")
                time.sleep(3)
        try:
            self.report_loop()
        except KeyboardInterrupt:
            pass
        finally:
            if self.scanner:
                self.scanner.cleanup()
            self.sio.disconnect()

    def connected(self):
        return self.sio.connected and NAMESPACE in self.sio.namespaces

    def emit(self, event, data, callback=None):
        if self.connected():
            try:
                self.sio.emit(event, data, namespace=NAMESPACE, callback=callback)
            except socketio.exceptions.SocketIOError:
                pass  # the connection just went: the next report will find out

    def on_connect(self):
        logger.info(f"Connected to {self.server} as {self.name} ({self.id})")
        # Not from this handler's thread: hello waits for the server's answer
        threading.Thread(target=self.hello, daemon=True).start()

    def on_disconnect(self, *_args):
        logger.warning("Connection to the server lost - scanning goes on, captures wait to be sent")
        self.preview_wanted = False

    def hello(self):
        """Take the camera's settings from the server; the first time, start the scanner with them"""
        try:
            answer = self.sio.call('hello', {}, namespace=NAMESPACE, timeout=10) or {}
        except Exception as e:
            logger.error(f"The server did not answer hello: {e}")
            return
        if self.scanner is None:
            from card_scanner.station.scanner import CardScanner  # opens the camera
            settings = ServerSettings(answer.get('settings') or {},
                                      lambda key, value: self.emit('setting', {'key': key, 'value': value}))
            scanner = CardScanner(log_callback=self.send_log, settings=settings)
            scanner.fast_scan_mode = True
            scanner.auto_capture_callback = self.auto_captured
            self.apply(scanner, answer.get('set') or {})
            self.scanner = scanner
        else:
            self.apply(self.scanner, answer.get('set') or {})

    @staticmethod
    def apply(scanner, values):
        for name, value in values.items():
            if name in SETTABLE:
                setattr(scanner, name, value)

    def send_log(self, message, level="info"):
        getattr(logger, 'info' if level == 'success' else level, logger.info)(message)
        self.emit('log', {'message': message, 'level': level})

    # ------------------------------------------------------------------------
    # What the server asks for
    # ------------------------------------------------------------------------

    def state(self):
        """What the server shows and decides by (RemoteScanner: REPORTED and SENT names, the camera settings)"""
        s = self.scanner
        return {
            'camera_error': s.camera_error, 'camera_type': s.camera_type,
            'auto_capture_enabled': bool(s.auto_capture_enabled), 'card_under_review': bool(s.card_under_review),
            'capture_pending': bool(s.capture_pending), 'rotation': s.rotation, 'refocus_every': s.refocus_every,
            'fixed_area': s.fixed_area, 'fixed_area_enabled': bool(s.fixed_area_enabled),
            'focus_locked_value': s.focus_locked_value, 'focus_sweep_running': bool(s.focus_sweep_running),
            'focus_probe_running': bool(s.focus_probe_running), 'enable_detection': bool(s.enable_detection),
        }

    def on_set(self, values):
        if self.scanner:
            self.apply(self.scanner, values or {})

    def on_command(self, data):
        """Run a CardScanner method for the server; the answer carries the state after it"""
        if self.scanner is None:
            return {'ok': False, 'error': 'The camera is still starting'}
        method = (data or {}).get('method')
        try:
            if method == 'capture':
                threading.Thread(target=self.manual_capture, daemon=True).start()
                result = True
            elif method in METHODS:
                result = getattr(self.scanner, method)(*data.get('args', []), **data.get('kwargs', {}))
            else:
                return {'ok': False, 'error': f'Unknown command {method}'}
        except (TypeError, ValueError) as e:
            return {'ok': False, 'error': str(e)}
        return {'ok': True, 'result': result, 'state': self.state()}

    def report_loop(self):
        """Status several times a second; the preview while the scanner page is watched"""
        last_status = last_preview = 0.0
        last_frame = -1
        while not self.stopping:
            time.sleep(0.02)  # the two intervals below are checked 50 times a second
            scanner = self.scanner
            if scanner is None or not self.connected():
                continue  # still starting, or the server is away: scanning goes on without reports
            now = time.time()
            if now - last_status >= STATUS_INTERVAL:
                last_status = now
                self.emit('status', {'state': self.state(), 'detection': scanner.get_detection_status()},
                          callback=self.status_answered)
            if self.preview_wanted and now - last_preview >= PREVIEW_INTERVAL:
                frame_id, jpeg = scanner.get_stream_jpeg()
                if jpeg is not None and frame_id != last_frame:
                    last_frame, last_preview = frame_id, now
                    self.emit('preview', {'jpeg': jpeg})

    def status_answered(self, answer=None):
        self.preview_wanted = bool((answer or {}).get('preview'))

    # ------------------------------------------------------------------------
    # Captures
    # ------------------------------------------------------------------------

    def auto_captured(self):
        """CardScanner's auto-capture callback (its own thread): take the image, beep, send it"""
        scanner = self.scanner
        fast = bool(getattr(scanner, 'fast_scan_mode', True))  # as it is now, not when it is sent
        if not scanner.is_card_detected():
            logger.warning("Auto-capture triggered but no card detected")
            scanner.capture_pending = False
            scanner.card_under_review = False
            return
        self.capture_number += 1
        number = self.capture_number
        image_path, card_image, foil_image = scanner.capture_card_image_only(number, settle=0)
        if not image_path:
            scanner.capture_pending = False
            scanner.card_under_review = False
            return
        # The capture beep - the signal to drop the next card: once the image is taken and a
        # focus probe started by this capture (~1 s, every refocus_every cards) is done
        deadline = time.time() + 2.5
        while scanner.focus_probe_running and time.time() < deadline:
            time.sleep(0.05)
        scanner.capture_pending = False
        self.emit('captured', {'number': number})
        if fast:
            scanner.card_under_review = False  # ready for the next drop; else until Add / Skip on the page
        self.queue_upload(image_path, card_image, foil_image, 'auto' if fast else 'review')

    def manual_capture(self):
        """The page's Capture button: whatever is in view, shown on the page for Add / Skip"""
        scanner = self.scanner
        self.capture_number += 1
        image_path, card_image, foil_image = scanner.capture_card_image_only(self.capture_number)
        if image_path:
            self.queue_upload(image_path, card_image, foil_image, 'review')
        else:
            self.send_log("Failed to capture image", level="error")

    def queue_upload(self, image_path, card_image, foil_image, mode):
        foil_path = None
        if foil_image is not None and foil_image is not card_image:
            # Fixed area: the capture is the area, the foil check uses the outlined card
            foil_path = Path(image_path).with_name(f"{Path(image_path).stem}_foil.jpg")
            cv2.imwrite(str(foil_path), cv2.cvtColor(foil_image, cv2.COLOR_RGB2BGR))
        item = {'image': Path(image_path), 'foil': foil_path, 'mode': mode,
                'foil_is_image': foil_image is card_image, 'capture_id': uuid.uuid4().hex}
        # The note first, then the queue: from here on the capture is on record on disk,
        # whatever happens to this program
        self.write_note(item)
        self.uploads.put(item)

    # ------------------------------------------------------------------------
    # Captures that are not on the server yet
    #
    # The queue above lives in memory only. If this program ends with captures in it - a
    # restart from the setup page, a power cut, `docker compose up --build` - they used to
    # stay behind as pictures nobody would ever send. So each queued capture also has a
    # small note on disk beside its picture, and a new start reads the notes back.
    #
    # The note holds the capture's capture_id. That id is what makes sending a capture
    # twice harmless: the server counts an id once (server/state.py: seen_captures; pending.py: settled_captures).
    # A capture that was being sent when the program ended may or may not have arrived - it
    # is sent again with the same id, and is one card either way.
    #
    # The note also holds where the capture was going: the server's address and this
    # station's id. A capture is only sent on its own if both are still the same. If the
    # setup page changed either, the capture is "held": it may belong to another server's
    # collection, and which one is not for this program to guess - the setup page shows the
    # held captures and asks (send_held / delete_held).
    # ------------------------------------------------------------------------

    @staticmethod
    def note_path(image):
        """card_3_1791754770.jpg -> card_3_1791754770.upload.json"""
        return Path(image).with_name(Path(image).stem + NOTE_SUFFIX)

    def write_note(self, item):
        """Put a capture on record on disk (written whole or not at all, as station_setup.save)"""
        note = {'capture_id': item['capture_id'], 'mode': item['mode'], 'foil_is_image': bool(item['foil_is_image']),
                'image': item['image'].name, 'foil': item['foil'].name if item['foil'] else None,
                'server': self.server, 'station': self.id, 'taken': time.strftime('%Y-%m-%d %H:%M:%S')}
        path = self.note_path(item['image'])
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(note, indent=2))
        os.replace(temporary, path)

    @staticmethod
    def remove_capture(item, keep_pictures=False):
        """A capture is settled: its note goes, and its pictures unless they are to be kept"""
        Station.note_path(item['image']).unlink(missing_ok=True)
        if not keep_pictures:
            for path in (item['image'], item['foil']):
                if path:
                    path.unlink(missing_ok=True)

    def load_pending(self):
        """
        At the start: queue the captures an earlier run left behind, oldest first. Those
        taken for another server or under another station id are held instead (see above).
        """
        self.adopt_old_folder()
        notes = sorted(Config.STATION_CAPTURES_DIR.glob('*' + NOTE_SUFFIX), key=lambda path: path.stat().st_mtime)
        queued = 0
        for path in notes:
            try:
                note = json.loads(path.read_text())
                item = {'image': path.with_name(note['image']),
                        'foil': path.with_name(note['foil']) if note.get('foil') else None,
                        'mode': note['mode'], 'foil_is_image': bool(note.get('foil_is_image')),
                        'capture_id': note['capture_id'],
                        'server': note.get('server'), 'station': note.get('station'), 'taken': note.get('taken')}
            except (OSError, ValueError, KeyError, TypeError) as e:
                logger.warning(f"{path.name} is not a capture's note ({e}) - left alone")
                continue
            if not item['image'].is_file():
                path.unlink(missing_ok=True)  # the picture is gone: nothing to send
                continue
            if (item['server'], item['station']) == (self.server, self.id):
                self.uploads.put(item)
                queued += 1
            else:
                with self.held_lock:
                    self.held.append(item)
        if queued:
            logger.info(f"{queued} captured card{'s' if queued != 1 else ''} from before the restart will be sent now")
        if self.held:
            logger.warning(f"{len(self.held)} captured cards were taken for {self.held[0]['server']} "
                           f"(station {self.held[0]['station']}) and are kept, not sent - the setup page asks what to do with them")

    @staticmethod
    def adopt_old_folder():
        """
        Until 2026-10-12 a station kept its waiting captures in scanned_cards/ - the folder
        the server keeps the captures it received in, and cleans old pictures out of. On a
        machine that runs both programs from one checkout, a capture that waited longer than
        the server keeps pictures (cleanup.days) would have been deleted before it was ever
        sent. Waiting captures now have a folder of their own, station_captures/.

        This moves over what an earlier version left in the old folder: every note, with its
        picture and foil picture. Notes mark what is the station's - the server never writes
        them - so nothing of the server's is touched. It runs at every start and finds
        nothing after the first.
        """
        Config.STATION_CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
        for note_path in sorted(Config.IMAGES_DIR.glob('*' + NOTE_SUFFIX)):
            try:
                note = json.loads(note_path.read_text())
                names = [note['image']] + ([note['foil']] if note.get('foil') else [])
                for name in names:
                    picture = note_path.with_name(name)
                    if picture.is_file():
                        os.replace(picture, Config.STATION_CAPTURES_DIR / name)
                os.replace(note_path, Config.STATION_CAPTURES_DIR / note_path.name)  # the note last: until then it is found here again
                logger.info(f"Moved a waiting capture to {Config.STATION_CAPTURES_DIR.name}/: {note['image']}")
            except (OSError, ValueError, KeyError, TypeError) as e:
                logger.warning(f"{note_path.name} could not be moved to {Config.STATION_CAPTURES_DIR.name}/ ({e}) - left where it is")

    def pending_count(self):
        """Captures taken but not on the server yet (queued or being sent), for the setup page"""
        return self.uploads.qsize() + (0 if self.upload_idle.is_set() else 1)

    def held_summary(self):
        """What the setup page shows about held captures: how many, and where they were going"""
        with self.held_lock:
            if not self.held:
                return None
            return {'count': len(self.held), 'server': self.held[0]['server'], 'station': self.held[0]['station'],
                    'oldest': self.held[0]['taken']}

    def send_held(self):
        """The setup page's "Send them to this server": the held captures join the queue"""
        with self.held_lock:
            held, self.held = self.held, []
        for item in held:
            self.write_note(item)  # the note now names this server: sent on its own from here on
            self.uploads.put(item)
        return len(held)

    def delete_held(self):
        """The setup page's "Delete them": the held captures' pictures and notes are removed"""
        with self.held_lock:
            held, self.held = self.held, []
        for item in held:
            self.remove_capture(item)
        return len(held)

    def wait_for_upload(self, seconds):
        """
        Before a restart: give a capture that is being sent right now the time to arrive.
        Not required for safety (it would be sent again under the same capture_id), but it
        spares the server a duplicate to recognise.
        """
        return self.upload_idle.wait(seconds)

    def upload_loop(self):
        """Send captures in order; one that can't be sent is tried again until it is"""
        session = requests.Session()
        while not self.stopping:
            try:
                item = self.uploads.get(timeout=0.5)  # not forever: `stopping` is looked at twice a second
            except queue.Empty:
                continue
            self.upload_idle.clear()
            try:
                self.send_until_settled(session, item)
            finally:
                self.upload_idle.set()

    def send_until_settled(self, session, item):
        """
        Send one capture, again and again until the server has it or refuses it for good. Left
        early only when the station is stopping: the capture's note stays, and the next start
        sends it.
        """
        delay = 1.0
        while not self.stopping:
            verdict = self.send_once(session, item)
            if verdict == 'accepted':
                self.remove_capture(item)
                return
            if verdict == 'refused':
                # The server will never take this picture (unreadable, too large): it is kept
                # here, but its note goes - it is not sent again, now or after a restart
                self.remove_capture(item, keep_pictures=True)
                return
            # Wait before the next try, in short steps so that a restart is not held up
            deadline = time.time() + delay
            while time.time() < deadline and not self.stopping:
                time.sleep(0.1)
            delay = min(delay * 2, 15.0)

    def send_once(self, session, item):
        """One attempt to send a capture: 'accepted', 'refused' or 'retry' (see upload_verdict)"""
        headers = {'X-Station-Token': self.token} if self.token else {}
        try:
            files = {'image': (item['image'].name, item['image'].read_bytes(), 'image/jpeg')}
            if item['foil']:
                files['foil_image'] = (item['foil'].name, item['foil'].read_bytes(), 'image/jpeg')
            response = session.post(
                f"{self.server}/api/stations/{self.id}/captures", files=files, headers=headers, timeout=90,
                # wait 0: the outcome is shown on the station's page, nobody waits for it here.
                # capture_id: this same capture sent again (no answer came) is still one card
                data={'name': self.name, 'camera': '1', 'mode': item['mode'], 'wait': '0',
                      'capture_id': item['capture_id'], 'foil_is_image': '1' if item['foil_is_image'] else '0'})
        except FileNotFoundError:
            logger.error(f"{item['image'].name} is gone - nothing to send")
            return 'refused'  # settled in the only way left: its note is removed
        except (requests.RequestException, OSError) as e:
            logger.warning(f"Could not send {item['image'].name} ({type(e).__name__}) - trying again")
            return 'retry'
        verdict = upload_verdict(response)
        if verdict == 'refused':
            logger.error(f"The server refused {item['image'].name}: {response.text[:200]} - the picture is kept")
        elif verdict == 'retry':
            logger.warning(f"Unexpected answer {response.status_code} for {item['image'].name} - trying again")
        return verdict


def upload_verdict(response):
    """
    What the server's answer to an uploaded capture means for the picture here:
      'accepted'  the server has the capture on record (its answer names the capture's number):
                  only then is the picture deleted
      'refused'   the server answered that it will never take it (400 unreadable, 413 too large)
      'retry'     anything else - an error, a wrong token (401: it is tried again until the token
                  is put right), a proxy's page, an answer that is not the server's
    An answer below 500 used to count as "the server has it", whatever it said.
    """
    try:
        answer = response.json()
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        return 'retry'
    if response.status_code in (200, 202) and isinstance(answer.get('capture'), int):
        return 'accepted'
    if response.status_code in (400, 413) and answer.get('success') is False:
        return 'refused'
    return 'retry'


def main():
    # In a container the machine's name is the one mounted from the host (deploy/docker-compose.client.yml)
    host_file = Path('/etc/host_hostname')
    host = host_file.read_text().strip() if host_file.is_file() else socket.gethostname()
    host = re.sub(r'[^A-Za-z0-9_-]', '-', host)[:40] or 'station'
    # No defaults here: a value that is not given is looked for in the environment, then in
    # what the setup page saved (station_setup.resolve) - and the page must know which of
    # them were given here, to show those as fixed
    parser = argparse.ArgumentParser(description="Camera station for the card scanner server")
    parser.add_argument('--server', help="e.g. http://192.168.1.20:5000 (or SCANNER_SERVER, or the setup page)")
    parser.add_argument('--id', help="1-40 letters, digits, - or _ (default: this machine's name)")
    parser.add_argument('--name', help="shown on the server (default: the id)")
    parser.add_argument('--token', help="the server's station token, if it has one")
    parser.add_argument('--setup-port', type=int, default=int(os.getenv('SCANNER_SETUP_PORT') or station_setup.DEFAULT_PORT),
                        help=f"port of the setup page (default {station_setup.DEFAULT_PORT}; 0 = no setup page)")
    args = parser.parse_args()

    # Stopping. The station ends on Ctrl+C: Python turns that signal (SIGINT) into a
    # KeyboardInterrupt, which the loops below catch to let go of the camera first. But
    # Python only does so if nobody told the process to ignore the signal - and a shell that
    # starts a program in the background (`station_client.py &` in scripts/start.sh) does
    # exactly that, after which `kill -INT` did nothing and the station could not be stopped
    # short of killing it. So the signal is taken back here, explicitly. SIGTERM - what
    # systemd and `docker stop` send by default - ends the station the same orderly way.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, lambda number, frame: signal.default_int_handler(signal.SIGINT, frame))

    Config.create_directories()  # data/ must exist before the saved settings are read
    values, fixed = station_setup.resolve({'server': args.server, 'id': args.id, 'name': args.name, 'token': args.token},
                                          station_setup.load(), host)
    if not re.fullmatch(station_setup.ID_PATTERN, values['id']):
        parser.error("the station's id must be 1-40 letters, digits, - or _")
    station_setup.apply_camera(values, fixed)  # before a CardScanner exists: it opens the camera

    # Without a server address there is nothing to connect to yet: only the setup page runs
    station = Station(values['server'], values['id'], values['name'], values['token']) if values['server'] else None
    if station:
        station.setup_port = args.setup_port

    # A restart is asked for from the setup page's thread, but must happen in this main
    # thread (it replaces the whole program): the page only raises a flag, and the loops
    # below notice it
    restart = threading.Event()

    def request_restart():
        restart.set()
        if station:
            station.stopping = True

    if args.setup_port:
        if station_setup.SetupPage(station, values, fixed, request_restart, args.setup_port).start():
            logger.info(f"Setup page: http://<this machine's address>:{args.setup_port}")
    if station is None:
        if not args.setup_port:
            parser.error("--server (or SCANNER_SERVER) is needed, e.g. http://192.168.1.20:5000")
        logger.info("No server address yet - open the setup page to set this station up")
        try:
            restart.wait()  # until the page has saved settings
        except KeyboardInterrupt:
            return
    else:
        try:
            station.run()  # until Ctrl+C, or a restart is asked for
        except KeyboardInterrupt:
            pass  # stopped while still looking for the server: nothing was opened yet

    if restart.is_set():
        # Start afresh with the saved settings. The camera is released by now (run() cleans
        # up when it ends). os.execv replaces this program by a new copy of itself with the
        # same command line - in a container that keeps the container running, where simply
        # ending would rely on Docker to start it again.
        # Captures that are still waiting are not lost by this: each has its note on disk,
        # and the new copy sends them (Station.load_pending).
        if station:
            station.wait_for_upload(10)  # one that is on its way right now may finish
        time.sleep(0.5)  # let the page's "saved" answer go out first
        logger.info("Restarting")
        os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    main()
