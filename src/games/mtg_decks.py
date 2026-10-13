"""
Magic deck formats, the rules checked while building a deck (deck size, copies, color identity,
legality - reported as issues, never enforced) and text decklists ("1 Sol Ring" lines, as
Moxfield / Archidekt / Arena write them).
"""
import re

# Format id (also Scryfall's legality key) -> rules. size: main deck (exact: neither more nor
# less - Commander counts the commander); copies: of one card, over main deck and sideboard
DECK_FORMATS = {
    'commander': {'label': 'Commander', 'size': 100, 'exact': True, 'copies': 1, 'sideboard': 0, 'commander': True},
    'standard': {'label': 'Standard', 'size': 60, 'copies': 4, 'sideboard': 15},
    'pioneer': {'label': 'Pioneer', 'size': 60, 'copies': 4, 'sideboard': 15},
    'modern': {'label': 'Modern', 'size': 60, 'copies': 4, 'sideboard': 15},
    'legacy': {'label': 'Legacy', 'size': 60, 'copies': 4, 'sideboard': 15},
    'vintage': {'label': 'Vintage', 'size': 60, 'copies': 4, 'sideboard': 15},
    'pauper': {'label': 'Pauper', 'size': 60, 'copies': 4, 'sideboard': 15},
}

# Where a card sits in a deck. 'side' is the sideboard of a 60-card deck; in a Commander deck
# (no sideboard) it holds cards being considered and is not checked
BOARDS = ('commander', 'main', 'side')

NUMBER_WORDS = {'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8,
                'nine': 9, 'ten': 10}
# Two commanders are allowed when they pair up through one of these
PAIRING_TEXT = ('partner', 'friends forever', 'choose a background', "doctor's companion")


def copy_limit(card, default):
    """How many copies of a card a deck may have (None = any number)"""
    text = card.get('oracle_text') or ''
    type_line = card.get('type_line') or ''
    if 'Basic' in type_line and 'Land' in type_line:
        return None
    if 'A deck can have any number of cards named' in text:
        return None
    limited = re.search(r'A deck can have up to (\w+) cards named', text)
    if limited:
        return NUMBER_WORDS.get(limited.group(1).lower(), default)
    return default


def _front(text):
    """Front face of a double-faced card's type line or name"""
    return (text or '').split(' // ')[0]


def can_be_commander(card):
    type_line = _front(card.get('type_line'))
    return ('Legendary' in type_line and 'Creature' in type_line) \
        or 'can be your commander' in (card.get('oracle_text') or '')


def _issue(level, message, cards=()):
    return {'level': level, 'message': message, 'cards': sorted(set(cards))}


def check_deck(format_id, entries):
    """
    What is wrong or unfinished in a deck.

    Args:
        entries: [{'name', 'quantity', 'board', 'card'}] - card is the card data
            (database.cards_by_names) or None when the name isn't in the card data
    Returns:
        [{'level': 'error' | 'warning', 'message', 'cards': [names]}]
    """
    rules = DECK_FORMATS.get(format_id)
    if not rules:
        return []
    label = rules['label']
    issues = []
    is_commander = rules.get('commander')
    # A Commander deck's 'side' board is only a list of cards being considered
    playing = [e for e in entries if not (is_commander and e['board'] == 'side')]
    unknown = [e['name'] for e in playing if not e.get('card')]
    if unknown:
        issues.append(_issue('warning', 'Not in the card data (not checked)', unknown))
    known = [e for e in playing if e.get('card')]
    main_count = sum(e['quantity'] for e in playing if e['board'] != 'side')
    side_count = sum(e['quantity'] for e in playing if e['board'] == 'side')

    # Deck size
    if is_commander:
        if main_count > rules['size']:
            issues.append(_issue('error', f"{main_count} cards - a {label} deck has exactly {rules['size']}, the commander included"))
        elif main_count < rules['size']:
            issues.append(_issue('warning', f"{main_count} of {rules['size']} cards"))
    else:
        if main_count < rules['size']:
            issues.append(_issue('warning', f"{main_count} cards in the main deck - at least {rules['size']}"))
        if side_count > rules['sideboard']:
            issues.append(_issue('error', f"{side_count} cards in the sideboard - at most {rules['sideboard']}"))

    # Legality (cards downloaded before deck data existed have none: nothing to check)
    with_legalities = [e for e in known if e['card'].get('legalities')]
    banned = [e['name'] for e in with_legalities if e['card']['legalities'].get(format_id) == 'banned']
    not_legal = [e['name'] for e in with_legalities
                 if e['card']['legalities'].get(format_id) not in ('legal', 'restricted', 'banned')]
    if banned:
        issues.append(_issue('error', f"Banned in {label}", banned))
    if not_legal:
        issues.append(_issue('error', f"Not legal in {label}", not_legal))

    # Copies of one card, over every board that is played
    copies, cards = {}, {}
    for entry in known:
        copies[entry['name']] = copies.get(entry['name'], 0) + entry['quantity']
        cards[entry['name']] = entry['card']
    too_many = []
    for name, count in copies.items():
        limit = copy_limit(cards[name], rules['copies'])
        if limit is not None and cards[name].get('legalities', {}).get(format_id) == 'restricted':
            limit = 1
        if limit is not None and count > limit:
            too_many.append(f"{name} ({count}, at most {limit})")
    if too_many:
        issues.append(_issue('error', 'Too many copies', too_many))

    if is_commander:
        commanders = [e for e in known if e['board'] == 'commander']
        if not any(e['board'] == 'commander' for e in playing):
            issues.append(_issue('warning', 'No commander chosen'))
        if len(commanders) > 2:
            issues.append(_issue('error', 'More than two commanders', [e['name'] for e in commanders]))
        unfit = [e['name'] for e in commanders if not can_be_commander(e['card'])]
        if unfit:
            issues.append(_issue('warning', 'Not a legendary creature', unfit))
        if len(commanders) == 2 and not all(
                any(text in (e['card'].get('oracle_text') or '').lower() for text in PAIRING_TEXT)
                or 'Background' in (e['card'].get('type_line') or '') for e in commanders):
            issues.append(_issue('warning', 'Two commanders need Partner, Friends forever, a Background '
                                            "or Doctor's companion", [e['name'] for e in commanders]))
        if commanders and all(e['card'].get('oracle_id') for e in commanders):
            identity = set().union(*(e['card'].get('color_identity') or [] for e in commanders))
            outside = [e['name'] for e in known
                       if not set(e['card'].get('color_identity') or []) <= identity]
            if outside:
                colors = ''.join(c for c in 'WUBRG' if c in identity) or 'colorless'
                issues.append(_issue('error', f"Outside the commander's color identity ({colors})", outside))
    return issues


