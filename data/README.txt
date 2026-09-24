Runtime data (not in git):
- cards_database.db  - Scryfall card data AND your inventory (table "inventory")
                       created by setup_database.py / scripts/deploy.sh
- settings.json      - choices made in the web interface (AI provider/model, add automatically)
- logs/              - app.log, ai.log, scanner.log, database.log, scanned_cards.log
- *_export_*.csv     - inventory exports
Back it up with scripts/backup.sh.
