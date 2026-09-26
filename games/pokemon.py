"""
Pokémon TCG - card data from TCGdex (https://tcgdex.dev, no API key), table `pokemon_cards`
in the card database file.

Download: one GraphQL request for every card (~24k, a few MB) and the set details (printed set
abbreviation, official card count) from the REST API. Pokémon TCG Pocket (digital) is left out.
Prices (TCGplayer, USD, per finish) are not in the bulk data: they are fetched for a card when
it is matched or picked, and cached for a day.

Matching what the AI read (name, number "012/193", set abbreviation "PAL" - printed on cards
since Scarlet & Violet; older cards only have a set symbol):
1. set abbreviation + number, when the name matches            -> set_number (confirmed)
2. name + number, narrowed by the set total ("/193") to one card -> name_number (confirmed)
3. otherwise the newest printing of the name (review)
"""
import csv
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from difflib import SequenceMatcher, get_close_matches

import requests

from config import Config
from database import _edit_distance, search_key
from games.base import Game

HEADERS = {'User-Agent': 'CardScanner/1.0', 'Accept': 'application/json'}
PRICE_MAX_AGE = timedelta(days=1)
PRICE_RETRY_OFFLINE = timedelta(minutes=5)  # no price requests after a failed connection

# TCGdex variant flag -> finish key
VARIANT_FINISHES = {'normal': 'normal', 'holo': 'holo', 'reverse': 'reverse', 'firstEdition': 'first_edition'}
# TCGplayer price key -> finish key
TCGPLAYER_FINISHES = {'normal': 'normal', 'holofoil': 'holo', 'reverse-holofoil': 'reverse',
                      '1stEditionHolofoil': 'first_edition', '1stEditionNormal': 'first_edition',
                      '1st-edition-holofoil': 'first_edition', '1st-edition-normal': 'first_edition'}
DIGITAL_SERIES = {'tcgp'}  # Pokémon TCG Pocket

COLUMNS = [
    ('id', 'TEXT PRIMARY KEY'),
    ('name', 'TEXT NOT NULL'),
    ('name_search', 'TEXT'),
    ('set_id', 'TEXT'),
    ('set_code', 'TEXT'),       # printed abbreviation ("PAL"), uppercase
    ('set_name', 'TEXT'),
    ('set_total', 'INTEGER'),   # official card count, printed after the number ("/193")
    ('serie', 'TEXT'),
    ('released_at', 'TEXT'),
    ('number', 'TEXT'),         # as printed ("012", "TG05", "SWSH001")
    ('number_key', 'TEXT'),     # see number_key()
    ('rarity', 'TEXT'),
    ('category', 'TEXT'),       # Pokemon / Trainer / Energy
    ('types', 'TEXT'),          # JSON list ("Fire")
    ('stage', 'TEXT'),
    ('hp', 'INTEGER'),
    ('trainer_type', 'TEXT'),
    ('energy_type', 'TEXT'),
    ('finishes', 'TEXT'),       # JSON list of finish keys the card exists in
    ('image_url', 'TEXT'),      # TCGdex asset base (+ /high.webp, /low.webp)
    ('prices', 'TEXT'),         # JSON {finish: USD}, fetched on demand
    ('prices_updated', 'TEXT'),
]
COLUMN_NAMES = [name for name, _ in COLUMNS]

CARDS_QUERY = ('{ cards { id localId name rarity category image hp types stage trainerType energyType '
               'variants { normal reverse holo firstEdition } set { id } } }')
SETS_QUERY = '{ sets { id name releaseDate serie { id } cardCount { official } } }'

CSV_COLUMNS = ['Card Name', 'Set', 'Card Number', 'Rarity', 'Type', 'Finish', 'Price (USD)',
               'Quantity', 'Condition', 'Timestamp']


def number_key(text):
    """Comparable card number: no leading zeros, uppercase ("012" -> "12", "TG05" -> "TG5")"""
    text = (text or '').strip().upper().replace(' ', '')
    match = re.fullmatch(r'([A-Z]*)0*(\d+)([A-Z]?)', text)
    return f"{match.group(1)}{int(match.group(2))}{match.group(3)}" if match else text


