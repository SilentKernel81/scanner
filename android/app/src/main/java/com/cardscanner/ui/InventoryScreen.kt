package com.cardscanner.ui

import androidx.compose.foundation.clickable
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.automirrored.filled.Sort
import androidx.compose.material.icons.filled.FileOpen
import androidx.compose.material3.Button
import androidx.compose.material3.Surface
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import com.cardscanner.scryfall.Printing
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.DeleteSweep
import androidx.compose.material.icons.filled.Remove
import androidx.compose.material.icons.filled.Share
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import coil3.compose.AsyncImage
import com.cardscanner.ScannerViewModel
import com.cardscanner.inventory.CONDITIONS
import com.cardscanner.inventory.ExportFormat
import com.cardscanner.inventory.Finish
import com.cardscanner.inventory.InventoryEntry
import com.cardscanner.scryfall.Scryfall

/** Orders of the inventory list */
private enum class Sort(val label: String, val comparator: Comparator<InventoryEntry>) {
    NEWEST("Newest", compareByDescending<InventoryEntry> { it.timestamp }.thenByDescending { it.id }),
    OLDEST("Oldest", compareBy<InventoryEntry> { it.timestamp }.thenBy { it.id }),
    PRICE("Price", compareByDescending<InventoryEntry> { it.priceUsd }.thenBy { it.name }),
    QUANTITY("Quantity", compareByDescending<InventoryEntry> { it.quantity }.thenBy { it.name }),
    NAME("Name", compareBy<InventoryEntry> { it.name }),
}

