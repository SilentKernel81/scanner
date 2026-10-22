"""
The reading log (core/reading/record.py: data/logs/reading.jsonl) - one record per capture of what each step of the reading saw and did, in either mode,
and the review's pick as a label (docs/plans/READING_REVIEW_2026-10-21_PLAN.md, step 2). It must record, and it must never cost a card:
  the record itself       its fields, the current record of a thread, handing it over to the router and writing it once
  PrintingCheck.live_read on the synthetic image index of test_reading_evidence.py: the picture, the strip, rule A2's answer, each guard by its name
  Identification          with a fake `evidence` (no OCR process, no AI, no index): the guards there, the OCR, the AI's verdict, cascade mode too
  the report              core/reading/report.py on a log written here

    venv/bin/python -m unittest discover tests -p test_reading_log.py
"""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))                 # the helpers of test_printing_check.py / test_reading_evidence.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from card_scanner.core import identification, image_index, printing_check  # noqa: E402
from card_scanner.core.reading import record, report  # noqa: E402
from test_printing_check import FakeReader, degraded, synthetic_card, write_pictures  # noqa: E402
from test_reading_evidence import READING, Cards, FakeEvidence, FakeGame, strip_lines  # noqa: E402


def lines_of(path):
    """The JSON lines of a log file ([] when there is none)"""
    path = Path(path)
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.is_file() else []


class TheRecord(unittest.TestCase):
    """ReadingRecord, ReadingLog and the thread's current record, on their own"""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'logs' / 'reading.jsonl'
        self.log = record.ReadingLog(self.path)

    def test_a_step_with_no_current_record_records_nothing_and_raises_nothing(self):
        record.note('picture', rotated=True)
        record.refuse(record.IMAGE_ALONE_OFF)
        self.assertIsNone(record.refused())
        record.complete({'name': 'x'}, {'status': 'added'})      # nothing handed over: nothing written
        self.assertEqual(lines_of(self.path), [])

    def test_notes_go_to_the_current_record_and_the_previous_one_comes_back(self):
        outer, inner = self.log.new(mode='evidence'), self.log.new(mode='cascade')
        with record.using(outer):
            with record.using(inner):
                record.note('ocr', name='Inner')
            record.note('ocr', name='Outer')
            record.refuse(record.BELOW_IMAGE_FLOOR)
            self.assertEqual(record.refused(), record.BELOW_IMAGE_FLOOR)
        self.assertEqual((outer.data['ocr'], inner.data['ocr']), ({'name': 'Outer'}, {'name': 'Inner'}))
        self.assertIsNone(record.current())

    def test_a_handed_over_record_is_completed_by_the_router_and_written_once(self):
        one = self.log.new(mode='evidence')
        one.finish({'name': 'Gandalf', 'reader': 'picture', 'printing_id': 'p3'})
        record.hand_over(one)
        record.complete({'name': 'Gandalf', 'reader': 'picture', 'printing_id': 'p3'}, {'status': 'added'}, capture='a.jpg', station='s1', capture_key='u1')
        record.flush_handed()                                      # the results thread looks again: nothing more is written
        written = lines_of(self.path)
        self.assertEqual(len(written), 1)
        self.assertEqual((written[0]['capture'], written[0]['station'], written[0]['capture_key']), ('a.jpg', 's1', 'u1'))
        self.assertEqual((written[0]['result']['outcome'], written[0]['result']['printing_id']), ('added', 'p3'))
        self.assertIn('total', written[0]['seconds'])

    def test_a_record_nobody_completes_is_written_without_an_outcome(self):
        one = self.log.new(mode='evidence')
        one.finish(None)
        record.hand_over(one)
        record.flush_handed()
        written = lines_of(self.path)
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]['result'], {'reader': None})

    def test_a_record_handed_over_and_forgotten_is_written_by_the_next_hand_over(self):
        first, second = self.log.new(n=1), self.log.new(n=2)
        record.hand_over(first)
        record.hand_over(second)                                   # (a caller of identify() that routes nothing: the prompt editor's test)
        record.complete(None, {'status': 'review'})
        self.assertEqual([r['n'] for r in lines_of(self.path)], [1, 2])

    def test_numpy_values_are_written_as_plain_numbers(self):
        one = self.log.new()
        one.note('picture', similarity=np.float32(0.5), rotated=np.bool_(True), rows=np.int64(3))
        one.write()
        picture = lines_of(self.path)[0]['picture']
        self.assertEqual((picture['similarity'], picture['rotated'], picture['rows']), (0.5, True, 3))

    def test_a_log_that_cannot_be_written_raises_nothing(self):
        self.path.parent.mkdir(parents=True)
        self.path.mkdir()                                          # a folder where the file should be: every write fails
        one = self.log.new()
        one.write()
        self.log.review_pick('r.jpg', 'p1', ['p1'])

    def test_the_log_is_renamed_beyond_its_limit(self):
        with mock.patch.object(record, 'LOG_LIMIT', 5):
            self.log.append({'n': 1})
            self.log.append({'n': 2})                              # the file is over 5 bytes now: renamed to .1 first
        self.assertEqual([r['n'] for r in lines_of(self.path)], [2])
        self.assertEqual([r['n'] for r in lines_of(self.path.with_name('reading.jsonl.1'))], [1])

    def test_the_review_pick_says_where_the_picked_printing_stood(self):
        self.log.review_pick('r1.jpg', 'p1', ['p1', 'p2'], station='s1', capture_key='u1')
        self.log.review_pick('r2.jpg', 'p2', ['p1', 'p2'])
        self.log.review_pick('r3.jpg', 'p7', ['p1', 'p2'])
        self.log.review_pick('r4.jpg', 'p7', [])
        picks = lines_of(self.path)
        self.assertEqual([p['rank'] for p in picks], [0, 1, None, None])
        self.assertTrue(all(p['review'] for p in picks))
        self.assertEqual((picks[0]['station'], picks[0]['capture_key']), ('s1', 'u1'))


