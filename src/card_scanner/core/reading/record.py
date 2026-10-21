# record.py
# The reading log: one JSON line per capture in data/logs/reading.jsonl, written down by the mode that ACTS (reading.mode 'evidence') as well as by
# 'cascade', so that the thresholds the picture's decisions rest on can be judged on live scans (docs/plans/READING_REVIEW_2026-10-21_PLAN.md, step 2).
# It is NOT the shadow log (printing_check.jsonl): that one keeps comparing the picture with what the OCR / the AI resolved, and skips every card the
# picture decided itself (it would compare the choice with itself). This one records what each step of the reading saw and did - the picture's five
# candidates, the strip, rule A2's answer, the guard that refused a decision (by its name), the full-card OCR, the AI's answer and its verdict - and
# what became of the card (added / review / shown).
#
# How a record travels. A capture's reading runs on up to three threads (the OCR worker, an AI worker, the thread that hands results back - or all in
# the caller's thread for Identification.identify), and the steps that know something (PrintingCheck.live_read, the guards in Identification) are
# called with signatures the tests patch. So the record is not handed down as an argument: the thread working on a capture holds it as its CURRENT
# record (`using`), and every step adds to whatever record is current (`note`, `refuse`) - a step with no current record (a test of one stage, a script,
# the shadow worker) adds to nothing. Once the reading is over the record is HANDED OVER to the code that routes the card (`hand_over`, then
# `complete` from captures.announce_and_route, which knows the outcome), or written without an outcome when nobody takes it.
#
# Never raises, never blocks a card: every public function here catches what it could throw, and a failed write is a debug line.

import contextlib
import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger('ai')

# The log is renamed to .1 beyond this (the old .1 goes) - as printing_check.jsonl (printing_check.LOG_LIMIT)
LOG_LIMIT = 10 * 1024 * 1024

# The names of the guards that can stop a decision of the picture (the 'refused' field). In the order the reading meets them.
IMAGE_ALONE_OFF = 'image_alone_off'              # the picture alone decided, and the person has not allowed that (reading.accept_image_alone)
INDEX_INCOMPLETE = 'index_incomplete'            # ... allowed, but the index lacks printings of the card data (PrintingCheck.missing_printings > 0)
CARD_NOT_IN_DATA = 'card_not_in_data'            # the decided printing is in the index and no longer in the card data
BELOW_IMAGE_FLOOR = 'below_image_floor'          # the picture alone, and it looks too little like its best match (printing_check.IMAGE_MIN_SIMILARITY)
BELOW_STRONG_FLOOR = 'below_strong_floor'        # the strip named the printing, and the picture looks too little like it (STRONG_MIN_SIMILARITY)
STRIP_NAMES_UNINDEXED = 'strip_names_unindexed'  # the picture alone, and the strip names a printing the card data has and the index lacks
STRIP_NAMES_UNKNOWN = 'strip_names_unknown'      # the picture alone, and the strip names a printing the card data does not have
OCR_DID_NOT_CONFIRM = 'ocr_did_not_confirm'      # the picture alone, with OCR on, and the full-card OCR did not confirm that printing (printing_check.ocr_agrees)
GUARDS = (IMAGE_ALONE_OFF, INDEX_INCOMPLETE, CARD_NOT_IN_DATA, BELOW_IMAGE_FLOOR, BELOW_STRONG_FLOOR, STRIP_NAMES_UNINDEXED, STRIP_NAMES_UNKNOWN,
          OCR_DID_NOT_CONFIRM)

_current = threading.local()     # .record: the record of the capture this thread is reading; .handed: one read here and handed to its router


class ReadingLog:
    """The file the records go to (data/logs/reading.jsonl), renamed to .1 beyond LOG_LIMIT. One lock: records come from several threads"""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def new(self, **fields):
        """A record for one capture, to be filled by the reading and written once (ReadingRecord)"""
        return ReadingRecord(self, **fields)

    def append(self, line):
        """Write one JSON line. Never raises: a full disk or a folder that cannot be made is a debug line, the card goes on"""
        try:
            text = json.dumps(line, ensure_ascii=False, default=_plain)
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if self.path.stat().st_size > LOG_LIMIT:
                        self.path.replace(self.path.with_name(self.path.name + '.1'))
                except OSError:
                    pass                      # no file yet
                with self.path.open('a', encoding='utf-8') as f:
                    f.write(text + '\n')
        except Exception as e:
            logger.debug(f"The reading log could not be written: {e}")

    def review_pick(self, capture, picked, candidates, station=None, capture_key=None):
        """
        A review item was settled by the person picking a printing: which one, and where it stood among the picture's candidates the review screen showed
        (scan_events.handle_review_open). rank 0: the picture's best match was right (a guard sent it to review); None: the picture had it wrong, or showed
        nothing - both what the thresholds need. One line of its own, joined to the capture's record by capture_key (the capture's uid; the review keeps its
        picture under a name of its own, `capture` here is that name).
        """
        try:
            ids = [c for c in (candidates or []) if c]
            self.append({'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'review': True, 'station': station, 'capture': capture, 'capture_key': capture_key, 'picked': picked,
                         'candidates': ids, 'rank': ids.index(picked) if picked in ids else None})
        except Exception as e:
            logger.debug(f"The review's pick could not be recorded: {e}")


