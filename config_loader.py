#!/usr/bin/env python3
"""
Configuration Loader
Loads configuration from YAML file with environment variable overrides
"""

import os
import yaml
import re
from pathlib import Path


class ConfigLoader:
    """Load and manage configuration from YAML file"""

    def __init__(self, config_file='config.yaml'):
        self.config_file = Path(__file__).parent / config_file
        self.config = self._load_config()

    def _load_config(self):
        """Load configuration from YAML file"""
        if not self.config_file.exists():
            raise FileNotFoundError(f"Configuration file not found: {self.config_file}")

        with open(self.config_file, 'r') as f:
            config = yaml.safe_load(f)

        # Process environment variable substitutions
        config = self._substitute_env_vars(config)

        return config

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
