// Collection page: inventory management, trades, statistics, and the settings drawer (backups).
// Shared helpers come from common.js (escapeHtml, dialogs, notify, the edit dialog, sorts) and
// library.js ($, api, money, optionsHtml, valueDialog, the card preview ...).
// The decks were part of this page until 2026-10-13: they are pages of their own now
// (/decks, decks.js). What is left of them here: the inventory's location filter lists the
// decks' names apart (loadDeckList), and a selection of cards can be added to a deck.

const socket = io();

// ============================================================================
// Tabs
// ============================================================================

let currentTab = 'inventory';

// -- Which collection: the cards on the shelf, or what the user has in MTG Arena ---------
// The page was written for the first. In Arena view it shows the digital collection - the
// same list, filters and statistics, read from another file on the server (?area=arena) -
// and nothing that changes it: that collection is a copy of what Arena has, replaced as a
// whole by each import. inventoryArea is what common.js's areaQuery() adds to a request.

let inventoryArea;                 // undefined: the collection; 'arena': the digital one
let arenaInfo = null;              // {imported, exported, cards, entries, wildcards, ...} (GET /api/arena)

function inArena() {
    return inventoryArea === 'arena';
}

function priceText(amount) {
    // A card in Arena has no price: what the paper printing costs says nothing about it
    return inArena() ? '–' : money(amount);
}

async function showCollection(kind) {
    // A delete that still waits for its undo time was made in the collection shown until
    // now: it is sent before the area changes, or its ids would go to the other collection
    await sendPending();
    inventoryArea = kind === 'arena' ? 'arena' : undefined;
    remember('collectionKind', inArena() ? 'arena' : 'paper');
    markSwitch('collection-kind', 'collection', inArena() ? 'arena' : 'paper');
    // The class switches off, in one place, everything that would change the collection
    // (collection.css: body.arena-view): ticking, editing, deleting, trades, the paper import
    document.body.classList.toggle('arena-view', inArena());
    $('arena-bar').hidden = !inArena();
    selected.clear();
    suggested = null;   // the spare cards were worked out for the other view's cards
    if (inArena() && currentTab === 'trades') showTab('inventory');   // trades are of paper cards
    await loadArenaInfo();
    await loadInventory();
    if (currentTab === 'stats') renderStats();
}

async function loadArenaInfo() {
    if (!inArena()) return;
    const data = await api('/api/arena');
    arenaInfo = data ? data.arena : null;
    const info = arenaInfo;
    const wild = info && info.wildcards ? ['common', 'uncommon', 'rare', 'mythic'].map(rarity => `${info.wildcards[rarity] || 0} ${rarity}`).join(', ') : '';
    $('arena-text').innerHTML = !info || !info.imported
        ? 'Nothing imported yet. Run the Arena exporter, then choose its <strong>mtga_collection.json</strong> and <strong>mtga_decks.json</strong> here - both at once, or one at a time.'
        : `<strong>Your Arena collection</strong> as exported ${escapeHtml((info.exported || '').slice(0, 16).replace('T', ' '))}: `
          + `${plural(info.cards, 'card')} · wildcards: ${escapeHtml(wild)}`
          + (info.summary && info.summary.unknown && info.summary.unknown.length
              ? ` · <span title="${escapeHtml(info.summary.unknown.join(', '))}">${plural(info.summary.unknown.length, 'card')} not in the card data</span>` : '');
}

async function importArena(files) {
    // The exporter's files, as they are: the server tells the collection from the decks
    const form = new FormData();
    [...files].forEach(file => form.append('files', file));
    $('arena-import').disabled = true;
    $('arena-text').textContent = 'Importing...';
    const data = await api('/api/arena/import', {method: 'POST', body: form});
    $('arena-import').disabled = false;
    if (data) {
        // Either file alone is an import: the message says which part was replaced
        const unknown = data.collection ? ((data.arena.summary || {}).unknown || []).length : 0;
        const missing = (data.deck_unknown || []).length;
        notify(!data.collection
               ? `Arena decks imported: ${plural(data.decks, 'deck')} (no collection file: the cards were left as they are)`
               : `Arena collection imported: ${plural(data.arena.cards, 'card')}`
                 + (data.decks !== null ? `, ${plural(data.decks, 'deck')}` : ' (no deck file: the Arena decks were left as they are)')
                 + (unknown ? ` - ${plural(unknown, 'card')} not in the card data` : ''), 'success');
        if (missing) notify(`Not in the card data, and left out of the decks: ${data.deck_unknown.slice(0, 12).join(', ')}`, 'warning');
        if (data.warning) notify(data.warning, 'warning');
    }
    await loadArenaInfo();
    await loadInventory();
    loadDeckList();
}

function showTab(tab) {
    currentTab = tab;
    document.querySelectorAll('.tab').forEach(button => button.classList.toggle('is-active', button.dataset.tab === tab));
    document.querySelectorAll('.tab-panel').forEach(panel => { panel.hidden = panel.id !== `tab-${tab}`; });
    if (tab === 'stats') renderStats();
    if (tab === 'trades') loadTrades();
}

// ============================================================================
// Inventory
// ============================================================================

const MTG_TYPES = ['Creature', 'Planeswalker', 'Instant', 'Sorcery', 'Artifact', 'Enchantment', 'Battle', 'Land'];
const CONDITIONS = ['Mint', 'Near Mint', 'Excellent', 'Good', 'Played', 'Poor'];
const PAGE_SIZES = [25, 50, 100, 200];
// The filters kept for the next visit. Not the "Added" batch (it comes with "Remove this
// batch")
const FILTER_FIELDS = ['filter-text', 'filter-type', 'filter-rarity', 'filter-set', 'filter-finish', 'filter-location',
                       'filter-tag', 'filter-price-min', 'filter-price-max'];
const FILTER_TICKS = ['filter-text-not', 'filter-free'];
// ... and their names in the page's address (?set=...&rarity=rare), which can be kept as a bookmark
const FILTER_PARAMS = {'filter-text': 'q', 'filter-type': 'type', 'filter-rarity': 'rarity', 'filter-set': 'set',
                       'filter-finish': 'finish', 'filter-location': 'loc', 'filter-tag': 'tag', 'filter-price-min': 'min',
                       'filter-price-max': 'max', 'filter-text-not': 'not', 'filter-free': 'free'};
const UNDO_SECONDS = 6;

let inventory = [];
let shown = [];             // after filters and sort
let pageSize = PAGE_SIZES.includes(parseInt(recall('collectionPageSize', ''))) ? parseInt(recall('collectionPageSize', '')) : 100;
let page = 0;               // of shown, pageSize entries each
let selected = new Set();   // entry ids
let lastPicked = null;      // the entry ticked or unticked last: a shift-click reaches from it to the clicked one
let savedFilters = recallFilters();   // until the first load has filled the lists to choose from
let filterColors = new Set(savedFilters.colors);
let inventoryView = recall('collectionView', 'list');

function recallFilters() {
    // The filters of the address when it names any (a bookmark), else the ones of the last visit
    let saved = {};
    const params = new URLSearchParams(window.location.search);
    if ([...Object.values(FILTER_PARAMS), 'colors'].some(name => params.has(name))) {
        [...FILTER_FIELDS, ...FILTER_TICKS].forEach(id => {
            const value = params.get(FILTER_PARAMS[id]);
            if (value) saved[id] = FILTER_TICKS.includes(id) ? true : value;
        });
        saved.colors = [...(params.get('colors') || '').toUpperCase()];
    } else {
        try { saved = JSON.parse(recall('collectionFilters', '{}')) || {}; } catch (e) { /* not ours */ }
    }
    return {...saved, colors: Array.isArray(saved.colors) ? saved.colors.filter(color => COLORS.some(([key]) => key === color)) : []};
}

function saveFilters() {
    const state = {colors: [...filterColors]};
    FILTER_FIELDS.forEach(id => { if ($(id).value) state[id] = $(id).value; });
    FILTER_TICKS.forEach(id => { if ($(id).checked) state[id] = true; });
    remember('collectionFilters', JSON.stringify(state));
    // The address says the same (the #settings part stays)
    const params = new URLSearchParams();
    [...FILTER_FIELDS, ...FILTER_TICKS].forEach(id => { if (state[id]) params.set(FILTER_PARAMS[id], state[id] === true ? '1' : state[id]); });
    if (state.colors.length) params.set('colors', state.colors.join(''));
    const query = params.toString();
    try {
        history.replaceState(null, '', window.location.pathname + (query ? '?' + query : '') + window.location.hash);
    } catch (e) { /* the list works without it */ }
}

function restoreFilters() {
    // Once, after the first load: a list takes only a value it offers, so a set or tag that
    // is gone from the collection is not filtered by
    FILTER_FIELDS.forEach(id => {
        $(id).value = typeof savedFilters[id] === 'string' ? savedFilters[id] : '';
        if ($(id).selectedIndex === -1) $(id).value = '';
    });
    FILTER_TICKS.forEach(id => { $(id).checked = savedFilters[id] === true; });
    savedFilters = null;
    saveFilters();  // what was taken, in the address too
}

function filtersChanged() {
    page = 0;
    saveFilters();
    applyFilters();
}

function inventoryChanged() {
    // After an edit or delete (common.js), a bulk action or a change made while scanning;
    // the promise resolves when the list is loaded again
    return loadInventory();
}

