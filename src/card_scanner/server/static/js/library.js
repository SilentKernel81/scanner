// What the collection page (collection.js) and the deck pages (decks.js) both use: small
// helpers for requests, text and fields, the bars of a chart, the dialog that asks for one
// value, and the card picture beside the row under the pointer.
//
// Loaded after common.js (escapeHtml, the dialogs, notify, byText are from there) and before
// the page's own script. Not for the scanner page: scanner.js has its own $ and its own
// connection, and two files declaring the same name stop a page from loading at all.

const $ = id => document.getElementById(id);
const money = value => formatPrice(value);   // common.js: in the chosen price source's currency
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
let valueSuggestions = [];   // the suggestions of the open dialog, for the list under its input

function showValueSuggestions(all = false) {
    // Our own list instead of the browser's <datalist>, whose popup was narrower than the
    // input and cannot be styled. Narrowed by what is typed (all: every suggestion). A suggestion
    // is a text, or [text, group heading]: a group gets its heading above its first suggestion
    const list = $('value-suggest'), text = $('value-input').value.trim().toLowerCase();
    const found = valueSuggestions.filter(([option]) => all || !text || option.toLowerCase().includes(text))
        .filter(([option]) => option.toLowerCase() !== text || all);
    let group = '';
    list.innerHTML = found.map(([option, heading]) => {
        const title = heading && heading !== group ? `<div class="value-suggest-heading">${escapeHtml(heading)}</div>` : '';
        group = heading || group;
        return `${title}<button type="button" class="value-suggest-item">${escapeHtml(option)}</button>`;
    }).join('');
    list.hidden = found.length === 0;
}

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
            valueSuggestions = options.map(option => Array.isArray(option) ? option : [option, '']);
        }
        $('value-suggest').hidden = true;
        $('value-toggle').hidden = !!choices || !options.length;   // the chevron: there is a list to open
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
    // A line that spans most of the window (a card in the list of a preconstructed deck) has
    // no room on either side: the picture goes over the line's empty middle, clear of the
    // name at its left end and the note at its right
    const wide = rect.width > window.innerWidth / 2;
    const left = wide ? rect.right - 250 - 150
        : rect.right + 250 < window.innerWidth ? rect.right + 8 : rect.left - 248;
    preview.style.left = `${Math.max(8, left)}px`;
    preview.style.top = `${Math.max(8, Math.min(rect.top - 40, window.innerHeight - height - 8))}px`;
}

function bindValueDialog() {
    // OK and Enter answer the dialog that asks for one value (valueDialog above)
    $('value-ok').addEventListener('click', () =>
        closeValueDialog($('value-select').hidden ? $('value-input').value.trim() : $('value-select').value));
    $('value-input').addEventListener('keydown', event => {
        const list = $('value-suggest'), items = [...list.querySelectorAll('.value-suggest-item')];
        const current = items.findIndex(item => item.classList.contains('active'));
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            // Up and down walk the list (down opens it); the walked-to one goes into the input
            event.preventDefault();
            if (list.hidden) showValueSuggestions(true);
            const all = [...list.querySelectorAll('.value-suggest-item')];
            if (!all.length) return;
            const next = event.key === 'ArrowDown' ? Math.min(current + 1, all.length - 1) : Math.max(current - 1, 0);
            all.forEach((item, index) => item.classList.toggle('active', index === next));
            all[next].scrollIntoView({block: 'nearest'});
            $('value-input').value = all[next].textContent;
        } else if (event.key === 'Enter') {
            // With the list open, Enter takes the highlighted one first; a second Enter confirms
            if (!list.hidden && current >= 0) { list.hidden = true; return; }
            $('value-ok').click();
        } else if (event.key === 'Escape' && !list.hidden) {
            list.hidden = true;
            event.stopPropagation();
        }
    });
    $('value-input').addEventListener('input', () => showValueSuggestions());
    // The list opens on the chevron (or a click in the box, or down); it is closed when the dialog opens
    $('value-input').addEventListener('click', () => { if ($('value-suggest').hidden) showValueSuggestions(true); });
    $('value-toggle').addEventListener('mousedown', event => {
        event.preventDefault();   // the input keeps focus
        if ($('value-suggest').hidden) showValueSuggestions(true); else $('value-suggest').hidden = true;
        $('value-input').focus();
    });
    // mousedown, not click: the input must not lose focus (and the list vanish) before the pick
    $('value-suggest').addEventListener('mousedown', event => {
        const item = event.target.closest('.value-suggest-item');
        if (!item) return;
        event.preventDefault();
        $('value-input').value = item.textContent;
        $('value-suggest').hidden = true;
    });
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
