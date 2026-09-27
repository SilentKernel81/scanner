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
import com.cardscanner.detection.Corners
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

    fun updateSettings(settings: AppSettings) {
        if (settings.rotation != _settings.value.rotation) pendingReset = true  // outlines of the old orientation
        _settings.value = settings
        settings.save(getApplication())
        tracker.requiredStableFrames = settings.stableFrames
        tracker.minSharpness = settings.minSharpness.toDouble()
    }

    /** Turn the camera image a quarter turn clockwise (like the Python scanner's rotate button) */
    fun rotate() = updateSettings(_settings.value.let { it.copy(rotation = (it.rotation + 90) % 360) })

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
            val frame = image.toRgbaMat(_settings.value.rotation)
            val (state, trigger) = tracker.process(frame, _autoCapture.value, capturing.get())
            frame.release()
            _detection.value = state
            state.message?.let { Log.i(TAG, it) }
            if (trigger && state.corners != null) capture(state.corners, state.frameWidth, state.frameHeight)
        } catch (e: Exception) {
            Log.e(TAG, "Frame analysis failed", e)
        } finally {
            image.close()
        }
    }

    /** Capture button: the card detected now */
    fun manualCapture() {
        val state = _detection.value
        if (state?.corners == null) {
            _message.value = "No card detected"
            return
        }
        if (!capturing.get()) {
            analysisExecutor.execute { tracker.markCaptured() }
            capture(state.corners, state.frameWidth, state.frameHeight)
        }
    }

    /**
     * Take a full-resolution still, find the card in it again (starting from the outline in the
     * analysis frame) and identify it in the background.
     */
    private fun capture(corners: Corners, frameWidth: Int, frameHeight: Int) {
        val capture = imageCapture ?: return
        if (!capturing.compareAndSet(false, true)) return
        capture.takePicture(captureExecutor, object : ImageCapture.OnImageCapturedCallback() {
            override fun onCaptureSuccess(image: ImageProxy) {
                val card = try {
                    cutOutCard(image, corners, frameWidth, frameHeight)
                } catch (e: Exception) {
                    Log.e(TAG, "Capture failed", e)
                    null
                } finally {
                    image.close()
                    capturing.set(false)
                }
                if (card == null) _message.value = "Capture failed" else identify(card)
            }

            override fun onError(exception: ImageCaptureException) {
                capturing.set(false)
                Log.e(TAG, "Capture failed", exception)
                _message.value = "Capture failed: ${exception.message}"
            }
        })
    }

    private fun cutOutCard(image: ImageProxy, corners: Corners, frameWidth: Int, frameHeight: Int): Bitmap {
        var bitmap = image.toBitmap()
        val rotation = (image.imageInfo.rotationDegrees + _settings.value.rotation) % 360
        if (rotation != 0) {
            bitmap = Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, Matrix().apply { postRotate(rotation.toFloat()) }, true)
        }
        val full = Mat()
        Utils.bitmapToMat(bitmap, full)  // RGBA
        // Same 4:3 view as the analysis frame, so its outline only needs scaling; the still is
        // searched again for the exact edges (the card may have shifted a pixel or two)
        val guess = corners.scaled(full.cols().toDouble() / frameWidth, full.rows().toDouble() / frameHeight)
        val outline = findCardOutline(full, previous = guess)?.corners ?: guess
        val warped = warpCard(full, outline)
        val card = Bitmap.createBitmap(warped.cols(), warped.rows(), Bitmap.Config.ARGB_8888)
        Utils.matToBitmap(warped, card)
        full.release(); warped.release()
        return card
    }

    // ------------------------------------------------------------------------
    // Identification
    // ------------------------------------------------------------------------

    private fun identify(card: Bitmap) {
        val thumbHeight = 280
        val thumbnail = Bitmap.createScaledBitmap(card, thumbHeight * card.width / card.height, thumbHeight, true)
        val id = nextId.incrementAndGet()
        _scans.update { (listOf(ScanResult(id, thumbnail)) + it).take(MAX_SCANS) }

        viewModelScope.launch(aiDispatcher) {
            val start = System.nanoTime()
            val result = try {
                val settings = _settings.value
                val identifier = identifier()
                val (answer, reading) = identifier.identify(card)
                if (reading == null) {
                    fail(id, "The AI could not read the card" + (if (answer.isNotBlank()) ": \"${answer.take(120)}\"" else ""))
                    return@launch
                }
                val marker = if (settings.detectFoil) identifier.readFoilSymbol(card) else Foil.UNKNOWN
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
