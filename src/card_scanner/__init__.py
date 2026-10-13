# The card scanner's code: one package, sorted by which program a module belongs to.
#
#   paths, config, config_loader, settings    what both programs use (this folder)
#   station/    a camera station: camera, detection, auto-capture, its setup page
#   core/       reading cards, card data, the collection, decks - no web, no camera
#   server/     the web app: pages, routes, events (its __init__.py lists its modules)
#   games/      what differs from one card game to the next
#
# A station's image holds only this folder's own files and station/. So this file must
# import nothing - an import of anything from core/ or server/ here and no station starts
# (tests/test_layout.py refuses it). The programs are started from the top folder:
# app.py (the server) and station.py (a station).