class InLiveRead(unittest.TestCase):
    """PrintingCheck.live_read writes the picture, the strip, the decision and - by its name - the guard that refused it into the current record"""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        pictures = self.root / 'pictures'
        pictures.mkdir()
        write_pictures(pictures, range(6))
        cv2.imwrite(str(pictures / 'p6.jpg'), cv2.cvtColor(synthetic_card(2), cv2.COLOR_RGB2BGR))   # p6: a reprint of p2, the same art
        image_index.build([(f'p{n}', None) for n in range(7)], self.root / 'index', image_dir=pictures, fetch=False, log=lambda m: None)
        self.log = record.ReadingLog(self.root / 'reading.jsonl')

    def read(self, number=None, alone=True, database=None):
        """live_read of synthetic card 3 with a strip naming `number` (None: no strip), inside a record; (its answer, the record's data)"""
        check = printing_check.PrintingCheck(database or Cards(), self.root / 'index', self.root / 'log.jsonl', 'off', 'off')
        one = self.log.new()
        with record.using(one):
            answer = check.live_read(degraded(synthetic_card(3)), None, FakeReader(strip_lines(number)), None, alone)
        return answer, one.data

    def test_a_decided_card_has_its_candidates_its_strip_and_no_refusal(self):
        answer, data = self.read(3)
        self.assertEqual(answer['path'], 'strong')
        self.assertEqual(data['decision']['path'], 'strong')
        self.assertEqual(data['decision']['printing_id'], 'p3')
        self.assertEqual(data['picture']['candidates'][0]['id'], 'p3')
        self.assertEqual(len(data['picture']['candidates']), 5)
        self.assertEqual(set(data['picture']['candidates'][0]), {'id', 'set', 'number', 'similarity'})
        self.assertEqual((data['strip']['number'], data['strip']['set'], data['strip']['symbol']), ('3', 'tst', 'foil'))
        self.assertIsNone(data['refused'])

    def test_the_picture_alone_not_allowed_is_named(self):
        answer, data = self.read(None, alone=False)
        self.assertIsNone(answer)
        self.assertEqual((data['decision']['path'], data['refused']), ('image', record.IMAGE_ALONE_OFF))

    def test_a_picture_below_the_floor_is_named(self):
        with mock.patch.object(printing_check, 'IMAGE_MIN_SIMILARITY', 1.01):
            self.assertEqual(self.read(None)[1]['refused'], record.BELOW_IMAGE_FLOOR)
        with mock.patch.object(printing_check, 'STRONG_MIN_SIMILARITY', 1.01):
            self.assertEqual(self.read(3)[1]['refused'], record.BELOW_STRONG_FLOOR)

    def test_a_strip_naming_a_printing_the_index_lacks_is_named(self):
        # the strip names number 8: the card data has p8 (Cards makes one for any number but 9), the index does not
        self.assertEqual(self.read(8)[1]['refused'], record.STRIP_NAMES_UNINDEXED)

    def test_a_strip_naming_a_printing_the_card_data_lacks_is_named(self):
        self.assertEqual(self.read(9)[1]['refused'], record.STRIP_NAMES_UNKNOWN)

    def test_a_decided_printing_the_card_data_no_longer_has_is_named(self):
        class Gone(Cards):
            def get_card_by_id(self, printing_id):
                return None if printing_id == 'p3' else super().get_card_by_id(printing_id)
        self.assertEqual(self.read(None, database=Gone())[1]['refused'], record.CARD_NOT_IN_DATA)

    def test_a_review_decision_is_recorded_without_a_refusal(self):
        with mock.patch.object(printing_check, 'decide_printing', return_value={'path': 'review', 'printing_id': None, 'similarity': None, 'lead': 0.01}):
            answer, data = self.read(3)
        self.assertIsNone(answer)
        self.assertEqual((data['decision']['path'], data['refused']), ('review', None))


