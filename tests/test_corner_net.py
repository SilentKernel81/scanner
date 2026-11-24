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

    def test_a_blurred_card_the_zoomed_window_doubts_is_still_a_card(self):
        # A card falling into the tray: the whole-frame look sees it, the zoomed window does not (motion blur) - the better score stands
        from unittest import mock
        answers = iter([(QUAD.astype(np.float64), 0.9), (QUAD.astype(np.float64), 0.05)])
        with mock.patch.object(self.net, '_read', side_effect=lambda *a, **k: next(answers)):
            _, score = self.net.find(card_scene())
        self.assertAlmostEqual(score, 0.9, places=3)

    def test_a_moved_card_falls_back_to_stage1_when_previous_misses(self):
        # Card is on the right half of the frame while `previous` points at the empty left half
        moved = card_scene(box=(420, 100, 570, 310))
        old_quad = np.array([[50, 100], [200, 100], [200, 310], [50, 310]], np.float32)
        target = np.array([[420, 100], [570, 100], [570, 310], [420, 310]], np.float32)
        corners, score = self.net.find(moved, previous=old_quad)
        self.assertGreater(score, 0.5)
        self.assertLess(float(np.abs(corners - target).max()), 35.0)

    def test_empty_frame_skips_stage2_refinement(self):
        from unittest import mock
        empty = np.full((480, 640, 3), 230, np.uint8)
        with mock.patch.object(self.net, '_read', wraps=self.net._read) as read:
            _, score = self.net.find(empty)
        self.assertLess(score, corner_net.CornerNet.STAGE1_SKIP_REFINE)
        self.assertEqual(read.call_count, 1)

    def test_degenerate_previous_quad_does_not_crash_and_falls_back_to_stage1(self):
        zero_quad = np.zeros((4, 2), np.float32)
        corners, score = self.net.find(card_scene(), previous=zero_quad)
        self.assertGreater(score, 0.5)
        self.assertLess(float(np.abs(corners - QUAD).max()), 35.0)

    def test_refine_corners_runs_one_window(self):
        from unittest import mock
        shifted = QUAD + np.array([6.0, -4.0], np.float32)
        with mock.patch.object(self.net, '_read', wraps=self.net._read) as read:
            refined, _ = self.net.refine_corners(card_scene(), shifted)
        self.assertEqual(read.call_count, 1)
        self.assertLess(float(np.abs(refined - QUAD).max()), 20.0)


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

    def test_implausible_previous_guided_result_retries_whole_frame_once(self):
        wide = np.array([[100, 100], [500, 100], [500, 160], [100, 160]], np.float32)

        class RetryNet:
            def __init__(self):
                self.asked = []

            def find(self, rgb, previous=None):
                self.asked.append(previous)
                if previous is not None:
                    return wide, 0.9
                return QUAD.copy(), 0.98

        net = RetryNet()
        _, label, score, corners = ObjectDetector(neural=net).detect(card_scene(), previous=QUAD + 30)
        self.assertEqual(len(net.asked), 2)
        self.assertIsNotNone(net.asked[0])
        self.assertIsNone(net.asked[1])
        self.assertEqual(label, 'Card')
        self.assertAlmostEqual(score, 0.98, places=2)
        self.assertTrue(np.allclose(corners, QUAD))

    def test_one_network_error_does_not_switch_the_network_off(self):
        from card_scanner.station import object_detector

        class Flaky:
            def __init__(self, fails):
                self.fails, self.calls = fails, 0

            def find(self, rgb, previous=None):
                self.calls += 1
                if self.calls <= self.fails:
                    raise RuntimeError('onnx hiccup')
                return QUAD.copy(), 0.99

        net = Flaky(fails=object_detector.NEURAL_MAX_FAILURES - 1)
        detector = ObjectDetector(neural=net)
        for _ in range(object_detector.NEURAL_MAX_FAILURES - 1):
            detector.detect(card_scene())
        self.assertIs(detector.neural, net)                     # a few errors: still asked
        _, _, score, _ = detector.detect(card_scene())
        self.assertAlmostEqual(score, 0.99, places=2)           # and its answer is used again once it works
        # the count starts over after a success: as many errors again still leave it on
        net.calls, net.fails = 0, object_detector.NEURAL_MAX_FAILURES - 1
        for _ in range(object_detector.NEURAL_MAX_FAILURES - 1):
            detector.detect(card_scene())
        self.assertIs(detector.neural, net)

    def test_a_network_that_keeps_failing_is_switched_off(self):
        from card_scanner.station import object_detector
        detector = ObjectDetector(neural=FakeNet(error=RuntimeError('broken model')))
        for _ in range(object_detector.NEURAL_MAX_FAILURES):
            detector.detect(card_scene())
        self.assertIsNone(detector.neural)

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
        from card_scanner.station import object_detector
        detector = ObjectDetector(neural=FakeNet(error=RuntimeError("dnn exploded")))
        for _ in range(object_detector.NEURAL_MAX_FAILURES):
            _, _, _, corners = detector.detect(card_scene())
            self.assertIsNotNone(corners)                        # the outline detector answered every frame
        self.assertIsNone(detector.neural)                       # errors in a row: not asked again

    def test_without_a_network_nothing_changes(self):
        _, _, _, corners = ObjectDetector().detect(card_scene())
        self.assertLess(float(np.abs(corners - QUAD).max()), 6.0)

    def test_a_network_outline_outside_the_frame_is_not_believed(self):
        outside = np.array([[-200, 50], [-50, 50], [-50, 290], [-200, 290]], np.float32)
        self.assertFalse(_plausible_neural(outside, (480, 640), False))
        self.assertTrue(_plausible_neural(QUAD, (480, 640), False))
        self.assertFalse(_plausible_neural(QUAD[::-1][[0, 2, 1, 3]], (480, 640), False))   # a bow tie, not convex


