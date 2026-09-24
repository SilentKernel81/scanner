# ============================================================================
# FILE: card_search.py
# Card search and identification logic
# ============================================================================
from database import CardDatabase, collector_number_variants, names_match


class CardSearcher:
    """Handles card searching and identification"""
    
    def __init__(self, database: CardDatabase, log_callback=None):
        self.db = database
        self.log_callback = log_callback
    
    def log(self, message, level="info"):
        """Send log message"""
        if self.log_callback:
            self.log_callback(message, level)
    
    def search_by_name(self, card_name, collector_number=None, fuzzy=True, ai_model=None, set_code=None):
        """
        Search for a card by name and optionally collector number

        Args:
            card_name: The card name
            collector_number: Optional collector number for exact match
            set_code: Optional set code - with the collector number it identifies the printing
            fuzzy: Whether to use fuzzy matching if exact match fails
            ai_model: Optional AI model info (e.g., "qwen3-vl:8b") for logging

        Returns:
            Card dict if found, None otherwise
        """
        # Build AI model suffix for log messages
        ai_suffix = f" (AI: {ai_model})" if ai_model else ""

        if collector_number:
            set_info = f" [{set_code}]" if set_code else ""
            self.log(f"Searching database for: '{card_name}' #{collector_number}{set_info}{ai_suffix}")
            card_info = self.db.search_card_exact(card_name, collector_number, set_code)

            # Check if we got an exact match or fallback
            if card_info and card_info.get('number') != collector_number.lstrip('0'):
                # We got a different number - log it
                variants = collector_number_variants(collector_number)
                if variants and card_info.get('number') not in variants:
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
    
    def find_printings(self, card_name, collector_number=None, treatment=None, set_code=None):
        """
        Find the printings of a card matching an optional collector number, treatment and set code

        Returns:
            tuple: (resolved_name, printings) - resolved_name is None if the card
            name matched nothing at all
        """
        filter_info = f" [{treatment}]" if treatment else ""
        number_info = f" #{collector_number}" if collector_number else ""
        set_info = f" {set_code}" if set_code else ""
        self.log(f"Searching printings for: '{card_name}'{set_info}{number_info}{filter_info}")

        # Set code + collector number identify one printing exactly
        if set_code and collector_number:
            card = self.db.get_card_by_set_number(set_code, collector_number)
            if card and names_match(card_name, {'name': card['name'], 'flavor_name': card['flavor_name']}):
                self.log(f"Found {card['name']} ({card['set']} #{card['number']})")
                return card['name'], [card]

        resolved_name, printings = self.db.find_printings(card_name, treatment=treatment)

        if set_code and printings:
            in_set = [card for card in printings if card['set_code'] == set_code.lower()]
            if in_set:
                printings = in_set
            else:
                self.log(f"No printing in set {set_code.upper()} - showing all {len(printings)} printings", level="warning")

        if collector_number and printings:
            variants = collector_number_variants(collector_number)
            matching = [card for card in printings if card['number'] in variants]
            if matching:
                printings = matching
            else:
                self.log(f"No printing numbered #{collector_number} - showing all {len(printings)} printings", level="warning")

        if resolved_name and printings:
            self.log(f"Found {len(printings)} printing(s) of {resolved_name}{filter_info}")
        elif resolved_name:
            self.log(f"No{filter_info} printings of {resolved_name}", level="warning")
        return resolved_name, printings

    def find_similar_cards(self, card_name, limit=5):
        """Find cards with similar names"""
        similar = self.db.search_cards_by_partial_name(card_name, limit=limit)
        
        if similar:
            self.log(f"Found {len(similar)} similar cards")
        
        return similar
