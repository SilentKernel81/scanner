# ============================================================================
# FILE: storage.py
# What the database managers share: one transaction per edit, and numbered
# schema upgrades
# ============================================================================
import logging
from contextlib import contextmanager

logger = logging.getLogger('database')


class Transactional:
    """
    For a manager with self.conn and self._lock (an RLock). `with self._transaction():` is one
    edit: everything in it is committed together, or - when it raises - not at all, so a
    failure never leaves half an edit on the connection for the next commit to pick up.
    Methods that use it may call each other: the outermost one commits.
    """

    _transaction_depth = 0

    @contextmanager
    def _transaction(self):
        with self._lock:
            if self._transaction_depth:
                yield  # part of the edit that is under way
                return
            self._transaction_depth = 1
            try:
                yield
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                self._transaction_depth = 0
                self._rolled_back()
                raise
            self._transaction_depth = 0
            self._committed()

    def _committed(self):
        """After an edit is committed: what cannot be undone (deleting files) belongs here"""

    def _rolled_back(self):
        pass


def upgrade(conn, component, steps):
    """
    Bring one part of a database file (a manager's tables: 'inventory', 'decks', ...) to its
    newest schema. steps[n](conn) takes it from version n to n + 1; each runs in one
    transaction together with the new version number (table schema_versions), so a step
    either happened and is recorded, or did not happen. Several managers share a file - each
    has its own component and never touches another's version.

    The managers' older migrations, which look at the columns a table has, come before these
    and stay as they are: a table they have brought up to date is version 0 here.

    Returns the version the component is at.
    """
    conn.execute('CREATE TABLE IF NOT EXISTS schema_versions '
                 '(component TEXT PRIMARY KEY, version INTEGER NOT NULL)')
    conn.commit()
    while True:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT version FROM schema_versions WHERE component = ?', (component,)).fetchone()
            version = row[0] if row else 0
            if version >= len(steps):  # also a file last opened by a newer version of the program
                conn.rollback()
                return version
            steps[version](conn)
            conn.execute('INSERT OR REPLACE INTO schema_versions (component, version) VALUES (?, ?)',
                         (component, version + 1))
            conn.commit()
            logger.info(f"Database schema: {component} upgraded to version {version + 1}")
        except Exception:
            conn.rollback()
            raise
