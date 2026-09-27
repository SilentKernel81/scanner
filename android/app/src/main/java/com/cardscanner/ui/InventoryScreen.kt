package com.cardscanner.ui

import androidx.compose.foundation.clickable
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

/** The saved inventory: totals, export (CSV / Moxfield), edit or remove entries */
@Composable
fun InventoryScreen(viewModel: ScannerViewModel, onBack: () -> Unit) {
    val entries by viewModel.inventoryEntries.collectAsStateWithLifecycle()
    var filter by remember { mutableStateOf("") }
    var editing by remember { mutableStateOf<InventoryEntry?>(null) }
    var confirmClear by remember { mutableStateOf(false) }
    val shown = entries.filter { filter.isBlank() || Scryfall.searchKey(it.name)!!.contains(Scryfall.searchKey(filter)!!) }

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
        Row(Modifier.padding(horizontal = 12.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            ExportFormat.entries.forEach { format ->
                OutlinedButton(onClick = { viewModel.export(format) }, enabled = entries.isNotEmpty()) {
                    Icon(Icons.Default.Share, null)
                    Text("Export ${format.label}", Modifier.padding(start = 6.dp))
                }
            }
        }
        if (entries.size > 5) {
            OutlinedTextField(filter, { filter = it }, Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
                placeholder = { Text("Filter by name") }, singleLine = true)
        }
        if (entries.isEmpty()) {
            Text("No cards yet. Confirmed scans are added automatically; the others with Add in the scan list.",
                Modifier.padding(24.dp), color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        LazyColumn(Modifier.fillMaxSize()) {
            items(shown, key = { it.id }) { entry ->
                EntryRow(entry) { editing = entry }
                HorizontalDivider()
            }
        }
    }

    editing?.let { entry -> EditDialog(entry, onDismiss = { editing = null },
        onSave = { quantity, condition, finish -> viewModel.updateEntry(entry.id, quantity, condition, finish); editing = null },
        onDelete = { viewModel.deleteEntry(entry.id); editing = null }) }

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
private fun EditDialog(entry: InventoryEntry, onDismiss: () -> Unit, onSave: (Int, String, Finish) -> Unit, onDelete: () -> Unit) {
    var quantity by remember { mutableIntStateOf(entry.quantity) }
    var condition by remember { mutableStateOf(entry.condition) }
    var finish by remember { mutableStateOf(entry.finish) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(entry.name) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("${entry.setName} (${entry.setCode}) #${entry.number}", style = MaterialTheme.typography.bodySmall)
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
