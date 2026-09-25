// Card Scanner Frontend JavaScript

const socket = io();
let cardNumber = 1;
let currentCard = null;
let cardDetected = false;
let lastDetectionStatus = {detected: false, stable_frames: 0, required_frames: 0, is_stable: false};  // from /api/detection_status
let detectionEnabled = true;  // Track detection state
let autoScanningEnabled = false;  // Track auto-scanning state
let fastScanMode = true;  // "Add cards automatically" - loaded from /api/scan_settings
let detectedFoilStatus = 'unknown';  // Foil marker read by the AI on the last capture: 'foil' | 'non-foil' | 'unknown'
let availableModels = {}; // Store available models for each provider
let currentProvider = 'gemini';
let currentModel = null;
let activeProvider = null;  // provider/model the server is actually using (not just selected)
let activeModel = null;

// Helper function for console logging
function timeNow() {
    // 24-hour HH:MM:SS, matching timestamps sent by the server
    return new Date().toLocaleTimeString('en-GB', {hour12: false});
}

function logSeparator() {
    return "=".repeat(60);
}

// ============================================================================
// Socket Event Handlers
// ============================================================================

socket.on('connect', function() {
    console.log(logSeparator());
    console.log('SOCKET CONNECTED TO SERVER');
    console.log(logSeparator());

    // Initialize audio context on connection (user interaction)
    audioManager.ensureAudioContext().then(() => {
        console.log('🔊 Audio system ready');
    });

    loadStats();
    // Connection message sent from server
    startDetectionPolling();
    loadScanSettings();
});

socket.on('disconnect', function() {
    console.log(logSeparator());
    console.log('SOCKET DISCONNECTED FROM SERVER');
    console.log(logSeparator());
    // Don't log disconnect message - expected when app is shut down
});

socket.on('log', function(data) {
    console.log('Server log:', data);
    addLog(data.timestamp, data.level, data.message);
});

socket.on('card_captured', function(data) {
    console.log(logSeparator());
    console.log('CARD CAPTURED EVENT RECEIVED');
    console.log(logSeparator());
    console.log('Data:', data);

    // Don't play sound here - it's played at capture time, not after AI processing

    // Store AI-detected foil status
    detectedFoilStatus = data.foil || 'unknown';
    console.log('AI detected foil status:', detectedFoilStatus);

    document.getElementById('card-name').value = data.card_name;
    document.getElementById('collector-number').value = data.collector_number || '';
    document.getElementById('set-code').value = data.set_code || '';
    document.getElementById('search-btn').disabled = false;

    // Show processing time if available
    if (data.processing_time) {
        console.log(`AI processing time: ${data.processing_time}s`);
    }

    // Log message sent from server
    console.log('Search button enabled, card name set:', data.card_name);
    if (data.collector_number) {
        console.log('Collector number set:', data.collector_number);
    }
});

socket.on('card_found', function(data) {
    console.log(logSeparator());
    console.log('CARD FOUND EVENT RECEIVED');
    console.log(logSeparator());
    console.log('Card data:', data.card);
    console.log('🔍 Auto-add flag:', data.auto_add);

    // Play success sound (skip if auto-adding to avoid too many sounds)
    if (!data.auto_add) {
        audioManager.playSuccess();
    }

    currentCard = data.card;
    displayCard(data.card);

    // Check auto_add flag from server (set at capture time, not current mode state)
    // This allows queued cards to complete even if fast scan mode is disabled
    if (data.auto_add) {
        setTimeout(function() {
            if (currentCard) {  // Verify card still loaded
                addToInventory(true);  // Pass true for autoMode
            } else {
                console.warn('Auto-add: currentCard is null, skipping');
            }
        }, 500);  // 0.5 second delay
    } else {
        console.log('⏸️ Auto-add disabled - waiting for manual confirmation');
    }
});

socket.on('card_not_found', function(data) {
    console.log('Card not found:', data.card_name);

    // Play error sound
    audioManager.playError();

    const message = data.message || `Card "${data.card_name}" not found in database.`;
    addLog(timeNow(), 'warning', message);
    document.getElementById('card-display').innerHTML = `
        <div class="empty-state is-error">
            ${escapeHtml(message)}<br>
            <span class="hint">${data.message ? 'Try a different treatment filter.' : 'Try a different name or check spelling.'}</span>
        </div>
    `;
    // Auto-dismiss after card not found to allow next auto-capture
    setTimeout(function() {
        socket.emit('dismiss_card');
    }, 2000);
});

socket.on('card_printings', function(data) {
    console.log(`Printings received for ${data.name}:`, data.cards.length);
    currentCard = null;
    displayPrintings(data.name, data.cards);
});

socket.on('inventory_undone', function(data) {
    loadStats();
    addLog(timeNow(), 'warning', `Removed ${data.name} from the inventory (undo)`);
    document.getElementById('card-display').innerHTML = `
        <div class="empty-state">
            Removed <strong>${escapeHtml(data.name)}</strong> from the inventory.<br>
            <span class="hint">Ready for the next card.</span>
        </div>
    `;
});

socket.on('similar_cards', function(data) {
    console.log('Similar cards received:', data.cards.length);
    displaySimilarCards(data.cards);
});

socket.on('inventory_updated', function(data) {
    console.log('Inventory updated:', data.stats);

    // The drop signal is the capture beep; adding (1-2 s later, after the AI) just dings
    audioManager.playSuccess();

    loadStats();
    const added = data.added;
    document.getElementById('card-display').innerHTML = `
        <div class="empty-state is-success">
            ✓ Added to inventory
            ${added ? `<div class="added-card">${added.quantity > 1 ? added.quantity + '× ' : ''}<strong>${escapeHtml(added.name)}</strong>
                <span class="hint">${escapeHtml(added.set)} · #${escapeHtml(added.number)} · ${added.finish}</span></div>
                <button class="btn btn-small" onclick="undoLastAdd()">Undo</button>` : ''}
            <span class="hint">Ready for the next card.</span>
        </div>
    `;
    document.getElementById('card-name').value = '';
    document.getElementById('collector-number').value = '';
    document.getElementById('set-code').value = '';
    document.getElementById('card-treatment').value = '';
    detectedFoilStatus = 'unknown';
    currentCard = null;
    document.getElementById('search-btn').disabled = true;
});

socket.on('error', function(data) {
    console.error(logSeparator());
    console.error('ERROR EVENT RECEIVED');
    console.error(logSeparator());
    console.error('Error message:', data.message);

    // Play error sound
    audioManager.playError();

    if (document.getElementById('prompt-modal').classList.contains('show')) {
        notify(data.message, 'error');
    } else {
        addLog(timeNow(), 'error', data.message);
    }
});

socket.on('auto_capture_triggered', function(data) {
    console.log(logSeparator());
    console.log('AUTO-CAPTURE TRIGGERED');
    console.log(logSeparator());
    console.log('Card number:', data.counter);

    // The image is taken: this beep is the signal to drop the next card
    console.log('📸 Playing capture sound...');
    audioManager.playCapture().catch(err => {
        console.error('❌ Failed to play capture sound:', err);
    });

    // Flash animation on video container (visual feedback)
    const videoContainer = document.querySelector('.video-container');
    videoContainer.classList.add('capture-flash');
    setTimeout(() => videoContainer.classList.remove('capture-flash'), 500);

    addLog(timeNow(), 'info', `📸 Card #${data.counter} captured${fastScanMode ? ' - drop the next card' : ''}`);
});

socket.on('processing_queue_update', function(data) {
    const queueCount = data.queue_count || 0;
    const queueBox = document.getElementById('processing-queue-box');
    const queueValue = document.getElementById('processing-queue');

    // Update the queue count
    queueValue.textContent = queueCount;

    // Show/hide the queue box based on count
    if (queueCount > 0) {
        queueBox.style.display = '';
        // Add visual emphasis for queue building up
        queueBox.classList.toggle('is-busy', queueCount >= 3);
    } else {
        queueBox.style.display = 'none';
    }
});

socket.on('ai_provider_set', function(data) {
    console.log(logSeparator());
    console.log('AI PROVIDER/MODEL CHANGED');
    console.log(logSeparator());
    console.log('Provider:', data.provider);
    console.log('Model:', data.model);
    console.log('Message:', data.message);
    currentProvider = data.provider;
    currentModel = data.model;
    activeProvider = data.provider;
    activeModel = data.model;
    addLog(timeNow(), 'success', data.message);
    loadPrompts();
});

socket.on('focus_reset', function(data) {
    console.log(logSeparator());
    console.log('FOCUS RESET SUCCESS');
    console.log(logSeparator());
    addLog(timeNow(), 'info', data.message || 'Focusing...');
    // Refocusing locks the focus - reflect that in the settings switch
    document.getElementById('toggle-autofocus').checked = false;
});

socket.on('database_update_progress', function(data) {
    console.log('DATABASE UPDATE PROGRESS:', data.message);
    addLog(timeNow(), 'info', data.message);
});

socket.on('database_update_complete', function(data) {
    console.log(logSeparator());
    console.log('DATABASE UPDATE COMPLETE');
    console.log(logSeparator());
    console.log('Total cards:', data.total_cards);

    // Play queue alert sound (triple beep for major operation)
    audioManager.playQueueAlert();

    const updateBtn = document.getElementById('update-database-btn');
    updateBtn.disabled = false;
    updateBtn.textContent = 'Update card database';

    addLog(timeNow(), 'success', `Database updated! ${data.total_cards.toLocaleString()} cards loaded.`);
    loadStats();
});

