#!/usr/bin/env python3
"""
Card Identifier Module
Uses Vision AI to identify Magic: The Gathering cards from images
"""

import base64
import io
import os
import logging
import re
import threading
import time
import cv2
from PIL import Image
import requests

import games
import prompts
from config import Config

# Create AI logger
logger = logging.getLogger('ai')


class CardIdentifier:
    """Identifies Magic cards using vision AI"""

    # Available models for each provider (first entry is the default)
    AVAILABLE_MODELS = {
        'gemini': [
            'gemini-flash-latest',
            'gemini-flash-lite-latest',
            'gemini-pro-latest',
            'gemini-2.5-flash',
            'gemini-2.5-flash-lite',
            'gemini-2.5-pro'
        ],
        'openai': [
            'gpt-4.1-mini',
            'gpt-4.1',
            'gpt-4o',
            'gpt-4o-mini'
        ],
        'anthropic': [
            'claude-sonnet-5',
            'claude-haiku-4-5-20251001',
            'claude-opus-5-5'
        ],
        'local': [
            'qwen3-vl:4b',
            'qwen3-vl:8b',
            'llava:7b',
            'llava:13b',
            'llava:34b',
            'llava-phi3',
            'moondream'
        ]
    }

    def __init__(self, provider='gemini', model=None, api_key=None, log_callback=None):
        """
        Initialize card identifier

        Args:
            provider: 'gemini', 'openai', or 'anthropic'
            model: Specific model to use (if None, uses default for provider)
            api_key: API key for the chosen provider (or set via env var)
            log_callback: Optional logging function
        """
        self.provider = provider.lower()
        self.log_callback = log_callback

        # Validate provider
        if self.provider not in self.AVAILABLE_MODELS:
            raise ValueError(f"Unknown provider: {provider}")

        # Set model (use default if not specified)
        if model:
            # For local provider, skip validation since models vary per installation
            if self.provider != 'local' and model not in self.AVAILABLE_MODELS[self.provider]:
                raise ValueError(f"Model '{model}' not available for {provider}. Available models: {', '.join(self.AVAILABLE_MODELS[self.provider])}")
            self.model = model
        else:
            # Use first model in list as default
            self.model = self.AVAILABLE_MODELS[self.provider][0]

        # Get API key from parameter or environment (skip for local provider)
        if self.provider == 'local':
            self.api_key = None  # No API key needed for local
            self.local_endpoint = Config.LOCAL_AI_ENDPOINT
            self.log(f"Card identifier initialized with local AI at {self.local_endpoint} ({self.model})")
        else:
            self.api_key = api_key or os.getenv(f"{self.provider.upper()}_API_KEY")
            if not self.api_key:
                raise ValueError(f"API key not found for {provider}. Set {provider.upper()}_API_KEY environment variable.")
            self.log(f"Card identifier initialized with {provider} ({self.model})")

    # How long Ollama keeps the model loaded after a request (its default is 5 minutes)
    OLLAMA_KEEP_ALIVE = "30m"

    def log(self, message, level="info"):
        """Send log message to both file logger and UI callback"""
        # Log to file
        log_method = getattr(logger, level, logger.info)
        log_method(message)

        # Send to UI callback if provided
        if self.log_callback:
            self.log_callback(message, level)

    def identify_card(self, image_array, instructions=None):
        """
        Identify a Magic card from an image array

        Args:
            image_array: NumPy array (RGB) of the card image
            instructions: prompt instructions to use instead of the saved ones (prompt editor test)

        Returns:
            dict: {'name': str, 'collector_number': str, 'set_code': str} or None if identification fails
        """
        return self.identify_card_verbose(image_array, instructions)[1]

    def identify_card_verbose(self, image_array, instructions=None):
        """identify_card that also returns the AI's raw answer: (raw_text, result)"""
        start_time = time.time()

        try:
            if instructions is None:
                prompt = prompts.prompt('identify', self.provider, self.model, games.active_id())
            else:
                prompt = prompts.build('identify', instructions, games.active_id())
            self.log(f"Sending image to {self.provider} ({self.model}) for identification...")
            image = self._image_array_to_base64(image_array, max_dimension=Config.VISION_AI_IMAGE_SIZE)
            response_text = self._ask(image, prompt, max_tokens=100)
            result = self._parse_response(response_text, f"{self.provider} ({self.model})") if response_text else None

            # Log processing time
            elapsed_time = time.time() - start_time
            if result:
                self.log(f"✓ AI processed image in {elapsed_time:.2f}s", level="success")
                # Add timing to result
                result['processing_time'] = round(elapsed_time, 2)
            else:
                self.log(f"✗ AI processing completed in {elapsed_time:.2f}s (no result)", level="warning")

            return response_text, result
        except Exception as e:
            elapsed_time = time.time() - start_time
            self.log(f"Card identification error after {elapsed_time:.2f}s: {e}", level="error")
            return None, None

    def read_foil_symbol(self, card_image, instructions=None):
        """
        Check the star/dot foil marker in the bottom-left corner of a card.

        Args:
            card_image: Perspective-corrected (flat, portrait, tightly cropped) RGB card
                image, so the corner is at a known position
            instructions: prompt instructions to use instead of the saved ones (prompt editor test)

        Returns:
            str: 'foil', 'non-foil' or 'unknown'
        """
        return self.read_foil_symbol_verbose(card_image, instructions)[1]

    def read_foil_symbol_verbose(self, card_image, instructions=None):
        """read_foil_symbol that also returns the AI's raw answer: (raw_text, result)"""
        # Bottom 14% x left 45%: tall enough to still hold the set line when the detected
        # outline also takes in the edge of the card underneath
        height, width = card_image.shape[:2]
        corner = card_image[int(height * 0.86):, :int(width * 0.45)]
        corner = cv2.resize(corner, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        if instructions is None:
            prompt = prompts.prompt('foil', self.provider, self.model, games.active_id())
        else:
            prompt = prompts.build('foil', instructions, games.active_id())

        try:
            answer = (self._ask(self._image_array_to_base64(corner), prompt, max_tokens=10) or '').lower()
        except Exception as e:
            self.log(f"Foil check failed: {e}", level="warning")
            return None, 'unknown'

        foil = 'foil' if 'star' in answer else 'non-foil' if 'dot' in answer else 'unknown'
        self.log(f"Foil marker: {answer.strip()!r} -> {foil}")
        return answer.strip(), foil

    def _image_array_to_base64(self, image_array, quality=95, max_dimension=2048):
        """Convert NumPy image array to base64 string (optimized for text readability)"""
        # Convert RGB array to PIL Image
        pil_image = Image.fromarray(image_array)

        # Downscale if too large (but preserve detail for small text)
        width, height = pil_image.size
        if max(width, height) > max_dimension:
            scale = max_dimension / max(width, height)
            new_width = int(width * scale)
            new_height = int(height * scale)
            pil_image = pil_image.resize((new_width, new_height), Image.LANCZOS)

        # Convert to JPEG bytes with high quality for text recognition
        buffer = io.BytesIO()
        pil_image.save(buffer, format='JPEG', quality=quality, optimize=True)
        image_bytes = buffer.getvalue()

        # Encode to base64
        return base64.b64encode(image_bytes).decode('utf-8')

    def _parse_response(self, response_text, source):
        """
        Parse 'NAME: ... / NUMBER: ... / SET: ...' response text from any provider

        Returns:
            dict: {'name': str, 'collector_number': str, 'set_code': str} or None if no name found
        """
        name_match = re.search(r'NAME:\s*(.+?)(?:\n|$)', response_text, re.IGNORECASE)
        number_match = re.search(r'NUMBER:\s*(.+?)(?:\n|$)', response_text, re.IGNORECASE)
        # Set codes are 2-5 letters/digits ("HOB", "M21", "PLST"); anything else (e.g. "Unknown") is ignored
        set_match = re.search(r'SET:\s*([A-Za-z0-9]{2,5})\s*(?:\n|$)', response_text, re.IGNORECASE)
        set_code = set_match.group(1).upper() if set_match else ""

        # Some models drop the labels (all of them, or only the first one), and sometimes a
        # line: "Mirkwood / 0188 / HOB", "Mirkwood / NUMBER: 0188 / SET: HOB", "Mirkwood / HOB"
        if not name_match:
            lines = [line.strip() for line in response_text.splitlines() if line.strip()]
            bare = [line for line in lines if not re.match(r'(NAME|NUMBER|SET)\s*:', line, re.IGNORECASE)]
            if bare and len(lines) <= 3:
                name_match = re.match(r'(.+)', bare[0])
                for line in bare[1:]:
                    if not number_match and self._looks_like_number(line):
                        number_match = re.match(r'(.+)', line)
                    elif not set_code and re.fullmatch(r'[A-Za-z0-9]{2,5}', line):
                        set_code = line.upper()

        card_name = name_match.group(1).strip() if name_match else ""
        collector_number = self._clean_number(number_match.group(1).strip()) if number_match else ""

        # Clean up "Unknown" responses
        if card_name.lower() == "unknown":
            card_name = ""
        if collector_number.lower() == "unknown":
            collector_number = ""

        self.log(f"{source} identified: '{card_name}' #{collector_number or 'not found'} [{set_code or 'set not found'}]")

        if card_name:
            return {
                'name': card_name,
                'collector_number': collector_number,
                'set_code': set_code
            }
        self.log(f"{source} could not identify card name", level="warning")
        return None

    # Letters a model reads for digits in small print: "018B" is 0188
    DIGIT_LOOKALIKES = str.maketrans('OoDBIlSZ', '00081152')

    @classmethod
    def _looks_like_number(cls, text):
        """A collector number, maybe with its rarity letter and misread digits ("L 018B", "0186")"""
        match = re.fullmatch(r'(?:[A-Za-z]\s+)?([0-9OoDBIlSZ]{1,4})[a-z]?', text.strip())
        if not match:
            return False
        core = match.group(1)
        return core.isdigit() or (len(core) >= 3 and sum(c.isdigit() for c in core) >= 2)

    @classmethod
    def _clean_number(cls, text):
        """Fix letters read for digits in a mostly-digit collector number ("018B" -> "0188")"""
        return re.sub(r'\b[0-9OoDBIlSZ]{3,4}\b',
                      lambda m: m.group().translate(cls.DIGIT_LOOKALIKES) if sum(c.isdigit() for c in m.group()) >= 2 else m.group(),
                      text)

    # ------------------------------------------------------------------------
    # Provider requests: send one image + prompt, return the response text
    # ------------------------------------------------------------------------

    def _ask(self, base64_image, prompt, max_tokens):
        """Send an image and a prompt to the configured provider and return the text answer"""
        if self.provider == 'gemini':
            return self._ask_gemini(base64_image, prompt, max_tokens)
        if self.provider == 'openai':
            return self._ask_openai(base64_image, prompt, max_tokens)
        if self.provider == 'anthropic':
            return self._ask_anthropic(base64_image, prompt, max_tokens)
        if self.provider == 'local':
            return self._ask_local(base64_image, prompt, max_tokens)
        raise ValueError(f"Unknown provider: {self.provider}")

    def _ask_gemini(self, base64_image, prompt, max_tokens):
        """Google Gemini (REST API)"""
        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "image/jpeg", "data": base64_image}}
                ]
            }]
        }
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
            json=payload,
            timeout=30
        )
        response.raise_for_status()
        parts = response.json()['candidates'][0]['content']['parts']
        return ''.join(part.get('text', '') for part in parts).strip()

    def _ask_openai(self, base64_image, prompt, max_tokens):
        """OpenAI chat completions"""
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }
            ],
            "max_tokens": max_tokens
        }
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            json=payload,
            timeout=30
        )
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content'].strip()

    def _ask_anthropic(self, base64_image, prompt, max_tokens):
        """Anthropic Messages API"""
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64_image}},
                        {"type": "text", "text": prompt}
                    ]
                }
            ]
        }
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            json=payload,
            timeout=30
        )
        response.raise_for_status()
        return response.json()['content'][0]['text'].strip()

    def _ask_local(self, base64_image, prompt, max_tokens):
        """Local vision AI server (Ollama, vLLM, LM Studio, ...)"""
        # Ollama: prefer its native /api/chat endpoint (better vision support)
        if '/v1/chat/completions' in self.local_endpoint:
            answer = self._ask_ollama_native(base64_image, prompt, max_tokens)
            if answer:
                return answer
            self.log("Ollama native API failed, trying OpenAI-compatible endpoint...", level="warning")

        # OpenAI-compatible endpoint (vLLM, LM Studio, newer Ollama)
        return self._ask_openai_compatible(base64_image, prompt, max_tokens)

    def _ask_ollama_native(self, base64_image, prompt, max_tokens):
        """Ollama's native /api/chat endpoint"""
        ollama_endpoint = f"{self.local_endpoint.replace('/v1/chat/completions', '')}/api/chat"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt, "images": [base64_image]}],
            "stream": False,
            "think": False,  # Thinking models otherwise spend the token budget reasoning and return no answer
            "keep_alive": self.OLLAMA_KEEP_ALIVE,
            # num_predict caps the answer: a model that starts reasoning aloud would
            # otherwise write hundreds of tokens (seconds) before answering
            "options": {"temperature": 0, "num_predict": max(max_tokens, 50)}
        }

        try:
            response = requests.post(ollama_endpoint, json=payload, timeout=60)
            response.raise_for_status()
            response_json = response.json()

            if 'message' not in response_json or 'content' not in response_json['message']:
                self.log(f"Unexpected Ollama response format: {response_json}", level="error")
                return None

            answer = response_json['message']['content'].strip()
            self.log(f"Ollama raw response: {answer[:200]}", level="info")
            return answer

        except requests.exceptions.HTTPError as e:
            # Ollama explains errors (e.g. "model not found") in the response body
            self.log(f"Ollama native API error: {e} - {e.response.text[:200]}", level="warning")
            return None
        except Exception as e:
            self.log(f"Ollama native API error: {e}", level="warning")
            return None

    def warm_up(self):
        """
        Preload the local model so the first card isn't slowed down by loading it
        (Ollama unloads idle models; loading a 9B model takes ~10 s). Runs in the
        background; no-op for cloud providers.
        """
        if self.provider != 'local' or '/v1/chat/completions' not in self.local_endpoint:
            return

        def load():
            ollama_endpoint = f"{self.local_endpoint.replace('/v1/chat/completions', '')}/api/chat"
            try:
                # An empty message list just loads the model
                requests.post(ollama_endpoint, json={"model": self.model, "messages": [], "keep_alive": self.OLLAMA_KEEP_ALIVE}, timeout=120)
                self.log(f"Local model {self.model} loaded")
            except Exception as e:
                self.log(f"Could not preload local model: {e}", level="warning")

        threading.Thread(target=load, daemon=True, name="AI-Warmup").start()

    def _ask_openai_compatible(self, base64_image, prompt, max_tokens):
        """OpenAI-compatible endpoint on a local server"""
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }
            ],
            "max_tokens": max(max_tokens, 150),
            "temperature": 0.1
        }

        try:
            response = requests.post(self.local_endpoint, json=payload, timeout=60)
            response.raise_for_status()
            response_json = response.json()

            if not response_json.get('choices'):
                self.log(f"Unexpected response format: {response_json}", level="error")
                return None

            answer = response_json['choices'][0]['message']['content'].strip()
            self.log(f"Local AI raw response: {answer[:200]}", level="info")
            return answer

        except requests.exceptions.ConnectionError:
            self.log(f"Failed to connect to local AI server at {self.local_endpoint}", level="error")
            self.log("Is the server running? Check with: curl " + self.local_endpoint.replace('/v1/chat/completions', '/api/tags'), level="error")
            return None
        except requests.exceptions.Timeout:
            self.log("Local AI request timed out after 60s", level="error")
            self.log("Model may be slow, try a smaller model", level="warning")
            return None