class ReadingRecord:
    """What one capture's reading saw and did (the fields: see docs/PROGRAM_DOCUMENTATION.md, "The reading log"). Written once (write)"""

    def __init__(self, log, **fields):
        self.log = log
        self.written = False
        self.started = time.monotonic()
        self.data = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'station': None, 'capture': None, **fields,
                     'seconds': {}, 'picture': None, 'strip': None, 'decision': None, 'refused': None, 'ocr': None, 'ai': None, 'result': None}

    def note(self, section, **values):
        """Add values to a section ('picture', 'ocr', ...; a section that is None becomes a dict). The top level when section is None"""
        if section is None:
            self.data.update(values)
            return
        if not isinstance(self.data.get(section), dict):
            self.data[section] = {}
        self.data[section].update(values)

    def refuse(self, guard, **details):
        """The guard (one of GUARDS) that stopped the picture's decision; details ('ocr_name', 'ocr_line') beside it. A later refusal replaces an earlier one"""
        self.data['refused'] = guard
        for key, value in details.items():
            self.data[f'refused_{key}'] = value

    def took(self, step, seconds):
        """Seconds a step took ('picture', 'ocr', 'ai'); a step done twice adds up"""
        self.data['seconds'][step] = round(self.data['seconds'].get(step, 0) + seconds, 3)

    def finish(self, card_info, outcome=None, capture=None, station=None, capture_key=None):
        """What was read (card_info, as Identification returns it - None: nothing) and, when the router knows it, what became of the card"""
        info = card_info if isinstance(card_info, dict) else {}
        result = {'reader': info.get('reader') or ('ai' if info else None), 'printing_id': info.get('printing_id'), 'name': info.get('name'),
                  'number': info.get('collector_number'), 'set': info.get('set_code'), 'foil': info.get('foil')} if info else {'reader': None}
        if outcome is not None:
            result['outcome'] = outcome.get('status')
            if outcome.get('reason'):
                result['reason'] = outcome['reason']
        self.data['result'] = result
        if capture is not None:
            self.data['capture'] = capture
        if station is not None:
            self.data['station'] = station
        if capture_key is not None:
            self.data['capture_key'] = capture_key      # the capture's uid (pending.py): a review item carries it too (review.py: capture_key)

    def write(self):
        """Once: a second call (the router completed it, then the thread that handed it over looks again) writes nothing"""
        if self.written:
            return
        self.written = True
        self.data['seconds']['total'] = round(time.monotonic() - self.started, 3)
        self.log.append(self.data)


def _plain(value):
    """JSON for what json does not know: NumPy numbers and booleans (the similarities), anything else as its text"""
    for kind in (float, int, bool):
        try:
            if hasattr(value, 'item'):
                return value.item()
            return kind(value)
        except (TypeError, ValueError):
            continue
    return str(value)


# ---------------------------------------------------------------------------------------------
# The current record of a thread (see the top of the file)
# ---------------------------------------------------------------------------------------------

def current():
    """The record this thread is filling, or None"""
    return getattr(_current, 'record', None)


@contextlib.contextmanager
def using(record):
    """Make `record` this thread's current one for the steps run inside (None: nothing is recorded); the previous one comes back after"""
    previous = current()
    _current.record = record
    try:
        yield record
    finally:
        _current.record = previous


def note(section, **values):
    """Add to the current record's section, if there is a current record. Never raises"""
    record = current()
    if record is None:
        return
    try:
        record.note(section, **values)
    except Exception as e:
        logger.debug(f"Reading log: {section} not noted: {e}")


def refuse(guard, **details):
    """Name the guard that stopped the picture's decision in the current record, if there is one. Never raises"""
    record = current()
    if record is None:
        return
    try:
        record.refuse(guard, **details)
    except Exception as e:
        logger.debug(f"Reading log: refusal not noted: {e}")


def refused():
    """The guard named in the current record so far, or None"""
    record = current()
    return record.data.get('refused') if record is not None else None


@contextlib.contextmanager
def timed(step):
    """Add the seconds of the block to the current record's step (nothing without a current record)"""
    start = time.monotonic()
    try:
        yield
    finally:
        record = current()
        if record is not None:
            try:
                record.took(step, time.monotonic() - start)
            except Exception:
                pass


def hand_over(record):
    """
    The reading of `record` is over, in this thread, and the code that routes the card runs next in it (Identification.identify, then the station's route,
    or the results thread calling on_done): `complete` there writes it with the outcome. A record handed over before and never completed is written now, without one.
    """
    flush_handed()
    _current.handed = record


def flush_handed():
    """Write the record handed over in this thread and not completed (no outcome), and forget it. Never raises"""
    record = getattr(_current, 'handed', None)
    _current.handed = None
    if record is not None:
        try:
            record.write()
        except Exception as e:
            logger.debug(f"Reading log: a record was not written: {e}")


def complete(card_info, outcome, capture=None, station=None, capture_key=None):
    """The router (captures.announce_and_route): what became of the card read last in this thread - its record finished and written. Never raises"""
    record = getattr(_current, 'handed', None)
    _current.handed = None
    if record is None:
        return
    try:
        record.finish(card_info, outcome, capture, station, capture_key)
        record.write()
    except Exception as e:
        logger.debug(f"Reading log: a record was not completed: {e}")