function identityOf(card) {
    return (card.details && card.details.identity) || null;
}

function mainType(card) {
    // Magic: the card type; other games: the first part of the type line
    const front = (card.type_line || '').split(' // ')[0];
    if (gameInfo.id === 'mtg') return MTG_TYPES.find(type => front.includes(type)) || 'Other';
    return front.split(' · ')[0] || 'Other';
}

function colorGroup(card) {
    const identity = identityOf(card);
    if (!identity) return card.color_identity || 'Unknown';
    if (!identity.length) return 'Colorless';
    return identity.length > 1 ? 'Multicolor' : COLORS.find(([key]) => key === identity[0])[1];
}

async function loadScanned() {
    // Cards scanned on the scanner page wait there until they are added to the collection
    const data = await api('/api/stats');
    const waiting = data && data.inventory ? data.inventory.total_cards : 0;
    $('scanned-notice').hidden = !waiting;
    $('scanned-text').textContent = `${plural(waiting, 'scanned card')} ${waiting === 1 ? 'is' : 'are'} waiting on the scanner page.`;
}

async function addScanned() {
    if (await addScannedToCollection()) loadInventory();
}

async function loadInventory() {
    // Cards waiting on the scanner page are for the paper collection; Arena has no scanner
    if (inArena()) $('scanned-notice').hidden = true; else loadScanned();
    const data = await api('/api/inventory' + areaQuery());
    if (!data) {
        $('inventory-list').innerHTML = '<div class="empty-state is-error">Failed to load the inventory</div>';
        return;
    }
    inventory = data.cards;
    selected = new Set([...selected].filter(id => inventory.some(card => card.id === id)));
    fillFilterOptions();
    if (savedFilters) restoreFilters();
    applyFilters();
    renderSpare();
    if (currentTab === 'stats') renderStats();
}

function fillSelect(id, label, values, labels = {}) {
    const select = $(id);
    const current = select.value;
    select.innerHTML = optionsHtml([['', label], ...values.map(value => [value, labels[value] || value])]);
    select.value = values.includes(current) ? current : '';
    select.hidden = values.length === 0;
}


function fillFilterOptions() {
    fillSelect('filter-type', 'Any type', distinct(inventory.map(mainType)));
    fillSelect('filter-rarity', 'Any rarity', distinct(inventory.map(card => card.rarity))
        .sort((a, b) => (RARITY_ORDER[a] ?? 9) - (RARITY_ORDER[b] ?? 9)));
    fillSelect('filter-set', 'Any set', distinct(inventory.map(card => card.set_name)));
    fillSelect('filter-finish', 'Any finish', gameInfo.finishes.map(([key]) => key),
               Object.fromEntries(gameInfo.finishes));
    fillLocationFilter();
    fillSelect('filter-tag', 'Any tag', distinct(inventory.flatMap(card => card.tags)));
    // The last times cards came into the collection (one batch per "Add to collection", import
    // or scanned card), newest first (card.batches: every batch an entry has copies of, not
    // only its latest)
    const batches = new Map();
    inventory.forEach(card => card.batches.forEach(part => {
        const batch = batches.get(part.batch) || {added_at: part.added_at, cards: 0};
        batch.cards += part.quantity;
        batches.set(part.batch, batch);
    }));
    const ids = [...batches.keys()].sort((a, b) => byText(batches.get(b).added_at, batches.get(a).added_at)).slice(0, 20);
    fillSelect('filter-added', 'Added any time', ids,
               Object.fromEntries(ids.map(id => [id, `Added ${batches.get(id).added_at.slice(0, 16)} (${plural(batches.get(id).cards, 'card')})`])));
    // Color chips only where the card data has color identities (Magic)
    $('filter-free-label').hidden = !gameInfo.deck_formats.length;
    const hasColors = inventory.some(card => identityOf(card));
    $('filter-colors').innerHTML = hasColors ? colorChipsHtml(filterColors) : '';
}


let deckList = [];   // the decks, for the location filter below (their names) and "Add to a deck"

async function loadDeckList() {
    // Only what this page needs of the decks; the decks themselves are at /decks
    if (!gameInfo.deck_formats.length) return;
    const data = await api('/api/decks');
    if (!data) return;
    deckList = data.decks;
    fillLocationFilter();
    renderSpareLocations();   // the trade tab's location list is grouped by the decks too
}

function fillLocationFilter() {
    // Boxes and binders first; the locations named after one of the decks under their own
    // heading at the bottom (a deck's cards are usually kept under the deck's name)
    const decks = new Set(deckList.map(deck => deck.name.toLowerCase()));
    const places = knownLocations();
    const inDecks = places.filter(place => decks.has(place.toLowerCase()));
    const select = $('filter-location');
    const current = select.value;
    select.innerHTML = optionsHtml([['', 'Any location'], ['(none)', '(none)'],
                                    ...places.filter(place => !inDecks.includes(place)).map(place => [place, place])])
        + (inDecks.length ? decksHeadingHtml(select, ['Any location', ...places]) + optionsHtml(inDecks.map(place => [place, place])) : '');
    select.value = current === '(none)' || places.includes(current) ? current : '';
    select.hidden = false;
}

function knownLocations() {
    return distinct(inventory.map(card => card.location));
}

// Search terms that say where to look: set:HOB, rarity:rare, loc:"Binder 2", -tag:trade.
// A set is its code, whole (HOB does not find the cards with "hob" in their name); typed
// without a known code, part of the set's name. Tags, locations, finishes and numbers are whole
const SEARCH_FIELDS = {
    set: (card, value, setCodes) => setCodes.has(value) ? (card.set_code || '').toLowerCase() === value
                                                        : (card.set_name || '').toLowerCase().includes(value),
    name: (card, value) => (card.name || '').toLowerCase().includes(value),
    type: (card, value) => (card.type_line || '').toLowerCase().includes(value),
    rarity: (card, value) => (card.rarity || '').toLowerCase().startsWith(value),
    tag: (card, value) => card.tags.some(tag => tag.toLowerCase() === value),
    loc: (card, value) => (card.location || '').toLowerCase() === value,
    finish: (card, value) => [card.finish, finishLabel(card.finish)].some(finish => (finish || '').toLowerCase() === value),
    number: (card, value) => String(card.number || '').toLowerCase() === value,
    // qty:>4, qty:<=2, qty:3 - how many copies the entry has
    qty: (card, value) => {
        const [, sign, number] = /^(>=|<=|>|<|=)?(\d+)$/.exec(value) || [];
        if (number === undefined) return false;
        const copies = card.quantity, wanted = parseInt(number);
        return sign === '>' ? copies > wanted : sign === '<' ? copies < wanted : sign === '>=' ? copies >= wanted
            : sign === '<=' ? copies <= wanted : copies === wanted;
    },
    // Set aside for a trade with this in its name; trade:"" = for any trade
    trade: (card, value) => (card.trades || []).some(trade => trade.name.toLowerCase().includes(value)),
};
SEARCH_FIELDS.location = SEARCH_FIELDS.loc;

function parseSearch(text) {
    // {terms: [{field, value, not}], phrase: what is left, looked for everywhere}. A word with
    // a colon that names no field stays text ("Circle of Protection: Red", "foo:bar")
    const terms = [];
    const phrase = text.toLowerCase().replace(/(^|\s)(-?)([a-z]+):(?:"([^"]*)"|(\S+))/g, (all, space, not, field, quoted, word) => {
        if (!SEARCH_FIELDS[field]) return all;
        terms.push({field, value: quoted !== undefined ? quoted.trim() : word, not: !!not});
        return ' ';
    });
    return {terms, phrase: phrase.replace(/\s+/g, ' ').trim()};
}

function activeFilters() {
    // [{text, clear}] of what narrows the list now
    const chips = [];
    const chosen = (id, label) => {
        if ($(id).value) chips.push({text: `${label}: ${$(id).selectedOptions[0].textContent}`, clear: () => { $(id).value = ''; }});
    };
    const text = $('filter-text').value.trim();
    if (text) {
        chips.push({text: `${$('filter-text-not').checked ? 'Not' : 'Search'}: ${text}`,
                    clear: () => { $('filter-text').value = ''; $('filter-text-not').checked = false; }});
    }
    if (filterColors.size) {
        chips.push({text: `Colors: ${[...filterColors].join(' ')}`, clear: () => { filterColors.clear(); fillFilterOptions(); }});
    }
    chosen('filter-type', 'Type');
    chosen('filter-rarity', 'Rarity');
    chosen('filter-set', 'Set');
    chosen('filter-finish', 'Finish');
    // (not in Arena view, where these three are neither shown nor applied: applyFilters)
    const digital = inArena();
    if (!digital) chosen('filter-location', 'Location');
    if (!digital) chosen('filter-tag', 'Tag');
    if ($('filter-added').value) chips.push({text: $('filter-added').selectedOptions[0].textContent, clear: () => { $('filter-added').value = ''; }});
    if ($('filter-free').checked) chips.push({text: 'Not in a deck', clear: () => { $('filter-free').checked = false; }});
    const min = $('filter-price-min').value, max = $('filter-price-max').value;
    if ((min || max) && !digital) {
        chips.push({text: 'Price: ' + (min && max ? `$${min} – $${max}` : min ? `from $${min}` : `up to $${max}`),
                    clear: () => { $('filter-price-min').value = ''; $('filter-price-max').value = ''; }});
    }
    return chips;
}

