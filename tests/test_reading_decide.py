"""
core/reading/decide.py: the decision (rule A2) as a pure function of plain data.

Two files of cases for rule A2 (and one each for the finish and for the guards - tests/data/reading_finish_cases.json, reading_guard_cases.json - both
hand-written), made from different sources on purpose (docs/plans/READING_REFACTOR_PLAN.md, "How phase 2 is checked"):
  tests/data/reading_decision_cases.json    HAND-WRITTEN: each edge with the answer a person working the rule out by hand expects;
  tests/data/reading_regression_cases.json  CAPTURED from the previous implementation (PrintingCheck.lookup as committed in a84dcb9, before it was
                                            rebuilt on `decide`) on real pictures: the evidence it used and the answer it gave. Ids and similarities only.
A file made by running the new `decide` would only test itself. Another program that decides the same way - the Android app, later - must pass the same files.

    venv/bin/python -m unittest discover tests -p test_reading_decide.py
"""
import ast
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

from card_scanner.core.reading import decide  # noqa: E402

DATA = Path(__file__).with_name('data')
HAND = json.loads((DATA / 'reading_decision_cases.json').read_text())['cases']
FINISH = json.loads((DATA / 'reading_finish_cases.json').read_text())['cases']
REGRESSION = json.loads((DATA / 'reading_regression_cases.json').read_text())['cases']
GUARD = json.loads((DATA / 'reading_guard_cases.json').read_text())['cases']


class HandWrittenCases(unittest.TestCase):
    def test_every_case_gives_the_answer_written_for_it(self):
        for case in HAND:
            with self.subTest(case['name']):
                answer = decide.decide_printing(case['picture'], case['strip'], case.get('thresholds'))
                self.assertEqual(answer['path'], case['expected']['path'], case['why'])
                self.assertEqual(answer['printing_id'], case['expected']['printing_id'], case['why'])
                if case['expected']['lead'] is None:
                    self.assertIsNone(answer['lead'])
                else:
                    self.assertAlmostEqual(answer['lead'], case['expected']['lead'], places=12)       # binary fractions: exact
                # the printing's similarity is the candidate's own; none for a review
                if answer['printing_id'] is None:
                    self.assertIsNone(answer['similarity'])
                else:
                    named = next(c for c in case['picture']['candidates'] if c['id'] == answer['printing_id'])
                    self.assertEqual(answer['similarity'], named['similarity'])

    def test_the_cases_cover_what_they_should(self):
        names = [c['name'] for c in HAND]
        self.assertEqual(len(names), len(set(names)), 'two cases with one name')
        self.assertGreaterEqual(len(HAND), 20)
        self.assertEqual({c['expected']['path'] for c in HAND}, {'strong', 'image', 'review'})
        for case in HAND:
            self.assertTrue(case['why'], case['name'])
            self.assertEqual(set(case), {'name', 'why', 'picture', 'strip', 'expected'} | ({'thresholds'} if 'thresholds' in case else set()), case['name'])


class RegressionCases(unittest.TestCase):
    """Real pictures: what the implementation before the refactor answered for the evidence it used"""

    def test_every_case_gives_the_answer_the_previous_implementation_gave(self):
        wrong = []
        for case in REGRESSION:
            answer = decide.decide_printing(case['picture'], case['strip'])
            expected = case['expected']
            # the previous code rounded the printing's similarity and the lead to four places when it logged them
            got = {'path': answer['path'], 'printing_id': answer['printing_id'],
                   'similarity': None if answer['similarity'] is None else round(answer['similarity'], 4),
                   'lead': None if answer['lead'] is None else round(answer['lead'], 4)}
            if got != expected:
                wrong.append((case['name'], got, expected))
        self.assertEqual(wrong, [])

    def test_the_cases_are_real_and_varied(self):
        self.assertGreaterEqual(len(REGRESSION), 100)
        paths = {c['expected']['path'] for c in REGRESSION}
        self.assertEqual(paths, {'strong', 'image', 'review'})
        for case in REGRESSION:
            self.assertEqual(len(case['picture']['candidates']), 5, case['name'])
            sims = [c['similarity'] for c in case['picture']['candidates']]
            self.assertEqual(sims, sorted(sims, reverse=True), f"{case['name']}: nearest first")
        # a case where the strip's printing looked different from the best match, and one where it was a reprint of the same art
        self.assertTrue(any(c['expected']['path'] == 'image' and c['strip']['number'] and c['strip']['set'] for c in REGRESSION))
        self.assertTrue(any(c['expected']['path'] == 'strong' and c['expected']['printing_id'] != c['picture']['candidates'][0]['id'] for c in REGRESSION))


