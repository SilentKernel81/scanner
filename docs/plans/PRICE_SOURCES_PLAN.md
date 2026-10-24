# Plan: prices from MTGJSON beside Scryfall's (2026-10-23)

**Goal:** the collection, the scanned cards, trades and decks can be valued from another shop than the one
Scryfall's bulk file gives (TCGplayer, in US dollars): Cardmarket (EUR), Card Kingdom (also its buylist),
Mana Pool, or MTGO prices - all of them from **one** MTGJSON file a day.

**Status:** phase 1 done (2026-10-23): Scryfall's etched and EUR prices kept (as three columns of `cards`, not rows of
`card_prices` - see phase 1 below), the etched price used, the entries priced again after each card data update
(`reprice_collections`); `tests/test_prices.py` (10 tests), on temporary databases and copies of this checkout's data;
run once after a real download on copied data (2026-10-24: 1,452 of 1,595 entries repriced - PROGRAM_DOCUMENTATION). Phase 2a done 2026-10-24: the dropdown (Settings > Card data > Prices) with Scryfall's two sources, TCGplayer USD and
Cardmarket EUR, through shown columns of their own (`cards.price`, `price_foil`, `price_etched` - the second of the two ways
named under phase 1 below); the currency on every page and in the CSV. Phase 2b (MTGJSON's shops) and 3-4 not started. What is under *Measured* was measured on 2026-10-23 on that day's
files and a copy of this checkout's card data (112,765 printings).

## The licence (read 2026-10-23)

| | What it says | Where |
|---|---|---|
| MTGJSON's code and site | **MIT**, "Copyright © 2018 – Present, Zach Halpern"; the licence page: "By using this website and its content you agree to the following License" - the MIT text, which allows to "use, copy, modify, merge, publish, distribute, sublicense, and/or sell" on condition that "the above copyright notice and this permission notice shall be included in all copies or substantial portions" | [mtgjson.com/license](https://mtgjson.com/license/), [LICENSE.txt](https://github.com/mtgjson/mtgjson/blob/master/LICENSE.txt) |
| The FAQ | "MTGJSON is free and open source under the MIT License"; asks for donations; no rule for apps, no attribution asked beyond MIT's notice | [mtgjson.com/faq](https://mtgjson.com/faq/) |
| The prices themselves | the home page: "We keep a **limited** history of all card prices provided by our partners"; its footer: "Third-Party content, links and referrals are provided as is and is subject to their own terms, rights and other legal notices" and "All other content © 2018 – Present, Zach Halpern. All rights reserved." The partners' links carry affiliate codes (`partner=mtgjson`, `affiliate_id=mtgjson`) | [mtgjson.com](https://mtgjson.com/) |

**What that means here.** For this project - PolyForm Noncommercial, a personal collection tool - MIT is no
obstacle: it is more permissive than the project's own licence, and MTGJSON is already used (preconstructed
decks, `recommendations.py`) and named in README *Acknowledgments*. Its notice is satisfied by naming it there
for prices too. **What stays open** is the footer's "third-party content ... subject to their own terms": the
pages do not say whether the price data counts as that, and neither TCGplayer's, Cardmarket's, Card Kingdom's
nor Mana Pool's terms were read. That matters only for a product (`PRODUCT_PLAN.md`, which tracks Scryfall's
terms the same way): **to ask MTGJSON** (their Discord, which the FAQ points data users to) whether the
partners' prices may be shown in an app that is sold, and whether a server may hand them on to phones.

## Measured (2026-10-23)