function filterByBadge(filter, value) {
    // A click on a row's badge shows only the cards with it; a second click takes that off again
    if (filter === 'trade') {
        const term = `trade:"${value.toLowerCase()}"`, field = $('filter-text');
        field.value = field.value.toLowerCase().includes(term)
            ? field.value.replace(new RegExp(term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i'), '').replace(/\s+/g, ' ').trim()
            : `${field.value.trim()} ${term}`.trim();
    } else {
        const select = $(filter);
        select.value = select.value === value ? '' : value;
        if (select.selectedIndex === -1) select.value = '';
    }
    filtersChanged();
    window.scrollTo(0, 0);
}

function applyFilters() {
    const {terms, phrase} = parseSearch($('filter-text').value);
    const setCodes = new Set(inventory.map(card => (card.set_code || '').toLowerCase()).filter(Boolean));
    const textMatches = card =>
        (!phrase || [card.name, card.set_name, card.set_code, card.type_line, card.rarity, card.location, ...card.tags]
            .some(value => (value || '').toLowerCase().includes(phrase)))
        && terms.every(term => SEARCH_FIELDS[term.field](card, term.value, setCodes) !== term.not);
    const textNot = $('filter-text-not').checked;
    const type = $('filter-type').value, rarity = $('filter-rarity').value, set = $('filter-set').value;
    // Arena view hides the filters by location, tag and price (its cards have none of the
    // three: collection.css, body.arena-view). A filter that cannot be seen must not filter:
    // a minimum price set for the paper cards would otherwise hide every Arena card, whose
    // price is 0. The values stay in their fields, for the Physical view.
    const digital = inArena();
    const finish = $('filter-finish').value;
    const location = digital ? '' : $('filter-location').value, tag = digital ? '' : $('filter-tag').value;
    const added = $('filter-added').value;
    const free = $('filter-free').checked;
    const min = digital ? NaN : parseFloat($('filter-price-min').value), max = digital ? NaN : parseFloat($('filter-price-max').value);
    const rows = inventory.filter(card => {
        // "Not" turns the text search around: the cards that don't have the text anywhere
        if ((phrase || terms.length) && textMatches(card) === textNot) return false;
        if (type && mainType(card) !== type) return false;
        if (rarity && card.rarity !== rarity) return false;
        if (set && card.set_name !== set) return false;
        if (finish && card.finish !== finish) return false;
        if (location && card.location !== (location === '(none)' ? '' : location)) return false;
        if (tag && !card.tags.includes(tag)) return false;
        if (added && !batchPart(card, added)) return false;
        if (free && card.decks.length) return false;
        if (!isNaN(min) && card.price < min) return false;
        if (!isNaN(max) && card.price > max) return false;
        if (filterColors.size) {
            // Every chosen color must be in the card's identity; C = colorless
            const identity = identityOf(card) || [];
            if (filterColors.has('C') ? identity.length > 0 : ![...filterColors].every(color => identity.includes(color))) return false;
        }
        return true;
    });
    $('batch-remove').hidden = !added;
    // Filters come back on the next visit: show what narrows the list, each to be taken off
    const chips = activeFilters();
    $('filter-chips').innerHTML = chips.map((chip, index) =>
        `<button class="filter-chip" data-chip="${index}" title="Take this filter off">${escapeHtml(chip.text)} <span>×</span></button>`).join('');
    $('filter-chips').hidden = !chips.length;
    $('filter-clear').disabled = !chips.length;
    const sort = recall('collectionSort', 'added');
    $('inventory-sort').value = sort in INVENTORY_SORTS ? sort : 'added';
    shown = INVENTORY_SORTS[sort] ? [...rows].sort(INVENTORY_SORTS[sort]) : rows;
    // Only cards the filters show can be selected: a bulk action must not reach cards out of sight
    const visible = new Set(rows.map(card => card.id));
    selected = new Set([...selected].filter(id => visible.has(id)));
    renderInventory();
}

function badgesHtml(card) {
    const rarity = (card.rarity || '').toLowerCase();
    const only = (filter, value) => `data-filter="${filter}" data-value="${escapeHtml(value)}"`;  // filterByBadge
    return `
        ${rarity ? `<span class="inventory-badge ${escapeHtml(rarity)}" ${only('filter-rarity', card.rarity)} title="Rarity - click to show only these">${escapeHtml(rarity.toUpperCase())}</span>` : ''}
        ${card.finish !== defaultFinish() ? `<span class="inventory-badge ${escapeHtml(card.finish)}" ${only('filter-finish', card.finish)} title="Finish - click to show only these">${escapeHtml(finishLabel(card.finish).toUpperCase())}</span>` : ''}
        ${card.location ? `<span class="inventory-badge location" ${only('filter-location', card.location)} title="Location - click to show only these">${escapeHtml(card.location)}</span>` : ''}
        ${card.tags.map(tag => `<span class="inventory-badge tag" ${only('filter-tag', tag)} title="Tag - click to show only these">${escapeHtml(tag)}</span>`).join('')}
        ${(card.trades || []).map(trade => `<span class="inventory-badge trade" ${only('trade', trade.name)} title="Set aside for the trade &quot;${escapeHtml(trade.name)}&quot; (Trades tab) - click to show only these">Trade: ${
            escapeHtml(trade.name)}${trade.quantity < card.quantity ? ` ${trade.quantity}×` : ''}</span>`).join('')}
        ${card.decks.length ? `<span class="inventory-badge deck" title="Used in: ${escapeHtml(card.decks.join(', '))}">${
            escapeHtml(card.decks.length === 1 ? card.decks[0] : plural(card.decks.length, 'deck'))}</span>` : ''}`;
}

function waiting(card) {
    // Part of a change that is not sent yet (changeLater)
    return !!pendingChange && pendingChange.ids.has(card.id);
}

function gridCardHtml(card) {
    const image = (card.details && card.details.image_uri) || (card.captures[0] && card.captures[0].url);
    return `
        <div class="grid-card ${selected.has(card.id) ? 'is-selected' : ''} ${waiting(card) ? 'is-pending' : ''}" data-id="${card.id}">
            ${image ? `<img src="${escapeHtml(image)}" alt="${escapeHtml(card.name)}" loading="lazy">`
                    : `<div class="no-image">${escapeHtml(card.name)}</div>`}
            <input type="checkbox" class="row-check" ${selected.has(card.id) ? 'checked' : ''} aria-label="Select">
            ${card.quantity > 1 ? `<span class="grid-qty">${card.quantity}×</span>` : ''}
            <div class="grid-name" title="${escapeHtml(card.name)}">${escapeHtml(card.name)}</div>
            <div class="grid-meta"><span>${escapeHtml((card.set_code || card.set_name).toUpperCase())} ${escapeHtml(card.number)}</span><span>${priceText(card.price)}</span></div>
            <div class="inventory-card-meta">${badgesHtml(card)}</div>
        </div>`;
}

function listRowHtml(card) {
    // On the left what was captured (a click opens the captures); by the buttons the card's
    // downloaded picture, small - the pointer on it shows it large (showPreview)
    const picture = (card.details && card.details.image_uri) || '';
    const image = (card.captures[0] && card.captures[0].url) || picture;
    // Arena view: nothing was scanned, so the picture on the left is the card's own. It is
    // the one to point at for the large picture (data-image), and the small second copy by
    // the buttons - there to compare a capture with the card - is left out
    const digital = inArena();
    return `
        <div class="inventory-card ${selected.has(card.id) ? 'is-selected' : ''} ${waiting(card) ? 'is-pending' : ''}" data-id="${card.id}">
            <input type="checkbox" class="row-check" ${selected.has(card.id) ? 'checked' : ''} aria-label="Select">
            ${image ? `<button class="inventory-thumb" ${digital ? `data-image="${escapeHtml(picture)}"` : ''} title="${card.captures.length ? plural(card.captures.length, 'capture') : digital ? '' : 'Card image'}">
                           <img src="${escapeHtml(image)}" alt="" loading="lazy"></button>`
                    : '<div class="inventory-thumb empty" title="No image"></div>'}
            <div class="inventory-card-info">
                <div class="inventory-card-name">
                    ${card.quantity > 1 ? `<span class="inventory-qty">${card.quantity}×</span> ` : ''}${escapeHtml(card.name)}
                    ${manaHtml(card.mana_cost)}
                </div>
                <div class="inventory-card-details">
                    ${escapeHtml(card.set_name)} ${card.number ? '#' + escapeHtml(card.number) : ''}
                    ${card.type_line ? '· ' + escapeHtml(card.type_line) : ''}
                </div>
                <div class="inventory-card-meta">${badgesHtml(card)}<span>${escapeHtml(card.condition)}</span></div>
            </div>
            <div class="inventory-card-price">
                <div class="inventory-price-value">${priceText(card.price * card.quantity)}</div>
                ${card.quantity > 1 && !digital ? `<div class="inventory-price-each">${money(card.price)} each</div>` : ''}
                <div class="inventory-timestamp" title="Scanned ${escapeHtml(card.timestamp)}">Added ${card.added_quantity ? '' : 'before '}${escapeHtml(card.added_at.slice(0, 16))}${card.added_quantity && card.added_quantity < card.quantity ? ` (+${card.added_quantity})` : ''}</div>
            </div>
            <div class="inventory-card-actions">
                ${picture && !digital ? `<span class="card-peek" data-image="${escapeHtml(picture)}" title="The card's picture - rest the pointer here to see it large">
                                 <img src="${escapeHtml(picture.replace('/normal/', '/small/'))}" alt="" loading="lazy"></span>` : ''}
                <button class="btn-trade" title="Set aside for a trade: it stays in your collection until you confirm the trade"><svg class="icon"><use href="#i-swap"/></svg></button>
                <button class="btn-edit" title="Edit"><svg class="icon"><use href="#i-edit"/></svg></button>
                <button class="btn-delete" title="Delete"><svg class="icon"><use href="#i-trash"/></svg></button>
            </div>
        </div>`;
}

function renderInventory() {
    const list = $('inventory-list');
    const cards = totalQuantity(shown);
    const value = shown.reduce((sum, card) => sum + card.price * card.quantity, 0);
    $('inventory-summary').innerHTML = `<strong>${entriesText(shown.length)}</strong> · ${plural(cards, 'card')}${inArena() ? '' : ` · <strong>${money(value)}</strong>`}`
        + (shown.length !== inventory.length ? ` (of ${inventory.length})` : '');
    list.classList.toggle('is-grid', inventoryView === 'grid');
    markSwitch('view-switch', 'view', inventoryView);

    if (!shown.length) {
        list.classList.remove('is-grid');
        list.innerHTML = inventory.length
            ? '<div class="empty-state">No cards match the filters.</div>'
            : '<div class="empty-state">No cards in the inventory yet.<br>Scan some cards to build your collection.</div>';
    } else {
        // The page stays where it was when the list is loaded again (after an edit); a filter
        // that leaves fewer pages ends on the last one
        const pages = Math.ceil(shown.length / pageSize);
        page = Math.max(0, Math.min(page, pages - 1));
        const first = page * pageSize;
        list.innerHTML = shown.slice(first, first + pageSize).map(inventoryView === 'grid' ? gridCardHtml : listRowHtml).join('');
        $('page-text').textContent = `${(first + 1).toLocaleString()}–${Math.min(first + pageSize, shown.length).toLocaleString()} of ${shown.length.toLocaleString()} · page ${page + 1} of ${pages}`;
        $('page-first').disabled = $('page-prev').disabled = page === 0;
        $('page-next').disabled = $('page-last').disabled = page >= pages - 1;
    }
    $('inventory-pager').hidden = shown.length <= pageSize;
    $('inventory-page-size').value = pageSize;
    renderBulkBar();
}

function renderBulkBar() {
    $('bulk-bar').hidden = selected.size === 0;
    const copies = totalQuantity(inventory.filter(card => selected.has(card.id)));
    $('bulk-count').textContent = `${entriesText(selected.size)} selected (${plural(copies, 'card')})`;
    $('bulk-deck').hidden = !gameInfo.deck_formats.length;
    // "Select all": ticked when every card the filters show is selected, a dash when some are
    const chosen = shown.filter(card => selected.has(card.id)).length;
    $('select-all').checked = shown.length > 0 && chosen === shown.length;
    $('select-all').indeterminate = chosen > 0 && chosen < shown.length;
    $('select-all').disabled = !shown.length;
}

function rowCard(row) {
    // The entry of a list row or grid card (data-id)
    return row && inventory.find(entry => entry.id === parseInt(row.dataset.id));
}

function toggleSelected(id, on) {
    // Nothing can be ticked in Arena view - and so nothing reaches the bulk bar, the Del
    // key or "Set aside for trade", which all work on what is ticked
    if (inArena()) return;
    if (on) selected.add(id); else selected.delete(id);
    const row = document.querySelector(`#inventory-list [data-id="${id}"]`);
    if (row) {
        row.classList.toggle('is-selected', on);
        row.querySelector('.row-check').checked = on;
    }
    renderBulkBar();
}

function pick(id, on, range) {
    // A tick; with Shift held, every entry from the one ticked before to this one (in the
    // order shown, also across pages) is ticked or unticked like it
    if (inArena()) return;
    const from = range ? shown.findIndex(card => card.id === lastPicked) : -1;
    const to = shown.findIndex(card => card.id === id);
    lastPicked = id;
    if (from < 0 || to < 0 || from === to) return toggleSelected(id, on);
    shown.slice(Math.min(from, to), Math.max(from, to) + 1).forEach(card => on ? selected.add(card.id) : selected.delete(card.id));
    renderInventory();
}

function inventoryKey(event) {
    // Keys of the Inventory tab, when no dialog is open and nothing is being typed:
    // / search, Esc clear the selection, arrows previous / next page, Ctrl+A select all shown,
    // Del delete the selection, Ctrl+Z undo
    if (currentTab !== 'inventory' || document.querySelector('.modal.show, .drawer.show')) return;
    const typing = event.target.matches('input, textarea, select') || event.target.isContentEditable;
    const command = event.ctrlKey || event.metaKey;
    if (event.key === 'Escape' && event.target === $('filter-text')) return $('filter-text').blur();
    if (typing || event.altKey) return;  // Ctrl+Z in a text field is the field's own
    if (command && event.key.toLowerCase() === 'z' && pendingChange) {
        event.preventDefault();
        undoPending();
    } else if (event.key === '/' && !command) {
        event.preventDefault();
        $('filter-text').focus();
        $('filter-text').select();
    } else if (event.key === 'Escape' && selected.size) {
        selected.clear();
        renderInventory();
    } else if (command && event.key.toLowerCase() === 'a' && shown.length && !inArena()) {
        event.preventDefault();
        shown.forEach(card => selected.add(card.id));
        renderInventory();
    } else if (event.key === 'Delete' && selected.size) {
        bulkAction('delete');
    } else if ((event.key === 'ArrowLeft' || event.key === 'ArrowRight') && !command && !event.shiftKey) {
        const next = page + (event.key === 'ArrowRight' ? 1 : -1);
        if (next >= 0 && next < Math.ceil(shown.length / pageSize)) showPage(next);
    }
}

function showPage(number) {
    page = number;
    renderInventory();
    // The buttons are below the list: back to its first row, when that is out of sight
    const topbar = document.querySelector('.topbar-nav');  // the row of the header that stays at the top
    const above = $('inventory-list').getBoundingClientRect().top - (topbar ? topbar.offsetHeight : 0) - 12;
    if (above < 0) window.scrollBy(0, above);
}


async function bulkAction(action) {
    const ids = [...selected];
    const entries = entriesText(ids.length);
    let value = '';
    if (action === 'delete') {
        const ok = await confirmDialog({title: `Delete ${entries}?`, confirmText: 'Delete', danger: true,
            message: `The selected cards are removed from your inventory. You have ${UNDO_SECONDS} seconds to undo it.`});
        if (!ok) return;
    } else if (action === 'location') {
        value = await valueDialog({title: `Move ${entries}`, label: 'Location (empty: none)', options: knownLocations()});
        if (value === null) return;
    } else if (action === 'add_tag' || action === 'remove_tag') {
        const tags = distinct(inventory.filter(card => action === 'add_tag' || selected.has(card.id)).flatMap(card => card.tags));
        value = await valueDialog({title: `${action === 'add_tag' ? 'Tag' : 'Remove a tag from'} ${entries}`, label: 'Tag', options: tags});
        if (!value) return;
    } else if (action === 'condition') {
        value = await valueDialog({title: `Condition of ${entries}`, label: 'Condition', choices: CONDITIONS.map(c => [c, c])});
        if (!value) return;
    } else if (action === 'deck') {
        return addSelectionToDeck();
    } else if (action === 'trade') {
        return setAsideForTrade();
    }
    const what = {delete: `Deleting ${entries}`, location: `Moving ${entries} to ${value || 'no location'}`,
                  add_tag: `Tagging ${entries} "${value}"`, remove_tag: `Taking the tag "${value}" off ${entries}`,
                  condition: `Setting ${entries} to ${value}`}[action];
    changeLater(ids, what, {ids, action, value});
}

// -- Undo: a change waits a few seconds before it is sent ---------------------

let pendingChange = null;            // {ids: Set of entry ids, body: for /api/inventory/bulk, timer}
let pendingSent = Promise.resolve(); // the change being sent
let changesAsked = Promise.resolve(); // changeLater, one call after the other
const plainFetch = window.fetch.bind(window);
// Whatever else writes (an edit, a trade, a backup) goes after the change that waits, never
// around it: it is sent first
window.fetch = async (input, options) => {
    if (((options && options.method) || 'GET').toUpperCase() !== 'GET') await sendPending();
    return plainFetch(input, options);
};

function changeLater(ids, text, body) {
    // The Arena collection is not changed here. Every delete and bulk change of the page
    // comes through this function: the one place that says no (the server refuses too)
    if (inArena()) return;
    // A delete or a bulk change: its rows are greyed and the server hears of it when the time
    // to undo is over - or at once when something else is changed or the page is left.
    // One call at a time: two asked for while an earlier change was on its way both waited
    // for it, and the second then took the first one's place - which was never sent
    changesAsked = changesAsked.then(async () => {
        await sendPending();
        pendingChange = {ids: new Set(ids), body, timer: setTimeout(sendPending, UNDO_SECONDS * 1000)};
        $('undo-text').textContent = text;
        $('undo-bar').hidden = false;
        $('undo-time').style.animation = 'none';
        $('undo-time').offsetWidth;  // the bar starts to run out again
        $('undo-time').style.animation = `undoTime ${UNDO_SECONDS}s linear forwards`;
        // Done with these cards: left selected, they went along with the next "Select all" and
        // its move (59 cards put in one box ended up in another, 2026-10-06)
        selected.clear();
        renderInventory();
    });
    return changesAsked;
}

function sendPending() {
    const change = pendingChange;
    if (!change) return pendingSent;
    pendingChange = null;
    clearTimeout(change.timer);
    $('undo-bar').hidden = true;
    pendingSent = (async () => {
        try {
            // keepalive: the request is finished also when the page is left while it is on
            // its way (browsers take up to 64 KB that way - some 8,000 entries)
            const body = JSON.stringify(change.body);
            const response = await plainFetch('/api/inventory/bulk' + areaQuery(), {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                                      body, keepalive: body.length < 60000});
            const data = await response.json();
            if (data.success === false) notify(data.error || 'Request failed', 'error');
            else notify(`${entriesText(data.changed)} ${change.body.action === 'delete' ? 'deleted' : 'changed'}`, 'success');
        } catch (error) {
            notify(`Request failed: ${error.message}`, 'error');
        }
        await loadInventory();
    })();
    return pendingSent;
}

function undoPending() {
    if (!pendingChange) return;
    clearTimeout(pendingChange.timer);
    pendingChange = null;
    $('undo-bar').hidden = true;
    notify('Undone - nothing was changed', 'info');
    renderInventory();
}

function batchPart(card, batch) {
    // The copies of an entry that came with a batch ({batch, added_at, quantity}), if any
    return card.batches.find(part => part.batch === batch);
}

async function removeBatch() {
    // Undo an "Add to collection": only the copies that came with it go
    const added = $('filter-added').value;
    const parts = inventory.map(card => [card, batchPart(card, added)]).filter(([, part]) => part);
    if (!parts.length) return;
    const cards = parts.reduce((sum, [, part]) => sum + part.quantity, 0);
    const kept = parts.filter(([card, part]) => part.quantity < card.quantity).length;
    const ok = await confirmDialog({title: `Remove the ${plural(cards, 'card')} added ${parts[0][1].added_at.slice(0, 16)}?`, confirmText: 'Remove', danger: true,
        message: `They are deleted from the collection (not moved back to the scanner).`
            + (kept ? ` ${entriesText(kept)} had copies before and keep${kept === 1 ? 's' : ''} those.` : '')
            + " This can't be undone."});
    if (!ok) return;
    const data = await api('/api/inventory/remove_batch' + areaQuery(), {method: 'POST', body: {batch: added}});
    if (!data) return;
    notify(`${plural(data.cards, 'card')} removed`, 'success');
    $('filter-added').value = '';
    loadInventory();
}

async function addSelectionToDeck() {
    const data = await api('/api/decks');
    if (!data) return;
    const choice = await valueDialog({title: 'Add to a deck', label: 'Deck (one copy of each selected card)',
        choices: [...data.decks.map(deck => [deck.id, deck.name]), ['new', 'New deck...']]});
    if (!choice) return;
    const text = distinct(inventory.filter(card => selected.has(card.id)).map(card => card.name)).map(name => `1 ${name}`).join('\n');
    if (choice === 'new') {
        // The deck pages make the deck: the card names travel there in this tab's storage
        // (an address would be too short for a long selection) and the page opens its
        // "New deck" dialog with them (decks.js: start-up)
        try { sessionStorage.setItem('newDeckText', text); } catch (e) { /* storage blocked: the dialog opens empty */ }
        window.location.href = '/decks?new=1';
        return;
    }
    const result = await api(`/api/decks/${choice}/import`, {method: 'POST', body: {text}});
    if (result) notify(`${plural(result.added, 'card')} added to ${result.deck.name}`, 'success');
}

// -- Export / import -----------------------------------------------------------

function renderExportMenu() {
    // The sites / apps the collection can be exported for (Game.export_formats), and read from
    const select = $('export-select');
    select.innerHTML = '<option value="">Export…</option>' + gameInfo.exports.map(([format, label]) =>
        `<option value="${escapeHtml(format)}">${escapeHtml(label)}</option>`).join('');
    select.hidden = !gameInfo.exports.length;
    $('import-button').title = `Add the cards of a ${importFormats()} file to your collection`;
}

function importFormats() {
    return [...gameInfo.imports, 'Card Scanner'].join(' or ');
}

async function exportInventory() {
    // The server sends the file as a download; the menu goes back to "Export…"
    const select = $('export-select');
    const format = select.value;
    if (!format) return;
    const label = select.selectedOptions[0].textContent;
    select.value = '';
    // With cards ticked or the list filtered: which of them
    let part = 'all';
    if (selected.size || shown.length !== inventory.length) {
        // Short labels: three buttons share the dialog's one row
        part = await choiceDialog({title: `Export for ${label}`, message: 'Which cards go into the file?', choices: [
            {label: `All ${inventory.length.toLocaleString()}`, value: 'all'},
            ...(shown.length !== inventory.length ? [{label: `${shown.length.toLocaleString()} shown`, value: 'shown', style: selected.size ? undefined : 'primary'}] : []),
            ...(selected.size ? [{label: `${selected.size.toLocaleString()} selected`, value: 'selected', style: 'primary'}] : [])]});
        if (!part) return;
    }
    const link = document.createElement('a');
    if (part === 'all') {
        link.href = `/api/export_inventory/${encodeURIComponent(format)}${areaQuery()}`;
        link.download = '';
    } else {
        const ids = part === 'selected' ? [...selected] : shown.map(card => card.id);
        let response;
        try {
            response = await plainFetch(`/api/export_inventory/${encodeURIComponent(format)}${areaQuery()}`, {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ids})});
        } catch (error) {
            return notify(`Export failed: ${error.message}`, 'error');
        }
        if (!response.ok) return notify('Export failed', 'error');
        link.href = URL.createObjectURL(await response.blob());
        link.download = (/filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') || '') || [])[1] || 'export.csv';
        setTimeout(() => URL.revokeObjectURL(link.href), 60000);
    }
    document.body.appendChild(link);
    link.click();
    link.remove();
    notify(`${label} export started - check your downloads folder`
        + (format === 'moxfield' ? '. Import it at moxfield.com/account/collection'
            : format === 'csv' ? '. Every entry with its location and tags; Import reads it back' : ''), 'success');
}

async function importInventory() {
    const input = $('import-file-input');
    const file = input.files[0];
    if (!file) return;
    const mode = await choiceDialog({
        title: `Import ${file.name}`,
        message: `Reads a ${importFormats()} file. Add its cards to your collection (quantities of matching cards are added up), or replace your whole collection with this file?`,
        choices: [
            {label: 'Cancel', value: null},
            {label: 'Replace inventory', value: 'replace', style: 'danger'},
            {label: 'Add to inventory', value: 'merge', style: 'primary'}
        ]
    });
    if (mode) {
        const form = new FormData();
        form.append('file', file);
        form.append('replace_existing', mode === 'replace' ? 'true' : 'false');
        const data = await api('/api/import_inventory' + areaQuery(), {method: 'POST', body: form});
        if (data) {
            const stats = data.stats;
            const unnamed = stats.unnamed || 0;   // rows without a card name: counted in `skipped`
            const missing = (stats.not_found.length
                ? ` Not found: ${stats.not_found.join(', ')}${stats.skipped - unnamed > stats.not_found.length ? ', ...' : ''}.` : '')
                + (unnamed ? ` ${unnamed} row${unnamed === 1 ? '' : 's'} without a card name.` : '');
            notify(`${stats.format} import: ${stats.added} added, ${stats.updated} added to cards you had`
                + (stats.by_name ? `, ${stats.by_name} matched by name only (check their printing)` : '')
                + (stats.skipped ? `, ${stats.skipped} skipped.` : '.') + (stats.errors ? ` ${stats.errors} errors.` : '') + missing,
                stats.skipped || stats.errors ? 'warning' : 'success');
            loadInventory();
        }
    }
    input.value = '';
}

// ============================================================================
// Statistics (from the loaded inventory)
// ============================================================================

let statsMeasure = 'value';


function groupTotals(rows, keyOf, measure) {
    const totals = new Map();
    rows.forEach(card => {
        const amount = measure === 'value' ? card.price * card.quantity : card.quantity;
        [].concat(keyOf(card)).forEach(key => totals.set(key, (totals.get(key) || 0) + amount));
    });
    return [...totals.entries()];
}

function renderStats() {
    const cards = totalQuantity(inventory);
    const value = inventory.reduce((sum, card) => sum + card.price * card.quantity, 0);
    const tile = (number, label) => `
        <div class="inventory-stat"><div class="inventory-stat-value">${number}</div><div class="inventory-stat-label">${label}</div></div>`;
    $('stats-tiles').innerHTML = tile(cards.toLocaleString(), 'Cards') + tile(inventory.length.toLocaleString(), 'Entries')
        + tile(new Set(inventory.map(card => card.name)).size.toLocaleString(), 'Different cards')
        + (inArena() ? '' : tile(money(value), 'Total value') + tile(money(cards ? value / cards : 0), 'Average per card'));
    // Arena cards have no price: the bars count cards there, and the choice is not offered
    const measure = inArena() ? 'cards' : statsMeasure;
    $('stats-measure').closest('.stats-controls').hidden = inArena();
    markSwitch('stats-measure', 'measure', measure);
    const format = measure === 'value' ? money : number => number.toLocaleString();
    const chart = (title, keyOf, limit) => `
        <div class="panel"><h3 class="section-title">${title}</h3>${barsHtml(groupTotals(inventory, keyOf, measure), format, limit)}</div>`;
    const valuable = [...inventory].sort((a, b) => b.price - a.price).slice(0, 10)
        .map(card => [`${card.name} (${(card.set_code || '').toUpperCase()}${card.finish !== defaultFinish() ? ', ' + finishLabel(card.finish) : ''})`, card.price]);
    $('stats-grid').innerHTML = chart('Color', colorGroup) + chart('Type', mainType)
        + chart('Rarity', card => card.rarity ? card.rarity[0].toUpperCase() + card.rarity.slice(1) : 'Unknown') + chart('Finish', card => finishLabel(card.finish))
        + chart('Set', card => card.set_name) + chart('Location', card => card.location || 'No location')
        + (inventory.some(card => card.tags.length) ? chart('Tag', card => card.tags.length ? card.tags : ['No tag']) : '')
        + `<div class="panel"><h3 class="section-title">Most valuable cards</h3>${barsHtml(valuable, money, 10)}</div>`;
}


// ============================================================================
// Settings drawer: backups of the collection, the scanned cards and the decks
// ============================================================================

function openSettings() {
    $('settings-drawer').classList.add('show');
    loadBackups();
    loadCaptureCopy();
}

// The large copy kept with each capture, for every camera: what is chosen, and what is kept so far
function showCaptureCopy(info) {
    $('capture-copy-size').value = info.size;
    $('capture-copy-hint').textContent = (info.size === 'off'
        ? 'Only the thumbnail of each captured card is kept. '
        : 'The size of the picture of each captured card that is kept beside its thumbnail, for reading it again later or for training a detector; the pages never show it. ')
        + `${info.count} kept so far, ${info.megabytes} MB (data/captures_full). It applies to captures made from now on, from every camera; the ones already kept stay as they are.`;
}

async function loadCaptureCopy() {
    const data = await api('/api/capture_copy');
    if (data) showCaptureCopy(data);
}

async function saveCaptureCopy(size) {
    const data = await api('/api/capture_copy', {method: 'POST', body: {size}});
    if (!data) return loadCaptureCopy();
    showCaptureCopy(data);
    notify(size === 'off' ? 'No large copies will be kept' : `Large copies will be ${size}`, 'success');
}

function closeSettings() {
    $('settings-drawer').classList.remove('show');
    if (window.location.hash === '#settings') history.replaceState(null, '', window.location.pathname);
}

async function loadBackups() {
    const data = await api('/api/backups');
    if (!data) return;
    renderBackups(data.backups);
    $('backup-every').value = String(data.schedule.every_hours);
    $('backup-keep').value = data.schedule.keep;
    $('backup-keep').disabled = !data.schedule.every_hours;
}

async function saveBackupSchedule(change) {
    // How often automatic backups are made, how many are kept (the server answers with what it took)
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
            <button class="btn btn-small" data-backup-action="restore">Restore</button>
            <a class="mini-btn" href="/api/backups/${encodeURIComponent(backup.id)}/download" download title="Download this backup as a zip file, to keep a copy elsewhere"><svg class="icon"><use href="#i-download"/></svg></a>
            <button class="mini-btn" data-backup-action="delete" title="Delete this backup"><svg class="icon"><use href="#i-trash"/></svg></button>
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
    // A backup downloaded earlier joins the list; restoring it is a separate step
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
        if (!data) return loadBackups();  // a backup of the state before may have been made
        renderBackups(data.backups);
        notify(`Backup of ${created} restored`, 'success');
        selected.clear();
        loadInventory();
        loadScanned();
        loadDeckList();  // the decks' names may be different (the location filter lists them)
    } else {
        const ok = await confirmDialog({title: `Delete the backup of ${created}?`, confirmText: 'Delete', danger: true,
            message: "Your cards are not changed. This can't be undone."});
        if (!ok) return;
        const data = await api(`/api/backups/${id}`, {method: 'DELETE'});
        if (data) renderBackups(data.backups);
    }
}

