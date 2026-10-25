"""
Other people's collections (core/others.py, server/others_api.py): imported under a name to
browse, put into a trade as what it receives, or on the buy list - all apart from the user's
collection. Temporary databases and folders only; no application data is touched.

    venv/bin/python -m unittest discover tests -p test_others.py
"""
import csv
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/
sys.path.insert(0, str(Path(__file__).resolve().parent))  # test_ownership: the temporary data

from card_scanner.core.others import Others, OthersError, card_key  # noqa: E402
from card_scanner.core.trades import TradeError, Trades  # noqa: E402
from card_scanner.games.mtg import Magic  # noqa: E402
from test_import_replace import CARDS, FakeDatabase  # noqa: E402
from test_ownership import CARD, GAME, TemporaryData  # noqa: E402


def row(name, number='1', quantity=1, price=1.0, finish='regular', condition='Near Mint', card_id=None, set_name='Test Set'):
    return {'card_id': card_id, 'card_name': name, 'set_name': set_name, 'set_code': 'tst', 'card_number': number,
            'rarity': 'rare', 'type_line': 'Creature - Elf', 'mana_cost': '{G}', 'colors': 'G', 'color_identity': 'G',
            'price_usd': price, 'quantity': quantity, 'condition': condition, 'finish': finish}


class OthersData(TemporaryData):
    def setUp(self):
        super().setUp()
        self.others = Others(db_file=self.tmp / 'others.db')
        self.trades = Trades(self.collection)

    def tearDown(self):
        self.others.conn.close()
        super().tearDown()

    def sam(self, rows=None, name='Sam'):
        return self.others.import_collection(GAME, name, rows or [
            row('Llanowar Elves', '1', 4, 0.5), row('Forest', '2', 20, 0.1), row('Rare Thing', '3', 1, 30.0),
            row('Rare Thing', '3', 1, 45.0, finish='foil')])['id']


class Importing(OthersData):
    def test_the_collection_is_kept_with_its_numbers(self):
        collection = self.sam()
        listed = self.others.collections(GAME)
        self.assertEqual([(c['name'], c['entries'], c['cards'], c['value']) for c in listed], [('Sam', 4, 26, 79.0)])
        self.assertEqual(collection, listed[0]['id'])

    def test_rows_of_the_same_card_are_added_up(self):
        collection = self.sam([row('Forest', '2', 3), row('Forest', '2', 2), row('Forest', '2', 1, finish='foil')])
        cards, total = self.others.cards(collection)
        self.assertEqual((total, sorted(card['quantity'] for card in cards)), (2, [1, 5]))

    def test_a_name_in_use_is_refused_unless_replacing(self):
        self.sam()
        with self.assertRaises(OthersError):
            self.sam(name=' sam ')
        again = self.others.import_collection(GAME, 'SAM', [row('Only Card', '9', 2)], replace=True)
        self.assertTrue(again['replaced'])
        self.assertEqual([c['cards'] for c in self.others.collections(GAME)], [2])

    def test_nothing_to_keep_or_no_name_is_refused(self):
        with self.assertRaises(OthersError):
            self.others.import_collection(GAME, 'Sam', [])
        with self.assertRaises(OthersError):
            self.others.import_collection(GAME, '  ', [row('A')])

    def test_the_users_collection_is_not_touched(self):
        self.collection.add_card(CARD, GAME, 'regular', quiet=True)
        self.sam()
        self.assertEqual(self.collection.get_stats(GAME)['total_cards'], 1)


