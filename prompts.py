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
            'instructions': """This is a Magic: The Gathering card. Please identify THREE pieces of information:

1. The card name (located at the top-left of the card)
2. The collector number (located at the BOTTOM-LEFT corner of the card)
3. The set code (the 3-4 character code at the start of LINE 2 in the BOTTOM-LEFT corner)

IMPORTANT INSTRUCTIONS FOR COLLECTOR NUMBER:
- The collector number is at the BOTTOM-LEFT corner in a TWO-LINE format:
  * LINE 1: A letter followed by 4-digit number (e.g., "E 0367", "D 0045", "B 0123")
  * LINE 2: Set code · Language (e.g., "LTR · EN", "M21 · EN")
- Look for this two-line pattern to identify the correct location
- Return ONLY the 4-digit number from Line 1 (e.g., "0367" not "E 0367")
- The letter is just a visual marker to help you find it - don't include it
- DO NOT confuse it with the mana cost symbols in the TOP-RIGHT corner
- The mana cost has symbols like {1}, {W}, {U}, {B}, {R}, {G} - IGNORE these completely

IMPORTANT INSTRUCTIONS FOR SET CODE:
- It is the first thing on LINE 2, before the separator and language (e.g. "LTR" in "LTR · EN")
- Return only the code, e.g. "LTR", "M21", "HOB" - if you cannot read it, return "Unknown"

Rules:
- If you see a double-faced card, return the front face name
- Return ONLY the 4-digit number (e.g., "0367", "0045", "0123")
- If you cannot find the collector number, return "Unknown"
- NEVER use the top-right corner mana cost as the collector number""",
            'answer_format': """Return your answer in EXACTLY this format:
NAME: [card name]
NUMBER: [4-digit number only]
SET: [set code]

Example response:
NAME: Lightning Bolt
NUMBER: 0367
SET: M21

Your response:""",
        },
        # Modern cards print a star instead of a dot between set code and language on
        # foil copies; asked about a zoomed crop of the bottom-left corner
        'foil': {
            'label': 'Foil marker (★/•)',
            'instructions': ("This is the bottom-left corner of a Magic: The Gathering card. The last line shows a set code, "
                             "a small separator symbol, and a language code - for example 'HOB • EN' or 'HOB ★ EN'. "
                             "Is the separator a five-pointed STAR or a round DOT?"),
            'answer_format': "Answer with one word: star, dot, or unclear.",
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
