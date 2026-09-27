package com.cardscanner.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import coil3.compose.AsyncImage
import com.cardscanner.ScannerViewModel
import com.cardscanner.scryfall.Printing
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch

/** A printing search (name, and set + number for one printing) and its results */
class PrintingSearchState(private val viewModel: ScannerViewModel, private val scope: CoroutineScope,
                          name: String, setCode: String, number: String) {
    var name by mutableStateOf(name)
    var setCode by mutableStateOf(setCode)
    var number by mutableStateOf(number)
    var results by mutableStateOf<List<Printing>>(emptyList())
    var searching by mutableStateOf(false)
    var status by mutableStateOf<String?>(null)

    /** Search; `onDone` gets the results (e.g. to pick the only one) */
    fun search(onDone: (List<Printing>) -> Unit = {}) {
        searching = true
        status = null
        scope.launch {
            results = try {
                viewModel.searchPrintings(name, setCode, number).also { if (it.isEmpty()) status = "Nothing found" }
            } catch (e: Exception) {
                status = "Search failed: ${e.message}"
                emptyList()
            }
            searching = false
            onDone(results)
        }
    }
}

@Composable
fun rememberPrintingSearch(viewModel: ScannerViewModel, name: String, setCode: String, number: String): PrintingSearchState {
    val scope = rememberCoroutineScope()
    return remember { PrintingSearchState(viewModel, scope, name, setCode, number) }
}

/**
 * The search fields and a grid of the printings found (tap one to choose it), under `header`
 * (all in one scrolling grid)
 */
@Composable
fun PrintingSearchGrid(state: PrintingSearchState, selectedId: String?, onSelect: (Printing) -> Unit, modifier: Modifier = Modifier,
                       header: @Composable ColumnScope.() -> Unit = {}) {
    LazyVerticalGrid(GridCells.Adaptive(96.dp), modifier.padding(horizontal = 12.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        item(span = { GridItemSpan(maxLineSpan) }) {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                header()
                OutlinedTextField(state.name, { state.name = it }, Modifier.fillMaxWidth(), label = { Text("Card name") }, singleLine = true,
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search), keyboardActions = KeyboardActions(onSearch = { state.search() }))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                    OutlinedTextField(state.setCode, { state.setCode = it }, Modifier.width(90.dp), label = { Text("Set") }, singleLine = true)
                    OutlinedTextField(state.number, { state.number = it }, Modifier.width(100.dp), label = { Text("Number") }, singleLine = true)
                    OutlinedButton(onClick = { state.search() }, enabled = !state.searching) { Text("Search") }
                    if (state.searching) CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp)
                }
                state.status?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
                if (state.results.size > 1) Text("${state.results.size} printings - tap the one you have", style = MaterialTheme.typography.bodySmall)
            }
        }
        items(state.results, key = { it.id }) { p ->
            val chosen = p.id == selectedId
            Column(Modifier.clickable { onSelect(p) }
                .then(if (chosen) Modifier.border(BorderStroke(3.dp, MaterialTheme.colorScheme.primary), RoundedCornerShape(6.dp)) else Modifier)
                .padding(3.dp)) {
                AsyncImage(p.imageUrl, p.name, Modifier.fillMaxWidth().aspectRatio(63f / 88f).clip(RoundedCornerShape(4.dp)))
                Text("${p.setCode} #${p.collectorNumber}", style = MaterialTheme.typography.labelSmall, maxLines = 1)
            }
        }
    }
}
