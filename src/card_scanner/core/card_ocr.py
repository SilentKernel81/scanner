#!/usr/bin/env python3
"""
Card OCR Module
Reads a card with light-ocr (PP-OCRv6, offline) before the vision AI is asked: OCR returns the
text lines of the card with their positions, and a parser per game picks the name, collector
number, set code and foil marker from where they are printed.

light-ocr has no Python binding: ocr/server.mjs (Node.js) is started once and kept running, and
answers one request per line over its stdin/stdout.
"""

import base64
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time

import cv2

from card_scanner.config import Config
from card_scanner.paths import TOP, tool

# OCR is logged with the AI: both are "what was read from the card"
logger = logging.getLogger('ai')

OCR_DIR = TOP / 'src' / 'ocr'  # the reader is Node.js, not Python: beside the package, in src/
READER = 'light-ocr'

# Language codes printed after the set code ("HOB • EN")
LANGUAGES = 'EN|DE|FR|IT|ES|SP|PT|JP|JA|KO|KR|RU|CS|CT|ZHS|ZHT|PH'
# "U 0172", "M0128", "M.0010", "P 0003 Play Promo": rarity letter, then 3-4 digits (which
# OCR may read as letters: "O172"), or four digits alone when the letter was missed. Nothing
# looser: a loyalty/mana read as "202米" above the collector line once matched another
# printing of the same card
MAGIC_NUMBER = re.compile(r'(?:[A-Z][. ]?\s*([0-9OoDBIlSZ]{3,4}[a-z]?)|([0-9OoDBIlSZ]{4}[a-z]?))(?=\s|$)')
# Older cards: "123/281 U" - the set total has three digits, which a "4/4 Creature" reminder
# on a transforming card has not
MAGIC_NUMBER_OF_TOTAL = re.compile(r'(\d{1,3})\s*/\s*\d{3}(?!\d)')
# "HOB·EN", "HOB★EN", "SPM *EN", "HOBEN", "SPM·ENLADRAWS" (artist name run into the line)
MAGIC_SET = re.compile(rf'([A-Z0-9]{{3,4}})\s*(?:([^A-Za-z0-9\s])\s*(?:{LANGUAGES})|(?:{LANGUAGES})\b)')
DIGIT_LOOKALIKES = str.maketrans('OoDBIlSZ', '00081152')


def prints_token_letter(match):
    """
    Whether a MAGIC_NUMBER match is a TOKEN's collector line: a token (and an emblem) prints "T" where a card prints its rarity
    ("T 0001" over "FRA • EN"; no card has the rarity "T"), with the set code of the set it came with. Scryfall keeps tokens in a set
    of their own - "t" + that code ("tfra", number 1) - so a token read as "FRA" 1 would be looked up as another card, the first
    one of the set. Only the letter group counts (the digits-alone alternative has no letter).
    """
    return bool(match and match.group(1) and match.group(0).lstrip()[:1] == 'T')


def token_set_code(set_code):
    """The set a token printed with the code `set_code` is filed under: 't' + the code, in the case it was written in (tokens: 'TFRA', the strip: 'tfra')"""
    return ('t' if set_code[:1].islower() else 'T') + set_code
STARS, DOTS = '*★☆', '·•.'


