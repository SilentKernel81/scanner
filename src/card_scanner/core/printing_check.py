# printing_check.py
# Does the picture look like the printing the server resolved? A second opinion that does not go
# through the OCR or the AI - and, in SHADOW MODE (the only mode there is for now), one that only
# writes down where it disagrees and changes nothing.
#
# What it is for (docs/plans/IMAGE_MATCH_EXPERIMENT.md): the collector line does not always name the
# printing (a Secret Lair card prints the set and number of the card it is made from, so the server
# adds the base printing), and the vision AI misreads at least 12% of the cards it handles (a name and
# a number that agree with each other and with nothing in the picture).
#
# The rule: the capture and every printing in the image index are compared by their 96-byte fingerprints
# (image_index.py), in both orientations (a card upside down would otherwise match a wrong card with a
# clear lead: 2% of them did in the experiment). With R the printing the server resolved:
#       gap = similarity(best match) - similarity(R)
#   gap below AGREE_BELOW        'agree'     R looks like the picture as well as the best match does (a reprint with
#                                            the same art has almost the same fingerprint, so it agrees too)
#   gap from DISAGREE_FROM       'disagree'  the picture looks like something else: kind 'other_card' (the best match
#                                            is another card: a misread) or 'same_card' (another printing of the
#                                            same card: the printed line named the wrong printing)
#   between                      'weak'      written down, not counted as a disagreement
#   R not in the index           'not_indexed'  (a printing newer than the index)
# The two thresholds come from the experiment's data (scripts/experiments/image_match/calibrate_gap.py):
# among 10,677 pictures with labels not known to be wrong the gap was 0 for 95%, and 0.036 at the 99th
# percentile; the 20 labels known to be wrong all had a gap of 0.089 or more. At 0.06, 0.68% of the right
# labels would have been flagged (some of those are probably labels that were wrong too). 20 wrong cases
# is a small sample: that is what shadow mode is for. Measure on live scans before it changes anything.
#
# Shadow mode writes one JSON line per card it looked at to data/logs/printing_check.jsonl, and a line in
# the log for each 'disagree'. `python scripts/printing_check_report.py` sums them up. The work is done by
# one background thread behind a small queue: a capture never waits for it, and nothing it does can fail one.

import json
import logging
import queue
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from card_scanner.core import image_index

logger = logging.getLogger('ai')

AGREE_BELOW = 0.02
DISAGREE_FROM = 0.06
QUEUE_SIZE = 200                  # captures waiting to be looked at: more are dropped (it is only a second opinion)
LOG_LIMIT = 10 * 1024 * 1024      # the log is renamed to .1 beyond this (the old .1 goes)


def verdict_for(gap):
    """'agree' / 'weak' / 'disagree' for a gap (best match's similarity minus the resolved printing's)"""
    if gap < AGREE_BELOW:
        return 'agree'
    return 'disagree' if gap >= DISAGREE_FROM else 'weak'


