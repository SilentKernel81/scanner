# ============================================================================
# FILE: server/settings_events.py
# Settings of a camera and of scanning: toggles, the game, focus, rotation, fixed area, sound.
# ============================================================================
from flask import jsonify, request
from flask_socketio import emit

from card_scanner.core.card_ocr import CardOcr
from card_scanner.config import Config
from card_scanner import games

from card_scanner.server.state import app, app_settings, identification, inventory, services, socketio, stations
from card_scanner.server.logs import logger
from card_scanner.server.desks import desk, desks, emit_desk, in_desk, log_to_client, scanner
from card_scanner.server.captures import scan_location, set_pending_capture
from card_scanner.server.card_data import start_card_data_update


@app.route('/api/ai_provider')
def get_ai_provider():
    """Get current AI provider and model for card identification"""

    if identification and identification.card_identifier:
        return jsonify({
            'provider': identification.card_identifier.provider,
            'model': identification.card_identifier.model,
            'enabled': True
        })

    return jsonify({
        'provider': Config.VISION_AI_PROVIDER,
        'model': None,
        'enabled': Config.VISION_AI_ENABLED
    })


def game_info(game):
    """What the page needs to know about a game (lists keep their order; jsonify sorts dicts)"""
    return {
        'id': game.id,
        'label': game.label,
        'source': game.source,
        'has_treatments': game.has_treatments,
        'set_example': game.set_example,
        'number_example': game.number_example,
        'finishes': [[key, label] for key, label in game.finishes.items()],
        'exports': [[key, label] for key, (label, _prefix, _writer) in game.export_formats().items()],
        'imports': list(game.import_formats),
        # Deck builder formats: [[id, label, has a commander]] (none: the game has no deck builder)
        'deck_formats': [[key, rules['label'], bool(rules.get('commander'))]
                         for key, rules in game.deck_formats.items()],
        # Whether a deck can be written for the game's digital client (the deck page's Arena button)
        'arena': bool(game.arena_decklist),
    }


@app.route('/api/games')
def get_games():
    """Supported card games and the one being scanned"""
    return jsonify({'active': games.active_id(), 'games': [game_info(game) for game in games.all_games()]})


@socketio.on('set_game')
def handle_set_game(data):
    """Switch the game being scanned (stops auto scanning; the current card is dropped)"""
    game_id = data.get('game')
    try:
        games.set_active(game_id)
    except ValueError as e:
        emit('error', {'message': str(e)})
        return
    # The game is everyone's: every camera stops scanning and every page drops its card
    for target in [services.default_desk, *list(desks.values())]:
        with in_desk(target):
            target.current_card_info = target.current_review_id = target.review_sid = None
            set_pending_capture(None)
            if scanner and scanner.auto_capture_enabled:
                scanner.auto_capture_enabled = False
                scanner.card_under_review = False
                emit_desk('auto_capture_toggled', {'enabled': False})
    game = games.active()
    logger.info(f"Game switched to {game.label}")
    card_count = game.card_count()
    socketio.emit('game_changed', {**game_info(game), 'card_count': card_count})
    if not card_count:
        # First time: fetch its card data right away
        log_to_client(f"Downloading the {game.label} card data from {game.source}...")
        start_card_data_update(game, retry=True)  # no data yet: a failure is tried again


@socketio.on('toggle_auto_capture')
def handle_toggle_auto_capture(data):
    """Toggle auto-capture on/off"""

    if not scanner:
        logger.error("Toggle auto-capture requested but scanner not initialized")
        emit('error', {'message': 'Scanner not initialized'})
        return

    enabled = data.get('enabled', True)
    scanner.auto_capture_enabled = enabled

    # Load a local AI model now, so the first auto-captured card doesn't wait for it
    if enabled and identification.card_identifier:
        identification.card_identifier.warm_up()

    # When disabling, also clear the card_under_review flag to reset state
    if not enabled:
        scanner.card_under_review = False
        scanner.stable_frames = 0  # Reset stability counter
        logger.info("Auto-capture disabled - resetting scanner state")

    emit('auto_capture_toggled', {'enabled': enabled})
    logger.info(f"Auto-capture toggled: {enabled}")
    log_to_client(f"Auto-capture {('enabled' if enabled else 'disabled')}", level="info")


def set_auto_add(enabled):
    """
    Auto-add ("fast scan") mode: auto-captured cards are identified in the background and
    confirmed ones are added to the inventory without review; otherwise each card waits
    for Add / Skip.
    """
    scanner.fast_scan_mode = enabled
    scanner.required_stable_frames = Config.FAST_SCAN_STABILITY_FRAMES if enabled else Config.AUTO_CAPTURE_STABILITY_FRAMES
    scanner.auto_capture_delay = Config.AUTO_CAPTURE_DELAY


def saved_debug_mode():
    """Flask's debug mode for the next start: the switch in Settings, else flask.debug in config.yaml"""
    return bool(app_settings.get('debug_mode', Config.DEBUG)) if app_settings else bool(Config.DEBUG)


