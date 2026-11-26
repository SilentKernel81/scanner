"""
The stillness test of auto-capture (station/scanner.py: _is_card_settled): how far a card's
outline may jitter between frames and still count as a card at rest. The limit is
auto_capture.max_movement (1.5%; it was 1%, which kept cards waiting 5-15 s on a tall sleeved
pile once the corner network drew the outline - Pi 5, 2026-10-15). A card that really moves
must still not count.

A fake frame with a sharp pattern and a fake camera object: no camera, no network.

    venv/bin/python -m unittest discover tests -p test_settle_limit.py
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

from card_scanner.config import Config  # noqa: E402
from card_scanner.station import scanner as scanner_module  # noqa: E402

SIZE = 400.0   # the card's diagonal in pixels is about 500: 1% = ~5 px
CARD = np.array([[300, 100], [600, 100], [600, 500], [300, 500]], np.float32)


def sharp_frame():
    rng = np.random.default_rng(1)
    return rng.integers(0, 255, (720, 1280, 3), dtype=np.uint8)   # noise: very sharp


def framed_card_scene():
    """White scanning box with a black-bordered textured card at CARD (reproduces border-edge sharpness spikes)."""
    rng = np.random.default_rng(7)
    frame = np.full((720, 1280, 3), 240, np.uint8)
    frame[100:500, 300:600] = 20
    # Card-like macro structure (title bar, dark art box, light text box) plus fine texture for sharpness
    frame[120:160, 320:580] = 210
    frame[165:330, 320:580] = 55
    frame[195:300, 365:535] = 195
    frame[340:475, 320:580] = 215
    for y in range(355, 465, 14):
        frame[y:y + 4, 335:565] = 45
    noise = rng.integers(-25, 25, (360, 260, 3), dtype=np.int16)
    frame[120:480, 320:580] = np.clip(frame[120:480, 320:580].astype(np.int16) + noise, 0, 255).astype(np.uint8)
    return frame


def bare_scanner():
    s = scanner_module.CardScanner.__new__(scanner_module.CardScanner)
    for name, value in dict(
        previous_card_points=None, previous_sharpness=None, previous_thumbnail=None,
        stable_frames=0, required_stable_frames=6, settled_corners=(),
        settle_anchor=None, _settle_anchor_samples=0,
        _sharpness_box=None, _sharpness_box_corners=None,
        _interior_prev=None, _interior_anchor=None, _interior_anchor_samples=0,
        _pixel_still_streak=0, _settle_hiccups=0, last_settle_outcome='reset',
        card_in_focus=False, card_disturbed=False, missing_frames=0,
        tracked_outline=None, frame_lock=threading.Lock(),
        fixed_area_enabled=True, fixed_area=[0.23, 0.14, 0.47, 0.70],
        enable_detection=True, auto_capture_enabled=False, awaiting_new_card=False,
        require_drop_signal=False, card_under_review=False, capture_pending=False,
        focus_sweep_running=False, focus_probe_running=False, last_focus_move=0.0,
        disturbed_during_focus=False, last_auto_capture_time=0.0, auto_capture_delay=1.0,
        capture_flash_at=0.0, area_thumb=None, area_previous=None, _area_last_raw=None,
        area_anchor=None, _area_anchor_samples=0, _area_hiccups=0,
        area_captured=None, area_metrics=(0.0, 0.0, 0.0), area_big_changes=0,
        card_detected=False, detected_card=None, detected_card_name="",
    ).items():
        setattr(s, name, value)
    s._detect_outline = lambda frame, previous=None: (None, "", 0.0, None)
    s.log = mock.Mock()
    return s


class Settled(unittest.TestCase):
    def setUp(self):
        self.camera = scanner_module.CardScanner.__new__(scanner_module.CardScanner)
        for name, value in dict(previous_card_points=None, previous_sharpness=None, previous_thumbnail=None,
                                stable_frames=0, settle_anchor=None, card_in_focus=False,
                                card_disturbed=False).items():
            setattr(self.camera, name, value)
        self.frame = sharp_frame()

    def after(self, shift):
        """_is_card_settled for a card that moves by `shift` pixels (all four corners) after a first frame"""
        self.camera._is_card_settled(self.frame, CARD)                 # the first frame only notes the card
        return self.camera._is_card_settled(self.frame, CARD + np.float32(shift))

    def diagonal(self):
        return float(np.linalg.norm(CARD[2] - CARD[0]))

    def step(self, camera, frame, points):
        if camera._is_card_settled(frame, points):
            camera.stable_frames = min(camera.stable_frames + 1, camera.required_stable_frames)
            camera._record_settled_corners(points)
        elif getattr(camera, 'last_settle_outcome', 'reset') != 'hiccup':
            camera._reset_settle_state()

    def test_the_default_limit_is_one_and_a_half_percent(self):
        self.assertAlmostEqual(Config.AUTO_CAPTURE_MAX_MOVEMENT, 0.015)

    def test_a_jitter_of_one_to_one_and_a_half_percent_is_still(self):
        shift = self.diagonal() * 0.0125 / np.sqrt(2)                  # 1.25% along the diagonal
        self.assertTrue(self.after(shift))

    def test_two_percent_is_not_still(self):
        shift = self.diagonal() * 0.02 / np.sqrt(2)
        self.assertFalse(self.after(shift))

    def test_the_limit_can_be_set(self):
        shift = self.diagonal() * 0.0125 / np.sqrt(2)
        with mock.patch.object(Config, 'AUTO_CAPTURE_MAX_MOVEMENT', 0.01):
            self.assertFalse(self.after(shift))                        # as before, at 1%

    def test_one_pixel_border_jitter_does_not_swing_sharpness(self):
        # Fix 1: locked inset sharpness ROI ignores the white-tray / black-border step edge
        cam = bare_scanner()
        scene = framed_card_scene()
        cam._is_card_settled(scene, CARD)
        for i in range(6):
            jitter = np.array([[-1.0 if i % 2 else 0.0, 0.0], [0.0, 0.0], [0.0, 0.0], [-1.0 if i % 2 else 0.0, 0.0]], np.float32)
            self.assertTrue(cam._is_card_settled(scene, CARD + jitter))
            self.assertLess(cam.settle_metrics[2], 0.01)               # sharpness_change ~0% instead of >20%
            cam.stable_frames += 1
        self.assertEqual(cam.stable_frames, 6)

    def test_single_corner_jitter_and_oscillating_outline_settle_on_still_card(self):
        # Fix 2: single corner jittering 2.2% (mean 0.55%) settles in 6 frames
        cam = bare_scanner()
        scene = framed_card_scene()
        diag = self.diagonal()
        for step in range(7):
            pts = CARD.copy()
            if step % 2 == 1:
                pts[0] += np.float32(diag * 0.022 / np.sqrt(2))
            self.step(cam, scene, pts)
        self.assertEqual(cam.stable_frames, 6)

        # Fix 2: whole outline oscillating 2.2% on a physically still card settles via interior pixel stillness
        cam2 = bare_scanner()
        for step in range(10):
            shift = (diag * 0.022 / np.sqrt(2)) if (step % 2 == 1) else 0.0
            self.step(cam2, scene, CARD + np.float32(shift))
        self.assertEqual(cam2.stable_frames, 6)

    def test_slow_sliding_card_never_reaches_six_stable_frames(self):
        # A sleeved card sliding 2 px/frame (~0.4%/frame at 500 px diag) must never capture mid-slide
        cam = bare_scanner()
        scene = framed_card_scene()
        max_sf = 0
        for step in range(18):
            M = np.float32([[1, 0, 2.0 * step], [0, 1, 0]])
            moved = cv2.warpAffine(scene, M, (scene.shape[1], scene.shape[0]), borderMode=cv2.BORDER_REPLICATE)
            self.step(cam, moved, CARD + np.array([2.0 * step, 0.0], np.float32))
            max_sf = max(max_sf, cam.stable_frames)
        self.assertLess(max_sf, 6)

    def test_single_minor_hiccup_holds_streak_without_poisoning_anchor_while_two_reset(self):
        # Fix 3 & 4: an isolated jitter spike at 3/6 holds without double-resetting the next good frame
        cam = bare_scanner()
        scene = framed_card_scene()
        for _ in range(4):                                             # 1 init + 3 settled -> 3/6
            self.step(cam, scene, CARD)
        self.assertEqual(cam.stable_frames, 3)

        # Inject a single sharpness glitch (blurs just enough to change >20% while staying in focus)
        slightly_soft = cv2.GaussianBlur(scene, (5, 5), 0.9)
        self.step(cam, slightly_soft, CARD)
        self.assertEqual(cam.last_settle_outcome, 'hiccup')
        self.assertEqual(cam.stable_frames, 3)                         # held, not wiped to 0

        # Returning to the crisp still frame resumes right from 3 -> 4 (no double-reset)
        self.step(cam, scene, CARD)
        self.assertEqual(cam.last_settle_outcome, 'settled')
        self.assertEqual(cam.stable_frames, 4)

        # Two consecutive glitches DO hard-reset to 0
        self.step(cam, slightly_soft, CARD)
        self.assertEqual(cam.last_settle_outcome, 'hiccup')
        self.step(cam, slightly_soft, CARD)
        self.assertEqual(cam.last_settle_outcome, 'reset')
        self.assertEqual(cam.stable_frames, 0)

    def test_single_missing_frame_held_when_pixels_still_and_reset_on_second_or_jump(self):
        # Fix 4: 1-frame detector miss on a still card holds stable_frames; 2nd miss or jump resets
        cam = bare_scanner()
        scene = framed_card_scene()
        for _ in range(4):
            self.step(cam, scene, CARD)
        self.assertEqual(cam.stable_frames, 3)

        # 1st miss on identical frame -> tolerated
        self.assertTrue(cam._tolerate_missing_frame(scene))
        self.assertEqual(cam.stable_frames, 3)
        # 2nd consecutive miss -> rejected
        cam.missing_frames = 1
        self.assertFalse(cam._tolerate_missing_frame(scene))

        # Reset hiccup and test a real disturbance (blank/hand frame) on the 1st miss -> rejected
        cam.missing_frames = 0
        cam._settle_hiccups = 0
        hand_frame = np.full_like(scene, 180)
        self.assertFalse(cam._tolerate_missing_frame(hand_frame))

    def test_fixed_area_ignores_exposure_flicker_and_holds_single_minor_spike(self):
        # Fix 5: gain-adjusted diff ignores 10% LED/exposure flicker and holds a single minor spike
        cam = bare_scanner()
        scene = framed_card_scene()
        flicker = np.clip(scene.astype(np.float32) * 1.10, 0, 255).astype(np.uint8)
        for i in range(6):
            frame = flicker if i % 2 else scene
            cam._fixed_area_step(frame, None, frame.copy())
        self.assertEqual(cam.stable_frames, 6)

        # Single minor spike (change between AREA_STILL and AREA_MINOR_SPIKE) holds streak at 3/6
        cam2 = bare_scanner()
        for _ in range(3):
            cam2._fixed_area_step(scene, None, scene.copy())
        self.assertEqual(cam2.stable_frames, 3)
        # Shift by ~1.8 px -> diff ~4.0 (between AREA_STILL=3.0 and AREA_MINOR_SPIKE=5.5)
        shifted = cv2.warpAffine(scene, np.float32([[1, 0, 1.8], [0, 1, 0]]), (scene.shape[1], scene.shape[0]), borderMode=cv2.BORDER_REPLICATE)
        cam2._fixed_area_step(shifted, None, shifted.copy())
        self.assertEqual(cam2.stable_frames, 3)                        # held
        cam2._fixed_area_step(scene, None, scene.copy())
        self.assertEqual(cam2.stable_frames, 4)                        # resumed cleanly

    def test_a_held_hiccup_frame_is_never_the_one_captured(self):
        # A card ready (6/6) but waiting out the capture delay: the frame that ends the wait must itself be
        # still - a hiccup frame keeps the count (the streak is not lost) but is not photographed
        cam = bare_scanner()
        scene = framed_card_scene()
        cam._auto_capture_enabled = True
        cam._trigger_auto_capture = mock.Mock()
        cam.last_auto_capture_time = scanner_module.time.time() + 3600     # the delay has not passed yet
        for _ in range(7):
            cam._fixed_area_step(scene, None, scene.copy())
        self.assertEqual(cam.stable_frames, 6)
        cam._trigger_auto_capture.assert_not_called()
        cam.last_auto_capture_time = 0.0                                     # the delay is over ...
        shifted = cv2.warpAffine(scene, np.float32([[1, 0, 1.8], [0, 1, 0]]), (scene.shape[1], scene.shape[0]), borderMode=cv2.BORDER_REPLICATE)
        cam._fixed_area_step(shifted, None, shifted.copy())                 # ... on a hiccup frame
        self.assertEqual(cam.stable_frames, 6)
        cam._trigger_auto_capture.assert_not_called()
        cam._fixed_area_step(scene, None, scene.copy())                     # the next still frame is the photo
        cam._trigger_auto_capture.assert_called_once()

    def test_minor_hiccup_does_not_poison_interior_prev_or_sharpness_box(self):
        cam = bare_scanner()
        scene = framed_card_scene()
        for _ in range(4):
            self.step(cam, scene, CARD)
        self.assertEqual(cam.stable_frames, 3)
        saved_prev = cam._interior_prev.copy()
        saved_box = cam._sharpness_box

        # A blurred hiccup frame must hold without overwriting _interior_prev or _sharpness_box
        slightly_soft = cv2.GaussianBlur(scene, (5, 5), 0.9)
        self.step(cam, slightly_soft, CARD)
        self.assertEqual(cam.last_settle_outcome, 'hiccup')
        self.assertEqual(cam._sharpness_box, saved_box)
        np.testing.assert_array_equal(cam._interior_prev, saved_prev)

    def test_relocked_sharpness_box_reanchors_interior_anchor_in_matching_coordinates(self):
        # Frame 0 starts +1.6% shifted on corner 0, frames 1-3 settle at 0% (within 3% of box_corners),
        # frame 4 shifts corner 0 to -1.6% (>3% from box_corners, triggering box re-lock) -> must not
        # leave _interior_anchor from the old box coordinates to cause false drift on frame 5
        cam = bare_scanner()
        scene = framed_card_scene()
        diag = self.diagonal()
        offset_pos = np.array([[diag * 0.016, 0.0], [0.0, 0.0], [0.0, 0.0], [0.0, 0.0]], np.float32)
        offset_neg = np.array([[-diag * 0.016, 0.0], [0.0, 0.0], [0.0, 0.0], [0.0, 0.0]], np.float32)
        self.step(cam, scene, CARD + offset_pos)
        for _ in range(3):
            self.step(cam, scene, CARD)
        self.assertEqual(cam.stable_frames, 3)
        self.step(cam, scene, CARD + offset_neg)
        # If box re-locked during a settled frame, _interior_anchor must match the current box's patch
        expected_patch = cam._box_patch(scene, cam._sharpness_box)
        if cam.last_settle_outcome == 'settled':
            self.assertLess(cam._gain_adjusted_difference(expected_patch, cam._interior_anchor), 1.0)
        # Next crisp frame at CARD must settle cleanly without bogus coordinate-mismatch drift
        self.step(cam, scene, CARD)
        self.assertEqual(cam.last_settle_outcome, 'settled')


if __name__ == '__main__':
    unittest.main()
