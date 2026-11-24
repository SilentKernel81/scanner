# image_index.py
# A 96-byte fingerprint of every printing's picture, and the search through them: the building
# block of the printing check (printing_check.py).
#
# Why: the collector line of a card does not always name its printing. A Secret Lair card prints the
# set and number of the card it is made from, and a card the AI misreads has a name and number that
# agree with each other and with nothing in the picture. The picture itself is the only evidence that
# does not go through the OCR or the AI. (docs/plans/IMAGE_MATCH_EXPERIMENT.md has what was measured.)
#
# The fingerprint (descriptor 'phash_rgb_v1') is a perceptual hash of the whole card, per colour:
#   1. the card's edge is cut off a little (2.5% top and bottom, 3.5% left and right: a camera's
#      picture has a dark rim and a sleeve's edge, Scryfall's has rounded corners), the picture is
#      scaled to 224 x 312 and each colour channel is stretched to its 2nd - 98th percentile (a dim,
#      glare-washed camera picture and Scryfall's bright one meet halfway);
#   2. it is scaled to 64 x 64 and each channel goes through the discrete cosine transform;
#   3. the lowest 16 x 16 frequencies of each channel become one bit each: above or below their
#      median. 3 channels x 256 bits = 768 bits = 96 bytes.
# Two pictures are compared by the number of bits that differ (Hamming distance); the similarity
# 1 - 2 x differing / 768 runs from -1 to 1, and is what the experiment's numbers use.
#
# The same function makes the fingerprints of Scryfall's pictures (the builder) and of a capture (the
# check), so they agree by construction - and a test pins it to what the experiment's own code gave.
# Change anything in `prepare` or `hash_card` and every index built before is wrong: DESCRIPTOR must
# change with it, and the loader refuses an index of another descriptor.
#
# The index is a folder (data/image_index/) of GENERATIONS and one pointer to the current one:
#   hashes-<generation>.bin   the fingerprints, 96 bytes each, back to back
#   ids-<generation>.txt      the Scryfall id of each, one a line, in the same order
#   manifest.json             the pointer: which generation is current, its descriptor, count, when it
#                             was built, and the SHA-256 of both files
# A generation's two files are written first, each under a temporary name and renamed, and nothing
# points at them yet; then the manifest is replaced - ONE rename, the only step that publishes. So an
# interrupted build leaves the previous generation current and whole (the next build clears what was
# left), a reader that loads while one is published gets one generation or the other and never a mix
# (it only follows the manifest), and a pair of files that do not belong together - a torn write, a copied
# file - fails the checksums even when the counts happen to agree. The generation before the current one
# is kept until the next publication, so a reader that was slow still finds its files.

import contextlib
import functools
import gzip
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import fcntl                 # flock: what data/app.lock uses too; released when the process ends, however it ends
except ImportError:              # a platform without it: no locks, as the project's own instance lock (the program is not run there)
    fcntl = None

import cv2
import numpy as np

logger = logging.getLogger('ai')

DESCRIPTOR = 'phash_rgb_v1'
MANIFEST = 'manifest.json'        # the pointer to the current generation: replaced atomically, the only thing that publishes
HASH_BYTES = 96
HASH_BITS = HASH_BYTES * 8
CARD_SIZE = (224, 312)            # (width, height) a card picture is scaled to first
EDGE = (0.025, 0.975, 0.035, 0.965)   # the part kept: y from / to, x from / to (fractions)

# Scryfall's rules (https://scryfall.com/docs/api): a User-Agent and an Accept header on every request,
# 50-100 ms between two requests. The pictures come from a CDN, but the rule is kept all the same.
HEADERS = {'User-Agent': 'mtg-card-scanner/1.0 (a personal collection scanner)', 'Accept': 'image/jpeg,image/*;q=0.9,*/*;q=0.8'}
REQUEST_GAP = 0.1

# Bits that differ between two bytes, for numpy versions without np.bitwise_count (numpy < 2)
_POPCOUNT = np.array([bin(i).count('1') for i in range(256)], np.uint8)


# ---------------------------------------------------------------------------------------------
# The fingerprint
# ---------------------------------------------------------------------------------------------

