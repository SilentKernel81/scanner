"""
The card scanner MCP server's tools (mcp_server/tools.py): finding a deck by name, what each tool
answers, and that `propose_deck_changes` changes nothing. A stand-in for the server's web API - no
network and no MCP library (that is only needed by card_scanner_mcp.py).

    venv/bin/python -m unittest discover tests -p test_mcp_tools.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'mcp_server'))

import tools  # noqa: E402
import scanner_api  # noqa: E402
from scanner_api import ScannerError, deck_write_allowed  # noqa: E402


def card(name, identity=(), type_line='Creature', price=1.0, owned=1, cost='{1}', cmc=1.0, text='', rarity='rare'):
    return {'id': name, 'name': name, 'type_line': type_line, 'mana_cost': cost, 'cmc': cmc, 'colors': list(identity),
            'identity': list(identity), 'rarity': rarity, 'oracle_text': text, 'price': price, 'owned': owned}


def entry(name, quantity=1, board='main', owned=1, elsewhere=(), **kwargs):
    return {'name': name, 'quantity': quantity, 'board': board, 'owned': owned,
            'elsewhere': [{'deck': d, 'deck_id': 9, 'quantity': 1} for d in elsewhere], 'card': card(name, **kwargs)}


DECK = {'id': 7, 'name': 'Calling All Angels', 'format': 'commander', 'kind': 'paper', 'notes': '', 'issues': [],
        'totals': {'cards': 4, 'side': 0, 'price': 10.0, 'missing': 1, 'missing_price': 2.0},
        'cards': [entry('Giada, Font of Hope', board='commander', identity='W', type_line='Legendary Creature'),
                  entry('Plains', quantity=2, owned=50, type_line='Basic Land — Plains', elsewhere=('A', 'B', 'C', 'D', 'E', 'F')),
                  entry('Sol Ring', owned=3, type_line='Artifact', elsewhere=('Other deck',)),
                  entry('Resplendent Angel', owned=0, identity='W', price=2.0)]}
DECKS = [{'id': 7, 'name': 'Calling All Angels', 'format': 'commander', 'kind': 'paper', 'commanders': ['Giada, Font of Hope'],
          'cards': 4, 'owned': 3, 'owned_percent': 75},
         {'id': 8, 'name': 'Wretched Ranks', 'format': 'commander', 'kind': 'paper', 'commanders': ['Ghoulcaller Gisa'],
          'cards': 100, 'owned': 100, 'owned_percent': 100},
         {'id': 9, 'name': 'Angels', 'format': 'standard', 'kind': 'paper', 'commanders': [], 'cards': 60, 'owned': 60, 'owned_percent': 100},
         {'id': 12, 'name': 'Elves on Arena', 'format': 'standard', 'kind': 'arena', 'commanders': [], 'cards': 7, 'owned': 6,
          'owned_percent': 86}]
# An Arena deck: 'owned' is the Arena collection's, a missing card costs a wildcard of its rarity
ARENA_DECK = {'id': 12, 'name': 'Elves on Arena', 'format': 'standard', 'kind': 'arena', 'notes': '', 'issues': [],
              'from_arena': True, 'totals': {'cards': 7, 'side': 0, 'price': 5.0, 'missing': 1, 'missing_price': 2.0},
              'wildcards': {'needed': {'common': 0, 'uncommon': 0, 'rare': 1, 'mythic': 0},
                            'have': {'common': 5, 'uncommon': 2, 'rare': 1, 'mythic': 0}},
              'cards': [entry('Forest', quantity=2, owned=40, type_line='Basic Land — Forest', rarity='common'),
                        entry('Llanowar Elves', quantity=4, owned=5, identity='G', rarity='common'),
                        entry('Bejeweled Warg', quantity=1, owned=0, identity='G', rarity='rare')]}
ARENA_INVENTORY = [
    {'name': 'Llanowar Elves', 'set_code': 'fdn', 'number': '227', 'quantity': 5, 'rarity': 'common', 'price': 0.15,
     'type_line': 'Creature — Elf Druid', 'set_name': 'Foundations', 'decks': ['Elves on Arena', 'Other Arena deck']},
    {'name': 'Sheoldred, the Apocalypse', 'set_code': 'dmu', 'number': '107', 'quantity': 1, 'rarity': 'mythic', 'price': 80.0,
     'type_line': 'Legendary Creature', 'set_name': 'Dominaria United', 'decks': []}]
INVENTORY = [
    {'name': 'Sol Ring', 'set_code': 'cmm', 'number': '1', 'finish': 'regular', 'quantity': 3, 'condition': 'Near Mint',
     'location': 'Box 1', 'tags': [], 'price': 1.0, 'rarity': 'uncommon', 'type_line': 'Artifact', 'decks': ['Calling All Angels', 'Wretched Ranks']},
    {'name': 'Lightning Bolt', 'set_code': 'm11', 'number': '149', 'finish': 'foil', 'quantity': 1, 'condition': 'Near Mint',
     'location': 'Box 2', 'tags': ['trade'], 'price': 3.0, 'rarity': 'common', 'type_line': 'Instant', 'decks': []},
    {'name': 'Resplendent Angel', 'set_code': 'm19', 'number': '5', 'finish': 'regular', 'quantity': 0, 'condition': 'Near Mint',
     'location': '', 'tags': [], 'price': 7.0, 'rarity': 'mythic', 'type_line': 'Creature — Angel', 'decks': []}]
CARDS = {c['name'].lower(): c for c in (
    card('Sol Ring', type_line='Artifact', owned=3), card('Lightning Bolt', identity='R', type_line='Instant', owned=1),
    card('Resplendent Angel', identity='W', owned=0), card('Swords to Plowshares', identity='W', owned=2),
    card('Illegal Card', identity='W'), card('Llanowar Elves', identity='G', owned=5, rarity='common'),
    card('Giada, Font of Hope', identity='W', type_line='Legendary Creature', owned=1),
    card('Plains', identity='W', type_line='Basic Land — Plains', owned=50, rarity='common'),
    card('Bejeweled Warg', identity='G', owned=0), card('Elvish Mystic', identity='G', owned=3, rarity='common'),
    card('Relentless Rats', identity='B', text='A deck can have any number of cards named Relentless Rats.'))}


class FakeApi:
    """The server's answers for the paths the tools use; counts what was written (nothing should be)"""

    def __init__(self, currency='EUR'):
        self.posts = []
        self.searches = []        # the parameters of each card search, to see what the tools asked for
        self.writes = []          # (method, path, body) of each change to a deck
        self.currency = currency

    def get(self, path, **params):
        if path == '/api/decks':
            return {'decks': DECKS}
        if path == '/api/decks/7':
            return {'deck': DECK}
        if path == '/api/inventory':
            return {'cards': ARENA_INVENTORY if params.get('area') == 'arena' else INVENTORY}
        if path == '/api/decks/12':
            return {'deck': ARENA_DECK}
        if path == '/api/decks/12/arena':
            return {'text': '', 'left_out': ['Paper Only Card'], 'cards': 7, 'check': {
                'imported': '2026-10-29T18:42', 'have': 6, 'craft': [{'name': 'Bejeweled Warg', 'rarity': 'rare', 'copies': 1}],
                'not_on_arena': 1, 'wildcards': ARENA_DECK['wildcards']}}
        if path == '/api/arena':
            return {'arena': {'cards': 6, 'entries': 2, 'exported': '2026-10-29T18:42',
                              'wildcards': {'common': 5, 'uncommon': 2, 'rare': 1, 'mythic': 0}, 'gold': 125, 'gems': 130}}
        if path == '/api/stats':
            return {'collection': {'total_cards': 4, 'unique_cards': 3, 'total_value': 12.0}}
        if path == '/api/prices/source':
            if self.currency is None:                    # a server from before the price sources: no such route
                raise ScannerError('The scanner server answered 404 for /api/prices/source', status=404)
            if self.currency == 'away':                  # a time-out, the server away: no answer at all
                raise ScannerError('Could not reach the scanner server')
            if self.currency == 500:
                raise ScannerError('The scanner server answered 500 for /api/prices/source', status=500)
            return {'success': True, 'source': 'scryfall_eur', 'currency': self.currency, 'symbol': '€'}
        if path == '/api/cards/search':
            name = (params.get('q') or '').lower()
            found = [c for key, c in CARDS.items() if name in key]
            if params.get('format'):   # only what is legal in the format
                found = [c for c in found if c['name'] != 'Illegal Card']
            if params.get('owned'):
                found = [c for c in found if c['owned']]
            self.searches.append(params)
            return {'cards': found, 'more': False}
        if path == '/api/decks/7/suggestions':
            return {'url': 'https://edhrec.example', 'decks': 100, 'categories': [
                {'title': 'Top Cards', 'cards': [
                    {'name': 'Sol Ring', 'type_line': 'Artifact', 'mana_cost': '{1}', 'inclusion': 90, 'synergy': 1, 'owned': 3, 'elsewhere': True, 'price': 1.0},
                    {'name': 'Swords to Plowshares', 'type_line': 'Instant', 'mana_cost': '{W}', 'inclusion': 80, 'synergy': 5, 'owned': 0, 'elsewhere': False, 'price': 2.0}]}]}
        raise AssertionError(f'unexpected GET {path}')

    def write(self, method, path, body):
        """A change to a deck, as ScannerApi.write: only a deck route, else refused before anything is sent"""
        if not deck_write_allowed(method, path):
            raise ScannerError(f'Refused: {method} {path} is not a change to a deck')
        self.writes.append((method, path, body))
        if path == '/api/decks':
            made = {'id': 50, 'name': body['name'], 'format': body['format'], 'kind': body['kind'], 'issues': [],
                    'totals': {'cards': 1, 'side': 0, 'price': 1.0, 'missing': 0, 'missing_price': 0.0}}
            return {'deck': made, 'unknown': []}
        if method == 'PUT':
            return {'deck': {**DECK, 'name': body.get('name', DECK['name'])}}
        return {'deck': DECK}

    def post(self, path, body):
        self.posts.append(path)
        return {'success': True}

    get_fresh = get


