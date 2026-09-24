"""
API keys and the local AI endpoint, editable from the web interface.

Values entered in the UI are saved to data/api_keys.env (only readable by the owner) and
override .env - the systemd service can only write inside data/. They are applied to
os.environ immediately, so no restart is needed.
"""
import os
import re

from dotenv import load_dotenv

from config import Config
from config_loader import get_config_loader

KEYS_FILE = Config.DATA_DIR / 'api_keys.env'

# Provider -> environment variable holding its credential
PROVIDER_VARIABLES = {
    'gemini': 'GEMINI_API_KEY',
    'openai': 'OPENAI_API_KEY',
    'anthropic': 'ANTHROPIC_API_KEY',
    'local': 'LOCAL_AI_ENDPOINT',
}


def load_saved_keys():
    """Load keys saved from the UI (they take precedence over .env)"""
    if KEYS_FILE.exists():
        load_dotenv(KEYS_FILE, override=True)
        _apply_local_endpoint()


def _apply_local_endpoint():
    """Config.LOCAL_AI_ENDPOINT is read at import time - keep it in sync with the environment"""
    if os.getenv('LOCAL_AI_ENDPOINT'):
        Config.LOCAL_AI_ENDPOINT = os.getenv('LOCAL_AI_ENDPOINT')


def mask(value):
    """Show just enough of a key to recognize it: 'AIza…3f9Q'"""
    if not value:
        return ''
    if len(value) <= 12:
        return '•' * 8
    return f"{value[:4]}…{value[-4:]}"


def credential_status():
    """
    What the settings panel shows per provider - keys masked, never in full (the web
    interface has no login and is reachable from the local network).
    """
    status = {}
    for provider, variable in PROVIDER_VARIABLES.items():
        if provider == 'local':
            status[provider] = {'endpoint': Config.LOCAL_AI_ENDPOINT}
        else:
            value = os.getenv(variable, '')
            status[provider] = {'configured': bool(value), 'masked': mask(value)}
    return status


def save_credential(provider, value):
    """
    Save a provider's API key (or the local endpoint) to data/api_keys.env and apply it.
    An empty value removes the saved entry (a key in .env, if any, is still used after a restart).
    """
    variable = PROVIDER_VARIABLES.get(provider)
    if not variable:
        raise ValueError(f"Unknown provider: {provider}")
    value = (value or '').strip()
    if provider == 'local' and value and not re.match(r'^https?://', value):
        raise ValueError("The local AI endpoint must start with http:// or https://")
    if '\n' in value or '\r' in value:
        raise ValueError("Invalid value")

    lines = KEYS_FILE.read_text().splitlines() if KEYS_FILE.exists() else []
    entries = [line for line in lines if line and not line.startswith('#') and not line.startswith(f"{variable}=")]
    if value:
        entries.append(f"{variable}={value}")

    Config.DATA_DIR.mkdir(exist_ok=True)
    # Create with owner-only permissions before writing the secret
    fd = os.open(KEYS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write('# Saved from the web interface (Settings -> Vision AI); overrides .env\n')
        f.write(''.join(entry + '\n' for entry in entries))
    os.chmod(KEYS_FILE, 0o600)

    if value:
        os.environ[variable] = value
    else:
        os.environ.pop(variable, None)
    if provider == 'local':
        # Cleared: back to the endpoint in config.yaml
        Config.LOCAL_AI_ENDPOINT = value or get_config_loader().get(
            'vision_ai', 'local', 'endpoint', default='http://localhost:11434/v1/chat/completions')