def stretch_plane(plane):
    """One colour of a picture with its 2nd to 98th percentile stretched to 0..255 (uint8 plane in, uint8 out). A function of its own so that another program - the Android app - can be tested against it"""
    low, high = np.percentile(plane, (2, 98))
    # float64 on purpose (what the experiment's numpy 2 did without saying so; numpy 1 would have used float32): the
    # same bits on any numpy. Cast to uint8 it cuts the fraction off, as the experiment did.
    return np.clip((plane.astype(np.float64) - low) * 255 / max(high - low, 1), 0, 255).astype(np.uint8)


def channel_bits(low):
    """The 32 bytes of one colour: which of the 16 x 16 lowest DCT frequencies (float32) lie above the median of all of them but the first (0, 0), packed 8 to a byte, row by row"""
    return np.packbits((low > np.median(low.flatten()[1:])).flatten())


def prepare(rgb):
    """The picture every fingerprint is made from: edge cut, 224 x 312, each colour stretched. RGB uint8 in and out."""
    height, width = rgb.shape[:2]
    y0, y1, x0, x1 = EDGE
    rgb = cv2.resize(rgb[int(height * y0):int(height * y1), int(width * x0):int(width * x1)], CARD_SIZE, interpolation=cv2.INTER_AREA)
    out = np.empty_like(rgb)
    for channel in range(3):
        out[..., channel] = stretch_plane(rgb[..., channel])
    return out


def hash_card(rgb):
    """The 96-byte fingerprint of a card picture (RGB uint8, the card filling the picture)"""
    small = cv2.resize(prepare(rgb), (64, 64), interpolation=cv2.INTER_AREA)
    parts = []
    for channel in range(3):
        low = cv2.dct(small[..., channel].astype(np.float32))[:16, :16]       # the 16 x 16 lowest frequencies
        parts.append(channel_bits(low))                                        # [1:] inside: not the plain average (0, 0)
    return np.concatenate(parts).tobytes()


def similarity(distance):
    """-1..1 from a Hamming distance (or an array of them): 1 = the same fingerprint"""
    return 1.0 - 2.0 * np.asarray(distance, np.float64) / HASH_BITS


# ---------------------------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------------------------

class ImageIndex:
    """All the fingerprints in memory (~11 MB for the whole card database): `distances` of one to all of them in a few ms"""

    def __init__(self, ids, hashes, built=None):
        self.ids = ids                                             # Scryfall ids, in the order of the rows
        self.hashes = hashes                                       # (n, 96) uint8
        self.built = built
        self._row = {printing: n for n, printing in enumerate(ids)}
        # The same bytes as 12 numbers of 64 bits a row: xor and count bits 8 bytes at a time
        self._words = np.ascontiguousarray(hashes).view(np.uint64) if len(hashes) else None

    def __len__(self):
        return len(self.ids)

    def row(self, printing_id):
        """The row of a printing, or None (a printing newer than the index)"""
        return self._row.get(printing_id)

    def distances(self, fingerprint):
        """How many bits differ between a fingerprint (96 bytes) and each row: an int array"""
        if self._words is None:
            return np.zeros(0, np.int32)
        query = np.frombuffer(fingerprint, np.uint64)
        differing = np.bitwise_xor(self._words, query)
        if hasattr(np, 'bitwise_count'):
            return np.bitwise_count(differing).sum(axis=1, dtype=np.int32)
        return _POPCOUNT[differing.view(np.uint8)].sum(axis=1, dtype=np.int32)

    @classmethod
    def load(cls, directory):
        """
        The index in a folder - the generation the manifest points at - or None when there is none, or it is not whole, not of
        this descriptor, or its two files do not belong together (the checksums in the manifest).
        """
        directory = Path(directory)
        try:
            manifest = json.loads((directory / MANIFEST).read_text())
            names = manifest['ids'], manifest['hashes']
            if any(Path(name).name != name for name in names):           # a file name, never a path
                return None
            ids_bytes = (directory / names[0]).read_bytes()
            hashes_bytes = (directory / names[1]).read_bytes()
        except (OSError, ValueError, KeyError, TypeError):
            return None
        ids = ids_bytes.decode(errors='replace').split()
        if (manifest.get('descriptor') != DESCRIPTOR or manifest.get('count') != len(ids) or len(hashes_bytes) != len(ids) * HASH_BYTES
                or manifest.get('ids_sha256') != hashlib.sha256(ids_bytes).hexdigest()
                or manifest.get('hashes_sha256') != hashlib.sha256(hashes_bytes).hexdigest()):
            logger.warning(f"Image index in {directory} is not usable (another descriptor, not whole, or its files do not belong together)"
                           " - build it again")
            return None
        return cls(ids, np.frombuffer(hashes_bytes, np.uint8).reshape(-1, HASH_BYTES), manifest.get('built'))


