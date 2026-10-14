# Database fix plan

Date: 2026-10-09

This document translates [DATABASE_REVIEW.md](DATABASE_REVIEW.md) into implementation tasks. It was written as a plan and is now a **record**: the status table below says what became of each task (2026-10-14). The task texts further down are the plan as written and say "should"; they are kept for the reasoning.

## Status (2026-10-14)

| # | Task | Status | Where, and what shows it |
|---|---|---|---|
| 1 | No writes in station-filtered reads | Done | `InventoryManager._station_rows` only reads; ownership cleanup moved into the edits that remove entries (`_delete_entries`, `_trim_sources`). `tests/test_ownership.py`, `tests/test_database.py` |
| 2 | Atomic catalog replacement | Done | `CardDatabase.replace_table` (`BEGIN IMMEDIATE`, drop, rename, indexes, `card_data_info`), imports on a connection of their own, refused when empty / missing / under `MIN_SHARE`; one refresh at a time. `tests/test_database.py` (`CardDataImport`) |
| 3 | Transaction ownership for manager writes | Done | `storage.Transactional`: `InventoryManager` (`@_writes`) and `DeckManager` (`_transaction`) - the outermost method commits, a failure rolls back everything, files are deleted after the commit (`_committed`); bulk edits are all or none. `tests/test_inventory_edits.py`, `tests/test_storage.py` |
| 4 | Quantity and station ownership reconciled | Done | `_trim_sources`, `_move_sources`, `_take_copies`; the same for batches (`inventory_batches`: `_take_from_batches`, `_trim_batches`). `tests/test_ownership.py`, `tests/test_batches.py` |
| 5 | Durable capture identity, idempotent settlement | Done | `capture_key` on `inventory_captures`, `applied_captures` written in the commit that adds the card, `unfollowed_captures`, `pending.py`. `tests/test_capture_sync.py`, `tests/test_station_uploads.py` |
| 6 | One connection setup, versioned migrations | Done | `storage.connect` (WAL, row factory, foreign keys), named migrations (`applied`, `record`), `storage.rebuild` (backup, one transaction, checked, `prepare`), `storage.upgrade` for numbered steps. `tests/test_storage.py`, `tests/test_database.py` |
| 7 | Relationship and value constraints | Done | CHECK / `REFERENCES ... ON DELETE CASCADE` on the inventory, captures, sources, batches and deck tables; a file made by the standalone program (triggers) is given them too. `tests/test_constraints.py` |
| 8 | External-response cache moved and bounded | Done | `data/web_cache.db`, bounded by age and size, out of `scripts/backup.sh` (`recommendations.py`). `tests/test_database.py` |
| 9 | Benchmarks and search indexes | Done, measured | `scripts/benchmark_db.py`; `idx_deck_cards_deck` removed. The other candidate indexes and FTS5 are **not** adopted, on purpose - the reasons and numbers are in PROGRAM_DOCUMENTATION.md, *Database* |
| 10 | Coordinated full backups and cleanup | Done | `maintenance_barrier` (capture settlement, "Add to collection", Arena import, cleanup) and `backups.create_full`; a restore is one critical section with its recovery backup (2026-10-14). `tests/test_database.py` (`FullBackup`), `tests/test_restore_locking.py` |

Found after the plan, and not in it: the batches model (`inventory_batches`, one row per batch and entry, replacing the single `added_at` / `added_quantity` of an entry), and the standalone program's files (rules kept as triggers) failing the constraints migration. Both have tests (`tests/test_batches.py`, `tests/test_constraints.py`).

Not done from "Delivery sequence and completion criteria": subprocess termination at commit boundaries (only injected exceptions were used), and a restore of a full backup checked in an isolated directory by a script (the tests do it on temporary folders).


## 1. Remove writes from station-filtered reads

**Files:** `inventory.py`, `tests/test_ownership.py`.

Remove `_drop_orphan_sources()` from `_station_rows()`. Its inner join already excludes sources without an inventory parent, so cleanup is unnecessary to return correct rows. Keep `_station_rows`, `get_all_cards`, `get_stats` and other query paths free of mutations.

Audit every mutation that deletes inventory rows. Clean up source rows inside that mutation's transaction until foreign-key cascades are introduced. Include undo, batch removal, bulk deletion, CSV replacement, merges, clearing and transfers. Never add a commit to a read method as a workaround: that could commit unrelated pending changes.

**Acceptance criteria:**

- A station-filtered query on a clean connection leaves `conn.in_transaction` false.
- A second connection can write immediately after that query, without an intervening commit.
- Deleting an entry removes its source rows in the same transaction.
- Existing station filtering, ownership and transfer tests pass.

