# ============================================================================
# FILE: core/prices.py
# Where the prices shown and counted come from: the price sources one can choose (Settings > Card data > Prices)
# ============================================================================
"""
The price sources (docs/plans/PRICE_SOURCES_PLAN.md). Every printing in the card data keeps the prices of every source
it has; the chosen one is copied into the columns everything reads - `cards.price`, `price_foil`, `price_etched` - by
apply(): before each new card data is swapped in (CardDatabase._populate) and when the choice changes. The readers (a
card's payload, an entry's price, a deck's cheapest printing) never ask which source it is.

A source is a currency and three columns (regular, foil, etched; None: the source has no such price), either of the card
data itself ('kind': 'card_data' - Scryfall's, filled by each card data download) or of the shops' table beside it
('kind': 'shops' - SHOP_TABLE, MTGJSON's prices, filled by core/shop_prices.py on its own schedule). Shops Scryfall already
gives (TCGplayer, Cardmarket) are taken from Scryfall only: one shop, one number.
"""

# The shops' prices (core/shop_prices.py): one row per printing, keyed by the printing's id like `cards`
SHOP_TABLE = 'shop_prices'

# The source of a fresh install, and of a file of settings that names an unknown one: Scryfall's prices, which are TCGplayer's
DEFAULT_SOURCE = 'scryfall_usd'

SOURCES = {
    'scryfall_usd': {'label': 'TCGplayer (US dollars)', 'currency': 'USD', 'symbol': '$', 'kind': 'card_data',
                     'columns': ('price_usd', 'price_usd_foil', 'price_usd_etched'),
                     'from': "Scryfall's card data: TCGplayer's market price"},
    # Scryfall's EUR prices are Cardmarket's trend price; Scryfall gives no etched one
    'scryfall_eur': {'label': 'Cardmarket (euros)', 'currency': 'EUR', 'symbol': '€', 'kind': 'card_data',
                     'columns': ('price_eur', 'price_eur_foil', None),
                     'from': "Scryfall's card data: Cardmarket's trend price"},
    # MTGJSON's (since 2026-10-24): what the shop sells it for - Card Kingdom's buylist is kept beside, never shown as a price
    'cardkingdom_usd': {'label': 'Card Kingdom (US dollars)', 'currency': 'USD', 'symbol': '$', 'kind': 'shops',
                        'columns': ('cardkingdom', 'cardkingdom_foil', 'cardkingdom_etched'),
                        'from': "MTGJSON's daily prices: Card Kingdom's retail price"},
    'manapool_usd': {'label': 'Mana Pool (US dollars)', 'currency': 'USD', 'symbol': '$', 'kind': 'shops',
                     'columns': ('manapool', 'manapool_foil', 'manapool_etched'),
                     'from': "MTGJSON's daily prices: Mana Pool's price"},
}


# What a shop would PAY for a copy (phase 4 of the plan): Card Kingdom's buylist, kept in the shops' table by each price
# update (core/shop_prices.py) - regular, foil, etched. Never a price source: it is not what a card is worth but what one
# shop offers for it, shown beside the value (Game.buylist_prices), always in its own currency
BUYLIST = {'label': 'Card Kingdom', 'currency': 'USD', 'symbol': '$',
           'columns': ('cardkingdom_buy', 'cardkingdom_buy_foil', 'cardkingdom_buy_etched')}


def source(source_id):
    """The source of that id, or the default one (a setting written by another version, or edited by hand)"""
    return SOURCES.get(source_id) or SOURCES[DEFAULT_SOURCE]


def known(source_id):
    return source_id in SOURCES


def choices():
    """[{'id', 'label', 'currency', 'symbol', 'from', 'kind'}] in the order the dropdown shows them"""
    fields = ('label', 'currency', 'symbol', 'from', 'kind')
    return [{'id': key, **{field: value[field] for field in fields}} for key, value in SOURCES.items()]


def _column_sql(table, source_id, column):
    """How a card table's row reads one of a source's columns: its own column, or the shops' row of the same printing"""
    if not column:
        return 'NULL'
    if source(source_id)['kind'] == 'shops':
        # A correlated subquery on the shops' primary key, not UPDATE ... FROM: the same SQL on every SQLite a server runs on
        return f'(SELECT shop.{column} FROM {SHOP_TABLE} AS shop WHERE shop.id = {table}.id)'
    return column


def priced_sql(table, source_id):
    """How many printings of a card table have a price in a source: 0 for card data downloaded before the source's columns
    were filled (the EUR prices: before 2026-10-23), and for a shop before the first price update - such a source would
    show the currency without its prices"""
    columns = [_column_sql(table, source_id, column) for column in source(source_id)['columns'] if column]
    return f"SELECT COUNT(*) FROM {table} WHERE COALESCE({', '.join(columns + ['NULL'])}) IS NOT NULL"


def apply_sql(table, source_id):
    """
    The UPDATE that copies a source's prices into a card table's shown columns (`price`, `price_foil`, `price_etched`).
    A price the source does not have is NULL there, never another source's: a euro price must never stand in for a
    missing dollar one, nor one shop's for another's. The table name comes from the code (CardDatabase), never from a
    request.
    """
    regular, foil, etched = (_column_sql(table, source_id, column) for column in source(source_id)['columns'])
    return f'UPDATE {table} SET price = {regular}, price_foil = {foil}, price_etched = {etched}'
