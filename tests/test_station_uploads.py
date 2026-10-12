"""
A camera station's captures that are not on the server yet (station_client.py): each has a
note on disk, so that a restart - from the setup page, of the container, of the machine -
does not strand it, and a capture that may or may not have arrived is sent again under the
same capture_id. No camera and no network: the "server" is a stand-in that records what it
is sent, and the pictures are in a temporary folder.

    venv/bin/python -m unittest discover tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import station_client  # noqa: E402
from config import Config  # noqa: E402

SERVER = 'http://server-a:5000'


class Answer:
    """What requests' post() returns, as far as upload_verdict looks at it"""

    def __init__(self, status_code, body):
        self.status_code, self.body, self.text = status_code, body, json.dumps(body)

    def json(self):
        return self.body


class Server:
    """Stands in for requests.Session: records each capture sent and answers as told"""

    def __init__(self, *answers):
        self.answers = list(answers)  # one per post(); the last one is repeated
        self.received = []            # (url, capture_id, token) of every post that arrived

    def post(self, url, files=None, headers=None, data=None, timeout=None):
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        self.received.append((url, data['capture_id'], (headers or {}).get('X-Station-Token')))
        return answer


ACCEPTED = Answer(202, {'capture': 1})
REFUSED = Answer(400, {'success': False, 'message': 'not a picture'})


