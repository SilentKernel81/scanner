# ============================================================================
# FILE: backups.py
# Backups of what the user made - the collection, the scanned cards and the
# decks - taken from the collection page (before a big load) and restored there
# ============================================================================
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime

from config import Config
from inventory import CAPTURES_DIR

logger = logging.getLogger(__name__)

# Made by the user (kept until deleted), when the app starts (create_daily) and before a restore.
# One folder per backup, named by its time: backup.db (the tables below as plain copies, and
# `info`) and captures/ - the capture thumbnails the entries point at, as hard links (no extra
# space; they stay when the app deletes its own). Card data and settings are not part of it:
# scripts/backup.sh archives everything.
BACKUPS_DIR = Config.DATA_DIR / 'backups'
BACKUP_ID = re.compile(r'^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(-\d+)?$')
INVENTORY_TABLES = ('inventory', 'inventory_captures', 'inventory_batches')
# Not in backups made before it existed: restoring one of those rebuilds it from its entries
OPTIONAL_TABLES = ('inventory_batches',)
DECK_TABLES = ('decks', 'deck_cards')
KEEP_AUTOMATIC = 5  # backups made before a restore; the ones the user makes are kept until deleted
KEEP_DAILY = 7      # backups made when the app starts (one per day, see create_daily)
# A backup as one file (archive / add_archive), to take the cards to another computer or data folder
ARCHIVE_MEMBER = re.compile(r'^(backup\.db|captures/[\w.-]+)$')
ARCHIVE_LIMIT = 4 * 1024 ** 3  # bytes unpacked


class BackupError(Exception):
    pass


def _parts(inventory, scan_inventory, deck_store):
    """(prefix of the tables in backup.db, manager, its tables) - also the order of the locks
    (collection before scanned cards, as InventoryManager.take_from)"""
    return (('collection_', inventory, INVENTORY_TABLES), ('scanned_', scan_inventory, INVENTORY_TABLES),
            ('', deck_store, DECK_TABLES))


def _columns(conn, table):
    return [row[1] for row in conn.execute(f'PRAGMA table_info({table})')]


def _folder(backup_id):
    if not BACKUP_ID.match(backup_id or ''):
        raise BackupError('Unknown backup')
    folder = BACKUPS_DIR / backup_id
    if not (folder / 'backup.db').is_file():
        raise BackupError('Unknown backup')
    return folder


def _link(source, target):
    """A capture file in another place: a hard link, or a copy where links don't work"""
    if target.exists() or not source.exists():
        return
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def create(inventory, scan_inventory, deck_store, note='', automatic=False, daily=False):
    """Back up the collection, the scanned cards and the decks as they are now; returns its info"""
    created = datetime.now()
    backup_id = created.strftime('%Y-%m-%d_%H-%M-%S')
    number = 1
    while (BACKUPS_DIR / backup_id).exists():  # another one this second
        number += 1
        backup_id = f"{created.strftime('%Y-%m-%d_%H-%M-%S')}-{number}"
    folder = BACKUPS_DIR / backup_id
    work = BACKUPS_DIR / f'.{backup_id}.tmp'  # moved into place when complete
    (work / 'captures').mkdir(parents=True)
    try:
        target = sqlite3.connect(str(work / 'backup.db'))
        parts = _parts(inventory, scan_inventory, deck_store)
        with inventory._lock, scan_inventory._lock, deck_store._lock:
            for prefix, manager, tables in parts:
                for table in tables:
                    columns = _columns(manager.conn, table)
                    target.execute(f"CREATE TABLE {prefix}{table} ({', '.join(columns)})")
                    target.executemany(
                        f"INSERT INTO {prefix}{table} VALUES ({', '.join('?' * len(columns))})",
                        (tuple(row) for row in manager.conn.execute(f"SELECT {', '.join(columns)} FROM {table}")))
            for prefix in ('collection_', 'scanned_'):
                for (file,) in target.execute(f'SELECT file FROM {prefix}inventory_captures'):
                    _link(CAPTURES_DIR / file, work / 'captures' / file)
        count = lambda sql: target.execute(sql).fetchone()[0]
        info = {
            'id': backup_id, 'created': created.strftime('%Y-%m-%d %H:%M:%S'), 'note': (note or '').strip()[:80],
            'automatic': bool(automatic), 'daily': bool(daily),
            'cards': count('SELECT COALESCE(SUM(quantity), 0) FROM collection_inventory'),
            'entries': count('SELECT COUNT(*) FROM collection_inventory'),
            'scanned': count('SELECT COALESCE(SUM(quantity), 0) FROM scanned_inventory'),
            'decks': count('SELECT COUNT(*) FROM decks'),
        }
        target.execute('CREATE TABLE info (value TEXT)')
        target.execute('INSERT INTO info VALUES (?)', (json.dumps(info),))
        target.commit()
        target.close()
        os.replace(work, folder)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    logger.info(f"Backup {backup_id}: {info['cards']} cards, {info['scanned']} scanned, {info['decks']} decks")
    # Only its own kind makes room: the daily ones don't push out the ones before a restore
    for kind, keep in (('daily', KEEP_DAILY if daily else None), ('automatic', KEEP_AUTOMATIC if automatic else None)):
        if keep:
            for old in [item for item in list_backups() if item.get(kind)][keep:]:
                delete(old['id'])
    return info


