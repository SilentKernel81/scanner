# One package with a name, sorted into station / core / server: plan

Date: 2026-10-12

**The move itself was done on 2026-10-13** (phases 0, 1 and 3; a real camera and the machines
are still to do) - see *How it went* at the end. What follows is the plan as it was written.

A plan for the step that [SRC_MOVE_PLAN.md](SRC_MOVE_PLAN.md) left for later (its decision
1): the modules that lie flat in `src/` become one package with a name, sorted into folders
by which program they belong to. Nothing here is implemented. The numbers were counted at
commit 5e52197; the design is argued from the code as it is and from what the move into
`src/` showed two hours ago, and none of it has been tried.

The package is called `card_scanner` below. That name is the first decision, not a fact.

## Why

`src/` holds 28 modules side by side, and nothing in the layout says which of them a camera
station runs. That is known only from a list: the ten files named in two `COPY` lines of the
Dockerfile, and a rule in CLAUDE.md ("the client must not import server modules"). A station
module that gains an import breaks the image until the list is edited, and a station module
that imports `inventory` by mistake is caught by nobody until a station fails to start.

The modules also have the plainest possible names - `config`, `settings`, `database`,
`scanner`, `server` - which is why the project cannot be installed and why "is this name
taken by a library?" had to be checked by hand for each image.

After this step the layout says it:

```
app.py   station.py          what you start
src/card_scanner/
    config.py  config_loader.py  settings.py  paths.py     what both programs use
    station/                  a camera station: all of it, and nothing else
    core/                     reading cards, card data, the collection - no web, no camera
    server/                   the web app: pages, routes, events
    games/
src/ocr/                      the OCR reader (Node.js), where it is
```

## What must not change

- **`python app.py` starts the server.**
- **The data stays where it is**: `data/`, `scanned_cards/`, `station_captures/`,
  `config.yaml`, `.env` in the top folder. `tests/test_paths.py` says so today and must say
  so afterwards with only its import lines changed.
- **Every module keeps its file name** (`inventory.py`, `station_client.py`): the documents
  name files by those, and so does anyone searching. Only the folder changes.
- **Every function body is what it was.** Import lines are rewritten, by a program; nothing
  else in a function changes. A comparison of every file with its import lines taken out
  must find no difference but the handful of lines listed in *Design*.
- **Every route, event and answer** (the surface test, the recorded walk), and what Docker
  Compose resolves (`config`, compared).
- **What the logs say.** Two loggers take their name from their module (`backups.py`,
  `cleanup.py`: `getLogger(__name__)`), so their lines would begin with
  `card_scanner.core.backups` instead of `backups`. They are given their present names as
  text. No log is sorted by these names (`server/logs.py` sorts by the fixed names
  `card_scanner`, `ai`, `database`, `scanned_cards`) - read, to be confirmed by a look at
  the logs of a walk before and after.

## What makes it harder than the last move

**1. 218 import lines in 54 files change** (counted: lines that import one of the 28
modules, `server` or `games`, in `app.py`, `src/`, `tests/` and `scripts/`). 28 of them are
inside function bodies. A test only runs such a line if it runs that function, and pyflakes
says whether a name is used, not whether an import can be found. An import the rewrite
missed would show when that function is first called - on the server, some evening.

**2. A file inside a package cannot be started by its path.** `python
src/station_client.py` works today because Python puts that file's folder on its path and
its neighbours are found. Inside a package the neighbours are found by the package's name,
from `src/` - which is not on the path when the file is started that way. So the commands
change again, three days after they changed last: either `python -m
card_scanner.station.station_client` with `PYTHONPATH=src`, or a launcher like `app.py`.

- The station **restarts itself** from its setup page by running its own command line again
  (`os.execv(sys.executable, [sys.executable] + sys.argv)`). Started with `-m`, that line
  would run the file by its path and fail; started through a launcher it is unchanged.
- The same holds for `setup_database.py` (run by the Docker entrypoint on a first start,
  and by `deploy.sh`) and `cleanup.py`.

**3. `__init__.py` files must do nothing.** The station's image will hold `card_scanner/`
without `core/` and `server/`: if the package's own `__init__.py` imported anything of
those, no station would start. And `server/__init__.py` puts the API keys into the
environment *before* `config.py` is imported - which only holds as long as nothing imports
`config` on the way to it.

**4. Everything else of the last move, again:** `paths.py` moves a folder deeper (its two
folders are counted from where it lies), three shell scripts run `python -c "from config
import Config"`, one test names a module as text (`mock.patch('server.scan_events...')`),
the Android repository's two scripts import from here, four machines are copies by rsync,
and a machine installed with `deploy.sh` runs the *old* script for the update that brings
this change.

## Design

### Where each module goes

