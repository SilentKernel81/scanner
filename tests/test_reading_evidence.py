"""
reading.mode 'evidence' (phase 3 of docs/plans/READING_REFACTOR_PLAN.md): a card the full-card OCR did not confirm is read by its picture and its strip before the
vision AI is asked, and the AI's answer is checked against the picture. Three layers, each with stand-ins for the rest:
  PrintingCheck.live_read    on a synthetic image index (the same synthetic cards as test_printing_check.py), a fake strip reader
  Identification             _read_with_ocr / _ask_ai with a fake `evidence` (no OCR process, no AI, no index)
  the routing                search_and_emit_card takes a decided printing by its id, Game.card_by_printing tags it
and that 'cascade' - the default - never reaches any of it (the foil-marker tests, unedited, pin the rest of that).

    venv/bin/python -m unittest discover tests -p test_reading_evidence.py
"""
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))                 # the helpers of test_printing_check.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from card_scanner.config import Config  # noqa: E402
from card_scanner.core import identification, image_index, printing_check  # noqa: E402
from test_printing_check import FakeReader, degraded, line, synthetic_card, write_pictures  # noqa: E402


class Cards:
    """get_card_by_id for the synthetic printings: p<n> is number n of set 'tst', in both finishes; p9 does not exist"""

    def __init__(self, finishes=None, old=()):
        self.finishes = finishes or {}
        self.old = old                      # printings of an older release / frame (no foil marker): '*' for all of them

    def get_card_by_id(self, printing_id):
        if printing_id == 'p9':
            return None
        older = '*' in self.old or printing_id in self.old
        return {'id': printing_id, 'name': f'Card {printing_id}', 'set_code': 'tst', 'number': printing_id[1:], 'oracle_id': f'o-{printing_id}',
                'finishes': self.finishes.get(printing_id, ['nonfoil', 'foil']), 'frame': '2015', 'released_at': '2019-11-01' if older else '2024-05-01'}

    def get_card_by_set_number(self, set_code, collector_number, exact=False):
        printing_id = f'p{int(collector_number)}'
        return self.get_card_by_id(printing_id) if set_code == 'tst' and printing_id != 'p9' else None


def strip_lines(number=None, symbol='★'):
    """The OCR lines of a strip: the collector line and the set line (with a star, a dot or nothing between the set and the language)"""
    if number is None:
        return []
    return [line(f'U {int(number):04d}', 5), line(f'TST {symbol} EN'.replace('  ', ' '), 40)]