def create_daily(inventory, scan_inventory, deck_store):
    """
    The backup made when the app starts: one per day - a later start the same day finds it and
    makes none, as does a start with nothing to keep. Returns its info, or None.
    """
    today = datetime.now().strftime('%Y-%m-%d')
    if any(item.get('daily') and item['created'].startswith(today) for item in list_backups()):
        return None
    made = create(inventory, scan_inventory, deck_store, note='Application start', daily=True)
    if not (made['entries'] or made['scanned'] or made['decks']):
        delete(made['id'])
        return None
    return made


def list_backups():
    """The backups, newest first"""
    found = []
    if not BACKUPS_DIR.is_dir():
        return found
    for folder in sorted(BACKUPS_DIR.iterdir(), reverse=True):
        if not BACKUP_ID.match(folder.name) or not (folder / 'backup.db').is_file():
            continue
        try:
            source = sqlite3.connect(f"file:{folder / 'backup.db'}?mode=ro", uri=True)
            info = json.loads(source.execute('SELECT value FROM info').fetchone()[0])
            source.close()
        except (sqlite3.Error, TypeError, ValueError) as e:
            logger.warning(f"Backup {folder.name} cannot be read: {e}")
            continue
        found.append({**info, 'id': folder.name})
    return found


def _has_table(conn, table):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone())


def _check(source):
    """A backup.db that can be restored in full: raises BackupError before anything is touched"""
    try:
        if source.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise BackupError('This backup is damaged')
        info = json.loads(source.execute('SELECT value FROM info').fetchone()[0])
        for prefix, _manager, tables in _parts(None, None, None):
            for table in tables:
                if table in OPTIONAL_TABLES and not _has_table(source, prefix + table):
                    continue
                source.execute(f'SELECT * FROM {prefix}{table} LIMIT 1').fetchone()
    except (sqlite3.Error, TypeError, ValueError) as e:
        raise BackupError(f'This backup cannot be read ({e})')
    return info


def _restore_part(conn, source, prefix, tables):
    """Replace `tables` through `conn` (inside its open transaction) with a backup's copies"""
    for table in tables:
        conn.execute(f'DELETE FROM {table}')
    for table in tables:
        if not _has_table(source, prefix + table):
            continue  # an optional one: rebuilt by the caller
        # Columns added since the backup keep their defaults
        saved = _columns(source, prefix + table)
        columns = [column for column in _columns(conn, table) if column in saved]
        conn.executemany(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            (tuple(row) for row in source.execute(f"SELECT {', '.join(columns)} FROM {prefix}{table}")))