class Tools(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()

    def test_a_deck_is_found_by_name_part_of_it_commander_or_id(self):
        for wanted in ('Calling All Angels', 'calling all', 'Giada', '7'):
            self.assertEqual(tools.find_deck(self.api, wanted)['id'], 7, wanted)

    def test_a_name_that_matches_several_decks_says_which(self):
        with self.assertRaises(ScannerError) as raised:
            tools.find_deck(self.api, 'ang')                      # Calling All Angels and Angels
        self.assertIn('Several decks match', str(raised.exception))
        self.assertIn('#7', str(raised.exception))

    def test_a_name_that_matches_none_lists_the_decks(self):
        with self.assertRaises(ScannerError) as raised:
            tools.find_deck(self.api, 'Zebras')
        self.assertIn('Wretched Ranks', str(raised.exception))

    def test_list_decks_is_short(self):
        decks = tools.list_decks(self.api)
        self.assertEqual([d['name'] for d in decks], ['Calling All Angels', 'Wretched Ranks', 'Angels', 'Elves on Arena'])
        self.assertEqual(set(decks[0]), {'id', 'name', 'format', 'kind', 'commanders', 'cards', 'owned', 'owned_percent'})

    def test_get_deck_groups_by_board_and_leaves_out_the_noise_of_basic_lands(self):
        deck = tools.get_deck(self.api, 'Calling All Angels')
        self.assertEqual(set(deck['boards']), {'commander', 'main'})
        plains = next(c for c in deck['boards']['main'] if c['name'] == 'Plains')
        self.assertNotIn('also_in_decks', plains)                    # every deck wants basic lands
        sol = next(c for c in deck['boards']['main'] if c['name'] == 'Sol Ring')
        self.assertEqual(sol['also_in_decks'], ['Other deck'])
        self.assertNotIn('text', sol)
        self.assertIn('text', tools.get_deck(self.api, 'Giada', with_card_text=True)['boards']['main'][0])

    def test_missing_cards_counts_what_is_not_owned(self):
        missing = tools.missing_cards(self.api, 'Calling All Angels')
        self.assertEqual(missing['missing_cards'], [{'name': 'Resplendent Angel', 'missing': 1, 'price_each': 2.0}])
        self.assertEqual(missing['cost_of_the_missing'], 2.0)

    def test_suggestions_can_be_limited_to_what_is_owned(self):
        everything = tools.suggest_for_deck(self.api, 'Giada')['categories'][0]['cards']
        owned = tools.suggest_for_deck(self.api, 'Giada', only_owned=True)['categories'][0]['cards']
        self.assertEqual([c['name'] for c in everything], ['Sol Ring', 'Swords to Plowshares'])
        self.assertEqual([c['name'] for c in owned], ['Sol Ring'])
        self.assertTrue(everything[0]['used_by_another_deck'])

    def test_a_proposal_checks_legality_colours_singleton_and_ownership_and_changes_nothing(self):
        answer = tools.propose_deck_changes(
            self.api, 'Giada',
            add=[{'name': 'Swords to Plowshares'}, {'name': 'Lightning Bolt'}, {'name': 'Sol Ring'},
                 {'name': 'Illegal Card'}, {'name': 'Not A Card'}],
            remove=[{'name': 'Plains', 'quantity': 1}, {'name': 'Zzz'}])
        by_name = {c['name']: c for c in answer['add']}
        self.assertNotIn('problem', by_name['Swords to Plowshares'])
        self.assertEqual(by_name['Swords to Plowshares']['copies_owned'], 2)
        self.assertIn("outside the commander's colours", by_name['Lightning Bolt']['problem'])
        self.assertIn('one copy', by_name['Sol Ring']['problem'])            # already in the deck: singleton
        self.assertEqual(by_name['Sol Ring']['also_used_by'], ['Wretched Ranks'])   # (not the deck itself)
        self.assertEqual(by_name['Illegal Card']['problem'], 'not legal in commander')
        self.assertEqual(by_name['Not A Card']['problem'], 'no card with this name in the card database')
        self.assertEqual(answer['remove'][0]['copies_removed'], 1)
        self.assertEqual(answer['remove'][1]['problem'], 'not in the deck')
        self.assertTrue(answer['nothing_was_changed'])
        self.assertEqual(self.api.posts, [])                                  # nothing was written

    def test_search_the_collection(self):
        found = tools.search_collection(self.api, text='bolt')
        self.assertEqual((found['matches'], found['cards'][0]['name']), (1, 'Lightning Bolt'))
        self.assertEqual(tools.search_collection(self.api, tag='trade')['matches'], 1)
        self.assertEqual(tools.search_collection(self.api, in_a_deck=True)['matches'], 1)
        self.assertEqual(tools.search_collection(self.api, rarity='mythic')['cards'][0]['rarity'], 'mythic')
        self.assertEqual(tools.search_collection(self.api, limit=1)['shown'], 1)

    def test_find_a_card_and_the_summary(self):
        found = tools.find_card(self.api, 'sol ring')
        self.assertEqual((found['name'], found['copies_owned']), ('Sol Ring', 3))
        summary = tools.collection_summary(self.api)
        self.assertEqual((summary['cards'], summary['in_a_deck']), (4, 3))
        self.assertEqual((summary['value'], summary['currency']), (12.0, 'EUR'))   # 'value_usd' until 2026-10-24

    def test_every_answer_names_the_currency_of_its_prices(self):
        # card_scanner_mcp.py passes each tool's answer through with_currency: a number in euros must not read as dollars
        answer = tools.with_currency(self.api, tools.missing_cards(self.api, '7'))
        self.assertEqual(answer['prices_in'], 'EUR')
        self.assertIn('cost_of_the_missing', answer)
        # A server from before the price sources (no route): its prices are dollars
        self.assertEqual(tools.with_currency(FakeApi(currency=None), {'decks': []})['prices_in'], 'USD')
        # Any other failure of the question: unknown - euros never labelled dollars (a Codex review of 2026-10-24)
        self.assertEqual(tools.with_currency(FakeApi(currency='away'), {'decks': []})['prices_in'], 'unknown')
        self.assertEqual(tools.with_currency(FakeApi(currency=500), {'decks': []})['prices_in'], 'unknown')
        # An Arena answer has no money in it: no currency that would read as "this costs dollars"
        arena = tools.with_currency(self.api, tools.missing_cards(self.api, 'Elves on Arena'))
        self.assertIn('wildcards', arena['prices_in'])
        self.assertNotIn('price', tools.find_card(self.api, 'Llanowar Elves', 'arena')['card'])

    def test_what_can_i_build_starts_a_search_and_returns_what_it_has(self):
        api = self.api
        api.get_fresh = lambda path, **p: {'running': False, 'done': 2, 'total': 2, 'items': [
            {'name': 'A', 'percent': 90}, {'name': 'B', 'percent': 50}]}
        answer = tools.what_can_i_build(api, 'precons', wait_seconds=1, limit=1)
        self.assertEqual(api.posts, ['/api/decks/ideas/precons'])            # the one thing it starts (nothing in the data)
        self.assertEqual(len(answer['best']), 1)
        with self.assertRaises(ScannerError):
            tools.what_can_i_build(api, 'cards')



class ArenaDecks(unittest.TestCase):
    """An Arena deck is counted against the Arena collection and costs wildcards, not money"""

    def setUp(self):
        self.api = FakeApi()

    def test_an_arena_deck_says_rarity_and_wildcards_and_no_paper_price(self):
        deck = tools.get_deck(self.api, 'Elves on Arena')
        elves = next(c for c in deck['boards']['main'] if c['name'] == 'Llanowar Elves')
        self.assertEqual(elves['rarity'], 'common')
        self.assertNotIn('price', elves)
        self.assertNotIn('price', deck['totals'])
        self.assertEqual(deck['wildcards']['needed']['rare'], 1)
        self.assertIn('MTG Arena', deck['how_to_change'])               # it came from an Arena export
        # a paper deck keeps its prices
        self.assertIn('price', tools.get_deck(self.api, 'Giada')['boards']['main'][0])

    def test_missing_cards_of_an_arena_deck_are_wildcards(self):
        missing = tools.missing_cards(self.api, 'Elves on Arena')
        self.assertEqual(missing['to_craft'], [{'name': 'Bejeweled Warg', 'rarity': 'rare', 'copies': 1}])
        self.assertTrue(missing['can_craft_all_now'])                    # 1 rare needed, 1 had
        self.assertEqual(missing['cards_not_on_arena'], ['Paper Only Card'])
        self.assertNotIn('cost_of_the_missing', missing)

    def test_a_proposal_for_an_arena_deck_checks_four_copies_and_counts_wildcards(self):
        answer = tools.propose_deck_changes(self.api, 'Elves on Arena', add=[
            {'name': 'Llanowar Elves'}, {'name': 'Elvish Mystic', 'quantity': 4}, {'name': 'Bejeweled Warg', 'quantity': 1},
            {'name': 'Relentless Rats', 'quantity': 9}])
        by_name = {c['name']: c for c in answer['add']}
        self.assertIn('at most 4', by_name['Llanowar Elves']['problem'])      # 4 + 1
        self.assertNotIn('problem', by_name['Elvish Mystic'])                 # exactly 4
        self.assertNotIn('problem', by_name['Relentless Rats'])               # its text allows any number
        self.assertNotIn('price', by_name['Elvish Mystic'])
        # 2 Bejeweled Warg owned 0, 4 Elvish Mystic owned 3 (Relentless Rats: 'owned' 1 of 9)
        self.assertEqual(answer['wildcards_after']['needed'], {'common': 1, 'uncommon': 0, 'rare': 10, 'mythic': 0})
        self.assertFalse(answer['wildcards_after']['enough'])
        self.assertEqual(self.api.searches[0]['kind'], 'arena')               # owned = the Arena collection's
        self.assertEqual(self.api.posts, [])

    def test_the_other_arena_decks_using_a_card_come_from_the_arena_collection(self):
        answer = tools.propose_deck_changes(self.api, 'Elves on Arena', remove=[{'name': 'Forest'}],
                                            add=[{'name': 'Llanowar Elves'}])
        self.assertEqual(answer['add'][0]['also_used_by'], ['Other Arena deck'])

    def test_suggestions_say_why_there_are_none_for_a_deck_without_a_commander(self):
        answer = tools.suggest_for_deck(self.api, 'Elves on Arena')
        self.assertIn('search_cards', answer['message'])

    def test_the_arena_collection_is_searched_and_summed_up(self):
        found = tools.search_collection(self.api, which='arena')
        self.assertEqual([c['name'] for c in found['cards']], ['Sheoldred, the Apocalypse', 'Llanowar Elves'])   # by rarity
        self.assertNotIn('price', found['cards'][0])
        self.assertEqual(tools.find_card(self.api, 'llanowar elves', 'arena')['copies_owned'], 5)
        self.assertEqual(tools.find_card(self.api, 'llanowar elves')['copies_owned'], 0)   # none on paper
        summary = tools.collection_summary(self.api, which='arena')
        self.assertEqual((summary['cards'], summary['wildcards']['rare'], summary['gems']), (6, 1, 130))
        with self.assertRaises(ScannerError):
            tools.search_collection(self.api, which='mtgo')

    def test_search_cards_passes_the_filters_to_the_server(self):
        answer = tools.search_cards(self.api, 'arena', 'Standard', True, text='trample', identity='gw', rarity='Rare')
        asked = self.api.searches[-1]
        self.assertEqual((asked['kind'], asked['format'], asked['owned'], asked['identity'], asked['rarity'], asked['text']),
                         ('arena', 'standard', 1, 'GW', 'rare', 'trample'))
        self.assertEqual(answer['collection'], 'arena')
        self.assertTrue(all('price' not in c for c in answer['cards']))
        tools.search_cards(self.api, owned_only=False, name='bolt')
        self.assertEqual((self.api.searches[-1]['kind'], self.api.searches[-1]['owned']), ('', ''))   # the paper one, everything



class DeckWrites(unittest.TestCase):
    """The tools that change decks (--allow-deck-writes): deck routes only, checked whole before anything is sent"""

    def setUp(self):
        self.api = FakeApi()

    def test_only_the_deck_routes_can_be_written(self):
        for allowed in (('POST', '/api/decks'), ('POST', '/api/decks/7/cards'), ('PUT', '/api/decks/7')):
            self.assertTrue(deck_write_allowed(*allowed), allowed)
        for refused in (('DELETE', '/api/decks/7'), ('POST', '/api/inventory'), ('PUT', '/api/inventory/3'),
                        ('POST', '/api/decks/7/import'), ('POST', '/api/decks/7/duplicate'), ('POST', '/api/trades'),
                        ('POST', '/api/decks/7/cards/../../inventory'), ('GET', '/api/decks'), ('POST', '/api/decks?area=arena')):
            self.assertFalse(deck_write_allowed(*refused), refused)

    def test_the_client_refuses_another_route_before_the_network_and_forgets_its_cache(self):
        api = scanner_api.ScannerApi('http://scanner.invalid')
        sent = []
        api._request = lambda path, params=None, body=None, method=None: (sent.append((method, path)) or ('', {'success': True}))
        with self.assertRaises(ScannerError):
            api.write('DELETE', '/api/decks/7', {})
        with self.assertRaises(ScannerError):
            api.write('POST', '/api/inventory', {})
        self.assertEqual(sent, [])                                            # never reached the server
        api._cache['/api/decks/7?'] = (9e99, {'old': True})
        api.write('POST', '/api/decks/7/cards', {'cards': []})
        self.assertEqual((sent, api._cache), ([('POST', '/api/decks/7/cards')], {}))   # the next read is fresh

    def test_a_change_is_checked_whole_and_refused_with_its_problems(self):
        answer = tools.change_deck(self.api, 'Giada', add=[{'name': 'Swords to Plowshares'}, {'name': 'Lightning Bolt'}])
        self.assertFalse(answer['changed'])
        self.assertIn("outside the commander's colours", answer['add'][1]['problem'])
        self.assertEqual(self.api.writes, [])                                 # nothing sent, not even the good card

    def test_a_change_sends_the_card_datas_names_removals_first(self):
        answer = tools.change_deck(self.api, 'Giada', add=[{'name': 'swords to plowshares', 'board': 'side'}],
                                   remove=[{'name': 'Plains', 'quantity': 1}])
        self.assertTrue(answer['changed'])
        method, path, body = self.api.writes[0]
        self.assertEqual((method, path), ('POST', '/api/decks/7/cards'))
        self.assertEqual(body['cards'], [{'name': 'Plains', 'board': 'main', 'change': -1},
                                         {'name': 'Swords to Plowshares', 'board': 'side', 'change': 1,
                                          'card_id': 'Swords to Plowshares'}])
        self.assertEqual(answer['now']['name'], 'Calling All Angels')

    def test_allow_problems_sends_only_the_cards_that_were_found(self):
        tools.change_deck(self.api, 'Giada', add=[{'name': 'Swords to Plowshares'}, {'name': 'Not A Card'}], allow_problems=True)
        self.assertEqual([item['name'] for item in self.api.writes[0][2]['cards']], ['Swords to Plowshares'])

    def test_a_deck_from_an_arena_export_is_never_changed(self):
        for write in (lambda: tools.change_deck(self.api, 'Elves on Arena', add=[{'name': 'Llanowar Elves'}]),
                      lambda: tools.rename_deck(self.api, 'Elves on Arena', name='Mine')):
            with self.assertRaises(ScannerError) as raised:
                write()
            self.assertIn('next import replaces it', str(raised.exception))
        self.assertEqual(self.api.writes, [])

    def test_a_new_deck_is_one_list_with_its_sections(self):
        answer = tools.create_deck(self.api, 'Angels', 'commander', cards=[
            {'name': 'Swords to Plowshares'}, {'name': 'Plains', 'quantity': 30}, {'name': 'Plains', 'quantity': 5},
            {'name': 'Resplendent Angel', 'board': 'side'}], commander='giada')
        self.assertTrue(answer['created'])
        method, path, body = self.api.writes[0]
        self.assertEqual((method, path, body['kind'], body['format']), ('POST', '/api/decks', 'paper', 'commander'))
        self.assertEqual(body['text'], 'Commander\n1 Giada, Font of Hope\n\nDeck\n1 Swords to Plowshares\n35 Plains'
                                       '\n\nSideboard\n1 Resplendent Angel')

    def test_a_new_deck_is_checked_first(self):
        refused = tools.create_deck(self.api, 'Angels', 'commander', cards=[{'name': 'Lightning Bolt'}], commander='Giada')
        self.assertFalse(refused['created'])
        too_many = tools.create_deck(self.api, 'Elves', 'standard', kind='arena', cards=[{'name': 'Llanowar Elves', 'quantity': 5}])
        self.assertIn('at most 4', too_many['add'][0]['problem'])
        self.assertEqual(too_many['wildcards_after']['have']['rare'], 1)       # the Arena account's, for an Arena deck
        self.assertEqual(self.api.writes, [])
        for bad in (dict(name='', deck_format='standard'), dict(name='X', deck_format=''),
                    dict(name='X', deck_format='standard', kind='mtgo')):
            with self.assertRaises(ScannerError):
                tools.create_deck(self.api, **bad)
        with self.assertRaises(ScannerError):
            tools.create_deck(self.api, 'X', 'commander', commander='Nobody At All')
        with self.assertRaises(ScannerError) as raised:                        # Standard has no commander
            tools.create_deck(self.api, 'X', 'standard', commander='Giada')
        self.assertIn('has no commander', str(raised.exception))
        self.assertEqual(self.api.writes, [])

    def test_a_standard_deck_needs_no_commander(self):
        answer = tools.create_deck(self.api, 'Elves', 'standard', kind='arena', cards=[
            {'name': 'Llanowar Elves', 'quantity': 4}, {'name': 'Plains', 'quantity': 20}])
        self.assertTrue(answer['created'])
        self.assertEqual(self.api.writes[0][2]['text'], 'Deck\n4 Llanowar Elves\n20 Plains')   # no Commander section

    def test_the_currency_label_of_a_write_follows_the_deck_it_reports(self):
        # create_deck's answer says the deck's kind only in 'now': an Arena deck was labelled 'USD' (2026-10-29)
        arena = tools.with_currency(self.api, tools.create_deck(self.api, 'Elves', 'standard', kind='arena',
                                                                cards=[{'name': 'Llanowar Elves', 'quantity': 4}]))
        self.assertIn('wildcards', arena['prices_in'])
        paper = tools.with_currency(self.api, tools.change_deck(self.api, 'Giada', add=[{'name': 'Swords to Plowshares'}]))
        self.assertEqual(paper['prices_in'], 'EUR')                            # a paper deck keeps its currency

    def test_rename(self):
        answer = tools.rename_deck(self.api, 'Giada', name='Angels Forever', notes='')
        self.assertEqual(self.api.writes, [('PUT', '/api/decks/7', {'name': 'Angels Forever', 'notes': ''})])
        self.assertEqual((answer['was'], answer['now']['name']), ('Calling All Angels', 'Angels Forever'))
        with self.assertRaises(ScannerError):
            tools.rename_deck(self.api, 'Giada')

    def test_the_read_only_tools_never_write(self):
        tools.propose_deck_changes(self.api, 'Giada', add=[{'name': 'Swords to Plowshares'}])
        tools.get_deck(self.api, 'Giada')
        tools.missing_cards(self.api, 'Elves on Arena')
        tools.search_cards(self.api, 'arena', 'standard')
        self.assertEqual((self.api.writes, self.api.posts), ([], []))


if __name__ == '__main__':
    unittest.main()
