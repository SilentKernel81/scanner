"""
The server reads no more than 64 MB of a request - except the backup upload, which takes up to
8 GB (server/state.py: MAX_CONTENT_LENGTH, backups_api.py: allow_a_big_backup_upload). A made-up
payload and Flask's test client: nothing is stored.

    venv/bin/python -m unittest discover tests -p test_upload_limit.py
"""
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as launcher  # noqa: E402,F401 - the server's routes
from card_scanner.server.state import app  # noqa: E402

BIG = b'x' * (65 * 1024 * 1024)


class UploadLimit(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def post(self, path, name):
        return self.client.post(path, data={'file': (io.BytesIO(BIG), name)}, content_type='multipart/form-data',
                                base_url='http://localhost')

    def test_a_collection_file_bigger_than_the_limit_is_refused(self):
        self.assertEqual(self.post('/api/import_inventory', 'cards.csv').status_code, 413)

    def test_a_capture_bigger_than_the_limit_is_refused(self):
        answer = self.client.post('/api/stations/pi/captures', data={'image': (io.BytesIO(BIG), 'card.jpg')},
                                  content_type='multipart/form-data', base_url='http://localhost')
        self.assertEqual(answer.status_code, 413)

    def test_a_backup_upload_may_be_bigger(self):
        answer = self.post('/api/backups/upload', 'backup.zip')
        self.assertNotEqual(answer.status_code, 413)                # not a zip: refused for that, not for its size

    def test_the_limit_is_what_is_written(self):
        self.assertEqual(app.config['MAX_CONTENT_LENGTH'], 64 * 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
