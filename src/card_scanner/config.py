# ============================================================================
# FILE: config.py
# Configuration of the server and of a camera station: config.yaml, with a few
# values that the environment can set instead (Docker): SCANNER_CAMERA,
# SCANNER_CAMERA_INDEX, SCANNER_STATION_TOKEN, VISION_AI_PROVIDER, LOCAL_AI_*.
# A station only uses the camera, detection and auto-capture values.
# ============================================================================
import os
import re
from pathlib import Path
from card_scanner.config_loader import get_config_loader
from card_scanner.paths import TOP, USER_DIR

# Load YAML configuration
config = get_config_loader()


# A laptop's own camera looks at the person, not at a box of cards
LAPTOP_CAMERA = re.compile(r'integrated|built-?in|facetime|\bir\b', re.IGNORECASE)


def auto_camera_index(root='/sys/class/video4linux'):
    """
    The N of /dev/videoN for "auto": the first USB camera that is not a laptop's own, else the
    first USB camera; None when there is none. Read from /sys - nothing is opened - so it can
    be asked again at every attempt to open the camera (one plugged in later is found).
    """
    cameras = []
    nodes = sorted(Path(root).glob('video*'), key=lambda node: int(node.name[5:] or 0))
    for node in nodes:
        try:
            if '/usb' not in os.path.realpath(node / 'device'):
                continue  # a Pi's image processor and codecs are /dev/videoN too
            if (node / 'index').read_text().strip() != '0':
                continue  # a webcam's second node is data about the pictures
            cameras.append((int(node.name[5:]), (node / 'name').read_text().strip()))
        except (OSError, ValueError):
            continue
    outside = [index for index, name in cameras if not LAPTOP_CAMERA.search(name)]
    return (outside or [index for index, _ in cameras] or [None])[0]


def camera_index_from(value, source="SCANNER_CAMERA_INDEX"):
    """
    A camera chosen as text - a number ("2"), "auto" (auto_camera_index), or a device path
    such as /dev/v4l/by-id/usb-...-video-index0 (the same camera whatever number it got this
    time) - as the N of /dev/videoN. `source` only names where the text came from, for the error.
    """
    value = str(value).strip()
    if value.lower() == 'auto':
        found = auto_camera_index()
        if found is None:
            raise ValueError(f"{source}=auto: no USB camera found")
        return found
    if value.isdigit():
        return int(value)
    device = os.path.realpath(value)
    digits = device.removeprefix('/dev/video')
    if not digits.isdigit():
        raise ValueError(f"{source}={value}: not a number or a /dev/videoN device ({device})")
    return int(digits)


def configured_camera():
    """
    Which USB camera was asked for: camera.usb_index in config.yaml, or SCANNER_CAMERA_INDEX -
    a number, or a device path as text (see camera_index_from). A station's setup page can
    choose another one later (station_setup.apply_camera).
    """
    value = os.getenv('SCANNER_CAMERA_INDEX')
    if not value:
        return config.get('camera', 'usb_index', default='auto')
    return int(value) if value.isdigit() else value


def camera_index_or_none(source):
    """
    The N of /dev/videoN for a configured camera, or None when it is not there right now.

    A camera named by its /dev/v4l/by-id/... path has no number while it is unplugged: the
    path does not exist then. That used to be an error at the moment this file was imported,
    so a station whose camera was unplugged did not start at all - in Docker it restarted
    over and over. A missing camera is not an error (the scanner waits for it): it is None
    here, and looked up again at every attempt to open the camera (scanner._open_camera),
    which also finds a camera that came back under another number.
    """
    try:
        return camera_index_from(source)
    except ValueError:
        return None


