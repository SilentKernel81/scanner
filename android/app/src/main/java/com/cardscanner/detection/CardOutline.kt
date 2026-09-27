package com.cardscanner.detection

import org.opencv.core.Core
import org.opencv.core.CvType
import org.opencv.core.Mat
import org.opencv.core.MatOfInt
import org.opencv.core.MatOfPoint
import org.opencv.core.MatOfPoint2f
import org.opencv.core.Point
import org.opencv.core.Scalar
import org.opencv.core.Size
import org.opencv.imgproc.Imgproc
import kotlin.math.abs
import kotlin.math.acos
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

/**
 * Card detection by outline - a port of object_detector.py (find_card_outline, warp_card and
 * helpers). Thresholds are the ones measured for the Python scanner; keep both in step.
 */

/** Magic card aspect ratio: 88mm / 63mm */
const val CARD_ASPECT_RATIO = 88.0 / 63.0

data class Pt(val x: Double, val y: Double) {
    operator fun plus(o: Pt) = Pt(x + o.x, y + o.y)
    operator fun minus(o: Pt) = Pt(x - o.x, y - o.y)
    operator fun times(f: Double) = Pt(x * f, y * f)
    operator fun div(f: Double) = Pt(x / f, y / f)
    fun dot(o: Pt) = x * o.x + y * o.y
    fun norm() = sqrt(x * x + y * y)
}

/** Four corners, ordered top-left, top-right, bottom-right, bottom-left */
typealias Corners = List<Pt>

fun Corners.scaled(f: Double): Corners = map { it * f }

fun Corners.scaled(fx: Double, fy: Double): Corners = map { Pt(it.x * fx, it.y * fy) }

private fun Corners.toPoints() = map { Point(it.x, it.y) }.toTypedArray()

private data class Candidate(val area: Double, val corners: Corners, val fill: Double)

/** Order 4 points as top-left, top-right, bottom-right, bottom-left */
private fun orderCorners(pts: List<Pt>): Corners {
    val sums = pts.map { it.x + it.y }
    val diffs = pts.map { it.y - it.x }
    return listOf(
        pts[sums.indexOf(sums.min())], pts[diffs.indexOf(diffs.min())],
        pts[sums.indexOf(sums.max())], pts[diffs.indexOf(diffs.max())]
    )
}

private fun boxCorners(rect: org.opencv.core.RotatedRect): Corners {
    val box = Mat()
    Imgproc.boxPoints(rect, box)
    val pts = (0 until 4).map { Pt(box.get(it, 0)[0], box.get(it, 1)[0]) }
    box.release()
    return orderCorners(pts)
}

private fun kernel(size: Int) = Mat.ones(size, size, CvType.CV_8U)

/**
 * True if the outline looks like the frame *inside* a card's dark border rather than the card's
 * outer edge: the band just outside it is darker than the band just inside. Assumes a background
 * lighter than the card border, like a white scanning box.
 */
private fun isInnerFrame(gray: Mat, corners: Corners, band: Int = 4): Boolean {
    val polygon = Mat.zeros(gray.size(), CvType.CV_8U)
    Imgproc.fillPoly(polygon, listOf(MatOfPoint(*corners.map { Point(it.x.toInt().toDouble(), it.y.toInt().toDouble()) }.toTypedArray())), Scalar(255.0))
    val k = kernel(2 * band + 1)
    val dilated = Mat(); val eroded = Mat(); val notPolygon = Mat(); val notEroded = Mat()
    val outside = Mat(); val inside = Mat()
    Imgproc.dilate(polygon, dilated, k)
    Imgproc.erode(polygon, eroded, k)
    Core.bitwise_not(polygon, notPolygon)
    Core.bitwise_not(eroded, notEroded)
    Core.bitwise_and(dilated, notPolygon, outside)
    Core.bitwise_and(polygon, notEroded, inside)
    val result = if (Core.countNonZero(outside) == 0 || Core.countNonZero(inside) == 0) false
    else Core.mean(gray, outside).`val`[0] < Core.mean(gray, inside).`val`[0]
    listOf(polygon, k, dilated, eroded, notPolygon, notEroded, outside, inside).forEach { it.release() }
    return result
}

/** Opposite sides about equally long and corners about square (camera looking down) */
private fun isRectangular(c: Corners, sideTolerance: Double = 0.08, angleTolerance: Double = 8.0): Boolean {
    val (tl, tr, br, bl) = c
    for ((first, second) in listOf((tr - tl) to (br - bl), (bl - tl) to (br - tr))) {
        val a = first.norm(); val b = second.norm()
        if (abs(a - b) > sideTolerance * max(a, b)) return false
    }
    for ((corner, before, after) in listOf(Triple(tl, bl, tr), Triple(tr, tl, br), Triple(br, tr, bl), Triple(bl, br, tl))) {
        val u = before - corner; val v = after - corner
        val cos = u.dot(v) / (u.norm() * v.norm() + 1e-9)
        if (abs(Math.toDegrees(acos(cos.coerceIn(-1.0, 1.0))) - 90) > angleTolerance) return false
    }
    return true
}