| Folder | Modules | Rule |
|---|---|---|
| `card_scanner/` itself | `paths`, `config`, `config_loader`, `settings` | imports nothing of the package but each other |
| `station/` | `station_client`, `station_setup` (+ `station_setup.html`), `station_login`, `scanner`, `object_detector` | imports only from `station/` and the four above |
| `core/` | `identification`, `card_ocr`, `card_identifier`, `prompts`, `api_keys`; `database`, `card_search`, `setup_database`; `inventory`, `decks`, `trades`, `backups`, `storage`, `pending`, `review`, `cleanup`; `stations`, `remote_scanner`, `recommendations` (19) | no Flask, no camera; never imports from `server/` or `station/` |
| `games/` | as it is | |
| `server/` | the 17 modules it has, `templates/`, `static/` | as today: each module only from those before it |

The 19 are sorted by one question - is this the web app, the camera, or neither? - and not
further. `core/` could later be split into reading, card data and collection; that is 19
placements with their own arguments and nothing here needs it.

Checked from the import lines of today: the five station modules and the four shared ones
import only each other. So the station's rule holds already and the move does not have to
untangle anything.

`src/ocr/` stays where it is. It is not Python, the package has no use for it inside, and
staying means the Dockerfile's OCR stage, `.dockerignore`, `deploy.sh`'s `npm install` and
the installed packages on machines without Docker are untouched - the part of the last
move that was most awkward.

### Imports: written out in full

`from card_scanner.core import inventory as inv`, `from card_scanner.config import Config`,
`from card_scanner.server.state import app`. Not the short relative form (`from ..core
import inventory`): the full form reads the same in every file, in the tests and in a
`python -c` line, and a search for a module finds every use.

### The rewrite is a program

As for the split of `app.py`: a script reads every Python file, finds each import statement
with Python's own parser (so that one inside a function, or one spread over lines, is found
like any other), and writes the new line in its place. It is run once, its result checked
and committed; the script itself goes into the commit message's story, not into the
repository.

Changed by hand, and listed in the commit: `paths.py` (its folders), the two loggers'
names, the `mock.patch` text in one test, `app.py`, and the new launchers.

### paths.py

```python
CODE = Path(__file__).resolve().parent   # src/card_scanner: the package
TOP = CODE.parent.parent                 # the folder that holds src/, app.py, config.yaml, data/
```

`SRC` becomes `CODE` because it no longer means `src/`. Flask's folder is `CODE / 'server'`,
the OCR reader `TOP / 'src' / 'ocr'`.

### What starts what

| Program | Today | After (recommended, decision 3) |
|---|---|---|
| Server | `python app.py` | the same |
| Station | `python src/station_client.py --server ...` | `python station.py --server ...` - a launcher in the top folder, like `app.py` |
| Card data | `python src/setup_database.py` | `python scripts/setup_database.py` |
| Old pictures | `python src/cleanup.py --stats` | `python scripts/cleanup.py --stats` |

Each launcher is a few lines: put `src/` on the path, call the module's `main()`. With them
no command needs `PYTHONPATH` or `-m`, the station's restart runs the same command line as
today, and the systemd unit needs no environment. The objection of the last plan - two files
called `station_client.py` - is gone: the module is `card_scanner.station.station_client`,
the launcher `station.py`, and nothing can be imported under the wrong name.

The images still set `PYTHONPATH=/app/src`, for the entrypoint's `python -c`.
`tests/test_paths.py`'s "only `app.py` in the top folder" becomes "only `app.py` and
`station.py`".

### The station's image

```dockerfile
COPY station.py config.yaml ./
COPY src/card_scanner/__init__.py src/card_scanner/paths.py src/card_scanner/config.py \
     src/card_scanner/config_loader.py src/card_scanner/settings.py src/card_scanner/
COPY src/card_scanner/station src/card_scanner/station
```

A new module in `station/` is in the image without anyone editing this. A new *shared*
module still has to be named - the four are few and change rarely; putting them in a folder
of their own (`card_scanner/common/`) would remove even that, at the price of
`card_scanner.common.config` in 31 files. Not recommended.

### An update that runs the new script

`deploy.sh --update` pulls the code and carries on - as the script it was before the pull.
That is why the last move needed the script run twice. The new script starts itself again
after a pull that brought something (`exec` of the file just pulled, with a mark so that it
does not pull a second time). It cannot help the update that brings *this* change - that
one is still run by today's script - but it ends the pattern for every later one.

## Decisions

**Decided by the user on 2026-10-12: all four as recommended** - `card_scanner`, three
folders, launchers, not installable. On the open point about Windows (risk 6): not to be
considered here.

