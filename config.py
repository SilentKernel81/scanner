# ============================================================================
# FILE: config.py
# Configuration settings for the card scanner
# All settings are loaded from config.yaml
# ============================================================================
import os
from pathlib import Path
from config_loader import get_config_loader

# Load YAML configuration
config = get_config_loader()


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

    # API URLs
    SCRYFALL_BULK_URL = config.get('api', 'scryfall_bulk', default="https://api.scryfall.com/bulk-data/default-cards")

    # Camera settings
    CAMERA_TYPE = config.get('camera', 'type', default='auto')
    USB_CAMERA_INDEX = config.get('camera', 'usb_index', default=0)
    CAMERA_RESOLUTION = tuple(config.get('camera', 'resolution', default=[2560, 1440]))
    CAMERA_PREVIEW_RESOLUTION = tuple(config.get('camera', 'preview_resolution', default=[640, 480]))
    CAMERA_FPS = config.get('camera', 'fps', default=20)

    # Object detection settings
    DETECTION_METHOD = config.get('detection', 'method', default='auto')
    DETECTION_ALLOW_LANDSCAPE = config.get('detection', 'allow_landscape', default=False)
    DETECTION_CONFIDENCE_THRESHOLD = config.get('detection', 'confidence_threshold', default=0.3)
    ASPECT_RATIO_TOLERANCE = config.get('detection', 'aspect_ratio_tolerance', default=0.15)

    # Card size filtering
    CARD_SIZE_FILTER_ENABLED = config.get('detection', 'card_size_filter', 'enabled', default=False)
    CARD_MIN_WIDTH = config.get('detection', 'card_size_filter', 'min_width', default=200)
    CARD_MAX_WIDTH = config.get('detection', 'card_size_filter', 'max_width', default=2500)
    CARD_MIN_HEIGHT = config.get('detection', 'card_size_filter', 'min_height', default=280)
    CARD_MAX_HEIGHT = config.get('detection', 'card_size_filter', 'max_height', default=1680)
    CARD_MIN_AREA = config.get('detection', 'card_size_filter', 'min_area', default=56000)
    CARD_MAX_AREA = config.get('detection', 'card_size_filter', 'max_area', default=4000000)

    # Anti-glare settings
    ANTI_GLARE_ENABLED = config.get('anti_glare', 'enabled', default=False)
    ANTI_GLARE_METHOD = config.get('anti_glare', 'method', default='adaptive')

    # Focus settings
    FOCUS_LOCK_ON_STABLE = config.get('focus', 'lock_on_stable', default=False)

    # Auto-capture settings (enabled state now controlled via UI button)
    AUTO_CAPTURE_DELAY = config.get('auto_capture', 'delay', default=4.0)
    AUTO_CAPTURE_STABILITY_FRAMES = config.get('auto_capture', 'stability_frames', default=5)
    AUTO_CAPTURE_WAIT_FOR_FOCUS = config.get('auto_capture', 'wait_for_focus', default=False)

    # Fast scan mode settings
    FAST_SCAN_STABILITY_FRAMES = config.get('fast_scan', 'stability_frames', default=2)

    # Flask settings
    SECRET_KEY = config.get('flask', 'secret_key', default='card_scanner_secret_key_change_in_production')
    HOST = config.get('flask', 'host', default='0.0.0.0')
    PORT = config.get('flask', 'port', default=5000)
    DEBUG = config.get('flask', 'debug', default=False)

    # Vision AI settings - Environment variables take precedence
    VISION_AI_PROVIDER = os.getenv('VISION_AI_PROVIDER') or config.get('vision_ai', 'provider', default='gemini')
    VISION_AI_ENABLED = config.get('vision_ai', 'enabled', default=True)

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
