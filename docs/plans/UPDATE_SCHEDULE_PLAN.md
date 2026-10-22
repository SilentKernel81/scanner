# Plan: automatic updates of the card data and the picture index (2026-10-22)

**Assumption:** the two components updated by hand today are the **card data** (Settings > Collection and data >
Card data > *Update card database*: `server/card_data.py` `start_card_data_update`) and the **picture index**
(Settings > Card reading > Picture > *Update the picture index*: `start_index_update`). Backups already have a
schedule on the same tab; this plan gives the two updates one too, built the same way.

**Status:** phase 1 done (2026-10-22: `card_data.py` `run_update_schedule`, `scheduled_card_data_updates`, `card_data_due`; `config.yaml` `database.auto_update_every_hours`) - tried only with stand-ins, never on a running server. Phase 2 done the same day (`index_due`, `scheduled_index_update`, `start_index_update(on_done=)`; `config.yaml` `reading.index_retry_every_hours`; one rule added: no automatic build of an index covering less than 90% of the printings - `INDEX_AUTO_MIN_COVERAGE`, a stopped first build stays the button's; the `refused` case is covered by `missing > 0`). Phase 3 done the same day (`GET` / `POST /api/updates/schedule`, the POST `@guarded`; `parse_update_schedule`, `update_schedule_state`; a select and a "last / next" line on the Card data card and in Card reading > Picture; times sent with the server's offset; the refusal worded for the schedule) - tried in a server on copied data and a headless browser (the routes, the page's two cards); a run of the schedule itself never watched. Phase 4 done the same day: `tests/test_update_schedule.py` (33 tests: both due rules, the back-off across a restart, every game with data, the end of a download told once, the index's gates, config.yaml against the person's choice, and `schedule_tick` - one tick of the loop, taken out of it so each step's failure can be tested: before, a failing update check skipped that tick's download). Phase 5 done the same day: PROGRAM_DOCUMENTATION (*Automatic updates* under Card data, the background tasks, the guarded list), FEATURES.md, TEST_CASES.md (DBR-13 to DBR-15), CLAUDE.md (Where Things Live). Deployed 2026-10-22 09:39 (off by default). First live run the same morning on copied data: the automatic download at 09:41:20, 10 minutes after start, then the index extended itself (3,592 new pictures) - see PROGRAM_DOCUMENTATION. Nothing here was measured.

## What is there today

| | Card data | Picture index |
|---|---|---|
| Started by | the button / the `update_database` event; the first download by itself (retried with `FIRST_DOWNLOAD_RETRIES`) | the button (`POST /api/image_index/update`, `@guarded`); and after a successful card data download when the switch *Update the index after a card data update* is on (`extend_index_after_update`, default on) |
| Never | started without the user once data exists (PROGRAM_DOCUMENTATION, *Update check*: "Updates are never started without the user") | a first build (hours, ~12 GB) |
| Already running | `data_updates_running` refuses a second | `index_state['running']` refuses a second; a CLI build raises `BuildRunning` |
| Safe while scanning | yes: staging table + one-transaction swap (`replace_table`), old cards kept on failure | yes: a new generation published by one manifest rename; readers follow it |
| How fresh it is | `card_data_info.downloaded_at` / `source_updated` | `ImageIndex.built`; `index_status()['missing']` |
| Check | `run_update_checks`: 10 s after start, then `sleep(24 h)` - resets on every restart; only a notice after `update_after_days` (7) | none |

The backup schedule (`backups_api.run_backup_schedule`) is the model: a thread that **asks every 10 minutes
whether something is due**, where due means "the last one is `every_hours` old or older". That survives a
restart for free - a laptop, an AppImage or a Pi that was off at "3 a.m." catches up when it runs again -
which a cron-like "at this time" would not.

## The design

### One loop, two questions

A new module-level loop in `server/card_data.py` (`run_update_schedule`, replacing `run_update_checks`), ticking
every 10 minutes, first tick ~5 minutes after start (a Pi or laptop whose network is not up yet - the reason
`FIRST_DOWNLOAD_RETRIES` exists). Each tick, inside one `try/except` that logs and carries on (as the backup
loop does - a failed automatic run must never end the thread):

1. **The update check** (what `run_update_checks` did): once a day, by its own timestamp - the notice and its
   7-day rule stay exactly as they are.
2. **Card data due?** For each game with data (`games.all_games()`, as the check does - not only the active one):
   due when the schedule is on, `downloaded_at` is `every_hours` old or older, **and** the last automatic
   attempt is at least an hour old (else a download that keeps failing - Scryfall down - would be tried
   every 10 minutes). Then `start_card_data_update(game)` (no `retry`: that is the first download's only).
   `data_updates_running` already makes a running one a skip.
3. **Picture index due?** Only when its schedule is on, an index exists (`printing_check.index() is not None` -
   **never a first build**), no build is running, and either the last build ended `refused` (Scryfall's rate
   limit: the code already says "to be tried again later") or `index_status()['missing'] > 0`; and the last
   automatic attempt is `every_hours` old. Then `start_index_update()`. `BuildRunning` (a build started from the
   command line) is a skip, not an error.

### The interval is the interval

`check_for_update` calls data outdated only after `update_after_days` (7), because Scryfall republishes every
day for prices. The schedule does **not** go through that check: "every day" means a download every day,
since fresh prices are the reason to pick it. The notice keeps its 7-day rule for people with the schedule
off.

### The index follows the card data

The index is already extended after every successful card data download (the switch). So its own schedule
is mostly the **retry**: the build that Scryfall refused half-way, the printings still missing. One control,
not a second independent "every N hours" that would duplicate the chain:

- *Picture index*: `Off` / `Retry what is missing every 6 h / 24 h / week`.

### Settings (data/settings.json, defaults in config.yaml)

| Key | Values | Default |
|---|---|---|
| `card_data_every_hours` | `0` (off), `24`, `72`, `168`, `720` | `0` - **off**: updating without the user is new and must be chosen |
| `image_index_every_hours` | `0`, `6`, `24`, `168` | `0` |
| `card_data_auto_last` | `{game: {at, result}}` - the last automatic attempt and how it ended | - |
| `image_index_auto_last` | `{at, result}` | - |

The last-attempt keys go in `app_settings` (not module state like `index_state`) because they must survive a
restart - the one-hour back-off and "next due" would otherwise start again at every start. A validator like
`backups.parse_schedule` (every value checked before any is saved; booleans and unknown intervals refused).
`config.yaml` gets `database.auto_update_every_hours: 0` and `reading.index_retry_every_hours: 0` (shipped
defaults only, no machine's values).

### The control

`POST /api/updates/schedule` in `card_data.py`, **`@guarded`**: turning on downloads the server makes on its own
is the same kind of decision as `image_index_update`, which is guarded (the backup schedule is not - an
inconsistency left as it is). `GET /api/updates/schedule` (or the fields added to `/api/image_index` and the
card data state) returns the schedule, the last automatic attempt and when the next is due.

On the page:

- *Card data* card: a `select` "Automatic update: Off / Every day / Every 3 days / Every week / Every month",
  under it "Last automatic update: <date> - <result>. Next: <date>."
- *Picture* card, beside the existing switch: a `select` "Retry what is missing: Off / every 6 h / day / week".

`settings.js`: load and save them (`api`), like `backup-every`; bump `?v=`.

## Things checked before writing code

- **`maintenance_barrier`**: the card data swap and the index publish do not take it today (it guards the
  inventories, backups and restores) and need not - both are their own atomic step. A scheduled download
  running while a scheduled backup is made touches different files.
- **The phone's index download** (`image_index_download`: current generation only, resumable): a scheduled
  rebuild publishes a new generation under a download in progress. That already happens with the button;
  what the phone then does (refused → starts the new generation?) is to be read in `index_for_download` and
  the app's downloader before phase 3, and written in the docs. **Done 2026-10-22** (after phase 5): read on both sides; the listing now says
  when the index is changing (`building`, `card_data_updating`) and the app waits then - PROGRAM_DOCUMENTATION, *What happens when the index
  changes during a download*.
- **The standalone mirror** gets this by merging `server/main`: generic server code, nothing behind
  `app_mode`, no edit in `../scanner`. The Android app is unaffected beyond the point above.

## Phases

**Phase 1 - the loop and the card data** (`server/card_data.py`, `server/startup.py` - the call that starts
`run_update_checks` - `config.py`, `config.yaml`)
- `update_schedule()` (reads + clamps the settings, like `backup_schedule`), `card_data_due(game, now)`,
  `run_update_schedule()` with the update check folded in; `now` passed in so tests need no clock.
- Remember each automatic attempt and its result (`database_update_complete` / error path of
  `start_card_data_update`: an `on_done` callback rather than parsing events).
- Code commented generously, as the rest of the file is.

**Phase 2 - the picture index retry** (`server/card_data.py`)
- `index_due(now)`: the gates above; the `refused` / `missing` rule; result remembered from `task`'s `finally`.

**Phase 3 - the route and the page** (`card_data.py`, `templates/settings.html`, `static/js/settings.js`)
- The two routes, `@guarded`; `tests/app_surface.json` re-recorded (`tests/test_app_surface.py --record`). No
  new module, so the list in `startup.py` does not change.

**Phase 4 - tests** (`tests/test_update_schedule.py`; temporary `app_settings`, `start_card_data_update` /
`start_index_update` stubbed to record calls, never real `data/`)
- off → nothing; due / not due by `downloaded_at`; catch-up after a "restart" (state only in settings);
  a failing download tried again only after the back-off; every game with data, none without;
  index: no index → never a build; `refused` → retried; `missing == 0` → nothing; running / `BuildRunning` → skip;
  the validator refuses booleans and unknown intervals and saves nothing then; the route is guarded.

**Phase 5 - docs**
- PROGRAM_DOCUMENTATION *Update check*: "Updates are never started without the user" → "... unless the
  automatic update is on (off by default)", the loop, the back-off, the index retry; the *Background tasks*
  row; the guarded list.
- FEATURES.md: a line, with what was never tried (a month-long run; a Pi whose network comes late).
- TEST_CASES.md: turn it on with a short interval on a copy of the data, watch one run and its "last / next".
- config.yaml comments.

## Left out on purpose (possible later steps)

- **A time of day** ("at 3 a.m."): the backup schedule has none, and both updates are safe while scanning.
  If wanted later: a "not before HH:00" window on top of the due rule, still catching up after a restart.
- **"Not while scanning"**: the swap is one transaction and the index readers follow the manifest; a
  download costs CPU and network on a Pi, which could be measured first before adding a rule.
- **Automatic first build of the index**: hours and ~12 GB - stays a button.
