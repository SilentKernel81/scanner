#!/usr/bin/env python3
"""
Tries the scanner page's handlers and timers in a real browser (headless Chromium), against a
running scanner server that knows a station called `check` (Stations().seen('check') in its data folder):

    venv/bin/python scripts/check_scanner_page.py http://localhost:5003

Run the server on an empty or copied data folder (MTG_SCANNER_HOME=... app.py): it changes nothing,
but never point it at real data out of habit. Checks (outside code review of 2026-10-16):
  - the page loads without a script error;
  - a card whose name holds a backslash and a quote can be clicked in the panel's list (the name
    reaches the handler whole), and `escapeHtml(null)` is an empty text;
  - no request is made while the tab is hidden, and the page asks again when it is shown.
It needs Chromium and websocket-client.
"""
import json
import subprocess
import sys
import tempfile
import time
import urllib.request

import websocket

BASE = sys.argv[1].rstrip('/')
PORT = 9335
failed = []


def check(what, ok, detail=''):
    print(('  ok   ' if ok else '  FAIL ') + what + (f'   [{detail}]' if detail and not ok else ''))
    if not ok:
        failed.append(what)


process = subprocess.Popen(
    ['chromium', '--headless=new', f'--remote-debugging-port={PORT}', f'--user-data-dir={tempfile.mkdtemp()}',
     '--no-first-run', '--disable-gpu', '--window-size=1440,900', 'about:blank'],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(60):
        try:
            tabs = json.load(urllib.request.urlopen(f'http://localhost:{PORT}/json'))
            break
        except OSError:
            time.sleep(0.2)
    ws = websocket.create_connection([t for t in tabs if t['type'] == 'page'][0]['webSocketDebuggerUrl'], suppress_origin=True)
    counter = [0]
    errors = []

    def call(method, **params):
        counter[0] += 1
        ws.send(json.dumps({'id': counter[0], 'method': method, 'params': params}))
        while True:
            message = json.loads(ws.recv())
            if message.get('method') == 'Runtime.exceptionThrown':
                errors.append(message['params']['exceptionDetails'].get('exception', {}).get('description', '')[:200])
            if message.get('id') == counter[0]:
                return message.get('result', {})

    def js(code):
        result = call('Runtime.evaluate', expression=code, awaitPromise=True, returnByValue=True)
        if 'exceptionDetails' in result:
            errors.append('in a check: ' + str(result['exceptionDetails'].get('exception', {}).get('description'))[:200])
        return result.get('result', {}).get('value')

    def wait(seconds):
        ws.settimeout(0.2)
        end = time.time() + seconds
        while time.time() < end:
            try:
                message = json.loads(ws.recv())
                if message.get('method') == 'Runtime.exceptionThrown':
                    errors.append(message['params']['exceptionDetails'].get('exception', {}).get('description', '')[:200])
            except (websocket.WebSocketTimeoutException, TimeoutError):
                pass
        ws.settimeout(None)

    call('Page.enable'); call('Runtime.enable')
    call('Page.navigate', url=BASE + '/scan/check')
    wait(4)
    check('the scanner page loads without a script error', not errors, str(errors))

    check('escapeHtml(null) and (undefined) are empty texts', js("escapeHtml(null) === '' && escapeHtml(undefined) === '' && escapeHtml(5) === '5'"))

    # a name with a backslash and a quote reaches the handler whole
    js("window.__picked = []; window.selectSimilarCard = n => window.__picked.push(['similar', n]);"
       "window.selectPrinting = i => window.__picked.push(['printing', i]);"
       "displaySimilarCards([{name: \"A\\\\B 'x' \\\"y\\\"\", set: 'tst', price: 1}]);")
    js("document.querySelector('.similar-card').click()")
    picked = js("JSON.stringify(window.__picked)")
    check('a similar card named A\\B \'x\' "y" is passed on whole', picked == json.dumps([['similar', 'A\\B \'x\' "y"']], separators=(',', ':')), picked)
    js("displayPrintings('Bolt', [{id: 'abc-123', name: 'Bolt', set: 'tst', number: '1', price: 0, price_foil: 0, treatments: []}])")
    js("window.__picked = []; document.querySelector('.printing-card').click()")
    check('a printing is chosen through its data attribute', js("JSON.stringify(window.__picked)") == '[["printing","abc-123"]]')

    # a hidden tab asks nothing
    js("window.__calls = 0; const f = window.fetch; window.fetch = function(url) { if (/detection_status|api[/]stats/.test(String(url))) window.__calls++; return f.apply(this, arguments); };"
       "Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});")
    wait(2.5)
    check('no request while the tab is hidden', js("window.__calls") == 0, str(js("window.__calls")))
    js("Object.defineProperty(document, 'hidden', {configurable: true, get: () => false}); document.dispatchEvent(new Event('visibilitychange'));")
    wait(1.5)
    check('the page asks again when the tab is shown', (js("window.__calls") or 0) > 0)
finally:
    process.terminate()
if errors:
    print('script errors seen:', errors)

print('\nFAILED: ' + ', '.join(failed) if failed else '\nall ok')
sys.exit(1 if failed else 0)
