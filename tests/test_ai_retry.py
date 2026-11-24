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


class ParsingAndProviders(unittest.TestCase):
    def setUp(self):
        self.identifier = CardIdentifier.__new__(CardIdentifier)
        self.identifier.provider = 'gemini'
        self.identifier.model = 'gemini-2.5-flash'
        self.identifier.api_key = 'k'
        self.identifier.log = mock.Mock()

    def test_parse_response_accepts_set_code_with_foil_marker_and_language(self):
        for raw, want in (('NAME: Sol Ring\nNUMBER: 0242\nSET: HOB • EN', 'HOB'),
                          ('NAME: Sol Ring\nNUMBER: 0242\nSET: HOB ★ EN', 'HOB'),
                          ('NAME: Sol Ring\nNUMBER: 0242\nSET: SPM·EN', 'SPM'),
                          ('NAME: Sol Ring\nNUMBER: 0242\nSET: HOB • ZHS', 'HOB'),
                          ('NAME: Sol Ring\nNUMBER: 0242\nSET: No set', ''),
                          ('NAME: Sol Ring\nNUMBER: 0242\nSET: Unknown', '')):
            parsed = self.identifier._parse_response(raw, 'test')
            self.assertEqual(parsed['set_code'], want, raw)

    def test_parse_response_maps_token_t_prefix_to_token_set(self):
        for raw, want_num, want_set in (
            ('NAME: Treasure\nNUMBER: T 0001\nSET: FRA • EN', '0001', 'TFRA'),
            ('NAME: Treasure\nNUMBER: T0001\nSET: FRA', '0001', 'TFRA'),
            ('NAME: Spirit\nNUMBER: T 0004\nSET: TDC', '0004', 'TTDC'),
            ('NAME: Treasure\nNUMBER: T 0001\nSET: TFRA', '0001', 'TFRA'),
            ('NAME: Treasure\nNUMBER: T 018B\nSET: FRA', '0188', 'TFRA'),
            ('Treasure\nT0001\nFRA', '0001', 'TFRA'),
            ('NAME: Sol Ring\nNUMBER: U 0242\nSET: FRA', 'U 0242', 'FRA'),
        ):
            parsed = self.identifier._parse_response(raw, 'test')
            self.assertEqual((parsed['collector_number'], parsed['set_code']), (want_num, want_set), raw)

    def test_openai_compatible_handles_null_content_and_zero_temperature(self):
        self.identifier.model = 'qwen3-vl:8b'
        self.identifier.local_endpoint = 'http://127.0.0.1:11434/v1/chat/completions'
        null_body = b'{"choices": [{"message": {"content": null}}]}'
        with mock.patch.object(card_identifier.requests, 'post', return_value=answer(200, body=null_body)) as post:
            self.assertEqual(self.identifier._ask_openai_compatible('img', 'prompt', 100), '')
        self.assertEqual(post.call_args.kwargs['json']['temperature'], 0)

    def test_parse_foil_answer_does_not_invert_negated_or_conflicting_replies(self):
        parse = CardIdentifier._parse_foil_answer
        self.assertEqual(parse('star'), 'foil')
        self.assertEqual(parse('dot'), 'non-foil')
        self.assertEqual(parse('It is a star.'), 'foil')
        self.assertEqual(parse('dot, not a star'), 'non-foil')
        self.assertEqual(parse('It is a dot, not a star.'), 'unknown')
        self.assertEqual(parse('unclear'), 'unknown')
        self.assertEqual(parse('stardust'), 'unknown')

    def test_gemini_sets_temperature_and_disables_thinking_on_flash_and_skips_thought_parts(self):
        body = (b'{"candidates": [{"content": {"parts": [{"text": "reasoning...", "thought": true}, '
                b'{"text": "NAME: Lightning Bolt\\nNUMBER: 0123\\nSET: M11"}]}}]}')
        with mock.patch.object(card_identifier.requests, 'post', return_value=answer(200, body=body)) as post:
            text = self.identifier._ask_gemini('img', 'prompt', 100)
        self.assertEqual(text, 'NAME: Lightning Bolt\nNUMBER: 0123\nSET: M11')
        gen_cfg = post.call_args.kwargs['json']['generationConfig']
        self.assertEqual(gen_cfg['temperature'], 0)
        self.assertEqual(gen_cfg['thinkingConfig'], {'thinkingBudget': 0})

        # Empty candidates (e.g. safety block) returns '' instead of raising KeyError
        with mock.patch.object(card_identifier.requests, 'post', return_value=answer(200, body=b'{"candidates": []}')):
            self.assertEqual(self.identifier._ask_gemini('img', 'prompt', 100), '')

    def test_identification_set_identifier_swaps_and_closes_previous_session(self):
        from card_scanner.core.identification import Identification
        ident = Identification.__new__(Identification)
        first = CardIdentifier(provider='gemini', model='gemini-2.5-flash', api_key='key-1')
        second = CardIdentifier(provider='gemini', model='gemini-2.5-flash', api_key='key-2')
        first.close = mock.Mock(wraps=first.close)
        ident.card_identifier = first
        ident.set_identifier(second)
        self.assertIs(ident.card_identifier, second)
        first.close.assert_called_once()
        ident.set_identifier(None)
        self.assertIsNone(ident.card_identifier)
        second.close()

    def test_sessions_are_pooled_across_short_lived_threads_and_closed_on_close(self):
        import threading
        identifier = CardIdentifier(provider='gemini', model='gemini-2.5-flash', api_key='key-1')
        # 20 sequential short-lived threads (like foil_thread per card) reuse 1 pooled session, never piling up
        seen = []
        for _ in range(20):
            def use():
                with identifier._http() as s:
                    seen.append(s)
            t = threading.Thread(target=use)
            t.start()
            t.join()
        self.assertEqual(len(set(seen)), 1)
        self.assertEqual(identifier._session_pool.qsize(), 1)

        # Concurrent callers never share the same Session simultaneously
        barrier = threading.Barrier(2)
        concurrent = []

        def hold():
            with identifier._http() as s:
                concurrent.append(s)
                barrier.wait(timeout=5)
        threads = [threading.Thread(target=hold), threading.Thread(target=hold)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(concurrent)), 2)
        self.assertEqual(identifier._session_pool.qsize(), 2)

        for s in concurrent:
            s.close = mock.Mock(wraps=s.close)
        identifier.close()
        for s in concurrent:
            s.close.assert_called_once()
        # After close(), _http() falls back cleanly without accumulating new pooled sessions
        with identifier._http() as after:
            self.assertIs(after, requests)
        self.assertEqual(identifier._session_pool.qsize(), 0)


if __name__ == '__main__':
    unittest.main()
