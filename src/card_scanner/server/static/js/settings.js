// The settings page (templates/settings.html): every setting of the program in five categories,
// each saved the moment it changes. Loaded after audio.js and common.js; it has its own `$`,
// `api` and connection (scanner.js, collection.js and library.js are for other pages and declare
// the same names).
//
// Per camera (Cameras): the connection to the server is opened for the chosen camera
// (?station=), so that the events of the camera's settings (auto-add, focus, rotation, debug
// trace) reach that camera's desk, as they do from its own page. The rest is the server's.

const $ = id => document.getElementById(id);
const plural = (count, word, many = word + 's') => `${count} ${count === 1 ? word : many}`;

async function api(path, options = {}) {
    // JSON request; shows the server's error and returns null when it fails
    if (options.body && typeof options.body !== 'string' && !(options.body instanceof FormData)) {
        options = {...options, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(options.body)};
    }
    try {
        const response = await fetch(path, options);
        const data = await response.json();
        if (data.success === false || (response.status >= 400 && data.error)) {
            notify(data.error || data.message || 'Request failed', 'error');
            return null;
        }
        return data;
    } catch (error) {
        notify(`Request failed: ${error.message}`, 'error');
        return null;
    }
}

function remember(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* storage blocked */ }
}

function recall(key, fallback) {
    try { return localStorage.getItem(key) || fallback; } catch (e) { return fallback; }
}

const CATEGORIES = ['cameras', 'sounds', 'reading', 'data', 'server'];

// ============================================================================
// Categories
// ============================================================================

function showCategory(name, remembered = true) {
    if (!CATEGORIES.includes(name)) name = 'cameras';
    CATEGORIES.forEach(category => {
        $('cat-' + category).hidden = category !== name;
    });
    document.querySelectorAll('#settings-nav button').forEach(button =>
        button.classList.toggle('is-active', button.dataset.category === name));
    if (remembered) {
        try { history.replaceState(null, '', location.pathname + location.search + '#' + name); } catch (e) { /* fine */ }
        remember('settingsCategory', name);
    }
}

// The old addresses: the scanner page's drawer (#settings) was cameras, sound and the AI; the
// collection page's was backups and storage
function categoryFromAddress() {
    const name = location.hash.replace('#', '');
    if (CATEGORIES.includes(name)) return name;
    return recall('settingsCategory', 'cameras');
}

// ============================================================================
// The connection (one for the chosen camera) and the server's answers
// ============================================================================

let socket = null;
let cameraId = null;   // the camera chosen under Cameras

function connect() {
    if (socket) socket.disconnect();
    socket = io(cameraId ? {query: {station: cameraId}} : {});

    socket.on('error', data => {
        if (data && data.locked) return unlockAndReconnect();
        notify((data && data.message) || 'The server refused that', 'error');
    });

    // Camera
    socket.on('fast_scan_toggled', () => {});
    socket.on('camera_rotation_updated', data => {
        $('camera-rotation').value = String(data.rotation);
        notify(`Camera image rotated ${data.rotation}°` + (data.fixed_area_off ? ' - fixed area off, draw it again' : ''), 'info');
        loadCameraSettings();
    });
    socket.on('refocus_every_updated', data => { $('refocus-every').value = String(data.captures); });
    socket.on('refocus_on_drop_updated', data => showRefocusOnDrop(data.ratio));
    socket.on('fixed_area_updated', () => loadCameraSettings());

    // Card reading
    socket.on('ocr_toggled', data => applyOcrState(data.enabled, data.installed));
    socket.on('debug_mode_toggled', data => applyDebugMode(data.enabled, data.running, data.debugger));
    socket.on('ai_provider_set', data => {
        activeProvider = data.provider; activeModel = data.model;
        notify(data.message || 'AI provider changed', 'success');
        loadPromptSummary();
    });
    socket.on('ai_credential_saved', onCredentialSaved);

    // Card data
    socket.on('database_update_progress', data => { $('database-progress').textContent = data.message; });
    socket.on('database_update_complete', data => {
        $('update-database').disabled = false;
        $('update-database').textContent = 'Update card database';
        $('database-progress').textContent = `Updated: ${data.total_cards.toLocaleString()} cards loaded.`;
        notify('Card data updated', 'success');
        loadDatabaseState();
    });
    socket.on('database_update_error', data => {
        $('update-database').disabled = false;
        $('update-database').textContent = 'Update card database';
        $('database-progress').textContent = `The update failed: ${data.message}`;
        notify(`Card data update failed: ${data.message}`, 'error');
    });
    socket.on('database_rebuild_progress', data => { $('rebuild-progress').textContent = data.message; });
    socket.on('database_rebuild_complete', data => {
        $('rebuild-database').disabled = false;
        $('rebuild-database').textContent = 'Rebuild database schema';
        $('rebuild-progress').textContent = `Rebuilt: ${data.cards_imported.toLocaleString()} cards migrated (${data.schema_type}).`;
        notify('Database rebuilt', 'success');
    });
    socket.on('database_rebuild_error', data => {
        $('rebuild-database').disabled = false;
        $('rebuild-database').textContent = 'Rebuild database schema';
        $('rebuild-progress').textContent = `The rebuild failed: ${data.message}`;
        notify(`Database rebuild failed: ${data.message}`, 'error');
    });
}

