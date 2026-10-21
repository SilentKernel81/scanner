# Deploy plan: the foil-marker network, the printing check and the lookup

Prepared 2026-10-20, updated the same day for phases 2 to 4 of the reading refactor (commit `883870b`, which is what this plan now deploys; `b9a4169` was phase 2, `70ecda7` phase 3). **Nothing in this plan has been run.** It covers the server only (`user@192.168.1.23`, `~/scanner-server`).
The camera stations (the Pis, the phone) are not touched: nothing in `station/` changed, and they reconnect by themselves. (One of the four modules the station images share, `config.py`, gained 13 lines of constants and no imports; it only matters if a station image is rebuilt later, which this plan does not do.)

## What goes live, and how risky each part is

| Part | Commit | What it does on the live server | Risk |
|---|---|---|---|
| Foil-marker network | `874520a` | **Changes a decision**: where the OCR left the ★/• unread, a small network reads it before the AI's foil check. Measured: on 400 held-out cards that exist in both finishes it answered 394 and all 394 were right (the other 6 are left to the AI); 15 of 15 on the Deadpool rescan | Low-medium: real behaviour change, never tried on live scans. Wrong finish on a card the OCR missed (the AI used to be asked) |
| Printing check (shadow) | `6a462ff` | Only writes `data/logs/printing_check.jsonl`; changes nothing | Low: one background thread, bounded queue, never raises |
| The lookup (shadow) | `a84dcb9` | Also log-only. For cards the AI handles, a second light-ocr process reads the strip | Low-medium: **a second OCR process on the GPU next to Ollama** (see "The question that needs an answer first") |
| Reading refactor, phase 2 | `b9a4169` | The lookup's decision moved into `core/reading/` (shown identical on 444 pictures), plus a new `finish` field in the shadow log (the finish the picture and strip would give, beside the server's own). Log-only; the shadow reader loads its own 310 KB foil network (OpenCV on the CPU) | Low: no schema, config key or route added; the extra work is one small network run per lookup capture |
| Reading mode `evidence` (phase 3), the Settings switch, the review screen's candidates (phase 4) | `70ecda7`, `27ebb48` | **Ships OFF** (`reading.mode: cascade` in the shipped `config.yaml`): the deployed server reads exactly as before. When switched on it replaces the vision AI for cards the OCR did not confirm | Off: the code is present **in the live path** (`identification.py` `_read_with_ocr` / `_ask_ai`, the routing in `captures.py`, `CONFIRMED_MATCHES`, `Game.card_by_printing`) but `cascade` never reaches it - the existing tests pass unedited and a test pins that; step 5's rehearsal still checks that ordinary captures are read and answered exactly as before. On: **a real behaviour change, never run on live scans** - a wrong add goes into the collection. **New since the first version of this plan: the Settings page can turn it on** (Settings > Card reading, with a confirmation) - anyone who can reach the server can, unless the settings password is set (the event is guarded); set one before it is reachable beyond the house, or ignore the section. Not part of this deploy's go-live; see "Trying the evidence mode" below; the picture-alone path inside it is a second, separate switch, also off |

Not in the server: `scripts/experiments/` (60 files) - the plan leaves them out of the copy. The new data artifacts are inert without the code: `data/image_index/`
(15 MB for the generation the manifest names; the folder also keeps the previous generation, about 29 MB in all - the rsync commands copy both) and `data/logs/printing_check.jsonl`.

