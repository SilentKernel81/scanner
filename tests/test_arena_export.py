#!/usr/bin/env python3
"""
A deck written for MTG Arena's Import button (games/mtg_decks.py: format_arena).

Run:  venv/bin/python tests/test_arena_export.py        (or with the others: -m unittest discover tests)

What can be tested here is what the text is made of: which cards are taken to exist on Arena,
how a two-part name is written, which boards are written under which heading. Whether Arena
accepts the text can only be seen in Arena, and was not.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

from card_scanner.games.mtg_decks import arena_name, format_arena, on_arena  # noqa: E402


def card(type_line='Creature', **legalities):
    return {'type_line': type_line, 'legalities': legalities}


ARENA = dict(timeless='legal', historic='legal')
PAPER = dict(commander='legal', legacy='legal', vintage='legal')


class OnArena(unittest.TestCase):

    def test_a_say_in_an_arena_format_means_the_card_is_there(self):
        self.assertTrue(on_arena(card(**ARENA)))
        self.assertTrue(on_arena(card(timeless='banned')))       # banned is still a card that exists there
        self.assertTrue(on_arena(card(timeless='restricted')))
        self.assertTrue(on_arena(card(brawl='legal')))

    def test_paper_only_cards_and_unknown_cards_are_not(self):
        self.assertFalse(on_arena(card(**PAPER)))
        self.assertFalse(on_arena(card()))
        self.assertFalse(on_arena(None))                          # not in the card data: it cannot be said
        self.assertFalse(on_arena({'type_line': 'Artifact', 'legalities': None}))


class Names(unittest.TestCase):

    def test_one_name_is_written_as_it_is(self):
        self.assertEqual(arena_name('Sol Ring', 'Artifact'), 'Sol Ring')

    def test_split_cards_and_rooms_keep_both_names(self):
        self.assertEqual(arena_name('Fire // Ice', 'Instant // Instant'), 'Fire // Ice')
        self.assertEqual(arena_name('Discovery // Dispersal', 'Sorcery // Instant'), 'Discovery // Dispersal')
        self.assertEqual(arena_name('Dazzling Theater // Prop Room', 'Enchantment — Room // Enchantment — Room'),
                         'Dazzling Theater // Prop Room')

    def test_other_two_part_cards_go_by_their_front(self):
        self.assertEqual(arena_name('Brazen Borrower // Petty Theft', 'Creature — Faerie Rogue // Instant — Adventure'), 'Brazen Borrower')
        self.assertEqual(arena_name('Fable of the Mirror-Breaker // Reflection of Kiki-Jiki',
                                    'Enchantment — Saga // Enchantment Creature — Goblin Shaman'), 'Fable of the Mirror-Breaker')
        self.assertEqual(arena_name('Valakut Awakening // Valakut Stoneforge', 'Instant // Land'), 'Valakut Awakening')
        # an instant with an adventure half is not a split card
        self.assertEqual(arena_name('A // B', 'Instant // Sorcery — Adventure'), 'A')


class TheText(unittest.TestCase):

    CARDS = {'Jace': card('Legendary Planeswalker', **ARENA), 'Island': card('Basic Land — Island', **ARENA),
             'Opt': card('Instant', **ARENA), 'Sol Ring': card('Artifact', **PAPER), 'Negate': card('Instant', **ARENA),
             'Fire // Ice': card('Instant // Instant', **ARENA)}

    def test_a_commander_deck(self):
        entries = [{'name': 'Jace', 'board': 'commander', 'quantity': 1}, {'name': 'Opt', 'board': 'main', 'quantity': 1},
                   {'name': 'Island', 'board': 'main', 'quantity': 30}, {'name': 'Sol Ring', 'board': 'main', 'quantity': 1},
                   {'name': 'Negate', 'board': 'side', 'quantity': 1}]   # considered, not in the deck
        text, left_out = format_arena(entries, 'commander', self.CARDS)
        self.assertEqual(text, 'Commander\n1 Jace\n\nDeck\n30 Island\n1 Opt\n')
        self.assertEqual(left_out, ['Sol Ring'])

    def test_a_sixty_card_deck_has_its_sideboard(self):
        entries = [{'name': 'Opt', 'board': 'main', 'quantity': 4}, {'name': 'Fire // Ice', 'board': 'main', 'quantity': 2},
                   {'name': 'Negate', 'board': 'side', 'quantity': 3}, {'name': 'Unknown Card', 'board': 'main', 'quantity': 1}]
        text, left_out = format_arena(entries, 'standard', {**self.CARDS, 'Unknown Card': None})
        self.assertEqual(text, 'Deck\n2 Fire // Ice\n4 Opt\n\nSideboard\n3 Negate\n')
        self.assertEqual(left_out, ['Unknown Card'])

    def test_a_deck_with_nothing_on_arena_gives_no_text(self):
        text, left_out = format_arena([{'name': 'Sol Ring', 'board': 'main', 'quantity': 1}], 'commander', self.CARDS)
        self.assertEqual((text, left_out), ('', ['Sol Ring']))


if __name__ == '__main__':
    unittest.main()
