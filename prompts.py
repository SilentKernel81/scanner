"""
Vision AI prompts, editable from the web interface (Settings -> Vision AI -> Prompts).

Each prompt has two parts:
- instructions: what the card looks like and where to read each field - editable;
- answer format: fixed and appended by the code, so an edit can never break the parser.

Edited instructions are saved to data/prompts.json, per game and prompt kind, either for
all models ('default') or for one model ('provider:model'):

    {"mtg": {"identify": {"default": "...", "local:qwen3.5:4b": "..."}}}

Lookup order: this model -> all models -> built-in.
"""
import json
import logging
import os
import threading

from config import Config

logger = logging.getLogger('ai')

PROMPTS_FILE = Config.DATA_DIR / 'prompts.json'
DEFAULT_GAME = 'mtg'
ALL_MODELS = 'default'
MAX_LENGTH = 8000

BUILT_IN = {
    'mtg': {
        'identify': {
            'label': 'Card identification',
            # No example values on purpose: on blurry cards, models copied them ("0367",
            # "LTR") instead of saying they could not read the card
            'instructions': """This is a Magic: The Gathering card. Read three things:
- NAME: the card name at the top-left (for a double-faced card, the front face).
- NUMBER: the collector number at the bottom-left corner, first line: a letter and a number - give only the number, with its leading zeros. It is not the mana cost at the top-right.
- SET: the set code (3-4 letters or digits) at the start of the bottom-left second line, before the language code.
Copy exactly what is printed. If a value is blurry or unreadable, write Unknown - do not guess.""",
            # With "Answer with exactly three lines" qwen3.5:9b often dropped the labels, and
            # sometimes the number line ("Mirkwood / HOB") - asking for the labels fixed both
            'answer_format': """Always answer with all three lines, each with its label:
NAME: <card name>
NUMBER: <collector number, or Unknown>
SET: <set code, or Unknown>""",
        },
        # Modern cards print a star instead of a dot between set code and language on
        # foil copies; asked about a zoomed crop of the bottom-left corner
        'foil': {
            'label': 'Foil marker (★/•)',
            # Describing both shapes matters: asked only "star or dot?", qwen3.5:9b called
            # 25 of 84 regular cards foil; with this wording none (40/40 readable foils right)
            'instructions': ("This is the bottom-left corner of a Magic: The Gathering card. Find the line with the set code "
                             "and the language code, like 'HOB•EN' or 'HOB★EN'. Look closely at the small symbol between "
                             "them: a dot is a plain round point; a star has five sharp points. Which is it? If that line is cut "
                             "off or not visible, answer unclear."),
            'answer_format': "Answer with one word: star, dot, or unclear.",
        },
    },
    'pokemon': {
        'identify': {
            'label': 'Card identification',
            # Short and plain on purpose: a longer version (what not to read, "Pokédex number",
            # format hints) made qwen3.5:9b answer in Markdown sentences for 14-23 of 40 cards
            # and take 2-2.4 s on camera captures; this one ~0.9 s, 0-1 of 160 chatty
            'instructions': """This is a Pokémon card. Read three things:
- NAME: the card name at the top, with its suffix if any (ex, V, VMAX, VSTAR, GX). A basic Energy card is named by the type of its big symbol, like Water Energy or Psychic Energy - not "Basic Energy".
- NUMBER: the card number at the bottom, like 012/193 (promo cards: a code like SWSH095).
- SET: the set code of 2-4 letters next to the number, before EN. Older cards have none: Unknown.
Copy exactly what is printed. If a value is unreadable, write Unknown.""",
            'answer_format': """Always answer with all three lines, each with its label:
NAME: <card name>
NUMBER: <card number as printed, or Unknown>
SET: <set abbreviation, or Unknown>""",
        },
    },
}

_lock = threading.RLock()
_overrides = None  # loaded on first use


def model_key(provider, model):
    return f"{provider}:{model}"


def _load():
    global _overrides
    if _overrides is None:
        try:
            _overrides = json.loads(PROMPTS_FILE.read_text()) if PROMPTS_FILE.exists() else {}
        except (OSError, ValueError) as e:
            logger.warning(f"Could not read {PROMPTS_FILE.name}, using built-in prompts: {e}")
            _overrides = {}
    return _overrides


def _write():
    Config.DATA_DIR.mkdir(exist_ok=True)
    tmp = PROMPTS_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(_overrides, indent=2, ensure_ascii=False) + '\n')
    os.replace(tmp, PROMPTS_FILE)


def _built_in(kind, game):
    try:
        return BUILT_IN[game][kind]
    except KeyError:
        raise ValueError(f"Unknown prompt: {game}/{kind}")


def instructions(kind, provider, model, game=DEFAULT_GAME):
    """(instructions text in effect, source) - source is 'model', 'all' or 'built-in'"""
    built_in = _built_in(kind, game)
    with _lock:
        saved = _load().get(game, {}).get(kind, {})
        if model_key(provider, model) in saved:
            return saved[model_key(provider, model)], 'model'
        if ALL_MODELS in saved:
            return saved[ALL_MODELS], 'all'
    return built_in['instructions'], 'built-in'


def build(kind, text, game=DEFAULT_GAME):
    """Full prompt sent to the AI: instructions + the fixed answer format"""
    return f"{text.strip()}\n\n{_built_in(kind, game)['answer_format']}"


def prompt(kind, provider, model, game=DEFAULT_GAME):
    """Full prompt in effect for this provider/model"""
    return build(kind, instructions(kind, provider, model, game)[0], game)


def save(kind, text, provider=None, model=None, game=DEFAULT_GAME):
    """Save instructions for one model (provider and model given) or for all models"""
    _built_in(kind, game)
    text = (text or '').strip()
    if not text:
        raise ValueError("The prompt is empty")
    if len(text) > MAX_LENGTH:
        raise ValueError(f"The prompt is too long (max {MAX_LENGTH} characters)")
    key = model_key(provider, model) if provider and model else ALL_MODELS
    with _lock:
        _load().setdefault(game, {}).setdefault(kind, {})[key] = text
        _write()
    logger.info(f"Prompt '{game}/{kind}' saved for {'all models' if key == ALL_MODELS else key}")


def reset(kind, provider, model, game=DEFAULT_GAME):
    """
    Remove the saved prompt in effect for this model: its own one if any, otherwise the
    one for all models. Returns the source that was removed (or None).
    """
    _built_in(kind, game)
    with _lock:
        saved = _load().get(game, {}).get(kind, {})
        for key, source in ((model_key(provider, model), 'model'), (ALL_MODELS, 'all')):
            if key in saved:
                del saved[key]
                _write()
                logger.info(f"Prompt '{game}/{kind}' for {'all models' if key == ALL_MODELS else key} reset")
                return source
    return None


def has(kind, game=DEFAULT_GAME):
    """Whether a game uses this kind of prompt (the foil marker check is Magic-only)"""
    return kind in BUILT_IN.get(game, {})


def status(provider, model, game=DEFAULT_GAME):
    """What the prompt editor shows for the current provider/model"""
    result = {}
    for kind, built_in in BUILT_IN[game].items():
        text, source = instructions(kind, provider, model, game)
        with _lock:
            saved = _load().get(game, {}).get(kind, {})
            has_all = ALL_MODELS in saved
        result[kind] = {
            'label': built_in['label'],
            'instructions': text,
            'source': source,
            'answer_format': built_in['answer_format'],
            'built_in': built_in['instructions'],
            'has_all_models': has_all,
        }
    return result