| | |
|---|---|
| Files | `AllPricesToday.json.xz` 4.3 MB (`.gz` 5.5 MB); `csv/cardIdentifiers.csv.xz` 11 MB; `csv/tokenIdentifiers.csv.xz` 0.7 MB; `Meta.json` (`date`, `version` `5.3.0+20261023`). All had `Last-Modified` 06:11-06:12 GMT; the FAQ: builds start 1:00 AM EST and go live 9:00 AM EST. `AllPrices` (90 days of history) is 147 MB - not wanted for now. `AllIdentifiers.json` (234 MB) is not needed: the two CSV files hold the same mapping |
| Providers (113,062 priced uuids) | paper: `cardmarket` (EUR, 102,459), `manapool` (USD, 101,651), `tcgplayer` (USD, 101,402), `cardkingdom` (USD, 99,109); mtgo: `cardhoarder` (USD, 63,028). `cardsphere`, which MTGJSON's docs list, has no prices in the file; `manapool`, which they do not list, has |
| Shape | `data[uuid][paper\|mtgo][provider] = {currency, retail: {finish: {date: value}}, buylist: {...}}`; finishes `normal`, `foil`, `etched`; a buylist only from `cardkingdom` (83,487 points) |
| Mapping to our ids | every price uuid is in one of the two CSV files (`scryfallId` column). 5,343 Scryfall ids have more than one uuid: **the faces of double-faced / split cards** (MTGJSON has a card per face, `Twinblade Geist // Twinblade Invocation` = 2 uuids). 2,638 of them have prices on more than one face, and **no provider + finish is priced differently between faces** (0 of them) - one face sometimes has fewer providers, so the faces are merged by union |
| Coverage | **106,833 of our 112,765 printings** (94.7%) get at least one MTGJSON price; 97,312 (86.3%) have a Scryfall USD price today |

## Today (what changes)

`database.py:_fill_import` keeps `prices.usd` / `usd_foil` as `cards.price_usd` / `price_usd_foil`, and
drops Scryfall's `eur`, `eur_foil`, `usd_etched` and `tix`. Every reader takes those two columns: `mtg.py`
`card_payload` / `inventory_fields` (foil and surge foil take the foil price; a missing one falls back to the
other), `database.py:807` (`cheapest`, raw SQL), `similar`, deck and trade totals. Each inventory entry keeps
its **own copy** (`inventory.price_usd`), taken when it is added and changed only by a finish or printing
change - **never refreshed**: `set_price` is reached only for games with `fetches_prices`, which Magic is not.
Etched copies get no price at all when the printing is etched-only.

## The design

### 1. A `prices` table, filled once a day

In the card database, beside `cards`:

```
card_prices(card_id TEXT, source TEXT, currency TEXT, finish TEXT, kind TEXT, value REAL,
            PRIMARY KEY (card_id, source, finish, kind))
-- source: 'scryfall' | 'tcgplayer' | 'cardmarket' | 'cardkingdom' | 'manapool' | 'cardhoarder'
-- finish: 'normal' | 'foil' | 'etched' (MTGJSON's words; Scryfall's usd / usd_foil / usd_etched map to them)
-- kind:   'retail' | 'buylist'
```

A new module `core/prices.py` (core: no web app) with `import_mtgjson(db, progress)`:
download the three files (conditional GET on `Last-Modified`: nothing fetched twice a day), map uuid ->
`scryfallId` from both CSV files, keep only ids that are in `cards`, merge the faces by union, take each
point's single date, write a staging table and swap it with `replace_table` - as `_populate` does, so
scanning goes on meanwhile and a failure keeps yesterday's prices. Its own `card_data_info` row
(`source_updated` = `Meta.json`'s `date`). Not through `recommendations._get` / `web_cache.db`: that cache
is disposable and made for small requests.

Scryfall's prices go into the same table during the card data import (`source = 'scryfall'`, also the EUR
and etched values it drops today), so the default source works without MTGJSON at all.