# ---------------------------------------------------------------------------------------------
# Where the card is, in a picture that is more than the card
# ---------------------------------------------------------------------------------------------

CARD_RATIO = 88 / 63              # a card's height / width
TRAY_BRIGHTNESS = 125             # brighter than this and connected to the picture's edge: what the card lies on, not the card
REGION_WORK_WIDTH = 320           # the search for the rectangle is done on a small copy
REGION_MIN_AREA = 0.35            # a "card" smaller than this share of the picture is something else


def card_regions(rgb):
    """
    Rectangles (x0, y0, x1, y1) that may be the card in a picture holding more than the card: a fixed capture area as drawn, with tray around it and the edges of the
    cards below. What is bright and touches the picture's edge is taken for the tray; what is left is the pile, and since the cards below stick out on some side, the
    card itself is tried against each side at a card's proportions. Only proposals: PrintingCheck.flat_card keeps one only if it looks more like a printing than the
    whole picture does. So a light-bordered card or a dark mat, which this rule misreads, costs nothing. NOT part of the fingerprint (hash_card, prepare).
    """
    height, width = rgb.shape[:2]
    if width < 32 or height < 32:
        return []
    scale = REGION_WORK_WIDTH / width
    small = cv2.resize(rgb, (REGION_WORK_WIDTH, max(8, int(height * scale))), interpolation=cv2.INTER_AREA)
    grey = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_RGB2GRAY), (5, 5), 0)
    light = cv2.morphologyEx((grey > TRAY_BRIGHTNESS).astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    _, labels = cv2.connectedComponents(light)
    touching = set(labels[0]) | set(labels[-1]) | set(labels[:, 0]) | set(labels[:, -1])
    touching.discard(0)                                    # 0 is "not light"
    pile = ~np.isin(labels, list(touching))
    columns, rows = np.flatnonzero(pile.mean(axis=0) > 0.6), np.flatnonzero(pile.mean(axis=1) > 0.6)
    if not len(columns) or not len(rows):
        return []
    x0, x1, y0, y1 = columns[0] / scale, (columns[-1] + 1) / scale, rows[0] / scale, (rows[-1] + 1) / scale
    box_width, box_height = x1 - x0, y1 - y0
    if box_width * box_height < REGION_MIN_AREA * width * height:
        return []
    boxes = [(x0, y0, x1, y1)]
    if box_height / box_width < CARD_RATIO:                # wider than a card: the pile sticks out left or right
        card_width = box_height / CARD_RATIO
        boxes += [(x0, y0, x0 + card_width, y1), (x1 - card_width, y0, x1, y1), ((x0 + x1 - card_width) / 2, y0, (x0 + x1 + card_width) / 2, y1)]
    else:                                                  # taller: above or below
        card_height = box_width * CARD_RATIO
        boxes += [(x0, y0, x1, y0 + card_height), (x0, y1 - card_height, x1, y1), (x0, (y0 + y1 - card_height) / 2, x1, (y0 + y1 + card_height) / 2)]
    inside = [(max(0, int(a)), max(0, int(b)), min(width, int(c)), min(height, int(d))) for a, b, c, d in boxes]
    return [box for box in dict.fromkeys(inside) if box[2] - box[0] >= 32 and box[3] - box[1] >= 32]


TABLE_COLUMNS = ('id', 'set', 'number', 'oracle_id', 'finishes', 'name', 'released_at', 'frame')


def printings_table(ids, facts):
    """
    What a phone needs of each printing of the index besides its fingerprint, as a gzipped tab-separated text (first line the column names, then one row per
    id, in the index's order): id, set code, collector number, oracle id, finishes ('nonfoil,foil,etched'), name, and - since 2026-10-21, added AT THE END so
    that a reader of the first six columns is not disturbed (the phone reads the columns by their names) - released_at and frame (printing_check.prints_a_marker:
    the phone's fixed-place foil reading asks it). `facts` is database.printing_facts() (a five-tuple of before is written with the two left empty). A printing
    the card data no longer has is left out (the phone joins by id, never by position), and the same input always gives the same bytes (no time in the gzip
    header), so its checksum says whether anything changed.
    """
    def clean(value):
        return str(value or '').replace('\t', ' ').replace('\n', ' ').replace('\r', ' ')

    lines = ['\t'.join(TABLE_COLUMNS)]
    for printing in ids:
        fact = facts.get(printing)
        if fact:
            name, set_code, number, oracle_id, finishes, *more = fact
            released_at, frame = (list(more) + [None, None])[:2]
            lines.append('\t'.join(clean(value) for value in (printing, (set_code or '').lower(), number, oracle_id, finishes, name, released_at, frame)))
    return gzip.compress(('\n'.join(lines) + '\n').encode(), mtime=0)


class BuildRunning(Exception):
    """Another build of the same index is running (in this or another process)"""


@contextlib.contextmanager
def _flock(path, wait=True):
    """An exclusive lock on a file (flock - one per open file, so it also separates two threads of one process). wait=False: BuildRunning if taken."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as handle:
        if fcntl is not None:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BuildRunning(f"another build of the image index is running ({path.name})") from None
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _publication(directory):
    """
    The lock `write` holds from looking up the previous generation to the end of the clean-up: two writers (two overlapping builds) would
    otherwise each clean up the other's files, even the ones the manifest points to. Held for seconds, never while pictures are hashed.
    """
    return _flock(Path(directory) / '.publishing')


def _building(directory):
    """
    Held by `build` for its whole run, and not waited for: a second build of the same index is refused. Two at once would do the same hours of
    work twice, and the one that publishes last would leave out what the other added.
    """
    return _flock(Path(directory) / '.building', wait=False)


def _put(path, data):
    """A file whole or not at all: to a temporary name, on disk (fsync), then renamed over its name"""
    temporary = path.with_name(path.name + '.tmp')
    with open(temporary, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


def _generation_of(name):
    """The generation a folder's file belongs to ('hashes-<g>.bin' -> g), or None for the manifest and anything else"""
    match = re.fullmatch(r'(?:hashes-(.+)\.bin|ids-(.+)\.txt)(?:\.tmp)?', name)
    return (match.group(1) or match.group(2)) if match else None


def write(directory, ids, hashes, built=None):
    """
    Publishes an index as a new generation: its two files first (each whole or not at all, nothing points at them yet), then the manifest
    - one rename, the only step that publishes. Whatever happens before that rename leaves the previous generation current and whole.
    Afterwards everything except this generation and the one before it is removed: what an interrupted earlier build left, and the
    files of the older format (hashes.bin, ids.txt, meta.json). built: when the index was built, for one imported from elsewhere (default: now).
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    ids_bytes, hashes_bytes = ''.join(i + '\n' for i in ids).encode(), b''.join(hashes)
    with _publication(directory):        # from the lookup of the previous generation to the end of the clean-up: one writer at a time
        try:
            previous = json.loads((directory / MANIFEST).read_text()).get('generation')
        except (OSError, ValueError, AttributeError):
            previous = None
        generation = f'{int(time.time())}-{uuid.uuid4().hex[:8]}'        # unique even for two writes in one second
        _put(directory / f'hashes-{generation}.bin', hashes_bytes)
        _put(directory / f'ids-{generation}.txt', ids_bytes)
        manifest = {'descriptor': DESCRIPTOR, 'count': len(ids), 'hash_bytes': HASH_BYTES, 'built': built or time.strftime('%Y-%m-%d %H:%M:%S'),
                    'generation': generation, 'hashes': f'hashes-{generation}.bin', 'ids': f'ids-{generation}.txt',
                    'hashes_sha256': hashlib.sha256(hashes_bytes).hexdigest(), 'ids_sha256': hashlib.sha256(ids_bytes).hexdigest()}
        _put(directory / MANIFEST, json.dumps(manifest, indent=1).encode())          # publishes
        keep = {generation, previous}
        for path in directory.iterdir():
            owner = _generation_of(path.name)
            if owner is not None:
                stale = owner not in keep                                             # a generation, current / previous or not
            else:
                stale = path.name in ('hashes.bin', 'ids.txt', 'meta.json', MANIFEST + '.tmp')   # the older format, a manifest never renamed
            if stale:
                try:
                    path.unlink()
                except OSError:
                    pass                                                              # a reader may have it open: the next write clears it


# ---------------------------------------------------------------------------------------------
# Moving an index between servers: one zip file (Settings > Card data > Picture index)
# ---------------------------------------------------------------------------------------------
# The zip holds what the folder's current generation is: manifest.json and its two files. An import never unpacks it into the folder: the
# contents are read into memory, checked, and published by `write` as a NEW generation - so the one-rename rule above holds for an import too, a
# reader sees the old index or the new one, and a phone sees a new generation and downloads it. The one backup is a zip of the same format, so
# putting it back is simply uploading it.

ARCHIVE_LIMIT = 200 * 1024 * 1024       # the largest upload taken (a whole index is about 15 MB packed)
MAX_PRINTINGS = 1_000_000               # far above the game's ~120,000: what a zip may claim before anything is unpacked (a "zip bomb")
MAX_ID_BYTES = 64                       # a Scryfall id and its line end take 37 bytes
MAX_MANIFEST_BYTES = 64 * 1024
_PRINTING_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')      # what Scryfall's ids look like


class IndexFileError(Exception):
    """An index file that cannot be used, or no index to pack. Its message is shown to the person."""


def backup_path(directory):
    """The ONE backup of an index folder: beside it (data/image_index_backup.zip), so that nothing that cleans up the folder can touch it"""
    directory = Path(directory)
    return directory.with_name(directory.name + '_backup.zip')


def backup_info(directory):
    """{'count', 'built'} of the one backup, read from the manifest inside it, or None when there is none or it is not a readable index file"""
    try:
        with zipfile.ZipFile(backup_path(directory)) as archive:
            manifest = json.loads(archive.read(MANIFEST))
        return {'count': int(manifest['count']), 'built': manifest.get('built')}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, zipfile.BadZipFile, zlib.error):
        return None


def _current_files(directory):
    """(manifest, ids bytes, hashes bytes) of the current generation, or None when there is none or it is not whole. The caller holds `_publication`."""
    try:
        manifest = json.loads((directory / MANIFEST).read_text())
        names = manifest['ids'], manifest['hashes']
        if any(Path(name).name != name for name in names):               # a file name, never a path (as `ImageIndex.load`)
            return None
        ids_bytes, hashes_bytes = (directory / names[0]).read_bytes(), (directory / names[1]).read_bytes()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    count = len(ids_bytes.split())
    if (manifest.get('descriptor') != DESCRIPTOR or manifest.get('count') != count or len(hashes_bytes) != count * HASH_BYTES
            or manifest.get('ids_sha256') != hashlib.sha256(ids_bytes).hexdigest()
            or manifest.get('hashes_sha256') != hashlib.sha256(hashes_bytes).hexdigest()):
        return None
    return manifest, ids_bytes, hashes_bytes


def export_archive(directory, out):
    """
    Writes the current index as a zip to `out` (a binary file) and returns its manifest; IndexFileError when there is no whole index. Under the publication
    lock from the first read to the last, so that a build publishing meanwhile cannot delete a file between the manifest and the zip.
    """
    directory = Path(directory)
    with _publication(directory):
        current = _current_files(directory)
        if current is None:
            raise IndexFileError('This server has no complete picture index to download')
        manifest, ids_bytes, hashes_bytes = current
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MANIFEST, json.dumps(manifest, indent=1))
            archive.writestr(manifest['ids'], ids_bytes)
            # Fingerprints look like noise to a compressor: stored as they are, which saves the time and costs nothing
            archive.writestr(manifest['hashes'], hashes_bytes, compress_type=zipfile.ZIP_STORED)
    return manifest


def _read_archive(stream):
    """(ids, hashes bytes, built) of an uploaded zip, every check done before anything is changed; IndexFileError says what is wrong with it"""
    try:
        with zipfile.ZipFile(stream) as archive:
            infos = archive.infolist()
            sizes = {info.filename: info.file_size for info in infos}
            # Sizes are checked from the zip's table BEFORE unpacking: a small file that unpacks to gigabytes is refused here
            if len(sizes) != len(infos) or sizes.get(MANIFEST, MAX_MANIFEST_BYTES + 1) > MAX_MANIFEST_BYTES:
                raise IndexFileError('This is not a picture index file')
            manifest = json.loads(archive.read(MANIFEST))
            if not isinstance(manifest, dict):
                raise IndexFileError('This is not a picture index file')
            if manifest.get('descriptor') != DESCRIPTOR or manifest.get('hash_bytes') != HASH_BYTES:
                raise IndexFileError('This picture index was made by another version of the program and cannot be used here')
            count, ids_name, hashes_name = manifest.get('count'), manifest.get('ids'), manifest.get('hashes')
            if (isinstance(count, bool) or not isinstance(count, int) or not 0 < count <= MAX_PRINTINGS
                    or set(sizes) != {MANIFEST, ids_name, hashes_name} or len(sizes) != 3
                    or sizes[hashes_name] != count * HASH_BYTES or sizes[ids_name] > count * MAX_ID_BYTES):
                raise IndexFileError('This picture index file is not complete')
            ids_bytes, hashes_bytes = archive.read(ids_name), archive.read(hashes_name)     # the zip's own checksum is verified here
    except IndexFileError:
        raise
    except (zipfile.BadZipFile, zlib.error, EOFError, ValueError, TypeError, KeyError, RuntimeError, NotImplementedError) as e:   # RuntimeError: encrypted
        raise IndexFileError('This is not a picture index file, or it is damaged') from e
    if (manifest.get('ids_sha256') != hashlib.sha256(ids_bytes).hexdigest()
            or manifest.get('hashes_sha256') != hashlib.sha256(hashes_bytes).hexdigest()):
        raise IndexFileError('This picture index file is damaged (its checksums do not match)')
    try:
        ids = ids_bytes.decode('ascii').split()
    except UnicodeDecodeError as e:
        raise IndexFileError('This picture index file is damaged') from e
    if len(ids) != count or len(set(ids)) != count or not all(_PRINTING_ID.fullmatch(printing) for printing in ids):
        raise IndexFileError('This picture index file is damaged (its list of printings is wrong)')
    built = manifest.get('built')
    return ids, hashes_bytes, built if isinstance(built, str) and len(built) <= 32 else None


def _save_backup(directory):
    """
    The current index into the one backup file, replacing the last backup only once the new one is whole on disk. Returns its path, or None when there is no
    whole index to keep (then an earlier backup is left alone: a broken index must never take the place of a good copy). A failure raises: nothing is replaced.
    """
    target = backup_path(directory)
    temporary = target.with_name(target.name + '.tmp')
    try:
        with open(temporary, 'wb') as out:
            export_archive(directory, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, target)
    except IndexFileError:
        temporary.unlink(missing_ok=True)
        return None
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def import_archive(directory, stream):
    """
    Replaces the folder's index with the one in an uploaded zip: checked whole, the current index saved as the one backup, then published as a new generation.
    IndexFileError: the file is refused; BuildRunning: a build is running (it would overwrite the import with what it holds in memory when it ends). In both,
    and when the backup cannot be written, the index is unchanged. Returns {'count', 'built', 'unchanged', 'backup' (whether one was saved)}.
    """
    directory = Path(directory)
    ids, hashes_bytes, built = _read_archive(stream)                     # before any lock: a bad file disturbs nothing
    with _building(directory):                                           # no build may start or run until the new index is published
        with _publication(directory):
            current = _current_files(directory)
        # The index already in use: nothing to do - and above all no backup, which would replace the good older copy with this same index
        if current is not None and current[0]['hashes_sha256'] == hashlib.sha256(hashes_bytes).hexdigest() and current[1].split() == [i.encode() for i in ids]:
            return {'count': len(ids), 'built': current[0].get('built'), 'unchanged': True, 'backup': False}
        saved = _save_backup(directory)
        write(directory, ids, [hashes_bytes[n * HASH_BYTES:(n + 1) * HASH_BYTES] for n in range(len(ids))], built=built)
    logger.info(f"Image index imported: {len(ids)} printings, built {built}" + (f"; the previous one is in {saved.name}" if saved else '; there was no whole index to back up'))
    return {'count': len(ids), 'built': built, 'unchanged': False, 'backup': saved is not None}


# ---------------------------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------------------------

class Stopped(Exception):
    """Scryfall keeps refusing: the builder stops instead of hammering it"""


def _one_build_at_a_time(function):
    """build only runs while it holds the folder's build lock: a second build (this process or another) raises BuildRunning at once"""
    @functools.wraps(function)
    def wrapper(printings, directory, *args, **kwargs):
        with _building(directory):
            return function(printings, directory, *args, **kwargs)
    return wrapper


class BuildResult(tuple):
    """
    What build returns: (printings in the index, new ones, left out for want of a picture) - a plain 3-tuple to every caller that unpacks or compares it - and
    `reason`, how the run ended: 'done', 'cancelled' (the caller asked it to stop) or 'refused' (Scryfall kept refusing: what was done is written, and the
    run is to be tried again later). A run that ended early is NOT a failure of what it wrote, but a caller that says "done" for it says something false.
    """
    reason = 'done'


@_one_build_at_a_time
def build(printings, directory, image_dir=None, fetch=True, workers=8, limit=None, log=print, progress=None, cancel=None):
    """
    Builds (or completes) the index of a folder from a list of (Scryfall id, picture address). One build at a time per folder
    (BuildRunning for a second); writing the result is one more, short lock (`write`: two writers must not clean up each other's files).
    Fingerprints already in the folder's index are kept (a descriptor of another kind is not
    reused: the loader refuses it), so a run after new sets arrive only does the new printings.
    image_dir: a folder of <id>.jpg pictures to take first (the experiment's cache has them all:
    no download); fetch: download what is missing from Scryfall - 8 requests at a time but never
    two started less than 0.1 s apart, one 429 / 403 is waited out, a refusal that goes on stops the
    run (what was done is written first: it resumes). limit: stop after this many new ones (a trial).
    progress(done, total): called after every printing, from the calling thread (the Settings page's button shows it); cancel: a threading.Event -
    once it is set no further request is sent - also not by a worker that is waiting out a 429 or its turn (those waits end within a quarter of a second; a request already on its
    way is waited for, at most its 60 s time-out) - what was done is written and the run ends (the same state as after a refusal: run it again and it resumes).
    Returns a BuildResult: (printings in the index, new ones, left out for want of a picture), and .reason ('done', 'cancelled', 'refused').
    """
    existing = ImageIndex.load(directory)
    have = dict(zip(existing.ids, existing.hashes)) if existing else {}
    todo = [(printing, address) for printing, address in printings if printing not in have]
    if limit:
        todo = todo[:limit]
    log(f"{len(have)} printings already in the index, {len(todo)} to do")
    gate, next_start, stopped = threading.Lock(), [0.0], threading.Event()
    image_dir = Path(image_dir) if image_dir else None

    def cancelled():
        return cancel is not None and cancel.is_set()

    def pause(seconds):
        """
        Wait - but not through a stop: a cancel, or the run stopping itself, ends the wait within a quarter of a second. (The waits here are the
        0.1 s between requests and what Scryfall asks after a 429 / 403: a minute or more, during which a Stop must not be ignored.)
        """
        end = time.monotonic() + seconds
        while not (stopped.is_set() or cancelled()):
            left = end - time.monotonic()
            if left <= 0:
                return
            stopped.wait(min(0.25, left))

    def picture(printing, address):
        """The picture's bytes: from the folder, else from Scryfall; None if there is none"""
        if image_dir and (image_dir / f'{printing}.jpg').is_file():
            return (image_dir / f'{printing}.jpg').read_bytes()
        if not fetch or not address:
            return None
        import requests            # only to download: a build from a folder of pictures needs no network
        for attempt in range(4):
            if stopped.is_set() or cancelled():
                stopped.set()
                raise Stopped()
            with gate:             # requests may overlap, but never two start less than REQUEST_GAP apart
                pause(max(0.0, next_start[0] - time.monotonic()))
                next_start[0] = time.monotonic() + REQUEST_GAP
            if stopped.is_set() or cancelled():      # a stop that came while this one waited for its turn: no request is sent after it
                stopped.set()
                raise Stopped()
            try:
                answer = requests.get(address, headers=HEADERS, timeout=60)
            except requests.RequestException:
                return None
            if answer.status_code in (429, 403):
                retry_after = answer.headers.get('Retry-After', '')
                delay = 0
                if retry_after:
                    try:
                        delay = max(0, int(retry_after))
                    except ValueError:
                        try:
                            from email.utils import parsedate_to_datetime
                            from datetime import datetime, timezone
                            when = parsedate_to_datetime(retry_after)
                            if when.tzinfo is None:
                                when = when.replace(tzinfo=timezone.utc)
                            delay = max(0, int((when - datetime.now(timezone.utc)).total_seconds()))
                        except (TypeError, ValueError):
                            delay = 0
                pause(delay or 60 * (attempt + 1))      # interruptible: the loop's first line then stops
                continue
            return answer.content if answer.ok else None
        stopped.set()
        raise Stopped()

    def one(item):
        printing, address = item
        if stopped.is_set() or cancelled():
            stopped.set()                  # asked to stop: nothing more is fetched, and what is done is written below
            return printing, None
        try:
            data = picture(printing, address)
        except Stopped:
            return printing, None
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) if data else None
        return printing, None if image is None else np.frombuffer(hash_card(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)), np.uint8)

    new = skipped = done = 0
    chunk_size = max(workers * 16, 64)
    pool = ThreadPoolExecutor(workers)
    try:
        for start in range(0, len(todo), chunk_size):
            if stopped.is_set() or cancelled():
                break
            chunk = todo[start:start + chunk_size]
            for printing, fingerprint in pool.map(one, chunk):
                done += 1
                if fingerprint is None:
                    if not cancelled():      # one left undone on request is not "without a picture"
                        skipped += 1
                else:
                    have[printing] = fingerprint
                    new += 1
                if progress is not None:
                    progress(done, len(todo))
                if done % 5000 == 0 and not stopped.is_set() and not cancelled():
                    write(directory, *_in_order(printings, have))
                    log(f"{done} of {len(todo)} done ({new} new, {skipped} without a picture)")
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    ids, hashes = _in_order(printings, have)
    write(directory, ids, hashes)
    if cancel is not None and cancel.is_set():
        log("Stopped on request. What was done is written; run it again and it resumes")
    elif stopped.is_set():
        log("Scryfall kept refusing: stopped. What was done is written; run it again later, it resumes")
    log(f"Index written: {len(ids)} printings ({new} new, {skipped} left out for want of a picture)")
    result = BuildResult((len(ids), new, skipped))
    result.reason = 'cancelled' if cancelled() else 'refused' if stopped.is_set() else 'done'
    return result


