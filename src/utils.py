"""
Shared utility functions for the card scanner application
"""

import unicodedata


def normalize_text(text):
    """Remove accents and diacritics from text for comparison"""
    if not text:
        return text
    # Normalize to NFD (decomposed form) and filter out combining characters
    normalized = unicodedata.normalize('NFD', text)
    return ''.join(c for c in normalized if unicodedata.category(c) != 'Mn')
