"""
The scanner server: the web pages, what happens to every captured card, the collection and decks.

Cameras are elsewhere (stations: station_client.py, the Android app) and send one picture per
card. The server opens none itself.

`python app.py` starts it (app.py only calls server.startup.main). The modules, in the order
they build on each other - a module imports only from those above it:

    state            the Flask / Socket.IO app and the shared objects (database, inventories, ...)
    logs             the log files
    desks            one desk per camera - whose card, review and events something is
    captures         a read card's way (route_identified -> search_and_emit_card ->
                     add_automatically / queue_for_review) and the captures waiting to be read
    card_data        updates of the card database
    pages            the pages, the live view, thumbnails, the header's numbers
    decks_api, deck_ideas_api, inventory_api, trades_api, backups_api      the collection page
    stations_api     the stations' routes; a station's capture arriving
    scan_events, settings_events, ai_events                                the scanner page
    startup          initialize_components (everything is created here), main()

A route or a Socket.IO event exists once its module has been imported: startup imports them
all, and tests/test_app_surface.py fails when one is missing.

Two rules that are easy to break (see CLAUDE.md): what concerns one camera's card goes through
desk() / emit_desk, and code running outside a page's request must be inside `with in_desk(...)`.

This file runs before any other module of the package, and that is used for the one thing
that has to happen first: API keys are put into the environment before config.py is imported,
because config.py reads the environment when it is imported.
"""
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # the project's top folder: app.py, data/, templates/, static/

# The project's own modules (config, inventory, ...) are in the top folder
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load API keys etc. from .env before config is imported (config reads env vars)
load_dotenv(ROOT / '.env')

# Keys entered in the web interface (data/api_keys.env) override .env
from api_keys import load_saved_keys  # noqa: E402
load_saved_keys()