def _in_order(printings, have):
    """The printings that have a fingerprint, in the card database's order: (ids, list of 96-byte strings)"""
    ids = [printing for printing, _ in printings if printing in have]
    return ids, [bytes(have[printing]) for printing in ids]


def main(argv=None):
    """scripts/build_image_index.py: python scripts/build_image_index.py [--images FOLDER] [--no-fetch] [--limit N] [--folder DATA_FOLDER]"""
    import argparse
    from card_scanner.config import Config
    from card_scanner.core.database import CardDatabase
    parser = argparse.ArgumentParser(description="Build the image index (a fingerprint of every printing's picture) in data/image_index/")
    parser.add_argument('--images', help="a folder of <Scryfall id>.jpg pictures to use before downloading")
    parser.add_argument('--no-fetch', action='store_true', help="do not download: only what --images has")
    parser.add_argument('--limit', type=int, default=0, help="stop after this many new printings (a trial run)")
    parser.add_argument('--folder', default=str(Config.DATA_DIR / 'image_index'), help="where the index is written")
    arguments = parser.parse_args(argv)
    try:
        result = build(CardDatabase().printing_images(), arguments.folder, arguments.images, not arguments.no_fetch, limit=arguments.limit)
    except BuildRunning as e:
        raise SystemExit(f"{e}. Wait for it to finish (or stop it), then run this again.")
    if result.reason == 'refused':
        raise SystemExit("Scryfall kept refusing, so the index is incomplete: what was done is written. Run this again later and it goes on from there.")


if __name__ == '__main__':
    main()