## 2. Make catalog replacement atomic

**Files:** `database.py`, game-specific catalog importers using `replace_table`.

Keep staging imports separate from the live table. Give the importer its own SQLite connection so import commits cannot accidentally commit work from another method sharing the catalog connection. Serialize competing refreshes of the same game.

Before publishing, verify the staging schema, primary-key uniqueness, required values, inserted/error counts and an acceptable row count. Abort publication if the import is incomplete or unexpectedly empty; preserve the old catalog. Define any allowed size-drop threshold explicitly and report rejected imports.

After staging writes are committed, publish through one explicitly owned transaction:

```text
BEGIN IMMEDIATE
    DROP live table
    RENAME staging table to live table
    CREATE live indexes
    UPDATE card_data_info for this catalog
COMMIT
```

On any exception, roll back the transaction and report failure. Do not call a metadata helper that commits inside this transaction; provide an internal helper without a commit. Validate dynamically supplied table identifiers against the application's known catalog tables.

**Acceptance criteria:**

- Missing or invalid staging data leaves the original catalog and metadata unchanged.
- Inject failures after DROP, after RENAME and during index creation; reopening the database still exposes the original catalog and indexes.
- A successful publish exposes the new catalog and matching metadata together.
- Concurrent readers see a complete old or new catalog, and collection writes remain usable during staging population.

## 3. Define transaction ownership for manager writes

**Files:** `decks.py`, `inventory.py`, `database.py`, `review.py`, `pending.py`.

Public mutation methods should own the manager lock and transaction. Private SQL helpers should execute statements without committing. Every exception must roll back the operation before it returns control to the caller. Avoid nested connection context managers that commit an outer operation prematurely; use a single outer transaction or deliberate savepoints when nesting is required.

Apply this first to the reproduced failures:

### Deck import

Parse and validate the complete input before deleting or inserting anything. Check parent deck existence, nonempty names, valid board values and valid quantities. Preserve documented import defaults explicitly. Reject invalid replacement input; require an explicit empty replacement action to clear a deck.

Perform replacement deletion, all inserts and `_touch` in one transaction. A failed import must leave the original deck intact, including its timestamp, with no pending transaction for later edits to commit.

### Deck board move

Introduce an internal `_set_card` helper without a commit. Under one lock and transaction, validate boards and deck, read the source/destination quantities, update the destination, remove the source and touch the deck once. Preserve the printing selection according to an explicit policy, including when the destination already exists.

### Inventory CSV import

Normalize headers once and use the normalized keys for both format detection and parsing. Validate rows before replacement. For large files, use a staging table or parsed temporary file rather than an unbounded in-memory list.

Reject a replacement containing invalid rows or no valid rows unless the user explicitly requested an empty replacement. Apply the validated replacement in one transaction. Keep any partial-import mode separate and clearly reported. Defer capture-file deletion until the database commit succeeds; retain failed cleanup for retry.

**Acceptance criteria:**

- Invalid deck input followed by an unrelated notes edit does not persist partial import changes.
- Failure between destination update and source removal leaves the original board counts unchanged.
- CSV headers differing only in case or surrounding whitespace behave consistently.
- Rejected CSV replacement preserves quantities, captures and station sources.
- Injected SQL failures roll back data and leave no unintended open transaction.

## 4. Reconcile inventory quantity and station ownership

**Files:** `inventory.py`, `tests/test_ownership.py`.

Create one allocation helper used whenever copies are removed or moved. Return the affected source quantities and capture IDs before applying changes. Keep all database updates in the caller's transaction.

Use the existing newest-copy behavior as the starting policy: consume newest captures first, decrement their known station ownership where available, then use a deterministic policy for copies without captures. Represent unassigned copies implicitly as total quantity minus assigned source quantities. Do not assign unowned copies to a station merely to make totals match.

For older overclaimed rows, report inconsistencies and repair them using a documented deterministic policy; do not invent historical ownership. Run repair as a migration with before/after totals recorded.

Verify these invariants after quantity edits, splits, merges, undo, batch removal, clearing and transfers:

```text
inventory.quantity > 0 for retained entries
inventory_sources.quantity > 0 for retained source rows
SUM(source quantities for an entry) <= inventory.quantity
capture rows reference an existing entry
```

Also reconcile `added_quantity` during splits and merges. If reliable removal of multiple historical batches is required, introduce a separate batch-allocation table; a single latest `added_at` / `added_quantity` pair cannot represent every historical contribution.

**Acceptance criteria:**

