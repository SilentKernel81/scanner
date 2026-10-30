# Plan: the trade pipeline's review - a bug, small fixes, improvements (2026-10-24)

**Status:** parts 1 and 2 done 2026-10-24 (`tests/test_trades.py`: `StaleRequests`, `TradeRoutes` - the reproduction of
part 1 through Flask's test client; `tests/test_others.py`: the receive count; both stale cases also driven in a headless
browser on copied data; the confirm's check is a fingerprint of the trade's content since a Codex review the same night -
totals missed a swap). Part 3 not started (the owner's choice). Before: Written from a review of 2026-10-24 (the trade store `core/trades.py`, its routes
`server/trades_api.py`, the triggers `inventory.TRADE_TRIGGERS`, receiving from other people's collections
`server/others_api.py`, the Trades tab in `collection.js`). Part 1 was reproduced on a server with copied data;
parts 2 and 3 were found by reading the code, nothing of them was measured.

What is solid and stays as it is: the database itself refuses to delete an entry a trade holds or to lower it below the
copies held (`trade_keeps_entry`, `trade_keeps_copies`); a confirm is one transaction and is refused while a change of
price source is half done; received cards are a record apart from the collection; bulk changes of held entries are
refused before anything changes; the game of a trade and of an imported collection are checked.

## 1. Bug: a stale "Cancel trade" deletes a confirmed trade from the history

`trades_api.trade_item` answers `DELETE /api/trades/<id>` for both buttons: it tries `Trades.cancel`, and when that raises
`TradeError` (the trade is not open) it calls `Trades.delete` - "Delete from history". The page sends the same request for
**Cancel trade** and for **Delete from history** (`collection.js`: `tradeAction`, `cancel` and `forget`).

**Reproduced** (2026-10-24, a server on copied data): a trade of one entry made with `POST /api/trades`, confirmed with
`POST /api/trades/<id>/confirm` (as a second tab or the phone would), then the `DELETE` a tab still showing it open sends
for Cancel trade: `{"success": true}`, and the confirmed trade was gone from `GET /api/trades`. The copies had left the
collection with the confirm either way; what is lost is the record of what left and its value.

**Fix:** the request says what it means - `DELETE /api/trades/<id>?action=cancel` and `?action=forget` (or two routes) -
and the server refuses a cancel of a trade that is not open ("This trade was confirmed meanwhile") and a forget of one
that is. The page reloads the trades after the refusal. A test through Flask's test client: confirm, then cancel ->
409 / 400 and the record still listed; forget of an open trade refused. `tests/app_surface.json` recorded again if a
route is added.

## 2. Small fixes

1. **Receive overstates the copies.** `Trades.receive` counts `copies += quantity` - what was asked - while the row is
   capped at what the other person has (`MIN(quantity + excluded.quantity, excluded.most)`). Count what the row gained
   (its quantity after minus before) so the message says what went in.
2. **Receiving does not tell other pages.** `POST /api/trades` calls `collection_changed()` (the event
   `collection_updated`); `others_api.receive_from_other_collection` and `trades_api.trade_receive` do not, so another
   browser shows the old trade until it is reloaded. Call it in both.
3. **Confirm shows this page's totals.** The dialog says "Its N cards ($X) are removed" from the page's last copy of the
   trade; the server confirms what the trade holds when it arrives. Reload the trade before asking (or send the
   quantity the dialog showed and refuse a confirm whose trade changed since).
4. **Cancel's wording for a trade that only receives**: "Its 0 cards stay in your collection and are free again". Say
   "Nothing of your collection is in it" when `trade.quantity` is 0.

## 3. Improvements (each the owner's choice)

1. **Move set-aside cards.** Moving a whole held stack to another location is refused ("take them out of the trade
   first") because a move deletes the entry and makes or merges into another. Packing a sale - moving the cards into a
   "To ship" box - is when that is wanted. In the move's transaction, point the trade's rows at the entry the copies
   went to (`trade_cards.inventory_id`), merging two rows of one trade (`UNIQUE (trade_id, inventory_id)`), before the
   old entry is deleted, so the trigger lets it go. Bulk move too (`bulk_update`, which refuses held entries today).
2. **A backup before Confirm.** Confirm "can't be undone". `backups.create` before `Trades.confirm` (kept like the other
   automatic ones) makes a wrong confirm recoverable by a restore.
3. **Say when an edit changes a card a trade holds.** Editing a held entry's finish, condition or printing changes it
   where it is (documented as intended: it stays in its trade), so the trade then gives another card than the one
   agreed. A notice in the edit dialog - "This card is set aside for the trade X" - from `card.trades`.
4. **Received cards and the collection.** CLAUDE.md says confirming adds nothing to the collection and to keep it that
   way ("Other people's collections are not the collection"). The only option kept here: after a confirm, a button
   *Send what I received to review*, which puts each received card in the review queue to be checked and added like a
   scanned one - nothing reaches the collection without the person.
5. **The sale picture of a confirmed trade.** Only open trades have it: a confirmed trade's cards have no entry, so no
   scans. Fine for selling (cards are pictured before they go); noted so it is not taken for a bug.

## Order

Part 1 first (data is lost), then part 2 (one change, one test each), then part 3 in the owner's order. Each step: its
test, `tests/test_app_surface.py` if a route changes, PROGRAM_DOCUMENTATION's *Trades*, FEATURES.md, TEST_CASES.md
(TRD-xx), CLAUDE.md's Trades row, `?v=` raised on a changed script.
