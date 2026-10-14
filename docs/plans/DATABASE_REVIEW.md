# Database investigation

Reviewed: 2026-10-09. What became of each finding: the status table in [DATABASE_FIX_PLAN.md](DATABASE_FIX_PLAN.md) (2026-10-14). The text below is the review as it was. Database inspection used read-only SQLite connections. Failure reproductions used temporary databases; application data and schemas were not modified.

## Current storage and usage

The application uses SQLite with multiple manager connections, generally protected by a per-manager Python lock. Card and inventory connections enable WAL and `synchronous=NORMAL`. WAL is a sensible choice for concurrent readers, but it does not allow simultaneous writers. Different managers' locks do not coordinate their separate connections.

| File / table | Local rows | Purpose and main owner |
| --- | ---: | --- |
| `cards_database.db`: `cards` | 112,765 | Downloaded Magic printing catalog; matching, search, images, prices and deck-building data. `database.py` stages refreshes in `cards_import` and replaces the live table. |
| `card_data_info` | 1 | Per-game source/download metadata and card count. `database.py`. |
| `inventory` | 1,595 | Owned collection: printing, finish, condition, location, quantity, tags and latest batch metadata. `inventory.py`. |
| `inventory_captures` | 2,959 | Thumbnail references associated with collection entries. Images live on disk. `inventory.py`. |
| `decks` | 10 | Named decks, game, format and timestamps. `decks.py`. |
| `deck_cards` | 857 | Names, quantities, boards and optional display printing; deck editing does not consume collection copies. `decks.py`. |
| `review_queue` | 0 | Unconfirmed captures awaiting review; image copies live in `data/review/`. `review.py`. |
| `web_cache` | 419 | Cached external JSON responses, keyed by URL. `recommendations.py`. |
| `arrived_moves` | 0 | Destination-side receipts for recovering transfers from scanned cards. `inventory.py`. |
| `scan_inventory.db`: `inventory` | 0 | Scanned cards waiting to move into the collection. Same inventory manager/schema. |
| `inventory_captures` | 0 | Thumbnail references for scanned entries. |
| `pending_moves` | 0 | Source-side journal for interrupted transfers. |

The checked-out code also creates `inventory_sources` (per-entry station quantities) and adds `inventory_captures.station`. These are absent from the inspected local files, which therefore predate those startup migrations. This is a schema-version difference, not evidence of corruption. Startup also ensures the move-journal tables it needs.

`pending.py` creates a separate `data/pending_captures.db` for captures queued for identification. That file was absent locally, so its behavior was assessed from code rather than stored records. Other games may introduce tables through their implementations; this inventory describes the files actually present and the current shared managers.

## Health checks

- Both local database files returned `ok` from `PRAGMA integrity_check`.
- No orphan inventory capture rows were found in either file.
- No orphan deck-card rows were found in the main file.
- No nonpositive collection quantities were found in the inspected main database.
- Ownership consistency could not be checked on local records because `inventory_sources` is not present yet.
- Exact set/collector-number lookup uses `idx_card_set_number`.
- A representative name substring query (`LIKE '%dragon%'` on normalized name/flavor columns) scans `cards`.
- The main file has 66,429 pages, including 1,281 free pages: about 5 MiB of reusable free space at its 4 KiB page size. File size alone does not justify vacuuming.

## Priority 1: transaction and correctness fixes

### Station-filtered reads acquire and retain a write transaction

**Location:** `inventory.py`, `_station_rows` / `_drop_orphan_sources` (around lines 337–353).

`_station_rows` unconditionally runs a `DELETE` to clean orphan sources before selecting entries. A read through `get_all_cards(..., station=...)` does not commit that delete. Even when it deletes zero rows, it can leave the connection in a write transaction.

**Reproduced:** After a station-filtered read, `conn.in_transaction` was true. An independent connection attempting an inventory update failed with `database is locked`.

**Improve:** Remove cleanup writes from read paths. Perform cleanup within the mutation that deletes an entry, or use foreign keys where appropriate. Ensure every write transaction commits or rolls back before returning. Add a test with two connections to verify filtered reads do not block a writer.