class Refusing(FakeEvidence):
    """FakeEvidence whose live_read, like the real one, names the guard when it is told not to take the picture alone"""

    def live_read(self, image, card_image, ocr, foil_net, accept_image_alone):
        answer = super().live_read(image, card_image, ocr, foil_net, accept_image_alone)
        if answer and answer['path'] == 'image' and not accept_image_alone:
            record.refuse(record.IMAGE_ALONE_OFF)
            return None
        return answer


class InIdentification(unittest.TestCase):
    """Identification.identify fills one record per capture and hands it to the router; what it decides stays what it was"""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'reading.jsonl'
        self.ident = identification.Identification.__new__(identification.Identification)     # no OCR process, no AI, no index
        self.ident.log_callback = None
        self.ident.ocr_enabled = True
        self.ident.card_identifier = None
        self.ident.foil_marker = None
        self.settings = {'reading_mode': 'evidence'}
        self.ident.settings = mock.Mock(get=lambda key, default=None: self.settings.get(key, default))
        self.ident.evidence = Refusing(dict(READING))
        self.ident._ai_foil_check = lambda foil_image: False
        self.ident.card_ocr = mock.Mock()
        self.ocr_read = {'name': 'Gandalf', 'collector_number': '', 'set_code': '', 'foil': 'unknown'}
        self.ident.card_ocr.read_card.side_effect = lambda image, game: dict(self.ocr_read) if self.ocr_read else None
        self.ident.reading_log = record.ReadingLog(self.path)
        self.game = FakeGame()
        patcher = mock.patch.object(identification, 'games')
        games = patcher.start()
        self.addCleanup(patcher.stop)
        games.active.side_effect = lambda: self.game
        games.active_id.return_value = 'mtg'
        self.image = np.zeros((100, 70, 3), np.uint8)
        self.addCleanup(record.flush_handed)           # nothing handed over is left for the next test

    def identify(self, outcome='added'):
        """identify(), then the router's completion as captures.announce_and_route does it: (card_info, the record written)"""
        info = self.ident.identify(self.image, self.image)
        record.complete(info, {'status': outcome}, capture='c.jpg', station='s1')
        written = lines_of(self.path)
        self.assertEqual(len(written), 1)
        return info, written[0]

    def test_a_card_the_picture_decided_is_one_record_with_its_configuration_and_outcome(self):
        info, data = self.identify()
        self.assertEqual(info['printing_id'], 'p3')
        self.assertEqual((data['mode'], data['ocr_on'], data['ai_on'], data['accept_image_alone']), ('evidence', True, False, True))
        self.assertEqual((data['index_covers'], data['index_missing']), (True, 0))
        self.assertEqual(data['decision'], {'accepted': True})
        self.assertIsNone(data['refused'])
        self.assertEqual((data['result']['reader'], data['result']['printing_id'], data['result']['outcome']), (printing_check.EVIDENCE_READER, 'p3', 'added'))
        self.assertEqual((data['capture'], data['station']), ('c.jpg', 's1'))
        self.assertIn('picture', data['seconds'])
        self.assertIsNone(data['ocr'])                  # the picture settled it: no full-card OCR was read

    def test_an_index_that_lacks_printings_is_named_as_the_reason_not_the_setting(self):
        self.ident.evidence.reading = {**READING, 'path': 'image'}
        self.ident.evidence.missing = 3
        _info, data = self.identify('review')
        self.assertEqual((data['refused'], data['refused_missing'], data['index_covers']), (record.INDEX_INCOMPLETE, 3, False))

    def test_the_picture_alone_switched_off_is_named(self):
        self.settings['reading_accept_image_alone'] = False
        self.ident.evidence.reading = {**READING, 'path': 'image'}
        _info, data = self.identify('review')
        self.assertEqual((data['refused'], data['index_covers']), (record.IMAGE_ALONE_OFF, None))

    def test_an_ocr_that_does_not_confirm_the_picture_alone_is_named_with_what_it_read(self):
        self.ident.evidence.reading = {**READING, 'path': 'image', 'lead': 0.0625}      # not sure enough for the lead to stand in for the name
        self.ocr_read = {'name': 'Frodo', 'collector_number': '17', 'set_code': 'LTR', 'foil': 'unknown'}
        info, data = self.identify('review')
        self.assertNotIn('printing_id', info)          # the picture's choice was not taken
        self.assertEqual((data['refused'], data['refused_ocr_name'], data['refused_ocr_line']), (record.OCR_DID_NOT_CONFIRM, 'Frodo', 'LTR 17'))
        self.assertEqual(data['ocr']['name'], 'Frodo')  # and the full-card read that followed

    def test_an_ocr_that_confirms_the_picture_alone_is_recorded_too(self):
        # (before 2026-10-21 only a refusal wrote the OCR's read: a picture-alone card the OCR confirmed showed no OCR at all)
        self.ident.evidence.reading = {**READING, 'path': 'image'}
        self.ocr_read = {'name': 'Gandalf', 'collector_number': '242', 'set_code': 'HOB', 'foil': 'unknown'}      # the very printing the picture chose
        info, data = self.identify()
        self.assertEqual(info['printing_id'], 'p3')
        self.assertIsNone(data['refused'])
        self.assertEqual((data['ocr']['name'], data['ocr']['set_code'], data['ocr']['collector_number']), ('Gandalf', 'HOB', '242'))
        self.assertEqual((data['ocr']['confirms_picture'], data['ocr']['name_matches'], data['ocr']['line_names']), (True, True, 'chosen'))

    def test_a_refusal_records_the_facts_it_was_made_on(self):
        self.ident.evidence.reading = {**READING, 'path': 'image', 'lead': 0.0625}      # not sure enough for the lead to stand in for the name
        self.ocr_read = {'name': 'Worn', 'collector_number': '', 'set_code': 'SCD', 'foil': 'unknown'}     # a Secret Lair printing another card's name
        _info, data = self.identify('review')
        self.assertEqual((data['ocr']['confirms_picture'], data['ocr']['name_matches'], data['ocr']['line_names']), (False, False, None))

    def test_an_ocr_that_read_nothing_is_named_with_nothing(self):
        self.ident.evidence.reading = {**READING, 'path': 'image', 'lead': 0.0625}      # not sure enough for the lead to stand in for the name
        self.ocr_read = None                            # the reader read nothing (or was down in its cool-down)
        _info, data = self.identify('review')
        self.assertEqual((data['refused'], data['refused_ocr_name'], data['refused_ocr_line']), (record.OCR_DID_NOT_CONFIRM, None, None))

    def test_an_ocr_confirmed_card_is_recorded_with_the_read_and_the_contradiction(self):
        self.ident.evidence.reading = None
        self.game = FakeGame(confirm=True)
        self.ocr_read = {'name': 'Gandalf', 'collector_number': '242', 'set_code': 'HOB', 'foil': 'foil'}
        _info, data = self.identify()
        self.assertEqual(data['ocr'], {'read': True, 'name': 'Gandalf', 'collector_number': '242', 'set_code': 'HOB', 'foil': 'foil', 'confirmed': True,
                                       'contradicted': False, 'why': None})
        self.ident.reading_log.path.unlink()
        self.ident.evidence.contradicted = True
        _info, data = self.identify('review')
        self.assertEqual((data['ocr']['contradicted'], data['ocr']['why']), (True, 'another printing of the card'))

    def test_an_ai_answer_the_picture_disagrees_with_is_recorded(self):
        self.ident.evidence.reading = None
        self.ident.evidence.verdict = 'disagree'
        self.game = FakeGame(found={'id': 'p5'})
        ai = mock.Mock()
        ai.identify_card.return_value = {'name': 'Gandalf', 'collector_number': '9', 'set_code': 'HOB'}
        self.ident.card_identifier = ai
        info, data = self.identify('review')
        self.assertEqual(info['set_code'], '')          # dropped, as before: the card goes to review
        self.assertEqual(data['ai']['asked'], identification.IDENTIFY)
        self.assertEqual(data['ai']['answer'], {'name': 'Gandalf', 'collector_number': '9', 'set_code': 'HOB'})
        self.assertEqual(data['ai']['verdict'], 'disagree')
        self.assertTrue(data['ai_on'])
        self.assertIn('ai', data['seconds'])

    def test_cascade_mode_records_too(self):
        self.settings['reading_mode'] = 'cascade'
        self.game = FakeGame(confirm=True)
        self.ocr_read = {'name': 'Gandalf', 'collector_number': '242', 'set_code': 'HOB', 'foil': 'foil'}
        _info, data = self.identify()
        self.assertEqual(data['mode'], 'cascade')
        self.assertEqual(self.ident.evidence.reads, [])  # the picture was not asked
        self.assertIsNone(data['picture'])
        self.assertTrue(data['ocr']['confirmed'])

    def test_a_log_that_cannot_be_written_loses_no_card(self):
        self.path.mkdir()                               # every write fails
        info = self.ident.identify(self.image, self.image)
        record.complete(info, {'status': 'added'})
        self.assertEqual(info['printing_id'], 'p3')

    def test_without_a_reading_log_nothing_is_recorded_and_the_card_is_the_same(self):
        self.ident.reading_log = None
        info = self.ident.identify(self.image, self.image)
        record.complete(info, {'status': 'added'})
        self.assertEqual(info['printing_id'], 'p3')
        self.assertFalse(self.path.exists())

    def test_the_queued_path_writes_one_record_through_the_results_thread(self):
        # The OCR worker's record travels on the Job; the results thread hands it to on_done, which completes it (here: a router that does not - then it is written anyway)
        job = identification.Job(self.image, self.image, 'mtg', on_done=lambda info, seconds: None)
        job.record = self.ident._new_record()
        with record.using(job.record):
            job.ocr_info, job.task = self.ident._read_with_ocr(job.image, job.foil_image)
        self.ident._running = True
        self.ident._done_queue = None
        # one turn of the results thread: _next gives the job once and stops the loop
        with mock.patch.object(self.ident, '_next', side_effect=lambda q: (setattr(self.ident, '_running', False), (job, job.ocr_info, 0.1))[1]):
            self.ident._done_worker()
        written = lines_of(self.path)
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]['result']['printing_id'], 'p3')
        self.assertNotIn('outcome', written[0]['result'])