// ============================================================================
// Trades: cards set aside until a trade is confirmed
// ============================================================================

let trades = [];   // open ones first, then the confirmed ones (GET /api/trades)

async function loadTrades() {
    const data = await api('/api/trades');
    if (!data) return;
    trades = data.trades;
    renderTrades();
}

function tradeCardHtml(card, open) {
    const where = [`${(card.set_code || card.set_name).toUpperCase()} ${card.number ? '#' + card.number : ''}`.trim(),
                   card.finish !== defaultFinish() ? finishLabel(card.finish) : '', card.condition, card.location].filter(Boolean);
    return `
        <div class="trade-row" data-row="${card.row}">
            <span class="trade-qty"><strong>${card.quantity}×</strong>${open && card.entry_quantity > card.quantity ? ` <span class="hint">of ${card.entry_quantity}</span>` : ''}</span>
            <span class="trade-card"><span class="result-name">${escapeHtml(card.name)}</span> <span class="result-sub">${escapeHtml(where.join(' · '))}</span></span>
            <span class="summary">${money(card.price * card.quantity)}</span>
            ${open ? `<span class="trade-steps">
                <button class="mini-btn" data-step="-1" title="One copy less" ${card.quantity < 2 ? 'disabled' : ''}>−</button>
                <button class="mini-btn" data-step="1" title="One copy more" ${card.quantity >= card.most ? 'disabled' : ''}>+</button>
                <button class="mini-btn" data-step="0" title="Take it out of the trade - it stays in your collection">×</button>
            </span>` : ''}
        </div>`;
}

