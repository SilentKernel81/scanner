# ============================================================================
# FILE: settings.py
# User preferences and settings persistence
# ============================================================================
import json
from pathlib import Path
from config import Config


class Settings:
    """Manage user preferences and settings"""

    SETTINGS_FILE = Config.DATA_DIR / 'settings.json'

    DEFAULT_SETTINGS = {
        'ai_provider': 'gemini',
        'ai_model': None,  # None means use provider's default
        'auto_capture_enabled': False,  # Controlled via UI button, not config
        'detection_enabled': True,
    }

    def __init__(self):
        self.settings = self._load_settings()

    def _load_settings(self):
        """Load settings from JSON file"""
        try:
            if self.SETTINGS_FILE.exists():
                with open(self.SETTINGS_FILE, 'r') as f:
                    loaded = json.load(f)
                    # Merge with defaults (in case new settings added)
                    settings = self.DEFAULT_SETTINGS.copy()
                    settings.update(loaded)
                    return settings
            else:
                # Create settings file with defaults
                self._save_settings(self.DEFAULT_SETTINGS)
                return self.DEFAULT_SETTINGS.copy()
        except Exception as e:
            print(f"Error loading settings: {e}")
            return self.DEFAULT_SETTINGS.copy()

    def _save_settings(self, settings):
        """Save settings to JSON file"""
        try:
            # Ensure data directory exists
            Config.DATA_DIR.mkdir(exist_ok=True)

            with open(self.SETTINGS_FILE, 'w') as f:
                json.dump(settings, f, indent=4)
        except Exception as e:
            print(f"Error saving settings: {e}")

    def get(self, key, default=None):
        """Get a setting value"""
        return self.settings.get(key, default)

    def set(self, key, value):
        """Set a setting value and save"""
        self.settings[key] = value
        self._save_settings(self.settings)

    def get_ai_provider(self):
        """Get saved AI provider"""
        return self.settings.get('ai_provider', 'gemini')

    def get_ai_model(self):
        """Get saved AI model (None means use provider's default)"""
        return self.settings.get('ai_model')

    def set_ai_provider(self, provider, model=None):
        """Save AI provider and model preference"""
        self.settings['ai_provider'] = provider
        self.settings['ai_model'] = model
        self._save_settings(self.settings)

    def get_all(self):
        """Get all settings"""
        return self.settings.copy()
