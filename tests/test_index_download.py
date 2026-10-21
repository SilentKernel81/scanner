"""
The picture index for a phone (server/stations_api.py: /api/image_index/download and /download/<kind>; core/image_index.py: printings_table;
core/database.py: printing_facts). A real index folder written by image_index.write with a few fingerprints, and stand-ins for the card data - nothing is
downloaded and no real data is touched.

    venv/bin/python -m unittest discover tests -p test_index_download.py
"""
import gzip
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as launcher  # noqa: E402,F401 - the server's routes
from card_scanner.config import Config  # noqa: E402
from card_scanner.core import image_index  # noqa: E402
from card_scanner.server.state import app  # noqa: E402

stations_api = sys.modules['card_scanner.server.stations_api']
BASE = 'http://localhost'
IDS = ['id-a', 'id-b', 'id-c']
HASHES = [bytes([n]) * image_index.HASH_BYTES for n in (1, 2, 3)]
FACTS = {'id-a': ('Lightning Bolt', 'SLD', 'IFIYW-2', 'oracle-1', 'nonfoil,foil', '2023-06-23', '2015'),
         'id-b': ("Tab\tand\nnewline", 'tfra', '1', 'oracle-2', 'nonfoil'),
         'id-c-gone': ('Not in the index', 'x', '9', 'o', '')}              # in the card data, not in the index; and 'id-c' is in the index, not the card data