**No data migration** (checked: no `CREATE TABLE`, `ALTER TABLE` or migration in the changes since `04cf82e`), **nothing deleted or moved away**
(so nothing to delete by hand on the server; `git diff --name-status` finds no deleted or renamed file). Counted on 2026-10-20 against `04cf82e` (the commit before this work), at `883870b`, the list the rsync commands send holds **47 files that differ**: 29 of code, config, pages and scripts and the rest tests and docs
(`CLAUDE.md` among them). New: `foil_marker.py`, `foil_net.onnx`, `image_index.py`, `printing_check.py`, the package `core/reading/` (`__init__.py`, `evidence.py`, `decide.py`), `scripts/build_image_index.py`,
`scripts/printing_check_report.py`, `scripts/check_review_page.py`. Edited: `identification.py`, `captures.py`, `startup.py`, `state.py`, `config.py`, `card_ocr.py`, `database.py`, `games/base.py`, `games/mtg.py`,
`config.yaml`, `.gitignore`, and - **the pages, new since the plan was first written** - `server/scan_events.py`, `server/settings_events.py`, `server/templates/scanner.html`, `server/templates/settings.html`,
`static/js/scanner.js`, `static/js/settings.js`, `static/css/style.css`. A server serves its templates from memory, so the container's restart picks the pages up; a browser may hold the old scripts for a moment
(their `?v=` numbers were raised). The changes since the plan first said "no new route" are one new guarded Socket.IO event (`set_reading_mode`) and a field in two existing answers (`/api/scan_settings`, `review_item`);
the recorded surface (`tests/app_surface.json`) has them. These counts are against the repository's history, not against the server's copy: step 1a is what says how the server differs.
The server image copies the whole tree (`COPY . .`), so the new package needs no line of its own in the Dockerfile; only the station images name modules.

## Decisions made (2026-10-20)