socket.on('database_update_error', function(data) {
    console.log(logSeparator());
    console.log('DATABASE UPDATE ERROR');
    console.log(logSeparator());
    console.log('Error:', data.message);

    // Play error sound
    audioManager.playError();

    const updateBtn = document.getElementById('update-database-btn');
    updateBtn.disabled = false;
    updateBtn.textContent = 'Update card database';

    addLog(timeNow(), 'error', `Database update failed: ${data.message}`);
});

socket.on('database_rebuild_progress', function(data) {
    console.log('DATABASE REBUILD PROGRESS:', data.message);
    addLog(timeNow(), 'info', data.message);
});

socket.on('database_rebuild_complete', function(data) {
    console.log(logSeparator());
    console.log('DATABASE REBUILD COMPLETE');
    console.log(logSeparator());
    console.log('Cards imported:', data.cards_imported);
    console.log('Schema type:', data.schema_type);

    // Play queue alert sound (triple beep for major operation)
    audioManager.playQueueAlert();

    const rebuildBtn = document.getElementById('rebuild-database-btn');
    rebuildBtn.disabled = false;
    rebuildBtn.textContent = 'Rebuild database schema';

    addLog(timeNow(), 'success', `Database rebuilt! ${data.cards_imported.toLocaleString()} cards migrated.`);
    addLog(timeNow(), 'success', `Schema optimized: ${data.schema_type} (with performance indexes)`);
    addLog(timeNow(), 'success', 'Database queries will now be faster!');
});

socket.on('database_rebuild_error', function(data) {
    console.log(logSeparator());
    console.log('DATABASE REBUILD ERROR');
    console.log(logSeparator());
    console.log('Error:', data.message);

    // Play error sound
    audioManager.playError();

    const rebuildBtn = document.getElementById('rebuild-database-btn');
    rebuildBtn.disabled = false;
    rebuildBtn.textContent = 'Rebuild database schema';

    addLog(timeNow(), 'error', `Database rebuild failed: ${data.message}`);
});

// ============================================================================
// Detection Status
// ============================================================================

function updateDetectionStatus(status) {
    console.log('Detection status update:', status);

    lastDetectionStatus = status;
    cardDetected = status.detected;
    const statusDiv = document.getElementById('detection-status');
    const statusIcon = statusDiv.querySelector('.status-icon');
    const statusText = statusDiv.querySelector('.status-text');
    const captureBtn = document.getElementById('capture-btn');

    // Remove all status classes
    statusDiv.classList.remove('status-detected', 'status-stabilizing', 'status-ready', 'status-locked');

    // If detection is disabled, always enable capture button (captures full frame)
    if (!detectionEnabled) {
        statusIcon.textContent = '📷';
        statusText.textContent = 'Manual Mode - Click Capture';
        captureBtn.disabled = false;
        console.log('Capture button ENABLED (detection disabled)');
        return;
    }

    // Focus sweep in progress (Refocus button, or automatic when the card stays blurry)
    if (status.focusing) {
        statusDiv.classList.add('status-stabilizing');
        statusIcon.textContent = '🎯';
        statusText.textContent = 'Focusing - finding the sharpest image...';
        captureBtn.disabled = true;
        return;
    }

    // Image being taken (and, every few cards, the focus checked) - the beep says when to drop
    if (status.capturing) {
        statusDiv.classList.add('status-stabilizing');
        statusIcon.textContent = '📸';
        statusText.textContent = 'Capturing - wait for the beep';
        captureBtn.disabled = true;
        return;
    }

    // Auto scanning captured this card and waits for the next one
    if (status.awaiting_new_card) {
        statusDiv.classList.add('status-locked');
        statusIcon.textContent = '✅';
        statusText.textContent = 'Captured - drop the next card';
        captureBtn.disabled = !status.detected;
        return;
    }

    // Detection is enabled - update based on detected state
    if (status.detected) {
        captureBtn.disabled = false;

        if (status.focus_locked) {
            // Focus locked - optimal capture time!
            statusDiv.classList.add('status-locked');
            statusIcon.textContent = '🔵';
            statusText.textContent = 'LOCKED - Perfect!';
            console.log('Capture button ENABLED (focus locked)');
        } else if (status.is_stable) {
            // Stable but focus still adjusting
            statusDiv.classList.add('status-ready');
            statusIcon.textContent = '🟢';
            statusText.textContent = 'Ready to Capture';
            console.log('Capture button ENABLED (card stable)');
        } else {
            // Card detected but still stabilizing
            statusDiv.classList.add('status-stabilizing');
            statusIcon.textContent = '🟠';
            statusText.textContent = status.in_focus === false ? 'Focusing...' : `Stabilizing ${status.stable_frames}/${status.required_frames}...`;
            console.log('Capture button ENABLED (stabilizing)');
        }
    } else {
        statusIcon.textContent = '⚪';
        statusText.textContent = 'Waiting for card...';
        captureBtn.disabled = true;
        console.log('Capture button DISABLED (no card)');
    }
}

function startDetectionPolling() {
    console.log("Starting detection status polling...");
    // Poll detection status every 500ms
    setInterval(function() {
        fetch('/api/detection_status')
            .then(response => response.json())
            .then(data => {
                updateDetectionStatus(data);  // Pass full status object
            })
            .catch(error => {
                console.error('Error polling detection status:', error);
            });
    }, 500);
}

// ============================================================================
// UI Functions
// ============================================================================

function captureCard() {
    console.log(logSeparator());
    console.log("CAPTURE CARD BUTTON CLICKED");
    console.log(logSeparator());
    console.log("Detection enabled:", detectionEnabled);
    console.log("Card detected status:", cardDetected);
    console.log("Card number:", cardNumber);

    // If detection is disabled, allow capture regardless of detection status (captures full frame)
    if (detectionEnabled && !cardDetected) {
        console.warn("Detection enabled but card not detected, showing warning");
        addLog(timeNow(), 'warning', 'No card detected. Please position card in frame.');
        return;
    }

    const data = {card_number: cardNumber};
    console.log("Emitting capture_card event with data:", data);

    // Play capture sound immediately when manual capture button is clicked
    console.log('📸 Playing capture sound (manual capture)...');
    audioManager.playCapture().catch(err => {
        console.error('❌ Failed to play capture sound:', err);
    });

    // Flash animation on video container (visual feedback)
    const videoContainer = document.querySelector('.video-container');
    videoContainer.classList.add('capture-flash');
    setTimeout(() => videoContainer.classList.remove('capture-flash'), 500);

    socket.emit('capture_card', data);
    console.log("Event emitted, incrementing card number");
    cardNumber++;
    console.log("New card number:", cardNumber);
    console.log(logSeparator());
}

function searchCard() {
    console.log(logSeparator());
    console.log("SEARCH CARD BUTTON CLICKED");
    console.log(logSeparator());

    const cardName = document.getElementById('card-name').value.trim();
    const collectorNumber = document.getElementById('collector-number').value.trim();
    const setCode = document.getElementById('set-code').value.trim().toUpperCase();
    const treatmentSelect = document.getElementById('card-treatment');
    const treatment = treatmentSelect.value;
    console.log("Card name from input:", cardName);
    console.log("Collector number from input:", collectorNumber);

    if (cardName) {
        const data = {
            card_name: cardName,
            collector_number: collectorNumber || null,
            set_code: setCode || null,
            treatment: treatment || null
        };
        console.log("Emitting search_card event with data:", data);

        const numberInfo = (setCode ? ` ${setCode}` : '') + (collectorNumber ? ` #${collectorNumber}` : '');
        const treatmentInfo = treatment ? ` (${treatmentSelect.options[treatmentSelect.selectedIndex].text})` : '';
        addLog(timeNow(), 'info', `Searching for: ${cardName}${numberInfo}${treatmentInfo}`);

        socket.emit('search_card', data);
    } else {
        console.warn("No card name entered");
        addLog(timeNow(), 'warning', 'Please enter a card name');
    }
    console.log(logSeparator());
}

function autoScanHint() {
    return fastScanMode ? 'Auto scanning - adding cards automatically' : 'Auto scanning - confirm each card';
}

function undoLastAdd() {
    socket.emit('undo_last_add');
}

function loadScanSettings() {
    // "Add cards automatically" is remembered on the server
    fetch('/api/scan_settings')
        .then(response => response.json())
        .then(data => {
            fastScanMode = data.auto_add;
            document.getElementById('toggle-fast-scan').checked = data.auto_add;
            document.getElementById('toggle-autofocus').checked = data.autofocus;
            applyFixedArea({enabled: data.fixed_area_enabled, area: data.fixed_area});
        })
        .catch(error => console.error('Error loading scan settings:', error));
}

// ============================================================================
// Fixed capture area (sleeved cards): drawn on the video, judged by image changes
// ============================================================================

let fixedArea = {enabled: false, area: null};
let areaDrag = null;

function applyFixedArea(state) {
    fixedArea = state;
    document.getElementById('fixed-area-toggle').checked = Boolean(state.enabled);
}

socket.on('fixed_area_updated', function(state) {
    const wasEnabled = fixedArea.enabled;
    applyFixedArea(state);
    if (state.enabled !== wasEnabled) {
        notify(state.enabled ? 'Fixed area on - cards are judged by the drawn area' : 'Fixed area off - cards are found by their outline', 'info');
    }
});

