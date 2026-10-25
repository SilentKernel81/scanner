# Plan: what is next after 2026-10-24 (for 2026-10-25)

**Status:** not started. Written at the end of 2026-10-24, when everything below "Done" was committed, pushed and
deployed (the server runs `f05a817`; every change of the night went through a Codex adversarial review until it approved).

## Done on 2026-10-24 (deployed)

- **The sale picture** (`34936c8`): an open trade's *Sale picture...* - 3 x 3 of the owner's scans with each card's name,
  set / number / finish / copies, for Facebook Marketplace. Downloads drawn from a snapshot on a canvas of their own; large
  copies loaded only when drawn. PROGRAM_DOCUMENTATION, *Trades*.
- **Prices, phase 4** (`749c944`, `7cde146`, `4ba30be`): what Card Kingdom offered, beside the value - under a list row,
  a Statistics tile with its coverage, a trade's summary; US dollars; always with the day of the prices, "N days ago" when
  older than two days; labelled Near Mint's, worn copies left out of the totals. `docs/plans/PRICE_SOURCES_PLAN.md` is
  complete (all phases). The owner ran the first *Update the shops' prices* on the server (102,652 printings).
- **Trades, parts 1 and 2 of `docs/plans/TRADES_REVIEW_PLAN.md`** (`505ead7`, `963d770`, `b124881`, `f05a817`): a stale
  Cancel no longer deletes a confirmed trade (`?action=cancel` / `?action=forget`); Confirm checks a fingerprint of exactly
  what the trade holds, only for the trade the person saw, and refuses a request without one (old pages must reload);
  receiving counts what went in and tells the other pages; Cancel's wording.

## 1. The owner's switch (no code)

**Turn on the shops' price schedule** - Settings > Card data > Prices, the dropdown beside *Update the shops' prices*
(daily). It is off (`prices.every_hours` 0): without it the Card Kingdom offers stay those of 2026-10-24 and the pages call
them old from 2026-10-27 ("N days ago - update the shops' prices"). Guarded by the settings password: only the owner.

## 2. Part 3 of the trade plan (the owner chooses; suggested order)

From `docs/plans/TRADES_REVIEW_PLAN.md`, *3. Improvements* - the details, the code to touch and the tests are there.

1. **A backup before Confirm** (small). `backups.create` before `Trades.confirm`, so a wrong confirm is undone by a
   restore. Check how long a collection backup takes on the server's data first (it must not make Confirm feel stuck).
2. **A notice when editing a card a trade holds** (small). In the edit dialog, from `card.trades`: "This card is set aside
   for the trade X" - an edit of its finish, condition or printing changes what the trade gives (and, since `963d770`,
   makes a Confirm already open refuse).
3. **Move set-aside cards** (the largest; the most useful for selling - packing into a "To ship" box). The move's
   transaction points the trade's rows at the entry the copies went to, merging two rows of one trade, before the old
   entry is deleted; bulk move too. Needs tests for the triggers (`trade_keeps_entry`) and for the fingerprint (a move
   changes it: a Confirm open during a move is refused, as it should be).
4. **"Send what I received to review"** (medium). The only option CLAUDE.md allows for received cards: each goes to the
   review queue to be checked and added like a scanned card - never straight into the collection.
5. The sale picture of a confirmed trade - nothing to do (its cards have no scans left); noted in the plan.

Each: its tests, PROGRAM_DOCUMENTATION's *Trades*, FEATURES.md, TEST_CASES.md (TRD-xx), CLAUDE.md's Trades row, `?v=`
raised on a changed script, a Codex adversarial review (`--base` the commit before), then commit, push and deploy when
the owner says so.

## 3. The standalone mirror (only when the owner asks)

The standalone checkout (`../scanner`) takes this repository's changes only by merging `server/main` in its own checkout,
as CLAUDE.md says; nothing of the night is in it yet unless it was merged since. Fetch `server` there to see how far
behind it is; merge, check that `git diff --stat server/main HEAD` is empty, run its tests, and push it only when asked.

## 4. Smaller things noticed on the way

- `docs/plans/TRADES_REVIEW_PLAN.md`'s status line reads awkwardly ("Before: Written from a review...") - tidy it when
  part 3 starts.
- Two kinds of test tooling were written in the scratch folder for the night's browser checks (headless Chromium driven
  over the DevTools protocol against a server on copied data). If browser checks of the collection page are wanted again,
  a script in `scripts/` like `check_deck_pages.py` would keep them - only against copied data, never the real server.
