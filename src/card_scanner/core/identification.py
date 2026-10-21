#!/usr/bin/env python3
"""
Card identification, shared by every camera: light-ocr reads each card first, and the vision
AI is asked only when that read isn't a confirmed match (measured on 747 scans: OCR alone
confirmed 82% in 0.16 s each, the AI 95% in ~1 s; OCR first with the AI behind it 95%).

Two stages, each with its own queue (submit): one OCR worker - the reader takes one request at
a time, ~0.1 s per card on a GPU - then several AI workers for what OCR couldn't settle. A card
OCR confirms never waits behind a slow AI answer (full-art cards, which only the AI reads, take
the whole AI stage). What was read is handed back by one more thread, one card at a time, so
looking a card up and adding it holds up neither stage. identify() runs both stages in the
caller's thread.

reading.mode (config.yaml, data/settings.json): 'cascade' is what is described above. 'evidence' puts
the picture first (printing_check.live_read): a fingerprint of the picture matched against every
printing, and the collector line read from its strip when OCR is on, decide a card by themselves; only
when they do not is the full-card OCR read (a confirmed read stands, asked one question: is the
picture another printing of the same card?), and the vision AI is last, its answer checked
against the picture. Phase 3 of
docs/plans/READING_REFACTOR_PLAN.md; the default since 2026-10-20 - the vision AI was only ever there because OCR was weak, so it is the last step.
"""

import contextlib
import logging
import queue
import threading
import time

from card_scanner import games
from card_scanner.core import foil_marker, prompts
from card_scanner.core.card_identifier import CardIdentifier
from card_scanner.core.card_ocr import CardOcr
from card_scanner.core.printing_check import EVIDENCE_READER, PICTURE_READER, name_only_partly_read, ocr_agrees, prints_a_marker
from card_scanner.core.reading import capture as reading_capture, record as reading_record
from card_scanner.config import Config

logger = logging.getLogger('ai')

# What the AI stage is asked for a card
IDENTIFY, FOIL = 'identify', 'foil'

# How a card is read (reading.mode)
CASCADE, EVIDENCE = 'cascade', 'evidence'


class Job:
    """A capture on its way through the two stages"""

    def __init__(self, image, foil_image, game_id, on_done, scope=None):
        self.image = image
        self.foil_image = foil_image
        self.game_id = game_id  # the game being scanned at capture: not read as another game's card
        self.on_done = on_done  # on_done(card_info or None, seconds since submit)
        # scope(): a context manager the job's work runs in - the server makes the log lines go
        # to the page of the camera the capture came from
        self.scope = scope or contextlib.nullcontext
        self.submitted = time.time()
        self.ocr_info = None
        self.task = None  # IDENTIFY or FOIL, once OCR has passed it on
        self.record = None  # what its reading saw and did (core/reading/record.py), when the reading log is on
        # what its reading has worked out about its picture (core/reading/capture.py: the picture search, the strip, the full-card OCR), so that the
        # AI stage does not search the picture again; let go of when it is finished
        self.capture = None


