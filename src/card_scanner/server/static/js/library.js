// What the collection page (collection.js) and the deck pages (decks.js) both use: small
// helpers for requests, text and fields, the bars of a chart, the dialog that asks for one
// value, and the card picture beside the row under the pointer.
//
// Loaded after common.js (escapeHtml, the dialogs, notify, byText are from there) and before
// the page's own script. Not for the scanner page: scanner.js has its own $ and its own
// connection, and two files declaring the same name stop a page from loading at all.

const $ = id => document.getElementById(id);
const money = value => '$' + (value || 0).toFixed(2);
const plural = (count, word, many = word + 's') => `${count} ${count === 1 ? word : many}`;
const entriesText = count => plural(count, 'entry', 'entries');
const totalQuantity = rows => rows.reduce((sum, row) => sum + row.quantity, 0);

async function api(path, options = {}) {
    // JSON request; shows the server's error and returns null when it fails
    if (options.body && typeof options.body !== 'string' && !(options.body instanceof FormData)) {
        options = {...options, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(options.body)};
    }
    try {
        const response = await fetch(path, options);
        const data = await response.json();
        if (data.success === false) {
            notify(data.error || 'Request failed', 'error');
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

function manaHtml(cost) {
    // "{2}{W}{U/B}" -> colored pips
    return (cost || '').replace(/\{([^}]+)\}/g, (_, symbol) => {
        // Hybrid symbols ("W/U") take the first color; numbers and X are neutral
        const color = [...'WUBRGC'].find(letter => symbol.includes(letter)) || 'N';
        const text = symbol.replace('/', '');
        return `<span class="mana mana-${color}${text.length > 1 ? ' is-long' : ''}">${escapeHtml(text)}</span>`;
    });
}

function closeModal(id) {
    $(id).classList.remove('show');
}

function optionsHtml(pairs) {
    // [[value, label]]
    return pairs.map(([value, label]) => `<option value="${escapeHtml(String(value))}">${escapeHtml(label)}</option>`).join('');
}

function searchWords(id) {
    // What is typed in a filter field: every word must be found
    return $(id).value.trim().toLowerCase().split(/\s+/).filter(Boolean);
}

function markSwitch(id, key, value) {
    // A row of buttons of which one is active (data-<key>)
    document.querySelectorAll(`#${id} button`).forEach(button => button.classList.toggle('is-active', button.dataset[key] === value));
}

const COLORS = [['W', 'White'], ['U', 'Blue'], ['B', 'Black'], ['R', 'Red'], ['G', 'Green'], ['C', 'Colorless']];

function distinct(values) {
    return [...new Set(values.filter(Boolean))].sort(byText);
}

function colorChipsHtml(active) {
    return COLORS.map(([key, label]) =>
        `<button class="color-chip mana-${key} ${active.has(key) ? 'is-active' : ''}" data-color="${key}" title="${label}">${key}</button>`).join('');
}

function barsHtml(rows, format, limit = 12) {
    // rows: [[label, number]] - largest first; the rest folds into "Other"
    rows = rows.filter(([, number]) => number > 0).sort((a, b) => b[1] - a[1]);
    if (rows.length > limit) {
        const rest = rows.slice(limit - 1);
        rows = [...rows.slice(0, limit - 1), [`Other (${rest.length})`, rest.reduce((sum, [, number]) => sum + number, 0)]];
    }
    const top = Math.max(...rows.map(([, number]) => number), 0);
    if (!rows.length) return '<div class="hint">Nothing to show</div>';
    return rows.map(([label, number]) => `
        <div class="bar-row" title="${escapeHtml(label)}: ${format(number)}">
            <span class="bar-label">${escapeHtml(label)}</span>
            <span class="bar-track"><div class="bar-fill" style="width: ${top ? (100 * number / top).toFixed(1) : 0}%"></div></span>
            <span class="bar-value">${format(number)}</span>
        </div>`).join('');
}

function debounce(callback, delay = 250) {
    let timer = null;
    return () => {
        clearTimeout(timer);
        timer = setTimeout(callback, delay);
    };
}

function bindSwitch(id, chosen) {
    // A row of buttons of which one is active (markSwitch shows which)
    $(id).addEventListener('click', event => {
        const button = event.target.closest('button');
        if (button) chosen(button);
    });
}

function bindColorChips(id, colors, changed) {
    $(id).addEventListener('click', event => {
        const chip = event.target.closest('.color-chip');
        if (!chip) return;
        if (colors.has(chip.dataset.color)) colors.delete(chip.dataset.color); else colors.add(chip.dataset.color);
        chip.classList.toggle('is-active');
        changed();
    });
}

// -- One value for a bulk action ----------------------------------------------

let valueResolve = null;

function valueDialog({title, label, options = [], choices = null, value = ''}) {
    // Free text with suggestions (options), or one of choices ([[value, label]]); resolves
    // with the value, or null when cancelled
    return new Promise(resolve => {
        valueResolve = resolve;
        $('value-title').textContent = title;
        $('value-label').textContent = label;
        const input = $('value-input'), select = $('value-select');
        input.hidden = !!choices;
        select.hidden = !choices;
        if (choices) {
            select.innerHTML = optionsHtml(choices);
        } else {
            input.value = value;
            $('value-options').innerHTML = options.map(option => `<option value="${escapeHtml(option)}"></option>`).join('');
        }
        $('value-modal').classList.add('show');
        (choices ? select : input).focus();
    });
}

function closeValueDialog(value) {
    closeModal('value-modal');
    const resolve = valueResolve;
    valueResolve = null;
    if (resolve) resolve(value);
}

// -- Card image beside the hovered row -----------------------------------------

let previewRow = null;   // the row whose card is shown large

function showPreview(row) {
    const preview = $('card-preview');
    const image = row && row.dataset.image;
    if (!image) {
        preview.hidden = true;
        previewRow = null;
        return;
    }
    if (preview.dataset.image !== image) {
        // Without its old picture: a browser keeps showing that until the new one has loaded
        // (the box itself is there at once, card-shaped)
        preview.removeAttribute('src');
        preview.src = image;
        preview.dataset.image = image;
    }
    preview.hidden = false;
    previewRow = row;
    placePreview();
}

function placePreview() {
    // Beside what the pointer is on (a deck row, the small picture of an inventory row), on
    // the side that has room
    if (!previewRow) return;
    const preview = $('card-preview');
    const height = 240 * 88 / 63;
    const rect = previewRow.getBoundingClientRect();
    const left = rect.right + 250 < window.innerWidth ? rect.right + 8 : rect.left - 248;
    preview.style.left = `${Math.max(8, left)}px`;
    preview.style.top = `${Math.max(8, Math.min(rect.top - 40, window.innerHeight - height - 8))}px`;
}

function bindValueDialog() {
    // OK and Enter answer the dialog that asks for one value (valueDialog above)
    $('value-ok').addEventListener('click', () =>
        closeValueDialog($('value-select').hidden ? $('value-input').value.trim() : $('value-select').value));
    $('value-input').addEventListener('keydown', event => { if (event.key === 'Enter') $('value-ok').click(); });
}

function bindCardPreview() {
    // The card's picture beside whatever carries data-image, on devices with a pointer
    if (window.matchMedia('(hover: hover)').matches) {
        document.addEventListener('mouseover', event => showPreview(event.target.closest('[data-image]')));
    }
}

function bindOverlays(overlays) {
    // overlays: [[id, close]] topmost first. Escape closes the topmost one that is open, a
    // click on the dimmed part around a dialog closes that dialog
    document.addEventListener('keydown', event => {
        if (event.key !== 'Escape') return;
        const overlay = overlays.find(([id]) => $(id).classList.contains('show'));
        if (overlay) overlay[1]();
    });
    window.addEventListener('click', event => {
        const overlay = overlays.find(([id]) => event.target === $(id));
        if (overlay) overlay[1]();
    });
}