function imageContentRect() {
    // Where the video is drawn inside its box (object-fit: contain may leave bars)
    const img = document.getElementById('video-feed');
    const box = img.getBoundingClientRect();
    const ratio = (img.naturalWidth && img.naturalHeight) ? img.naturalWidth / img.naturalHeight : 16 / 9;
    let width = box.width, height = box.width / ratio;
    if (height > box.height) { height = box.height; width = height * ratio; }
    const parent = img.parentElement.getBoundingClientRect();
    return {left: box.left - parent.left + (box.width - width) / 2, top: box.top - parent.top + (box.height - height) / 2, width, height};
}

function placeAreaLayer() {
    const rect = imageContentRect();
    const layer = document.getElementById('area-layer');
    Object.assign(layer.style, {left: rect.left + 'px', top: rect.top + 'px', width: rect.width + 'px', height: rect.height + 'px'});
}

function startDrawArea() {
    placeAreaLayer();
    document.getElementById('area-layer').classList.add('is-drawing');
    document.getElementById('area-hint').hidden = false;
}

function cancelDrawArea() {
    document.getElementById('area-layer').classList.remove('is-drawing');
    document.getElementById('area-hint').hidden = true;
    document.getElementById('area-rect').hidden = true;
    areaDrag = null;
    document.getElementById('fixed-area-toggle').checked = Boolean(fixedArea.enabled);
}

function useDetectedArea() {
    socket.emit('set_fixed_area', {use_detected: true, enabled: true});
    cancelDrawArea();
}

function areaPoint(event) {
    const box = document.getElementById('area-layer').getBoundingClientRect();
    return {x: Math.min(1, Math.max(0, (event.clientX - box.left) / box.width)),
            y: Math.min(1, Math.max(0, (event.clientY - box.top) / box.height))};
}

function drawAreaRect(a, b) {
    const rect = document.getElementById('area-rect');
    Object.assign(rect.style, {
        left: Math.min(a.x, b.x) * 100 + '%', top: Math.min(a.y, b.y) * 100 + '%',
        width: Math.abs(a.x - b.x) * 100 + '%', height: Math.abs(a.y - b.y) * 100 + '%'
    });
    rect.hidden = false;
}

function setupAreaDrawing() {
    const layer = document.getElementById('area-layer');
    layer.addEventListener('pointerdown', event => {
        areaDrag = areaPoint(event);
        layer.setPointerCapture(event.pointerId);
        drawAreaRect(areaDrag, areaDrag);
    });
    layer.addEventListener('pointermove', event => {
        if (areaDrag) drawAreaRect(areaDrag, areaPoint(event));
    });
    layer.addEventListener('pointerup', event => {
        if (!areaDrag) return;
        const a = areaDrag, b = areaPoint(event);
        areaDrag = null;
        if (Math.abs(a.x - b.x) < 0.05 || Math.abs(a.y - b.y) < 0.05) {
            notify('Drag a rectangle around the card', 'warning');
            document.getElementById('area-rect').hidden = true;
            return;
        }
        socket.emit('set_fixed_area', {
            area: [Math.min(a.x, b.x), Math.min(a.y, b.y), Math.max(a.x, b.x), Math.max(a.y, b.y)],
            enabled: true
        });
        cancelDrawArea();
    });
    document.getElementById('fixed-area-toggle').addEventListener('change', event => {
        if (event.target.checked && !fixedArea.area) {
            startDrawArea();  // nothing to turn on yet: draw the area first
            return;
        }
        socket.emit('set_fixed_area', {enabled: event.target.checked});
    });
    window.addEventListener('resize', () => {
        if (document.getElementById('area-layer').classList.contains('is-drawing')) placeAreaLayer();
    });
}

function resetFocus() {
    console.log(logSeparator());
    console.log("RESET FOCUS BUTTON CLICKED");
    console.log(logSeparator());

    addLog(timeNow(), 'info', 'Resetting camera focus...');
    socket.emit('reset_focus');
}

function toggleAutoScanning() {
    console.log(logSeparator());
    console.log("TOGGLE AUTO SCANNING BUTTON CLICKED");
    console.log(`Current state: autoScanningEnabled = ${autoScanningEnabled}`);
    console.log(logSeparator());

    const btn = document.getElementById('toggle-auto-scanning-btn');
    const hint = document.getElementById('auto-scan-hint');

    // Toggle the state
    autoScanningEnabled = !autoScanningEnabled;
    console.log(`New state: autoScanningEnabled = ${autoScanningEnabled}`);

    // Update button appearance and text
    btn.classList.toggle('is-active', autoScanningEnabled);
    hint.classList.toggle('is-active', autoScanningEnabled);
    if (autoScanningEnabled) {
        btn.innerHTML = '<svg class="icon"><use href="#i-play"/></svg> Stop auto scanning';
        hint.textContent = autoScanHint();

        console.log('Sending toggle_auto_capture with enabled=true');
        socket.emit('toggle_auto_capture', {enabled: true});
        addLog(timeNow(), 'success', `Auto scanning started${fastScanMode ? ' - adding cards automatically' : ' - confirm each card'}`);
    } else {
        btn.innerHTML = '<svg class="icon"><use href="#i-play"/></svg> Start auto scanning';
        hint.textContent = 'Click to start automatic card scanning';

        console.log('Sending toggle_auto_capture with enabled=false');
        socket.emit('toggle_auto_capture', {enabled: false});
        addLog(timeNow(), 'info', 'Auto scanning stopped - captures in progress will complete');
    }
}

function selectSimilarCard(cardName) {
    console.log("Similar card selected:", cardName);
    document.getElementById('card-name').value = cardName;
    searchCard();
}

function suggestedFinish(card) {
    // Which finish the card in hand most likely is: 'regular' | 'foil' | 'surge', and why.
    // Printings that only exist in one finish are certain; otherwise use the ★/• marker
    // the AI read next to the set code on the last capture.
    if (!gameInfo || gameInfo.id !== 'mtg') return {finish: defaultFinish(), reason: null};
    const finishes = card.finishes || [];
    const hasFoil = finishes.includes('foil') || finishes.includes('etched');
    const hasNonfoil = finishes.includes('nonfoil');
    const foilKind = (card.treatments || []).includes('Surge Foil') ? 'surge' : 'foil';

    if (hasFoil && !hasNonfoil) return {finish: foilKind, reason: 'only printed in foil'};
    if (hasNonfoil && !hasFoil) return {finish: 'regular', reason: 'only printed non-foil'};
    if (detectedFoilStatus === 'foil') return {finish: foilKind, reason: '★ next to the set code'};
    if (detectedFoilStatus === 'non-foil') return {finish: 'regular', reason: '• next to the set code'};
    return {finish: 'regular', reason: null};
}

function quantityCell(id, label, kind, value = 0) {
    return `
        <div class="qty-cell ${kind}">
            <span class="qty-label"><span class="qty-dot"></span>${label}</span>
            <div class="qty-stepper">
                <button onclick="adjustQtyInput('${id}', -1)" aria-label="Decrease ${label}">−</button>
                <input type="number" id="${id}" value="${value}" min="0" max="999" aria-label="${label} quantity">
                <button onclick="adjustQtyInput('${id}', 1)" aria-label="Increase ${label}">+</button>
            </div>
        </div>
    `;
}

function displayCard(card) {
    const suggestion = suggestedFinish(card);
    // Only show prices that exist (e.g. foil-only printings have no regular price)
    const prices = [];
    if (card.price > 0) {
        prices.push(`<span class="price">$${card.price.toFixed(2)}</span>`);
    }
    if (card.price_foil > 0) {
        prices.push(`<span class="price-label">foil</span> <span class="price">$${card.price_foil.toFixed(2)}</span>`);
    }
    if (prices.length === 0) {
        prices.push('<span class="price-label">No price data</span>');
    }

    const html = `
        <div class="card-summary">
            ${card.image_uri ? `<img src="${escapeHtml(card.image_uri)}" alt="${escapeHtml(card.name)}" class="card-image">` : ''}
            <div class="card-facts">
                <div class="card-title">${escapeHtml(card.name)}</div>
                <div class="card-meta">${escapeHtml(card.set)} · #${escapeHtml(card.number)}</div>
                <div class="card-meta"><span class="card-rarity">${escapeHtml(card.rarity)}</span> · ${escapeHtml(card.type)}</div>
                ${card.treatments && card.treatments.length ? treatmentTagsHtml(card.treatments) : ''}
                ${card.confirmed === false ? '<div class="card-warning">Printing not confirmed - check the set and number</div>' : ''}
                <div class="card-prices">${prices.join('<span class="price-sep">·</span>')}</div>
            </div>
        </div>

        <div class="input-group">
            <label for="condition">Condition</label>
            <select id="condition">
                <option value="Mint">Mint (M)</option>
                <option value="Near Mint" selected>Near Mint (NM)</option>
                <option value="Excellent">Excellent (EX)</option>
                <option value="Good">Good (GD)</option>
                <option value="Played">Played (PL)</option>
                <option value="Poor">Poor (P)</option>
            </select>
        </div>

        <div class="input-group">
            <span class="field-label">Quantity</span>
            <div class="qty-grid">
                ${gameInfo.finishes.map(([key, label]) => quantityCell(`qty-${key}`, label, key, suggestion.finish === key ? 1 : 0)).join('')}
            </div>
            ${suggestion.reason ? `<div class="finish-hint ${suggestion.finish}">${finishLabel(suggestion.finish)}: ${suggestion.reason}</div>` : ''}
        </div>

        <div class="card-actions">
            <button class="btn btn-success" onclick="addToInventoryBoth()">Add to inventory</button>
            <button class="btn" onclick="dismissCard()">Skip</button>
        </div>
    `;

    document.getElementById('card-display').innerHTML = html;
}