def restore(backup_id, inventory, scan_inventory, deck_store):
    """
    Put the collection, the scanned cards and the decks back as they were in a backup (what
    is there now is backed up first, so a restore can be taken back). Returns
    {'restored': the backup's info, 'previous': the backup made of the state replaced}.

    The backup is checked in full before anything changes. The collection and the decks are
    one file and come back in one transaction; the scanned cards, in their own file, are
    written in a second one that is committed right after it - a failure before that point
    rolls both back and nothing has changed.
    """
    folder = _folder(backup_id)
    source = sqlite3.connect(f"file:{folder / 'backup.db'}?mode=ro", uri=True)
    try:
        info = _check(source)
        previous = create(inventory, scan_inventory, deck_store, note=f"Before restoring {backup_id.replace('_', ' ')}",
                          automatic=True)
        # The decks through the collection's connection when they share its file: one commit
        together = str(deck_store.db_file) == str(inventory.db_file)
        deck_conn = inventory.conn if together else deck_store.conn
        with inventory._lock, scan_inventory._lock, deck_store._lock:
            main, scan = inventory.conn, scan_inventory.conn
            try:
                for conn in {main, scan, deck_conn}:
                    conn.commit()  # nothing of ours is pending; the restore starts clean
                    conn.execute('BEGIN IMMEDIATE')
                for manager, prefix in ((inventory, 'collection_'), (scan_inventory, 'scanned_')):
                    _restore_part(manager.conn, source, prefix, INVENTORY_TABLES)
                    if not _has_table(source, prefix + 'inventory_batches'):
                        manager.rebuild_batches()
                    # A move between the two that was under way describes other cards
                    manager.forget_moves()
                _restore_part(deck_conn, source, '', DECK_TABLES)
            except Exception as e:
                for conn in {main, scan, deck_conn}:
                    conn.rollback()
                raise BackupError(f"The restore failed ({e}) - nothing was changed") from e
            try:
                main.commit()
                if deck_conn is not main:
                    deck_conn.commit()
                scan.commit()
            except Exception as e:
                for conn in {main, scan, deck_conn}:
                    conn.rollback()
                raise BackupError(f"The restore stopped part way ({e}) - the state from before is in the "
                                  f"backup \"{previous['note']}\"") from e
            inventory.last_added = scan_inventory.last_added = None
            # Captures deleted since the backup come back with their entries
            CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
            for saved in (folder / 'captures').glob('*'):
                _link(saved, CAPTURES_DIR / saved.name)
    finally:
        source.close()
    logger.info(f"Restored backup {backup_id} (the state before it: backup {previous['id']})")
    return {'restored': {**info, 'id': backup_id}, 'previous': previous}




def archive(backup_id):
    """
    A backup as one zip: returns (the open file, at its start; its size). The file has no
    name - each download packs its own, and it is gone when closed, also when the download
    is cut short.
    """
    folder = _folder(backup_id)
    packed = tempfile.TemporaryFile(dir=BACKUPS_DIR)
    try:
        with zipfile.ZipFile(packed, 'w') as target:
            target.write(folder / 'backup.db', 'backup.db', compress_type=zipfile.ZIP_DEFLATED)
            for capture in sorted((folder / 'captures').glob('*')):
                target.write(capture, f'captures/{capture.name}')  # JPEGs: stored as they are
        size = packed.tell()
        packed.seek(0)
    except Exception:
        packed.close()
        raise
    return packed, size


def add_archive(file):
    """
    Add a backup downloaded elsewhere (archive) to the list, from where it can be restored;
    returns its info. It counts as made by the user: the automatic ones never push it out.
    """
    created = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    work = BACKUPS_DIR / f'.upload-{created}.tmp'
    not_a_backup = BackupError('This file is not a Card Scanner backup')
    try:
        try:
            with zipfile.ZipFile(file) as source:
                members = [member for member in source.infolist() if not member.is_dir()]
                if ('backup.db' not in {member.filename for member in members}
                        or not all(ARCHIVE_MEMBER.match(member.filename) for member in members)):
                    raise not_a_backup
                if sum(member.file_size for member in members) > ARCHIVE_LIMIT:
                    raise BackupError('This backup is too large')
                (work / 'captures').mkdir(parents=True)
                for member in members:
                    with source.open(member) as packed, open(work / member.filename, 'wb') as unpacked:
                        shutil.copyfileobj(packed, unpacked)
        except zipfile.BadZipFile:
            raise not_a_backup
        try:
            saved = sqlite3.connect(str(work / 'backup.db'))
            info = json.loads(saved.execute('SELECT value FROM info').fetchone()[0])
            for prefix, _manager, tables in _parts(None, None, None):
                for table in tables:
                    if table not in OPTIONAL_TABLES or _has_table(saved, prefix + table):
                        saved.execute(f'SELECT 1 FROM {prefix}{table} LIMIT 1')
            # Its own time when free here, so it sorts where it belongs
            base = info['id'][:19] if BACKUP_ID.match(str(info.get('id') or '')) else created
            backup_id, number = base, 1
            while (BACKUPS_DIR / backup_id).exists():
                number += 1
                backup_id = f'{base}-{number}'
            info.update(id=backup_id, automatic=False, daily=False, note=info.get('note') or 'Uploaded')
            saved.execute('UPDATE info SET value = ?', (json.dumps(info),))
            saved.commit()
            saved.close()
        except (sqlite3.Error, TypeError, ValueError, KeyError):
            raise not_a_backup
        os.replace(work, BACKUPS_DIR / backup_id)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    logger.info(f"Backup {backup_id} uploaded: {info.get('cards')} cards, {info.get('scanned')} scanned, {info.get('decks')} decks")
    return info


def delete(backup_id):
    shutil.rmtree(_folder(backup_id))
    logger.info(f"Backup {backup_id} deleted")
