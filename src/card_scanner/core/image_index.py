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


TABLE_COLUMNS = ('id', 'set', 'number', 'oracle_id', 'finishes', 'name')


def printings_table(ids, facts):
    """
    What a phone needs of each printing of the index besides its fingerprint, as a gzipped tab-separated text (first line the column names, then one row per
    id, in the index's order): id, set code, collector number, oracle id, finishes ('nonfoil,foil,etched'), name. `facts` is database.printing_facts(). A
    printing the card data no longer has is left out (the phone joins by id, never by position), and the same input always gives the same bytes (no time
    in the gzip header), so its checksum says whether anything changed.
    """
    def clean(value):
        return str(value or '').replace('\t', ' ').replace('\n', ' ').replace('\r', ' ')

    lines = ['\t'.join(TABLE_COLUMNS)]
    for printing in ids:
        fact = facts.get(printing)
        if fact:
            name, set_code, number, oracle_id, finishes = fact
            lines.append('\t'.join(clean(value) for value in (printing, (set_code or '').lower(), number, oracle_id, finishes, name)))
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


def write(directory, ids, hashes):
    """
    Publishes an index as a new generation: its two files first (each whole or not at all, nothing points at them yet), then the manifest
    - one rename, the only step that publishes. Whatever happens before that rename leaves the previous generation current and whole.
    Afterwards everything except this generation and the one before it is removed: what an interrupted earlier build left, and the
    files of the older format (hashes.bin, ids.txt, meta.json).
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
        manifest = {'descriptor': DESCRIPTOR, 'count': len(ids), 'hash_bytes': HASH_BYTES, 'built': time.strftime('%Y-%m-%d %H:%M:%S'),
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
                pause(int(answer.headers.get('Retry-After', 0) or 0) or 60 * (attempt + 1))      # interruptible: the loop's first line then stops
                continue
            return answer.content if answer.ok else None
        stopped.set()
        raise Stopped()

    def one(item):
        printing, address = item
        if cancel is not None and cancel.is_set():
            stopped.set()                  # asked to stop: nothing more is fetched, and what is done is written below
            return printing, None
        try:
            data = picture(printing, address)
        except Stopped:
            return printing, None
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) if data else None
        return printing, None if image is None else np.frombuffer(hash_card(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)), np.uint8)

    new = skipped = 0
    with ThreadPoolExecutor(workers) as pool:
        for n, (printing, fingerprint) in enumerate(pool.map(one, todo), 1):
            if fingerprint is None:
                if not (cancel is not None and cancel.is_set()):      # one left undone on request is not "without a picture"
                    skipped += 1
            else:
                have[printing] = fingerprint
                new += 1
            if progress is not None:
                progress(n, len(todo))
            if n % 5000 == 0:      # a checkpoint: a stopped or crashed run keeps what it did
                write(directory, *_in_order(printings, have))
                log(f"{n} of {len(todo)} done ({new} new, {skipped} without a picture)")
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