function treatmentTagsHtml(treatments) {
    return '<span class="treatment-tags">' +
        treatments.map(t => `<span class="treatment-tag">${escapeHtml(t)}</span>`).join('') +
        '</span>';
}

function displayPrintings(cardName, cards) {
    let html = `<div class="similar-cards"><div class="list-heading">${escapeHtml(cardName)}</div>`;
    html += `<div class="list-subheading">${cards.length} printings - pick the one you have</div><div class="printing-grid">`;

    cards.forEach(card => {
        // Scryfall's "small" image size keeps the grid light
        const thumb = card.image_uri ? card.image_uri.replace('/normal/', '/small/') : '';
        const price = card.price > 0 ? `$${card.price.toFixed(2)}` : (card.price_foil > 0 ? `$${card.price_foil.toFixed(2)} foil` : 'N/A');
        html += `
            <div class="printing-card" onclick="selectPrinting('${escapeHtml(card.id)}')">
                ${thumb ? `<img src="${escapeHtml(thumb)}" alt="${escapeHtml(card.name)}" loading="lazy">` : ''}
                <strong>${escapeHtml(card.set)}</strong><br>
                <span class="meta">#${escapeHtml(card.number)} &middot; ${price}</span>
                ${card.treatments.length ? treatmentTagsHtml(card.treatments) : ''}
            </div>
        `;
    });

    html += '</div></div>';
    document.getElementById('card-display').innerHTML = html;
}

function selectPrinting(cardId) {
    console.log("Printing selected:", cardId);
    socket.emit('select_printing', {id: cardId});
}

function displaySimilarCards(cards) {
    let html = '<div class="similar-cards"><div class="list-heading">No exact match</div><div class="list-subheading">Did you mean:</div>';

    if (cards.length === 0) {
        html += '<div class="empty-state">No similar cards found.</div>';
    } else {
        cards.forEach(card => {
            html += `
                <div class="similar-card" onclick="selectSimilarCard('${escapeHtml(card.name.replace(/'/g, "\\'"))}')">
                    <strong>${escapeHtml(card.name)}</strong><br>
                    <small>${escapeHtml(card.set)} · ${card.price}</small>
                </div>
            `;
        });
    }
    
    html += '</div>';
    document.getElementById('card-display').innerHTML = html;
}

function adjustQtyInput(inputId, delta) {
    const input = document.getElementById(inputId);
    const currentValue = parseInt(input.value) || 0;
    const newValue = Math.max(0, Math.min(999, currentValue + delta));
    input.value = newValue;
}

function addToInventoryBoth() {
    if (!currentCard) {
        addLog(timeNow(), 'error', 'No card selected');
        return;
    }

    const condition = document.getElementById('condition').value;
    const quantities = gameInfo.finishes.map(([key]) => [key, parseInt(document.getElementById(`qty-${key}`).value) || 0]);
    if (quantities.every(([, quantity]) => quantity === 0)) {
        addLog(timeNow(), 'warning', 'Please set at least one quantity');
        return;
    }

    // One entry per finish with a quantity
    quantities.filter(([, quantity]) => quantity > 0).forEach(([finish, quantity]) => {
        socket.emit('add_to_inventory', {quantity: quantity, condition: condition, finish: finish});
    });
}

function addToInventory(autoMode = false) {
    // Automatic add: one Near Mint copy in the suggested finish
    console.log(`addToInventory called with autoMode=${autoMode}, currentCard:`, currentCard);

    if (!currentCard) {
        console.error('addToInventory: No card selected!');
        addLog(timeNow(), 'error', 'No card selected');
        return;
    }

    const data = {
        quantity: 1,
        condition: 'Near Mint',
        finish: suggestedFinish(currentCard).finish
    };
    console.log('Emitting add_to_inventory event with:', data);
    socket.emit('add_to_inventory', data);
}

function dismissCard() {
    console.log('Card dismissed by user');
    currentCard = null;
    detectedFoilStatus = 'unknown';

    // Emit dismiss event to server to re-enable auto-capture
    socket.emit('dismiss_card');

    // Clear the card display
    document.getElementById('card-display').innerHTML = `
        <div class="empty-state">
            Card skipped.<br>
            Ready to scan the next card.
        </div>
    `;

    addLog(timeNow(), 'info', 'Card dismissed - ready for next card');
}

function addLog(timestamp, level, message) {
    const logContainer = document.getElementById('log-container');
    const logEntry = document.createElement('div');
    logEntry.className = 'log-entry';
    logEntry.innerHTML = `
        <span class="log-timestamp">[${timestamp}]</span>
        <span class="log-${level}">${escapeHtml(message)}</span>
    `;
    logContainer.appendChild(logEntry);
    logContainer.scrollTop = logContainer.scrollHeight;
    
    // Keep only last 100 log entries
    while (logContainer.children.length > 100) {
        logContainer.removeChild(logContainer.firstChild);
    }
}

function loadStats() {
    fetch('/api/stats')
        .then(response => response.json())
        .then(data => {
            if (data.database) {
                document.getElementById('db-cards').textContent =
                    data.database.total_cards.toLocaleString();
            }
            if (data.inventory) {
                document.getElementById('inv-cards').textContent =
                    data.inventory.total_cards.toLocaleString();
                document.getElementById('total-value').textContent =
                    '$' + data.inventory.total_value.toFixed(2);
            }
        })
        .catch(error => {
            console.error('Error loading stats:', error);
            // Don't log stats error - expected when server is down
        });
}

function loadAIModels() {
    fetch('/api/ai_models')
        .then(response => response.json())
        .then(data => {
            availableModels = data.models;
            console.log('Available models loaded:', availableModels);
        })
        .catch(error => {
            console.error('Error loading AI models:', error);
        });
}

function refreshAIModels() {
    const refreshBtn = document.getElementById('refresh-models-btn');
    const modelSelect = document.getElementById('ai-model');
    const provider = currentProvider;

    // Disable button and show loading state
    refreshBtn.disabled = true;
    refreshBtn.textContent = '⏳';
    modelSelect.innerHTML = '<option value="">Refreshing models...</option>';

    addLog(timeNow(), 'info', `Refreshing ${provider} models...`);

    // For local provider, dynamically fetch from Ollama
    if (provider === 'local') {
        loadLocalModels(currentModel).finally(() => {
            refreshBtn.disabled = false;
            refreshBtn.textContent = '🔄';
        });
    } else {
        // For cloud providers, just reload from static list
        fetch('/api/ai_models')
            .then(response => response.json())
            .then(data => {
                availableModels = data.models;
                console.log('Models refreshed:', availableModels);

                // Re-populate dropdown
                populateModelDropdown(provider, currentModel);

                addLog(timeNow(), 'success', `${provider} models refreshed`);
            })
            .catch(error => {
                console.error('Error refreshing models:', error);
                addLog(timeNow(), 'error', 'Failed to refresh models');
            })
            .finally(() => {
                // Reset button
                refreshBtn.disabled = false;
                refreshBtn.textContent = '🔄';
            });
    }
}

function populateModelDropdown(provider, selectedModel = null) {
    const modelSelect = document.getElementById('ai-model');
    modelSelect.innerHTML = ''; // Clear existing options

    if (availableModels[provider]) {
        availableModels[provider].forEach(model => {
            const option = document.createElement('option');
            option.value = model;
            option.textContent = model;
            if (selectedModel && model === selectedModel) {
                option.selected = true;
            }
            modelSelect.appendChild(option);
        });
    } else {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = 'No models available';
        modelSelect.appendChild(option);
    }
}

function loadLocalModels(selectedModel = null) {
    const modelSelect = document.getElementById('ai-model');
    modelSelect.innerHTML = '<option value="">Loading models from Ollama...</option>';

    // Fetch live models from Ollama and return promise
    return fetch('/api/local_ai_models')
        .then(response => response.json())
        .then(data => {
            if (data.success && data.models && data.models.length > 0) {
                // Update available models with live Ollama models
                availableModels['local'] = data.models;
                console.log('Local models loaded:', data.models);
                addLog(timeNow(), 'success', `Found ${data.models.length} local models`);

                // Populate dropdown
                populateModelDropdown('local', selectedModel);
            } else {
                // Fall back to static list
                console.warn('Could not fetch local models, using static list');
                addLog(timeNow(), 'warning', 'Using static model list (local server not available)');
                populateModelDropdown('local', selectedModel);
            }
        })
        .catch(error => {
            console.error('Error loading local models:', error);
            addLog(timeNow(), 'warning', 'Could not connect to local AI server');

            // Fall back to static list
            populateModelDropdown('local', selectedModel);
        });
}

// ============================================================================
// API keys / local endpoint (Settings -> Vision AI)
// Keys are shown masked only: the server never sends a full key to the browser.
// ============================================================================

let aiCredentials = {};
const PROVIDER_INFO = {
    gemini: {name: 'Google Gemini', keyUrl: 'https://aistudio.google.com/app/apikey'},
    openai: {name: 'OpenAI', keyUrl: 'https://platform.openai.com/api-keys'},
    anthropic: {name: 'Anthropic', keyUrl: 'https://console.anthropic.com/settings/keys'},
    local: {name: 'Local AI'}
};

function loadCredentials() {
    return fetch('/api/ai_credentials')
        .then(response => response.json())
        .then(data => {
            aiCredentials = data;
            renderCredentialField(document.getElementById('ai-provider').value);
        })
        .catch(error => console.error('Error loading API key status:', error));
}

