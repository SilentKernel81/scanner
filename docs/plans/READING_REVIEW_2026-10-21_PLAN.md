# Review of the reading order (picture first, OCR in doubt, the AI last), and what to do about it

*Written 2026-10-21 by Claude Fable 5.1 after reading the code of commits b79f692 and aa45967 (and the modules they
build on) at f2f2ef8. Nothing of it is built yet. Each step is meant to be given to Opus / Sonnet as one task, in
order; each says how it is checked.*

**Baseline:** at f2f2ef8 the reading tests are green: `tests/test_reading_*.py` 122 tests, `tests/test_printing_check.py`
94, `tests/test_foil_marker.py` 43 (run by discovery, as in CLAUDE.md: `venv/bin/python -m unittest discover -s tests -p "test_reading_*.py"`).

## The verdict in short

The order itself is sound and it is where the measurements say the value is: the picture and the collector line settle most cards in
one small OCR request instead of a full-card one, the AI is behind two gates (asked last, answer checked against the picture), and
every rule that acts was measured on real pictures with 0 wrong. Nothing in the review found a card that would be *added wrong* by the
code as it stands. What the review did find falls in four groups:

1. **The same work is done two and three times per card** (the picture search up to three times, the strip's OCR twice, the full-card
   OCR twice on one path). All of it on the single OCR worker. Behaviour-neutral to fix, and it also mends one small blind spot (the
   foil marker of an upside-down OCR-confirmed card).
2. **The mode that acts is the one mode that writes nothing down.** The shadow log skips every card the picture decided (on purpose: it
   would compare the choice with itself), `scanned_cards.log` keeps only the reader's name, and the docs say "the collection itself is
   the test". The thresholds came from 20 wrong labels and the plan's own words say live evidence is to replace them - but in `evidence`
   mode no live evidence of the picture's decisions is collected. This is the most important gap, and the cheapest to close.
3. **The guards of the picture-alone path are spread over three layers** (`decide.py`: the lead; `PrintingCheck.live_read`: the two
   floors, `_names_unindexed`, `_names_unknown`; `Identification._read_by_picture`: `missing_printings`, `ocr_agrees`). The project's own
   rule is "the decision is a pure function on plain data, and the Android app passes the same files" - the guards are not in that
   function, so the app would have to rebuild them from prose.
