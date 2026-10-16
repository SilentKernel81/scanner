#!/usr/bin/env python3
"""
An MCP server for the card scanner: lets an AI assistant (Claude Desktop, Claude Code, any MCP
client - also one that runs a local model) look at your decks and your collection and suggest
changes. Version 1 only READS: it asks the scanner server's web API, and `propose_deck_changes`
says what a change would do without making it.

    mcp_server/card_scanner_mcp.py [--server http://192.168.1.23:5000]
    mcp_server/card_scanner_mcp.py --check        (lists the decks and stops: is the server reachable?)

The server's address is --server, else the environment's CARD_SCANNER_URL, else
http://localhost:5000. It talks MCP over standard input / output (the client starts it).
The tools are in tools.py (plain functions, tested without the MCP library); this file only
registers them. Needs `pip install mcp` (README.md here).
"""
import argparse
import functools
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tools.py and scanner_api.py are beside this file

import tools  # noqa: E402
from scanner_api import ScannerApi, ScannerError  # noqa: E402

INSTRUCTIONS = """\
You can look at the user's Magic: The Gathering decks and card collection (kept by their card
scanner) and help them build and improve decks from the cards they own.
- Start with list_decks, then get_deck for one deck. 'owned' on a card is how many copies the user has.
- suggest_for_deck gives popular cards for a deck's commander and marks which the user owns.
- Prefer cards the user owns; say when a suggestion would need buying and what it costs.
- A card marked also_in_decks / used_by_another_deck is in another of the user's decks: say so.
- propose_deck_changes checks a change (legal? colours? owned?) but changes NOTHING: tell the user to make it on the decks page.
- You cannot change the collection or the decks.
"""


def build_server(api):
    """The MCP server with the tools registered (the library is imported here, so tests of tools.py do not need it)"""
    try:
        from mcp.server.mcpserver import MCPServer            # mcp 2.x
        server = MCPServer('card-scanner', instructions=INSTRUCTIONS)
    except ImportError:
        from mcp.server.fastmcp import FastMCP                # mcp 1.x
        server = FastMCP('card-scanner', instructions=INSTRUCTIONS)
    from mcp.types import ToolAnnotations
    try:
        from mcp.server.mcpserver.exceptions import ToolError  # mcp 2.x
    except ImportError:
        from mcp.server.fastmcp.exceptions import ToolError    # mcp 1.x
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    def explained(function):
        """A ScannerError (no such deck, the server is away ...) reaches the assistant in its own words: the library hides every other error"""
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except ScannerError as e:
                raise ToolError(str(e)) from None
        return wrapper

    @server.tool(annotations=read_only)
    @explained
    def list_decks() -> dict:
        """Every deck: id, name, format, commander, number of cards, and how many of them the user owns (owned_percent)."""
        return {'decks': tools.list_decks(api)}

    @server.tool(annotations=read_only)
    @explained
    def get_deck(deck: str, with_card_text: bool = False) -> dict:
        """A deck's cards by board (main, commander, side): quantity, copies owned, type, mana cost, price, other decks using the card; plus the format's problems and the totals. `deck` is its name (or part of it), its commander, or its id. with_card_text adds each card's rules text (longer)."""
        return tools.get_deck(api, deck, with_card_text)

    @server.tool(annotations=read_only)
    @explained
    def check_deck(deck: str) -> dict:
        """The format's problems with a deck (illegal cards, too many copies, colours outside the commander) and its totals (price, copies missing)."""
        return tools.check_deck(api, deck)

    @server.tool(annotations=read_only)
    @explained
    def missing_cards(deck: str) -> dict:
        """The cards of a deck the user does not own enough copies of, with what the missing ones cost."""
        return tools.missing_cards(api, deck)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
    @explained
    def suggest_for_deck(deck: str, per_category: int = 8, only_owned: bool = False) -> dict:
        """Cards other players put in a deck with the same commander (EDHREC): how often (inclusion_percent), how specific (synergy), the copies the user owns, and whether another of their decks uses it. only_owned=true leaves out what the user does not own. Needs the internet on the server; the first call can take several seconds."""
        return tools.suggest_for_deck(api, deck, per_category, only_owned)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
    @explained
    def what_can_i_build(kind: str = 'commanders', wait_seconds: int = 40, limit: int = 10) -> dict:
        """Decks the user's collection could make. kind='commanders': legendary creatures they own, by how much of their popular deck they own. kind='precons': preconstructed decks by the share they own. It asks other sites and can take minutes: it waits up to wait_seconds, then returns what it has (running=true means ask again for more)."""
        return tools.what_can_i_build(api, kind, wait_seconds, limit)

    @server.tool(annotations=read_only)
    @explained
    def propose_deck_changes(deck: str, add: list[dict] | None = None, remove: list[dict] | None = None) -> dict:
        """Work out what adding / removing cards would do to a deck - NOTHING IS CHANGED. add and remove are lists of {"name": "Card Name", "quantity": 1}. For each card added it says if it exists, is legal in the format, fits the commander's colours, how many copies the user owns and which other decks use it; and the deck's size before and after. The user makes the change on the decks page."""
        return tools.propose_deck_changes(api, deck, add, remove)

    @server.tool(annotations=read_only)
    @explained
    def search_collection(text: str = '', set_code: str = '', rarity: str = '', card_type: str = '',
                          tag: str = '', location: str = '', in_a_deck: bool | None = None, limit: int = 25) -> dict:
        """Search the cards the user owns by part of a name, set code (HOB), rarity, type (Creature), tag, location, and whether a deck uses them (in_a_deck true/false). Returns up to `limit` entries, most valuable first, and how many matched."""
        return tools.search_collection(api, text, set_code, rarity, card_type, tag, location, in_a_deck, limit)

    @server.tool(annotations=read_only)
    @explained
    def find_card(name: str) -> dict:
        """Everything about one card: how many copies the user owns (each printing, finish, location), which decks use them, and the card's type, cost, colours and rules text."""
        return tools.find_card(api, name)

    @server.tool(annotations=read_only)
    @explained
    def collection_summary() -> dict:
        """How big the collection is and what it is made of: card count, value, the biggest sets, rarities, card types, locations, and how many cards are in a deck."""
        return tools.collection_summary(api)

    return server


def main():
    parser = argparse.ArgumentParser(description="MCP server for the card scanner's decks and collection")
    parser.add_argument('--server', default=os.environ.get('CARD_SCANNER_URL', 'http://localhost:5000'),
                        help="the card scanner server's address (default: CARD_SCANNER_URL, else http://localhost:5000)")
    parser.add_argument('--check', action='store_true', help="list the decks and stop (is the server reachable?)")
    args = parser.parse_args()
    api = ScannerApi(args.server)
    if args.check:
        try:
            decks = tools.list_decks(api)
        except ScannerError as e:
            sys.exit(str(e))
        print(f"{args.server}: {len(decks)} decks")
        for deck in decks:
            print(f"  #{deck['id']} {deck['name']} ({deck['format']}) {deck['owned_percent']}% owned")
        return
    build_server(api).run()   # standard input / output: the client starts this program


if __name__ == '__main__':
    main()
