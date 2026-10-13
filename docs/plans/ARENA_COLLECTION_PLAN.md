# A digital collection (MTG Arena), and "can I build this deck there?": plan

Date: 2026-10-13

A plan for a second collection - the cards the user has in MTG Arena - filled only by
importing the file an exporter writes, and for a check that says of a paper deck whether it
can be built in Arena. Nothing here is implemented. What is said about the export was read
from one real export of the user's (2026-10-13); what is said about the code was read at
commit 204206a.

## What there is to work with

**The export** (a third-party exporter that reads Arena's data while it runs; the user ran
it, its files lay in `~/Work/mtga_collection_export/`). The one that matters is
`mtga_collection.json`:

- `cards`: 1,972 entries - `count`, `name`, `set`, `collector_number`, `rarity`, `type`,
  `rebalanced`, `arena_ids`;
- `inventory`: the account's wildcards (`WildCardCommons` 18, `WildCardUnCommons` 30,
  `WildCardRares` 1; no mythic key when there are none), gold, gems;
- `export_date`.

It also writes CSVs for other sites (Moxfield's has no collector number), the user's Arena
decks, and what is new since its last run. None of those is needed.

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

A switch at the left of the page's tabs: **Paper | Arena**. In Arena the inventory list,
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

## Decisions still open

Each with a recommendation; the plan is written for it.

1. **Rebalanced `A-` cards.** **Skip them** (they are a second face of a card already
   counted; the exporter's totals skip them too), or count them as the card they change.
2. **The digital list read only** (above), or editable like the paper one.
3. **The exporter's Arena decks** (`mtga_decks.json`). **Not now**: a deck can already be
   copied out of Arena and pasted into New deck.
4. **Where the import button is.** **On the collection page, in Arena view**; nowhere else.

## What must not change

- The paper collection, the scanned cards, the decks' owned marks and buy lists: they
  never read the digital collection.
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

1. The store, `replace_all`, the import and its tests.
2. The collection page: the switch, the read-only list, the upload.
3. The deck check in the Arena window.
4. Backups.
5. Documents.
6. Deploy to the server (a new file is created at the first start; nothing existing is
   migrated), then the user imports their export there.

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

- No link to Arena: the file is made by the exporter and uploaded by hand.
- No history between imports (the exporter keeps its own "new since last time").
- No Arena decks as their own kind of deck, and no deck counted against the digital
  collection for its owned marks.
