"""
Moving the picture index by hand (core/image_index.py: export_archive, import_archive; server/card_data.py: /api/image_index/export and /import).
Real index folders in temporary directories, written by image_index.write - nothing is downloaded and no real data is touched.

    venv/bin/python -m unittest discover tests -p test_index_transfer.py
"""
import io
import json
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest import mock

from werkzeug.exceptions import RequestEntityTooLarge

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as launcher  # noqa: E402,F401 - the server's routes
from card_scanner.core import image_index  # noqa: E402
from card_scanner.server.state import app  # noqa: E402

card_data = sys.modules['card_scanner.server.card_data']
BASE = 'http://localhost'


def printings(count, fill):
    """`count` made-up printings: Scryfall-shaped ids and fingerprints of one repeated byte"""
    return [str(uuid.uuid4()) for _ in range(count)], [bytes([fill + n]) * image_index.HASH_BYTES for n in range(count)]


def packed(folder):
    """The folder's index as zip bytes"""
    out = io.BytesIO()
    image_index.export_archive(folder, out)
    return out.getvalue()


def rewritten(archive, change):
    """A copy of a zip with change(files) applied to its {name: bytes}"""
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        files = {name: source.read(name) for name in source.namelist()}
    change(files)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as target:
        for name, data in files.items():
            target.writestr(name, data)
    return out.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.here, self.there = self.root / 'here' / 'image_index', self.root / 'there' / 'image_index'
        self.here_ids, self.here_hashes = printings(3, 1)
        self.there_ids, self.there_hashes = printings(2, 50)
        image_index.write(self.here, self.here_ids, self.here_hashes)
        image_index.write(self.there, self.there_ids, self.there_hashes)


