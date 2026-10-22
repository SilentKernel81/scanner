#!/usr/bin/env python3
"""
The Python reference of light-ocr's reading (docs/plans/ANDROID_OCR_PLAN.md, step 1): the same two PaddleOCR PP-OCRv6 small networks the server's OCR
reader runs (src/ocr/node_modules/@arcships/light-ocr-model-ppocrv6-small/bundle/), the same pre- and post-processing, in Python on OpenCV - so that the
Android port (Kotlin, OpenCV dnn + onnxruntime) can be checked against a reference that is CODE, line by line (light-ocr's own core is C++ in a Node
addon: no reference file can be made of it). It is checked against light-ocr itself by compare_with_light_ocr.py: the texts and the parses must agree,
the boxes only closely (C++ and Python OpenCV differ in sub-pixel ways).

Every constant is read from the bundle's normalized-config.json BY ITS KEY (a different bundle fails loudly, never silently); the steps are PaddleOCR's
own (ppocr/postprocess/db_postprocess.py, tools/infer/predict_rec.py, predict_system.py), as the config names them. Read in this order:

  1. Detection: the picture (BGR, as light-ocr decodes it) resized - the longer side at most runtimeDefaults.detection.maxSide (scale down only), each
     side rounded UP to a multiple of 32, at least minimumShortSide; normalised (pixel * scale - mean) / std in BGR order; the network; the DB
     post-processing (threshold, boxes of the contours, the "fast" score, unclip by unclipRatio, minimumBoxSide), the boxes scaled back and ordered
     clockwise from the top-left; the lines in reading order (rowBandPixels).
  2. Recognition, one line at a time: the quad cut out by a perspective warp (cubic, replicate border), turned when tall (tallLineRatio); resized to
     height 48, width ceil(48 w / h) up to maximumTensorWidth, the tensor at least minimumTensorWidth wide and padded with paddingValue; normalised;
     the network; CTC greedy decoding (blank 0, repeats collapsed), the confidence the mean of the kept steps' probabilities.

    from ppocr_reference import Reader
    lines = Reader().read_lines(rgb)            # [{'text', 'confidence', 'box': [[x, y] x 4]}] - server.mjs's format
    venv/bin/python scripts/experiments/ocr_port/ppocr_reference.py picture.jpg [--trace]
"""
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[3]
BUNDLE = REPO / 'src/ocr/node_modules/@arcships/light-ocr-model-ppocrv6-small/bundle'
BUNDLE_ID = 'ppocrv6-small-native-20260719.1'     # the bundle this reference was written against (normalized-config.json: bundleId)


