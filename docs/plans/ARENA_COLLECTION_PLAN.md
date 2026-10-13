# A digital collection (MTG Arena), and "can I build this deck there?": plan

Date: 2026-10-13

A plan for a second collection - the cards the user has in MTG Arena - filled only by
importing the files an exporter writes, for decks of two kinds, and for a check that says
of a paper deck whether it can be built in Arena. **Built on 2026-10-13** (branch
`arena-collection`; *How it went* at the end says what was done, what was measured and
what was not tried). What is said about the export was read from one real export of the
user's (2026-10-13); what is said about the code before it was read at commit 204206a.
The user's decisions changed the plan after it was first written: where the text below
said otherwise it has been rewritten, and *Decided by the user* says which parts those are.

## What there is to work with

**The export** (the user's own exporter, [mtga-export](https://github.com/SilentKernel81/mtga-export) -
first described here as third-party, which it is not - that reads Arena's data while it runs; the user ran
it, its files lay in `~/Work/mtga_collection_export/`). The one that matters is
`mtga_collection.json`:

- `cards`: 1,972 entries - `count`, `name`, `set`, `collector_number`, `rarity`, `type`,
  `rebalanced`, `arena_ids`;
- `inventory`: the account's wildcards (`WildCardCommons` 18, `WildCardUnCommons` 30,
  `WildCardRares` 1; no mythic key when there are none), gold, gems;
- `export_date`.

