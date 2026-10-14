#!/usr/bin/env python3
"""
The rules the databases keep themselves, whatever code writes (core/inventory.py: the tables'
CHECK and REFERENCES ... ON DELETE CASCADE; core/decks.py likewise). The standalone program kept
the same rules as triggers; this is the list, one case per rule, so that neither way of keeping
them can lose one without a test saying so.

Run:  venv/bin/python tests/test_constraints.py        (or with the others: -m unittest discover tests)
"""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ownership import CARD, GAME, TemporaryData  # noqa: E402


class Rules(TemporaryData):
    def refused(self, manager, sql, *params):
        """The statement is refused by the database - and leaves nothing half done"""
        with self.assertRaises(sqlite3.IntegrityError, msg=sql):
            manager.conn.execute(sql, params)
        manager.conn.rollback()

    def setUp(self):
        super().setUp()
        self.entry = self.collection.add_card(CARD, GAME, 'regular', quantity=2, quiet=True, capture=self.photo)
        self.deck = self.decks.create(GAME, 'Rules deck', 'commander')
        self.decks.import_cards(self.deck, [{'name': 'Llanowar Elves', 'card_id': None, 'quantity': 1, 'board': 'main'}])

    # -- inventory ----------------------------------------------------------------------------

    def test_an_entry_has_a_positive_whole_quantity(self):
        for value in (0, -1, 1.5, 'many'):
            self.refused(self.collection, 'UPDATE inventory SET quantity = ? WHERE id = ?', value, self.entry)
        self.refused(self.collection, "INSERT INTO inventory (card_name, set_name, quantity, timestamp) "
                                      "VALUES ('X', 'Y', 0, 'now')")

    # -- what stands beside an entry ----------------------------------------------------------

    def test_captures_batches_and_sources_belong_to_an_entry(self):
        self.refused(self.collection, "INSERT INTO inventory_captures (inventory_id, file, captured_at) "
                                      "VALUES (999999, 'x.jpg', 'now')")
        self.refused(self.collection, "INSERT INTO inventory_batches (batch, inventory_id, added_at, quantity) "
                                      "VALUES ('b', 999999, 'now', 1)")
        self.refused(self.collection, "INSERT INTO inventory_sources (inventory_id, station, quantity) "
                                      "VALUES (999999, 'desk', 1)")

    def test_batches_and_sources_have_a_positive_whole_quantity(self):
        for value in (0, -1, 'x'):
            self.refused(self.collection, "INSERT INTO inventory_batches (batch, inventory_id, added_at, quantity) "
                                          "VALUES ('b2', ?, 'now', ?)", self.entry, value)
            self.refused(self.collection, "INSERT INTO inventory_sources (inventory_id, station, quantity) "
                                          "VALUES (?, 'desk', ?)", self.entry, value)

    def test_a_deleted_entry_takes_what_stands_beside_it(self):
        self.collection.conn.execute("INSERT INTO inventory_sources VALUES (?, 'desk', 1)", (self.entry,))
        before = [self.collection.conn.execute(f'SELECT COUNT(*) FROM {t} WHERE inventory_id = ?', (self.entry,)).fetchone()[0]
                  for t in ('inventory_captures', 'inventory_batches', 'inventory_sources')]
        self.assertTrue(all(before), before)
        self.collection.conn.execute('DELETE FROM inventory WHERE id = ?', (self.entry,))
        self.collection.conn.commit()
        after = [self.collection.conn.execute(f'SELECT COUNT(*) FROM {t} WHERE inventory_id = ?', (self.entry,)).fetchone()[0]
                 for t in ('inventory_captures', 'inventory_batches', 'inventory_sources')]
        self.assertEqual(after, [0, 0, 0])

    # -- decks --------------------------------------------------------------------------------

    def test_a_deck_card_belongs_to_a_deck_has_a_positive_quantity_and_a_known_board(self):
        self.refused(self.decks, "INSERT INTO deck_cards (deck_id, card_name, quantity, board) VALUES (999999, 'X', 1, 'main')")
        for value in (0, -1, 'x', 1.5):
            self.refused(self.decks, "INSERT INTO deck_cards (deck_id, card_name, quantity, board) VALUES (?, 'X', ?, 'main')",
                         self.deck, value)
        self.refused(self.decks, "INSERT INTO deck_cards (deck_id, card_name, quantity, board) VALUES (?, 'X', 1, 'maybe')",
                     self.deck)
        self.refused(self.decks, "UPDATE deck_cards SET board = 'maybe' WHERE deck_id = ?", self.deck)

    def test_a_deleted_deck_takes_its_cards(self):
        self.assertEqual(self.decks.conn.execute('SELECT COUNT(*) FROM deck_cards WHERE deck_id = ?', (self.deck,)).fetchone()[0], 1)
        self.decks.conn.execute('DELETE FROM decks WHERE id = ?', (self.deck,))
        self.decks.conn.commit()
        self.assertEqual(self.decks.conn.execute('SELECT COUNT(*) FROM deck_cards WHERE deck_id = ?', (self.deck,)).fetchone()[0], 0)