function hasCredential(provider) {
    return provider === 'local' || Boolean(aiCredentials[provider] && aiCredentials[provider].configured);
}

function renderCredentialField(provider) {
    const input = document.getElementById('ai-credential');
    const label = document.getElementById('ai-credential-label');
    const hint = document.getElementById('ai-credential-hint');
    const info = PROVIDER_INFO[provider] || {name: provider};
    const status = aiCredentials[provider] || {};
    input.value = '';
    hint.classList.remove('is-missing');

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
        hint.classList.add('is-missing');
    }
}

async function saveCredential() {
    const provider = document.getElementById('ai-provider').value;
    const input = document.getElementById('ai-credential');
    const value = input.value.trim();
    const info = PROVIDER_INFO[provider] || {name: provider};

    if (!value) {
        if (provider === 'local' || !hasCredential(provider)) {
            notify(provider === 'local' ? 'Enter the server address' : 'Paste an API key first', 'warning');
            return;
        }
        const remove = await confirmDialog({
            title: `Remove the ${info.name} key?`,
            message: 'Removes the key saved in the web interface. A key in the .env file (if any) is used again after a restart.',
            confirmText: 'Remove key',
            danger: true
        });
        if (!remove) return;
    }
    socket.emit('save_ai_credential', {provider: provider, value: value});
}

socket.on('ai_credential_saved', function(data) {
    aiCredentials[data.provider] = data.status;
    const info = PROVIDER_INFO[data.provider] || {name: data.provider};
    const selected = document.getElementById('ai-provider').value;
    renderCredentialField(selected);

    if (data.provider === 'local') {
        notify('Local AI address saved', 'success');
    } else {
        notify(data.status.configured ? `${info.name} API key saved` : `${info.name} API key removed`, 'success');
    }
    // Use the new key / address right away for the selected provider
    if (data.provider === selected && hasCredential(selected)) {
        applyProvider(selected);
    }
});

function applyProvider(provider) {
    // Load the provider's models and switch the scanner to it; returning to the active
    // provider keeps its model instead of jumping to the first one in the list
    const keepModel = provider === activeProvider ? activeModel : null;
    const loaded = provider === 'local' ? loadLocalModels(keepModel) : Promise.resolve(populateModelDropdown(provider, keepModel));
    loaded.then(() => {
        const model = document.getElementById('ai-model').value;
        socket.emit('set_ai_provider', {provider: provider, model: model});
        addLog(timeNow(), 'info', `Changing AI provider to ${provider}...`);
    });
}

function loadAIProvider() {
    return fetch('/api/ai_provider')
        .then(response => response.json())
        .then(data => {
            if (data.provider) {
                const providerSelect = document.getElementById('ai-provider');
                providerSelect.value = data.provider;
                currentProvider = data.provider;
                currentModel = data.model;
                activeProvider = data.provider;
                activeModel = data.model;

                // For local provider, fetch live models from Ollama
                if (data.provider === 'local') {
                    loadLocalModels(data.model);
                } else {
                    // Populate model dropdown for cloud providers
                    populateModelDropdown(data.provider, data.model);
                }

                console.log('Current AI provider:', data.provider);
                console.log('Current AI model:', data.model);
            }
        })
        .catch(error => {
            console.error('Error loading AI provider:', error);
        });
}

// ============================================================================
// Prompt editor (Settings -> Vision AI -> Edit prompts)
// ============================================================================

let promptData = null;      // {provider, model, prompts: {kind: {label, instructions, source, ...}}}
let promptKind = 'identify';
let promptDrafts = {};      // kind -> edited text not saved yet

const PROMPT_SOURCES = {
    'built-in': 'Built-in prompt',
    'all': 'Saved for all models',
    'model': 'Saved for this model'
};

function loadPrompts() {
    return fetch('/api/prompts')
        .then(response => response.json())
        .then(data => {
            promptData = data;
            updatePromptSummary();
            if (document.getElementById('prompt-modal').classList.contains('show')) renderPromptEditor();
        })
        .catch(error => console.error('Error loading prompts:', error));
}

function updatePromptSummary() {
    const custom = promptData.order.map(kind => promptData.prompts[kind]).filter(p => p.source !== 'built-in');
    document.getElementById('prompt-summary').textContent = custom.length
        ? custom.map(p => `${p.label}: ${PROMPT_SOURCES[p.source].toLowerCase()}`).join(' · ')
        : 'What the AI is asked to read on each card - adjustable per model';
}

function promptText(kind) {
    return kind in promptDrafts ? promptDrafts[kind] : promptData.prompts[kind].instructions;
}

function openPromptEditor() {
    loadPrompts().then(() => {
        if (!promptData) return;
        document.getElementById('prompt-test').hidden = true;
        document.getElementById('prompt-modal').classList.add('show');
        renderPromptEditor();
    });
}

async function closePromptEditor() {
    const edited = Object.keys(promptDrafts).length > 0;
    if (edited && !await confirmDialog({
        title: 'Discard changes?',
        message: 'The edited prompt has not been saved.',
        confirmText: 'Discard',
        danger: true
    })) return;
    promptDrafts = {};
    document.getElementById('prompt-modal').classList.remove('show');
}

function renderPromptEditor() {
    const kinds = promptData.prompts;
    if (!(promptKind in kinds)) promptKind = promptData.order[0];
    const prompt = kinds[promptKind];

    document.getElementById('prompt-model').textContent = promptData.model
        ? `In use with ${promptData.provider} / ${promptData.model}`
        : 'No AI model active - prompts can only be saved for all models';

    document.getElementById('prompt-kinds').innerHTML = promptData.order.map(kind => [kind, kinds[kind]]).map(([kind, p]) => `
        <label class="radio-pill">
            <input type="radio" name="prompt-kind" value="${kind}" ${kind === promptKind ? 'checked' : ''} onchange="selectPromptKind('${kind}')">
            <span>${escapeHtml(p.label)}${kind in promptDrafts ? ' *' : ''}</span>
        </label>`).join('');

    const textarea = document.getElementById('prompt-text');
    if (textarea.value !== promptText(promptKind)) textarea.value = promptText(promptKind);
    document.getElementById('prompt-format').textContent = prompt.answer_format;
    document.getElementById('prompt-save-model-btn').disabled = !promptData.model;
    renderPromptSource();
}

function renderPromptSource() {
    const prompt = promptData.prompts[promptKind];
    const edited = promptKind in promptDrafts;
    const tag = `<span class="source-tag ${prompt.source === 'built-in' ? '' : 'is-custom'}">${PROMPT_SOURCES[prompt.source]}</span>`;
    const note = edited ? '<span class="source-tag is-edited">Edited - not saved</span>'
        : prompt.source === 'model' && prompt.has_all_models ? 'overrides the prompt saved for all models' : '';
    document.getElementById('prompt-source').innerHTML = tag + note;
    document.getElementById('prompt-reset-btn').disabled = prompt.source === 'built-in' && !edited;
}

function selectPromptKind(kind) {
    promptKind = kind;
    document.getElementById('prompt-test').hidden = true;
    renderPromptEditor();
}

function onPromptInput() {
    const text = document.getElementById('prompt-text').value;
    const wasEdited = promptKind in promptDrafts;
    if (text === promptData.prompts[promptKind].instructions) {
        delete promptDrafts[promptKind];
    } else {
        promptDrafts[promptKind] = text;
    }
    // Redraw the tabs only when the unsaved-changes marker appears or disappears
    if (wasEdited !== (promptKind in promptDrafts)) renderPromptEditor();
    else renderPromptSource();
}

function savePrompt(scope) {
    const text = document.getElementById('prompt-text').value.trim();
    if (!text) {
        notify('The prompt is empty', 'warning');
        return;
    }
    socket.emit('save_prompt', {kind: promptKind, text: text, scope: scope});
}

async function resetPrompt() {
    const prompt = promptData.prompts[promptKind];
    if (prompt.source === 'built-in') {
        // Nothing saved - just discard the edits
        delete promptDrafts[promptKind];
        renderPromptEditor();
        return;
    }
    const fallback = prompt.source === 'model' && prompt.has_all_models ? 'the prompt saved for all models' : 'the built-in prompt';
    const message = prompt.source === 'model'
        ? `Removes the prompt saved for ${promptData.provider} / ${promptData.model}. It will use ${fallback}.`
        : 'Removes the prompt saved for all models. Models without their own prompt will use the built-in prompt.';
    if (!await confirmDialog({title: 'Restore default?', message: message, confirmText: 'Remove saved prompt', danger: true})) return;
    delete promptDrafts[promptKind];
    socket.emit('reset_prompt', {kind: promptKind});
}

socket.on('prompts_updated', function(data) {
    delete promptDrafts[promptKind];
    promptData = data;
    updatePromptSummary();
    if (document.getElementById('prompt-modal').classList.contains('show')) {
        document.getElementById('prompt-text').value = '';  // force a refresh with the saved text
        renderPromptEditor();
    }
    notify(data.message, 'success');
});

function testPrompt() {
    const text = document.getElementById('prompt-text').value.trim();
    if (!text) {
        notify('The prompt is empty', 'warning');
        return;
    }
    const button = document.getElementById('prompt-test-btn');
    button.disabled = true;
    button.textContent = 'Testing...';
    const panel = document.getElementById('prompt-test');
    panel.hidden = false;
    panel.innerHTML = `Asking ${escapeHtml(promptData.model || 'the AI')} about the last captured card...`;
    socket.emit('test_prompt', {kind: promptKind, text: text});
}

