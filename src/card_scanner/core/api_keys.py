"""
API keys and the local AI endpoint, editable from the web interface.

Values entered in the UI are saved to data/api_keys.env (only readable by the owner) and
override .env - the systemd service can only write inside data/. They are applied to
os.environ immediately, so no restart is needed.
"""
import ipaddress
import os
import re
import socket
from urllib.parse import urlparse

from dotenv import load_dotenv

from card_scanner.config import Config
from card_scanner.config_loader import get_config_loader

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


# Tailscale and other carrier-grade-NAT addresses: a private network too
SHARED_ADDRESS_SPACE = ipaddress.ip_network('100.64.0.0/10')


def _is_own_network(address):
    """Whether one resolved address (an ipaddress object) is loopback, private, link-local or shared (Tailscale)"""
    return address.is_private or address.is_loopback or address.is_link_local or (
        address.version == 4 and address in SHARED_ADDRESS_SPACE)


def check_local_endpoint(value):
    """
    The local AI endpoint is where every card's picture is sent: it must be a machine of the person's
    own network. The host is RESOLVED, and every address it resolves to must be loopback, private
    (10/8, 172.16/12, 192.168/16, fc00::/7), link-local or Tailscale's 100.64/10 - judging the text of
    the name is not enough: `http://134744072` is a name without dots to a reader and 8.8.8.8 to the
    resolver (an outside review found this, 2026-10-16), and a public DNS name can end in .lan. A name
    that does not resolve is refused. Anything else is refused unless the environment says
    ALLOW_PUBLIC_AI_ENDPOINT=1: the settings can be changed by anyone who can reach the server (unless a
    settings password is set), and an address of someone else would receive a photo of each card.
    Not covered: a name whose DNS answer changes after it was saved (the check is at saving time), and
    the owner's own address in config.yaml or the environment (not checked at all). Raises ValueError.
    """
    try:
        parts = urlparse(value)
        host = parts.hostname
        parts.port  # a bad port raises here
    except ValueError:
        raise ValueError("The local AI endpoint is not a valid address")
    if parts.scheme not in ('http', 'https') or not host:
        raise ValueError("The local AI endpoint must start with http:// or https://")
    if parts.username or parts.password:
        raise ValueError("The local AI endpoint must not contain a user name or password")
    if os.environ.get('ALLOW_PUBLIC_AI_ENDPOINT') == '1':
        return
    own_network_addresses(host)


def own_network_addresses(host):
    """
    The addresses a host name resolves to, every one of them on the person's own network (see
    check_local_endpoint); raises ValueError when the name cannot be resolved or any address is outside.
    """
    try:
        # What the resolver makes of the text - the same call a connection makes - also for 127.1,
        # 0x7f000001 or a decimal number, which ipaddress.ip_address() would not read
        found = {info[4][0] for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)}
    except (socket.gaierror, UnicodeError, OSError):
        raise ValueError(f"The local AI endpoint's host {host} could not be found on this machine's network")
    inside, outside = [], []
    for text in sorted(found):
        try:
            address = ipaddress.ip_address(text.split('%')[0])   # a link-local IPv6 address names its interface after a %
        except ValueError:
            outside.append(text)
            continue
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        (inside if _is_own_network(address) else outside).append(str(address))
    if outside or not inside:
        raise ValueError(f"The local AI endpoint must be on your own network ({host} is {', '.join(outside) or 'not an address'}); "
                         "ALLOW_PUBLIC_AI_ENDPOINT=1 in the server's environment allows another address")
    return inside


def pinned_endpoint(url):
    """
    (url to connect to, extra headers) for a request to the local AI server, made at the moment of the
    request. The host is resolved NOW, every address must be on the person's own network, and an http
    request is sent to that address itself (the name stays in the Host header): a name whose DNS answer
    changed after the endpoint was saved - to a public server - is refused, and cannot change between the
    check and the connection. An https endpoint is checked the same way but connects by name (the
    certificate is checked against it; a short gap remains). ALLOW_PUBLIC_AI_ENDPOINT=1 switches this off.
    Raises ValueError. (Outside code review, 2026-10-16.)
    """
    if os.environ.get('ALLOW_PUBLIC_AI_ENDPOINT') == '1':
        return url, {}
    parts = urlparse(url)
    host = parts.hostname
    if not host:
        raise ValueError("The local AI endpoint has no host")
    address = own_network_addresses(host)[0]
    if parts.scheme != 'http':
        return url, {}
    shown = f"[{address}]" if ':' in address else address
    netloc = shown + (f":{parts.port}" if parts.port else '')
    return parts._replace(netloc=netloc).geturl(), {'Host': parts.netloc}


def save_credential(provider, value):
    """
    Save a provider's API key (or the local endpoint) to data/api_keys.env and apply it.
    An empty value removes the saved entry (a key in .env, if any, is still used after a restart).
    """
    variable = PROVIDER_VARIABLES.get(provider)
    if not variable:
        raise ValueError(f"Unknown provider: {provider}")
    value = (value or '').strip()
    if provider == 'local' and value:
        check_local_endpoint(value)
    if '\n' in value or '\r' in value:
        raise ValueError("Invalid value")

    lines = KEYS_FILE.read_text().splitlines() if KEYS_FILE.exists() else []
    entries = [line for line in lines if line and not line.startswith('#') and not line.startswith(f"{variable}=")]
    if value:
        entries.append(f"{variable}={value}")

    Config.DATA_DIR.mkdir(parents=True, exist_ok=True)
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
