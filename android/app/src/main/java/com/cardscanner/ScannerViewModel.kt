package com.cardscanner

import android.app.Application
import android.graphics.Bitmap
import android.graphics.Matrix
import android.util.Log
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.ImageProxy
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.cardscanner.ai.CardIdentifier
import com.cardscanner.ai.CardReading
import com.cardscanner.ai.Foil
import com.cardscanner.detection.CardTracker
import com.cardscanner.detection.findCardOutline
import com.cardscanner.detection.scaled
import com.cardscanner.detection.warpCard
import com.cardscanner.scryfall.Printing
import com.cardscanner.scryfall.Scryfall
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import org.opencv.android.Utils
import org.opencv.core.Core
import org.opencv.core.CvType
import org.opencv.core.Mat
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

/** One captured card and what became of it */
data class ScanResult(
    val id: Long,
    val thumbnail: Bitmap,
    val status: Status = Status.IDENTIFYING,
    val reading: CardReading? = null,
    val printing: Printing? = null,
    val foil: Boolean? = null,
    /** Why the finish was chosen: from the printing data (certain) or from the ★/• marker */
    val foilReason: String? = null,
    val seconds: Double? = null,
    val error: String? = null,
) {
    enum class Status { IDENTIFYING, DONE, FAILED }
}

class ScannerViewModel(application: Application) : AndroidViewModel(application) {
    private val http = CardIdentifier.httpClient()
    private val scryfall = Scryfall(http)
    private val nextId = AtomicLong()

    private val _settings = MutableStateFlow(AppSettings.load(application))
    val settings: StateFlow<AppSettings> = _settings.asStateFlow()

    private val _detection = MutableStateFlow<CardTracker.State?>(null)
    val detection: StateFlow<CardTracker.State?> = _detection.asStateFlow()

    private val _scans = MutableStateFlow<List<ScanResult>>(emptyList())
    val scans: StateFlow<List<ScanResult>> = _scans.asStateFlow()

    private val _autoCapture = MutableStateFlow(false)
    val autoCapture: StateFlow<Boolean> = _autoCapture.asStateFlow()

    private val _message = MutableStateFlow<String?>(null)
    val message: StateFlow<String?> = _message.asStateFlow()

    private val tracker = CardTracker(_settings.value.stableFrames, _settings.value.minSharpness.toDouble())
    private val analysisExecutor = Executors.newSingleThreadExecutor()
    private val captureExecutor = Executors.newSingleThreadExecutor()
    // One AI request at a time, in capture order
    private val aiDispatcher = Dispatchers.IO.limitedParallelism(1)

    /** Taking the still image of a capture (the next auto-capture waits for it) */
    private val capturing = AtomicBoolean(false)
    @Volatile private var pendingReset = false

    var imageCapture: ImageCapture? = null

    fun updateSettings(newSettings: AppSettings) {
        var settings = newSettings
        if (settings.rotation != _settings.value.rotation) {
            pendingReset = true  // outlines of the old orientation
            if (settings.fixedAreaEnabled) {
                // An area drawn for the old orientation no longer fits: draw it again
                settings = settings.copy(fixedAreaEnabled = false, fixedArea = null)
                _message.value = "Fixed area off - draw it again"
            }
        }
        _settings.value = settings
        settings.save(getApplication())
        tracker.requiredStableFrames = settings.stableFrames
        tracker.minSharpness = settings.minSharpness.toDouble()
    }

    /** Turn the camera image a quarter turn clockwise (like the Python scanner's rotate button) */
    fun rotate() = updateSettings(_settings.value.let { it.copy(rotation = (it.rotation + 90) % 360) })

    /** Draw a fixed area (fractions of the frame) and switch to it */
    fun setFixedArea(area: List<Double>) =
        updateSettings(_settings.value.copy(fixedArea = area.map { it.coerceIn(0.0, 1.0) }, fixedAreaEnabled = true))

    fun setFixedAreaEnabled(enabled: Boolean) = updateSettings(_settings.value.copy(fixedAreaEnabled = enabled))

    /** The detected card plus 5% as the fixed area (scanner.py:detected_area); false without a card */
    fun useDetectedCard(margin: Double = 0.05): Boolean {
        val state = _detection.value
        val corners = state?.corners ?: return false
        val x1 = corners.minOf { it.x }; val x2 = corners.maxOf { it.x }
        val y1 = corners.minOf { it.y }; val y2 = corners.maxOf { it.y }
        val dx = (x2 - x1) * margin; val dy = (y2 - y1) * margin
        setFixedArea(listOf((x1 - dx) / state.frameWidth, (y1 - dy) / state.frameHeight,
            (x2 + dx) / state.frameWidth, (y2 + dy) / state.frameHeight))
        return true
    }