class LiveRead(unittest.TestCase):
    """PrintingCheck.live_read: the picture and the strip, decided"""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        pictures = self.root / 'pictures'
        pictures.mkdir()
        write_pictures(pictures, range(6))
        cv2.imwrite(str(pictures / 'p6.jpg'), cv2.cvtColor(synthetic_card(2), cv2.COLOR_RGB2BGR))   # p6: a reprint of p2, the same art
        image_index.build([(f'p{n}', None) for n in range(7)], self.root / 'index', image_dir=pictures, fetch=False, log=lambda m: None)
        self.check = self.make(Cards())

    def make(self, database, folder=None):
        return printing_check.PrintingCheck(database, folder or self.root / 'index', self.root / 'log.jsonl', 'off', 'off')

    def test_a_strip_that_names_the_pictures_printing_decides_it(self):
        reading = self.check.live_read(degraded(synthetic_card(3)), None, FakeReader(strip_lines(3)))
        self.assertEqual((reading['path'], reading['card']['id']), ('strong', 'p3'))
        self.assertEqual(reading['strip'], {'number': '3', 'set': 'tst'})
        self.assertEqual(reading['finish'], {'finish': 'foil', 'source': 'symbol'})         # the star the strip's OCR read

    def test_two_reprints_with_one_art_are_told_apart_by_the_strip(self):
        picture = degraded(synthetic_card(2))
        self.assertEqual(self.check.live_read(picture, None, FakeReader(strip_lines(6)))['card']['id'], 'p6')
        self.assertEqual(self.check.live_read(picture, None, FakeReader(strip_lines(2)))['card']['id'], 'p2')

    def test_the_picture_alone_decides_when_the_strip_is_unread_unless_that_is_refused(self):
        picture = degraded(synthetic_card(3))
        reading = self.check.live_read(picture, None, FakeReader([]), accept_image_alone=True)
        self.assertEqual((reading['path'], reading['card']['id']), ('image', 'p3'))
        self.assertIsNone(self.check.live_read(picture, None, FakeReader([]), accept_image_alone=False))
        self.assertIsNone(self.check.live_read(picture, None, FakeReader([])))              # live_read itself is OFF unless told (Codex review, second finding) ...
        self.assertFalse(inspect.signature(printing_check.PrintingCheck.live_read).parameters['accept_image_alone'].default)
        self.assertTrue(Config.READING_ACCEPT_IMAGE_ALONE)                                  # ... and the setting Identification passes on is ON by default since 2026-10-20 (the picture is first)
        # ... and a strip that did decide is not affected by that refusal
        self.assertEqual(self.check.live_read(picture, None, FakeReader(strip_lines(3)), accept_image_alone=False)['path'], 'strong')

    def test_codex_2026_10_20_a_picture_of_a_printing_the_index_lacks_is_not_added_on_its_lead(self):
        # An index of p0 and p1 only; the picture is p7, which it has never seen. The best match is p1 with a lead over p0 above 0.05 but a similarity
        # near zero: before the guard this was added (as p1) with the AI skipped
        pictures = self.root / 'pictures'
        image_index.build([('p0', None), ('p1', None)], self.root / 'small', image_dir=pictures, fetch=False, log=lambda m: None)
        small = self.make(Cards(), self.root / 'small')
        picture = degraded(synthetic_card(7))
        search = small._search(picture)
        self.assertGreaterEqual(search['sims'][0] - search['sims'][1], 0.05)       # the lead alone would have passed
        self.assertLess(search['sims'][0], printing_check.IMAGE_MIN_SIMILARITY)
        self.assertIsNone(small.live_read(picture, None, FakeReader([])))
        self.assertIsNone(small.live_read(picture, None, FakeReader(strip_lines(7))))     # ... with or without a strip

    def test_codex_2026_10_20_a_poor_picture_is_not_a_strong_decision_because_the_strip_names_a_nearby_candidate(self):
        # synthetic_card(11) matches nothing in the index: its best match is only 0.057 similar, and p4, p2, p6 and p0 are all within 0.02 of it. A collector line that names
        # p2 (or p6) was enough for 'strong' - and an add by the default reading - before 'strong' had a floor of its own
        picture = degraded(synthetic_card(11))
        search = self.check._search(picture)
        self.assertLess(search['sims'][0], printing_check.STRONG_MIN_SIMILARITY)
        self.assertGreaterEqual(search['sims'][2] if len(search['sims']) > 2 else 0, search['sims'][0] - 0.02)     # candidates close to the best: the hazard is real
        for named in (2, 6):
            self.assertIsNone(self.check.live_read(picture, None, FakeReader(strip_lines(named))), named)
            self.assertIsNone(self.check.live_read(picture, None, FakeReader(strip_lines(named)), accept_image_alone=True), named)
        with mock.patch.object(printing_check, 'STRONG_MIN_SIMILARITY', -1.0):        # the floor off: the very reading the review described
            self.assertEqual(self.check.live_read(picture, None, FakeReader(strip_lines(2)))['path'], 'strong')

    def test_a_good_strong_decision_is_not_refused_by_its_floor(self):
        reading = self.check.live_read(degraded(synthetic_card(3)), None, FakeReader(strip_lines(3)))
        self.assertEqual(reading['path'], 'strong')
        self.assertGreater(reading['similarity'], printing_check.IMAGE_MIN_SIMILARITY)     # a clearly matching picture is far above both floors
        self.assertLess(printing_check.STRONG_MIN_SIMILARITY, printing_check.IMAGE_MIN_SIMILARITY)

    def test_upright_says_which_way_up_a_card_is_and_only_when_it_is_sure(self):
        # (Codex review, 2026-10-20: the foil network cut at a fixed place must not be given an upside-down card)
        picture = degraded(synthetic_card(3))
        turned = cv2.rotate(picture, cv2.ROTATE_180)
        self.assertIs(self.check.upright(picture), picture)                          # as given: the same array
        np.testing.assert_array_equal(self.check.upright(turned), picture)            # put in upside down: turned back
        noise = np.random.default_rng(1).integers(0, 255, picture.shape, dtype=np.uint8)
        self.assertIsNone(self.check.upright(noise))                                  # looks like nothing: it cannot say
        self.assertIsNone(self.make(Cards(), self.root / 'no-index').upright(picture))   # no index: nothing to say
        with mock.patch.object(printing_check, 'ORIENTATION_MIN_MARGIN', 2.0):        # the two orientations not clearly apart
            self.assertIsNone(self.check.upright(picture))
        with mock.patch.object(printing_check, 'ORIENTATION_MIN_SIMILARITY', 2.0):    # the better one not similar enough
            self.assertIsNone(self.check.upright(picture))

    def test_without_a_reader_the_foil_network_is_still_asked_at_the_fixed_place(self):
        # OCR off: no number, set or symbol, but the finish is no longer the AI's alone - the network reads the set line where it is on a flat card
        class Net:
            def __init__(self):
                self.crops = []

            def probability(self, crop):
                self.crops.append(crop.shape)
                return 0.97
        net = Net()
        card = np.full((1462, 1046, 3), 200, np.uint8)
        evidence = printing_check.read_strip_evidence(None, card, net)
        self.assertEqual(evidence, {'number': '', 'set': '', 'symbol': 'unknown', 'net': 0.97})
        self.assertEqual(net.crops, [(32, 128)])
        self.assertEqual(printing_check.read_strip_evidence(None, card, None)['net'], None)      # no network: nothing to say

    def test_prints_a_marker_is_true_only_for_the_releases_and_the_frame_the_network_was_measured_on(self):
        modern = {'released_at': '2024-05-01', 'frame': '2015'}
        self.assertTrue(printing_check.prints_a_marker(modern))
        self.assertTrue(printing_check.prints_a_marker({**modern, 'released_at': printing_check.MARKER_FROM}))         # the day it starts counts
        self.assertFalse(printing_check.prints_a_marker({**modern, 'released_at': '2023-06-22'}))                     # the day before does not
        self.assertFalse(printing_check.prints_a_marker({**modern, 'released_at': '2015-07-17'}))                     # a modern frame, but before the marker
        self.assertFalse(printing_check.prints_a_marker({**modern, 'frame': '1997'}))                                 # a retro frame in a new set: the old collector line
        self.assertFalse(printing_check.prints_a_marker({**modern, 'frame': '1993'}))
        for unreadable in (None, {}, {'frame': '2015'}, {'released_at': None, 'frame': '2015'}):
            self.assertFalse(printing_check.prints_a_marker(unreadable))

    def test_without_a_reader_the_network_is_not_asked_about_a_card_that_prints_no_marker(self):
        # (Codex review, 2026-10-20: orientation is not layout - an older frame has no star or dot, and the network would answer about whatever sits in the box)
        picture = degraded(synthetic_card(3))
        net = mock.Mock(probability=mock.Mock(return_value=0.97))
        top = [self.check.index().ids[int(row)] for row in self.check._search(picture)['top']]
        for old in ({'*'}, {top[0]}):                                                      # all of them old, the best match
            check = self.make(Cards(old=old))
            net.probability.reset_mock()
            reading = check.live_read(picture, None, None, net, accept_image_alone=True)
            net.probability.assert_not_called()
            self.assertEqual((reading['path'], reading['card']['id']), ('image', 'p3'))            # the printing is still the picture's
            self.assertNotEqual(reading['finish']['source'], 'network')
            self.assertIsNone(check.upright(picture, needs_marker=True))                          # and the AI path is told 'unknown' too
            self.assertIs(check.upright(picture), picture)                                        # while orientation alone is still established
        net.probability.reset_mock()
        self.make(Cards()).live_read(picture, None, None, net, accept_image_alone=True)
        net.probability.assert_called_once()                                                   # all of them modern: asked

    def test_only_the_printings_the_picture_cannot_tell_apart_from_its_best_match_count(self):
        # an older reprint that looks clearly different from the picture does not make a modern card unmarked (all five nearest would: 15% of modern cards passed) ...
        picture = degraded(synthetic_card(3))
        search = self.check._search(picture)
        far = self.check.index().ids[int(search['top'][-1])]                                 # the fifth nearest: far from the best here
        self.assertIsNotNone(self.make(Cards(old={far})).upright(picture, needs_marker=True))
        # ... one that looks as much like it as the best match does (a twin: same art, older frame) does
        with mock.patch.object(printing_check, 'AGREE_BELOW', 2.0):                         # every one of the five agrees with the best
            self.assertIsNone(self.make(Cards(old={far})).upright(picture, needs_marker=True))

    def twins(self, count=8):
        """An index of the synthetic printings p0..p6 and `count` twins of p3 (the same picture, so they tie with it): the card data's ids t0..t7"""
        pictures = self.root / 'twin-pictures'
        pictures.mkdir(exist_ok=True)
        write_pictures(pictures, range(7))
        for n in range(count):
            (pictures / f't{n}.jpg').write_bytes((pictures / 'p3.jpg').read_bytes())
        folder = self.root / 'twin-index'
        image_index.build([(f'p{n}', None) for n in range(7)] + [(f't{n}', None) for n in range(count)], folder, image_dir=pictures, fetch=False, log=lambda m: None)
        return folder

    def test_an_older_printing_tied_with_the_best_but_outside_the_five_nearest_is_still_found(self):
        # (Codex review, 2026-10-20: with more than five same-art printings tied, an older one just outside `top` was never looked at)
        folder = self.twins(8)                                                      # p3 and eight twins: nine printings with one picture
        picture = degraded(synthetic_card(3))
        search = self.make(Cards(), folder)._search(picture)
        sims = image_index.similarity(search['distances'])
        tied = int((sims.max() - sims < printing_check.AGREE_BELOW).sum())
        self.assertEqual(tied, 9)                                                   # p3 and the eight twins: more than the five rows of `top`
        self.assertEqual(len(search['top']), 5)
        net = mock.Mock(probability=mock.Mock(return_value=0.97))
        for older in [f't{n}' for n in range(8)] + ['p3']:                         # whichever of the nine is the old one - five of them are in `top`, four are not
            check = self.make(Cards(old={older}), folder)
            self.assertIsNone(check.upright(picture, needs_marker=True), older)
            net.probability.reset_mock()
            check.live_read(picture, None, None, net, accept_image_alone=True)
            net.probability.assert_not_called()
        self.assertIsNotNone(self.make(Cards(), folder).upright(picture, needs_marker=True))      # all nine modern: allowed

    def test_too_many_tied_printings_is_too_ambiguous_to_say(self):
        folder = self.twins(8)
        picture = degraded(synthetic_card(3))
        with mock.patch.object(printing_check, 'MARKER_MAX_TIED', 3):
            self.assertIsNone(self.make(Cards(), folder).upright(picture, needs_marker=True))      # nine tied, all modern, but more than the cap
        self.assertIsNotNone(self.make(Cards(), folder).upright(picture, needs_marker=True))

    def test_a_printing_the_card_data_does_not_have_is_not_a_marker_layout(self):
        class Lacking(Cards):
            def get_card_by_id(self, printing_id):
                return None if printing_id == 'p3' else super().get_card_by_id(printing_id)
        picture = degraded(synthetic_card(3))
        self.assertIsNone(self.make(Lacking()).upright(picture, needs_marker=True))
        class Failing(Cards):
            def get_card_by_id(self, printing_id):
                raise RuntimeError('database is locked')
        self.assertIsNone(self.make(Failing()).upright(picture, needs_marker=True))           # fails closed, never raises

    def test_without_a_reader_an_orientation_that_is_not_established_keeps_the_network_out(self):
        # (Codex review, 2026-10-20: the picture alone can pass 0.5 similarity and a printing lead of 0.05 and still be nearly as near to the other orientation -
        # the fixed crop would then be cut from the wrong way up, and the network's confident answer would be recorded as the finish)
        picture = degraded(synthetic_card(3))
        net = mock.Mock(probability=mock.Mock(return_value=0.97))
        with mock.patch.object(printing_check, 'ORIENTATION_MIN_MARGIN', 2.0):
            reading = self.check.live_read(picture, None, None, net, accept_image_alone=True)
        self.assertEqual((reading['path'], reading['card']['id']), ('image', 'p3'))        # the printing is still decided by the picture
        net.probability.assert_not_called()                                                # but the network was not asked
        self.assertNotEqual(reading['finish']['source'], 'network')                        # so the finish is not its guess
        with mock.patch.object(printing_check, 'ORIENTATION_MIN_SIMILARITY', 2.0):
            net.probability.reset_mock()
            self.check.live_read(picture, None, None, net, accept_image_alone=True)
            net.probability.assert_not_called()

    def test_with_a_reader_the_orientation_floor_does_not_apply_to_the_ocr_read(self):
        # the OCR finds the set line wherever it is: the network at the OCR's box needs no orientation confidence from the search
        picture = degraded(synthetic_card(3))
        net = mock.Mock(probability=mock.Mock(return_value=0.97))
        reader = FakeReader(strip_lines(3, symbol=''))                      # a set line with no star or dot read: the network is asked about it
        with mock.patch.object(printing_check, 'ORIENTATION_MIN_MARGIN', 2.0):    # an orientation margin nothing can reach
            reading = self.check.live_read(picture, None, reader, net, accept_image_alone=True)
        net.probability.assert_called_once()                                # still asked: at the OCR's box, which needs no orientation confidence of the search
        self.assertEqual(reading['finish']['source'], 'network')

    def test_without_a_reader_the_picture_alone_decides_the_printing_and_the_network_the_finish(self):
        picture = degraded(synthetic_card(3))
        net = mock.Mock(probability=mock.Mock(return_value=0.97))
        reading = self.check.live_read(picture, None, None, net, accept_image_alone=True)
        self.assertEqual((reading['path'], reading['card']['id']), ('image', 'p3'))
        self.assertEqual((reading['finish']['finish'], reading['finish']['source']), ('foil', 'network'))    # not left to the AI

    def test_without_a_reader_no_strip_is_read_and_only_the_picture_alone_can_decide(self):
        # OCR switched off (Identification hands over no reader): the strip cannot name a printing, so there is no 'strong' decision and the picture alone
        # decides only when it is allowed - the use is trying the picture on its own
        picture = degraded(synthetic_card(3))
        reading = self.check.live_read(picture, None, None, accept_image_alone=True)
        self.assertEqual((reading['path'], reading['card']['id']), ('image', 'p3'))
        self.assertEqual((reading['strip']['number'], reading['strip']['set']), ('', ''))
        self.assertIsNone(self.check.live_read(picture, None, None))                       # the default: not allowed, so the AI is asked
        # ... a strip that WOULD name another candidate is not read: the same picture, a reader that says p6, is not asked
        reader = FakeReader(strip_lines(6))
        self.assertEqual(self.check.live_read(picture, None, None, accept_image_alone=True)['card']['id'], 'p3')
        self.assertEqual(reader.calls, 0)

    def test_missing_printings_counts_what_the_index_lacks_of_the_card_data_and_remembers_it(self):
        class CardData(Cards):
            def __init__(self, ids):
                super().__init__()
                self.ids, self.counted, self.stamp = ids, 0, '2026-10-20T12:00:00'
            def printing_images(self):
                self.counted += 1
                return [(i, f'https://cards.example/{i}.jpg') for i in self.ids]
            def get_data_info(self, game):
                return {'downloaded_at': self.stamp}
        data = CardData([f'p{n}' for n in range(7)])
        check = self.make(data)
        self.assertEqual(check.missing_printings(), 0)                                # p0..p6 all indexed
        self.assertEqual(check.missing_printings(), 0)
        self.assertEqual(data.counted, 1)                                             # counted once ...
        data.ids.append('p-new')
        data.stamp = '2026-10-21T08:00:00'                                            # ... and again after a card data update
        self.assertEqual(check.missing_printings(), 1)
        self.assertEqual(data.counted, 2)
        self.assertIsNone(self.make(data, self.root / 'no-index').missing_printings())   # no index: nothing to say
        # a printing with no picture address is missing too: it can never be indexed, so a card of it could only be matched as another printing (second Codex finding)
        data.ids.append('no-picture')
        data.stamp = '2026-10-21T09:00:00'
        original = data.printing_images
        data.printing_images = lambda: [(i, None if i == 'no-picture' else f'https://cards.example/{i}.jpg') for i in data.ids]
        self.assertEqual(check.missing_printings(), 2)                                # p-new and the one with no picture
        data.printing_images = original
        self.assertIsNone(self.make(Cards()).missing_printings())                    # card data that cannot say: not covered (the caller refuses the picture alone)

    def test_codex_2026_10_20_a_printing_the_index_lacks_is_not_added_as_another_with_the_default_settings(self):
        # The index lacks p5 and p6 (the card data has them); a picture of p5, no strip (OCR off), the picture alone ON (the default since the picture comes first): nothing is added -
        # before, the best match (another printing) was added if it was clear enough. Once the index covers the card data again, the picture decides p5 itself.
        class CardData(Cards):
            def printing_images(self):
                return [(f'p{n}', f'https://cards.example/p{n}.jpg') for n in range(7)]
            def get_data_info(self, game):
                return {'downloaded_at': '2026-10-20T12:00:00'}
        pictures = self.root / 'pictures-stale'
        pictures.mkdir()
        write_pictures(pictures, range(5))
        image_index.build([(f'p{n}', None) for n in range(5)], self.root / 'stale', image_dir=pictures, fetch=False, log=lambda m: None)
        ident = identification.Identification.__new__(identification.Identification)
        ident.log_callback, ident.ocr_enabled, ident.ai_enabled, ident.foil_marker, ident.card_ocr = None, False, False, None, None
        ident.settings = mock.Mock(get=lambda key, default=None: default)            # nothing chosen: every default (evidence, picture alone ON)
        ident._ai_foil_check = lambda foil_image: False
        self.assertTrue(ident.accept_image_alone)
        picture = degraded(synthetic_card(5))
        ident.evidence = self.make(CardData(), self.root / 'stale')
        self.assertEqual(ident.evidence.missing_printings(), 2)
        with mock.patch.object(printing_check, 'IMAGE_MIN_SIMILARITY', -1.0):         # whatever the floor: it is the coverage that refuses
            self.assertIsNone(ident._read_by_picture(picture, picture))
        ident.evidence = self.make(CardData())                                        # the index extended: p0..p6
        self.assertEqual(ident.evidence.missing_printings(), 0)
        info, task = ident._read_by_picture(picture, picture)
        self.assertEqual(info['printing_id'], 'p5')
        # ... and one printing of the card data with NO picture address (it can never be indexed): the picture alone is off again (second Codex finding)
        class WithoutPicture(CardData):
            def printing_images(self):
                return super().printing_images() + [('no-picture', None)]
        ident.evidence = self.make(WithoutPicture())
        self.assertEqual(ident.evidence.missing_printings(), 1)
        self.assertIsNone(ident._read_by_picture(picture, picture))

    def test_codex_2026_10_20_a_card_newer_than_the_card_data_is_not_added_as_its_best_match(self):
        # p5 is in neither the card data nor the index (both "complete" and both stale): its picture's best match is another printing
        class StaleData(Cards):
            def get_card_by_id(self, printing_id):
                return None if printing_id in ('p5', 'p6') else super().get_card_by_id(printing_id)
            def get_card_by_set_number(self, set_code, collector_number, exact=False):
                return None if int(collector_number) in (5, 6) else super().get_card_by_set_number(set_code, collector_number, exact)
            def printing_images(self):
                return [(f'p{n}', f'https://cards.example/p{n}.jpg') for n in range(5)]
            def get_data_info(self, game):
                return {'downloaded_at': '2026-10-20T12:00:00'}
        pictures = self.root / 'pictures-newer'
        pictures.mkdir()
        write_pictures(pictures, range(5))
        image_index.build([(f'p{n}', None) for n in range(5)], self.root / 'newer', image_dir=pictures, fetch=False, log=lambda m: None)
        check = self.make(StaleData(), self.root / 'newer')
        self.assertEqual(check.missing_printings(), 0)                                  # "complete": the index covers the (stale) card data
        picture = degraded(synthetic_card(5))
        with mock.patch.object(printing_check, 'IMAGE_MIN_SIMILARITY', -1.0), mock.patch.object(printing_check, 'IMAGE_LEAD', -1.0):    # whatever the thresholds
            self.assertIsNone(check.live_read(picture, None, FakeReader(strip_lines(5)), accept_image_alone=True))    # the strip names a printing the data lacks
            # ... and the strip unread: the full-card OCR must read the chosen card's name, which it does not - the card is another one (a real name: the synthetic
            # 'Card p5' / 'Card p3' are close enough for names_match, which forgives OCR misreads)
            ident = identification.Identification.__new__(identification.Identification)
            ident.log_callback, ident.ocr_enabled, ident.ai_enabled, ident.foil_marker = None, True, False, None
            ident.settings = mock.Mock(get=lambda key, default=None: default)
            ident._ai_foil_check = lambda foil_image: False
            ident.card_ocr = FakeReader([])
            ident.card_ocr.read_card = lambda image, game: {'name': 'Shocker, Unshakable', 'collector_number': '', 'set_code': '', 'foil': 'unknown'}
            ident.evidence = check
            with mock.patch.object(identification, 'games') as games:
                games.active_id.return_value = 'mtg'
                self.assertIsNone(ident._read_by_picture(picture, picture))

    def test_a_strip_that_names_a_printing_the_index_lacks_stops_the_picture_alone(self):
        # The index has p0..p6 but not p5's picture; the strip names p5 (the card database has it). The best match then is some other card: no add
        pictures = self.root / 'pictures2'
        pictures.mkdir()
        write_pictures(pictures, [0, 1, 2, 3, 4])
        image_index.build([(f'p{n}', None) for n in range(5)], self.root / 'no5', image_dir=pictures, fetch=False, log=lambda m: None)
        check = self.make(Cards(), self.root / 'no5')
        picture = degraded(synthetic_card(5))
        with mock.patch.object(printing_check, 'IMAGE_MIN_SIMILARITY', -1.0):              # the floor off: only the strip rule is under test
            self.assertEqual(check.live_read(picture, None, FakeReader([]), accept_image_alone=True)['path'], 'image')      # nothing read: the lead decided (the hazard)
            self.assertIsNone(check.live_read(picture, None, FakeReader(strip_lines(5)), accept_image_alone=True))           # the strip names p5, which the index lacks
            # a printing the database lacks too (p9): refused since a third Codex review of 2026-10-20 - a card newer than the card data is in neither, and the strip naming
            # an unknown printing is its sign (it was allowed before; what that costs: 13 of 47 correct picture-alone decisions, which go on to OCR - unknown_strip.py)
            self.assertIsNone(check.live_read(picture, None, FakeReader(strip_lines(9)), accept_image_alone=True))
            # ... and with the picture alone off (the default) neither is added, the unreadable strip included: the stale-index hazard of the second review
            self.assertIsNone(check.live_read(picture, None, FakeReader([])))
            self.assertIsNone(check.live_read(picture, None, FakeReader(strip_lines(9))))

    def test_a_strip_that_names_an_indexed_printing_does_not_stop_a_picture_alone(self):
        # the Secret Lair case: the printed line names another printing (p2), which the index has; the picture (p3) decides
        reading = self.check.live_read(degraded(synthetic_card(3)), None, FakeReader(strip_lines(2)), accept_image_alone=True)
        self.assertEqual((reading['path'], reading['card']['id']), ('image', 'p3'))

    def test_when_the_card_database_cannot_say_the_picture_alone_is_not_enough(self):
        class Broken(Cards):
            def get_card_by_set_number(self, *args, **kwargs):
                raise RuntimeError('database is locked')
        picture = degraded(synthetic_card(3))
        self.assertIsNone(self.make(Broken()).live_read(picture, None, FakeReader(strip_lines(2))))
        self.assertEqual(self.make(Broken()).live_read(picture, None, FakeReader(strip_lines(3)))['path'], 'strong')      # a strong decision needs no such look-up

    def test_what_the_picture_cannot_decide_is_no_reading_at_all(self):
        # p2 and p6 look the same and there is no strip: review - the caller asks the AI as it always did
        self.assertIsNone(self.check.live_read(degraded(synthetic_card(2)), None, FakeReader([])))

    def test_without_an_image_index_there_is_no_reading(self):
        self.assertIsNone(self.make(Cards(), self.root / 'no-index').live_read(degraded(synthetic_card(3)), None, FakeReader(strip_lines(3))))

    def test_a_printing_the_card_database_does_not_have_is_not_a_card_to_add(self):
        class Without(Cards):
            def get_card_by_id(self, printing_id):
                return None if printing_id == 'p3' else super().get_card_by_id(printing_id)
        self.assertIsNone(self.make(Without()).live_read(degraded(synthetic_card(3)), None, FakeReader(strip_lines(3))))

    def test_a_card_put_in_upside_down_is_read_the_right_way_up(self):
        reading = self.check.live_read(cv2.rotate(degraded(synthetic_card(4)), cv2.ROTATE_180), None, FakeReader(strip_lines(4)))
        self.assertEqual((reading['path'], reading['card']['id']), ('strong', 'p4'))

    def test_the_strip_is_cut_from_the_flat_card_when_there_is_one(self):
        seen = []

        class Recording(FakeReader):
            def read_lines(self, image):
                seen.append(image.shape)
                return super().read_lines(image)
        flat = np.zeros((1000, 700, 3), np.uint8)
        self.check.live_read(degraded(synthetic_card(3)), flat, Recording(strip_lines(3)))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], int(1000 * 0.14))                    # the strip of the flat card (foil_marker.STRIP: the lowest 14%), not of the capture

    def test_the_fingerprint_is_made_of_the_flat_card_too_not_of_the_area_as_drawn(self):
        # a fixed area's `image` is the area as drawn (here: another card entirely); only the flat card is a card the index has seen
        drawn, flat = degraded(synthetic_card(1)), degraded(synthetic_card(3))
        reading = self.check.live_read(drawn, flat, FakeReader(strip_lines(3)))
        self.assertEqual((reading['path'], reading['card']['id']), ('strong', 'p3'))
        self.assertEqual(self.check.verdict_of(flat, 'p3'), 'agree')

    def test_the_finish_follows_the_printing_the_symbol_and_the_network(self):
        picture = degraded(synthetic_card(3))
        # a printing that exists in one finish only is certain, whatever the strip shows
        check = self.make(Cards({'p3': ['nonfoil']}))
        self.assertEqual(check.live_read(picture, None, FakeReader(strip_lines(3, '★')))['finish'], {'finish': 'regular', 'source': 'printing'})
        # no symbol, no network: unknown - not regular
        got = self.check.live_read(picture, None, FakeReader(strip_lines(3, '')))['finish']
        self.assertEqual(got, {'finish': 'unknown', 'source': 'none'})
        # no symbol, a network that is sure
        net = mock.Mock()
        net.probability.return_value = 0.97
        self.assertEqual(self.check.live_read(picture, None, FakeReader(strip_lines(3, '')), net)['finish'], {'finish': 'foil', 'source': 'network'})
        # a printing that does NOT print the marker (released before The Lord of the Rings): neither the star read nor the network says its finish - unknown,
        # the AI's foil check or review follows (a Codex review of 2026-10-21: an older line's separator is not known to encode the finish)
        older = self.make(Cards(old=('p3',)))
        self.assertEqual(older.live_read(picture, None, FakeReader(strip_lines(3, '★')))['finish'], {'finish': 'unknown', 'source': 'none'})
        self.assertEqual(older.live_read(picture, None, FakeReader(strip_lines(3, '•')))['finish'], {'finish': 'unknown', 'source': 'none'})
        self.assertEqual(older.live_read(picture, None, FakeReader(strip_lines(3, '')), net)['finish'], {'finish': 'unknown', 'source': 'none'})
        # ... its own finishes still decide when it exists in one only
        self.assertEqual(self.make(Cards({'p3': ['foil']}, old=('p3',))).live_read(picture, None, FakeReader(strip_lines(3, '•')))['finish'],
                         {'finish': 'foil', 'source': 'printing'})

    def test_a_card_read_by_picture_and_strip_is_not_looked_at_by_the_shadow_check(self):
        # the picture chose it: comparing the picture with it would only compare the choice with itself (circular evidence for the exit criteria)
        shadow = printing_check.PrintingCheck(Cards(), self.root / 'index', self.root / 'shadow.jsonl', 'shadow', 'all')
        capture = self.root / 'capture.jpg'
        cv2.imwrite(str(capture), cv2.cvtColor(degraded(synthetic_card(3)), cv2.COLOR_RGB2BGR))
        for reader in printing_check.PICTURE_READERS:                             # the picture alone and the picture with the collector line: both are the picture's choice
            shadow.observe(capture, {'id': 'p3'}, 'phone', reader)
            self.assertTrue(shadow.wait())
            self.assertFalse((self.root / 'shadow.jsonl').exists(), reader)
        shadow.observe(capture, {'id': 'p3'}, 'phone', 'qwen3.5:9b')                      # ... while the AI's answer still is
        self.assertTrue(shadow.wait())
        self.assertTrue((self.root / 'shadow.jsonl').exists())

    def test_contradicts_is_true_only_for_another_printing_of_the_same_card(self):
        class SameCard(Cards):
            def get_card_by_id(self, printing_id):
                card = super().get_card_by_id(printing_id)
                if card and printing_id in ('p3', 'p4'):
                    card['oracle_id'] = 'one-card'            # p3 and p4: two printings of one card, with different art
                return card
        check = self.make(SameCard())
        picture = degraded(synthetic_card(3))
        self.assertTrue(check.contradicts(picture, 'p4'))         # the picture is p3: the Secret Lair case
        self.assertFalse(check.contradicts(picture, 'p3'))        # it agrees
        self.assertFalse(check.contradicts(picture, 'p5'))        # another CARD: a glare or a misread, not this rule's business
        self.assertFalse(check.contradicts(picture, 'p9'))        # a printing the index does not have
        self.assertFalse(self.make(SameCard(), self.root / 'no-index').contradicts(picture, 'p4'))

    def test_disagrees_is_true_for_any_other_card_or_printing_the_picture_looks_like(self):
        picture = degraded(synthetic_card(3))
        self.assertTrue(self.check.disagrees(picture, 'p5'))           # another card: what contradicts() leaves alone
        self.assertTrue(self.check.disagrees(picture, 'p2'))
        self.assertFalse(self.check.disagrees(picture, 'p3'))          # it agrees
        self.assertFalse(self.check.disagrees(picture, 'p9'))          # a printing the index does not have: not a disagreement
        self.assertFalse(self.make(Cards(), self.root / 'no-index').disagrees(picture, 'p5'))

    def test_verdict_of_says_whether_the_picture_agrees_with_an_answer(self):
        self.assertEqual(self.check.verdict_of(degraded(synthetic_card(3)), 'p3'), 'agree')
        self.assertEqual(self.check.verdict_of(degraded(synthetic_card(3)), 'p2'), 'disagree')
        self.assertIsNone(self.make(Cards(), self.root / 'no-index').verdict_of(degraded(synthetic_card(3)), 'p3'))


