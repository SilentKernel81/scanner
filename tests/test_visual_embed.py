"""Tests for the optional v2 visual embedding sidecar (core/visual_embed.py, image_index sidecar, PrintingCheck Cascade -> Fusion)."""
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from card_scanner.core import image_index, printing_check, visual_embed
from card_scanner.core.reading import record as reading_record


def _card_rgb(seed, height=312, width=224):
    """Deterministic textured RGB card image (low-frequency colour blocks + pattern)."""
    rng = np.random.default_rng(seed)
    small = rng.integers(20, 235, size=(12, 9, 3), dtype=np.uint8)
    return cv2.resize(small, (width, height), interpolation=cv2.INTER_CUBIC)


class FakeEmbedder:
    """Deterministic stand-in for VisualEmbedder that maps RGB mean/seed signatures to unit 128-d vectors."""

    def __init__(self, mapping=None, fail=False):
        self.mapping = mapping or {}
        self.fail = fail
        self.calls = 0

    def embed(self, rgb):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        key = int(rgb[50, 50, 0])
        if key in self.mapping:
            v = np.asarray(self.mapping[key], dtype=np.float32)
        else:
            rng = np.random.default_rng(int(rgb.mean() * 1000) + key)
            v = rng.standard_normal(visual_embed.DIMS).astype(np.float32)
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else np.zeros(visual_embed.DIMS, dtype=np.float32)

    def embed_int8(self, rgb):
        return visual_embed.quantize_int8(self.embed(rgb))


class FakeDatabase:
    def __init__(self, cards):
        self._by_id = {c['id']: c for c in cards}
        self._by_sn = {(c['set_code'].lower(), c['number'].lower()): c for c in cards}

    def get_card_by_id(self, printing_id):
        return self._by_id.get(printing_id)

    def cards_by_ids(self, ids):
        return {pid: self._by_id[pid] for pid in ids if pid in self._by_id}

    def get_card_by_set_number(self, set_code, number, exact=False):
        return self._by_sn.get(((set_code or '').lower(), (number or '').lstrip('0').lower() or '0'))


class VisualEmbedMathTests(unittest.TestCase):
    def test_int8_cosine_matches_float32_losslessly(self):
        rng = np.random.default_rng(42)
        raw = rng.standard_normal((500, visual_embed.DIMS)).astype(np.float32)
        raw /= np.linalg.norm(raw, axis=1, keepdims=True)
        q = rng.standard_normal(visual_embed.DIMS).astype(np.float32)
        q /= np.linalg.norm(q)

        quantized = np.vstack([visual_embed.quantize_int8(row) for row in raw])
        inv_norms = visual_embed.row_inv_norms(quantized, chunk=128)
        approx = visual_embed.cosine_similarities(quantized, inv_norms, q, chunk=128)
        exact = raw @ q

        max_err = float(np.max(np.abs(approx - exact)))
        self.assertLess(max_err, 0.005)
        self.assertEqual(int(np.argmax(approx)), int(np.argmax(exact)))

    def test_zero_and_nonfinite_vectors_are_safe(self):
        zero = np.zeros(visual_embed.DIMS, dtype=np.float32)
        q_zero = visual_embed.quantize_int8(zero)
        self.assertEqual(q_zero.dtype, np.int8)
        self.assertTrue(np.all(q_zero == 0))

        nan_vec = np.full(visual_embed.DIMS, np.nan, dtype=np.float32)
        self.assertTrue(np.all(visual_embed.quantize_int8(nan_vec) == 0))

        table = np.zeros((3, visual_embed.DIMS), dtype=np.int8)
        inv = visual_embed.row_inv_norms(table)
        sims = visual_embed.cosine_similarities(table, inv, np.ones(visual_embed.DIMS, dtype=np.float32))
        self.assertTrue(np.all(sims == 0.0))

        # Zero query
        sims_zero_q = visual_embed.cosine_similarities(table, inv, zero)
        self.assertTrue(np.all(sims_zero_q == 0.0))

    def test_load_missing_or_corrupt_model_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / 'nope.onnx'
            self.assertIsNone(visual_embed.load(missing))
            corrupt = Path(tmp) / 'broken.onnx'
            corrupt.write_bytes(b'not an onnx file')
            self.assertIsNone(visual_embed.load(corrupt))

    def test_real_ft_onnx_smoke_if_cached(self):
        cached = Path.home() / '.cache' / 'mtg-image-match' / 'out' / 'ft.onnx'
        if not cached.is_file():
            self.skipTest("cached ft.onnx not present on this machine")
        embedder = visual_embed.load(cached)
        self.assertIsNotNone(embedder)
        rgb = _card_rgb(7)
        vec1 = embedder.embed(rgb)
        vec2 = embedder.embed(rgb)
        self.assertEqual(vec1.shape, (visual_embed.DIMS,))
        self.assertAlmostEqual(float(np.linalg.norm(vec1)), 1.0, places=5)
        np.testing.assert_allclose(vec1, vec2, atol=1e-6)
        q = embedder.embed_int8(rgb)
        self.assertEqual(q.shape, (visual_embed.DIMS,))
        self.assertEqual(q.dtype, np.int8)


class ImageIndexSidecarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ids = [f'00000000-0000-0000-0000-{i:012d}' for i in range(4)]
        self.hashes = [image_index.hash_card(_card_rgb(i + 1)) for i in range(4)]
        rng = np.random.default_rng(123)
        self.vecs = rng.standard_normal((4, visual_embed.DIMS)).astype(np.float32)
        self.vecs /= np.linalg.norm(self.vecs, axis=1, keepdims=True)
        self.embeds = [visual_embed.quantize_int8(v) for v in self.vecs]

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_and_load_with_sidecar(self):
        folder = self.root / 'idx'
        image_index.write(folder, self.ids, self.hashes, embeds=self.embeds)
        idx = image_index.ImageIndex.load(folder)
        self.assertIsNotNone(idx)
        self.assertTrue(idx.has_embeddings)
        sims = idx.embed_similarities(self.vecs[2])
        self.assertEqual(int(np.argmax(sims)), 2)
        self.assertGreater(float(sims[2]), 0.99)

    def test_corrupt_sidecar_falls_back_to_v1(self):
        folder = self.root / 'idx'
        image_index.write(folder, self.ids, self.hashes, embeds=self.embeds)
        manifest = json.loads((folder / image_index.MANIFEST).read_text())
        embed_file = folder / manifest['sidecar']['embeds']
        # Flip one byte in the sidecar binary
        data = bytearray(embed_file.read_bytes())
        data[0] ^= 0xFF
        embed_file.write_bytes(bytes(data))

        idx = image_index.ImageIndex.load(folder)
        self.assertIsNotNone(idx)
        self.assertEqual(len(idx), 4)
        self.assertFalse(idx.has_embeddings)
        self.assertIsNone(idx.embed_similarities(self.vecs[0]))

    def test_old_sidecar_generations_are_cleaned_up(self):
        folder = self.root / 'idx'
        image_index.write(folder, self.ids, self.hashes, embeds=self.embeds)
        gen1 = json.loads((folder / image_index.MANIFEST).read_text())['generation']
        image_index.write(folder, self.ids, self.hashes, embeds=self.embeds)
        image_index.write(folder, self.ids, self.hashes, embeds=self.embeds)
        # Generation 1's sidecar should be removed (only current + previous kept)
        self.assertFalse((folder / f'embeds-{gen1}.bin').exists())
        embed_files = list(folder.glob('embeds-*.bin'))
        self.assertEqual(len(embed_files), 2)

    def test_export_archive_stays_three_file_v1_compatible(self):
        folder = self.root / 'idx'
        image_index.write(folder, self.ids, self.hashes, embeds=self.embeds)
        buf = io.BytesIO()
        exported = image_index.export_archive(folder, buf)
        self.assertNotIn('sidecar', exported)
        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            self.assertEqual(len(zf.namelist()), 3)
            inner_manifest = json.loads(zf.read(image_index.MANIFEST))
            self.assertNotIn('sidecar', inner_manifest)
        # And import_archive accepts it cleanly
        dest = self.root / 'imported'
        buf.seek(0)
        res = image_index.import_archive(dest, buf)
        self.assertEqual(res['count'], 4)
        loaded = image_index.ImageIndex.load(dest)
        self.assertIsNotNone(loaded)
        self.assertFalse(loaded.has_embeddings)

    def test_build_and_backfill_sidecar(self):
        pics = self.root / 'pics'
        pics.mkdir()
        for i, pid in enumerate(self.ids):
            cv2.imwrite(str(pics / f'{pid}.jpg'), cv2.cvtColor(_card_rgb(i + 10), cv2.COLOR_RGB2BGR))
        folder = self.root / 'idx'
        printings = [(pid, None) for pid in self.ids[:3]]

        # 1. Build v1 only first
        image_index.build(printings, folder, image_dir=pics, fetch=False, workers=1, log=lambda m: None)
        idx = image_index.ImageIndex.load(folder)
        self.assertFalse(idx.has_embeddings)

        # 2. Backfill sidecar with rebuild_sidecar=True
        fake = FakeEmbedder()
        image_index.build(printings, folder, image_dir=pics, fetch=False, workers=1, log=lambda m: None,
                          embedder=fake, rebuild_sidecar=True)
        idx = image_index.ImageIndex.load(folder)
        self.assertTrue(idx.has_embeddings)
        self.assertEqual(len(idx), 3)
        self.assertEqual(fake.calls, 3)

        # 3. Incremental update adds the 4th printing and embeds ONLY the 4th printing
        all_printings = [(pid, None) for pid in self.ids]
        image_index.build(all_printings, folder, image_dir=pics, fetch=False, workers=1, log=lambda m: None,
                          embedder=fake)
        idx = image_index.ImageIndex.load(folder)
        self.assertTrue(idx.has_embeddings)
        self.assertEqual(len(idx), 4)
        self.assertEqual(fake.calls, 4)

    def test_partial_backfill_persists_progress_and_resumes_without_losing_work(self):
        pics = self.root / 'pics'
        pics.mkdir()
        for i, pid in enumerate(self.ids):
            cv2.imwrite(str(pics / f'{pid}.jpg'), cv2.cvtColor(_card_rgb(i + 20), cv2.COLOR_RGB2BGR))
        folder = self.root / 'idx'
        all_printings = [(pid, None) for pid in self.ids]

        # Build v1 for all 4 printings first
        image_index.build(all_printings, folder, image_dir=pics, fetch=False, workers=1, log=lambda m: None)
        # Backfill only 2 printings (simulating --limit / cancel / 429)
        fake = FakeEmbedder()
        image_index.build(all_printings, folder, image_dir=pics, fetch=False, workers=1, limit=2,
                          log=lambda m: None, embedder=fake, rebuild_sidecar=True)
        self.assertEqual(fake.calls, 2)
        partial = image_index.ImageIndex.load(folder)
        self.assertIsNotNone(partial.embeds)
        self.assertFalse(partial.has_embeddings)  # partial checkpoint must not activate live fusion yet

        # Resume backfill: only the remaining 2 printings are embedded, and now the sidecar activates
        image_index.build(all_printings, folder, image_dir=pics, fetch=False, workers=1,
                          log=lambda m: None, embedder=fake, rebuild_sidecar=True)
        self.assertEqual(fake.calls, 4)
        complete = image_index.ImageIndex.load(folder)
        self.assertTrue(complete.has_embeddings)

    def test_incremental_update_without_embedder_preserves_existing_sidecar(self):
        pics = self.root / 'pics'
        pics.mkdir()
        for i, pid in enumerate(self.ids):
            cv2.imwrite(str(pics / f'{pid}.jpg'), cv2.cvtColor(_card_rgb(i + 30), cv2.COLOR_RGB2BGR))
        folder = self.root / 'idx'
        fake = FakeEmbedder()
        image_index.build([(pid, None) for pid in self.ids[:3]], folder, image_dir=pics, fetch=False, workers=1,
                          log=lambda m: None, embedder=fake, rebuild_sidecar=True)
        # Add the 4th printing without an embedder: existing 3 embeddings must stay saved on disk
        image_index.build([(pid, None) for pid in self.ids], folder, image_dir=pics, fetch=False, workers=1,
                          log=lambda m: None, embedder=None)
        idx = image_index.ImageIndex.load(folder)
        self.assertIsNotNone(idx.embeds)
        self.assertFalse(idx.has_embeddings)  # 4th printing lacks an embedding until backfilled
        self.assertEqual(sum(1 for row in idx.embeds if row.any()), 3)


class CascadeFusionLiveReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cards = [
            {'id': '00000000-0000-0000-0000-000000000000', 'name': 'Lightning Bolt', 'set_code': 'm11', 'number': '149',
             'oracle_id': 'ora-bolt', 'finishes': ['nonfoil', 'foil'], 'released_at': '2025-01-01', 'frame': '2015'},
            {'id': '00000000-0000-0000-0000-000000000001', 'name': 'Lightning Bolt', 'set_code': 'sta', 'number': '42',
             'oracle_id': 'ora-bolt', 'finishes': ['nonfoil', 'foil'], 'released_at': '2025-01-01', 'frame': '2015'},
            {'id': '00000000-0000-0000-0000-000000000002', 'name': 'Giant Growth', 'set_code': 'fdn', 'number': '223',
             'oracle_id': 'ora-grow', 'finishes': ['nonfoil'], 'released_at': '2025-01-01', 'frame': '2015'},
        ]
        self.db = FakeDatabase(self.cards)

    def tearDown(self):
        self.tmp.cleanup()

    def _unit(self, idx):
        v = np.zeros(visual_embed.DIMS, dtype=np.float32)
        v[idx] = 1.0
        return v

    def test_embedder_not_called_when_phash_decides(self):
        # Three distinct images -> phash decides easily with huge lead -> embedder must NOT be called
        imgs = [_card_rgb(100 + i) for i in range(3)]
        hashes = [image_index.hash_card(im) for im in imgs]
        embeds = [visual_embed.quantize_int8(self._unit(i)) for i in range(3)]
        folder = self.root / 'idx'
        image_index.write(folder, [c['id'] for c in self.cards], hashes, embeds=embeds)

        embedder = FakeEmbedder()
        checker = printing_check.PrintingCheck(self.db, folder, self.root / 'shadow.jsonl', mode='off', embedder=embedder)
        result = checker.live_read(imgs[2], imgs[2], ocr=None, accept_image_alone=True)
        self.assertIsNotNone(result)
        self.assertEqual(result['card']['id'], self.cards[2]['id'])
        self.assertEqual(embedder.calls, 0)

    def test_cascade_fusion_rescues_tied_phash_review(self):
        # Cards 0 and 1 share the EXACT same phash (identical art/reprint tie -> phash lead == 0 -> 'review'),
        # while their v2 embeddings are orthogonal (unit 0 vs unit 1). Fusion gives card 0 a 0.50 lead!
        shared_img = _card_rgb(200)
        other_img = _card_rgb(201)
        hashes = [image_index.hash_card(shared_img), image_index.hash_card(shared_img), image_index.hash_card(other_img)]
        embeds = [visual_embed.quantize_int8(self._unit(0)),
                  visual_embed.quantize_int8(self._unit(1)),
                  visual_embed.quantize_int8(self._unit(2))]
        folder = self.root / 'idx'
        image_index.write(folder, [c['id'] for c in self.cards], hashes, embeds=embeds)

        # Without embedder (v1 only): phash ties cards 0 & 1 -> live_read returns None
        checker_v1 = printing_check.PrintingCheck(self.db, folder, self.root / 'shadow.jsonl', mode='off', embedder=None)
        checker_v1._embedder_checked = True
        self.assertIsNone(checker_v1.live_read(shared_img, shared_img, ocr=None, accept_image_alone=True))

        # With v2 embedder returning unit(0): Cascade -> Fusion rescues card 0!
        embedder = FakeEmbedder(mapping={int(shared_img[50, 50, 0]): self._unit(0)})
        checker_v2 = printing_check.PrintingCheck(self.db, folder, self.root / 'shadow.jsonl', mode='off', embedder=embedder)
        log = reading_record.ReadingLog(self.root / 'reading.jsonl')
        rec = log.new()
        with reading_record.using(rec):
            result = checker_v2.live_read(shared_img, shared_img, ocr=None, accept_image_alone=True)
        self.assertIsNotNone(result)
        self.assertEqual(result['card']['id'], self.cards[0]['id'])
        self.assertEqual(result['path'], 'image')
        self.assertGreater(embedder.calls, 0)
        self.assertTrue(rec.data['decision'].get('v2_fused'))
        self.assertIsNone(rec.data['refused'])

    def test_embedder_failure_does_not_break_live_read(self):
        shared_img = _card_rgb(210)
        other_img = _card_rgb(211)
        hashes = [image_index.hash_card(shared_img), image_index.hash_card(shared_img), image_index.hash_card(other_img)]
        embeds = [visual_embed.quantize_int8(self._unit(i)) for i in range(3)]
        folder = self.root / 'idx'
        image_index.write(folder, [c['id'] for c in self.cards], hashes, embeds=embeds)

        broken = FakeEmbedder(fail=True)
        checker = printing_check.PrintingCheck(self.db, folder, self.root / 'shadow.jsonl', mode='off', embedder=broken)
        # Must not raise; cleanly returns None (falls back to phash review)
        self.assertIsNone(checker.live_read(shared_img, shared_img, ocr=None, accept_image_alone=True))
        self.assertGreater(broken.calls, 0)

    def test_fusion_still_respects_similarity_floor(self):
        # Even if v2 embedding leads, fused similarity below IMAGE_MIN_SIMILARITY must still be refused
        img_a = _card_rgb(300)
        img_b = _card_rgb(301)
        query_img = _card_rgb(302)  # unrelated to img_a and img_b -> low phash similarity (~0.0)
        hashes = [image_index.hash_card(img_a), image_index.hash_card(img_b), image_index.hash_card(img_b)]
        embeds = [visual_embed.quantize_int8(self._unit(0)),
                  visual_embed.quantize_int8(self._unit(1)),
                  visual_embed.quantize_int8(self._unit(2))]
        folder = self.root / 'idx'
        image_index.write(folder, [c['id'] for c in self.cards], hashes, embeds=embeds)

        # Embedder returns weak 0.4 similarity to unit(0) -> fused ~ 0.5*0 + 0.5*0.4 = 0.2 < 0.50 floor
        weak_v = np.zeros(visual_embed.DIMS, dtype=np.float32)
        weak_v[0] = 0.4
        weak_v[10] = np.sqrt(1.0 - 0.16)
        embedder = FakeEmbedder(mapping={int(query_img[50, 50, 0]): weak_v})
        checker = printing_check.PrintingCheck(self.db, folder, self.root / 'shadow.jsonl', mode='off', embedder=embedder)
        self.assertIsNone(checker.live_read(query_img, query_img, ocr=None, accept_image_alone=True))

    def test_fusion_orientation_flip_uses_flipped_strip_and_updates_capture_evidence(self):
        from card_scanner.core.reading import capture as reading_capture
        # Build a 180°-symmetric image so phash has equal distance upright and turned (orientation_established == False,
        # phash ties cards 0 & 1 -> 'review'), while v2 only matches the 180°-rotated orientation.
        half = _card_rgb(240, height=156, width=224)
        sym_img = np.vstack([half, cv2.rotate(half, cv2.ROTATE_180)])
        # Perturb one corner pixel slightly so upright and rotated have distinct keys in FakeEmbedder while phash stays tied
        sym_img[50, 50, 0] = 10
        sym_img[311 - 50, 223 - 50, 0] = 240
        other_img = _card_rgb(241)
        hashes = [image_index.hash_card(sym_img), image_index.hash_card(sym_img), image_index.hash_card(other_img)]
        embeds = [visual_embed.quantize_int8(self._unit(0)),
                  visual_embed.quantize_int8(self._unit(1)),
                  visual_embed.quantize_int8(self._unit(2))]
        folder = self.root / 'idx'
        image_index.write(folder, [c['id'] for c in self.cards], hashes, embeds=embeds)

        embedder = FakeEmbedder(mapping={
            10: self._unit(50),    # upright: orthogonal
            240: self._unit(0),    # rotated 180°: matches card 0
        })

        class WrongEndNumberOcr:
            # 1st call (upright, wrong end) returns a stray number without a set line; 2nd call (turned) returns []
            def __init__(self):
                self.calls = 0

            def read_lines(self, _rgb):
                self.calls += 1
                if self.calls == 1:
                    return [{'text': 'R 0149', 'box': [[0, 0], [40, 0], [40, 12], [0, 12]]}]
                return []

        checker = printing_check.PrintingCheck(self.db, folder, self.root / 'shadow.jsonl', mode='off', embedder=embedder)
        ev = reading_capture.CaptureEvidence()
        with reading_capture.using(ev):
            result = checker.live_read(sym_img, sym_img, ocr=WrongEndNumberOcr(), accept_image_alone=True)
            kept_rot, rotated = reading_capture.recall('rotated', sym_img)
            kept_strip, kept_strip_val = reading_capture.recall('strip', sym_img)
        self.assertIsNotNone(result)
        self.assertTrue(result['rotated'])
        self.assertEqual(result['card']['id'], self.cards[0]['id'])
        # CaptureEvidence records the post-fusion orientation (rotated=True) and the flipped orientation's strip
        self.assertTrue(kept_rot)
        self.assertTrue(rotated)
        self.assertTrue(kept_strip)
        self.assertEqual(kept_strip_val['number'], '')
        self.assertEqual(result['strip'], {'number': '', 'set': ''})

    def test_fusion_skipped_when_image_alone_off_and_no_strip(self):
        shared_img = _card_rgb(250)
        other_img = _card_rgb(251)
        hashes = [image_index.hash_card(shared_img), image_index.hash_card(shared_img), image_index.hash_card(other_img)]
        embeds = [visual_embed.quantize_int8(self._unit(i)) for i in range(3)]
        folder = self.root / 'idx'
        image_index.write(folder, [c['id'] for c in self.cards], hashes, embeds=embeds)

        embedder = FakeEmbedder()
        checker = printing_check.PrintingCheck(self.db, folder, self.root / 'shadow.jsonl', mode='off', embedder=embedder)
        self.assertIsNone(checker.live_read(shared_img, shared_img, ocr=None, accept_image_alone=False))
        self.assertEqual(embedder.calls, 0)


if __name__ == '__main__':
    unittest.main()