    fun setAutoCapture(enabled: Boolean) {
        _autoCapture.value = enabled
        if (enabled) {
            pendingReset = true  // the card lying there now counts as new
            viewModelScope.launch(Dispatchers.IO) { identifier().warmUp() }
        }
    }

    fun clearMessage() { _message.value = null }

    fun clearScans() { _scans.value = emptyList() }

    private fun identifier() = _settings.value.let {
        CardIdentifier(it.provider, it.model(), it.apiKey(), it.localUrl, http)
    }

    // ------------------------------------------------------------------------
    // Camera frames
    // ------------------------------------------------------------------------

    val analyzer = ImageAnalysis.Analyzer { image -> analyze(image) }
    val analyzerExecutor get() = analysisExecutor

    private fun analyze(image: ImageProxy) {
        try {
            if (pendingReset) { pendingReset = false; tracker.reset() }
            tracker.setArea(_settings.value.activeArea)
            val frame = image.toRgbaMat(_settings.value.rotation)
            val (state, trigger) = tracker.process(frame, _autoCapture.value, capturing.get())
            frame.release()
            _detection.value = state
            state.message?.let { Log.i(TAG, it) }
            if (trigger) capture(state)
        } catch (e: Exception) {
            Log.e(TAG, "Frame analysis failed", e)
        } finally {
            image.close()
        }
    }

    /** Capture button: the card detected now */
    fun manualCapture() {
        val state = _detection.value
        // Fixed area: the photo is the area, whatever the detector sees
        if (state == null || (state.corners == null && state.area == null)) {
            _message.value = "No card detected"
            return
        }
        if (!capturing.get()) {
            analysisExecutor.execute { tracker.markCaptured() }
            capture(state)
        }
    }

    /**
     * Take a full-resolution still, find the card in it again (starting from the outline in the
     * analysis frame) and identify it in the background.
     */
    private fun capture(state: CardTracker.State) {
        val capture = imageCapture ?: return
        if (!capturing.compareAndSet(false, true)) return
        capture.takePicture(captureExecutor, object : ImageCapture.OnImageCapturedCallback() {
            override fun onCaptureSuccess(image: ImageProxy) {
                val card = try {
                    cutOutCard(image, state)
                } catch (e: Exception) {
                    Log.e(TAG, "Capture failed", e)
                    null
                } finally {
                    image.close()
                    capturing.set(false)
                }
                if (card == null) _message.value = "Capture failed" else identify(card.first, card.second)
            }

            override fun onError(exception: ImageCaptureException) {
                capturing.set(false)
                Log.e(TAG, "Capture failed", exception)
                _message.value = "Capture failed: ${exception.message}"
            }
        })
    }

