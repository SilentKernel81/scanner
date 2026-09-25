# Multi-Game Support - Implementation Plan

**Status:** phases 1-3 done (Pokémon 2026-09-25), phases 4-5 planned. Replaces the 2025-11 plan, which predated the current
architecture (online API lookup per scan, YOLO-first detection).

## Goals

- Scan **one game at a time**: a game selector in the top bar decides which card database,
  prompts, matching rules, finishes and exports are used. No mixed piles, no game auto-detection.
- Support Magic (unchanged), Pokémon, Disney Lorcana and Yu-Gi-Oh.
- **Editable AI prompts** in Settings, per game and optionally per AI model.
- Keep everything that works today: local SQLite card data (offline, instant search), outline
  detection, drop-pile auto-capture, confirmed-only auto-add, printing picker, undo.

The owner only has Magic cards. The other games are built to be ready, and are verified with the
official card images from each data source (fed as synthetic frames through `CardScanner`, and
sent to the AI) - not with physical cards. The docs must say so.

## Data sources (checked 2026-09-24)

| Game | Source | Bulk download | Notes |
|---|---|---|---|
| Magic | Scryfall (as today) | bulk JSONL | unchanged |
| Pokémon | [TCGdex](https://tcgdex.dev) REST v2, no key | `GET /v2/en/sets` (220 sets) + `GET /v2/en/sets/{id}` per set -> every card's id, `localId` (number), name, image; set has `abbreviation.official` ("PAL") and `cardCount.official` (193) | Per-card details (rarity, `variants`, `variants_detailed` with Cardmarket/TCGplayer prices) need `GET /v2/en/cards/{id}`: fetched lazily for the matched card and cached. pokemontcg.io is closed to new keys and shuts down 2027-03-01 (successor Scrydex is paid) - not used. |
| Lorcana | [Lorcast](https://lorcast.com/docs/api) `api.lorcast.com/v0`, no key | `/v0/sets` + `/v0/sets/{code}/cards` (~2,000+ cards) | Prices updated daily. Images are `.avif`. |
| Yu-Gi-Oh | [YGOPRODeck](https://ygoprodeck.com/api-guide/) v7, no key | one `GET /api/v7/cardinfo.php` (~21 MB, 14.5k cards) | Rate limit 20 req/s, 1 h block if exceeded - bulk download only. Printings come from each card's `card_sets` (`set_code` "LOB-EN001", `set_rarity`, `set_price`). |

## Architecture

### `games/` package

One module per game behind a small common interface; `games/__init__.py` holds the registry and
the active game (saved in `data/settings.json` as `game`, default `mtg`).

The interface is `games/base.py:Game` (implemented in phase 2): `id`, `label`, `finishes`,
`confirmed_matches`, `card_count`, `download`, `identify`, `find_printings`, `similar`,
`get_card`, `card_payload`, `inventory_fields`, `export_formats`. A new game adds a module with
a `Game` subclass, registers it in `games/__init__.py:init`, and adds its prompts to
`prompts.py:BUILT_IN` (the foil marker prompt is optional - `prompts.has('foil', game)`).

`games/mtg.py` wraps today's code (`database.py` search functions, prompts, foil check) without
changing behaviour; the Scryfall `cards` table keeps its name. Other games get their own tables
(`pokemon_cards`, `lorcana_cards`, `ygo_printings`) in the same SQLite file, with a shared core
(`id, name, name_search, set_code, set_name, number, rarity, image_url, price, extra JSON`) so
formatting, the printing picker and the inventory can stay generic.

### Matching per game

`search_exact` must keep tagging results; `CONFIRMED_MATCHES` becomes per game.

| Game | What the AI reads | Confirmed (auto-add) |
|---|---|---|
| Magic | name, collector number, set code, ★/• | set+number, name+number (as today) |
| Pokémon | name, number `123/193`, set abbreviation (SV era onwards: "PAL EN") | set+number with matching name; for older cards without abbreviation, name + number + set total (`/193` = `cardCount.official`) when that identifies one set |
| Lorcana | name + subtitle, number `123/204`, set number | set+number with matching name |
| Yu-Gi-Oh | name, set code `LOB-EN001` (right, under the art) | set code with matching name **and** a single rarity for that code; otherwise review (printing picker lists the rarities) |

### Finishes

| Game | Finishes | How it is suggested |
|---|---|---|
| Magic | nonfoil / foil / surge etc. (as today) | ★/• check (`read_foil_symbol`) |
| Pokémon | normal / holo / reverse holo (+ 1st edition) | only variants the card exists in (`variants`); if only one, use it; otherwise ask the AI and leave for review |
| Lorcana | normal / foil | ask the AI; review when unsure |
| Yu-Gi-Oh | rarity of the printing | from `set_rarity` |

The inventory's `foil`/`surge` columns become a generic `finish` text column (migrated from the
old flags) so every game fits.

### Inventory

- New column `game` (default `mtg`) and `finish`. The table's `UNIQUE(...)` constraint must
  include both, so SQLite needs a table rebuild (copy rows into a new table) - done once in
  `initialize_database`, after an automatic backup of the DB file.
- Inventory modal: filter by game (defaults to the active game); summary/stats per game.
- Exports per game: Magic keeps CSV + Moxfield; others get a generic CSV (name, set, number,
  finish, condition, quantity, price) - site-specific formats can come later.

### Editable prompts (Settings -> Vision AI -> Prompt)

The prompt is split in two:

1. **Instructions** - editable text: what the card looks like and where to read each field.
2. **Answer format** - fixed, added by the code from `answer_fields`
   (`NAME: ... / NUMBER: ... / SET: ...`), so an edit can never break the parser.

Overrides live in `data/prompts.json`:

```json
{"mtg": {"default": "...", "local:qwen3.5:4b": "..."}, "pokemon": {"default": "..."}}
```

Lookup order: game + provider:model -> game `default` override -> built-in default. The foil
prompt (Magic) is editable the same way.

UI:
- a textarea with the prompt in effect for the current game and model, and a badge showing
  where it comes from (built-in / all models / this model);
- "Save for this model" and "Save for all models" buttons;
- "Restore default" (removes the override);
- **"Test on last capture"**: re-runs the AI on the last captured image with the text in the
  box, without saving, and shows the raw answer and the parsed result. This is what makes
  tuning a prompt for a model practical.

Socket events: `save_prompt`, `reset_prompt`, `test_prompt` (+ `GET /api/prompts`).

### UI

- Game selector in the top bar. Switching game: stops auto-scan, clears the current card, loads
  that game's prompt, and shows a notice if its card database is not downloaded yet.
- Settings -> Database: status and "Download / Update" per game (progress through the existing
  database events).
- Manual search: the Set / Number / Treatment fields adapt to the game (e.g. Treatment ->
  Finish for Pokémon, Set code only for Yu-Gi-Oh).
- Card panel: game-specific details (HP/type for Pokémon, ink/cost for Lorcana, ATK/DEF for YGO)
  from the `extra` JSON; the layout stays compact.

## Phases

Each phase ends working, tested (DB copy, scratch settings, never the real inventory) and
documented (PROGRAM_DOCUMENTATION.md, README.md, CLAUDE.md), and is committed separately.

1. **Editable prompts** (useful now for Magic): split prompt, `data/prompts.json`, Settings UI,
   "Test on last capture". **Done** - `prompts.py` already keys prompts by game (`mtg`).
2. **Game abstraction, Magic only**: `games/` package, `games/mtg.py` wrapping current code,
   inventory `game` + `finish` columns (rebuild + backup), per-game `CONFIRMED_MATCHES`. No
   visible change (the game selector only shows with two or more games). **Done** - inventory
   rows are now addressed by id; `/api/games` drives the finishes and export buttons.
3. **Pokémon**: TCGdex import, lazy details/prices, prompt, matching, finishes, export. **Done** -
   one GraphQL request returns every card (no per-set card requests); older cards are matched by
   name + number + set total. The finish is suggested from the printing's variants only (normal
   first); reverse holo detection from the image is not done.
4. **Lorcana**: Lorcast import, prompt, matching, foil, export.
5. **Yu-Gi-Oh**: YGOPRODeck bulk import into printings, set-code matching, rarity picker, export.

Phases 3-5 are independent and can be done in any order.

## Risks and open points

- **No physical cards for Pokémon/Lorcana/YGO**: thresholds and prompts are verified on
  official images only; real-world accuracy (glare on holo, reverse holo recognition) is unknown.
- **Reverse holo / Lorcana foil from a camera** is hard; the default is review, not auto-add.
- **Pokémon without printed set abbreviation** (pre-2023): identification relies on name +
  number + set total; some collisions will need the printing picker.
- **Yu-Gi-Oh card ratio** (86/59 = 1.46 vs 1.40): expected to pass the current tolerance -
  confirm with a real frame; if not, use `Game.card_ratio` in `find_card_outline`.
- **External APIs change** (pokemontcg.io is being shut down): each importer is isolated in its
  game module so a source can be swapped without touching the rest.
