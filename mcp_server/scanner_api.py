"""
A small client for the card scanner server's web API (the same one its pages use).

Only the standard library: this program runs on a laptop beside an AI client and must not need
the server's code or its libraries. It reads (GET); the one POST of the read-only tools is
"start a search" for `what_can_i_build`, which changes nothing in the collection or the decks.
The deck tools (tools.py, only with --allow-deck-writes) write through `write`, which takes
nothing but the deck routes.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request


# The only writes the deck tools may make (method, path): create a deck, change its cards, rename it /
# change its notes. No DELETE, and nothing outside /api/decks - the collection, the scanned cards,
# trades and settings are out of reach of every tool.
DECK_WRITE_ROUTES = (
    ('POST', re.compile(r'/api/decks')),                 # a new deck (name, format, kind, its list)
    ('POST', re.compile(r'/api/decks/\d+/cards')),       # add / remove / move a deck's cards
    ('PUT', re.compile(r'/api/decks/\d+')),              # its name and notes
)


def deck_write_allowed(method, path):
    """Whether a write is one of DECK_WRITE_ROUTES (the whole path must match: no query, nothing after)"""
    return any(method == allowed and pattern.fullmatch(path) for allowed, pattern in DECK_WRITE_ROUTES)


class ScannerError(Exception):
    """Something the assistant should be told in words: the server is away, a deck does not exist ... status: the HTTP
    status the server answered, or None when it did not answer (away, a time-out)"""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class ScannerApi:
    def __init__(self, base='http://localhost:5000', timeout=60, cache_seconds=20):
        self.base = base.rstrip('/')
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self._cache = {}   # url -> (time, answer): the collection and the decks are big, and asked for often

    def _request(self, path, params=None, body=None, method=None):
        url = self.base + path
        if params:
            url += '?' + urllib.parse.urlencode({key: value for key, value in params.items() if value not in (None, '')})
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method,
                                         headers={'Content-Type': 'application/json'} if data else {})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as answer:
                return url, json.loads(answer.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            # The server's own words, when it said any ({'error': ...})
            try:
                message = json.loads(e.read().decode('utf-8')).get('error')
            except (ValueError, OSError):
                message = None
            raise ScannerError(message or f"The scanner server answered {e.code} for {path}", status=e.code)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ScannerError(f"Could not reach the scanner server at {self.base}: {getattr(e, 'reason', e)}")

    def get(self, path, **params):
        """The server's JSON answer to a GET, kept for a few seconds (the same data is often asked twice in a row)"""
        key = path + '?' + urllib.parse.urlencode(sorted((k, str(v)) for k, v in params.items() if v not in (None, '')))
        now = time.time()
        cached = self._cache.get(key)
        if cached and now - cached[0] < self.cache_seconds:
            return cached[1]
        _url, answer = self._request(path, params)
        if isinstance(answer, dict) and answer.get('success') is False:
            raise ScannerError(answer.get('error') or 'The scanner server refused the request')
        self._cache[key] = (now, answer)
        return answer

    def get_fresh(self, path, **params):
        """A GET that is never kept (a search that is still running changes from one look to the next)"""
        _url, answer = self._request(path, params)
        if isinstance(answer, dict) and answer.get('success') is False:
            raise ScannerError(answer.get('error') or 'The scanner server refused the request')
        return answer

    def post(self, path, body):
        """A POST (not kept): only used to start a search that changes nothing"""
        _url, answer = self._request(path, body=body)
        if isinstance(answer, dict) and answer.get('success') is False:
            raise ScannerError(answer.get('error') or 'The scanner server refused the request')
        return answer

    def write(self, method, path, body):
        """
        A change to a deck: POST / PUT of a deck route only (DECK_WRITE_ROUTES above) -
        anything else is refused here, before it reaches the network, so no tool can change the
        collection, the scanned cards, a trade or a setting even by mistake. Every kept answer is
        forgotten afterwards: the deck read next must be the deck as it now is, not one 20 s old.
        """
        if not deck_write_allowed(method, path):
            raise ScannerError(f'Refused: {method} {path} is not a change to a deck')
        try:
            _url, answer = self._request(path, body=body, method=method)
        finally:
            self._cache.clear()   # also after a failure: the server may have done part of it
        if isinstance(answer, dict) and answer.get('success') is False:
            raise ScannerError(answer.get('error') or 'The scanner server refused the change')
        return answer
