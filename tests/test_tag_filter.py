"""
The collection page's Tags dropdown (static/js/collection.js: passesTagFilter): nothing ticked
shows every card, else a card needs any one of the ticked tags. Run with node.

    venv/bin/python -m unittest discover tests -p test_tag_filter.py
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / 'src/card_scanner/server/static/js/collection.js'


class TagFilter(unittest.TestCase):
    def run_cases(self, cases):
        node = shutil.which('node')
        if not node:
            self.skipTest("node is not installed")
        found = re.search(r'// tagFilter begin(.*?)// tagFilter end', SCRIPT.read_text(), re.S)
        self.assertTrue(found, "passesTagFilter is not in collection.js")
        program = found.group(1) + """
const cases = JSON.parse(process.argv[process.argv.length - 1]);
console.log(JSON.stringify(cases.map(c => passesTagFilter(c.card, new Set(c.chosen)))));
"""
        out = subprocess.run([node, '-e', program, '--', json.dumps(cases)], capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_nothing_ticked_shows_every_card(self):
        self.assertEqual(self.run_cases([{'card': [], 'chosen': []}, {'card': ['trade'], 'chosen': []}]), [True, True])

    def test_a_card_needs_any_one_of_the_ticked_tags(self):
        cases = [{'card': ['trade'], 'chosen': ['trade', 'binder']},
                 {'card': ['binder', 'foil'], 'chosen': ['trade', 'binder']},
                 {'card': ['foil'], 'chosen': ['trade', 'binder']},
                 {'card': [], 'chosen': ['trade']}]
        self.assertEqual(self.run_cases(cases), [True, True, False, False])


if __name__ == '__main__':
    unittest.main()