// ============================================================================
// Cameras
// ============================================================================

let stations = [];

function stationSetupUrl(station) {
    // The station's own page (which camera, its name, its server). A station on this computer
    // connects from a loopback address: that is this page's host
    if (!station.setup || !station.setup.port) return '';
    const loopback = !station.setup.address || ['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(station.setup.address);
    const host = loopback ? window.location.hostname : station.setup.address;
    return `http://${host.includes(':') ? '[' + host + ']' : host}:${station.setup.port}`;
}

async function loadStations(keep = true) {
    const data = await api('/api/stations');
    if (!data) return;
    stations = data.stations;
    $('server-origin').textContent = window.location.origin;
    const select = $('camera-select');
    select.innerHTML = stations.map(station =>
        `<option value="${escapeHtml(station.id)}">${escapeHtml(station.name)}${station.connected ? '' : ' (not connected)'}</option>`).join('');
    $('camera-empty').hidden = stations.length > 0;
    $('camera-settings').hidden = stations.length === 0;
    select.hidden = stations.length === 0;
    document.querySelector('label[for="camera-select"]').hidden = stations.length === 0;
    if (!stations.length) { cameraId = null; return; }
    const wanted = (keep && cameraId) || new URLSearchParams(location.search).get('camera') || recall('settingsCamera', '');
    const chosen = stations.find(station => station.id === wanted)
        || stations.find(station => station.connected) || stations[0];
    select.value = chosen.id;
    if (cameraId !== chosen.id) {
        cameraId = chosen.id;
        connect();
    }
    showCamera();
}

function currentStation() {
    return stations.find(station => station.id === cameraId) || null;
}

function showCamera() {
    const station = currentStation();
    if (!station) return;
    $('camera-name').value = station.name;
    $('camera-location').value = station.location || '';
    $('camera-status').textContent =
        `${station.connected ? 'Connected' : 'Not connected'} · ${plural(station.scanned, 'scanned card')} · `
        + `${station.captures} captured in all · last seen ${station.last_seen || 'never'}`
        + (station.camera_error ? ` · camera: ${station.camera_error}` : '');
    $('camera-page').href = '/scan/' + encodeURIComponent(station.id);
    const url = stationSetupUrl(station);
    $('camera-setup').hidden = !url;
    if (url) $('camera-setup').href = url;
    const first = stations[0];
    $('edit-prompts').href = '/scan/' + encodeURIComponent((station || first).id) + '#prompts';
    loadCameraSettings();
}

let cameraRequest = 0;

async function loadCameraSettings() {
    const mine = ++cameraRequest;
    const query = cameraId ? '?station=' + encodeURIComponent(cameraId) : '';
    const data = await api('/api/scan_settings' + query);
    if (!data || mine !== cameraRequest) return;
    $('toggle-auto-add').checked = Boolean(data.auto_add);
    $('toggle-autofocus').checked = Boolean(data.autofocus);
    $('refocus-every').value = String(data.refocus_every);
    showRefocusOnDrop(data.refocus_on_drop);
    $('camera-rotation').value = String(data.camera_rotation || 0);
    $('toggle-debug-trace').checked = Boolean(data.debug_trace);
    $('fixed-area-state').textContent = data.fixed_area_enabled
        ? 'Fixed area: on. Cards are judged by the area drawn on the camera\'s page, not by their outline. Draw or turn it off there.'
        : 'Fixed area: off. Cards are found by their outline; the area for sleeved cards is drawn on the camera\'s page.';
    $('location-options').innerHTML = (data.locations || [])
        .map(location => `<option value="${escapeHtml(location)}"></option>`).join('');
    // The server's own settings come with the same answer
    applySound(data.sound);
    applyOcrState(data.ocr_first, data.ocr_installed);
    applyDebugMode(data.debug_mode, data.debug_mode_running, data.debugger_running);
}

function showRefocusOnDrop(ratio) {
    // A value that is not one of the choices (set in config.yaml) is shown as it is
    const select = $('refocus-on-drop');
    const value = String(ratio || 0);
    if (![...select.options].some(option => option.value === value)) {
        select.add(new Option(`When they are ${Math.round(ratio * 100)}% as sharp (config.yaml)`, value));
    }
    select.value = value;
}

async function saveStation(change) {
    const station = currentStation();
    if (!station) return;
    const data = await api('/api/stations/' + encodeURIComponent(station.id), {method: 'PUT', body: change});
    if (data) {
        await loadStations();
        notify('Saved', 'success');
    }
}

async function forgetCamera() {
    const station = currentStation();
    if (!station) return;
    const ok = await confirmDialog({
        title: `Forget ${station.name}?`,
        message: 'It is removed from the list, with the cards it scanned that are not in the collection yet and the cards it has waiting for review. Cards already in the collection stay. It comes back, empty, if it connects or sends a card again.',
        confirmText: 'Forget and delete its cards', danger: true
    });
    if (!ok) return;
    const data = await api('/api/stations/' + encodeURIComponent(station.id), {method: 'DELETE'});
    if (!data) return;
    notify(`Forgotten: ${plural(data.cards, 'scanned card')} and ${plural(data.reviews, 'review card')} removed`, 'success');
    cameraId = null;
    await loadStations(false);
}

function bindCameras() {
    $('camera-select').addEventListener('change', event => {
        cameraId = event.target.value;
        remember('settingsCamera', cameraId);
        connect();
        showCamera();
    });
    $('camera-name').addEventListener('change', event => {
        const name = event.target.value.trim();
        if (name) saveStation({name}); else showCamera();
    });
    $('camera-location').addEventListener('change', event => saveStation({location: event.target.value.trim()}));
    $('camera-undo').addEventListener('click', () => {
        // Answered with inventory_updated (undone) or an error
        socket.emit('undo_last_add', {station: cameraId});
        notify('Asked it to take back the last card added', 'info');
    });
    $('camera-forget').addEventListener('click', forgetCamera);

    $('toggle-auto-add').addEventListener('change', event => {
        socket.emit('toggle_fast_scan', {enabled: event.target.checked});
        notify(`Cards are added ${event.target.checked ? 'automatically' : 'after you confirm them'}`, 'success');
    });
    $('toggle-autofocus').addEventListener('change', event => {
        socket.emit('set_autofocus', {enabled: event.target.checked});
        notify(`Autofocus ${event.target.checked ? 'on' : 'off'}`, 'success');
    });
    $('refocus-every').addEventListener('change', event =>
        socket.emit('set_refocus_every', {captures: parseInt(event.target.value) || 0}));
    $('refocus-on-drop').addEventListener('change', event =>
        socket.emit('set_refocus_on_drop', {ratio: parseFloat(event.target.value)}));
    $('camera-rotation').addEventListener('change', event =>
        socket.emit('set_camera_rotation', {rotation: parseInt(event.target.value)}));
    $('toggle-debug-trace').addEventListener('change', event => {
        socket.emit('toggle_debug_trace', {enabled: event.target.checked});
        notify(`Debug trace ${event.target.checked ? 'on' : 'off'} for this camera`, 'success');
    });
}

// ============================================================================
// Sounds
// ============================================================================

function applySound(sound) {
    if (!sound) return;
    $('toggle-audio').checked = sound.enabled;
    $('audio-volume').value = sound.volume;
    $('volume-value').textContent = sound.volume + '%';
    $('toggle-sound-capture').checked = sound.capture;
    $('toggle-sound-added').checked = sound.added;
    audioManager.setEnabled(sound.enabled);
    audioManager.setVolume(sound.volume / 100);
    audioManager.setMuted('capture', !sound.capture);
    audioManager.setMuted('added', !sound.added);
}

function saveSound(change) {
    api('/api/sound', {method: 'POST', body: change});
}

function bindSounds() {
    $('toggle-audio').addEventListener('change', event => {
        audioManager.setEnabled(event.target.checked);
        saveSound({enabled: event.target.checked});
        if (event.target.checked) audioManager.playSuccess();
    });
    $('toggle-sound-capture').addEventListener('change', event => {
        audioManager.setMuted('capture', !event.target.checked);
        saveSound({capture: event.target.checked});
        if (event.target.checked) audioManager.playCapture();
    });
    $('toggle-sound-added').addEventListener('change', event => {
        audioManager.setMuted('added', !event.target.checked);
        saveSound({added: event.target.checked});
        if (event.target.checked) audioManager.playAdded();
    });
    $('audio-volume').addEventListener('input', event => {
        audioManager.setVolume(event.target.value / 100);
        $('volume-value').textContent = event.target.value + '%';
    });
    // Saved when the slider is let go, not on every step of the drag
    $('audio-volume').addEventListener('change', event => saveSound({volume: parseInt(event.target.value)}));
    $('audio-test').addEventListener('click', () => {
        audioManager.ensureAudioContext().then(() => {
            audioManager.playCapture();
            setTimeout(() => audioManager.playSuccess(), 400);
            setTimeout(() => audioManager.playNextReady(), 1000);
        });
    });
}

// ============================================================================
// Card reading: OCR, the vision AI
// ============================================================================

const OCR_DESC = $('ocr-desc').textContent;

function applyOcrState(enabled, installed) {
    const toggle = $('toggle-ocr');
    toggle.checked = Boolean(enabled) && Boolean(installed);
    toggle.disabled = !installed;
    $('ocr-desc').textContent = installed ? OCR_DESC
        : 'Not installed: needs Node.js 22+ and "npm install" in the ocr folder (scripts/deploy.sh does it)';
}

let availableModels = {};
let activeProvider = null, activeModel = null;
let aiCredentials = {};

const PROVIDER_INFO = {
    gemini: {name: 'Google Gemini', keyUrl: 'https://aistudio.google.com/app/apikey'},
    openai: {name: 'OpenAI', keyUrl: 'https://platform.openai.com/api-keys'},
    anthropic: {name: 'Anthropic', keyUrl: 'https://console.anthropic.com/settings/keys'},
    local: {name: 'Local AI'}
};
const providerInfo = provider => PROVIDER_INFO[provider] || {name: provider};

function populateModels(provider, selected = null) {
    const select = $('ai-model');
    select.innerHTML = '';
    const models = availableModels[provider];
    if (!models || !models.length) {
        select.add(new Option('No models available', ''));
        return;
    }
    models.forEach(model => select.add(new Option(model, model, false, model === selected)));
}

async function loadLocalModels(selected = null) {
    $('ai-model').innerHTML = '<option value="">Loading models from Ollama...</option>';
    try {
        const data = await (await fetch('/api/local_ai_models')).json();
        if (data.success && data.models && data.models.length) availableModels.local = data.models;
        else notify('Using the static model list (the local AI server is not available)', 'warning');
    } catch (error) {
        notify('Could not reach the local AI server', 'warning');
    }
    populateModels('local', selected);
}

async function loadAI() {
    try {
        availableModels = (await (await fetch('/api/ai_models')).json()).models || {};
        aiCredentials = await (await fetch('/api/ai_credentials')).json();
        const active = await (await fetch('/api/ai_provider')).json();
        if (active.provider) {
            activeProvider = active.provider; activeModel = active.model;
            $('ai-provider').value = active.provider;
            if (active.provider === 'local') await loadLocalModels(active.model);
            else populateModels(active.provider, active.model);
        }
        renderCredentialField($('ai-provider').value);
    } catch (error) {
        notify(`Could not load the AI settings: ${error.message}`, 'error');
    }
}

function hasCredential(provider) {
    return provider === 'local' || Boolean(aiCredentials[provider] && aiCredentials[provider].configured);
}

function renderCredentialField(provider) {
    const input = $('ai-credential'), label = $('ai-credential-label'), hint = $('ai-credential-hint');
    const info = providerInfo(provider), status = aiCredentials[provider] || {};
    input.value = '';
    if (provider === 'local') {
        label.textContent = 'Server address';
        input.type = 'text';
        input.value = status.endpoint || '';
        input.placeholder = 'http://localhost:11434/v1/chat/completions';
        hint.textContent = 'Ollama or another OpenAI-compatible server';
        return;
    }
    label.textContent = `${info.name} API key`;
    input.type = 'password';
    if (status.configured) {
        input.placeholder = `Saved: ${status.masked} - type a new key to replace it`;
        hint.textContent = `Key configured (${status.masked})`;
    } else {
        input.placeholder = 'Paste your API key';
        hint.innerHTML = `No key yet - <a href="${info.keyUrl}" target="_blank" rel="noopener">get one from ${escapeHtml(info.name)}</a>`;
    }
}

function applyProvider(provider) {
    // The provider's models, and the program switches to it; returning to the active provider keeps its model
    const keep = provider === activeProvider ? activeModel : null;
    const loaded = provider === 'local' ? loadLocalModels(keep) : Promise.resolve(populateModels(provider, keep));
    loaded.then(() => socket.emit('set_ai_provider', {provider, model: $('ai-model').value}));
}

async function saveCredential() {
    const provider = $('ai-provider').value, value = $('ai-credential').value.trim();
    if (!value) {
        if (provider === 'local' || !hasCredential(provider)) {
            notify(provider === 'local' ? 'Enter the server address' : 'Paste an API key first', 'warning');
            return;
        }
        const ok = await confirmDialog({
            title: `Remove the ${providerInfo(provider).name} key?`,
            message: 'Removes the key saved in the web interface. A key in the .env file (if any) is used again after a restart.',
            confirmText: 'Remove key', danger: true
        });
        if (!ok) return;
    }
    socket.emit('save_ai_credential', {provider, value});
}

function onCredentialSaved(data) {
    aiCredentials[data.provider] = data.status;
    const selected = $('ai-provider').value, info = providerInfo(data.provider);
    renderCredentialField(selected);
    notify(data.provider === 'local' ? 'Local AI address saved'
        : data.status.configured ? `${info.name} API key saved` : `${info.name} API key removed`, 'success');
    if (data.provider === selected && hasCredential(selected)) applyProvider(selected);
}

const PROMPT_SOURCES = {'built-in': 'built-in prompt', 'all': 'saved for all models', 'model': 'saved for this model'};

async function loadPromptSummary() {
    try {
        const data = await (await fetch('/api/prompts')).json();
        const custom = data.order.map(kind => data.prompts[kind]).filter(prompt => prompt.source !== 'built-in');
        $('prompt-summary').textContent = custom.length
            ? custom.map(prompt => `${prompt.label}: ${PROMPT_SOURCES[prompt.source]}`).join(' · ') + '. The editor is on a scanner page.'
            : 'What the AI is asked to read on each card - adjustable per model. The editor is on a scanner page.';
    } catch (error) { /* the summary is only a hint */ }
}

function bindReading() {
    $('toggle-ocr').addEventListener('change', event => socket.emit('toggle_ocr', {enabled: event.target.checked}));
    $('ai-provider').addEventListener('change', event => {
        const provider = event.target.value;
        renderCredentialField(provider);
        if (hasCredential(provider)) {
            applyProvider(provider);
        } else {
            populateModels(provider);
            $('ai-credential').focus();
            notify(`Enter your ${providerInfo(provider).name} API key to use it`, 'warning');
        }
    });
    $('ai-model').addEventListener('change', event =>
        socket.emit('set_ai_provider', {provider: $('ai-provider').value, model: event.target.value}));
    $('ai-credential').addEventListener('keydown', event => { if (event.key === 'Enter') saveCredential(); });
    $('save-credential').addEventListener('click', saveCredential);
    $('refresh-models').addEventListener('click', async () => {
        const provider = $('ai-provider').value;
        $('ai-model').innerHTML = '<option value="">Refreshing models...</option>';
        if (provider === 'local') {
            await loadLocalModels(activeModel);
        } else {
            try { availableModels = (await (await fetch('/api/ai_models')).json()).models || {}; } catch (e) { /* keep */ }
            populateModels(provider, provider === activeProvider ? activeModel : null);
        }
    });
}

// ============================================================================
// Collection and data: backups, card data, storage
// ============================================================================

async function loadBackups() {
    const data = await api('/api/backups');
    if (!data) return;
    renderBackups(data.backups);
    $('backup-every').value = String(data.schedule.every_hours);
    $('backup-keep').value = data.schedule.keep;
    $('backup-keep').disabled = !data.schedule.every_hours;
}

async function saveBackupSchedule(change) {
    const data = await api('/api/backups/schedule', {method: 'POST', body: change});
    if (!data) return loadBackups();
    $('backup-every').value = String(data.schedule.every_hours);
    $('backup-keep').value = data.schedule.keep;
    $('backup-keep').disabled = !data.schedule.every_hours;
    notify(data.schedule.every_hours ? 'Automatic backups saved' : 'Automatic backups switched off', 'success');
}

function renderBackups(backups) {
    $('backup-list').innerHTML = backups.map(backup => `
        <div class="backup-row" data-backup="${escapeHtml(backup.id)}" data-created="${escapeHtml(backup.created.slice(0, 16))}">
            <div>
                <div class="idea-name">${escapeHtml(backup.created.slice(0, 16))}${backup.automatic || backup.daily ? ' <span class="backup-auto">automatic</span>' : backup.uploaded ? ' <span class="backup-auto">uploaded</span>' : ''}</div>
                ${backup.note ? `<div class="idea-meta">${escapeHtml(backup.note)}</div>` : ''}
                <div class="idea-meta">${plural(backup.cards, 'card')} in the collection · ${backup.scanned} scanned · ${plural(backup.decks, 'deck')}</div>
            </div>
            <button type="button" class="btn btn-small" data-backup-action="restore">Restore</button>
            <a class="mini-btn" href="/api/backups/${encodeURIComponent(backup.id)}/download" download title="Download this backup as a zip file, to keep a copy elsewhere"><svg class="icon"><use href="#i-download"/></svg></a>
            <button type="button" class="mini-btn" data-backup-action="delete" title="Delete this backup"><svg class="icon"><use href="#i-trash"/></svg></button>
        </div>`).join('') || '<div class="hint">No backups yet.</div>';
}

async function createBackup() {
    const button = $('backup-create');
    button.disabled = true;
    const data = await api('/api/backups', {method: 'POST', body: {note: $('backup-note').value}});
    button.disabled = false;
    if (!data) return;
    $('backup-note').value = '';
    renderBackups(data.backups);
    notify(`Backup made: ${plural(data.backup.cards, 'card')}, ${data.backup.scanned} scanned, ${plural(data.backup.decks, 'deck')}`, 'success');
}

async function uploadBackup(file) {
    const button = $('backup-upload');
    button.disabled = true;
    button.textContent = 'Uploading...';
    const form = new FormData();
    form.append('file', file);
    const data = await api('/api/backups/upload', {method: 'POST', body: form});
    button.disabled = false;
    button.textContent = 'Upload a backup file';
    if (!data) return;
    renderBackups(data.backups);
    notify(`Backup of ${data.backup.created.slice(0, 16)} added to the list: ${plural(data.backup.cards, 'card')}, ${plural(data.backup.decks, 'deck')}`, 'success');
}

async function backupAction(row, action) {
    const id = row.dataset.backup, created = row.dataset.created;
    if (action === 'restore') {
        const ok = await confirmDialog({title: `Restore the backup of ${created}?`, confirmText: 'Restore', danger: true,
            message: 'The collection, the scanned cards and the decks go back to how they were then - everything '
                + 'changed since is replaced. What you have now is backed up first, so you can go back to it.'});
        if (!ok) return;
        const data = await api(`/api/backups/${id}/restore`, {method: 'POST'});
        if (!data) return loadBackups();
        renderBackups(data.backups);
        notify(`Backup of ${created} restored`, 'success');
    } else {
        const ok = await confirmDialog({title: `Delete the backup of ${created}?`, confirmText: 'Delete', danger: true,
            message: "Your cards are not changed. This can't be undone."});
        if (!ok) return;
        const data = await api(`/api/backups/${id}`, {method: 'DELETE'});
        if (data) renderBackups(data.backups);
    }
}

async function loadDatabaseState() {
    const data = await api('/api/stats');
    if (!data || !data.database) return;
    const state = data.database;
    const parts = [`${(state.total_cards || 0).toLocaleString()} cards in the card database`];
    if (state.updating) parts.push('an update is running');
    else if (state.update) parts.push(state.update);
    if (state.error) parts.push(`last update failed: ${state.error}`);
    $('database-state').textContent = parts.join(' · ');
    try {
        // What the update downloads depends on the game being scanned
        const games = await (await fetch('/api/games')).json();
        const game = games.games.find(item => item.id === games.active);
        if (game) {
            $('database-source-hint').textContent =
                `Downloads the latest ${game.label} card data${game.id === 'mtg' ? ' and prices' : ''} from ${game.source}`
                + (game.id === 'mtg' ? ' (a few minutes)' : ' (a few seconds)') + '. Scanning keeps working while it downloads.';
        }
    } catch (error) { /* the general text stays */ }
}

function formatBytes(bytes) {
    if (bytes >= 1073741824) return `${(bytes / 1073741824).toFixed(2)} GB`;
    return `${(bytes / 1048576).toFixed(bytes >= 10485760 ? 0 : 1)} MB`;
}

function showCaptureUsage(usage) {
    // The total, then each kind of file the cameras' captures leave on the server
    const kinds = [
        ['received', 'Pictures received from the cameras', 'scanned_cards, removed after a few days'],
        ['thumbnails', 'Thumbnails of the cards in the lists', 'data/captures'],
        ['large', 'Large copies kept with each capture', 'data/captures_full'],
        ['frames', 'Camera frames kept for training', 'data/frames'],
    ];
    $('capture-usage').innerHTML = `<div class="storage-total">${formatBytes(usage.total_bytes)}</div>`
        + kinds.map(([key, label, where]) => `<div class="storage-row" title="${escapeHtml(where)}"><span>${escapeHtml(label)}</span>`
            + `<span>${usage[key].count.toLocaleString()} files, ${formatBytes(usage[key].bytes)}</span></div>`).join('');
}

let compressWatch = null;   // the timer that asks how far the compression is, while it runs

function showCompress(info) {
    const state = info.compress || {};
    $('compress-copies').disabled = !!state.running;
    if (state.running) {
        $('compress-hint').textContent = `Compressing... ${state.done} of ${state.total || '?'} copies looked at.`;
        if (!compressWatch) compressWatch = setInterval(loadCaptureCopy, 2000);
        return;
    }
    if (compressWatch) {
        // It has just finished: say how it went, once
        clearInterval(compressWatch);
        compressWatch = null;
        if (state.error) notify(`Compressing failed: ${state.error}`, 'error');
        else notify(`${state.files} large cop${state.files === 1 ? 'y' : 'ies'} made smaller, ${state.megabytes} MB freed`, 'success');
        $('compress-hint').textContent = state.error ? 'Compressing failed (see the log).'
            : `Done: ${state.files} made smaller, ${state.megabytes} MB freed. Backups keep the old files until they are deleted.`;
    }
}

function showCaptureCopy(info) {
    if (document.activeElement !== $('cleanup-days')) $('cleanup-days').value = info.cleanup_days;
    showCompress(info);
    if (info.usage) showCaptureUsage(info.usage); else $('capture-usage').textContent = 'Could not be measured (see the log).';
    $('capture-copy-size').value = info.size;
    $('keep-frames').checked = !!info.keep_frames;
    $('keep-frames-hint').textContent = `${info.frames} camera frame${info.frames === 1 ? '' : 's'} kept, ${info.frames_megabytes} MB (data/frames). `
        + (info.keep_frames ? 'They stay after their scanned cards are cleared or added to the collection (until the 3 GB limit).'
                            : 'The frame of a scanned card is removed when that card is cleared or added to the collection. Label the frames you want for training first, or turn this on.');
    $('capture-copy-hint').textContent = (info.size === 'off'
        ? 'Only the thumbnail of each captured card is kept. '
        : 'The size of the picture of each captured card kept beside its thumbnail, for reading it again later or for training a detector; the pages never show it. ')
        + `${info.count} kept so far, ${info.megabytes} MB (data/captures_full). It applies to captures made from now on, from every camera.`;
}

async function loadCaptureCopy() {
    const data = await api('/api/capture_copy');
    if (data) showCaptureCopy(data);
}

async function saveCaptureCopy(body, message) {
    const data = await api('/api/capture_copy', {method: 'POST', body});
    if (!data) return loadCaptureCopy();
    showCaptureCopy(data);
    notify(message, 'success');
}

function bindData() {
    $('backup-create').addEventListener('click', createBackup);
    $('backup-upload').addEventListener('click', () => $('backup-upload-input').click());
    $('backup-upload-input').addEventListener('change', event => {
        const file = event.target.files[0];
        event.target.value = '';
        if (file) uploadBackup(file);
    });
    $('backup-every').addEventListener('change', event => saveBackupSchedule({every_hours: parseInt(event.target.value)}));
    $('backup-keep').addEventListener('change', event => saveBackupSchedule({keep: parseInt(event.target.value)}));
    $('backup-list').addEventListener('click', event => {
        const button = event.target.closest('[data-backup-action]');
        if (button) backupAction(button.closest('[data-backup]'), button.dataset.backupAction);
    });

    $('update-database').addEventListener('click', () => {
        $('update-database').disabled = true;
        $('update-database').textContent = 'Updating...';
        $('database-progress').textContent = 'Starting the card data update...';
        socket.emit('update_database');
    });
    $('rebuild-database').addEventListener('click', async () => {
        const ok = await confirmDialog({
            title: 'Rebuild database schema?',
            message: 'Reorders the card table and rebuilds its indexes (about 30 seconds). Your cards are not touched.',
            confirmText: 'Rebuild'
        });
        if (!ok) return;
        $('rebuild-database').disabled = true;
        $('rebuild-database').textContent = 'Rebuilding...';
        $('rebuild-progress').textContent = 'Starting the rebuild...';
        socket.emit('rebuild_database');
    });

    $('cleanup-days').addEventListener('change', event => {
        const days = parseInt(event.target.value);
        if (!(days >= 1 && days <= 3650)) { notify('Days: a whole number from 1 to 3650', 'error'); return loadCaptureCopy(); }
        saveCaptureCopy({cleanup_days: days}, `Received pictures are kept ${days} days`);
    });
    $('cleanup-now').addEventListener('click', async () => {
        $('cleanup-now').disabled = true;
        const data = await api('/api/storage/cleanup', {method: 'POST', body: {}});
        $('cleanup-now').disabled = false;
        if (!data) return;
        $('cleanup-result').textContent = data.deleted_files
            ? `Deleted ${data.deleted_files} picture${data.deleted_files === 1 ? '' : 's'}, freed ${data.freed_megabytes} MB.`
            : `Nothing older than ${data.cleanup_days} days.`;
        showCaptureCopy(data);
    });
    $('compress-copies').addEventListener('click', async () => {
        const data = await api('/api/storage/compress', {method: 'POST', body: {}});
        if (data) showCaptureCopy(data);
    });
    $('capture-copy-size').addEventListener('change', event =>
        saveCaptureCopy({size: event.target.value}, event.target.value === 'off' ? 'No large copies will be kept' : `Large copies will be ${event.target.value}`));
    $('keep-frames').addEventListener('change', event =>
        saveCaptureCopy({keep_frames: event.target.checked},
            event.target.checked ? 'Camera frames stay after their cards are cleared or added' : 'Camera frames go with their cards'));
}

// ============================================================================
// Server: restart, network, debug mode
// ============================================================================

const DEBUG_MODE_DESC = $('debug-mode-desc').textContent;

function applyDebugMode(enabled, running, debuggerRunning) {
    $('toggle-debug-mode').checked = Boolean(enabled);
    let text = Boolean(enabled) === Boolean(running) ? DEBUG_MODE_DESC
        : `Now ${running ? 'on' : 'off'}: restart Card Scanner to turn it ${enabled ? 'on' : 'off'}. ` + DEBUG_MODE_DESC;
    // The debugger is half of the mode, and only where nobody else can reach the server
    if (running) {
        text += debuggerRunning ? ' The interactive debugger is on (the server listens on this computer only).'
            : ' The interactive debugger is off: this server can be reached from the network, so only the request log runs.';
    }
    $('debug-mode-desc').textContent = text;
}

// -- The settings password (server/lock.py) ---------------------------------

async function unlockAndReconnect() {
    // A guarded event was refused. The unlocked cookie is only read when a connection is made:
    // after the password the page connects again, and the action is asked for once more
    if (!(await unlockSettings())) return;
    connect();
    loadLock();
    notify('Unlocked - do that again', 'info');
}

async function loadLock() {
    const status = await (await fetch('/api/settings_lock', {cache: 'no-store'})).json();
    $('lock-status').textContent = !status.has_password
        ? 'No password is set: everything is open.'
        : status.fixed
            ? 'The password is set by SCANNER_SETTINGS_PASSWORD in the server\'s environment (it cannot be changed here). '
              + (status.unlocked ? 'This browser is unlocked.' : 'This browser is locked.')
            : status.unlocked ? 'A password is set. This browser is unlocked (for 12 hours).' : 'A password is set. This browser is locked.';
    $('lock-set-row').hidden = status.fixed;
    $('lock-set').textContent = status.has_password ? 'Change password' : 'Set password';
    $('lock-unlock').hidden = !(status.has_password && !status.unlocked);
    $('lock-lock').hidden = !(status.has_password && status.unlocked);
    $('lock-remove').hidden = !(status.has_password && !status.fixed);
}

function bindLock() {
    $('lock-set').addEventListener('click', async () => {
        const data = await api('/api/settings_password', {method: 'POST', body: {password: $('lock-new').value}});
        if (!data) return;
        $('lock-new').value = '';
        notify('Settings password saved', 'success');
        connect();          // the cookie it came with is read when a connection is made
        loadLock();
    });
    $('lock-unlock').addEventListener('click', async () => { if (await unlockSettings()) { connect(); loadLock(); } });
    $('lock-lock').addEventListener('click', async () => {
        await fetch('/api/settings_lock', {method: 'DELETE'});
        connect();
        loadLock();
    });
    $('lock-remove').addEventListener('click', async () => {
        if (!(await confirmDialog({title: 'Remove the settings password?', message: 'Everything is open again to anyone who can reach this server.',
                                    confirmText: 'Remove', danger: true}))) return;
        if (await api('/api/settings_password', {method: 'POST', body: {password: ''}})) {
            notify('Settings password removed', 'success');
            connect();
            loadLock();
        }
    });
    // A backup's download is a link: a refused request would be saved as a file, so ask first
    $('backup-list').addEventListener('click', async event => {
        const link = event.target.closest('a[download]');
        if (!link || link.dataset.unlocked) return;
        event.preventDefault();
        if (await ensureSettingsUnlocked()) {
            link.dataset.unlocked = '1';
            link.click();
            delete link.dataset.unlocked;
        }
    });
}

function bindServer() {
    bindLock();
    loadLock();
    $('restart-program').addEventListener('click', restartProgram);
    if ($('quit-program')) $('quit-program').addEventListener('click', quitProgram);
    $('toggle-network-access').addEventListener('change', event => setNetworkAccess(event.target.checked));
    $('toggle-debug-mode').addEventListener('change', event => socket.emit('toggle_debug_mode', {enabled: event.target.checked}));
}

// ============================================================================
// Start
// ============================================================================

document.addEventListener('DOMContentLoaded', async () => {
    document.querySelectorAll('#settings-nav button').forEach(button =>
        button.addEventListener('click', () => showCategory(button.dataset.category)));
    window.addEventListener('hashchange', () => showCategory(categoryFromAddress(), false));
    showCategory(categoryFromAddress(), false);

    bindCameras();
    bindSounds();
    bindReading();
    bindData();
    bindServer();

    connect();              // the server's own settings need a connection even before a camera is chosen
    await loadStations();   // chooses a camera and reconnects for it
    if (!stations.length) loadCameraSettings();
    loadAI();
    loadPromptSummary();
    loadBackups();
    loadDatabaseState();
    loadCaptureCopy();
});