private fun bytesOf(mat: Mat): ByteArray = ByteArray((mat.total() * mat.channels()).toInt()).also { mat.get(0, 0, it) }

/** Fraction of points along a rectangle's sides that lie on (or within `reach` of) an edge */
private fun perimeterCoverage(edges: Mat, corners: Corners, samples: Int = 240, reach: Int = 3): Double {
    val near = Mat()
    val k = kernel(2 * reach + 1)
    Imgproc.dilate(edges, near, k)
    val width = near.cols(); val height = near.rows()
    val pixels = bytesOf(near)
    near.release(); k.release()
    var hits = 0; var total = 0
    val perSide = samples / 4
    for (i in 0 until 4) {
        val start = corners[i]; val end = corners[(i + 1) % 4]
        for (s in 0 until perSide) {
            val p = start + (end - start) * (s.toDouble() / perSide)
            val x = p.x.toInt(); val y = p.y.toInt()
            if (y in 0 until height && x in 0 until width) {
                total++
                if (pixels[y * width + x].toInt() != 0) hits++
            }
        }
    }
    return hits.toDouble() / max(total, 1)
}

/**
 * Fallback for an outline with a gap (a borderless foil's silver frame against the white box):
 * edge pieces lying close together are grouped, and a group whose hull is a card-shaped rectangle
 * with edges along most of its sides is taken as the card.
 */
private fun outlineFromEdgeGroups(edges: Mat, gray: Mat, minArea: Double, allowLandscape: Boolean, ratioTolerance: Double): Candidate? {
    val grouped = Mat(); val labels = Mat(); val stats = Mat(); val centroids = Mat()
    val k = kernel(15)
    Imgproc.dilate(edges, grouped, k)
    val count = Imgproc.connectedComponentsWithStats(grouped, labels, stats, centroids)
    val width = edges.cols()
    val labelValues = IntArray(labels.total().toInt()).also { labels.get(0, 0, it) }
    val edgeValues = bytesOf(edges)
    val groups = (1 until count).filter {
        stats.get(it, Imgproc.CC_STAT_WIDTH)[0] * stats.get(it, Imgproc.CC_STAT_HEIGHT)[0] >= minArea
    }.toSet()
    listOf(grouped, labels, stats, centroids, k).forEach { it.release() }
    if (groups.isEmpty()) return null

    val points = HashMap<Int, MutableList<Point>>()
    for (i in labelValues.indices) {
        val group = labelValues[i]
        if (group in groups && edgeValues[i].toInt() != 0) {
            points.getOrPut(group) { ArrayList() }.add(Point((i % width).toDouble(), (i / width).toDouble()))
        }
    }

    var best: Candidate? = null
    for ((_, groupPoints) in points) {
        val all = MatOfPoint(*groupPoints.toTypedArray())
        val hullIndices = MatOfInt()
        Imgproc.convexHull(all, hullIndices)
        val hull = hullIndices.toArray().map { groupPoints[it] }
        all.release(); hullIndices.release()
        val corners = boxCorners(Imgproc.minAreaRect(MatOfPoint2f(*hull.toTypedArray())))
        val sideW = (corners[1] - corners[0]).norm()
        val sideH = (corners[3] - corners[0]).norm()
        if (min(sideW, sideH) == 0.0 || (sideW > sideH && !allowLandscape)) continue
        val area = sideW * sideH
        val ratio = max(sideW, sideH) / min(sideW, sideH)
        val fill = Imgproc.contourArea(MatOfPoint2f(*hull.toTypedArray())) / area
        if (abs(ratio - CARD_ASPECT_RATIO) / CARD_ASPECT_RATIO > ratioTolerance || fill < 0.9) continue
        if ((best != null && area <= best.area) || perimeterCoverage(edges, corners) < 0.8) continue
        if (isInnerFrame(gray, corners)) continue
        best = Candidate(area, corners, fill)
    }
    return best
}

private data class Line(val point: Pt, val direction: Pt)

/**
 * Line through the edge points along one side of a rectangle (within `band` of it), leaving out
 * the corner zones - where other outlines touch (the box's corner crease, the pile)
 */