4. **The docs contradict themselves about the picture alone** (on by default in one paragraph, "why it is OFF by default ... defaults to
   false" a few paragraphs later), and several docstrings describe the order before 2026-10-20.

Two workflow findings besides: a card data update does not extend the picture index, so the picture-alone path switches itself off after
every update until someone clicks (the banner says so, nothing does it); and the review screen already shows the five candidates but does
not record which one the person picks - a free label for exactly the cards the guards refused.

## Findings, each with its place

| # | Finding | Where | Kind |
|---|---|---|---|
| F1 | The picture search (`_search`: two fingerprints and two passes over the whole index, 18 ms median) runs once in `live_read`, again for an OCR-confirmed card in `contradicts` -> `check`, again with OCR off in `_read_foil_locally` -> `upright`, and again for an AI answer in `verdict_of` -> `check`. A card that ends with the AI has had three. | `identification.py:270, 238, 318, 345`; `printing_check.py:548, 428, 439` | waste |
| F2 | The strip's OCR is read in `live_read` (`read_strip_evidence`, with the set line's symbol and the network's probability) and, for an OCR-confirmed card whose marker the full-card OCR did not read, read **again** by `foil_marker.read` on the same flat card. One small OCR request (50-80 ms) per such card for an answer already in hand. And `foil_marker.read` reads the card *as given*, not turned upright as `live_read` did: an upside-down OCR-confirmed card gets `unknown` and the AI's check where `live_read`'s strip evidence had the answer. | `identification.py:233, 386`; `printing_check.py:556`; `foil_marker.py:182-183` | waste, small blind spot |
| F3 | With OCR on and the picture alone refused by `_ocr_names`, the full-card OCR is read in `_ocr_names` and read **again** in `_read_with_ocr` - the same image, the same reader, 0.1-0.16 s on a GPU, more on a CPU, and the first read's result is thrown away. | `identification.py:302, 367` | waste |
| F4 | `_names_unindexed` and `_names_unknown` look the same strip up twice (`get_card_by_set_number`). | `printing_check.py:580, 597` | trivial |
| F5 | No record of the picture's decisions: `observe` returns for readers `picture` / `picture+strip`; `scanned_cards.log` has the reader name only; the guards say nothing about *why* they refused. The exit criteria's evidence is collected in `cascade` mode only (docs say so). | `printing_check.py:693`; `captures.py: log_scanned_card` | observability |
| F6 | The review screen shows the five candidates (`handle_review_open`, `candidates`) and the person picks one (`select_printing`, then `add_to_inventory`) - nothing writes down "the picture's candidate N was right / none was", the one label that would recalibrate the guards. | `scan_events.py:115-131, 230-260` | observability |
| F7 | A card data update (`start_card_data_update` -> `database_update_complete`) does not start `start_index_update`; `missing_printings() > 0` then switches the picture-alone path off (silently but for the banner) until Settings > Card reading > Picture is clicked. | `card_data.py:50-56, 194` | workflow |
| F8 | The guards of the picture-alone path are in three places (see group 3); none is in `decide.py`, none has a case in the shared JSON files, and a refusal has no name. | `decide.py`; `printing_check.py:559-567, 572-600`; `identification.py:269, 277-284` | structure |
| F9 | `decide.FINISH_SURE` (0.9) duplicates `foil_marker.SURE` (0.9); nothing pins them equal. (`decide.py` may import nothing, so the pin is a test.) | `decide.py:28`; `foil_marker.py:59` | small |
| F10 | `missing_printings` reads `get_data_info('mtg')` - a game id written into `core/`, which the multi-game plan says `games.active()` owns. | `printing_check.py:349` | small |
| F11 | The AI's answer is looked up three times: `_resolved_prints_a_marker` (`confirmed_card`), `_checked_against_picture` (`identify`), then `search_and_emit_card` (`identify` again, with its log lines). | `identification.py:342, 469`; `captures.py: search_and_emit_card` | small waste |
| F12 | Docs and docstrings that are no longer true: PROGRAM_DOCUMENTATION.md "Residual risk, and why the picture alone is OFF by default ... defaults to **false** (config, `Config`, and the `live_read` signature)" (lines ~1602-1603) against "on by default" (1552, 1576) and `config.yaml` (`true`); the same section says a contradicted OCR read "goes to the picture (when the picture alone is on)" - the picture was asked first and has already declined; `identification.py` module docstring (14-19) and `_read_by_picture`'s (256-262) say the picture reads "a card the full-card OCR did not confirm"; `printing_check.py` header (2-4) says shadow mode is "the only mode there is for now"; `live_read`'s docstring (544) says the picture alone is "OFF unless asked for"; `verify_live_read.py --no-image-alone` help says "as shipped (the default)". | as listed | docs |
| F13 | `ocr_agrees` refuses 20 of 49 correct picture-alone decisions (41%) and prevented no error in the data (`confirm_cost.py`). Each refusal costs a full-card OCR, then the AI (or review with the AI off). Also: while the OCR reader is in its 60 s cool-down, every picture-alone decision is refused (`read_card` returns None -> not confirmed), so an OCR outage takes the picture path down with it. | `printing_check.py:142-177`; `identification.py:296-307` | measured cost, by design - see step 6 |

Not findings (looked at, fine): the one OCR worker and the decision against a worker pool; thread safety of the index, the network's lock, the
separate `StripReader`; the `printing_id` route (`card_by_printing`, tag `picture`); the handling of a switched game; failures in every picture step
falling back to what `cascade` does; the AI switch through `_ai()`; the station's synchronous route using `identify()` (it gets the same order).

## What not to change

- **Rule A2, `decide_printing`, `decide_finish` and the thresholds** (`AGREE_BELOW`, `IMAGE_LEAD`, `IMAGE_MIN_SIMILARITY`, `STRONG_MIN_SIMILARITY`,
  `DISAGREE_FROM`, the orientation and marker floors). They were measured; nothing here was. Step 2 is what will let them be re-measured on live scans.
- **The order** (picture, OCR in doubt, the AI last) and the two gates on the AI.
- **The fingerprint** and the index's layout and publication.
- **The single OCR worker.** If step 4 shows the worker is the bottleneck, the plan's own narrower fix (only the picture search off the worker) comes first - measured first.

## The steps

Each step is one commit (or a few), with its tests, run in this order. Steps 1-3 change no decision. Step 4 must be shown to change no
decision. Step 5 changes where a decision is written, not what it is. Step 6 is a list of things to *measure* with step 2's log before
anyone builds them.

### Step 1 - Make the docs and docstrings true (F12)

No behaviour. Fix every item of F12; in PROGRAM_DOCUMENTATION.md keep the *history* (the picture alone was off by default for part of
2026-10-20, and why) but say it as history, in one place, under what is true now. Update the `verify_live_read.py` help text (the flag stays:
`--no-image-alone` is still a useful measurement). Grep for other "OFF by default", "the only mode", "did not confirm" phrasings in
`identification.py`, `printing_check.py`, `config.yaml`, FEATURES.md, README.md.

Check: `grep -n "OFF by default\|only mode there is\|OFF unless asked" docs src config.yaml` finds nothing left that contradicts `config.yaml`.

### Step 2 - A reading log for the mode that acts, and the review's pick as a label (F5, F6)

**Why first:** everything below, and every threshold, is to be judged on live scans, and today the live mode writes nothing down.

**The log.** `data/logs/reading.jsonl`, one JSON line per capture, appended by `Identification` at the end of `identify` / the done path (one
place: wrap `_read_with_ocr` + `_ask_ai` in a small recorder object created per capture and written once; never raise, never block: a failed
write is a debug line). It is *not* the shadow log and does not replace it (the shadow log keeps measuring the AI's cards in `cascade`; this
one measures what `evidence` does). Each record:

- `time`, `station` (the desk's id), `capture` (file name, when known - the server path has it, `identify()` from the station route may not),
  `mode` (`cascade` / `evidence`), `ocr_on`, `ai_on`, `accept_image_alone`, `index_covers` (missing_printings == 0 / the count), `seconds` per step;
- `picture`: `rotated`, `candidates` (the five: id, set, number, similarity - the plain `Picture` evidence as `evidence.py` has it), `lead`;
- `strip`: `number`, `set`, `symbol`, `net` (rounded 4);
- `decision`: `decide_printing`'s answer (`path`, `printing_id`, `similarity`, `lead`);
- `refused`: `null`, or **the name of the guard** that stopped a decision: `image_alone_off`, `index_incomplete`, `below_image_floor`,
  `below_strong_floor`, `strip_names_unindexed`, `strip_names_unknown`, `ocr_did_not_confirm` (+ `ocr_name`, `ocr_line`), `card_not_in_data`;
- `ocr`: the full-card read when it happened (`name`, `collector_number`, `set_code`, `foil`), `confirmed`, `contradicted` (+ `why`);
- `ai`: `asked` (identify / foil / no), `answer` (name, number, set, foil), `verdict` (agree / weak / disagree / not_indexed / none);
- `result`: `reader`, `printing_id` or the name / number / set that went on, `foil`, and later (see below) `outcome` (`added` / `review` / `shown`).

The record of the capture's *outcome* is written by `captures.py` (`announce_and_route` knows `route_identified`'s answer): simplest is for the
recorder to be handed through `card_info` (a `reading` key the server pops before routing) and completed there; or two lines per capture joined
by the capture file name. Choose the first unless it complicates `card_info` for the stations (`ServerOutcome.parse` must not see it: pop it
before `outcome['read']` is built).

**The review's pick.** When a review item is added (`add_selected_card` with `desk().current_review_id` set), append one line
`{'review': True, 'capture': item's image, 'picked': card id, 'candidates': the ids the review screen showed (handle_review_open has them -
keep them on the desk with the open item), 'rank': the picked id's place among them or null}`. A rank of 0 says the picture's best match
was right and a guard refused it; `null` says the picture had it wrong - both are what the thresholds need.

**The report.** `scripts/reading_report.py` (the code in `core/reading/report.py`, like `printing_check.report`): how many captures, by mode
and configuration; decided by `picture` / `picture+strip` / OCR / AI / review; the refusals by guard name; the review picks by rank (how often
the best match was right when refused, by which guard - the cost of each guard in live numbers); median and 95th percentile seconds per
step; and a list of the captures to look at by eye: picture-alone adds under 0.6 similarity, strong decisions whose strip disagreed with the
full-card OCR, AI answers with verdict `weak`.

**Rotation and clean-up:** like `printing_check.jsonl` (`LOG_LIMIT`, `.1`); in no backup. Add the file to the Logs paragraph of CLAUDE.md and to
PROGRAM_DOCUMENTATION.md ("Logs" and the evidence section); FEATURES.md one line.

Checks: `tests/test_reading_log.py` (new; name in `tests/test_layout.py`'s `CORE` set for the new module): a decided card writes one record with
the candidates and no `refused`; each guard writes its name (drive `FakeEvidence` as `tests/test_reading_evidence.py` does); an AI answer with
`disagree` is recorded; a failing write loses no card; the review pick records the rank; `cascade` mode records too (so the two modes can be
compared). `tests/test_app_surface.py` unchanged (no new route or event). Rehearse on the rehearsal server with `rehearse_matrix.py`, then
run the report on its log: the numbers must match what the script printed.

### Step 3 - Extend the picture index after a card data update (F7)

In `card_data.py`, after a successful download (`database_update_complete`), call `start_index_update()` when there is a printing check and an
index already (never a first build unasked: that is hours and ~12 GB - the Settings button stays the way to start one). Behind a setting
`reading.extend_index_after_update: true` in `config.yaml` (`Config.READING_EXTEND_INDEX`), shown on the Card reading page's Picture card as a
checkbox beside the index button (guarded like the mode switch, remembered in `data/settings.json`). The banner (`showIndexNotice`) already
refreshes after an update; make it say "extending the index: N of M" while the build runs (the state is in `/api/image_index`).

Checks: `tests/test_index_update.py`: after the update task's success, with an index present and the setting on, the build is started once;
not started with the setting off, without an index, or when a build is running; a failed download starts nothing. Surface test re-recorded
if a new event is added (prefer extending `set_reading_mode`'s payload, as `accept_image_alone` was). Docs: the evidence section's paragraph on
`missing_printings`, FEATURES.md, the Settings paragraph.

### Step 4 - One body of evidence per capture; nothing read twice (F1, F2, F3, F4, F11)

**Shape.** A small class in `core/reading/` (say `capture.py: CaptureEvidence`), created once per capture in `_read_with_ocr` and carried on the
`Job` (beside `ocr_info`) into `_ask_ai`. It holds the two pictures and lazily computes and **caches**: `search` (the `_search` of the flat card),
`strip` (`read_strip_evidence` of the upright flat card - with or without a reader), `ocr` (the full-card `read_card` of `image`), the
`get_card_by_set_number` of the strip's line (one lookup, answering `_names_unindexed` and `_names_unknown` both). It imports `printing_check` for
the gathering (it is gathering, not deciding: `decide.py` stays pure).

**Then:**

- `PrintingCheck.live_read`, `contradicts`, `disagrees`, `verdict_of`, `upright` take an optional `search=` (as `check` and `lookup` already do) and
  `strip=` where they read one; `Identification` passes the evidence's.
- `_read_foil_locally` takes the strip evidence: with OCR on, the symbol and the network's probability are already there from `live_read`
  (same flat card, now *upright* - this mends F2's blind spot); `foil_marker.read` is called only when no strip evidence exists (cascade mode).
  `foil_marker.read` itself is not changed (its tests and the measured numbers stay).
- `_ocr_names` reads `evidence.ocr`; `_read_with_ocr` reads the same `evidence.ocr` - one `read_card` per capture.
- `_checked_against_picture` and `_resolved_prints_a_marker` resolve the AI's answer once (`confirmed_card` is `search_card_exact` + the
  confirmation; use it for both, keep `identify` in `search_and_emit_card` as it is - its tagging and logging belong to the route).

**It must change no decision.** The check is threefold:

1. the existing tests pass unedited (`test_reading_evidence.py`, `test_printing_check.py`, `test_foil_marker.py`, `test_reading_decide.py`) -
   plus new ones: the search is computed once per capture (count calls on a `FakeEvidence`), `read_card` once, the strip once; an upside-down
   OCR-confirmed card's marker comes from the upright strip;
2. `scripts/experiments/image_match/verify_live_read.py` before and after (same decided counts, 0 wrong, the per-card time should fall);
   `compare_lookup_versions.py` is for `lookup`, which this step does not touch - run it anyway, it must show no change;
3. `rehearse_matrix.py` on the rehearsal server in the four configurations: the same adds, the same reviews, 0 wrong, and the step-2 log's
   seconds per step compared with the run before this step.

**Risk:** the live path's one riskiest file. Keep the diff mechanical (cache, pass through), no rule moves in this step. Mode `cascade` must stay
byte-for-byte what it does today (`test_in_cascade_mode_the_picture_is_never_asked` and the foil-marker tests pin it).

### Step 5 - The guards as a pure function, with their own case file (F8, F9, F10)

**Why:** the Android app's standalone mode will need the very same guards, and today they can only be copied from prose; and a guard with a
name is a guard the log (step 2) can count.

- `core/reading/decide.py: accept_printing(decision, picture, strip_names, index_covers, ocr, thresholds)` - the second pure function. Inputs
  are plain data gathered by the caller: `decision` (from `decide_printing`), `picture`, `strip_names` (`None` - no whole line; `'chosen'`;
  `'indexed_other'`; `'unindexed'`; `'unknown'` - what the strip's line names, looked up by the caller), `index_covers` (bool / None), `ocr`
  (`None` - OCR off or not read; or `{'name_matches': bool, 'line_names': None | 'chosen' | 'same_card' | 'other_card' | 'unknown'}` - the
  caller's `names_match` and oracle-id comparison, done outside), `thresholds` (`image_min_similarity`, `strong_min_similarity`). Answer:
  `{'accepted': bool, 'refused': None | the guard's name}` with exactly today's rules, in today's order.
- `live_read` and `_read_by_picture` call it instead of their inline checks; `ocr_agrees` becomes the gathering half (it computes `name_matches`
  and `line_names`) and the decision half moves into `accept_printing`.
- `tests/data/reading_guard_cases.json`, hand-written like `reading_decision_cases.json` (one case per guard, one per "passes"), and
  `tests/test_reading_decide.py` runs it; the regression file is **not** regenerated - if an existing case's answer changes, the step is wrong.
- `decide.FINISH_SURE == foil_marker.SURE` pinned in `tests/test_foil_marker.py` (F9). `missing_printings` takes the game id from the caller
  (`Identification` passes `games.active_id()`) (F10).
- The Android app: `reading_reference.py` copies the new file too; `Decide.kt` gets `acceptPrinting` and passes it. One commit there, after the
  Python one.

Checks: `verify_live_read.py` before and after (identical counts); `test_reading_evidence.py` unedited; the new case file; `compare_lookup_versions.py`
(unchanged: `lookup` does not use the guards). Docs: the "pure function" paragraphs in CLAUDE.md and PROGRAM_DOCUMENTATION.md name the second
function and the fourth case file.

### Step 6 - Measure first, then decide (nothing to build until step 2's log says so)

- **F13, `ocr_agrees`.** With the log: how many picture-alone decisions are refused with `ocr_did_not_confirm`, split into *name not read* and *name
  differs* and *line names another printing*; and what the AI then answered (the same printing -> the refusal cost an AI call and prevented nothing).
  If the live numbers repeat the measurement (41% refused, none prevented), the candidate change is "OCR must not **contradict**" (a different whole
  name, or a line naming another printing of the same card, refuses; nothing read passes) - which keeps the protection against a *different* card
  newer than the data and gives up only the case where the title is also unreadable. The owner decides on the numbers; it is a one-line change in
  `accept_printing` and one case in the file.
- **The OCR cool-down taking the picture path down** (F13, second half): count in the log how often `ocr_did_not_confirm` coincides with `ocr: null`
  (the reader was down). If it happens, the fix is to treat "reader unavailable" as "OCR off" for that card (no confirmation possible, the picture
  alone with its other guards) - or to send it to review, never to the AI blind. Decide on the count.
- **The review item's "why".** A review item from the evidence mode could carry the guard's name and the picture's best similarity ("picture 0.48, below
  the floor; strip unread"), shown under the candidates. Small UI; do it once the names exist (step 5) and if the owner wants it.
- **Splitting `Identification`.** The reading steps (`_read_by_picture`, `_ocr_names`, `_contradicted_by_picture`, `_checked_against_picture`,
  `_read_with_ocr`, `_ask_ai`, `_read_foil_locally`) could move into `core/reading/pipeline.py`, leaving `Identification` the queues, the AI provider
  and the switches. Worth it only if steps 4 and 5 leave `identification.py` hard to read; it changes no decision but touches everything the
  tests patch (`FakeEvidence`, `FakeGame`). Not before a release has run on steps 1-5.
- **Thresholds in one place** (`IMAGE_MIN_SIMILARITY`, `STRONG_MIN_SIMILARITY`, the orientation and marker floors live in `printing_check.py`, `SURE` in
  `foil_marker.py`, A2's in `decide.py`): a `THRESHOLDS`-style dict in `decide.py` for the guards (step 5 does half of this); the rest stays where its
  measurement is documented. Not a step of its own.

## How to run this with Opus / Sonnet

- One step per session, in order; step 1 and step 3 are Sonnet-sized, steps 2, 4 and 5 are Opus-sized. Give each the step's text, the finding
  numbers and the baseline line above; the model reads CLAUDE.md's "Card data, matching, inventory" pitfalls on the printing check and the
  evidence mode before touching `identification.py` or `printing_check.py`.
- Before each step: `git stash list` empty, tests green. After each: the step's checks, then `venv/bin/python -m unittest discover tests` whole,
  and `tests/test_layout.py` (new modules are named in `CORE`). Steps 4 and 5 also run `verify_live_read.py` before and after (needs the
  development machine's index and OCR reader; `MTG_SCANNER_HOME=<repo>`), and the rehearsal server (`rehearse_matrix.py`, the pattern of
  DEPLOY_PLAN_FOIL_AND_PRINTING_CHECK.md) before a deploy.
- Never on the real `data/`: the rehearsal container has its own copy (the deploy plan says how it is made).
- Docs with every step (CLAUDE.md's table and pitfalls, PROGRAM_DOCUMENTATION.md's evidence section, FEATURES.md with what was never tried),
  and the statement "developed entirely with AI" stays.
- The Android app changes only in step 5 (the shared case file and `Decide.kt`); step 2's log carries nothing the phone reads.
- What the review did **not** do: run anything on live scans, time anything, or read the Android code beyond the reader labels. The numbers
  quoted are the project's own, from its docs and scripts.
