# Card Scanner MCP server

Lets an AI assistant look at your **decks and collection** (kept by the scanner server) and help
you build and improve decks from the cards you own. Version 1 **only reads**: it asks the scanner
server's web API, and `propose_deck_changes` works out what a change would do without making it -
you make the change on the decks page.

It runs on the machine of the AI client (your laptop), not on the scanner server, and changes
nothing there.

## Tools

| Tool | What it answers |
|---|---|
| `list_decks` | every deck: id, name, format, commander, cards, how much of it you own |
| `get_deck(deck, with_card_text=false)` | a deck's cards by board, with copies owned, type, cost, price, other decks using each card; problems and totals |
| `check_deck(deck)` | the format's problems (legality, copies, colours) and totals |
| `missing_cards(deck)` | what you don't own enough copies of, with the cost |
| `suggest_for_deck(deck, per_category=8, only_owned=false)` | popular cards for the deck's commander (EDHREC) with inclusion %, synergy, copies you own, whether another deck uses it |
| `what_can_i_build(kind='commanders'\|'precons')` | decks your collection could make (asks other sites: can take minutes, returns what it has) |
| `propose_deck_changes(deck, add, remove)` | what adding / removing cards would do: legal? in colours? owned? used by other decks? - **changes nothing** |
| `search_collection(text, set_code, rarity, card_type, tag, location, in_a_deck, limit)` | the cards you own |
| `find_card(name)` | copies owned (printing, finish, location), decks using them, the card's text |
| `collection_summary()` | cards, value, biggest sets, rarities, types, locations |

`deck` is a name (or part of it), a commander, or an id. If it matches several decks the tool says which.

## Install (once)

```bash
uv venv ~/venvs/mcp-scanner --python 3.12
uv pip install --python ~/venvs/mcp-scanner/bin/python -r mcp_server/requirements.txt
~/venvs/mcp-scanner/bin/python mcp_server/card_scanner_mcp.py --server http://192.168.1.23:5000 --check
```

`--check` lists the decks and stops: it shows whether the server can be reached.

## Connect Claude

**Claude Code:**

```bash
claude mcp add card-scanner -- ~/venvs/mcp-scanner/bin/python \
    ~/Projects/scanner-server/mcp_server/card_scanner_mcp.py --server http://192.168.1.23:5000
```

**Claude Desktop** (`claude_desktop_config.json`, `mcpServers`):

```json
{
  "mcpServers": {
    "card-scanner": {
      "command": "/home/you/venvs/mcp-scanner/bin/python",
      "args": ["/home/you/Projects/scanner-server/mcp_server/card_scanner_mcp.py",
               "--server", "http://192.168.1.23:5000"]
    }
  }
}
```

The server's address can also be the environment's `CARD_SCANNER_URL`.

## Try it

Ask: *"List my decks."*, *"Which of my commander decks is the least complete?"*,
*"Suggest cards for Calling All Angels that I already own"*, *"What would it do to add Lightning
Bolt and Swords to Plowshares to Calling All Angels?"*, *"What are my most valuable rares?"*

## A local model

Any MCP client works, also one in front of a local model (Ollama): the model needs **tool
calling** (try Qwen 3 / Llama 3.1 class models), and fewer tools help small models. The facts
(what you own, what is legal) come from the server, so they are right whichever model reads them.

## What it does not do

It cannot add, remove, move or delete anything: not in the collection, not in a deck. The server
has no login, so keep it that way until there is a reason to add writes - then one at a time, each
with a confirmation.

## Tests

`tests/test_mcp_tools.py` tests `tools.py` against a stand-in for the server (no network, no MCP
library). The protocol itself was tried with the library's own client against the real server.