class Download(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        image_index.write(self.root, IDS, HASHES)
        self.manifest = json.loads((self.root / image_index.MANIFEST).read_text())
        self.index = image_index.ImageIndex.load(self.root)
        self.facts_calls = 0
        self.data_stamp = '2026-10-20T12:00:00'
        self.facts = dict(FACTS)                 # what the card data says now; a test changes it to stand for a card data update

        def facts():
            self.facts_calls += 1
            return dict(self.facts)
        self.check = mock.Mock(index=mock.Mock(side_effect=lambda: self.index), index_folder=self.root)
        self.database = mock.Mock(printing_facts=facts, get_data_info=mock.Mock(side_effect=lambda game: {'downloaded_at': self.data_stamp}))
        for name, value in (('printing_check', self.check), ('database', self.database)):
            patcher = mock.patch.object(stations_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        stations_api.index_table_cache.clear()
        self.addCleanup(stations_api.index_table_cache.clear)
        self.client = app.test_client()

    def listing(self, **headers):
        return self.client.get('/api/image_index/download', base_url=BASE, headers=headers)

    def file(self, kind, generation='current', version='current', **headers):
        generation = self.manifest['generation'] if generation == 'current' else generation
        query = '' if generation is None else f'?generation={generation}'
        if kind == 'printings' and version is not None:       # as the listing's address has it
            version = self.printings_version() if version == 'current' else version
            query += f'&version={version}'
        return self.client.get(f'/api/image_index/download/{kind}{query}', base_url=BASE, headers=headers)

    def printings_version(self):
        return next(f['sha256'] for f in self.listing().get_json()['files'] if f['kind'] == 'printings')


class TheListing(Download):
    def test_it_names_the_generation_and_each_file_with_its_size_and_checksum(self):
        answer = self.listing()
        self.assertEqual(answer.status_code, 200)
        body = answer.get_json()
        self.assertEqual((body['descriptor'], body['count'], body['hash_bytes'], body['generation']),
                         (image_index.DESCRIPTOR, 3, image_index.HASH_BYTES, self.manifest['generation']))
        files = {f['kind']: f for f in body['files']}
        self.assertEqual(set(files), {'hashes', 'ids', 'printings'})
        self.assertEqual(files['hashes']['bytes'], 3 * image_index.HASH_BYTES)
        self.assertEqual(files['hashes']['sha256'], self.manifest['hashes_sha256'])
        self.assertEqual(files['ids']['sha256'], self.manifest['ids_sha256'])
        for kind, entry in files.items():
            served = self.client.get(entry['url'], base_url=BASE)
            self.assertEqual(served.status_code, 200, kind)
            self.assertEqual(len(served.data), entry['bytes'], kind)                       # what it says is what is sent
            self.assertEqual(hashlib.sha256(served.data).hexdigest(), entry['sha256'], kind)

    def test_without_an_index_there_is_nothing_to_download(self):
        self.index = None
        self.assertEqual(self.listing().status_code, 404)
        self.assertEqual(self.file('hashes').status_code, 404)


class TheFiles(Download):
    def test_the_hashes_and_the_ids_are_the_files_on_disk(self):
        self.assertEqual(self.file('hashes').data, b''.join(HASHES))
        self.assertEqual(self.file('ids').data.decode().split(), IDS)

    def test_a_file_can_be_resumed(self):
        answer = self.file('hashes', Range='bytes=96-191')
        self.assertEqual(answer.status_code, 206)
        self.assertEqual(answer.data, HASHES[1])                                           # the second fingerprint, and only it
        self.assertEqual(answer.headers['ETag'].strip('"'), self.manifest['hashes_sha256'])

    def test_a_file_of_another_generation_is_not_served(self):
        for generation in ('1-deadbeef', None, ''):
            answer = self.file('hashes', generation)
            self.assertEqual(answer.status_code, 409, generation)
            self.assertEqual(answer.get_json()['generation'], self.manifest['generation'])   # it says which one is current

    def test_an_unknown_file_is_refused_and_a_path_in_the_manifest_never_leaves_the_folder(self):
        self.assertEqual(self.file('manifest').status_code, 404)
        self.assertEqual(self.file('../x').status_code, 404)
        tampered = dict(self.manifest, hashes='../outside.bin')
        (self.root / image_index.MANIFEST).write_text(json.dumps(tampered))
        self.assertEqual(self.file('hashes').status_code, 404)


class ThePrintingsTable(Download):
    def rows(self):
        text = gzip.decompress(self.file('printings').data).decode()
        lines = text.rstrip('\n').split('\n')
        return lines[0].split('\t'), [line.split('\t') for line in lines[1:]]

    def test_one_row_per_printing_of_the_index_that_the_card_data_has_in_the_index_order(self):
        header, rows = self.rows()
        # released_at and frame (since 2026-10-21, for the phone's fixed-place foil reading) come LAST: a reader of the first six columns is undisturbed
        self.assertEqual(header, ['id', 'set', 'number', 'oracle_id', 'finishes', 'name', 'released_at', 'frame'])
        self.assertEqual([row[0] for row in rows], ['id-a', 'id-b'])                       # id-c: not in the card data; id-c-gone: not in the index
        self.assertEqual(rows[0], ['id-a', 'sld', 'IFIYW-2', 'oracle-1', 'nonfoil,foil', 'Lightning Bolt', '2023-06-23', '2015'])   # the set in lower case, as the card data writes it
        self.assertEqual(rows[1][1:], ['tfra', '1', 'oracle-2', 'nonfoil', 'Tab and newline', '', ''])        # a tab or a newline in a name cannot break a row; a fact of five: empty

    def test_the_same_input_gives_the_same_bytes(self):
        ids = list(IDS)
        self.assertEqual(image_index.printings_table(ids, FACTS), image_index.printings_table(ids, FACTS))

    def test_without_the_version_of_the_listing_it_is_not_served(self):
        # (Codex review, 2026-10-20: the table follows the card data, so the generation alone does not say which table a download is of)
        answer = self.file('printings', version=None)
        self.assertEqual(answer.status_code, 409)
        self.assertEqual(answer.get_json()['version'], self.printings_version())
        self.assertEqual(self.file('printings', version='0' * 64).status_code, 409)

    def test_a_card_data_update_between_the_listing_and_the_download_is_a_conflict_not_other_bytes(self):
        old = self.listing().get_json()
        old_url = next(f['url'] for f in old['files'] if f['kind'] == 'printings')
        before = self.client.get(old_url, base_url=BASE)
        self.assertEqual(before.status_code, 200)
        self.data_stamp = '2026-10-21T08:00:00'                                            # the card data was updated: the same generation, another table
        self.facts['id-a'] = ('Lightning Bolt', 'sld', 'IFIYW-7', 'oracle-1', 'nonfoil')
        after = self.client.get(old_url, base_url=BASE)
        self.assertEqual(after.status_code, 409)                                           # not 200 with different bytes
        self.assertNotEqual(after.get_json()['version'], old['files'][2]['sha256'])
        again = next(f for f in self.listing().get_json()['files'] if f['kind'] == 'printings')
        fresh = self.client.get(again['url'], base_url=BASE)                               # ask again, as the 409 says: the new table, whole
        self.assertEqual((fresh.status_code, hashlib.sha256(fresh.data).hexdigest()), (200, again['sha256']))
        self.assertNotEqual(fresh.data, before.data)

    def test_a_resumed_download_of_the_old_table_is_refused_too(self):
        old_url = next(f['url'] for f in self.listing().get_json()['files'] if f['kind'] == 'printings')
        first = self.client.get(old_url, base_url=BASE, headers={'Range': 'bytes=0-9'})
        self.assertEqual((first.status_code, len(first.data)), (206, 10))
        self.data_stamp = '2026-10-21T08:00:00'
        self.facts['id-b'] = ('Cadet', 'tfra', '1', 'oracle-2', 'nonfoil,foil')
        resumed = self.client.get(old_url, base_url=BASE, headers={'Range': 'bytes=10-'})
        self.assertEqual(resumed.status_code, 409)                                         # the rest of ANOTHER table is never sent

    def test_it_is_built_once_per_generation_and_card_data(self):
        self.listing()
        self.file('printings')
        self.file('printings')
        self.assertEqual(self.facts_calls, 1)
        self.data_stamp = '2026-10-21T08:00:00'                                            # a card data update: built again
        self.file('printings')
        self.assertEqual(self.facts_calls, 2)


class TheStationToken(Download):
    def test_a_token_that_is_set_is_asked_for(self):
        with mock.patch.object(Config, 'STATION_TOKEN', 'secret'):
            for answer in (self.listing(), self.file('hashes'), self.listing(**{'X-Station-Token': 'wrong'})):
                self.assertEqual(answer.status_code, 401)
            self.assertEqual(self.listing(**{'X-Station-Token': 'secret'}).status_code, 200)
            self.assertEqual(self.file('hashes', **{'X-Station-Token': 'secret'}).status_code, 200)

    def test_without_a_token_set_it_is_open_like_the_other_station_routes(self):
        with mock.patch.object(Config, 'STATION_TOKEN', ''):
            self.assertEqual(self.listing().status_code, 200)


if __name__ == '__main__':
    unittest.main()