socket.on('prompt_test_result', function(data) {
    const button = document.getElementById('prompt-test-btn');
    button.disabled = false;
    button.textContent = 'Test on last capture';
    const panel = document.getElementById('prompt-test');
    panel.hidden = false;

    if (data.error) {
        panel.innerHTML = `<span class="is-error">${escapeHtml(data.error)}</span>`;
        return;
    }

    let parsed;
    if (data.kind === 'foil') {
        const labels = {'foil': 'Foil (star)', 'non-foil': 'Not foil (dot)', 'unknown': 'Not recognized - answer must contain "star" or "dot"'};
        parsed = `<div>${escapeHtml(labels[data.result.foil] || data.result.foil)}</div>`;
    } else if (!data.result) {
        parsed = '<div class="is-error">No card name found in the answer</div>';
    } else {
        const r = data.result;
        parsed = `<div>${escapeHtml(r.name)} · #${escapeHtml(r.collector_number || '?')} · ${escapeHtml(r.set_code || '?')}</div>`;
        const m = data.match;
        parsed += '<div class="test-title">Database match</div>' + (!m
            ? '<div class="is-error">No card found in the database</div>'
            : `<div class="${m.confirmed ? 'is-confirmed' : 'is-review'}">${escapeHtml(m.name)} · ${escapeHtml(m.set_name || m.set)} (${escapeHtml(m.set)}) #${escapeHtml(m.number || '?')}
               - ${m.confirmed ? 'confirmed, would be added automatically' : `needs review (matched by ${escapeHtml(m.match || '?')})`}</div>`);
    }
    panel.innerHTML = `
        <div class="test-title">Answer (${data.seconds} s)</div>
        <pre>${escapeHtml(data.raw || '(empty)')}</pre>
        <div class="test-title">Read as</div>
        ${parsed}`;
});

// ============================================================================
// Card game being scanned (one at a time; the selector shows when there are several)
// ============================================================================

let gameInfo = null;  // {id, label, finishes: [[key, label]], exports: [[key, label]]}

function defaultFinish() {
    return gameInfo.finishes[0][0];
}

function finishLabel(key) {
    const finish = gameInfo.finishes.find(([k]) => k === key);
    return finish ? finish[1] : key;
}

function loadGames() {
    return fetch('/api/games')
        .then(response => response.json())
        .then(data => {
            gameInfo = data.games.find(game => game.id === data.active);
            const select = document.getElementById('game-select');
            select.innerHTML = data.games.map(game =>
                `<option value="${escapeHtml(game.id)}">${escapeHtml(game.label)}</option>`).join('');
            select.value = data.active;
            select.hidden = data.games.length < 2;
            renderExportButtons();
        })
        .catch(error => console.error('Error loading games:', error));
}

socket.on('game_changed', function(data) {
    gameInfo = data;
    document.getElementById('game-select').value = data.id;
    renderExportButtons();
    currentCard = null;
    document.getElementById('card-display').innerHTML =
        `<div class="empty-state">Scanning ${escapeHtml(data.label)}.</div>`;
    loadStats();
    loadPrompts();
    notify(data.card_count
        ? `Scanning ${data.label}`
        : `Scanning ${data.label} - download its card database in Settings first`, data.card_count ? 'success' : 'warning');
});

function escapeHtml(text) {
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return text.replace(/[&<>"']/g, m => map[m]);
}

// ============================================================================
// Event Listeners
// ============================================================================

document.addEventListener('DOMContentLoaded', function() {
    // Initialize detection state from checkbox
    const detectionCheckbox = document.getElementById('toggle-detection');
    if (detectionCheckbox) {
        detectionEnabled = detectionCheckbox.checked;
    }

    // Allow Enter key to search (both fields)
    document.getElementById('card-name').addEventListener('keypress', function(e) {
        if (e.key === 'Enter' && !this.disabled) {
            searchCard();
        }
    });

    document.getElementById('set-code').addEventListener('keypress', function(e) {
        if (e.key === 'Enter') {
            searchCard();
        }
    });

    document.getElementById('collector-number').addEventListener('keypress', function(e) {
        if (e.key === 'Enter' && !this.disabled) {
            searchCard();
        }
    });

    // Enable search button when user types in card name field
    document.getElementById('card-name').addEventListener('input', function(e) {
        const searchBtn = document.getElementById('search-btn');
        searchBtn.disabled = !this.value.trim();
    });
    
    // Toggle detection
    document.getElementById('toggle-detection').addEventListener('change', function(e) {
        const enabled = e.target.checked;
        const autoScanBtn = document.getElementById('toggle-auto-scanning-btn');

        // Update global detection state
        detectionEnabled = enabled;

        socket.emit('toggle_detection', {enabled: enabled});
        // Log message will be sent from server

        // Update UI immediately (the next poll refreshes it from the server)
        updateDetectionStatus(lastDetectionStatus);

        if (!enabled) {
            // When disabling detection, also stop auto-scanning
            if (autoScanningEnabled) {
                toggleAutoScanning(); // Stop auto-scanning
            }
            autoScanBtn.disabled = true;
        } else {
            // When enabling detection, enable the button
            autoScanBtn.disabled = false;
        }
    });

    // Toggle fast scan mode
    document.getElementById('toggle-fast-scan').addEventListener('change', function(e) {
        const enabled = e.target.checked;
        fastScanMode = enabled;  // Update global state
        socket.emit('toggle_fast_scan', {enabled: enabled});  // server logs the change

        // Update hint if auto-scanning is active
        if (autoScanningEnabled) {
            document.getElementById('auto-scan-hint').textContent = autoScanHint();
        }
    });

    // Continuous autofocus on/off (off = find the sharpest focus and lock it)
    document.getElementById('toggle-autofocus').addEventListener('change', function(e) {
        socket.emit('set_autofocus', {enabled: e.target.checked});
    });

    // Toggle anti-glare
    document.getElementById('toggle-anti-glare').addEventListener('change', function(e) {
        const enabled = e.target.checked;
        socket.emit('toggle_anti_glare', {enabled: enabled});
        // Log message will be sent from server
    });

    // Toggle debug trace
    document.getElementById('toggle-debug-trace').addEventListener('change', function(e) {
        const enabled = e.target.checked;
        socket.emit('toggle_debug_trace', {enabled: enabled});
        addLog(timeNow(), 'info', `Debug trace ${enabled ? 'enabled' : 'disabled'}`);
    });

    // Toggle audio
    document.getElementById('toggle-audio').addEventListener('change', function(e) {
        const enabled = e.target.checked;
        audioManager.setEnabled(enabled);
        addLog(timeNow(), 'info', `Sound effects ${enabled ? 'enabled' : 'disabled'}`);

        // Play test sound when enabling
        if (enabled) {
            audioManager.playSuccess();
        }
    });

    // Volume slider
    document.getElementById('audio-volume').addEventListener('input', function(e) {
        const volume = e.target.value / 100; // Convert 0-100 to 0.0-1.0
        audioManager.setVolume(volume);
        document.getElementById('volume-value').textContent = e.target.value + '%';
    });

    // Change AI provider: show its key / address field; switch once it has a key
    document.getElementById('ai-provider').addEventListener('change', function(e) {
        const provider = e.target.value;
        currentProvider = provider;
        renderCredentialField(provider);

        if (hasCredential(provider)) {
            applyProvider(provider);
        } else {
            populateModelDropdown(provider);
            document.getElementById('ai-credential').focus();
            notify(`Enter your ${PROVIDER_INFO[provider].name} API key to use it`, 'warning');
        }
    });

    document.getElementById('prompt-text').addEventListener('input', onPromptInput);

    loadGames();
    setupAreaDrawing();
    document.getElementById('game-select').addEventListener('change', function(e) {
        socket.emit('set_game', {game: e.target.value});
    });

    // Enter in the key field saves it
    document.getElementById('ai-credential').addEventListener('keydown', function(e) {
        if (e.key === 'Enter') saveCredential();
    });

    // Change AI model
    document.getElementById('ai-model').addEventListener('change', function(e) {
        const model = e.target.value;
        const provider = currentProvider;

        // Send update to server
        socket.emit('set_ai_provider', {provider: provider, model: model});
        addLog(timeNow(), 'info', `Changing model to ${model}...`);
    });
    
    // Load stats every 5 seconds
    setInterval(loadStats, 5000);

    // Initial stats load
    loadStats();

    // Load available models first, then load current provider/model
    loadAIModels();
    // Small delay to ensure models are loaded first; the key field needs the current provider
    setTimeout(() => loadAIProvider().then(loadCredentials).then(loadPrompts), 100);

});

// ============================================================================
// Inventory Viewer Functions
// ============================================================================

let currentInventory = [];
let filteredInventory = [];

function showInventory() {
    document.getElementById('inventory-modal').classList.add('show');
    loadInventory();
}

function closeInventory() {
    document.getElementById('inventory-modal').classList.remove('show');
}

function loadInventory() {
    fetch('/api/inventory')
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                currentInventory = data.cards;
                filteredInventory = data.cards;
                renderInventory();
            } else {
                document.getElementById('inventory-list').innerHTML =
                    '<div class="empty-state is-error">Error loading inventory</div>';
            }
        })
        .catch(error => {
            console.error('Error loading inventory:', error);
            document.getElementById('inventory-list').innerHTML =
                '<div class="empty-state is-error">Failed to load inventory</div>';
        });
}