It also writes `mtga_decks.json` - the user's Arena decks: `id` (Arena's own), `name`,
`format` ("Historic", "Standard"), and one list `deck` of cards like the collection's
(three decks in this export; none had a sideboard or a commander, so how the exporter
writes those was not seen) - and CSVs for other sites (Moxfield's has no collector number)
and what is new since its last run, which are not needed.

**How the entries match the card data** (the laptop's, which is the same Scryfall data as the
server's), measured:

| | entries | cards |
|---|---|---|
| set + collector number, the same name | 1,891 | 2,993 |
| set + number, another name | 31 | 56 |
| the name, in another set (Arena's Alchemy sets Y23, Y26: printings the card data lacks) | 49 | 61 |
| the name, same set, another number | 1 | 2 |
| not found | 0 | 0 |

26 of the 31 are Arena's *rebalanced* cards, written `A-Ancestral Katana`: digital twins that
share set and number with the card they change. The exporter's own totals leave them out
(1,946 cards, 3,061 copies). The other 5 are basic lands of one set (LCI) that Arena numbers
differently from the printed set - its Forest #400 is the paper set's Mountain. So a set
and number are only believed when the card found there has the entry's name; otherwise the
name decides, in the same set where it has it.

**The code.** The collection and the scanned cards are two `InventoryManager`s on two files,
and a request says which it is about (`inventory_area()`, `?area=scan`). A third is the same
again. "Which cards exist on Arena" is known since the Arena list (`mtg_decks.on_arena`).

## Decided by the user

- **An import replaces everything.** The export is the whole collection, and Arena keeps
  the original: nothing in the digital collection is worth merging or keeping.
- **No scanning.** The digital collection is never added to from a camera.
- **Paper decks get a check: can this be built in Arena?**
- **Decks are of two kinds, Physical and Arena** (decided after the plan was first written,
  replacing "decks stay what they are" and open decision 3): the Decks page filters by
  kind; the import brings the exporter's Arena decks, and **replaces** the ones it brought
  before (a deck deleted in Arena goes here too) - an Arena deck made here is left alone;
  an Arena deck counts what is owned in the digital collection, and what is missing in
  wildcards. No "copy as Arena deck": the Arena list is pasted into Arena, and the deck
  comes back with the next import.
- Open decisions 1, 2 and 4 as recommended.

## Design

### The store

`data/arena_inventory.db`, a third `InventoryManager` (`services.arena_inventory`, the proxy
`arena_inventory`), with the tables every inventory has - so the list, the filters, the
statistics and the exports of the collection page work on it as they are. Its rows: the
card (the printing the export names, else any printing of the name), quantity, finish
`regular`, condition `Near Mint`, no location, no tags. Beside them a small table
`arena_account`: the export's date, the wildcards by rarity, gold and gems, and how the
import went (matched by set and number, by name, skipped).

### The import

`POST /api/arena/import` with the JSON file (the collection page's upload; the server is
not the machine Arena runs on). In this order, and whole or not at all:

1. read the file - anything that is not this exporter's JSON is refused with a sentence;
2. resolve every entry against the card data **before** anything is changed: by set +
   number when that printing carries the entry's name (or its front name), else by name -
   in the entry's set if the card is there, else in any;
   rebalanced entries are skipped (decision 1); what cannot be resolved is collected;
3. in one transaction: empty the digital collection, write the resolved entries, write the
   account;
4. answer with the counts and the names that could not be resolved.

A new `InventoryManager.replace_all` does step 3 (`@_writes`: rolled back as a whole when
it fails), rather than 1,900 single adds that each commit.

### The collection page

A switch at the left of the page's tabs: **Physical | Arena**. In Arena the inventory list,
its filters and the statistics show the digital collection (`?area=arena`), **read only**:
no edit, delete, bulk change, trade, location or tag - every change would be gone at the
next import, and Arena is where that collection is changed. Above the list: when it was
imported, the wildcards, and **Import from Arena** (the upload).

### The check for a paper deck

The deck page's **Arena** window (it exists: the list to copy) gains what the user asked
for. For the cards of the deck:

- **in your Arena collection** - you have the copies;
- **to craft** - the card exists on Arena and you have too few copies: counted by rarity,
  next to the wildcards you have ("3 rare - you have 1");
- **not on Arena** - as today.

Basic lands are free in Arena and never count as missing. The rarity is the card data's
for the card, which is the paper rarity of one printing; Arena's can differ for a few
cards, so the wildcard count is close, not exact. `GET /api/decks/<id>/arena` gives the
three groups and the totals; nothing is written.

### Backups

The digital collection is in the collection's backups (a third part, `arena_`) and in the
full backup: it can be imported again, but only from a machine with Arena on it. A backup
from before has no such tables and leaves the digital collection as it is on restore -
the rule that exists for tables a backup was made without.

### Decks of two kinds

Every deck is **Physical** (`kind = 'paper'`: every deck there was) or **Arena**
(`'arena'`). The kind says which collection a deck is counted against - what is owned,
which other decks want the same copies, the buy list, the card search's "owned" - and an
Arena deck shows what is missing in wildcards by rarity, beside the wildcards the account
had. The Decks page filters All | Physical | Arena, an Arena deck has a badge, and New deck
asks for the kind.

The import brings the exporter's decks as Arena decks, each with Arena's id for it
(`decks.arena_id`), and **replaces** the ones the last import brought: a deck changed in
Arena changes here, one deleted there goes here. An Arena deck made here has no such id
and stays. There is no "copy as Arena deck": the Arena button's list is pasted into
Arena, and the deck comes back with the next import.

## The decisions, as they were taken

1. **Rebalanced `A-` cards**: skipped in the collection (a second face of a card already
   counted; the exporter's totals skip them too). In a deck such a card is the card it
   changes.
2. **The digital list is read only.**
3. **The exporter's Arena decks**: imported, and replaced by each import (first planned as
   "not now"; the user decided otherwise).
4. **The import button** is on the collection page, in Arena view; nowhere else.

## What must not change

- The paper collection and the scanned cards: nothing done with the digital collection
  reaches them. A paper deck's owned marks and buy list read the paper collection only.
- Every existing `/api/` answer without `area=arena` (the recorded walk).
- The stations: nothing of this reaches them.

## How it is checked

| Check | What it catches |
|---|---|
| Tests on temporary files: the import (a small made-up export: by set and number, by name, a rebalanced twin, an unknown card, a wrong file), replace-all whole or not at all, a second import replacing the first, the deck check's three groups and wildcard counts | the importer's rules |
| The user's real export imported on a test server (a copy of the data): the counts against the exporter's own statistics (1,946 / 3,061) | a matching rule that loses cards |
| `scripts/walk_server.py`, `main` against the branch | an answer that changed |
| The browser check: the switch, the Arena list read only, the paper list as before, the deck window's three groups | the pages |
| Backups: one made and restored on the test server with the digital collection in it; an old backup restored | the third part |

## Phases

1. The store, `replace_all`, the decks' kind, the import and its tests.
2. The collection page: the switch, the read-only list, the upload.
3. The deck pages: the filter, the badge, the kind of a new deck, wildcards, the check in
   the Arena window.
4. Backups.
5. Documents.
6. Deploy to the server (a new file is created at the first start; the deck table gets
   its two columns by the migration `deck_kind_1`), then the user imports their export
   there.

## Risks and open points

1. **The export's format is one person's tool.** If it changes, the import refuses the
   file and says what it expected; the digital collection stays as it was.
2. **Arena-only cards.** 49 entries are found only by name, in a paper printing: they show
   with that printing's picture and set. A card that exists nowhere in the card data would
   be reported and left out - none in this export.
3. **Wildcard counts are an estimate** where Arena's rarity differs from paper's.
4. **Three inventories in one page.** The collection page was written for one; the switch
   has to turn off everything that writes, in Arena view, in one place.

## What this does not do

- No link to Arena: the files are made by the exporter and uploaded by hand.
- No history between imports (the exporter keeps its own "new since last time").
- A deck's kind cannot be changed afterwards - but Duplicate asks for the copy's kind
  (added after the first deploy, at the user's wish: an Arena deck as a Physical one, to
  see what to buy).
- Changes made here to a deck that came from Arena are lost at the next import with a
  deck file - the deck's badge says so on pointing; nothing stops the change.

## How it went

Built on 2026-10-13 in phases 1 to 5, each committed; not deployed when this was written.

**What was built, beyond the design above.** `core/arena.py` (reading the two files,
finding the cards, the account, `wildcards_needed`); `server/arena_api.py`
(`POST /api/arena/import`, `GET /api/arena`; the event `arena_updated`);
`InventoryManager.replace_all`; `DeckManager.replace_arena_decks` (a deck that is still
in Arena keeps its cover); five more deck formats - Historic, Timeless, Alchemy, Brawl,
Standard Brawl - which every deck can have; a card Arena does not have, in an Arena deck,
is a problem of the deck ("Not on Arena") and not a wildcard; an Arena deck's basic lands
count as owned.

**The read-only list has three layers**, after a review found the first design had a
hole: the page's deletes and bulk changes named no area, so in Arena view the ids of
Arena entries would have been applied to the *paper* collection. Now the page hides what
changes a collection, its handlers refuse in Arena view (nothing can be ticked;
`changeLater` and `setAsideForTrade` return), every write of the page names its area, and
the server refuses a write to `area=arena`. A change waiting for its undo is sent before
the view changes.

**Measured, on test servers with copies of the laptop's data:**

- The user's export imported: 3,061 cards in 1,946 entries - the exporter's own totals -
  and the same count for each of 1,602 card names. 1,891 entries found by set and number,
  55 by name, 26 rebalanced skipped, none unknown. The three Arena decks came in (60, 60
  and 14 cards), each fully owned. A second import gave the same; a CSV was refused and
  changed nothing.
- `scripts/check_arena_pages.py`, 30 of 30 in headless Chromium: the switch, the Arena
  list and its statistics, that ticking / deleting / trading do nothing there and the
  server answers 400 to a change sent anyway, the deck filter and badges, an Arena deck's
  totals, a new Arena deck with a card that is not on Arena and one to craft, an import
  leaving that deck alone, the check in a paper deck's Arena window - and the paper
  collection the same at the end as at the start. `scripts/check_deck_pages.py` 64 of 64.
- `scripts/walk_server.py`, `main` against the branch: 12 of 71 steps differ, all by the
  new fields only (`kind`, `from_arena`, `wildcards`, a backup's `arena` and so its
  fingerprint, the five formats).
- The migration `deck_kind_1` on a copy of the server's deck tables (15 decks, 969 deck
  cards): identical before and after, all `paper`.
- 235 unit tests (20 new in `tests/test_arena_collection.py`, three of them backups).

**After the user looked at it** (a preview server on copied data, a real browser; the
import through the page's button worked): the rows of the Arena list were laid out for
the checkbox it no longer has, and names wrapped - fixed; Arena cards are stored and
shown without a price; the second small picture of a row is left out there; either
exporter file can be imported alone (the collection file had been required); "What can I
build?" got the same choice, Physical | Arena. A review then found that a price filter
remembered from the paper view emptied the Arena list - the hidden filters are no longer
applied there. `scripts/check_arena_pages.py` is at 32 of 32.

**Not tried:**

- A sideboard or a commander in the exporter's deck file (read as `sideboard` /
  `commander` lists if they are there - a guess at the names).
- Another version of the exporter, another account, an export with mythic wildcards.
- Whether the wildcard counts agree with what Arena asks: the rarity is the printed
  card's.
- The results of "What can I build?" for Arena (the rankings ask other sites; checked
  only that they start for the Arena collection and that a deck started there is an
  Arena deck).
- A backup with the digital collection restored on a running server (the unit tests
  restore one on temporary files).