@app.route('/api/scan_settings')
def get_scan_settings():
    """Scanning preferences the page needs on load"""
    return jsonify({
        'auto_add': bool(scanner.fast_scan_mode) if scanner else True,
        'ocr_first': bool(identification.ocr_enabled) if identification else True,
        'ocr_installed': CardOcr.installed(),
        'debug_trace': bool(scanner.debug_trace_enabled) if scanner else False,
        'debug_mode': saved_debug_mode(),
        'debug_mode_running': bool(services.debug_mode_running),
        'autofocus': scanner.focus_locked_value is None if scanner else True,
        'fixed_area_enabled': bool(scanner.fixed_area_enabled) if scanner else False,
        'fixed_area': scanner.fixed_area if scanner else None,
        'camera_rotation': scanner.rotation if scanner else 0,
        'refocus_every': scanner.refocus_every if scanner else Config.AUTO_CAPTURE_REFOCUS_EVERY,
        'scan_location': scan_location(),
        'sound': sound_settings(),
        'locations': inventory.locations(games.active_id()) if inventory else [],
    })


def sound_settings():
    """
    Sound effects of the scanner page: {'enabled', 'volume' (0-100), 'capture', 'added'} - the
    last two switch off the capture beep and the ding of an added card, each on its own
    """
    settings = app_settings
    return {'enabled': bool(settings.get('sound_enabled', True)) if settings else True,
            'volume': int(settings.get('sound_volume', 30)) if settings else 30,
            'capture': bool(settings.get('sound_capture', True)) if settings else True,
            'added': bool(settings.get('sound_added', True)) if settings else True}


@app.route('/api/sound', methods=['POST'])
def set_sound():
    """Remember the sound switches and / or the volume (JSON: 'enabled', 'capture', 'added', 'volume' 0-100)"""
    data = request.get_json(silent=True) or {}
    try:
        for key in ('enabled', 'capture', 'added'):
            if key in data:
                app_settings.set(f'sound_{key}', bool(data[key]))
        if 'volume' in data:
            app_settings.set('sound_volume', max(0, min(100, int(data['volume']))))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'The volume must be a number from 0 to 100'}), 400
    return jsonify({'success': True, 'sound': sound_settings()})


@app.route('/api/scan_location', methods=['POST'])
def set_scan_location():
    """Set the inventory location scanned cards are added to ('' = none)"""
    location = str((request.get_json(silent=True) or {}).get('location') or '').strip()[:60]
    if desk().id:
        stations.update(desk().id, location=location)  # a station's cards go to its own location
    else:
        app_settings.set('scan_location', location)
    log_to_client(f"Scanned cards go to: {location}" if location else "Scanned cards get no location")
    return jsonify({'success': True, 'scan_location': location})


@socketio.on('toggle_fast_scan')
def handle_toggle_fast_scan(data):
    """Toggle adding auto-scanned cards to the inventory automatically (remembered)"""

    if not scanner:
        logger.error("Toggle auto-add requested but scanner not initialized")
        emit('error', {'message': 'Scanner not initialized'})
        return

    enabled = bool(data.get('enabled', False))
    set_auto_add(enabled)
    scanner.settings.set('auto_add', enabled)
    emit('fast_scan_toggled', {'enabled': enabled})
    logger.info(f"Auto-add toggled: {enabled} (required frames: {scanner.required_stable_frames})")
    log_to_client(f"Add cards automatically: {'on' if enabled else 'off - review each card'}", level="info")


@socketio.on('toggle_ocr')
def handle_toggle_ocr(data):
    """Read cards with light-ocr first, the vision AI only when needed (remembered)"""
    if not scanner:
        emit('error', {'message': 'Scanner not initialized'})
        return
    identification.set_ocr_enabled(bool(data.get('enabled', False)))
    emit('ocr_toggled', {'enabled': identification.ocr_enabled, 'installed': CardOcr.installed()})


@socketio.on('toggle_debug_trace')
def handle_toggle_debug_trace(data):
    """Toggle debug trace logging on/off (remembered)"""

    if not scanner:
        logger.error("Toggle debug trace requested but scanner not initialized")
        emit('error', {'message': 'Scanner not initialized'})
        return

    enabled = bool(data.get('enabled', False))
    scanner.debug_trace_enabled = enabled
    scanner.settings.set('debug_trace', enabled)
    emit('debug_trace_toggled', {'enabled': enabled})
    logger.info(f"Debug trace toggled: {enabled}")


@socketio.on('toggle_debug_mode')
def handle_toggle_debug_mode(data):
    """Flask's debug mode on/off (remembered; the server takes it when it starts)"""
    if not scanner:
        emit('error', {'message': 'Scanner not initialized'})
        return
    enabled = bool(data.get('enabled', False))
    app_settings.set('debug_mode', enabled)
    emit('debug_mode_toggled', {'enabled': enabled, 'running': bool(services.debug_mode_running)})
    logger.info(f"Debug mode toggled: {enabled} (running: {services.debug_mode_running})")
    if enabled != bool(services.debug_mode_running):
        log_to_client(f"Debug mode: {'on' if enabled else 'off'} after the next restart", level="info")