class FakeGame:
    """The game as _read_with_ocr and _ask_ai use it: `confirm` says which OCR reads are confirmed; identify gives the AI's answer a printing"""

    def __init__(self, confirm=False, found=None, card_name=None):
        self.confirm, self.found = confirm, found
        self.card_name = card_name                    # the name of the card the OCR read resolves to, when it is not the name the OCR read

    def confirmed_read(self, name, number, set_code):
        return self.confirm

    def confirmed_card(self, name, number=None, set_code=None):
        return {'id': 'ocr-printing', 'name': self.card_name or name, 'flavor_name': None, 'number': number, 'set_code': set_code} if self.confirm else None

    def identify(self, name, number=None, set_code=None, ai_model=None):
        return self.found


class FakeEvidence:
    """What Identification is given as `evidence`: a reading it was told to give (or to raise), and a verdict"""

    def __init__(self, reading=None, error=None, verdict='agree', contradicted=False, disagreed=False):
        self.reading, self.error, self.verdict, self.contradicted = reading, error, verdict, contradicted
        self.disagreed, self.disagreements = disagreed, []          # the picture looks like something else (any kind); what it was asked about
        self.missing = 0                                            # printings of the card data the index lacks (missing_printings): 0, it covers them
        self.printings = {}                                         # (set, plain number) -> card: what a collector line names (database.get_card_by_set_number)
        self.database = mock.Mock(get_card_by_set_number=lambda set_code, number, exact=False: self.printings.get((set_code.lower(), number.lstrip('0') or '0')))
        self.reads, self.verdicts, self.verdict_images, self.contradictions = [], [], [], []
        self.ocr_given = []                          # the reader live_read was handed (None: OCR was switched off)

    def missing_printings(self):
        return self.missing

    def contradicts(self, image, printing_id):
        self.contradictions.append((image, printing_id))
        if isinstance(self.contradicted, Exception):
            raise self.contradicted
        return self.contradicted

    def disagrees(self, image, printing_id):
        self.disagreements.append((image, printing_id))
        return self.disagreed

    def live_read(self, image, card_image, ocr, foil_net, accept_image_alone):
        self.reads.append((image, card_image, accept_image_alone))
        self.ocr_given.append(ocr)
        if self.error:
            raise self.error
        return self.reading

    def verdict_of(self, image, printing_id):
        self.verdicts.append(printing_id)
        self.verdict_images.append(image)
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