# ----------------------------------------------------------------------------
# Text decklists
# ----------------------------------------------------------------------------

SECTION_BOARDS = {
    'commander': 'commander', 'commanders': 'commander',
    'deck': 'main', 'main': 'main', 'mainboard': 'main', 'maindeck': 'main', 'main deck': 'main',
    'sideboard': 'side', 'side': 'side', 'maybeboard': 'side', 'considering': 'side',
    'companion': 'side',
}
# "4 Lightning Bolt", "4x Lightning Bolt (2X2) 117", "1 Sol Ring (C21) 263 *F* [Ramp]"
CARD_LINE = re.compile(r'^(?:(\d+)\s*x?\s+)?(.+?)(?:\s+\(([A-Za-z0-9]{2,6})\)(?:\s+([A-Za-z0-9★\-]+))?)?$')
TRAILING_NOTES = re.compile(r'(\s+\*[A-Za-z]+\*|\s+\[[^\]]*\]|\s+\^[^^]*\^|\s+#\S.*)+$')


def parse_decklist(text, commander_format=False):
    """
    Cards of a pasted decklist: [{'name', 'quantity', 'board', 'set', 'number'}]. Sections
    ("Commander", "Deck", "Sideboard") set the board; without any, a blank line starts the
    sideboard of a 60-card list (the Arena / MTGO layout).
    """
    entries = []
    board = 'main'
    seen_section = False
    after_blank = False
    for line in (text or '').splitlines():
        line = line.strip()
        if not line:
            after_blank = bool(entries)
            continue
        if line.startswith('//') and line.lstrip('/ ').lower().rstrip(':') not in SECTION_BOARDS:
            continue
        section = re.sub(r'\s*\(\d+\)$', '', line.lstrip('/ ').rstrip(':')).lower()
        if section in SECTION_BOARDS:
            board = SECTION_BOARDS[section]
            seen_section = True
            continue
        if after_blank and not seen_section and not commander_format:
            board = 'side'
        after_blank = False
        match = CARD_LINE.match(TRAILING_NOTES.sub('', line))
        if not match or not match.group(2).strip():
            continue
        entries.append({
            'name': match.group(2).strip(),
            'quantity': max(1, int(match.group(1) or 1)),
            'board': board,
            'set': (match.group(3) or '').lower(),
            'number': match.group(4) or '',
        })
    return entries


def format_decklist(entries, format_id=None):
    """Text decklist (the layout Moxfield and Archidekt import): sections, "1 Name" lines"""
    is_commander = DECK_FORMATS.get(format_id, {}).get('commander')
    titles = {'commander': 'Commander', 'main': 'Deck', 'side': 'Maybeboard' if is_commander else 'Sideboard'}
    parts = []
    for board in BOARDS:
        lines = [f"{e['quantity']} {e['name']}" for e in sorted(entries, key=lambda e: e['name'])
                 if e['board'] == board and e['quantity'] > 0]
        if lines:
            parts.append('\n'.join([titles[board]] + lines))
    return '\n\n'.join(parts) + '\n'