function renderInventory() {
    const statsDiv = document.getElementById('inventory-stats');
    const listDiv = document.getElementById('inventory-list');

    if (filteredInventory.length === 0) {
        statsDiv.innerHTML = '';
        listDiv.innerHTML = currentInventory.length
            ? '<div class="empty-state">No cards match the filter.</div>'
            : '<div class="empty-state">No cards in inventory yet.<br>Start scanning cards to build your collection!</div>';
        return;
    }

    const totalCards = filteredInventory.reduce((sum, card) => sum + card.quantity, 0);
    const totalValue = filteredInventory.reduce((sum, card) => sum + card.price * card.quantity, 0);
    // Copies in any finish other than the default one (foil, surge foil, holo, ...)
    const specialCount = filteredInventory.reduce((sum, card) => sum + (card.finish !== defaultFinish() ? card.quantity : 0), 0);

    statsDiv.innerHTML = `
        <div class="inventory-stat">
            <div class="inventory-stat-value">${totalCards}</div>
            <div class="inventory-stat-label">Total Cards</div>
        </div>
        <div class="inventory-stat">
            <div class="inventory-stat-value">$${totalValue.toFixed(2)}</div>
            <div class="inventory-stat-label">Total Value</div>
        </div>
        <div class="inventory-stat">
            <div class="inventory-stat-value">${specialCount}</div>
            <div class="inventory-stat-label">Foil Cards</div>
        </div>
        <div class="inventory-stat">
            <div class="inventory-stat-value">$${(totalValue / totalCards).toFixed(2)}</div>
            <div class="inventory-stat-label">Avg. Value</div>
        </div>
    `;

    listDiv.innerHTML = filteredInventory.map((card, index) => {
        const rarity = (card.rarity || '').toLowerCase();
        const special = card.finish !== defaultFinish();
        return `
            <div class="inventory-card">
                <div class="inventory-card-number">#${index + 1}</div>
                <div class="inventory-card-info">
                    <div class="inventory-card-name">
                        ${card.quantity > 1 ? `<span class="inventory-qty">${card.quantity}×</span> ` : ''}${escapeHtml(card.name)}
                    </div>
                    <div class="inventory-card-details">
                        ${escapeHtml(card.set_name)} ${card.number ? '#' + escapeHtml(card.number) : ''}
                        ${card.type_line ? '· ' + escapeHtml(card.type_line) : ''}
                    </div>
                    <div class="inventory-card-meta">
                        ${rarity ? `<span class="inventory-badge ${escapeHtml(rarity)}">${escapeHtml(rarity.toUpperCase())}</span>` : ''}
                        ${special ? `<span class="inventory-badge ${escapeHtml(card.finish)}">${escapeHtml(finishLabel(card.finish).toUpperCase())}</span>` : ''}
                        ${card.color_identity ? `<span class="inventory-badge">${escapeHtml(card.color_identity)}</span>` : ''}
                        <span>${escapeHtml(card.condition)}</span>
                    </div>
                </div>
                <div class="inventory-card-price">
                    <div class="inventory-price-value">$${(card.price * card.quantity).toFixed(2)}</div>
                    ${card.quantity > 1 ? `<div class="inventory-price-each">$${card.price.toFixed(2)} each</div>` : ''}
                    <div class="inventory-timestamp">${escapeHtml(card.timestamp)}</div>
                </div>
                <div class="inventory-card-actions">
                    <button class="btn-edit" data-id="${card.id}" title="Edit">
                        <svg class="icon"><use href="#i-edit"/></svg>
                    </button>
                    <button class="btn-delete" data-id="${card.id}" title="Delete">
                        <svg class="icon"><use href="#i-trash"/></svg>
                    </button>
                </div>
            </div>
        `;
    }).join('');
}

function filterInventory() {
    const searchTerm = document.getElementById('inventory-search').value.toLowerCase();
    filteredInventory = !searchTerm ? currentInventory : currentInventory.filter(card =>
        [card.name, card.set_name, card.rarity, card.color_identity, card.type_line]
            .some(value => (value || '').toLowerCase().includes(searchTerm)));
    renderInventory();
}

function refreshInventory() {
    loadInventory();
    addLog(timeNow(), 'info', 'Inventory refreshed');
}

function exportInventory(format, label) {
    // The server sends the file as a download
    const downloadLink = document.createElement('a');
    downloadLink.href = `/api/export_inventory/${encodeURIComponent(format)}`;
    downloadLink.download = '';
    document.body.appendChild(downloadLink);
    downloadLink.click();
    document.body.removeChild(downloadLink);
    addLog(timeNow(), 'success', `${label} export started - check your downloads folder`);
    if (format === 'moxfield') {
        addLog(timeNow(), 'info', 'Import it at moxfield.com/account/collection');
    }
}

function renderExportButtons() {
    document.getElementById('export-buttons').innerHTML = gameInfo.exports.map(([format, label]) =>
        `<button class="btn btn-small" onclick="exportInventory('${escapeHtml(format)}', '${escapeHtml(label)}')">Export ${escapeHtml(label)}</button>`
    ).join('');
}

function updateDatabase() {
    const updateBtn = document.getElementById('update-database-btn');

    // Disable button and show loading state
    updateBtn.disabled = true;
    updateBtn.textContent = 'Updating...';

    addLog(timeNow(), 'info', 'Starting database update from Scryfall...');
    addLog(timeNow(), 'warning', 'This will take 5-10 minutes. Please do not close the browser.');

    // Emit the update request
    socket.emit('update_database');
}

async function rebuildDatabase() {
    const rebuildBtn = document.getElementById('rebuild-database-btn');

    const ok = await confirmDialog({
        title: 'Rebuild database schema?',
        message: 'Reorders the card table and rebuilds its indexes (about 30 seconds). Your inventory is not touched.',
        confirmText: 'Rebuild'
    });
    if (!ok) return;

    // Disable button and show loading state
    rebuildBtn.disabled = true;
    rebuildBtn.textContent = 'Rebuilding...';

    addLog(timeNow(), 'info', 'Starting database schema rebuild...');
    addLog(timeNow(), 'info', 'This will optimize database structure and indexes (~30 seconds)');

    // Emit the rebuild request
    socket.emit('rebuild_database');
}

async function importInventory() {
    const fileInput = document.getElementById('import-file-input');
    const file = fileInput.files[0];

    if (!file) {
        addLog(timeNow(), 'warning', 'No file selected');
        return;
    }

    // Check file extension
    if (!file.name.endsWith('.csv')) {
        addLog(timeNow(), 'error', 'Only CSV files are supported');
        fileInput.value = ''; // Clear the input
        return;
    }

    const mode = await choiceDialog({
        title: `Import ${file.name}`,
        message: 'Add the cards in this file to your inventory (quantities of matching cards are added up), or replace your whole inventory with this file?',
        choices: [
            {label: 'Cancel', value: null},
            {label: 'Replace inventory', value: 'replace', style: 'danger'},
            {label: 'Add to inventory', value: 'merge', style: 'primary'}
        ]
    });
    if (!mode) {
        fileInput.value = '';
        return;
    }
    const replaceExisting = mode === 'replace';

    // Show loading state
    addLog(timeNow(), 'info', `Importing inventory from ${file.name}...`);

    // Create form data
    const formData = new FormData();
    formData.append('file', file);
    formData.append('replace_existing', replaceExisting ? 'true' : 'false');

    // Upload file
    fetch('/api/import_inventory', {
        method: 'POST',
        body: formData
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            const stats = data.stats;
            addLog(timeNow(), 'success',
                `Import complete! Added: ${stats.added}, Updated: ${stats.updated}, Skipped: ${stats.skipped}, Errors: ${stats.errors}`
            );

            // Reload inventory and stats
            loadInventory();
            loadStats();

            // Clear the file input
            fileInput.value = '';
        } else {
            notify('Import failed: ' + (data.error || 'Unknown error'), 'error');
            fileInput.value = '';
        }
    })
    .catch(error => {
        console.error('Import error:', error);
        notify('Import failed - check the file format and try again (' + error + ')', 'error');
        fileInput.value = '';
    });
}

async function clearInventory() {
    const count = document.getElementById('inv-cards').textContent;
    const ok = await confirmDialog({
        title: 'Clear the whole inventory?',
        message: `This permanently deletes all ${count} cards from your inventory and can't be undone.\n\nExport a CSV first if you want a backup.`,
        confirmText: 'Delete everything',
        danger: true
    });
    if (!ok) return;

    // Show loading state
    addLog(timeNow(), 'warning', 'Clearing inventory...');

    // Call API to clear inventory
    fetch('/api/clear_inventory', {
        method: 'POST'
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            notify(`Inventory cleared: ${data.deleted} entries removed`, 'success');

            // Reload inventory and stats
            loadInventory();
            loadStats();
        } else {
            notify('Failed to clear inventory: ' + (data.error || 'Unknown error'), 'error');
        }
    })
    .catch(error => {
        console.error('Clear inventory error:', error);
        notify('Failed to clear inventory: ' + error, 'error');
    });
}

let currentEditId = null;

// Original finish and quantity, to detect a split
let originalFinish = null;
let originalQuantity = 0;

function renderEditFinishes(selected) {
    document.getElementById('edit-finishes').innerHTML = gameInfo.finishes.map(([key, label]) => `
        <label class="radio-pill">
            <input type="radio" name="edit-finish" value="${escapeHtml(key)}" ${key === selected ? 'checked' : ''} onchange="updateSplitQuantityVisibility()">
            <span>${escapeHtml(label)}</span>
        </label>`).join('');
}

