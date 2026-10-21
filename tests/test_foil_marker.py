"""
The foil marker read by a small network (core/foil_marker.py) and its place in Identification (core/identification.py): what is asked
first - the OCR's marker, then the network, then the AI's foil check - and that a failing network never loses a card.

The network's accuracy is measured elsewhere (docs/plans/IMAGE_MATCH_EXPERIMENT.md: 10,501 set lines, tested on cards it never
saw); here: the shipped file loads, gives the answers it gave when it was exported on 16 real set-line crops
(tests/data/foil_marker_crops.npz - a collector line, no card art), and the order of the steps. No OCR reader, no card database.

    venv/bin/python -m unittest discover tests -p test_foil_marker.py
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

import numpy as np  # noqa: E402

from card_scanner.core import foil_marker, identification  # noqa: E402

FIXTURE = Path(__file__).with_name('data') / 'foil_marker_crops.npz'


class TheFile(unittest.TestCase):
    def test_the_network_is_beside_the_module(self):
        # paths.py's one exception: a file that names another lying beside it - the server image and the AppImage copy the package
        self.assertTrue(foil_marker.MODEL_FILE.is_file())
        self.assertEqual(foil_marker.MODEL_FILE.parent, Path(foil_marker.__file__).parent)
        self.assertLess(foil_marker.MODEL_FILE.stat().st_size, 2 * 1024 * 1024)

    def test_off_loads_nothing_and_on_loads_it(self):
        self.assertIsNone(foil_marker.load(False))
        self.assertIsInstance(foil_marker.load(True), foil_marker.FoilMarker)

    def test_a_missing_or_broken_file_means_no_network_not_a_failed_start(self):
        with mock.patch.object(foil_marker, 'MODEL_FILE', Path('/nonexistent/foil_net.onnx')):
            self.assertIsNone(foil_marker.load(True))
        broken = Path(__file__)          # a file that is no ONNX model
        with mock.patch.object(foil_marker, 'MODEL_FILE', broken):
            self.assertIsNone(foil_marker.load(True))


class TheNetwork(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.marker = foil_marker.load(True)
        cls.fixture = np.load(FIXTURE)

    def test_it_gives_the_exported_answers_on_real_set_lines(self):
        # 16 crops of cards it never trained on, each chosen because it was sure of them (> 0.97 foil, < 0.03 regular) when exported:
        # a wrong normalisation or a changed file would flip or blur them
        crops, foil, expected = self.fixture['crops'], self.fixture['foil'], self.fixture['probability']
        self.assertEqual(len(crops), 16)
        for crop, is_foil, want in zip(crops, foil, expected):
            got = self.marker.probability(crop)
            self.assertAlmostEqual(got, float(want), delta=0.01)
            self.assertEqual(got > 0.5, bool(is_foil))

    def test_a_flat_crop_gives_a_probability_and_does_not_divide_by_zero(self):
        got = self.marker.probability(np.full((32, 128), 120, np.uint8))
        self.assertTrue(0.0 <= got <= 1.0)


class TheGeometry(unittest.TestCase):
    def line(self, text, top):
        return {'text': text, 'confidence': 0.9, 'box': [[10, top], [200, top], [200, top + 20], [10, top + 20]]}

    def test_a_small_card_is_enlarged_and_a_large_one_is_not(self):
        small = foil_marker.strip_of(np.zeros((500, 350, 3), np.uint8))
        large = foil_marker.strip_of(np.zeros((1462, 1046, 3), np.uint8))
        self.assertEqual(large.shape[:2], (int(1462 * 1.0) - int(1462 * 0.86), int(1046 * 0.62)))     # as cut, not resized
        self.assertAlmostEqual(small.shape[0], (500 - int(500 * 0.86)) * 2, delta=2)                   # 1000 / 500 = 2 x

    def test_the_set_line_is_the_one_after_the_collector_number(self):
        lines = [self.line('U 0172', 5), self.line('HOB ★ EN', 40)]
        self.assertEqual(foil_marker.set_line_box(lines), lines[1]['box'])
        # given in any order: they are read from the top down
        self.assertEqual(foil_marker.set_line_box(lines[::-1]), lines[1]['box'])

    def test_no_set_line_means_none(self):
        self.assertIsNone(foil_marker.set_line_box([self.line('U 0172', 5), self.line('Illus. Somebody', 40)]))
        self.assertIsNone(foil_marker.set_line_box([]))

    def test_the_crop_is_32_by_128_grey_and_stays_inside_the_picture(self):
        strip = np.random.default_rng(0).integers(0, 255, (140, 600, 3), dtype=np.uint8)
        crop = foil_marker.set_line_crop(strip, [[-30, -5], [300, -5], [300, 25], [-30, 25]])     # sticks out of the strip
        self.assertEqual(crop.shape, (32, 128))
        self.assertEqual(crop.dtype, np.uint8)
        self.assertIsNone(foil_marker.set_line_crop(strip, [[700, 10], [800, 10], [800, 30], [700, 30]]))   # outside it: nothing


class FakeOcr:
    """Stands in for CardOcr.read_lines: answers the given lines and remembers what image it was asked about"""

    def __init__(self, lines):
        self.lines, self.asked = lines, []

    def read_lines(self, image):
        self.asked.append(image.shape)
        return self.lines


class Reading(unittest.TestCase):
    """FoilMarker.read: the strip is read, the line found, and the probability turned into an answer at SURE"""

    # The strip's OCR lines: the collector number, then the set line with NO symbol between set and language (the symbol was not read:
    # the case the network is for). WITH_STAR / WITH_DOT are the same set line with the symbol the strip's OCR did read.
    SET_LINES = [{'text': 'U 0172', 'confidence': 0.9, 'box': [[5, 5], [120, 5], [120, 25], [5, 25]]},
                 {'text': 'HOB EN', 'confidence': 0.9, 'box': [[5, 45], [150, 45], [150, 70], [5, 70]]}]
    WITH_STAR = [SET_LINES[0], {**SET_LINES[1], 'text': 'HOB ★ EN'}]
    WITH_DOT = [SET_LINES[0], {**SET_LINES[1], 'text': 'HOB • EN'}]
    CARD = np.full((1200, 860, 3), 90, np.uint8)

    def marker(self, probability):
        marker = foil_marker.load(True)
        marker.probability = lambda crop: probability          # the network's answer, set by the test
        return marker

    def test_a_star_the_strip_ocr_read_beats_a_network_that_is_sure_it_is_regular(self):
        # the contradiction the review of 2026-10-20 found: the second OCR request recovered a ★, the network says 0.001
        self.assertEqual(self.marker(0.001).read(self.CARD, FakeOcr(self.WITH_STAR)), ('foil', None))

    def test_a_dot_the_strip_ocr_read_beats_a_network_that_is_sure_it_is_foil(self):
        self.assertEqual(self.marker(0.999).read(self.CARD, FakeOcr(self.WITH_DOT)), ('non-foil', None))

    def test_a_symbol_the_ocr_read_is_the_answer_without_asking_the_network(self):
        marker = foil_marker.load(True)
        marker.probability = mock.Mock(side_effect=AssertionError('the network must not be asked'))
        self.assertEqual(marker.read(self.CARD, FakeOcr(self.WITH_STAR))[0], 'foil')
        self.assertEqual(marker.read(self.CARD, FakeOcr(self.WITH_DOT))[0], 'non-foil')

    def test_the_symbols_ocr_gives_are_read_as_parse_magic_reads_them(self):
        for text, want in (('HOB ★ EN', 'foil'), ('HOB*EN', 'foil'), ('HOB ☆ EN', 'foil'), ('HOB·EN', 'non-foil'),
                           ('HOB • EN', 'non-foil'), ('SPM.EN', 'non-foil'), ('HOB EN', 'unknown'), ('HOBEN', 'unknown'),
                           ('SPM·ENLADRAWS', 'non-foil')):
            self.assertEqual(foil_marker.symbol_read({'text': text, 'box': []}), want, text)
        self.assertEqual(foil_marker.symbol_read({'text': 'Illus. Somebody', 'box': []}), 'unknown')

    def test_a_sure_probability_is_an_answer_either_way(self):
        card = np.full((1200, 860, 3), 90, np.uint8)
        self.assertEqual(self.marker(0.95).read(card, FakeOcr(self.SET_LINES))[0], 'foil')
        self.assertEqual(self.marker(0.04).read(card, FakeOcr(self.SET_LINES))[0], 'non-foil')

    def test_a_probability_in_between_is_unknown(self):
        self.assertEqual(self.marker(0.6).read(np.full((1200, 860, 3), 90, np.uint8), FakeOcr(self.SET_LINES))[0], 'unknown')

    def test_the_ocr_is_asked_about_the_strip_not_the_whole_card(self):
        ocr = FakeOcr(self.SET_LINES)
        self.marker(0.95).read(np.full((1200, 860, 3), 90, np.uint8), ocr)
        self.assertEqual(ocr.asked, [(1200 - int(1200 * 0.86), int(860 * 0.62), 3)])

    def test_no_lines_or_no_set_line_is_unknown_without_asking_the_network(self):
        marker = foil_marker.load(True)
        marker.probability = mock.Mock(side_effect=AssertionError('the network must not be asked'))
        card = np.full((1200, 860, 3), 90, np.uint8)
        self.assertEqual(marker.read(card, FakeOcr(None)), ('unknown', None))
        self.assertEqual(marker.read(card, FakeOcr([])), ('unknown', None))
        self.assertEqual(marker.read(card, FakeOcr(self.SET_LINES[:1])), ('unknown', None))      # a collector line, no set line


class FakeGame:
    """The game as _read_with_ocr uses it: it confirms every read"""

    def confirmed_read(self, name, number, set_code):
        return True


class InIdentification(unittest.TestCase):
    """_read_with_ocr and _ask_ai: the OCR's marker, then the network, then the AI - and the network never breaks a card"""

    def setUp(self):
        self.ident = identification.Identification.__new__(identification.Identification)   # no OCR process, no AI, no settings
        self.ident.log_callback = None
        self.ident.ocr_enabled = True
        self.ident.card_identifier = None
        self.ident.foil_marker = mock.Mock()
        self.ident.foil_marker.read.return_value = ('foil', 0.97)
        self.ident._ai_foil_check = lambda foil_image: True       # the AI foil check is possible
        self.read = {'name': 'Gandalf', 'collector_number': '242', 'set_code': 'HOB', 'foil': 'unknown'}
        self.ident.card_ocr = mock.Mock()
        self.ident.card_ocr.read_card.side_effect = lambda image, game: dict(self.read)
        patcher = mock.patch.object(identification, 'games')
        games = patcher.start()
        self.addCleanup(patcher.stop)
        games.active.return_value = FakeGame()
        games.active_id.return_value = 'mtg'
        self.image = np.zeros((100, 70, 3), np.uint8)

    def test_a_sure_network_settles_the_card_without_the_ai(self):
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual(info['foil'], 'foil')
        self.assertIsNone(task)                                    # nothing left for the AI stage
        self.ident.foil_marker.read.assert_called_once()

    def test_an_unsure_network_leaves_the_ai_foil_check(self):
        self.ident.foil_marker.read.return_value = ('unknown', 0.55)
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual(info['foil'], 'unknown')
        self.assertEqual(task, identification.FOIL)

    def test_a_marker_the_ocr_read_is_not_asked_again(self):
        self.read['foil'] = 'non-foil'
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual((info['foil'], task), ('non-foil', None))
        self.ident.foil_marker.read.assert_not_called()

    def test_a_failing_network_is_a_shrug_not_a_lost_card(self):
        self.ident.foil_marker.read.side_effect = RuntimeError('dnn exploded')
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual((info['foil'], task), ('unknown', identification.FOIL))

    def test_without_a_network_it_is_as_before(self):
        self.ident.foil_marker = None
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual((info['foil'], task), ('unknown', identification.FOIL))
        self.ident._ai_foil_check = lambda foil_image: False
        self.assertEqual(self.ident._read_with_ocr(self.image, None)[1], None)      # no AI foil check either: the card is done, finish unknown

    def test_the_flat_card_is_read_when_there_is_one(self):
        # a fixed area's `image` is the area as drawn; the flat card is `foil_image` (the AI's foil check reads it too)
        area, flat = np.zeros((90, 120, 3), np.uint8), np.ones((100, 70, 3), np.uint8)
        self.ident._read_with_ocr(area, flat)
        self.assertIs(self.ident.foil_marker.read.call_args[0][0], flat)
        # without a separate foil picture the card image itself is the flat card
        self.ident.foil_marker.read.reset_mock()
        self.ident._read_with_ocr(area, None)
        self.assertIs(self.ident.foil_marker.read.call_args[0][0], area)

    def test_the_ai_identification_path_reads_the_flat_card_too(self):
        identifier = mock.Mock()
        identifier.identify_card.return_value = {'name': 'Gandalf', 'collector_number': '242', 'set_code': 'HOB'}
        self.ident.card_identifier = identifier
        area, flat = np.zeros((90, 120, 3), np.uint8), np.ones((100, 70, 3), np.uint8)
        self.ident._ask_ai(identification.IDENTIFY, area, flat, None)
        self.assertIs(self.ident.foil_marker.read.call_args[0][0], flat)

    def test_ocr_switched_off_means_no_network_either(self):
        # the network needs the OCR reader to find the set line
        self.ident.ocr_enabled = False
        self.assertEqual(self.ident._read_foil_locally(self.image), 'unknown')
        self.ident.foil_marker.read.assert_not_called()

    def test_a_symbol_read_by_the_strip_ocr_is_logged_and_settles_the_card(self):
        # FoilMarker.read answers (foil, None) for a symbol the OCR read itself: no probability to print
        self.ident.foil_marker.read.return_value = ('non-foil', None)
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual((info['foil'], task), ('non-foil', None))

    def test_the_ai_identification_path_uses_the_network_before_the_ai_foil_request(self):
        identifier = mock.Mock()
        identifier.identify_card.return_value = {'name': 'Gandalf', 'collector_number': '242', 'set_code': 'HOB'}
        self.ident.card_identifier = identifier
        self.ident.foil_marker.read.return_value = ('non-foil', 0.03)
        info = self.ident._ask_ai(identification.IDENTIFY, self.image, self.image, None)
        self.assertEqual(info['foil'], 'non-foil')
        identifier.read_foil_symbol.assert_not_called()            # the network answered: no extra AI request

    def test_the_ai_identification_path_still_asks_the_ai_foil_check_when_the_network_is_unsure(self):
        identifier = mock.Mock()
        identifier.identify_card.return_value = {'name': 'Gandalf', 'collector_number': '242', 'set_code': 'HOB'}
        identifier.read_foil_symbol.return_value = 'foil'
        self.ident.card_identifier = identifier
        self.ident.foil_marker.read.return_value = ('unknown', 0.5)
        info = self.ident._ask_ai(identification.IDENTIFY, self.image, self.image, None)
        self.assertEqual(info['foil'], 'foil')
        identifier.read_foil_symbol.assert_called_once()


if __name__ == '__main__':
    unittest.main()
