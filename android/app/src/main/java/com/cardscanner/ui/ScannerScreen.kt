package com.cardscanner.ui

import android.content.Intent
import android.net.Uri
import android.util.Size
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageCapture
import androidx.camera.core.Preview
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.DeleteSweep
import androidx.compose.material.icons.filled.FlashlightOff
import androidx.compose.material.icons.filled.FlashlightOn
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import coil3.compose.AsyncImage
import com.cardscanner.ScanResult
import com.cardscanner.ScannerViewModel
import com.cardscanner.detection.CardTracker
import android.widget.Toast

@Composable
fun ScannerScreen(viewModel: ScannerViewModel, onSettings: () -> Unit) {
    val detection by viewModel.detection.collectAsStateWithLifecycle()
    val scans by viewModel.scans.collectAsStateWithLifecycle()
    val auto by viewModel.autoCapture.collectAsStateWithLifecycle()
    val message by viewModel.message.collectAsStateWithLifecycle()
    val settings by viewModel.settings.collectAsStateWithLifecycle()
    var torch by remember { mutableStateOf(false) }
    val context = LocalContext.current

    LaunchedEffect(message) {
        message?.let { Toast.makeText(context, it, Toast.LENGTH_SHORT).show(); viewModel.clearMessage() }
    }

    BoxWithConstraints(Modifier.fillMaxSize().safeDrawingPadding()) {
    // At most 60% of the height, so the controls and results stay visible on wide screens
    val cameraWidth = minOf(maxWidth, maxHeight * 0.6f * 3f / 4f)
    Column(Modifier.fillMaxSize()) {
        // Camera view with the detected outline: 3:4 like the (portrait) camera frames, so frame
        // coordinates map linearly onto it
        Box(Modifier.width(cameraWidth).aspectRatio(3f / 4f).align(Alignment.CenterHorizontally).background(Color.Black)) {
            CameraPreview(viewModel, torch)
            OutlineOverlay(detection)
            StatusChip(detection, auto, Modifier.align(Alignment.TopStart).padding(8.dp))
        }

        Row(
            Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 6.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Switch(checked = auto, onCheckedChange = viewModel::setAutoCapture)
            Text("Auto", Modifier.padding(start = 8.dp))
            Spacer(Modifier.weight(1f))
            Button(onClick = viewModel::manualCapture) { Text("Capture") }
            IconButton(onClick = { torch = !torch }) {
                Icon(if (torch) Icons.Default.FlashlightOn else Icons.Default.FlashlightOff, "Light")
            }
            IconButton(onClick = viewModel::clearScans, enabled = scans.isNotEmpty()) {
                Icon(Icons.Default.DeleteSweep, "Clear list")
            }
            IconButton(onClick = onSettings) { Icon(Icons.Default.Settings, "Settings") }
        }
        detection?.metrics?.let {
            Text(
                "movement %.1f%% · drift %.1f%% · sharpness %.0f (min %d)".format(
                    it.movement * 100, it.drift * 100, it.sharpness, settings.minSharpness),
                Modifier.padding(horizontal = 12.dp),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        LazyColumn(Modifier.fillMaxSize().padding(top = 4.dp)) {
            items(scans, key = { it.id }) { ScanRow(it) }
        }
    }
    }
}

@Composable
private fun CameraPreview(viewModel: ScannerViewModel, torch: Boolean) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val previewView = remember {
        PreviewView(context).apply { scaleType = PreviewView.ScaleType.FIT_CENTER }
    }
    var camera by remember { mutableStateOf<androidx.camera.core.Camera?>(null) }

    DisposableEffect(lifecycleOwner) {
        val providerFuture = ProcessCameraProvider.getInstance(context)
        providerFuture.addListener({
            val provider = providerFuture.get()
            // Everything in 4:3 (the sensor's own shape): preview, analysis and stills show the
            // same view, so outlines found in analysis frames also fit the still
            val fourByThree = AspectRatioStrategy.RATIO_4_3_FALLBACK_AUTO_STRATEGY
            val preview = Preview.Builder()
                .setResolutionSelector(ResolutionSelector.Builder().setAspectRatioStrategy(fourByThree).build())
                .build().also { it.surfaceProvider = previewView.surfaceProvider }
            val analysis = ImageAnalysis.Builder()
                .setResolutionSelector(ResolutionSelector.Builder()
                    .setAspectRatioStrategy(fourByThree)
                    .setResolutionStrategy(ResolutionStrategy(Size(1280, 960), ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER))
                    .build())
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888)
                .build().also { it.setAnalyzer(viewModel.analyzerExecutor, viewModel.analyzer) }
            val still = ImageCapture.Builder()
                .setResolutionSelector(ResolutionSelector.Builder()
                    .setAspectRatioStrategy(fourByThree)
                    .setResolutionStrategy(ResolutionStrategy(Size(3264, 2448), ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER))
                    .build())
                .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
                .build()
            viewModel.imageCapture = still
            provider.unbindAll()
            camera = provider.bindToLifecycle(lifecycleOwner, CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis, still)
        }, ContextCompat.getMainExecutor(context))
        onDispose {
            viewModel.imageCapture = null
            if (providerFuture.isDone) providerFuture.get().unbindAll()
        }
    }
    LaunchedEffect(camera, torch) { camera?.cameraControl?.enableTorch(torch) }

    AndroidView({ previewView }, Modifier.fillMaxSize())
}

