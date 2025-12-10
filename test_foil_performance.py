#!/usr/bin/env python3
"""
Test script to measure AI processing time with and without foil detection
"""

import os
import sys
import time
import random
import base64
import io
from pathlib import Path
from PIL import Image
import numpy as np

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config


def load_test_images(num_samples=10):
    """Load sample images from scanned_cards folder"""
    scanned_dir = Path("scanned_cards")
    if not scanned_dir.exists():
        print("ERROR: scanned_cards directory not found")
        return []

    # Get all jpg files
    image_files = list(scanned_dir.glob("*.jpg"))
    if not image_files:
        print("ERROR: No images found in scanned_cards directory")
        return []

    # Randomly sample images
    num_samples = min(num_samples, len(image_files))
    selected_files = random.sample(image_files, num_samples)

    print(f"Loading {num_samples} test images from {len(image_files)} available...")

    images = []
    for img_path in selected_files:
        try:
            # Load as numpy array (RGB)
            pil_img = Image.open(img_path).convert('RGB')
            img_array = np.array(pil_img)
            images.append({
                'path': str(img_path),
                'array': img_array,
                'name': img_path.name
            })
        except Exception as e:
            print(f"Warning: Failed to load {img_path}: {e}")

    return images


def test_with_foil_detection(identifier, images):
    """Test AI processing time WITH foil detection"""
    print("\n" + "="*60)
    print("TEST 1: WITH FOIL DETECTION (Current Implementation)")
    print("="*60)

    times = []
    for i, img_data in enumerate(images, 1):
        print(f"\n[{i}/{len(images)}] Processing {img_data['name']}...")
        start = time.time()

        try:
            result = identifier.identify_card(img_data['array'])
            elapsed = time.time() - start
            times.append(elapsed)

            if result:
                print(f"  ✓ Identified: {result['name']} #{result.get('collector_number', 'N/A')} ({result.get('foil', 'unknown')})")
                print(f"  ⏱ Time: {elapsed:.2f}s")
            else:
                print(f"  ✗ Failed to identify")
                print(f"  ⏱ Time: {elapsed:.2f}s")
        except Exception as e:
            elapsed = time.time() - start
            times.append(elapsed)
            print(f"  ✗ Error: {e}")
            print(f"  ⏱ Time: {elapsed:.2f}s")

    return times


def test_without_foil_detection(identifier, images):
    """Test AI processing time WITHOUT foil detection (simplified prompt)"""
    print("\n" + "="*60)
    print("TEST 2: WITHOUT FOIL DETECTION (Simplified Prompt)")
    print("="*60)

    # Monkey-patch the identifier methods to use simplified prompts
    original_local = identifier._identify_with_local
    original_ollama_native = identifier._identify_with_ollama_native
    original_openai_compatible = identifier._identify_with_openai_compatible

    def simplified_local(image_array):
        """Simplified local AI identification without foil detection"""
        import re
        base64_image = identifier._image_array_to_base64(image_array)

        # Try Ollama native API first
        result = simplified_ollama_native(image_array, base64_image)
        if result:
            return result

        # Fallback to OpenAI-compatible
        return simplified_openai_compatible(image_array, base64_image)

    def simplified_ollama_native(image_array, base64_image):
        """Simplified Ollama native API without foil detection"""
        import re
        import requests

        ollama_base = identifier.local_endpoint.replace('/v1/chat/completions', '')
        ollama_endpoint = f"{ollama_base}/api/chat"

        # SIMPLIFIED PROMPT - NO FOIL DETECTION
        prompt_text = """Look at this Magic: The Gathering card image.

Please tell me:
1. Card name (large text at the top of the card)
2. Collector number (small text at bottom-left corner)

The collector number has TWO lines:
- Line 1: A letter + 4-digit number (like "E 0367")
- Line 2: Set code · Language (like "LTR · EN")

Return ONLY the 4-digit number from Line 1.

Format:
NAME: [card name]
NUMBER: [4-digit number only]

Example:
NAME: Lightning Bolt
NUMBER: 0367

Just give the name and number, nothing else."""

        payload = {
            "model": identifier.model,
            "messages": [{"role": "user", "content": prompt_text, "images": [base64_image]}],
            "stream": False,
            "options": {"temperature": 0.1}
        }

        try:
            response = requests.post(ollama_endpoint, headers={"Content-Type": "application/json"}, json=payload, timeout=60)
            response.raise_for_status()
            response_json = response.json()

            if 'message' in response_json and 'content' in response_json['message']:
                response_text = response_json['message']['content'].strip()
            else:
                return None

            name_match = re.search(r'NAME:\s*(.+?)(?:\n|$)', response_text, re.IGNORECASE)
            number_match = re.search(r'NUMBER:\s*(.+?)(?:\n|$)', response_text, re.IGNORECASE)

            card_name = name_match.group(1).strip() if name_match else ""
            collector_number = number_match.group(1).strip() if number_match else ""

            if card_name.lower() == "unknown":
                card_name = ""
            if collector_number.lower() == "unknown":
                collector_number = ""

            if card_name:
                return {'name': card_name, 'collector_number': collector_number, 'foil': 'non-foil'}
            return None
        except Exception as e:
            print(f"  ⚠ Ollama native error: {e}")
            return None

    def simplified_openai_compatible(image_array, base64_image):
        """Simplified OpenAI-compatible endpoint without foil detection"""
        import re
        import requests

        # SIMPLIFIED PROMPT - NO FOIL DETECTION
        prompt_text = """Look at this Magic: The Gathering card image.

Please tell me:
1. Card name (large text at the top of the card)
2. Collector number (small text at bottom-left corner)

The collector number has TWO lines:
- Line 1: A letter + 4-digit number (like "E 0367")
- Line 2: Set code · Language (like "LTR · EN")

Return ONLY the 4-digit number from Line 1.

Format:
NAME: [card name]
NUMBER: [4-digit number only]

Example:
NAME: Lightning Bolt
NUMBER: 0367

Just give the name and number, nothing else."""

        payload = {
            "model": identifier.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]
            }],
            "max_tokens": 150,
            "temperature": 0.1
        }

        try:
            response = requests.post(identifier.local_endpoint, headers={"Content-Type": "application/json"}, json=payload, timeout=60)
            response.raise_for_status()
            response_json = response.json()

            if 'choices' in response_json and len(response_json['choices']) > 0:
                response_text = response_json['choices'][0]['message']['content'].strip()
            else:
                return None

            name_match = re.search(r'NAME:\s*(.+?)(?:\n|$)', response_text, re.IGNORECASE)
            number_match = re.search(r'NUMBER:\s*(.+?)(?:\n|$)', response_text, re.IGNORECASE)

            card_name = name_match.group(1).strip() if name_match else ""
            collector_number = number_match.group(1).strip() if number_match else ""

            if card_name.lower() == "unknown":
                card_name = ""
            if collector_number.lower() == "unknown":
                collector_number = ""

            if card_name:
                return {'name': card_name, 'collector_number': collector_number, 'foil': 'non-foil'}
            return None
        except Exception as e:
            print(f"  ⚠ OpenAI-compatible error: {e}")
            return None

    # Monkey-patch all local AI methods
    identifier._identify_with_local = simplified_local
    identifier._identify_with_ollama_native = simplified_ollama_native
    identifier._identify_with_openai_compatible = simplified_openai_compatible

    times = []
    for i, img_data in enumerate(images, 1):
        print(f"\n[{i}/{len(images)}] Processing {img_data['name']}...")
        start = time.time()

        try:
            result = identifier.identify_card(img_data['array'])
            elapsed = time.time() - start
            times.append(elapsed)

            if result:
                print(f"  ✓ Identified: {result['name']} #{result.get('collector_number', 'N/A')}")
                print(f"  ⏱ Time: {elapsed:.2f}s")
            else:
                print(f"  ✗ Failed to identify")
                print(f"  ⏱ Time: {elapsed:.2f}s")
        except Exception as e:
            elapsed = time.time() - start
            times.append(elapsed)
            print(f"  ✗ Error: {e}")
            print(f"  ⏱ Time: {elapsed:.2f}s")

    # Restore original methods
    identifier._identify_with_local = original_local
    identifier._identify_with_ollama_native = original_ollama_native
    identifier._identify_with_openai_compatible = original_openai_compatible

    return times


