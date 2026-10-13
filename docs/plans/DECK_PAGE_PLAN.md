# Decks as a page of their own, redesigned: plan

Date: 2026-10-13

**Built and deployed to the server the same day** - see *How it went* at the end. What follows is the plan
as it was written.

A plan for taking the decks out of the collection page and giving them a new layout. Nothing
here is implemented. The layout was chosen by the user from four static mockups
(`docs/plans/deck_mockups/`, not in Git): **A, "Columns"**, with **C, "Stacks"**, as a second
view of the same deck. The numbers below were counted at commit 0072a90.

## Why

The collection page is four things in one - inventory, decks, trades, statistics - held by
one script of 2,270 lines (129 functions), a template of 448 lines and 1,277 lines of CSS.
About 880 of the script's lines are the decks'.

What the deck tab is like today, seen in screenshots of the test data:

- **A deck has no address.** It cannot be bookmarked or linked, the browser's Back leaves
  the collection instead of the deck, and reloading the page drops the deck being edited.
- **The deck list is a short row of text tiles above a long scroll** of three other jobs:
  "Build around a card", "Commanders you own", "Preconstructed decks".
- **The builder is two very long columns**: a 100-card deck makes a page about 4,000 pixels
  tall, with the search results beside it just as long. No card is shown as a card until it
  is pointed at.

## What is chosen

**The deck list** (`/decks`): each deck a tile with its commander's picture, colours, card
count and how much of it is owned. "What can I build?" and the preconstructed decks become
tabs of that page instead of sections below the list.

**A deck** (`/decks/<id>`), two views of the same content, one switch:

- **Columns** (the default): the commander's picture as a banner with the deck's name,
  format, legality, card count, value and share owned; the charts in one strip; the whole
  deck as two or three columns of text, like a decklist on paper.
- **Stacks**: the cards themselves, overlapping, one stack per kind of card.

**Adding cards** is one panel that slides in from the right - search, suggestions and
popular decks are its three tabs - and is closed when it is not needed.

## What must not change

- **The server's answers.** No route under `/api/` changes; `scripts/walk_server.py` must
  show no step different. Two routes are *added* - the pages - and
  `tests/app_surface.json` is recorded again on purpose.
- **Nothing the deck tab can do today is lost.** That is the part a redesign gets wrong
  quietly, so it is written out below and is the checklist the work is accepted against.
- **The header** is the same on every page (CLAUDE.md), and every page defines
  `openSettings()` and `inventoryChanged()`.
- **Nothing from the internet** but card pictures.

## Everything the deck tab does today, and where it goes

50 things, taken from the tab's markup (`templates/collection.html`, lines 105-273 and the
three dialogs) and its event bindings (`collection.js`: `bindDeckHomeEvents`,
`bindBuilderEvents`).

### The deck list

| # | Today | In the new page |
|---|---|---|
| 1 | Notice "the card data is from before deck building" with its update button and progress | the same notice, at the top of `/decks` and of a deck |
| 2 | New deck (name, format) | button at the top right; the same dialog |
| 3 | ... from another site's address (Moxfield, Archidekt) | in that dialog |
| 4 | ... from a pasted decklist | in that dialog |
| 5 | Unknown cards of an import are reported | the same |
| 6 | Filter by deck or commander name | above the tiles |
| 7 | Filter by format | above the tiles |
| 8 | Sort: last changed, name, format, most owned, fewest missing, most cards | above the tiles |
| 9 | A deck's tile: name, format, cards, commanders, % owned, changed | the tile, with the commander's picture and colours added |
| 10 | Click a tile: the deck opens | a link to `/decks/<id>` |

### What can I build? (a tab of `/decks`)

| # | Today | In the new page |
|---|---|---|
| 11 | Build around a card: format | tab "What can I build?" |
| 12 | ... search among your cards by name, by type | same |
| 13 | ... "Not in a deck", "Commanders only" | same |
| 14 | ... click a card to see it; Find decks (Archidekt), with progress and Stop | same |
| 15 | ... a found deck: open it as a new deck | same |
| 16 | Commanders you own: Find (EDHREC), with progress and Stop | same tab, second panel |
| 17 | ... start a deck with that commander | same |

### Preconstructed decks (a tab of `/decks`)