1. **The package's name.** It cannot be `scanner`, `server`, `games` or `config`: those are
   modules inside it. Candidates: `card_scanner` (the name the server's own log already
   uses, and neutral about the game - there is a plan for more than Magic), `mtg_scanner`
   (what the images, the services and the Android repository are called), `scanner_server`
   (the repository; odd as the name of a station's code). **Recommended: `card_scanner`.**
2. **How finely to sort.** Three folders as above (`station/`, `core/`, `server/`); or
   `core/` split further now; or the 19 put inside `server/` beside the web modules, which
   would bring them under its "only import from those before you" rule and make that list
   36 long. **Recommended: three folders.**
3. **The commands.** Launchers (`station.py` in the top folder, the two tools in
   `scripts/`) as in the table; or `python -m card_scanner.station.station_client` with
   `PYTHONPATH=src` set wherever something is started. **Recommended: launchers.**
4. **`pyproject.toml`: installable now?** With one package under a name of its own, the
   reason it could not be installed is gone, and `pip install -e .` would put the package
   on the path without launchers touching it. The Pi camera image still has no pip.
   **Recommended: still not** - one way of finding the code, the same on every machine.

## How it is checked

The checks of the last move, all of which exist now, and two new ones written first.

| Check | What it catches | When |
|---|---|---|
| **No import by an old name** (new test): every `import` in `app.py`, `src/`, `tests/`, `scripts/`, found with Python's parser, also inside functions - none may name one of the 28 modules, `server` or `games` at the top level | an import the rewrite missed, in a function no test runs | written in phase 0 (it then lists today's 218); must find none after |
| **Every import of the project can be found** (same test): for each `card_scanner...` import, the module exists | a rewritten line that points at the wrong folder | after |
| **The station stands alone** (new test): nothing in `station/` or the four shared modules imports from `core/`, `server/` or `games/` | the CLAUDE.md rule, broken - today caught by a station that does not start | phase 0, against today's files by their names; after, by their folders |
| **Every module can be imported** (new test): each file of the package, one by one | a cycle, a missing `__init__.py` | after |
| `tests/test_paths.py` | the data folder, the pages, the OCR reader, the setup page | before and after, only its import lines changed |
| The other tests, pyflakes | | after |
| **Bodies unchanged**: every file before and after with its import lines removed | an edit that is not an import | after |
| **The recorded walk**, the commit before against the new tree, twice each on one fresh copy; also against the server image | an answer that differs; OCR quietly off | after |
| **The log files of those walks** compared by logger name | a line that changed its name | after |
| `docker compose config`, four combinations | | before, after |
| **Each image**: imports its program, `config` comes from `/app/src/card_scanner`, the station image holds no `core/` and no `server/` | a file the image lacks; server code in a station | after each build |
| **The station image**: connects, restarts from its setup page, connects again | the launcher, the restart | after |
| **`deploy.sh` in the systemd container**, as on 2026-10-12: installed at today's commit with services, `--update` across, the script again; `start.sh` and Ctrl+C in a terminal | the units, the crossing, the launchers without Docker | after |
| The three pages in headless Chromium | | after |
| **The Pi 4's camera on a test image** against a local server, as on 2026-10-12 | the Pi camera image; a real camera | before the machines |

## Phases

0. **Prepare.** The two new tests against today's layout. Record the walk and the compose
   outputs. A time when no other session edits the code.
1. **The move**, one commit: `git mv` into the folders, the rewrite, `paths.py`, the
   launchers, the Dockerfile, the three scripts, the unit, the loggers' names. Then every
   check up to the systemd container.
2. **The Pi 4's camera** on an image built on the Pi 5, against a local server; put back.
3. **Documents.** CLAUDE.md (the layout, the rules that become tests, the commands),
   README, INSTALL.md (the commands; a third "coming from" paragraph), the program
   documentation, this plan's *How it went*. The Android repository: its two scripts'
   imports and the paths in its documents.
4. **The machines**, as on 2026-10-12: the server (backup; compare and delete what left the
   tree; build; look into the image; `up`; the numbers), then the three stations.

## Risks and open points

1. **An import that no check runs.** The reason for the parser-based test: it reads every
   import statement there is, whether or not anything executes it.
2. **A second change of commands in three days**, for anyone who followed the first. The
   station's command becomes shorter than it ever was (`python station.py`), and INSTALL.md
   gets one paragraph that covers coming from either earlier layout.
3. **The update across this change, on a machine installed with `deploy.sh`**: run by
   today's script. Its unit starts `src/station_client.py`, which will be gone, and its
   `python -c 'from config import Config'` will fail quietly - the script then believes the
   machine wants a station whatever `config.yaml` says. Run twice, as last time; to be seen
   in the systemd container before the documents describe it.
4. **Every machine is rebuilt again.** A station left on its image keeps working.
5. **Renaming `core/` later.** A folder's name is in every import of 19 modules. It is
   decided once, here (decision 2).