class Browsing(OthersData):
    def test_search_sort_and_pages(self):
        collection = self.sam()
        cards, total = self.others.cards(collection, 'elf')
        self.assertEqual((total, {card['name'] for card in cards}), (4, {'Llanowar Elves', 'Forest', 'Rare Thing'}))  # type line
        cards, total = self.others.cards(collection, 'rare thing', sort='price_each')
        self.assertEqual([card['finish'] for card in cards], ['foil', 'regular'])
        cards, total = self.others.cards(collection, '', limit=2, offset=2)
        self.assertEqual((len(cards), total), (2, 4))

    def test_a_card_of_another_collection_is_not_found_by_its_row(self):
        sam, ana = self.sam(), self.sam([row('Mountain', '5', 2)], name='Ana')
        row_of_ana = self.others.cards(ana)[0][0]['row']
        with self.assertRaises(OthersError):
            self.others.card(sam, row_of_ana)

    def test_deleting_a_collection_removes_its_cards_only(self):
        sam, ana = self.sam(), self.sam([row('Mountain', '5', 2)], name='Ana')
        self.others.delete(sam)
        self.assertEqual([c['name'] for c in self.others.collections(GAME)], ['Ana'])
        self.assertEqual(self.others.conn.execute('SELECT COUNT(*) FROM other_cards').fetchone()[0], 1)


class Receiving(OthersData):
    def receive(self, name='Jo', collection=None, quantity=2, index=0):
        collection = collection or self.sam()
        card = self.others.cards(collection)[0][index]
        key = card_key({'card_id': card['card_id'], 'card_name': card['name'], 'set_name': card['set_name'],
                        'card_number': card['number'], 'condition': card['condition'], 'finish': card['finish']})
        return self.trades.receive(GAME, name, 'Sam', [{'card': card, 'key': key, 'quantity': quantity, 'most': card['quantity']}])

    def test_a_trade_can_receive_without_giving(self):
        result = self.receive()
        trade = self.trades.list(GAME)[0]
        self.assertEqual((result['cards'], trade['quantity'], trade['receive_quantity']), (2, 0, 2))
        self.assertEqual(trade['receives'][0]['seller'], 'Sam')
        self.assertEqual(self.collection.get_stats(GAME)['total_cards'], 0)   # nothing of the collection moved

    def test_the_same_name_is_the_same_trade_and_the_copies_add_up_to_what_the_seller_has(self):
        collection = self.sam()
        first = self.receive(collection=collection, quantity=3)
        again = self.receive(collection=collection, quantity=3)     # Sam has 4 of the first card (sorted by name: Forest 20)
        trade = self.trades.list(GAME)
        self.assertEqual(len(trade), 1)
        card = trade[0]['receives'][0]
        self.assertEqual(card['quantity'], min(6, card['most']))
        # What each answer says went in is what did: the second one is cut at what Sam has (plan, part 2)
        self.assertEqual(first['cards'] + again['cards'], card['quantity'])

    def test_the_number_is_changed_and_cut_at_what_the_seller_has(self):
        self.receive(quantity=1)
        trade = self.trades.list(GAME)[0]
        received = trade['receives'][0]
        self.trades.set_receive_quantity(trade['id'], received['row'], received['most'])
        with self.assertRaises(TradeError):
            self.trades.set_receive_quantity(trade['id'], received['row'], received['most'] + 1)
        self.trades.set_receive_quantity(trade['id'], received['row'], 0)
        self.assertEqual(self.trades.list(GAME)[0]['receives'], [])

    def test_cancelling_removes_what_it_receives_and_confirming_keeps_it_as_history(self):
        collection = self.sam()
        self.receive('Jo', collection)
        self.receive('Ana', collection, index=1)
        jo, ana = (next(t for t in self.trades.list(GAME) if t['name'] == name) for name in ('Jo', 'Ana'))
        self.trades.cancel(jo['id'])
        self.assertEqual(self.others.conn.execute('SELECT 1').fetchone()[0], 1)
        self.trades.confirm(ana['id'])
        done = next(t for t in self.trades.list(GAME) if t['status'] == 'done')
        self.assertEqual(done['receive_quantity'], 2)
        self.assertEqual(self.collection.get_stats(GAME)['total_cards'], 0)       # confirming adds nothing to the collection
        self.assertEqual(self.trades.conn.execute('SELECT COUNT(*) FROM trade_receives').fetchone()[0], 1)

    def test_deleting_the_imported_collection_leaves_the_trade_whole(self):
        collection = self.sam()
        self.receive(collection=collection)
        self.others.delete(collection)
        trade = self.trades.list(GAME)[0]
        self.assertEqual((trade['receive_quantity'], trade['receives'][0]['name']), (2, 'Forest'))


