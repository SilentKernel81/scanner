"""
A cloud AI provider's rate limit, server error or timeout is tried again (core/card_identifier.py:
_post_with_retry); a wrong key or a bad request is not. No network: requests.post is a stand-in.

    venv/bin/python -m unittest discover tests -p test_ai_retry.py
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/

from card_scanner.core import card_identifier  # noqa: E402
from card_scanner.core.card_identifier import CardIdentifier  # noqa: E402


def answer(status, headers=None, body=None):
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    response._content = b'{}' if body is None else body
    return response


class Retry(unittest.TestCase):
    def setUp(self):
        self.identifier = CardIdentifier.__new__(CardIdentifier)
        self.identifier.provider = 'openai'
        self.identifier.log = mock.Mock()
        sleep = mock.patch.object(card_identifier.time, 'sleep')
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def post(self, *results):
        with mock.patch.object(card_identifier.requests, 'post', side_effect=list(results)) as post:
            response = self.identifier._post_with_retry('https://example.invalid', json={})
        return response, post.call_count

    def test_a_rate_limit_is_tried_again_and_then_succeeds(self):
        response, calls = self.post(answer(429), answer(429), answer(200))
        self.assertEqual((response.status_code, calls), (200, 3))
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1.0, 3.0])

    def test_a_server_error_and_a_timeout_too(self):
        response, calls = self.post(answer(503), requests.exceptions.Timeout('slow'), answer(200))
        self.assertEqual((response.status_code, calls), (200, 3))

    def test_it_gives_up_after_three_tries_and_hands_back_the_last_answer(self):
        response, calls = self.post(answer(429), answer(429), answer(429))
        self.assertEqual((response.status_code, calls), (429, 3))       # the caller raises for it, as before

    def test_a_timeout_every_time_raises_the_last_one(self):
        with self.assertRaises(requests.exceptions.Timeout):
            self.post(requests.exceptions.Timeout('a'), requests.exceptions.Timeout('b'), requests.exceptions.Timeout('c'))

    def test_a_wrong_key_or_a_bad_request_is_not_repeated(self):
        for status in (400, 401, 403, 404):
            response, calls = self.post(answer(status))
            self.assertEqual((response.status_code, calls), (status, 1), status)

    def test_retry_after_is_respected_up_to_a_limit(self):
        self.post(answer(429, {'Retry-After': '5'}), answer(429, {'Retry-After': '120'}), answer(200))
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [5.0, 8.0])

    def test_the_cloud_providers_use_it_and_a_local_server_does_not(self):
        self.identifier.model, self.identifier.api_key, self.identifier.local_endpoint = 'm', 'k', 'http://localhost:1/v1/chat/completions'
        good = answer(200, body=b'{"choices": [{"message": {"content": " Lightning Bolt "}}]}')
        with mock.patch.object(card_identifier.requests, 'post', side_effect=[answer(429), good]):
            self.assertEqual(self.identifier._ask_openai('img', 'prompt', 100), 'Lightning Bolt')
        with mock.patch.object(card_identifier.requests, 'post', side_effect=[answer(429), good]) as post:
            with self.assertRaises(requests.exceptions.HTTPError):
                self.identifier._ask_openai_compatible('img', 'prompt', 100)   # local: no retry here (raises as before)
            self.assertEqual(post.call_count, 1)


if __name__ == '__main__':
    unittest.main()