def print_statistics(with_foil_times, without_foil_times):
    """Print comparison statistics"""
    print("\n" + "="*60)
    print("RESULTS SUMMARY")
    print("="*60)

    if with_foil_times:
        avg_with = sum(with_foil_times) / len(with_foil_times)
        min_with = min(with_foil_times)
        max_with = max(with_foil_times)
        print(f"\nWITH Foil Detection:")
        print(f"  Average: {avg_with:.2f}s")
        print(f"  Min:     {min_with:.2f}s")
        print(f"  Max:     {max_with:.2f}s")
        print(f"  Total:   {sum(with_foil_times):.2f}s")

    if without_foil_times:
        avg_without = sum(without_foil_times) / len(without_foil_times)
        min_without = min(without_foil_times)
        max_without = max(without_foil_times)
        print(f"\nWITHOUT Foil Detection:")
        print(f"  Average: {avg_without:.2f}s")
        print(f"  Min:     {min_without:.2f}s")
        print(f"  Max:     {max_without:.2f}s")
        print(f"  Total:   {sum(without_foil_times):.2f}s")

    if with_foil_times and without_foil_times:
        diff = avg_with - avg_without
        pct = (diff / avg_with) * 100 if avg_with > 0 else 0

        print(f"\n" + "-"*60)
        print(f"DIFFERENCE:")
        print(f"  Absolute: {diff:+.2f}s per request")
        print(f"  Relative: {pct:+.1f}%")

        if diff > 0:
            print(f"\n✓ Removing foil detection saves {diff:.2f}s per request ({pct:.1f}% faster)")
        else:
            print(f"\n✗ No significant improvement (difference: {diff:.2f}s)")

    print("="*60)


def main():
    """Main test function"""
    print("="*60)
    print("FOIL DETECTION PERFORMANCE TEST (LOCAL AI)")
    print("="*60)

    # Check local AI endpoint
    local_endpoint = Config.LOCAL_AI_ENDPOINT

    # Use qwen3-vl:8b model for testing (override config)
    local_model = 'qwen3-vl:8b'

    print(f"\nLocal AI Endpoint: {local_endpoint}")
    print(f"Local AI Model: {local_model}")

    # Load test images
    num_samples = 5  # Test with 5 images
    images = load_test_images(num_samples)

    if not images:
        print("ERROR: No test images loaded")
        return 1

    print(f"\nLoaded {len(images)} test images")

    # Initialize identifier with LOCAL provider
    from card_identifier import CardIdentifier
    identifier = CardIdentifier(
        provider='local',
        model=local_model,
        log_callback=None  # Disable UI logging
    )
    print(f"Using provider: local")
    print(f"Using model: {identifier.model}")

    # Run tests
    with_foil_times = test_with_foil_detection(identifier, images)

    print("\n\n⏳ Waiting 5 seconds between tests to avoid rate limiting...\n")
    time.sleep(5)

    without_foil_times = test_without_foil_detection(identifier, images)

    # Print statistics
    print_statistics(with_foil_times, without_foil_times)

    return 0


if __name__ == '__main__':
    sys.exit(main())