class Config:
    """Application configuration - All values loaded from config.yaml"""

    # Paths
    BASE_DIR = TOP  # what ships with the program (paths.py) - not this file's folder
    USER_DIR = USER_DIR  # what is the person's: data/, scanned_cards/, .env, their config.yaml
    DATA_DIR = USER_DIR / 'data'
    IMAGES_DIR = USER_DIR / 'scanned_cards'  # the server's: every capture it received
    # A camera station's own: captures it has taken that are not on the server yet. Not the
    # folder above, also when both programs run from one checkout: the server deletes old
    # pictures from scanned_cards/ (cleanup.days), and must never reach these
    STATION_CAPTURES_DIR = USER_DIR / 'station_captures'

    # Database
    DATABASE_FILE = DATA_DIR / config.get('database', 'file', default='cards_database.db')
    DATABASE_UPDATE_AFTER_DAYS = config.get('database', 'update_after_days', default=7)

    # API URLs
    SCRYFALL_BULK_URL = config.get('api', 'scryfall_bulk', default="https://api.scryfall.com/bulk-data/default-cards")

    # Camera settings
    # A camera station's camera: auto / usb / picamera - also from the environment. The server
    # opens no camera and does not read this
    CAMERA_TYPE = os.getenv('SCANNER_CAMERA') or config.get('camera', 'type', default='auto')
    USB_CAMERA_SOURCE = configured_camera()  # as configured: a number, or a device path
    USB_CAMERA_INDEX = camera_index_or_none(USB_CAMERA_SOURCE)  # its /dev/videoN now; None = not plugged in
    CAMERA_RESOLUTION = tuple(config.get('camera', 'resolution', default=[2560, 1440]))
    CAMERA_PREVIEW_RESOLUTION = tuple(config.get('camera', 'preview_resolution', default=[640, 480]))
    CAMERA_FPS = config.get('camera', 'fps', default=20)
    CAMERA_ROTATE = int(config.get('camera', 'rotate', default=0))
    # Pi camera: the part of the sensor's image to read out (fractions x1, y1, x2, y2), or None
    CAMERA_CROP = config.get('camera', 'crop', default=None)

    # Object detection settings
    DETECTION_ALLOW_LANDSCAPE = config.get('detection', 'allow_landscape', default=False)

    # Focus settings

    # Auto-capture settings (enabled state now controlled via UI button)
    AUTO_CAPTURE_DELAY = config.get('auto_capture', 'delay', default=1.0)
    AUTO_CAPTURE_STABILITY_FRAMES = config.get('auto_capture', 'stability_frames', default=5)
    AUTO_CAPTURE_MIN_SHARPNESS = config.get('auto_capture', 'min_sharpness', default=250)
    AUTO_CAPTURE_REFOCUS_EVERY = int(config.get('auto_capture', 'refocus_every', default=10))

    # Fast scan mode settings
    FAST_SCAN_STABILITY_FRAMES = config.get('fast_scan', 'stability_frames', default=6)

    # Flask settings
    SECRET_KEY = config.get('flask', 'secret_key', default=None)  # None: data/secret_key (server/access.py)
    HOST = config.get('flask', 'host', default='0.0.0.0')
    PORT = config.get('flask', 'port', default=5000)
    ALLOWED_HOSTS = config.get('flask', 'allowed_hosts', default=None) or []  # server/access.py
    DEBUG = config.get('flask', 'debug', default=False)

    # Vision AI settings - Environment variables take precedence
    VISION_AI_PROVIDER = os.getenv('VISION_AI_PROVIDER') or config.get('vision_ai', 'provider', default='gemini')
    VISION_AI_ENABLED = config.get('vision_ai', 'enabled', default=True)
    # Ask the AI about the star (foil) / dot marker on outline-detected captures
    VISION_AI_DETECT_FOIL = config.get('vision_ai', 'detect_foil', default=True)
    VISION_AI_IMAGE_SIZE = int(config.get('vision_ai', 'image_size', default=1024))
    # AI requests running at the same time (identification.py)
    VISION_AI_WORKERS = int(config.get('vision_ai', 'workers', default=3))

    # Stations (stations.py): when set, captures are only accepted with this token
    STATION_TOKEN = os.getenv('SCANNER_STATION_TOKEN') or config.get('stations', 'token', default='') or ''

    # light-ocr reader (card_ocr.py): auto (GPU when available), cpu or webgpu
    OCR_PROVIDER = config.get('ocr', 'provider', default='auto')

    # Local AI settings
    LOCAL_AI_ENDPOINT = os.getenv('LOCAL_AI_ENDPOINT') or config.get('vision_ai', 'local', 'endpoint', default='http://localhost:11434/v1/chat/completions')
    LOCAL_AI_MODEL = os.getenv('LOCAL_AI_MODEL') or config.get('vision_ai', 'local', 'model', default='llava:13b')

    # Cleanup settings
    CLEANUP_ENABLED = config.get('cleanup', 'enabled', default=True)
    CLEANUP_DAYS = config.get('cleanup', 'days', default=7)

    @staticmethod
    def shown(*parts):
        """
        A file or folder in data/ as messages name it: data/logs/app.log in a checkout, the
        whole path (~/.local/share/...) when the data lives elsewhere
        """
        if Config.USER_DIR == Config.BASE_DIR:
            return '/'.join(('data',) + parts)
        path = str(Config.DATA_DIR.joinpath(*parts))
        home = os.path.expanduser('~')
        return '~' + path[len(home):] if path.startswith(home + os.sep) else path

    @staticmethod
    def create_directories():
        """Create required directories if they don't exist"""
        Config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        Config.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