function tradeHtml(trade) {
    const open = trade.status === 'open';
    const summary = `${plural(trade.quantity, 'card')} · <strong>${money(trade.value)}</strong> · `
        + (open ? `since ${escapeHtml(trade.created_at.slice(0, 10))}` : `confirmed ${escapeHtml((trade.closed_at || '').slice(0, 10))}`);
    const exports = `<select class="select trade-export" title="Download this trade's cards as a file for another site or app">
        ${optionsHtml([['', 'Export…'], ...gameInfo.exports])}</select>`;
    const cards = trade.cards.map(card => tradeCardHtml(card, open)).join('')
        || '<div class="hint">No cards in this trade.</div>';
    if (!open) {
        return `
            <details class="panel trade" data-trade="${trade.id}">
                <summary><span class="trade-name">${escapeHtml(trade.name)}</span> <span class="summary">${summary}</span></summary>
                <div class="trade-cards">${cards}</div>
                <div class="filter-row trade-actions">
                    <span class="spacer"></span>
                    ${exports}
                    <button class="btn btn-small btn-ghost" data-trade-action="forget" title="Take this trade out of the list - the cards are long gone">Delete from history</button>
                </div>
            </details>`;
    }
    return `
        <div class="panel trade" data-trade="${trade.id}">
            <div class="filter-row">
                <span class="trade-name">${escapeHtml(trade.name)}</span>
                <span class="summary">${summary}</span>
                <span class="spacer"></span>
                ${exports}
                <button class="btn btn-small" data-trade-action="rename">Rename</button>
                <button class="btn btn-small" data-trade-action="cancel" title="The trade is off: its cards simply stay in your collection">Cancel trade</button>
                <button class="btn btn-small btn-primary" data-trade-action="confirm" title="The trade happened: remove its cards from your collection" ${trade.cards.length ? '' : 'disabled'}>Confirm trade</button>
            </div>
            <div class="trade-cards">${cards}</div>
        </div>`;
}

