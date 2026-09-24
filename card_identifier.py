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
import time
from PIL import Image
import requests
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

    # Card identification prompt (shared across all AI providers)
    CARD_IDENTIFICATION_PROMPT = """This is a Magic: The Gathering card. Please identify TWO pieces of information:

1. The card name (located at the top-left of the card)
2. The collector number (located at the BOTTOM-LEFT corner of the card)

IMPORTANT INSTRUCTIONS FOR COLLECTOR NUMBER:
- The collector number is at the BOTTOM-LEFT corner in a TWO-LINE format:
  * LINE 1: A letter followed by 4-digit number (e.g., "E 0367", "D 0045", "B 0123")
  * LINE 2: Set code · Language (e.g., "LTR · EN", "M21 · EN")
- Look for this two-line pattern to identify the correct location
- Return ONLY the 4-digit number from Line 1 (e.g., "0367" not "E 0367")
- The letter is just a visual marker to help you find it - don't include it
- DO NOT confuse it with the mana cost symbols in the TOP-RIGHT corner
- The mana cost has symbols like {1}, {W}, {U}, {B}, {R}, {G} - IGNORE these completely

Return your answer in EXACTLY this format:
NAME: [card name]
NUMBER: [4-digit number only]

Rules:
- If you see a double-faced card, return the front face name
- Return ONLY the 4-digit number (e.g., "0367", "0045", "0123")
- If you cannot find the collector number, return "Unknown"
- NEVER use the top-right corner mana cost as the collector number

Example response:
NAME: Lightning Bolt
NUMBER: 0367

Your response:"""

    def log(self, message, level="info"):
        """Send log message to both file logger and UI callback"""
        # Log to file
        log_method = getattr(logger, level, logger.info)
        log_method(message)

        # Send to UI callback if provided
        if self.log_callback:
            self.log_callback(message, level)

    def identify_card(self, image_array):
        """
        Identify a Magic card from an image array

        Args:
            image_array: NumPy array (RGB) of the card image

        Returns:
            dict: {'name': str, 'collector_number': str} or None if identification fails
        """
        start_time = time.time()

        try:
            if self.provider == 'gemini':
                result = self._identify_with_gemini(image_array)
            elif self.provider == 'openai':
                result = self._identify_with_openai(image_array)
            elif self.provider == 'anthropic':
                result = self._identify_with_anthropic(image_array)
            elif self.provider == 'local':
                result = self._identify_with_local(image_array)
            else:
                self.log(f"Unknown provider: {self.provider}", level="error")
                return None

            # Log processing time
            elapsed_time = time.time() - start_time
            if result:
                self.log(f"✓ AI processed image in {elapsed_time:.2f}s", level="success")
                # Add timing to result
                result['processing_time'] = round(elapsed_time, 2)
            else:
                self.log(f"✗ AI processing completed in {elapsed_time:.2f}s (no result)", level="warning")

            return result
        except Exception as e:
            elapsed_time = time.time() - start_time
            self.log(f"Card identification error after {elapsed_time:.2f}s: {e}", level="error")
            return None

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
        Parse 'NAME: ... / NUMBER: ...' response text from any provider

        Returns:
            dict: {'name': str, 'collector_number': str} or None if no name found
        """
        name_match = re.search(r'NAME:\s*(.+?)(?:\n|$)', response_text, re.IGNORECASE)
        number_match = re.search(r'NUMBER:\s*(.+?)(?:\n|$)', response_text, re.IGNORECASE)

        card_name = name_match.group(1).strip() if name_match else ""
        collector_number = number_match.group(1).strip() if number_match else ""

        # Clean up "Unknown" responses
        if card_name.lower() == "unknown":
            card_name = ""
        if collector_number.lower() == "unknown":
            collector_number = ""

        self.log(f"{source} identified: '{card_name}' #{collector_number or 'not found'}")

        if card_name:
            return {
                'name': card_name,
                'collector_number': collector_number
            }
        self.log(f"{source} could not identify card name", level="warning")
        return None

    def _identify_with_gemini(self, image_array):
        """Identify card using Google Gemini Vision (REST API)"""
        base64_image = self._image_array_to_base64(image_array)

        payload = {
            "contents": [{
                "parts": [
                    {"text": self.CARD_IDENTIFICATION_PROMPT},
                    {"inline_data": {"mime_type": "image/jpeg", "data": base64_image}}
                ]
            }]
        }

        self.log(f"Sending image to Gemini ({self.model}) for identification...")
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
            json=payload,
            timeout=30
        )

        response.raise_for_status()
        parts = response.json()['candidates'][0]['content']['parts']
        response_text = ''.join(part.get('text', '') for part in parts).strip()
        return self._parse_response(response_text, f"Gemini ({self.model})")

    def _identify_with_openai(self, image_array):
        """Identify card using OpenAI Vision"""
        base64_image = self._image_array_to_base64(image_array)

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": self.CARD_IDENTIFICATION_PROMPT
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            "max_tokens": 100
        }

        self.log(f"Sending image to OpenAI ({self.model}) for identification...")
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=30
        )

        response.raise_for_status()
        response_text = response.json()['choices'][0]['message']['content'].strip()
        return self._parse_response(response_text, "OpenAI")

    def _identify_with_anthropic(self, image_array):
        """Identify card using Anthropic Claude Vision"""
        base64_image = self._image_array_to_base64(image_array)

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01"
        }

        payload = {
            "model": self.model,
            "max_tokens": 100,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": base64_image
                            }
                        },
                        {
                            "type": "text",
                            "text": self.CARD_IDENTIFICATION_PROMPT
                        }
                    ]
                }
            ]
        }

        self.log(f"Sending image to Claude ({self.model}) for identification...")
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
            timeout=30
        )

        response.raise_for_status()
        response_text = response.json()['content'][0]['text'].strip()
        return self._parse_response(response_text, "Claude")

    def _identify_with_local(self, image_array):
        """Identify card using local vision AI server (Ollama, vLLM, etc.)"""
        base64_image = self._image_array_to_base64(image_array)

        # Detect if this is an Ollama server
        is_ollama = '/v1/chat/completions' in self.local_endpoint

        if is_ollama:
            # Use Ollama's native /api/chat endpoint for better vision support
            result = self._identify_with_ollama_native(base64_image)
            if result:
                return result
            # If native API fails, try OpenAI-compatible as fallback
            self.log("Ollama native API failed, trying OpenAI-compatible endpoint...", level="warning")

        # OpenAI-compatible endpoint (vLLM, LM Studio, newer Ollama)
        return self._identify_with_openai_compatible(base64_image)

    def _identify_with_ollama_native(self, base64_image):
        """Use Ollama's native /api/chat endpoint (better vision support)"""
        ollama_base = self.local_endpoint.replace('/v1/chat/completions', '')
        ollama_endpoint = f"{ollama_base}/api/chat"

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": self.CARD_IDENTIFICATION_PROMPT,
                    "images": [base64_image]
                }
            ],
            "stream": False,
            "options": {
                "temperature": 0.1
            }
        }

        self.log(f"Sending image to Ollama ({self.model}) at {ollama_endpoint}...")

        try:
            response = requests.post(ollama_endpoint, json=payload, timeout=60)
            response.raise_for_status()
            response_json = response.json()

            if 'message' not in response_json or 'content' not in response_json['message']:
                self.log(f"Unexpected Ollama response format: {response_json}", level="error")
                return None

            response_text = response_json['message']['content'].strip()
            self.log(f"Ollama raw response: {response_text[:200]}", level="info")
            return self._parse_response(response_text, "Local AI")

        except Exception as e:
            self.log(f"Ollama native API error: {e}", level="warning")
            return None

    def _identify_with_openai_compatible(self, base64_image):
        """Use OpenAI-compatible endpoint (vLLM, LM Studio, newer Ollama)"""
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": self.CARD_IDENTIFICATION_PROMPT
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            "max_tokens": 150,
            "temperature": 0.1
        }

        self.log(f"Sending image to local AI ({self.model}) at {self.local_endpoint}...")

        try:
            response = requests.post(self.local_endpoint, json=payload, timeout=60)
            response.raise_for_status()
            response_json = response.json()

            if not response_json.get('choices'):
                self.log(f"Unexpected response format: {response_json}", level="error")
                return None

            response_text = response_json['choices'][0]['message']['content'].strip()
            self.log(f"Local AI raw response: {response_text[:200]}", level="info")
            return self._parse_response(response_text, "Local AI")

        except requests.exceptions.ConnectionError:
            self.log(f"Failed to connect to local AI server at {self.local_endpoint}", level="error")
            self.log("Is the server running? Check with: curl " + self.local_endpoint.replace('/v1/chat/completions', '/api/tags'), level="error")
            return None
        except requests.exceptions.Timeout:
            self.log("Local AI request timed out after 60s", level="error")
            self.log("Model may be slow, try a smaller model", level="warning")
            return None
