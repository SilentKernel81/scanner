"""
One body of evidence per capture (core/reading/capture.py; docs/plans/READING_REVIEW_2026-10-21_PLAN.md, step 4): the picture search, the strip and the full-card OCR are
worked out ONCE per capture and changed no decision (the reading tests pass unedited); and an upside-down OCR-confirmed card's foil marker comes from the strip of the card
turned upright (the blind spot of finding F2).

    venv/bin/python -m unittest discover tests -p test_reading_capture.py
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))                 # the helpers of test_printing_check.py / test_reading_evidence.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from card_scanner.core import foil_marker, identification, image_index, printing_check  # noqa: E402
from card_scanner.core.reading import capture  # noqa: E402
from test_printing_check import FakeReader, degraded, synthetic_card, write_pictures  # noqa: E402
from test_reading_evidence import READING, Cards, FakeEvidence, FakeGame, strip_lines  # noqa: E402


class TheMemo(unittest.TestCase):
    def test_a_value_is_computed_once_for_its_picture_and_never_given_for_another(self):
        picture, other = np.zeros((4, 4, 3), np.uint8), np.zeros((4, 4, 3), np.uint8)     # equal, but not the same array
        calls = []
        with capture.using(capture.CaptureEvidence()) as one:
            self.assertEqual(capture.get('x', picture, lambda: calls.append(1) or 'a'), 'a')
            self.assertEqual(capture.get('x', picture, lambda: calls.append(1) or 'b'), 'a')
            self.assertEqual(capture.get('x', other, lambda: calls.append(1) or 'c'), 'c')     # another picture: computed, and not kept
            self.assertEqual(capture.get('x', picture, lambda: calls.append(1) or 'd'), 'a')
        self.assertEqual((len(calls), one.computed['x']), (2, 2))
        self.assertEqual(capture.get('x', picture, lambda: 'e'), 'e')                              # no current capture: computed every time

    def test_a_failure_is_not_kept(self):
        picture = np.zeros((4, 4, 3), np.uint8)
        with capture.using(capture.CaptureEvidence()):
            with self.assertRaises(RuntimeError):
                capture.get('x', picture, mock.Mock(side_effect=RuntimeError('down')))
            self.assertEqual(capture.get('x', picture, lambda: 'later'), 'later')


class Upright(FakeReader):
    """A strip reader that remembers the pictures it was given"""

    def __init__(self, lines):
        super().__init__(lines)
        self.images = []

    def read_lines(self, image):
        self.images.append(image)
        return super().read_lines(image)


class OnTheSyntheticIndex(unittest.TestCase):
    """PrintingCheck on the synthetic index of test_reading_evidence.py"""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        (root / 'pictures').mkdir()
        write_pictures(root / 'pictures', range(6))
        image_index.build([(f'p{n}', None) for n in range(6)], root / 'index', image_dir=root / 'pictures', fetch=False, log=lambda m: None)
        self.check = printing_check.PrintingCheck(Cards(), root / 'index', root / 'log.jsonl', 'off', 'off')
        self.picture = degraded(synthetic_card(3))

    def test_the_picture_is_searched_once_per_capture(self):
        with capture.using(capture.CaptureEvidence()) as one, mock.patch.object(self.check, '_search_now', wraps=self.check._search_now) as search:
            self.check.live_read(self.picture, None, FakeReader(strip_lines(3)))
            self.check.contradicts(self.picture, 'p3')
            self.check.disagrees(self.picture, 'p3')
            self.check.verdict_of(self.picture, 'p3')
            self.check.upright(self.picture)
        self.assertEqual(search.call_count, 1)
        self.assertEqual(one.computed['search'], 1)

    def test_without_a_capture_every_ask_searches_as_before(self):
        with mock.patch.object(self.check, '_search_now', wraps=self.check._search_now) as search:
            self.check.verdict_of(self.picture, 'p3')
            self.check.verdict_of(self.picture, 'p3')
        self.assertEqual(search.call_count, 2)

    def test_the_answers_are_the_same_with_and_without_the_memo(self):
        alone = (self.check.live_read(self.picture, None, FakeReader(strip_lines(3))), self.check.verdict_of(self.picture, 'p4'),
                 self.check.contradicts(self.picture, 'p4'))
        with capture.using(capture.CaptureEvidence()):
            kept = (self.check.live_read(self.picture, None, FakeReader(strip_lines(3))), self.check.verdict_of(self.picture, 'p4'),
                    self.check.contradicts(self.picture, 'p4'))
        self.assertEqual(alone, kept)

    def test_the_strips_printing_is_looked_up_once_for_both_guards(self):
        database = mock.Mock(wraps=Cards())
        self.check.database = database
        self.check.live_read(self.picture, None, FakeReader(strip_lines(9)), None, True)      # the picture alone, a strip naming a printing the data lacks
        numbers = [c for c in database.method_calls if c[0] == 'get_card_by_set_number']
        self.assertEqual(len(numbers), 1)

    def test_an_upside_down_ocr_confirmed_cards_marker_comes_from_its_upright_strip(self):
        turned = cv2.rotate(self.picture, cv2.ROTATE_180)
        reader = Upright(strip_lines(3, symbol='★'))
        ident = identification.Identification.__new__(identification.Identification)
        ident.log_callback = None
        ident.ocr_enabled = True
        ident.card_ocr = reader
        ident.foil_marker = mock.Mock()               # foil_marker.read would read the card as given: it must not be asked
        ident.evidence = self.check
        with capture.using(capture.CaptureEvidence()):
            self.check.live_read(turned, None, reader, ident.foil_marker, False)
            foil = ident._read_foil_locally(turned)
        self.assertEqual(foil, 'foil')
        ident.foil_marker.read.assert_not_called()
        self.assertEqual(len(reader.images), 1)       # one read of the strip, of the card turned back upright
        np.testing.assert_array_equal(reader.images[0], foil_marker.strip_of(cv2.rotate(turned, cv2.ROTATE_180)))


class InIdentification(unittest.TestCase):
    """The full-card OCR is read once per capture"""

    def setUp(self):
        self.ident = identification.Identification.__new__(identification.Identification)
        self.ident.log_callback = None
        self.ident.ocr_enabled = True
        self.ident.card_identifier = None
        self.ident.foil_marker = None
        self.ident.settings = mock.Mock(get=lambda key, default=None: {'reading_mode': 'evidence'}.get(key, default))
        self.ident.evidence = FakeEvidence({**READING, 'path': 'image', 'lead': 0.0625})      # the picture alone decided, not sure enough to need no name
        self.ident._ai_foil_check = lambda foil_image: False
        self.ident.card_ocr = mock.Mock()
        self.ident.card_ocr.read_card.side_effect = lambda image, game: {'name': 'Frodo', 'collector_number': '', 'set_code': '', 'foil': 'unknown'}
        patcher = mock.patch.object(identification, 'games')
        games = patcher.start()
        self.addCleanup(patcher.stop)
        games.active.side_effect = lambda: FakeGame()
        games.active_id.return_value = 'mtg'
        self.image = np.zeros((100, 70, 3), np.uint8)

    def test_a_picture_alone_the_ocr_did_not_confirm_reads_the_whole_card_once(self):
        info = self.ident.identify(self.image, self.image)
        self.assertEqual(info['name'], 'Frodo')                  # read by the OCR instead, as before
        self.ident.card_ocr.read_card.assert_called_once()

    def test_the_read_handed_on_is_a_copy(self):
        with capture.using(capture.CaptureEvidence()):
            first = self.ident._read_card(self.image)
            first['foil'] = 'foil'
            self.assertEqual(self.ident._read_card(self.image)['foil'], 'unknown')


if __name__ == '__main__':
    unittest.main()
