// Shared by the scanner page and the collection page: text helpers, the active game's
// finishes, in-page dialogs and notifications, the inventory edit dialog and the capture viewer.
// Each page defines inventoryChanged() (reload what it shows after an edit).

function timeNow() {
    // 24-hour HH:MM:SS, matching timestamps sent by the server
    return new Date().toLocaleTimeString('en-GB', {hour12: false});
}

let gameInfo = null;  // {id, label, finishes: [[key, label]], exports: [[key, label]]}

function defaultFinish() {
    return gameInfo.finishes[0][0];
}

function finishLabel(key) {
    const finish = gameInfo.finishes.find(([k]) => k === key);
    return finish ? finish[1] : key;
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

const RARITY_ORDER = {special: 0, bonus: 0, mythic: 1, rare: 2, uncommon: 3, common: 4};
const byText = (a, b) => (a || '').localeCompare(b || '', undefined, {numeric: true, sensitivity: 'base'});
const INVENTORY_SORTS = {
    newest: null,
    oldest: (a, b) => byText(a.timestamp, b.timestamp) || a.id - b.id,
    // When the entry came into the collection (one time per "Add to collection")
    added: (a, b) => byText(b.added_at, a.added_at) || b.id - a.id,
    name: (a, b) => byText(a.name, b.name),
    price_desc: (a, b) => (b.price || 0) - (a.price || 0),
    price_asc: (a, b) => (a.price || 0) - (b.price || 0),
    value_desc: (a, b) => (b.price || 0) * b.quantity - (a.price || 0) * a.quantity,
    quantity_desc: (a, b) => b.quantity - a.quantity,
    rarity: (a, b) => (RARITY_ORDER[a.rarity] ?? 9) - (RARITY_ORDER[b.rarity] ?? 9),
    set: (a, b) => byText(a.set_name, b.set_name) || byText(a.number, b.number),
};

// ============================================================================
// Inventory edit dialog (templates/_dialogs.html)
// ============================================================================

let currentEditId = null;

// The entry as it was, to detect a split (another finish or location for part of a stack)
let originalFinish = null;
let originalLocation = '';
let originalQuantity = 0;
let originalPrinting = '';   // card id of the entry's printing ('' = not in the list)

function areaQuery() {
    // The scanner page works on the scanned cards (it sets inventoryArea = 'scan'), the
    // collection page on the collection
    return typeof inventoryArea === 'string' ? `?area=${inventoryArea}` : '';
}

function logLine(level, message) {
    // The activity panel only exists on the scanner page
    if (typeof addLog === 'function') addLog(timeNow(), level, message);
}

function parseTags(text) {
    // "trade, Keep ,trade" -> ["trade", "Keep"]
    const seen = new Set();
    return (text || '').split(',').map(tag => tag.trim()).filter(tag => {
        const key = tag.toLowerCase();
        if (!tag || seen.has(key)) return false;
        seen.add(key);
        return true;
    });
}

function renderEditFinishes(selected) {
    document.getElementById('edit-finishes').innerHTML = gameInfo.finishes.map(([key, label]) => `
        <label class="radio-pill">
            <input type="radio" name="edit-finish" value="${escapeHtml(key)}" ${key === selected ? 'checked' : ''} onchange="updateSplitQuantityVisibility()">
            <span>${escapeHtml(label)}</span>
        </label>`).join('');
}

function editCard(card, locations = []) {
    // locations: the ones already in use, offered while typing
    currentEditId = card.id;
    originalQuantity = card.quantity;
    originalFinish = card.finish;
    originalLocation = card.location || '';

    document.getElementById('edit-card-name').textContent = card.name;
    document.getElementById('edit-quantity').value = card.quantity;
    document.getElementById('edit-condition').value = card.condition;
    document.getElementById('edit-location').value = originalLocation;
    document.getElementById('edit-location-options').innerHTML =
        locations.map(location => `<option value="${escapeHtml(location)}"></option>`).join('');
    document.getElementById('edit-tags').value = (card.tags || []).join(', ');
    renderEditFinishes(card.finish);

    loadEditPrintings(card.id);

    const splitInput = document.getElementById('edit-split-quantity');
    splitInput.value = 1;
    splitInput.max = originalQuantity;
    splitInput.oninput = updateSplitPreview;
    document.getElementById('split-quantity-section').style.display = 'none';

    document.getElementById('edit-card-modal').classList.add('show');
}

function loadEditPrintings(rowId) {
    // The other printings the entry can be changed to; hidden when the card has only one
    // (or the game has no list of printings)
    const group = document.getElementById('edit-printing-group'), select = document.getElementById('edit-printing');
    group.hidden = true;
    select.innerHTML = '';
    originalPrinting = '';
    fetch(`/api/inventory/${rowId}/printings${areaQuery()}`)
        .then(response => response.json())
        .then(data => {
            if (currentEditId !== rowId || !data.success || data.printings.length < 2) return;
            const price = value => value ? `$${Number(value).toFixed(2)}` : '';
            select.innerHTML = data.printings.map(printing => {
                const prices = [price(printing.price), printing.price_foil ? `foil ${price(printing.price_foil)}` : ''].filter(Boolean).join(' / ');
                return `<option value="${escapeHtml(printing.id)}">${escapeHtml(printing.set)} (${escapeHtml((printing.set_code || '').toUpperCase())}) #${escapeHtml(printing.number)}${prices ? ' - ' + prices : ''}</option>`;
            }).join('');
            const current = data.printings.find(printing => printing.current);
            // An entry whose printing the card data doesn't have (an old import): no choice made yet
            if (!current) select.insertAdjacentHTML('afterbegin', '<option value="">Keep as it is</option>');
            select.value = originalPrinting = current ? current.id : '';
            group.hidden = false;
        })
        .catch(() => {});  // the dialog works without the list
}

function editedPrinting() {
    // The printing chosen, '' when it is the one the entry already is
    const select = document.getElementById('edit-printing');
    return select.value !== originalPrinting ? select.value : '';
}

function stepNumber(id, delta) {
    // The - / + buttons beside a number field (.number-stepper): within its min and max,
    // and the field's own oninput runs as if the number had been typed
    const input = document.getElementById(id);
    const low = input.min === '' ? -Infinity : Number(input.min);
    const high = input.max === '' ? Infinity : Number(input.max);
    input.value = Math.max(low, Math.min(high, (parseInt(input.value) || 0) + delta));
    input.dispatchEvent(new Event('input', {bubbles: true}));
}

function closeEditCard() {
    document.getElementById('edit-card-modal').classList.remove('show');
    currentEditId = null;
    originalFinish = null;
    originalLocation = '';
    originalQuantity = 0;
}

function editedFinish() {
    return document.querySelector('input[name="edit-finish"]:checked').value;
}

function editedLocation() {
    return document.getElementById('edit-location').value.trim();
}

function editSplits() {
    // Several copies and a new finish or location: ask how many get it
    return originalQuantity > 1 && (editedFinish() !== originalFinish || editedLocation() !== originalLocation || editedPrinting() !== '');
}

function updateSplitQuantityVisibility() {
    const splitSection = document.getElementById('split-quantity-section');
    if (editSplits()) {
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
    if (!preview) return;
    const describe = (finish, location) => escapeHtml(finishLabel(finish) + (location ? `, ${location}` : ''));
    // "2 Foil, Box #1, other printing + 1 stay Regular"
    preview.innerHTML = `<strong>${splitQuantity}</strong> ${describe(editedFinish(), editedLocation())}`
        + (editedPrinting() ? ', other printing' : '')
        + ` + <strong>${remaining}</strong> stay ${describe(originalFinish, originalLocation)}`;
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

    if (isNaN(quantity) || quantity < 1 || quantity > 999) {
        notify('Please enter a quantity between 1 and 999', 'warning');
        return;
    }

    const requestBody = {quantity: quantity, condition: condition, finish: editedFinish(),
                         location: editedLocation(), tags: parseTags(document.getElementById('edit-tags').value)};
    if (editedPrinting()) requestBody.card_id = editedPrinting();
    if (editSplits()) {
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
    logLine('info', `Updating ${cardName}...`);

    fetch(`/api/inventory/update/${rowId}${areaQuery()}`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(requestBody)
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            logLine('success', `${cardName} updated` + (data.split ? ' (entry split)' : ''));
            inventoryChanged();
        } else {
            notify('Failed to update card: ' + (data.error || data.message || 'Unknown error'), 'error');
        }
    })
    .catch(error => notify('Failed to update card: ' + error.message, 'error'));
}

async function deleteCard(rowId, cardName) {
    const ok = await confirmDialog({
        title: 'Delete card?',
        message: `Delete "${cardName}"? This can't be undone.`,
        confirmText: 'Delete',
        danger: true
    });
    if (!ok) return;

    logLine('info', `Deleting ${cardName}...`);

    fetch(`/api/inventory/delete/${rowId}${areaQuery()}`, {
        method: 'DELETE'
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            logLine('success', `${cardName} deleted`);
            inventoryChanged();
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

// Scanned cards -> collection, from either page: asks for a location first

let toCollectionResolve = null;

function closeToCollection(location) {
    // location: what was typed ('' = none), or null when cancelled
    document.getElementById('to-collection-modal').classList.remove('show');
    const resolve = toCollectionResolve;
    toCollectionResolve = null;
    if (resolve) resolve(location === null ? null : location.trim());
}

async function addScannedToCollection() {
    // Resolves true when the cards were moved (the page then reloads its lists)
    try {
        const waiting = await (await fetch('/api/scan_inventory/to_collection')).json();
        if (!waiting.cards) {
            notify('No scanned cards to add', 'info');
            return false;
        }
        document.getElementById('to-collection-message').textContent =
            `The ${waiting.cards} scanned card${waiting.cards > 1 ? 's' : ''} move to your collection (cards you already have there get the copies added) and the scanned list is emptied.`;
        // The locations in use, to pick from; a new one is typed in the field below
        const used = document.getElementById('to-collection-used');
        // Boxes and binders first, the locations named after a deck under their own heading
        const decks = new Set((waiting.decks || []).map(name => name.toLowerCase()));
        const option = location => `<option value="${escapeHtml(location)}">${escapeHtml(location)}</option>`;
        const inDecks = waiting.locations.filter(location => decks.has(location.toLowerCase()));
        used.innerHTML = '<option value="">Locations you already use...</option>' +
            waiting.locations.filter(location => !inDecks.includes(location)).map(option).join('') +
            (inDecks.length ? `<optgroup label="──── Decks ────">${inDecks.map(option).join('')}</optgroup>` : '');
        used.hidden = !waiting.locations.length;
        const input = document.getElementById('to-collection-location');
        input.value = '';
        const location = await new Promise(resolve => {
            if (toCollectionResolve) toCollectionResolve(null);
            toCollectionResolve = resolve;
            document.getElementById('to-collection-modal').classList.add('show');
            input.focus();
        });
        if (location === null) return false;
        const response = await fetch('/api/scan_inventory/to_collection', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({location: location})
        });
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Unknown error');
        notify(`${data.cards} card${data.cards === 1 ? '' : 's'} added to the collection${location ? ` in ${location}` : ''}`, 'success');
        return true;
    } catch (error) {
        notify('Could not add to the collection: ' + error.message, 'error');
        return false;
    }
}

function notify(message, level = 'info') {
    // Shows a short notification and records it in the activity log
    logLine(level, message);
    const toast = document.createElement('div');
    toast.className = `toast ${level}`;
    toast.textContent = message;
    document.getElementById('toast-container').appendChild(toast);
    setTimeout(() => toast.remove(), 5000);
}

const POPOVER_MAX = 8;

function captureGridHtml(card, max = Infinity) {
    const shown = card.captures.slice(0, max);
    const more = card.captures.length - shown.length;
    return shown.map(capture => `
        <figure>
            <img src="${escapeHtml(capture.url)}" alt="${escapeHtml(card.name)}" loading="lazy">
            <figcaption>${escapeHtml(capture.captured_at)}</figcaption>
        </figure>`).join('')
        + (more > 0 ? `<div class="capture-more">+${more} more<br><span>click to see all</span></div>` : '');
}

function showCapturePopover(card, row) {
    const popover = document.getElementById('capture-popover');
    popover.className = 'capture-popover capture-grid' + (card.captures.length === 1 ? ' single' : '');
    popover.innerHTML = captureGridHtml(card, POPOVER_MAX);
    popover.hidden = false;
    // Beside the row's thumbnail; above the row when there is no room below
    const rect = row.getBoundingClientRect();
    const box = popover.getBoundingClientRect();
    const left = Math.min(rect.left + 70, window.innerWidth - box.width - 16);
    const below = rect.bottom + 6;
    popover.style.left = `${Math.max(16, left)}px`;
    popover.style.top = `${below + box.height < window.innerHeight - 8 ? below : Math.max(8, rect.top - box.height - 6)}px`;
}

function hideCapturePopover() {
    document.getElementById('capture-popover').hidden = true;
}

function openCaptures(card) {
    hideCapturePopover();
    const count = card.captures.length;
    document.getElementById('capture-title').textContent =
        `${card.name} - ${count} capture${count > 1 ? 's' : ''} of ${card.quantity} ${card.quantity > 1 ? 'copies' : 'copy'}`;
    document.getElementById('capture-grid').innerHTML = captureGridHtml(card);
    document.getElementById('capture-modal').classList.add('show');
}

function closeCaptures() {
    document.getElementById('capture-modal').classList.remove('show');
}
