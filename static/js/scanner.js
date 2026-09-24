// Card Scanner Frontend JavaScript

const socket = io();
let cardNumber = 1;
let currentImagePath = '';
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
    currentImagePath = data.image_path;
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

    // In fast scan mode, play "ready for next card" sound, otherwise regular success
    if (fastScanMode) {
        console.log('✅ Card added - playing NEXT READY sound');
        audioManager.playNextReady();
        // Show clear message
    } else {
        audioManager.playSuccess();
    }

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
    currentImagePath = '';
    document.getElementById('search-btn').disabled = true;
});

socket.on('error', function(data) {
    console.error(logSeparator());
    console.error('ERROR EVENT RECEIVED');
    console.error(logSeparator());
    console.error('Error message:', data.message);

    // Play error sound
    audioManager.playError();

    addLog(timeNow(), 'error', data.message);
});

socket.on('auto_capture_triggered', function(data) {
    console.log(logSeparator());
    console.log('AUTO-CAPTURE TRIGGERED');
    console.log(logSeparator());
    console.log('Card number:', data.card_number);

    // Play capture sound immediately when image is captured
    console.log('📸 Playing capture sound...');
    audioManager.playCapture().catch(err => {
        console.error('❌ Failed to play capture sound:', err);
    });

    // Flash animation on video container (visual feedback)
    const videoContainer = document.querySelector('.video-container');
    videoContainer.classList.add('capture-flash');
    setTimeout(() => videoContainer.classList.remove('capture-flash'), 500);

    // In fast scan mode, show clear instruction in log
    if (fastScanMode) {
        addLog(timeNow(), 'info', '📸 CAPTURED! Remove card...');
    }

    addLog(timeNow(), 'info', `Auto-capture triggered for card #${data.card_number}`);
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
    addLog(timeNow(), 'success', data.message);
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
    console.log('Cards with prices:', data.cards_with_prices);

    // Play queue alert sound (triple beep for major operation)
    audioManager.playQueueAlert();

    const updateBtn = document.getElementById('update-database-btn');
    updateBtn.disabled = false;
    updateBtn.textContent = 'Update card database';

    addLog(timeNow(), 'success', `Database updated! ${data.total_cards.toLocaleString()} cards loaded.`);
    addLog(timeNow(), 'success', `${data.cards_with_prices.toLocaleString()} cards have pricing data.`);
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
    console.log('Inventory imported:', data.inventory_imported);
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
        })
        .catch(error => console.error('Error loading scan settings:', error));
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
    const finishLabels = {regular: 'Regular', foil: 'Foil', surge: 'Surge foil'};
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
                ${quantityCell('regular-qty', 'Regular', 'regular', suggestion.finish === 'regular' ? 1 : 0)}
                ${quantityCell('foil-qty', 'Foil', 'foil', suggestion.finish === 'foil' ? 1 : 0)}
                ${quantityCell('surge-qty', 'Surge foil', 'surge', suggestion.finish === 'surge' ? 1 : 0)}
            </div>
            ${suggestion.reason ? `<div class="finish-hint ${suggestion.finish}">${finishLabels[suggestion.finish]}: ${suggestion.reason}</div>` : ''}
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

    const regularQty = parseInt(document.getElementById('regular-qty').value) || 0;
    const foilQty = parseInt(document.getElementById('foil-qty').value) || 0;
    const surgeQty = parseInt(document.getElementById('surge-qty').value) || 0;
    const condition = document.getElementById('condition').value;

    if (regularQty === 0 && foilQty === 0 && surgeQty === 0) {
        addLog(timeNow(), 'warning', 'Please set at least one quantity');
        return;
    }

    // Add regular cards if quantity > 0
    if (regularQty > 0) {
        socket.emit('add_to_inventory', {
            quantity: regularQty,
            condition: condition,
            is_foil: false,
            is_surge: false,
            image_path: currentImagePath
        });
    }

    // Add foil cards if quantity > 0
    if (foilQty > 0) {
        socket.emit('add_to_inventory', {
            quantity: foilQty,
            condition: condition,
            is_foil: true,
            is_surge: false,
            image_path: currentImagePath
        });
    }

    // Add surge foil cards if quantity > 0
    if (surgeQty > 0) {
        socket.emit('add_to_inventory', {
            quantity: surgeQty,
            condition: condition,
            is_foil: false,
            is_surge: true,
            image_path: currentImagePath
        });
    }
}