private fun statusColor(status: CardTracker.Status?) = when (status) {
    CardTracker.Status.READY -> Color(0xFF00E676)
    CardTracker.Status.STABILIZING -> Color(0xFFFFA500)
    CardTracker.Status.CAPTURED -> Color(0xFF64C8FF)
    else -> Color.Gray
}

@Composable
private fun OutlineOverlay(state: CardTracker.State?) {
    Canvas(Modifier.fillMaxSize()) {
        val corners = state?.corners ?: return@Canvas
        val sx = size.width / state.frameWidth
        val sy = size.height / state.frameHeight
        val path = Path().apply {
            moveTo(corners[0].x.toFloat() * sx, corners[0].y.toFloat() * sy)
            corners.drop(1).forEach { lineTo(it.x.toFloat() * sx, it.y.toFloat() * sy) }
            close()
        }
        drawPath(path, statusColor(state.status), style = Stroke(width = 3.dp.toPx()))
        drawCircle(statusColor(state.status), 4.dp.toPx(), Offset(corners[0].x.toFloat() * sx, corners[0].y.toFloat() * sy))
    }
}

@Composable
private fun StatusChip(state: CardTracker.State?, auto: Boolean, modifier: Modifier) {
    val text = when (state?.status) {
        null, CardTracker.Status.NO_CARD -> "No card"
        CardTracker.Status.STABILIZING -> "Stabilizing ${state.stableFrames}/${state.requiredFrames}" +
            if (!state.inFocus) " · blurry" else ""
        CardTracker.Status.READY -> if (auto) "Ready" else "Ready - tap Capture"
        CardTracker.Status.CAPTURED -> "Captured - drop the next card"
    }
    Text(
        text,
        modifier.clip(RoundedCornerShape(6.dp)).background(Color.Black.copy(alpha = 0.6f)).padding(horizontal = 8.dp, vertical = 4.dp),
        color = statusColor(state?.status),
        style = MaterialTheme.typography.labelLarge,
    )
}

@Composable
private fun ScanRow(scan: ScanResult) {
    val context = LocalContext.current
    val printing = scan.printing
    Row(
        Modifier.fillMaxWidth()
            .clickable(enabled = printing != null) {
                context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(printing!!.scryfallUrl)))
            }
            .padding(horizontal = 12.dp, vertical = 6.dp),
        horizontalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        Image(scan.thumbnail.asImageBitmap(), "Captured card", Modifier.height(96.dp).clip(RoundedCornerShape(4.dp)))
        if (printing?.imageUrl != null) {
            AsyncImage(printing.imageUrl, "Scryfall image", Modifier.height(96.dp).width(69.dp).clip(RoundedCornerShape(4.dp)))
        }
        Column(Modifier.weight(1f)) {
            when (scan.status) {
                ScanResult.Status.IDENTIFYING -> Row(verticalAlignment = Alignment.CenterVertically) {
                    CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp)
                    Text("Identifying…", Modifier.padding(start = 8.dp))
                }
                ScanResult.Status.FAILED -> Text(scan.error ?: "Failed", color = MaterialTheme.colorScheme.error)
                ScanResult.Status.DONE -> {
                    Text(printing?.name ?: scan.reading?.name.orEmpty(), fontWeight = FontWeight.SemiBold)
                    if (printing != null) {
                        Text("${printing.setName} (${printing.setCode}) #${printing.collectorNumber} · ${printing.rarity}",
                            style = MaterialTheme.typography.bodySmall)
                    }
                    val finish = when (scan.foil) { true -> "Foil"; false -> "Non-foil"; null -> "Finish unknown" }
                    Text(finish + (scan.foilReason?.let { " ($it)" } ?: ""), style = MaterialTheme.typography.bodySmall)
                    val price = if (scan.foil == true) printing?.usdFoil ?: printing?.eurFoil?.let { "€$it" }
                                else printing?.usd ?: printing?.eur?.let { "€$it" }
                    price?.let { Text(if (it.startsWith("€")) it else "$$it", style = MaterialTheme.typography.bodySmall) }
                    if (printing != null && !printing.confirmed) {
                        Text("Check: matched by ${printing.match.replace('_', ' ')}",
                            color = MaterialTheme.colorScheme.tertiary, style = MaterialTheme.typography.bodySmall)
                    }
                    scan.error?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }
                    val read = scan.reading
                    Text(
                        "AI read: ${read?.name} #${read?.collectorNumber?.ifEmpty { "?" }} [${read?.setCode?.ifEmpty { "?" }}]" +
                            (scan.seconds?.let { " · %.1f s".format(it) } ?: ""),
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }
    }
}
