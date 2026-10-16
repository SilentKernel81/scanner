# ============================================================================
# FILE: server/ai_events.py
# The vision AI's settings: provider and model, credentials, prompts.
# ============================================================================
from flask import jsonify, request
from flask_socketio import emit
import time

from card_scanner.core.api_keys import credential_status, save_credential
from card_scanner.config import Config
from card_scanner import games
from card_scanner.core import prompts

from card_scanner.server.state import app, identification, socketio
from card_scanner.server.logs import logger
from card_scanner.server.lock import guarded_event


@app.route('/api/ai_credentials')
def get_ai_credentials():
    """Per provider: whether an API key is set (masked, never the full key) / the local endpoint"""
    return jsonify(credential_status())


@app.route('/api/ai_models')
def get_ai_models():
    """Get available models for all providers"""
    from card_scanner.core.card_identifier import CardIdentifier
    return jsonify({
        'models': CardIdentifier.AVAILABLE_MODELS
    })


@app.route('/api/ai_models/<provider>')
def get_provider_models(provider):
    """Get available models for a specific provider"""
    from card_scanner.core.card_identifier import CardIdentifier
    provider = provider.lower()

    if provider in CardIdentifier.AVAILABLE_MODELS:
        return jsonify({
            'provider': provider,
            'models': CardIdentifier.AVAILABLE_MODELS[provider]
        })
    else:
        return jsonify({
            'error': f'Unknown provider: {provider}'
        }), 404


@app.route('/api/local_ai_models')
def get_local_ai_models():
    """Fetch live models from local Ollama server"""
    import requests

    ollama_base = Config.LOCAL_AI_ENDPOINT.replace('/v1/chat/completions', '')
    try:
        ollama_api = f"{ollama_base}/api/tags"

        logger.info(f"Fetching models from Ollama: {ollama_api}")

        # Query Ollama for available models
        response = requests.get(ollama_api, timeout=5, allow_redirects=False)
        response.raise_for_status()

        data = response.json()

        # Extract model names from Ollama response
        # Ollama returns: {"models": [{"name": "llava:7b", ...}, ...]}
        models = []
        if 'models' in data:
            for model in data['models']:
                if 'name' in model:
                    models.append(model['name'])

        logger.info(f"Found {len(models)} local models: {models}")

        return jsonify({
            'success': True,
            'models': models,
            'endpoint': ollama_base
        })

    except requests.exceptions.ConnectionError:
        logger.warning(f"Could not connect to local AI server at {ollama_base}")
        return jsonify({
            'success': False,
            'error': 'Could not connect to local AI server',
            'models': []
        })
    except requests.exceptions.Timeout:
        logger.warning("Local AI server request timed out")
        return jsonify({
            'success': False,
            'error': 'Request timed out',
            'models': []
        })
    except Exception as e:
        # The details go to the log, not to whoever asked: this route fetches an address the settings
        # name, and an error message would tell a caller what is behind it
        logger.error(f"Error fetching local models: {e}")
        return jsonify({
            'success': False,
            'error': 'Could not get the models from the local AI server',
            'models': []
        })


@socketio.on('save_ai_credential')
@guarded_event
def handle_save_ai_credential(data):
    """Save a provider's API key (or the local endpoint) entered in Settings"""
    provider = (data.get('provider') or '').lower()
    try:
        save_credential(provider, data.get('value', ''))
    except (ValueError, OSError) as e:
        emit('error', {'message': f'Could not save: {e}'})
        return
    logger.info(f"Credential for {provider} {'saved' if data.get('value') else 'removed'} from the web interface")
    emit('ai_credential_saved', {'provider': provider, 'status': credential_status()[provider]})


def active_ai():
    """(provider, model) the scanner identifies cards with - model is None without an identifier"""
    if identification and identification.card_identifier:
        return identification.card_identifier.provider, identification.card_identifier.model
    return Config.VISION_AI_PROVIDER, None


def prompts_payload():
    provider, model = active_ai()
    status = prompts.status(provider, model, games.active_id())
    # 'order' keeps the editor's tabs in order (jsonify sorts the keys)
    return {'provider': provider, 'model': model, 'prompts': status, 'order': list(status)}


@app.route('/api/prompts')
def get_prompts():
    """Prompt instructions in effect for the active AI model, and where they come from"""
    return jsonify(prompts_payload())


@socketio.on('save_prompt')
def handle_save_prompt(data):
    """Save edited prompt instructions for the active model (scope 'model') or all models"""
    provider, model = active_ai()
    kind = data.get('kind')
    for_model = data.get('scope') == 'model'
    if for_model and not model:
        emit('error', {'message': 'No AI model is active - save the prompt for all models'})
        return
    try:
        prompts.save(kind, data.get('text'), provider if for_model else None, model if for_model else None,
                     games.active_id())
    except ValueError as e:
        emit('error', {'message': str(e)})
        return
    target = f'{provider} / {model}' if for_model else 'all models'
    emit('prompts_updated', {**prompts_payload(), 'message': f'Prompt saved for {target}'})


@socketio.on('reset_prompt')
def handle_reset_prompt(data):
    """Remove the saved prompt in effect (this model's, else the all-models one)"""
    provider, model = active_ai()
    try:
        removed = prompts.reset(data.get('kind'), provider, model, games.active_id())
    except ValueError as e:
        emit('error', {'message': str(e)})
        return
    message = {'model': f'Prompt for {provider} / {model} removed',
               'all': 'Prompt for all models removed'}.get(removed, 'Already using the built-in prompt')
    emit('prompts_updated', {**prompts_payload(), 'message': message})


@socketio.on('test_prompt')
def handle_test_prompt(data):
    """
    Run the AI on the last captured card with the prompt text from the editor (not saved)
    and report the raw answer and how it would be matched.
    """
    kind = data.get('kind')
    text = (data.get('text') or '').strip()
    sid = request.sid

    def reply(**result):
        socketio.emit('prompt_test_result', {'kind': kind, **result}, to=sid)

    if not identification or not identification.card_identifier:
        reply(error='Vision AI is not configured')
        return
    if not identification.last_capture:
        reply(error='No card captured yet - capture a card first')
        return
    if not text:
        reply(error='The prompt is empty')
        return
    image, foil_image = identification.last_capture
    if kind == 'foil' and foil_image is None:
        reply(error='The last capture has no detected card outline, so the foil corner cannot be located')
        return

    def run():
        identifier = identification.card_identifier
        start = time.time()
        try:
            if kind == 'foil':
                raw, foil = identifier.read_foil_symbol_verbose(foil_image, text)
                reply(raw=raw, result={'foil': foil}, seconds=round(time.time() - start, 2))
                return
            raw, card = identifier.identify_card_verbose(image, text)
            seconds = round(time.time() - start, 2)
            match = None
            if card:
                game = games.active()
                found = game.identify(card['name'], card.get('collector_number'), card.get('set_code'))
                if found:
                    match = {'name': found['name'], 'set': (found.get('set_code') or '').upper(),
                             'set_name': found.get('set'), 'number': found.get('number'), 'match': found.get('match'),
                             'confirmed': game.is_confirmed(found)}
                card = {k: card.get(k, '') for k in ('name', 'collector_number', 'set_code')}
            reply(raw=raw, result=card, match=match, seconds=seconds)
        except Exception as e:
            logger.exception(f"Prompt test failed: {e}")
            reply(error=str(e))

    socketio.start_background_task(run)
