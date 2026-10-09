"""
Card games the scanner supports, and which one is being scanned (one game at a time; saved as
`game` in data/settings.json). See MULTI_GAME_IMPLEMENTATION_PLAN.md.

    games.init(database, settings, log_callback)   # once, at startup
    games.active()                                 # the Game being scanned
"""
DEFAULT_GAME = 'mtg'

_games = {}
_active_id = DEFAULT_GAME
_settings = None


def init(database, settings, log_callback=None):
    """Create the game objects and restore the saved game"""
    global _settings, _active_id
    from games.mtg import Magic
    _games.clear()
    for game_class in (Magic,):
        game = game_class(database, log_callback)
        _games[game.id] = game
    _settings = settings
    saved = settings.get('game', DEFAULT_GAME) if settings else DEFAULT_GAME
    _active_id = saved if saved in _games else DEFAULT_GAME


def active_id():
    """Id of the game being scanned - usable before init() (defaults to Magic)"""
    return _active_id


def active():
    return _games[_active_id]


def get(game_id):
    return _games.get(game_id)


def all_games():
    return list(_games.values())


def set_active(game_id):
    global _active_id
    if game_id not in _games:
        raise ValueError(f"Unknown game: {game_id}")
    _active_id = game_id
    if _settings:
        _settings.set('game', game_id)
