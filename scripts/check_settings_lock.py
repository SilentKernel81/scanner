#!/usr/bin/env python3
"""
Tries the settings password in a real browser (headless Chromium) against a running scanner server
on an EMPTY or copied data folder (it sets, changes and removes the password; run it as
`MTG_SCANNER_HOME=<new folder> venv/bin/python app.py` with `flask: port: 5003` in that folder's config.yaml):

    venv/bin/python scripts/check_settings_lock.py http://localhost:5003

Checks: the password is set on the Settings page; a guarded request is answered with the password
question, a wrong password asks again, the right one repeats the request; "Lock this browser" asks
again; the password is removed. It needs Chromium and websocket-client.
"""
import json
import subprocess
import sys
import tempfile
import time
import urllib.request

import websocket

BASE = sys.argv[1].rstrip('/')
PORT = 9336
failed = []


def check(what, ok, detail=''):
    print(('  ok   ' if ok else '  FAIL ') + what + (f'   [{detail}]' if detail and not ok else ''))
    if not ok:
        failed.append(what)


process = subprocess.Popen(
    ['chromium', '--headless=new', f'--remote-debugging-port={PORT}', f'--user-data-dir={tempfile.mkdtemp()}',
     '--no-first-run', '--disable-gpu', '--window-size=1440,900', 'about:blank'],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
errors = []
try:
    for _ in range(60):
        try:
            tabs = json.load(urllib.request.urlopen(f'http://localhost:{PORT}/json'))
            break
        except OSError:
            time.sleep(0.2)
    ws = websocket.create_connection([t for t in tabs if t['type'] == 'page'][0]['webSocketDebuggerUrl'], suppress_origin=True)
    counter = [0]

    def note(message):
        if message.get('method') == 'Runtime.exceptionThrown':
            errors.append(message['params']['exceptionDetails'].get('exception', {}).get('description', '')[:200])

    def call(method, **params):
        counter[0] += 1
        ws.send(json.dumps({'id': counter[0], 'method': method, 'params': params}))
        while True:
            message = json.loads(ws.recv())
            note(message)
            if message.get('id') == counter[0]:
                return message.get('result', {})

    def js(code):
        # (a promise returned here would be waited for - and the guarded request waits for the password question)
        result = call('Runtime.evaluate', expression=code, awaitPromise=True, returnByValue=True)
        if 'exceptionDetails' in result:
            errors.append('in a check: ' + str(result['exceptionDetails'].get('exception', {}).get('description'))[:200])
        return result.get('result', {}).get('value')

    def wait(seconds):
        ws.settimeout(0.2)
        end = time.time() + seconds
        while time.time() < end:
            try:
                note(json.loads(ws.recv()))
            except (websocket.WebSocketTimeoutException, TimeoutError):
                pass
        ws.settimeout(None)

    def dialog_open():
        return js("document.getElementById('dialog-modal').classList.contains('show') && !!document.querySelector('#dialog-modal .dialog-field')")

    def type_and_submit(text):
        js(f"(() => {{ const f = document.querySelector('#dialog-modal .dialog-field'); f.value = {json.dumps(text)};"
           "f.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter'})); })()")
        wait(1.5)

    call('Page.enable'); call('Runtime.enable')
    call('Page.navigate', url=BASE + '/settings#server')
    wait(3)
    check('the Settings page loads without a script error', not errors, str(errors))
    check('no password at first', 'No password is set' in (js("document.getElementById('lock-status').textContent") or ''))

    js("document.getElementById('lock-new').value = 'correct horse'; document.getElementById('lock-set').click()")
    wait(2)
    check('the password is set and this browser is unlocked', 'unlocked' in (js("document.getElementById('lock-status').textContent") or ''),
          js("document.getElementById('lock-status').textContent"))

    js("document.getElementById('lock-lock').click()")
    wait(1.5)
    check('"Lock this browser" locks it', 'locked' in (js("document.getElementById('lock-status').textContent") or '')
          and 'unlocked' not in js("document.getElementById('lock-status').textContent"))

    # a guarded request: the question appears, a wrong answer asks again, the right one repeats the request
    js("window.__result = null; (() => { fetch('/api/backups/x/restore', {method: 'POST'}).then(r => { window.__result = r.status; }); })()")
    wait(1)
    check('a guarded request opens the password question', dialog_open())
    type_and_submit('wrong one')
    check('a wrong password asks again, saying so', dialog_open() and 'Wrong password' in (js("document.getElementById('dialog-message').textContent") or ''))
    wait(2.5)                                                     # the wait after a wrong attempt
    type_and_submit('correct horse')
    check('the right password repeats the request (answered by the route, not 401)', js("window.__result") not in (None, 401), str(js("window.__result")))
    check('and the question is closed', not dialog_open())

    # a refused request that is cancelled stays refused
    js("document.getElementById('lock-lock').click()")
    wait(1)
    js("window.__result = null; (() => { fetch('/api/backups/x/restore', {method: 'POST'}).then(r => { window.__result = r.status; }); })()")
    wait(1)
    js("document.querySelector('#dialog-buttons button').click()")      # Cancel
    wait(1)
    check('cancelling leaves the request refused', js("window.__result") == 401, str(js("window.__result")))

    # a guarded Socket.IO event (the debug-mode switch) is answered with the same question
    js("socket.emit('toggle_debug_mode', {enabled: true})")
    wait(1.5)
    check('a guarded Socket.IO event opens the password question too', dialog_open())
    js("document.querySelector('#dialog-buttons button').click()")      # Cancel
    wait(0.5)

    # remove the password (unlock first, through the page's own button)
    js("document.getElementById('lock-unlock').click()")
    wait(1)
    type_and_submit('correct horse')
    js("document.getElementById('lock-remove').click()")
    wait(1)
    js("[...document.querySelectorAll('#dialog-buttons button')].find(b => b.textContent === 'Remove').click()")
    wait(2)
    check('the password is removed', 'No password is set' in (js("document.getElementById('lock-status').textContent") or ''),
          js("document.getElementById('lock-status').textContent"))
    check('no script error', not errors, str(errors))
finally:
    process.terminate()

print('\nFAILED: ' + ', '.join(failed) if failed else '\nall ok')
sys.exit(1 if failed else 0)