@socketio.on('reset_focus')
def handle_reset_focus():
    """Reset camera focus"""

    if not scanner:
        logger.error("Reset focus requested but scanner not initialized")
        emit('error', {'message': 'Scanner not initialized'})
        return

    logger.info("Focus reset requested by user")
    if scanner.reset_focus():
        # The sweep runs in the background; the result arrives as a log message
        emit('focus_reset', {'message': 'Focusing...'})
    elif scanner.focus_sweep_running:
        emit('focus_reset', {'message': 'Already focusing...'})
    else:
        emit('error', {'message': no_focus_message()})


def no_focus_message():
    return 'No camera connected' if scanner.camera_error else 'This camera has no manual focus control'


@socketio.on('set_autofocus')
def handle_set_autofocus(data):
    """Continuous autofocus on, or off = find the sharpest focus and lock it"""
    if not scanner:
        emit('error', {'message': 'Scanner not initialized'})
        return
    if not scanner.set_continuous_autofocus(bool(data.get('enabled'))):
        emit('error', {'message': no_focus_message()})


@socketio.on('set_camera_rotation')
def handle_set_camera_rotation(data):
    """Rotate the camera image (0/90/180/270 degrees clockwise) - e.g. a camera mounted sideways"""
    if not scanner:
        emit('error', {'message': 'Scanner not initialized'})
        return
    try:
        fixed_area_off = scanner.set_rotation(data.get('rotation', 0))
    except (TypeError, ValueError) as e:
        emit('error', {'message': str(e)})
        return
    emit_desk('camera_rotation_updated', {'rotation': scanner.rotation, 'fixed_area_off': fixed_area_off})
    if fixed_area_off:
        emit_desk('fixed_area_updated', {'enabled': False, 'area': scanner.fixed_area})


@socketio.on('set_refocus_every')
def handle_set_refocus_every(data):
    """Check the focus every this many captures while scanning (0 = never); remembered"""
    if not scanner:
        emit('error', {'message': 'Scanner not initialized'})
        return
    try:
        scanner.set_refocus_every(data.get('captures', 0))
    except (TypeError, ValueError) as e:
        emit('error', {'message': str(e)})
        emit('refocus_every_updated', {'captures': scanner.refocus_every})  # put the field back
        return
    emit_desk('refocus_every_updated', {'captures': scanner.refocus_every})


@socketio.on('set_fixed_area')
def handle_set_fixed_area(data):
    """
    Fixed capture area (sleeved cards): {'enabled': bool} turns it on/off, {'area': [x1, y1, x2, y2]}
    (fractions of the frame, drawn on the video) sets it, {'use_detected': true} takes the
    detected card plus a margin. Sent back to every page as fixed_area_updated.
    """
    if not scanner:
        emit('error', {'message': 'Scanner not initialized'})
        return
    area = data.get('area')
    if data.get('use_detected'):
        try:
            area = scanner.detected_area()
        except ValueError:
            area = None
        if area is None:
            emit('error', {'message': 'No card detected - put a card in the box, or draw the area'})
            return
    if area is not None:
        try:
            x1, y1, x2, y2 = (float(v) for v in area)
        except (TypeError, ValueError):
            emit('error', {'message': 'Invalid area'})
            return
        x1, y1, x2, y2 = max(0.0, x1), max(0.0, y1), min(1.0, x2), min(1.0, y2)
        if x2 - x1 < 0.05 or y2 - y1 < 0.05:
            emit('error', {'message': 'The area is too small - draw it around the card'})
            return
        area = [x1, y1, x2, y2]
    enabled = data.get('enabled')
    if enabled and area is None and scanner.fixed_area is None:
        emit('error', {'message': 'Draw the capture area first'})
        return
    try:
        state = scanner.set_fixed_area(enabled=enabled, area=area)
    except ValueError as e:  # the camera station did not answer
        emit('error', {'message': str(e)})
        return
    logger.info(f"Fixed area: {'on' if state['enabled'] else 'off'} {state['area']}")
    emit_desk('fixed_area_updated', state)


@socketio.on('set_ai_provider')
def handle_set_ai_provider(data):
    """Set AI provider and/or model for card identification"""

    if not scanner:
        logger.error("Set AI provider requested but scanner not initialized")
        emit('error', {'message': 'Scanner not initialized'})
        return

    provider = data.get('provider', 'gemini')
    model = data.get('model', None)  # Optional model parameter

    if model:
        logger.info(f"AI provider/model change requested: {provider} / {model}")
    else:
        logger.info(f"AI provider change requested: {provider}")

    result = identification.set_ai_provider(provider, model)

    if result['success']:
        emit('ai_provider_set', {
            'provider': provider,
            'model': result.get('model'),
            'message': result['message']
        })
        logger.info(f"AI provider set to: {provider} ({result.get('model')})")
    else:
        emit('error', {'message': result['message']})
        logger.error(f"Failed to set AI provider: {result['message']}")
