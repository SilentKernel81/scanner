package com.cardscanner.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.Button
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExposedDropdownMenuAnchorType
import androidx.compose.material3.ExposedDropdownMenuBox
import androidx.compose.material3.ExposedDropdownMenuDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.cardscanner.ScannerViewModel
import com.cardscanner.ai.CardIdentifier
import com.cardscanner.ai.Provider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

@Composable
fun SettingsScreen(viewModel: ScannerViewModel, onBack: () -> Unit) {
    val settings by viewModel.settings.collectAsStateWithLifecycle()
    val scope = rememberCoroutineScope()
    var localModels by remember { mutableStateOf<List<String>>(emptyList()) }
    var localStatus by remember { mutableStateOf<String?>(null) }
    val provider = settings.provider

    Column(Modifier.fillMaxSize().safeDrawingPadding().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") }
            Text("Settings", style = MaterialTheme.typography.titleLarge)
        }

        Text("Vision AI", style = MaterialTheme.typography.titleMedium)
        Dropdown("Provider", provider.label, Provider.entries.map { it.label }) {
            viewModel.updateSettings(settings.copy(provider = Provider.entries[it]))
        }

        var listedModels by remember(provider) { mutableStateOf<List<String>>(emptyList()) }
        val modelChoices = when {
            provider == Provider.LOCAL && localModels.isNotEmpty() -> localModels
            listedModels.isNotEmpty() -> listedModels
            else -> provider.models
        }
        Dropdown("Model", settings.model(), modelChoices) {
            viewModel.updateSettings(settings.copy(models = settings.models + (provider to modelChoices[it])))
        }
        OutlinedTextField(
            value = settings.models[provider].orEmpty(),
            onValueChange = { viewModel.updateSettings(settings.copy(models = settings.models + (provider to it.trim()))) },
            label = { Text("Other model (optional)") },
            placeholder = { Text(provider.models.first()) },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )

        if (provider.needsKey) {
            OutlinedTextField(
                value = settings.apiKey(),
                onValueChange = { viewModel.updateSettings(settings.copy(apiKeys = settings.apiKeys + (provider to it.trim()))) },
                label = { Text("${provider.label} API key") },
                singleLine = true,
                visualTransformation = PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                modifier = Modifier.fillMaxWidth(),
            )
            if (provider == Provider.OPENROUTER) {
                var listStatus by remember { mutableStateOf<String?>(null) }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    OutlinedButton(onClick = {
                        listStatus = "Loading…"
                        scope.launch {
                            listStatus = try {
                                listedModels = withContext(Dispatchers.IO) { CardIdentifier.openRouterModels(CardIdentifier.httpClient()) }
                                "${listedModels.size} models read images (free ones first)"
                            } catch (e: Exception) {
                                "Could not load the list: ${e.message}"
                            }
                        }
                    }) { Text("List models") }
                    listStatus?.let { Text(it, Modifier.padding(start = 12.dp), style = MaterialTheme.typography.bodySmall) }
                }
                Text("Models ending in :free cost nothing: 20 requests a minute, 50 a day (1,000 a day once you have bought \$10 of credits). A card takes 2 requests with the foil check.",
                    style = MaterialTheme.typography.bodySmall)
            }
        } else {
            OutlinedTextField(
                value = settings.localUrl,
                onValueChange = { viewModel.updateSettings(settings.copy(localUrl = it.trim())) },
                label = { Text("Server address") },
                placeholder = { Text("http://192.168.1.10:11434") },
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
                modifier = Modifier.fillMaxWidth(),
            )
            Row(verticalAlignment = Alignment.CenterVertically) {
                OutlinedButton(onClick = {
                    localStatus = "Connecting…"
                    scope.launch {
                        localStatus = try {
                            localModels = withContext(Dispatchers.IO) { CardIdentifier.localModels(settings.localUrl, CardIdentifier.httpClient()) }
                            "Connected: ${localModels.size} models"
                        } catch (e: Exception) {
                            "Not reachable: ${e.message}"
                        }
                    }
                }) { Text("Test / list models") }
                localStatus?.let { Text(it, Modifier.padding(start = 12.dp), style = MaterialTheme.typography.bodySmall) }
            }
        }

        HorizontalDivider()
        CardDataSection(viewModel)

        HorizontalDivider()
        Text("Scanning", style = MaterialTheme.typography.titleMedium)
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("Foil check (★/• marker)")
                Text("A second, small AI request per card", style = MaterialTheme.typography.bodySmall)
            }
            Switch(settings.detectFoil, { viewModel.updateSettings(settings.copy(detectFoil = it)) })
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("Add confirmed cards automatically")
                Text("Cards matched by set + number go into the inventory right away (Undo in the list); uncertain ones go to the review queue and scanning goes on. Off: every card waits for Add",
                    style = MaterialTheme.typography.bodySmall)
            }
            Switch(settings.autoAdd, { viewModel.updateSettings(settings.copy(autoAdd = it)) })
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Capture sound", Modifier.weight(1f))
            Switch(settings.sounds, { viewModel.updateSettings(settings.copy(sounds = it)) })
        }
        NumberField("Minimum sharpness", settings.minSharpness,
            "Cards below it are not auto-captured. Lower it if cards never become Ready (see the live value under the camera).") {
            viewModel.updateSettings(settings.copy(minSharpness = it))
        }
        NumberField("Still frames before capture", settings.stableFrames, "Consecutive still, sharp frames.") {
            viewModel.updateSettings(settings.copy(stableFrames = it.coerceIn(1, 60)))
        }
        // Every change is saved as it is made; Save just closes the page
        Button(onClick = onBack, Modifier.fillMaxWidth()) { Text("Save") }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun Dropdown(label: String, value: String, options: List<String>, onSelect: (Int) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    ExposedDropdownMenuBox(expanded, { expanded = it }) {
        OutlinedTextField(
            value = value, onValueChange = {}, readOnly = true, label = { Text(label) },
            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded) },
            modifier = Modifier.fillMaxWidth().menuAnchor(ExposedDropdownMenuAnchorType.PrimaryNotEditable),
        )
        ExposedDropdownMenu(expanded, { expanded = false }) {
            options.forEachIndexed { index, option ->
                DropdownMenuItem(text = { Text(option) }, onClick = { onSelect(index); expanded = false })
            }
        }
    }
}