class PrintingCheck:
    """Looks at the cards the server resolved. mode: 'shadow' (log only) or 'off'."""

    def __init__(self, database, index_folder, log_file, mode='shadow'):
        self.database = database
        self.index_folder = Path(index_folder)
        self.log_file = Path(log_file)
        self.mode = mode if mode in ('shadow', 'off') else 'shadow'
        self._index = None
        self._index_stamp = 'not looked at yet'   # meta.json's modification time when the index was loaded (None: no index)
        self._queue = queue.Queue(QUEUE_SIZE)
        self._thread = None
        self._lock = threading.Lock()       # starting the thread, loading the index
        self._said_no_index = False
        self._failures = 0

    # ------------------------------------------------------------------ the index

    def index(self):
        """The image index, loaded when first needed and again when it was rebuilt; None when there is none (said once in the log)"""
        meta = self.index_folder / image_index.MANIFEST      # replaced whenever a new generation is published
        try:
            stamp = meta.stat().st_mtime_ns
        except OSError:
            stamp = None
        with self._lock:
            if stamp != self._index_stamp:     # first call, or the index was rebuilt (an unusable one is not tried again until it changes)
                self._index = image_index.ImageIndex.load(self.index_folder) if stamp is not None else None
                self._index_stamp = stamp
                if self._index is not None:
                    logger.info(f"Printing check: image index loaded ({len(self._index)} printings, built {self._index.built})")
            if self._index is None and not self._said_no_index:
                self._said_no_index = True
                logger.info("Printing check: no image index (build one with scripts/build_image_index.py): nothing is checked")
            return self._index

    # ------------------------------------------------------------------ the check

    def check(self, rgb, printing_id):
        """
        The verdict for a capture (RGB array) and the printing the server resolved for it: a dict, or None without an index.
        {'verdict', 'kind', 'gap', 'lead', 'rotated', 'best': {'id', 'similarity'}, 'resolved_similarity'}
        """
        index = self.index()
        if index is None or not len(index):
            return None
        upright = index.distances(image_index.hash_card(rgb))
        turned = index.distances(image_index.hash_card(cv2.rotate(rgb, cv2.ROTATE_180)))
        rotated = int(turned.min()) < int(upright.min())      # the card was put in upside down
        distances = turned if rotated else upright
        best_two = np.argpartition(distances, 1)[:2] if len(distances) > 1 else np.array([0, 0])
        best_two = best_two[np.argsort(distances[best_two])]
        best = int(best_two[0])
        sims = image_index.similarity(distances[best_two])
        result = {'rotated': rotated, 'best': {'id': index.ids[best], 'similarity': round(float(sims[0]), 4)},
                  'lead': round(float(sims[0] - sims[1]), 4) if len(best_two) > 1 else None}
        row = index.row(printing_id)
        if row is None:
            return {**result, 'verdict': 'not_indexed', 'kind': None, 'gap': None, 'resolved_similarity': None}
        resolved_similarity = float(image_index.similarity(distances[row]))
        gap = float(sims[0]) - resolved_similarity
        result.update(gap=round(gap, 4), resolved_similarity=round(resolved_similarity, 4), verdict=verdict_for(gap), kind=None)
        if result['verdict'] != 'agree':
            result['kind'] = self._kind(printing_id, index.ids[best])
        return result

    def _kind(self, resolved_id, best_id):
        """'same_card' when the best match is another printing of the resolved card, else 'other_card'"""
        try:
            resolved, best = self.database.get_card_by_id(resolved_id), self.database.get_card_by_id(best_id)
            same = bool(resolved and best and resolved.get('oracle_id') and resolved.get('oracle_id') == best.get('oracle_id'))
        except Exception:
            return None
        return 'same_card' if same else 'other_card'

    # ------------------------------------------------------------------ shadow mode

    def observe(self, image_path, card, station=None):
        """
        Shadow mode: look at a capture and the card the server resolved for it (a card dict with 'id'), in the background.
        Returns at once and never raises: the capture goes on whatever happens here.
        """
        if self.mode != 'shadow' or not image_path or not card or not card.get('id'):
            return
        try:
            self._start()
            self._queue.put_nowait((str(image_path), dict(card), station))
        except queue.Full:
            pass                          # busy: this one is not looked at
        except Exception as e:
            logger.debug(f"Printing check could not queue a capture: {e}")

    def _start(self):
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, daemon=True, name="Printing-check")
                self._thread.start()

    def _work(self):
        while True:
            job = self._queue.get()
            try:
                self._look(*job)
            except Exception as e:        # a bad picture, a full disk: written down, never raised (a few times, then silent)
                self._failures += 1
                if self._failures <= 5:
                    logger.warning(f"Printing check failed on {Path(job[0]).name}: {e}")
            finally:
                self._queue.task_done()

    def wait(self, timeout=30):
        """Until what is queued has been looked at (tests, scripts); False if it takes longer than timeout"""
        done = threading.Event()
        threading.Thread(target=lambda: (self._queue.join(), done.set()), daemon=True).start()
        return done.wait(timeout)

    def _look(self, image_path, card, station):
        image = cv2.imread(image_path)
        if image is None:                 # the picture is gone (cleaned up, or a path that does not exist)
            return
        result = self.check(cv2.cvtColor(image, cv2.COLOR_BGR2RGB), card['id'])
        if result is None:
            return
        best = self.database.get_card_by_id(result['best']['id']) or {}
        record = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'station': station, 'capture': Path(image_path).name,
                  'verdict': result['verdict'], 'kind': result['kind'], 'gap': result['gap'], 'lead': result['lead'],
                  'rotated': result['rotated'],
                  'resolved': {'id': card['id'], 'name': card.get('name'), 'set': card.get('set_code'), 'number': card.get('number'),
                               'similarity': result['resolved_similarity']},
                  'best': {'id': result['best']['id'], 'name': best.get('name'), 'set': best.get('set_code'),
                           'number': best.get('number'), 'similarity': result['best']['similarity']}}
        self._append(record)
        if result['verdict'] == 'disagree':
            logger.info(f"Printing check (shadow, nothing changed): {card.get('name')} [{card.get('set_code')} {card.get('number')}] was added, "
                        f"the picture looks like {best.get('name')} [{best.get('set_code')} {best.get('number')}] "
                        f"({result['kind']}, gap {result['gap']:.3f}, {Path(image_path).name})")

    def _append(self, record):
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.log_file.stat().st_size > LOG_LIMIT:
                self.log_file.replace(self.log_file.with_name(self.log_file.name + '.1'))
        except OSError:
            pass
        with self.log_file.open('a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')


