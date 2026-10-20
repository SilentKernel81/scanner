#!/usr/bin/env python3
"""
The 15 Deadpool Secret Lair captures (phone #1037-1051: five cards, each regular / foil / confetti-foil; the printed line names IFIYW-6..10 for all three of a card) through
Identification._read_with_ocr as it runs live in reading.mode 'evidence' - the real OCR reader, the real index, the real card database - with no vision AI:
what would be added, and as which printing? Run with and without the picture alone (accept_image_alone).

    MTG_SCANNER_HOME=<repo> venv/bin/python -I scripts/experiments/image_match/verify_deadpool.py
"""
import json
import sys
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(1, str(HERE))

from card_scanner import games
from card_scanner.config import Config
from card_scanner.core import card_ocr, foil_marker, identification
from card_scanner.core.database import CardDatabase
from card_scanner.core.printing_check import PrintingCheck
from common import HOME, read


class Settings:
    def __init__(self, mode):
        self.mode = mode

    def get(self, key, default=None):
        return self.mode if key == 'reading_mode' else (True if key == 'game' and False else default)


database = CardDatabase()
games.init(database, Settings('evidence'))
game = games.active()
ocr = card_ocr.CardOcr()
ocr.warm_up()
check = PrintingCheck(database, Config.DATA_DIR / 'image_index', Path('/nonexistent/never-written.jsonl'), 'off', 'off')
ident = identification.Identification.__new__(identification.Identification)
ident.log_callback = None
ident.ocr_enabled = True
ident.card_identifier = None                 # no vision AI: what the picture and the OCR alone leave
ident.foil_marker = foil_marker.load(True)
ident.card_ocr = ocr
ident.evidence = check
ident._ai_foil_check = lambda foil_image: False
files = sorted(f for f in (HOME / 'truth' / 'rescan_cards').iterdir() if f.name.startswith('phone-0240eb84_10') and 1037 <= int(f.name.split('_')[1]) <= 1051)
for mode, alone in (('evidence, picture alone OFF (the default)', False), ('evidence, picture alone ON', True)):
    ident.settings = Settings('evidence')
    print(f'\n== {mode}')
    with mock.patch.object(Config, 'READING_ACCEPT_IMAGE_ALONE', alone):
        added, review = 0, 0
        for f in files:
            image = read(f)
            info, task = ident._read_with_ocr(image, image)
            number = info.get('collector_number') if info else ''
            card = game.card_by_printing(info['printing_id']) if info and info.get('printing_id') else None
            got = f"{card['name']} [{card['set_code']} {card['number']}]" if card else (f"{info['name']} (OCR, number kept {number})" if number else f"{(info or {}).get('name')} - REVIEW")
            added += bool(number)
            review += not number
            print(f"  {f.name[-16:]}: {got}   foil={info.get('foil') if info else None}")
        print(f'  -> {added} would be added, {review} would go to review')
ocr.stop()
