"""
Importing a collection file (server/inventory_api.py: import_inventory, games/mtg.py: import_rows),
from FIX.md's review of 2026-10-15:

  - a Moxfield file that REPLACES the collection is refused, whole, when any of its rows could
    not be matched (or has no name): the collection is left as it was;
  - a row whose set and collector number belong to another card than the name says is not taken
    on the numbers alone;
  - two uploads of the app's own CSV in the same second each read their own file.

Temporary databases, a fake card database and Flask's test client - nothing of a real server.

    venv/bin/python -m unittest discover tests -p test_import_replace.py
"""
import io
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/
sys.path.insert(0, str(Path(__file__).resolve().parent))  # test_ownership: the temporary data

from card_scanner.config import Config  # noqa: E402
from card_scanner.games.mtg import Magic  # noqa: E402
from test_ownership import CARD, GAME, TemporaryData  # noqa: E402

CARDS = {
    ('tst', '1'): {'name': 'Test Card', 'set_name': 'Test Set', 'set_code': 'tst', 'number': '1', 'card_id': 'a'},
    ('tst', '2'): {'name': 'Mountain', 'set_name': 'Test Set', 'set_code': 'tst', 'number': '2', 'card_id': 'b'},
    ('tst', '3'): {'name': 'Forest', 'set_name': 'Test Set', 'set_code': 'tst', 'number': '3', 'card_id': 'c'},
    ('tst', '4'): {'name': 'Front // Back', 'set_name': 'Test Set', 'set_code': 'tst', 'number': '4', 'card_id': 'd'},
}
BY_NAME = {card['name'].lower(): card for card in CARDS.values()}


class FakeDatabase:
    """Just what Magic.import_rows asks of the card database"""

    def get_card_by_set_number(self, edition, number, exact=False):
        return CARDS.get((edition.lower(), number.lstrip('0') if not exact else number))

    def search_card(self, name, fuzzy=True):
        return BY_NAME.get(name.lower())


def moxfield(*rows):
    lines = ['Count,Name,Edition,Condition,Foil,Collector Number,Tags']
    lines += [','.join(row) for row in rows]
    return '\n'.join(lines) + '\n'


class MoxfieldRows(unittest.TestCase):
    """Game.import_rows: which card a row is"""

    def setUp(self):
        self.game = Magic.__new__(Magic)
        self.game.db = FakeDatabase()
        self.game.inventory_fields = lambda card, finish: dict(card)

    def parse(self, *rows):
        import csv
        reader = csv.DictReader(io.StringIO(moxfield(*rows)))
        columns = {name.strip().lower() for name in reader.fieldnames}
        return self.game.import_rows(columns, ({k.strip().lower(): v for k, v in row.items()} for row in reader))

    def test_a_row_whose_name_disagrees_with_its_printing_is_not_taken_on_the_numbers(self):
        result = self.parse(('1', 'Forest', 'tst', 'Near Mint', '', '2', ''))   # tst 2 is Mountain
        self.assertEqual([e['fields']['name'] for e in result['entries']], ['Forest'])   # by name
        self.assertEqual((result['by_name'], result['not_found']), (1, []))

    def test_if_the_name_cannot_be_found_either_the_row_is_unresolved(self):
        result = self.parse(('1', 'Plains', 'tst', 'Near Mint', '', '2', ''))
        self.assertEqual(result['entries'], [])
        self.assertEqual(result['not_found'], ['Plains'])

    def test_matching_printings_and_double_faced_front_names_still_work(self):
        result = self.parse(('2', 'Mountain', 'tst', 'Near Mint', '', '2', ''),
                            ('1', 'Front', 'tst', 'Near Mint', '', '4', ''))
        self.assertEqual([e['fields']['name'] for e in result['entries']], ['Mountain', 'Front // Back'])
        self.assertEqual((result['by_name'], result['not_found']), (0, []))

    def test_a_row_without_a_name_is_counted_but_an_empty_one_is_not(self):
        result = self.parse(('1', '', 'tst', 'Near Mint', '', '1', ''), (',' * 6,))
        self.assertEqual((result['unnamed'], result['entries']), (1, []))