@Composable
private fun NumberField(label: String, value: Int, help: String, onChange: (Int) -> Unit) {
    var text by remember(value) { mutableStateOf(value.toString()) }
    OutlinedTextField(
        value = text,
        onValueChange = { new -> text = new.filter { it.isDigit() }.take(5); text.toIntOrNull()?.let(onChange) },
        label = { Text(label) },
        supportingText = { Text(help) },
        singleLine = true,
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
        modifier = Modifier.fillMaxWidth(),
    )
}

/** Offline card data: download / update / delete Scryfall's card list */
@Composable
private fun CardDataSection(viewModel: ScannerViewModel) {
    val state by viewModel.cardDataState.collectAsStateWithLifecycle()
    androidx.compose.runtime.LaunchedEffect(state.printings) { if (state.printings != null) viewModel.checkCardDataUpdate() }
    Text("Card data (offline)", style = MaterialTheme.typography.titleMedium)
    Text(
        if (state.printings != null) "%,d printings from Scryfall, %s".format(state.printings, state.updatedAt?.take(10).orEmpty()) +
            (if (state.updateAvailable == true) " - newer data available" else "")
        else "Not downloaded: cards are looked up on Scryfall's website. With the data (~80 MB download) lookups work offline and faster; card images and new cards still need the internet.",
        style = MaterialTheme.typography.bodySmall,
    )
    state.message?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.tertiary) }
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
        OutlinedButton(onClick = viewModel::downloadCardData, enabled = !state.busy) {
            Text(if (state.printings == null) "Download" else "Update")
        }
        if (state.printings != null) OutlinedButton(onClick = viewModel::deleteCardData, enabled = !state.busy) { Text("Delete") }
        if (state.busy) androidx.compose.material3.CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp)
    }
    if (state.busy) Text("Keep the app open until it is done.", style = MaterialTheme.typography.bodySmall)
}