READING = {'path': 'strong', 'card': {'id': 'p3', 'name': 'Gandalf', 'number': '242', 'set_code': 'hob'}, 'strip': {'number': '242', 'set': 'hob'},
           'finish': {'finish': 'foil', 'source': 'symbol'}, 'similarity': 0.9, 'lead': 0.4}


class InIdentification(unittest.TestCase):
    def setUp(self):
        self.ident = identification.Identification.__new__(identification.Identification)     # no OCR process, no AI, no index
        self.ident.log_callback = None
        self.ident.ocr_enabled = True
        self.ident.card_identifier = None
        self.ident.foil_marker = None
        self.ident.settings = mock.Mock(get=lambda key, default=None: {'reading_mode': 'evidence'}.get(key, default))
        self.ident.evidence = FakeEvidence(dict(READING))
        self.ident._ai_foil_check = lambda foil_image: False
        self.ident.card_ocr = mock.Mock()
        self.ocr_read = {'name': 'Gandalf', 'collector_number': '', 'set_code': '', 'foil': 'unknown'}     # a read the game does not confirm
        self.ident.card_ocr.read_card.side_effect = lambda image, game: dict(self.ocr_read)
        self.game = FakeGame()
        patcher = mock.patch.object(identification, 'games')
        games = patcher.start()
        self.addCleanup(patcher.stop)
        games.active.side_effect = lambda: self.game
        games.active_id.return_value = 'mtg'
        self.image = np.zeros((100, 70, 3), np.uint8)

    def mode(self, mode):
        self.ident.settings = mock.Mock(get=lambda key, default=None: mode if key == 'reading_mode' else default)

    # ----- the mode
    def test_the_mode_is_evidence_by_default_and_anything_unclear_is_the_cascade(self):
        # (changed 2026-10-20: the picture and the strip before the vision AI is the DESIGN - the AI was only ever there because OCR was weak - so it is the
        # default; a choice that is not understood falls to the old reading, never to the new one)
        for given, want in (('evidence', 'evidence'), (' Evidence ', 'evidence'), ('cascade', 'cascade'), ('nonsense', 'cascade'), (None, 'cascade'), ('', 'cascade')):
            self.mode(given)
            self.assertEqual(self.ident.reading_mode, want, repr(given))
        self.ident.settings = None
        self.assertEqual(self.ident.reading_mode, identification.Config.READING_MODE)       # config.yaml's default
        self.assertEqual(identification.Config.READING_MODE, 'evidence')
        self.ident.settings = mock.Mock(get=lambda key, default=None: default)             # nothing chosen: the default
        self.assertEqual(self.ident.reading_mode, 'evidence')
        self.assertTrue(self.ident.accept_image_alone)                                     # ... and a clear picture settles a card by itself (since 2026-10-20: the picture is first)

    # ----- a card the OCR did not confirm
    def test_a_decided_card_is_returned_with_its_printing_and_never_goes_to_the_ai(self):
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertIsNone(task)
        self.assertEqual((info['name'], info['collector_number'], info['set_code']), ('Gandalf', '242', 'hob'))
        self.assertEqual((info['printing_id'], info['reader'], info['foil']), ('p3', printing_check.EVIDENCE_READER, 'foil'))

    def test_the_finish_becomes_the_foil_field_the_server_has_always_taken(self):
        for finish, foil in (('foil', 'foil'), ('regular', 'non-foil'), ('unknown', 'unknown')):
            self.ident.evidence.reading = {**READING, 'finish': {'finish': finish, 'source': 'x'}}
            self.assertEqual(self.ident._read_with_ocr(self.image, self.image)[0]['foil'], foil, finish)

    def test_an_unknown_finish_leaves_the_ai_foil_check_when_it_is_possible(self):
        self.ident.evidence.reading = {**READING, 'finish': {'finish': 'unknown', 'source': 'none'}}
        self.ident._ai_foil_check = lambda foil_image: True
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual((info['foil'], task), ('unknown', identification.FOIL))

    def test_the_setting_for_the_picture_alone_is_passed_on(self):
        with mock.patch.object(identification.Config, 'READING_ACCEPT_IMAGE_ALONE', False):
            self.ident._read_with_ocr(self.image, self.image)
        self.assertFalse(self.ident.evidence.reads[0][2])

    def test_the_flat_card_is_what_the_strip_is_cut_from(self):
        flat = np.ones((100, 70, 3), np.uint8)
        self.ident._read_with_ocr(self.image, flat)
        self.assertIs(self.ident.evidence.reads[0][1], flat)

    def test_what_the_picture_cannot_decide_goes_to_the_ai_as_before(self):
        self.ident.evidence.reading = None
        self.ident.card_identifier = object()
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual((info, task), (self.ocr_read, identification.IDENTIFY))

    def test_without_an_ai_an_undecided_card_is_as_before(self):
        self.ident.evidence.reading = None
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual((info, task), (self.ocr_read, None))          # the name OCR read, for review

    def test_a_failing_reading_is_a_reason_to_ask_the_ai_never_a_lost_card(self):
        self.ident.evidence.error = RuntimeError('index went away')
        self.ident.card_identifier = object()
        self.assertEqual(self.ident._read_with_ocr(self.image, self.image)[1], identification.IDENTIFY)

    # ----- the vision AI switched off (Settings): what OCR and the picture cannot decide goes to review, nothing is asked of any AI
    def ai_off(self):
        self.ident.ai_enabled = False
        identifier = mock.Mock()
        self.ident.card_identifier = identifier            # a provider IS set up: the switch, not a missing key, is why it is not asked
        return identifier

    def test_with_the_ai_off_an_undecided_card_is_not_sent_to_it(self):
        identifier = self.ai_off()
        self.ident.evidence.reading = None                 # the picture cannot decide it either
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertIsNone(task)                            # no IDENTIFY: _finish with what OCR read, which goes to review under its name
        self.assertEqual(info['name'], 'Gandalf')
        identifier.identify_card.assert_not_called()

    def test_with_the_ai_off_the_picture_still_decides(self):
        identifier = self.ai_off()
        info, task = self.ident._read_with_ocr(self.image, self.image)                         # READING: a strong decision
        self.assertEqual((info['printing_id'], task), ('p3', None))
        identifier.identify_card.assert_not_called()

    def test_with_the_ai_off_there_is_no_foil_check_by_the_ai_either(self):
        self.ai_off()
        self.assertFalse(self.ident._ai_foil_check(np.zeros((10, 10, 3), np.uint8)))

    def test_a_card_already_waiting_for_the_ai_when_it_is_switched_off_comes_back_as_read(self):
        identifier = self.ai_off()
        ocr_info = {'name': 'Gandalf', 'collector_number': '', 'set_code': '', 'foil': 'unknown'}
        self.assertIs(self.ident._ask_ai(identification.IDENTIFY, self.image, self.image, ocr_info), ocr_info)
        identifier.identify_card.assert_not_called()
        identifier.read_foil_symbol.assert_not_called()

    def test_with_the_ai_on_nothing_changes(self):
        self.ident.card_identifier = object()
        self.ident.evidence.reading = None
        self.assertEqual(self.ident._read_with_ocr(self.image, self.image)[1], identification.IDENTIFY)

    def test_a_stale_index_takes_the_picture_alone_away_and_a_covering_one_gives_it_back(self):
        # (Codex review, 2026-10-20: with the picture alone on by default, a printing the index lacks would be added as its best match - another printing)
        with mock.patch.object(identification.Identification, 'accept_image_alone', new_callable=mock.PropertyMock, return_value=True):
            for missing, allowed in ((0, True), (3, False), (None, False)):          # covers / lacks three printings / cannot say
                self.ident.evidence.missing = missing
                self.ident._read_by_picture(self.image, self.image)
                self.assertEqual(self.ident.evidence.reads[-1][2], allowed, missing)
        with mock.patch.object(identification.Identification, 'accept_image_alone', new_callable=mock.PropertyMock, return_value=False):
            self.ident.evidence.missing = 0
            self.ident._read_by_picture(self.image, self.image)
            self.assertFalse(self.ident.evidence.reads[-1][2])                          # the person's "off" still wins over a covering index

    def test_with_ocr_switched_off_the_picture_is_asked_without_a_reader(self):
        # the person switched OCR off to try the picture alone: no strip is read - the reader is not even handed over - and what the picture cannot
        # decide goes to the AI at once
        self.ident.ocr_enabled = False
        self.ident.card_identifier = object()
        self.ident.evidence.reading = None                      # the picture cannot decide
        self.assertEqual(self.ident._read_with_ocr(self.image, self.image)[1], identification.IDENTIFY)
        self.assertEqual(len(self.ident.evidence.reads), 1)
        self.assertIsNone(self.ident.evidence.ocr_given[-1])

    def test_with_ocr_switched_off_the_picture_alone_can_add_a_card_when_allowed(self):
        self.ident.ocr_enabled = False
        self.ident.card_identifier = object()
        self.ident.evidence.reading = {**READING, 'path': 'image', 'strip': {'number': '', 'set': ''}, 'finish': {'finish': 'unknown', 'source': 'none'}}
        with mock.patch.object(identification.Identification, 'accept_image_alone', new_callable=mock.PropertyMock, return_value=True):    # the person's setting
            info, task = self.ident._read_by_picture(self.image, self.image)
        self.assertTrue(self.ident.evidence.reads[-1][2])                                # the setting reached the reading
        self.assertEqual((info['printing_id'], info['reader']), ('p3', printing_check.PICTURE_READER))        # the picture alone: no collector line took part
        self.assertIsNone(self.ident.evidence.ocr_given[-1])

    def test_without_the_printing_check_the_default_reads_as_the_cascade_to_the_last_call(self):
        # the default mode is 'evidence', so a server without the printing check (and every test of one stage) must not reach for what only evidence needs
        self.confirmed_ocr()
        self.ident.evidence = None
        self.game.confirmed_card = None                                          # evidence's look-up: calling it would fail
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual((info['collector_number'], task), ('242', None))        # read by confirmed_read, as in the cascade

    def test_without_the_printing_check_it_is_cascade(self):
        self.ident.evidence = None
        self.ident.card_identifier = object()
        self.assertEqual(self.ident._read_with_ocr(self.image, self.image)[1], identification.IDENTIFY)

    # ----- cascade, and a card the OCR confirmed, never reach it
    def test_in_cascade_mode_the_picture_is_never_asked(self):
        self.mode('cascade')
        self.ident.card_identifier = object()
        self.assertEqual(self.ident._read_with_ocr(self.image, self.image)[1], identification.IDENTIFY)
        self.assertEqual(self.ident.evidence.reads, [])

    # ----- the picture first; in doubt the OCR, which then reads as before - unless the picture is another printing of that very card (Secret Lair)
    def confirmed_ocr(self):
        """The OCR confirms its read - and the picture, which comes first, is in doubt (the cases below that follow the OCR path)"""
        self.game = FakeGame(confirm=True)
        self.ocr_read.update(collector_number='242', set_code='hob', foil='non-foil')
        self.ident.evidence.reading = None

    def test_in_doubt_the_picture_leaves_the_card_to_the_ocr_which_reads_it_as_before(self):
        # (the contract changed on 2026-10-20, twice: the picture was never asked about a confirmed card; then it was asked ONE thing about it; now it comes FIRST and a card
        # it does not settle is the OCR's, read exactly as before, with that one question about the printing it read)
        self.confirmed_ocr()
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual((info['name'], info['collector_number'], info['foil'], task), ('Gandalf', '242', 'non-foil', None))
        self.assertNotIn('printing_id', info)
        self.assertEqual(len(self.ident.evidence.reads), 1)                                # the picture was tried first, and did not settle it
        self.assertEqual(self.ident.evidence.contradictions, [(self.image, 'ocr-printing')])   # the one question, about the printing the OCR read

    # ----- the order: picture, then OCR in doubt, then the AI - with each of the two switched off
    def test_a_card_the_picture_settles_is_never_read_by_the_ocr(self):
        self.confirmed_ocr()
        self.ident.evidence.reading = READING                                              # the picture decides it
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual((info['printing_id'], task), ('p3', None))
        self.ident.card_ocr.read_card.assert_not_called()                                  # the full-card OCR never ran
        self.assertEqual(self.ident.evidence.contradictions, [])                           # and nothing is asked about a printing it read

    def test_a_secret_lair_the_picture_settles_is_added_as_the_picture_says_whatever_the_ocr_would_read(self):
        # (replaces: "an OCR read the picture contradicts is read by the picture when that decides" - the picture is first now, so the wrong confirmation is never reached)
        # (the Deadpool Bolt: its strip names an existing printing - the Game Night card - that the picture clearly is not; the picture decides, once the full-card OCR has read
        # the same name - since a fourth Codex review of 2026-10-20 a contradicting strip is not overruled unconfirmed)
        self.confirmed_ocr()
        self.ident.evidence.reading = {**READING, 'path': 'image', 'strip': {'number': '42', 'set': 'gn2'}}
        with mock.patch.object(identification.Identification, 'accept_image_alone', new_callable=mock.PropertyMock, return_value=True):
            info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual((info['printing_id'], info['reader']), ('p3', printing_check.PICTURE_READER))
        self.ident.card_ocr.read_card.assert_called_once()                         # the OCR's name ('Gandalf') agreed with the picture's card

    def test_ocr_agrees_needs_the_name_and_no_contradicting_collector_line(self):
        # (fifth Codex review: a name alone let the picture's printing A stand where the OCR had read printing B of the same card)
        card = {'name': 'Gandalf the Grey', 'flavor_name': None, 'set_code': 'hob', 'number': '242', 'oracle_id': 'o-gandalf'}
        printings = {('hob', '245'): {'name': 'Gandalf the Grey', 'oracle_id': 'o-gandalf'},                        # another printing of the same card
                     ('ltr', '242'): {'name': 'Gandalf the Grey', 'oracle_id': 'o-gandalf'},
                     ('gn2', '42'): {'name': 'Lightning Strike', 'oracle_id': 'o-strike'},                          # another card's printing
                     ('spm', '69'): {'name': 'Swarm, Being of Bees', 'oracle_id': 'o-swarm'}}
        def printing_at(set_code, number, exact=False):
            return printings.get((set_code.lower(), number.lstrip('0') or '0'))
        def agrees(read, of=card):
            return printing_check.ocr_agrees(read, of, printing_at)
        self.assertTrue(agrees({'name': 'Gandalf the Grey', 'collector_number': '0242', 'set_code': 'HOB'}))     # the same printing (zeros and case aside)
        self.assertTrue(agrees({'name': 'Gandalf the Grey', 'collector_number': '', 'set_code': ''}))            # nothing of the line read: the name decides
        self.assertTrue(agrees({'name': 'Gandalf the Grey', 'collector_number': '242', 'set_code': ''}))         # a part of the line names no printing
        self.assertFalse(agrees({'name': 'Gandalf the Grey', 'collector_number': '0245', 'set_code': 'HOB'}))    # ANOTHER printing of the same card (Codex)
        self.assertFalse(agrees({'name': 'Gandalf the Grey', 'collector_number': '242', 'set_code': 'LTR'}))     # same number, another set: the same card again
        self.assertFalse(agrees({'name': 'Gandalf the Grey', 'collector_number': '0009', 'set_code': 'ZZZ'}))    # a printing the card data does not have
        self.assertFalse(agrees({'name': 'Saruman', 'collector_number': '', 'set_code': ''}))                    # another name, no line: refused
        self.assertFalse(agrees({'name': 'Saruman', 'collector_number': '245', 'set_code': 'HOB'}))              # another name, and the line names another printing
        # another name, but the whole line names EXACTLY the printing the picture chose (since 2026-10-21, decide.confirm_by_ocr): the picture and the collector line
        # agree on the printing - the evidence a 'strong' decision of rule A2 is made on - and the name is taken as misread
        self.assertTrue(agrees({'name': 'Saruman', 'collector_number': '242', 'set_code': 'HOB'}))
        # and a picture far enough ahead needs no name at all, its line still checked (decide.LEAD_WAIVES_NAME)
        self.assertTrue(printing_check.ocr_agrees({'name': 'Saruman', 'collector_number': '', 'set_code': ''}, card, printing_at, lead=0.125))
        self.assertFalse(printing_check.ocr_agrees({'name': 'Saruman', 'collector_number': '245', 'set_code': 'HOB'}, card, printing_at, lead=0.125))
        # a line that names a DIFFERENT card says nothing about this card's printing: a Secret Lair's printed line, an OCR misread of the number
        bolt = {'name': 'Lightning Bolt', 'flavor_name': None, 'set_code': 'sld', 'number': 'IFIYW-2', 'oracle_id': 'o-bolt'}
        self.assertTrue(agrees({'name': 'Lightning', 'collector_number': '042', 'set_code': 'GN2'}, bolt))
        shocker = {'name': 'Shocker, Unshakable', 'flavor_name': None, 'set_code': 'spm', 'number': '89', 'oracle_id': 'o-shocker'}
        self.assertTrue(agrees({'name': 'Shocker, Unshakable', 'collector_number': '0069', 'set_code': 'SPM'}, shocker))
        self.assertTrue(agrees({'name': 'Elektra, Deadly Assassin', 'collector_number': '67', 'set_code': 'MAR'},
                               {'name': 'Massacre Girl', 'flavor_name': 'Elektra, Deadly Assassin', 'set_code': 'mar', 'number': '67'}))   # the flavor name
        def broken(set_code, number, exact=False):
            raise RuntimeError('database is locked')
        self.assertFalse(printing_check.ocr_agrees({'name': 'Gandalf the Grey', 'collector_number': '245', 'set_code': 'HOB'}, card, broken))   # cannot say: no
        for nothing in (None, {}, {'name': ''}):
            self.assertFalse(agrees(nothing))

    def test_the_picture_alone_is_not_added_when_ocr_reads_another_printing_of_the_same_card(self):
        self.ident.evidence.reading = {**READING, 'path': 'image', 'strip': {'number': '', 'set': ''}}            # the picture chose Gandalf [hob 242]
        self.ident.evidence.printings[('ltr', '301')] = {'name': 'Gandalf', 'oracle_id': 'o-p3'}                    # LTR 301: another printing of Gandalf
        self.ocr_read.update(name='Gandalf', collector_number='0301', set_code='LTR')                               # the OCR read that one
        with mock.patch.object(identification.Identification, 'accept_image_alone', new_callable=mock.PropertyMock, return_value=True):
            self.assertIsNone(self.ident._read_by_picture(self.image, self.image))
            self.ocr_read.update(collector_number='0242', set_code='HOB')                                           # ... the one the picture chose: added
            self.assertEqual(self.ident._read_by_picture(self.image, self.image)[0]['printing_id'], 'p3')

    def test_an_upside_down_cards_confirmation_reads_it_upright(self):
        # (a Codex review of 2026-10-21: the strip was read the way up the search found the card, the full card as given - parse_magic found no title at the
        # top of an upside-down card, and its decision was refused for that alone) - _ocr_names reads the card turned as live_read says
        image = np.arange(100 * 70 * 3, dtype=np.uint8).reshape(100, 70, 3)              # every pixel different: which way up is visible
        seen = []
        self.ident.card_ocr.read_card.side_effect = lambda picture, game: seen.append(picture.copy()) or dict(self.ocr_read)
        with mock.patch.object(identification.Identification, 'accept_image_alone', new_callable=mock.PropertyMock, return_value=True):
            self.ident.evidence.reading = {**READING, 'path': 'image', 'strip': {'number': '', 'set': ''}, 'lead': 0.0625, 'rotated': True}
            self.ident._read_by_picture(image, image)
            np.testing.assert_array_equal(seen[-1], cv2.rotate(image, cv2.ROTATE_180))
            self.ident.evidence.reading['rotated'] = False                               # upright: as given
            self.ident._read_by_picture(image, image)
            np.testing.assert_array_equal(seen[-1], image)

    def test_a_collector_number_with_a_letter_suffix_names_its_own_printing(self):
        # (a Codex review of 2026-10-21: the lookup kept only the digits, so a line saying ZEN 233a was taken for the chosen ZEN 233 - both Plains are in the
        # index - and the same-card check never saw the contradiction) - printing_check.printing_named_by_line keeps the suffix first
        plains = {('zen', '233'): {'id': 'p233', 'name': 'Plains', 'set_code': 'zen', 'number': '233', 'oracle_id': 'o-plains'},
                  ('zen', '233a'): {'id': 'p233a', 'name': 'Plains', 'set_code': 'zen', 'number': '233a', 'oracle_id': 'o-plains'}}

        def lookup(set_code, number, exact=False):
            if exact:
                return plains.get((set_code.lower(), number))
            digits = ''.join(c for c in number if c.isdigit()).lstrip('0') or '0'      # what collector_number_variants does: the digits
            return plains.get((set_code.lower(), digits))
        named = lambda number: (printing_check.printing_named_by_line(lookup, 'ZEN', number) or {}).get('id')
        self.assertEqual(named('233a'), 'p233a')
        self.assertEqual(named('0233a'), 'p233a')          # leading zeros off, the suffix kept
        self.assertEqual(named('233'), 'p233')
        self.assertEqual(named('0233'), 'p233')
        self.assertIsNone(named('233z'))                   # a suffix no printing has: unknown, never the base printing (fail closed)
        # ... and the full-card OCR's line check sees ZEN 233a as ANOTHER printing of the chosen ZEN 233's card
        chosen = plains[('zen', '233')]
        printing_at = lambda set_code, number: printing_check.printing_named_by_line(lookup, set_code, number)
        self.assertEqual(printing_check.ocr_facts({'name': 'Plains', 'collector_number': '233a', 'set_code': 'ZEN'}, chosen, printing_at)['line_names'], 'same_card')
        self.assertFalse(printing_check.ocr_agrees({'name': 'Plains', 'collector_number': '233a', 'set_code': 'ZEN'}, chosen, printing_at))
        # card data OLDER than the card: 233 there, 233a not (a second Codex review) - the line is a printing the card data lacks, never the chosen 233
        del plains[('zen', '233a')]
        self.assertIsNone(named('233a'))
        self.assertEqual(printing_check.ocr_facts({'name': 'Plains', 'collector_number': '233a', 'set_code': 'ZEN'}, chosen, printing_at)['line_names'], 'unknown')
        self.assertFalse(printing_check.ocr_agrees({'name': 'Plains', 'collector_number': '233a', 'set_code': 'ZEN'}, chosen, printing_at))

    def test_a_strip_naming_another_existing_card_and_an_ocr_that_disagrees_keep_the_picture_from_adding_it(self):
        # (fourth Codex review: a readable strip that contradicts the picture skipped the name check)
        # (lead 0.0625: a picture not sure enough for its lead alone to stand in for the name - decide.LEAD_WAIVES_NAME, 2026-10-21)
        self.ident.evidence.reading = {**READING, 'path': 'image', 'strip': {'number': '42', 'set': 'gn2'}, 'lead': 0.0625}
        self.ocr_read.update(name='Lightning Strike')                              # OCR reads what the strip says, not what the picture chose
        with mock.patch.object(identification.Identification, 'accept_image_alone', new_callable=mock.PropertyMock, return_value=True):
            self.assertIsNone(self.ident._read_by_picture(self.image, self.image))

    def test_a_name_and_a_line_both_naming_another_card_refuse_the_picture_whatever_its_lead(self):
        # (a Codex review of 2026-10-21: the lead waiver let a picture far ahead overrule a full-card OCR that read another card's name, where a whole collector line -
        # the strip's or the OCR's own - names that same other card: two readings agreeing on a different card is a contradiction, not a misread)
        self.ident.evidence.printings[('gn2', '42')] = {'name': 'Lightning Strike', 'oracle_id': 'o-strike'}
        self.ident.evidence.reading = {**READING, 'path': 'image', 'strip': {'number': '42', 'set': 'gn2'}, 'lead': 0.25}
        self.ocr_read.update(name='Lightning Strike')                              # the strip's line and the OCR's name: Lightning Strike, not the picture's Gandalf
        with mock.patch.object(identification.Identification, 'accept_image_alone', new_callable=mock.PropertyMock, return_value=True):
            self.assertIsNone(self.ident._read_by_picture(self.image, self.image))
            # the same with the OCR's OWN line naming it, and no strip
            self.ident.evidence.reading['strip'] = {'number': '', 'set': ''}
            self.ocr_read.update(collector_number='042', set_code='GN2')
            self.assertIsNone(self.ident._read_by_picture(self.image, self.image))
            # ... while another card's line under a name that is NOT that card's (a misread number, words of the text box) is still no evidence: the lead stands
            self.ocr_read.update(name='WASHINGTONDC')
            self.assertEqual(self.ident._read_by_picture(self.image, self.image)[0]['printing_id'], 'p3')

    def test_a_strip_naming_another_printing_of_the_same_card_refuses_the_picture_whatever_its_lead(self):
        # (a second Codex review of 2026-10-21: the strip's line was only checked against the name - a strip naming printing B of the card whose printing A the picture
        # chose let the lead add A, with an OCR that read nothing or another name. The lead says which card, never which printing: the strip's line refuses it)
        self.ident.evidence.printings[('hob', '245')] = {'id': 'p4', 'name': 'Gandalf', 'oracle_id': 'o-gandalf'}
        self.ident.evidence.reading = {**READING, 'path': 'image', 'strip': {'number': '245', 'set': 'hob'}, 'lead': 0.25,
                                       'card': {**READING['card'], 'oracle_id': 'o-gandalf'}}
        with mock.patch.object(identification.Identification, 'accept_image_alone', new_callable=mock.PropertyMock, return_value=True):
            self.ocr_read.update(name='Worn')                                                    # another name, no line
            self.assertIsNone(self.ident._read_by_picture(self.image, self.image))
            self.ident.card_ocr.read_card.side_effect = lambda image, game: None                 # nothing read at all
            self.assertIsNone(self.ident._read_by_picture(self.image, self.image))
            self.ident.card_ocr.read_card.side_effect = lambda image, game: dict(self.ocr_read, name='Gandalf')   # even the card's own name
            self.assertIsNone(self.ident._read_by_picture(self.image, self.image))
            # ... and the same strip naming the printing the picture chose is no contradiction: the lead stands
            self.ident.evidence.reading['strip'] = {'number': '242', 'set': 'hob'}
            self.ocr_read.update(name='Worn')
            self.ident.card_ocr.read_card.side_effect = lambda image, game: dict(self.ocr_read)
            self.assertEqual(self.ident._read_by_picture(self.image, self.image)[0]['printing_id'], 'p3')

    # ----- the picture alone with nothing from the strip: with OCR on, its name must agree (third Codex review: a card newer than the card data)
    def picture_alone_without_strip(self):
        # READING's card: 'Gandalf'. Lead 0.0625: over the picture alone's IMAGE_LEAD, under decide.LEAD_WAIVES_NAME - the name is still needed (since 2026-10-21 a
        # clearer lead stands in for it: see the tests of the waiver below)
        self.ident.evidence.reading = {**READING, 'path': 'image', 'strip': {'number': '', 'set': ''}, 'lead': 0.0625}
        return mock.patch.object(identification.Identification, 'accept_image_alone', new_callable=mock.PropertyMock, return_value=True)

    def test_the_picture_alone_is_added_when_ocr_reads_the_same_name(self):
        self.ocr_read.update(name='Gandalf')
        with self.picture_alone_without_strip():
            info, task = self.ident._read_by_picture(self.image, self.image)
        self.assertEqual(info['printing_id'], 'p3')
        self.ident.card_ocr.read_card.assert_called_once()

    def test_the_picture_alone_is_not_added_when_ocr_reads_another_name(self):
        self.ocr_read.update(name='Dwalin, Weaponmaster')                          # a card newer than the card data: its best match was another card
        with self.picture_alone_without_strip():
            self.assertIsNone(self.ident._read_by_picture(self.image, self.image))

    def test_the_picture_alone_is_not_added_when_ocr_reads_nothing_or_fails(self):
        with self.picture_alone_without_strip():
            self.ident.card_ocr.read_card.side_effect = lambda image, game: None
            self.assertIsNone(self.ident._read_by_picture(self.image, self.image))
            self.ident.card_ocr.read_card.side_effect = RuntimeError('reader gone')
            self.assertIsNone(self.ident._read_by_picture(self.image, self.image))

    def test_a_picture_far_enough_ahead_needs_no_name_but_its_line_is_still_checked(self):
        # decide.LEAD_WAIVES_NAME (2026-10-21): the Deadpool Sol Ring prints 'Worn Powerstone' - no OCR read of its name can confirm it, the picture leads by 0.12-0.19
        with self.picture_alone_without_strip():
            self.ident.evidence.reading['lead'] = 0.125
            self.ocr_read.update(name='Worn', set_code='SCD', collector_number='')
            info, _task = self.ident._read_by_picture(self.image, self.image)
            self.assertEqual(info['printing_id'], 'p3')
            self.ident.card_ocr.read_card.side_effect = lambda image, game: None                     # nothing read: the lead alone, as with OCR off
            self.assertEqual(self.ident._read_by_picture(self.image, self.image)[0]['printing_id'], 'p3')
            # ... but a whole line naming ANOTHER printing of the same card still refuses: the lead says which card, not which printing
            self.ident.card_ocr.read_card.side_effect = lambda image, game: {'name': 'Worn', 'collector_number': '245', 'set_code': 'HOB', 'foil': 'unknown'}
            self.ident.evidence.printings[('hob', '245')] = {'id': 'p4', 'name': 'Gandalf', 'oracle_id': 'o-gandalf'}
            self.ident.evidence.reading['card'] = {**READING['card'], 'oracle_id': 'o-gandalf'}
            self.assertIsNone(self.ident._read_by_picture(self.image, self.image))

    def test_a_line_naming_the_very_printing_confirms_without_the_name(self):
        # a picture under the waiver's lead, a misread name, and the whole collector line naming exactly the printing the picture chose: two readings agree
        self.ocr_read.update(name='Gandalph', set_code='HOB', collector_number='242')
        with self.picture_alone_without_strip():
            info, _task = self.ident._read_by_picture(self.image, self.image)
        self.assertEqual(info['printing_id'], 'p3')

    def test_with_ocr_off_the_picture_alone_is_not_asked_to_agree_with_anything(self):
        # the person's 'picture only' configuration: nothing else to agree with - its trade-off, documented
        self.ident.ocr_enabled = False
        with self.picture_alone_without_strip():
            info, task = self.ident._read_by_picture(self.image, self.image)
        self.assertEqual(info['printing_id'], 'p3')
        self.ident.card_ocr.read_card.assert_not_called()

    def test_a_strip_decision_is_not_asked_to_agree_with_the_ocr(self):
        self.ident.evidence.reading = READING                                       # 'strong': the collector line named it
        info, task = self.ident._read_by_picture(self.image, self.image)
        self.assertEqual(info['printing_id'], 'p3')
        self.ident.card_ocr.read_card.assert_not_called()

    def test_in_doubt_an_unconfirmed_ocr_read_goes_to_the_ai_and_with_the_ai_off_to_review(self):
        self.ident.evidence.reading = None
        self.ident.card_identifier = object()
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual((info['name'], task), ('Gandalf', identification.IDENTIFY))      # picture -> OCR (not confirmed) -> AI
        self.ident.ai_enabled = False
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual((info['name'], task), ('Gandalf', None))                          # picture -> OCR -> review under the name it read

    def test_ocr_and_ai_both_off_is_the_picture_alone_then_review(self):
        self.ident.ocr_enabled = False
        self.ident.ai_enabled = False
        self.ident.card_identifier = object()
        self.ident.evidence.reading = None
        self.assertEqual(self.ident._read_with_ocr(self.image, self.image), (None, None))  # nothing read, nothing asked: review
        self.ident.card_ocr.read_card.assert_not_called()
        self.ident.evidence.reading = {**READING, 'path': 'image', 'strip': {'number': '', 'set': ''}}
        with mock.patch.object(identification.Identification, 'accept_image_alone', new_callable=mock.PropertyMock, return_value=True):
            info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual((info['printing_id'], task), ('p3', None))                        # a clear picture still adds the card

    def test_only_the_ocr_off_is_the_picture_then_the_ai(self):
        self.ident.ocr_enabled = False
        self.ident.card_identifier = object()
        self.ident.evidence.reading = None
        self.assertEqual(self.ident._read_with_ocr(self.image, self.image), (None, identification.IDENTIFY))
        self.ident.card_ocr.read_card.assert_not_called()

    def test_the_label_says_whether_the_collector_line_took_part(self):
        self.ident.evidence.reading = READING                                              # path 'strong': the strip named the printing
        self.assertEqual(self.ident._read_with_ocr(self.image, self.image)[0]['reader'], printing_check.EVIDENCE_READER)
        self.ident.evidence.reading = {**READING, 'path': 'image', 'strip': {'number': '', 'set': ''}}
        with mock.patch.object(identification.Identification, 'accept_image_alone', new_callable=mock.PropertyMock, return_value=True):
            self.assertEqual(self.ident._read_with_ocr(self.image, self.image)[0]['reader'], printing_check.PICTURE_READER)
        self.assertEqual(printing_check.PICTURE_READERS, (printing_check.PICTURE_READER, printing_check.EVIDENCE_READER))

    def test_the_picture_is_asked_with_the_flat_card(self):
        self.confirmed_ocr()
        flat = np.ones((100, 70, 3), np.uint8)
        self.ident._read_with_ocr(self.image, flat)
        self.assertIs(self.ident.evidence.contradictions[0][0], flat)

    def test_an_ocr_read_the_picture_contradicts_is_no_longer_confirmed_and_goes_to_review_without_an_ai(self):
        self.confirmed_ocr()
        self.ident.evidence.contradicted = True
        self.ident.evidence.reading = None
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertIsNone(task)
        self.assertEqual((info['name'], info['collector_number'], info['set_code']), ('Gandalf', '', ''))     # the name only: review shows the card
        self.assertEqual(info['foil'], 'non-foil')

    def test_an_ocr_read_the_picture_contradicts_goes_to_the_ai_when_there_is_one(self):
        self.confirmed_ocr()
        self.ident.evidence.contradicted = True
        self.ident.evidence.reading = None
        self.ident.card_identifier = object()
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual(task, identification.IDENTIFY)
        self.assertEqual(info['collector_number'], '')

    # ----- Deadpool's Secret Lair `Lightning Bolt`: a sticker hides "Bolt", the printed line is the Game Night card's, and the OCR confirms Lightning Strike
    def partly_read(self, disagreed):
        self.game = FakeGame(confirm=True, card_name='Lightning Strike')
        self.ocr_read.update(name='Lightning', collector_number='042', set_code='GN2', foil='non-foil')
        self.ident.evidence.disagreed = disagreed
        self.ident.evidence.reading = None
        self.ident.card_identifier = object()

    def test_a_name_read_in_part_and_a_picture_that_looks_like_another_card_is_not_a_confirmed_read(self):
        self.partly_read(disagreed=True)
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual(task, identification.IDENTIFY)                                        # on to the AI (or review without one), never an add
        self.assertEqual((info['name'], info['collector_number'], info['set_code']), ('Lightning', '', ''))
        self.assertEqual(self.ident.evidence.disagreements, [(self.image, 'ocr-printing')])

    def test_a_name_read_in_part_stays_confirmed_when_the_picture_agrees(self):
        self.partly_read(disagreed=False)                                                      # 15 of the 18 such reads in the evaluation set
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual((info['collector_number'], info['set_code'], task), ('042', 'GN2', None))
        self.assertEqual(len(self.ident.evidence.disagreements), 1)

    def test_a_whole_name_is_never_doubted_for_a_picture_that_looks_like_something_else(self):
        # a foil's glare or a showcase background disagrees for 0.67% of the correctly read cards: that alone is not a reason
        self.confirmed_ocr()
        self.ident.evidence.disagreed = True
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual((info['collector_number'], task), ('242', None))
        self.assertEqual(self.ident.evidence.disagreements, [])                                # not even asked

    def test_the_picture_is_asked_with_the_flat_card_for_the_partly_read_name_too(self):
        self.partly_read(disagreed=False)
        flat = np.ones((100, 70, 3), np.uint8)
        self.ident._read_with_ocr(self.image, flat)
        self.assertIs(self.ident.evidence.disagreements[0][0], flat)

    def test_cascade_mode_does_not_ask(self):
        self.partly_read(disagreed=True)
        self.mode('cascade')
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual((info['collector_number'], task), ('042', None))                      # as before the picture matching existed

    def test_a_failing_check_leaves_the_ocr_read_standing(self):
        self.confirmed_ocr()
        self.ident.evidence.contradicted = RuntimeError('index went away')
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual((info['collector_number'], task), ('242', None))

    def test_without_the_printing_check_or_in_cascade_the_ocr_read_is_not_questioned(self):
        self.confirmed_ocr()
        self.ident.evidence.contradicted = True
        self.mode('cascade')
        self.game.confirmed_card = None                                          # cascade never asks for it: confirmed_read is the old look-up
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual((info['collector_number'], task), ('242', None))
        self.assertEqual(self.ident.evidence.contradictions, [])
        self.mode('evidence')
        self.game.confirmed_card = lambda *a, **k: {'id': 'ocr-printing'}
        self.ident.evidence = None
        self.assertEqual(self.ident._read_with_ocr(self.image, self.image)[0]['collector_number'], '242')

    # ----- the AI's answer against the picture
    def asked(self, **extra):
        return {'name': 'Gandalf', 'collector_number': '242', 'set_code': 'hob', 'foil': 'unknown', **extra}

    def test_an_ai_answer_the_picture_clearly_contradicts_goes_to_review(self):
        self.game = FakeGame(found={'id': 'p2'})
        self.ident.evidence.verdict = 'disagree'
        got = self.ident._checked_against_picture(self.image, self.asked())
        self.assertEqual((got['name'], got['collector_number'], got['set_code']), ('Gandalf', '', ''))      # the name stays: review shows the card
        self.assertEqual(self.ident.evidence.verdicts, ['p2'])

    def test_an_ai_answer_the_picture_agrees_with_or_cannot_judge_stands(self):
        self.game = FakeGame(found={'id': 'p2'})
        for verdict in ('agree', 'weak', 'not_indexed', None):
            self.ident.evidence.verdict = verdict
            self.assertEqual(self.ident._checked_against_picture(self.image, self.asked()), self.asked(), verdict)

    def test_an_ai_answer_the_database_does_not_know_stands(self):
        self.game = FakeGame(found=None)
        self.assertEqual(self.ident._checked_against_picture(self.image, self.asked()), self.asked())
        self.assertEqual(self.ident.evidence.verdicts, [])

    def test_a_failing_check_leaves_the_ai_answer_alone(self):
        self.game = FakeGame(found={'id': 'p2'})
        self.ident.evidence.verdict = RuntimeError('no index')
        self.assertEqual(self.ident._checked_against_picture(self.image, self.asked()), self.asked())

    def test_in_cascade_mode_the_ai_answer_is_not_checked(self):
        self.mode('cascade')
        self.game = FakeGame(found={'id': 'p2'})
        self.ident.evidence.verdict = 'disagree'
        self.assertEqual(self.ident._checked_against_picture(self.image, self.asked()), self.asked())
        self.assertEqual(self.ident.evidence.verdicts, [])

    def test_nothing_read_by_the_ai_is_nothing_to_check(self):
        self.assertIsNone(self.ident._checked_against_picture(self.image, None))
        self.assertEqual(self.ident._checked_against_picture(self.image, {'name': ''}), {'name': ''})

    def test_the_ai_stage_applies_the_check_to_its_answer(self):
        identifier = mock.Mock(provider='x', model='y')
        identifier.identify_card.return_value = {'name': 'Gandalf', 'collector_number': '242', 'set_code': 'hob'}
        self.ident.card_identifier = identifier
        self.game = FakeGame(found={'id': 'p2'})
        self.ident.evidence.verdict = 'disagree'
        got = self.ident._ask_ai(identification.IDENTIFY, self.image, None, None)
        self.assertEqual((got['name'], got['collector_number'], got['set_code']), ('Gandalf', '', ''))
        self.assertIs(self.ident.evidence.verdict_images[0], self.image)             # no separate flat card: the capture itself

    def test_the_ai_answer_is_checked_against_the_flat_card_when_there_is_one(self):
        identifier = mock.Mock(provider='x', model='y')
        identifier.identify_card.return_value = {'name': 'Gandalf', 'collector_number': '242', 'set_code': 'hob'}
        self.ident.card_identifier = identifier
        self.game = FakeGame(found={'id': 'p2'})
        flat = np.ones((100, 70, 3), np.uint8)
        self.ident._ask_ai(identification.IDENTIFY, self.image, flat, None)
        self.assertIs(self.ident.evidence.verdict_images[0], flat)