class PurityAndTheNumber(unittest.TestCase):
    def test_the_inputs_are_not_changed(self):
        for case in HAND + REGRESSION[:20]:
            picture, strip = copy.deepcopy(case['picture']), copy.deepcopy(case['strip'])
            decide.decide_printing(picture, strip, case.get('thresholds'))
            self.assertEqual((picture, strip), (case['picture'], case['strip']), case['name'])

    def test_the_module_imports_nothing_but_typing(self):
        # "pure": no database, no index, no picture, no clock, no log - not even numpy. It is what lets another program pass the same files.
        tree = ast.parse(Path(decide.__file__).read_text())
        modules = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        modules += [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertEqual(sorted(set(modules)), ['typing'])

    def test_the_package_init_is_empty(self):
        init = Path(decide.__file__).with_name('__init__.py')
        statements = [n for n in ast.parse(init.read_text()).body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
        self.assertEqual(statements, [])

    def test_a_missing_picture_or_strip_is_a_review_not_an_error(self):
        self.assertEqual(decide.decide_printing(None, None)['path'], 'review')
        self.assertEqual(decide.decide_printing({}, {})['path'], 'review')
        self.assertEqual(decide.decide_printing({'candidates': []}, {'number': '1', 'set': 'tst'})['path'], 'review')

    def test_the_default_thresholds_are_the_measured_ones(self):
        # the exit criteria were written against rule A2 with these numbers
        self.assertEqual((decide.AGREE_BELOW, decide.IMAGE_LEAD), (0.02, 0.05))
        self.assertEqual(decide.THRESHOLDS, {'agree_below': 0.02, 'image_lead': 0.05})

    def test_plain_number(self):
        for given, want in (('0172', '172'), ('172', '172'), ('0', '0'), ('00', '0'), ('', '0'), (None, '0'), (' 7 ', '7'), ('12a', '12a'),
                            ('IFIYW-2', 'IFIYW-2'), (10, '10'), ('007b', '7b')):
            self.assertEqual(decide.plain_number(given), want, repr(given))


class Finish(unittest.TestCase):
    """decide_finish: its own rule, answered apart from rule A2. Expectations written by hand."""
    BOTH = ['nonfoil', 'foil']

    def test_every_case_of_the_shared_file_gives_its_answer(self):
        # tests/data/reading_finish_cases.json: hand-written, and passed by the Android app too (its copy is made by a script, never by hand)
        for case in FINISH:
            with self.subTest(case['name']):
                self.assertEqual(decide.decide_finish(case['finishes'], case['symbol'], case['net'], case.get('thresholds')), case['expected'])
        self.assertGreaterEqual(len(FINISH), 15)
        self.assertEqual(len({c['name'] for c in FINISH}), len(FINISH))

    def test_a_printing_in_one_finish_is_certain(self):
        # whatever the marker says: a foil-only printing is a foil, a non-foil-only one is regular
        self.assertEqual(decide.decide_finish(['foil'], 'non-foil', 0.01), {'finish': 'foil', 'source': 'printing'})
        self.assertEqual(decide.decide_finish(['etched']), {'finish': 'foil', 'source': 'printing'})
        self.assertEqual(decide.decide_finish(['nonfoil'], 'foil', 0.99), {'finish': 'regular', 'source': 'printing'})

    def test_a_symbol_the_ocr_read_stands_over_the_network(self):
        self.assertEqual(decide.decide_finish(self.BOTH, 'foil', 0.01), {'finish': 'foil', 'source': 'symbol'})
        self.assertEqual(decide.decide_finish(self.BOTH, 'non-foil', 0.99), {'finish': 'regular', 'source': 'symbol'})

    def test_the_network_is_believed_only_when_sure(self):
        self.assertEqual(decide.decide_finish(self.BOTH, 'unknown', 0.95), {'finish': 'foil', 'source': 'network'})
        self.assertEqual(decide.decide_finish(self.BOTH, 'unknown', 0.05), {'finish': 'regular', 'source': 'network'})
        self.assertEqual(decide.decide_finish(self.BOTH, 'unknown', 0.9), {'finish': 'foil', 'source': 'network'})          # the bound is included
        self.assertEqual(decide.decide_finish(self.BOTH, 'unknown', 0.5), {'finish': 'unknown', 'source': 'none'})

    def test_an_unread_marker_is_unknown_not_regular(self):
        # the live rule files it as regular; the evidence says it does not know
        self.assertEqual(decide.decide_finish(self.BOTH), {'finish': 'unknown', 'source': 'none'})
        self.assertEqual(decide.decide_finish(None), {'finish': 'unknown', 'source': 'none'})
        self.assertEqual(decide.decide_finish([], 'unknown', None), {'finish': 'unknown', 'source': 'none'})

    def test_thresholds_can_be_given(self):
        self.assertEqual(decide.decide_finish(self.BOTH, 'unknown', 0.7, {'sure': 0.6}), {'finish': 'foil', 'source': 'network'})

    def test_the_finish_does_not_touch_rule_a2(self):
        # the printing decision takes no finish: nothing about a symbol in the strip changes its answer
        case = HAND[0]
        plain = decide.decide_printing(case['picture'], case['strip'])
        with_foil = decide.decide_printing(case['picture'], {**case['strip'], 'symbol': 'foil', 'net': 0.99})
        self.assertEqual(plain, with_foil)




class Guards(unittest.TestCase):
    """accept_printing and confirm_by_ocr (the guards of a decision: step 5 of docs/plans/READING_REVIEW_2026-10-21_PLAN.md) on the hand-written file"""

    def test_every_case_of_the_shared_file_gives_its_answer(self):
        # tests/data/reading_guard_cases.json: hand-written, and passed by the Android app too (its copy is made by reading_reference.py, never by hand)
        for case in GUARD:
            if case['kind'] == 'accept':
                got = decide.accept_printing(case['decision'], case['facts'], case.get('thresholds'))
            else:
                got = decide.confirm_by_ocr(case['ocr'], case.get('lead'), case.get('thresholds'))
            self.assertEqual(got, case['expected'], case['name'])

    def test_every_guard_and_every_way_through_has_a_case(self):
        refused = {c['expected']['refused'] for c in GUARD}
        self.assertEqual(refused - {None}, set(decide.GUARDS))
        accepted = [c for c in GUARD if c['expected']['accepted']]
        self.assertTrue(any(c['kind'] == 'accept' and c['decision']['path'] == 'strong' for c in accepted))
        self.assertTrue(any(c['kind'] == 'accept' and c['decision']['path'] == 'image' for c in accepted))
        self.assertTrue(any(c['kind'] == 'accept' and c['expected']['needs_ocr'] for c in GUARD))
        self.assertTrue(any(c['kind'] == 'confirm' for c in accepted))

    def test_the_inputs_are_not_changed(self):
        for case in GUARD:
            given = copy.deepcopy(case)
            if case['kind'] == 'accept':
                decide.accept_printing(given['decision'], given['facts'])
            else:
                decide.confirm_by_ocr(given['ocr'], given.get('lead'))
            self.assertEqual(given, case, case['name'])

    def test_the_floors_are_the_servers(self):
        # the server passes printing_check's own floors at call time (where their measurements are documented); the defaults here must be the same numbers
        from card_scanner.core import printing_check
        self.assertEqual((decide.IMAGE_MIN_SIMILARITY, decide.STRONG_MIN_SIMILARITY),
                         (printing_check.IMAGE_MIN_SIMILARITY, printing_check.STRONG_MIN_SIMILARITY))
        self.assertEqual(decide.ACCEPT_THRESHOLDS, {'image_min_similarity': 0.5, 'strong_min_similarity': 0.30})
        self.assertEqual(decide.CONFIRM_THRESHOLDS, {'lead_waives_name': 0.10})     # measured: scripts/experiments/image_match/lead_waiver.py

    def test_the_log_writes_the_same_names(self):
        from card_scanner.core.reading import record
        self.assertIs(record.GUARDS, decide.GUARDS)


if __name__ == '__main__':
    unittest.main()