| # | Today | In the new page |
|---|---|---|
| 18 | Search by name, set code or year; kind of deck | tab "Preconstructed decks" |
| 19 | Rank by what I own, with progress and Stop | same |
| 20 | View cards (a dialog with the list) | same dialog |
| 21 | ... Create deck from it; asks when a deck of that name exists | same |
| 22 | I own it: also adds its cards to the inventory | same |

### A deck

| # | Today | Columns | Stacks |
|---|---|---|---|
| 23 | Back to the list | link "All decks"; the browser's Back works | same |
| 24 | Rename | click the name in the banner | same |
| 25 | Change format | the format in the banner is a menu | same |
| 26 | Import cards into this deck (paste / address), with "replace the deck" | button in the banner | same |
| 27 | Export as text | button in the banner | same |
| 28 | Buy list | "⋯" menu | same |
| 29 | Duplicate | "⋯" menu | same |
| 30 | Delete, asked first | "⋯" menu, red | same |
| 31 | Totals: cards, sideboard / maybe, price, missing and its price | banner | head line |
| 32 | Legality and the list of problems (too many copies, outside the colours, ...) | chip in the banner; the problems listed under it when there are any | same |
| 33 | Charts: mana curve, types, mana symbols | strip under the banner | same strip |
| 34 | Cards grouped: commander, then by type; maybe / sideboard apart | groups in columns | one stack per group |
| 35 | One copy more / less | on the row (see *Touch*) | on the card |
| 36 | Owned state of each card: owned, shared with another deck, n of m, missing | a mark on the row; its explanation on pointing / tapping | a corner mark on the card |
| 37 | Price of each card | on the row | under the pulled-out card |
| 38 | The printing shown, and choosing another (dialog with the printings) | row menu "Printing" | same |
| 39 | Move to main / maybe (side) | row menu | same |
| 40 | Make it the commander | row menu | same |
| 41 | A card the card data does not know is marked | marked row | a placeholder card with its name |
| 42 | The card's picture on pointing | beside the pointer; on touch, a tap | the card is the picture |

### Adding cards (the panel)

| # | Today | In the new page |
|---|---|---|
| 43 | Search: name, type, rules text, mana value, rarity, colours | tab "Search" |
| 44 | ... owned, not in other decks, legal, commander's colours | same |
| 45 | ... a result: add to main, to maybe, as commander; how many are in the deck already | same |
| 46 | ... More results | same |
| 47 | Suggestions (EDHREC for the commander): owned, not in other decks; add | tab "Suggestions" |
| 48 | ... "start from the average deck" | same |
| 49 | Popular decks for the commander; open one as a new deck | tab "Popular decks" |
| 50 | ... open a deck by its address as a new deck | same |

## Design

### Pages and addresses

| Address | What |
|---|---|
| `/decks` | the list; `?tab=build` and `?tab=precons` are its other two tabs |
| `/decks/<id>` | one deck; `?view=stacks` the second view (the view last used is remembered) |

A deck that does not exist answers with the list and a notice, not an error page. A game
without deck formats has no "Decks" in the header, and `/decks` sends to `/collection`.

"Decks" moves in the header from the collection page's tabs to the page links, beside
Scanner and Collection. The collection page keeps Inventory, Statistics and Trades.

### Files

| | |
|---|---|
| `server/pages.py` | the two routes |
| `templates/decks.html`, `templates/deck.html` | the two pages |
| `static/js/decks.js` | everything of today's deck code, moved; rendering rewritten for the new layout |
| `static/css/decks.css` | the deck rules, out of `collection.css` |
| `static/js/common.js` | what both scripts use, moved out of `collection.js`: `api`, `money`, `plural`, `manaHtml`, `optionsHtml`, `closeModal`, `remember` / `recall`, `debounce`, the settings and backups dialog |

The deck code is moved first and *then* changed: the first commit only cuts it out of
`collection.js` into `decks.js` on the new page, with today's markup, so that a move can be
told from a redesign.

### Ties to the collection page

- **"Add to a deck" from the inventory's selection** stays on the collection page: adding
  to an existing deck is one request; "New deck..." goes to `/decks?new=` with the card
  names carried over.
- **The remembered tab** may be `decks`: the collection page then opens `/decks`.
- **The inventory's "spare cards" filter** asks EDHREC about the decks' commanders
  (`loadSuggested`): it is the inventory's and stays there.
- **The inventory's location filter lists the decks' locations**: it only needs the deck
  list, one request, which stays in `collection.js`.
