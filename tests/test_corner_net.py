"""
The corner network on a station (station/corner_net.py) and its place in ObjectDetector:
what is asked first, when its answer is believed, and that the outline detector remains the
fallback. The network's accuracy is measured on labelled frames (scripts/score_detector.py
--neural), not here - these tests need no labelled data.

    venv/bin/python -m unittest discover tests -p test_corner_net.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from card_scanner.station import corner_net  # noqa: E402
from card_scanner.station.object_detector import ObjectDetector, _plausible_neural  # noqa: E402


def card_scene(width=640, height=480, box=(250, 100, 400, 310)):   # 150 x 210: a card's shape (1.4)
    """A pale box with one dark, card-shaped rectangle in it (x1, y1, x2, y2)"""
    frame = np.full((height, width, 3), 225, np.uint8)
    x1, y1, x2, y2 = box
    cv2.rectangle(frame, (x1, y1), (x2, y2), (35, 35, 40), -1)
    cv2.rectangle(frame, (x1 + 10, y1 + 12), (x2 - 10, y2 - 12), (120, 150, 110), -1)
    return frame


QUAD = np.array([[250, 100], [400, 100], [400, 310], [250, 310]], np.float32)   # card_scene's card


class FakeNet:
    """Stands in for CornerNet: answers what it is told, and remembers what it was asked"""

    def __init__(self, corners=QUAD, score=0.99, error=None):
        self.corners, self.score, self.error, self.asked = corners, score, error, []

    def find(self, rgb, previous=None):
        self.asked.append(previous)
        if self.error:
            raise self.error
        return np.array(self.corners, np.float32), self.score


class TheFile(unittest.TestCase):
    def test_the_network_is_beside_the_module(self):
        # paths.py's one exception: a file that names another lying beside it - the image
        # copies the whole station/ folder, so the file travels with the module
        self.assertTrue(corner_net.MODEL_FILE.is_file())
        self.assertEqual(corner_net.MODEL_FILE.parent, Path(corner_net.__file__).parent)
        self.assertLess(corner_net.MODEL_FILE.stat().st_size, 10 * 1024 * 1024)

    def test_off_loads_nothing_and_auto_loads_it(self):
        for setting in ('off', 'OFF', 'false', '0'):
            self.assertIsNone(corner_net.load(setting))
        self.assertIsInstance(corner_net.load('auto'), corner_net.CornerNet)

    def test_a_missing_or_broken_file_means_no_network_not_a_failed_start(self):
        from unittest import mock
        with mock.patch.object(corner_net, 'MODEL_FILE', Path('/nonexistent/corner_net.onnx')):
            self.assertIsNone(corner_net.load('auto'))
        broken = Path(__file__).with_name('test_corner_net.py')   # a file that is no ONNX model
        with mock.patch.object(corner_net, 'MODEL_FILE', broken):
            self.assertIsNone(corner_net.load('auto'))


class FindsACard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.net = corner_net.load('auto')

    def test_a_simple_card_is_found_where_it_is(self):
        corners, score = self.net.find(card_scene())
        self.assertEqual(corners.shape, (4, 2))
        self.assertGreater(score, 0.5)
        # top left, top right, bottom right, bottom left, within a few percent of the frame
        for got, want in zip(corners, QUAD):
            self.assertLess(abs(got[0] - want[0]) / 640, 0.06)
            self.assertLess(abs(got[1] - want[1]) / 480, 0.06)

    def test_a_previous_outline_is_followed(self):
        shifted = QUAD + np.array([8.0, 5.0], np.float32)
        corners, _ = self.net.find(card_scene(), previous=shifted)
        self.assertLess(float(np.abs(corners - QUAD).max()), 25.0)


class InTheObjectDetector(unittest.TestCase):
    def detect(self, net, frame=None):
        return ObjectDetector(neural=net).detect(card_scene() if frame is None else frame)

    def test_the_network_is_asked_first(self):
        net = FakeNet(corners=QUAD + 3)
        box, label, score, corners = self.detect(net)
        self.assertEqual(len(net.asked), 1)
        self.assertEqual(label, 'Card')
        self.assertTrue(np.allclose(corners, QUAD + 3))
        self.assertAlmostEqual(score, 0.99, places=2)

    def test_the_previous_outline_is_handed_on(self):
        net = FakeNet()
        ObjectDetector(neural=net).detect(card_scene(), previous=QUAD)
        self.assertTrue(np.array_equal(net.asked[0], QUAD))

    def test_an_unsure_network_hands_over_to_the_outline_detector(self):
        net = FakeNet(corners=QUAD + 40, score=0.2)
        _, _, _, corners = self.detect(net)
        # the outline detector's answer, not the network's: it follows the rectangle's real edge
        self.assertIsNotNone(corners)
        self.assertLess(float(np.abs(corners - QUAD).max()), 6.0)

    def test_a_shape_no_card_has_hands_over_too(self):
        wide = np.array([[100, 100], [500, 100], [500, 160], [100, 160]], np.float32)   # 6.7 : 1, not a card
        self.assertFalse(_plausible_neural(wide, (480, 640), False))
        _, _, _, corners = self.detect(FakeNet(corners=wide))
        self.assertLess(float(np.abs(corners - QUAD).max()), 6.0)

    def test_a_failing_network_is_dropped_and_scanning_goes_on(self):
        detector = ObjectDetector(neural=FakeNet(error=RuntimeError("dnn exploded")))
        _, _, _, corners = detector.detect(card_scene())
        self.assertIsNone(detector.neural)                       # not asked again
        self.assertIsNotNone(corners)                            # the outline detector answered

    def test_without_a_network_nothing_changes(self):
        _, _, _, corners = ObjectDetector().detect(card_scene())
        self.assertLess(float(np.abs(corners - QUAD).max()), 6.0)

    def test_a_network_outline_outside_the_frame_is_not_believed(self):
        outside = np.array([[-200, 50], [-50, 50], [-50, 290], [-200, 290]], np.float32)
        self.assertFalse(_plausible_neural(outside, (480, 640), False))
        self.assertTrue(_plausible_neural(QUAD, (480, 640), False))
        self.assertFalse(_plausible_neural(QUAD[::-1][[0, 2, 1, 3]], (480, 640), False))   # a bow tie, not convex


if __name__ == '__main__':
    unittest.main()
