"""
A small client for the card scanner server's web API (the same one its pages use).

Only the standard library: this program runs on a laptop beside an AI client and must not need
the server's code or its libraries. It only ever reads (GET) in this first version - the one
POST it makes is "start a search" for `what_can_i_build`, which changes nothing in the
collection or the decks.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request


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

    def _request(self, path, params=None, body=None):
        url = self.base + path
        if params:
            url += '?' + urllib.parse.urlencode({key: value for key, value in params.items() if value not in (None, '')})
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'} if data else {})
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
