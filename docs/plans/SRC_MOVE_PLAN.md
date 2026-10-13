# Moving the code into src/: plan

Date: 2026-10-12

**Done on 2026-10-12**, all phases - see *How it went* at the end. What follows is the plan
as it was written.

A plan for the last step of the folder reorganisation: the Python code out of the top folder
into `src/`, and a `pyproject.toml`. Nothing here is implemented. The numbers below were
counted at commit 71df153; the design is argued from the code as it is, and none of it has
been tried. What was looked at and what was only assumed is marked.

It follows the documents (`docs/`, 8df12b5), the Docker files (`deploy/`, e65b203) and the
split of `app.py` ([APP_SPLIT_PLAN.md](APP_SPLIT_PLAN.md), b26d6ef), which was done first so
that the server would be moved once, as a package.

## Why

The top folder holds 29 Python files next to the things a person looks for there: the
README, `config.yaml`, `compose.yaml`, `data/`. Of those 29, one is what you start
(`app.py`), three are programs you run now and then (`station_client.py`,
`setup_database.py`, `cleanup.py`), and 25 are modules nobody runs. Together with `server/`
(17 files) and `games/` (4) that is 50 files of code, 18,348 lines with tests and scripts.

After the move the top folder says what the project is made of - `src/`, `tests/`,
`scripts/`, `deploy/`, `docs/`, the settings, the data - and `pyproject.toml` says what it
is and which Python it needs.

## What must not change

This is a move of files, not a change of code. After it:

- **`python app.py` starts the server**, as the Docker entrypoint, `scripts/start.sh`,
  `scripts/deploy.sh` and the systemd unit run it today.
- **The data stays where it is.** `data/`, `scanned_cards/`, `station_captures/` and
  `config.yaml` are in the top folder (`/app` in the images): the volumes in the compose
  files, the units' `ReadWritePaths`, `scripts/backup.sh`, the rsync of a deployment and
  every backup made so far name them there.
- **Every module keeps its name.** `import inventory`, `from server.state import app`,
  `from games.base import ...` are written as they are now; no import line changes.
- **Every route, event and answer** is the same (the surface test, the recorded walk), and
  so is every address a page loads from: `/static/js/scanner.js?v=N` is the same address
  wherever the file lies on disk.
- **The compose files resolve the same folders, volumes and network**
  (`docker compose ... config`, compared before and after, as for `deploy/`).

## What makes it harder than moving files

**1. The code finds the data by where the code is.** Four places take "the project's top
folder" from their own file's location. Moved into `src/`, each of them would silently mean
`src/` - and a server whose `Config.DATA_DIR` is `src/data` starts on an empty collection.
Started from a checkout it would do so without complaint (it creates the folder); in the
server's image it would probably fail at once instead, because only `data/` and
`scanned_cards/` belong to the user it runs as. (Tried in phase 0, on a copy: a move with
nothing repaired is loud - `config.yaml` is not found and nothing imports. The quiet case
begins the moment that one line is repaired and the others are not.)