- **The Android app** has no link to `/collection` or the deck tab (searched).

### Touch

Both mockups lean on the pointer: a row's buttons and a card's picture appear when it is
pointed at. On a phone nothing is ever pointed at. (Decided: the phone does not matter, so
this stays as in the mockups - the pages are for a screen with a pointer.)

### The Stacks view and the network

Stacks is nothing but pictures from Scryfall: about 100 for a Commander deck. They are
loaded as they scroll into view; a card whose picture does not arrive shows its name and
cost in a card-shaped box; and without any network the view says so and offers Columns.

## Decisions

**Decided by the user on 2026-10-13:** 1 - the phone does not matter at all: desktop only,
so the tap sheets of *Touch* and phase 5 are dropped, and the pages are tried at desktop
widths only. 2 - "Decks" is its own link beside Scanner and Collection. 3 was not answered:
the mockups stay out of Git, as recommended, until said otherwise. Also asked for, to come
*after* what the mockups show is built: a fourth tab in the decks "to keep the build stuff
we currently have" - which of today's parts that means is to be asked before it is built
(the tab "What can I build?" already keeps "Build around a card" and "Commanders you own";
the other reading is today's builder layout kept as a further view of a deck).

1. **How much the phone matters.** (a) Works on a phone: one column, the panel full-width,
   tap sheets - about a fifth more work, and every part is tried at 390 pixels too.
   (b) Desktop first: usable on a phone, not polished. **Recommended: (a)** - the scanner
   pages are used at the box, on whatever is at hand.
2. **The header.** "Decks" as a third page link, or kept inside Collection as now with
   only the address new. **Recommended: its own link.**
3. **The mockups in Git.** The folder is 5 MB, mostly screenshots, and its `data.js` and
   pictures show your decks and collection; the repository is public. (a) Not committed
   (as now); (b) the HTML and CSS committed with made-up data, no screenshots; (c) all of
   it. **Recommended: (a)**, and the folder deleted once the page is built.

## How it is checked

| Check | What it catches | When |
|---|---|---|
| The 50 rows above, each tried by hand through a script in headless Chromium against a server on copied data: the action done, the result seen in the page and in `/api/decks/...` | a feature that did not survive | after the move, after each view |
| No script error and no missing file of the server's on any of the pages, at 1440 and at 1100 pixels wide | a broken page | every step |
| `scripts/walk_server.py`, before and after | an `/api/` answer that changed | after the move, at the end |
| The surface test, recorded again: two routes more, none less | a route lost | once |
| The other tests | | every commit |
| The collection page: inventory, trades, statistics, settings and backups | what was cut out of its script took something with it | after the move |
| Screenshots of both views with the test decks, beside the mockups | that what was built is what was chosen | at the end, shown to the user before deploying |

## Phases

One commit each.

0. **Prepare.** The checklist as a script that can be run (it then passes on today's tab,
   which proves the script). Record the walk.
1. **The move.** Shared helpers to `common.js`; the two pages with today's markup;
   `decks.js` and `decks.css` cut out; the header; the collection page's ties. Everything
   works as before, at new addresses.
2. **The deck list**: tiles with pictures; the three tabs.
3. **A deck, Columns**: banner, strip, columns, the row menu and tap sheet, the panel.
4. **A deck, Stacks**, and the switch.
5. (The phone - dropped, decision 1.)
6. **Documents**: the program documentation's pages and deck sections, CLAUDE.md's table,
   README, TEST_CASES.md's deck cases.
7. **Deploy**: the server only - the stations have no part in this.

## Risks and open points

1. **A feature lost in the redesign.** The reason for the table and for moving before
   changing.
2. **`collection.js` after the cut.** It shares helpers and state with the deck code
   (`inventory`, `gameInfo`, `valueDialog`); what both need has to be in `common.js` before
   anything moves, or the collection page breaks in places no test looks at.
3. **Cached scripts in browsers**: `?v=` raised on everything touched.
4. **Long decks in Columns.** A 100-card deck fits in about a screen and a half at 1440
   pixels; a 250-card list will not, and still has to read well.
5. **Not decided here:** drag and drop between groups (neither mockup has it; today's page
   does not either), and a "By cost / By colour" grouping for Stacks, which mockup C shows
   as a switch. Proposed: the grouping switch yes, as it is cheap once groups are a
   function; drag and drop no.

## What this does not do

- No change to what a deck is, to the deck checks, or to any `/api/` answer.
- No split of `decks_api.py`.
- The trades and statistics tabs stay in the collection page.

## Size

About 880 lines of script and 510 of CSS moved (the stylesheet's Decks section, lines 550-1063), perhaps half of the script's rendering
rewritten; two templates; two routes. Larger than it looks in the mockups, because the
mockups show about a dozen of the 50 things.

## How it went (2026-10-13)

Built on the branch `decks-page`, in two commits: the move (1475877), then the redesign
(bee4924); merged into `main` and deployed to the server at the user's word (below).

Differently from the plan:

- **One page for both addresses**, not two templates: `decks.html` holds the list and the
  deck, and `decks.js` shows the one the address says. Opening a deck from the list, and
  going back, then needs no new page load - and the code that was moved kept working as it
  was. `?tab=` and `?view=` are not in the address: the tab, the view and the grouping are
  remembered in the browser instead.
- **The deck list's answer gained a field**, `commander_cards` (name, picture, colours).
  The plan said no `/api/` answer would change; the tiles need the pictures, and one
  answer with them is better than a request per deck. Nothing was taken away or renamed.
- **The checklist script was written with the move**, not before it, and so was not seen
  to pass on the old tab first. It was seen to *fail* by accident instead: run against a
  server that did not have the new files yet, it reported the deck list empty.
- **`decks.css` has only the new rules.** The plan moved the stylesheet's whole Decks
  section; the collection page turned out to use some of those rules for other things
  (its backup list borrows the idea rows), so they stay in `collection.css`, which the
  deck pages load too.
- **The header shows "Decks" on every page**; only the collection and deck pages hide it
  for a game without deck formats (there is one game today, and it has them).
- Phases 2, 3 and 4 are one commit.

Checked, on the laptop, against a server on copied data:

| | |
|---|---|
| `scripts/check_deck_pages.py`, headless Chromium | 54 checks, all passing: the list (tiles, filter, sort, tabs), a deck made from a pasted list with an unknown card reported, copies, moves, printings, both views and the three groupings, the panel, the menu, search and add, rename, format, import, the two exports, the addresses (reload, Back, a deck that is gone), duplicate, delete - and the collection page that was cut: its three tabs, the location filter, the settings drawer, "New deck..." from a selection |
| The recorded walk, `main` against the branch | 70 steps, 2 different: `GET /decks` exists, and `GET /api/decks` has the new field |
| All tests; the route list recorded again | pass; two routes more (`/decks`, `/decks/<id>`) |
| Screenshots of the list, both views, the groupings, the panel, and with the pointer on a row and on a stacked card | looked at beside the mockups (`docs/plans/deck_mockups/screenshots/new_*.png`) |

Of the 50 things in the table above, the script tries those that need no other site.
**Not tried**: what asks EDHREC, Archidekt, Moxfield or MTGJSON - suggestions and "start from
the average deck", popular decks, "Find decks" around a card, "Commanders you own", the
preconstructed decks (list, view, create, "I own it", ranking), a deck from another site's
address - whose code was moved unchanged but not run; the notice about old card data and
its update button; a deck with two commanders, without a commander, with missing or shared
cards (the test decks are all owned); a game without deck formats; a real person using it.

**Deployed to the server** (192.168.1.23) at 01:50, idle at the time. A backup first
(`scanner-backup-20261013_014950.tar.gz`); the folder compared with the tree (nothing left
over); the image built while the old container served and looked into (the two deck
routes, the new template and files); then the switch - answering after 4 seconds. The
collection's numbers the same (5,407 cards, 2,151 entries), the same 15 decks, OCR on the
GPU, both stations back, no error in the log; the new pages and their files answer, the
collection page has no deck tab and a Decks link.
Then looked at in headless Chromium, reading only (the check script is never run against
real data - it makes and deletes decks): the list shows 15 tiles, 10 with a commander's
picture; a Standard deck - five of the real decks have no commander, a case the test data
did not have - shows its plain banner, its charts and its cards in columns; the collection
page lists the inventory under its three tabs. No script error, no missing file.
One thing seen there and corrected afterwards (in the repository, not yet on the server):
a deck without a commander left a small gap before its format in the banner.
The stations were not touched: they have no part in this.

Still open: the fourth tab the user asked for ("to keep the build stuff we currently
have") - which parts that means is to be asked; and the mockups folder, which is not in
Git.
