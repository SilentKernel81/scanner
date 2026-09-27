# Makes export_reference.json: games/mtg.py write_csv output for the rows in ExportParityTest
# (run from the repository root with the Python venv)
import io, json, sys
sys.path.insert(0, '.')
from games.mtg import write_csv
rows = [
    dict(name='Thorin, King of Durin\'s Folk', set_name='The Hobbit Eternal', number='3', rarity='rare',
         type_line='Legendary Creature — Dwarf Noble', mana_cost='{3}{R}{W}', colors='R, W', color_identity='Multicolor',
         price=6.07, quantity=1, condition='Near Mint', finish='regular', timestamp='2026-09-25 21:24:47'),
    dict(name='The "Quoted" Card', set_name='Test', number='12a', rarity='common', type_line='Instant',
         mana_cost='{U}', colors='U', color_identity='Blue', price=0.5, quantity=3, condition='Lightly Played',
         finish='foil', timestamp='2026-09-26 10:00:00'),
    dict(name='Smaug', set_name='The Hobbit', number='109', rarity='common', type_line='Legendary Creature — Dragon',
         mana_cost='{5}{R}{R}', colors='R', color_identity='Red', price=0.0, quantity=2, condition='Near Mint',
         finish='surge', timestamp='2026-09-26 11:00:00'),
]
out = io.StringIO(); write_csv(rows, out)
json.dump({'rows': rows, 'csv': out.getvalue()}, open(sys.argv[1], 'w'), ensure_ascii=False, indent=1)
