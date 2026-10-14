#!/usr/bin/env python3
"""
The layout of the code: which module may import which, and that every import can be found.

Run:  venv/bin/python tests/test_layout.py        (or with the others: -m unittest discover tests)

The code is one package, card_scanner, sorted by which program a module belongs to
(docs/plans/PACKAGE_PLAN.md):

    card_scanner/            paths, config, config_loader, settings - what both programs use
    card_scanner/station/    a camera station: all of it, and nothing else
    card_scanner/core/       reading cards, card data, the collection - no web, no camera
    card_scanner/server/     the web app
    card_scanner/games/

Three rules follow from that, and each used to be kept by nothing but attention:

  1. A station stands alone. Its image holds station/ and the four shared modules, not core/
     or server/ - an import of `inventory` in a station module would be found by a station
     that does not start.
  2. core/ knows neither the web app nor the camera (no import from server/ or station/).
  3. Every import of the project names a module that exists. Tests only run an import
     that sits inside a function if they call that function, and pyflakes checks whether a
     name is used, not whether it can be found - so a wrong import in a rarely used
     function would show on the server, some evening.

This file reads the import statements with Python's own parser (ast): every one there is,
also those inside functions, whether or not anything executes them.

It was written before the move into the package and understands both layouts, so that it
could be seen to work on the old one first: with the modules still flat in src/ it checks
rule 1 by the modules' names and skips what only the package can answer.
"""
import ast
import importlib
import sys
import unittest
from pathlib import Path

TOP = Path(__file__).resolve().parent.parent
SRC = TOP / 'src'
PACKAGE = 'card_scanner'
PACKAGED = (SRC / PACKAGE).is_dir()          # False: the layout before the move (modules flat in src/)
sys.path.insert(0, str(SRC))

# The modules by where they belong - their names never changed, only their folder
SHARED = {'paths', 'config', 'config_loader', 'settings'}
STATION = {'station_client', 'station_setup', 'station_login', 'scanner', 'object_detector'}
CORE = {'identification', 'card_ocr', 'card_identifier', 'prompts', 'api_keys',
        'database', 'card_search', 'setup_database',
        'inventory', 'decks', 'trades', 'backups', 'storage', 'pending', 'review', 'cleanup',
        'stations', 'remote_scanner', 'recommendations', 'arena', 'frames'}
# What an import began with before the package existed: any of these at the start of an
# import now is one the move forgot
OLD_NAMES = SHARED | STATION | CORE | {'server', 'games'}


def python_files():
    """Every Python file of the project: the launchers, the code, the tests, the scripts"""
    files = sorted(TOP.glob('*.py'))
    for folder in ('src', 'tests', 'scripts'):
        files += sorted(path for path in (TOP / folder).rglob('*.py')
                        if 'node_modules' not in path.parts and '__pycache__' not in path.parts)
    return files


def imports_of(path):
    """
    Every import statement of a file as (line, module, names): `import a.b as c` gives
    (line, 'a.b', []), `from a.b import c, d` gives (line, 'a.b', ['c', 'd']). Relative
    imports (`from . import x`) are not used in this project and are reported as module ''.
    """
    found = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name, []) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found.append((node.lineno, node.module or '' if not node.level else '', [alias.name for alias in node.names]))
    return found


def where(path, line):
    return f"{path.relative_to(TOP)}:{line}"


def part_of(path):
    """Which part of the project a file is: 'shared', 'station', 'core', 'server', 'games' or None (tests, scripts, launchers)"""
    try:
        relative = path.relative_to(SRC)
    except ValueError:
        return None
    parts = relative.parts
    if PACKAGED:
        if parts[0] != PACKAGE:
            return None
        return 'shared' if len(parts) == 2 else parts[1]
    if len(parts) > 1:
        return parts[0]                       # src/server/..., src/games/...
    name = relative.stem
    return 'shared' if name in SHARED else 'station' if name in STATION else 'core' if name in CORE else None


def target_part(module, names):
    """Which part of the project an import reaches into, or None when it is not the project's"""
    pieces = module.split('.')
    if PACKAGED:
        if pieces[0] != PACKAGE:
            return None
        if len(pieces) == 1:                  # from card_scanner import config / games / server
            pieces = [PACKAGE, names[0]] if names else pieces
            if len(pieces) == 1:
                return 'shared'
        return 'shared' if pieces[1] in SHARED else pieces[1]
    first = pieces[0]
    return ('shared' if first in SHARED else 'station' if first in STATION else 'core' if first in CORE
            else first if first in ('server', 'games') else None)