6. **Not decided, and asked of the user: what "Windows as a standalone program" means.** If
   it is one program on a Windows PC with its own camera, reading and collection and no
   server, it would want exactly what `core/` is - the reading and the collection without
   the web app - and `station/` beside it; the layout above would serve it, and that is an
   argument for keeping `core/` free of Flask as a tested rule, not only a table. If it
   means something else, this plan does not depend on it.

## What this does not do

- It changes no behaviour and renames no file.
- It does not split `core/` further, `decks_api.py`, or `scanner.py`.
- It does not replace the proxies by `services.x`.
- It does not make the project installable (decision 4), or the data folder a setting.

## Size

49 Python files and the pages' 17 moved, 218 import lines rewritten by a program, about 30
lines by hand, three new launchers and three new tests; the Dockerfile's three targets,
three scripts, one unit; the documents of two repositories; four machines. In code about
the size of the last move; the checks are written, the machines' steps are known, and the
systemd container makes `deploy.sh` testable this time before anyone is told how to update.

## How it went (2026-10-13)

Phases 0, 1 and 3 are done, on the branch `package`. **Not done: phase 2 (the Pi 4's camera on
a test image) and phase 4 (the machines)** - every machine still runs the layout of
2026-10-12, which goes on working.

Done as planned. What differed or was added:

- **`cleanup.py` is started by its launcher as a module run under the name `__main__`**
  (its command line is not in a `main()`), so that not a line of it had to move.
- **`.dockerignore` kept compiled files out of the top folder only.** The station images
  now copy a folder, and the laptop's `__pycache__` came along with it: the pattern is
  `**/__pycache__/` now.
- **A machine that only runs the server gets a station's service from the update** that
  crosses this change: the script from before asks `config.py` about the camera under the
  module's old name, gets no answer and takes that for a camera. Seen in a container; the
  new script removes such a service where `camera.type` is `remote`, and INSTALL.md says to
  run it once more on such a machine. The plan's risk 3 had expected the station's unit to
  point at a missing file instead - it does not: the old script already writes the units
  anew at every update, from the files the pull has just brought.

Checked, on the laptop:

| | |
|---|---|
| The rewrite | 218 import statements in 54 files, the number counted beforehand |
| Every file before and after, without its import lines | 62 compared, 10 differ - by the lines changed on purpose (listed in the commit) |
| All tests | 198 pass, none skipped. The layout's: no import by an old name, no relative import, every import of the project names something that exists, every module can be imported, the station and `core/` stand alone, the three `__init__.py` run nothing |
| ... that the layout test notices | before the move, on a scratch copy: an import of `inventory` planted in `scanner.py` and one of `server.state` in `database.py` were both reported with file and line |
| Undefined names (pyflakes) | none |
| The recorded walk, 73 steps with a card's picture | the commit before against the new tree, twice each on one fresh copy: four identical recordings (and identical to every recording since 2026-10-12); the same against the server image |
| Logger names in `app.log` of those walks | the same before and after |
| `docker compose config`, four combinations and `compose.yaml` | identical |
| Inside the images | `config` from `/app/src/card_scanner`; the station image holds `station.py`, `config.yaml`, the four shared modules and `station/` - no `core/`, no `server/`; the server image the two launchers, `card_scanner/` and `ocr/` |
| The station image against a local server | connected, restarted from its setup page (the same container, `station.py` run again), connected again |
| The three pages in headless Chromium | no script error, no file of the server's missing |
| `deploy.sh` in a fresh clone (Arch, Python 3.15), `start.sh` in a terminal with the laptop's camera, Ctrl+C | installed; server and station up and connected; both ended at once |
| `scripts/cleanup.py --stats`, `scripts/setup_database.py`, `station.py --help` | each starts its program |
| `deploy.sh` in an ordinary container with a **stand-in for `systemctl`** (it writes down what it is asked): installed with services at the layout of 2026-10-12, `--update` across | one run: both unit files written anew, the station's starts `station.py`, which exists |
| ... a later `--update` that brings a newer script | "carrying on with the new version of this script", once; with nothing new, "Already up to date" |
| ... on a server-only installation | the update across adds the station's service (above); the next run of the new script removes it |

Not checked:

- **real services**: what the units do when systemd starts them. The stand-in checks what
  the script writes and asks for. The command a unit runs was started by hand in that
  container (`station.py`: setup page up, looking for its server; `app.py`: imports, 62
  routes);
- the `client-pi` image and any real camera on the new layout; a server with a GPU;
- the machines.

**What went wrong on the way, and is not the change's doing:** to try `deploy.sh --service`
with a real systemd I started a *privileged* container running systemd on the laptop, as
the evening before. This time its image no longer had the login and console services
switched off; the container's systemd took the laptop's console, the desktop session
ended and the machine was restarted. Nothing committed was lost - the move had been
committed minutes earlier - but everything in the temporary folder was (the recordings,
the helper scripts). That way of testing is not used again; the stand-in above replaced
it.