class BuyList(OthersData):
    def test_cards_are_put_on_the_list_with_a_cap_and_merged(self):
        collection = self.sam()
        elves = next(card for card in self.others.cards(collection)[0] if card['name'] == 'Llanowar Elves')
        self.others.buy(collection, [(elves['row'], 3)])
        self.others.buy(collection, [(elves['row'], 3)])       # 4 is all Sam has
        [card] = self.others.buy_list(GAME)
        self.assertEqual((card['name'], card['quantity'], card['most'], card['seller']), ('Llanowar Elves', 4, 4, 'Sam'))
        self.others.set_buy_quantity(card['row'], 2)
        with self.assertRaises(OthersError):
            self.others.set_buy_quantity(card['row'], 5)
        self.others.set_buy_quantity(card['row'], 0)
        self.assertEqual(self.others.buy_list(GAME), [])

    def test_the_list_outlives_the_imported_collection_and_is_cleared_by_seller(self):
        sam = self.sam()
        ana = self.sam([row('Mountain', '5', 2)], name='Ana')
        self.others.buy(sam, [(self.others.cards(sam)[0][0]['row'], 1)])
        self.others.buy(ana, [(self.others.cards(ana)[0][0]['row'], 1)])
        self.others.delete(sam)
        self.assertEqual([card['seller'] for card in self.others.buy_list(GAME)], ['Ana', 'Sam'])
        self.assertEqual(self.others.clear_buy_list(GAME, 'sam'), 1)
        self.assertEqual([card['seller'] for card in self.others.buy_list(GAME)], ['Ana'])


class InTheFullBackup(OthersData):
    def test_the_full_backup_holds_the_file_and_the_collection_backups_do_not(self):
        import threading
        from card_scanner.core import backups
        self.sam()
        saved = backups.FULL_DIR
        backups.FULL_DIR = self.tmp / 'full'
        try:
            made = backups.create_full(managers=[self.collection, self.others], barrier=threading.RLock(),
                                       databases=[self.tmp / 'collection.db', self.tmp / 'others.db'], linked=[], copied=[])
        finally:
            backups.FULL_DIR = saved
        self.assertTrue(made['verified'], made)
        self.assertTrue(list((self.tmp / 'full').rglob('others.db')))
        self.assertNotIn('other_cards', backups.INVENTORY_TABLES + backups.DECK_TABLES + backups.ARENA_TABLES)


class ManaBoxRows(unittest.TestCase):
    """Game.import_rows reads a ManaBox export, as it reads Moxfield's"""

    def setUp(self):
        self.game = Magic.__new__(Magic)
        self.game.db = FakeDatabase()
        self.game.db.get_card_by_id = lambda card_id: {'id': card_id, 'name': 'Mountain', 'treatments': []} if card_id == 'abc' else None
        self.game.inventory_fields = lambda card, finish: dict(card)

    def parse(self, *rows):
        header = 'Name,Set code,Set name,Collector number,Foil,Rarity,Quantity,ManaBox ID,Scryfall ID,Purchase price,Misprint,Altered,Condition,Language,Purchase price currency'
        reader = csv.DictReader(io.StringIO('\n'.join([header, *rows]) + '\n'))
        columns = {name.strip().lower() for name in reader.fieldnames}
        return self.game.import_rows(columns, ({k.strip().lower(): v for k, v in r.items()} for r in reader))

    def test_the_card_by_its_scryfall_id_with_its_finish_condition_and_count(self):
        result = self.parse('Mountain,TST,Test,99,foil,common,3,1,abc,0.10,false,false,lightly_played,en,USD')
        entry = result['entries'][0]
        self.assertEqual((entry['fields']['name'], entry['finish'], entry['quantity'], entry['condition'], result['format']),
                         ('Mountain', 'foil', 3, 'Excellent', 'ManaBox'))

    def test_normal_is_not_foil_and_an_id_of_another_card_is_not_trusted(self):
        result = self.parse('Forest,tst,Test,1,normal,common,1,1,abc,0,false,false,near_mint,en,USD')
        entry = result['entries'][0]
        # the id belongs to Mountain, the row says Forest, and tst 1 is another card: found by name
        self.assertEqual((entry['finish'], entry['fields']['name'], result['by_name']), ('regular', 'Forest', 1))

    def test_moxfield_columns_are_still_moxfield(self):
        reader = csv.DictReader(io.StringIO('Count,Name,Edition,Condition,Foil,Collector Number,Tags\n1,Mountain,tst,Near Mint,,2,\n'))
        columns = {name.lower() for name in reader.fieldnames}
        self.assertEqual(self.game.import_rows(columns, ({k.lower(): v for k, v in r.items()} for r in reader))['format'], 'Moxfield')