- Reducing A=2 and B=2 from four total copies to two leaves at most two assigned copies overall.
- Clearing or transferring one station cannot consume copies assigned to another.
- Splitting then removing a batch removes exactly that batch's allocated copies.
- Capture and source allocation agree for copies with a known captured station.

## 5. Persist capture identity and make settlement idempotent

**Files:** `pending.py`, `app.py`, `inventory.py`, `review.py`, `station_client.py`.

Introduce a durable capture identity scoped to its sender, such as `(sender_id, capture_id)`. Assign legacy queued captures a stable identity once during migration. Use a unique constraint to arbitrate concurrent acceptance; an in-memory check followed by insertion is insufficient.

Persist capture states such as `accepted`, `processing`, `completed` and `failed`, including a retryable error and terminal outcome. Save accepted images durably before acknowledging acceptance; a failed save must not leave an accepted job pointing to a missing image. Write files through temporary paths and rename them into place, with recovery for files left between filesystem and database operations.

Add an applied-capture receipt in the database that receives the result. Insert that receipt in the **same transaction and connection** as the inventory addition or review insertion. A replay that finds the receipt returns the recorded outcome without adding a second result.

Settlement then follows:

1. Read the durable job and stable identity.
2. Check for an existing result receipt before applying a new result.
3. Commit the result and its receipt together at the destination.
4. Record completion in the queue database.
5. Clean up expendable files after completion is durable.

If a crash occurs between steps 3 and 4, recovery finds the receipt and completes the job without duplicating the result. Preserve failed jobs for retry or explicit resolution. On the client, remove the local image only after explicit durable acceptance/completion according to the protocol; arbitrary HTTP responses below 500 are not sufficient.

Define receipt retention to cover the supported retry window. Preserve deduplication across collection transfers and restores: a receipt in scanned inventory must not be dropped when its card moves. Retain an authoritative completed identity and document restore semantics so old client retries cannot silently re-add restored captures.

**Acceptance criteria:**

- Concurrent uploads with the same sender and capture identity create one job/result.
- Save/enqueue failures allow a later successful retry.
- Restart after result commit but before queue completion does not duplicate inventory or review entries.
- Restart after completion returns the recorded outcome for a retry.
- Processing failure retains a recoverable job and image.
- Transfer, restore and receipt cleanup preserve the documented retry guarantees.

## 6. Centralize connection setup and version migrations

**Files:** new shared SQLite helper, all database managers, startup code.

Centralize timeout, row factory, connection closing and connection-specific PRAGMAs. Keep WAL for the existing databases. Set synchronous mode deliberately per database role; evaluate stronger durability for user-authored data separately from downloadable catalog/cache data. Do not change durability silently.

Use one migration owner per database file. Record successful migrations with `PRAGMA user_version` or a migration table. Shared managers must not each independently assign a competing version to `cards_database.db`. Run migrations before starting request handlers and worker threads.

For each migration:

1. Create a consistent backup and verify it can be opened.
2. Check the starting schema and data invariants.
3. Apply transactional schema/data changes.
4. Verify counts, total quantities, indexes and constraints.
5. Record the new version only on success.

Test upgrades from the actual older schema missing `inventory_sources` and `inventory_captures.station`. Make repeated startup safe. Do not backfill invented station ownership for old rows.

## 7. Add relationship and value constraints

**Dependencies:** transaction ownership and versioned migrations.

Rebuild affected SQLite tables through versioned migrations to add:

| Table | Proposed constraints |
| --- | --- |
| `deck_cards` | Foreign key to `decks(id)` with cascade on deletion; positive integer quantity; board in `commander`, `main`, `side`. |
| `inventory_captures` | Foreign key to `inventory(id)` with cascade on deletion. |
| `inventory_sources` | Foreign key to `inventory(id)` with cascade on deletion; positive integer quantity. |
| `inventory` | Positive integer quantity for retained rows; nonnegative nullable `added_quantity`, bounded by quantity after reconciliation. |

Use type-aware checks if retaining SQLite's flexible typing, such as `typeof(quantity) = 'integer' AND quantity > 0`. Review all current mutation sequences before enforcing them: decrementing a row to zero and deleting it afterward must become a direct delete or another sequence that never violates an immediate CHECK.

Enable `PRAGMA foreign_keys=ON` on every relevant connection before beginning transactions. Validate referenced parents at API boundaries for clear error responses. Preserve AUTOINCREMENT sequences and row IDs during rebuilds.

