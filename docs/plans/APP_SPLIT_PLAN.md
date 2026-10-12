# Splitting app.py: plan

Date: 2026-10-11

**Done on 2026-10-12** - see *How it went* at the end. What follows is the plan as it was
written the day before.

A plan for breaking `app.py` into modules by area. Nothing here is implemented. The numbers
below were counted in `app.py` as of commit 5390d76; the design choices are argued from the
code as it is, and none of them has been tried.

## Why

`app.py` is 3,637 lines: 162 functions, 60 HTTP routes and 30 Socket.IO events in one file,
more than twice the size of the next largest module (`scanner.py`, 1,624). It holds six
different things - logging, the desks, the capture pipeline, the collection's routes, the
stations' routes, the scanner page's events - and it changes constantly: 52 commits touched it
in the last 14 days. Everything that wants to know "where is the code for trades" has to
search one file.

The split is also the step before moving the code into `src/` with a `pyproject.toml`
(the rest of the folder reorganisation): doing it first means the largest file is moved once,
as a package, and not moved and then split.

## What must not change

This is a move, not a rewrite. After it:

- every route has the same address, methods and **endpoint name** (templates call
  `url_for('collection')`, `url_for('index')`, `url_for('video_feed')`);
- every Socket.IO event has the same name and behaviour;
- `python app.py` still starts the server: the Docker entrypoint, `scripts/start.sh`,
  `scripts/deploy.sh` and the systemd unit all run exactly that;
- function bodies are moved as they are. No renaming, no "while I am here" clean-ups: a diff
  of a moved function against its old text must be empty apart from the indentation of
  nothing. Improvements come later, in commits of their own, where they can be reviewed.

## What makes it harder than moving text

**1. Shared objects that do not exist yet when the file is imported.** `app.py` has 21
module-level names that start as `None` and are filled in by `initialize_components()` at
start-up - `database`, `inventory`, `scan_inventory`, `identification`, `stations`, `pending`,
`review`, `deck_store`, `trade_store`, `recommend`, `camera_hub`, `app_settings` and more.
Functions use them as bare names: `inventory` 83 times, `review` 66, `database` 47,
`stations` 42. Once those functions live in other modules, `from app import inventory` would
copy the `None` of import time and never see the real object.

**2. There are no tests of app.py.** The 143 automated tests cover the inventory, backups,
the database and the station's own code; none imports `app`. A mistake in the move - a name
that is no longer in reach, a handler that is never registered - would show only when that
route is used.

**3. The file is edited all the time**, also by other sessions. A branch that takes days
would collide with every one of those edits.

## Design

### A package, with app.py left as the launcher

```
app.py                 5 lines: from server.startup import main; main()
server/
    state.py           the Flask app, Socket.IO, and the shared objects
    logs.py            setup_logging, enable_request_log
    desks.py           Desk, desk, desk_for, in_desk, at_station, emit_desk, the scanner proxy
    captures.py        what becomes of a capture: submit_capture, route_identified, ...
    pages.py           the pages themselves, video feed, thumbnails, /api/stats
    inventory_api.py   /api/inventory..., import / export, clear
    trades_api.py      /api/trades...
    backups_api.py     /api/backups..., the backup schedule
    decks_api.py       /api/decks..., card search for decks
    deck_ideas_api.py  suggestions, popular decks, precons
    stations_api.py    /api/stations..., a station's capture arriving
    scan_events.py     the scanner page: capture, search, add, review, undo
    settings_events.py toggles, game, focus, rotation, fixed area, sound, scan location
    ai_events.py       AI provider, credentials, prompts
    card_data.py       card database updates
    startup.py         initialize_components, main, the background clean-up
```

Grouped by what the code is about, not by how it is called: `/api/prompts` (HTTP) and
`save_prompt` (Socket.IO) are both in `ai_events.py`, because whoever changes prompts needs
both.

The package is called `server` now and lands in `src/` later without changing again.

### The shared objects: proxies, so that function bodies stay as they are

`server/state.py` holds the real objects as attributes of one holder, and exports a proxy for
each:

```python
class Services:            # filled by startup.initialize_components
    database = None
    inventory = None
    ...
services = Services()

database = LocalProxy(lambda: services.database)
inventory = LocalProxy(lambda: services.inventory)
```

