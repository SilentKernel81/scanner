# object_detector.py
# Card detection: the card's outline (contours) and the perspective-corrected crop

import logging

import cv2
import numpy as np

logger = logging.getLogger('scanner')

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


def side_area(corners):
    """Area of an outline's rectangle (width x height of its sides)"""
    return float(np.linalg.norm(corners[1] - corners[0]) * np.linalg.norm(corners[3] - corners[0]))


def side_middles(corners):
    """Middle of each side of an outline (top, right, bottom, left)"""
    return (corners + np.roll(corners, -1, axis=0)) / 2


def _is_rectangular(corners, side_tolerance=0.04, angle_tolerance=3):
    """
    Opposite sides about equally long and corners about square (camera looking down). Measured
    on recorded pile frames: outlines following the card right were within 2% / 1 degree, the
    ones that had latched onto another edge 7-15% / 3-7 degrees off (and chopped the photo).
    """
    tl, tr, br, bl = corners
    for first, second in ((tr - tl, br - bl), (bl - tl, br - tr)):
        a, b = np.linalg.norm(first), np.linalg.norm(second)
        if abs(a - b) > side_tolerance * max(a, b):
            return False
    for corner, before, after in ((tl, bl, tr), (tr, tl, br), (br, tr, bl), (bl, br, tl)):
        u, v = before - corner, after - corner
        cos = np.dot(u, v) / (np.linalg.norm(u) * np.linalg.norm(v) + 1e-9)
        if abs(np.degrees(np.arccos(np.clip(cos, -1, 1))) - 90) > angle_tolerance:
            return False
    return True