class Uploads(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        # Two folders, as on a machine that runs the server and a station from one checkout:
        # the station's waiting captures, and the server's received ones (scanned_cards/)
        self.pictures = Path(self.folder.name) / 'station_captures'
        self.server_pictures = Path(self.folder.name) / 'scanned_cards'
        self.pictures.mkdir()
        self.server_pictures.mkdir()
        for name, folder in (('STATION_CAPTURES_DIR', self.pictures), ('IMAGES_DIR', self.server_pictures)):
            patcher = mock.patch.object(Config, name, folder)
            patcher.start()
            self.addCleanup(patcher.stop)

    def station(self, server=SERVER, station_id='desk', token=''):
        return station_client.Station(server, station_id, 'Desk camera', token)

    def capture(self, station, number, with_foil=False):
        """Queue a capture as auto_captured does: a picture on disk, then queue_upload"""
        image = self.pictures / f"card_{number}_17917{number:05d}.jpg"
        image.write_bytes(b'\xff\xd8 a picture \xff\xd9')
        card = np.zeros((4, 3, 3), np.uint8)
        station.queue_upload(image, card, np.zeros((4, 3, 3), np.uint8) if with_foil else card, 'auto')
        return image

    def notes(self):
        return sorted(path.name for path in self.pictures.glob('*' + station_client.NOTE_SUFFIX))

    def send_all(self, station, server):
        while not station.uploads.empty():
            station.send_until_settled(server, station.uploads.get())

    # --------------------------------------------------------------------

    def test_a_queued_capture_has_a_note(self):
        station = self.station()
        image = self.capture(station, 1, with_foil=True)
        note = json.loads(station.note_path(image).read_text())
        queued = station.uploads.get()
        self.assertEqual(note['capture_id'], queued['capture_id'])
        self.assertEqual((note['server'], note['station'], note['mode']), (SERVER, 'desk', 'auto'))
        self.assertEqual((note['image'], note['foil'], note['foil_is_image']), (image.name, queued['foil'].name, False))

    def test_accepted_removes_pictures_and_note(self):
        station = self.station()
        image = self.capture(station, 1, with_foil=True)
        server = Server(ACCEPTED)
        self.send_all(station, server)
        self.assertEqual(len(server.received), 1)
        self.assertEqual(list(self.pictures.iterdir()), [])
        self.assertFalse(image.exists())

    def test_refused_keeps_the_picture_but_never_sends_it_again(self):
        station = self.station()
        image = self.capture(station, 1)
        self.send_all(station, Server(REFUSED))
        self.assertTrue(image.exists())
        self.assertEqual(self.notes(), [])
        again = self.station()
        again.load_pending()
        self.assertTrue(again.uploads.empty())

    def test_captures_waiting_at_a_restart_are_sent_by_the_next_run(self):
        # The server is away; three cards are captured; the station restarts (as after the
        # setup page's "Save and restart", with a corrected token)
        before = self.station(token='wrong')
        ids = []
        for number in (1, 2, 3):
            self.capture(before, number)
        while not before.uploads.empty():
            ids.append(before.uploads.get()['capture_id'])  # never sent: the queue dies with the program
        self.assertEqual(len(self.notes()), 3)

        after = self.station(token='right')
        after.load_pending()
        server = Server(ACCEPTED)
        self.send_all(after, server)
        self.assertEqual([capture_id for _, capture_id, _ in server.received], ids)  # the same ids, oldest first
        self.assertEqual({token for _, _, token in server.received}, {'right'})       # with the token of now
        self.assertEqual(list(self.pictures.iterdir()), [])

    def test_a_capture_that_may_have_arrived_is_sent_again_under_the_same_id(self):
        # The picture was sent and the connection broke before the answer: arrived or not?
        before = self.station()
        self.capture(before, 1)
        item = before.uploads.get()
        self.assertEqual(before.send_once(Server(requests.ConnectionError("cut off")), item), 'retry')
        self.assertEqual(len(self.notes()), 1)  # not settled: the note stays

        after = self.station()
        after.load_pending()
        server = Server(ACCEPTED)
        self.send_all(after, server)
        # The server counts a capture_id once, so the second arrival is the same card
        self.assertEqual([capture_id for _, capture_id, _ in server.received], [item['capture_id']])

    def test_a_stopping_station_leaves_the_note_for_the_next_run(self):
        station = self.station()
        self.capture(station, 1)
        station.stopping = True
        server = Server(Answer(503, {}))
        station.send_until_settled(server, station.uploads.get())
        self.assertEqual(len(self.notes()), 1)

    def test_captures_for_another_server_are_held_not_sent(self):
        before = self.station(server='http://server-a:5000')
        self.capture(before, 1)
        self.capture(before, 2)
        after = self.station(server='http://server-b:5000')
        after.load_pending()
        self.assertTrue(after.uploads.empty())
        held = after.held_summary()
        self.assertEqual((held['count'], held['server'], held['station']), (2, 'http://server-a:5000', 'desk'))

    def test_another_station_id_is_held_too(self):
        self.capture(self.station(station_id='desk'), 1)
        after = self.station(station_id='shelf')
        after.load_pending()
        self.assertTrue(after.uploads.empty())
        self.assertEqual(after.held_summary()['count'], 1)

    def test_held_captures_can_be_sent_to_the_new_server(self):
        before = self.station(server='http://server-a:5000')
        self.capture(before, 1)
        capture_id = before.uploads.get()['capture_id']
        after = self.station(server='http://server-b:5000')
        after.load_pending()
        self.assertEqual(after.send_held(), 1)
        self.assertIsNone(after.held_summary())
        server = Server(ACCEPTED)
        self.send_all(after, server)
        self.assertEqual(server.received, [('http://server-b:5000/api/stations/desk/captures', capture_id, None)])
        self.assertEqual(list(self.pictures.iterdir()), [])

    def test_sending_held_captures_survives_another_restart(self):
        self.capture(self.station(server='http://server-a:5000'), 1)
        after = self.station(server='http://server-b:5000')
        after.load_pending()
        after.send_held()  # their notes now name server b
        again = self.station(server='http://server-b:5000')
        again.load_pending()
        self.assertEqual(again.uploads.qsize(), 1)
        self.assertIsNone(again.held_summary())

    def test_held_captures_can_be_deleted(self):
        self.capture(self.station(server='http://server-a:5000'), 1, with_foil=True)
        after = self.station(server='http://server-b:5000')
        after.load_pending()
        self.assertEqual(after.delete_held(), 1)
        self.assertEqual(list(self.pictures.iterdir()), [])

    def test_a_note_without_its_picture_and_a_broken_note(self):
        station = self.station()
        image = self.capture(station, 1)
        image.unlink()
        (self.pictures / ('junk' + station_client.NOTE_SUFFIX)).write_text('{"capture_id": ')
        after = self.station()
        after.load_pending()
        self.assertTrue(after.uploads.empty())
        self.assertEqual(self.notes(), ['junk' + station_client.NOTE_SUFFIX])  # the orphan is gone, the broken one left alone

    def test_the_server_s_cleanup_does_not_reach_waiting_captures(self):
        # The server deletes pictures older than cleanup.days from scanned_cards/. A capture
        # that has waited longer than that (the server away, a wrong token) must survive it.
        import os
        import time
        import cleanup
        station = self.station()
        waiting = self.capture(station, 1, with_foil=True)
        received = self.server_pictures / 'desk_7_1791000000.jpg'
        received.write_bytes(b'a picture the server has')
        long_ago = time.time() - 400 * 24 * 3600
        for path in list(self.pictures.iterdir()) + [received]:
            os.utime(path, (long_ago, long_ago))
        cleanup.cleanup_old_images(days_to_keep=30)
        self.assertFalse(received.exists())          # the server's old picture went, as it should
        self.assertTrue(waiting.exists())            # the station's did not
        self.assertEqual(len(self.notes()), 1)
        after = self.station()
        after.load_pending()
        self.assertEqual(after.uploads.qsize(), 1)   # and it is still sent

    def test_captures_an_earlier_version_left_in_scanned_cards_are_moved_over(self):
        # Until 2026-10-12 waiting captures lay in scanned_cards/, among the server's pictures
        old_picture = self.server_pictures / 'card_3_1791754770.jpg'
        old_foil = self.server_pictures / 'card_3_1791754770_foil.jpg'
        old_picture.write_bytes(b'waiting'); old_foil.write_bytes(b'its foil picture')
        (self.server_pictures / ('card_3_1791754770' + station_client.NOTE_SUFFIX)).write_text(json.dumps({
            'capture_id': 'from-the-old-folder', 'mode': 'auto', 'foil_is_image': False, 'image': old_picture.name,
            'foil': old_foil.name, 'server': SERVER, 'station': 'desk', 'taken': '2026-10-11 20:00:00'}))
        servers_own = self.server_pictures / 'desk_7_1791000000.jpg'
        servers_own.write_bytes(b'a picture the server has')

        station = self.station()
        station.load_pending()
        item = station.uploads.get()
        self.assertEqual(item['capture_id'], 'from-the-old-folder')
        self.assertEqual((item['image'].parent, item['foil'].parent), (self.pictures, self.pictures))
        self.assertTrue(item['image'].exists() and item['foil'].exists())
        self.assertEqual(sorted(p.name for p in self.server_pictures.iterdir()), [servers_own.name])  # only the server's is left there

    def test_pending_count(self):
        station = self.station()
        self.capture(station, 1)
        self.capture(station, 2)
        self.assertEqual(station.pending_count(), 2)
        station.upload_idle.clear()  # as while one is being sent
        station.uploads.get()
        self.assertEqual(station.pending_count(), 2)


if __name__ == '__main__':
    unittest.main()
