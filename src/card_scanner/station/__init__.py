# A camera station: everything that runs next to a camera, and nothing else.
#
#   station_client     the program: connection to the server, status, preview, uploads
#   station_setup      its own setup page (port 8080), station_setup.html
#   station_login      that page's password
#   scanner            the camera: capture loop, stillness, auto-capture, focus
#   object_detector    a card's outline in a picture, and the picture straightened
#
# These modules import only each other and the four shared ones (card_scanner.paths, config,
# config_loader, settings): never core/, server/ or games/, which a station's image does not
# even contain. tests/test_layout.py checks that. This file must stay free of imports.
