#!/usr/bin/env python3
"""
The lookup's StripReader with REAL light-ocr processes: a live reader (as the server's identification has) and the StripReader's own, side by side.
Checks that (1) a second process really starts and answers, (2) the two work at the same time without waiting for each other, (3) the live reader's
state is untouched, and (4) the second process stops itself when idle (counted as child processes of this one running ocr/server.mjs).

    venv/bin/python -I scripts/experiments/image_match/check_strip_reader.py        (the repository's venv)
"""
import os
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
from card_scanner.core import card_ocr
from card_scanner.core.printing_check import StripReader, read_strip
from common import HOME, read

card = read(next(f for f in (HOME / 'truth' / 'rescan_cards').iterdir() if f.name.startswith('phone-0240eb84_1038')))


def readers():
    """How many light-ocr processes are children of this one"""
    count = 0
    for pid in os.listdir('/proc'):
        if pid.isdigit():
            try:
                stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
                if int(stat[1]) == os.getpid() and b'server.mjs' in Path(f'/proc/{pid}/cmdline').read_bytes():
                    count += 1
            except (OSError, IndexError, ValueError):
                pass
    return count


live = card_ocr.CardOcr()
live.warm_up()
live_answer = read_strip(live, card)
print(f'live reader alone: {live_answer}; light-ocr processes: {readers()}')

shadow = StripReader(idle_stop=3)
t0 = time.time()
shadow_answer = shadow(card)
print(f"the StripReader's own reader: {shadow_answer} in {time.time() - t0:.1f} s (it includes starting the process); light-ocr processes: {readers()}")
print('the two readers agree:', live_answer == shadow_answer)

# both at once: 30 strips each, from two threads
timings = {'live': [], 'shadow': []}


def run(name, function):
    for _ in range(30):
        t = time.time()
        function()
        timings[name].append(time.time() - t)
threads = [threading.Thread(target=run, args=('live', lambda: read_strip(live, card))), threading.Thread(target=run, args=('shadow', lambda: shadow(card)))]
start = time.time()
[t.start() for t in threads]
[t.join() for t in threads]
print(f"30 strips each at the same time in {time.time() - start:.1f} s: live median {1000 * np.median(timings['live']):.0f} ms, "
      f"own reader median {1000 * np.median(timings['shadow']):.0f} ms")
print(f'live reader after it: process alive {live.process is not None and live.process.poll() is None}, cool-down {live._failed_at}')

time.sleep(4.5)                                                   # idle_stop is 3 s
print(f'after 4.5 s idle: light-ocr processes {readers()} (the live one stays, the shadow one stopped)')
print('and it starts again when needed:', shadow(card), f'- processes {readers()}')
shadow.close()
live.stop()
time.sleep(0.5)
print(f'after both are closed: light-ocr processes {readers()}')
