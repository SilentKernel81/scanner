# Plan: reading a card from several pieces of evidence, with one decision

Written 2026-10-20. **Status: phase 1 is done, phases 2a and 2b are built (the pure decision, shown unchanged on 444 pictures; the finish as log-only evidence); phase 3 is built and, since 2026-10-20, **the default** (`reading.mode: evidence`: the vision AI is the last step - see "Phase 3 as built" and "The background"); what waits for the gates below is *removing* the AI (phase 5), the Android wiring and a shipped index.**
The measurements behind this plan are in [IMAGE_MATCH_EXPERIMENT.md](IMAGE_MATCH_EXPERIMENT.md). The exit criteria for dropping the vision AI are written once,
in [PROGRAM_DOCUMENTATION.md](../PROGRAM_DOCUMENTATION.md) ("Printing check"); this plan points at them and does not restate them, so the two cannot drift apart.

## The background (the owner, 2026-10-20)

The vision AI is in this project because, when it started, there was no light-ocr and the OCR that existed did not read well enough. Now the OCR is good and the picture of a card can be matched against every
printing, so **the AI is pushed to the last step and kept until it can be removed**. Earlier drafts of this plan (and the first deploy) treated "the picture decides before the AI" as an experiment to be held back
until the exit criteria were met; those criteria are about *removing* the AI, a larger step. The order is the design and the default.

## Why change the shape

Today a card is read as a **cascade**: the OCR reads the whole card; if its read is a confirmed printing the card is added; otherwise the vision AI
reads it and its answer is looked up the same way (`core/identification.py`, `Game.confirmed_read`). Every answer therefore rests on **one source**.
Two things the experiment found follow from that:
- a card whose printed line names *another* printing (Secret Lair cards print the set and number of the card they are made from) is "confirmed" and added as the wrong printing;
- the AI's answer is trusted whole, and it misread about 12% of the cards it handled.

What worked in the experiment is the opposite shape: **independent pieces of evidence, and a card is added when two of them agree** - the picture
(a 96-byte fingerprint of every printing, 5 nearest), the collector line (number and set, a small crop), the foil symbol read from the set line (and a tiny
network where the OCR cannot read it). See the report for the numbers; none of them is repeated here.

## The target

```
capture (the flat card)
  ├─ picture   fingerprint, both orientations → the 5 nearest printings (+ their set, number, oracle id, finishes)
  ├─ strip     collector number + set code + the foil symbol, from a small crop (the card turned upright first)
  ├─ foil net  ★/• from the set line, only when the strip's OCR read no symbol
  ├─ title     the card's name (optional: the review screen, a third source where the picture cannot speak)
  └─ AI        only if the decision is still open (optional, behind a plugin boundary)
                  ↓
       decide(evidence) → printing, finish, add | review, reason       a PURE function
                  ↓
       one log record per reading: the evidence and the decision        the audit trail, and the data to calibrate the thresholds
```

**The decision** (rule A2 of the experiment, kept exactly as measured): the printing the strip names is accepted if it is one of the picture's
nearest and looks as much like the capture as the best match does; else the best match, if it leads the second by a margin; else the card goes to review.
Anything beyond A2 (a printing newer than the index, the finish) is a **separate rule with a separate field**, never folded into A2's answer:
the exit criteria were written against A2's numbers, and live evidence must keep measuring that.

**Evidence is plain data** (ids, numbers, similarities, strings). The code that gathers it does every lookup - the database for each candidate's set, number,
oracle id and finishes, the index for the similarities - so that `decide(evidence, thresholds)` makes no call at all. That is what makes
each test case a complete, self-contained file, and what lets another program (the Android app) pass the same files.

## Where it lives

A package `core/reading/` (an empty `__init__.py`, as `core/` has):
- `evidence.py` - the shapes of the evidence (plain dictionaries and the fields they carry; written down once, in code);
- `decide.py` - `decide_printing` (rule A2), and from phase 2b `decide_finish`; the thresholds; `plain_number`.
Later phases add the gathering and the worker pool (phase 3). The existing modules keep their names and what they export (`printing_check.py`
re-exports `AGREE_BELOW`, `IMAGE_LEAD`); `Identification` (`submit`, `identify`, `on_done`) is **not touched** before phase 3, so `server/captures.py` and the stations do not change.

