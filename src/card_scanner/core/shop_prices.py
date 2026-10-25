# ============================================================================
# FILE: core/shop_prices.py
# Shops' prices from MTGJSON (Card Kingdom, Mana Pool), beside the ones Scryfall's card data brings
# ============================================================================
"""
MTGJSON publishes one file a day with every shop's price of every printing it knows (AllPricesToday.json.xz, ~4 MB),
keyed by MTGJSON's own card ids (uuid). Two more files map each uuid to a Scryfall id - ours: cardIdentifiers.csv.xz
(cards, ~11 MB) and tokenIdentifiers.csv.xz (tokens). import_prices() downloads the three, turns them into one row per
Scryfall id and hands the rows to CardDatabase.replace_shop_prices, which swaps them in whole (a failure keeps
yesterday's). The table is `shop_prices`, beside `cards` - a card data download replaces `cards` and leaves it alone.

Which shops: only those Scryfall does not already give (core/prices.py: TCGplayer and Cardmarket come with the card
data), and paper prices only - cardhoarder's are MTGO's. Card Kingdom's buylist (what the shop pays) is kept too, for
a later "what would a shop pay for it" (docs/plans/PRICE_SOURCES_PLAN.md, phase 4); it is never a price shown.

Measured 2026-10-23 (the plan, "Measured"): a double-faced card is one Scryfall id and TWO uuids (a card per face),
and no shop + finish was priced differently between the faces - so the faces are merged by union. MIT-licensed data
(README, Acknowledgments). No server import here: core/.
"""
import csv
import json
import logging
import lzma
import re
import tempfile
from pathlib import Path

import requests

logger = logging.getLogger('database')    # the card data's log (data/logs/database.log)

MTGJSON = 'https://mtgjson.com/api/v5'
HEADERS = {'User-Agent': 'CardScanner/1.0 (personal collection tool)'}
TIMEOUT = 60
FILES = ('AllPricesToday.json.xz', 'csv/cardIdentifiers.csv.xz', 'csv/tokenIdentifiers.csv.xz')

# The shops kept and their columns in `shop_prices`: (MTGJSON's provider, its kind of price) -> column prefix.
# A column per finish follows the prefix: '' (normal), '_foil', '_etched' - MTGJSON's three finishes
SHOPS = {
    ('cardkingdom', 'retail'): 'cardkingdom',
    ('cardkingdom', 'buylist'): 'cardkingdom_buy',
    ('manapool', 'retail'): 'manapool',
}
FINISHES = {'normal': '', 'foil': '_foil', 'etched': '_etched'}
COLUMNS = [prefix + suffix for prefix in SHOPS.values() for suffix in FINISHES.values()]

# The key of this import's row in card_data_info (beside each game's): its 'source_updated' is MTGJSON's date of the
# prices in use - the next import downloads nothing while MTGJSON's Meta.json still says that date
INFO_KEY = 'mtgjson_prices'


class NothingNew(Exception):
    """MTGJSON has no newer prices than the ones in use: nothing was downloaded"""