    /**
     * The photo (the flat card; in fixed-area mode the area as drawn) and the flat card for the
     * ★/• check (null in fixed-area mode without an outline inside the area)
     */
    private fun cutOutCard(image: ImageProxy, state: CardTracker.State): Pair<Bitmap, Bitmap?> {
        var bitmap = image.toBitmap()
        val rotation = (image.imageInfo.rotationDegrees + _settings.value.rotation) % 360
        if (rotation != 0) {
            bitmap = Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, Matrix().apply { postRotate(rotation.toFloat()) }, true)
        }
        val full = Mat()
        Utils.bitmapToMat(bitmap, full)  // RGBA
        // Same 4:3 view as the analysis frame, so its outline and area only need scaling; the
        // still is searched again for the exact edges (the card may have shifted a pixel or two)
        val scaleX = full.cols().toDouble() / state.frameWidth
        val scaleY = full.rows().toDouble() / state.frameHeight
        val flat = state.corners?.let { corners ->
            val guess = corners.scaled(scaleX, scaleY)
            val warped = warpCard(full, findCardOutline(full, previous = guess)?.corners ?: guess)
            warped.toBitmap().also { warped.release() }
        }
        val photo = state.area?.let { (x1, y1, x2, y2) ->
            // The photo is the area as drawn: nothing inside it is cut off (a holo streak once
            // passed for a card's top edge and the outline cut off the name)
            val left = (x1 * scaleX).toInt().coerceIn(0, full.cols() - 1)
            val top = (y1 * scaleY).toInt().coerceIn(0, full.rows() - 1)
            val right = (x2 * scaleX).toInt().coerceIn(left + 1, full.cols())
            val bottom = (y2 * scaleY).toInt().coerceIn(top + 1, full.rows())
            val region = full.submat(org.opencv.core.Rect(left, top, right - left, bottom - top))
            region.toBitmap()
        } ?: flat ?: throw IllegalStateException("No card outline")
        full.release()
        return photo to flat
    }

    // ------------------------------------------------------------------------
    // Identification
    // ------------------------------------------------------------------------

    private fun identify(card: Bitmap, foilCard: Bitmap?) {
        val thumbHeight = 280
        val thumbnail = Bitmap.createScaledBitmap(card, thumbHeight * card.width / card.height, thumbHeight, true)
        val id = nextId.incrementAndGet()
        _scans.update { (listOf(ScanResult(id, thumbnail)) + it).take(MAX_SCANS) }
        Log.i(TAG, "Capture #$id: ${card.width}x${card.height}" + if (foilCard == null) " (no outline: no foil check)" else "")

        viewModelScope.launch(aiDispatcher) {
            val start = System.nanoTime()
            lateinit var reading: CardReading
            val result = try {
                val settings = _settings.value
                val identifier = identifier()
                val (answer, read) = identifier.identify(card)
                if (read == null) {
                    fail(id, "The AI could not read the card" + (if (answer.isNotBlank()) ": \"${answer.take(120)}\"" else ""))
                    return@launch
                }
                reading = read
                // The ★/• corner is only at a known place on the flat, tightly cropped card
                val marker = if (settings.detectFoil && foilCard != null) identifier.readFoilSymbol(foilCard) else Foil.UNKNOWN
                val printing = scryfall.find(reading.name, reading.collectorNumber, reading.setCode)
                val (foil, reason) = finish(printing, marker)
                ScanResult(id, thumbnail, ScanResult.Status.DONE, reading, printing, foil, reason,
                    seconds = (System.nanoTime() - start) / 1e9,
                    error = if (printing == null) "Not found on Scryfall" else null)
            } catch (e: Exception) {
                Log.e(TAG, "Identification failed", e)
                fail(id, e.message ?: e.toString())
                return@launch
            }
            Log.i(TAG, "Identified #$id: ${reading.name} #${reading.collectorNumber} [${reading.setCode}] -> " +
                (result.printing?.let { "${it.name} ${it.setCode} #${it.collectorNumber} (${it.match})" } ?: "not found") +
                " foil=${result.foil} in %.1f s".format(result.seconds))
            _scans.update { list -> list.map { if (it.id == id) result else it } }
        }
    }

    private fun fail(id: Long, error: String) {
        _scans.update { list -> list.map { if (it.id == id) it.copy(status = ScanResult.Status.FAILED, error = error) else it } }
    }

    /**
     * Foil or not: printings that exist in only one finish are known for certain; otherwise the
     * ★/• marker decides (Game.suggested_finish / scanner.js:suggestedFinish)
     */
    private fun finish(printing: Printing?, marker: Foil): Pair<Boolean?, String?> {
        val finishes = printing?.finishes.orEmpty()
        val hasFoil = "foil" in finishes || "etched" in finishes
        val hasNonFoil = "nonfoil" in finishes
        return when {
            hasFoil && !hasNonFoil -> true to "only printed in foil"
            hasNonFoil && !hasFoil -> false to "only printed non-foil"
            marker == Foil.FOIL -> true to "★ marker"
            marker == Foil.NON_FOIL -> false to "• marker"
            else -> null to null
        }
    }

    override fun onCleared() {
        analysisExecutor.shutdown()
        captureExecutor.shutdown()
    }

    companion object {
        private const val TAG = "Scanner"
        private const val MAX_SCANS = 100
    }
}

private fun Mat.toBitmap(): Bitmap =
    Bitmap.createBitmap(cols(), rows(), Bitmap.Config.ARGB_8888).also { Utils.matToBitmap(this, it) }

/** RGBA_8888 analysis frame as an upright RGBA Mat, turned `extraRotation` degrees further clockwise */
private fun ImageProxy.toRgbaMat(extraRotation: Int): Mat {
    val plane = planes[0]
    val buffer = plane.buffer.apply { rewind() }
    val mat = Mat(height, width, CvType.CV_8UC4)
    val rowBytes = width * 4
    if (plane.rowStride == rowBytes) {
        val bytes = ByteArray(rowBytes * height)
        buffer.get(bytes)
        mat.put(0, 0, bytes)
    } else {
        // Rows padded to rowStride
        val row = ByteArray(rowBytes)
        for (y in 0 until height) {
            buffer.position(y * plane.rowStride)
            buffer.get(row)
            mat.put(y, 0, row)
        }
    }
    val code = when ((imageInfo.rotationDegrees + extraRotation) % 360) {
        90 -> Core.ROTATE_90_CLOCKWISE
        180 -> Core.ROTATE_180
        270 -> Core.ROTATE_90_COUNTERCLOCKWISE
        else -> return mat
    }
    val rotated = Mat()
    Core.rotate(mat, rotated, code)
    mat.release()
    return rotated
}
