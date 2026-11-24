"""
Which way up a card is read, and the card inside a picture that is more than the card (core/printing_check.py: live_read, _oriented, flat_card, saw_a_set_line;
core/image_index.py: card_regions; server/stations_api.py: station_capture). Synthetic cards and a real index in a temporary folder: no capture and no real data.

    venv/bin/python -m unittest discover tests -p test_card_orientation.py
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from card_scanner.core import image_index, printing_check  # noqa: E402
from card_scanner.core.printing_check import PrintingCheck, saw_a_set_line  # noqa: E402


def card(seed, height=700):
    """A made-up dark-bordered card, with a picture of its own: no two seeds alike, and not the same upside down"""
    width = int(height / image_index.CARD_RATIO)
    random = np.random.default_rng(seed)
    blocks = random.integers(40, 255, (14, 10, 3), dtype=np.uint8)
    face = cv2.resize(blocks, (width, height), interpolation=cv2.INTER_NEAREST)
    face[:int(height * .35)] = (face[:int(height * .35)] * 0.45).astype(np.uint8)        # top and bottom differ on every card
    border = max(6, width // 22)
    face[:border], face[-border:], face[:, :border], face[:, -border:] = 18, 18, 18, 18
    return face


def on_a_tray(picture, left=60, top=40, right=30, bottom=50, under=12):
    """A card on a light tray, with the border of the card below it sticking out to the right"""
    height, width = picture.shape[:2]
    out = np.full((height + top + bottom, width + left + right + under, 3), 190, np.uint8)
    out[top + 8:top + height + 8, left + under:left + width + under] = 18
    out[top:top + height, left:left + width] = picture
    return out


class Index(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cards = {f'p{n}': card(n) for n in range(1, 13)}
        image_index.write(folder.name, list(self.cards), [image_index.hash_card(c) for c in self.cards.values()])
        self.check = PrintingCheck.__new__(PrintingCheck)
        self.check.index_folder = Path(folder.name)
        self.check._lock = __import__('threading').Lock()
        self.check._index, self.check._index_stamp, self.check._said_no_index = None, object(), False
        self.check.database = mock.Mock(get_card_by_id=lambda printing: {'id': printing, 'name': printing, 'set_code': 'tst', 'number': printing[1:], 'oracle_id': printing,
                                                                          'finishes': ['nonfoil'], 'released_at': '2024-01-01', 'frame': '2015'} if printing else {})


class FlatCard(Index):
    def test_the_card_is_found_inside_the_area(self):
        found = self.check.flat_card(on_a_tray(self.cards['p3']))
        self.assertIsNotNone(found)
        distances = self.check.index().distances(image_index.hash_card(found))
        self.assertEqual(int(distances.argmin()), self.check.index().row('p3'))            # its own printing, first
        whole = self.check.index().distances(image_index.hash_card(on_a_tray(self.cards['p3'])))
        self.assertLess(int(distances.min()), int(whole[self.check.index().row('p3')]))     # and nearer than the area as drawn

    def test_a_picture_that_is_already_the_card_is_left_alone(self):
        self.assertIsNone(self.check.flat_card(self.cards['p3']))

    def test_no_index_or_a_failure_means_as_it_was(self):
        with mock.patch.object(self.check, 'index', return_value=None):
            self.assertIsNone(self.check.flat_card(on_a_tray(self.cards['p3'])))
        with mock.patch.object(image_index, 'card_regions', side_effect=RuntimeError('no')):
            self.assertIsNone(self.check.flat_card(on_a_tray(self.cards['p3'])))

    def test_something_that_looks_like_no_printing_is_not_taken_for_a_card(self):
        unknown = on_a_tray(np.random.default_rng(99).integers(0, 255, (700, 501, 3), dtype=np.uint8))
        with mock.patch.object(printing_check, 'FLAT_CARD_MIN_SIMILARITY', 0.95):
            self.assertIsNone(self.check.flat_card(unknown))

    def test_the_fingerprint_itself_is_not_changed_by_any_of_this(self):
        self.assertNotIn('card_regions', image_index.hash_card.__code__.co_names + image_index.prepare.__code__.co_names)

    def test_regions_stay_inside_the_picture(self):
        picture = on_a_tray(self.cards['p1'])
        height, width = picture.shape[:2]
        regions = image_index.card_regions(picture)
        self.assertTrue(regions)
        for x0, y0, x1, y1 in regions:
            self.assertTrue(0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height)

    def test_a_tiny_or_featureless_picture_gives_no_region(self):
        self.assertEqual(image_index.card_regions(np.zeros((10, 10, 3), np.uint8)), [])
        self.assertEqual(image_index.card_regions(np.full((400, 300, 3), 200, np.uint8)), [])


class Orientation(Index):
    """The strip reader sees a set line only on the side of the picture where `line_on` says it is"""

    def read(self, picture, seen, line=True):
        return {'number': '3', 'set': 'tst', 'symbol': 'non-foil', 'net': None} if line else {'number': '', 'set': '', 'symbol': 'unknown', 'net': None}

    def live(self, picture, close_call, line_when_turned):
        seen = []

        def strip(ocr, given, net=None):
            turned = not np.array_equal(given, picture)
            seen.append(turned)
            return self.read(given, seen, line=turned == line_when_turned)
        with mock.patch.object(printing_check, 'read_strip_evidence', strip), mock.patch.object(printing_check, 'orientation_established', return_value=not close_call):
            return self.check.live_read(picture, picture, object()), seen

    def test_a_close_call_is_read_as_captured(self):
        turned = cv2.rotate(self.cards['p3'], cv2.ROTATE_180)            # the search says "turned" for this picture
        _, seen = self.live(turned, close_call=True, line_when_turned=False)
        self.assertEqual(seen, [False])

    def test_a_sure_search_is_believed(self):
        turned = cv2.rotate(self.cards['p3'], cv2.ROTATE_180)
        reading, seen = self.live(turned, close_call=False, line_when_turned=True)
        self.assertEqual(seen, [True])
        self.assertTrue(reading['rotated'])

    def test_no_set_line_is_tried_the_other_way_and_the_candidates_follow(self):
        turned = cv2.rotate(self.cards['p3'], cv2.ROTATE_180)
        reading, seen = self.live(turned, close_call=True, line_when_turned=True)
        self.assertEqual(seen, [False, True])                            # as captured, then turned: where the line is
        self.assertTrue(reading['rotated'])
        self.assertEqual(reading['card']['id'], 'p3')

    def test_a_sure_search_with_no_line_is_checked_too(self):
        upright = self.cards['p3']
        with mock.patch.object(self.check, '_search', wraps=self.check._search) as search:
            def flipped(picture):
                found = PrintingCheck._search(self.check, picture)
                return self.check._oriented(found, True)
            search.side_effect = flipped
            _, seen = self.live(upright, close_call=False, line_when_turned=False)
        self.assertEqual(seen, [True, False])

    def test_without_ocr_the_search_decides_as_before(self):
        turned = cv2.rotate(self.cards['p3'], cv2.ROTATE_180)
        given = []

        def strip(ocr, picture, net=None):
            given.append(picture)
            return {'number': '', 'set': '', 'symbol': 'unknown', 'net': None}
        with mock.patch.object(printing_check, 'read_strip_evidence', strip), mock.patch.object(printing_check, 'orientation_established', return_value=False):
            self.check.live_read(turned, turned, None)
        self.assertEqual(len(given), 1)
        self.assertTrue(np.array_equal(given[0], self.cards['p3']))

    def test_cards_known_to_lie_one_way_are_never_turned_or_tried_the_other_way(self):
        self.check.cards_upright = lambda: True
        turned = cv2.rotate(self.cards['p3'], cv2.ROTATE_180)            # looks like p3 only if turned: with the setting on it is not
        search = self.check._search(turned)
        self.assertFalse(search['rotated'])
        given = []

        def strip(ocr, picture, net=None):
            given.append(picture)
            return {'number': '', 'set': '', 'symbol': 'unknown', 'net': None}
        with mock.patch.object(printing_check, 'read_strip_evidence', strip):
            self.check.live_read(turned, turned, object())
        self.assertEqual(len(given), 1)
        self.assertTrue(np.array_equal(given[0], turned))

    def test_the_setting_failing_means_looking_both_ways(self):
        self.check.cards_upright = mock.Mock(side_effect=RuntimeError('no desk'))
        self.assertTrue(self.check._search(cv2.rotate(self.cards['p3'], cv2.ROTATE_180))['rotated'])

    def test_what_counts_as_a_set_line(self):
        self.assertFalse(saw_a_set_line({'number': '97', 'set': '', 'symbol': 'unknown', 'net': None}))      # a number alone: any text on the wrong end
        self.assertTrue(saw_a_set_line({'number': '', 'set': 'ltr', 'symbol': 'unknown', 'net': None}))
        self.assertTrue(saw_a_set_line({'number': '', 'set': '', 'symbol': 'foil', 'net': None}))
        self.assertTrue(saw_a_set_line({'number': '', 'set': '', 'symbol': 'unknown', 'net': 0.1}))


if __name__ == '__main__':
    unittest.main()