def parse_magic(lines, width, height):
    """
    Name, collector number, set code and foil marker of a Magic card from its OCR lines
    (see ocr/server.mjs for the line format).

    Returns:
        dict: {'name', 'collector_number', 'set_code', 'foil'} or None if no name was found
    """
    def left(line):
        return line['box'][0][0] / width

    def top(line):
        return line['box'][0][1] / height

    # Name: the first line of text in the title bar (the mana cost is to its right)
    title = [l for l in lines if top(l) < 0.16 and left(l) < 0.35 and re.search('[A-Za-z]{3}', l['text'])]
    if not title:
        return None
    # Mana symbols at the end of the title bar come out as stray characters ("Healer **", "Healer 迷")
    name = re.sub(r"[^A-Za-zÀ-ÿ0-9!?'’\".)]+$", '', min(title, key=top)['text']).strip()
    if not name:
        return None

    # Collector line and set line: bottom-left, in that order. 0.84: the outline of a card on
    # a pile may take in the edge of the card underneath, which moves both lines up
    number = set_code = ''
    foil = 'unknown'
    token = False
    for line in sorted((l for l in lines if top(l) > 0.84 and left(l) < 0.6), key=top):
        text = line['text'].strip()
        if not number:
            match = MAGIC_NUMBER.match(text)
            digits = match and (match.group(1) or match.group(2))
            if digits and sum(c.isdigit() for c in digits) >= 2:
                number = digits.translate(DIGIT_LOOKALIKES)
                token = prints_token_letter(match)
                continue
            match = MAGIC_NUMBER_OF_TOTAL.match(text)
            if match:
                number = match.group(1)
                continue
        if not set_code:
            match = MAGIC_SET.match(text)
            if match:
                set_code = match.group(1)
                marker = match.group(2) or ''
                # Only a marker that was read counts: a missed one says nothing (measured:
                # no symbol was read on 52 foils and 3 regular cards)
                foil = 'foil' if marker and marker in STARS else 'non-foil' if marker and marker in DOTS else 'unknown'
    if token and set_code:
        set_code = token_set_code(set_code)       # "T 0001" over "FRA": the token set 'TFRA', not the card FRA #1
    return {'name': name, 'collector_number': number, 'set_code': set_code, 'foil': foil}


def parse_strip(lines):
    """
    The collector number and the set code out of the OCR lines of the bottom-left STRIP of a flat card (foil_marker.strip_of: only the
    collector line and the set line are in it, so no position filter is needed): (number, set_code), each '' when it was not read.
    The number has its leading zeros taken off ('0172' -> '172') and the set code is lower case, as the card database writes them.
    The same reading parse_magic does of those two lines, for the printing check's picture lookup (printing_check.py).
    """
    number = set_code = ''
    token = False
    for line in sorted(lines, key=lambda l: l['box'][0][1]):
        text = line['text'].strip()
        if not number:
            match = MAGIC_NUMBER.match(text)
            digits = match and (match.group(1) or match.group(2))
            if digits and sum(c.isdigit() for c in digits) >= 2:
                number = digits.translate(DIGIT_LOOKALIKES)
                token = prints_token_letter(match)
                continue
            match = MAGIC_NUMBER_OF_TOTAL.match(text)
            if match:
                number = match.group(1)
                continue
        if not set_code:
            match = MAGIC_SET.match(text)
            if match:
                set_code = match.group(1)
    set_code = set_code.lower()
    if token and set_code:
        set_code = token_set_code(set_code)       # a token's strip says "T 0001" over "FRA": the picture's candidate is the printing 'tfra' 1
    return number.lstrip('0'), set_code


# Game id -> parser(lines, width, height); a game without one is read by the vision AI only
PARSERS = {'mtg': parse_magic}


# Words that make an environment variable a secret (or an address that leads to one)
SECRET_WORDS = ('API_KEY', 'SECRET', 'TOKEN', 'PASSWORD', 'PASSWD', 'CREDENTIAL', 'LOCAL_AI_ENDPOINT')


def reader_environment(environment=None):
    """
    What the OCR reader's process is given: this process's environment without the API keys, the
    station token and the like. The reader is Node.js with a third-party package (light-ocr) and
    needs none of them - load_saved_keys() puts every key into os.environ, and a child inherits all
    of it unless told otherwise. (Outside code review, 2026-10-16.)
    """
    environment = os.environ if environment is None else environment
    return {name: value for name, value in environment.items()
            if not any(word in name.upper() for word in SECRET_WORDS)}