### Card catalog replacement is not atomic

**Location:** `database.py:271`, `replace_table`.

The method commits before `DROP TABLE` and `ALTER TABLE` without beginning an explicit transaction. A failed rename can leave the original table deleted. A Python lock cannot protect against a process crash or provide database rollback.

**Reproduced:** Calling the actual method with a missing staging table raised an error after dropping `cards`; the original table no longer existed.

**Improve:** Validate staging data first, then perform drop, rename, index creation and associated metadata updates in an explicit transaction with rollback on failure. SQLite supports explicit transactions for grouping these operations: [SQLite transaction documentation](https://www.sqlite.org/lang_transaction.html).

### Failed deck imports can leak partial changes into later commits

**Location:** `decks.py:164`, `import_cards`.

A replacement deletes existing cards and inserts entries before all quantities are validated. Exceptions leave the transaction open, with no rollback.

**Reproduced:** Replace a deck containing `Original` with a valid `New` entry followed by an invalid quantity. The import raised `ValueError`. A later notes update committed the partial replacement, leaving only `New`.

**Improve:** Validate inputs before mutation and wrap replacement in a transaction that rolls back on every exception. Review other manager methods for the same pattern, particularly inventory CSV imports, which also delete before validating the full input and can commit a replacement with no accepted rows.

### Moving a deck card commits twice

**Location:** `decks.py:155`, `move_card`.

The method calls `set_card` for the destination, then again for source removal. Each call commits independently.

**Reproduced:** Injecting a failure before source removal left a committed destination copy and the original source copy.

**Improve:** Update both boards and the deck timestamp in one transaction. Use internal helpers that do not commit; let the operation own the commit.

### Quantity changes can leave conflicting station ownership

**Location:** `inventory.py`, `update_card`, the branch updating an entry without merging it (around line 716).

Reducing quantity trims captures but does not reduce `inventory_sources`. Station views cap each station independently, which cannot enforce a shared total.

**Reproduced:** Add two copies from A and two from B, then reduce the entry to two. The entry holds two copies while both station rows still claim two each.

**Improve:** Define a removal allocation policy and adjust source quantities and captures in the same transaction as inventory quantity. Verify `SUM(source.quantity) <= inventory.quantity` after edits, splits, merges, undo and transfers. A normal row-level CHECK alone cannot enforce this cross-table invariant.

### Capture settlement needs persistent idempotency

**Locations:** `app.py`, `submit_capture`, `resume_pending`, station upload deduplication; `pending.py`.

Pending jobs are durable, but completed capture identities and outcomes are held in memory. Adding a card or review item and deleting its pending row happen through separate connections/files. A crash between those commits can replay a job whose result was already recorded. Retrying a completed upload after restart can also bypass in-memory deduplication.

This finding is based on tracing commit boundaries; a full process-crash reproduction was not performed.

**Improve:** Persist a unique capture identity, state and outcome. Make applying its result idempotent at the destination database, recording the applied identity in the same transaction as the inventory/review mutation. A uniqueness constraint on the pending queue alone will not prevent replay after a job leaves that queue. Keep failures recoverable instead of deleting their only durable queue record unconditionally.

## Priority 2: storage, constraints and performance

### Bound and isolate the web cache

`web_cache` occupies approximately **138.63 MiB**, compared with **90.71 MiB** for `cards` itself, measured using SQLite `dbstat`. Cached bodies total approximately 138.18 MiB. None of the 419 current entries was older than 30 days at inspection time, so a 30-day expiry alone would not shrink this sample much.

`recommendations.py` checks freshness when reading, but has no general size bound or expired-entry pruning. Large cached responses share the same file and writer contention as user collection data.

**Improve:** Give the cache a separate disposable database, a maximum size or row budget, and expiry/eviction. Measure the largest response sizes before choosing limits. Deleting rows makes pages reusable; returning disk space requires a separate maintenance decision. Do not delete the current cache merely because it is large.

### Enforce local relationships and value ranges

Inspected tables have no declared foreign keys. Source code also relies on manual cleanup for inventory children and deck cards. In a temporary database, `DeckManager.set_card` accepted a card for nonexistent deck ID 999.

**Improve:** Consider `deck_cards.deck_id -> decks.id` and inventory child `inventory_id -> inventory.id`, plus quantity and board constraints. Enable foreign-key enforcement on **every connection**; see [SQLite foreign-key documentation](https://www.sqlite.org/foreignkeys.html). Add endpoint parent validation as well.

Plan the migration alongside backup restore and transfer code: their current insertion/deletion order may conflict with immediate constraints. Preserve thumbnail cleanup separately; deleting a database row does not remove a file. Avoid a naive foreign key from every `card_id` to Magic's refreshable `cards` table because references can outlive catalog refreshes and inventory is shared across games.

### Optimize substring searches only after benchmarking

The existing composite printing index serves exact lookup well. The measured substring query scans all 112,765 catalog rows, and deck-builder search also groups printings by name and computes cheapest prices.

**Improve:** Benchmark real search requests and their query plans. Consider FTS5 with a trigram tokenizer for substring semantics; ordinary token-based full-text search changes matching behavior. SQLite documents trigram substring support and its limitations in [FTS5 documentation](https://www.sqlite.org/fts5.html). Consider a derived one-row-per-name search table if repeated grouping is a measured bottleneck. Rebuild derived indexes alongside catalog refreshes and preserve current matching rules.

Potential indexes to measure as their tables grow:

- `inventory_sources(station, inventory_id)` for station filtering; its existing primary key starts with `inventory_id`.
- `review_queue(game, station, id)` for a station's oldest item and counts; also assess all-station queries separately.
- `decks(game, updated_at DESC, id DESC)` for deck listing, though ten local decks do not justify urgency.

`idx_deck_cards_deck` may overlap with the leading `deck_id` column of the UNIQUE index on `(deck_id, card_name, board)`. Compare plans and index sizes before removing it. Avoid adding indexes that do not benefit measured queries: they increase import and write cost.

### Standardize connections and migrations

Connection configuration varies by manager. WAL persists at file level, while settings such as synchronous mode and foreign-key enforcement are connection-specific. Shared connections with `check_same_thread=False` depend on consistent locking and transaction ownership. Catalog population writes through its connection outside its manager lock for much of the import loop, which deserves review before adding more background writers.

**Improve:** Centralize connection setup, define transaction ownership, close short-lived connections explicitly, and introduce a schema migration version or migration table. Validate upgrades on copies of older databases, including the local schema missing station ownership tables. Preserve the existing timeout and WAL approach unless measurement establishes a better configuration.

For user-authored data, evaluate whether the durability tradeoff of `synchronous=NORMAL` matches expectations during power loss. A separate catalog/cache database would allow different durability settings from the collection. This is a design decision, not evidence of current corruption.

## Existing strengths and cautions

- Queries generally use bound values, with indexes for exact printing and inventory keys.
- Collection uniqueness includes game, printing fields, condition, finish and location.
- Transfers already use source intent and destination receipts to recover across two databases. Preserve that protocol when changing transaction handling.
- `scripts/backup.sh` uses SQLite's backup API to snapshot each file rather than archiving live WAL files directly. However, separate file snapshots are not a single coordinated application snapshot; recovery journals and image consistency still matter during concurrent activity.
- Inventory stores descriptive fields and prices independently of the downloadable catalog. This supports user data surviving catalog changes; normalize it only with a clear fallback and price-refresh policy.

## Recommended order

1. Eliminate write transactions from station-filtered reads.
2. Make catalog replacement, deck imports and board moves atomic, with failure regression tests.
3. Repair quantity/ownership reconciliation and capture idempotency.
4. Add versioned migrations and carefully staged local foreign keys/value constraints.
5. Isolate and bound the cache.
6. Benchmark search and apply only justified index/search changes.

The existing test suite passed all **25 tests** when run at the end of this investigation. The failure cases described above are additional isolated reproductions, not tests currently covered by that suite. No fixes were applied as part of this review.