# ---------------------------------------------------------------------------------------------
# The report (scripts/printing_check_report.py)
# ---------------------------------------------------------------------------------------------

def report(log_file, show=15, out=print):
    """Sums up the shadow log: how many cards, how many agreed, and the latest disagreements (name, printing, what it looks like)"""
    log_file = Path(log_file)
    if not log_file.is_file():
        out(f"No {log_file.name} yet: shadow mode has looked at nothing (is there an image index, and has a card been scanned since?)")
        return
    records = []
    for line in log_file.read_text(encoding='utf-8').splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            pass
    verdicts = {}
    for r in records:
        verdicts[r['verdict']] = verdicts.get(r['verdict'], 0) + 1
    out(f"{len(records)} cards looked at ({records[0]['time'] if records else '-'} to {records[-1]['time'] if records else '-'}): "
        + ', '.join(f"{n} {v}" for v, n in sorted(verdicts.items(), key=lambda kv: -kv[1])))
    kinds = {}
    for r in records:
        if r['verdict'] == 'disagree':
            kinds[r['kind']] = kinds.get(r['kind'], 0) + 1
    if kinds:
        out("disagreements by kind: " + ', '.join(f"{n} {k}" for k, n in kinds.items())
            + "   (other_card: probably a misread; same_card: the printed line names another printing)")
    out(f"\nthe latest {show} disagreements:")
    for r in [r for r in records if r['verdict'] == 'disagree'][-show:]:
        a, b = r['resolved'], r['best']
        out(f"  {r['time']} {r['capture']}: added {a['name']} [{a['set']} {a['number']}], the picture looks like {b['name']} [{b['set']} {b['number']}] "
            f"- {r['kind']}, gap {r['gap']:.3f}, lead {r['lead']}{', card upside down' if r['rotated'] else ''}")


def main(argv=None):
    """scripts/printing_check_report.py [--show N]"""
    import argparse
    from card_scanner.config import Config
    parser = argparse.ArgumentParser(description="What the printing check (shadow mode) has found")
    parser.add_argument('--show', type=int, default=15, help="how many of the latest disagreements to list")
    report(Config.DATA_DIR / 'logs' / 'printing_check.jsonl', parser.parse_args(argv).show)


if __name__ == '__main__':
    main()
