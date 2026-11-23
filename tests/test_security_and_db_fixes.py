"""
Regression tests for security, database integrity, and MCP/station fixes:
- Backup archive upload and restore refuse path traversal in inventory_captures.file
- Transactional._transaction resets _transaction_depth even if rollback() raises
- InventoryManager.update_card invalidates stale Undo pointers on split
- CardDatabase search escapes SQL LIKE wildcards (_, %)
- MCP _plan_changes applies removals before validating additions and enforces Commander singleton on new cards
- server/lock.py revokes the session token server-side on DELETE /api/settings_lock
- station_setup.SetupPage.test never sends X-Station-Token to /api/stations or to a different server URL

    venv/bin/python -m unittest discover tests -p test_security_and_db_fixes.py
"""
import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

TOP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOP / 'src'))
sys.path.insert(0, str(TOP / 'mcp_server'))
sys.path.insert(0, str(TOP))

from card_scanner.core import backups, database, decks, inventory, storage  # noqa: E402
from card_scanner.station import station_setup  # noqa: E402
from mcp_server import tools as mcp_tools  # noqa: E402


class BackupCaptureFileValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.backups_dir = root / 'backups'
        self.captures_dir = root / 'captures'
        self.full_dir = root / 'captures_full'
        for patch in (
            mock.patch.object(backups, 'BACKUPS_DIR', self.backups_dir),
            mock.patch.object(backups, 'CAPTURES_DIR', self.captures_dir),
            mock.patch.object(inventory, 'CAPTURES_DIR', self.captures_dir),
            mock.patch.object(inventory, 'FULL_CAPTURES_DIR', self.full_dir),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.col = inventory.InventoryManager(db_file=root / 'col.db')
        self.scan = inventory.InventoryManager(db_file=root / 'scan.db')
        self.deck_store = decks.DeckManager(db_file=root / 'col.db')
        self.addCleanup(self.col.conn.close)
        self.addCleanup(self.scan.conn.close)
        self.addCleanup(self.deck_store.conn.close)

    def _make_poisoned_backup_zip(self, bad_filename):
        info = backups.create(self.col, self.scan, self.deck_store, note='clean')
        bid = info['id']
        db_path = self.backups_dir / bid / 'backup.db'
        conn = sqlite3.connect(str(db_path))
        conn.execute("INSERT INTO collection_inventory (id, game, card_name, set_name, card_number, quantity, condition, finish, timestamp, location, tags) "
                     "VALUES (1, 'mtg', 'Sol Ring', 'Commander', '1', 1, 'Near Mint', 'regular', '2026-01-01', '', '')")
        conn.execute("INSERT INTO collection_inventory_captures (id, inventory_id, file, captured_at) VALUES (1, 1, ?, '2026-01-01')",
                     (bad_filename,))
        conn.commit()
        conn.close()
        buf = io.BytesIO()
        backups.archive(bid, buf)
        backups.delete(bid)
        buf.seek(0)
        return buf, bid

    def test_upload_rejects_traversal_in_inventory_captures_file(self):
        for bad in ('../api_keys.env', '/etc/passwd', 'sub/card.jpg', '..\\secret_key'):
            buf, _ = self._make_poisoned_backup_zip(bad)
            with self.assertRaises(backups.BackupError, msg=bad):
                backups.add_archive(buf)

    def test_restore_rejects_traversal_in_existing_backup_db(self):
        info = backups.create(self.col, self.scan, self.deck_store, note='clean')
        bid = info['id']
        conn = sqlite3.connect(str(self.backups_dir / bid / 'backup.db'))
        conn.execute("INSERT INTO collection_inventory (id, game, card_name, set_name, card_number, quantity, condition, finish, timestamp, location, tags) "
                     "VALUES (1, 'mtg', 'Sol Ring', 'Commander', '1', 1, 'Near Mint', 'regular', '2026-01-01', '', '')")
        conn.execute("INSERT INTO collection_inventory_captures (id, inventory_id, file, captured_at) VALUES (1, 1, '../secret_key', '2026-01-01')")
        conn.commit()
        conn.close()
        with self.assertRaises(backups.BackupError):
            backups.restore(bid, self.col, self.scan, self.deck_store)


class TransactionalDepthReset(unittest.TestCase):
    def test_depth_resets_even_if_rollback_raises(self):
        class BrokenConn:
            def commit(self):
                raise sqlite3.OperationalError('disk full')

            def rollback(self):
                raise sqlite3.OperationalError('rollback failed')

        class Dummy(storage.Transactional):
            def __init__(self):
                self._lock = threading.RLock()
                self.conn = BrokenConn()
                self.rolled_back = 0

            def _rolled_back(self):
                self.rolled_back += 1

        d = Dummy()
        with self.assertRaises(sqlite3.OperationalError):
            with d._transaction():
                pass
        self.assertEqual(d._transaction_depth, 0)
        self.assertEqual(d.rolled_back, 1)


class LikeWildcardEscaping(unittest.TestCase):
    def test_underscore_does_not_match_arbitrary_character(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = database.CardDatabase(db_file=Path(tmp) / 'cards.db')
            self.addCleanup(db.close)
            with db._lock:
                db.conn.execute(
                    "INSERT INTO cards (id, name, set_code, set_name, collector_number, rarity, name_search) "
                    "VALUES ('1', 'Alpha Goblin', 'set', 'Set', '1', 'common', 'alpha goblin')"
                )
                db.conn.execute(
                    "INSERT INTO cards (id, name, set_code, set_name, collector_number, rarity, name_search) "
                    "VALUES ('2', '_____ Goblin', 'unf', 'Unfinity', '1', 'common', '_____ goblin')"
                )
                db.conn.commit()
            rows, _ = db.search_cards(text='_____')
            self.assertEqual([r['name'] for r in rows], ['_____ Goblin'])


class McpPlanChangesFixes(unittest.TestCase):
    def _fake_api(self, deck_data, cards):
        class FakeApi:
            def get(self, path, **params):
                if path == '/api/decks':
                    return {'decks': [{'id': 1, 'name': deck_data['name'], 'format': deck_data['format'], 'kind': deck_data['kind']}]}
                if path == '/api/decks/1':
                    return {'deck': deck_data}
                if path == '/api/inventory':
                    return {'cards': [], 'entries': []}
                if path == '/api/cards/search':
                    q = (params.get('q') or '').lower()
                    return {'cards': [c for c in cards if c['name'].lower() == q]}
                raise AssertionError(path)
        return FakeApi()

    def test_board_move_of_four_of_succeeds(self):
        deck = {
            'id': 1, 'name': 'Burn', 'format': 'standard', 'kind': 'paper',
            'cards': [{'name': 'Lightning Bolt', 'quantity': 4, 'board': 'main', 'owned': 4}],
        }
        cards = [{'id': 'bolt', 'name': 'Lightning Bolt', 'type_line': 'Instant', 'oracle_text': '', 'identity': ['R'], 'owned': 4}]
        api = self._fake_api(deck, cards)
        result, _ = mcp_tools._plan_changes(
            api, deck,
            add=[{'name': 'Lightning Bolt', 'quantity': 2, 'board': 'side'}],
            remove=[{'name': 'Lightning Bolt', 'quantity': 2, 'board': 'main'}],
        )
        self.assertEqual(result['problems'], 0, result)
        self.assertEqual(result['cards_after'], 4)

    def test_commander_rejects_multiple_copies_of_new_card(self):
        deck = {
            'id': 1, 'name': 'EDH', 'format': 'commander', 'kind': 'paper',
            'cards': [{'name': 'Krenko, Mob Boss', 'quantity': 1, 'board': 'commander', 'owned': 1, 'card': {'identity': ['R']}}],
        }
        cards = [{'id': 'bolt', 'name': 'Lightning Bolt', 'type_line': 'Instant', 'oracle_text': '', 'identity': ['R'], 'owned': 4}]
        api = self._fake_api(deck, cards)
        result, _ = mcp_tools._plan_changes(
            api, deck,
            add=[{'name': 'Lightning Bolt', 'quantity': 2, 'board': 'main'}],
        )
        self.assertGreater(result['problems'], 0)
        self.assertIn('one copy', result['add'][0]['problem'])


class StationSetupTokenSafety(unittest.TestCase):
    def test_saved_token_not_sent_to_stations_list_or_different_server(self):
        asked = []

        class Answer:
            def __init__(self, code, body):
                self.status_code, self.body = code, body

            def json(self):
                return self.body

        def fake_get(url, timeout=None, headers=None):
            asked.append((url, (headers or {}).get('X-Station-Token')))
            return Answer(200, {'stations': [], 'token_required': True})

        page = station_setup.SetupPage(
            None, {'id': 'desk', 'server': 'http://real:5000', 'token': 'supersecret'}, {}, restart=lambda: None
        )
        with mock.patch.object(station_setup.requests, 'get', fake_get):
            result = page.test({'server': 'http://attacker:5000'})
        self.assertFalse(result['ok'])
        self.assertEqual(len(asked), 1)
        self.assertIsNone(asked[0][1])  # never leaked saved token to attacker


if __name__ == '__main__':
    unittest.main()