class Reader:
    def __init__(self, bundle=BUNDLE):
        config = json.loads((bundle / 'normalized-config.json').read_text())
        if config['bundleId'] != BUNDLE_ID:
            raise SystemExit(f"another light-ocr bundle ({config['bundleId']}): check every step against its normalized-config.json before using this")
        det, rec, geometry = config['detection'], config['recognition'], config['geometry']
        runtime = config['runtimeDefaults']['detection']
        # --- detection constants (normalized-config.json: detection.*, runtimeDefaults.detection.*) ---
        assert det['input']['colorOrder'] == 'BGR' and det['input']['tensorLayout'] == 'NCHW'
        self.det_mean = np.array(det['normalize']['mean'], np.float32)             # in BGR order, as listed
        self.det_std = np.array(det['normalize']['std'], np.float32)
        self.det_scale = float(det['normalize']['scale'])                          # 1 / 255
        post = det['postprocess']
        assert post['algorithm'] == 'DB' and post['boxType'] == 'quad' and post['scoreMode'] == 'fast' and not post['useDilation']
        self.threshold = float(post['threshold'])                                  # 0.3: the bitmap
        self.box_threshold = float(post['boxThreshold'])                           # 0.6: a box's mean probability
        self.unclip_ratio = float(post['unclipRatio'])                             # 1.5
        self.min_side = float(post['minimumBoxSide'])                              # 3
        self.max_candidates = int(post['maxCandidates'])                           # 3000
        assert runtime['strategy'] == 'bounded' and runtime['dimensionMultipleRounding'] == 'ceil'
        self.max_side = int(runtime['maxSide'])                                    # 960
        self.min_short_side = int(runtime['minimumShortSide'])                     # 64
        # --- geometry (geometry.*) ---
        assert geometry['borderMode'] == 'replicate' and geometry['perspectiveInterpolation'] == 'cubic'
        assert geometry['tallLineRotation'] == 'counterclockwise90'
        self.row_band = float(geometry['rowBandPixels'])                           # 10
        self.tall_ratio = float(geometry['tallLineRatio'])                         # 1.5
        # --- recognition constants (recognition.*) ---
        rin = rec['input']
        assert rin['colorOrder'] == 'BGR' and rin['interpolation'] == 'linear' and rin['resizedContentWidthRounding'] == 'ceil'
        assert rin['tensorWidthRounding'] == 'truncate_toward_zero'
        self.rec_height = int(rin['shape'][1])                                     # 48
        self.min_width = int(rin['minimumTensorWidth'])                            # 320
        self.max_width = int(rin['maximumTensorWidth'])                            # 3200
        self.rec_mean = np.array(rec['normalize']['mean'], np.float32)
        self.rec_std = np.array(rec['normalize']['std'], np.float32)
        self.rec_scale = float(rec['normalize']['scale'])
        self.padding = float(rec['normalize']['paddingValue'])                     # 0.0, after normalising
        decode = rec['decode']
        assert decode['algorithm'] == 'CTC' and decode['blankIndex'] == 0 and decode['collapseRepeats']
        assert decode['confidence'] == 'mean_selected_argmax_probability'
        self.dictionary = json.loads((bundle / decode['dictionaryPath']).read_text())
        if isinstance(self.dictionary, dict):
            self.dictionary = self.dictionary.get('characters') or self.dictionary.get('dictionary') or list(self.dictionary)
        assert len(self.dictionary) == decode['dictionaryEntries'], (len(self.dictionary), decode['dictionaryEntries'])
        self.score_threshold = float(rec['defaultScoreThreshold'])                 # 0.0: every line with a text is kept
        # --- the two networks (the fp32 pair; webgpu/ is fp16 for GPUs) ---
        self.det = cv2.dnn.readNetFromONNX(str(bundle / 'det/inference.onnx'))
        self.rec = cv2.dnn.readNetFromONNX(str(bundle / 'rec/inference.onnx'))

    # ------------------------------------------------------------------ 1. detection

    def detection_size(self, height, width):
        """(rows, cols) of the detector's input: the longer side at most max_side (never enlarged), each side rounded UP to a multiple of 32, at least min_short_side"""
        ratio = min(1.0, self.max_side / max(height, width))
        def side(n):
            return max(self.min_short_side, int(math.ceil(n * ratio / 32.0)) * 32)
        return side(height), side(width)

    def detect(self, bgr, trace=None):
        """The text quads of a BGR picture, in its pixels, clockwise from the top-left, in reading order"""
        height, width = bgr.shape[:2]
        rows, cols = self.detection_size(height, width)
        resized = cv2.resize(bgr, (cols, rows), interpolation=cv2.INTER_LINEAR)
        blob = (resized.astype(np.float32) * self.det_scale - self.det_mean) / self.det_std
        self.det.setInput(blob.transpose(2, 0, 1)[None])
        prob = self.det.forward()[0, 0]                                            # rows x cols
        bitmap = (prob > self.threshold).astype(np.uint8)
        contours, _ = cv2.findContours(bitmap, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        ratio_x, ratio_y = width / cols, height / rows
        quads, raw = [], []
        for contour in contours[:self.max_candidates]:
            box, short = mini_box(contour)
            if short < self.min_side:
                continue
            if box_score_fast(prob, box) < self.box_threshold:
                continue
            grown, short = mini_box(grow(box, self.unclip_ratio).reshape(-1, 1, 2))
            if short < self.min_side + 2:
                continue
            raw.append((box.tolist(), grown.tolist()))
            # back to the picture's pixels (PaddleOCR rounds), clipped to it
            quad = np.empty_like(grown)
            quad[:, 0] = np.clip(np.round(grown[:, 0] * ratio_x), 0, width)
            quad[:, 1] = np.clip(np.round(grown[:, 1] * ratio_y), 0, height)
            quads.append(order_clockwise(quad))
        quads = reading_order(quads, self.row_band)
        if trace is not None:
            trace.update({'detection_size': [rows, cols], 'raw_boxes': raw, 'quads': [q.tolist() for q in quads]})
        return quads

    # ------------------------------------------------------------------ 2. recognition

    def tensor_width(self, crop_height, crop_width):
        """(the tensor's width, the content's width) of a crop: PaddleOCR's resize_norm_img with imgW = int(48 * max(320/48, w/h))"""
        ratio = crop_width / float(crop_height)
        tensor = int(self.rec_height * max(self.min_width / self.rec_height, ratio))        # truncate toward zero
        tensor = min(tensor, self.max_width)
        content = min(tensor, int(math.ceil(self.rec_height * ratio)))
        return tensor, content

    def recognize(self, bgr, quad, trace=None):
        """(text, confidence) of one line"""
        crop = rotate_crop(bgr, quad)
        if crop.shape[0] >= crop.shape[1] * self.tall_ratio:                          # a tall line: turned 90 degrees counter-clockwise
            crop = np.rot90(crop)
        h, w = crop.shape[:2]
        tensor, content = self.tensor_width(h, w)
        resized = cv2.resize(crop, (content, self.rec_height), interpolation=cv2.INTER_LINEAR)
        normal = (resized.astype(np.float32) * self.rec_scale - self.rec_mean) / self.rec_std
        blob = np.full((self.rec_height, tensor, 3), self.padding, np.float32)
        blob[:, :content] = normal
        self.rec.setInput(blob.transpose(2, 0, 1)[None])
        probs = self.rec.forward()[0]                                              # steps x classes
        text, confidence, indices = ctc_decode(probs, self.dictionary)
        if trace is not None:
            trace.setdefault('lines', []).append({'crop': [h, w], 'tensor': tensor, 'content': content, 'indices': indices, 'text': text})
        return text, confidence

    def read_lines(self, rgb, trace=None):
        """The text lines of an RGB picture, as server.mjs answers: [{'text', 'confidence', 'box': [[x, y] x 4]}]"""
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        lines = []
        for quad in self.detect(bgr, trace):
            text, confidence = self.recognize(bgr, quad, trace)
            if text and confidence >= self.score_threshold:
                lines.append({'text': text, 'confidence': float(confidence), 'box': [[float(x), float(y)] for x, y in quad]})
        return lines


# ---------------------------------------------------------------------- the helpers (PaddleOCR's, as plain functions)

def mini_box(contour):
    """db_postprocess.get_mini_boxes: the min-area rectangle's four corners, ordered top-left, top-right, bottom-right, bottom-left - and its shorter side"""
    rect = cv2.minAreaRect(contour)
    points = sorted(list(cv2.boxPoints(rect)), key=lambda p: p[0])
    i1, i4 = (0, 1) if points[1][1] > points[0][1] else (1, 0)
    i2, i3 = (2, 3) if points[3][1] > points[2][1] else (3, 2)
    return np.array([points[i1], points[i2], points[i3], points[i4]], np.float32), min(rect[1])


def box_score_fast(prob, box):
    """db_postprocess.box_score_fast: the mean of the probability map inside the quad (on its bounding rectangle, a filled-polygon mask)"""
    h, w = prob.shape
    xmin = int(np.clip(np.floor(box[:, 0].min()), 0, w - 1))
    xmax = int(np.clip(np.ceil(box[:, 0].max()), 0, w - 1))
    ymin = int(np.clip(np.floor(box[:, 1].min()), 0, h - 1))
    ymax = int(np.clip(np.ceil(box[:, 1].max()), 0, h - 1))
    mask = np.zeros((ymax - ymin + 1, xmax - xmin + 1), np.uint8)
    shifted = box.copy()
    shifted[:, 0] -= xmin
    shifted[:, 1] -= ymin
    cv2.fillPoly(mask, shifted.reshape(1, -1, 2).astype(np.int32), 1)
    return cv2.mean(prob[ymin:ymax + 1, xmin:xmax + 1], mask)[0]


def grow(box, ratio):
    """
    db_postprocess.unclip: the quad offset outward by distance = area * ratio / perimeter (shapely's area and length of the float quad) with ROUND
    joins, as PaddleOCR does it with pyclipper - here Clipper 6.4's ClipperOffset written out for one closed polygon (clipper_offset_round), so that
    neither this reference nor the phone needs the library. Measured (compare_with_light_ocr.py on 8 Pixel captures): growing the min-area rectangle by
    `distance` instead - mathematically the same rectangle - matched light-ocr's line texts on 82 of 95 lines, Clipper's integer offset on 93: its
    points are whole numbers and its round corners an approximation, and the rectangle fitted around them (mini_box, next) differs by about a pixel.
    """
    area = abs(cv2.contourArea(box))
    perimeter = cv2.arcLength(box.reshape(-1, 1, 2), True)
    distance = area * ratio / perimeter if perimeter > 0 else 0.0
    return np.array(clipper_offset_round(box.tolist(), distance), np.float32)


ARC_TOLERANCE = 0.25        # ClipperOffset's default (def_arc_tolerance)


def clipper_round(value):
    """Clipper's Round: half away from zero"""
    return int(value - 0.5) if value < 0 else int(value + 0.5)


def clipper_offset_round(points, delta):
    """
    Clipper 6.4.2's ClipperOffset (MiterLimit 2, ArcTolerance 0.25) on ONE closed polygon with JT_ROUND joins and delta > 0, as pyclipper calls it
    (AddPath(points, JT_ROUND, ET_CLOSEDPOLYGON); Execute(delta)). The input points become integers by truncation (pyclipper's conversion to IntPoint); the
    output points are Clipper's rounded ones. The final union Execute does is left out: on the convex quads of a text line it removes nothing that changes
    the min-area rectangle of the points, which is all that is used. Checked equal to pyclipper on random quads: test_clipper_offset in this folder's README.
    """
    import math
    # AddPath: integers (truncated toward zero), consecutive duplicates removed, the closing duplicate too
    path = []
    for x, y in points:
        pt = (int(x), int(y))
        if not path or pt != path[-1]:
            path.append(pt)
    if len(path) > 1 and path[0] == path[-1]:
        path.pop()
    if len(path) < 3 or delta <= 0:
        return path
    # FixOrientations: a closed polygon is offset in Clipper's positive orientation (Area >= 0); else it is reversed
    area = 0.0
    for i in range(len(path)):
        x1, y1 = path[i - 1]
        x2, y2 = path[i]
        area += (x1 + x2) * (y1 - y2)
    if area * -0.5 < 0:                         # Clipper's Area(): -0.5 * sum((x1 + x2) * (y1 - y2))... negative: Orientation false
        path.reverse()
    # DoOffset's constants
    y = ARC_TOLERANCE
    steps = math.pi / math.acos(1 - y / abs(delta))
    if steps > abs(delta) * math.pi:
        steps = abs(delta) * math.pi
    sin, cos = math.sin(2 * math.pi / steps), math.cos(2 * math.pi / steps)
    steps_per_rad = steps / (2 * math.pi)
    # the normals of the edges (GetUnitNormal)
    n = len(path)
    normals = []
    for j in range(n):
        x1, y1 = path[j]
        x2, y2 = path[(j + 1) % n]
        dx, dy = float(x2 - x1), float(y2 - y1)
        if dx == 0 and dy == 0:
            normals.append((0.0, 0.0))
            continue
        f = 1.0 / math.sqrt(dx * dx + dy * dy)
        normals.append((dy * f, -dx * f))
    out = []
    k = n - 1
    for j in range(n):
        sx, sy = path[j]
        nk, nj = normals[k], normals[j]
        sin_a = nk[0] * nj[1] - nj[0] * nk[1]
        if abs(sin_a * delta) < 1.0:
            cos_a = nk[0] * nj[0] + nj[1] * nk[1]
            if cos_a > 0:                         # nearly no angle: one point
                out.append((clipper_round(sx + nk[0] * delta), clipper_round(sy + nk[1] * delta)))
                k = j
                continue
        elif sin_a > 1.0:
            sin_a = 1.0
        elif sin_a < -1.0:
            sin_a = -1.0
        if sin_a * delta < 0:                     # a concave corner
            out.append((clipper_round(sx + nk[0] * delta), clipper_round(sy + nk[1] * delta)))
            out.append((sx, sy))
            out.append((clipper_round(sx + nj[0] * delta), clipper_round(sy + nj[1] * delta)))
        else:                                     # DoRound
            a = math.atan2(sin_a, nk[0] * nj[0] + nk[1] * nj[1])
            count = max(clipper_round(steps_per_rad * abs(a)), 1)
            x, yy = nk
            for _ in range(count):
                out.append((clipper_round(sx + x * delta), clipper_round(sy + yy * delta)))
                x2 = x
                x = x * cos - sin * yy
                yy = x2 * sin + yy * cos
            out.append((clipper_round(sx + nj[0] * delta), clipper_round(sy + nj[1] * delta)))
        k = j
    return out


def order_clockwise(points):
    """predict_det.order_points_clockwise: top-left, top-right, bottom-right, bottom-left"""
    by_x = points[np.argsort(points[:, 0], kind='stable')]
    left, right = by_x[:2], by_x[2:]
    left = left[np.argsort(left[:, 1], kind='stable')]
    right = right[np.argsort(right[:, 1], kind='stable')]
    return np.array([left[0], right[0], right[1], left[1]], np.float32)


def reading_order(quads, band):
    """predict_system.sorted_boxes: by the top-left corner's y, then x; neighbours whose y differ by less than `band` by x"""
    ordered = sorted(quads, key=lambda q: (q[0][1], q[0][0]))
    for i in range(len(ordered) - 1):
        for j in range(i, -1, -1):
            if abs(ordered[j + 1][0][1] - ordered[j][0][1]) < band and ordered[j + 1][0][0] < ordered[j][0][0]:
                ordered[j], ordered[j + 1] = ordered[j + 1], ordered[j]
            else:
                break
    return ordered


def rotate_crop(bgr, quad):
    """predict_system.get_rotate_crop_image: the quad warped upright - width the longer of the top / bottom edges, height of the left / right; cubic, replicate"""
    quad = np.asarray(quad, np.float32)
    width = int(max(np.linalg.norm(quad[0] - quad[1]), np.linalg.norm(quad[2] - quad[3])))
    height = int(max(np.linalg.norm(quad[0] - quad[3]), np.linalg.norm(quad[1] - quad[2])))
    width, height = max(width, 1), max(height, 1)
    target = np.array([[0, 0], [width, 0], [width, height], [0, height]], np.float32)
    matrix = cv2.getPerspectiveTransform(quad, target)
    return cv2.warpPerspective(bgr, matrix, (width, height), borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_CUBIC)


def ctc_decode(probs, dictionary):
    """Greedy CTC: per step the argmax; blank (0) dropped; a repeat of the previous step's index collapsed. Character = dictionary[index - 1]. (text, confidence, indices)"""
    indices = probs.argmax(axis=1)
    best = probs.max(axis=1)
    chars, kept = [], []
    previous = -1
    for step, index in enumerate(indices):
        if index != 0 and index != previous:
            chars.append(dictionary[index - 1])
            kept.append(float(best[step]))
        previous = index
    return ''.join(chars), (float(np.mean(kept)) if kept else 0.0), [int(i) for i in indices]


if __name__ == '__main__':
    picture = cv2.cvtColor(cv2.imread(sys.argv[1]), cv2.COLOR_BGR2RGB)
    trace = {} if '--trace' in sys.argv else None
    for line in Reader().read_lines(picture, trace):
        print(f"{line['confidence']:.3f}  {line['text']!r}  {[[round(x), round(y)] for x, y in line['box']]}")
    if trace is not None:
        print(json.dumps({k: v for k, v in trace.items() if k != 'lines'})[:2000])
