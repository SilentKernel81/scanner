# ============================================================================
# FILE: core/prices.py
# Where the prices shown and counted come from: the price sources one can choose (Settings > Card data > Prices)
# ============================================================================
"""
The price sources (docs/plans/PRICE_SOURCES_PLAN.md). Every printing in the card data keeps the prices of every source
it has; the chosen one is copied into the columns everything reads - `cards.price`, `price_foil`, `price_etched` - by
apply(): before each new card data is swapped in (CardDatabase._populate) and when the choice changes. The readers (a
card's payload, an entry's price, a deck's cheapest printing) never ask which source it is.

A source is a currency and three columns of the card data (regular, foil, etched; None: the source has no such price).
"""

# The source of a fresh install, and of a file of settings that names an unknown one: Scryfall's prices, which are TCGplayer's
DEFAULT_SOURCE = 'scryfall_usd'

SOURCES = {
    'scryfall_usd': {'label': 'TCGplayer (US dollars)', 'currency': 'USD', 'symbol': '$',
                     'columns': ('price_usd', 'price_usd_foil', 'price_usd_etched'),
                     'from': "Scryfall's card data: TCGplayer's market price"},
    # Scryfall's EUR prices are Cardmarket's trend price; Scryfall gives no etched one
    'scryfall_eur': {'label': 'Cardmarket (euros)', 'currency': 'EUR', 'symbol': '€',
                     'columns': ('price_eur', 'price_eur_foil', None),
                     'from': "Scryfall's card data: Cardmarket's trend price"},
}


def source(source_id):
    """The source of that id, or the default one (a setting written by another version, or edited by hand)"""
    return SOURCES.get(source_id) or SOURCES[DEFAULT_SOURCE]


def known(source_id):
    return source_id in SOURCES


def choices():
    """[{'id', 'label', 'currency', 'symbol', 'from'}] in the order the dropdown shows them"""
    return [{'id': key, **{field: value[field] for field in ('label', 'currency', 'symbol', 'from')}} for key, value in SOURCES.items()]


def priced_sql(table, source_id):
    """How many printings of a card table have a price in a source: 0 for card data downloaded before the source's columns
    were filled (the EUR prices: before 2026-10-23) - such a source would show the currency without its prices"""
    columns = [column for column in source(source_id)['columns'] if column]
    return f"SELECT COUNT(*) FROM {table} WHERE COALESCE({', '.join(columns + ['NULL'])}) IS NOT NULL"


def apply_sql(table, source_id):
    """
    The UPDATE that copies a source's prices into a card table's shown columns (`price`, `price_foil`, `price_etched`).
    A price the source does not have is NULL there, never another source's: a euro price must never stand in for a
    missing dollar one. The table name comes from the code (CardDatabase), never from a request.
    """
    regular, foil, etched = source(source_id)['columns']
    value = lambda column: column or 'NULL'
    return f'UPDATE {table} SET price = {value(regular)}, price_foil = {value(foil)}, price_etched = {value(etched)}'
