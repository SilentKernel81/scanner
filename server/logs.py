# ============================================================================
# FILE: server/logs.py
# Log files of the server (data/logs/): what goes where, and the request log of debug mode.
# ============================================================================
import logging
from logging.handlers import RotatingFileHandler

from server.state import ROOT


def setup_logging():
    """Configure logging with separate log files for different components"""
    # Create logs directory
    log_dir = ROOT / 'data' / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)

    # Create formatters
    detailed_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_formatter = logging.Formatter(
        '%(levelname)s: %(message)s'
    )

    # Console handler - WARNING and above only
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(console_formatter)

    # Configure root logger (catches everything not specifically handled)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(console_handler)

    # Silence Werkzeug (Flask development server) logging
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    # Silence SocketIO/EngineIO logging
    logging.getLogger('socketio').setLevel(logging.WARNING)
    logging.getLogger('engineio').setLevel(logging.WARNING)

    # ========================================================================
    # APP LOGGER - Flask application, routes, general events
    # ========================================================================
    app_logger = logging.getLogger('card_scanner')
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False  # Don't send to root logger

    app_file_handler = RotatingFileHandler(
        log_dir / 'app.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    app_file_handler.setLevel(logging.INFO)
    app_file_handler.setFormatter(detailed_formatter)
    app_logger.addHandler(app_file_handler)
    app_logger.addHandler(console_handler)

    # ========================================================================
    # AI LOGGER - Vision AI identification, model changes, API calls
    # ========================================================================
    ai_logger = logging.getLogger('ai')
    ai_logger.setLevel(logging.INFO)
    ai_logger.propagate = False

    ai_file_handler = RotatingFileHandler(
        log_dir / 'ai.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    ai_file_handler.setLevel(logging.INFO)
    ai_file_handler.setFormatter(detailed_formatter)
    ai_logger.addHandler(ai_file_handler)
    ai_logger.addHandler(console_handler)

    # ========================================================================
    # SCANNER LOGGER - Camera/scanner operations, detection events
    # ========================================================================
    scanner_logger = logging.getLogger('scanner')
    scanner_logger.setLevel(logging.INFO)
    scanner_logger.propagate = False

    scanner_file_handler = RotatingFileHandler(
        log_dir / 'scanner.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    scanner_file_handler.setLevel(logging.INFO)
    scanner_file_handler.setFormatter(detailed_formatter)
    scanner_logger.addHandler(scanner_file_handler)
    scanner_logger.addHandler(console_handler)

    # ========================================================================
    # DATABASE LOGGER - Database queries, inventory operations
    # ========================================================================
    db_logger = logging.getLogger('database')
    db_logger.setLevel(logging.INFO)
    db_logger.propagate = False

    db_file_handler = RotatingFileHandler(
        log_dir / 'database.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    db_file_handler.setLevel(logging.INFO)
    db_file_handler.setFormatter(detailed_formatter)
    db_logger.addHandler(db_file_handler)
    db_logger.addHandler(console_handler)

    # Scanned Cards Logger (CSV-like format for easy parsing)
    scanned_cards_logger = logging.getLogger('scanned_cards')
    scanned_cards_logger.setLevel(logging.INFO)
    scanned_cards_logger.propagate = False

    # Custom format for scanned cards log (CSV-like)
    scanned_cards_formatter = logging.Formatter('%(asctime)s,%(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    scanned_cards_file_handler = RotatingFileHandler(
        log_dir / 'scanned_cards.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    scanned_cards_file_handler.setLevel(logging.INFO)
    scanned_cards_file_handler.setFormatter(scanned_cards_formatter)
    scanned_cards_logger.addHandler(scanned_cards_file_handler)

    # Write CSV header if file is new/empty
    scanned_cards_log_path = log_dir / 'scanned_cards.log'
    if not scanned_cards_log_path.exists() or scanned_cards_log_path.stat().st_size == 0:
        with open(scanned_cards_log_path, 'w') as f:
            f.write('# Scanned Cards Log - CSV Format\n')
            f.write('# Columns: Timestamp,Card Name,Collector Number,AI Model,DB Found,Added To Inventory,Processing Time\n')
            f.write('Timestamp,Card Name,Collector Number,AI Model,DB Found,Added To Inventory,Processing Time\n')

    # Log initialization
    app_logger.info("="*80)
    app_logger.info("Logging initialized - Multi-file configuration")
    app_logger.info(f"Log directory: {log_dir}")
    app_logger.info("Log files: app.log, ai.log, scanner.log, database.log, scanned_cards.log")
    app_logger.info("="*80)

    return app_logger
logger = setup_logging()


def enable_request_log():
    """
    Debug mode: every request the web server answers goes to data/logs/requests.log
    (setup_logging silences Werkzeug, and its console handler shows warnings only - the page
    asks for the detection status several times a second, too much for the console)
    """
    handler = RotatingFileHandler(ROOT / 'data' / 'logs' / 'requests.log',
                                  maxBytes=10*1024*1024, backupCount=2)
    handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    requests_logger = logging.getLogger('werkzeug')
    requests_logger.setLevel(logging.INFO)
    requests_logger.addHandler(handler)


# Get scanned cards logger for tracking all scanned cards
scanned_cards_logger = logging.getLogger('scanned_cards')
