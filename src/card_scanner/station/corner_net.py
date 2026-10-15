# corner_net.py
# Our own card corner network, run with OpenCV's dnn module (no PyTorch on a station).
#
# It is trained from labelled camera frames only (scripts/train_corner_net.py: the network's
# definition, the training and the scoring are there; docs/PROGRAM_DOCUMENTATION.md: what was
# measured). The file beside this module is its exported weights (ONNX, ~3 MB). Everything here
# must give the numbers training and scoring give: the window, its cut and its normalisation
# are the training script's `window_matrix` / `cut` / `to_tensor`, written out again.
#
# A frame is read in windows of 192 x 320 pixels (the network's input): the card's four corners
# (top left, top right, bottom right, bottom left) come out in the window's own pixels, as the
# expected position under a softmax of one map per corner, with a score for "a card is here".
#   1. the whole frame in one window: the card is small there, this only finds where it is;
#   2. a window of 1.45 x the card's box, cut from the frame itself: the corners come out
#      precisely. Once: a second window gave the same accuracy (IoU 0.978 both on 127 labelled
#      frames) and cost another 30 ms on a Pi 4. With the card's previous outline known (a card
#      held in view), the first stage is skipped and the window is put around that outline.

import logging
import threading
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger('scanner')

# The exported network, lying beside this module (a path built from this file's own place is the
# paths.py rule's one exception: a file that names another lying beside it)
MODEL_FILE = Path(__file__).with_name('corner_net.onnx')

WINDOW_W, WINDOW_H = 192, 320   # the network's input
STRIDE = 4                      # its corner maps are WINDOW_W / 4 x WINDOW_H / 4
ZOOM_OUT = 1.45                 # the second stage's window is this much larger than the card's box
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)


class CornerNet:
    """The network, loaded once; `find` reads a frame. One instance serves all threads (a lock)."""

    def __init__(self, path=MODEL_FILE, refine=1):
        self.net = cv2.dnn.readNetFromONNX(str(path))
        self.refine = refine           # windows of the second stage
        self._lock = threading.Lock()  # a dnn network is not safe to run from two threads at once
        # The map's cell centres, once: (WINDOW_H / 4, WINDOW_W / 4)
        self._ys = (np.arange(WINDOW_H // STRIDE, dtype=np.float32) + 0.5) * STRIDE
        self._xs = (np.arange(WINDOW_W // STRIDE, dtype=np.float32) + 0.5) * STRIDE

    # ------------------------------------------------------------------

    @staticmethod
    def _window(rgb, cx, cy, height):
        """The window of this height (frame pixels) centred on (cx, cy): (RGB window, 2x3 matrix frame -> window)"""
        scale = WINDOW_H / height
        matrix = np.array([[scale, 0.0, WINDOW_W / 2 - scale * cx],
                           [0.0, scale, WINDOW_H / 2 - scale * cy]], np.float64)
        flags = cv2.INTER_AREA if abs(scale) < 1 else cv2.INTER_LINEAR   # as the training script's `cut`
        window = cv2.warpAffine(rgb, matrix, (WINDOW_W, WINDOW_H), flags=flags, borderMode=cv2.BORDER_REPLICATE)
        return window, matrix

    def _run(self, window):
        """(corners (4, 2) in window pixels, score 0..1) of one RGB window"""
        blob = ((window.astype(np.float32) / 255.0 - MEAN) / STD).transpose(2, 0, 1)[None]
        with self._lock:
            self.net.setInput(np.ascontiguousarray(blob))
            outputs = self.net.forward(self.net.getUnconnectedOutLayersNames())
        maps = next(o for o in outputs if o.ndim == 4)[0]               # (4, H/4, W/4)
        presence = float(next(o for o in outputs if o.ndim == 2).reshape(-1)[0])
        flat = maps.reshape(4, -1)
        flat = np.exp(flat - flat.max(axis=1, keepdims=True))
        prob = (flat / flat.sum(axis=1, keepdims=True)).reshape(maps.shape)
        x = (prob.sum(axis=1) * self._xs).sum(axis=1)                   # expected x of each corner
        y = (prob.sum(axis=2) * self._ys).sum(axis=1)
        return np.stack([x, y], axis=1), 1.0 / (1.0 + np.exp(-presence))

    def _read(self, rgb, cx, cy, height):
        window, matrix = self._window(rgb, cx, cy, height)
        corners, score = self._run(window)
        inverse = cv2.invertAffineTransform(matrix)
        return corners @ inverse[:, :2].T + inverse[:, 2], score

    # ------------------------------------------------------------------

    def find(self, rgb, previous=None):
        """
        The card in an RGB frame: (corners, score) - corners a (4, 2) float array in the frame's
        pixels (top left, top right, bottom right, bottom left), score 0..1 how sure the network
        is that a card is there. previous: the card's corners in an earlier frame, if known.
        """
        height, width = rgb.shape[:2]
        if previous is None:
            # The whole frame in one window (the window is taller than any frame: letterboxed)
            quad, score = self._read(rgb, width / 2, height / 2, max(height, width * WINDOW_H / WINDOW_W))
        else:
            quad, score = np.asarray(previous, np.float64), 0.0
        for _ in range(self.refine):
            xs, ys = quad[:, 0], quad[:, 1]
            # the card's height; for a quad that came out too flat, a card's shape from its width
            card_h = max(ys.max() - ys.min(), (xs.max() - xs.min()) * WINDOW_H / WINDOW_W * 0.6)
            quad, again = self._read(rgb, (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2, card_h * ZOOM_OUT)
            score = max(score, again)
        return quad.astype(np.float32), float(score)


def load(setting='auto'):
    """
    The network for `detection.neural` (config.yaml / SCANNER_NEURAL_DETECTOR): 'auto' (and 'on')
    load the file beside this module and return None when that fails - a station then works
    with the outline detector alone; 'off' never loads it.
    """
    if str(setting).strip().lower() in ('off', 'false', '0', 'no'):
        return None
    if not MODEL_FILE.is_file():
        logger.warning(f"Corner network {MODEL_FILE.name} not found - the outline detector alone is used")
        return None
    try:
        return CornerNet(MODEL_FILE)
    except Exception as e:  # an OpenCV build that does not read the file: not worth stopping a station
        logger.warning(f"Corner network could not be loaded ({e}) - the outline detector alone is used")
        return None
