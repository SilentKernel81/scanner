# ============================================================================
# FILE: server/backups_api.py
# Backups: the schedule, create / restore / upload / download, full backups.
# ============================================================================
from flask import abort, jsonify, request, send_file
import tempfile
import threading
import time

from card_scanner.core import backups
from card_scanner.config import Config
from card_scanner import games
from card_scanner.core.pending import PENDING_FILE
from card_scanner.core.review import REVIEW_DIR

from card_scanner.server.state import (
    ARENA_INVENTORY_FILE, SCAN_INVENTORY_FILE, app, app_settings, arena_inventory, database, deck_store, inventory,
    maintenance_barrier,
    pending, review, scan_inventory, socketio,
)
from card_scanner.server.logs import logger
from card_scanner.server.desks import log_to_client


def backup_schedule():
    """The automatic backups' settings: {'every_hours' (0: off), 'keep'}"""
    try:
        every, keep = int(app_settings.get('backup_every_hours', backups.EVERY_HOURS)), int(app_settings.get('backup_keep', backups.KEEP_DAILY))
    except (TypeError, ValueError):  # data/settings.json edited by hand
        every, keep = backups.EVERY_HOURS, backups.KEEP_DAILY
    return {'every_hours': every if every in backups.SCHEDULE_HOURS else backups.EVERY_HOURS,
            'keep': max(1, min(backups.KEEP_MAX, keep))}


def scheduled_backup(note='Automatic'):
    """Make the automatic backup if one is due (backups.create_scheduled); its info, or None"""
    schedule = backup_schedule()
    return backups.create_scheduled(inventory, scan_inventory, deck_store, schedule['every_hours'],
                                    schedule['keep'], note=note, arena_inventory=arena_inventory)


def run_backup_schedule():
    """Ask every ten minutes whether an automatic backup is due - the app runs for weeks on a server"""
    def loop():
        while True:
            time.sleep(600)
            try:
                scheduled_backup()
            except Exception as e:
                logger.error(f"The automatic backup failed: {e}", exc_info=True)
    threading.Thread(target=loop, daemon=True, name='backup-schedule').start()


@app.route('/api/backups/schedule', methods=['POST'])
def set_backup_schedule():
    """How often automatic backups are made and how many are kept (JSON: 'every_hours', 'keep')"""
    data = request.get_json(silent=True) or {}
    try:
        changes = backups.parse_schedule(data)  # all of it checked before any of it is saved
    except ValueError:
        return jsonify({'success': False, 'error': 'Unknown backup interval or number to keep'}), 400
    for key, value in changes.items():
        app_settings.set(key, value)
    logger.info(f"Automatic backups: {backup_schedule()}")
    return jsonify({'success': True, 'schedule': backup_schedule()})


def inventory_captures_dir():
    from card_scanner.core import inventory as inventory_module  # read when asked: the tests point it elsewhere
    return inventory_module.CAPTURES_DIR


def full_backup():
    """Everything the server keeps, at one moment (backups.create_full): the manifest"""
    pictures = 'SELECT file FROM inventory_captures'
    return backups.create_full(
        managers=[inventory, scan_inventory, arena_inventory, deck_store, review, pending, database],  # the order take_from locks in
        barrier=maintenance_barrier,
        databases=[Config.DATABASE_FILE, SCAN_INVENTORY_FILE, ARENA_INVENTORY_FILE, PENDING_FILE],
        linked=[('captures', inventory_captures_dir(), Config.DATABASE_FILE, pictures),
                ('captures', inventory_captures_dir(), SCAN_INVENTORY_FILE, pictures),
                # The large copies: none for the captures made before they were kept
                ('captures_full', backups.full_captures_dir(), Config.DATABASE_FILE, pictures, True),
                ('captures_full', backups.full_captures_dir(), SCAN_INVENTORY_FILE, pictures, True),
                ('review', REVIEW_DIR, Config.DATABASE_FILE, 'SELECT file FROM review_queue'),
                ('scanned_cards', Config.IMAGES_DIR, PENDING_FILE, 'SELECT image FROM pending_captures'),
                ('scanned_cards', Config.IMAGES_DIR, PENDING_FILE, 'SELECT foil_image FROM pending_captures')],
        copied=[Config.DATA_DIR / name for name in ('settings.json', 'stations.json', 'prompts.json', 'api_keys.env')])