The table stays out of the picture index's printings table (`index_printings_table`): a price update must
keep changing nothing a phone downloads (PROGRAM_DOCUMENTATION, around "an update that only refreshes
prices").

### 2. One chosen source, applied to `cards` - the readers stay as they are

**Done otherwise (2026-10-24, see Status):** the chosen source goes to shown columns of its own (`cards.price`, `price_foil`,
`price_etched`), never into `price_usd` & co. - Scryfall's own prices are kept, so a switch back needs no download.

**Choice:** the setting `price_source` (`data/settings.json`, default `scryfall`; Settings > Card data) is
*applied*: after each price import, after each card data import (which rewrites `cards` with Scryfall's
values) and when the setting changes, one statement copies the chosen source's retail `normal` / `foil` into
`cards.price_usd` / `price_usd_foil`, falling back to Scryfall's where the source has no price.

**Why this and not "every reader asks `card_prices`":** there are at least six readers of the two columns
(`card_payload`, `inventory_fields`, `cheapest`'s SQL, `similar`, decks, trades), and the Android app's
standalone mode prices from Scryfall itself (`scryfall/Scryfall.kt`, `inventory/Export.kt`). Applying the
choice to the columns changes none of them, and switching back and forth is a re-apply, never a download.
**The cost:** the columns are named `_usd` and may then hold euros - the currency is recorded beside the
choice (`card_data_info` or the setting) and shown, and renaming the columns is left for later (a migration
through `storage.rebuild`, and every reader).

Etched: a new column `price_etched` (added on startup like every `CARD_COLUMNS` entry), and
`inventory_fields` takes it for an etched printing before the foil price.

A buylist source (Card Kingdom's) is a second, separate value - "what a shop would pay" - shown beside the
retail one on the collection page, never written into `price_usd`. Optional, last phase.

### 3. Revalue the collections

`InventoryManager.reprice(game, prices_of)` (`@_writes`, one transaction): every entry with a `card_id` gets
`inventory_fields(card, finish)['price']` of today's `cards`; entries without one (CSV imports) keep theirs.
Run on `inventory` and `scan_inventory` after each apply, then `socketio.emit('inventory_prices_updated')`
(the pages already listen for it). `arena_inventory` stays without prices (`arena_api.py`: on purpose).
Open trades: the copies they hold are revalued like any entry; a confirmed trade's value is history and
stays. `others.db` (someone else's imported collection) keeps the prices of their file - their price is
part of what they offered.

**To decide (the owner's):** whether an entry should follow the market (this plan) or keep what it was worth
when added (a purchase price). If both are wanted: a new column `price_added`, filled once, and the existing
one revalued.

### 4. When it runs

A third "due" question in `card_data.py:run_update_schedule` beside the card data and the index:
`prices_every_hours` (default 24, off until switched on like the others), never before 14:00 UTC (MTGJSON's
files are live by 9:00 AM EST), a failure retried after an hour. A button *Update prices now* (Settings >
Card data), guarded like the other card data buttons.

### 5. Currency on the pages and in exports

`library.js:10` `money()` writes `$`: it takes the currency from the stats / card payloads (`€` for
Cardmarket). The CSV export's column `Price (USD)` (`games/base.py:12`) is pinned by the Android app's
`ExportParityTest`: it stays as it is unless both change together - a column `Currency` after the last one is
the smaller change. Moxfield's `Purchase Price` column (`mtg.py:46`) gets the number only.

## Phases

1. **Done 2026-10-23.** Scryfall's dropped values, as columns of `cards` (`price_usd_etched`, `price_eur`,
   `price_eur_foil`) rather than `card_prices` rows: they come in the same file as the cards and are swapped in by the
   same commit (`replace_table` swaps one table), with no second staging table. `inventory_fields` for etched;
   `InventoryManager.reprice` and its call after a card data download (`server/card_data.py`: `reprice_collections`).
   **For phase 2 this changes one thing:** the apply step must not overwrite Scryfall's own columns (a switch back to
   Scryfall would have nothing to go back to until the next download). Either the chosen source is read where a row
   becomes a card (`database._format_card_result`, plus `cheapest`'s SQL and `search_cards_by_partial_name`), or the
   apply writes to columns of its own - to decide when phase 2 starts.
2. `core/prices.py`: the MTGJSON import, the button, the apply step and the setting with its currency.
3. The schedule's third question; `money()` and the exports' currency.
4. (Optional) The buylist beside the retail value.

Each phase: `tests/test_prices.py` on temporary databases (the import of a hand-written 3-card price file -
one double-faced card with a provider on one face only, one etched-only printing, one id not in `cards`;
the apply with its fallback; `reprice` leaving CSV entries and Arena alone; a failed import keeping the old
table), the route list recorded again (`tests/test_app_surface.py --record`), and the docs this repository
expects: PROGRAM_DOCUMENTATION (the schema table "Prices", Card data), FEATURES.md, TEST_CASES.md, CLAUDE.md
(Where Things Live), README *Acknowledgments* ("prices: Scryfall and MTGJSON").

## Follow-ups found on the way (2026-10-24)

- The MCP server (`mcp_server/tools.py`) hands prices on as bare numbers: an assistant reading a collection counted in
  euros would take them for dollars. It should say the currency (`/api/prices/source`).
- The activity log's "Added to inventory: ... - 3.00 for 2" has lost its `$` and has no symbol yet (`inventory.add_card`
  does not know the source).

## Never tried

Whether the MTGJSON values are right (no price was compared with a shop's page); how long the import takes
and how much memory the 113,000-uuid JSON needs on a Raspberry Pi; whether the uuid -> Scryfall id mapping
stays this clean on other days.
