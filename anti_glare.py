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
