"""
The collection page's dropdowns with ticks - Type, Rarity, Set, Finish, Location, Tags
(static/js/collection.js: passesMulti): nothing ticked shows every card, else a card needs any
one of the ticked values; with Not, none of them. Run with node.

    venv/bin/python -m unittest discover tests -p test_multi_filter.py
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / 'src/card_scanner/server/static/js/collection.js'


class MultiFilter(unittest.TestCase):
    def run_cases(self, cases):
        node = shutil.which('node')
        if not node:
            self.skipTest("node is not installed")
        found = re.search(r'// multiFilter begin(.*?)// multiFilter end', SCRIPT.read_text(), re.S)
        self.assertTrue(found, "passesMulti is not in collection.js")
        program = found.group(1) + """
const cases = JSON.parse(process.argv[process.argv.length - 1]);
console.log(JSON.stringify(cases.map(c => passesMulti(c.card, new Set(c.chosen), !!c.not))));
"""
        out = subprocess.run([node, '-e', program, '--', json.dumps(cases)], capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_nothing_ticked_shows_every_card(self):
        cases = [{'card': [], 'chosen': []}, {'card': ['rare'], 'chosen': []}, {'card': ['rare'], 'chosen': [], 'not': True}]
        self.assertEqual(self.run_cases(cases), [True, True, True])

    def test_a_card_needs_any_one_of_the_ticked_values(self):
        cases = [{'card': ['rare'], 'chosen': ['rare', 'mythic']},
                 {'card': ['common'], 'chosen': ['rare', 'mythic']},
                 {'card': ['trade', 'foil'], 'chosen': ['binder', 'foil']},    # a card's tags: any one
                 {'card': [], 'chosen': ['trade']}]
        self.assertEqual(self.run_cases(cases), [True, False, True, False])

    def test_not_turns_it_around(self):
        cases = [{'card': ['common'], 'chosen': ['rare', 'mythic'], 'not': True},
                 {'card': ['rare'], 'chosen': ['rare', 'mythic'], 'not': True},
                 {'card': [None], 'chosen': ['rare'], 'not': True},            # a card without a rarity
                 {'card': ['trade', 'foil'], 'chosen': ['foil'], 'not': True}]  # one of its tags is ticked
        self.assertEqual(self.run_cases(cases), [True, False, True, False])


if __name__ == '__main__':
    unittest.main()
