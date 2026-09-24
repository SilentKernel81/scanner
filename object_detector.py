# object_detector.py
# Card detection: outline (contour) detection first, YOLO as a fallback

import cv2
import numpy as np

# Magic card aspect ratio: 88mm / 63mm
CARD_ASPECT_RATIO = 88.0 / 63.0


def _order_corners(pts):
    """Order 4 points as top-left, top-right, bottom-right, bottom-left"""
    pts = pts.reshape(4, 2).astype(np.float32)
    sums = pts.sum(axis=1)
    diffs = np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(sums)], pts[np.argmin(diffs)], pts[np.argmax(sums)], pts[np.argmax(diffs)]])


def _is_inner_frame(gray, corners, band=4):
    """
    True if the outline looks like the frame *inside* a card's dark border rather than
    the card's outer edge: the band just outside it is darker than the band just inside.
    (The inner frame has nearly the card's aspect ratio, so it can match when the card's
    outer edge is cut off by the image border.) Assumes a background lighter than the
    card border, like a white scanning box.
    """
    polygon = np.zeros(gray.shape, np.uint8)
    cv2.fillPoly(polygon, [corners.astype(np.int32)], 255)
    kernel = np.ones((2 * band + 1, 2 * band + 1), np.uint8)
    outside = cv2.dilate(polygon, kernel) & ~polygon
    inside = polygon & ~cv2.erode(polygon, kernel)
    if not outside.any() or not inside.any():
        return False
    return cv2.mean(gray, outside)[0] < cv2.mean(gray, inside)[0]


def find_card_outline(frame, allow_landscape=False, ratio_tolerance=0.18, work_size=640):
    """
    Find a card by its outline: the largest 4-sided contour with a card's aspect ratio.

    Works well when the card border contrasts with the background (e.g. a
    black-bordered card on a light surface) and is unaffected by foil glare
    inside the card.

    Args:
        frame: RGB image
        allow_landscape: Accept cards lying sideways. Off by default because a
            card's (landscape) art box has nearly the same aspect ratio as a card.
        ratio_tolerance: Allowed relative deviation from the card aspect ratio
        work_size: Longest side of the downscaled image used for detection

    Returns:
        tuple: (corners, score) - corners is a 4x2 float array (tl, tr, br, bl)
        in frame coordinates and score is how rectangular the outline is (0-1);
        (None, 0) if no card outline was found
    """
    height, width = frame.shape[:2]
    scale = work_size / max(height, width)
    small = cv2.resize(frame, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)

    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_RGB2GRAY), (5, 5), 0)
    median = np.median(gray)
    edges = cv2.Canny(gray, int(max(0, 0.5 * median)), int(min(255, 1.3 * median)))
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)

    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    min_area = 0.02 * small.shape[0] * small.shape[1]  # card must cover at least 2% of the frame

    best = None
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area:
            continue
        rect = cv2.minAreaRect(contour)
        corners = _order_corners(cv2.boxPoints(rect))
        side_w = np.linalg.norm(corners[1] - corners[0])
        side_h = np.linalg.norm(corners[3] - corners[0])
        if min(side_w, side_h) == 0:
            continue
        if side_w > side_h and not allow_landscape:
            continue
        ratio = max(side_w, side_h) / min(side_w, side_h)
        fill = area / (side_w * side_h)  # 1.0 = perfectly rectangular
        if abs(ratio - CARD_ASPECT_RATIO) / CARD_ASPECT_RATIO > ratio_tolerance or fill < 0.85:
            continue
        if best is not None and area <= best[0]:
            continue
        if _is_inner_frame(gray, corners):
            continue
        best = (area, corners, fill)

    if best is None:
        return None, 0
    return best[1] / scale, float(best[2])


def warp_card(frame, corners):
    """Perspective-correct the card inside `corners` into a flat, portrait image"""
    tl, tr, br, bl = corners
    if np.linalg.norm(tr - tl) > np.linalg.norm(bl - tl):
        # Card lying sideways - rotate so the output is portrait
        tl, tr, br, bl = bl, tl, tr, br
    out_h = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
    out_w = int(out_h / CARD_ASPECT_RATIO)
    src = np.array([tl, tr, br, bl], dtype=np.float32)
    dst = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]], dtype=np.float32)
    return cv2.warpPerspective(frame, cv2.getPerspectiveTransform(src, dst), (out_w, out_h))


