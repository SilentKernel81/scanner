package com.cardscanner.detection

import org.opencv.core.Core
import org.opencv.core.CvType
import org.opencv.core.Mat
import org.opencv.core.MatOfDouble
import org.opencv.core.Rect
import org.opencv.core.Size
import org.opencv.imgproc.Imgproc
import kotlin.math.abs
import kotlin.math.max

/**
 * Stillness, auto-capture and new-card detection - a port of the outline branch of
 * scanner.py:_capture_frames (_is_card_settled, _new_card_arrived, _outline_flicker,
 * _mark_captured). The phone's own continuous autofocus replaces the focus sweep / probe.
 *
 * The thresholds were measured on the Raspberry Pi webcam (camera noise, live drop tests);
 * re-measure them on phones before tuning - `metrics` shows the live values.
 */
class CardTracker(
    var requiredStableFrames: Int = 5,
    var minSharpness: Double = 250.0,
    private val autoCaptureDelayMs: Long = 1000,
) {
    enum class Status { NO_CARD, STABILIZING, READY, CAPTURED }

    data class Metrics(val movement: Double, val drift: Double, val sharpnessChange: Double, val sharpness: Double)

    data class State(
        /** The card's outline (in fixed-area mode: only one inside the area, for the foil check) */
        val corners: Corners?,
        /** Fixed-area mode: the area in frame pixels (x1, y1, x2, y2), else null */
        val area: IntArray?,
        val frameWidth: Int,
        val frameHeight: Int,
        val status: Status,
        val stableFrames: Int,
        val requiredFrames: Int,
        val inFocus: Boolean,
        val metrics: Metrics?,
        val message: String?,
    )

    // Frames without a card that always count as a change of card (the detector can miss 1-2
    // frames of a card lying still; shorter gaps are judged by where the card reappears)
    private val missingFramesForNewCard = 6

    private var trackedOutline: Corners? = null
    private var missingFrames = 0
    private var stableFrames = 0
    private var previousPoints: Corners? = null
    private var previousSharpness: Double? = null
    private var previousThumbnail: FloatArray? = null
    private var settleAnchor: Corners? = null
    private var capturedThumbnail: FloatArray? = null
    private var cardDisturbed = false
    private var lastFrameChange = 0.0 to 0.0  // (movement, image change)
    private var newCardGapOnly = false
    private var cardInFocus = false
    private var metrics: Metrics? = null
    private var lastAutoCapture = 0L

    // Fixed area (see processArea)
    private var area: List<Double>? = null
    private var areaThumb: FloatArray? = null
    private var areaPrevious: FloatArray? = null
    private var areaAnchor: FloatArray? = null
    private var areaCaptured: FloatArray? = null
    private var areaBigChanges = 0

    /** After a capture: waiting for the next card to be dropped */
    var awaitingNewCard = false
        private set

    /**
     * One analysis frame (RGBA, upright). Returns the detection state, and whether an
     * auto-capture should start now (`autoCapture` on and the card still and new).
     */
    fun process(frame: Mat, autoCapture: Boolean, busy: Boolean): Pair<State, Boolean> {
        area?.let { return processArea(frame, it, autoCapture, busy) }
        val outline = findCardOutline(frame, previous = if (missingFrames <= 2) trackedOutline else null)
        var message: String? = null
        var trigger = false

        if (outline != null) {
            val corners = outline.corners
            trackedOutline = corners
            val gap = missingFrames  // frames without a card just before this one
            missingFrames = 0
            stableFrames = if (isCardSettled(frame, corners)) minOf(stableFrames + 1, requiredStableFrames) else 0

            if (awaitingNewCard && newCardArrived(gap)) {
                awaitingNewCard = false
                stableFrames = 0  // the new card must settle first
                // Find the new card afresh: following the old outline could latch onto edges of
                // the new card that happen to lie where the old one's were
                trackedOutline = null
                message = "New card (jump %.1f%%, image change %.2f)".format(lastFrameChange.first * 100, lastFrameChange.second)
            }

            val now = System.currentTimeMillis()
            if (autoCapture && !busy && !awaitingNewCard && stableFrames >= requiredStableFrames &&
                now - lastAutoCapture >= autoCaptureDelayMs) {
                if (outlineFlicker()) {
                    awaitingNewCard = true
                    message = "Same card as the last capture (outline flickered) - not captured again"
                } else {
                    lastAutoCapture = now
                    markCaptured()
                    trigger = true
                }
            }
        } else {
            // A lost card is not a still card: the next one must settle from scratch
            stableFrames = 0
            missingFrames++
            if (awaitingNewCard && missingFrames >= missingFramesForNewCard) {
                awaitingNewCard = false
                message = "Card gone - ready for the next card"
            }
        }

        val status = when {
            outline == null -> Status.NO_CARD
            awaitingNewCard && autoCapture -> Status.CAPTURED
            stableFrames >= requiredStableFrames -> Status.READY
            else -> Status.STABILIZING
        }
        val state = State(outline?.corners, null, frame.cols(), frame.rows(), status, stableFrames, requiredStableFrames,
            cardInFocus, metrics, message)
        return state to trigger
    }

    /** Remember the captured card; auto-capture waits for the next one (manual captures too) */
    fun markCaptured() {
        awaitingNewCard = true
        capturedThumbnail = previousThumbnail
        areaCaptured = areaThumb
    }

    /**
     * Fixed area on (x1, y1, x2, y2 as fractions of the frame) or off (null). The area's
     * judgement starts afresh; a card already captured stays captured.
     */
    fun setArea(newArea: List<Double>?) {
        if (newArea == area) return
        area = newArea
        areaPrevious = null
        areaAnchor = null
        areaCaptured = if (awaitingNewCard) areaThumb else null
        stableFrames = 0
    }

    /**
     * One frame in fixed-area mode (scanner.py:_fixed_area_step - sleeved or borderless cards,
     * whose outline is unreliable): a card is present when the area is sharp, still when its
     * image hardly changes (frame to frame and since the still streak began - a sliding card
     * drifts), and new after a capture when the area changed like a card falling in, or settled
     * looking different. The photo is the area; an outline inside it serves the foil check.
     */
    private fun processArea(frame: Mat, fractions: List<Double>, autoCapture: Boolean, busy: Boolean): Pair<State, Boolean> {
        val width = frame.cols(); val height = frame.rows()
        val x1 = (fractions[0] * width).toInt().coerceIn(0, width - 8)
        val y1 = (fractions[1] * height).toInt().coerceIn(0, height - 8)
        val x2 = max(x1 + 8, (fractions[2] * width).toInt()).coerceAtMost(width)
        val y2 = max(y1 + 8, (fractions[3] * height).toInt()).coerceAtMost(height)
        val region = frame.submat(Rect(x1, y1, x2 - x1, y2 - y1))
        val sharpness = sharpness(region)
        val present = sharpness >= minSharpness  // an empty box measured ~30, a card ~1,600
        cardInFocus = present

        val thumb = areaThumbnail(region)
        areaThumb = thumb
        val change = areaPrevious?.let { areaDifference(thumb, it) } ?: 0.0
        areaPrevious = thumb
        if (stableFrames == 0 || areaAnchor == null) areaAnchor = thumb
        val drift = areaDifference(thumb, areaAnchor!!)
        metrics = Metrics(change, drift, 0.0, sharpness)

        val settled = present && change < AREA_STILL && drift < AREA_DRIFT
        stableFrames = if (settled) minOf(stableFrames + 1, requiredStableFrames) else 0
        if (!settled) areaAnchor = thumb

        var message: String? = null
        areaBigChanges = if (change > AREA_DROP) areaBigChanges + 1 else 0
        if (awaitingNewCard) {
            val different = areaCaptured?.let { areaDifference(thumb, it) > AREA_DIFFERENT } ?: false
            if (areaBigChanges >= 2 || (settled && different)) {
                awaitingNewCard = false
                stableFrames = 0
                message = "New card (area change %.1f)".format(change)
            }
        }

        // Outline inside the area -> the flat card whose ★/• corner is read
        var corners = findCardOutline(frame, previous = if (missingFrames <= 2) trackedOutline else null)?.corners
        if (corners != null) {
            trackedOutline = corners
            missingFrames = 0
            val marginX = 0.03 * width; val marginY = 0.03 * height
            if (corners.minOf { it.x } < x1 - marginX || corners.minOf { it.y } < y1 - marginY ||
                corners.maxOf { it.x } > x2 + marginX || corners.maxOf { it.y } > y2 + marginY) {
                corners = null  // an outline outside the area (e.g. the whole pile)
            }
        } else {
            missingFrames++
        }

        var trigger = false
        val now = System.currentTimeMillis()
        if (autoCapture && present && !busy && !awaitingNewCard && stableFrames >= requiredStableFrames &&
            now - lastAutoCapture >= autoCaptureDelayMs) {
            lastAutoCapture = now
            markCaptured()
            trigger = true
        }

        val status = when {
            !present -> Status.NO_CARD
            awaitingNewCard && autoCapture -> Status.CAPTURED
            stableFrames >= requiredStableFrames -> Status.READY
            else -> Status.STABILIZING
        }
        return State(corners, intArrayOf(x1, y1, x2, y2), width, height, status, stableFrames, requiredStableFrames,
            present, metrics, message) to trigger
    }

    /** Auto scanning switched on: the card lying there now counts as new */
    fun reset() {
        awaitingNewCard = false
        stableFrames = 0
        trackedOutline = null
        previousPoints = null
        previousSharpness = null
        settleAnchor = null
        areaPrevious = null
        areaAnchor = null
        areaCaptured = null
    }

    /**
     * True if the card is still and in focus: its corners moved less than ~1% of the card size
     * since the previous frame and since the still streak began, its sharpness changed by less
     * than 20% (autofocus still adjusting changes it a lot), and it is sharp enough to read.
     */
    private fun isCardSettled(frame: Mat, points: Corners): Boolean {
        val x1 = max(points.minOf { it.x }.toInt(), 0)
        val y1 = max(points.minOf { it.y }.toInt(), 0)
        val x2 = minOf(points.maxOf { it.x }.toInt(), frame.cols())
        val y2 = minOf(points.maxOf { it.y }.toInt(), frame.rows())
        if (x2 <= x1 || y2 <= y1) return false
        val sharpness = sharpness(frame.submat(Rect(x1, y1, x2 - x1, y2 - y1)))
        cardInFocus = sharpness >= minSharpness
        val thumbnail = cardThumbnail(frame, points)

        val prevPoints = previousPoints; val prevSharpness = previousSharpness; val prevThumbnail = previousThumbnail
        previousPoints = points; previousSharpness = sharpness; previousThumbnail = thumbnail
        if (prevPoints == null || prevSharpness == null) {
            cardDisturbed = true
            return false
        }

        val cardSize = (points[2] - points[0]).norm()
        val movement = (0 until 4).maxOf { (points[it] - prevPoints[it]).norm() } / cardSize
        // Drift since the still streak began: a sleeved card sliding slowly moves less than 1%
        // per frame and used to be captured mid-slide
        if (stableFrames == 0 || settleAnchor == null) settleAnchor = points
        val anchor = settleAnchor!!
        val drift = (0 until 4).maxOf { (points[it] - anchor[it]).norm() } / cardSize
        val sharpnessChange = abs(sharpness - prevSharpness) / max(prevSharpness, 1e-6)
        val imageChange = thumbnailDifference(thumbnail, prevThumbnail)

        // A drop (or a hand) makes the card jump or change far beyond camera noise
        // (measured on a card lying still: movement <= 0.4%, image change <= 0.07)
        cardDisturbed = movement > 0.03 || imageChange > 0.3
        lastFrameChange = movement to imageChange
        metrics = Metrics(movement, drift, sharpnessChange, sharpness)
        val settled = movement < 0.01 && drift < 0.01 && sharpnessChange < 0.2 && cardInFocus
        if (!settled) settleAnchor = points
        return settled
    }

    /**
     * After a capture: has the next card been dropped onto the pile? The card jumped or its
     * image changed sharply, or it reappears after a short gap in a different spot, or the card
     * on the pile looks different from the captured one.
     */
    private fun newCardArrived(gap: Int): Boolean {
        newCardGapOnly = false
        if (cardDisturbed) return true
        if (thumbnailDifference(previousThumbnail, capturedThumbnail) > 0.3) return true
        if (gap >= 1 && lastFrameChange.first > 0.008) {
            // Only this weak sign: the outline flickered and came back a little shifted
            newCardGapOnly = true
            return true
        }
        return false
    }

    /** A "new card" seen only because the outline flickered, settled as the very image just captured */
    private fun outlineFlicker(): Boolean {
        val same = newCardGapOnly && thumbnailDifference(previousThumbnail, capturedThumbnail) < 0.05
        newCardGapOnly = false
        return same
    }

    companion object {
        // Mean difference of the area's 48x64 thumbnail (brightness removed), measured on recorded
        // sleeved piles: still card <= 2.4 frame to frame (<= 5.4 over 1 s with the light
        // changing), a hand's shadow <= 0.6, a card falling in 10-38 over several frames in a
        // row, a different card settled ~21; a sleeved card settling after its capture jumped
        // 13 in a single frame - so a drop needs AREA_DROP in 2 frames in a row
        const val AREA_STILL = 3.0
        const val AREA_DRIFT = 4.0
        const val AREA_DROP = 8.0
        const val AREA_DIFFERENT = 10.0

        /** 48x64 grayscale thumbnail with its mean removed: a uniform brightness change (a shadow) is not a change */
        private fun areaThumbnail(region: Mat): FloatArray {
            val small = Mat(); val gray = Mat()
            Imgproc.resize(region, small, Size(48.0, 64.0), 0.0, 0.0, Imgproc.INTER_AREA)
            Imgproc.cvtColor(small, gray, if (region.channels() == 4) Imgproc.COLOR_RGBA2GRAY else Imgproc.COLOR_RGB2GRAY)
            val bytes = ByteArray(48 * 64).also { gray.get(0, 0, it) }
            small.release(); gray.release()
            val values = FloatArray(bytes.size) { (bytes[it].toInt() and 0xFF).toFloat() }
            val mean = values.average().toFloat()
            return FloatArray(values.size) { values[it] - mean }
        }

        private fun areaDifference(a: FloatArray, b: FloatArray): Double {
            var sum = 0.0
            for (i in a.indices) sum += abs(a[i] - b[i])
            return sum / a.size
        }

        /** Variance of the Laplacian on a 160 px wide copy (same measure as the Python scanner) */
        fun sharpness(region: Mat): Double {
            val small = Mat(); val gray = Mat(); val laplacian = Mat()
            val height = max(1, (160.0 * region.rows() / region.cols()).toInt())
            Imgproc.resize(region, small, Size(160.0, height.toDouble()), 0.0, 0.0, Imgproc.INTER_AREA)
            Imgproc.cvtColor(small, gray, if (region.channels() == 4) Imgproc.COLOR_RGBA2GRAY else Imgproc.COLOR_RGB2GRAY)
            Imgproc.Laplacian(gray, laplacian, CvType.CV_64F)
            val mean = MatOfDouble(); val std = MatOfDouble()
            Core.meanStdDev(laplacian, mean, std)
            val value = std.get(0, 0)[0].let { it * it }
            listOf(small, gray, laplacian, mean, std).forEach { it.release() }
            return value
        }

        /** Tiny, brightness-normalized, perspective-corrected card image for comparisons */
        private fun cardThumbnail(frame: Mat, points: Corners): FloatArray {
            val card = warpCard(frame, points, outH = 180)
            val small = Mat(); val gray = Mat()
            Imgproc.resize(card, small, Size(32.0, 45.0), 0.0, 0.0, Imgproc.INTER_AREA)
            Imgproc.cvtColor(small, gray, if (frame.channels() == 4) Imgproc.COLOR_RGBA2GRAY else Imgproc.COLOR_RGB2GRAY)
            val bytes = ByteArray(32 * 45).also { gray.get(0, 0, it) }
            listOf(card, small, gray).forEach { it.release() }
            val values = FloatArray(bytes.size) { (bytes[it].toInt() and 0xFF).toFloat() }
            val mean = values.average()
            val std = kotlin.math.sqrt(values.sumOf { (it - mean) * (it - mean) } / values.size)
            return FloatArray(values.size) { ((values[it] - mean) / (std + 1e-6)).toFloat() }
        }

        /** Mean difference between two card thumbnails (0 = identical) */
        private fun thumbnailDifference(a: FloatArray?, b: FloatArray?): Double {
            if (a == null || b == null || a.size != b.size) return 0.0
            var sum = 0.0
            for (i in a.indices) sum += abs(a[i] - b[i])
            return sum / a.size
        }
    }
}
