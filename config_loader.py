#!/usr/bin/env python3
"""
Configuration Loader
Loads configuration from YAML file with environment variable overrides
"""

import os
import yaml
import re

from paths import BASE_DIR, USER_DIR


class ConfigLoader:
    """Load and manage configuration from YAML file"""

    def __init__(self, config_file='config.yaml'):
        self.config_file = BASE_DIR / config_file
        # A packaged program's own config.yaml is read-only: the user's copy in their folder
        # only needs the settings they change
        self.user_config_file = USER_DIR / config_file
        self.config = self._load_config()

    def _load_config(self):
        """Load configuration from YAML file"""
        if not self.config_file.exists():
            raise FileNotFoundError(f"Configuration file not found: {self.config_file}")

        with open(self.config_file, 'r') as f:
            config = yaml.safe_load(f) or {}

        if self.user_config_file != self.config_file and self.user_config_file.exists():
            with open(self.user_config_file, 'r') as f:
                config = self._merge(config, yaml.safe_load(f) or {})

        # Process environment variable substitutions
        config = self._substitute_env_vars(config)

        return config

    def _merge(self, defaults, changes):
        """The defaults with the user's settings on top (sections are merged key by key)"""
        merged = dict(defaults)
        for key, value in changes.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = self._merge(merged[key], value)
            else:
                merged[key] = value
        return merged

    def _substitute_env_vars(self, obj):
        """Recursively substitute ${VAR_NAME} with environment variables"""
        if isinstance(obj, dict):
            return {k: self._substitute_env_vars(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._substitute_env_vars(item) for item in obj]
        elif isinstance(obj, str):
            # Match ${VAR_NAME} pattern
            pattern = r'\$\{([^}]+)\}'
            match = re.match(pattern, obj)
            if match:
                var_name = match.group(1)
                return os.getenv(var_name, '')  # Return empty string if not set
            return obj
        else:
            return obj

    def get(self, *keys, default=None):
        """Get nested configuration value"""
        value = self.config
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
                if value is None:
                    return default
            else:
                return default
        return value


# Create global config instance
_config_loader = None

def get_config_loader():
    """Get or create config loader singleton"""
    global _config_loader
    if _config_loader is None:
        _config_loader = ConfigLoader()
    return _config_loader