A module then says `from server.state import database, inventory` and its functions keep
using the bare names. The proxy looks the real object up at each use, so it does not matter
that it was `None` at import time.

This is not a new idea in this code: `scanner = LocalProxy(lambda: desk().scanner)` is
already how every handler reaches its camera, with a known rule (`if scanner:` works,
`scanner is None` does not). I checked: `app.py` compares none of these 21 names with
`is None` today, so the rule costs nothing now - but it becomes a rule for all of them and
goes into CLAUDE.md.

The alternative - writing `services.inventory` at each of several hundred uses - is honest
and explicit, but turns a move into an edit of every function, which is exactly where
mistakes come from. It can be done later, module by module, if the proxies turn out to
bother anyone.

Only the six functions that *assign* these names (`global database`, ...) change:
they set `services.database = ...` instead.

### Routes stay on the one Flask app - no Blueprints

Each module imports `app` and `socketio` from `server.state` and decorates its functions
with `@app.route` / `@socketio.on` exactly as now. A Blueprint would prefix every endpoint
name (`inventory.get_inventory`), which changes `url_for` and gains nothing here: there is
one app and no need to mount parts of it elsewhere.

Handlers register when their module is imported, so `startup.py` imports every module, in
one visible list. A module missing from that list is a set of routes that silently does not
exist - the route test below catches that.

### Layers, to keep imports from going in circles

```
state  ←  logs  ←  desks  ←  captures  ←  the *_api and *_events modules  ←  startup
```

A module imports only from its left, and the `*_api` / `*_events` modules do not import
each other. Where two of them need the same function (`undo_station_add` is called by the
stations' undo route and by the scanner page's undo event), that function moves left, into
`desks` or `captures`. Those cases are listed in phase 0 before anything moves.

## How it is checked

Because nothing tests `app.py` today, the checks come first and are part of the work:

