# ============================================================================
# FILE: card_search.py
# Card search and identification logic
# ============================================================================
from database import CardDatabase


class CardSearcher:
    """Handles card searching and identification"""
    
    def __init__(self, database: CardDatabase, log_callback=None):
        self.db = database
        self.log_callback = log_callback
    
    def log(self, message, level="info"):
        """Send log message"""
        if self.log_callback:
            self.log_callback(message, level)
    
    def search_by_name(self, card_name, collector_number=None, fuzzy=True, ai_model=None):
        """
        Search for a card by name and optionally collector number

        Args:
            card_name: The card name
            collector_number: Optional collector number for exact match
            fuzzy: Whether to use fuzzy matching if exact match fails
            ai_model: Optional AI model info (e.g., "qwen3-vl:8b") for logging

        Returns:
            Card dict if found, None otherwise
        """
        # Build AI model suffix for log messages
        ai_suffix = f" (AI: {ai_model})" if ai_model else ""

        if collector_number:
            self.log(f"Searching database for: '{card_name}' #{collector_number}{ai_suffix}")
            card_info = self.db.search_card_exact(card_name, collector_number)

            # Check if we got an exact match or fallback
            if card_info and card_info.get('number') != collector_number.lstrip('0'):
                # We got a different number - log it
                import re
                num_match = re.search(r'(\d+)', collector_number)
                if num_match:
                    wanted_num = str(int(num_match.group(1)))
                    if card_info.get('number') != wanted_num:
                        self.log(f"⚠ Exact match not found for #{collector_number}, using fallback{ai_suffix}", level="warning")
        else:
            self.log(f"Searching database for: '{card_name}'{ai_suffix}")
            card_info = self.db.search_card(card_name, fuzzy=fuzzy)

        if card_info:
            set_info = f" ({card_info['set']} #{card_info['number']})" if card_info.get('number') else ""
            self.log(f"Card found: {card_info['name']}{set_info} - ${card_info['price']:.2f}")
            return card_info
        else:
            self.log(f"Card not found: {card_name}{ai_suffix}", level="warning")
            return None
    
    def find_similar_cards(self, card_name, limit=5):
        """Find cards with similar names"""
        similar = self.db.search_cards_by_partial_name(card_name, limit=limit)
        
        if similar:
            self.log(f"Found {len(similar)} similar cards")
        
        return similar