function addToInventory(autoMode = false) {
    // Automatic add: one Near Mint copy in the suggested finish
    console.log(`addToInventory called with autoMode=${autoMode}, currentCard:`, currentCard);

    if (!currentCard) {
        console.error('addToInventory: No card selected!');
        addLog(timeNow(), 'error', 'No card selected');
        return;
    }

    const finish = suggestedFinish(currentCard).finish;
    const data = {
        quantity: 1,
        condition: 'Near Mint',
        is_foil: finish === 'foil',
        is_surge: finish === 'surge',
        image_path: currentImagePath
    };
    console.log('Emitting add_to_inventory event with:', data);
    socket.emit('add_to_inventory', data);
}

function dismissCard() {
    console.log('Card dismissed by user');
    currentCard = null;
    currentImagePath = null;
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
    // Load the provider's models and switch the scanner to it
    const loaded = provider === 'local' ? loadLocalModels() : Promise.resolve(populateModelDropdown(provider));
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
    setTimeout(() => loadAIProvider().then(loadCredentials), 100);

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
        listDiv.innerHTML = '<div class="empty-state">No cards in inventory yet.<br>Start scanning cards to build your collection!</div>';
        return;
    }

    // Calculate stats
    const totalCards = filteredInventory.reduce((sum, card) => {
        const quantity = parseInt(card.Quantity || 1);
        return sum + quantity;
    }, 0);

    const totalValue = filteredInventory.reduce((sum, card) => {
        const price = parseFloat(card['Price (USD)'].replace('$', '')) || 0;
        const quantity = parseInt(card.Quantity || 1);
        return sum + (price * quantity);
    }, 0);

    const foilCount = filteredInventory.reduce((sum, card) => {
        if (card.Foil === 'Yes') {
            const quantity = parseInt(card.Quantity || 1);
            return sum + quantity;
        }
        return sum;
    }, 0);

    const rarityBreakdown = {};
    filteredInventory.forEach(card => {
        const rarity = card.Rarity || 'Unknown';
        const quantity = parseInt(card.Quantity || 1);
        rarityBreakdown[rarity] = (rarityBreakdown[rarity] || 0) + quantity;
    });

    // Render stats
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
            <div class="inventory-stat-value">${foilCount}</div>
            <div class="inventory-stat-label">Foil Cards</div>
        </div>
        <div class="inventory-stat">
            <div class="inventory-stat-value">$${(totalValue / totalCards).toFixed(2)}</div>
            <div class="inventory-stat-label">Avg. Value</div>
        </div>
    `;

    // Render card list
    listDiv.innerHTML = filteredInventory.map((card, index) => {
        const price = parseFloat(card['Price (USD)'].replace('$', '')) || 0;
        const quantity = parseInt(card.Quantity || 1);
        const totalValue = price * quantity;
        const rarity = (card.Rarity || '').toLowerCase();
        const foil = card.Foil === 'Yes';
        const surge = card.Surge === 'Yes';
        const colorIdentity = card['Color Identity'] || '';

        // Find actual index in currentInventory
        const actualIndex = currentInventory.indexOf(card);

        return `
            <div class="inventory-card">
                <div class="inventory-card-number">#${index + 1}</div>
                <div class="inventory-card-info">
                    <div class="inventory-card-name">
                        ${quantity > 1 ? `<span class="inventory-qty">${quantity}×</span> ` : ''}${escapeHtml(card['Card Name'])}
                    </div>
                    <div class="inventory-card-details">
                        ${escapeHtml(card.Set)} ${card['Card Number'] ? '#' + card['Card Number'] : ''}
                        ${card.Type ? '· ' + escapeHtml(card.Type) : ''}
                    </div>
                    <div class="inventory-card-meta">
                        ${rarity ? `<span class="inventory-badge ${rarity}">${rarity.toUpperCase()}</span>` : ''}
                        ${surge ? '<span class="inventory-badge surge">SURGE</span>' : (foil ? '<span class="inventory-badge foil">FOIL</span>' : '')}
                        ${colorIdentity ? `<span class="inventory-badge">${escapeHtml(colorIdentity)}</span>` : ''}
                        <span>${escapeHtml(card.Condition || 'Near Mint')}</span>
                    </div>
                </div>
                <div class="inventory-card-price">
                    <div class="inventory-price-value">$${totalValue.toFixed(2)}</div>
                    ${quantity > 1 ? `<div class="inventory-price-each">$${price.toFixed(2)} each</div>` : ''}
                    <div class="inventory-timestamp">${card.Timestamp}</div>
                </div>
                <div class="inventory-card-actions">
                    <button class="btn-edit"
                        data-index="${actualIndex}"
                        data-quantity="${quantity}"
                        data-card-name="${escapeHtml(card['Card Name'])}"
                        data-condition="${escapeHtml(card.Condition || 'Near Mint')}"
                        data-foil="${foil ? 'Yes' : 'No'}"
                        data-surge="${surge ? 'Yes' : 'No'}"
                        title="Edit">
                        <svg class="icon"><use href="#i-edit"/></svg>
                    </button>
                    <button class="btn-delete" data-index="${actualIndex}" data-card-name="${escapeHtml(card['Card Name'])}" title="Delete">
                        <svg class="icon"><use href="#i-trash"/></svg>
                    </button>
                </div>
            </div>
        `;
    }).join('');
}

function filterInventory() {
    const searchTerm = document.getElementById('inventory-search').value.toLowerCase();

    if (!searchTerm) {
        filteredInventory = currentInventory;
    } else {
        filteredInventory = currentInventory.filter(card => {
            return card['Card Name'].toLowerCase().includes(searchTerm) ||
                   (card.Set || '').toLowerCase().includes(searchTerm) ||
                   (card.Rarity || '').toLowerCase().includes(searchTerm) ||
                   (card['Color Identity'] || '').toLowerCase().includes(searchTerm) ||
                   (card.Type || '').toLowerCase().includes(searchTerm);
        });
    }

    renderInventory();
}

function refreshInventory() {
    loadInventory();
    addLog(timeNow(), 'info', 'Inventory refreshed');
}

function exportInventory() {
    // Create a temporary link element to trigger the download
    const downloadLink = document.createElement('a');
    downloadLink.href = '/api/export_inventory';
    downloadLink.download = ''; // Let the server set the filename

    // Append to body, click, and remove
    document.body.appendChild(downloadLink);
    downloadLink.click();
    document.body.removeChild(downloadLink);

    // Log the action
    addLog(timeNow(), 'success', 'Inventory export started...');

    // Optional: Show a brief success message
    setTimeout(() => {
        addLog(timeNow(), 'success', 'Check your downloads folder for the CSV file');
    }, 500);
}

function exportInventoryMoxfield() {
    // Create a temporary link element to trigger the download
    const downloadLink = document.createElement('a');
    downloadLink.href = '/api/export_inventory_moxfield';
    downloadLink.download = ''; // Let the server set the filename

    // Append to body, click, and remove
    document.body.appendChild(downloadLink);
    downloadLink.click();
    document.body.removeChild(downloadLink);

    // Log the action
    addLog(timeNow(), 'success', 'Moxfield export started...');

    // Show success message with import link
    setTimeout(() => {
        addLog(timeNow(), 'success', 'Moxfield CSV ready! Import at: moxfield.com/account/collection');
    }, 500);
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

let currentEditIndex = null;

// Store original foil type and quantity for split detection
let originalFoilType = null;
let originalQuantity = 0;

function editCard(index, quantity, cardName, condition, isFoil, isSurge) {
    // Store the index we're editing
    currentEditIndex = index;

    // Store original quantity
    originalQuantity = parseInt(quantity);

    // Set card name in modal
    document.getElementById('edit-card-name').textContent = cardName;

    // Set current values
    document.getElementById('edit-quantity').value = quantity;
    document.getElementById('edit-condition').value = condition;

    // Set foil type radio button
    let foilType = 'non-foil';
    if (isSurge === 'Yes') {
        foilType = 'surge';
    } else if (isFoil === 'Yes') {
        foilType = 'foil';
    }

    // Store original foil type
    originalFoilType = foilType;

    const radioButtons = document.getElementsByName('edit-foil-type');
    radioButtons.forEach(radio => {
        radio.checked = (radio.value === foilType);
    });

    // Initialize split quantity input
    const splitInput = document.getElementById('edit-split-quantity');
    if (splitInput) {
        splitInput.value = 1;
        splitInput.max = originalQuantity;
        // Add input event listener for preview updates
        splitInput.oninput = updateSplitPreview;
    }

    // Hide split quantity section initially
    const splitSection = document.getElementById('split-quantity-section');
    if (splitSection) {
        splitSection.style.display = 'none';
    }

    // Show modal
    document.getElementById('edit-card-modal').classList.add('show');
}

function closeEditCard() {
    document.getElementById('edit-card-modal').classList.remove('show');
    currentEditIndex = null;
    originalFoilType = null;
    originalQuantity = 0;
}

function updateSplitQuantityVisibility() {
    const currentFoilType = document.querySelector('input[name="edit-foil-type"]:checked').value;
    const splitSection = document.getElementById('split-quantity-section');

    // Show split section only if foil type changed AND original quantity > 1
    if (currentFoilType !== originalFoilType && originalQuantity > 1) {
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
        preview.innerHTML = `This will create: <strong>${splitQuantity}</strong> with new foil type + <strong>${remaining}</strong> remaining with original foil type`;
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
    console.log('saveEditCard called, currentEditIndex:', currentEditIndex);

    if (currentEditIndex === null) {
        console.error('currentEditIndex is null!');
        notify('No card selected for editing', 'error');
        return;
    }

    // Get values from form
    const quantity = parseInt(document.getElementById('edit-quantity').value);
    const condition = document.getElementById('edit-condition').value;
    const foilType = document.querySelector('input[name="edit-foil-type"]:checked').value;

    // Get split quantity if foil type changed
    let splitQuantity = null;
    if (foilType !== originalFoilType && originalQuantity > 1) {
        splitQuantity = parseInt(document.getElementById('edit-split-quantity').value) || 1;

        // Validate split quantity
        if (splitQuantity < 1 || splitQuantity > originalQuantity) {
            notify(`Split quantity must be between 1 and ${originalQuantity}`, 'warning');
            return;
        }
    }

    console.log('Edit values:', {index: currentEditIndex, quantity, condition, foilType, splitQuantity});

    // Validate quantity
    if (isNaN(quantity) || quantity < 1 || quantity > 999) {
        notify('Please enter a quantity between 1 and 999', 'warning');
        return;
    }

    // Convert foil type to boolean flags
    const isFoil = (foilType === 'foil');
    const isSurge = (foilType === 'surge');

    // Show loading state
    const cardName = document.getElementById('edit-card-name').textContent;
    addLog(timeNow(), 'info', `Updating ${cardName}...`);

    // Save the index before closing modal (closeEditCard sets it to null)
    const indexToUpdate = currentEditIndex;

    // Close modal
    closeEditCard();

    const url = `/api/inventory/update/${indexToUpdate}`;
    console.log('Fetching URL:', url);

    // Update via API
    const requestBody = {
        quantity: quantity,
        condition: condition,
        is_foil: isFoil,
        is_surge: isSurge
    };

    // Add split_quantity if splitting
    if (splitQuantity !== null) {
        requestBody.split_quantity = splitQuantity;
    }

    fetch(url, {
        method: 'PUT',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(requestBody)
    })
    .then(response => {
        // Check if response is OK before parsing JSON
        if (!response.ok) {
            return response.text().then(text => {
                console.error('Server error response:', text);
                throw new Error(`Server error: ${response.status} ${response.statusText}`);
            });
        }
        return response.json();
    })
    .then(data => {
        if (data.success) {
            addLog(timeNow(), 'success', `${cardName} updated successfully`);
            if (data.split) {
                addLog(timeNow(), 'info', 'Entry was split due to foil type change');
            }
            // Reload inventory
            loadInventory();
            // Update main stats
            loadStats();
        } else {
            notify('Failed to update card: ' + (data.error || data.message || 'Unknown error'), 'error');
        }
    })
    .catch(error => {
        console.error('Update error:', error);
        notify('Failed to update card: ' + error.message, 'error');
    });
}

async function deleteCard(index, cardName) {
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
    fetch(`/api/inventory/delete/${index}`, {
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
    if (document.getElementById('dialog-modal').classList.contains('show')) {
        closeDialog(null);
    } else if (document.getElementById('edit-card-modal').classList.contains('show')) {
        closeEditCard();
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

            // Handle delete button clicks
            if (target.classList.contains('btn-delete') || target.closest('.btn-delete')) {
                const button = target.classList.contains('btn-delete') ? target : target.closest('.btn-delete');
                const index = parseInt(button.dataset.index);
                const cardName = button.dataset.cardName;

                if (!isNaN(index)) {
                    deleteCard(index, cardName);
                }
            }

            // Handle edit button clicks
            if (target.classList.contains('btn-edit') || target.closest('.btn-edit')) {
                const button = target.classList.contains('btn-edit') ? target : target.closest('.btn-edit');
                const index = parseInt(button.dataset.index);
                const quantity = parseInt(button.dataset.quantity);
                const cardName = button.dataset.cardName;
                const condition = button.dataset.condition;
                const foil = button.dataset.foil;
                const surge = button.dataset.surge;

                if (!isNaN(index) && !isNaN(quantity)) {
                    editCard(index, quantity, cardName, condition, foil, surge);
                }
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
