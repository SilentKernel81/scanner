#!/usr/bin/env python3
"""
A deck's cover: the card whose picture stands for the deck.

Run:  venv/bin/python tests/test_deck_cover.py        (or with the others: -m unittest discover tests)

Two things are tested, both on temporary files:
  - the decks table of a file from before covers gets its new column without losing or
    changing a deck (core/decks.py: COVER_MIGRATION), and a cover can then be set, cleared
    and is copied with the deck;
  - which card is shown when (server/decks_api.py: deck_covers): the one chosen, else the
    commanders, else the most valuable card that is not a land.
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

from card_scanner.core import storage  # noqa: E402
from card_scanner.core.decks import COVER_MIGRATION, DeckManager  # noqa: E402

# The decks table as it was created until 2026-10-13: no cover
OLD_DECKS = '''
    CREATE TABLE decks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game TEXT NOT NULL,
        name TEXT NOT NULL,
        format TEXT NOT NULL,
        notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )'''


class CoverColumn(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.file = Path(self.folder.name) / 'cards.db'

    def old_file(self):
        """A database as an installation from before covers has it: two decks, one with cards"""
        conn = sqlite3.connect(self.file)
        conn.execute(OLD_DECKS)
        conn.execute("INSERT INTO decks VALUES (3, 'mtg', 'Elves', 'commander', 'a note', '2026-10-01 10:00:00', '2026-10-02 11:00:00')")
        conn.execute("INSERT INTO decks VALUES (7, 'mtg', 'Lifegain', 'standard', '', '2026-10-03 10:00:00', '2026-10-03 10:00:00')")
        conn.commit()
        conn.close()

    def test_an_old_file_gets_the_column_and_keeps_its_decks(self):
        self.old_file()
        decks = DeckManager(self.file)
        self.addCleanup(decks.conn.close)
        columns = [row[1] for row in decks.conn.execute('PRAGMA table_info(decks)')]
        self.assertEqual(columns[-1], 'cover')
        rows = [tuple(row) for row in decks.conn.execute('SELECT * FROM decks ORDER BY id')]
        self.assertEqual(rows, [
            (3, 'mtg', 'Elves', 'commander', 'a note', '2026-10-01 10:00:00', '2026-10-02 11:00:00', ''),
            (7, 'mtg', 'Lifegain', 'standard', '', '2026-10-03 10:00:00', '2026-10-03 10:00:00', '')])
        self.assertTrue(storage.applied(decks.conn, COVER_MIGRATION))
        # ids go on from where they were: a new deck is not given a number a deleted one had
        self.assertEqual(decks.create('mtg', 'New', 'commander'), 8)
        # the file was copied before it was changed
        self.assertTrue(list((self.file.parent / storage.MIGRATION_BACKUPS).glob('*')), "no backup of the file was made")

    def test_opening_the_file_again_changes_nothing(self):
        self.old_file()
        first = DeckManager(self.file)
        first.update(3, cover='Llanowar Elves')
        first.conn.close()
        again = DeckManager(self.file)
        self.addCleanup(again.conn.close)
        self.assertEqual(again.get(3)['cover'], 'Llanowar Elves')   # not rebuilt a second time: the cover is still there

    def test_a_new_file_has_the_column_from_the_start(self):
        decks = DeckManager(self.file)
        self.addCleanup(decks.conn.close)
        self.assertTrue(storage.applied(decks.conn, COVER_MIGRATION))
        deck_id = decks.create('mtg', 'Elves', 'commander')
        self.assertEqual(decks.get(deck_id)['cover'], '')

    def test_setting_clearing_and_copying_a_cover(self):
        decks = DeckManager(self.file)
        self.addCleanup(decks.conn.close)
        deck_id = decks.create('mtg', 'Elves', 'commander')
        decks.add_card(deck_id, 'Llanowar Elves', 'main')
        decks.update(deck_id, cover='Llanowar Elves')
        self.assertEqual(decks.get(deck_id)['cover'], 'Llanowar Elves')
        self.assertEqual(decks.list_decks('mtg')[0]['cover'], 'Llanowar Elves')
        # a change of something else leaves the cover alone
        decks.update(deck_id, name='Elves!')
        self.assertEqual(decks.get(deck_id)['cover'], 'Llanowar Elves')
        self.assertEqual(decks.get(decks.duplicate(deck_id))['cover'], 'Llanowar Elves')
        decks.update(deck_id, cover='')
        self.assertEqual(decks.get(deck_id)['cover'], '')


class WhichCardIsShown(unittest.TestCase):

    def covers(self, cover, cards):
        from card_scanner.server.decks_api import deck_covers
        return [card['name'] for card in deck_covers({'cover': cover}, cards)]

    @staticmethod
    def card(name, board='main', price=1.0, type_line='Creature', image='x'):
        return {'name': name, 'board': board, 'price': price, 'type_line': type_line, 'image_uri': image}

    def test_the_card_chosen_comes_before_everything(self):
        cards = [self.card('Jace', 'commander'), self.card('Sol Ring', price=2.0)]
        self.assertEqual(self.covers('Sol Ring', cards), ['Sol Ring'])

    def test_a_chosen_card_that_left_the_deck_does_not_count(self):
        cards = [self.card('Jace', 'commander'), self.card('Sol Ring')]
        self.assertEqual(self.covers('Black Lotus', cards), ['Jace'])

    def test_two_commanders_are_both_given_first_added_first(self):
        cards = [self.card('Frodo', 'commander'), self.card('Sam', 'commander'), self.card('Sol Ring', price=99)]
        self.assertEqual(self.covers('', cards), ['Frodo', 'Sam'])

    def test_without_a_commander_the_most_valuable_card_that_is_not_a_land(self):
        cards = [self.card('Plains', price=50, type_line='Basic Land — Plains'), self.card('Bat', price=1.2),
                 self.card('Haliya', price=6.0), self.card('Sideboard card', 'side', price=30)]
        self.assertEqual(self.covers('', cards), ['Haliya'])

    def test_a_deck_of_lands_only_still_has_a_cover_and_an_empty_deck_has_none(self):
        self.assertEqual(self.covers('', [self.card('Plains', price=0.1, type_line='Basic Land — Plains')]), ['Plains'])
        self.assertEqual(self.covers('', []), [])
        # a card the card data does not know has no picture: it cannot be a cover
        self.assertEqual(self.covers('', [self.card('Unknown', image='')]), [])


if __name__ == '__main__':
    unittest.main()