class Identification:
    """The OCR reader, the vision AI and the queues in front of them"""

    # Whether the vision AI is asked at all (the Settings switch, remembered as 'ai_enabled'), and the AI itself; defaults here for an Identification made without __init__ (tests of one stage)
    ai_enabled = True
    card_identifier = None

    # Set by the server once the printing check exists (see __init__); a default here so that an Identification made without __init__ (tests of
    # one stage) reads in 'cascade' mode like any other
    evidence = None

    # The reading log (core/reading/record.ReadingLog: data/logs/reading.jsonl), set by the server; None: nothing is recorded (tests of one stage, scripts)
    reading_log = None

    def __init__(self, settings, log_callback=None, ai_workers=None):
        self.settings = settings
        self.log_callback = log_callback
        self.ai_workers = max(1, int(ai_workers or Config.VISION_AI_WORKERS))
        self.last_capture = None  # (card image RGB, foil image or None) last given - prompt editor tests

        self.card_identifier = None
        if Config.VISION_AI_ENABLED:
            # Load saved provider and model from settings
            saved_provider = settings.get_ai_provider()
            saved_model = settings.get_ai_model()
            try:
                self.card_identifier = CardIdentifier(provider=saved_provider, model=saved_model,
                                                      log_callback=log_callback)
                self.log(f"Initialized AI with saved settings: {saved_provider} ({saved_model or 'default'})")
            except Exception as e:
                # Fallback to config default if saved settings fail
                self.log(f"Failed to load saved AI settings, using config defaults: {e}", level="warning")
                try:
                    self.card_identifier = CardIdentifier(provider=Config.VISION_AI_PROVIDER, log_callback=log_callback)
                    self.log(f"Vision AI enabled ({Config.VISION_AI_PROVIDER})", level="success")
                except Exception as e:
                    self.log(f"Vision AI initialization failed: {e}", level="warning")
                    self.log("Continuing without AI identification", level="warning")

        self.card_ocr = CardOcr(log_callback=log_callback)
        # The small network that reads the foil marker where the OCR could not (None: switched off
        # in config.yaml, or its file did not load - the OCR's marker and the AI's check as before)
        self.foil_marker = foil_marker.load(Config.OCR_FOIL_MARKER)
        self.ocr_enabled = bool(settings.get('ocr_first', True))
        if self.ocr_enabled:
            self.card_ocr.warm_up()
        self.ai_enabled = bool(settings.get('ai_enabled', True))

        # The printing check (core/printing_check.py: the image index and the reading by picture and strip), given by the server after both exist;
        # None: reading.mode 'evidence' has nothing to read with and every card is read as in 'cascade'
        self.evidence = None
        self.reading_log = None     # data/logs/reading.jsonl, given by the server (startup.py)

        self._ocr_queue = queue.Queue()
        self._ai_queue = queue.Queue()
        self._done_queue = queue.Queue()
        self._threads = []
        self._running = False

    @property
    def reading_mode(self):
        """'cascade' or 'evidence' (reading.mode; the person's data/settings.json 'reading_mode' wins over config.yaml; anything else is cascade)"""
        settings = getattr(self, 'settings', None)
        mode = str((settings.get('reading_mode', Config.READING_MODE) if settings else Config.READING_MODE) or '').strip().lower()
        return EVIDENCE if mode == EVIDENCE else CASCADE

    @property
    def accept_image_alone(self):
        """Whether a card only the picture decided may be added (reading.accept_image_alone; the person's 'reading_accept_image_alone' in data/settings.json wins)"""
        settings = getattr(self, 'settings', None)
        value = settings.get('reading_accept_image_alone', Config.READING_ACCEPT_IMAGE_ALONE) if settings else Config.READING_ACCEPT_IMAGE_ALONE
        return value is True

    def log(self, message, level="info"):
        """Send log message to both file logger and UI callback"""
        getattr(logger, 'info' if level == 'success' else level, logger.info)(message)
        if self.log_callback:
            self.log_callback(message, level)

    def ai_info(self):
        """'provider/model' of the vision AI, or None without one"""
        identifier = self.card_identifier
        return f"{identifier.provider}/{identifier.model}" if identifier else None

    def set_ai_provider(self, provider, model=None):
        """
        Dynamically change the AI provider and/or model for card identification

        Args:
            provider: 'gemini', 'openai', 'anthropic' or 'local'
            model: Specific model to use (optional, uses default if not specified)

        Returns:
            dict: {'success': bool, 'message': str, 'model': str}
        """
        provider = provider.lower()
        valid_providers = list(CardIdentifier.AVAILABLE_MODELS.keys())

        if provider not in valid_providers:
            return {
                'success': False,
                'message': f"Invalid provider '{provider}'. Must be one of: {', '.join(valid_providers)}"
            }

        try:
            new_identifier = CardIdentifier(provider=provider, model=model, log_callback=self.log_callback)

            # If successful, replace the old identifier
            self.card_identifier = new_identifier
            new_identifier.warm_up()

            # Save the selection to settings for persistence
            self.settings.set_ai_provider(provider, model)

            # Update the config for consistency (optional, doesn't persist across restarts)
            Config.VISION_AI_PROVIDER = provider

            message = f"AI provider changed to {provider} ({new_identifier.model})"
            self.log(message, level="success")
            return {'success': True, 'message': message, 'model': new_identifier.model}
        except ValueError as e:
            # API key not found or invalid model
            self.log(f"Failed to change AI provider: {e}", level="error")
            return {'success': False, 'message': str(e)}
        except Exception as e:
            self.log(f"Error changing AI provider: {e}", level="error")
            return {'success': False, 'message': f"Error: {str(e)}"}

    def _new_record(self):
        """
        A record of one capture's reading for the reading log (core/reading/record.py), with the configuration it is read in - or None without a log.
        Never raises: a reading log that cannot be started is no reason to lose the card.
        """
        if self.reading_log is None:
            return None
        try:
            return self.reading_log.new(mode=self.reading_mode, ocr_on=bool(self.ocr_enabled), ai_on=self._ai() is not None,
                                        accept_image_alone=self.accept_image_alone, game=games.active_id())
        except Exception as e:
            logger.debug(f"Reading log: no record for this capture ({e})")
            return None

    def _ai(self):
        """The vision AI's identifier - or None when there is none or the person switched it off: every place that would ask it asks this, so the one switch covers the identification, the foil check and a card already in the queue"""
        return self.card_identifier if self.ai_enabled else None

    def set_ai_enabled(self, enabled):
        """Ask the vision AI at all (remembered). Off: what OCR and the picture cannot decide goes to the review queue under the name OCR read; the provider and its key are kept"""
        self.ai_enabled = bool(enabled)
        self.settings.set('ai_enabled', self.ai_enabled)

    def set_ocr_enabled(self, enabled):
        """Read cards with light-ocr before asking the vision AI (remembered)"""
        self.ocr_enabled = enabled
        self.settings.set('ocr_first', enabled)
        if enabled:
            self.card_ocr.warm_up()
        else:
            self.card_ocr.stop()
        self.log(f"Read cards with OCR first: {'on' if enabled else 'off'}")

    # ------------------------------------------------------------------------
    # The two stages
    # ------------------------------------------------------------------------

    def _ai_foil_check(self, foil_image):
        """
        Whether the AI can be asked about the star/dot foil marker: only on a perspective-
        corrected card (the marker's corner is at a known position)
        """
        return bool(self._ai() and foil_image is not None and Config.VISION_AI_DETECT_FOIL
                    and prompts.has('foil', games.active_id()))

    def _read_foil_locally(self, image, foil_image=None):
        """
        The foil marker read by the small network (core/foil_marker.py) from the set line of the
        flat card: 'foil', 'non-foil', or 'unknown' - not sure, no set line found, or no network.
        With OCR off nothing finds the set line, so the network is asked about it at its fixed place on the flat card (foil_marker.FIXED_BOX; a little less
        sure than at the OCR's own box, and without the OCR's own symbol to stand first) - after the picture search has said which way up the card is, and 'unknown' when it cannot
        (no printing check, no index, an ambiguous picture): the AI's check follows. Never raises: a failure here is not
        a reason to lose the card, the OCR's marker and the AI's check still follow.
        Which picture: the network cuts the strip where a FLAT card has its set line, so it reads
        foil_image - the perspective-corrected card, the same picture the AI's foil check gets - when
        there is one; image is the card itself only when no separate foil picture was made (a fixed
        area's `image` is the area as drawn, not a flat card: stations_api.station_capture).
        """
        card = foil_image if foil_image is not None else image
        if self.foil_marker is None or card is None:
            return 'unknown'
        try:
            # the strip live_read read for this capture (reading.mode 'evidence', OCR on): its symbol and the network's answer are already there - for the card turned
            # UPRIGHT, where foil_marker.read reads it as given (an upside-down card gave 'unknown' and the AI's check). Kept only for this very picture
            kept, strip = reading_capture.recall('strip', card) if self.ocr_enabled else (False, None)
            if kept:
                foil, probability = self._foil_of_strip(strip)
            elif self.ocr_enabled:
                foil, probability = self.foil_marker.read(card, self.card_ocr)
            else:
                # no OCR to find the set line: the network is asked about it at its fixed place (foil_marker.FIXED_BOX), which beats the AI's 25 s on a cold model - but only for a
                # card whose orientation is established AND that prints a marker at all (an upside-down card would feed it art and title pixels, a card of an older frame whatever sits in the box:
                # a confident wrong answer either way - Codex reviews, 2026-10-20)
                upright = self.evidence.upright(card, needs_marker=True) if self.evidence is not None else None
                if upright is None:
                    return 'unknown'
                foil, probability = self.foil_marker.read_fixed(upright)
        except Exception as e:
            self.log(f"Foil marker network failed ({e}) - left to the AI's check", level="warning")
            return 'unknown'
        if foil != 'unknown':
            # probability None: the strip's own OCR read the symbol; otherwise the network's answer
            how = 'by OCR on the strip' if probability is None else f'by the network ({probability:.2f})'
            self.log(f"Foil marker read {how}: {foil}")
        return foil

    @staticmethod
    def _foil_of_strip(strip):
        """
        The marker from the strip's evidence (printing_check.read_strip_evidence), exactly as foil_marker.read answers from the same read: the symbol the OCR read
        stands (probability None); otherwise the network's probability, believed one way or the other at foil_marker.SURE; nothing: 'unknown'
        """
        if strip['symbol'] != 'unknown':
            return strip['symbol'], None
        if strip['net'] is None:
            return 'unknown', None
        return foil_marker.answer_for(strip['net'])

    def _read_card(self, image):
        """
        The full-card OCR of a capture - ONE read per capture (core/reading/capture.py): _ocr_names (the picture alone's confirmation) and _read_with_ocr ask about
        the same image, and the second ask is the first's answer. A copy each time: the caller changes the read (its foil, its number and set).
        """
        read = reading_capture.get('ocr', image, lambda: self.card_ocr.read_card(image, games.active_id()))
        return dict(read) if read else read

    @staticmethod
    def _said_by_ocr(ocr_info):
        how = {EVIDENCE_READER: 'picture and strip', PICTURE_READER: 'picture'}.get(ocr_info.get('reader'), 'OCR')
        return f"✓ Card identified by {how}: {ocr_info['name']} #{ocr_info['collector_number']}"

    def _read_by_picture(self, image, foil_image):
        """
        reading.mode 'evidence': the card read by its picture and its strip (printing_check.live_read) - the FIRST step of _read_with_ocr - (card_info, what the AI is still needed
        for) like _read_with_ocr, or None when they do not decide (review, no image index, or any failure): the card then goes on to the full-card OCR
        and, if that does not settle it, to the AI as in 'cascade'. Never loses a card: whatever goes wrong here is a reason to ask the AI, which is what would have happened anyway.
        With OCR switched off there is no strip (the reader is not handed over, so it is not run): only the picture alone can decide, and only when the
        person allowed it (accept_image_alone, which is on by default) - otherwise the card goes to the AI at once. Switching OCR off to try the picture alone is the use of this.
        """
        if self.reading_mode != EVIDENCE or self.evidence is None:
            return None
        try:
            # The picture alone may add a card only while the index covers the whole card data (PrintingCheck.missing_printings == 0): after a card data update and before the
            # index is extended, a new printing is not in it, and its best match would be another printing - then only the collector line (or OCR, the AI, review) decides
            missing = self.evidence.missing_printings() if self.accept_image_alone else None
            alone = self.accept_image_alone and missing == 0
            # the reading log: whether the index covers the card data (None: not asked - the picture alone is off - or it cannot be said)
            reading_record.note(None, index_covers=None if missing is None else missing == 0, index_missing=missing)
            with reading_record.timed('picture'):
                reading = self.evidence.live_read(image, foil_image, self.card_ocr if self.ocr_enabled else None, self.foil_marker, alone)
        except Exception as e:
            self.log(f"Reading by picture failed ({e}) - asking the vision AI", level="warning")
            reading_record.note(None, picture_error=str(e))
            return None
        if reading is None:
            if reading_record.refused() == reading_record.IMAGE_ALONE_OFF and self.accept_image_alone:
                # live_read was told not to take the picture alone - here because the index lacks printings of the card data, not because the person said no
                reading_record.refuse(reading_record.INDEX_INCOMPLETE, missing=missing)
            return None
        card = reading['card']
        if reading['path'] == 'image' and self.ocr_enabled:
            # The picture ALONE decided (the collector line did not name the printing it chose): with OCR on, its name must agree before the card is added - whatever the strip
            # said. A card newer than the card data is in neither the data nor the index, and its best match is another card's printing (a third Codex review of 2026-10-20);
            # and a strip naming ANOTHER existing printing is a contradiction the picture alone must not overrule unconfirmed (a fourth). Not agreeing: the card is in doubt,
            # and the OCR path below reads it as any other.
            if not self._ocr_names(image, card):
                self.log(f"The picture chose {card['name']} [{card['set_code']} {card['number']}] alone, and OCR did not read that printing - read by OCR instead")
                return None
        reading_record.note('decision', accepted=True)
        # The finish, as the 'foil' field the server's finish rule (Game.suggested_finish) has always taken: an unread marker stays 'unknown'
        foil = {'foil': 'foil', 'regular': 'non-foil'}.get(reading['finish']['finish'], 'unknown')
        info = {'name': card['name'], 'collector_number': card['number'], 'set_code': card['set_code'], 'foil': foil,
                'printing_id': card['id'], 'reader': EVIDENCE_READER if reading['path'] == 'strong' else PICTURE_READER}
        self.log(f"Picture and strip: {card['name']} [{card['set_code']} {card['number']}] ({reading['path']}, "
                 f"{'strip ' + reading['strip']['set'] + ' ' + reading['strip']['number'] if reading['strip']['number'] else 'no strip'})")
        if foil == 'unknown' and self._ai_foil_check(foil_image):
            return info, FOIL       # as for an OCR-confirmed card whose marker nothing read: the AI's foil check
        self.log(self._said_by_ocr(info), level="success")
        return info, None

    def _ocr_names(self, image, card):
        """
        Whether the full-card OCR confirms the printing the picture alone chose (printing_check.ocr_agrees): the same name, and nothing of the collector line it read naming
        another printing - not a set code or a number that are not the card's. False when it reads nothing, contradicts it, or fails.
        """
        try:
            with reading_record.timed('ocr'):
                read = self._read_card(image)
            printing_at = self.evidence.database.get_card_by_set_number       # what the OCR's collector line names (ocr_agrees looks it up)
        except Exception as e:
            self.log(f"OCR could not confirm the picture ({e})", level="warning")
            reading_record.refuse(reading_record.OCR_DID_NOT_CONFIRM, ocr_name=None, ocr_line=None, ocr_error=str(e))
            return False
        agrees = ocr_agrees(read, card, printing_at)
        if not agrees:
            # the reading log: what the OCR read instead - nothing (None: the reader read nothing, or was down in its cool-down), another name, or a line naming another printing
            line = f"{read.get('set_code') or ''} {read.get('collector_number') or ''}".strip() if read else None
            reading_record.refuse(reading_record.OCR_DID_NOT_CONFIRM, ocr_name=read.get('name') if read else None, ocr_line=line or None)
        return agrees

    def _contradicted_by_picture(self, image, foil_image, ocr_info, ocr_card):
        """
        reading.mode 'evidence': whether the picture clearly looks like another printing of the card the OCR read (printing_check.contradicts: the Secret
        Lair case, 6 of 10,568 correctly read cards). Fails open to the OCR: no printing check, no index, a failure - the OCR's read stands.
        """
        if self.reading_mode != EVIDENCE or self.evidence is None or not ocr_card or not ocr_card.get('id'):
            return False
        picture = foil_image if foil_image is not None else image
        try:
            contradicted = self.evidence.contradicts(picture, ocr_card['id'])
            why = 'another printing of the card'
            if not contradicted and name_only_partly_read(ocr_info['name'], ocr_card):
                # The name was read only in part ("Lightning" of "Lightning Strike"), so the printed set + number alone confirmed the card - and a Secret
                # Lair prints another card's set + number. Here, and only here, a picture that looks like something else is believed.
                contradicted = self.evidence.disagrees(picture, ocr_card['id'])
                why = 'a different card, and the name on the card was only partly read'
        except Exception as e:
            self.log(f"The OCR read could not be checked against the picture ({e})", level="warning")
            return False
        reading_record.note('ocr', contradicted=bool(contradicted), why=why if contradicted else None)
        if contradicted:
            self.log(f"The OCR read {ocr_info['name']} [{ocr_info['set_code']} {ocr_info['collector_number']}] but the picture looks like "
                     f"{why} - not taken as a confirmed read", level="warning")
        return contradicted

    def _checked_against_picture(self, image, card_info):
        """
        reading.mode 'evidence': the vision AI's answer against the picture. An answer whose printing the picture clearly does NOT look like (the
        check's 'disagree': the AI misread about 12% of the cards it was asked about in the experiment) is not added: its number and set are dropped,
        so the card goes to the review queue under the name the AI gave. Any other verdict, no index, or a failure here leaves the answer as it was.
        """
        if self.reading_mode != EVIDENCE or self.evidence is None or not card_info or not card_info.get('name'):
            return card_info
        try:
            found = games.active().identify(card_info['name'], card_info.get('collector_number'), card_info.get('set_code'))
            if not found or not found.get('id'):
                reading_record.note('ai', verdict='not_found')
                return card_info
            verdict = self.evidence.verdict_of(image, found['id'])
            reading_record.note('ai', verdict=verdict)
            if verdict != 'disagree':
                return card_info
        except Exception as e:
            self.log(f"The AI's answer could not be checked against the picture ({e})", level="warning")
            return card_info
        self.log(f"The AI's answer {card_info['name']} [{card_info.get('set_code') or '?'} {card_info.get('collector_number') or '?'}] "
                 f"does not look like the picture - sent to review", level="warning")
        return {**card_info, 'collector_number': '', 'set_code': ''}

    def _read_with_ocr(self, image, foil_image):
        """
        The reading before the AI. Returns (ocr_info or None, what the AI is still needed for): None when the card is settled - the picture decided it, OCR's read is a confirmed
        match, or there is no vision AI to ask.
        Order (reading.mode 'evidence', the default): the PICTURE first - with the collector line read from its strip when OCR is on - and a card it settles is settled; in doubt the
        full-card OCR reads it (when OCR is on); what is still open goes to the vision AI (when it is on) and otherwise to review. OCR off and AI off: the picture alone, then review.
        Mode 'cascade' (and a server without an image index): OCR, then the AI, as before the picture matching existed.
        """
        by_picture = self._read_by_picture(image, foil_image)       # None: not in evidence mode, no index, or the picture does not settle it
        if by_picture:
            return by_picture
        ocr_info = None
        if self.ocr_enabled:
            with reading_record.timed('ocr'):
                ocr_info = self._read_card(image)      # (read already when the picture alone asked it to confirm: not read again)
            confirmed, ocr_card = False, None
            if ocr_info:
                if self.reading_mode == EVIDENCE and self.evidence is not None:
                    # (no printing check - a server without the image index machinery, a test of one stage - is the cascade, whatever the setting says)
                    # the same look-up as confirmed_read, keeping the card: its printing is what the picture is asked about
                    ocr_card = games.active().confirmed_card(ocr_info['name'], ocr_info['collector_number'], ocr_info['set_code'])
                    confirmed = ocr_card is not None
                else:
                    confirmed = games.active().confirmed_read(ocr_info['name'], ocr_info['collector_number'], ocr_info['set_code'])
            # the reading log: the full-card read (before a contradiction below drops its number and set)
            reading_record.note('ocr', read=bool(ocr_info), name=(ocr_info or {}).get('name'), collector_number=(ocr_info or {}).get('collector_number'),
                                set_code=(ocr_info or {}).get('set_code'), foil=(ocr_info or {}).get('foil'), confirmed=bool(confirmed))
            if confirmed and self._contradicted_by_picture(image, foil_image, ocr_info, ocr_card):
                # reading.mode 'evidence': the OCR read a printing the picture clearly is another printing of (Secret Lair). It is no longer a
                # confirmed read: without its number and set it can only go to review under its name, if nothing below settles it
                ocr_info = {**ocr_info, 'collector_number': '', 'set_code': ''}
                confirmed = False
            if confirmed:
                # OCR misses the marker on about a third of the foils: the small network reads it
                # from the set line first; only what it is not sure of is asked of the AI
                if ocr_info['foil'] == 'unknown':
                    ocr_info['foil'] = self._read_foil_locally(image, foil_image)
                if ocr_info['foil'] == 'unknown' and self._ai_foil_check(foil_image):
                    return ocr_info, FOIL
                self.log(self._said_by_ocr(ocr_info), level="success")
                return ocr_info, None
            if ocr_info and self._ai():
                self.log("OCR read is not a confirmed match - asking the vision AI")

        if self._ai():
            return ocr_info, IDENTIFY
        if not ocr_info:
            if self.card_identifier:      # there is one, and it is switched off
                self.log("Vision AI is switched off - this card goes to the review queue", level="warning")
            else:
                self.log("⚠ Vision AI not enabled - set API key to enable automatic identification", level="warning")
        return ocr_info, None

    def _ask_ai(self, task, image, foil_image, ocr_info):
        """AI stage: the foil marker of a card OCR confirmed, or the whole identification"""
        identifier = self._ai()  # a provider change swaps it; this card stays with one (None: none, or switched off while this card waited in the queue)
        if identifier is None:
            return ocr_info

        reading_record.note('ai', asked=task)
        if task == FOIL:
            with reading_record.timed('ai'):
                ocr_info['foil'] = identifier.read_foil_symbol(foil_image)
            reading_record.note('ai', answer={'foil': ocr_info['foil']})
            self.log(self._said_by_ocr(ocr_info), level="success")
            return ocr_info

        self.log("Identifying card with Vision AI...")
        # The foil check runs alongside the identification (saves ~0.3 s per card with
        # Ollama, more with cloud providers that serve requests in parallel)
        foil_result = {}
        foil_thread = None
        provisional = False
        if not ocr_info or ocr_info['foil'] == 'unknown':
            # the small network first (a card OCR confirmed was tried in the OCR stage already
            # and comes here as FOIL, not IDENTIFY, so this does not read the same set line twice)
            local = self._read_foil_locally(image, foil_image)
            if local != 'unknown':
                foil_result['foil'] = local
                # With OCR off that answer is the network at a FIXED place, judged fit by the nearest INDEXED printings - a card the index lacks can borrow a modern lookalike's
                # marker layout. So it stands only until the AI says which printing this really is (a Codex review, 2026-10-20): see below.
                provisional = not self.ocr_enabled
        if 'foil' not in foil_result and self._ai_foil_check(foil_image) and (not ocr_info or ocr_info['foil'] == 'unknown'):
            foil_thread = threading.Thread(
                target=lambda: foil_result.update(foil=identifier.read_foil_symbol(foil_image)),
                daemon=True)
            foil_thread.start()
        with reading_record.timed('ai'):
            card_info = identifier.identify_card(image)
        reading_record.note('ai', answer={'name': card_info.get('name'), 'collector_number': card_info.get('collector_number'),
                                          'set_code': card_info.get('set_code')} if card_info else None)
        if provisional and not self._resolved_prints_a_marker(card_info):
            # The printing the AI named is not one that prints a marker (or cannot be found): the network's answer was about whatever sat in the box. Discarded - the AI's own check answers.
            foil_result.pop('foil', None)
            if self._ai_foil_check(foil_image):
                foil_result['foil'] = identifier.read_foil_symbol(foil_image)
        if foil_thread:
            foil_thread.join(timeout=60)
        # A marker OCR did read stands (it agreed with the AI on 480 of 481 scans)
        foil = foil_result.get('foil') or (ocr_info or {}).get('foil') or 'unknown'

        if card_info and card_info.get('name'):
            name = card_info['name']
            number = card_info.get('collector_number', '')
            if number:
                self.log(f"✓ Card identified: {name} #{number}", level="success")
            else:
                self.log(f"✓ Card identified: {name} (no collector number)", level="success")
            card_info['foil'] = foil
            # (the flat card, like the reading by picture: a fixed area's `image` is the area as drawn, which the index never saw)
            return self._checked_against_picture(foil_image if foil_image is not None else image, card_info)

        self.log("Vision AI could not identify card", level="warning")
        if ocr_info:
            # Better than nothing: the name OCR read still finds the card for review
            ocr_info['foil'] = foil
            return ocr_info
        return card_info

    @staticmethod
    def _resolved_prints_a_marker(card_info):
        """Whether the printing the AI's answer resolves to prints a foil marker (printing_check.prints_a_marker); False for no answer, no number, no such printing, or a lookup that fails"""
        if not card_info or not card_info.get('name'):
            return False
        try:
            card = games.active().confirmed_card(card_info['name'], card_info.get('collector_number', ''), card_info.get('set_code', ''))
        except Exception:
            return False
        return prints_a_marker(card)

    def identify(self, image, foil_image=None):
        """
        Identify a card from a preprocessed RGB image, in the caller's thread.
        foil_image: perspective-corrected card to read the star/dot foil marker from (its
        corner is at a known position); no AI foil check without it.
        Returns: card_info dict with name, collector_number, set_code, foil
        ('foil'|'non-foil'|'unknown'), and 'reader' when it wasn't the vision AI that read the
        card - or None
        """
        self.last_capture = (image, foil_image)
        record = self._new_record()
        with reading_record.using(record), reading_capture.using(reading_capture.CaptureEvidence()):
            ocr_info, task = self._read_with_ocr(image, foil_image)
            card_info = self._ask_ai(task, image, foil_image, ocr_info) if task else ocr_info
        if record is not None:
            # the caller routes the card next, in this thread (stations_api: announce_and_route), and completes the record with what became of it
            record.finish(card_info)
            reading_record.hand_over(record)
        return card_info

    # ------------------------------------------------------------------------
    # Queues
    # ------------------------------------------------------------------------

    def start(self):
        """Start the OCR worker and the AI workers"""
        if self._running:
            return
        self._running = True
        self._threads = [threading.Thread(target=self._ocr_worker, daemon=True, name="OCR-Worker")]
        self._threads += [threading.Thread(target=self._ai_worker, daemon=True, name=f"AI-Worker-{n + 1}")
                          for n in range(self.ai_workers)]
        self._threads.append(threading.Thread(target=self._done_worker, daemon=True, name="Identified-Worker"))
        for thread in self._threads:
            thread.start()
        logger.info(f"Identification started: 1 OCR worker, {self.ai_workers} AI workers")

    def stop(self):
        self._running = False
        self.card_ocr.stop()

    def submit(self, image, foil_image, game_id, on_done, scope=None):
        """
        Queue a capture. on_done(card_info or None, seconds) is called exactly once, from the
        one thread that hands results back - in the order cards are settled, not submitted.
        """
        self.last_capture = (image, foil_image)
        self._ocr_queue.put(Job(image, foil_image, game_id, on_done, scope))

    def waiting(self):
        """(captures waiting for OCR, captures waiting for the AI) - not counting those being read"""
        return self._ocr_queue.qsize(), self._ai_queue.qsize()

    def _finish(self, job, card_info):
        job.capture = None          # the picture search's distances to the whole index: not kept while the card waits to be routed
        self._done_queue.put((job, card_info, time.time() - job.submitted))

    def _done_worker(self):
        while self._running:
            done = self._next(self._done_queue)
            if done is None:
                continue
            job, card_info, seconds = done
            if job.record is not None:
                # what was read; on_done (the server's router, captures.announce_and_route) adds what became of it and writes it - or, if it does not, it is written below
                job.record.finish(card_info)
                reading_record.hand_over(job.record)
            try:
                job.on_done(card_info, seconds)
            except Exception as e:
                logger.exception(f"Handling an identified card failed: {e}")
            finally:
                reading_record.flush_handed()

    def _next(self, jobs):
        """The next job of a queue, or None once a second (so the worker sees a stop)"""
        try:
            return jobs.get(timeout=1.0)
        except queue.Empty:
            return None

    def _ocr_worker(self):
        while self._running:
            job = self._next(self._ocr_queue)
            if job is None:
                continue
            try:
                if job.game_id != games.active_id():
                    # The game was switched while this capture waited: not read as a card of
                    # the other game (the caller keeps it for its own game's review)
                    self._finish(job, None)
                    continue
                job.record = self._new_record()
                job.capture = reading_capture.CaptureEvidence()
                with job.scope(), reading_record.using(job.record), reading_capture.using(job.capture):
                    job.ocr_info, job.task = self._read_with_ocr(job.image, job.foil_image)
                if job.task:
                    self._ai_queue.put(job)
                else:
                    self._finish(job, job.ocr_info)
            except Exception as e:
                logger.exception(f"OCR stage failed: {e}")
                self.log(f"OCR error: {e} - asking the vision AI", level="warning")
                job.ocr_info, job.task = None, IDENTIFY
                self._ai_queue.put(job)

    def _ai_worker(self):
        while self._running:
            job = self._next(self._ai_queue)
            if job is None:
                continue
            card_info = None
            try:
                if job.game_id == games.active_id():
                    with job.scope(), reading_record.using(job.record), reading_capture.using(job.capture):
                        card_info = self._ask_ai(job.task, job.image, job.foil_image, job.ocr_info)
            except Exception as e:
                logger.exception(f"AI stage failed: {e}")
                self.log(f"AI processing error: {e}", level="error")
                card_info = job.ocr_info  # what OCR read still stands (a confirmed card, or a name for review)
            self._finish(job, card_info)
