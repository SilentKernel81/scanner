package com.cardscanner.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Image
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import coil3.compose.AsyncImage
import com.cardscanner.ScannerViewModel
import com.cardscanner.ai.Foil
import com.cardscanner.inventory.Finish
import com.cardscanner.inventory.ReviewItem
import com.cardscanner.scryfall.Printing
import kotlinx.coroutines.launch

/**
 * The review queue, oldest first (the web UI's review): the photo beside the suggested printing,
 * what the AI read, and a search prefilled with it. Add resolves the item and opens the next;
 * Skip drops it.
 */
@Composable
fun ReviewScreen(viewModel: ScannerViewModel, onClose: () -> Unit) {
    val queue by viewModel.review.collectAsStateWithLifecycle()
    val item = queue.firstOrNull()
    LaunchedEffect(item == null) { if (item == null) onClose() }
    if (item == null) return
    // Everything below starts afresh for each item
    androidx.compose.runtime.key(item.id) { ReviewItemView(viewModel, item, queue.size, onClose) }
}

@Composable
private fun ReviewItemView(viewModel: ScannerViewModel, item: ReviewItem, remaining: Int, onClose: () -> Unit) {
    val scope = rememberCoroutineScope()
    val photo = remember(item.id) { item.image() }
    val search = rememberPrintingSearch(viewModel, item.name, item.setCode, item.number)
    var selected by remember { mutableStateOf<Printing?>(null) }
    var finish by remember { mutableStateOf(Finish.REGULAR) }

    fun choose(printing: Printing?) {
        selected = printing
        if (printing != null) finish = viewModel.suggestedFinish(printing, item.foil)
    }
    fun runSearch() = search.search { if (it.size == 1) choose(it.first()) }
    // Start with the suggestion; without one, search what the AI read right away
    LaunchedEffect(Unit) {
        val suggestion = viewModel.suggestion(item)
        if (suggestion != null) { search.results = listOf(suggestion); choose(suggestion) }
        if (item.name.isNotBlank()) runSearch()
    }

    Column(Modifier.fillMaxSize().safeDrawingPadding()) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onClose) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Close") }
            Column(Modifier.weight(1f)) {
                Text("Review ($remaining)", style = MaterialTheme.typography.titleLarge)
                Text(item.reason, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.tertiary)
            }
        }
        PrintingSearchGrid(search, selected?.id, ::choose, Modifier.fillMaxSize()) {
            // The photo beside the chosen printing
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                photo?.let { Image(it.asImageBitmap(), "Captured card", Modifier.height(220.dp).clip(RoundedCornerShape(6.dp))) }
                Column(Modifier.weight(1f)) {
                    selected?.let { p ->
                        AsyncImage(p.imageUrl, p.name, Modifier.height(150.dp).aspectRatio(63f / 88f).clip(RoundedCornerShape(6.dp)))
                        Text(p.name, fontWeight = FontWeight.SemiBold)
                        Text("${p.setName} (${p.setCode}) #${p.collectorNumber}", style = MaterialTheme.typography.bodySmall)
                    } ?: Text("Pick the printing below", color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            Text("AI read: ${item.name.ifEmpty { "?" }} #${item.number.ifEmpty { "?" }} [${item.setCode.ifEmpty { "?" }}]" +
                when (item.foil) { Foil.FOIL -> " · ★"; Foil.NON_FOIL -> " · •"; Foil.UNKNOWN -> "" },
                style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                Finish.entries.forEach { FilterChip(finish == it, { finish = it }, label = { Text(it.label) }) }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = { selected?.let { viewModel.addFromReview(item, it, finish) } }, enabled = selected != null) {
                    Text("Add")
                }
                OutlinedButton(onClick = { viewModel.skipReview(item) }) { Text("Skip") }
                // Nothing read (a network or quota error, a blurry photo): ask the AI again
                if (item.name.isBlank()) OutlinedButton(onClick = {
                    search.status = "Asking the AI…"
                    scope.launch {
                        val read = try { viewModel.identifyAgain(item) } catch (e: Exception) { search.status = "AI failed: ${e.message}"; return@launch }
                        if (read == null) { search.status = "The AI could not read the card"; return@launch }
                        search.name = read.name; search.setCode = read.setCode; search.number = read.collectorNumber
                        runSearch()
                    }
                }) { Text("Identify again") }
            }
        }
    }
}
