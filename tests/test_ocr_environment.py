"""
The OCR reader (Node.js, a third-party package) is started without the API keys and the other
secrets of this process's environment (core/card_ocr.py: reader_environment).

    venv/bin/python -m unittest discover tests -p test_ocr_environment.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

from card_scanner.core.card_ocr import reader_environment  # noqa: E402


class ReaderEnvironment(unittest.TestCase):
    def test_secrets_are_left_out_and_the_rest_is_kept(self):
        environment = {'PATH': '/usr/bin', 'HOME': '/home/x', 'DISPLAY': ':0', 'XDG_RUNTIME_DIR': '/run/user/1000',
                       'NVIDIA_DRIVER_CAPABILITIES': 'all', 'GEMINI_API_KEY': 'k1', 'OPENAI_API_KEY': 'k2',
                       'ANTHROPIC_API_KEY': 'k3', 'SCANNER_STATION_TOKEN': 't', 'LOCAL_AI_ENDPOINT': 'http://h:1',
                       'MY_SECRET': 's', 'DB_PASSWORD': 'p', 'github_token': 'g'}
        given = reader_environment(environment)
        self.assertEqual(sorted(given), ['DISPLAY', 'HOME', 'NVIDIA_DRIVER_CAPABILITIES', 'PATH', 'XDG_RUNTIME_DIR'])

    def test_it_reads_this_process_environment_by_default(self):
        import os
        os.environ['TEST_OCR_API_KEY'] = 'x'
        try:
            self.assertNotIn('TEST_OCR_API_KEY', reader_environment())
            self.assertIn('PATH', reader_environment())
        finally:
            del os.environ['TEST_OCR_API_KEY']


if __name__ == '__main__':
    unittest.main()