class TheRouter(unittest.TestCase):
    """captures.announce_and_route completes the record handed over in its thread - and nothing of it goes into the outcome a phone parses"""

    def test_the_outcome_is_written_and_the_answer_is_unchanged(self):
        from card_scanner.server import captures
        with tempfile.TemporaryDirectory() as folder:
            log = record.ReadingLog(Path(folder) / 'reading.jsonl')
            one = log.new(mode='evidence')
            info = {'name': 'Gandalf', 'collector_number': '242', 'set_code': 'hob', 'foil': 'foil', 'printing_id': 'p3', 'reader': printing_check.EVIDENCE_READER}
            one.finish(info)
            record.hand_over(one)
            desk = mock.MagicMock(id='phone')
            with mock.patch.object(captures, 'route_identified', return_value={'status': 'review', 'reason': 'printing not confirmed'}), \
                    mock.patch.object(captures, 'socketio'), mock.patch.object(captures, 'desk', return_value=desk), \
                    mock.patch.object(captures, 'capture_key', return_value='uid-1'):
                outcome = captures.announce_and_route('/x/c.jpg', info, 1, 'mtg')
            written = lines_of(log.path)
        self.assertEqual(set(outcome), {'status', 'reason', 'read'})          # as before: the record is not in it
        self.assertEqual(len(written), 1)
        self.assertEqual((written[0]['capture'], written[0]['station'], written[0]['capture_key']), ('c.jpg', 'phone', 'uid-1'))
        self.assertEqual((written[0]['result']['outcome'], written[0]['result']['reason']), ('review', 'printing not confirmed'))