class CaptureRefinementAndAveraging(unittest.TestCase):
    def _scanner(self, half_size=True, neural=None):
        import threading
        from card_scanner.station.scanner import CardScanner
        s = object.__new__(CardScanner)
        s.frame_lock = threading.Lock()
        s.half_size_decode = half_size
        s.current_small = None
        s.rotation, s.camera_rotation = 0, 0
        s.fixed_area_enabled = False
        s._fixed_area_active = lambda: False
        s.required_stable_frames = 5
        s.object_detector = ObjectDetector(neural=neural)
        return s

    def test_settled_corners_average_and_reset(self):
        s = self._scanner(half_size=False)
        for dx in (-2.0, -1.0, 0.0, 1.0, 2.0):
            s._record_settled_corners(QUAD + np.array([dx, 0.0], np.float32))
        avg = s._averaged_settled_corners(QUAD + 99)
        self.assertTrue(np.allclose(avg, QUAD, atol=1e-4))
        # Bounded to required_stable_frames (5): pushing 5 shifted frames replaces the old average
        for _ in range(5):
            s._record_settled_corners(QUAD + np.array([4.0, 0.0], np.float32))
        self.assertTrue(np.allclose(s._averaged_settled_corners(QUAD), QUAD + np.array([4.0, 0.0], np.float32)))
        s._clear_settled_corners()
        self.assertTrue(np.allclose(s._averaged_settled_corners(QUAD), QUAD))

    def test_get_detected_card_refines_upscaled_corners_on_full_frame(self):
        full_quad = QUAD * 2.0  # in 1280x960 full-res pixels
        snapped = full_quad + np.array([4.0, -3.0], np.float32)  # small (~1% diag) refinement

        class StubRefineNet:
            def __init__(self, target, score=0.95):
                self.target, self.score, self.calls = target, score, []

            def refine_corners(self, rgb, corners):
                self.calls.append((rgb.shape[:2], corners.copy()))
                return self.target.copy(), self.score

        full = card_scene(width=1280, height=960, box=(500, 200, 800, 620))
        ok, buf = cv2.imencode('.jpg', cv2.cvtColor(full, cv2.COLOR_RGB2BGR))
        self.assertTrue(ok)
        raw = np.frombuffer(buf.tobytes(), np.uint8)
        half = cv2.resize(full, (640, 480))

        # Valid full-res refinement is accepted
        net = StubRefineNet(snapped, score=0.95)
        s = self._scanner(half_size=True, neural=net)
        s.detected_card = (half, (250, 100, 400, 310), QUAD.copy(), raw)
        s.detected_card_name = 'Card'
        snapshot = {}
        card, _, _ = s.get_detected_card(snapshot)
        self.assertIsNotNone(card)
        self.assertEqual(len(net.calls), 1)
        self.assertEqual(net.calls[0][0], (960, 1280))
        self.assertTrue(np.allclose(snapshot['corners'], snapped, atol=0.15))

        # Refinement that jumps >8% of diagonal is rejected (keeps upscaled corners)
        jumped = StubRefineNet(full_quad + np.array([150.0, 0.0], np.float32), score=0.99)
        s.object_detector = ObjectDetector(neural=jumped)
        snapshot2 = {}
        s.get_detected_card(snapshot2)
        self.assertTrue(np.allclose(snapshot2['corners'], full_quad, atol=0.15))

        # Low-score refinement is rejected (keeps upscaled corners)
        unsure = StubRefineNet(snapped, score=0.2)
        s.object_detector = ObjectDetector(neural=unsure)
        snapshot3 = {}
        s.get_detected_card(snapshot3)
        self.assertTrue(np.allclose(snapshot3['corners'], full_quad, atol=0.15))

    def test_full_resolution_refinement_may_only_nudge_the_corners(self):
        # The refinement is there for the detector's ~1-1.5% jitter: a move of 5% of the diagonal is another answer, not a nudge
        from card_scanner.station import scanner as scanner_module
        full_quad = QUAD * 2.0
        diag = float(np.linalg.norm(full_quad[2] - full_quad[0]))

        class Stub:
            def __init__(self, target):
                self.target = target

            def refine_corners(self, rgb, corners):
                return self.target.copy(), 0.95

        frame = card_scene(width=1280, height=960, box=(500, 200, 800, 620))
        for share, kept in ((0.01, False), (0.05, True)):
            moved = full_quad + np.array([diag * share, 0.0], np.float32)
            s = self._scanner(half_size=False, neural=Stub(moved))
            out = s._refine_capture_corners(frame, full_quad)
            self.assertTrue(np.allclose(out, full_quad if kept else moved), share)
        self.assertLessEqual(scanner_module.CAPTURE_REFINE_MAX_SHIFT, 0.03)

    def test_warp_card_handles_tiny_degenerate_quad_without_crashing(self):
        from card_scanner.station.object_detector import warp_card
        tiny = np.array([[10, 10], [10.1, 10], [10.1, 10.1], [10, 10.1]], np.float32)
        out = warp_card(card_scene(), tiny)
        self.assertGreaterEqual(out.shape[0], 2)
        self.assertGreaterEqual(out.shape[1], 2)


if __name__ == '__main__':
    unittest.main()
