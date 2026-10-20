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

    def __init__(self, finishes=None):
        self.finishes = finishes or {}

    def get_card_by_id(self, printing_id):
        if printing_id == 'p9':
            return None
        return {'id': printing_id, 'name': f'Card {printing_id}', 'set_code': 'tst', 'number': printing_id[1:], 'oracle_id': f'o-{printing_id}',
                'finishes': self.finishes.get(printing_id, ['nonfoil', 'foil'])}

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
        self.assertIsNone(self.check.live_read(picture, None, FakeReader([])))              # OFF unless asked for (Codex review, second finding)
        self.assertFalse(inspect.signature(printing_check.PrintingCheck.live_read).parameters['accept_image_alone'].default)
        self.assertFalse(Config.READING_ACCEPT_IMAGE_ALONE)
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
            self.assertIsNotNone(check.live_read(picture, None, FakeReader(strip_lines(9)), accept_image_alone=True))        # a printing the database lacks too (p9): no reason to stop
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

    def test_a_card_read_by_picture_and_strip_is_not_looked_at_by_the_shadow_check(self):
        # the picture chose it: comparing the picture with it would only compare the choice with itself (circular evidence for the exit criteria)
        shadow = printing_check.PrintingCheck(Cards(), self.root / 'index', self.root / 'shadow.jsonl', 'shadow', 'all')
        capture = self.root / 'capture.jpg'
        cv2.imwrite(str(capture), cv2.cvtColor(degraded(synthetic_card(3)), cv2.COLOR_RGB2BGR))
        shadow.observe(capture, {'id': 'p3'}, 'phone', printing_check.EVIDENCE_READER)
        self.assertTrue(shadow.wait())
        self.assertFalse((self.root / 'shadow.jsonl').exists())
        shadow.observe(capture, {'id': 'p3'}, 'phone', 'qwen3.5:9b')                      # ... while the AI's answer still is
        self.assertTrue(shadow.wait())
        self.assertTrue((self.root / 'shadow.jsonl').exists())

    def test_verdict_of_says_whether_the_picture_agrees_with_an_answer(self):
        self.assertEqual(self.check.verdict_of(degraded(synthetic_card(3)), 'p3'), 'agree')
        self.assertEqual(self.check.verdict_of(degraded(synthetic_card(3)), 'p2'), 'disagree')
        self.assertIsNone(self.make(Cards(), self.root / 'no-index').verdict_of(degraded(synthetic_card(3)), 'p3'))


class FakeGame:
    """The game as _read_with_ocr and _ask_ai use it: `confirm` says which OCR reads are confirmed; identify gives the AI's answer a printing"""

    def __init__(self, confirm=False, found=None):
        self.confirm, self.found = confirm, found

    def confirmed_read(self, name, number, set_code):
        return self.confirm

    def identify(self, name, number=None, set_code=None, ai_model=None):
        return self.found


class FakeEvidence:
    """What Identification is given as `evidence`: a reading it was told to give (or to raise), and a verdict"""

    def __init__(self, reading=None, error=None, verdict='agree'):
        self.reading, self.error, self.verdict = reading, error, verdict
        self.reads, self.verdicts, self.verdict_images = [], [], []

    def live_read(self, image, card_image, ocr, foil_net, accept_image_alone):
        self.reads.append((image, card_image, accept_image_alone))
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
    def test_the_mode_is_cascade_unless_evidence_is_asked_for(self):
        for given, want in (('evidence', 'evidence'), (' Evidence ', 'evidence'), ('cascade', 'cascade'), ('nonsense', 'cascade'), (None, 'cascade'), ('', 'cascade')):
            self.mode(given)
            self.assertEqual(self.ident.reading_mode, want, repr(given))
        self.ident.settings = None
        self.assertEqual(self.ident.reading_mode, identification.Config.READING_MODE)       # config.yaml's default
        self.assertEqual(identification.Config.READING_MODE, 'cascade')                     # ... which is the old behaviour

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

    def test_with_ocr_switched_off_the_picture_is_not_used(self):
        # the strip is read by the OCR reader: no OCR, no strip
        self.ident.ocr_enabled = False
        self.ident.card_identifier = object()
        self.assertEqual(self.ident._read_with_ocr(self.image, self.image)[1], identification.IDENTIFY)
        self.assertEqual(self.ident.evidence.reads, [])

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

    def test_a_card_the_ocr_confirmed_is_read_as_before_in_evidence_mode_too(self):
        self.game = FakeGame(confirm=True)
        self.ocr_read.update(collector_number='242', set_code='hob', foil='non-foil')
        info, task = self.ident._read_with_ocr(self.image, self.image)
        self.assertEqual((info['name'], info['foil'], task), ('Gandalf', 'non-foil', None))
        self.assertNotIn('printing_id', info)
        self.assertEqual(self.ident.evidence.reads, [])

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

    def test_the_decided_printing_travels_from_what_identification_read(self):
        from card_scanner.server import captures
        with mock.patch.object(captures, 'route_identified', return_value={'status': 'added'}) as route, \
                mock.patch.object(captures, 'socketio'), mock.patch.object(captures, 'desk', mock.MagicMock()), \
                mock.patch.object(captures, 'get_ai_model_info', return_value='ai'):
            captures.announce_and_route('c.jpg', {'name': 'Gandalf', 'collector_number': '242', 'set_code': 'hob', 'foil': 'foil',
                                                  'printing_id': 'p3', 'reader': printing_check.EVIDENCE_READER}, 1, 'mtg')
        self.assertEqual(route.call_args.kwargs['printing_id'], 'p3')


class Magic(unittest.TestCase):
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
