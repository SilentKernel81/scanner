"""
The settings page (templates/settings.html, static/js/settings.js): the script only asks for
elements the page has, the five categories each have a button and a section, and the old
drawers (scanner and collection pages) are gone with the elements their scripts used.

    venv/bin/python -m unittest discover tests -p test_settings_page.py
"""
import re
import sys
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / 'src/card_scanner/server'
PAGE = (SERVER / 'templates/settings.html').read_text()
SCRIPT = (SERVER / 'static/js/settings.js').read_text()


class SettingsPage(unittest.TestCase):
    def test_every_element_the_script_asks_for_is_in_the_page(self):
        ids = set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", SCRIPT)) | set(re.findall(r"getElementById\('([A-Za-z0-9_-]+)'\)", SCRIPT))
        in_page = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', PAGE))
        # Added by the shared files, not by this template: the toasts and dialogs (_dialogs.html)
        in_page |= set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', (SERVER / 'templates/_dialogs.html').read_text()))
        # `quit-program` is only there when started as a desktop program (and the script checks)
        missing = sorted(ids - in_page)
        self.assertEqual(missing, [], f"settings.js asks for elements settings.html does not have: {missing}")

    def test_each_category_has_a_button_and_a_section(self):
        categories = re.search(r"const CATEGORIES = \[([^\]]*)\]", SCRIPT).group(1)
        names = re.findall(r"'([a-z]+)'", categories)
        self.assertEqual(len(names), 5)
        for name in names:
            self.assertIn(f'data-category="{name}"', PAGE)
            self.assertIn(f'id="cat-{name}"', PAGE)

    def test_the_pages_have_no_drawer_left(self):
        for template in ('scanner.html', 'collection.html'):
            text = (SERVER / 'templates' / template).read_text()
            self.assertNotIn('settings-drawer', text, template)
            self.assertNotIn('_program.html', text, template)
        for script in ('scanner.js', 'collection.js'):
            text = (SERVER / 'static/js' / script).read_text()
            self.assertNotIn("$('settings-drawer')", text, script)

    def test_the_header_links_to_the_page(self):
        top = (SERVER / 'templates/_topbar.html').read_text()
        self.assertIn("url_for('settings_page')", top)


if __name__ == '__main__':
    unittest.main()