class ObjectDetector:
    """
    Finds a card in a frame. Methods:
      'contour' - outline detection only
      'yolo'    - YOLO only (pre-trained COCO model; it has no card class)
      'auto'    - outline detection, falling back to YOLO
    """
    def __init__(self, model_path='yolov8n.pt', method='auto', allow_landscape=False):
        """
        Initializes the ObjectDetector.

        Args:
            model_path (str): The path to the YOLOv8 model file.
            method (str): 'auto', 'contour' or 'yolo'
            allow_landscape (bool): Accept sideways cards in outline detection
        """
        self.method = method
        self.allow_landscape = allow_landscape
        self.model = None
        if method in ('auto', 'yolo'):
            from ultralytics import YOLO
            self.model = YOLO(model_path)
            self.names = self.model.names

    def detect(self, frame, conf_threshold=0.1):
        """
        Detect a card in an RGB frame.

        Returns:
            tuple: (bounding_box, label, confidence, corners) - corners is None
            for YOLO detections; (None, "", 0, None) if nothing was found
        """
        if frame is None:
            return None, "", 0, None

        if self.method in ('auto', 'contour'):
            corners, score = find_card_outline(frame, allow_landscape=self.allow_landscape)
            if corners is not None:
                height, width = frame.shape[:2]
                x1, y1 = np.floor(corners.min(axis=0)).astype(int)
                x2, y2 = np.ceil(corners.max(axis=0)).astype(int)
                bounding_box = (max(0, x1), max(0, y1), min(width, x2), min(height, y2))
                return bounding_box, "Card", score, corners

        if self.model is not None:
            bounding_box, label, confidence = self.predict_frame(frame, conf_threshold=conf_threshold)
            if bounding_box:
                return bounding_box, f"{label} (YOLO)", confidence, None

        return None, "", 0, None

    def predict_frame(self, frame, conf_threshold=0.1, verbose=False, target_size=640):
        """
        Performs object detection on a raw image frame.

        Args:
            frame (np.ndarray): The image frame (from OpenCV).
            conf_threshold (float): Minimum confidence threshold (default 0.1 for permissive detection).
            verbose (bool): If True, print all detections for debugging.
            target_size (int): Target size for inference (default 640, YOLOv8n native size).

        Returns:
            tuple: A tuple containing (bounding_box, card_name, confidence), or (None, "", 0) if no card is detected.
        """
        if frame is None:
            return None, "", 0

        # Store original dimensions for coordinate scaling
        original_height, original_width = frame.shape[:2]

        # Downscale for inference (YOLO is trained on 640x640)
        # This significantly improves performance without sacrificing accuracy
        inference_frame = frame
        scale_factor = 1.0

        if original_width > target_size or original_height > target_size:
            scale = target_size / max(original_width, original_height)
            new_width = int(original_width * scale)
            new_height = int(original_height * scale)

            inference_frame = cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_AREA)
            scale_factor = scale

        # Use low confidence threshold to catch cards that don't match COCO classes well
        results = self.model.predict(inference_frame, conf=conf_threshold, verbose=False)

        if not results or len(results[0].boxes) == 0:
            return None, "", 0

        # Debug: Print all detections if verbose
        if verbose and len(results[0].boxes) > 0:
            print(f"\nDetected {len(results[0].boxes)} objects:")
            for i, box in enumerate(results[0].boxes):
                cls = int(box.cls)
                conf = float(box.conf)
                class_name = self.names[cls]
                print(f"  {i+1}. {class_name} (confidence: {conf:.3f})")

        # Filter out person detections (class 0) - cards are not people!
        # Person class often detects faces which interferes with card detection
        EXCLUDED_CLASSES = {0}  # 0 = person in COCO dataset

        filtered_boxes = []
        for box in results[0].boxes:
            cls = int(box.cls)
            if cls not in EXCLUDED_CLASSES:
                filtered_boxes.append(box)

        # If no boxes left after filtering, return None
        if len(filtered_boxes) == 0:
            return None, "", 0

        # Helper function to calculate aspect ratio
        def get_aspect_ratio(box):
            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
            width = x2 - x1
            height = y2 - y1
            if width == 0 or height == 0:
                return 0
            return max(width, height) / min(width, height)

        # Helper function to calculate area
        def get_box_area(box):
            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
            return (x2 - x1) * (y2 - y1)

        # Debug: Print all detections with aspect ratios (only if verbose=True)
        # Set to True in predict_frame() call to enable debugging
        if verbose:
            print(f"\n=== YOLO Detections ({len(filtered_boxes)} objects) ===")
            for i, box in enumerate(filtered_boxes):
                x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                width = int(x2 - x1)
                height = int(y2 - y1)
                ratio = get_aspect_ratio(box)
                conf = float(box.conf)
                cls = int(box.cls)
                class_name = self.names[cls]
                match_diff = abs(ratio - CARD_ASPECT_RATIO)
                print(f"  [{i+1}] {class_name} | {width}x{height}px | AR={ratio:.3f} (diff={match_diff:.3f}) | conf={conf:.3f}")
            print("  Target: AR=1.397 (Magic card: 88mm x 63mm)")
            print("=" * 50)

        # If multiple detections, prefer the one with aspect ratio closest to Magic card
        # This helps when both a white box and card inside are detected
        def aspect_ratio_score(box):
            ratio = get_aspect_ratio(box)
            # Return negative absolute difference (so min() finds best match)
            return -abs(ratio - CARD_ASPECT_RATIO)

        # Sort by aspect ratio match (best match first)
        filtered_boxes.sort(key=aspect_ratio_score, reverse=True)
        top_prediction = filtered_boxes[0]

        # Bounding box coordinates (from inference frame)
        x1, y1, x2, y2 = top_prediction.xyxy[0].cpu().numpy().astype(int)

        # Scale bounding box back to original frame coordinates
        if scale_factor != 1.0:
            x1 = int(x1 / scale_factor)
            y1 = int(y1 / scale_factor)
            x2 = int(x2 / scale_factor)
            y2 = int(y2 / scale_factor)

        bounding_box = (x1, y1, x2, y2)

        # For card detection, we don't care about the YOLO class - just call it "Card"
        card_name = "Card"

        # Confidence score
        confidence = float(top_prediction.conf)

        return bounding_box, card_name, confidence