class ImportRoute(TemporaryData):
    """POST /api/import_inventory, with temporary data behind the server"""

    def setUp(self):
        super().setUp()
        import app as launcher
        from card_scanner.server import inventory_api, state
        self.state, self.api = state, inventory_api
        self.saved_services = {name: getattr(state.services, name) for name in ('inventory', 'scan_inventory')}
        state.services.inventory, state.services.scan_inventory = self.collection, self.scan
        self.saved_data_dir = Config.DATA_DIR
        Config.DATA_DIR = self.tmp / 'data'
        game = Magic.__new__(Magic)
        game.db = FakeDatabase()
        game.inventory_fields = lambda card, finish: dict(card, price=0)
        patch = mock.patch.object(inventory_api.games, 'active', return_value=game)
        patch.start()
        self.addCleanup(patch.stop)
        self.client = launcher.app.test_client()
        self.collection.add_card(CARD, GAME, 'regular', quiet=True)
        self.before = self.cards_of(self.collection)

    def tearDown(self):
        Config.DATA_DIR = self.saved_data_dir
        for name, value in self.saved_services.items():
            setattr(self.state.services, name, value)
        super().tearDown()

    @staticmethod
    def cards_of(manager):
        return {(c['name'], c['finish']): c['quantity'] for c in manager.get_all_cards(GAME)}

    def post(self, text, replace, name='cards.csv'):
        return self.client.post('/api/import_inventory', data={
            'file': (io.BytesIO(text.encode()), name), 'replace_existing': 'true' if replace else 'false'},
            content_type='multipart/form-data')

    def test_replacing_with_an_unresolved_row_changes_nothing(self):
        answer = self.post(moxfield(('1', 'Mountain', 'tst', 'Near Mint', '', '2', ''),
                                    ('1', 'No Such Card', 'tst', 'Near Mint', '', '99', '')), replace=True)
        self.assertEqual(answer.status_code, 400)
        self.assertIn('No Such Card', answer.get_json()['error'])
        self.assertEqual(self.cards_of(self.collection), self.before)

    def test_replacing_with_a_row_without_a_name_changes_nothing(self):
        answer = self.post(moxfield(('1', 'Mountain', 'tst', 'Near Mint', '', '2', ''),
                                    ('1', '', 'tst', 'Near Mint', '', '3', '')), replace=True)
        self.assertEqual(answer.status_code, 400)
        self.assertEqual(self.cards_of(self.collection), self.before)

    def test_adding_still_adds_the_known_cards_and_reports_the_rest(self):
        answer = self.post(moxfield(('2', 'Mountain', 'tst', 'Near Mint', '', '2', ''),
                                    ('1', 'No Such Card', 'tst', 'Near Mint', '', '99', '')), replace=False)
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.get_json()['stats']['not_found'], ['No Such Card'])
        self.assertEqual(self.cards_of(self.collection), {**self.before, ('Mountain', 'regular'): 2})

    def test_a_fully_matched_replacement_replaces(self):
        answer = self.post(moxfield(('3', 'Mountain', 'tst', 'Near Mint', '', '2', '')), replace=True)
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(self.cards_of(self.collection), {('Mountain', 'regular'): 3})


class UploadFiles(ImportRoute):
    """The app's own CSV: every request has a temporary file of its own"""

    def csv(self, name):
        return ('Card Name,Set,Set Code,Card Number,Quantity,Finish,Condition\n'
                f'{name},Test Set,tst,1,1,regular,Near Mint\n')

    def test_two_uploads_in_the_same_second_read_their_own_files(self):
        import datetime as real_datetime
        frozen = real_datetime.datetime(2026, 10, 15, 9, 30, 0)
        read, lock = [], threading.Lock()
        original = self.collection.import_csv

        def spy(path, *args, **kwargs):
            with lock:
                read.append(Path(path).read_text().splitlines()[1].split(',')[0])
            return original(path, *args, **kwargs)

        with mock.patch.object(self.api, 'datetime') as clock, \
                mock.patch.object(self.collection, 'import_csv', side_effect=spy):
            clock.now.return_value = frozen
            answers = []
            threads = [threading.Thread(target=lambda n=n: answers.append(self.post(self.csv(n), replace=False)))
                       for n in ('First Card', 'Second Card')]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(sorted(read), ['First Card', 'Second Card'])
        self.assertEqual([a.status_code for a in answers], [200, 200])
        self.assertEqual(list((Config.DATA_DIR / 'uploads').glob('*.csv')), [])   # all removed

    def test_the_temporary_file_is_removed_when_the_import_fails(self):
        with mock.patch.object(self.collection, 'import_csv', side_effect=RuntimeError('boom')):
            answer = self.post(self.csv('Test Card'), replace=False)
        self.assertEqual(answer.status_code, 500)
        self.assertEqual(list((Config.DATA_DIR / 'uploads').glob('*.csv')), [])


if __name__ == '__main__':
    unittest.main()