class Routing(unittest.TestCase):
    """A printing the picture and the strip decided on is taken by its id - the lookup by name and number cannot be trusted for it"""

    def route(self, printing_id, identified=None, by_printing=None):
        from card_scanner.server import captures
        game = mock.MagicMock()
        game.identify.return_value = identified
        game.card_by_printing.return_value = by_printing
        game.is_confirmed.side_effect = lambda card: bool(card) and card.get('match') in ('picture', 'set_number')
        patches = [mock.patch.object(captures, name, value) for name, value in (
            ('printing_check', mock.MagicMock()), ('games', mock.MagicMock(**{'active.return_value': game})), ('log_to_client', mock.Mock()),
            ('log_scanned_card', mock.Mock()), ('get_ai_model_info', mock.Mock(return_value='ai')), ('desk', mock.MagicMock()),
            ('queue_for_review', mock.Mock(return_value='not confirmed')), ('station_prefix', mock.Mock(return_value='')),
            ('add_automatically', mock.Mock(return_value={'id': 'added'})))]
        mocks = {}
        for p in patches:
            mocks[p.attribute] = p.start()
            self.addCleanup(p.stop)
        outcome = captures.search_and_emit_card('Gandalf', '242', was_fast_scan_mode=True, set_code='hob', image_path='c.jpg', station='phone',
                                               reader=printing_check.EVIDENCE_READER, printing_id=printing_id)
        return outcome, game, mocks

    def test_the_decided_printing_is_added_without_a_search_by_name(self):
        card = {'id': 'p3', 'name': 'Gandalf', 'match': 'picture'}
        outcome, game, mocks = self.route('p3', by_printing=card)
        self.assertEqual(outcome['status'], 'added')
        game.card_by_printing.assert_called_once_with('p3')
        game.identify.assert_not_called()
        self.assertIs(mocks['add_automatically'].call_args[0][1], card)

    def test_without_a_printing_id_it_is_the_search_by_name_as_always(self):
        outcome, game, mocks = self.route(None, identified={'id': 'p1', 'match': 'set_number'})
        self.assertEqual(outcome['status'], 'added')
        game.card_by_printing.assert_not_called()
        game.identify.assert_called_once()

    def test_a_printing_id_the_database_lacks_falls_back_to_the_search(self):
        outcome, game, mocks = self.route('p3', identified={'id': 'p1', 'match': 'set_number'}, by_printing=None)
        game.identify.assert_called_once()
        self.assertEqual(outcome['status'], 'added')

    def test_a_confirmed_printing_whose_finish_nothing_read_waits_for_review(self):
        # (the owner's choice, 2026-10-21: a confetti foil whose star the marker reading missed went in as regular) - Game.finish_known decides; the real rule here
        from card_scanner.games.mtg import Magic
        magic = Magic(mock.Mock())
        both = {'id': 'p3', 'name': 'Lightning Bolt', 'match': 'picture', 'finishes': ['nonfoil', 'foil']}
        outcome, game, mocks = self.route_with(both, magic, foil='unknown')
        self.assertEqual(outcome['status'], 'review')
        mocks['add_automatically'].assert_not_called()
        self.assertEqual(mocks['queue_for_review'].call_args.kwargs['why'], 'finish not read (printed in foil and non-foil)')
        # ... the marker read, or a printing made in one finish only: added as before
        self.assertEqual(self.route_with(both, magic, foil='foil')[0]['status'], 'added')
        self.assertEqual(self.route_with(both, magic, foil='non-foil')[0]['status'], 'added')
        self.assertEqual(self.route_with({**both, 'finishes': ['nonfoil']}, magic, foil='unknown')[0]['status'], 'added')
        self.assertEqual(self.route_with({**both, 'finishes': ['foil']}, magic, foil='unknown')[0]['status'], 'added')

    def route_with(self, card, magic, foil):
        """search_and_emit_card for a decided printing `card`, with the real Magic.finish_known and the marker `foil`"""
        from card_scanner.server import captures
        game = mock.MagicMock()
        game.card_by_printing.return_value = card
        game.is_confirmed.side_effect = lambda c: bool(c) and c.get('match') in ('picture', 'set_number')
        game.finish_known.side_effect = magic.finish_known
        mocks = {}
        for name, value in (('printing_check', mock.MagicMock()), ('games', mock.MagicMock(**{'active.return_value': game})), ('log_to_client', mock.Mock()),
                            ('log_scanned_card', mock.Mock()), ('get_ai_model_info', mock.Mock(return_value='ai')), ('desk', mock.MagicMock()),
                            ('queue_for_review', mock.Mock(return_value='finish not read (printed in foil and non-foil)')),
                            ('station_prefix', mock.Mock(return_value='')), ('add_automatically', mock.Mock(return_value={'id': 'added'}))):
            patcher = mock.patch.object(captures, name, value)
            mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)
        outcome = captures.search_and_emit_card(card['name'], '2', was_fast_scan_mode=True, set_code='sld', image_path='c.jpg', station='phone',
                                               foil=foil, reader=printing_check.PICTURE_READER, printing_id=card['id'])
        return outcome, game, mocks

    def test_the_decided_printing_travels_from_what_identification_read(self):
        from card_scanner.server import captures
        with mock.patch.object(captures, 'route_identified', return_value={'status': 'added'}) as route, \
                mock.patch.object(captures, 'socketio'), mock.patch.object(captures, 'desk', mock.MagicMock()), \
                mock.patch.object(captures, 'get_ai_model_info', return_value='ai'):
            captures.announce_and_route('c.jpg', {'name': 'Gandalf', 'collector_number': '242', 'set_code': 'hob', 'foil': 'foil',
                                                  'printing_id': 'p3', 'reader': printing_check.EVIDENCE_READER}, 1, 'mtg')
        self.assertEqual(route.call_args.kwargs['printing_id'], 'p3')


