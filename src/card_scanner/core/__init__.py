# What the server is made of, without the web app and without a camera.
#
#   reading a card     identification, card_ocr, card_identifier, prompts, api_keys
#   card data          database, card_search, setup_database
#   the collection     inventory, decks, trades, backups, storage, pending, review, cleanup
#   stations           stations (who they are, their settings), remote_scanner (the server's
#                      stand-in for a station's camera)
#   other sites        recommendations
#
# Nothing here imports Flask's side (server/) or the camera's (station/): the web app uses
# these modules, never the other way round (tests/test_layout.py). This file must stay free
# of imports.
