# object_detector.py

from ultralytics import YOLO
import cv2
import numpy as np

class ObjectDetector:
    """
    Handles object detection using a YOLOv8 model.
    """
    def __init__(self, model_path='yolov8n.pt'):
        """
        Initializes the ObjectDetector.

        Args:
            model_path (str): The path to the YOLOv8 model file.
        """
        self.model = YOLO(model_path)
        self.names = self.model.names

    def predict(self, image_path):
        """
        Performs object detection on an image file.

        Args:
            image_path (str): The path to the image file.

        Returns:
            tuple: A tuple containing (bounding_box, card_name, confidence), or (None, "", 0) if no card is detected.
        """
        img = cv2.imread(image_path)
        if img is None:
            return None, "", 0

        return self.predict_frame(img)

    def predict_frame(self, frame, conf_threshold=0.1, verbose=False, target_size=640):
        """
        Performs object detection on a raw image frame.

        Args:
            frame (np.ndarray): The image frame (from OpenCV).
            conf_threshold (float): Minimum confidence threshold (default 0.1 for permissive detection).
            verbose (bool): If True, print all detections for debugging.
            target_size (int): Target size for inference (default 640, YOLOv8n native size).

        Returns:
            tuple: A tuple containing (bounding_box, card_name, confidence), or (None, "", 0) if no card is detected.
        """
        if frame is None:
            return None, "", 0

        # Store original dimensions for coordinate scaling
        original_height, original_width = frame.shape[:2]

        # Downscale for inference (YOLO is trained on 640x640)
        # This significantly improves performance without sacrificing accuracy
        inference_frame = frame
        scale_factor = 1.0

        if original_width > target_size or original_height > target_size:
            scale = target_size / max(original_width, original_height)
            new_width = int(original_width * scale)
            new_height = int(original_height * scale)

            inference_frame = cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_AREA)
            scale_factor = scale

        # Use low confidence threshold to catch cards that don't match COCO classes well
        results = self.model.predict(inference_frame, conf=conf_threshold, verbose=False)

        if not results or len(results[0].boxes) == 0:
            return None, "", 0

        # Debug: Print all detections if verbose
        if verbose and len(results[0].boxes) > 0:
            print(f"\nDetected {len(results[0].boxes)} objects:")
            for i, box in enumerate(results[0].boxes):
                cls = int(box.cls)
                conf = float(box.conf)
                class_name = self.names[cls]
                print(f"  {i+1}. {class_name} (confidence: {conf:.3f})")

        # Filter out person detections (class 0) - cards are not people!
        # Person class often detects faces which interferes with card detection
        EXCLUDED_CLASSES = {0}  # 0 = person in COCO dataset

        filtered_boxes = []
        for box in results[0].boxes:
            cls = int(box.cls)
            if cls not in EXCLUDED_CLASSES:
                filtered_boxes.append(box)

        # If no boxes left after filtering, return None
        if len(filtered_boxes) == 0:
            return None, "", 0

        # Helper function to calculate aspect ratio
        def get_aspect_ratio(box):
            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
            width = x2 - x1
            height = y2 - y1
            if width == 0 or height == 0:
                return 0
            return max(width, height) / min(width, height)

        # Helper function to calculate area
        def get_box_area(box):
            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
            return (x2 - x1) * (y2 - y1)

        # Magic card aspect ratio: 88mm / 63mm = 1.397
        CARD_ASPECT_RATIO = 1.397

        # Debug: Print all detections with aspect ratios (only if verbose=True)
        # Set to True in predict_frame() call to enable debugging
        if verbose:
            print(f"\n=== YOLO Detections ({len(filtered_boxes)} objects) ===")
            for i, box in enumerate(filtered_boxes):
                x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                width = int(x2 - x1)
                height = int(y2 - y1)
                ratio = get_aspect_ratio(box)
                conf = float(box.conf)
                cls = int(box.cls)
                class_name = self.names[cls]
                match_diff = abs(ratio - CARD_ASPECT_RATIO)
                print(f"  [{i+1}] {class_name} | {width}x{height}px | AR={ratio:.3f} (diff={match_diff:.3f}) | conf={conf:.3f}")
            print("  Target: AR=1.397 (Magic card: 88mm x 63mm)")
            print("=" * 50)

        # If multiple detections, prefer the one with aspect ratio closest to Magic card
        # This helps when both a white box and card inside are detected
        def aspect_ratio_score(box):
            ratio = get_aspect_ratio(box)
            # Return negative absolute difference (so min() finds best match)
            return -abs(ratio - CARD_ASPECT_RATIO)

        # Sort by aspect ratio match (best match first)
        filtered_boxes.sort(key=aspect_ratio_score, reverse=True)
        top_prediction = filtered_boxes[0]

        # Bounding box coordinates (from inference frame)
        x1, y1, x2, y2 = top_prediction.xyxy[0].cpu().numpy().astype(int)

        # Scale bounding box back to original frame coordinates
        if scale_factor != 1.0:
            x1 = int(x1 / scale_factor)
            y1 = int(y1 / scale_factor)
            x2 = int(x2 / scale_factor)
            y2 = int(y2 / scale_factor)

        bounding_box = (x1, y1, x2, y2)

        # For card detection, we don't care about the YOLO class - just call it "Card"
        card_name = "Card"

        # Confidence score
        confidence = float(top_prediction.conf)

        return bounding_box, card_name, confidence
