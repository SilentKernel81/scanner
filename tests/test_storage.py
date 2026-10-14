#!/usr/bin/env python3
"""
The shared database helpers (core/storage.py): one edit is all-or-nothing, numbered steps.

Run:  venv/bin/python tests/test_storage.py        (or with the others: -m unittest discover tests)
"""
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from card_scanner.core import storage  # noqa: E402


class Manager(storage.Transactional):
    def __init__(self, path):
        self._lock = threading.RLock()
        self.conn = storage.connect(path)
        self.conn.execute('CREATE TABLE t (n INTEGER)')
        self.conn.commit()
        self.committed = self.rolled_back = 0

    def _committed(self):
        self.committed += 1

    def _rolled_back(self):
        self.rolled_back += 1

    def count(self):
        return self.conn.execute('SELECT COUNT(*) FROM t').fetchone()[0]

    def add_two(self, fail=False):
        with self._transaction():
            self.conn.execute('INSERT INTO t VALUES (1)')
            self.add_one()  # nested: part of this edit
            if fail:
                raise RuntimeError('boom')

    def add_one(self):
        with self._transaction():
            self.conn.execute('INSERT INTO t VALUES (2)')


class TransactionalTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.m = Manager(Path(self.dir.name) / 'x.db')

    def tearDown(self):
        self.m.conn.close()
        self.dir.cleanup()

    def test_commits_once_when_nested(self):
        self.m.add_two()
        self.assertEqual((self.m.count(), self.m.committed), (2, 1))

    def test_failure_leaves_nothing_and_the_next_edit_is_clean(self):
        with self.assertRaises(RuntimeError):
            self.m.add_two(fail=True)
        self.assertEqual((self.m.count(), self.m.rolled_back, self.m.conn.in_transaction), (0, 1, False))
        self.m.add_one()
        self.assertEqual(self.m.count(), 1)


class UpgradeTests(unittest.TestCase):
    def test_steps_run_once_and_a_failing_step_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            conn = storage.connect(Path(d) / 'x.db')
            steps = [lambda c: c.execute('CREATE TABLE a (x)'), lambda c: c.execute('CREATE TABLE b (x)')]
            self.assertEqual(storage.upgrade(conn, 'part', steps), 2)
            self.assertEqual(storage.upgrade(conn, 'part', steps), 2)

            def bad(c):
                c.execute('CREATE TABLE c (x)')
                raise RuntimeError('no')
            with self.assertRaises(RuntimeError):
                storage.upgrade(conn, 'part', steps + [bad])
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'c'").fetchone())
            self.assertEqual(conn.execute("SELECT version FROM schema_versions WHERE component = 'part'").fetchone()[0], 2)
            conn.close()


if __name__ == '__main__':
    unittest.main()