# A file as the standalone program made it (its code at df5108a, 2026-10-09): the rules kept as
# triggers, on tables with no CHECK or REFERENCES. Written out here, as that code is not in
# this project. Opened by this program it must be given the rules themselves - this failed
# once ("no such table: main.inventory" at the rename), leaving the file unchanged and the
# server unable to start on it.
STANDALONE_FILE = """
CREATE TABLE inventory (id INTEGER PRIMARY KEY AUTOINCREMENT, game TEXT NOT NULL DEFAULT 'mtg', card_id TEXT,
    card_name TEXT NOT NULL, set_name TEXT NOT NULL, set_code TEXT, card_number TEXT NOT NULL DEFAULT '',
    rarity TEXT, type_line TEXT, mana_cost TEXT, colors TEXT, color_identity TEXT, price_usd REAL,
    quantity INTEGER NOT NULL DEFAULT 1, condition TEXT NOT NULL DEFAULT 'Near Mint',
    finish TEXT NOT NULL DEFAULT 'regular', timestamp TEXT NOT NULL, location TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '', added_at TEXT, added_quantity INTEGER,
    UNIQUE(game, card_name, set_name, card_number, condition, finish, location));
CREATE TABLE inventory_captures (id INTEGER PRIMARY KEY AUTOINCREMENT, inventory_id INTEGER NOT NULL,
    file TEXT NOT NULL, captured_at TEXT NOT NULL);
CREATE TABLE inventory_batches (id INTEGER PRIMARY KEY AUTOINCREMENT, batch TEXT NOT NULL,
    inventory_id INTEGER NOT NULL, added_at TEXT NOT NULL, quantity INTEGER NOT NULL, UNIQUE(batch, inventory_id));
CREATE TABLE decks (id INTEGER PRIMARY KEY AUTOINCREMENT, game TEXT NOT NULL, name TEXT NOT NULL,
    format TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE deck_cards (id INTEGER PRIMARY KEY AUTOINCREMENT, deck_id INTEGER NOT NULL, card_name TEXT NOT NULL,
    card_id TEXT, quantity INTEGER NOT NULL DEFAULT 1, board TEXT NOT NULL DEFAULT 'main',
    UNIQUE(deck_id, card_name, board));
CREATE TRIGGER inventory_quantity_insert BEFORE INSERT ON inventory BEGIN
    SELECT RAISE(ABORT, 'q') WHERE typeof(NEW.quantity) != 'integer' OR NEW.quantity <= 0; END;
CREATE TRIGGER inventory_quantity_update BEFORE UPDATE ON inventory BEGIN
    SELECT RAISE(ABORT, 'q') WHERE typeof(NEW.quantity) != 'integer' OR NEW.quantity <= 0; END;
CREATE TRIGGER inventory_captures_entry_insert BEFORE INSERT ON inventory_captures BEGIN
    SELECT RAISE(ABORT, 'e') WHERE NOT EXISTS (SELECT 1 FROM inventory WHERE id = NEW.inventory_id); END;
CREATE TRIGGER inventory_captures_entry_update BEFORE UPDATE ON inventory_captures BEGIN
    SELECT RAISE(ABORT, 'e') WHERE NOT EXISTS (SELECT 1 FROM inventory WHERE id = NEW.inventory_id); END;
CREATE TRIGGER inventory_takes_inventory_captures AFTER DELETE ON inventory BEGIN
    DELETE FROM inventory_captures WHERE inventory_id = OLD.id; END;
CREATE TRIGGER inventory_batches_entry_insert BEFORE INSERT ON inventory_batches BEGIN
    SELECT RAISE(ABORT, 'e') WHERE NOT EXISTS (SELECT 1 FROM inventory WHERE id = NEW.inventory_id); END;
CREATE TRIGGER inventory_batches_entry_update BEFORE UPDATE ON inventory_batches BEGIN
    SELECT RAISE(ABORT, 'e') WHERE NOT EXISTS (SELECT 1 FROM inventory WHERE id = NEW.inventory_id); END;
CREATE TRIGGER inventory_batches_quantity_insert BEFORE INSERT ON inventory_batches BEGIN
    SELECT RAISE(ABORT, 'q') WHERE typeof(NEW.quantity) != 'integer' OR NEW.quantity <= 0; END;
CREATE TRIGGER inventory_batches_quantity_update BEFORE UPDATE ON inventory_batches BEGIN
    SELECT RAISE(ABORT, 'q') WHERE typeof(NEW.quantity) != 'integer' OR NEW.quantity <= 0; END;
CREATE TRIGGER inventory_takes_inventory_batches AFTER DELETE ON inventory BEGIN
    DELETE FROM inventory_batches WHERE inventory_id = OLD.id; END;
CREATE TRIGGER deck_cards_valid_insert BEFORE INSERT ON deck_cards BEGIN
    SELECT RAISE(ABORT, 'd') WHERE NOT EXISTS (SELECT 1 FROM decks WHERE id = NEW.deck_id); END;
CREATE TRIGGER deck_cards_valid_update BEFORE UPDATE ON deck_cards BEGIN
    SELECT RAISE(ABORT, 'd') WHERE NOT EXISTS (SELECT 1 FROM decks WHERE id = NEW.deck_id); END;
CREATE TRIGGER decks_take_cards AFTER DELETE ON decks BEGIN
    DELETE FROM deck_cards WHERE deck_id = OLD.id; END;
CREATE TABLE schema_versions (component TEXT PRIMARY KEY, version INTEGER NOT NULL);
INSERT INTO schema_versions VALUES ('inventory', 2), ('decks', 1);
INSERT INTO inventory (card_name, set_name, card_number, quantity, timestamp, added_at, added_quantity)
    VALUES ('Old Card', 'Old Set', '1', 3, '2026-10-01 10:00:00', '2026-10-01 10:00:00', 3),
           ('Other', 'Old Set', '2', 2, '2026-10-02 10:00:00', '2026-10-02 10:00:00', 1);
INSERT INTO inventory_captures (inventory_id, file, captured_at) VALUES (1, 'a.jpg', 'then');
INSERT INTO inventory_batches (batch, inventory_id, added_at, quantity)
    VALUES ('old:1', 1, '2026-10-01 10:00:00', 3), ('old:2', 2, '2026-10-02 10:00:00', 1);
INSERT INTO decks (game, name, format, created_at, updated_at) VALUES ('mtg', 'Old deck', 'commander', 'then', 'then');
INSERT INTO deck_cards (deck_id, card_name, quantity, board) VALUES (1, 'Llanowar Elves', 1, 'main');
"""


