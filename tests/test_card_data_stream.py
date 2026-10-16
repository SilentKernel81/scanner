"""
The Scryfall bulk download is read as a stream (core/database.py: download_scryfall_data /
_stream_cards): cards come out one at a time, gzipped or plain, and a cut-off file raises instead of
being half imported. A local web server serves generated files; nothing goes to the internet.

    venv/bin/python -m unittest discover tests -p test_card_data_stream.py
"""
import gzip
import http.server
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

from card_scanner.core import database  # noqa: E402

CARDS = [{'id': f'id{n}', 'name': f'Jötun Grunt {n} 龍', 'set': 'tst', 'set_name': 'Test', 'collector_number': str(n),
          'rarity': 'common', 'prices': {'usd': '0.10'}, 'released_at': '2026-01-01'} for n in range(300)]
LINES = ''.join(json.dumps(card, ensure_ascii=False) + '\n' for card in CARDS).encode('utf-8')


class Served:
    """A one-file web server on a free port; `body` is what it serves"""
    def __init__(self, body):
        outer = body

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(len(outer)))
                self.end_headers()
                self.wfile.write(outer)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/default-cards.jsonl'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def downloaded(body, url_suffix='.jsonl', messages=None):
    """What download_scryfall_data hands back for a file with this body"""
    served = Served(body)
    try:
        db = types.SimpleNamespace(
            fetch_scryfall_info=lambda: {'updated_at': 'x', 'jsonl_download_uri': served.url.replace('.jsonl', url_suffix),
                                         'compressed_size': len(body)},
            _stream_cards=database.CardDatabase._stream_cards, last_download_source=None)
        stream = database.CardDatabase.download_scryfall_data(db, messages.append if messages is not None else None)
        return stream, served
    except Exception:
        served.close()
        raise


class Stream(unittest.TestCase):
    def read(self, body, **kwargs):
        stream, served = downloaded(body, **kwargs)
        try:
            return list(stream)
        finally:
            served.close()

    def test_it_is_a_generator_not_a_list(self):
        stream, served = downloaded(gzip.compress(LINES))
        try:
            self.assertTrue(hasattr(stream, '__next__'))
            self.assertEqual(next(stream)['id'], 'id0')
        finally:
            stream.close()
            served.close()

    def test_a_gzipped_file_gives_every_card_with_its_characters_intact(self):
        self.assertEqual(self.read(gzip.compress(LINES)), CARDS)

    def test_a_plain_file_too(self):
        self.assertEqual(self.read(LINES), CARDS)

    def test_the_progress_is_said(self):
        messages = []
        self.read(gzip.compress(LINES), messages=messages)
        self.assertIn('Download progress: 100%', messages)

    def test_a_cut_off_file_raises_instead_of_ending_quietly(self):
        packed = gzip.compress(LINES * 20)
        with self.assertRaises(Exception):
            self.read(packed[:len(packed) // 2])

    def test_the_older_json_array_format_still_reads(self):
        stream, served = downloaded(json.dumps(CARDS, ensure_ascii=False).encode('utf-8'), url_suffix='.json')
        try:
            self.assertEqual(list(stream), CARDS)
        finally:
            served.close()


class Populate(unittest.TestCase):
    def test_a_stream_fills_the_table_like_a_list_does(self):
        with tempfile.TemporaryDirectory() as folder:
            db = database.CardDatabase(Path(folder) / 'cards.db')
            stream, served = downloaded(gzip.compress(LINES))
            try:
                self.assertEqual(db.populate_database(stream), len(CARDS))
            finally:
                served.close()
            self.assertEqual(db.get_database_stats()['total_cards'], len(CARDS))
            db.close()


if __name__ == '__main__':
    unittest.main()