private fun fitSide(points: List<Pt>, start: Pt, end: Pt, band: Int): Line? {
    val side = end - start
    val length = side.norm()
    if (length == 0.0) return null
    val along = side / length
    val normal = Pt(-along.y, along.x)
    val kept = points.filter {
        val rel = it - start
        val t = rel.dot(along)
        abs(rel.dot(normal)) <= band && t > 0.15 * length && t < 0.85 * length
    }
    if (kept.size < 10) return null
    val mat = MatOfPoint2f(*kept.map { Point(it.x, it.y) }.toTypedArray())
    val line = Mat()
    Imgproc.fitLine(mat, line, Imgproc.DIST_HUBER, 0.0, 0.01, 0.01)
    val v = FloatArray(4).also { line.get(0, 0, it) }
    mat.release(); line.release()
    return Line(Pt(v[2].toDouble(), v[3].toDouble()), Pt(v[0].toDouble(), v[1].toDouble()))
}

private fun lineIntersection(first: Line, second: Line): Pt? {
    val p = first.point; val r = first.direction
    val q = second.point; val s = second.direction
    // Solve p + t*r = q + u*s
    val det = -r.x * s.y + s.x * r.y
    if (abs(det) < 1e-6) return null
    val d = q - p
    val t = (-d.x * s.y + s.x * d.y) / det
    return p + r * t
}

/**
 * Follow the card found in the previous frame: fit a line to the edges along each of its sides
 * (within `band` px) and intersect them. On a pile the card's outline often merges with the edge
 * of a card underneath; tracking also keeps the choice between nested outlines from flipping.
 */
private fun trackOutline(rawEdges: Mat, edges: Mat, gray: Mat, previous: Corners, allowLandscape: Boolean,
                         ratioTolerance: Double, band: Int = 6): Candidate? {
    val bandMask = Mat.zeros(rawEdges.size(), CvType.CV_8U)
    Imgproc.polylines(bandMask, listOf(MatOfPoint(*previous.map { Point(it.x.toInt().toDouble(), it.y.toInt().toDouble()) }.toTypedArray())),
        true, Scalar(255.0), 2 * band + 1)
    val inBand = Mat()
    Core.bitwise_and(rawEdges, bandMask, inBand)
    val width = inBand.cols()
    val pixels = bytesOf(inBand)
    inBand.release()
    val points = ArrayList<Pt>()
    for (i in pixels.indices) if (pixels[i].toInt() != 0) points.add(Pt((i % width).toDouble(), (i / width).toDouble()))
    if (points.size < 50) { bandMask.release(); return null }

    val lines = (0 until 4).map { fitSide(points, previous[it], previous[(it + 1) % 4], band) }
    if (lines.any { it == null }) { bandMask.release(); return null }
    val intersections = (0 until 4).map { lineIntersection(lines[(it + 3) % 4]!!, lines[it]!!) }
    if (intersections.any { it == null }) { bandMask.release(); return null }
    val corners = orderCorners(intersections.map { it!! })
    val sideW = (corners[1] - corners[0]).norm()
    val sideH = (corners[3] - corners[0]).norm()
    val reject = min(sideW, sideH) == 0.0 || (sideW > sideH && !allowLandscape) ||
        abs(max(sideW, sideH) / min(sideW, sideH) - CARD_ASPECT_RATIO) / CARD_ASPECT_RATIO > ratioTolerance
    if (reject) { bandMask.release(); return null }
    val area = sideW * sideH
    val previousArea = (previous[1] - previous[0]).norm() * (previous[3] - previous[0]).norm()
    if (area / previousArea !in 0.85..1.15) { bandMask.release(); return null }
    val edgesInBand = Mat()
    Core.bitwise_and(edges, bandMask, edgesInBand)
    val ok = perimeterCoverage(edgesInBand, corners) >= 0.8 && !isInnerFrame(gray, corners)
    edgesInBand.release(); bandMask.release()
    return if (ok) Candidate(area, corners, 1.0) else null
}

private fun median(gray: Mat): Double {
    val hist = IntArray(256)
    for (b in bytesOf(gray)) hist[b.toInt() and 0xFF]++
    val total = gray.total()
    // numpy's median of an even count averages the two middle values
    val lowIndex = (total - 1) / 2; val highIndex = total / 2
    var seen = 0L; var low = -1; var high = -1
    for (v in 0 until 256) {
        seen += hist[v]
        if (low < 0 && seen > lowIndex) low = v
        if (high < 0 && seen > highIndex) { high = v; break }
    }
    return (low + high) / 2.0
}

data class Outline(val corners: Corners, val score: Double)

/**
 * Find a card by its outline: the largest 4-sided contour with a card's aspect ratio.
 *
 * frame: RGB or RGBA image. previous: the card's corners in the previous frame (frame
 * coordinates) - the card is then followed along its sides first (see trackOutline).
 * Returns the corners in frame coordinates and how rectangular the outline is, or null.
 */
