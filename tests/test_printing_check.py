"""
The image index (core/image_index.py) and the printing check in shadow mode (core/printing_check.py), and the hook in
server/captures.py. Synthetic card pictures only (coloured blocks, scaled up): no card art in the repository, no network, no card database.

What is pinned here: the fingerprint is bit for bit what the experiment's own code made (so the experiment's numbers, in
docs/plans/IMAGE_MATCH_EXPERIMENT.md, apply to it); the index is written whole or not at all and refused when it is of another kind;
the verdicts (agree / disagree - another card or another printing of the card - / weak / not indexed, and a card put in upside down);
and that shadow mode changes nothing and never breaks a capture. How well the rule separates right from wrong labels was measured on
real pictures (scripts/experiments/image_match/calibrate_gap.py), not here.

    venv/bin/python -m unittest discover tests -p test_printing_check.py
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from card_scanner.core import image_index, printing_check  # noqa: E402


def synthetic_card(seed, width=488, height=680):
    """A deterministic card-shaped picture: coloured blocks, smoothly scaled up (scripts/experiments/image_match/pin_hash.py has the same function)"""
    blocks = np.random.default_rng(seed).integers(0, 256, (9, 6, 3), dtype=np.uint8)
    return cv2.resize(blocks, (width, height), interpolation=cv2.INTER_CUBIC)


def degraded(rgb):
    """What a camera makes of it: darker, softer, a little noise"""
    soft = cv2.GaussianBlur(rgb, (0, 0), 2.0).astype(np.float32) * 0.6
    noise = np.random.default_rng(0).normal(0, 4, rgb.shape)
    return np.clip(soft + noise, 0, 255).astype(np.uint8)


def files(folder):
    """The names in an index folder, without its lock files (.publishing, .building)"""
    return sorted(p.name for p in Path(folder).iterdir() if not p.name.startswith('.'))


def write_pictures(folder, seeds):
    """<id>.jpg of a synthetic card for each seed; ids are 'p<seed>'"""
    for seed in seeds:
        cv2.imwrite(str(Path(folder) / f'p{seed}.jpg'), cv2.cvtColor(synthetic_card(seed), cv2.COLOR_RGB2BGR))


# What the experiment's own code (common.prepare + embed.phash_bits, which built every number in the report) gives for
# synthetic_card(1234) and synthetic_card(99): scripts/experiments/image_match/pin_hash.py prints them and checks they agree with the shipped code
PINNED = {
    1234: 'e1fff48974b695dff190ee012bef1b00a4e61b474b00e4bddc760000dfe7f0289182e06ddb546c33b4ab9b44ab55d4b2c1fcd46cbe558b2a36ee31553132366c'
          'd499e0b9db114b57d1961b47ab0d7bd9118d7bcfee41864744c1e6dd80c908d9',
    99: 'fbbaa1f5d1b2db5510c38b496e39c49a2469c6a95f503ba891d431b60db2cdb281aa11aa9eaad1badb8adea26b32e1d6e192e1d33f575c56945244f2c0faf02a'
        'e0e1a4e7bb3e359e1b1940d39cc1d4d68b16c71d5619696d70e639d6428a176b',
}


class TheFingerprint(unittest.TestCase):
    def test_it_is_bit_for_bit_what_the_experiments_code_made(self):
        for seed, want in PINNED.items():
            self.assertEqual(image_index.hash_card(synthetic_card(seed)).hex(), want)

    def test_it_is_96_bytes_and_the_same_every_time(self):
        a = image_index.hash_card(synthetic_card(7))
        self.assertEqual(len(a), image_index.HASH_BYTES)
        self.assertEqual(a, image_index.hash_card(synthetic_card(7)))

    def test_a_degraded_picture_is_far_closer_to_its_card_than_to_another(self):
        index = image_index.ImageIndex([f'p{n}' for n in range(5)],
                                       np.array([np.frombuffer(image_index.hash_card(synthetic_card(n)), np.uint8) for n in range(5)]))
        similarity = image_index.similarity(index.distances(image_index.hash_card(degraded(synthetic_card(3)))))
        self.assertEqual(int(np.argmax(similarity)), 3)
        self.assertGreater(similarity[3] - np.delete(similarity, 3).max(), 0.2)

    def test_similarity_runs_from_minus_one_to_one(self):
        self.assertEqual(float(image_index.similarity(0)), 1.0)
        self.assertEqual(float(image_index.similarity(image_index.HASH_BITS)), -1.0)
        self.assertEqual(float(image_index.similarity(image_index.HASH_BITS // 2)), 0.0)


class TheIndex(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.pictures = Path(self.folder.name) / 'pictures'
        self.pictures.mkdir()
        self.index = Path(self.folder.name) / 'index'
        write_pictures(self.pictures, range(4))
        self.printings = [(f'p{n}', None) for n in range(4)] + [('nopicture', None)]
        self.log = []

    def build(self, printings=None, **kw):
        return image_index.build(printings or self.printings, self.index, image_dir=self.pictures, fetch=False, log=self.log.append, **kw)

    def test_a_build_keeps_the_printings_that_have_a_picture_in_the_given_order(self):
        self.assertEqual(self.build(), (4, 4, 1))               # in the index, new, left out for want of a picture
        loaded = image_index.ImageIndex.load(self.index)
        self.assertEqual(loaded.ids, ['p0', 'p1', 'p2', 'p3'])
        self.assertIsNone(loaded.row('nopicture'))
        self.assertEqual(loaded.row('p2'), 2)

    def test_a_card_is_nearest_to_itself_at_distance_zero(self):
        self.build()
        loaded = image_index.ImageIndex.load(self.index)
        # the picture as it is in the file the index was built from (a JPEG's compression alone moves ~10 of the 768 bits of a synthetic card)
        same_file = cv2.cvtColor(cv2.imread(str(self.pictures / 'p2.jpg')), cv2.COLOR_BGR2RGB)
        distances = loaded.distances(image_index.hash_card(same_file))
        self.assertEqual(int(distances[2]), 0)
        self.assertEqual(int(np.argmin(distances)), 2)

    def test_distances_are_the_number_of_differing_bits(self):
        self.build()
        loaded = image_index.ImageIndex.load(self.index)
        query = image_index.hash_card(synthetic_card(9))
        brute = [sum(bin(a ^ b).count('1') for a, b in zip(row.tobytes(), query)) for row in loaded.hashes]
        self.assertEqual(list(loaded.distances(query)), brute)
        # the table that stands in for np.bitwise_count on a numpy without it counts the same
        differing = np.bitwise_xor(loaded._words, np.frombuffer(query, np.uint64))
        self.assertEqual(list(image_index._POPCOUNT[differing.view(np.uint8)].sum(axis=1, dtype=np.int32)), brute)

    def test_a_second_build_hashes_only_what_is_new(self):
        self.build()
        write_pictures(self.pictures, [4])
        with mock.patch.object(image_index, 'hash_card', wraps=image_index.hash_card) as hashing:
            self.build(self.printings + [('p4', None)])
        self.assertEqual(hashing.call_count, 1)
        self.assertEqual(image_index.ImageIndex.load(self.index).ids, ['p0', 'p1', 'p2', 'p3', 'p4'])

    def manifest(self):
        return json.loads((self.index / image_index.MANIFEST).read_text())

    def test_nothing_is_left_half_written(self):
        self.build()
        manifest = self.manifest()
        self.assertEqual(files(self.index), sorted([image_index.MANIFEST, manifest['hashes'], manifest['ids']]))

    def test_an_index_that_is_missing_cut_short_or_of_another_kind_is_not_loaded(self):
        self.assertIsNone(image_index.ImageIndex.load(self.index))                        # no folder
        self.build()
        current = self.index / self.manifest()['hashes']
        current.write_bytes(current.read_bytes()[:-5])
        self.assertIsNone(image_index.ImageIndex.load(self.index))                        # cut short
        self.build(self.printings)                                                         # (the broken one is rebuilt whole)
        self.assertIsNotNone(image_index.ImageIndex.load(self.index))
        (self.index / image_index.MANIFEST).write_text(json.dumps({**self.manifest(), 'descriptor': 'something_else'}))
        self.assertIsNone(image_index.ImageIndex.load(self.index))                        # another descriptor: every fingerprint would be wrong

    def test_a_limit_makes_a_trial_run(self):
        self.assertEqual(self.build(limit=2)[1], 2)


class ThePublicationIsAtomic(unittest.TestCase):
    """image_index.write publishes a generation with ONE rename (the manifest): the review of 2026-10-20 found that replacing three files
    one after the other leaves a mix when it is interrupted, and a mix when a reader comes in between"""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.index = Path(self.folder.name)

    @staticmethod
    def generation(k, count=4):
        """ids and hashes of a generation that say which one it is: ids 'g<k>-<i>', every hash byte k"""
        return [f'g{k}-{i}' for i in range(count)], [bytes([k]) * image_index.HASH_BYTES for _ in range(count)]

    def publish(self, k, count=4):
        image_index.write(self.index, *self.generation(k, count))

    def interrupted(self, where):
        """os.replace failing for the files whose name starts with `where` (the manifest, or a generation's ids): a crash at that step"""
        real = os.replace

        def failing(source, target):
            if Path(target).name.startswith(where):
                raise OSError('interrupted')
            return real(source, target)
        return mock.patch.object(image_index.os, 'replace', failing)

    def assert_is_generation(self, k, count=4):
        loaded = image_index.ImageIndex.load(self.index)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.ids, self.generation(k, count)[0])
        self.assertTrue((loaded.hashes == k).all())

    def test_an_interruption_before_the_manifest_leaves_the_previous_generation_current_and_whole(self):
        self.publish(1)
        with self.interrupted(image_index.MANIFEST), self.assertRaises(OSError):
            self.publish(2)
        self.assert_is_generation(1)

    def test_an_interruption_between_the_two_files_does_too(self):
        self.publish(1)
        with self.interrupted('ids-'), self.assertRaises(OSError):
            self.publish(2)
        self.assert_is_generation(1)

    def test_the_next_build_clears_what_the_interrupted_one_left_and_publishes_normally(self):
        self.publish(1)
        with self.interrupted(image_index.MANIFEST), self.assertRaises(OSError):
            self.publish(2)
        self.assertGreater(len(files(self.index)), 3)                    # the failed generation's files are lying there
        self.publish(3)
        self.assert_is_generation(3)
        self.assertEqual(len(files(self.index)), 5)                      # manifest + the current and the previous generation (2 files each)
        self.assertEqual(len([n for n in files(self.index) if n.endswith('.tmp')]), 0)

    def test_only_the_current_and_the_previous_generation_are_kept(self):
        for k in range(1, 5):
            self.publish(k)
        names = sorted(p.name for p in self.index.iterdir())
        self.assertEqual(len([n for n in names if n.startswith('hashes-')]), 2)
        self.assertEqual(len([n for n in names if n.startswith('ids-')]), 2)
        self.assert_is_generation(4)

    def test_two_files_that_do_not_belong_together_are_refused_even_when_the_counts_agree(self):
        self.publish(1)
        first_ids = (self.index / json.loads((self.index / image_index.MANIFEST).read_text())['ids'])
        self.publish(2)
        manifest = json.loads((self.index / image_index.MANIFEST).read_text())
        # the current generation's ids replaced by the previous generation's: same count, other ids
        (self.index / manifest['ids']).write_bytes(first_ids.read_bytes())
        self.assertEqual(len((self.index / manifest['ids']).read_text().split()), manifest['count'])
        self.assertIsNone(image_index.ImageIndex.load(self.index))

    def test_the_older_format_is_cleared_when_the_new_one_is_published(self):
        for name in ('hashes.bin', 'ids.txt', 'meta.json'):
            (self.index / name).write_bytes(b'x')
        self.publish(1)
        self.assertFalse(any((self.index / name).exists() for name in ('hashes.bin', 'ids.txt', 'meta.json')))
        self.assert_is_generation(1)

    def test_a_manifest_that_names_a_path_is_refused(self):
        self.publish(1)
        manifest = json.loads((self.index / image_index.MANIFEST).read_text())
        (self.index / image_index.MANIFEST).write_text(json.dumps({**manifest, 'ids': '../escape.txt'}))
        self.assertIsNone(image_index.ImageIndex.load(self.index))

    def test_two_overlapping_writers_leave_a_usable_current_generation(self):
        # The review of 2026-10-20: two builders that both saw the same previous generation each cleaned up the other's files, the ones the
        # manifest pointed at included. Forced overlap, deterministically: A is held just before it publishes while B is started.
        self.publish(1)
        real = os.replace
        reached, release = threading.Event(), threading.Event()

        def pausing(source, target):
            if Path(target).name == image_index.MANIFEST and threading.current_thread().name == 'writer-a':
                reached.set()
                release.wait(10)
            return real(source, target)
        failures = []

        def run(k):
            try:
                self.publish(k)
            except Exception as e:                        # noqa: BLE001 - reported below
                failures.append(e)
        a = threading.Thread(target=run, args=(2,), name='writer-a')
        b = threading.Thread(target=run, args=(3,), name='writer-b')
        with mock.patch.object(image_index.os, 'replace', pausing):
            a.start()
            self.assertTrue(reached.wait(10))             # A has written its files and is about to publish
            b.start()
            b.join(0.5)
            self.assertTrue(b.is_alive(), 'B did not wait for A: two writers at once')
            release.set()
            a.join(10)
            b.join(10)
        self.assertEqual(failures, [])
        self.assert_is_generation(3)                      # B published last: its generation is current, and it loads
        manifest = json.loads((self.index / image_index.MANIFEST).read_text())
        self.assertTrue((self.index / manifest['hashes']).is_file() and (self.index / manifest['ids']).is_file())

    def test_a_second_build_at_the_same_time_is_refused(self):
        pictures = self.index / 'pictures'
        pictures.mkdir()
        write_pictures(pictures, range(2))
        with image_index._building(self.index):           # a build is running
            with self.assertRaises(image_index.BuildRunning):
                image_index.build([('p0', None), ('p1', None)], self.index, image_dir=pictures, fetch=False, log=lambda m: None)
        # and when it is over the next one runs
        self.assertEqual(image_index.build([('p0', None), ('p1', None)], self.index, image_dir=pictures, fetch=False, log=lambda m: None)[0], 2)

    def test_a_reader_loading_while_another_publishes_never_sees_a_mix(self):
        self.publish(1)
        stop = threading.Event()
        errors = []

        def publisher():
            try:
                for k in range(2, 80):
                    self.publish(k, count=20)
            except Exception as e:            # noqa: BLE001 - reported below
                errors.append(e)
            finally:
                stop.set()
        thread = threading.Thread(target=publisher)
        thread.start()
        loaded_count, none_count = 0, 0
        while not stop.is_set() or loaded_count + none_count < 50:
            loaded = image_index.ImageIndex.load(self.index)
            if loaded is None:
                none_count += 1               # the generation it was told about was cleared before it read it: allowed, never wrong
                continue
            loaded_count += 1
            generation = int(loaded.ids[0].split('-')[0][1:])
            self.assertTrue(all(i.startswith(f'g{generation}-') for i in loaded.ids), 'ids of two generations')
            self.assertTrue((loaded.hashes == generation).all(), 'hashes of another generation than the ids')
        thread.join()
        self.assertEqual(errors, [])
        self.assertGreater(loaded_count, none_count)