function editCard(card) {
    currentEditId = card.id;
    originalQuantity = card.quantity;
    originalFinish = card.finish;

    document.getElementById('edit-card-name').textContent = card.name;
    document.getElementById('edit-quantity').value = card.quantity;
    document.getElementById('edit-condition').value = card.condition;
    renderEditFinishes(card.finish);

    const splitInput = document.getElementById('edit-split-quantity');
    splitInput.value = 1;
    splitInput.max = originalQuantity;
    splitInput.oninput = updateSplitPreview;
    document.getElementById('split-quantity-section').style.display = 'none';

    document.getElementById('edit-card-modal').classList.add('show');
}

function closeEditCard() {
    document.getElementById('edit-card-modal').classList.remove('show');
    currentEditId = null;
    originalFinish = null;
    originalQuantity = 0;
}

function updateSplitQuantityVisibility() {
    const finish = document.querySelector('input[name="edit-finish"]:checked').value;
    const splitSection = document.getElementById('split-quantity-section');

    // Several copies and a new finish: ask how many get it
    if (finish !== originalFinish && originalQuantity > 1) {
        splitSection.style.display = 'block';
        updateSplitPreview();
    } else {
        splitSection.style.display = 'none';
    }
}

function updateSplitPreview() {
    const splitQuantity = parseInt(document.getElementById('edit-split-quantity').value) || 1;
    const remaining = originalQuantity - splitQuantity;
    const preview = document.getElementById('split-preview');

    if (preview) {
        preview.innerHTML = `<strong>${splitQuantity}</strong> ${escapeHtml(finishLabel(document.querySelector('input[name="edit-finish"]:checked').value))} + <strong>${remaining}</strong> stay ${escapeHtml(finishLabel(originalFinish))}`;
    }
}

function updateSplitMaxQuantity() {
    const newQuantity = parseInt(document.getElementById('edit-quantity').value) || 1;
    const splitInput = document.getElementById('edit-split-quantity');

    if (splitInput) {
        // Update max to the new total quantity
        splitInput.max = newQuantity;

        // If current split value exceeds new max, adjust it
        if (parseInt(splitInput.value) > newQuantity) {
            splitInput.value = newQuantity;
        }

        // Update preview
        updateSplitPreview();
    }
}

function saveEditCard() {
    if (currentEditId === null) {
        notify('No card selected for editing', 'error');
        return;
    }

    const quantity = parseInt(document.getElementById('edit-quantity').value);
    const condition = document.getElementById('edit-condition').value;
    const finish = document.querySelector('input[name="edit-finish"]:checked').value;

    if (isNaN(quantity) || quantity < 1 || quantity > 999) {
        notify('Please enter a quantity between 1 and 999', 'warning');
        return;
    }

    const requestBody = {quantity: quantity, condition: condition, finish: finish};
    if (finish !== originalFinish && originalQuantity > 1) {
        const splitQuantity = parseInt(document.getElementById('edit-split-quantity').value) || 1;
        if (splitQuantity < 1 || splitQuantity > originalQuantity) {
            notify(`Split quantity must be between 1 and ${originalQuantity}`, 'warning');
            return;
        }
        requestBody.split_quantity = splitQuantity;
    }

    const cardName = document.getElementById('edit-card-name').textContent;
    const rowId = currentEditId;
    closeEditCard();
    addLog(timeNow(), 'info', `Updating ${cardName}...`);

    fetch(`/api/inventory/update/${rowId}`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(requestBody)
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            addLog(timeNow(), 'success', `${cardName} updated` + (data.split ? ' (entry split by finish)' : ''));
            loadInventory();
            loadStats();
        } else {
            notify('Failed to update card: ' + (data.error || data.message || 'Unknown error'), 'error');
        }
    })
    .catch(error => notify('Failed to update card: ' + error.message, 'error'));
}

async function deleteCard(rowId, cardName) {
    const ok = await confirmDialog({
        title: 'Delete card?',
        message: `Delete "${cardName}" from your inventory? This can't be undone.`,
        confirmText: 'Delete',
        danger: true
    });
    if (!ok) return;

    // Show loading state
    addLog(timeNow(), 'info', `Deleting ${cardName}...`);

    // Delete via API
    fetch(`/api/inventory/delete/${rowId}`, {
        method: 'DELETE'
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            addLog(timeNow(), 'success', `${cardName} deleted from inventory`);
            // Reload inventory
            loadInventory();
            // Update main stats
            loadStats();
        } else {
            notify('Failed to delete card: ' + (data.error || 'Unknown error'), 'error');
        }
    })
    .catch(error => {
        console.error('Delete error:', error);
        notify('Failed to delete card: ' + error, 'error');
    });
}

// ============================================================================
// In-page dialogs and notifications
// Native confirm()/alert() can be silently blocked by the browser ("prevent this page from
// creating additional dialogs"), after which confirm() always returns false - so the app
// uses its own.
// ============================================================================

let dialogResolve = null;

function choiceDialog({title, message, choices}) {
    // choices: [{label, value, style: 'primary' | 'danger' | undefined}]; resolves with the
    // chosen value, or null when dismissed (Escape, click outside)
    return new Promise(resolve => {
        if (dialogResolve) dialogResolve(null);
        dialogResolve = resolve;
        document.getElementById('dialog-title').textContent = title;
        document.getElementById('dialog-message').textContent = message;
        const buttons = document.getElementById('dialog-buttons');
        buttons.innerHTML = '';
        choices.forEach(choice => {
            const button = document.createElement('button');
            button.className = 'btn' + (choice.style ? ` btn-${choice.style}` : '');
            button.textContent = choice.label;
            button.onclick = () => closeDialog(choice.value);
            buttons.appendChild(button);
        });
        document.getElementById('dialog-modal').classList.add('show');
        buttons.firstChild.focus();  // the safe choice (Cancel) is first
    });
}

function closeDialog(value = null) {
    document.getElementById('dialog-modal').classList.remove('show');
    const resolve = dialogResolve;
    dialogResolve = null;
    if (resolve) resolve(value);
}

function confirmDialog({title, message, confirmText = 'OK', danger = false}) {
    return choiceDialog({title, message, choices: [
        {label: 'Cancel', value: false},
        {label: confirmText, value: true, style: danger ? 'danger' : 'primary'}
    ]}).then(value => value === true);
}

function notify(message, level = 'info') {
    // Shows a short notification and records it in the activity log
    addLog(timeNow(), level, message);
    const toast = document.createElement('div');
    toast.className = `toast ${level}`;
    toast.textContent = message;
    document.getElementById('toast-container').appendChild(toast);
    setTimeout(() => toast.remove(), 5000);
}

function openSettings() {
    document.getElementById('settings-drawer').classList.add('show');
}

function closeSettings() {
    document.getElementById('settings-drawer').classList.remove('show');
}

// Close the topmost overlay with Escape
document.addEventListener('keydown', function(event) {
    if (event.key !== 'Escape') return;
    if (document.getElementById('area-layer').classList.contains('is-drawing')) {
        cancelDrawArea();
    } else if (document.getElementById('dialog-modal').classList.contains('show')) {
        closeDialog(null);
    } else if (document.getElementById('edit-card-modal').classList.contains('show')) {
        closeEditCard();
    } else if (document.getElementById('prompt-modal').classList.contains('show')) {
        closePromptEditor();
    } else if (document.getElementById('inventory-modal').classList.contains('show')) {
        closeInventory();
    } else {
        closeSettings();
    }
});

// Close modal when clicking outside
window.onclick = function(event) {
    const inventoryModal = document.getElementById('inventory-modal');
    const editModal = document.getElementById('edit-card-modal');

    if (event.target === document.getElementById('settings-drawer')) {
        closeSettings();
    }
    if (event.target === document.getElementById('dialog-modal')) {
        closeDialog(null);
    }
    if (event.target === inventoryModal) {
        closeInventory();
    }
    if (event.target === document.getElementById('prompt-modal')) {
        closePromptEditor();
    }
    if (event.target === editModal) {
        closeEditCard();
    }
}

// Event delegation for inventory action buttons
document.addEventListener('DOMContentLoaded', function() {
    const inventoryList = document.getElementById('inventory-list');

    if (inventoryList) {
        inventoryList.addEventListener('click', function(event) {
            const target = event.target;

            const button = target.closest('.btn-edit, .btn-delete');
            if (!button) return;
            const card = currentInventory.find(entry => entry.id === parseInt(button.dataset.id));
            if (!card) return;
            if (button.classList.contains('btn-delete')) {
                deleteCard(card.id, card.name);
            } else {
                editCard(card);
            }
        });
    }
});

// Banner removed - using simple flash feedback only

// Test audio function (called from HTML button)
function testAudio() {
    console.log('🔊 Test Audio button clicked');
    addLog(timeNow(), 'info', 'Testing audio system...');

    // Ensure audio context is initialized
    audioManager.ensureAudioContext().then(() => {
        console.log('🎵 Audio context initialized, playing test sounds...');

        // Play all sounds in sequence
        audioManager.playCapture();
        addLog(timeNow(), 'info', '1/3: Capture sound');

        setTimeout(() => {
            audioManager.playSuccess();
            addLog(timeNow(), 'info', '2/3: Success sound');
        }, 400);

        setTimeout(() => {
            audioManager.playNextReady();
            addLog(timeNow(), 'success', '3/3: Next Ready sound - Check browser console');
        }, 1000);
    });
}
