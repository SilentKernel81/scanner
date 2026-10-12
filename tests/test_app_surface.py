"""
The server's surface: every HTTP route (address, methods, endpoint name) and every Socket.IO
event the pages can send, compared with the list recorded in tests/app_surface.json.

Why: the web app has no tests of its own, and its code is spread over the modules of
server/. A route or an event exists only if the module that defines it is imported at
start-up - a module left out of that list is a set of routes that silently does not exist,
and nothing else would notice until someone uses them. Templates also rely on the endpoint
names (url_for('collection')), so those are part of the surface too.

When a route or event is added, renamed or removed on purpose, record the list again:

    venv/bin/python tests/test_app_surface.py --record

and commit the changed app_surface.json with the change - the diff of that file is then a
readable summary of what changed for the pages and the stations.

    venv/bin/python -m unittest discover tests
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RECORDED = Path(__file__).with_name('app_surface.json')


def surface():
    """The routes and events of the app as it is when imported (nothing is started)"""
    import app  # noqa: E402 - sets up logging (data/logs); starts no thread and opens no database
    routes = sorted([rule.rule, sorted(rule.methods - {'HEAD', 'OPTIONS'}), rule.endpoint]
                    for rule in app.app.url_map.iter_rules())
    # python-socketio keeps the handlers per namespace; '/' is the pages' namespace (the
    # stations' namespace, /station, is registered at start-up, by remote_scanner.CameraHub)
    events = sorted(app.socketio.server.handlers.get('/', {}))
    return {'routes': routes, 'events': events}


class Surface(unittest.TestCase):
    maxDiff = None

    def test_routes_and_events_are_the_recorded_ones(self):
        recorded = json.loads(RECORDED.read_text())
        now = surface()
        self.assertEqual(now['events'], recorded['events'])
        self.assertEqual(now['routes'], recorded['routes'])


if __name__ == '__main__':
    if '--record' in sys.argv:
        RECORDED.write_text(json.dumps(surface(), indent=1) + '\n')
        print(f"Recorded {RECORDED}")
    else:
        unittest.main()
