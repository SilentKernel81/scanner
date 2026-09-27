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
    var name by remember { mutableStateOf(item.name) }
    var setCode by remember { mutableStateOf(item.setCode) }
    var number by remember { mutableStateOf(item.number) }
    var results by remember { mutableStateOf<List<Printing>>(emptyList()) }
    var selected by remember { mutableStateOf<Printing?>(null) }
    var finish by remember { mutableStateOf(Finish.REGULAR) }
    var searching by remember { mutableStateOf(false) }
    var status by remember { mutableStateOf<String?>(null) }

    fun choose(printing: Printing?) {
        selected = printing
        if (printing != null) finish = viewModel.suggestedFinish(printing, item.foil)
    }
    fun search() {
        searching = true
        status = null
        scope.launch {
            results = try {
                viewModel.searchPrintings(name, setCode, number).also { if (it.isEmpty()) status = "Nothing found" }
            } catch (e: Exception) {
                status = "Search failed: ${e.message}"
                emptyList()
            }
            if (results.size == 1) choose(results.first())
            searching = false
        }
    }
    // Start with the suggestion; without one, search what the AI read right away
    LaunchedEffect(Unit) {
        val suggestion = viewModel.suggestion(item)
        if (suggestion != null) { results = listOf(suggestion); choose(suggestion) }
        if (item.name.isNotBlank()) search()
    }

    Column(Modifier.fillMaxSize().safeDrawingPadding()) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onClose) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Close") }
            Column(Modifier.weight(1f)) {
                Text("Review ($remaining)", style = MaterialTheme.typography.titleLarge)
                Text(item.reason, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.tertiary)
            }
        }
        LazyVerticalGrid(GridCells.Adaptive(96.dp), Modifier.fillMaxSize().padding(horizontal = 12.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            item(span = { GridItemSpan(maxLineSpan) }) {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
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
                            status = "Asking the AI…"
                            scope.launch {
                                val read = try { viewModel.identifyAgain(item) } catch (e: Exception) { status = "AI failed: ${e.message}"; return@launch }
                                if (read == null) { status = "The AI could not read the card"; return@launch }
                                name = read.name; setCode = read.setCode; number = read.collectorNumber
                                search()
                            }
                        }) { Text("Identify again") }
                    }
                    // Search: name, and set + number for one exact printing
                    OutlinedTextField(name, { name = it }, Modifier.fillMaxWidth(), label = { Text("Card name") }, singleLine = true,
                        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search), keyboardActions = KeyboardActions(onSearch = { search() }))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                        OutlinedTextField(setCode, { setCode = it }, Modifier.width(90.dp), label = { Text("Set") }, singleLine = true)
                        OutlinedTextField(number, { number = it }, Modifier.width(100.dp), label = { Text("Number") }, singleLine = true)
                        OutlinedButton(onClick = { search() }, enabled = !searching) { Text("Search") }
                        if (searching) CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp)
                    }
                    status?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
                    if (results.size > 1) Text("${results.size} printings - tap the one you have", style = MaterialTheme.typography.bodySmall)
                }
            }
            items(results, key = { it.id }) { p ->
                val chosen = p.id == selected?.id
                Column(Modifier.clickable { choose(p) }
                    .then(if (chosen) Modifier.border(BorderStroke(3.dp, MaterialTheme.colorScheme.primary), RoundedCornerShape(6.dp)) else Modifier)
                    .padding(3.dp)) {
                    Box { AsyncImage(p.imageUrl, p.name, Modifier.fillMaxWidth().aspectRatio(63f / 88f).clip(RoundedCornerShape(4.dp))) }
                    Text("${p.setCode} #${p.collectorNumber}", style = MaterialTheme.typography.labelSmall, maxLines = 1)
                }
            }
        }
    }
}
