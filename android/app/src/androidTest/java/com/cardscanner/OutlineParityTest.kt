package com.cardscanner

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.cardscanner.detection.Corners
import com.cardscanner.detection.findCardOutline
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Test
import org.junit.runner.RunWith
import org.opencv.android.OpenCVLoader
import org.opencv.core.Mat
import org.opencv.core.MatOfByte
import org.opencv.imgcodecs.Imgcodecs
import org.opencv.imgproc.Imgproc
import java.io.File

/**
 * Runs the outline detector over recorded scanner frames (data/debug_frames, passed in with
 * -PdebugFrames=<dir>) and writes the corners to the app's external files dir as outlines.json,
 * to compare with object_detector.find_card_outline on the same frames.
 */
@RunWith(AndroidJUnit4::class)
class OutlineParityTest {
    @Test
    fun detectRecordedFrames() {
        check(OpenCVLoader.initLocal())
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val assets = instrumentation.context.assets
        val out = JSONObject()
        var totalMs = 0L
        for (folder in assets.list("frames")!!.sorted()) {
            var tracked: Corners? = null
            var missing = 0
            for (name in assets.list("frames/$folder")!!.sorted()) {
                val bytes = assets.open("frames/$folder/$name").readBytes()
                val bgr = Imgcodecs.imdecode(MatOfByte(*bytes), Imgcodecs.IMREAD_COLOR)
                val rgb = Mat()
                Imgproc.cvtColor(bgr, rgb, Imgproc.COLOR_BGR2RGB)
                val start = System.nanoTime()
                val outline = findCardOutline(rgb, previous = if (missing <= 2) tracked else null)
                totalMs += (System.nanoTime() - start) / 1_000_000
                if (outline != null) { tracked = outline.corners; missing = 0 } else missing++
                out.put("$folder/$name", outline?.let { o ->
                    JSONObject().put("corners", JSONArray(o.corners.map { JSONArray(listOf(it.x, it.y)) })).put("score", o.score)
                } ?: JSONObject.NULL)
                bgr.release(); rgb.release()
            }
        }
        out.put("_total_ms", totalMs)
        File(instrumentation.targetContext.getExternalFilesDir(null), "outlines.json").writeText(out.toString())
    }
}