class AFileTheStandaloneMade(TemporaryData):
    def test_it_is_given_the_rules_themselves_and_loses_nothing(self):
        from card_scanner.core import decks as decks_module
        from card_scanner.core import inventory as inventory_module
        path = self.tmp / 'standalone.db'
        old = sqlite3.connect(str(path))
        old.executescript(STANDALONE_FILE)
        old.close()

        inventory = inventory_module.InventoryManager(db_file=path)
        decks = decks_module.DeckManager(db_file=path)
        self.addCleanup(inventory.conn.close)
        self.addCleanup(decks.conn.close)
        one = lambda sql: inventory.conn.execute(sql).fetchone()
        self.assertEqual(tuple(one('SELECT COUNT(*), SUM(quantity) FROM inventory')), (2, 5))
        self.assertEqual(tuple(one('SELECT COUNT(*), SUM(quantity) FROM inventory_batches')), (2, 4))
        self.assertEqual(one('SELECT COUNT(*) FROM inventory_captures')[0], 1)
        self.assertEqual(tuple(one('SELECT COUNT(*) FROM decks')), (1,))
        self.assertEqual(one('SELECT COUNT(*) FROM deck_cards')[0], 1)
        self.assertEqual(one('PRAGMA integrity_check')[0], 'ok')
        self.assertEqual(inventory.conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        # The triggers of the other program are gone, the tables carry the rules
        self.assertEqual(inventory.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name NOT LIKE 'trade_%'").fetchall(), [])
        for table in ('inventory', 'inventory_captures', 'inventory_batches', 'deck_cards'):
            sql = one(f"SELECT sql FROM sqlite_master WHERE name = '{table}'")[0]
            self.assertTrue('CHECK' in sql or 'REFERENCES' in sql, table)
        # ... and they hold
        for connection, statement in (
                (inventory, 'UPDATE inventory SET quantity = 0 WHERE id = 1'),
                (inventory, "INSERT INTO inventory_captures (inventory_id, file, captured_at) VALUES (99, 'x', 'n')"),
                (inventory, "INSERT INTO inventory_batches (batch, inventory_id, added_at, quantity) VALUES ('b', 99, 'n', 1)"),
                (inventory, "INSERT INTO inventory_batches (batch, inventory_id, added_at, quantity) VALUES ('b', 1, 'n', 0)"),
                (decks, "INSERT INTO deck_cards (deck_id, card_name, quantity, board) VALUES (99, 'X', 1, 'main')"),
                (decks, "INSERT INTO deck_cards (deck_id, card_name, quantity, board) VALUES (1, 'X', 1, 'maybe')")):
            with self.assertRaises(sqlite3.IntegrityError, msg=statement):
                connection.conn.execute(statement)
            connection.conn.rollback()
        # A second start finds nothing left to do
        inventory_module.InventoryManager(db_file=path).conn.close()


if __name__ == '__main__':
    unittest.main()
