#!/usr/bin/env python3
"""
Anti-Glare Module
Preprocessing to reduce glare and reflections from foil/shiny cards
"""

import cv2
import numpy as np


def reduce_glare_adaptive(image):
    """
    Adaptive glare reduction (combines multiple techniques)
    Best for foil cards with complex reflections

    Args:
        image: Input image (BGR or RGB format)

    Returns:
        Image with reduced glare
    """
    # Step 1: Apply CLAHE to reduce brightness spikes
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)

    # CLAHE on L channel (luminance)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l_clahe = clahe.apply(l)

    # Merge back
    lab_clahe = cv2.merge([l_clahe, a, b])
    result = cv2.cvtColor(lab_clahe, cv2.COLOR_LAB2BGR)

    # Step 2: Bilateral filter (smooths while preserving edges)
    result = cv2.bilateralFilter(result, d=9, sigmaColor=75, sigmaSpace=75)

    # Step 3: Detect and reduce very bright spots (glare)
    gray = cv2.cvtColor(result, cv2.COLOR_BGR2GRAY)

    # Find very bright pixels (likely glare)
    _, bright_mask = cv2.threshold(gray, 240, 255, cv2.THRESH_BINARY)

    # Dilate mask slightly to catch glare edges
    kernel = np.ones((3, 3), np.uint8)
    bright_mask = cv2.dilate(bright_mask, kernel, iterations=1)

    # Inpaint (fill in) bright spots using surrounding pixels
    if np.any(bright_mask):
        result = cv2.inpaint(result, bright_mask, inpaintRadius=3, flags=cv2.INPAINT_TELEA)

    return result


def enhance_for_detection(image, reduce_glare_enabled=True):
    """
    Complete preprocessing pipeline for card detection
    Optimized for foil cards

    Args:
        image: Input image (BGR or RGB format)
        reduce_glare_enabled: Whether to apply anti-glare preprocessing

    Returns:
        Enhanced image ready for YOLO detection
    """
    if not reduce_glare_enabled:
        return image

    # Apply adaptive glare reduction
    result = reduce_glare_adaptive(image)

    # Optional: slight sharpening to restore edge clarity
    # (glare reduction can sometimes soften edges)
    kernel = np.array([[-1, -1, -1],
                       [-1,  9, -1],
                       [-1, -1, -1]]) * 0.5
    result = cv2.filter2D(result, -1, kernel)

    return result


def detect_glare_level(image):
    """
    Detect the amount of glare in an image
    Useful for adaptive processing

    Args:
        image: Input image (BGR or RGB format)

    Returns:
        float: Glare level (0.0 = no glare, 1.0 = severe glare)
    """
    # Convert to grayscale
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Count very bright pixels
    very_bright = np.sum(gray > 240)
    total_pixels = gray.size

    # Calculate percentage
    glare_percentage = very_bright / total_pixels

    # Normalize to 0-1 scale
    # 5% very bright pixels = severe glare (1.0)
    glare_level = min(glare_percentage / 0.05, 1.0)

    return glare_level


if __name__ == "__main__":
    # Test the anti-glare functions
    import sys

    if len(sys.argv) > 1:
        # Load test image
        test_image_path = sys.argv[1]
        print(f"Testing anti-glare on: {test_image_path}")

        img = cv2.imread(test_image_path)
        if img is None:
            print(f"Error: Could not load image from {test_image_path}")
            sys.exit(1)

        # Detect glare level
        glare_level = detect_glare_level(img)
        print(f"Glare level: {glare_level:.2%}")

        # Apply anti-glare
        print("Applying anti-glare preprocessing...")
        result = enhance_for_detection(img, reduce_glare_enabled=True)

        # Save result
        output_path = test_image_path.replace('.', '_antiglare.')
        cv2.imwrite(output_path, result)
        print(f"Saved result to: {output_path}")

        # Show comparison (if display available)
        try:
            cv2.imshow('Original', img)
            cv2.imshow('Anti-Glare', result)
            print("Press any key to close...")
            cv2.waitKey(0)
            cv2.destroyAllWindows()
        except:
            print("Display not available (headless system)")
    else:
        print("Usage: python3 anti_glare.py <image_path>")
        print("Example: python3 anti_glare.py test_foil_card.jpg")
