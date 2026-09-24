#!/usr/bin/env python3
"""
Scanner Module - Updated with AI-based Object Detection
Camera and card scanning logic with real-time object detection.
"""

import cv2
import numpy as np
import time
import threading
import subprocess
import logging
from datetime import datetime
from config import Config
from object_detector import ObjectDetector, warp_card
from card_identifier import CardIdentifier
from settings import Settings

# Create scanner logger
logger = logging.getLogger('scanner')


class CardScanner:
    """Handles camera operations and card scanning"""
    
    def __init__(self, log_callback=None, model_path='yolov8n.pt'):
        self.log_callback = log_callback
        self.camera = None
        self.camera_type = None
        self.current_frame = None
        self.annotated_frame = None  # Frame with card detection overlay
        self.detected_card = None  # (frame, bbox, corners) of the detected card - see get_detected_card()
        self.detected_card_name = "" # Name of the detected card
        self.card_detected = False
        self.frame_lock = threading.Lock()
        self.capture_thread = None
        self.running = False
        self.object_detector = ObjectDetector(
            model_path=model_path,
            method=Config.DETECTION_METHOD,
            allow_landscape=Config.DETECTION_ALLOW_LANDSCAPE
        )

        # Frame stability tracking (for auto-capture)
        self.stable_frames = 0
        self.required_stable_frames = 5  # Require 5 stable frames before capture
        self.frames_since_card_lost = 0  # Track frames without card detection

        # User settings
        self.settings = Settings()

        # Vision AI for card identification
        self.card_identifier = None
        if Config.VISION_AI_ENABLED:
            # Load saved provider and model from settings
            saved_provider = self.settings.get_ai_provider()
            saved_model = self.settings.get_ai_model()
            try:
                self.card_identifier = CardIdentifier(
                    provider=saved_provider,
                    model=saved_model,
                    log_callback=log_callback
                )
                self.log(f"Initialized AI with saved settings: {saved_provider} ({saved_model or 'default'})", level="info")
            except Exception as e:
                # Fallback to config default if saved settings fail
                self.log(f"Failed to load saved AI settings, using config defaults: {e}", level="warning")
                try:
                    self.card_identifier = CardIdentifier(
                        provider=Config.VISION_AI_PROVIDER,
                        log_callback=log_callback
                    )
                    self.log(f"Vision AI enabled ({Config.VISION_AI_PROVIDER})", level="success")
                except Exception as e:
                    self.log(f"Vision AI initialization failed: {e}", level="warning")
                    self.log("Continuing without AI identification", level="warning")

        # Detection settings
        self.enable_detection = True  # Toggle auto-detection
        self.anti_glare_enabled = Config.ANTI_GLARE_ENABLED  # Toggle anti-glare preprocessing
        self.debug_trace_enabled = False  # Toggle debug trace logging

        # Card size detection - keep box visible longer for card-sized objects
        self.last_card_detection = None  # Store last valid card detection (bbox, time)
        self.card_display_duration = 6.0  # Keep detection box visible for 6 seconds (increased from 3.0 for stability)
        self.card_aspect_ratio_target = 88.0 / 63.0  # Magic card: 88mm x 63mm = 1.397
        self.aspect_ratio_tolerance = Config.ASPECT_RATIO_TOLERANCE  # Load from config (adjustable in config.yaml)

        # Card size filtering
        if Config.CARD_SIZE_FILTER_ENABLED:
            self.log(f"Card size filtering enabled: {Config.CARD_MIN_WIDTH}-{Config.CARD_MAX_WIDTH}px width, "
                    f"{Config.CARD_MIN_HEIGHT}-{Config.CARD_MAX_HEIGHT}px height", level="info")
        else:
            self.log("Card size filtering disabled (using aspect ratio only)", level="info")

        # Aspect ratio detection
        min_ratio = self.card_aspect_ratio_target * (1 - self.aspect_ratio_tolerance)
        max_ratio = self.card_aspect_ratio_target * (1 + self.aspect_ratio_tolerance)
        self.log(f"Aspect ratio detection: {min_ratio:.2f}-{max_ratio:.2f} (tolerance: {self.aspect_ratio_tolerance*100:.0f}%)", level="info")

        # Anti-glare preprocessing (user-toggleable via web interface)
        self.log(f"Anti-glare preprocessing: {('enabled' if Config.ANTI_GLARE_ENABLED else 'disabled')} by default (toggle in UI for foil cards)", level="info")

        # Auto-capture focus requirement
        if Config.AUTO_CAPTURE_WAIT_FOR_FOCUS and Config.FOCUS_LOCK_ON_STABLE:
            self.log("Auto-capture will wait for focus lock (ensures sharp images)", level="info")

        # Bounding box smoothing to eliminate flicker
        self.smoothed_bbox = None  # Smoothed bounding box coordinates
        self.bbox_smoothing_alpha = 0.3  # Smoothing factor (0.3 = 30% new, 70% old)
        self.bbox_movement_threshold = 5  # Minimum pixel movement to update (reduces jitter)

        # Auto-capture settings (enabled state controlled via UI button)
        self.auto_capture_enabled = False  # Disabled by default, enabled via UI button
        self.auto_capture_delay = Config.AUTO_CAPTURE_DELAY
        self.last_auto_capture_time = 0
        self.auto_capture_callback = None
        self.card_under_review = False  # Prevent auto-capture while card is being reviewed

        self.initialize_camera()
    
    def log(self, message, level="info"):
        """Send log message to both file logger and UI callback"""
        # Log to file
        log_method = getattr(logger, level, logger.info)
        log_method(message)

        # Send to UI callback if provided
        if self.log_callback:
            self.log_callback(message, level)

    def is_card_sized(self, bbox):
        """
        Check if bounding box matches Magic card dimensions
        Uses both aspect ratio AND pixel size filtering

        Args:
            bbox: Tuple of (x1, y1, x2, y2)

        Returns:
            bool: True if both aspect ratio and size match card dimensions
        """
        x1, y1, x2, y2 = bbox
        width = x2 - x1
        height = y2 - y1

        if width == 0 or height == 0:
            return False

        # Calculate area
        area = width * height

        # Step 1: Size filtering (if enabled)
        if Config.CARD_SIZE_FILTER_ENABLED:
            # Check width range
            if not (Config.CARD_MIN_WIDTH <= width <= Config.CARD_MAX_WIDTH):
                return False

            # Check height range
            if not (Config.CARD_MIN_HEIGHT <= height <= Config.CARD_MAX_HEIGHT):
                return False

            # Check area range (prevents very small or very large detections)
            if not (Config.CARD_MIN_AREA <= area <= Config.CARD_MAX_AREA):
                return False

        # Step 2: Aspect ratio check (Magic card: 88mm x 63mm = 1.397)
        aspect_ratio = max(width, height) / min(width, height)

        # Check if aspect ratio matches card (with tolerance)
        min_ratio = self.card_aspect_ratio_target * (1 - self.aspect_ratio_tolerance)
        max_ratio = self.card_aspect_ratio_target * (1 + self.aspect_ratio_tolerance)

        is_match = min_ratio <= aspect_ratio <= max_ratio

        # Debug logging (controlled by UI toggle)
        if self.debug_trace_enabled and not is_match:
            if not hasattr(self, '_last_aspect_log_time') or time.time() - self._last_aspect_log_time > 2:
                self.log(f"Aspect ratio rejected: {aspect_ratio:.3f} (need {min_ratio:.3f}-{max_ratio:.3f}) | Size: {width:.0f}x{height:.0f}px", level="debug")
                self._last_aspect_log_time = time.time()

        return is_match

    def smooth_bounding_box(self, new_bbox):
        """
        Apply exponential moving average smoothing to bounding box coordinates
        This eliminates flickering when card is physically still

        Args:
            new_bbox: Tuple of (x1, y1, x2, y2) from YOLO detection

        Returns:
            Smoothed bounding box tuple (x1, y1, x2, y2)
        """
        if new_bbox is None:
            # No detection - clear smoothed box after a delay
            return self.smoothed_bbox

        x1_new, y1_new, x2_new, y2_new = new_bbox

        # Initialize smoothed box on first detection
        if self.smoothed_bbox is None:
            self.smoothed_bbox = new_bbox
            return new_bbox

        x1_old, y1_old, x2_old, y2_old = self.smoothed_bbox

        # Calculate movement distance (center point)
        center_x_new = (x1_new + x2_new) / 2
        center_y_new = (y1_new + y2_new) / 2
        center_x_old = (x1_old + x2_old) / 2
        center_y_old = (y1_old + y2_old) / 2

        movement = ((center_x_new - center_x_old)**2 + (center_y_new - center_y_old)**2)**0.5

        # If movement is very small (below threshold), keep old box (reduces jitter)
        if movement < self.bbox_movement_threshold:
            return self.smoothed_bbox

        # Apply exponential moving average
        # alpha = smoothing factor (0.3 = 30% new, 70% old)
        alpha = self.bbox_smoothing_alpha

        x1_smooth = int(alpha * x1_new + (1 - alpha) * x1_old)
        y1_smooth = int(alpha * y1_new + (1 - alpha) * y1_old)
        x2_smooth = int(alpha * x2_new + (1 - alpha) * x2_old)
        y2_smooth = int(alpha * y2_new + (1 - alpha) * y2_old)

        self.smoothed_bbox = (x1_smooth, y1_smooth, x2_smooth, y2_smooth)
        return self.smoothed_bbox

    def detect_camera_type(self):
        """Auto-detect available camera"""
        camera_type = Config.CAMERA_TYPE.lower()
        
        if camera_type == 'usb':
            return 'usb'
        elif camera_type == 'picamera':
            return 'picamera'
        elif camera_type == 'auto':
            if self._test_usb_camera():
                return 'usb'
            elif self._test_picamera():
                return 'picamera'
            else:
                raise Exception("No camera detected. Please check camera connection.")
        else:
            raise Exception(f"Unknown camera type: {camera_type}")
    
    def _test_usb_camera(self):
        """Test if USB camera is available"""
        try:
            cap = cv2.VideoCapture(Config.USB_CAMERA_INDEX)
            if cap.isOpened():
                ret, _ = cap.read()
                cap.release()
                return ret
            return False
        except:
            return False
    
    def _test_picamera(self):
        """Test if Picamera is available"""
        try:
            from picamera2 import Picamera2
            picam = Picamera2()
            picam.close()
            return True
        except:
            return False
    
    def initialize_camera(self):
        """Initialize the camera"""
        try:
            self.camera_type = self.detect_camera_type()
            self.log(f"Detected camera type: {self.camera_type}")
            
            if self.camera_type == 'usb':
                self._initialize_usb_camera()
            elif self.camera_type == 'picamera':
                self._initialize_picamera()
            
            self.running = True
            self.capture_thread = threading.Thread(target=self._capture_frames, daemon=True)
            self.capture_thread.start()
            
            self.log("Camera initialized successfully", level="success")
        except Exception as e:
            self.log(f"Failed to initialize camera: {e}", level="error")
            raise

    def set_ai_provider(self, provider, model=None):
        """
        Dynamically change the AI provider and/or model for card identification

        Args:
            provider: 'gemini', 'openai', or 'anthropic'
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
            # Create new CardIdentifier with the specified provider and model
            new_identifier = CardIdentifier(
                provider=provider,
                model=model,
                log_callback=self.log_callback
            )

            # If successful, replace the old identifier
            self.card_identifier = new_identifier

            # Save the selection to settings for persistence
            self.settings.set_ai_provider(provider, model)

            # Update the config for consistency (optional, doesn't persist across restarts)
            Config.VISION_AI_PROVIDER = provider

            message = f"AI provider changed to {provider} ({new_identifier.model})"
            self.log(message, level="success")
            return {
                'success': True,
                'message': message,
                'model': new_identifier.model
            }
        except ValueError as e:
            # API key not found or invalid model
            self.log(f"Failed to change AI provider: {e}", level="error")
            return {
                'success': False,
                'message': str(e)
            }
        except Exception as e:
            # Other errors
            self.log(f"Error changing AI provider: {e}", level="error")
            return {
                'success': False,
                'message': f"Error: {str(e)}"
            }

    def _initialize_usb_camera(self):
        """Initialize USB camera using OpenCV, setting high resolution and focus settings"""
        # CRITICAL: Use V4L2 backend directly instead of GStreamer
        # GStreamer has issues with format changes on Raspberry Pi
        self.camera = cv2.VideoCapture(Config.USB_CAMERA_INDEX, cv2.CAP_V4L2)

        if not self.camera.isOpened():
            raise Exception(f"Failed to open USB camera at index {Config.USB_CAMERA_INDEX}")

        width = Config.CAMERA_RESOLUTION[0]
        height = Config.CAMERA_RESOLUTION[1]
        fps = Config.CAMERA_FPS

        # CRITICAL: Set MJPEG codec BEFORE setting resolution
        # USB cameras require MJPEG for high resolutions (2560x1440)
        # YUYV only supports up to 640x480
        self.camera.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))

        self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.camera.set(cv2.CAP_PROP_FPS, fps)

        # CRITICAL: Reset camera to optimal settings on every startup
        # These settings persist in camera hardware across runs
        self._run_v4l2_command('-c', 'sharpness=50')  # Default sharpness
        self._run_v4l2_command('-c', 'zoom_absolute=100')  # Minimum zoom (widest field of view)

        # ALWAYS enable continuous autofocus (critical for sharp images)
        self._run_v4l2_command('-c', 'focus_automatic_continuous=1')
        self.log("Camera settings: Reset to defaults (sharpness=50, zoom=100, autofocus=enabled)")

        # Keep autofocus continuous (no locking)
        if not Config.FOCUS_LOCK_ON_STABLE:
            self.log("Autofocus: Continuous (no locking)")

        self.log(f"Resolution: {width}x{height} @ {fps} FPS")

        # Give camera time to initialize
        time.sleep(2)

        self.log(f"USB camera initialized (index: {Config.USB_CAMERA_INDEX})")
    
    def _initialize_picamera(self):
        """Initialize Raspberry Pi Camera using Picamera2"""
        from picamera2 import Picamera2
        
        self.camera = Picamera2()
        
        config = self.camera.create_video_configuration(
            main={"size": Config.CAMERA_RESOLUTION},
            lores={"size": Config.CAMERA_PREVIEW_RESOLUTION},
        )
        self.camera.configure(config)
        self.camera.start()
        
        time.sleep(2)
        
        self.log("Picamera initialized")
    
    def _capture_frames(self):
        """Continuously capture frames and perform object detection"""
        while self.running:
            try:
                if self.camera_type == 'usb':
                    ret, frame = self.camera.read()
                    if not ret:
                        self.log("Failed to read frame from USB camera", level="error")
                        time.sleep(0.1)
                        continue
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    
                elif self.camera_type == 'picamera':
                    frame = self.camera.capture_array()
                    if len(frame.shape) == 3 and frame.shape[2] == 4:
                        frame = cv2.cvtColor(frame, cv2.COLOR_RGBA2RGB)
                    elif len(frame.shape) == 3 and frame.shape[2] == 3:
                        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

                annotated = frame.copy()

                # Track which detection to display (current or cached)
                display_bbox = None
                display_corners = None
                is_cached_detection = False

                if not self.enable_detection:
                    # Detection is disabled - clear all detection state
                    self.last_card_detection = None
                    self.stable_frames = 0
                    with self.frame_lock:
                        self.detected_card = None
                        self.detected_card_name = ""
                        self.card_detected = False

                elif self.enable_detection:
                    # Run detection on raw frame for better performance
                    # Anti-glare is only applied during capture if enabled
                    bounding_box, card_name, confidence, corners = self.object_detector.detect(
                        frame,
                        conf_threshold=Config.DETECTION_CONFIDENCE_THRESHOLD
                    )

                    if bounding_box:
                        # Apply smoothing to eliminate flicker
                        smoothed_box = self.smooth_bounding_box(bounding_box)

                        # Use smoothed bounding box for all subsequent operations
                        if smoothed_box:
                            bounding_box = smoothed_box
                            x1, y1, x2, y2 = bounding_box
                            width = x2 - x1
                            height = y2 - y1

                            # Outline detections are already validated as card-shaped
                            # (and may be rotated, which skews the bounding box ratio)
                            if corners is not None or self.is_card_sized(bounding_box):
                                # Store this as a valid card detection
                                self.last_card_detection = {
                                    'bbox': bounding_box,
                                    'corners': corners,
                                    'name': card_name,
                                    'confidence': confidence,
                                    'time': time.time(),
                                    'frame': frame
                                }
                                display_bbox = bounding_box
                                display_corners = corners

                                # Card is detected - reset lost frames counter
                                self.frames_since_card_lost = 0

                                # Track stable frames
                                self.stable_frames = min(self.stable_frames + 1, self.required_stable_frames)
                            else:
                                # Not card-sized - ignore this detection
                                # Log occasionally for debugging (throttled to avoid spam, controlled by UI toggle)
                                if self.debug_trace_enabled:
                                    if not hasattr(self, '_last_filter_log_time') or time.time() - self._last_filter_log_time > 5:
                                        self.log(f"Filtered detection: {width:.0f}x{height:.0f}px (outside card size range)", level="debug")
                                        self._last_filter_log_time = time.time()

                                display_bbox = None  # Don't show non-card boxes
                                # Reset stability counter since this isn't a valid card
                                self.stable_frames = 0

                            # Keep the card (only if it passed the card checks) for capture
                            if display_bbox:
                                with self.frame_lock:
                                    self.detected_card = (frame, display_bbox, corners)
                                    self.detected_card_name = card_name
                                    self.card_detected = True
                            else:
                                with self.frame_lock:
                                    self.detected_card = None
                                    self.detected_card_name = ""
                                    self.card_detected = False
                    else:
                        # No current detection - signal to smoothing algorithm
                        self.smooth_bounding_box(None)

                        # Check if we have a recent cached card detection
                        if self.last_card_detection:
                            time_since_detection = time.time() - self.last_card_detection['time']
                            if time_since_detection < self.card_display_duration:
                                # Show cached card detection
                                display_bbox = self.last_card_detection['bbox']
                                display_corners = self.last_card_detection['corners']
                                bounding_box = display_bbox
                                card_name = self.last_card_detection['name']
                                confidence = self.last_card_detection['confidence']
                                is_cached_detection = True

                                # Make the cached card available for capture
                                with self.frame_lock:
                                    self.detected_card = (self.last_card_detection['frame'], display_bbox, display_corners)
                                    self.detected_card_name = card_name
                                    self.card_detected = True

                                # Don't increment lost frames counter yet - we're showing cached detection
                            else:
                                # Cache expired, clear it
                                self.last_card_detection = None
                                self.frames_since_card_lost += 1
                        else:
                            # No cached detection available
                            self.frames_since_card_lost += 1

                        # Reset stability if no detection (cached or current)
                        if not display_bbox:
                            self.stable_frames = 0
                            # Reset smoothed bounding box after losing card for several frames
                            if self.frames_since_card_lost > 10:
                                self.smoothed_bbox = None

                        # Update detection state
                        if not display_bbox:
                            with self.frame_lock:
                                self.detected_card = None
                                self.detected_card_name = ""
                                self.card_detected = False

                    # Draw the detection box (current or cached)
                    if display_bbox:
                        x1, y1, x2, y2 = display_bbox

                        # Change box color based on stability
                        if is_cached_detection:
                            box_color = (100, 200, 255)  # Light blue for cached detection
                        elif self.stable_frames >= self.required_stable_frames:
                            box_color = (0, 255, 0)  # Green when stable
                        else:
                            box_color = (255, 165, 0)  # Orange when stabilizing

                        # Draw the card outline (or bounding box for YOLO detections)
                        if display_corners is not None:
                            cv2.polylines(annotated, [display_corners.astype(np.int32)], True, box_color, 3)
                        else:
                            cv2.rectangle(annotated, (x1, y1), (x2, y2), box_color, 3)

                        # Create label
                        label = f"{card_name} ({confidence:.2f})"
                        if is_cached_detection:
                            time_remaining = self.card_display_duration - (time.time() - self.last_card_detection['time'])
                            label += f" [HOLD {time_remaining:.1f}s]"
                        elif self.stable_frames < self.required_stable_frames:
                            label += f" [Stabilizing {self.stable_frames}/{self.required_stable_frames}]"
                        else:
                            label += " [Ready]"

                        cv2.putText(annotated, label, (x1, y1 - 10),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, box_color, 2)

                        # Auto-capture logic: Trigger when card-sized object is stable and ready
                        # Re-check auto_capture_enabled right before triggering to avoid race conditions
                        if (self.enable_detection and  # Detection must be enabled
                            self.auto_capture_enabled and
                            not is_cached_detection and  # Only auto-capture current detections, not cached ones
                            not self.card_under_review and  # Don't auto-capture if card is being reviewed
                            (display_corners is not None or self.is_card_sized(display_bbox)) and
                            self.stable_frames >= self.required_stable_frames):

                            # Check if enough time has passed since last auto-capture
                            time_since_last_capture = time.time() - self.last_auto_capture_time

                            # Debug logging for timing
                            if time_since_last_capture < self.auto_capture_delay:
                                # Only log occasionally to avoid spam (every 20 frames = ~1 second at 20 FPS)
                                if self.stable_frames % 20 == 0:
                                    remaining = self.auto_capture_delay - time_since_last_capture
                                    self.log(f"Auto-capture ready, waiting for cooldown: {remaining:.1f}s remaining", level="info")

                            if time_since_last_capture >= self.auto_capture_delay:
                                # Double-check auto_capture_enabled before triggering (avoid race condition)
                                if self.auto_capture_enabled and self.auto_capture_callback:
                                    self.log(f"Auto-capturing card (stable={self.stable_frames}/{self.required_stable_frames}, cooldown={time_since_last_capture:.1f}s)", level="info")
                                    self.last_auto_capture_time = time.time()
                                    self.card_under_review = True  # Set flag to prevent further auto-captures
                                    # Call the callback in a non-blocking way
                                    threading.Thread(target=self.auto_capture_callback).start()

                with self.frame_lock:
                    self.current_frame = frame
                    self.annotated_frame = annotated
                    
                time.sleep(1.0 / Config.CAMERA_FPS)
            except Exception as e:
                self.log(f"Error capturing frame: {e}", level="error")
                time.sleep(0.1)
    
    def get_frame(self, annotated=True):
        """Get current frame for streaming"""
        with self.frame_lock:
            if annotated and self.annotated_frame is not None:
                return self.annotated_frame.copy()
            elif self.current_frame is not None:
                return self.current_frame.copy()
        return None
    
    def get_detected_card(self):
        """
        Get the currently detected card image

        Returns:
            tuple: (image, card_name, is_warped) - is_warped is True when the image is a
            perspective-corrected card from outline detection; (None, "", False) if no card
        """
        with self.frame_lock:
            detected, card_name = self.detected_card, self.detected_card_name
        if detected is None:
            return None, "", False

        # Frames are never modified after capture, so cropping outside the lock is safe
        frame, bbox, corners = detected
        if corners is not None:
            return warp_card(frame, corners), card_name, True
        x1, y1, x2, y2 = bbox
        return frame[y1:y2, x1:x2].copy(), card_name, False
    
    def is_card_detected(self):
        """Check if a card is currently detected"""
        with self.frame_lock:
            return self.card_detected

    def get_detection_status(self):
        """Get detailed detection status for enhanced UI feedback"""
        with self.frame_lock:
            return {
                'detected': self.card_detected,
                'stable_frames': self.stable_frames,
                'required_frames': self.required_stable_frames,
                'is_stable': self.stable_frames >= self.required_stable_frames
            }

    def capture_card_image_only(self, card_number):
        """
        Capture and save a card image WITHOUT AI processing.
        Returns: (image_path, card_image_rgb, is_warped) or (None, None, False) on failure
        This is used for async AI processing in Fast Scan Mode.
        """
        self.log(f"Capturing card #{card_number}...")

        # Wait a moment for frames to stabilize
        self.log("Waiting for stable image...")
        time.sleep(0.3)  # Brief pause to let autofocus settle

        card_image, card_name, is_warped = self.get_detected_card()

        if card_image is None:
            # No card detected - capture full frame
            self.log("No card detected, capturing full frame", level="info")
            frame = self.get_frame(annotated=False)
            if frame is None:
                self.log("Failed to capture frame", level="error")
                return None, None, False
            card_image = frame
        else:
            self.log(f"Using detected card crop ({card_image.shape[1]}x{card_image.shape[0]}px)", level="info")

        # Apply anti-glare preprocessing if enabled (for foil/glossy cards)
        card_image_final = card_image
        if self.anti_glare_enabled:
            self.log("Applying anti-glare preprocessing for foil card...")
            from anti_glare import reduce_glare_adaptive
            card_image_final = reduce_glare_adaptive(card_image)

        # Save image
        timestamp = int(time.time())
        image_path = Config.IMAGES_DIR / f"card_{card_number}_{timestamp}.jpg"

        card_bgr = cv2.cvtColor(card_image_final, cv2.COLOR_RGB2BGR)
        cv2.imwrite(str(image_path), card_bgr)

        self.log(f"Card captured: {image_path.name}")
        return image_path, card_image_final, is_warped

    def identify_card_from_image(self, card_image_rgb, detect_foil=False):
        """
        Identify a card using Vision AI from a preprocessed RGB image.
        detect_foil: also read the star/dot foil marker - only reliable for
        perspective-corrected captures, where the corner is at a known position.
        Returns: card_info dict with name, collector_number, foil ('foil'|'non-foil'|'unknown')
        This is used for async AI processing.
        """
        card_info = None
        if self.card_identifier:
            self.log("Identifying card with Vision AI...")
            card_info = self.card_identifier.identify_card(card_image_rgb)

            if card_info and card_info.get('name'):
                name = card_info['name']
                number = card_info.get('collector_number', '')
                if number:
                    self.log(f"✓ Card identified: {name} #{number}", level="success")
                else:
                    self.log(f"✓ Card identified: {name} (no collector number)", level="success")

                card_info['foil'] = 'unknown'
                if detect_foil and Config.VISION_AI_DETECT_FOIL:
                    card_info['foil'] = self.card_identifier.read_foil_symbol(card_image_rgb)
            else:
                self.log("Vision AI could not identify card", level="warning")
        else:
            self.log("⚠ Vision AI not enabled - set API key to enable automatic identification", level="warning")

        return card_info

    def capture_card_image(self, card_number):
        """
        Capture a still image of the card and identify it with AI (SYNCHRONOUS).
        This is the original method used for manual capture and Normal Auto-Scan Mode.
        Returns: (image_path, card_info)
        """
        image_path, card_image, is_warped = self.capture_card_image_only(card_number)
        if image_path is None:
            return None, None

        card_info = self.identify_card_from_image(card_image, detect_foil=is_warped)
        return image_path, card_info

    def _run_v4l2_command(self, *args):
        """Run a v4l2-ctl command"""
        try:
            video_device = f'/dev/video{Config.USB_CAMERA_INDEX}'
            cmd = ['v4l2-ctl', '-d', video_device] + list(args)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            if result.returncode != 0:
                self.log(f"v4l2-ctl error: {result.stderr}", level="warning")
                return False
            return True
        except subprocess.TimeoutExpired:
            self.log("v4l2-ctl command timed out", level="warning")
            return False
        except Exception as e:
            self.log(f"v4l2-ctl command failed: {e}", level="warning")
            return False

    def _enable_v4l2_autofocus(self):
        """Enable continuous autofocus using v4l2-ctl"""
        success = self._run_v4l2_command('-c', 'focus_automatic_continuous=1')
        if success:
            self.log("Enabled continuous autofocus via v4l2-ctl")
        return success

    def reset_focus(self):
        """Reset focus - re-enable autofocus for refocusing"""
        if self.camera and self.camera_type == 'usb':
            try:
                # Re-enable autofocus to force refocus
                if self._enable_v4l2_autofocus():
                    self.log("Focus reset - autofocus re-enabled for refocusing", level="success")
                    return True
                else:
                    self.log("Failed to reset focus via v4l2-ctl", level="warning")
                    return False
            except Exception as e:
                self.log(f"Failed to reset focus: {e}", level="error")
                return False
        else:
            self.log("Focus reset only available for USB cameras", level="warning")
            return False

    def set_detection_enabled(self, enabled):
        """Enable or disable card detection"""
        self.enable_detection = enabled
        self.log(f"Card detection {'enabled' if enabled else 'disabled'}")

    def set_anti_glare_enabled(self, enabled):
        """Enable or disable anti-glare preprocessing"""
        self.anti_glare_enabled = enabled
        self.log(f"Anti-glare preprocessing {'enabled' if enabled else 'disabled'}")

    def cleanup(self):
        """Clean up camera resources"""
        self.running = False
        if self.capture_thread:
            self.capture_thread.join(timeout=2)

        if self.camera:
            if self.camera_type == 'usb':
                self.camera.release()
            elif self.camera_type == 'picamera':
                self.camera.stop()

        self.log("Camera stopped")