function renderTrades() {
    const open = trades.filter(trade => trade.status === 'open'), done = trades.filter(trade => trade.status !== 'open');
    // Confirmed trades that were unfolded stay so when the list is drawn again
    const unfolded = new Set([...document.querySelectorAll('#trades-done details[open]')].map(item => item.dataset.trade));
    $('trades-open').innerHTML = open.map(tradeHtml).join('')
        || '<div class="empty-state">No trade is open.</div>';
    $('trades-done').innerHTML = done.map(tradeHtml).join('');
    $('trades-done-title').hidden = !done.length;
    document.querySelectorAll('#trades-done details').forEach(item => { item.open = unfolded.has(item.dataset.trade); });
}

// -- Cards to trade or sell: no deck uses them, and EDHREC does not list them for a commander ----

let suggested = null;        // {card name: [decks whose commander it is played with]}, once EDHREC was asked
let spareUnknown = [];       // decks EDHREC had no answer for: their cards are not counted
let spareTicked = new Set(); // entry ids ticked in the list

function spareBase() {
    // The entries no deck uses and EDHREC does not suggest, with the copies that no trade holds
    if (!suggested) return [];
    return inventory.map(card => ({...card, free: card.quantity - (card.trades || []).reduce((sum, trade) => sum + trade.quantity, 0)}))
        .filter(card => card.free > 0 && !card.decks.length && !suggested[card.name])
        .sort((a, b) => b.price * b.free - a.price * a.free || a.name.localeCompare(b.name));
}

