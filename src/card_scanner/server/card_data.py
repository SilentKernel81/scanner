# ============================================================================
# FILE: server/card_data.py
# Card data: updating and rebuilding the card database from the web interface.
# ============================================================================
from flask_socketio import emit
import threading
import time

from card_scanner import games

from card_scanner.server.state import database, socketio
from card_scanner.server.logs import logger


# Newer card data found by the update check: game id -> message (shown on the page)
data_update_notices = {}
data_updates_running = set()


def start_card_data_update(game):
    """Download a game's card data in the background (progress/complete/error events)"""
    if game.id in data_updates_running:
        return False
    data_updates_running.add(game.id)

    def progress_callback(message):
        socketio.emit('database_update_progress', {'message': message})
        logger.info(f"Database update: {message}")

    def update_task():
        try:
            logger.info(f"Updating {game.label} card data from {game.source}")
            game.download(progress_callback)
            total = game.card_count()
            data_update_notices.pop(game.id, None)
            socketio.emit('database_update_complete', {'game': game.id, 'total_cards': total})
            logger.info(f"{game.label} card data updated: {total} cards")
        except Exception as e:
            logger.exception(f"Database update failed: {e}")
            socketio.emit('database_update_error', {'game': game.id, 'message': str(e)})
        finally:
            data_updates_running.discard(game.id)

    threading.Thread(target=update_task, daemon=True).start()
    return True


def check_card_data_updates():
    """Ask each game's source whether newer card data exists (games with data only)"""
    for game in games.all_games():
        try:
            if not game.card_count() or game.id in data_updates_running:
                continue
            message = game.check_for_update()
        except Exception as e:
            logger.warning(f"{game.label} update check failed: {e}")
            continue
        if message:
            data_update_notices[game.id] = message
            logger.info(f"{game.label} card data update available: {message}")
            socketio.emit('database_update_available', {'game': game.id, 'label': game.label, 'message': message})
        else:
            data_update_notices.pop(game.id, None)


def run_update_checks():
    """Check for newer card data shortly after startup, then once a day"""
    def loop():
        time.sleep(10)
        while True:
            check_card_data_updates()
            time.sleep(24 * 3600)
    threading.Thread(target=loop, daemon=True, name='update-check').start()


@socketio.on('update_database')
def handle_update_database():
    """Download the active game's card data again"""
    logger.info("Database update request received")
    if not database:
        logger.error("Database update requested but database not initialized")
        emit('error', {'message': 'Database not initialized'})
        return
    if not start_card_data_update(games.active()):
        emit('error', {'message': 'An update of this card data is already running'})


@socketio.on('rebuild_database')
def handle_rebuild_database():
    """Handle database schema rebuild request"""

    logger.info("Database rebuild request received")

    if not database:
        logger.error("Database rebuild requested but database not initialized")
        emit('error', {'message': 'Database not initialized'})
        return

    def progress_callback(message):
        """Send progress updates to the client"""
        socketio.emit('database_rebuild_progress', {'message': message})
        logger.info(f"Database rebuild: {message}")

    def rebuild_task():
        """Run database rebuild in background thread"""
        try:
            logger.info("Starting database rebuild in background thread")

            # Rebuild the database schema
            result = database.rebuild_database_schema(progress_callback)

            if result['success']:
                # Send completion event
                socketio.emit('database_rebuild_complete', {
                    'cards_imported': result['cards_imported'],
                    'schema_type': result['schema_type']
                })

                logger.info(f"Database rebuild complete: {result['cards_imported']} cards, schema type: {result['schema_type']}")
            else:
                socketio.emit('database_rebuild_error', {
                    'message': result.get('error', 'Unknown error')
                })
                logger.error(f"Database rebuild failed: {result.get('error')}")

        except Exception as e:
            logger.exception(f"Database rebuild failed: {e}")
            socketio.emit('database_rebuild_error', {
                'message': str(e)
            })

    # Run rebuild in background thread
    rebuild_thread = threading.Thread(target=rebuild_task, daemon=True)
    rebuild_thread.start()

    logger.info("Database rebuild thread started")
