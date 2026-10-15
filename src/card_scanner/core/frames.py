#!/usr/bin/env python3
"""
The camera frames behind captures: training data for a better card detector later.

A capture is the card as the station cut it out and straightened - the picture it was cut from
and the outline that was found in it are gone. A station with Debug trace on sends both beside
the capture (CardScanner._frame_snapshot): the whole camera frame as a JPEG and a small note -
the outline's corners in that frame's pixels, what made the card count as new, the stable
frames, the time since the capture before. They are kept here, one pair of files per capture:

    data/frames/<capture_id>.jpg    the frame, as the camera made it
    data/frames/<capture_id>.json   the station's note + what the server adds (station, number,
                                    the name of the capture's picture in scanned_cards/)

Not part of the collection: not in the collection backups (data/backups/), and capped - the
oldest pairs go first once the folder is over MAX_BYTES. scripts/backup.sh's archive takes the
data folder whole, so it is in that one. Nothing reads these during scanning; they are for
scripts (scripts/ ... later) that train or score a detector on them.
"""

import json
import logging
import os
import re
import threading
import time

from card_scanner.config import Config

logger = logging.getLogger('database')

FRAMES_DIR = Config.DATA_DIR / 'frames'
MAX_BYTES = 3 * 1024 ** 3        # the folder's size: the oldest go when it is over
MAX_FRAME_BYTES = 12 * 1024 ** 2  # one frame (a 4K JPEG is ~2 MB): more is not a camera frame
PRUNE_EVERY = 50                  # saves between two looks at the folder's size

_NAME = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
_lock = threading.Lock()
_saved = 0


def save(jpeg, meta, station_id, number, capture_id=None, image_name=None):
    """
    Keep one capture's frame. jpeg: its bytes; meta: the station's note (a dict). Returns
    whether it was kept: False for what is not a JPEG, too large, or already here (the same
    capture sent again is not a second frame).
    """
    global _saved
    if not jpeg or jpeg[:2] != b'\xff\xd8' or len(jpeg) > MAX_FRAME_BYTES:
        return False
    # The station's own id names the pair - checked: it is part of a path. Without one (an
    # older station) the station, the number and the time do.
    stem = capture_id if capture_id and _NAME.match(capture_id) else f"{station_id}_{int(number)}_{int(time.time())}"
    note = {**(meta if isinstance(meta, dict) else {}), 'station': station_id, 'number': int(number),
            'capture_id': capture_id, 'image': image_name, 'received': time.strftime('%Y-%m-%d %H:%M:%S')}
    with _lock:
        FRAMES_DIR.mkdir(parents=True, exist_ok=True)
        picture, record = FRAMES_DIR / f"{stem}.jpg", FRAMES_DIR / f"{stem}.json"
        if record.exists():
            return False
        # The picture, then the note: a pair without its note is a write that was cut short
        for path, data in ((picture, jpeg), (record, json.dumps(note, indent=1).encode())):
            temporary = path.with_name(path.name + '.tmp')
            temporary.write_bytes(data)
            os.replace(temporary, path)
        _saved += 1
        if _saved % PRUNE_EVERY == 0:
            _prune()
    return True


def attach(capture_id, uid):
    """
    Note which capture (its uid, pending.py) a frame belongs to, once the capture is settled: the
    scanned card carries that uid (inventory_captures.capture_key), so the frame can be let go
    with the card (forget). Nothing happens for a capture that has no frame.
    """
    if not capture_id or not uid or not _NAME.match(capture_id):
        return False
    record = FRAMES_DIR / f"{capture_id}.json"
    with _lock:
        try:
            note = json.loads(record.read_text())
            if note.get('uid') == uid:
                return True
            note['uid'] = uid
            temporary = record.with_name(record.name + '.tmp')
            temporary.write_text(json.dumps(note, indent=1))
            os.replace(temporary, record)
            return True
        except (OSError, ValueError):
            return False


def forget(uids):
    """
    Delete the frames of these captures (their uids): the scanned cards they were taken for
    were cleared or moved into the collection. A frame without a uid (taken before this, or
    never settled) stays - it is the cap that removes those. Returns how many were deleted.
    """
    uids = {uid for uid in uids if uid}
    if not uids or not FRAMES_DIR.is_dir():
        return 0
    deleted = 0
    with _lock:
        for record in FRAMES_DIR.glob('*.json'):
            try:
                note = json.loads(record.read_text())
            except (OSError, ValueError):
                continue
            if note.get('uid') in uids:
                record.with_suffix('.jpg').unlink(missing_ok=True)
                record.unlink(missing_ok=True)
                deleted += 1
    return deleted


def usage():
    """(number of frames kept, their size in bytes)"""
    if not FRAMES_DIR.is_dir():
        return 0, 0
    count = size = 0
    for path in FRAMES_DIR.iterdir():
        try:
            if path.suffix == '.json':
                count += 1
            size += path.stat().st_size
        except OSError:
            continue
    return count, size


def _prune():
    """Delete the oldest frames while the folder is over MAX_BYTES (caller holds _lock)"""
    files = []
    for path in FRAMES_DIR.iterdir():
        try:
            info = path.stat()
        except OSError:
            continue
        files.append((info.st_mtime, info.st_size, path))
    total = sum(size for _, size, _ in files)
    if total <= MAX_BYTES:
        return
    for _, size, path in sorted(files):
        if total <= MAX_BYTES:
            break
        path.unlink(missing_ok=True)
        total -= size
    logger.info(f"Camera frames: over {MAX_BYTES // 1024 ** 2} MB, the oldest were removed")