| Check | What it catches | When |
|---|---|---|
| **The surface test** (new, `tests/test_app_surface.py`): the list of (address, methods, endpoint name) and of Socket.IO events, compared with a list recorded before the split | a route or event lost, renamed or never registered | every commit |
| **Undefined names** (pyflakes on `server/`) | a function moved away from a name it uses - the typical mistake of this job | every commit |
| **The moved text** (a script comparing each function's body before and after) | an accidental edit inside a moved function | every commit |
| **The 143 existing tests** | damage to the modules app.py uses | every commit |
| **A recorded walk through the server** (new script): start it on *copies* of the databases in a temporary folder, call every GET route and a fixed list of actions (add, edit, move, trade, deck, backup), save the answers; run before and after and compare | a route that answers differently | start, middle, end |
| **The pages in a headless browser** (as for the redesign) | script errors, a page that no longer connects | end |
| **A station against it** (the Pi 4 on a test server, or recorded captures uploaded) | the capture pipeline end to end | end |

The recorded walk needs the server to run on a data folder that is not the checkout's
`data/`. `Config` has no setting for that; running a *copy of the checkout* in a temporary
folder does it without touching the code. Never the real server's data.

## Phases

Each phase is one commit that passes the first four checks. The order goes from what
nothing depends on to what depends on everything.

### 0. Prepare (no behaviour change)

- Write the surface test and record the surface.
- Write the recorded walk and record its answers.
- List every call that goes "up" the layers and decide where each such function lives.
- Agree a time when nobody else edits `app.py` (see Risks).

### 1. state.py, logs.py

The app, Socket.IO, the holder and the proxies; the two logging functions. `app.py` imports
them back, so everything else still works unchanged. This is the phase that proves the proxy
idea: if it is going to cause trouble, it shows here, with almost nothing moved yet.

### 2. desks.py, then captures.py

The core that everything else calls. ~150 and ~340 lines.

### 3. The collection's routes, one module per commit

`trades_api` (~95 lines) first - the smallest, to rehearse the routine - then `backups_api`
(~155), `decks_api` and `deck_ideas_api` (~630 together), `inventory_api` (~350), `pages`
(~110).

### 4. stations_api.py (~210)

The capture's arrival: the one route where a mistake loses cards. Checked with real uploads
before moving on.

### 5. The scanner page's events

`scan_events` (~325), `settings_events` (~340), `ai_events` (~155), `card_data` (~120).

### 6. startup.py; app.py becomes the launcher

`initialize_components`, `main`, the background clean-up. `app.py` is five lines.

### 7. Documentation

CLAUDE.md's *Where Things Live* names `app.py` in 18 rows, PROGRAM_DOCUMENTATION.md in 15
places, the README once, and the Android repository's README and CLAUDE.md once each: each
gets the module that now holds what it points at. A new rule in CLAUDE.md: the proxies, the
layers, and that a new module must be in `startup.py`'s list.

### 8. Deploy

Backup, then the server (it is the only machine this touches - stations do not run
`app.py`), comparing `/api/stats` before and after as on 2026-10-11.

## Risks and open points

1. **Edits to app.py while the split is under way.** With 52 commits in two weeks, this is
   the main risk. The split should be done in one sitting, with `app.py` frozen for that
   time. If something must change meanwhile, it is applied to the split afterwards, by hand.
2. **Proxies are not the objects.** `isinstance(inventory, InventoryManager)` is false for a
   proxy, `inventory is None` is always false, and a proxy passed into a thread keeps
   working (it looks the object up by itself) - unlike `scanner`, which depends on the
   current desk. To be verified in phase 1 that no module *outside* `app.py` is handed one of
   these and tests its type.
3. **Functions that are used before they are defined, across modules.** Inside one file the
   order does not matter at call time; between modules an import cycle fails at start-up.
   Loud, not silent - and the layers are there to prevent it.
4. **Background threads started at import.** If any module starts work when imported rather
   than in `initialize_components`, importing it for the surface test would start it too. To
   be checked in phase 0. Today importing `app.py` sets up logging (it creates `data/logs/`)
   and starts no thread; everything else waits for `main()`.
5. **The recorded walk is only as good as its list of actions.** Routes it does not call are
   covered by the surface test and the undefined-names check only.
6. **Not decided:** whether `decks_api` is split further (it is the largest module after the
   split, ~420 lines), and whether the proxies stay for good or are replaced by
   `services.x` later.

## What this does not do

- It does not make `app.py`'s logic testable by itself; it makes that possible later, one
  module at a time.
- It does not touch `scanner.py` (the Windows plan separates the camera kinds there).
- It does not move anything into `src/` or add `pyproject.toml` - that is the next step,
  and smaller once this is done.

## Size

About 3,600 lines moved in about 15 commits, plus two new test tools. With `app.py` frozen
and the checks in place, one long working session; without the checks it would be faster and
not something to deploy to the server that holds the collection.

## How it went (2026-10-12)

Done as planned in its design, differently in its steps.

- **One generated move, not fifteen commits.** The plan's phases would have meant
  intermediate states where `app.py` and the new modules import each other. Instead a
  generator read `app.py` and sent every top-level statement to its module, writing each
  module's imports from the names it uses; it was run, checked and committed as one step.
  The per-function check below is what the phase-by-phase commits were meant to give.
- **The module order instead of "siblings do not import each other".** Letting the
  `*_api` / `*_events` modules import from those before them in one fixed order left a
  single function to relocate (`start_price_update`, with `update_added_prices`, to
  `captures.py`); the stricter rule would have needed a grab-bag module for five more.
- **Two things the plan had not seen**: `Flask(__name__)` and `Path(__file__).parent` both
  meant "the project's top folder" and would have meant `server/` after the move - the app
  is created as `Flask('app', root_path=ROOT)` and paths are built from `server.ROOT`.
  `default_desk` is reassigned at start-up, so it became `services.default_desk` everywhere
  rather than a proxy (a proxy returned by `desk()` would have been compared and stored as
  if it were the desk).

Checked:

| | |
|---|---|
| Functions and classes of the old `app.py` | 162: 151 identical in text, 11 changed - only by the rewrites above (`services.x`, `ROOT`, `global` lines removed), each read |
| Undefined names (pyflakes) | none |
| Routes and events (`tests/test_app_surface.py`) | 61 and 30, as recorded before |
| The recorded walk (`scripts/walk_server.py`) | 73 steps, twice before and twice after on fresh copies of the laptop's test data: no step different |
| Pages in a headless browser | no script errors |
| A station's connection (`hello` on `/station`) | answered with the camera's settings |
| All tests | pass |

Not checked: a real camera station scanning against the split server, and the server with a
camera of its own (`camera.type` other than `remote`) - that branch of
`initialize_components` was moved like the rest but not run.

`app.py` is 13 lines; the largest module is `decks_api.py` (about 400 lines).