Update backup restore to delete children before parents and insert parents before children, or use deliberately deferred constraints within one transaction. Preserve compatibility with backups lacking ownership tables. Cross-file transfers must preserve their existing intent/receipt recovery protocol. Cascades delete rows, so thumbnail-file removal still needs explicit post-commit cleanup.

Do not add a universal inventory `card_id` foreign key to the Magic catalog: manual entries, other games and catalog refreshes need to remain supported.

**Acceptance criteria:** orphan inserts and invalid quantities fail; parent deletion removes child rows; restore and transfer tests pass; `PRAGMA foreign_key_check` returns no violations after migration and restore.

## 8. Move and bound the external-response cache

**Files:** `recommendations.py`, configuration, backup/maintenance documentation.

Move `web_cache` to a separate disposable file such as `data/web_cache.db`. Use an explicit expiry timestamp, response byte count and optional last-access timestamp. Keep cache reads/writes isolated from collection transactions.

Measure response sizes, then configure an overall byte budget and maximum cacheable response size. Evict expired entries first, then least-recently-used or oldest entries until within budget. Skip caching an oversized response while allowing the current request to use it. Cache write/eviction failures should not discard an otherwise usable fetched response.

Migrate existing cache records once, verify the new cache can serve them, and only then remove the old table. The sample has no entries older than 30 days, so expiry alone is insufficient. Deleting the old table frees pages for reuse; schedule optional offline compaction separately if reclaiming disk space is worthwhile. Exclude disposable cache content from essential user-data backup requirements.

**Acceptance criteria:** cache size stays within the configured payload budget; expired/oversized entries behave correctly; a cache write cannot hold the collection's writer lock; disabling or losing the cache does not prevent collection/deck use.

## 9. Benchmark and improve search/indexes

**Files:** `database.py`, `inventory.py`, `review.py`, `decks.py`, benchmark fixtures.

Record baseline plans and latency for exact printing lookup, normalized name substring search, deck-builder grouped search, station filtering and review dequeue. Include representative data sizes and repeated measurements; distinguish cold and warm cache behavior.

Evaluate these candidates individually:

- `inventory_sources(station, inventory_id)` for station-first filtering.
- `review_queue(game, station, id)` for station-specific oldest-item queries; measure all-station queries separately.
- `decks(game, updated_at DESC, id DESC)` if deck counts grow enough to matter.
- Removing `idx_deck_cards_deck` only if the existing composite UNIQUE index provides equivalent useful plans.

For substring matching, prototype FTS5 trigram indexing against the existing normalized fields. Preserve accent/case handling, flavor names, double-faced names and ranking. Keep a fallback for short queries unsupported by the chosen index strategy and check FTS5 availability in deployment images. Compare a derived one-row-per-name table if grouping printings dominates latency.

Publish catalog and derived search structures consistently. A successful optimization must improve measured requests without changing identification confidence or accepted matches. Do not introduce every proposed index by default.

## 10. Coordinate full backups and filesystem cleanup

**Files:** `scripts/backup.sh`, `backups.py`, capture/review cleanup paths.

Preserve the SQLite backup API for each database snapshot. For a complete application backup, provide a maintenance barrier that pauses capture settlement, inventory transfers and destructive file cleanup while recording related database snapshots and referenced files. A sequence of individually valid snapshots is not automatically one consistent cross-file application state.

Keep database changes transactional and file deletion retryable after commit. Verify a restored snapshot in an isolated directory, including move-journal recovery and image references. Avoid holding a database write transaction open while archiving large files.

## Delivery sequence and completion criteria

Implement as independently reviewable changes:

1. Read-path locking fix and two-connection regression test.
2. Atomic catalog publication, deck import/move and CSV replacement.
3. Ownership/batch reconciliation.
4. Shared connection setup and versioned migrations.
5. Durable capture acceptance and idempotent settlement.
6. Constraints with compatible restore/transfer ordering.
7. Cache isolation and bounded eviction.
8. Measured search improvements and coordinated full backup support.

For each change, run the relevant failure tests and the full existing suite. For migrations, compare table counts and total card quantities on copies of real older databases, then run integrity and foreign-key checks. For capture recovery and transfer changes, use subprocess termination at commit boundaries as well as injected exceptions.

Completion means the reproduced failures in `DATABASE_REVIEW.md` have regression coverage, rejected operations leave prior state intact, retries do not duplicate results, and migrations preserve user data. Performance work is complete only when its benchmark results justify the selected changes.

## References

- [SQLite transactions](https://www.sqlite.org/lang_transaction.html)
- [SQLite foreign-key support](https://www.sqlite.org/foreignkeys.html)
- [SQLite FTS5 and trigram substring matching](https://www.sqlite.org/fts5.html)