- **Lookup mode: `ai_cases`** (the owner's answer). It is the default already, so nothing to change.
- **Nothing is pushed or deployed until the reading refactor is finished** (phases 3 and 4 are being completed first, the Android part last); this plan waits for that, and will be re-checked
  against the final commit before it is used.
- Still open, below: excluding `scripts/experiments/`, rehearsing first, and timing.
- **The vision AI is the last step, by default (the owner, 2026-10-20):** `reading.mode` is `evidence` from the next deploy on - it turns itself on where no `reading_mode` is saved (production has none), and
  without the picture index (or the printing check) it is the cascade. Earlier parts of this plan, written when the mode was off by default, describe how it was first deployed (off) and how to turn it off again.

## Decisions I need from you

1. ~~**Lookup mode.**~~ Decided: `ai_cases` (see above).
2. **Exclude `scripts/experiments/` from the copy** (52 tracked files that would end up in the image; the commands below already leave them out, the new comparison scripts too). I recommend yes.
3. **Rehearse first** on a copy of the data (the plan's step 5), as for the earlier deploys that changed behaviour. I recommend yes: it is where the GPU question gets answered.
4. **When.** At a quiet moment: the container restarts (~20-40 s for the OCR models) and the stations queue captures meanwhile ("a station keeps scanning while the server is away").

## Facts that shape the plan

- **My notes of the server's state stop at 2026-10-15** (main at `003a0fe`), and that hash is not an ancestor of today's `main`: it cannot say what is on
  the server. The server's data folder has `others.db` and `settings_login.json` (features committed after 10-15), so it is newer than the notes.
  **So the plan starts by comparing checksums** (step 1) instead of assuming. If that shows much more than the files listed below, the server was behind: stop and decide.
- The container runs as **uid 1000** (`user: "${SCANNER_UID:-1000}"`); `HOME=/tmp`; `data/` is a volume. Files rsynced by `user` (uid 1000?) are readable and writable by it - step 1 checks.
- `config.yaml` is **baked into the image** (`COPY . .`): there is no environment switch for the new settings, so changing one means editing the file and rebuilding. The one exception is the reading mode: `"reading_mode"` in `data/settings.json` (a volume) wins over `config.yaml`, so it needs a restart and no rebuild.
- The deploy procedure is the one in my notes (`git ls-files | rsync -rlpt --checksum --files-from=-`, `build` before `up`, both GPU files, `-l` for the `deploy/.env` link).
  rsync does not delete; nothing needs deleting this time.

## The question that needs an answer first: a second OCR process next to Ollama

The lookup starts a second light-ocr process (own models, on the GPU through WebGPU, like the live one) on the first strip it needs and stops it after two idle minutes.
The server's RTX 4070 Ti SUPER has 16 GB, and Ollama keeps qwen3.5:9b-q8_0 resident (roughly 10 GB, not measured by me). Whether a second reader fits was **not measured**. If it
does not, it fails to start: the lookup then records no strip and goes by the picture alone (caught, logged, nothing else affected) - but the live OCR reader must not be the
one that is squeezed. The rehearsal (step 5) measures it: `nvidia-smi` before and while two readers run. If it is tight, set `printing_check.lookup: off` (rebuild) or give the
shadow reader the CPU (a small code change I can make: the OCR is ~0.7 s a card on a CPU, fine for 3% of the captures).

## Step 0 - decide the open points above (no commands)

## Step 1 - read-only checks on the server (nothing is changed)

Run from the repo on the laptop. `rsync --dry-run` changes nothing; the rest only reads.

```bash
cd ~/Projects/scanner-server
# a) what differs from the server's copy (expected: about the 47 files above; more = the server was behind)
git ls-files | grep -v "^data/\|^\.claude/\|^scripts/experiments/" | LC_ALL=C sort \
  | rsync -rlpt --checksum --dry-run --itemize-changes --files-from=- ./ user@192.168.1.23:scanner-server/ | grep -v "^\.d"
# b) does the server's folder hold files the repo no longer has? (rsync never deletes them; a stray .py in the top folder is imported ahead of src/)
ssh user@192.168.1.23 'cd ~/scanner-server && find . -path ./data -prune -o -path ./scanned_cards -prune -o -type f -print | LC_ALL=C sort' > /tmp/server_files.txt
git ls-files | LC_ALL=C sort > /tmp/repo_files.txt;  LC_ALL=C comm -23 /tmp/server_files.txt <(sed 's#^#./#' /tmp/repo_files.txt)   # on the server only
#    (expected: .env and a few runtime files; a stray .py or a moved file's old copy is what to look for)
# c) the server's state, to compare with after
ssh user@192.168.1.23 'df -h ~ | tail -1; nvidia-smi --query-gpu=memory.used,memory.total --format=csv; sudo docker ps --format "{{.Names}} {{.Image}} {{.Status}}"; sudo docker images mtg-scanner-server --format "{{.ID}} {{.CreatedAt}}"; ls -ld ~/scanner-server/data ~/scanner-server/data/logs'
curl -s http://192.168.1.23:5000/api/stats    # collection, scanned and deck numbers: write them down
```

(These are read-only; an earlier `ssh` of this kind was blocked by the safety check on this machine - if it happens again, run them yourself with `!` in the prompt.)

## Step 2 - backups (data and code)

```bash
ssh user@192.168.1.23 'cd ~/scanner-server && bash scripts/backup.sh'        # the data: ~/scanner-backups/scanner-backup-<time>.tar.gz
ssh user@192.168.1.23 'tar czf ~/scanner-backups/code-$(date +%Y%m%d_%H%M%S).tar.gz -C ~ --exclude=scanner-server/data --exclude=scanner-server/scanned_cards scanner-server'
```
The code backup is the way back: my notes say no previous image is kept after a rebuild. Write down the two file names.

## Step 3 - the code to a working folder (not the live one)

```bash
git ls-files | grep -v "^data/\|^\.claude/\|^scripts/experiments/" | rsync -rlpt --files-from=- ./ user@192.168.1.23:scanner-rehearsal/
```

## Step 4 - build the new image under its own tag (the live image is not replaced)

```bash
ssh user@192.168.1.23 'cd ~/scanner-rehearsal && sudo docker build -f deploy/Dockerfile --target server -t mtg-scanner-server:rehearsal .'
ssh user@192.168.1.23 'sudo docker run --rm --entrypoint python mtg-scanner-server:rehearsal -c "import app, card_scanner.config as c, card_scanner.core.foil_marker as f, card_scanner.core.printing_check, card_scanner.core.reading.decide; print(c.__file__, f.MODEL_FILE.exists())"'
#   expect: /app/src/card_scanner/config.py True
```

## Step 5 - rehearsal on a copy of the real data (port 5002, own data folder; nothing touches the live data)

```bash
# the backup of step 2 as the rehearsal's data. First look at how the archive is laid out: it must unpack to /tmp/rehearsal/data/ (the earlier rehearsals did; adjust -C if it does not)
ssh user@192.168.1.23 'tar tzf ~/scanner-backups/scanner-backup-<the one from step 2>.tar.gz | head -5'
ssh user@192.168.1.23 'mkdir -p /tmp/rehearsal/scanned_cards && tar xzf ~/scanner-backups/scanner-backup-<the one from step 2>.tar.gz -C /tmp/rehearsal/ && ls /tmp/rehearsal/data | head'
# the index goes into that data folder (15 MB, from the laptop; it exists now, so the copy can be made)
rsync -rt ~/Projects/scanner-server/data/image_index/ user@192.168.1.23:/tmp/rehearsal/data/image_index/
ssh user@192.168.1.23 'sudo docker run -d --name rehearsal -p 5002:5000 --gpus all -e NVIDIA_DRIVER_CAPABILITIES=all \
  --add-host host.docker.internal:host-gateway -e LOCAL_AI_ENDPOINT=http://host.docker.internal:11434/v1/chat/completions \
  -v /tmp/rehearsal/data:/app/data -v /tmp/rehearsal/scanned_cards:/app/scanned_cards mtg-scanner-server:rehearsal'
# test pictures: the 15 Deadpool captures (their versions are known) and 15 ordinary ones, from the laptop's experiment cache
rsync -rt ~/.cache/mtg-image-match/truth/rescan_cards/ user@192.168.1.23:/tmp/rehearsal/pictures/
```
Send them through the whole pipeline as a station would (a station id of its own, `foil_is_image=1`, the pictures are the finished cards):
```bash
for f in /tmp/rehearsal/pictures/phone-0240eb84_10[3-5]*.jpg; do
  curl -s -F image=@$f -F foil_is_image=1 -F capture_id=rehearsal-$(basename $f .jpg) -F wait=60 http://localhost:5002/api/stations/rehearsal/captures; echo; done
```
**What must be true before going on:**
- the log (`sudo docker logs rehearsal`) has `light-ocr ready (webgpu)` and `Printing check: image index loaded (112765 printings ...)`, and no warning about `foil_net.onnx`;
- the captures are answered (`added` or `review`); a Deadpool foil whose marker the full-card OCR misses gets its finish from the strip / the network (log: `Foil marker read by ...`);
- `sudo docker exec rehearsal python scripts/printing_check_report.py` shows a THE CHECK and a THE LOOKUP section, the Deadpool captures as disagreements of kind `other_card`;
- **GPU memory**: `nvidia-smi` before the rehearsal, with the live container and Ollama running, and while the lookup's second reader is up (`sudo docker top rehearsal | grep -c server.mjs` shows two; the slim image has no `ps` of its own):
  enough free memory left? the live container's OCR unaffected? Note the numbers;
- two minutes after the last capture the second reader is gone (one `server.mjs`);
- the lookup's records in `printing_check.jsonl` carry a `finish` field: `{printing, decision, source, symbol, net, server, same}`. Look at the Deadpool ones by eye:
  a foil-only printing should give `decision: foil, source: printing`; for a printing that exists both ways, `source` is `symbol` or `network`, and `same: false`
  (the server filed an unread marker as regular) is the case worth noting. This is the first time the field runs on real pictures;
- `curl` of `/api/stats` and `/api/decks` on port 5002 equal the live server's (the code did not change data);
- the time of the OCR stage in the log is as before (the lookup must not slow the live reader).

Clean up: `sudo docker rm -f rehearsal; sudo docker rmi mtg-scanner-server:rehearsal; rm -rf /tmp/rehearsal ~/scanner-rehearsal`.
If the GPU does not have room: decide between `lookup: off`, the shadow reader on the CPU, or `ocr.foil_marker` only. **Do not go to step 6 until the numbers are in.**

### Done locally first (2026-10-20), on a copy, without Docker

A server run from the checkout (`venv/bin/python app.py`) on a COPY of the card database and the image index, port 5012, the laptop's own Ollama (`qwen3.5:4b`) as the vision AI, and
`scripts/experiments/image_match/rehearse_local.py` sending 39 pictures as a station would (the 15 Deadpool captures, 12 OCR-confirmed, 12 that the AI had to read), first in `cascade`, then
switched to `evidence` through the Settings page's own event. **Passed**: no failed request and no traceback in either log; in `cascade` the server read exactly as before; in `evidence` every add with a known
truth was the right printing (23 of 23 and 24 of 24); 8 cards were read by `picture+strip` in 0.24-0.45 s (the AI takes 1.7-2.4 s); a card the AI had misread and sent to review was added right by the picture
(`The Arkenstone`); two Deadpool cards the AI had resolved to the wrong card (`Worn Powerstone`) were added in `cascade` and sent to review in `evidence` (the AI's answer checked against the picture);
`picture+strip` never appears in the shadow log; the `finish` field was written for the 40 lookups (11 say `same: false`: nine are Secret Lair cards the server had resolved to a wrong printing and so filed
as regular, two are a real conflict between the server's read of the marker and the strip's); the mode survived a restart; the Settings page shows the new section; a review item opens with its five
candidates, the right Deadpool printings first. **Not answered** (laptop, a 6 GB GPU and a 4B model): a second OCR process next to the server's Ollama model, the Docker image and its volumes, and the server's own
state - those are this plan's steps 1, 4 and 5. **Known and not fixed:** three Deadpool cards (`Lightning Bolt`, read as `Lightning` #042 and confirmed as `Lightning Strike`) are still added wrongly in `evidence`
mode - an OCR-confirmed card whose picture is another CARD is not caught, only another printing of the same card (PROGRAM_DOCUMENTATION.md); in `cascade` it is the same, plus the two `Worn Powerstone`.

## Step 6 - the real deploy

```bash
cd ~/Projects/scanner-server
# 1. the index first (harmless to the old code: it is only a folder it does not look at)
rsync -rt data/image_index/ user@192.168.1.23:scanner-server/data/image_index/
# 2. the code (no --delete; the same list as step 1a)
git ls-files | grep -v "^data/\|^\.claude/\|^scripts/experiments/" | rsync -rlpt --checksum --files-from=- ./ user@192.168.1.23:scanner-server/
# 3. on the server: look at where the volumes point, build while the old container serves, look into the image, then start
ssh user@192.168.1.23
cd ~/scanner-server
sudo docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.gpu.yml config | grep "source:"          # must be ~/scanner-server/data and scanned_cards
sudo docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.gpu.yml build
sudo docker run --rm --entrypoint python mtg-scanner-server -c "import app, card_scanner.config as c; print(c.__file__)"      # /app/src/card_scanner/config.py
sudo docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.gpu.yml up -d       # always BOTH files: without the GPU file the OCR runs on the CPU
```
Do not run `docker image prune` afterwards (it removed the previous image once, with 15.8 GB of others).

## Step 7 - after it is up (the checks, in this order)

1. `sudo docker logs mtg-scanner-server | head -60`, or `data/logs/app.log`: `light-ocr ready (webgpu)`, `Printing check: image index loaded (112765 printings ...)`, no traceback.
2. `/api/stats` and `/api/decks`: the numbers from step 1c (plus anything scanned meanwhile).
3. The stations reconnected (`pi`, `rpi-scanner`, the phone): the stations page.
4. One real scan of a foil card; its finish and, in the log, how it was read (`Foil marker read by ...` when the OCR missed the marker).
5. After some captures: `sudo docker exec mtg-scanner-server python scripts/printing_check_report.py`. The first lines show it works (cards looked at, agree / disagree).
6. `nvidia-smi` and `docker stats`: the memory and CPU are as the rehearsal showed.
7. The pages: Settings > Card reading shows "Read by the picture first (experimental)" **off**, with the image index's number of printings; open a review item (or scan a card that goes to review): with the index it shows "The picture looks like".

## The switches, if something misbehaves

| Problem | Switch | Needs |
|---|---|---|
| The printing check / the lookup (any problem) | `ssh ... 'mv ~/scanner-server/data/image_index ~/scanner-server/data/image_index.off'` | **Nothing else, at once**: without an index it does nothing (the next capture notices; no restart) |
| The foil network gives wrong finishes | `ocr.foil_marker: false` in `~/scanner-server/config.yaml`, then build + `up -d` | a rebuild (2-3 min); or the rollback below |
| The evidence mode misbehaves (a wrong printing added) | Settings > Card reading > turn "Read by picture and collector line first" off (the next card takes it), or `"reading_mode": "cascade"` in `data/settings.json` | **Nothing else, at once** - no rebuild, no restart; cards it added stay as added |
| The second OCR process is a problem | `printing_check.lookup: off` (same way), or the index switch above | a rebuild, or the index switch |

## Rollback

There is no data migration, so **a rollback is code only**: restore the code backup of step 2 (`tar xzf ~/scanner-backups/code-<time>.tar.gz -C ~`), `build`, `up -d` with both files.
The data backup is a precaution; the new `data/image_index/` and `data/logs/printing_check.jsonl` are inert and can stay.

## Trying the evidence mode (written when it was off by default; since 2026-10-20 it is the default, and this is how to turn it off or on)

The mode was off in the first deploy. To try it, on the **rehearsal container's own copy of the data** (never the live `data/`): turn on Settings > Card reading > "Read by picture and collector line first" on the
rehearsal's page (port 5002; or put `"reading_mode": "evidence"` into that copy's `data/settings.json` and restart it) - no rebuild either way. Send the Deadpool pictures and a dozen ordinary ones as in step 5, then check by eye: every `added` card is the
right printing (the Deadpool foil / confetti ones the picture's, not the printed line's), `picture+strip` shows in `data/logs/scanned_cards.log`, the cards it could not decide
went to the AI, and the time per card is not worse (the mode adds about 60 ms in the OCR worker on the laptop - **not measured next to Ollama on the server**). Switching it on for
the live server is the owner's decision after the exit criteria are met, with the same switch on the live Settings page (the next card takes it); **to switch it off again**, turn it off there (or set `cascade` in `data/settings.json`) - nothing else changed on disk (no schema, nothing to undo; cards it added stay as added).

## What happened (2026-10-20, the server's local time, CDT) - deployed

Main at `69ee17a` (its code is `883870b` plus this plan's own edits), evidence mode OFF, nothing pushed. **Step 1**: the server differed from the repo only in this work (24 changed, 23 new files; one harmless
leftover `stations_api.py.bak-20261017` in `src/`); `user` is uid 1000 there. **Step 2**: backups `~/scanner-backups/scanner-backup-20261020_161506.tar.gz` (2.8 GB) and `code-20261020_161717.tar.gz`.
**Steps 3-5**: the image built as `mtg-scanner-server:rehearsal` and imported correctly; the rehearsal ran on a copy of the real data (8,925 cards / 4,176 unique / $16,503.46 / 23 decks identical to production)
with the same 39 pictures as the local one, through the real `qwen3.5:9b-q8_0`: no errors; cascade read as before; evidence added 8 cards by picture and strip in 0.18-0.41 s and sent the two `Sol Ring` cards the
9B model also misreads (as `Worn Powerstone`) to review. **The GPU question is answered**: with the 9B model loaded, the production container's OCR reader and the rehearsal's TWO readers, the GPU peaked at
**11,188 MiB of 16,376** (5,188 MiB of headroom); the second reader costs about 350 MiB. **Step 6**: the index and the code copied (a second dry run showed 0 differences), `build`, the image checked, `up -d` with both
GPU files at 16:32:42 (production had been idle for over ten minutes). **Step 7**: `light-ocr ready (webgpu)` at 16:32:43, no errors, collection / decks / finish split / review identical (8,925 / 4,176 / $16,503.46 / 23 /
0), `pi` and `rpi-scanner` reconnected (the phone was not connected at the time), the Settings page shows the new section off, the image index loaded. **After**: the index extended inside the container to all 112,818
printings (53 new, 7 s; the server followed the new generation by itself). The rehearsal's container, image and data copy were removed. **Learned**: the real server has a settings password, so the Settings switch
(a guarded event) is refused without it - verified on production, which has no `reading_mode` key - and a rehearsal copy needs its `settings_login.json` deleted to be driven by a script; `/tmp` there is a 7 GB
RAM-backed filesystem (the backup was extracted under `~` instead, without the picture folders); `docker logs` does not show `light-ocr ready` (it is in `data/logs/app.log`); no previous image remains after a rebuild
(the way back is the code backup and a rebuild, as the notes said). Left alone: `nomad_homebox` is in a restart loop (not ours).

## Second deploy (2026-10-20, 18:50 CDT): the vision AI last by default

Main at `68f0643` (nothing pushed). Backups `scanner-backup-20261020_174228.tar.gz` and `code-20261020_174434.tar.gz` first. The comparison showed exactly this round's 18 files. Rehearsed on a copy extracted from that backup
(the settings lock deleted from the COPY, the index copied in): left alone, the server reported `evidence`, the 39 pictures were read as in the first server rehearsal (8 by picture and strip in 0.2-0.4 s, 16 by the 9B model, the two
`Sol Ring` cards to review), the picture index button on the copy finished at once with nothing missing and Stop was harmless, no tracebacks, `picture+strip` never in the shadow log. Then `build` + `up -d` with both GPU files at 18:50:14
(production idle: no capture in the previous ten minutes). After: `light-ocr ready (webgpu)` at 18:50:16, no errors, collection / decks / finish split / review identical (8,925 / 4,176 / $16,503.46 / 23 / 0), the mode `evidence`
with nothing saved, the index 112,818 of 112,818, the Settings page shows the new text and the Storage block, the guard refuses a mode change and an index build without the settings password (production still has no `reading_mode` key).
`rpi-scanner` reconnected at once; **the `pi` station was not connected: its machine (192.168.1.88) does not answer ping or SSH - it went off at 17:29:03, 80 minutes before this deploy**, so it is powered off or off the network (not
the server's doing; the other Pi, 192.168.1.199, is fine). The vision AI is now the last step on the real server; **nothing has been scanned in this mode there yet** - the first live evidence is `picture+strip` in `scanned_cards.log`.

## Third deploy (2026-10-20, 19:25 CDT): a floor of picture similarity for `strong` decisions

Main at `be7294d` (nothing pushed): `STRONG_MIN_SIMILARITY` 0.30 (a Codex review of the whole branch: a collector line naming any of the five nearest printings was enough for `strong`, even for a picture that looks
like nothing). Backups `scanner-backup-20261020_192118.tar.gz` and `code-20261020_192320.tar.gz`; the comparison showed exactly five files; rehearsed on a copy (the 39 pictures, the mode left at its default): 7 cards by picture
and strip instead of 8 - `Mystic Confluence`'s match is under the floor, so the AI read it, to the same card - every other outcome identical, no errors. Then `build` + `up -d` at 19:25:52 (production idle). After: `light-ocr ready (webgpu)`,
no errors, collection / decks / review identical (8,925 / 4,176 / $16,503.46 / 23 / 0), mode `evidence`, index 112,818 of 112,818, the page and the guard as before. The `pi` station (the Pi 5, 192.168.1.88) was still offline.

## Deploys 4 to 6 (2026-10-20, evening, CDT): the header, tokens, OCR off and Secret Lair, the download route

- **19:49:58, header** (`faafd86`): Settings stays in the right corner when the deck page hides its tabs (`.topbar-page[hidden] + .topbar-settings-link`), checked in headless Chromium; held until the server was quiet.
- **20:01:21, tokens** (`572e819`): `T 0001` over the parent set's code files a token under `t` + the code; the loader keeps tokens and emblems. Rehearsed: the same 39 outcomes. The tokens reached the card data only
  when the owner ran the card-data update and the index button afterwards (2,894 tokens and 141 emblems, 116,329 of 116,329 indexed, `Cadet tfra 1` among them).
- **21:37:34, OCR off lets the picture alone decide + the Secret Lair gate** (`18aaece`): a first rehearsal run was not comparable - the copy had inherited the owner's settings (OCR off, picture alone on: their test of the
  picture alone), so the copy's two keys were reset and it was run again with the shipped defaults: 3 differences in 39, the three `Lightning Strike` adds now reviews. **The owner's settings stayed** (`ocr_first: false`,
  `reading_accept_image_alone: true`): on the server, OCR is off and the picture alone may add a card.
- **22:07:03, the index download** (`d0f9850`): `GET /api/image_index/download[/<kind>]`, no rehearsal (read-only routes, no change to the reading). Backups `scanner-backup-20261020_220435.tar.gz`,
  `code-20261020_220650.tar.gz`. Checked on the live server with plain GETs: the three files (22.3 MB) match their sizes and SHA-256 (ETag equal), `Range` resume returns the right slice, a wrong generation / a
  table without its version are 409, an unknown kind 404, 116,329 ids = 116,329 hashes = rows of the table, none missing. Collection numbers identical before and after (8,925 / 4,176 / $16,503.46 / 23 decks).

## Deploy 7 (2026-10-20, 23:16 CDT): the foil network at a fixed place, behind three guards (`1fbade6`)

OCR off no longer sends the finish to the vision AI: the network reads the set line at its fixed place, behind the orientation, marker-layout (every tied printing) and AI-resolved-printing guards (six Codex rounds). Backups
`scanner-backup-20261020_231156.tar.gz`, `code-20261020_231403.tar.gz`. **Rehearsed in the owner's real configuration** (the copy inherited OCR off + picture alone on): the same 39 outcomes as the earlier run in it, finishes
included (0 differences); the median time of a picture-decided card 0.42 s -> 0.07 s (the 21 s maximum in both runs is the first AI call loading the model). Live afterwards: no errors, collection numbers identical (8,925 /
4,176 / $16,503.46 / 23 decks), reading `evidence` with OCR off and picture alone on (the owner's test, kept), the download route answering. Not tried live on a scan yet.

## After the deploy

- Update my notes of the server (they stop at 2026-10-15) with the date, the commit (`883870b`) and the backup names.
- **The index must be rebuilt after each card data update** (a new set's printings are `not_indexed` until then):
  `sudo docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.gpu.yml exec server python scripts/build_image_index.py` - it downloads only the new printings from
  Scryfall (10 requests a second at most), can be stopped and run again, and a second one at the same time is refused. The report's `not_indexed` count says when it is due.
  A timer for it is a later decision.
- Read `printing_check_report.py` now and then. The exit criteria for dropping the AI are in `docs/PROGRAM_DOCUMENTATION.md` ("Printing check"); at ~3% of captures, 300 AI-handled
  cards is weeks of normal scanning.
- The Android app and the standalone mirror (`../scanner`, by `git merge server/main`) are separate decisions and are not part of this deploy.

## What I could not verify from here

The server's current state (only step 1 can say), its free GPU memory, whether `user` is uid 1000 there, whether a second reader fits next to Ollama, that the image builds with these
changes (step 4 does it), and how the backup archive is laid out (step 5 looks).

Checked against the repository on 2026-10-20 (the claims above that are not in the list just given): `04cf82e` is an ancestor of `main` and `003a0fe` is not; no `CREATE TABLE`, `ALTER TABLE`,
migration or `storage.rebuild` in what was added since `04cf82e`; no file deleted or renamed since; the compose file's image (`mtg-scanner-server`), container, service (`server`), `user:` and
volumes, the Dockerfile's `HOME=/tmp`, `COPY . .` and `.dockerignore` (which leaves `data/` out, and `scripts/` in); the log texts the checks look for (`image index loaded`, `Foil marker read
by`); that `scanned_cards.log` carries the reader (`picture+strip`); that moving `data/image_index` away stops the lookup at once (`index()` follows the manifest's stat) and, with the evidence mode
on, falls back to the AI; the measured numbers quoted (docs/PROGRAM_DOCUMENTATION.md, docs/plans/IMAGE_MATCH_EXPERIMENT.md). Unverified and said so where they appear: Ollama's ~10 GB, the 20-40 s
start, "weeks" for 300 AI-handled cards (it depends on how much is scanned).