PROMO_NUMBER = re.compile(r'\b(SWSH|SVP|SM|XY|BW|DP|HGSS)\s?(\d{1,3})\b', re.IGNORECASE)


def parse_number(text):
    """Number read from a card ("012/193", "TG05/TG30", "12", promo "SWSH095") -> (number key,
    set total or None)"""
    promo = PROMO_NUMBER.search(text or '')
    if promo:
        # Black Star promos print a code instead of number/total; SVP promos print "SVP 045"
        prefix = '' if promo.group(1).upper() == 'SVP' else promo.group(1)
        return number_key(prefix + promo.group(2)), None
    left, _, right = (text or '').replace(' ', '').partition('/')
    total = re.search(r'\d+', right)
    return (number_key(left) or None), (int(total.group()) if total else None)


NAME_SUFFIXES = {'ex', 'v', 'vmax', 'vstar', 'v union', 'gx', 'break', 'prime', 'legend', 'lv.x', 'star',
                 'δ', '☆', 'tag team', 'radiant'}


ENERGY_TYPES = {'grass', 'fire', 'water', 'lightning', 'psychic', 'fighting', 'darkness', 'metal', 'fairy'}


def basic_energy(name):
    """For a name read from a basic Energy card: (True, its type or None); else (False, None).
    Modern ones print "Basic <symbol> Energy" and the AI can't name the symbol reliably
    (it read Metal as Fairy), so the type is often missing or wrong"""
    match = re.fullmatch(r'(?:basic )?(?:(\w+) )?energy', search_key(name) or '')
    if not match or (match.group(1) and match.group(1) not in ENERGY_TYPES):
        return False, None
    return True, match.group(1)


def names_match(query, name):
    """A name read from a card against a card name: same, one a prefix of the other
    ("Charizard" / "Charizard ex"), or a close spelling"""
    query_key, key = search_key(query), search_key(name)
    if not query_key or not key:
        return False
    # Modern basic Energy cards print "Basic <symbol> Energy"; the data says "Water Energy"
    query_key, key = (re.sub(r'^basic (?=.*energy$)', '', k) for k in (query_key, key))
    if len(query_key) > 30:
        # A sentence instead of a name ("the text at the top reads ho-oh v"): the name inside it
        return re.search(rf'(?<!\w){re.escape(key)}(?!\w)', query_key) is not None
    if query_key == key or SequenceMatcher(None, query_key, key).ratio() >= 0.8:
        return True
    # One is the other plus a suffix ("Charizard" / "Charizard ex") - only a suffix: "Energy"
    # must not match "Energy Retrieval"
    short, long = sorted((query_key, key), key=len)
    return long.startswith(short + ' ') and long[len(short) + 1:].replace('-', ' ') in NAME_SUFFIXES


def write_csv(rows, file):
    writer = csv.writer(file)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([row['name'], row['set_name'], row['number'], row['rarity'], row['type_line'],
                         row['finish'], f"${row['price']:.2f}", row['quantity'], row['condition'],
                         row['timestamp']])