class Magic(unittest.TestCase):
    def test_confirmed_card_is_confirmed_read_with_the_card_from_one_look_up(self):
        from card_scanner.games.mtg import Magic as Game
        db = mock.Mock()
        db.search_card_exact.side_effect = lambda name, number, set_code: {'id': 'x', 'match': 'set_number'} if set_code == 'hob' else {'id': 'y', 'match': 'name'}
        game = Game(db)
        self.assertEqual(game.confirmed_card('Gandalf', '242', 'hob'), {'id': 'x', 'match': 'set_number'})
        self.assertTrue(game.confirmed_read('Gandalf', '242', 'hob'))                       # the old yes / no, unchanged
        self.assertIsNone(game.confirmed_card('Gandalf', '242', 'zzz'))                     # found, but not confirmed
        self.assertFalse(game.confirmed_read('Gandalf', '242', 'zzz'))
        db.search_card_exact.reset_mock()
        self.assertIsNone(game.confirmed_card('Gandalf', '', 'hob'))                        # every confirmed match needs the number
        db.search_card_exact.assert_not_called()

    def test_a_decided_printing_is_a_confirmed_match_and_nothing_else_is_tagged_so(self):
        from card_scanner.core.database import CONFIRMED_MATCHES
        from card_scanner.games.mtg import Magic as Game
        db = mock.Mock()
        db.get_card_by_id.side_effect = lambda i: {'id': i, 'name': 'X'} if i == 'p3' else None
        game = Game(db)
        card = game.card_by_printing('p3')
        self.assertEqual(card['match'], 'picture')
        self.assertTrue(game.is_confirmed(card))
        self.assertIsNone(game.card_by_printing('nope'))
        self.assertIn('picture', CONFIRMED_MATCHES)
        # the tags the searches give are as they were
        self.assertEqual(CONFIRMED_MATCHES - {'picture'}, {'set_number', 'name_number', 'name_set_digit'})


if __name__ == '__main__':
    unittest.main()
