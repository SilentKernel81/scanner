# capture.py
# One body of evidence per capture: what the reading has already worked out about this capture's picture, so that no step works it out again
# (docs/plans/READING_REVIEW_2026-10-21_PLAN.md, step 4: findings F1-F3). Before this, a card that ended with the AI had its picture searched up to
# three times (live_read, then contradicts / upright / verdict_of: two fingerprints and two passes over the whole index each, 18 ms median), the
# strip read twice for an OCR-confirmed card (live_read, then foil_marker.read), and the full-card OCR twice when the picture alone was not
# confirmed (_ocr_names, then _read_with_ocr) - all of it on the single OCR worker.
#
# It is GATHERING, not deciding: it keeps values, it computes nothing itself, and decide.py stays a pure function. It travels like the reading
# record (record.py): the thread working on a capture holds its CaptureEvidence as the current one (`using`; on the Job between the OCR and the AI
# worker), and the steps that compute something costly ask it first (`get`). A step run with no current capture (the shadow worker, the review
# screen's candidates, a test of one stage, a script) computes as it always did - so every caller that never heard of this is unchanged.
#
# A value is kept for ONE picture: the array object it was computed from (`is`, not equality - the arrays are never changed after capture, and
# the reading hands the same objects from step to step). Asked about another picture (the capture as drawn instead of the flat card, an image
# turned upright) it is computed afresh and not kept, so a value can never be given for a picture it was not made of.

import contextlib
import threading

_current = threading.local()     # .capture: the CaptureEvidence of the capture this thread is reading


class CaptureEvidence:
    """What has been worked out about one capture so far: {name: (the picture it is of, the value)}"""

    def __init__(self):
        self._values = {}
        self.computed = {}       # name -> how many times it was computed (not served from here) - for the tests that pin "once per capture"

    def get(self, name, picture, compute):
        """The value `name` of `picture`: kept from before, or compute() - kept when it returns, never when it raises (the next step tries again, as before)"""
        kept = self._values.get(name)
        if kept is not None and kept[0] is picture:
            return kept[1]
        value = compute()
        self.computed[name] = self.computed.get(name, 0) + 1
        if kept is None:                  # the first picture asked about is the capture's; another one is not kept (see the top of the file)
            self._values[name] = (picture, value)
        return value

    def remember(self, name, picture, value):
        """Keep a value a step worked out anyway (the strip's evidence live_read read) for the steps after it"""
        self._values[name] = (picture, value)

    def recall(self, name, picture):
        """(True, the value) when `name` is kept for this very picture, else (False, None)"""
        kept = self._values.get(name)
        return (True, kept[1]) if kept is not None and kept[0] is picture else (False, None)


def current():
    """The CaptureEvidence this thread is reading, or None"""
    return getattr(_current, 'capture', None)


@contextlib.contextmanager
def using(capture):
    """Make `capture` this thread's current one for the steps inside (None: nothing is kept); the previous one comes back after"""
    previous = current()
    _current.capture = capture
    try:
        yield capture
    finally:
        _current.capture = previous


def get(name, picture, compute):
    """current().get(...) when there is a current capture, else compute() - what a step calls"""
    capture = current()
    return capture.get(name, picture, compute) if capture is not None else compute()


def remember(name, picture, value):
    """Keep a value for the current capture, if there is one"""
    capture = current()
    if capture is not None:
        capture.remember(name, picture, value)


def recall(name, picture):
    """(True, value) when the current capture keeps `name` for this picture, else (False, None)"""
    capture = current()
    return capture.recall(name, picture) if capture is not None else (False, None)