| Place | What it finds from its own location |
|---|---|
| `config.py:65` `Config.BASE_DIR` | `data/`, `scanned_cards/`, `station_captures/`, `ocr/` (through `card_ocr.OCR_DIR`) |
| `config_loader.py:17` | `config.yaml` |
| `server/__init__.py:37` `ROOT` | `.env`, `templates/`, `static/` (Flask's `root_path`), `data/logs/` |
| `station_setup.py:47` `PAGE_FILE` | `station_setup.html` - right again if the page moves with it |

After the move there are two answers where there was one: the data, `config.yaml` and
`.env` are in the top folder, and the pages, their files and the OCR reader are with the
code (decision 4). Each use has to be given the right one.

This is the mistake the plan is built around. The same kind of thing was the one surprise of
the split ("`Flask(__name__)` and `Path(__file__).parent` would have meant `server/`").

**2. The modules are found because they lie beside what is started.** `python app.py` puts
the top folder on Python's path, and that is the only reason `import inventory` works. The
tests (7 files) and two scripts add the top folder themselves; three shell scripts run
`python -c "from config import Config"` from the top folder.

**3. Three machines build a station from a fixed list of files** (the `COPY` lines of the
`client` and `client-pi` images), and the server and all stations are deployed by rsync,
which **does not delete**. After the move the old files would still lie in the top folder of
every machine: `COPY . .` would put them into the server's image, and wherever the top
folder comes before `src/` on the path, yesterday's `config.py` would be the one imported.

**4. The Android repository runs scripts against this folder** (`parser_reference.py`,
`export_reference.py`: `sys.path.insert(0, '.')`, run from here) and names files of this
project in its README and CLAUDE.md.

## Design

### The layout

```
app.py                  starts the server - unchanged in what it does
pyproject.toml          new
config.yaml  .env  compose.yaml  requirements*.txt  README.md  CLAUDE.md  LICENSE
src/
    paths.py            new: the one place that says where the top folder and src/ are
    server/             as it is, and with it what only the server uses:
        templates/      the pages
        static/         their scripts, styles, the font, Socket.IO's client
    games/              as it is
    inventory.py, database.py, scanner.py, station_client.py, ...   the other 28 files
    station_setup.html  beside station_setup.py, which reads it
    ocr/                the OCR reader (Node.js), beside card_ocr.py, which starts it
tests/  scripts/  deploy/  docs/
data/  scanned_cards/  station_captures/     the data: never moved
```

### One definition of the top folder

A new module, `src/paths.py`, with nothing in it but

```python
SRC = Path(__file__).resolve().parent   # the code: this folder
TOP = SRC.parent                         # the folder that holds src/, config.yaml, data/
```

and every place above takes its folder from there:

| What | From |
|---|---|
| `Config.BASE_DIR` (data, captures), the config loader's `config.yaml`, `server.ROOT` (`.env`, `data/logs/`) | `TOP` |
| Flask's `root_path` in `server/state.py` - where it looks for `templates/` and `static/` | `SRC / 'server'` |
| `card_ocr.OCR_DIR` | `SRC / 'ocr'` |
| `Config.TEMPLATES_DIR`, `Config.STATIC_DIR` (defined, used nowhere today) | `SRC / 'server' / ...`, so that they do not name folders that are gone |

The Flask app keeps its name (`Flask('app', ...)`), so endpoint names and `url_for` are
untouched; only the folder it is told to look in changes. `paths.py` imports nothing of the
project, so it can be the first thing anything imports (also `server/__init__.py`, which
runs before `config`). It must be in the station images' `COPY` list.

`server/__init__.py` stops touching Python's path. Today it puts the top folder first on it;
it could not put `src/` there itself (it needs `paths`, which is in `src/`), and the top
folder ahead of `src/` is exactly how a file left behind would be imported instead of the
moved one. Whatever starts a program puts `src/` first: `app.py`, `PYTHONPATH`, the tests'
own line.

This is the only new code of the move, and the rule in CLAUDE.md ("paths are built from
`server.ROOT`, never from `__file__`") becomes: from `paths.TOP` for data and settings,
from `paths.SRC` for what belongs to the code.

### The OCR reader's folder

`ocr/` is not Python: `package.json`, its lock file, `server.mjs`, and `node_modules/`
(128 MB on the laptop, installed by `npm install`, in no repository). Moving it to
`src/ocr/` touches, besides `card_ocr.py`:

- the Dockerfile's first stage (`COPY ocr/package.json ...`) and the line that puts the
  built `node_modules` into the server's image (`ocr/node_modules` → `src/ocr/node_modules`);
- `.dockerignore` (`ocr/node_modules/`): if it keeps the old path, the laptop's 128 MB of
  modules are sent to every build and copied into the image before the built ones replace
  them;
- `scripts/deploy.sh` (`cd ocr && npm install`);
- every machine that runs the server **without Docker**: its installed `node_modules` are
  left in the old `ocr/`, and the server would start with OCR switched off - which it
  reports as "not installed", not as an error. Moving the folder by hand or running
  `npm install` in the new one is part of the update there (the laptop's dev checkout is
  one such machine).

Python may now see `src/ocr` as something importable called `ocr`. Nothing imports that
name (searched), and `card_ocr` is a different name.

### How `src/` gets onto Python's path

| What is run | How it finds the modules |
|---|---|
| `python app.py` | `app.py` puts `src/` first on the path before it imports `server` - three more lines in the launcher |
| `python src/station_client.py`, `src/setup_database.py`, `src/cleanup.py` | Python puts a script's own folder on the path: nothing to do |
| The three images | `ENV PYTHONPATH=/app/src`, which also serves the entrypoint's `python -c "from config import Config"` |
| `scripts/start.sh`, `scripts/deploy.sh` | `export PYTHONPATH="$DIR/src"` once, for their `python -c` lines |
| Tests, `scripts/benchmark_*.py` | their existing `sys.path.insert(0, ...)` line, with `/ 'src'` |

No `pip install -e .`. Two reasons, both from this project:

- the `client-pi` image has no pip - it takes Python and every library from apt, because
  picamera2 exists only there;
- the modules have names like `config`, `settings`, `utils`, `database`, `scanner`. An
  installed project is found *after* the installed libraries, so a library of one of those
  names would be imported instead. Checked in the dev venv on 2026-10-12: none of the 31
  module names is taken there. Not checked: the Pi image's apt Python.

### pyproject.toml

Says what the project is - name, version, `requires-python = ">=3.10"`, the licence, the
README - and where tools should look (`src`). It is **not** how the project is installed:
`requirements.txt` and `requirements-client.txt` stay the lists of libraries, because the
Dockerfile installs from them in a layer of its own (a code change does not reinstall
300 MB), and listing the libraries a second time would be two lists to keep equal.

## Decisions

Each had a recommendation. **Decided by the user on 2026-10-12:** 1, 2, 3 and 5 as
recommended; 4 the other way. The plan is written for these answers.

1. **Flat in `src/`, or a package with a name?** A package (`src/mtg_scanner/inventory.py`,
   `from mtg_scanner import inventory`) is what "standard" usually means, would end the
   worry about names like `config`, and could sort the files into server / station / shared
   folders - which would turn the station images' file list into "copy two folders".
   It also rewrites every import in 50 files and the tests, and changes the names the
   Android scripts import. **Recommended: flat now**, the package as a later step of its
   own, judged once this one is deployed. The files then move twice; Git follows that.
2. **Which commands stay as they are?** `python app.py` must. For the other three programs
   the choice is a second file of the same name in the top folder that only starts the real
   one (`station_client.py` → `src/station_client.py`), or a new command.
   **Recommended: the new command** (`python src/station_client.py --server ...`): two files
   called `station_client.py` is the kind of thing that is edited in the wrong place. It
   costs about 6 mentions of `station_client.py` as a command, 6 of `setup_database.py` and
   1 of `cleanup.py` in the docs, scripts, unit and Dockerfile (counted with a search, not
   read one by one).
3. **`pyproject.toml` as description only** (above), or as the way to install.
   **Recommended: description only.**
4. **`templates/`, `static/`, `ocr/`: stay, or go beside the code?** They are code too.
   Recommended was: stay, as part of decision 1's later step. **Decided: they move** -
   `templates/` and `static/` into `src/server/` (only the server uses them), `ocr/` to
   `src/ocr/` (beside `card_ocr.py`, which is not part of `server/`). The top folder then
   holds no code but `app.py`.
5. **Do the documents write `src/inventory.py` or `inventory.py`?** CLAUDE.md names Python
   files 94 times, PROGRAM_DOCUMENTATION.md 88, the README 29, the Android repository in two
   tables. **Recommended: bare names stay** - a module's name is what one searches for -
   with one sentence where each document introduces the code ("the Python modules are in
   `src/`"), and full paths only in commands and in the README's *Project structure*.

## How it is checked

| Check | What it catches | When |
|---|---|---|
| **A paths test** (new, `tests/test_paths.py`): `Config.DATA_DIR`, `IMAGES_DIR`, `STATION_CAPTURES_DIR`, the loaded `config.yaml` and `server.ROOT` are in the folder that holds `app.py`; Flask's template folder holds `scanner.html` and its static folder `js/scanner.js`; `card_ocr.OCR_DIR` holds `server.mjs`; `station_setup.PAGE_FILE` exists | the empty-data mistake, in every form; pages or an OCR reader that are looked for where they no longer are | written first, against today's code - it says "is there", not "is in this folder", so the same test must pass before and after |
| **No Python file but `app.py` in the top folder** (in the same test, from `git ls-files`) | a module left behind, or added there later out of habit | every commit |
| **The 189 tests** (182 and the paths test's 7), the surface test among them | a module that is no longer found | after the move |
| **Undefined names** (pyflakes on `src/`) | - | after the move. pyflakes is not in the venv: installed for this, as for the split |
| **The recorded walk** (`scripts/walk_server.py`), before and after on fresh copies of the laptop's test data | a route that answers differently; a server on the wrong data folder answers with different numbers at the first step | before, after |
| **`docker compose ... config`** for the four combinations of files | a volume or build path that moved | before, after: the output must be identical |
| **Each image imports its program**: `docker run --rm --entrypoint python <image> -c "import station_client, config; print(config.__file__)"` (`import app` for the server) | a file missing from a `COPY` list - at build time, not on a station; and `config` coming from anywhere but `/app/src` | after every build |
| **The pages in a headless browser**: the three pages, no script errors, and no request answered 404 (the font, Socket.IO's client, the styles, the icons) | templates or static files not found - with decision 4 this is no longer a formality | after |
| **OCR reads a card** on a local server: a recorded capture uploaded, `light-ocr ready` in `data/logs/ai.log`, the card read by `light-ocr` in `scanned_cards.log` | the reader not found in its new folder - which the server reports as "OCR not installed" and carries on without | after, without Docker and in the image |
| **A station against a local server**, as on 2026-10-12 (the Pi 4 through a tunnel, on copies of the test data): connect, live view, a capture; **the setup page's Save**, which restarts the station by running its own command line again | the station's path through `src/`; a restart that no longer finds the program | before deploying |

Never on the real server's data: the walk and the station test run on copies, as before.

## Phases

One commit each for 0, 1 and 3; the move itself (1) is one step, because a half-moved tree
imports nothing.

### 0. Prepare (no behaviour change)

- `src/paths.py` cannot exist yet, so: write the paths test against today's layout and see
  it pass. Record the walk and the four `docker compose config` outputs.
- Decide the five points above.
- Agree a time when no other session edits the code: every file is renamed, and a change
  made meanwhile would have to be carried over by hand (the split's risk 1, unchanged).

**Done on 2026-10-12, all but the last point:**

- **`tests/test_paths.py`**: 7 tests, passing on today's layout; the one about the top
  folder holding no Python file but `app.py` is skipped until there is a `src/`. The file
  looks for the code in `src/` when that exists and beside `app.py` otherwise, so it is
  the same file before and after.
- **That it catches the mistake** was tried on a throwaway copy of the project with the
  files moved and nothing repaired: every test stops at once (`config.yaml` not found).
  With only that line repaired - the state in which a server would start on an empty
  `src/data` - four tests fail: the data folders, `server.ROOT`, the pages, the OCR reader.
- **The walk**: 73 steps, with a recorded picture of a card uploaded as a station does
  (read by `light-ocr`, added, the same capture sent again counted once). Two walks, each
  on a server of its own from a fresh copy of the code and of the laptop's test data, port
  5001: no step different. `scripts/walk_server.py` needed one addition first: a capture's
  `sync_key`, which did not exist when the script was written, is random and now counted
  among what differs from run to run.
- **Docker Compose**: the output of `config` kept for the four combinations of files and
  for `compose.yaml` in the top folder (which gives exactly the server's). The laptop has no
  `.env`, so this is the files' own defaults; on the server the same comparison is part of
  the deployment (`config | grep source:`).
- **Where the recordings are**: outside the repository, in this session's temporary
  folder - they hold the laptop's paths and test data and are not worth keeping. In phase 1
  the "before" is made again from the commit before the move (a second checkout of it),
  on the same fresh copy of the data as the "after", minutes apart: test data that changed
  in between cannot then look like a difference.
- One thing learned for that: a server started in the background by a script ignores
  Ctrl+C's signal, and the next "fresh" server then fails to start on the taken port while
  the walk happily talks to the old one. So: the port is seen free before each server is
  started and after it is stopped, and a walk only counts when that server's `ai.log` says
  `light-ocr ready` - a server without its OCR reader reads the card another way.

### 1. The move

- `git mv` of 28 files and the folders `server/` and `games/` into `src/` (`app.py`
  stays), `station_setup.html` with them; `templates/` and `static/` into `src/server/`
  (17 files), `ocr/` to `src/ocr/` (3 files, and by hand the installed `node_modules`).
- `server/state.py`: Flask's `root_path`; `card_ocr.py`: `OCR_DIR`; `config.py`: the two
  unused folder names.
- `.dockerignore`: `src/ocr/node_modules/`. Looked at for other paths that moved: no other
  line of it names a Python file or one of the three folders.
- `src/paths.py`; `config.py`, `config_loader.py` and `server/__init__.py` take the top
  folder from it. `app.py` puts `src/` on the path; `server/__init__.py` no longer puts
  anything there.
- The path line of the 7 test files and of `scripts/benchmark_db.py`,
  `benchmark_station.py`; `tests/test_app_surface.py` imports `app` from the top folder and
  needs both folders.
- `deploy/Dockerfile`: `ENV PYTHONPATH=/app/src` in all three targets; the station targets
  copy to `src/` the nine files they copy today and `paths.py`, `config.yaml` to `/app`,
  and start `src/station_client.py`. The server's `COPY . .` and the entrypoint's
  path stay; the OCR stage and the `node_modules` line get the new folder.
- `scripts/docker-entrypoint.sh`, `start.sh`, `deploy.sh`, `mtg-scanner-station.service`:
  the commands of decision 2 and `PYTHONPATH` for the `python -c` lines; in `deploy.sh`
  also the folder `npm install` runs in.
- `pyproject.toml`.

Then every check of the table up to the image check, on the laptop.

### 2. With a station, on copies

The Pi 4's `client-pi` image can only be built on a Pi. Built there from the moved tree and
pointed at a local server on copies of the test data, as on 2026-10-12, then put back.
The `client` image is built and import-checked on the laptop.

### 3. Documentation

CLAUDE.md (*Common Commands*, the rule about paths, the rule that a new station module goes
into the `COPY` lines, *Where Things Live* by decision 5), PROGRAM_DOCUMENTATION.md, the
README's commands and *Project structure*, INSTALL.md (`npm install` in `src/ocr/`),
TEST_CASES.md (ONE-03 runs `app.py`), the pages' files by the same rule as the modules -
`scanner.js`, `templates/scanner.html`, `static/fonts/` stay as they are written (CLAUDE.md
names `static/` 5 times and `templates/` 3, PROGRAM_DOCUMENTATION.md 6 and 5), with the
sentence that says where they are - the message `setup_database.py` prints, this plan's *How
it went*. In the Android repository: the two reference scripts' path line (`'src'`), and the
paths in its README and CLAUDE.md - a commit there.

### 4. Deploy

Four machines, each a copy by rsync. For each: the files that left the top folder are
**deleted there first** - their list is `git diff --no-renames --diff-filter=D --name-only
<before> <after>` - and then the tree is copied and built as always. The list holds only
files Git knows: the old `templates/`, `static/` and `ocr/` folders are then empty (on a
machine without Docker `ocr/` still holds `node_modules`) and are removed by name.

- **The server** (192.168.1.23, the real collection): a backup first; build before `up`,
  so the old container serves meanwhile; `... config | grep source:` must show
  `~/scanner-server/data`; afterwards `/api/stats` gives the numbers it gave before, the log
  says `light-ocr ready (webgpu)`, the three pages open with their styles, and inside the
  container `config.__file__` is under `/app/src`, no `.py` but `app.py` is in `/app`, and
  `/app` has no `templates`, `static` or `ocr`.
- **The stations** (Pi 5, Pi 4, the laptop's stopped container): rebuilt, each seen
  connected on the cameras page with its name and settings, the Pi 4's crop still in place.
  A station that is not updated keeps working - it runs its old image and talks to the
  server as before - so they need not be done the same hour.

## Risks and open points

1. **A server on an empty data folder.** The reason for the paths test, the `config` check
   on the compose files and the `/api/stats` comparison. The real data would not be
   damaged by it - an empty `src/data` would be created beside it - but cards scanned in
   that state would land in the wrong place.
2. **Old files left on a machine.** See above; the image check prints where `config` came
   from for exactly this.
3. **A machine installed with `scripts/deploy.sh --service`.** Its station unit names
   `station_client.py` in the top folder. Read in the script: `--update` without
   `--service` pulls the code and *restarts* the units that are installed - it writes a
   unit only for a machine that has none for its camera yet. So after this move a plain
   update would restart a station unit that starts a file which is no longer there.
   `deploy.sh` has to write both units again on every update (a change of phase 1, with
   the unit's new command); to be tried in a container or a VM, not only read. No machine
   of this installation is known to run that way; a user of the published repository may.
4. **The station's restart.** Saving on the setup page runs the station's own command line
   again (`os.execv`). With `src/station_client.py` as a path relative to the working
   folder this should work as today; it is on the list for phase 2 because it is easy to
   assume.
5. **Pages cached in browsers.** The addresses do not change and neither do the files'
   contents, so nothing has to be fetched again and no `?v=N` needs raising. Assumed from
   how Flask serves `static/`, to be seen in the headless browser.
6. **OCR that is quietly off.** A server that does not find the reader reads every card
   with the vision AI instead: slower, and it costs requests, but nothing looks broken.
   Hence the check that a card is read *by `light-ocr`*, not only that it is read.
7. **`python -c` from the top folder** finds modules in the current folder before those of
   `PYTHONPATH`. Harmless once the top folder holds only `app.py` - which is why "no other
   Python file there" is a test and not a habit.
8. **Not looked at:** how other people's checkouts of the public repository are run. The
   README's commands change with decision 2 and the folder of `npm install` with decision 4;
   that belongs in the commit message and the
   README's first lines about updating.

## What this does not do

- It does not give the code a package name or sort the modules into server / station /
  shared folders (decision 1). Only what is plainly the server's - its pages - goes into
  `server/` now.
- It does not make the data folder a setting. The top folder is still found from the code;
  a server on other data is still a copy of the checkout, as `walk_server.py` describes.
  With `paths.TOP` in one place, that becomes a small change later.
- It does not touch what any module does, `scanner.py` included.

## Size

About 70 files renamed (50 of Python, 20 of pages and the OCR reader) and some 40 lines
changed in code (an estimate), 8 path lines in tests, the Dockerfile's four stages,
`.dockerignore`, three scripts and one unit, plus two new small files and one new test; then
the documents of two repositories and four machines to update. Smaller than the split in
code, larger in machines: the split touched only the server, this rebuilds every station.
## How it went (2026-10-12)

All phases are done and merged into `main` (both repositories): the server and the three
stations run the moved code.

Done as planned, with these differences:

- **`server.ROOT` stayed a name** (it is `paths.TOP`): `server/logs.py` uses it, and now takes
  it from `server` instead of `server.state`, which no longer needs it.
- **`Config.BASE_DIR`'s import of `Path` went** with the line that used it - the one edit that
  is not a path.
- **A station copied by hand into one flat folder no longer starts**: the code looks for
  `config.yaml` in the folder above its own. The plan had not seen that case; INSTALL.md
  described it for a Pi without Docker and now describes the two-folder layout.
- **`deploy.sh` also takes the OCR reader's installed packages along** from `ocr/` to
  `src/ocr/` instead of downloading 128 MB again.

Checked, on the laptop:

| | |
|---|---|
| All tests | 189 pass, none skipped: the top folder holds no Python file but `app.py` |
| Undefined names (pyflakes on `src/`, `tests/`, `scripts/`) | none |
| Renames | 70 files, by `git mv`; 16 files edited, 2 new (`src/paths.py`, `pyproject.toml`) |
| The recorded walk, 73 steps with a card's picture | a server from the commit before and one from the moved tree, twice each, on one fresh copy of the test data: four identical recordings |
| ... against the server **image** built from the moved tree, on a copy of the data | the same recording; `light-ocr ready (cpu)`, the card read by `light-ocr` |
| Inside that image | `config` from `/app/src`, data in `/app/data`, pages in `/app/src/server`, the OCR reader installed in `/app/src/ocr`, no `.py` in `/app` but `app.py` |
| `docker compose config`, four combinations and `compose.yaml` | identical to before the move |
| The three pages in headless Chromium | no script error, every file of the server's found (the one 404 is `/favicon.ico`, which the project never had) |
| The station **image** (`client`) against a local server | connected ("No camera": none was given to it), restarted from its setup page - the same container, its own command line run again - and connected again |
| `scripts/start.sh --no-station` on a copy | the server came up on the copy's data |

Not checked:

- a card *read* through a real camera on the moved code (the box was dark, see below);
- **`scripts/deploy.sh`**, neither a fresh installation nor `--update` on a machine with the
  services installed; `bash -n` only. And one thing the plan's risk 3 had not seen: the
  `--update` that crosses this change is still run by the *old* script (it is already
  running when `git pull` replaces it), so the new unit and the OCR packages only come with
  a second run of the script. INSTALL.md says to run it twice;
- **a server with a GPU**: the laptop has none, so OCR ran on the CPU there;
- other people's checkouts (INSTALL.md says what changes for them).

**Phase 2, with the Pi 4's camera, the same evening.** The `client-pi` image was built from
the moved tree on the Pi 5 (3 min 24 s; the user's choice, it is the faster machine), under
a name of its own beside the station running there, and carried to the Pi 4. In it: Python
3.13 from apt with picamera2 and OpenCV, `config` from `/app/src`, data in `/app/data`,
the ten files in `/app/src` - and none of the project's 32 module names is taken by that
system Python, which the plan had left unchecked.

On the Pi 4 the real station's container was stopped for about a minute and the new image
run in its place - with a volume of its own holding only the camera kind and the crop, so
neither the real station's settings nor its waiting captures were in reach - against a
server on the laptop from the moved tree, on a copy of the test data, through a tunnel:

| | |
|---|---|
| Connection, camera | connected, Camera Module 3 opened (`picamera`), no camera error |
| Live view through the server | 10 frames in 4 s, 14 after the restart (the station reported 6.4 a second) |
| A manual capture from a scanner page's side | taken, uploaded, received, sent to OCR and then to the AI: no card in a dark box, "could not identify" - the whole way, without a card to read |
| The setup page | logged in, state shown; its restart ran `src/station_client.py` again in the same container (no restart by Docker), the camera was closed and opened again, the station reconnected |

Afterwards: the test container, its volume and the test images removed on both Pis, the
real container started again - connected to the real server with its camera, its crop and
its server address as before, nothing waiting to be sent. Nothing was written to the real
server.

**Phase 4, the server (192.168.1.23), 22:25-22:30.** Idle at the time: no station
scanning, nothing in the scanned list or the review queue.

1. A full backup (`scripts/backup.sh`: the running server's own snapshot,
   `scanner-backup-20261012_222526.tar.gz`, 523 MB).
2. The list of files that left the top folder (70, from `git diff --no-renames
   --diff-filter=D --name-only`) compared with the server's folder *before* deleting: every
   one was there, and nothing else lay in `server/`, `games/`, `templates/`, `static/`,
   `ocr/` or as a `.py` in the top folder. Then deleted, the five folders with them.
3. The new tree copied; `docker compose ... config` showed `~/scanner-server/data` and
   `~/scanner-server/scanned_cards` as before.
4. The image built while the old container served (19 s), and looked into before it was
   started: `config` from `/app/src`, the pages and the OCR reader found, no `.py` in
   `/app` but `app.py`, none of the old folders.
5. `up -d`: the server answered again after 5 seconds.

Afterwards: `/api/stats` for the collection and for the scanned cards the same as before
(5,407 cards, 2,151 entries), 15 decks, `light-ocr ready (webgpu)` in `ai.log`, both
connected stations back by themselves with their cameras, the three pages in headless
Chromium without a script error or a missing file, no error in the container's log.

Not seen yet on the real server: a card scanned through it since. And one thing done that
was not in the plan: after the start, Docker's untagged images were pruned there (15.8 GB)
- among them the server's image from before the move, so going back would mean building
it again from commit 71df153, not starting the old image.

**Phase 4, the stations, 22:32-22:36.** Both Pis idle, nothing waiting to be sent. On each,
the folder was compared with the new tree first: what lay there and is not in the tree
was exactly the 70 moved files (and the Pi 5's `.env`, which stays). Those deleted, the
tree copied, the image built while the old container ran and looked into before it was
started (`config` from `/app/src`, the setup page found), then the container replaced.

| Station | Image | After |
|---|---|---|
| Pi 5, USB webcam | `client`, built there in 24 s | connected as "Pi camera", the webcam opened by its `/dev/v4l/by-id` name, a card in view detected |
| Pi 4, Camera Module 3 | `client-pi`, built there in 5 s (only the copied files were new) | connected, the camera opened, its crop and its setup page's password in the volume as before |
| Laptop | `client` | rebuilt and the container created again with the settings it had; left not started, as it was |

So a USB webcam on the moved code is seen too. Still not seen, anywhere: a card *read*
through a camera since the move.

Found on the way, not the move's doing: `scripts/start.sh` and `scripts/backup.sh` are not
marked executable in Git, though the documents run them as `./scripts/start.sh`.