def meta_date(get=requests.get):
    """The date of MTGJSON's current build ('2026-10-24'), from its small Meta.json"""
    response = get(f'{MTGJSON}/Meta.json', headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    meta = response.json()
    # Both 'meta' and 'data' carry it (seen 2026-10-24); either will do
    date = (meta.get('data') or {}).get('date') or (meta.get('meta') or {}).get('date')
    if not date:
        raise ValueError("MTGJSON's Meta.json has no date")
    return date


def download(folder, get=requests.get, progress=None):
    """The three files into a folder, each written as it arrives (the identifiers are 11 MB). Their paths, by name"""
    paths = {}
    for name in FILES:
        if progress:
            progress(f'Downloading {name.split("/")[-1]}')
        path = Path(folder) / name.split('/')[-1]
        with get(f'{MTGJSON}/{name}', headers=HEADERS, timeout=TIMEOUT, stream=True) as response:
            response.raise_for_status()
            with open(path, 'wb') as out:
                for chunk in response.iter_content(1 << 16):
                    out.write(chunk)
        paths[name.split('/')[-1]] = path
    return paths


def each_price(text):
    """
    (uuid, prices) for every card of AllPricesToday's 'data', one at a time. json.load of the whole file peaks at ~580 MB
    (the plan, measured): this reads the text one card at a time with the JSON decoder (raw_decode), ~120 MB at most -
    the decompressed text itself. Only this file's shape is understood: {"meta": ..., "data": {uuid: {...}, ...}}.
    """
    decoder = json.JSONDecoder()
    found = re.search(r'"data"\s*:\s*\{', text)
    if not found:
        raise ValueError('No "data" in the price file')
    blank = re.compile(r'[\s,]*')          # whitespace and the commas between two entries
    i = found.end()
    while True:
        i = blank.match(text, i).end()
        if i >= len(text):
            raise ValueError('The price file ends too early')
        if text[i] == '}':
            return
        uuid, i = decoder.raw_decode(text, i)
        i = blank.match(text, text.index(':', i) + 1).end()
        prices, i = decoder.raw_decode(text, i)
        yield uuid, prices


def file_date(text):
    """
    The build date the price file itself carries ({"meta": {"date": ...}, ...} - its first bytes). It can differ from
    Meta.json's: the two are separate files, published and cached apart (a Codex review of 2026-10-24: Meta.json's date
    was recorded for whatever price file came, and yesterday's prices saved as today's made every later update say
    "nothing new")
    """
    found = re.search(r'"meta"\s*:\s*', text[:4096])
    if found:
        try:
            meta, _ = json.JSONDecoder().raw_decode(text, found.end())
            if isinstance(meta, dict) and isinstance(meta.get('date'), str) and meta['date']:
                return meta['date']
        except ValueError:
            pass
    raise ValueError('The price file says no date')


def latest(points):
    """The value of the latest date in {date: value} (the file of today has one date per price), or None"""
    if not isinstance(points, dict) or not points:
        return None
    value = points[max(points)]
    try:
        return round(float(value), 2) if value is not None else None
    except (TypeError, ValueError):
        return None


def shop_values(prices):
    """{column: value} of one uuid's paper prices, for the shops kept (SHOPS); empty when it has none of them"""
    values = {}
    paper = prices.get('paper') if isinstance(prices, dict) else None
    for (provider, kind), prefix in SHOPS.items():
        kinds = (paper or {}).get(provider) or {}
        for finish, suffix in FINISHES.items():
            value = latest((kinds.get(kind) or {}).get(finish))
            if value is not None:
                values[prefix + suffix] = value
    return values


def scryfall_ids(csv_paths, wanted):
    """{uuid: Scryfall id} for the uuids wanted, read row by row from the identifier files (never all of them in memory)"""
    mapping = {}
    for path in csv_paths:
        with lzma.open(path, 'rt', encoding='utf-8', newline='') as text:
            for row in csv.DictReader(text):
                uuid, scryfall_id = row.get('uuid'), row.get('scryfallId')
                if uuid in wanted and scryfall_id:
                    mapping[uuid] = scryfall_id
    return mapping


def rows_from(price_text, csv_paths):
    """
    {Scryfall id: {column: value}} from the price file's text and the identifier files. A Scryfall id with two uuids (the
    faces of a double-faced card) gets every value either face has - the first face's where both have one (they never
    differed: the plan, "Measured"). Ids without any kept shop's price are left out.
    """
    by_uuid = {}
    for uuid, prices in each_price(price_text):
        values = shop_values(prices)
        if values:
            by_uuid[uuid] = values
    mapping = scryfall_ids(csv_paths, set(by_uuid))
    rows = {}
    for uuid, values in by_uuid.items():
        scryfall_id = mapping.get(uuid)
        if scryfall_id:
            merged = rows.setdefault(scryfall_id, {})
            for column, value in values.items():
                merged.setdefault(column, value)
    return rows


def import_prices(database, progress=None, force=False, get=requests.get):
    """
    Download today's shop prices and put them in place of the ones in use (CardDatabase.replace_shop_prices: whole or not
    at all). Returns (MTGJSON's date, how many printings got a price). Raises NothingNew, without downloading the price
    files, when MTGJSON's date is the one already in use (force: download anyway), and anything else that goes wrong
    (the network, a file of another shape) - the prices in use stay as they were.
    """
    date = meta_date(get)
    current = (database.get_data_info(INFO_KEY) or {}).get('source_updated')
    if date == current and not force and database.shop_priced_printings():
        raise NothingNew(f'The prices of {date} are the ones in use')
    with tempfile.TemporaryDirectory(prefix='mtgjson-') as folder:
        paths = download(folder, get, progress)
        if progress:
            progress('Reading the prices')
        with lzma.open(paths['AllPricesToday.json.xz'], 'rt', encoding='utf-8') as text:
            price_text = text.read()
        # The file's own date is the one recorded - never Meta.json's for a file of another build. Not newer than the
        # prices in use: nothing is saved (the next update looks again); a file newer than those in use but older than
        # Meta.json says is saved as what it is, and the next update, seeing Meta.json's other date, downloads again
        built = file_date(price_text)
        if built != date:
            logger.warning(f"Shop prices: Meta.json says {date}, the price file is of {built}")
        if current and built < current:
            raise ValueError(f'The price file is of {built}, older than the prices in use ({current}): not saved')
        if current and built == current and not force and database.shop_priced_printings():
            raise NothingNew(f'The price file is still the one of {built} (MTGJSON says {date}): tried again later')
        rows = rows_from(price_text, [paths['cardIdentifiers.csv.xz'], paths['tokenIdentifiers.csv.xz']])
        del price_text
    if progress:
        progress(f'Saving the prices of {len(rows):,} printings')
    database.replace_shop_prices(rows, built)
    logger.info(f'Shop prices of {built}: {len(rows):,} printings')
    return built, len(rows)