class WhoMayImportWhom(unittest.TestCase):

    def crossing(self, parts, forbidden):
        """Imports in files of `parts` that reach into one of `forbidden`"""
        found = []
        for path in python_files():
            if part_of(path) not in parts:
                continue
            for line, module, names in imports_of(path):
                # `from card_scanner import a, b` can reach two parts: look at each name
                for name in (names if PACKAGED and module == PACKAGE else [None]):
                    target = target_part(module, [name] if name else names)
                    if target in forbidden:
                        found.append(f"{where(path, line)} imports {module}{' (' + name + ')' if name else ''}")
        return found

    def test_a_station_stands_alone(self):
        # The station's image has no core/, server/ or games/
        self.assertEqual(self.crossing({'station', 'shared'}, {'core', 'server', 'games'}), [])

    def test_shared_modules_import_only_each_other(self):
        self.assertEqual(self.crossing({'shared'}, {'station', 'core', 'server', 'games'}), [])

    def test_core_knows_neither_the_web_app_nor_the_camera(self):
        self.assertEqual(self.crossing({'core', 'games'}, {'server', 'station'}), [])

    def test_no_file_was_left_unsorted(self):
        # A module in src/ that is in none of the lists above would be checked by nothing
        unsorted = [str(path.relative_to(TOP)) for path in python_files()
                    if path.is_relative_to(SRC) and part_of(path) is None]
        self.assertEqual(unsorted, [])


@unittest.skipUnless(PACKAGED, "the modules are not in the package yet (docs/plans/PACKAGE_PLAN.md)")
class EveryImportCanBeFound(unittest.TestCase):

    def test_no_import_by_a_name_from_before_the_package(self):
        # `from config import Config`, `import inventory`, `from server.state import app` ...:
        # what every import of the project looked like before. The rewrite was done by a
        # program; this is what says it missed none - also in functions no test calls.
        # (tests/ may import each other by bare name: test_ownership is not in the list.)
        old = [f"{where(path, line)}: {module}" for path in python_files()
               for line, module, _names in imports_of(path) if module.split('.')[0] in OLD_NAMES]
        self.assertEqual(old, [])

    def test_no_relative_imports(self):
        # Imports are written out in full (card_scanner.core.inventory): the same line works
        # in the code, in a test and in `python -c`, and a search finds every use
        relative = [where(path, line) for path in python_files()
                    for line, module, _names in imports_of(path) if module == '']
        self.assertEqual(relative, [])

    def test_every_import_of_the_project_names_something_that_exists(self):
        missing = []
        for path in python_files():
            for line, module, names in imports_of(path):
                if module.split('.')[0] != PACKAGE:
                    continue
                folder = SRC.joinpath(*module.split('.'))
                if folder.is_dir():
                    # A package: each name is a module in it, or something its __init__.py defines
                    defined = self.names_defined_in(folder / '__init__.py')
                    for name in names:
                        if not ((folder / f'{name}.py').is_file() or (folder / name).is_dir() or name in defined):
                            missing.append(f"{where(path, line)}: {module} has no {name}")
                elif not folder.with_suffix('.py').is_file():
                    missing.append(f"{where(path, line)}: there is no module {module}")
        self.assertEqual(missing, [])

    @staticmethod
    def names_defined_in(init_file):
        """Names an __init__.py gives its package: what it assigns and what it imports"""
        names = set()
        for node in ast.walk(ast.parse(init_file.read_text())):
            if isinstance(node, ast.Assign):
                names |= {target.id for target in node.targets if isinstance(target, ast.Name)}
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names |= {(alias.asname or alias.name).split('.')[0] for alias in node.names}
        return names

    def test_packages_that_a_station_imports_do_nothing_when_imported(self):
        # card_scanner/__init__.py and station/__init__.py are run by every station. One import
        # in them of anything from core/ or server/ and no station starts: they stay empty
        # (comments and a docstring are fine)
        for init_file in (SRC / PACKAGE / '__init__.py', SRC / PACKAGE / 'station' / '__init__.py',
                          SRC / PACKAGE / 'core' / '__init__.py'):
            statements = [node for node in ast.parse(init_file.read_text()).body
                          if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))]
            self.assertEqual(statements, [], f"{init_file.relative_to(TOP)} must not run anything")

    def test_every_module_can_be_imported(self):
        # Finds an import cycle and a folder without __init__.py. Importing the server's
        # modules sets up logging (data/logs) and starts nothing, as tests/test_app_surface.py
        # relies on; the two command-line tools only act under `if __name__ == "__main__"`.
        failed = []
        for path in sorted((SRC / PACKAGE).rglob('*.py')):
            if '__pycache__' in path.parts:
                continue
            relative = path.relative_to(SRC).with_suffix('')
            name = '.'.join(relative.parts[:-1] if relative.name == '__init__' else relative.parts)
            try:
                importlib.import_module(name)
            except Exception as e:  # noqa: BLE001 - whatever stops an import is the finding
                failed.append(f"{name}: {type(e).__name__}: {e}")
        self.assertEqual(failed, [])


if __name__ == '__main__':
    unittest.main()