class Pokemon(Game):
    id = 'pokemon'
    label = 'Pokémon'
    source = 'TCGdex'
    set_example = 'PAL'
    number_example = '012/193'
    finishes = {'normal': 'Normal', 'holo': 'Holo', 'reverse': 'Reverse holo', 'first_edition': '1st edition'}
    confirmed_matches = {'set_number', 'name_number'}

    def __init__(self, database, log_callback=None):
        super().__init__(database, log_callback)
        self._names = None  # distinct card names, for fuzzy matching
        self._price_lock = threading.Lock()
        self._prices_offline_until = datetime.min
        with self.db._lock:
            cursor = self.db.conn.cursor()
            self._create_table(cursor, 'pokemon_cards', if_not_exists=True)
            self._create_indexes(cursor)
            # Every set at the last download (also those without cards), for the update check
            cursor.execute('CREATE TABLE IF NOT EXISTS pokemon_sets (id TEXT PRIMARY KEY, name TEXT, '
                           'set_code TEXT, set_total INTEGER, serie TEXT, released_at TEXT)')
            self.db.conn.commit()

    # -- Card data -----------------------------------------------------------

    @staticmethod
    def _create_table(cursor, table, if_not_exists=False):
        columns = ', '.join(f"{name} {kind}" for name, kind in COLUMNS)
        cursor.execute(f"CREATE TABLE {'IF NOT EXISTS ' if if_not_exists else ''}{table} ({columns})")

    @staticmethod
    def _create_indexes(cursor):
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_pokemon_name ON pokemon_cards(name_search)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_pokemon_number ON pokemon_cards(number_key)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_pokemon_set ON pokemon_cards(set_code, number_key)')

    def card_count(self):
        with self.db._lock:
            return self.db.conn.execute('SELECT COUNT(*) FROM pokemon_cards').fetchone()[0]

    @staticmethod
    def _graphql(query):
        response = requests.post(Config.TCGDEX_URL.rsplit('/', 1)[0] + '/graphql', json={'query': query},
                                 headers=HEADERS, timeout=120)
        response.raise_for_status()
        data = response.json()
        if data.get('errors'):
            raise Exception(f"TCGdex: {data['errors'][0].get('message')}")
        return data['data']

    def _paper_sets(self):
        return [s for s in self._graphql(SETS_QUERY)['sets'] if (s.get('serie') or {}).get('id') not in DIGITAL_SERIES]

    def download(self, progress_callback=None):
        progress = progress_callback or (lambda message: None)
        progress("Fetching the Pokémon set list from TCGdex...")
        sets = {s['id']: s for s in self._paper_sets()}

        # Printed set abbreviations ("PAL") are only in the REST set details
        progress(f"Fetching details of {len(sets)} sets...")
        session = requests.Session()

        def set_details(set_id):
            try:
                return session.get(f"{Config.TCGDEX_URL}/sets/{set_id}", headers=HEADERS, timeout=30).json()
            except (requests.RequestException, ValueError):
                return {}
        with ThreadPoolExecutor(8) as pool:
            for details in pool.map(set_details, list(sets)):
                if details.get('id') in sets:
                    sets[details['id']]['abbreviation'] = (details.get('abbreviation') or {}).get('official')

        progress("Downloading every Pokémon card...")
        cards = [c for c in self._graphql(CARDS_QUERY)['cards'] if c['set']['id'] in sets]
        progress(f"Saving {len(cards):,} cards...")

        with self.db._lock:
            cursor = self.db.conn.cursor()
            # Keep the prices already fetched
            cached = {row['id']: (row['prices'], row['prices_updated']) for row in
                      cursor.execute('SELECT id, prices, prices_updated FROM pokemon_cards WHERE prices IS NOT NULL')}
            cursor.execute('DROP TABLE IF EXISTS pokemon_cards_import')
            self._create_table(cursor, 'pokemon_cards_import')
            rows = []
            for card in cards:
                card_set = sets[card['set']['id']]
                variants = card.get('variants') or {}
                prices, prices_updated = cached.get(card['id'], (None, None))
                rows.append((
                    card['id'], card['name'], search_key(card['name']), card_set['id'],
                    (card_set.get('abbreviation') or '').upper() or None, card_set['name'],
                    (card_set.get('cardCount') or {}).get('official'), card_set['serie']['id'],
                    card_set.get('releaseDate'), card['localId'], number_key(card['localId']),
                    card.get('rarity') if card.get('rarity') != 'None' else None, card.get('category'), json.dumps(card.get('types') or []),
                    card.get('stage'), card.get('hp'), card.get('trainerType'), card.get('energyType'),
                    json.dumps([key for flag, key in VARIANT_FINISHES.items() if variants.get(flag)]),
                    card.get('image'), prices, prices_updated,
                ))
            cursor.executemany(f"INSERT OR REPLACE INTO pokemon_cards_import VALUES ({', '.join('?' * len(COLUMNS))})", rows)
            self.db.replace_table('pokemon_cards_import', 'pokemon_cards', self._create_indexes)
            cursor.execute('DELETE FROM pokemon_sets')
            cursor.executemany('INSERT INTO pokemon_sets VALUES (?, ?, ?, ?, ?, ?)', [
                (s['id'], s['name'], (s.get('abbreviation') or '').upper() or None,
                 (s.get('cardCount') or {}).get('official'), s['serie']['id'], s.get('releaseDate'))
                for s in sets.values()])
            self.db.conn.commit()
        self._names = self._codes = None
        self.db.set_data_info(self.id, max((s.get('releaseDate') or '') for s in sets.values()), len(rows))
        progress(f"Pokémon card data ready: {len(rows):,} cards in {len(sets)} sets")
        return len(rows)

    def check_for_update(self):
        with self.db._lock:
            local = {row[0] for row in self.db.conn.execute('SELECT id FROM pokemon_sets')}
        new = [s['name'] for s in self._paper_sets() if s['id'] not in local]
        if not new:
            return None
        return f"TCGdex has {len(new)} new set{'s' if len(new) > 1 else ''}: {', '.join(new[:3])}" \
               + (' ...' if len(new) > 3 else '')

    # -- Cards ---------------------------------------------------------------

    def _rows(self, where, params=()):
        with self.db._lock:
            return self.db.conn.execute(
                f'SELECT * FROM pokemon_cards WHERE {where} ORDER BY released_at DESC, set_id, number_key',
                params).fetchall()

    def _card(self, row, match=None):
        card = {key: row[key] for key in row.keys()}
        card.update(set=row['set_name'], types=json.loads(row['types'] or '[]'),
                    finishes=json.loads(row['finishes'] or '[]'), prices=json.loads(row['prices'] or '{}'))
        if match:
            card['match'] = match
        return card

    def _with_prices(self, card):
        """The card with its TCGplayer prices, fetched when missing or older than a day"""
        updated = card.get('prices_updated')
        if updated and datetime.now() - datetime.fromisoformat(updated) < PRICE_MAX_AGE:
            return card
        with self._price_lock:
            # Offline, every card would wait for the timeout: after a failed connection the
            # cached prices (or none) are used for a while
            if datetime.now() < self._prices_offline_until:
                return card
            try:
                details = requests.get(f"{Config.TCGDEX_URL}/cards/{card['id']}", headers=HEADERS, timeout=5).json()
            except (requests.ConnectionError, requests.Timeout) as e:
                self._prices_offline_until = datetime.now() + PRICE_RETRY_OFFLINE
                self.log(f"TCGdex unreachable - no price updates for {PRICE_RETRY_OFFLINE.seconds // 60} min ({e})",
                         level='warning')
                return card
            except (requests.RequestException, ValueError) as e:
                self.log(f"Could not fetch prices for {card['name']}: {e}", level='warning')
                return card
        prices = {}
        pricing = [details.get('pricing') or {}] + [v.get('pricing') or {} for v in details.get('variants_detailed') or []]
        for source in pricing:
            for key, value in (source.get('tcgplayer') or {}).items():
                finish = TCGPLAYER_FINISHES.get(key)
                price = isinstance(value, dict) and (value.get('marketPrice') or value.get('midPrice'))
                if finish and price and finish not in prices:
                    prices[finish] = float(price)
        card['prices'], card['prices_updated'] = prices, datetime.now().isoformat(timespec='seconds')
        with self.db._lock:
            self.db.conn.execute('UPDATE pokemon_cards SET prices = ?, prices_updated = ? WHERE id = ?',
                                 (json.dumps(prices), card['prices_updated'], card['id']))
            self.db.conn.commit()
        return card

    def _set_codes(self):
        if getattr(self, '_codes', None) is None:
            with self.db._lock:
                self._codes = {row[0] for row in self.db.conn.execute(
                    'SELECT DISTINCT set_code FROM pokemon_cards WHERE set_code IS NOT NULL')}
        return self._codes

    def _all_names(self):
        if self._names is None:
            with self.db._lock:
                self._names = {row[0]: row[1] for row in
                               self.db.conn.execute('SELECT name_search, name FROM pokemon_cards GROUP BY name_search')}
        return self._names

    def _resolve_name(self, name):
        """Card name for what was read or typed: exact (accent/case-insensitive) or closest"""
        key = search_key(name)
        names = self._all_names()
        if key in names:
            return names[key], 'name'
        close = get_close_matches(key or '', list(names), n=1, cutoff=0.75)
        return (names[close[0]], 'fuzzy') if close else (None, None)

    # -- Matching and search -------------------------------------------------

    def identify(self, name, number=None, set_code=None, ai_model=None):
        card = self._identify(name, number, (set_code or '').strip().upper())
        if card:
            self.log(f"Matched {card['name']} ({card['set']} #{card['number']}) by {card['match']}")
            card = self._with_prices(card)
        return card

    def _identify(self, name, number, set_code):
        key, total = parse_number(number)
        if PROMO_NUMBER.fullmatch(set_code) and not (key and total):
            key, total = parse_number(set_code)  # a promo code ("SM10") read as the set
            set_code = ''
        read_code = set_code
        if set_code.endswith('EN') and len(set_code) > 3 and set_code not in self._set_codes():
            set_code = set_code[:-2]  # "PREN": the set and language codes read as one

        # 1. Printed set abbreviation + number. A code no set has may be a misread one
        # ("SYE" for SVE, "MEEE" for MEE + EN): the codes one letter away count if exactly one
        # of them has a card of that name and number
        set_number_row = None
        is_energy, energy_type = basic_energy(name)
        if is_energy:
            card = self._identify_energy(key, {set_code, read_code}, energy_type)
            if card:
                return card
            # No set code / number read (older Energy prints none): the name only, for review
        if set_code and key and not is_energy:
            codes = self._codes_like(set_code)
            rows = [row for code in codes for row in self._rows('set_code = ? AND number_key = ?', (code, key))]
            named = [row for row in rows if names_match(name, row['name'])]
            if len(named) == 1:
                return self._card(named[0], 'set_number')
            set_number_row = rows[0] if len(codes) == 1 and rows else None

        # 2. Name + number, narrowed by the set total
        if key and not is_energy:
            rows = [row for row in self._rows('number_key = ?', (key,)) if names_match(name, row['name'])]
            # "Charizard" read: Charizard before Charizard δ / Charizard ex
            rows = [row for row in rows if row['name_search'] == search_key(name)] or rows
            if total:
                same_total = [row for row in rows if row['set_total'] == total]
                if not same_total and rows:
                    # No set of that size: something else was read as the number (review)
                    return self._card(rows[0], 'name_number_other_total')
                rows = same_total
            if len(rows) == 1 and not (rows[0]['category'] == 'Energy' and not total):
                return self._card(rows[0], 'name_number')
            if rows:
                # Several, or an Energy: basic Energy numbers have no set total and the same
                # names recur in many sets, so a misread number would pick another Energy
                return self._card(rows[0], 'name_number_ambiguous')

        # 3. Name only: the newest printing (for review)
        resolved, how = self._resolve_name(name)
        if resolved:
            rows = self._rows('name = ?', (resolved,))
            if rows:
                in_set = [row for row in rows if set_code and row['set_code'] == set_code]
                return self._card((in_set or rows)[0], how)

        # Name not recognized: trust the printed set + number
        if set_number_row:
            return self._card(set_number_row, 'set_number_unverified')
        return None

    def _codes_like(self, set_code):
        """The set code read, or the codes one letter away when no set has it"""
        if set_code in self._set_codes():
            return [set_code]
        return [code for code in self._set_codes() if _edit_distance(code, set_code) == 1]

    def _identify_energy(self, key, set_codes, energy_type):
        """
        A basic Energy: by set code + number (no set total is printed). Confirmed only when
        the type read agrees - a misread number (011 read as 017) would otherwise add another
        type; with the type missing the card is shown for review
        """
        codes = {code for read in set_codes if read for code in self._codes_like(read)}
        if not (codes and key):
            return None
        rows = [row for code in codes
                for row in self._rows("set_code = ? AND number_key = ? AND category = 'Energy'", (code, key))]
        if len(rows) != 1:
            return None
        type_agrees = energy_type and energy_type in search_key(rows[0]['name'])
        return self._card(rows[0], 'set_number' if type_agrees else 'set_number_energy')

    def find_printings(self, name, number=None, treatment=None, set_code=None):
        resolved, _how = self._resolve_name(name)
        if not resolved:
            return None, []
        rows = self._rows('name = ?', (resolved,))
        key, total = parse_number(number)
        set_code = (set_code or '').strip().upper()
        # Narrow by what was given, as long as something is left
        for keep in (lambda row: not set_code or row['set_code'] == set_code,
                     lambda row: not key or row['number_key'] == key,
                     lambda row: not total or row['set_total'] == total):
            rows = [row for row in rows if keep(row)] or rows
        cards = [self._card(row) for row in rows[:200]]
        if len(cards) == 1:
            cards[0] = self._with_prices(cards[0])
        return resolved, cards

    def similar(self, name, limit=5):
        names = self._all_names()
        result = []
        for close in get_close_matches(search_key(name) or '', list(names), n=limit, cutoff=0.5):
            row = self._rows('name_search = ?', (close,))[0]
            result.append({'name': row['name'], 'set': row['set_name'], 'price': 'N/A'})
        return result

    def get_card(self, card_id):
        rows = self._rows('id = ?', (card_id,))
        return self._with_prices(self._card(rows[0])) if rows else None

    @staticmethod
    def _type_line(card):
        parts = [card.get('category') == 'Pokemon' and 'Pokémon' or card.get('category')]
        parts += card.get('types') or []
        parts += [re.sub(r'Stage(\d)', r'Stage \1', card.get('stage') or ''), card.get('trainer_type'), card.get('energy_type'),
                  f"{card['hp']} HP" if card.get('hp') else None]
        return ' · '.join(p for p in parts if p)

    @staticmethod
    def _printed_number(card):
        return f"{card['number']}/{card['set_total']}" if card.get('set_total') and card['number'].isdigit() \
            else card['number']

    def card_payload(self, card):
        options = [key for key in self.finishes if key in card['finishes']] or [self.default_finish]
        prices = card.get('prices') or {}
        image = card.get('image_url')
        return {
            'id': card['id'],
            'name': card['name'],
            'set': card['set'],
            'set_code': card.get('set_code') or '',
            'number': self._printed_number(card),
            'rarity': card.get('rarity') or '',
            'type': self._type_line(card),
            'price': prices.get(options[0], 0.0),
            'price_foil': 0.0,
            # [[finish label, USD]] for the finishes this card exists in
            'prices': [[self.finishes[key], prices[key]] for key in options if prices.get(key)],
            'image_uri': f"{image}/high.webp" if image else '',
            'thumb_uri': f"{image}/low.webp" if image else '',
            'treatments': [],
            'finishes': [],
            'finish_options': options,
            'confirmed': self.is_confirmed(card) if card.get('match') else None,
        }

    # -- Inventory -----------------------------------------------------------

    def inventory_fields(self, card, finish):
        prices = card.get('prices') or {}
        types = card.get('types') or []
        return {
            'card_id': card['id'],
            'name': card['name'],
            'set_name': card['set'],
            'set_code': card.get('set_code') or '',
            'number': self._printed_number(card),
            'rarity': card.get('rarity') or '',
            'type_line': self._type_line(card),
            'colors': ', '.join(types),
            # Filterable like Magic's colors: the energy type, or Trainer / Energy
            'color_identity': types[0] if len(types) == 1 else 'Multicolor' if types else (card.get('category') or ''),
            # A finish without a price: the first priced one, in finish order
            'price': float(prices.get(finish) or next((prices[k] for k in self.finishes if prices.get(k)), 0.0)),
        }

    def export_formats(self):
        return {'csv': ('CSV', 'pokemon_inventory_export', write_csv)}
