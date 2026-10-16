# Plan: the outside code review of 2026-10-16

An outside review ("Review do scanner-server", a PDF, in Portuguese, written from a reading of
commit `55592a2` - nothing was run) found 17 things. This plan says which of them are real in
today's code, in what order to fix them, what each fix touches, and what needs a decision.

**Status (2026-10-16): Phase 1 is implemented and committed locally (six commits, tested, not pushed or
deployed); Phases 2 and 3 are not started.** Debug mode was adjusted at the person's request: it keeps
the request log everywhere and the interactive debugger on a server that only this computer can reach
(or with `SCANNER_LAN_DEBUGGER=1`), instead of removing the debugger.

 Every claim below was checked against the code of
2026-10-16 (`main` at `34fa73b`) before it went into the plan: all 17 are accurate as written.
The review also praises what the project does well (idempotent uploads, the two-phase "Add to
collection", the atomic card-data swap, CSRF and DNS-rebinding defences, the staged OCR → AI
pipeline); that is not repeated here.

The three themes of the review: *"anyone on the LAN is the owner" combined with a few dangerous
switches*; *station threads that fail silently*; *a few paths that grow with the collection*.

## What was confirmed

| # | Review item | Where (today) | Confirmed |
|---|---|---|---|
| 1 | Debug mode, switchable from the LAN, turns on Werkzeug's interactive debugger | `settings_events.py` `toggle_debug_mode`; `startup.py:335` `socketio.run(debug=services.debug_mode_running, allow_unsafe_werkzeug=True)` | yes (it was ON on the real server until the restart of 2026-10-15 21:51) |
| 2 | AI keys and the "local" endpoint can be changed by anyone on the LAN; the endpoint is only checked for `^https?://` | `ai_events.py` `save_ai_credential`, `api_keys.py:73`, `/api/local_ai_models` echoes the connection error | yes |
| 3 | A capture is lost silently when the disk write fails; a failed capture leaves `capture_pending` / `card_under_review` stuck | `scanner.py` `capture_card_image_only` (`cv2.imwrite` unchecked), `station_client.py` `auto_captured` (no `try`), `_run_focus_probe`, `_capture_frames` | yes |
| 4 | `backup.sh` makes a world-readable tar with `.env`, `api_keys.env`, `secret_key` | `scripts/backup.sh` (no `umask`, no `chmod`) | yes |
| 5 | The Scryfall download is held in memory whole (~1.5 GB peak) | `database.py` `download_scryfall_data`: chunks → `b''.join` → `decompress` → list of dicts | yes |
| 6 | The scheduled-backup fingerprint hashes every row every 10 min, under each manager's lock | `backups.py` `_fingerprint` (`repr` + SHA-1 of every row) | yes |
| 7 | Thumbnails are made inside the inventory transaction | `inventory.py` `add_card` (`@_writes`) → `_make_thumbnail` | yes |
| 8 | No retry for the AI providers: a 429 sends the card to manual review | `card_identifier.py` (`except Exception → (None, None)`) | yes |
| 9 | The scanner page polls without pausing in a hidden tab; the inventory modal rebuilds on every update | `scanner.js:127` (500 ms), `:1503`, `:1506` | yes |
| 10 | On the station, an empty frame is the most expensive (network, then the outline fallback; `copy()` of the annotated frame) | `object_detector.py`, `scanner.py` | yes (not measured) |
| 11 | No upload size limit (backup upload up to 8 GB, imports read whole into memory) | no `MAX_CONTENT_LENGTH` anywhere | yes |
| 12 | `onclick` built with `escapeHtml` does not protect a JS string (a `\` in a name breaks the handler); `escapeHtml(null)` breaks the card panel | `scanner.js` (three places) | yes |
| 13 | The station's send queue blocks at its head: any answer that is not accepted / refused (wrong token, a proxy's 404 page) is retried for ever and holds every later capture | `station_client.py` `send_until_settled` | yes |
| 14 | The OCR child process (Node, third-party code) inherits every API key | `card_ocr.py` `Popen` without `env=` | yes |
| 15 | Configuration comes from four sources, and `${X}/path` in `config.yaml` becomes only `X`'s value | `config_loader.py` `_substitute_env_vars` (`re.match`) | yes |
| 16 | Two monoliths: `scanner.js` (~1,600 lines now) and `station/scanner.py` (~1,900) | | yes |
| 17 | Unpinned dependencies, base images by tag, no `HEALTHCHECK` | `requirements*.txt`, `deploy/Dockerfile` | yes |

## What I would NOT do as the review suggests

- **Publish the server on 127.0.0.1 by default** (item 2's last suggestion). The camera stations,
  the phone and the other computers reach the server over the LAN: that is the product. The
  compose file should keep publishing on the network; what is added is an explicit
  `SCANNER_BIND` variable for a server that only one machine uses, and a sentence in
  INSTALL.md.
- **Replace the Werkzeug server with gunicorn / waitress** (item 1). It is a separate, larger
  change (Socket.IO's threading mode, the Docker image, the AppImage) for a risk that item 1's fix
  already removes. Worth doing one day; not part of this plan.
- **Split the two monoliths** (item 16) as a task of its own. Done only when a file is being
  changed anyway.

## The plan, in order

### Phase 1 - small and safe (one sitting; each one is its own commit)

1. **Never lose a capture to a failed write (item 3).** `capture_card_image_only` checks
   `cv2.imwrite`'s answer and returns `(None, None, None)` with a log line that says what failed;
   `auto_captured` runs inside `try / finally` that clears `capture_pending` and
   `card_under_review`; `_run_focus_probe` and `_capture_frames` get the same (the probe's
   `focus_probe_running` must not stay `True` if anything before its `try` raises; the release
   request must not be able to end the capture thread). Test: a scanner with a failing `imwrite`
   and a callback that raises - the flags end `False`, the next capture works.
   *Touches the stations (Pi 5, Pi 4, AppImage).*
2. **Debug mode: the request log, never the debugger (item 1).** The switch keeps its meaning for
   `data/logs/requests.log`; the server always starts with Flask's `debug=False` (no interactive
   debugger, no reloader). The switch's text says so. If someone really wants the debugger, an
   environment variable (`SCANNER_WERKZEUG_DEBUGGER=1`) allowed only when the server listens on
   loopback. Test: starting the server with the setting on does not enable the debugger.
   *Touches the server and the AppImage.*
3. **Backups are private (item 4).** `umask 077` at the top of `scripts/backup.sh`, `chmod 700` on
   the backup directory; the same for the archive the app makes (`backups.archive` /
   `add_archive`: zip files written 0600). Test on a temporary directory.
4. **`${X}/path` in `config.yaml` (item 15).** `re.sub` instead of `re.match`; a unit test. The
   rest of item 15 (four sources) is a documentation task: one table in the docs saying which
   source wins - done with this commit.
5. **The OCR sidecar does not get the API keys (item 14).** `env=` with the keys removed
   (everything named `*_API_KEY`, `LOCAL_AI_ENDPOINT`); test that the child's environment has none,
   and that OCR still starts (`light-ocr ready (webgpu)` in the log after a deploy).
6. **Upload limits (item 11).** `MAX_CONTENT_LENGTH` of 64 MB by default (a capture is at most
   30 MB, a CSV a few MB); the backup upload route raises it for itself (it is meant for up to 8
   GB). Test: a larger body to another route is refused with 413; the backup route still takes one.

### Phase 2 - a day, in this order

7. **The AI endpoint and the credential events (item 2).** First, what costs little: the local
   endpoint must be `http(s)` and its host must be loopback, a private address (10/8, 172.16/12,
   192.168/16, link-local), `localhost`, a single-label or `.local` / `.lan` name - a public address
   is refused unless `ALLOW_PUBLIC_AI_ENDPOINT=1`; `/api/local_ai_models` answers a plain "could not
   reach it" without the exception text. (Your Ollama at 192.168.1.23 stays allowed.) Second,
   **decision needed** (see below): whether sensitive events (keys, endpoint, restart, debug mode,
   restore) should need a password.
8. **A retry for the AI providers (item 8).** One helper in `card_identifier.py`: up to 3 tries with
   back-off (1 s, 3 s) for 429, 5xx and timeouts, never for 4xx; the three `_ask_*` functions that
   build the same payload share it. Test with a fake provider that fails twice. Fewer cards to
   review during a batch with a cloud provider.
9. **The backup fingerprint by a change counter (item 6).** Each manager counts its own commits
   (`_committed`) and the fingerprint is only taken when a counter moved; the hash stays as the
   fallback after a restart. **Measure first** (on a copy of the real database): how long the
   fingerprint takes today with 7,800 cards - if it is a few milliseconds, this drops in priority.
10. **Streaming the card data download (item 5).** `gzip.GzipFile(fileobj=response.raw)` and the
    JSONL read line by line into `executemany` batches of 5,000; the swap (`replace_table`) and its
    "refuse a file under half the size" check unchanged. Test against a small generated JSONL; a
    full download on a copy of the data folder to compare the peak memory and the row count with the
    old path. *Matters most for a Pi or a small host; the current server has the room.*
11. **The scanner page's timers and the modal (item 9).** `visibilitychange` pauses the 500 ms
    detection poll, the 5 s stats and the 15 s link while the tab is hidden (and one request at a
    time: no new one while the last is not back); the inventory modal updates the changed row
    instead of rebuilding all of them. Check in a headless browser: no request while hidden.
12. **Inline handlers (item 12).** The three `onclick="...('${escapeHtml(name)}')"` become
    `data-*` attributes and one delegated listener (the pattern `collection.js` has);
    `escapeHtml` takes `String(text ?? '')`. A card named `A\B` and one named `It's` in a test page.
13. **The station's send queue (item 13).** After N (say 20) failed tries *with a server answer that
    is neither accepted nor refused* the capture is parked ("held", as the ones for another server
    already are) and shown on the setup page; a network error (server away) keeps retrying for ever
    - that is "at least once". Test with a fake server that answers 404 HTML.
14. **A thumbnail outside the transaction (item 7).** `add_card` makes the thumbnail first and
    passes the file name into the transaction, deleting the file if it fails. **Measure first**: how
    long the lock is held today with a real capture (if it is ~30 ms every 2 s the benefit is small).

### Phase 3 - decisions, measurements, later

15. **Station idle cost (item 10).** Measure the Pi 4's and the Pi 5's CPU with the box empty; only
    if it matters, run the outline fallback every Nth frame when the network answers "no card" with
    a high score. This changes detection behaviour: needs the same before/after scoring we used for
    the corner network.
16. **Dependencies and the image (item 17).** A constraints file written from the images that work
    today (`pip freeze` inside the built server and client images), used by the Dockerfile; a
    `HEALTHCHECK` that asks `/api/stats`; base images by digest. Done once, after the rest, so the
    lock describes the final code.
17. **The monoliths (item 16).** Not scheduled.

## Decisions needed from you

1. **Debug mode (step 2):** remove the debugger for good (my recommendation), or keep an
   environment-only switch for loopback?
2. **A password for sensitive actions (step 7):** the web pages have no login by design; the
   review's strongest point is that this makes keys, the AI endpoint, restart, debug mode and
   backup restore changeable by anyone on the network. Options: (a) leave it, and only do the
   cheap validation; (b) an optional **settings password** (off by default; when set, those
   actions need it - one field on the Settings → Server page; cameras and scanning stay open);
   (c) a login for everything (a large change - the phone app and the stations would need it).
   I would do (b) after Phase 1.
3. **The compose file's port (item 2):** keep it published on the network and add `SCANNER_BIND`
   (my recommendation), or bind to loopback by default?
4. **Where to start:** Phase 1 in one go (six small commits, deployed together), or step 1 first
   because it is the only item that can lose a card?

## How each step is checked and shipped

Each step: unit tests (temporary data, never the real server's `data/`), the full suite, a headless
page check where a page is touched, then the usual deploy (backup first, `light-ocr ready (webgpu)`,
collection and decks identical before and after). Station steps go to the Pi 5 first and are
tried on a run before the Pi 4 and the AppImage. Nothing here is committed until you say so.