fun findCardOutline(frame: Mat, allowLandscape: Boolean = false, ratioTolerance: Double = 0.18,
                    workSize: Int = 640, previous: Corners? = null): Outline? {
    val scale = workSize.toDouble() / max(frame.rows(), frame.cols())
    val small = Mat()
    Imgproc.resize(frame, small, Size((frame.cols() * scale).toInt().toDouble(), (frame.rows() * scale).toInt().toDouble()),
        0.0, 0.0, Imgproc.INTER_AREA)
    val gray = Mat()
    Imgproc.cvtColor(small, gray, if (frame.channels() == 4) Imgproc.COLOR_RGBA2GRAY else Imgproc.COLOR_RGB2GRAY)
    Imgproc.GaussianBlur(gray, gray, Size(5.0, 5.0), 0.0)
    val med = median(gray)
    val rawEdges = Mat(); val edges = Mat()
    Imgproc.Canny(gray, rawEdges, max(0.0, 0.5 * med).toInt().toDouble(), min(255.0, 1.3 * med).toInt().toDouble())
    val k3 = kernel(3)
    Imgproc.dilate(rawEdges, edges, k3, Point(-1.0, -1.0), 2)
    k3.release()

    try {
        // Follow the previous card - as a small refinement only: an outline more than 2% away
        // was fitted to another edge, and taking it would make the result flip between outlines
        var tracked: Candidate? = null
        if (previous != null) {
            val prev = previous.scaled(scale)
            tracked = trackOutline(rawEdges, edges, gray, prev, allowLandscape, ratioTolerance)
            if (tracked != null) {
                val shift = (0 until 4).maxOf { (tracked.corners[it] - prev[it]).norm() } / (prev[2] - prev[0]).norm()
                if (shift <= 0.02) return Outline(tracked.corners.scaled(1 / scale), tracked.fill)
            }
        }

        val contours = ArrayList<MatOfPoint>()
        val hierarchy = Mat()
        Imgproc.findContours(edges, contours, hierarchy, Imgproc.RETR_LIST, Imgproc.CHAIN_APPROX_SIMPLE)
        hierarchy.release()
        val minArea = 0.02 * small.rows() * small.cols()  // card must cover at least 2% of the frame

        var best: Candidate? = null
        for (contour in contours) {
            val area = Imgproc.contourArea(contour)
            if (area < minArea) continue
            val corners = boxCorners(Imgproc.minAreaRect(MatOfPoint2f(*contour.toArray())))
            val sideW = (corners[1] - corners[0]).norm()
            val sideH = (corners[3] - corners[0]).norm()
            if (min(sideW, sideH) == 0.0) continue
            if (sideW > sideH && !allowLandscape) continue
            val ratio = max(sideW, sideH) / min(sideW, sideH)
            val fill = area / (sideW * sideH)  // 1.0 = perfectly rectangular
            if (abs(ratio - CARD_ASPECT_RATIO) / CARD_ASPECT_RATIO > ratioTolerance || fill < 0.85) continue
            if (best != null && area <= best.area) continue
            if (isInnerFrame(gray, corners)) continue
            best = Candidate(area, corners, fill)
        }
        contours.forEach { it.release() }

        // Outlines assembled from edge pieces or fitted lines can come out skewed; seen from
        // above a card is a rectangle
        if (best == null) {
            best = outlineFromEdgeGroups(edges, gray, minArea, allowLandscape, ratioTolerance)
            if (best != null && !isRectangular(best.corners)) best = null
        }
        if (best == null && tracked != null && isRectangular(tracked.corners)) best = tracked
        return best?.let { Outline(it.corners.scaled(1 / scale), it.fill) }
    } finally {
        listOf(small, gray, rawEdges, edges).forEach { it.release() }
    }
}

/**
 * Perspective-correct the card inside `corners` into a flat, portrait image.
 * outH: output height (default: the card's own height in the frame)
 */
fun warpCard(frame: Mat, corners: Corners, outH: Int? = null): Mat {
    var (tl, tr, br, bl) = corners
    if ((tr - tl).norm() > (bl - tl).norm()) {
        // Card lying sideways - rotate so the output is portrait
        val oldTl = tl; tl = bl; bl = br; br = tr; tr = oldTl
    }
    val height = outH ?: max((bl - tl).norm(), (br - tr).norm()).toInt()
    val width = (height / CARD_ASPECT_RATIO).toInt()
    val src = MatOfPoint2f(*listOf(tl, tr, br, bl).toPoints())
    val dst = MatOfPoint2f(Point(0.0, 0.0), Point(width - 1.0, 0.0), Point(width - 1.0, height - 1.0), Point(0.0, height - 1.0))
    val transform = Imgproc.getPerspectiveTransform(src, dst)
    val out = Mat()
    Imgproc.warpPerspective(frame, out, transform, Size(width.toDouble(), height.toDouble()))
    listOf(src, dst, transform).forEach { it.release() }
    return out
}