function spareCards() {
    // ... of the place chosen above the list ('' = every place, '(none)' = no location)
    const location = $('spare-location').value;
    return spareBase().filter(card => !location || card.location === (location === '(none)' ? '' : location));
}

function renderSpareLocations() {
    // The places to choose from, always there: with the spare cards found, those that have
    // some (with how many entries); before that, every place of the collection. The choice
    // stays while it is on the list
    const select = $('spare-location');
    const chosen = select.value;
    let head, places;   // head: "all" and "(none)"; places: [[location, label]]
    if (suggested) {
        const counts = new Map();
        for (const card of spareBase()) counts.set(card.location, (counts.get(card.location) || 0) + 1);
        head = [['', `All locations (${[...counts.values()].reduce((sum, count) => sum + count, 0)})`],
            ...(counts.has('') ? [['(none)', `(none) (${counts.get('')})`]] : [])];
        places = [...counts].filter(([place]) => place).sort((a, b) => byText(a[0], b[0])).map(([place, count]) => [place, `${place} (${count})`]);
    } else {
        head = [['', 'All locations'], ['(none)', '(none)']];
        places = knownLocations().map(place => [place, place]);
    }
    // As the inventory's location filter: boxes and binders first, the locations named after
    // one of the decks under their own "Decks" heading at the bottom
    const decks = new Set(deckList.map(deck => deck.name.toLowerCase()));
    const inDecks = places.filter(([place]) => decks.has(place.toLowerCase()));
    select.innerHTML = optionsHtml([...head, ...places.filter(option => !inDecks.includes(option))])
        + (inDecks.length ? decksHeadingHtml(select, [...head, ...places].map(([, label]) => label)) + optionsHtml(inDecks) : '');
    select.value = [...head, ...places].some(([value]) => value === chosen) ? chosen : '';
}

function renderSpare() {
    const list = $('spare-list');
    renderSpareLocations();
    if (!suggested) {
        list.innerHTML = '';
        $('spare-summary').textContent = '';
        $('spare-aside').disabled = true;
        return;
    }
    const cards = spareCards();
    // Cards that are gone (set aside, sold) leave the ticks
    spareTicked = new Set([...spareTicked].filter(id => cards.some(card => card.id === id)));
    const value = cards.reduce((sum, card) => sum + card.price * card.free, 0);
    $('spare-summary').innerHTML = `${plural(cards.length, 'entry', 'entries')} · ${plural(cards.reduce((sum, card) => sum + card.free, 0), 'free copy', 'free copies')} · <strong>${money(value)}</strong>`
        + (spareUnknown.length ? ` · <span class="hint">no EDHREC data for ${escapeHtml(spareUnknown.join(', '))} - their cards are not counted</span>` : '');
    list.innerHTML = cards.map(card => {
        const where = [`${(card.set_code || card.set_name).toUpperCase()} ${card.number ? '#' + card.number : ''}`.trim(),
                       card.finish !== defaultFinish() ? finishLabel(card.finish) : '', card.condition, card.location].filter(Boolean);
        return `
        <label class="trade-row spare-row" data-id="${card.id}">
            <span class="trade-qty"><strong>${card.free}×</strong>${card.free < card.quantity ? ` <span class="hint">of ${card.quantity}</span>` : ''}</span>
            <span class="trade-card"><span class="result-name">${escapeHtml(card.name)}</span> <span class="result-sub">${escapeHtml(where.join(' · '))}</span></span>
            <span class="summary">${money(card.price * card.free)}</span>
            <input type="checkbox" class="spare-check" ${spareTicked.has(card.id) ? 'checked' : ''}>
        </label>`;
    }).join('') || '<div class="empty-state">Every card is in a deck, suggested for one, or already set aside.</div>';
    $('spare-all').checked = !!cards.length && spareTicked.size === cards.length;
    $('spare-aside').disabled = !spareTicked.size;
    $('spare-aside').textContent = spareTicked.size ? `Set ${plural(spareTicked.size, 'entry', 'entries')} aside for a trade` : 'Set aside for a trade';
}

async function findSpareCards() {
    // Slow the first time: one request per deck with a commander (then from the 7-day cache)
    $('spare-find').disabled = true;
    $('spare-list').innerHTML = '<div class="hint">Asking EDHREC about your commanders...</div>';
    const data = await api('/api/inventory/suggested');
    $('spare-find').disabled = false;
    if (!data) return renderSpare();
    suggested = data.cards;
    spareUnknown = data.unknown;
    spareTicked.clear();
    $('spare-find').textContent = 'Ask EDHREC again';
    renderSpare();
}

function bindSpareEvents() {
    $('spare-find').addEventListener('click', findSpareCards);
    $('spare-list').addEventListener('change', event => {
        const row = event.target.closest('.spare-row');
        if (!row || !event.target.classList.contains('spare-check')) return;
        const id = parseInt(row.dataset.id);
        if (event.target.checked) spareTicked.add(id); else spareTicked.delete(id);
        renderSpare();
    });
    $('spare-location').addEventListener('change', () => {
        spareTicked.clear();   // what is ticked stays to the place that was shown
        renderSpare();
    });
    $('spare-all').addEventListener('change', event => {
        spareTicked = new Set(event.target.checked ? spareCards().map(card => card.id) : []);
        renderSpare();
    });
    $('spare-aside').addEventListener('click', () => {
        const ids = [...spareTicked];
        spareTicked.clear();
        setAsideForTrade(null, ids);
    });
}

async function setAsideForTrade(card = null, chosen = null) {
    // Every free copy of the selected entries - or of one row's, from its own button; how many
    // of each is changed on the Trades tab
    if (inArena()) return;   // a trade is of paper cards: an Arena entry's id means another card there
    // chosen: the ids ticked in the Trades tab's list of spare cards (it leaves the Inventory's ticks alone)
    const ids = chosen || (card ? [card.id] : [...selected]);
    const open = trades.filter(trade => trade.status === 'open').map(trade => trade.name);
    const name = await valueDialog({title: `Set ${card ? card.name : entriesText(ids.length)} aside for a trade`,
        label: open.length ? 'Trade (one of the open ones, or a new name)' : 'Name of the trade (who it is with)',
        options: open, value: open.length === 1 ? open[0] : ''});
    if (!name) return;
    const data = await api('/api/trades', {method: 'POST', body: {name, ids}});
    if (!data) return;
    notify(`${plural(data.cards, 'card')} set aside for "${data.name}"`
        + (data.skipped ? ` - ${entriesText(data.skipped)} had no copy left to give` : ''), data.cards ? 'success' : 'warning');
    if (!card && !chosen) selected.clear();  // a row's button leaves what is ticked alone
    loadInventory();
    loadTrades();
}

async function tradeAction(trade, action) {
    let request = null;
    if (action === 'confirm') {
        const ok = await confirmDialog({title: `Confirm the trade "${trade.name}"?`, confirmText: 'Confirm trade', danger: true,
            message: `Its ${plural(trade.quantity, 'card')} (${money(trade.value)}) are removed from your collection. This can't be undone.`});
        if (ok) request = api(`/api/trades/${trade.id}/confirm`, {method: 'POST'});
    } else if (action === 'cancel') {
        // Not confirmDialog: its "Cancel" beside "Cancel trade" says nothing
        const ok = await choiceDialog({title: `Cancel the trade "${trade.name}"?`,
            message: `Its ${plural(trade.quantity, 'card')} stay in your collection and are free again.`,
            choices: [{label: 'Keep the trade', value: false}, {label: 'Cancel the trade', value: true, style: 'danger'}]});
        if (ok === true) request = api(`/api/trades/${trade.id}`, {method: 'DELETE'});
    } else if (action === 'forget') {
        const ok = await confirmDialog({title: `Delete "${trade.name}" from the history?`, confirmText: 'Delete', danger: true,
            message: 'Only the record of the trade goes; your collection is not changed.'});
        if (ok) request = api(`/api/trades/${trade.id}`, {method: 'DELETE'});
    } else if (action === 'rename') {
        const name = await valueDialog({title: 'Rename the trade', label: 'Name', value: trade.name});
        if (name) request = api(`/api/trades/${trade.id}`, {method: 'PUT', body: {name}});
    }
    if (!request) return;
    const data = await request;
    if (data && action === 'confirm') notify(`${plural(data.cards, 'card')} left your collection`, 'success');
    loadTrades();
    loadInventory();
}

function bindTradeEvents() {
    $('tab-trades').addEventListener('click', async event => {
        const panel = event.target.closest('[data-trade]');
        const trade = panel && trades.find(item => item.id === parseInt(panel.dataset.trade));
        if (!trade) return;
        const action = event.target.closest('[data-trade-action]');
        if (action) return tradeAction(trade, action.dataset.tradeAction);
        const step = event.target.closest('[data-step]');
        const row = event.target.closest('[data-row]');
        const card = row && trade.cards.find(item => item.row === parseInt(row.dataset.row));
        if (!step || !card) return;
        const by = parseInt(step.dataset.step);
        await api(`/api/trades/${trade.id}/cards/${card.row}`, {method: 'PUT', body: {quantity: by ? card.quantity + by : 0}});
        loadTrades();
        loadInventory();
    });
    $('tab-trades').addEventListener('change', event => {
        // The server sends the file as a download; the menu goes back to "Export…"
        const select = event.target.closest('.trade-export');
        if (!select || !select.value) return;
        const link = document.createElement('a');
        link.href = `/api/trades/${select.closest('[data-trade]').dataset.trade}/export/${encodeURIComponent(select.value)}`;
        link.download = '';
        document.body.appendChild(link);
        link.click();
        link.remove();
        select.value = '';
    });
}