/** The saved inventory: totals, sort / filter, export (CSV / Moxfield), import, edit or remove entries */
@Composable
fun InventoryScreen(viewModel: ScannerViewModel, onBack: () -> Unit) {
    val entries by viewModel.inventoryEntries.collectAsStateWithLifecycle()
    val importStatus by viewModel.importStatus.collectAsStateWithLifecycle()
    var filter by remember { mutableStateOf("") }
    var sort by rememberSaveable { mutableStateOf(Sort.NEWEST) }
    var editing by remember { mutableStateOf<InventoryEntry?>(null) }
    var changing by remember { mutableStateOf<InventoryEntry?>(null) }
    var confirmClear by remember { mutableStateOf(false) }
    val shown = entries.filter { filter.isBlank() || Scryfall.searchKey(it.name)!!.contains(Scryfall.searchKey(filter)!!) }
        .sortedWith(sort.comparator)
    // CSV files: this app's / the Python scanner's export, or Moxfield's
    val pickCsv = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri -> uri?.let(viewModel::importCsv) }

    Column(Modifier.fillMaxSize().safeDrawingPadding()) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") }
            Column(Modifier.weight(1f)) {
                Text("Inventory", style = MaterialTheme.typography.titleLarge)
                Text("${entries.sumOf { it.quantity }} cards · ${entries.size} entries · $%.2f".format(entries.sumOf { it.priceUsd * it.quantity }),
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            IconButton(onClick = { confirmClear = true }, enabled = entries.isNotEmpty()) { Icon(Icons.Default.DeleteSweep, "Clear inventory") }
        }
        FlowRow(Modifier.padding(horizontal = 12.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            ExportFormat.entries.forEach { format ->
                OutlinedButton(onClick = { viewModel.export(format) }, enabled = entries.isNotEmpty(),
                    contentPadding = PaddingValues(horizontal = 12.dp)) {
                    Icon(Icons.Default.Share, null, Modifier.size(18.dp))
                    Text(format.label, Modifier.padding(start = 6.dp))
                }
            }
            OutlinedButton(onClick = { pickCsv.launch(arrayOf("text/*", "application/csv", "application/vnd.ms-excel", "application/octet-stream")) },
                contentPadding = PaddingValues(horizontal = 12.dp)) {
                Icon(Icons.Default.FileOpen, null, Modifier.size(18.dp))
                Text("Import CSV", Modifier.padding(start = 6.dp))
            }
        }
        importStatus?.let { status ->
            Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                Text(status, Modifier.weight(1f), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.tertiary)
                TextButton(onClick = viewModel::clearImportStatus) { Text("OK") }
            }
        }
        if (entries.size > 1) {
            Row(Modifier.horizontalScroll(rememberScrollState()).padding(horizontal = 12.dp),
                horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.AutoMirrored.Filled.Sort, "Sort", Modifier.size(18.dp))
                Sort.entries.forEach { FilterChip(sort == it, { sort = it }, label = { Text(it.label) }) }
            }
        }
        if (entries.size > 5) {
            OutlinedTextField(filter, { filter = it }, Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
                placeholder = { Text("Filter by name") }, singleLine = true)
        }
        if (entries.isEmpty()) {
            Text("No cards yet. Confirmed scans are added automatically; uncertain ones wait in the review queue. Or import a CSV (this app, the Python scanner or Moxfield).",
                Modifier.padding(24.dp), color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        // A new order or filter starts at the top (a keyed list otherwise keeps the item that was
        // on top in view, and the new first ones are hidden above it)
        val listState = rememberLazyListState()
        LaunchedEffect(sort, filter) { listState.scrollToItem(0) }
        // An entry that moves to the top (a new printing, an import) stays in view when the list
        // was at the top; the list then shows it one item down
        LaunchedEffect(shown.firstOrNull()?.id) { if (listState.firstVisibleItemIndex <= 1) listState.scrollToItem(0) }
        LazyColumn(Modifier.fillMaxSize(), state = listState) {
            items(shown, key = { it.id }) { entry ->
                EntryRow(entry) { editing = entry }
                HorizontalDivider()
            }
        }
    }

    editing?.let { entry -> EditDialog(entry, onDismiss = { editing = null },
        onSave = { quantity, condition, finish -> viewModel.updateEntry(entry.id, quantity, condition, finish); editing = null },
        onDelete = { viewModel.deleteEntry(entry.id); editing = null },
        onChangePrinting = { editing = null; changing = entry }) }

    changing?.let { entry ->
        ChangePrintingDialog(viewModel, entry, onDismiss = { changing = null },
            onChosen = { viewModel.changePrinting(entry.id, it); changing = null })
    }

    if (confirmClear) {
        AlertDialog(
            onDismissRequest = { confirmClear = false },
            title = { Text("Clear the inventory?") },
            text = { Text("All ${entries.sumOf { it.quantity }} cards are removed. Export first to keep a copy.") },
            confirmButton = { TextButton(onClick = { viewModel.clearInventory(); confirmClear = false }) { Text("Clear") } },
            dismissButton = { TextButton(onClick = { confirmClear = false }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun EntryRow(entry: InventoryEntry, onClick: () -> Unit) {
    Row(Modifier.fillMaxWidth().clickable(onClick = onClick).padding(horizontal = 12.dp, vertical = 8.dp),
        horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.CenterVertically) {
        AsyncImage(entry.imageUrl, entry.name, Modifier.height(64.dp).width(46.dp).clip(RoundedCornerShape(3.dp)))
        Column(Modifier.weight(1f)) {
            Text(entry.name, fontWeight = FontWeight.SemiBold)
            Text("${entry.setName} (${entry.setCode}) #${entry.number}", style = MaterialTheme.typography.bodySmall)
            Text(listOfNotNull(entry.finish.label.takeIf { entry.finish != Finish.REGULAR }, entry.condition).joinToString(" · "),
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Column(horizontalAlignment = Alignment.End) {
            Text("×${entry.quantity}", fontWeight = FontWeight.SemiBold)
            Text("$%.2f".format(entry.priceUsd * entry.quantity), style = MaterialTheme.typography.bodySmall)
        }
    }
}

@Composable
private fun EditDialog(entry: InventoryEntry, onDismiss: () -> Unit, onSave: (Int, String, Finish) -> Unit, onDelete: () -> Unit,
                       onChangePrinting: () -> Unit) {
    var quantity by remember { mutableIntStateOf(entry.quantity) }
    var condition by remember { mutableStateOf(entry.condition) }
    var finish by remember { mutableStateOf(entry.finish) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(entry.name) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("${entry.setName} (${entry.setCode}) #${entry.number}", Modifier.weight(1f), style = MaterialTheme.typography.bodySmall)
                    TextButton(onClick = onChangePrinting) { Text("Change printing") }
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("Quantity", Modifier.weight(1f))
                    IconButton(onClick = { if (quantity > 1) quantity-- }) { Icon(Icons.Default.Remove, "Fewer") }
                    Text("$quantity", style = MaterialTheme.typography.titleMedium)
                    IconButton(onClick = { quantity++ }) { Icon(Icons.Default.Add, "More") }
                }
                Text("Finish", style = MaterialTheme.typography.labelLarge)
                FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    Finish.entries.forEach { FilterChip(finish == it, { finish = it }, label = { Text(it.label) }) }
                }
                Text("Condition", style = MaterialTheme.typography.labelLarge)
                FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    CONDITIONS.forEach { FilterChip(condition == it, { condition = it }, label = { Text(it) }) }
                }
            }
        },
        confirmButton = { TextButton(onClick = { onSave(quantity, condition, finish) }) { Text("Save") } },
        dismissButton = {
            Row {
                TextButton(onClick = onDelete) { Text("Remove", color = MaterialTheme.colorScheme.error) }
                Spacer(Modifier.width(8.dp))
                TextButton(onClick = onDismiss) { Text("Cancel") }
            }
        },
    )
}

/** Pick another printing for an entry (a misread card): search prefilled with the entry's card */
@Composable
private fun ChangePrintingDialog(viewModel: ScannerViewModel, entry: InventoryEntry, onDismiss: () -> Unit, onChosen: (Printing) -> Unit) {
    val search = rememberPrintingSearch(viewModel, entry.name, "", "")
    var selected by remember { mutableStateOf<Printing?>(null) }
    LaunchedEffect(Unit) { search.search() }
    Dialog(onDismissRequest = onDismiss, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Surface(Modifier.fillMaxSize()) {
            Column(Modifier.fillMaxSize().safeDrawingPadding()) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    IconButton(onClick = onDismiss) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Cancel") }
                    Column(Modifier.weight(1f)) {
                        Text("Change printing", style = MaterialTheme.typography.titleLarge)
                        Text("Now: ${entry.setName} (${entry.setCode}) #${entry.number}", style = MaterialTheme.typography.bodySmall)
                    }
                    Button(onClick = { selected?.let(onChosen) }, enabled = selected != null, modifier = Modifier.padding(end = 8.dp)) { Text("Use") }
                }
                PrintingSearchGrid(search, selected?.id, { selected = it }, Modifier.fillMaxSize()) {
                    selected?.let { Text("${it.name} · ${it.setName} (${it.setCode}) #${it.collectorNumber}", fontWeight = FontWeight.SemiBold) }
                }
            }
        }
    }
}