## Phases

| # | What | Gate to start | Changes live behaviour? |
|---|---|---|---|
| 1 | The shadow check and the lookup, collecting evidence beside the AI's answers | - | No. **Done** (commits `6a462ff`, `a84dcb9`) |
| 2a | `core/reading/`: the pure `decide_printing`; the lookup is rebuilt on it. **No behaviour change**, proved picture by picture against the previous implementation | 1 | No |
| 2b | The finish as evidence: the strip's symbol and the foil network's probability are gathered and `decide_finish` answers in its own log field, next to the finish the server chose | 2a | No (log only) |
| 3 | The decision drives add / review; the AI becomes the fallback for what is still open (its answer checked against the picture) - **the worker pool was decided against**. Built 2026-10-20, the default since the same day (the owner: the AI was only ever there because OCR was weak, so it is the last step until it can be removed) | The owner's decision (given: the AI last). The exit criteria are for removing the AI, not for this | **Yes** |
| 4 | The review screen shows the picture's candidates; an unsure finish is flagged | 3 | Yes (a screen). **Built** (2026-10-20): the five nearest printings as tiles, and a hint for an unread foil marker - in the review only; a card added automatically with an unread marker is still filed as regular, unflagged |
| 5 | The AI is off by default; its code (providers, prompts, keys, the endpoint checks) is deleted one release later | 3 in use for a release | Yes |
| 6 | The Android app: the same fingerprint, the foil network through OpenCV, the same decision, passing the same test files as Python | 2a (the files exist) | The app. **Built, not wired in** (2026-10-20, in `../mtg-scanner-android`, `reading/`): `Decide.kt` passes the three shared case files, `Fingerprint.kt` is the Python fingerprint in 0 of 768 bits on three pictures and `FoilNet.kt` is within 5e-8 of the Python probability on 16 crops - on an x86-64 emulator, never a phone; the app does not call it (it needs the image index on the phone: phase 7's question; the phone also cannot find the set line without an OCR) |
| 7 | Keeping the index current without anyone shipping it: **built 2026-10-20** as a button (Settings > Storage > Picture index) that downloads only the printings new since the last build from Scryfall, with progress, Stop and the settings password; still open: re-fetching pictures Scryfall replaced (needs `image_status`), a coverage guard for the picture-alone path, and an optional shipped index (the question below) | The shipped index: the open question below | No |

**Phase 2 changes nothing the live server does.** It touches `printing_check.py` (a shadow-only module) and adds the package, tests and test files.
`identification.py` and the live path stay as they are; if phase 2 turns out to need a change there, stop and ask.

## How phase 2 is checked

- **The test files** are the contract. Two kinds, written from different sources: *hand-written* cases for each edge, with the expected answer stated by the person writing
  the case (strip names a printing that looks different; two reprints with one art, decided by the strip; no strip and no lead; the nearest is far ahead; one
  candidate only; a strip that names two printings; an empty picture...), and *regression* cases captured from the previous implementation on real pictures
  (ids and similarities only - no images): evidence in, that implementation's answer out. A file produced by running the new `decide` would only test itself.
- **No behaviour change is shown picture by picture**: the previous lookup and the new one run on the same pictures (the AI-read ones and a sample of the OCR-confirmed ones),
  and the path, the printing and the comparison with the resolved card are compared for each one. Equal totals would not be enough: they can hide swapped answers.
- The existing tests pass **unchanged** (an expectation that has to change means the behaviour changed).

## Phase 3 as built (2026-10-20)

`reading.mode` (`evidence`, the default since 2026-10-20 / `cascade`) in `config.yaml`, overridable in `data/settings.json` (Settings > Card reading) without a rebuild. In `evidence` the decision drives add / review for a card the
full-card OCR did **not** confirm, in front of the AI: `PrintingCheck.live_read` -> `Identification._read_by_picture`; the AI keeps the cards the picture cannot decide, and **its answer is
checked against the picture** (`disagree` -> review). The decided printing is carried by id (the lookup by name and number fails for 14% of printings: `verify_roundtrip.py`). Details and
numbers: PROGRAM_DOCUMENTATION.md, "Reading mode: evidence". Deliberately **not** in this step:
- the single worker pool replacing the OCR and AI workers: **decided against** (see below);
- a rule for an OCR-confirmed card the picture contradicts is built for the `same_card` case only and is **not finished** (PROGRAM_DOCUMENTATION.md: 4 of the 15 Deadpool captures would still be added wrongly - garbled OCR names that fuzzily match another card);
- the picture alone adding a card is OFF by default (`reading.accept_image_alone: false`): a printing newer than the index whose strip is unreadable could be added by it even after the two guards (4% in a simulation on 428 pictures; with it off, 0 of 428) - the strip-confirmed decisions alone give 58.6% of the AI-read cards and none wrong;
- the shadow log cannot judge the mode's own adds (it would compare the choice with itself): the exit criteria stay measured in `cascade`.

## What stays decided against

**The single worker pool.** The plan's phase 3 named one pool replacing the OCR worker and the AI workers. Not built, on purpose: the OCR reader takes one request at a time, so the one serial OCR worker is
deliberate, and merging the stages would change no decision while putting the riskiest part of the live path at stake. If throughput ever matters in the evidence mode, the narrower fix is to move only
the picture search (CPU, no reader lock; 18 ms median) off the OCR worker. Measure first.

The neural embeddings (the hash beat them by a wide margin) and the fine-tuned network (it passed its bar on thin evidence and was not adopted): see the report.
The index's manifest names its descriptor, so a better one can replace the hash later without a new design.

**Changing the card finder for the sake of the fingerprint.** Not needed: a separate analysis (2026-10-20, outside this repository; 34 labelled frames, one camera - the Pi, and it is not
recorded which finder produced the corners) found that the finder's corner error costs the colour-hash lookup nothing measurable (top-1 94.1% with the station's corners and with human-marked
ones). It is mostly a consistent outward bias - the station's corners sit about 1.85% of the card width outside the human ones - and the fingerprint's edge cut (`image_index.EDGE`: 2.5% top
and bottom, 3.5% left and right) lies inside it; "the cut absorbs the bias" is a reading that was not tested. **The condition that goes with it:** an alignment-sensitive descriptor (the HOG
descriptor the analysis tried lost 12 points, 91.2% -> 79.4%, with the station's corners) would need the finder retrained to the human edge definition first - so a descriptor change in the
manifest is also a detection decision.

## Risks and open questions

- **Cards the picture cannot speak for:** a printing newer than the index (the strip then has no second source), old cards that print no collector line. A rule for the first
  is sketched (the strip and the title agree) and **not measured**; it is not part of phase 2.
- **Foil kinds** beyond the 13 recent sets the foil network was measured on (etched, surge, older cards).
- **Throughput and the GPU** next to the local model: the lookup's second OCR reader has not been measured on the server (the deploy plan's rehearsal does).
- **Open, and no longer in the way: may the fingerprints be published?** A shipped file of 96-byte hashes derived from Scryfall's pictures would spare a new installation its first build
  (hours, about 12 GB). Whether Scryfall's and Wizards of the Coast's terms allow that has **not been checked**; it is an optional shortcut now, since each installation builds its own index and keeps it
  current with the button.
- **Corner error and the fingerprint** (from that analysis): random corner noise leaves the hash flat up to about 1.25% of the card width and falls off between 2.5% and 5% (top-1 88.0% -> 82.5%
  -> 50.1%); the finder's worst single corner in the 34 frames was 9.2%. Such a frame matches badly and is caught by the guards (the similarity floor, the lead, the strip rule) and goes to
  the AI - which is the intended failure. **Not known:** the phone station, a card in a sleeve or on a playmat, and whether the corner net improves on the corners used. These are the cases
  to look at first when the live log shows picture misses, before suspecting the index.
- **The live numbers** are still to come: the thresholds were set from 20 wrong labels, and phase 1 exists to replace that with live evidence.

## Rollback

Phase 2: `git revert` of its commits; nothing it adds is read by the live path. Phase 3 onward: each is behind a setting (to be added with it), and phase 5 deletes code only a release after the AI is off.
