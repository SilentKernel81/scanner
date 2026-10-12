# ============================================================================
# FILE: config.py
# Configuration of the server and of a camera station: config.yaml, with a few
# values that the environment can set instead (Docker): SCANNER_CAMERA,
# SCANNER_CAMERA_INDEX, SCANNER_STATION_TOKEN, VISION_AI_PROVIDER, LOCAL_AI_*.
# A station only uses the camera, detection and auto-capture values.
# ============================================================================
import os
from pathlib import Path
from config_loader import get_config_loader

# Load YAML configuration
config = get_config_loader()


def camera_index_from(value, source="SCANNER_CAMERA_INDEX"):
    """
    A camera chosen as text - a number ("2"), or a device path such as
    /dev/v4l/by-id/usb-...-video-index0 (the same camera whatever number it got this time) -
    as the N of /dev/videoN. `source` only names where the text came from, for the error.
    """
    value = str(value).strip()
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
        return config.get('camera', 'usb_index', default=0)
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
    BASE_DIR = Path(__file__).parent
    DATA_DIR = BASE_DIR / 'data'
    IMAGES_DIR = BASE_DIR / 'scanned_cards'
    TEMPLATES_DIR = BASE_DIR / 'templates'
    STATIC_DIR = BASE_DIR / 'static'

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
    SECRET_KEY = config.get('flask', 'secret_key', default='card_scanner_secret_key_change_in_production')
    HOST = config.get('flask', 'host', default='0.0.0.0')
    PORT = config.get('flask', 'port', default=5000)
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
    LOCAL_AI_ENDPOINT = os.getenv('LOCAL_AI_ENDPOINT') or config.get('vision_ai', 'local', 'endpoint', default='http://192.168.1.60:11434/v1/chat/completions')
    LOCAL_AI_MODEL = os.getenv('LOCAL_AI_MODEL') or config.get('vision_ai', 'local', 'model', default='llava:13b')

    # Cleanup settings
    CLEANUP_ENABLED = config.get('cleanup', 'enabled', default=True)
    CLEANUP_DAYS = config.get('cleanup', 'days', default=7)

    @staticmethod
    def create_directories():
        """Create required directories if they don't exist"""
        Config.DATA_DIR.mkdir(exist_ok=True)
        Config.IMAGES_DIR.mkdir(exist_ok=True)