class Transfer(Base):
    def test_an_import_replaces_the_index_and_keeps_its_built_date(self):
        built = json.loads((self.there / image_index.MANIFEST).read_text())['built']
        answer = image_index.import_archive(self.here, io.BytesIO(packed(self.there)))
        loaded = image_index.ImageIndex.load(self.here)
        self.assertEqual(loaded.ids, self.there_ids)
        self.assertEqual(loaded.hashes.tobytes(), b''.join(self.there_hashes))
        self.assertEqual((answer['count'], answer['built'], answer['backup'], answer['unchanged']), (2, built, True, False))

    def test_the_backup_is_the_previous_index_and_can_be_uploaded_to_go_back(self):
        image_index.import_archive(self.here, io.BytesIO(packed(self.there)))
        backup = image_index.backup_path(self.here)
        self.assertEqual(backup.name, 'image_index_backup.zip')
        image_index.import_archive(self.here, io.BytesIO(backup.read_bytes()))
        self.assertEqual(image_index.ImageIndex.load(self.here).ids, self.here_ids)

    def test_only_one_backup_is_kept(self):
        image_index.import_archive(self.here, io.BytesIO(packed(self.there)))
        third = self.root / 'third' / 'image_index'
        image_index.write(third, *printings(4, 90))
        image_index.import_archive(self.here, io.BytesIO(packed(third)))
        self.assertEqual([p.name for p in self.here.parent.iterdir() if 'backup' in p.name], ['image_index_backup.zip'])
        with zipfile.ZipFile(image_index.backup_path(self.here)) as backup:
            self.assertEqual(json.loads(backup.read(image_index.MANIFEST))['count'], 2)   # the index just replaced, not the first one

    def test_the_index_in_use_again_changes_nothing_and_keeps_the_backup(self):
        image_index.import_archive(self.here, io.BytesIO(packed(self.there)))
        before = image_index.backup_path(self.here).read_bytes()
        manifest = (self.here / image_index.MANIFEST).read_text()
        self.assertTrue(image_index.import_archive(self.here, io.BytesIO(packed(self.there)))['unchanged'])
        self.assertEqual(image_index.backup_path(self.here).read_bytes(), before)
        self.assertEqual((self.here / image_index.MANIFEST).read_text(), manifest)

    def test_a_failed_backup_leaves_the_index_alone(self):
        manifest = (self.here / image_index.MANIFEST).read_text()
        with mock.patch.object(image_index.os, 'fsync', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                image_index.import_archive(self.here, io.BytesIO(packed(self.there)))
        self.assertEqual((self.here / image_index.MANIFEST).read_text(), manifest)
        self.assertEqual(image_index.ImageIndex.load(self.here).ids, self.here_ids)
        self.assertEqual([p.name for p in self.here.parent.iterdir() if 'backup' in p.name], [])

    def test_a_broken_index_never_replaces_a_good_backup(self):
        image_index.import_archive(self.here, io.BytesIO(packed(self.there)))
        good = image_index.backup_path(self.here).read_bytes()
        manifest = json.loads((self.here / image_index.MANIFEST).read_text())
        (self.here / manifest['hashes']).write_bytes(b'broken')
        third = self.root / 'third' / 'image_index'
        third_ids, third_hashes = printings(1, 200)
        image_index.write(third, third_ids, third_hashes)
        self.assertFalse(image_index.import_archive(self.here, io.BytesIO(packed(third)))['backup'])
        self.assertEqual(image_index.backup_path(self.here).read_bytes(), good)
        self.assertEqual(image_index.ImageIndex.load(self.here).ids, third_ids)

    def test_refused_while_a_build_runs(self):
        archive = packed(self.there)
        with image_index._building(self.here):
            with self.assertRaises(image_index.BuildRunning):
                image_index.import_archive(self.here, io.BytesIO(archive))
        self.assertEqual(image_index.ImageIndex.load(self.here).ids, self.here_ids)


class BadFiles(Base):
    def refused(self, data):
        manifest = (self.here / image_index.MANIFEST).read_text()
        with self.assertRaises(image_index.IndexFileError):
            image_index.import_archive(self.here, io.BytesIO(data))
        self.assertEqual((self.here / image_index.MANIFEST).read_text(), manifest)
        self.assertFalse(image_index.backup_path(self.here).exists())       # refused before the backup

    def edit_manifest(self, **changes):
        def change(files):
            files[image_index.MANIFEST] = json.dumps({**json.loads(files[image_index.MANIFEST]), **changes}).encode()
        return rewritten(packed(self.there), change)

    def test_not_a_zip(self):
        self.refused(b'this is not a zip')

    def test_a_zip_of_something_else(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:
            archive.writestr('photo.jpg', b'x')
        self.refused(out.getvalue())

    def test_another_descriptor(self):
        self.refused(self.edit_manifest(descriptor='another_v2'))

    def test_a_wrong_count(self):
        self.refused(self.edit_manifest(count=3))

    def test_a_count_too_large(self):
        self.refused(self.edit_manifest(count=image_index.MAX_PRINTINGS + 1))

    def test_changed_fingerprints(self):
        def change(files):
            name = json.loads(files[image_index.MANIFEST])['hashes']
            files[name] = bytes([files[name][0] ^ 1]) + files[name][1:]
        self.refused(rewritten(packed(self.there), change))

    def test_an_extra_file(self):
        self.refused(rewritten(packed(self.there), lambda files: files.update({'../evil.txt': b'x'})))

    def test_a_path_as_a_file_name(self):
        def change(files):
            manifest = json.loads(files[image_index.MANIFEST])
            files['../' + manifest['ids']] = files.pop(manifest['ids'])
            manifest['ids'] = '../' + manifest['ids']
            files[image_index.MANIFEST] = json.dumps(manifest).encode()
        # Accepted names are only looked up inside the zip, never written: what matters is that nothing lands outside the folder
        try:
            image_index.import_archive(self.here, io.BytesIO(rewritten(packed(self.there), change)))
        except image_index.IndexFileError:
            pass
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['here', 'there'])
        self.assertIsNotNone(image_index.ImageIndex.load(self.here))


class Routes(Base):
    def setUp(self):
        super().setUp()
        self.client = app.test_client()
        check = mock.Mock(index_folder=self.here)
        for patcher in (mock.patch.object(card_data, 'printing_check', check), mock.patch.object(card_data, 'index_status', return_value={'indexed': 2})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_download_then_upload(self):
        answer = self.client.get('/api/image_index/export', base_url=BASE)
        self.assertEqual(answer.status_code, 200)
        self.assertIn('picture-index-3-', answer.headers['Content-Disposition'])
        self.assertEqual(json.loads(zipfile.ZipFile(io.BytesIO(answer.data)).read(image_index.MANIFEST))['count'], 3)
        answer = self.client.post('/api/image_index/import', base_url=BASE, content_type='multipart/form-data',
                                  data={'file': (io.BytesIO(packed(self.there)), 'index.zip')})
        self.assertEqual(answer.status_code, 200, answer.data)
        self.assertEqual(answer.get_json()['imported']['count'], 2)
        self.assertEqual(image_index.ImageIndex.load(self.here).ids, self.there_ids)

    def test_the_backup_can_be_downloaded_once_there_is_one_and_uploaded_to_go_back(self):
        self.assertIsNone(image_index.backup_info(self.here))
        self.assertEqual(self.client.get('/api/image_index/backup', base_url=BASE).status_code, 404)
        image_index.import_archive(self.here, io.BytesIO(packed(self.there)))
        self.assertEqual(image_index.backup_info(self.here)['count'], 3)        # the index that was replaced
        answer = self.client.get('/api/image_index/backup', base_url=BASE)
        self.assertEqual(answer.status_code, 200)
        self.assertIn('picture-index-backup-3-', answer.headers['Content-Disposition'])
        self.assertEqual(answer.data, image_index.backup_path(self.here).read_bytes())
        data = answer.data
        answer.close()
        image_index.import_archive(self.here, io.BytesIO(data))
        self.assertEqual(image_index.ImageIndex.load(self.here).ids, self.here_ids)

    def test_a_damaged_backup_is_not_offered(self):
        image_index.backup_path(self.here).write_bytes(b'not a zip')
        self.assertIsNone(image_index.backup_info(self.here))
        self.assertEqual(self.client.get('/api/image_index/backup', base_url=BASE).status_code, 404)

    def test_a_bad_file_is_a_400(self):
        answer = self.client.post('/api/image_index/import', base_url=BASE, content_type='multipart/form-data',
                                  data={'file': (io.BytesIO(b'nope'), 'index.zip')})
        self.assertEqual(answer.status_code, 400)

    def test_an_upload_over_the_general_limit_is_allowed_up_to_its_own(self):
        limit_the_request_body = sys.modules['card_scanner.server.backups_api'].limit_the_request_body
        # Above the server's general 64 MB, within the index's own limit: let through
        with app.test_request_context('/api/image_index/import', method='POST', base_url=BASE, environ_overrides={'CONTENT_LENGTH': str(100 * 1024 * 1024)}):
            limit_the_request_body()
        # Above the index's own limit: refused before the body is read
        with app.test_request_context('/api/image_index/import', method='POST', base_url=BASE, environ_overrides={'CONTENT_LENGTH': str(image_index.ARCHIVE_LIMIT + 1)}):
            with self.assertRaises(RequestEntityTooLarge):
                limit_the_request_body()

    def test_both_routes_need_the_password_when_one_is_set(self):
        lock = sys.modules['card_scanner.server.lock']
        with mock.patch.object(lock, 'unlocked', return_value=False):
            self.assertEqual(self.client.get('/api/image_index/export', base_url=BASE).status_code, 401)
            self.assertEqual(self.client.get('/api/image_index/backup', base_url=BASE).status_code, 401)
            self.assertEqual(self.client.post('/api/image_index/import', base_url=BASE).status_code, 401)


if __name__ == '__main__':
    unittest.main()
