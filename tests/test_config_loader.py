"""
config.yaml's ${VARIABLE} substitution (config_loader.py: _substitute_env_vars): the whole text is
kept, not only the variable's value.

    venv/bin/python -m unittest discover tests -p test_config_loader.py
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

from card_scanner.config_loader import ConfigLoader  # noqa: E402


class Substitution(unittest.TestCase):
    def substitute(self, value):
        loader = ConfigLoader.__new__(ConfigLoader)          # the method only needs itself
        return loader._substitute_env_vars(value)

    def test_the_whole_text_is_kept(self):
        with mock.patch.dict(os.environ, {'SCAN_DATA': '/srv/scan', 'SCAN_PORT': '8080'}):
            self.assertEqual(self.substitute('${SCAN_DATA}/cards'), '/srv/scan/cards')
            self.assertEqual(self.substitute('http://host:${SCAN_PORT}/api'), 'http://host:8080/api')
            self.assertEqual(self.substitute('${SCAN_DATA}:${SCAN_PORT}'), '/srv/scan:8080')

    def test_a_lone_variable_is_its_value_and_an_unset_one_is_empty(self):
        with mock.patch.dict(os.environ, {'SCAN_KEY': 'abc'}):
            os.environ.pop('SCAN_UNSET', None)
            self.assertEqual(self.substitute('${SCAN_KEY}'), 'abc')
            self.assertEqual(self.substitute('${SCAN_UNSET}'), '')
            self.assertEqual(self.substitute('prefix-${SCAN_UNSET}-suffix'), 'prefix--suffix')

    def test_text_without_a_variable_and_other_types_are_left_alone(self):
        self.assertEqual(self.substitute('plain text'), 'plain text')
        self.assertEqual(self.substitute(12), 12)
        self.assertEqual(self.substitute({'a': ['${NOT_SET_X}/y', 3]}), {'a': ['/y', 3]})


if __name__ == '__main__':
    unittest.main()