class CardOcr:
    """Runs the light-ocr reader process and parses what it reads"""

    START_TIMEOUT = 30    # seconds for the models to load
    READ_TIMEOUT = 10     # seconds per card (measured: 0.16 s on a GPU, 0.8 s on a desktop CPU)
    RETRY_AFTER = 60      # seconds before a failed reader is started again

    def __init__(self, log_callback=None):
        self.log_callback = log_callback
        self.process = None
        self.provider = None
        self._answers = queue.Queue()
        self._lock = threading.Lock()   # one request at a time; also guards start/stop
        self._next_id = 0
        self._failed_at = None

    def log(self, message, level="info"):
        getattr(logger, level, logger.info)(message)
        if self.log_callback:
            self.log_callback(message, level)

    @staticmethod
    def installed():
        """Whether Node.js and the light-ocr package (npm install in src/ocr/) are present"""
        return bool(tool('node')) and (OCR_DIR / 'node_modules' / '@arcships' / 'light-ocr').is_dir()

    def supports(self, game_id):
        return game_id in PARSERS

    def warm_up(self):
        """Start the reader in the background, so the first card doesn't wait for the models"""
        if self.installed():
            threading.Thread(target=self._ensure_started_locked, daemon=True).start()

    def _ensure_started_locked(self):
        with self._lock:
            return self._ensure_started()

    def _ensure_started(self):
        if self.process and self.process.poll() is None:
            return True
        if self._failed_at and time.time() - self._failed_at < self.RETRY_AFTER:
            return False
        if not self.installed():
            return False

        self._stop()
        log_dir = Config.DATA_DIR / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(log_dir / 'ocr.log', 'ab') as errors:
                self.process = subprocess.Popen(
                    [tool('node'), 'server.mjs', Config.OCR_PROVIDER], cwd=str(OCR_DIR), env=reader_environment(),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True)
        except OSError as e:
            return self._failed(f"light-ocr could not be started: {e}")
        self._answers = queue.Queue()
        threading.Thread(target=self._read_answers, args=(self.process, self._answers), daemon=True).start()

        try:
            ready = self._answers.get(timeout=self.START_TIMEOUT)
        except queue.Empty:
            ready = None
        if not ready or not ready.get('ready'):
            return self._failed(f"light-ocr did not start (needs Node.js 22 or newer; see {Config.shown('logs', 'ocr.log')})")
        self.provider = ready.get('provider')
        self._failed_at = None
        self.log(f"light-ocr ready ({self.provider or 'unknown provider'})")
        return True

    @staticmethod
    def _read_answers(process, answers):
        """Reader thread: one JSON answer per line, None when the process ends"""
        for line in process.stdout:
            try:
                answers.put(json.loads(line))
            except ValueError:
                pass
        answers.put(None)

    def _failed(self, message):
        self.log(message, level="warning")
        self._stop()
        self._failed_at = time.time()
        return False

    def _stop(self):
        if self.process:
            try:
                self.process.stdin.close()
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                self.process.kill()
            self.process = None

    def stop(self):
        with self._lock:
            self._stop()

    def read_lines(self, image_rgb):
        """Text lines of an RGB image: [{'text', 'confidence', 'box'}], or None if OCR failed"""
        encoded, jpeg = cv2.imencode('.jpg', cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not encoded:
            return None
        with self._lock:
            if not self._ensure_started():
                return None
            self._next_id += 1
            request = {'id': self._next_id, 'image': base64.b64encode(jpeg.tobytes()).decode('ascii')}
            try:
                self.process.stdin.write(json.dumps(request) + '\n')
                self.process.stdin.flush()
                deadline = time.time() + self.READ_TIMEOUT
                while True:
                    answer = self._answers.get(timeout=max(0.1, deadline - time.time()))
                    if answer is None:
                        return self._failed(f"light-ocr stopped (see {Config.shown('logs', 'ocr.log')})") or None
                    if answer.get('id') == self._next_id:
                        break
            except (OSError, ValueError, queue.Empty) as e:
                return self._failed(f"light-ocr did not answer: {e or 'timed out'}") or None
        if 'error' in answer:
            self.log(f"light-ocr could not read the image: {answer['error']}", level="warning")
            return None
        return answer['lines']

    def read_card(self, image_rgb, game_id):
        """
        What OCR read on a card of the given game.

        Returns:
            dict: {'name', 'collector_number', 'set_code', 'foil', 'processing_time', 'reader'}
            or None (OCR not available, no parser for the game, or no name found)
        """
        parser = PARSERS.get(game_id)
        if not parser:
            return None
        start_time = time.time()
        lines = self.read_lines(image_rgb)
        if lines is None:
            return None
        height, width = image_rgb.shape[:2]
        result = parser(lines, width, height)
        elapsed = time.time() - start_time
        if not result:
            self.log(f"light-ocr found no card name ({elapsed:.2f}s)", level="warning")
            return None
        result.update(processing_time=round(elapsed, 2), reader=READER)
        self.log(f"light-ocr read: '{result['name']}' #{result['collector_number'] or 'not found'} "
                 f"[{result['set_code'] or 'set not found'}] foil marker: {result['foil']} ({elapsed:.2f}s)")
        return result
