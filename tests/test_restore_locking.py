#!/usr/bin/env python3
"""
A restore is one critical section: the backup of what is there now and the replacement of it
(core/backups.py: restore). A card accepted between the two would be in neither.

Run:  venv/bin/python tests/test_restore_locking.py        (or with the others: -m unittest discover tests)
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ownership import CARD, GAME, OTHER, TemporaryData  # noqa: E402

from card_scanner.core import backups  # noqa: E402


class RestoreIsOneSection(TemporaryData):
    def test_a_card_added_while_the_recovery_backup_is_made_is_not_lost(self):
        self.collection.add_card(CARD, GAME, 'regular', quiet=True)
        old = backups.create(self.collection, self.scan, self.decks, note='x')['id']
        self.scan.add_card(OTHER, GAME, 'regular', quiet=True, source='desk-a')   # there at the restore

        started, results = threading.Event(), {}
        real_create = backups.create

        def create_then_let_a_capture_in(*args, **kwargs):
            made = real_create(*args, **kwargs)
            # The window the review found: the recovery backup is made, the tables not yet replaced
            def settle():
                started.set()
                results['id'] = self.scan.add_card(CARD, GAME, 'regular', quiet=True, source='desk-b')
                results['finished'] = time.time()
            thread = threading.Thread(target=settle)
            thread.start()
            started.wait(5)
            time.sleep(0.4)
            results['blocked_in_window'] = thread.is_alive()  # waiting for the restore's locks
            results['thread'] = thread
            return made

        with mock.patch.object(backups, 'create', create_then_let_a_capture_in):
            backups.restore(old, self.collection, self.scan, self.decks)
        results['thread'].join(5)

        self.assertTrue(results['blocked_in_window'], 'the capture got in between the backup and the replacement')
        # It went in after the restore, into the restored state: not deleted by it
        self.assertEqual({c['name'] for c in self.scan.get_all_cards(GAME)}, {'Test Card'})
        self.assertEqual(self.scan.get_stats(GAME)['total_cards'], 1)

    def test_the_restore_route_takes_the_barrier_before_the_locks(self):
        import card_scanner.server.startup  # noqa: F401 - the routes exist once their modules are imported
        from card_scanner.server import backups_api
        from card_scanner.server.state import app, maintenance_barrier
        seen = {}

        def fake_restore(*args, **kwargs):
            seen['barrier_held'] = maintenance_barrier._is_owned()
            return {'restored': {'created': 'then'}, 'previous': {}}

        with mock.patch.object(backups_api.backups, 'restore', fake_restore), \
                mock.patch.object(backups_api.backups, 'list_backups', return_value=[]), \
                mock.patch.object(backups_api, 'scan_inventory', new=mock.MagicMock()), mock.patch.object(backups_api, 'inventory', new=mock.MagicMock()), \
                mock.patch.object(backups_api, 'deck_store', new=mock.MagicMock()), mock.patch.object(backups_api, 'arena_inventory', new=mock.MagicMock()), \
                mock.patch.object(backups_api, 'socketio', new=mock.MagicMock()), mock.patch.object(backups_api, 'log_to_client', new=mock.MagicMock()), \
                mock.patch.object(backups_api, 'games', new=mock.MagicMock()):
            reply = app.test_client().post('/api/backups/2026-01-01_00-00-00/restore', base_url='http://localhost')
        self.assertEqual(reply.status_code, 200)
        self.assertTrue(seen['barrier_held'])


if __name__ == '__main__':
    unittest.main()