// ============================================================================
// Events
// ============================================================================


function bindInventoryEvents() {
    // Filters
    ['filter-type', 'filter-rarity', 'filter-set', 'filter-finish', 'filter-location', 'filter-tag', 'filter-added',
     'filter-free', 'filter-text-not'].forEach(id => $(id).addEventListener('change', filtersChanged));
    ['filter-text', 'filter-price-min', 'filter-price-max'].forEach(id => $(id).addEventListener('input', debounce(filtersChanged, 150)));
    bindColorChips('filter-colors', filterColors, filtersChanged);
    $('filter-clear').addEventListener('click', () => {
        ['filter-text', 'filter-type', 'filter-rarity', 'filter-set', 'filter-finish', 'filter-location', 'filter-tag',
         'filter-added', 'filter-price-min', 'filter-price-max'].forEach(id => { $(id).value = ''; });
        $('filter-free').checked = false;
        $('filter-text-not').checked = false;
        filterColors.clear();
        fillFilterOptions();
        filtersChanged();
    });
    $('inventory-sort').addEventListener('change', event => {
        remember('collectionSort', event.target.value);
        page = 0;
        applyFilters();
    });
    $('inventory-page-size').innerHTML = optionsHtml(PAGE_SIZES.map(size => [size, `${size} per page`]));
    $('inventory-page-size').addEventListener('change', event => {
        // The card at the top of the page stays on the page
        const first = page * pageSize;
        pageSize = parseInt(event.target.value);
        remember('collectionPageSize', pageSize);
        page = Math.floor(first / pageSize);
        renderInventory();
    });
    $('page-first').addEventListener('click', () => showPage(0));
    $('page-prev').addEventListener('click', () => showPage(page - 1));
    $('page-next').addEventListener('click', () => showPage(page + 1));
    $('page-last').addEventListener('click', () => showPage(Infinity));
    bindSwitch('view-switch', button => {
        inventoryView = button.dataset.view;
        remember('collectionView', inventoryView);
        renderInventory();
    });
    $('import-file-input').addEventListener('change', importInventory);
    $('export-select').addEventListener('change', exportInventory);
    $('scanned-add').addEventListener('click', addScanned);
    $('batch-remove').addEventListener('click', removeBatch);

    // Inventory rows
    $('inventory-list').addEventListener('click', event => {
        const badge = event.target.closest('.inventory-badge[data-filter]');
        if (badge) return filterByBadge(badge.dataset.filter, badge.dataset.value);
        const row = event.target.closest('[data-id]');
        const card = rowCard(row);
        if (!card) return;
        if (event.target.closest('.btn-trade')) return setAsideForTrade(card);
        if (inArena()) return;   // Arena view: a row has nothing to click (its buttons are hidden, too)
        if (event.target.closest('.btn-edit')) return editCard(card, knownLocations(), () => shown);
        // No question first: it can be undone
        if (event.target.closest('.btn-delete')) return changeLater([card.id], `Deleting ${card.name}`, {ids: [card.id], action: 'delete', value: ''});
        if (event.target.closest('.inventory-thumb') && card.captures.length) return openCaptures(card);
        if (event.target.classList.contains('row-check')) return pick(card.id, event.target.checked, event.shiftKey);
        // Grid: a click on the card selects it, a double click edits it
        if (row.classList.contains('grid-card')) pick(card.id, !selected.has(card.id), event.shiftKey);
    });
    $('inventory-list').addEventListener('mousedown', event => {
        // A shift-click selects cards, not the text between them
        if (event.shiftKey) event.preventDefault();
    });
    $('inventory-list').addEventListener('dblclick', event => {
        const row = event.target.closest('.grid-card');
        const card = inArena() ? null : rowCard(row);   // no editing in Arena view
        if (card) {
            toggleSelected(card.id, false);
            editCard(card, knownLocations(), () => shown);
        }
    });
    $('filter-chips').addEventListener('click', event => {
        const chip = event.target.closest('[data-chip]');
        if (!chip) return;
        activeFilters()[parseInt(chip.dataset.chip)].clear();
        filtersChanged();
    });
    $('undo-button').addEventListener('click', undoPending);
    // Leaving the page is not an undo: what waits is sent on the way out
    window.addEventListener('pagehide', () => { sendPending(); });
    document.addEventListener('keydown', inventoryKey);
    $('bulk-bar').addEventListener('click', event => {
        const button = event.target.closest('[data-bulk]');
        if (button) bulkAction(button.dataset.bulk);
    });
    $('select-all').addEventListener('change', event => {
        // Every card the filters show (also the ones further down the list), or none of them
        shown.forEach(card => event.target.checked ? selected.add(card.id) : selected.delete(card.id));
        renderInventory();
    });
    $('bulk-all').addEventListener('click', () => {
        shown.forEach(card => selected.add(card.id));
        renderInventory();
    });
    $('bulk-none').addEventListener('click', () => {
        selected.clear();
        renderInventory();
    });
    bindValueDialog();
}

// Topmost first
const OVERLAYS = [
    ['dialog-modal', () => closeDialog(null)],
    ['value-modal', () => closeValueDialog(null)],
    ['capture-modal', closeCaptures],
    ['edit-card-modal', closeEditCard],
    ['settings-drawer', closeSettings],
];

function bindEvents() {
    $('tabs').addEventListener('click', event => {
        const tab = event.target.closest('.tab');
        if (tab) showTab(tab.dataset.tab);
    });
    bindInventoryEvents();
    bindSpareEvents();
    bindSwitch('stats-measure', button => {
        statsMeasure = button.dataset.measure;
        renderStats();
    });
    bindTradeEvents();
    bindSwitch('collection-kind', button => showCollection(button.dataset.collection));
    $('arena-import').addEventListener('click', () => $('arena-file-input').click());
    $('arena-file-input').addEventListener('change', event => {
        if (event.target.files.length) importArena(event.target.files);
        event.target.value = '';   // the same files can be chosen again after the next export
    });
    bindCardPreview();

    // Settings drawer (backups); /collection#settings opens it (the scanner's settings link here)
    $('settings-close').addEventListener('click', closeSettings);
    $('backup-create').addEventListener('click', createBackup);
    $('backup-upload').addEventListener('click', () => $('backup-upload-input').click());
    $('backup-upload-input').addEventListener('change', event => {
        if (event.target.files[0]) uploadBackup(event.target.files[0]);
        event.target.value = '';  // the same file can be chosen again
    });
    $('capture-copy-size').addEventListener('change', event => saveCaptureCopy(event.target.value));
    $('backup-every').addEventListener('change', event => saveBackupSchedule({every_hours: parseInt(event.target.value)}));
    $('backup-keep').addEventListener('change', event => saveBackupSchedule({keep: parseInt(event.target.value) || 1}));
    $('backup-note').addEventListener('keydown', event => { if (event.key === 'Enter') createBackup(); });
    $('backup-list').addEventListener('click', event => {
        const button = event.target.closest('[data-backup-action]');
        if (button) backupAction(button.closest('[data-backup]'), button.dataset.backupAction);
    });
    if (window.location.hash === '#settings') openSettings();

    // Overlays: Escape closes the topmost one open, a click outside the one clicked
    bindOverlays(OVERLAYS);
}

// The inventory changes while scanning on another tab or device
// (collection_updated: a trade changed - only this page listens)
['inventory_updated', 'inventory_undone', 'inventory_prices_updated', 'collection_updated'].forEach(name =>
    socket.on(name, debounce(() => {
        loadInventory();
        if (currentTab === 'trades') loadTrades();
    }, 500)));
socket.on('game_changed', () => window.location.reload());
// An Arena export was imported (on this page or another): the Arena view shows the new state
socket.on('arena_updated', () => { if (inArena()) { loadArenaInfo(); loadInventory(); } });
socket.on('database_update_complete', () => {
    notify('Card database updated', 'success');
    loadInventory();
    loadDeckList();
});

document.addEventListener('DOMContentLoaded', async () => {
    const data = await api('/api/games');
    if (!data) return;
    gameInfo = data.games.find(game => game.id === data.active);
    $('game-label').textContent = data.games.length > 1 ? gameInfo.label : '';
    // "Decks" in the header is a link to the deck pages; a game without decks has none
    $('page-link-decks').hidden = !gameInfo.deck_formats.length;
    renderExportMenu();
    bindEvents();
    // The Physical | Arena switch, for a game that has a digital collection; the one last looked at
    $('collection-kind').hidden = !gameInfo.arena;
    if (gameInfo.arena && recall('collectionKind', 'paper') === 'arena') await showCollection('arena');
    else await loadInventory();
    loadDeckList();  // the location filter lists the decks' locations apart
    loadTrades();  // "Set aside for trade" offers the open ones
    // Always the inventory first, whichever tab was used last (the page used to reopen on it)
    showTab('inventory');
});