def _perimeter_coverage(edges, corners, samples=240, reach=3):
    """Fraction of points along a rectangle's sides that lie on (or within `reach` of) an edge"""
    near = cv2.dilate(edges, np.ones((2 * reach + 1, 2 * reach + 1), np.uint8))
    hits = total = 0
    for start, end in zip(corners, np.roll(corners, -1, axis=0)):
        for t in np.linspace(0, 1, samples // 4, endpoint=False):
            x, y = (start + (end - start) * t).astype(int)
            if 0 <= y < near.shape[0] and 0 <= x < near.shape[1]:
                total += 1
                hits += near[y, x] > 0
    return hits / max(total, 1)


def _outline_from_edge_groups(edges, gray, min_area, allow_landscape, ratio_tolerance):
    """
    Fallback for an outline with a gap: where a card's edge has the brightness of the background
    (e.g. a borderless foil's silver frame against the white box) the outline breaks and no
    closed contour exists. Edge pieces lying close together are grouped, and a group whose hull
    is a card-shaped rectangle with edges along most of its sides is taken as the card.

    Returns:
        tuple: (area, corners, fill) or None
    """
    count, labels, stats, _ = cv2.connectedComponentsWithStats(cv2.dilate(edges, np.ones((15, 15), np.uint8)))
    best = None
    for group in range(1, count):
        if stats[group, cv2.CC_STAT_WIDTH] * stats[group, cv2.CC_STAT_HEIGHT] < min_area:
            continue
        ys, xs = np.nonzero((labels == group) & (edges > 0))
        hull = cv2.convexHull(np.column_stack([xs, ys]).astype(np.int32))
        corners = _order_corners(cv2.boxPoints(cv2.minAreaRect(hull)))
        side_w = np.linalg.norm(corners[1] - corners[0])
        side_h = np.linalg.norm(corners[3] - corners[0])
        if min(side_w, side_h) == 0 or (side_w > side_h and not allow_landscape):
            continue
        area = side_w * side_h
        ratio = max(side_w, side_h) / min(side_w, side_h)
        fill = cv2.contourArea(hull) / area
        if abs(ratio - CARD_ASPECT_RATIO) / CARD_ASPECT_RATIO > ratio_tolerance or fill < 0.9:
            continue
        if (best is not None and area <= best[0]) or _perimeter_coverage(edges, corners) < 0.8:
            continue
        if _is_inner_frame(gray, corners):
            continue
        best = (area, corners, fill)
    return best


def _border_brightness(gray, corners, band=4):
    """Mean brightness of the bands just outside and just inside an outline: (outside, inside)"""
    polygon = np.zeros(gray.shape, np.uint8)
    cv2.fillPoly(polygon, [corners.astype(np.int32)], 255)
    kernel = np.ones((2 * band + 1, 2 * band + 1), np.uint8)
    outside = cv2.dilate(polygon, kernel) & ~polygon
    inside = polygon & ~cv2.erode(polygon, kernel)
    if not outside.any() or not inside.any():
        return None
    return cv2.mean(gray, outside)[0], cv2.mean(gray, inside)[0]


def _card_inside_box(contours, edges, gray, box, min_area, allow_landscape, ratio_tolerance):
    """
    The card lying inside an outline that is the scanning box itself. With the whole box in
    view, the box floor is a closed, card-shaped outline (a card box is made for cards), while
    a card pushed into its corner shares two sides with it and has no closed contour of its
    own - the largest outline was then the box, and the photo had the box floor around the
    card. The box is told from a card by its edge: about as bright inside as outside (white
    floor, white wall), where a card's dark border is far darker than the floor around it.

    Returns:
        tuple: (area, corners, fill) of the dark-bordered card inside, or None (the outline
        is a card, or holds none)
    """
    brightness = _border_brightness(gray, box[1])
    if brightness is None or brightness[1] < 0.75 * brightness[0]:
        return None  # a dark edge: this is a card (or a pile of them)
    box_polygon = box[1].astype(np.float32)
    box_long_side = max(np.linalg.norm(box[1][1] - box[1][0]), np.linalg.norm(box[1][3] - box[1][0]))
    best = None
    for contour in contours:
        # An open outline encloses little area: judged by its rectangle instead
        corners = _order_corners(cv2.boxPoints(cv2.minAreaRect(contour)))
        side_w = np.linalg.norm(corners[1] - corners[0])
        side_h = np.linalg.norm(corners[3] - corners[0])
        area = side_w * side_h
        if area < max(min_area, 0.5 * box[0]) or area >= box[0] or (side_w > side_h and not allow_landscape):
            continue
        if abs(max(side_w, side_h) / min(side_w, side_h) - CARD_ASPECT_RATIO) / CARD_ASPECT_RATIO > ratio_tolerance:
            continue
        if any(cv2.pointPolygonTest(box_polygon, (float(x), float(y)), True) < -4 for x, y in corners):
            continue
        # A clear sleeve's edge is such an outline too, 1-2% outside its card on every side:
        # left alone (taking the card there would make the outline flip between the two)
        if max(np.linalg.norm(side_middles(corners) - side_middles(box[1]), axis=1)) < 0.04 * box_long_side:
            continue
        if (best is not None and area <= best[0]) or _perimeter_coverage(edges, corners) < 0.8:
            continue
        brightness = _border_brightness(gray, corners)
        if brightness is None or brightness[1] > 0.5 * brightness[0]:
            continue  # not a dark border on a light floor (e.g. the frame inside a white-bordered card)
        best = (area, corners, _perimeter_coverage(edges, corners))
    return best


def _fit_side(points, start, end, band):
    """
    Line through the edge points along one side of a rectangle (within `band` of it), leaving
    out the corner zones - where other outlines touch (the box's corner crease, the pile)
    Returns (point, direction) or None
    """
    side = end - start
    length = np.linalg.norm(side)
    if length == 0:
        return None
    along = side / length
    normal = np.array([-along[1], along[0]])
    rel = points - start
    t, offset = rel @ along, rel @ normal
    keep = (np.abs(offset) <= band) & (t > 0.15 * length) & (t < 0.85 * length)
    if keep.sum() < 10:
        return None
    vx, vy, x0, y0 = cv2.fitLine(points[keep], cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
    return np.array([x0, y0]), np.array([vx, vy])


def _line_intersection(first, second):
    (p, r), (q, s) = first, second
    matrix = np.array([r, -s]).T
    if abs(np.linalg.det(matrix)) < 1e-6:
        return None
    t, _ = np.linalg.solve(matrix, q - p)
    return p + t * r


def _track_outline(raw_edges, edges, gray, previous, allow_landscape, ratio_tolerance, band=6):
    """
    Follow the card found in the previous frame: fit a line to the edges along each of its
    sides (within `band` px) and intersect them. On a pile the card's outline often merges
    with the edge of a card underneath or the box's corner crease, and then no closed contour
    exists; tracking also keeps the choice between nested outlines (top card / whole pile)
    from flipping between frames. The result must still be card-shaped, about the same size,
    with edges along >= 80% of its perimeter.

    Returns:
        tuple: (area, corners, fill) or None
    """
    band_mask = np.zeros(raw_edges.shape, np.uint8)
    cv2.polylines(band_mask, [previous.astype(np.int32)], True, 255, 2 * band + 1)
    ys, xs = np.nonzero(raw_edges & band_mask)
    if len(xs) < 50:
        return None
    points = np.column_stack([xs, ys]).astype(np.float32)
    lines = [_fit_side(points, previous[i], previous[(i + 1) % 4], band) for i in range(4)]
    if any(line is None for line in lines):
        return None
    corners = [_line_intersection(lines[i - 1], lines[i]) for i in range(4)]
    if any(corner is None for corner in corners):
        return None
    corners = _order_corners(np.array(corners))
    side_w = np.linalg.norm(corners[1] - corners[0])
    side_h = np.linalg.norm(corners[3] - corners[0])
    if min(side_w, side_h) == 0 or (side_w > side_h and not allow_landscape):
        return None
    if abs(max(side_w, side_h) / min(side_w, side_h) - CARD_ASPECT_RATIO) / CARD_ASPECT_RATIO > ratio_tolerance:
        return None
    area = side_w * side_h
    previous_area = np.linalg.norm(previous[1] - previous[0]) * np.linalg.norm(previous[3] - previous[0])
    if not 0.85 <= area / previous_area <= 1.15:
        return None
    if _perimeter_coverage(edges & band_mask, corners) < 0.8 or _is_inner_frame(gray, corners):
        return None
    return area, corners, 1.0


def find_card_outline(frame, allow_landscape=False, ratio_tolerance=0.08, work_size=640, previous=None):
    """
    Find a card by its outline: the largest 4-sided contour with a card's aspect ratio.

    Works well when the card border contrasts with the background (e.g. a
    black-bordered card on a light surface) and is unaffected by foil glare
    inside the card.

    Args:
        frame: RGB image
        allow_landscape: Accept cards lying sideways. Off by default because a
            card's (landscape) art box has nearly the same aspect ratio as a card.
        ratio_tolerance: Allowed relative deviation from the card aspect ratio. Settled cards
            measure 1.33-1.37 here (1.397 less the widened edges); at 18% the part of a
            borderless card below its name banner (1.18-1.25) passed, and the photo lost the name
        work_size: Longest side of the downscaled image used for detection
        previous: The card's corners in the previous frame (frame coordinates), if any - the
            card is then followed along its sides first (see _track_outline)

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
    raw_edges = cv2.Canny(gray, int(max(0, 0.5 * median)), int(min(255, 1.3 * median)))
    edges = cv2.dilate(raw_edges, np.ones((3, 3), np.uint8), iterations=2)

    # Follow the previous card - as a small refinement only: an outline more than 2% away was
    # fitted to another edge (e.g. the card's inner frame on a blurry image), and taking it
    # would make the result flip between two outlines. Then it is only a last resort. It must
    # be a rectangle too: 2% a frame adds up, and a side that crept onto another edge (the
    # card below, a frame inside the card) skewed the outline and chopped the photo. A skewed
    # one keeps the previous outline - going on to the other steps took another outline (the
    # frame inside a borderless card), and the change looked like a new card, again and again.
    tracked = None
    if previous is not None:
        previous = np.asarray(previous, np.float32) * scale
        tracked = _track_outline(raw_edges, edges, gray, previous, allow_landscape, ratio_tolerance)
        if tracked is not None:
            shift = np.linalg.norm(tracked[1] - previous, axis=1).max() / np.linalg.norm(previous[2] - previous[0])
            if shift <= 0.02:
                if _is_rectangular(tracked[1]):
                    return tracked[1] / scale, float(tracked[2])
                if _is_rectangular(previous):
                    return previous / scale, 1.0

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

    if best is not None:
        best = _card_inside_box(contours, edges, gray, (side_area(best[1]), best[1], best[2]), min_area,
                                allow_landscape, ratio_tolerance) or best

    # Outlines assembled from edge pieces or fitted lines can come out skewed (a holo streak
    # taken for the top edge); seen from above a card is a rectangle
    if best is None:
        best = _outline_from_edge_groups(edges, gray, min_area, allow_landscape, ratio_tolerance)
        if best is not None and not _is_rectangular(best[1]):
            best = None
    if best is None and tracked is not None and _is_rectangular(tracked[1]):
        best = tracked
    if best is None:
        return None, 0
    return best[1] / scale, float(best[2])


def warp_card(frame, corners, out_h=None):
    """
    Perspective-correct the card inside `corners` into a flat, portrait image.
    out_h: output height (default: the card's own height in the frame)
    """
    tl, tr, br, bl = corners
    if np.linalg.norm(tr - tl) > np.linalg.norm(bl - tl):
        # Card lying sideways - rotate so the output is portrait
        tl, tr, br, bl = bl, tl, tr, br
    if out_h is None:
        out_h = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
    out_w = int(out_h / CARD_ASPECT_RATIO)
    src = np.array([tl, tr, br, bl], dtype=np.float32)
    dst = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]], dtype=np.float32)
    return cv2.warpPerspective(frame, cv2.getPerspectiveTransform(src, dst), (out_w, out_h))


class ObjectDetector:
    """Finds a card in a frame by its outline"""
    def __init__(self, allow_landscape=False):
        """
        Args:
            allow_landscape (bool): Accept sideways cards
        """
        self.allow_landscape = allow_landscape

    def detect(self, frame, previous=None):
        """
        Detect a card in an RGB frame. previous: the card's corners in the previous frame
        (it is followed first)

        Returns:
            tuple: (bounding_box, label, score, corners); (None, "", 0, None) if nothing was found
        """
        if frame is None:
            return None, "", 0, None

        corners, score = find_card_outline(frame, allow_landscape=self.allow_landscape, previous=previous)
        if corners is None:
            return None, "", 0, None
        height, width = frame.shape[:2]
        x1, y1 = np.floor(corners.min(axis=0)).astype(int)
        x2, y2 = np.ceil(corners.max(axis=0)).astype(int)
        bounding_box = (max(0, x1), max(0, y1), min(width, x2), min(height, y2))
        return bounding_box, "Card", score, corners