class TheReport(unittest.TestCase):
    """core/reading/report.py sums up a log: by mode, by reader, the refusals by guard, the review picks by rank"""

    def test_the_report_counts_what_the_log_says(self):
        with tempfile.TemporaryDirectory() as folder:
            log = record.ReadingLog(Path(folder) / 'reading.jsonl')
            for refused, reader, outcome in ((None, 'picture+strip', 'added'), (None, 'picture', 'added'), (record.OCR_DID_NOT_CONFIRM, 'light-ocr', 'added'),
                                             (record.BELOW_IMAGE_FLOOR, None, 'review')):
                one = log.new(mode='evidence', ocr_on=True, ai_on=False, accept_image_alone=True)
                one.data['refused'] = refused
                one.data['seconds'] = {'picture': 0.02, 'ocr': 0.1}
                one.finish({'name': 'x', 'reader': reader} if reader else None, {'status': outcome})
                one.write()
            log.review_pick('r.jpg', 'p1', ['p1', 'p2'])
            out = io.StringIO()
            summary = report.report(log.path, out=lambda line='': print(line, file=out))
        self.assertEqual(summary['captures'], 4)
        self.assertEqual(summary['by_reader'], {'picture+strip': 1, 'picture': 1, 'light-ocr': 1, 'none': 1})
        self.assertEqual(summary['refused'], {record.OCR_DID_NOT_CONFIRM: 1, record.BELOW_IMAGE_FLOOR: 1})
        self.assertEqual(summary['picks_by_rank'], {0: 1})
        self.assertIn('ocr_did_not_confirm', out.getvalue())

    def test_no_log_is_said(self):
        out = []
        self.assertIsNone(report.report('/nonexistent/reading.jsonl', out=out.append))
        self.assertTrue(out)


if __name__ == '__main__':
    unittest.main()