class OthersRoutes(OthersData):
    """The routes, through Flask's test client, with temporary data behind the server"""

    def setUp(self):
        super().setUp()
        import app as launcher
        from card_scanner.server import others_api, state
        self.state = state
        self.saved_services = {name: getattr(state.services, name) for name in ('inventory', 'others', 'trade_store')}
        state.services.inventory, state.services.others, state.services.trade_store = self.collection, self.others, self.trades
        game = Magic.__new__(Magic)
        game.db = FakeDatabase()
        game.inventory_fields = lambda card, finish: dict(card, price=1.5)
        patch = mock.patch.object(others_api.games, 'active', return_value=game)
        patch.start()
        self.addCleanup(patch.stop)
        self.client = launcher.app.test_client()

    def tearDown(self):
        for name, value in self.saved_services.items():
            setattr(self.state.services, name, value)
        super().tearDown()

    def upload(self, text, name='Sam', filename='sam.csv', **form):
        return self.client.post('/api/others/import', data={'file': (io.BytesIO(text.encode()), filename), 'name': name, **form},
                                content_type='multipart/form-data')

    MOXFIELD = 'Count,Name,Edition,Condition,Foil,Collector Number,Tags\n3,Mountain,tst,Near Mint,,2,\n1,Forest,tst,Near Mint,foil,3,\n'
    OWN = ('Card Name,Set,Set Code,Card Number,Finish,Quantity,Condition,Price (USD)\n'
           'Elf,Test Set,tst,7,regular,2,Near Mint,$0.50\nElf,Test Set,tst,7,foil,1,Near Mint,$2.00\n')

    def test_both_formats_are_read_and_a_wrong_file_is_refused(self):
        answer = self.upload(self.MOXFIELD)
        self.assertEqual((answer.status_code, answer.get_json()['format'], answer.get_json()['cards']), (200, 'Moxfield', 4))
        answer = self.upload(self.OWN, name='Ana')
        self.assertEqual((answer.status_code, answer.get_json()['format'], answer.get_json()['entries']), (200, 'Card Scanner', 2))
        self.assertEqual(self.upload('a,b\n1,2\n', name='Bob').status_code, 400)
        self.assertEqual(self.upload(self.MOXFIELD, filename='sam.txt', name='Bob').status_code, 400)
        self.assertEqual(self.upload(self.MOXFIELD).status_code, 400)                      # Sam again: refused
        self.assertEqual(self.upload(self.MOXFIELD, replace='true').status_code, 200)    # ... unless replacing
        self.assertEqual(self.collection.get_stats(GAME)['total_cards'], 0)                # the collection never changed

    def test_browse_receive_and_buy_through_the_routes(self):
        self.upload(self.MOXFIELD)
        collection = self.client.get('/api/others').get_json()['collections'][0]
        page = self.client.get(f"/api/others/{collection['id']}/cards?q=mountain").get_json()
        self.assertEqual((page['total'], page['cards'][0]['quantity']), (1, 3))
        row = page['cards'][0]['row']
        answer = self.client.post(f"/api/others/{collection['id']}/receive", json={'name': 'Jo', 'items': [{'row': row, 'quantity': 9}]})
        self.assertEqual((answer.status_code, answer.get_json()['seller']), (200, 'Sam'))
        trade = self.client.get('/api/trades').get_json()['trades'][0]
        self.assertEqual((trade['receive_quantity'], trade['quantity']), (3, 0))           # cut at the 3 Sam has
        self.assertEqual(self.client.post(f"/api/others/{collection['id']}/buy", json={'items': [{'row': row, 'quantity': 2}]}).status_code, 200)
        listed = self.client.get('/api/buy_list').get_json()['cards']
        self.assertEqual([(card['name'], card['quantity'], card['seller']) for card in listed], [('Mountain', 2, 'Sam')])
        export = self.client.get('/api/buy_list/export/csv')
        self.assertIn('Mountain', export.get_data(as_text=True))
        self.assertEqual(self.client.put(f"/api/buy_list/{listed[0]['row']}", json={'quantity': 0}).status_code, 200)
        self.assertEqual(self.client.get('/api/buy_list').get_json()['cards'], [])

    def test_a_replacement_with_rows_left_out_changes_nothing(self):
        self.upload(self.OWN)
        before = self.client.get('/api/others').get_json()['collections'][0]
        bad = self.OWN + 'Odd,Test Set,tst,8,sparkly,1,Near Mint,$1\n'            # a finish the game does not have
        answer = self.upload(bad, replace='true')
        self.assertEqual(answer.status_code, 400)
        self.assertIn('not replaced', answer.get_json()['error'])
        self.assertEqual(self.client.get('/api/others').get_json()['collections'][0], before)
        unnamed = self.MOXFIELD + '1,,tst,Near Mint,,2,\n'                        # a row with no name
        self.upload(self.MOXFIELD, name='Bob')
        self.assertEqual(self.upload(unnamed, name='Bob', replace='true').status_code, 400)
        # added as a new collection the same file is fine: the left-out rows are only counted
        answer = self.upload(unnamed, name='Cy')
        self.assertEqual((answer.status_code, answer.get_json()['not_found_count']), (200, 1))

    def test_a_trade_refuses_cards_of_another_game(self):
        collection = self.sam()
        card = self.others.cards(collection)[0][0]
        with self.assertRaises(TradeError):
            self.trades.receive('other-game', 'Jo', 'Sam', [{'card': card, 'key': 'k', 'quantity': 1, 'most': 1}])
        self.assertEqual(self.trades.list('other-game'), [])                        # not even the trade was made
        other = self.others.import_collection('other-game', 'Zed', [row('X')])['id']
        answer = self.client.post(f'/api/others/{other}/receive', json={'name': 'Jo', 'items': [{'row': 1, 'quantity': 1}]})
        self.assertEqual(answer.status_code, 400)

    def test_a_row_of_another_collection_or_none_is_refused(self):
        self.upload(self.MOXFIELD)
        self.upload(self.OWN, name='Ana')
        sam, ana = self.client.get('/api/others').get_json()['collections']
        ana_row = self.client.get(f"/api/others/{ana['id']}/cards").get_json()['cards'][0]['row']
        for path in ('receive', 'buy'):
            self.assertEqual(self.client.post(f"/api/others/{sam['id']}/{path}", json={'name': 'Jo', 'items': [{'row': ana_row}]}).status_code, 400)
            self.assertEqual(self.client.post(f"/api/others/{sam['id']}/{path}", json={'name': 'Jo', 'items': []}).status_code, 400)

    def test_deleting_a_collection(self):
        self.upload(self.MOXFIELD)
        collection = self.client.get('/api/others').get_json()['collections'][0]
        self.assertEqual(self.client.delete(f"/api/others/{collection['id']}").status_code, 200)
        self.assertEqual(self.client.get('/api/others').get_json()['collections'], [])


if __name__ == '__main__':
    unittest.main()