class FakeDatabase:
    """get_card_by_id for the printings of the tests: p0 and p1 are two printings of one card (oracle 'A'), the others are other cards"""

    def get_card_by_id(self, printing_id):
        oracle = 'A' if printing_id in ('p0', 'p1') else f'oracle-{printing_id}'
        return {'id': printing_id, 'name': f'Card {printing_id}', 'set_code': 'tst', 'number': printing_id[1:], 'oracle_id': oracle}


class TheCheck(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.pictures = root / 'pictures'
        self.pictures.mkdir()
        write_pictures(self.pictures, range(6))
        image_index.build([(f'p{n}', None) for n in range(6)], root / 'index', image_dir=self.pictures, fetch=False, log=lambda m: None)
        self.log_file = root / 'printing_check.jsonl'
        self.check = printing_check.PrintingCheck(FakeDatabase(), root / 'index', self.log_file)

    def test_the_verdict_changes_at_the_thresholds(self):
        self.assertEqual(printing_check.verdict_for(0.0), 'agree')
        self.assertEqual(printing_check.verdict_for(printing_check.AGREE_BELOW - 0.0001), 'agree')
        self.assertEqual(printing_check.verdict_for(printing_check.AGREE_BELOW), 'weak')
        self.assertEqual(printing_check.verdict_for(printing_check.DISAGREE_FROM - 0.0001), 'weak')
        self.assertEqual(printing_check.verdict_for(printing_check.DISAGREE_FROM), 'disagree')

    def test_a_picture_that_looks_like_the_printing_agrees(self):
        result = self.check.check(degraded(synthetic_card(0)), 'p0')
        self.assertEqual(result['verdict'], 'agree')
        self.assertEqual(result['best']['id'], 'p0')
        self.assertFalse(result['rotated'])
        self.assertAlmostEqual(result['gap'], 0.0, places=3)

    def test_a_picture_of_another_card_disagrees_as_other_card(self):
        result = self.check.check(degraded(synthetic_card(0)), 'p2')           # the server resolved p2, the picture is p0
        self.assertEqual((result['verdict'], result['kind']), ('disagree', 'other_card'))
        self.assertEqual(result['best']['id'], 'p0')
        self.assertGreater(result['gap'], 0.3)

    def test_a_picture_of_another_printing_of_the_card_disagrees_as_same_card(self):
        # p1 is another printing of p0's card, with other art: the printed line named the wrong one (a Secret Lair card)
        result = self.check.check(degraded(synthetic_card(0)), 'p1')
        self.assertEqual((result['verdict'], result['kind']), ('disagree', 'same_card'))

    def test_a_card_put_in_upside_down_is_still_found(self):
        upside_down = cv2.rotate(degraded(synthetic_card(4)), cv2.ROTATE_180)
        result = self.check.check(upside_down, 'p4')
        self.assertEqual(result['verdict'], 'agree')
        self.assertTrue(result['rotated'])

    def test_a_printing_newer_than_the_index_is_not_indexed(self):
        result = self.check.check(synthetic_card(0), 'not-in-the-index')
        self.assertEqual(result['verdict'], 'not_indexed')
        self.assertIsNone(result['gap'])

    def test_without_an_index_there_is_no_verdict(self):
        empty = printing_check.PrintingCheck(FakeDatabase(), Path(self.folder.name) / 'nothing', self.log_file)
        self.assertIsNone(empty.check(synthetic_card(0), 'p0'))

    def test_an_index_built_later_is_picked_up_without_a_restart(self):
        late = printing_check.PrintingCheck(FakeDatabase(), Path(self.folder.name) / 'later', self.log_file)
        self.assertIsNone(late.check(synthetic_card(0), 'p0'))
        image_index.build([(f'p{n}', None) for n in range(6)], Path(self.folder.name) / 'later', image_dir=self.pictures,
                          fetch=False, log=lambda m: None)
        self.assertEqual(late.check(synthetic_card(0), 'p0')['verdict'], 'agree')


class InShadowMode(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        pictures = root / 'pictures'
        pictures.mkdir()
        write_pictures(pictures, range(6))
        image_index.build([(f'p{n}', None) for n in range(6)], root / 'index', image_dir=pictures, fetch=False, log=lambda m: None)
        self.capture = root / 'capture.jpg'
        cv2.imwrite(str(self.capture), cv2.cvtColor(degraded(synthetic_card(0)), cv2.COLOR_RGB2BGR))
        self.log_file = root / 'logs' / 'printing_check.jsonl'
        self.root = root

    def make(self, mode='shadow'):
        return printing_check.PrintingCheck(FakeDatabase(), self.root / 'index', self.log_file, mode)

    def lines(self):
        return [json.loads(l) for l in self.log_file.read_text().splitlines()] if self.log_file.is_file() else []

    def test_a_card_that_disagrees_is_written_down_with_what_the_picture_looks_like(self):
        shadow = self.make()
        shadow.observe(self.capture, {'id': 'p2', 'name': 'Card p2', 'set_code': 'tst', 'number': '2'}, 'phone')
        self.assertTrue(shadow.wait())
        (record,) = self.lines()
        self.assertEqual((record['verdict'], record['kind'], record['station'], record['capture']), ('disagree', 'other_card', 'phone', 'capture.jpg'))
        self.assertEqual((record['resolved']['id'], record['best']['id'], record['best']['name']), ('p2', 'p0', 'Card p0'))

    def test_a_card_that_agrees_is_written_down_too(self):
        shadow = self.make()
        shadow.observe(self.capture, {'id': 'p0', 'name': 'Card p0', 'set_code': 'tst', 'number': '0'})
        self.assertTrue(shadow.wait())
        self.assertEqual([r['verdict'] for r in self.lines()], ['agree'])

    def test_off_looks_at_nothing(self):
        off = self.make('off')
        off.observe(self.capture, {'id': 'p2', 'name': 'x'})
        self.assertTrue(off.wait())
        self.assertEqual(self.lines(), [])
        self.assertIsNone(off._thread)

    def test_a_picture_that_is_gone_or_a_card_without_an_id_is_skipped_without_a_word(self):
        shadow = self.make()
        shadow.observe(self.root / 'not-there.jpg', {'id': 'p2', 'name': 'x'})
        shadow.observe(self.capture, {'name': 'no id'})
        shadow.observe(None, {'id': 'p2'})
        self.assertTrue(shadow.wait())
        self.assertEqual(self.lines(), [])

    def test_a_failing_check_never_raises_and_the_next_card_is_still_looked_at(self):
        shadow = self.make()
        real = shadow.check
        calls = []

        def flaky(rgb, printing_id):
            calls.append(printing_id)
            if len(calls) == 1:
                raise RuntimeError('boom')
            return real(rgb, printing_id)
        with mock.patch.object(shadow, 'check', flaky):
            shadow.observe(self.capture, {'id': 'p2', 'name': 'x'})
            shadow.observe(self.capture, {'id': 'p0', 'name': 'y'})
            self.assertTrue(shadow.wait())
        self.assertEqual([r['resolved']['id'] for r in self.lines()], ['p0'])

    def test_the_log_does_not_grow_past_its_limit(self):
        shadow = self.make()
        self.log_file.parent.mkdir(parents=True)
        self.log_file.write_text('x' * 100)
        with mock.patch.object(printing_check, 'LOG_LIMIT', 50):
            shadow.observe(self.capture, {'id': 'p0', 'name': 'Card p0'})
            self.assertTrue(shadow.wait())
        self.assertTrue(self.log_file.with_name('printing_check.jsonl.1').is_file())
        self.assertEqual(len(self.lines()), 1)

    def test_the_report_counts_and_lists_the_disagreements(self):
        shadow = self.make()
        shadow.observe(self.capture, {'id': 'p2', 'name': 'Card p2', 'set_code': 'tst', 'number': '2'})
        shadow.observe(self.capture, {'id': 'p0', 'name': 'Card p0', 'set_code': 'tst', 'number': '0'})
        self.assertTrue(shadow.wait())
        out = []
        printing_check.report(self.log_file, out=out.append)
        text = '\n'.join(out)
        self.assertIn('2 cards looked at', text)
        self.assertIn('1 agree', text)
        self.assertIn('1 disagree', text)
        self.assertIn('other_card', text)
        self.assertIn('Card p2', text)
        out.clear()
        printing_check.report(self.root / 'nothing.jsonl', out=out.append)
        self.assertIn('looked at nothing', out[0])


class TheHook(unittest.TestCase):
    """server/captures.search_and_emit_card hands the capture and the card it resolved to the printing check - and nothing depends on the answer"""

    def run_search(self, check, resolved, image_path='capture.jpg'):
        from card_scanner.server import captures
        game = mock.MagicMock()
        game.identify.return_value = resolved
        game.is_confirmed.return_value = False
        patches = [mock.patch.object(captures, name, value) for name, value in (
            ('printing_check', check), ('games', mock.MagicMock(**{'active.return_value': game})), ('log_to_client', mock.Mock()),
            ('log_scanned_card', mock.Mock()), ('get_ai_model_info', mock.Mock(return_value='ai')), ('desk', mock.MagicMock()),
            ('queue_for_review', mock.Mock(return_value='not confirmed')), ('station_prefix', mock.Mock(return_value='')))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return captures.search_and_emit_card('Card', '1', was_fast_scan_mode=True, image_path=image_path, station='phone')

    def test_the_capture_and_the_resolved_card_are_handed_over(self):
        check = mock.MagicMock()
        card = {'id': 'p2', 'name': 'Card p2'}
        outcome = self.run_search(check, card)
        check.observe.assert_called_once_with('capture.jpg', card, 'phone')
        self.assertEqual(outcome['status'], 'review')                  # what became of the card is not the check's business

    def test_no_card_or_no_picture_means_nothing_is_handed_over(self):
        check = mock.MagicMock()
        self.run_search(check, None)
        self.run_search(check, {'id': 'p2'}, image_path=None)
        check.observe.assert_not_called()

    def test_before_the_service_exists_nothing_breaks(self):
        # a LocalProxy of None is falsy: the hook skips it (a capture that arrives before start-up has finished)
        self.assertEqual(self.run_search(None, {'id': 'p2', 'name': 'x'})['status'], 'review')


if __name__ == '__main__':
    unittest.main()