@app.route('/api/backups/full', methods=['GET', 'POST'])
def full_backups():
    """
    The full backups (GET), or make one (POST): every database file and the pictures their rows
    point at, taken at one moment, in data/backups/full/<date_time>/ - what scripts/backup.sh
    archives while the server runs. Not restored from the web interface (docs/INSTALL.md).
    """
    if request.method == 'POST':
        try:
            made = full_backup()
        except Exception as e:
            logger.error(f"The full backup failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': f'The full backup failed: {e}'}), 500
        return jsonify({'success': True, 'backup': made})
    return jsonify({'success': True, 'backups': backups.list_full()})


@app.route('/api/backups', methods=['GET', 'POST'])
def collection_backups():
    """The backups of the collection, scanned cards and decks; POST makes one (JSON: note)"""
    if request.method == 'POST':
        note = str((request.get_json(silent=True) or {}).get('note') or '')
        try:
            made = backups.create(inventory, scan_inventory, deck_store, note, arena_inventory=arena_inventory)
        except Exception as e:
            logger.error(f"Backup failed: {e}", exc_info=True)
            return jsonify({'success': False, 'error': f'The backup failed: {e}'}), 500
        return jsonify({'success': True, 'backup': made, 'backups': backups.list_backups()})
    return jsonify({'success': True, 'backups': backups.list_backups(), 'schedule': backup_schedule()})


@app.route('/api/backups/<backup_id>/restore', methods=['POST'])
def restore_backup(backup_id):
    """Put the collection, scanned cards and decks back as in a backup (the current state is backed up first)"""
    try:
        # The barrier first, as a capture being settled takes it before the inventories' locks
        with maintenance_barrier:
            result = backups.restore(backup_id, inventory, scan_inventory, deck_store, arena_inventory=arena_inventory)
    except backups.BackupError as e:
        logger.error(f"Restoring backup {backup_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Restoring backup {backup_id} failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': f'The restore failed: {e}'}), 500
    log_to_client(f"Backup of {result['restored']['created']} restored", level="success")
    # Every open page shows the restored cards
    socketio.emit('inventory_updated', {'auto': False, 'stats': scan_inventory.get_stats(games.active().id)}, namespace='/')
    socketio.emit('arena_updated')   # ... and the restored digital collection
    return jsonify({'success': True, **result, 'backups': backups.list_backups()})


BACKUP_UPLOAD_LIMIT = 8 * 1024 ** 3   # a backup with its captures can be gigabytes


@app.before_request
def limit_the_request_body():
    """
    The server's request limit (state.py) - 64 MB, for pictures and collection files - is applied
    before any route runs (Flask only checks when a route reads the body, and several answer
    "not ready" first); a backup, which holds the captures, is the one upload allowed to be bigger.
    """
    if request.endpoint == 'upload_backup':
        request.max_content_length = BACKUP_UPLOAD_LIMIT
    limit = request.max_content_length
    if limit is not None and request.content_length is not None and request.content_length > limit:
        abort(413)


@app.route('/api/backups/upload', methods=['POST'])
def upload_backup():
    """Add a downloaded backup to the list again (multipart: file) - it is not restored by this"""
    file = request.files.get('file')
    if not file:
        return jsonify({'success': False, 'error': 'No file uploaded'}), 400
    try:
        added = backups.add_archive(file.stream)
    except backups.BackupError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Taking in an uploaded backup failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': f'The backup could not be added: {e}'}), 500
    return jsonify({'success': True, 'backup': added, 'backups': backups.list_backups()})


@app.route('/api/backups/<backup_id>/download')
def download_backup(backup_id):
    """A backup as one zip file (its folder of data/backups/, to keep somewhere else)"""
    packed = tempfile.TemporaryFile()  # on disk: a backup with its captures is ~100 MB
    try:
        name = backups.archive(backup_id, packed)
    except backups.BackupError as e:
        packed.close()
        return jsonify({'success': False, 'error': str(e)}), 404
    except Exception as e:
        packed.close()
        logger.error(f"Packing backup {backup_id} failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': f'The backup could not be packed: {e}'}), 500
    packed.seek(0)
    logger.info(f"Backup {backup_id} downloaded")
    return send_file(packed, mimetype='application/zip', as_attachment=True, download_name=name)


@app.route('/api/backups/<backup_id>', methods=['DELETE'])
def delete_backup(backup_id):
    try:
        backups.delete(backup_id)
    except backups.BackupError as e:
        return jsonify({'success': False, 'error': str(e)}), 404
    return jsonify({'success': True, 'backups': backups.list_backups()})
