# Multi-Game Card Scanner Implementation Plan

**Created:** 2025-11-26
**Status:** PLANNING
**Target Games:** Magic: The Gathering, Pokemon TCG, Yu-Gi-Oh!, Disney Lorcana

---

## Table of Contents
1. [Overview](#overview)
2. [Architecture Design](#architecture-design)
3. [Database Schema](#database-schema)
4. [Game-Specific Implementations](#game-specific-implementations)
5. [Implementation Phases](#implementation-phases)
6. [File Changes](#file-changes)
7. [Testing Strategy](#testing-strategy)
8. [Migration Path](#migration-path)
9. [Timeline & Estimates](#timeline--estimates)

---

## Overview

### Goals
- Support multiple trading card games in a single scanner application
- Maintain backward compatibility with existing MTG inventory
- Provide game-specific card identification and database integration
- Enable users to switch between games seamlessly
- Preserve existing functionality while adding flexibility

### Non-Goals
- Mixed-game inventory (each scan session targets one game)
- Simultaneous multi-game scanning
- Cross-game card comparison or analytics

### Key Principles
- **Abstraction:** Common scanning logic, game-specific details
- **Extensibility:** Easy to add new games in the future
- **Data Integrity:** Separate game inventories, no data mixing
- **User Experience:** Minimal UI changes, intuitive game selection

---

## Architecture Design

### Current Architecture (MTG-Only)
```
Scanner → Vision AI → Database (Scryfall) → Inventory
         ↓
    Card detected → AI identifies → Search MTG DB → Add to MTG inventory
```

### New Architecture (Multi-Game)
```
Scanner → Vision AI → Game Adapter → Game-Specific DB → Inventory
         ↓              ↓                ↓                  ↓
    Card detected → Game context → Pokemon API      → Pokemon inventory
                                  → Scryfall API     → MTG inventory
                                  → YGOPRODeck API   → YuGiOh inventory
                                  → Lorcana API      → Lorcana inventory
```

### Component Responsibilities

**1. Game Abstraction Layer** (`card_games.py` - NEW)
- Define base `CardGame` class with common interface
- Implement game-specific subclasses
- Handle game-specific data parsing and validation

**2. Game Manager** (`game_manager.py` - NEW)
- Manage active game selection
- Load/unload game-specific databases
- Route requests to appropriate game handler

**3. Database Layer** (`database.py` - MODIFIED)
- Support game-agnostic schema
- Handle game-specific queries
- Maintain separate tables per game (or use game column)

**4. Vision AI** (`card_identifier.py` - MODIFIED)
- Accept game context
- Use game-specific prompts
- Parse game-specific responses

**5. UI Layer** (`scanner.html`, `app.py` - MODIFIED)
- Game selector dropdown
- Game-specific inventory views
- Game-specific card detail displays

---

## Database Schema

### Option A: Unified Table with JSON (Recommended)

**Pros:** Single table, easier queries, flexible attributes
**Cons:** Some data redundancy, JSON queries less efficient

```sql
-- Unified cards table
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game TEXT NOT NULL,                    -- 'mtg', 'pokemon', 'yugioh', 'lorcana'
    name TEXT NOT NULL,
    flavor_name TEXT,                      -- Alternate names (e.g., Universes Beyond)
    set_code TEXT,
    set_name TEXT,
    collector_number TEXT,
    rarity TEXT,
    price_usd REAL,
    price_usd_foil REAL,
    image_uri TEXT,

    -- Common attributes
    card_type TEXT,                        -- 'Monster', 'Spell', 'Creature', etc.
    description TEXT,                      -- Card text/effect

    -- Game-specific attributes stored as JSON
    attributes JSON,                       -- Flexible storage for game-specific data

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_cards_game ON cards(game);
CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(game, name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_cards_set ON cards(game, set_code, collector_number);

-- Unified inventory table
CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game TEXT NOT NULL,
    card_name TEXT NOT NULL,
    set_name TEXT,
    card_number TEXT,
    rarity TEXT,
    card_type TEXT,

    -- Common inventory fields
    quantity INTEGER DEFAULT 1,
    condition TEXT DEFAULT 'Near Mint',
    foil BOOLEAN DEFAULT 0,

    -- Game-specific variants (JSON)
    variants JSON,                         -- Holofoil, Reverse Holo, Surge Foil, etc.

    -- Pricing
    price_usd REAL,

    -- Game-specific attributes
    attributes JSON,

    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(game, card_name, set_name, card_number, condition, foil)
);

CREATE INDEX IF NOT EXISTS idx_inventory_game ON inventory(game);
CREATE INDEX IF NOT EXISTS idx_inventory_card ON inventory(game, card_name COLLATE NOCASE);
```

**JSON Attributes Examples:**

```json
// MTG attributes
{
  "mana_cost": "{2}{U}{U}",
  "colors": ["U"],
  "color_identity": ["Blue"],
  "type_line": "Instant",
  "oracle_text": "Counter target spell."
}

// Pokemon attributes
{
  "hp": 120,
  "type": "Fire",
  "stage": "Stage 1",
  "evolves_from": "Charmander",
  "weakness": "Water",
  "resistance": null,
  "retreat_cost": 2,
  "abilities": [{"name": "Blaze", "text": "..."}],
  "attacks": [{"name": "Flamethrower", "cost": ["R","R"], "damage": 90}]
}

// Yu-Gi-Oh attributes
{
  "card_type": "Monster",
  "monster_type": "Dragon",
  "attribute": "DARK",
  "level": 8,
  "atk": 3000,
  "def": 2500,
  "pendulum_scale": null,
  "link_rating": null,
  "archetype": "Blue-Eyes"
}

// Lorcana attributes
{
  "ink_cost": 5,
  "ink_type": "Amber",
  "inkwell": true,
  "strength": 4,
  "willpower": 5,
  "lore": 2,
  "character_version": "Strange Sorcerer",
  "franchise": "Aladdin"
}
```

### Option B: Separate Tables Per Game

**Pros:** Strongly typed, better performance, clearer schema
**Cons:** More complex queries, code duplication, harder to add games

```sql
-- MTG Cards (existing)
CREATE TABLE IF NOT EXISTS cards_mtg (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    -- ... existing MTG schema
);

-- Pokemon Cards
CREATE TABLE IF NOT EXISTS cards_pokemon (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    set_code TEXT,
    set_name TEXT,
    collector_number TEXT,
    rarity TEXT,
    hp INTEGER,
    type TEXT,
    stage TEXT,
    evolves_from TEXT,
    retreat_cost INTEGER,
    price_usd REAL,
    image_uri TEXT
);

-- Yu-Gi-Oh Cards
CREATE TABLE IF NOT EXISTS cards_yugioh (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    card_type TEXT,
    monster_type TEXT,
    attribute TEXT,
    level INTEGER,
    atk INTEGER,
    def INTEGER,
    price_usd REAL,
    image_uri TEXT
);

-- Similar for Lorcana, inventory tables, etc.
```

**Recommendation:** Use **Option A (Unified with JSON)** for easier extensibility and simpler codebase.

---

## Game-Specific Implementations

### 1. Pokemon TCG

**API:** [Pokemon TCG API](https://docs.pokemontcg.io/)
- **Endpoint:** `https://api.pokemontcg.io/v2/cards`
- **Auth:** API Key required (free)
- **Rate Limit:** 20,000 requests/day
- **Data Quality:** Excellent, official card data

**Card Layout:**
- Name: Top of card
- HP: Top right corner
- Collector Number: Bottom left (format: "025/165")
- Set Symbol: Right side
- Rarity: Indicated by symbol

**Vision AI Prompt:**
```python
POKEMON_IDENTIFICATION_PROMPT = """This is a Pokemon Trading Card Game card. Please identify:

1. The Pokemon name (large text at the top of the card)
2. The collector number (bottom-left corner in format "XXX/YYY" or "XXX/YYY ◆" for holos)

IMPORTANT INSTRUCTIONS FOR COLLECTOR NUMBER:
- Located at BOTTOM-LEFT corner
- Format: Three-digit number / total (e.g., "025/165", "001/078")
- May have symbols after it (◆ for holo, ★ for rare) - ignore these
- Return ONLY the first number before the slash

Return your answer in EXACTLY this format:
NAME: [Pokemon name]
NUMBER: [collector number]

Examples:
NAME: Charizard
NUMBER: 006

NAME: Pikachu
NUMBER: 025

Your response:"""
```

**Attributes Mapping:**
```python
class PokemonGame(CardGame):
    name = "Pokemon TCG"
    short_code = "pokemon"
    database_api = "https://api.pokemontcg.io/v2/cards"

    card_attributes = {
        'hp': 'integer',
        'type': 'string',       # Fire, Water, Grass, etc.
        'stage': 'string',      # Basic, Stage 1, Stage 2, V, VMAX, etc.
        'evolves_from': 'string',
        'weakness': 'string',
        'resistance': 'string',
        'retreat_cost': 'integer',
        'abilities': 'json',
        'attacks': 'json'
    }

    rarity_values = ['Common', 'Uncommon', 'Rare', 'Rare Holo',
                     'Rare Ultra', 'Rare Secret', 'Promo']

    foil_types = ['Normal', 'Holofoil', 'Reverse Holofoil']
```

**Database Search:**
```python
def search_by_name(self, card_name, collector_number=None):
    """Search Pokemon TCG API"""
    params = {
        'q': f'name:"{card_name}"',
        'select': 'id,name,number,set,rarity,images,tcgplayer'
    }

    if collector_number:
        params['q'] += f' number:{collector_number}'

    response = requests.get(
        self.database_api,
        params=params,
        headers={'X-Api-Key': self.api_key}
    )

    # Parse response and map to unified schema
    return self._parse_pokemon_card(response.json())
```

---

### 2. Yu-Gi-Oh!

**API:** [YGOPRODeck API](https://ygoprodeck.com/api-guide/)
- **Endpoint:** `https://db.ygoprodeck.com/api/v7/cardinfo.php`
- **Auth:** None required
- **Rate Limit:** Generous (no official limit)
- **Data Quality:** Good, community-maintained

**Card Layout:**
- Name: Top of card
- Card Number: Bottom right (format: "LOB-001")
- ATK/DEF: Bottom right for monsters
- Level: Stars at top for monsters

**Vision AI Prompt:**
```python
YUGIOH_IDENTIFICATION_PROMPT = """This is a Yu-Gi-Oh! trading card. Please identify:

1. The card name (at the top of the card, in large text)
2. The card number/code (bottom-right corner, format like "LOB-001" or "LART-EN001")

IMPORTANT INSTRUCTIONS FOR CARD NUMBER:
- Located at BOTTOM-RIGHT corner
- Format: Set code + dash + number (e.g., "LOB-001", "SDBE-EN001")
- May have edition text (1st Edition, Limited Edition) nearby - ignore this
- Return the FULL code including set prefix

Return your answer in EXACTLY this format:
NAME: [card name]
NUMBER: [card code]

Examples:
NAME: Blue-Eyes White Dragon
NUMBER: LOB-001

NAME: Dark Magician
NUMBER: YGLD-ENB01

Your response:"""
```

**Attributes Mapping:**
```python
class YuGiOhGame(CardGame):
    name = "Yu-Gi-Oh!"
    short_code = "yugioh"
    database_api = "https://db.ygoprodeck.com/api/v7/cardinfo.php"

    card_attributes = {
        'card_type': 'string',      # Monster, Spell, Trap
        'monster_type': 'string',   # Dragon, Spellcaster, etc. (if monster)
        'attribute': 'string',      # DARK, LIGHT, EARTH, etc.
        'level': 'integer',         # Star level (monsters)
        'atk': 'integer',
        'def': 'integer',
        'scale': 'integer',         # Pendulum scale
        'link_rating': 'integer',   # Link monsters
        'archetype': 'string'
    }

    rarity_values = ['Common', 'Rare', 'Super Rare', 'Ultra Rare',
                     'Secret Rare', 'Starlight Rare', 'Ghost Rare']

    card_types = ['Monster', 'Spell', 'Trap']
```

**Database Search:**
```python
def search_by_name(self, card_name, card_number=None):
    """Search YGOPRODeck API"""
    params = {'name': card_name}

    response = requests.get(self.database_api, params=params)
    cards = response.json().get('data', [])

    if card_number and cards:
        # Filter by card number/set
        cards = [c for c in cards
                 if any(card_number in img.get('id', '')
                        for img in c.get('card_sets', []))]

    return self._parse_yugioh_card(cards[0]) if cards else None
```

---

### 3. Disney Lorcana

**API:** [Lorcana API](https://lorcana-api.com/) (Unofficial)
- **Endpoint:** `https://api.lorcana-api.com/cards/all`
- **Auth:** None required
- **Rate Limit:** Unknown (unofficial API)
- **Data Quality:** Good but unofficial, may need fallback

**Alternative:** Scrape from [Lorcana Deckbuilder](https://dreamborn.ink/) or build own database

**Card Layout:**
- Name: Top of card
- Character Version: Below name (e.g., "Sorcerer's Apprentice")
- Ink Cost: Top left
- Collector Number: Bottom left (format: "001/204")

**Vision AI Prompt:**
```python
LORCANA_IDENTIFICATION_PROMPT = """This is a Disney Lorcana card. Please identify:

1. The character name (top of card, large text)
2. The collector number (bottom-left corner, format "XXX/YYY")

IMPORTANT INSTRUCTIONS FOR COLLECTOR NUMBER:
- Located at BOTTOM-LEFT corner of the card
- Format: Three-digit number / total (e.g., "001/204", "042/204")
- May have copyright symbol (©) nearby - ignore it
- Return ONLY the first number before the slash

NOTE: Some cards have a character version (like "Sorcerer's Apprentice") below the name.
Include only the main character name, not the version subtitle.

Return your answer in EXACTLY this format:
NAME: [character name]
NUMBER: [collector number]

Examples:
NAME: Mickey Mouse
NUMBER: 001

NAME: Elsa
NUMBER: 042

Your response:"""
```

**Attributes Mapping:**
```python
class LorcanaGame(CardGame):
    name = "Disney Lorcana"
    short_code = "lorcana"
    database_api = "https://api.lorcana-api.com/cards/fetch"

    card_attributes = {
        'ink_cost': 'integer',
        'ink_type': 'string',       # Amber, Amethyst, Emerald, Ruby, Sapphire, Steel
        'inkwell': 'boolean',       # Can be used as ink
        'strength': 'integer',      # Attack value
        'willpower': 'integer',     # HP/defense
        'lore': 'integer',          # Victory points
        'character_version': 'string',  # e.g., "Sorcerer's Apprentice"
        'franchise': 'string',      # Disney movie/franchise
        'card_type': 'string'       # Character, Action, Item, Location
    }

    rarity_values = ['Common', 'Uncommon', 'Rare', 'Super Rare',
                     'Legendary', 'Enchanted']

    ink_types = ['Amber', 'Amethyst', 'Emerald', 'Ruby', 'Sapphire', 'Steel']
```

**Database Search:**
```python
def search_by_name(self, card_name, collector_number=None):
    """Search Lorcana API (or fallback to local DB)"""
    # API may be rate-limited, consider caching
    params = {
        'name': card_name,
        'number': collector_number
    }

    try:
        response = requests.get(self.database_api, params=params, timeout=5)
        return self._parse_lorcana_card(response.json())
    except:
        # Fallback to local database if API unavailable
        return self._search_local_lorcana_db(card_name, collector_number)
```

---

## Implementation Phases

### PHASE 0: Preparation & Planning (4 hours)
**Goal:** Set up architecture foundation

- [x] Create this implementation plan
- [ ] Review current codebase dependencies
- [ ] Set up test card images for each game
- [ ] Register API keys (Pokemon TCG, others as needed)
- [ ] Create feature branch: `feature/multi-game-support`

**Deliverables:**
- Implementation plan document ✓
- Test image collection (5-10 cards per game)
- API credentials configured

---

### PHASE 1: Core Architecture (12 hours)
**Goal:** Build game abstraction layer without breaking existing MTG functionality

#### Task 1.1: Create Game Abstraction Layer (4 hours)
**File:** `card_games.py` (NEW)

```python
"""
Game abstraction layer for multi-game card scanner
Defines base class and game-specific implementations
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
import requests
import logging

logger = logging.getLogger('games')


class CardGame(ABC):
    """Base class for all card game implementations"""

    # Override in subclasses
    name: str = ""
    short_code: str = ""
    database_api: str = ""
    card_attributes: Dict[str, str] = {}
    rarity_values: List[str] = []

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
        self.logger = logger

    @abstractmethod
    def get_vision_prompt(self) -> str:
        """Return Vision AI prompt for this game"""
        pass

    @abstractmethod
    def search_by_name(self, card_name: str, collector_number: Optional[str] = None) -> Optional[Dict]:
        """Search game's database API for card"""
        pass

    @abstractmethod
    def parse_card_data(self, api_response: Dict) -> Dict:
        """Parse API response into unified card format"""
        pass

    def get_foil_types(self) -> List[str]:
        """Return list of foil types for this game"""
        return ['Normal', 'Foil']

    def validate_card_data(self, card_data: Dict) -> bool:
        """Validate card data structure"""
        required_fields = ['name', 'set_name', 'collector_number']
        return all(field in card_data for field in required_fields)


class MTGGame(CardGame):
    """Magic: The Gathering implementation"""

    name = "Magic: The Gathering"
    short_code = "mtg"
    database_api = "https://api.scryfall.com/cards"

    card_attributes = {
        'mana_cost': 'string',
        'colors': 'json',
        'color_identity': 'json',
        'type_line': 'string',
        'oracle_text': 'string',
        'power': 'string',
        'toughness': 'string',
        'loyalty': 'string'
    }

    rarity_values = ['common', 'uncommon', 'rare', 'mythic', 'special', 'bonus']

    def get_vision_prompt(self) -> str:
        """Return MTG-specific prompt"""
        # Use existing CARD_IDENTIFICATION_PROMPT from card_identifier.py
        from card_identifier import CardIdentifier
        return CardIdentifier.CARD_IDENTIFICATION_PROMPT

    def search_by_name(self, card_name: str, collector_number: Optional[str] = None) -> Optional[Dict]:
        """Search Scryfall API"""
        # Delegate to existing database.py logic
        from database import CardDatabase
        db = CardDatabase()
        return db.search_by_name(card_name, collector_number)

    def parse_card_data(self, api_response: Dict) -> Dict:
        """Parse Scryfall response"""
        # Use existing parsing logic
        return api_response

    def get_foil_types(self) -> List[str]:
        return ['Normal', 'Foil', 'Surge Foil', 'Etched']


# Import other game classes
from card_games_pokemon import PokemonGame
from card_games_yugioh import YuGiOhGame
from card_games_lorcana import LorcanaGame


# Game registry
AVAILABLE_GAMES = {
    'mtg': MTGGame,
    'pokemon': PokemonGame,
    'yugioh': YuGiOhGame,
    'lorcana': LorcanaGame
}


def get_game(game_code: str, api_key: Optional[str] = None) -> CardGame:
    """Factory function to get game instance"""
    if game_code not in AVAILABLE_GAMES:
        raise ValueError(f"Unknown game: {game_code}. Available: {list(AVAILABLE_GAMES.keys())}")

    return AVAILABLE_GAMES[game_code](api_key=api_key)
```

**Testing:**
```python
# test_card_games.py
def test_game_factory():
    mtg = get_game('mtg')
    assert mtg.name == "Magic: The Gathering"
    assert mtg.short_code == "mtg"

def test_mtg_prompt():
    mtg = get_game('mtg')
    prompt = mtg.get_vision_prompt()
    assert "Magic: The Gathering" in prompt
```

#### Task 1.2: Create Game Manager (3 hours)
**File:** `game_manager.py` (NEW)

```python
"""
Game Manager - Handles active game selection and context
"""

import os
import json
from pathlib import Path
from typing import Optional
from card_games import get_game, CardGame, AVAILABLE_GAMES


class GameManager:
    """Manages current active game and game switching"""

    def __init__(self, config_file: str = 'data/game_config.json'):
        self.config_file = Path(config_file)
        self.current_game: Optional[CardGame] = None
        self.game_code: str = 'mtg'  # Default to MTG

        # Load saved game preference
        self._load_config()

        # Initialize current game
        self.switch_game(self.game_code)

    def _load_config(self):
        """Load game configuration from file"""
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                    self.game_code = config.get('current_game', 'mtg')
            except Exception as e:
                print(f"Error loading game config: {e}")

    def _save_config(self):
        """Save game configuration to file"""
        self.config_file.parent.mkdir(exist_ok=True)
        with open(self.config_file, 'w') as f:
            json.dump({
                'current_game': self.game_code,
                'last_updated': str(Path().ctime())
            }, f, indent=2)

    def switch_game(self, game_code: str) -> bool:
        """Switch to a different game"""
        try:
            # Get API key from environment if needed
            api_key = None
            if game_code == 'pokemon':
                api_key = os.getenv('POKEMON_TCG_API_KEY')

            # Create game instance
            self.current_game = get_game(game_code, api_key=api_key)
            self.game_code = game_code

            # Save preference
            self._save_config()

            print(f"Switched to game: {self.current_game.name}")
            return True

        except Exception as e:
            print(f"Error switching to {game_code}: {e}")
            return False

    def get_current_game(self) -> CardGame:
        """Get current active game instance"""
        if not self.current_game:
            raise RuntimeError("No game initialized")
        return self.current_game

    def get_available_games(self) -> dict:
        """Get list of available games"""
        return {
            code: game_class(None).name
            for code, game_class in AVAILABLE_GAMES.items()
        }

    def get_game_info(self) -> dict:
        """Get current game information"""
        if not self.current_game:
            return {}

        return {
            'code': self.game_code,
            'name': self.current_game.name,
            'rarity_values': self.current_game.rarity_values,
            'foil_types': self.current_game.get_foil_types(),
            'attributes': self.current_game.card_attributes
        }


# Global game manager instance
_game_manager = None

def get_game_manager() -> GameManager:
    """Get global game manager instance (singleton)"""
    global _game_manager
    if _game_manager is None:
        _game_manager = GameManager()
    return _game_manager
```

#### Task 1.3: Update Configuration (2 hours)
**File:** `config.py` (MODIFIED)

Add game-related configuration:

```python
# Game settings
CURRENT_GAME = os.getenv('CARD_GAME') or config.get('game', 'current', default='mtg')
GAME_CONFIG_FILE = DATA_DIR / 'game_config.json'

# Game-specific API keys
POKEMON_TCG_API_KEY = os.getenv('POKEMON_TCG_API_KEY')
YUGIOH_API_KEY = os.getenv('YUGIOH_API_KEY')  # If needed in future
LORCANA_API_KEY = os.getenv('LORCANA_API_KEY')  # If needed in future
```

#### Task 1.4: Database Schema Migration (3 hours)
**File:** `database_migration.py` (NEW)

```python
"""
Database migration script - Add multi-game support
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime


def migrate_to_multi_game(db_file: str = 'data/cards_database.db'):
    """
    Migrate existing MTG database to multi-game schema

    Steps:
    1. Rename existing cards table to cards_mtg_backup
    2. Create new unified cards table
    3. Migrate MTG data to new table with game='mtg'
    4. Create game column indexes
    5. Update inventory table
    """

    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()

    print("Starting database migration...")

    # Step 1: Backup existing table
    print("1. Creating backup of existing cards table...")
    cursor.execute("ALTER TABLE cards RENAME TO cards_mtg_backup")
    cursor.execute("ALTER TABLE inventory RENAME TO inventory_mtg_backup")

    # Step 2: Create new multi-game schema
    print("2. Creating new multi-game schema...")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game TEXT NOT NULL DEFAULT 'mtg',
            name TEXT NOT NULL,
            flavor_name TEXT,
            set_code TEXT,
            set_name TEXT,
            collector_number TEXT,
            rarity TEXT,
            price_usd REAL,
            price_usd_foil REAL,
            image_uri TEXT,
            card_type TEXT,
            description TEXT,
            attributes JSON,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Step 3: Migrate MTG data
    print("3. Migrating MTG card data...")

    cursor.execute('''
        INSERT INTO cards (
            game, name, flavor_name, set_code, set_name, collector_number,
            rarity, price_usd, price_usd_foil, image_uri, card_type, description, attributes
        )
        SELECT
            'mtg' as game,
            name,
            flavor_name,
            set_code,
            set_name,
            collector_number,
            rarity,
            price_usd,
            price_usd_foil,
            image_uri,
            type_line as card_type,
            oracle_text as description,
            json_object(
                'mana_cost', mana_cost,
                'colors', colors,
                'type_line', type_line,
                'oracle_text', oracle_text
            ) as attributes
        FROM cards_mtg_backup
    ''')

    rows_migrated = cursor.rowcount
    print(f"   Migrated {rows_migrated} MTG cards")

    # Step 4: Create indexes
    print("4. Creating indexes...")
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_cards_game ON cards(game)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(game, name COLLATE NOCASE)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_cards_set ON cards(game, set_code, collector_number)')

    # Step 5: Migrate inventory
    print("5. Creating new inventory table...")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game TEXT NOT NULL DEFAULT 'mtg',
            card_name TEXT NOT NULL,
            set_name TEXT,
            card_number TEXT,
            rarity TEXT,
            card_type TEXT,
            quantity INTEGER DEFAULT 1,
            condition TEXT DEFAULT 'Near Mint',
            foil BOOLEAN DEFAULT 0,
            variants JSON,
            price_usd REAL,
            attributes JSON,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(game, card_name, set_name, card_number, condition, foil)
        )
    ''')

    cursor.execute('''
        INSERT INTO inventory (
            game, card_name, set_name, card_number, rarity, card_type,
            quantity, condition, foil, price_usd, attributes
        )
        SELECT
            'mtg' as game,
            card_name,
            set_name,
            card_number,
            rarity,
            type_line as card_type,
            quantity,
            condition,
            foil,
            price_usd,
            json_object(
                'mana_cost', mana_cost,
                'colors', colors,
                'surge', surge
            ) as attributes
        FROM inventory_mtg_backup
    ''')

    inventory_migrated = cursor.rowcount
    print(f"   Migrated {inventory_migrated} inventory entries")

    cursor.execute('CREATE INDEX IF NOT EXISTS idx_inventory_game ON inventory(game)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_inventory_card ON inventory(game, card_name COLLATE NOCASE)')

    # Commit changes
    conn.commit()

    print("\n✓ Migration completed successfully!")
    print(f"  - {rows_migrated} cards migrated")
    print(f"  - {inventory_migrated} inventory entries migrated")
    print("\nBackup tables created:")
    print("  - cards_mtg_backup")
    print("  - inventory_mtg_backup")
    print("\nTo rollback: Rename backup tables back to 'cards' and 'inventory'")

    conn.close()


def rollback_migration(db_file: str = 'data/cards_database.db'):
    """Rollback migration - restore from backup"""
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()

    print("Rolling back migration...")
    cursor.execute("DROP TABLE IF EXISTS cards")
    cursor.execute("DROP TABLE IF EXISTS inventory")
    cursor.execute("ALTER TABLE cards_mtg_backup RENAME TO cards")
    cursor.execute("ALTER TABLE inventory_mtg_backup RENAME TO inventory")

    conn.commit()
    conn.close()
    print("✓ Rollback completed")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == 'rollback':
        rollback_migration()
    else:
        # Create backup first
        backup_file = f"data/cards_database_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        import shutil
        shutil.copy('data/cards_database.db', backup_file)
        print(f"Created backup: {backup_file}")

        migrate_to_multi_game()
```

**Testing Migration:**
```bash
# Backup database first
cp data/cards_database.db data/cards_database_backup.db

# Run migration
python database_migration.py

# Verify migration
sqlite3 data/cards_database.db "SELECT COUNT(*), game FROM cards GROUP BY game"

# Rollback if needed
python database_migration.py rollback
```

---

### PHASE 2: Pokemon TCG Integration (10 hours)
**Goal:** Fully implement Pokemon card scanning and identification

#### Task 2.1: Pokemon Game Implementation (4 hours)
**File:** `card_games_pokemon.py` (NEW)

```python
"""
Pokemon TCG game implementation
"""

from typing import Dict, List, Optional
import requests
from card_games import CardGame


class PokemonGame(CardGame):
    """Pokemon Trading Card Game implementation"""

    name = "Pokemon TCG"
    short_code = "pokemon"
    database_api = "https://api.pokemontcg.io/v2/cards"

    card_attributes = {
        'hp': 'integer',
        'type': 'string',
        'stage': 'string',
        'evolves_from': 'string',
        'weakness': 'string',
        'resistance': 'string',
        'retreat_cost': 'integer',
        'abilities': 'json',
        'attacks': 'json',
        'rules': 'json'
    }

    rarity_values = [
        'Common', 'Uncommon', 'Rare',
        'Rare Holo', 'Rare Holo EX', 'Rare Holo GX', 'Rare Holo V', 'Rare Holo VMAX',
        'Rare Ultra', 'Rare Secret', 'Rare Rainbow',
        'Promo', 'Amazing Rare', 'Radiant Rare'
    ]

    pokemon_types = [
        'Colorless', 'Darkness', 'Dragon', 'Fairy', 'Fighting',
        'Fire', 'Grass', 'Lightning', 'Metal', 'Psychic', 'Water'
    ]

    def get_vision_prompt(self) -> str:
        """Return Pokemon-specific Vision AI prompt"""
        return """This is a Pokemon Trading Card Game card. Please identify:

1. The Pokemon name (large text at the top of the card)
2. The collector number (bottom-left corner in format "XXX/YYY" or "XXX/YYY ◆")

IMPORTANT INSTRUCTIONS FOR COLLECTOR NUMBER:
- Located at BOTTOM-LEFT corner
- Format: Three-digit number / total (e.g., "025/165", "001/078")
- May have symbols after it (◆ for holo, ★ for rare) - ignore these
- Some cards have letters before the number (like "SV025/165") - include the letters
- Return ONLY the part before the slash

Return your answer in EXACTLY this format:
NAME: [Pokemon name]
NUMBER: [collector number]

Examples:
NAME: Charizard
NUMBER: 006

NAME: Pikachu
NUMBER: 025

NAME: Mewtwo
NUMBER: SV010

Your response:"""

    def search_by_name(self, card_name: str, collector_number: Optional[str] = None) -> Optional[Dict]:
        """
        Search Pokemon TCG API for card

        API Docs: https://docs.pokemontcg.io/
        """
        try:
            # Build query
            query = f'name:"{card_name}"'
            if collector_number:
                query += f' number:{collector_number}'

            params = {
                'q': query,
                'select': 'id,name,number,set,rarity,images,tcgplayer,hp,types,subtypes,attacks,abilities,weaknesses,resistances,retreatCost,evolvesFrom,rules'
            }

            headers = {}
            if self.api_key:
                headers['X-Api-Key'] = self.api_key

            self.logger.info(f"Searching Pokemon TCG API: {query}")

            response = requests.get(
                self.database_api,
                params=params,
                headers=headers,
                timeout=10
            )

            response.raise_for_status()
            data = response.json()

            if data.get('data') and len(data['data']) > 0:
                return self.parse_card_data(data['data'][0])
            else:
                self.logger.warning(f"No Pokemon cards found for: {card_name}")
                return None

        except Exception as e:
            self.logger.error(f"Pokemon API search error: {e}")
            return None

    def parse_card_data(self, api_response: Dict) -> Dict:
        """Parse Pokemon TCG API response into unified format"""

        # Extract pricing
        price_usd = None
        price_usd_foil = None

        if 'tcgplayer' in api_response and api_response['tcgplayer']:
            prices = api_response['tcgplayer'].get('prices', {})

            # Normal price
            if 'normal' in prices:
                price_usd = prices['normal'].get('market')
            elif 'holofoil' in prices:
                price_usd_foil = prices['holofoil'].get('market')

            # Reverse holo
            if 'reverseHolofoil' in prices:
                if not price_usd_foil:
                    price_usd_foil = prices['reverseHolofoil'].get('market')

        # Build attributes JSON
        attributes = {
            'hp': api_response.get('hp'),
            'type': api_response.get('types', [None])[0],  # Primary type
            'types': api_response.get('types', []),
            'stage': api_response.get('subtypes', [None])[0],
            'evolves_from': api_response.get('evolvesFrom'),
            'retreat_cost': len(api_response.get('retreatCost', [])),
            'abilities': api_response.get('abilities', []),
            'attacks': api_response.get('attacks', []),
            'rules': api_response.get('rules', [])
        }

        # Handle weakness and resistance
        if api_response.get('weaknesses'):
            weakness = api_response['weaknesses'][0]
            attributes['weakness'] = f"{weakness.get('type')} {weakness.get('value', '')}"

        if api_response.get('resistances'):
            resistance = api_response['resistances'][0]
            attributes['resistance'] = f"{resistance.get('type')} {resistance.get('value', '')}"

        # Build unified card data
        card_data = {
            'name': api_response.get('name'),
            'set_code': api_response.get('set', {}).get('id'),
            'set_name': api_response.get('set', {}).get('name'),
            'collector_number': api_response.get('number'),
            'rarity': api_response.get('rarity'),
            'price_usd': price_usd,
            'price_usd_foil': price_usd_foil,
            'image_uri': api_response.get('images', {}).get('large'),
            'card_type': attributes.get('stage', 'Pokemon'),
            'description': self._build_description(api_response),
            'attributes': attributes
        }

        return card_data

    def _build_description(self, card_data: Dict) -> str:
        """Build card description from attacks and abilities"""
        parts = []

        # Add abilities
        if card_data.get('abilities'):
            for ability in card_data['abilities']:
                parts.append(f"[{ability.get('type')}] {ability.get('name')}: {ability.get('text', '')}")

        # Add attacks
        if card_data.get('attacks'):
            for attack in card_data['attacks']:
                cost = ''.join(attack.get('cost', []))
                damage = attack.get('damage', '')
                text = attack.get('text', '')
                parts.append(f"[{cost}] {attack.get('name')} {damage}: {text}")

        return '\n'.join(parts)

    def get_foil_types(self) -> List[str]:
        """Pokemon-specific foil types"""
        return ['Normal', 'Holofoil', 'Reverse Holofoil']
```

#### Task 2.2: Pokemon Database Setup (2 hours)
**File:** `setup_pokemon_database.py` (NEW)

```python
"""
Download and setup Pokemon TCG database
Similar to setup_database.py but for Pokemon cards
"""

import requests
import sqlite3
import json
from pathlib import Path
from game_manager import get_game_manager


def download_pokemon_cards():
    """
    Download all Pokemon cards from Pokemon TCG API

    Note: Pokemon API has pagination, need to fetch multiple pages
    """

    print("Downloading Pokemon TCG card database...")

    api_key = get_game_manager().current_game.api_key
    headers = {'X-Api-Key': api_key} if api_key else {}

    all_cards = []
    page = 1
    page_size = 250

    while True:
        print(f"Fetching page {page}...")

        params = {
            'page': page,
            'pageSize': page_size,
            'orderBy': 'set.releaseDate'
        }

        response = requests.get(
            'https://api.pokemontcg.io/v2/cards',
            params=params,
            headers=headers,
            timeout=30
        )

        response.raise_for_status()
        data = response.json()

        cards = data.get('data', [])
        if not cards:
            break

        all_cards.extend(cards)
        print(f"  Downloaded {len(cards)} cards (total: {len(all_cards)})")

        # Check if there are more pages
        total_count = data.get('totalCount', 0)
        if len(all_cards) >= total_count:
            break

        page += 1

    print(f"\n✓ Downloaded {len(all_cards)} Pokemon cards")
    return all_cards


def insert_pokemon_cards(cards: list, db_file: str = 'data/cards_database.db'):
    """Insert Pokemon cards into unified database"""

    from card_games_pokemon import PokemonGame

    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()

    pokemon_game = PokemonGame()
    inserted = 0

    print(f"Inserting {len(cards)} Pokemon cards into database...")

    for card in cards:
        try:
            parsed = pokemon_game.parse_card_data(card)

            cursor.execute('''
                INSERT OR REPLACE INTO cards (
                    game, name, set_code, set_name, collector_number,
                    rarity, price_usd, price_usd_foil, image_uri,
                    card_type, description, attributes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                'pokemon',
                parsed['name'],
                parsed['set_code'],
                parsed['set_name'],
                parsed['collector_number'],
                parsed['rarity'],
                parsed['price_usd'],
                parsed['price_usd_foil'],
                parsed['image_uri'],
                parsed['card_type'],
                parsed['description'],
                json.dumps(parsed['attributes'])
            ))

            inserted += 1

            if inserted % 100 == 0:
                print(f"  Inserted {inserted} cards...")
                conn.commit()

        except Exception as e:
            print(f"  Error inserting card {card.get('name')}: {e}")

    conn.commit()
    conn.close()

    print(f"✓ Inserted {inserted} Pokemon cards")


if __name__ == "__main__":
    # Download cards
    cards = download_pokemon_cards()

    # Insert into database
    insert_pokemon_cards(cards)

    print("\n✓ Pokemon database setup complete!")
```

#### Task 2.3: Update Scanner for Pokemon (2 hours)
**File:** `app.py` (MODIFIED)

Add game switching endpoint and use game manager:

```python
from game_manager import get_game_manager

# Initialize game manager
game_manager = get_game_manager()

@socketio.on('switch_game')
def handle_switch_game(data):
    """Handle game switching"""
    game_code = data.get('game')

    if game_manager.switch_game(game_code):
        # Update scanner's card identifier with new game prompt
        if scanner and scanner.card_identifier:
            current_game = game_manager.get_current_game()
            # Update prompt (need to modify CardIdentifier to accept custom prompt)
            scanner.card_identifier.custom_prompt = current_game.get_vision_prompt()

        emit('game_switched', {
            'game': game_manager.get_game_info(),
            'message': f'Switched to {game_code}'
        })

        log_to_client(f"Switched to {game_manager.current_game.name}", level="success")
    else:
        emit('error', {'message': f'Failed to switch to {game_code}'})

@socketio.on('get_game_info')
def handle_get_game_info():
    """Return current game information"""
    emit('game_info', game_manager.get_game_info())

@socketio.on('get_available_games')
def handle_get_available_games():
    """Return list of available games"""
    emit('available_games', game_manager.get_available_games())
```

#### Task 2.4: UI Updates for Game Selection (2 hours)
**File:** `templates/scanner.html` (MODIFIED)

Add game selector to UI:

```html
<!-- Add to settings panel -->
<div class="setting-group">
    <label for="game-selector">Card Game:</label>
    <select id="game-selector" class="game-selector">
        <option value="mtg">Magic: The Gathering</option>
        <option value="pokemon">Pokemon TCG</option>
        <option value="yugioh">Yu-Gi-Oh!</option>
        <option value="lorcana">Disney Lorcana</option>
    </select>
    <div class="game-info">
        <small id="game-status">Current: Magic: The Gathering</small>
    </div>
</div>

<script>
// Game selection handler
document.getElementById('game-selector').addEventListener('change', function(e) {
    const gameCode = e.target.value;

    // Confirm game switch
    if (confirm(`Switch to ${e.target.options[e.target.selectedIndex].text}?`)) {
        socket.emit('switch_game', {game: gameCode});
    } else {
        // Revert selection
        e.target.value = currentGame;
    }
});

// Handle game switched event
socket.on('game_switched', function(data) {
    currentGame = data.game.code;
    document.getElementById('game-status').textContent = `Current: ${data.game.name}`;

    // Update UI elements based on game
    updateGameSpecificUI(data.game);

    addLog(`Switched to ${data.game.name}`, 'success');
});

function updateGameSpecificUI(gameInfo) {
    // Update foil options
    const foilSelect = document.getElementById('foil-type');
    foilSelect.innerHTML = '';
    gameInfo.foil_types.forEach(type => {
        const option = document.createElement('option');
        option.value = type.toLowerCase();
        option.textContent = type;
        foilSelect.appendChild(option);
    });

    // Update rarity options if needed
    // Update any game-specific fields
}

// Load game info on connect
socket.on('connect', function() {
    socket.emit('get_game_info');
});
</script>
```

---

### PHASE 3: Yu-Gi-Oh! Integration (8 hours)
**Goal:** Implement Yu-Gi-Oh! card scanning

Follow similar pattern as Pokemon:

#### Task 3.1: Yu-Gi-Oh! Game Implementation (4 hours)
**File:** `card_games_yugioh.py` (NEW)

```python
"""
Yu-Gi-Oh! game implementation
"""

from typing import Dict, List, Optional
import requests
from card_games import CardGame


class YuGiOhGame(CardGame):
    """Yu-Gi-Oh! Trading Card Game implementation"""

    name = "Yu-Gi-Oh!"
    short_code = "yugioh"
    database_api = "https://db.ygoprodeck.com/api/v7/cardinfo.php"

    card_attributes = {
        'card_type': 'string',
        'monster_type': 'string',
        'attribute': 'string',
        'level': 'integer',
        'atk': 'integer',
        'def': 'integer',
        'scale': 'integer',
        'link_rating': 'integer',
        'archetype': 'string',
        'race': 'string'
    }

    rarity_values = [
        'Common', 'Rare', 'Super Rare', 'Ultra Rare',
        'Secret Rare', 'Ultimate Rare', 'Ghost Rare',
        'Starlight Rare', 'Prismatic Secret Rare'
    ]

    card_types = ['Monster', 'Spell', 'Trap']
    attributes = ['DARK', 'LIGHT', 'EARTH', 'WATER', 'FIRE', 'WIND', 'DIVINE']

    def get_vision_prompt(self) -> str:
        """Return Yu-Gi-Oh!-specific Vision AI prompt"""
        return """This is a Yu-Gi-Oh! trading card. Please identify:

1. The card name (at the top of the card, in large text)
2. The card number/code (bottom-right corner, format like "LOB-001" or "SDBE-EN001")

IMPORTANT INSTRUCTIONS FOR CARD NUMBER:
- Located at BOTTOM-RIGHT corner
- Format: Set code + dash + number (e.g., "LOB-001", "SDBE-EN001")
- May have edition text (1st Edition, Limited Edition) nearby - ignore this
- May have eye symbol (Unlimited) - ignore this
- Return the FULL code including set prefix

Return your answer in EXACTLY this format:
NAME: [card name]
NUMBER: [card code]

Examples:
NAME: Blue-Eyes White Dragon
NUMBER: LOB-001

NAME: Dark Magician
NUMBER: YGLD-ENB01

NAME: Exodia the Forbidden One
NUMBER: LOB-124

Your response:"""

    def search_by_name(self, card_name: str, collector_number: Optional[str] = None) -> Optional[Dict]:
        """Search YGOPRODeck API for card"""
        try:
            params = {'name': card_name}

            self.logger.info(f"Searching YGOPRODeck API: {card_name}")

            response = requests.get(
                self.database_api,
                params=params,
                timeout=10
            )

            response.raise_for_status()
            data = response.json()

            cards = data.get('data', [])

            if not cards:
                self.logger.warning(f"No Yu-Gi-Oh! cards found for: {card_name}")
                return None

            # If collector_number provided, try to match by set
            if collector_number and len(cards) > 0:
                # Match by card number in card_sets
                for card in cards:
                    if 'card_sets' in card:
                        for card_set in card['card_sets']:
                            if collector_number.upper() in card_set.get('set_code', '').upper():
                                return self.parse_card_data(card, card_set)

            # Return first match
            return self.parse_card_data(cards[0])

        except Exception as e:
            self.logger.error(f"YGOPRODeck API search error: {e}")
            return None

    def parse_card_data(self, api_response: Dict, specific_set: Optional[Dict] = None) -> Dict:
        """Parse YGOPRODeck API response into unified format"""

        # Extract set information
        set_code = None
        set_name = None
        collector_number = None
        rarity = None
        price_usd = None

        if specific_set:
            set_code = specific_set.get('set_code')
            set_name = specific_set.get('set_name')
            collector_number = specific_set.get('set_code')  # YGO uses set code as collector number
            rarity = specific_set.get('set_rarity')
            price_usd = float(specific_set.get('set_price', 0)) if specific_set.get('set_price') else None
        elif api_response.get('card_sets'):
            # Use first set if no specific set provided
            first_set = api_response['card_sets'][0]
            set_code = first_set.get('set_code')
            set_name = first_set.get('set_name')
            collector_number = first_set.get('set_code')
            rarity = first_set.get('set_rarity')
            price_usd = float(first_set.get('set_price', 0)) if first_set.get('set_price') else None

        # Build attributes JSON
        attributes = {
            'card_type': api_response.get('type'),
            'race': api_response.get('race'),
            'archetype': api_response.get('archetype')
        }

        # Add monster-specific attributes
        if 'Monster' in api_response.get('type', ''):
            attributes.update({
                'monster_type': api_response.get('race'),
                'attribute': api_response.get('attribute'),
                'level': api_response.get('level'),
                'atk': api_response.get('atk'),
                'def': api_response.get('def')
            })

            # Pendulum scale
            if api_response.get('scale'):
                attributes['scale'] = api_response.get('scale')

            # Link rating
            if api_response.get('linkval'):
                attributes['link_rating'] = api_response.get('linkval')

        # Build unified card data
        card_data = {
            'name': api_response.get('name'),
            'set_code': set_code,
            'set_name': set_name,
            'collector_number': collector_number,
            'rarity': rarity,
            'price_usd': price_usd,
            'price_usd_foil': None,  # YGO doesn't have foils in same way
            'image_uri': api_response.get('card_images', [{}])[0].get('image_url'),
            'card_type': api_response.get('type'),
            'description': api_response.get('desc'),
            'attributes': attributes
        }

        return card_data

    def get_foil_types(self) -> List[str]:
        """Yu-Gi-Oh! doesn't use traditional foils"""
        return ['Normal', '1st Edition', 'Limited Edition', 'Unlimited']
```

#### Task 3.2: Yu-Gi-Oh! Database Setup (2 hours)
**File:** `setup_yugioh_database.py` (NEW)

Similar to Pokemon setup, but YGOPRODeck API returns all cards in one request:

```python
def download_yugioh_cards():
    """Download all Yu-Gi-Oh! cards from YGOPRODeck API"""

    print("Downloading Yu-Gi-Oh! card database...")

    response = requests.get(
        'https://db.ygoprodeck.com/api/v7/cardinfo.php',
        timeout=60  # May take a while
    )

    response.raise_for_status()
    data = response.json()

    cards = data.get('data', [])
    print(f"✓ Downloaded {len(cards)} Yu-Gi-Oh! cards")

    return cards
```

#### Task 3.3-3.4: Integration & Testing (2 hours)
Follow same pattern as Pokemon integration.

---

### PHASE 4: Disney Lorcana Integration (8 hours)
**Goal:** Implement Lorcana card scanning

**Challenge:** Limited official API, may need to build local database or use unofficial API.

#### Option A: Use Unofficial API
- Lorcana API: https://lorcana-api.com/
- Similar implementation to Pokemon/Yu-Gi-Oh!

#### Option B: Build Local Database
- Scrape from https://dreamborn.ink/
- Create local SQLite database
- Update periodically

Follow similar implementation pattern as previous games.

---

### PHASE 5: Testing & Refinement (8 hours)

#### Task 5.1: Unit Tests (3 hours)
**File:** `test_multi_game.py` (NEW)

```python
"""
Unit tests for multi-game support
"""

import unittest
from card_games import get_game, AVAILABLE_GAMES
from game_manager import GameManager


class TestGameAbstraction(unittest.TestCase):

    def test_get_all_games(self):
        """Test all games can be instantiated"""
        for code in AVAILABLE_GAMES:
            game = get_game(code)
            self.assertIsNotNone(game)
            self.assertTrue(game.name)
            self.assertTrue(game.short_code)

    def test_mtg_game(self):
        """Test MTG game implementation"""
        mtg = get_game('mtg')
        self.assertEqual(mtg.short_code, 'mtg')
        self.assertIn('Magic', mtg.name)

        prompt = mtg.get_vision_prompt()
        self.assertIn('Magic: The Gathering', prompt)

    def test_pokemon_game(self):
        """Test Pokemon game implementation"""
        pokemon = get_game('pokemon')
        self.assertEqual(pokemon.short_code, 'pokemon')
        self.assertIn('Pokemon', pokemon.name)

        prompt = pokemon.get_vision_prompt()
        self.assertIn('Pokemon', prompt)
        self.assertIn('bottom-left', prompt.lower())


class TestGameManager(unittest.TestCase):

    def setUp(self):
        self.manager = GameManager()

    def test_default_game(self):
        """Test default game is MTG"""
        self.assertEqual(self.manager.game_code, 'mtg')

    def test_switch_game(self):
        """Test game switching"""
        result = self.manager.switch_game('pokemon')
        self.assertTrue(result)
        self.assertEqual(self.manager.game_code, 'pokemon')

        game = self.manager.get_current_game()
        self.assertEqual(game.short_code, 'pokemon')

    def test_game_info(self):
        """Test game info retrieval"""
        info = self.manager.get_game_info()
        self.assertIn('code', info)
        self.assertIn('name', info)
        self.assertIn('rarity_values', info)


if __name__ == '__main__':
    unittest.main()
```

#### Task 5.2: Integration Testing (3 hours)

Test complete workflow for each game:

```python
"""
Integration tests - Full scanning workflow
"""

def test_pokemon_scan_workflow():
    """Test Pokemon card: Pikachu"""
    # 1. Switch to Pokemon
    game_manager.switch_game('pokemon')

    # 2. Load test image
    test_image = cv2.imread('test_images/pikachu_025.jpg')

    # 3. Run Vision AI
    scanner.identify_card_from_image(test_image)

    # 4. Verify result
    assert result['name'] == 'Pikachu'
    assert result['collector_number'] == '025'

    # 5. Search database
    db_card = searcher.search_by_name('Pikachu', '025')

    # 6. Verify database match
    assert db_card is not None
    assert db_card['set_name'] is not None

def test_yugioh_scan_workflow():
    """Test Yu-Gi-Oh! card: Blue-Eyes White Dragon"""
    # Similar test for YGO
    pass

def test_lorcana_scan_workflow():
    """Test Lorcana card: Mickey Mouse"""
    # Similar test for Lorcana
    pass
```

#### Task 5.3: UI/UX Testing (2 hours)

Manual testing checklist:
- [ ] Game selector works correctly
- [ ] Switching games updates UI appropriately
- [ ] Card scanning works for each game
- [ ] Database search works for each game
- [ ] Inventory management works for each game
- [ ] Pricing displays correctly
- [ ] Images load properly
- [ ] Foil types match game
- [ ] Rarity values match game

---

### PHASE 6: Documentation & Deployment (4 hours)

#### Task 6.1: Update Documentation (2 hours)

Update files:
- `README.md` - Add multi-game support section
- `CLAUDE.md` - Update architecture documentation
- Create `MULTI_GAME_GUIDE.md` - User guide for multiple games

#### Task 6.2: Create Setup Scripts (2 hours)

**File:** `setup_all_games.py` (NEW)

```python
"""
Setup script to download all game databases
"""

import argparse
from setup_database import setup_mtg_database
from setup_pokemon_database import download_pokemon_cards, insert_pokemon_cards
from setup_yugioh_database import download_yugioh_cards, insert_yugioh_cards
from setup_lorcana_database import setup_lorcana_database


def setup_all_databases():
    """Download and setup all game databases"""

    print("=== Multi-Game Database Setup ===\n")

    # MTG
    print("1. Setting up Magic: The Gathering database...")
    setup_mtg_database()

    # Pokemon
    print("\n2. Setting up Pokemon TCG database...")
    pokemon_cards = download_pokemon_cards()
    insert_pokemon_cards(pokemon_cards)

    # Yu-Gi-Oh!
    print("\n3. Setting up Yu-Gi-Oh! database...")
    yugioh_cards = download_yugioh_cards()
    insert_yugioh_cards(yugioh_cards)

    # Lorcana
    print("\n4. Setting up Disney Lorcana database...")
    setup_lorcana_database()

    print("\n✓ All game databases set up successfully!")
    print("\nTotal cards in database:")

    # Show counts
    import sqlite3
    conn = sqlite3.connect('data/cards_database.db')
    cursor = conn.cursor()
    cursor.execute("SELECT game, COUNT(*) FROM cards GROUP BY game")
    for row in cursor.fetchall():
        print(f"  {row[0]}: {row[1]:,} cards")
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Setup multi-game card databases')
    parser.add_argument('--games', nargs='+',
                       choices=['mtg', 'pokemon', 'yugioh', 'lorcana', 'all'],
                       default=['all'],
                       help='Games to setup (default: all)')

    args = parser.parse_args()

    if 'all' in args.games:
        setup_all_databases()
    else:
        # Setup individual games
        pass
```

---

## File Changes Summary

### New Files to Create (14 files)
1. `card_games.py` - Game abstraction base class
2. `card_games_pokemon.py` - Pokemon implementation
3. `card_games_yugioh.py` - Yu-Gi-Oh! implementation
4. `card_games_lorcana.py` - Lorcana implementation
5. `game_manager.py` - Game switching manager
6. `database_migration.py` - Schema migration script
7. `setup_pokemon_database.py` - Pokemon DB setup
8. `setup_yugioh_database.py` - Yu-Gi-Oh! DB setup
9. `setup_lorcana_database.py` - Lorcana DB setup
10. `setup_all_games.py` - Unified setup script
11. `test_multi_game.py` - Unit tests
12. `test_integration_multi_game.py` - Integration tests
13. `MULTI_GAME_GUIDE.md` - User documentation
14. `data/game_config.json` - Game preferences (auto-created)

### Files to Modify (6 files)
1. `app.py` - Add game switching endpoints
2. `scanner.py` - Support custom prompts
3. `card_identifier.py` - Accept custom prompts
4. `database.py` - Support game column
5. `config.py` - Add game settings
6. `templates/scanner.html` - Add game selector UI
7. `CLAUDE.md` - Update documentation
8. `README.md` - Add multi-game info

### Database Changes
- Migration script to add `game` column to `cards` and `inventory` tables
- JSON `attributes` field for game-specific data
- New indexes on `game` column

---

## Testing Strategy

### Unit Testing
- Test each game class independently
- Test game manager switching
- Test database queries per game
- Test Vision AI prompt generation

### Integration Testing
- End-to-end workflow per game
- Database migration testing
- API integration testing (with mock responses)
- UI interaction testing

### Manual Testing
Test with real cards:
- [ ] MTG: 5 different cards (various sets/rarities)
- [ ] Pokemon: 5 different cards (different types)
- [ ] Yu-Gi-Oh!: 5 different cards (Monster/Spell/Trap mix)
- [ ] Lorcana: 5 different cards (if available)

Test scenarios:
- [ ] Cold start (no game selected)
- [ ] Switch between games multiple times
- [ ] Scan cards after switching
- [ ] Add to inventory (each game)
- [ ] Export inventory (each game)
- [ ] Database updates
- [ ] Price updates

---

## Migration Path

### For Existing MTG Users

**Option 1: Automatic Migration (Recommended)**
```bash
# Run migration script
python database_migration.py

# Verify migration
python -c "from game_manager import get_game_manager; print(get_game_manager().get_game_info())"

# Test scanning
python app.py
```

**Option 2: Manual Migration**
1. Backup existing database: `cp data/cards_database.db data/cards_database_backup.db`
2. Run migration: `python database_migration.py`
3. Verify: Check database has `game` column
4. Test: Scan a few MTG cards to ensure compatibility

**Rollback Plan:**
```bash
# If migration fails, rollback
python database_migration.py rollback

# Or manual rollback
mv data/cards_database_backup.db data/cards_database.db
```

### Setup for New Users

```bash
# Install dependencies
pip install -r requirements.txt

# Setup all game databases (choose games)
python setup_all_games.py --games mtg pokemon

# Run scanner
python app.py
```

---

## Timeline & Estimates

### Development Phases

| Phase | Description | Hours | Priority |
|-------|-------------|-------|----------|
| Phase 0 | Preparation & Planning | 4 | HIGH |
| Phase 1 | Core Architecture | 12 | HIGH |
| Phase 2 | Pokemon Integration | 10 | MEDIUM |
| Phase 3 | Yu-Gi-Oh! Integration | 8 | MEDIUM |
| Phase 4 | Lorcana Integration | 8 | LOW |
| Phase 5 | Testing & Refinement | 8 | HIGH |
| Phase 6 | Documentation & Deployment | 4 | MEDIUM |
| **Total** | | **54 hours** | |

### Phased Rollout (Recommended)

**Version 2.0: Core Multi-Game (Phases 0-1)** - 16 hours
- Game abstraction layer
- Database migration
- UI for game selection
- MTG still works (backward compatible)

**Version 2.1: Pokemon Support (Phase 2)** - 10 hours
- Full Pokemon integration
- Pokemon database setup
- Testing with Pokemon cards

**Version 2.2: Yu-Gi-Oh! Support (Phase 3)** - 8 hours
- Yu-Gi-Oh! integration
- YGO database setup

**Version 2.3: Lorcana Support (Phase 4)** - 8 hours
- Lorcana integration
- Lorcana database setup

**Version 2.4: Polish & Optimization (Phases 5-6)** - 12 hours
- Comprehensive testing
- Documentation updates
- Performance optimization

### Minimum Viable Product (MVP)

**Core + Pokemon Only**: ~26 hours
- Phases 0-1: Core architecture (16 hours)
- Phase 2: Pokemon integration (10 hours)
- Skip Yu-Gi-Oh! and Lorcana for MVP
- Basic testing included

---

## Risk Assessment & Mitigation

### Technical Risks

**Risk 1: API Availability**
- Pokemon API: Low risk (official, well-maintained)
- Yu-Gi-Oh! API: Medium risk (unofficial but stable)
- Lorcana API: High risk (unofficial, limited)

*Mitigation:*
- Implement local caching
- Build fallback to local database
- Download full databases periodically

**Risk 2: Database Migration**
- Existing MTG data could be corrupted

*Mitigation:*
- Automatic backup before migration
- Rollback script
- Extensive testing on copy of production DB

**Risk 3: Vision AI Accuracy**
- Different card layouts may confuse AI
- Collector numbers in different positions

*Mitigation:*
- Game-specific prompts with detailed instructions
- Test with variety of card images
- Allow manual correction of AI results

**Risk 4: Performance**
- Large unified database may be slow

*Mitigation:*
- Proper indexing on `game` column
- Consider separate tables if performance issues
- Optimize queries with game filter

### Timeline Risks

**Risk: Scope Creep**
- Adding too many features during implementation

*Mitigation:*
- Strict adherence to phase plan
- Defer nice-to-have features to future versions
- Focus on MVP first

**Risk: API Integration Complexity**
- Each API has different structure/requirements

*Mitigation:*
- Abstract API differences in game classes
- Prototype API integration before full implementation
- Have fallback options for each API

---

## Success Criteria

### Phase Completion Criteria

**Phase 1: Core Architecture**
- [x] Game abstraction layer created
- [x] Game manager functional
- [x] Database migration successful
- [x] MTG still works (backward compatible)
- [x] Unit tests pass

**Phase 2: Pokemon Integration**
- [x] Pokemon API integrated
- [x] Pokemon database populated
- [x] Can scan and identify Pokemon cards
- [x] Pokemon inventory management works
- [x] Integration tests pass

**Phase 3: Yu-Gi-Oh! Integration**
- [x] YGO API integrated
- [x] YGO database populated
- [x] Can scan and identify YGO cards
- [x] Integration tests pass

**Phase 4: Lorcana Integration**
- [x] Lorcana data source integrated
- [x] Can scan and identify Lorcana cards
- [x] Integration tests pass

**Phase 5: Testing**
- [x] All unit tests pass
- [x] All integration tests pass
- [x] Manual testing complete
- [x] No regressions in MTG functionality

**Phase 6: Documentation**
- [x] User guide created
- [x] Developer documentation updated
- [x] README updated
- [x] Deployment instructions clear

### Overall Success Metrics

**Functional:**
- Can scan cards from all supported games
- Vision AI identifies cards with >85% accuracy per game
- Database searches work correctly per game
- Inventory management works per game
- No data mixing between games

**Performance:**
- Card identification time <15 seconds (same as current)
- Database queries <500ms per game
- Game switching <2 seconds

**Usability:**
- Users can switch games intuitively
- No confusing UI elements
- Clear indication of current game
- Error messages are helpful

---

## Future Enhancements (Post-Implementation)

### Version 3.0 Ideas

1. **Multi-Game Inventory Dashboard**
   - View all games' inventories in one place
   - Cross-game statistics (total cards, total value)
   - Combined export functionality

2. **Price Tracking**
   - Historical price data per game
   - Price alerts for valuable cards
   - Market trend analysis

3. **Collection Goals**
   - Set completion tracking
   - Wishlist functionality
   - Trade recommendations

4. **Advanced Search**
   - Search across all games
   - Complex filtering (price range, rarity, etc.)
   - Saved searches

5. **Mobile App**
   - React Native or Flutter app
   - Same backend, mobile-optimized UI
   - Offline mode

6. **Additional Games**
   - Flesh and Blood
   - Weiss Schwarz
   - Cardfight!! Vanguard
   - One Piece Card Game

7. **Social Features**
   - Share collections
   - Trade matching
   - Collection comparisons

8. **OCR Fallback**
   - Use OCR if Vision AI fails
   - Hybrid AI + OCR approach
   - Better accuracy for damaged cards

---

## Appendix

### A. API Documentation Links

- **Pokemon TCG API**: https://docs.pokemontcg.io/
- **YGOPRODeck API**: https://ygoprodeck.com/api-guide/
- **Scryfall API** (MTG): https://scryfall.com/docs/api
- **Lorcana API**: https://lorcana-api.com/docs (unofficial)

### B. Example API Responses

See separate file: `docs/API_RESPONSE_EXAMPLES.md`

### C. Database Schema Reference

See separate file: `docs/DATABASE_SCHEMA.md`

### D. Vision AI Prompt Templates

See separate file: `docs/VISION_PROMPTS.md`

---

## Notes

- This plan is a living document - update as implementation progresses
- Adjust time estimates based on actual development speed
- Defer features that are out of scope for MVP
- Prioritize code quality and testing over speed
- Keep backward compatibility with existing MTG functionality
- Document all breaking changes

---

**Next Steps:**
1. Review and approve this plan
2. Set up development environment
3. Create feature branch
4. Begin Phase 0 tasks
5. Regular check-ins after each phase

---

**End of Implementation Plan**
