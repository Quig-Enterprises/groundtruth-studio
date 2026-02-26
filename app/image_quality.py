"""
Image Quality Scoring for Detection Crops

Computes quality metrics (sharpness, brightness, contrast, area) for
bounding box crops to filter out blurry, dark, or ambiguous detections
before they pollute training data.
"""

import logging
import numpy as np
import cv2

logger = logging.getLogger(__name__)


def compute_crop_quality(image_bgr, bbox: dict) -> dict:
    """Compute quality metrics for a detection crop.

    Args:
        image_bgr: numpy array (BGR format) or PIL Image of the full frame
        bbox: dict with 'x', 'y', 'width', 'height' keys

    Returns:
        dict with keys:
            quality_score: float 0-1 (normalized composite)
            sharpness: float (Laplacian variance - higher is sharper)
            brightness: float (mean pixel value 0-255)
            contrast: float (pixel standard deviation)
            area: int (crop pixel area)
            usable: bool (passes all minimum thresholds)
            flags: list of string reasons if not usable
    """
    try:
        # Handle PIL Image input
        if not isinstance(image_bgr, np.ndarray):
            image_bgr = np.array(image_bgr.convert('RGB'))
            image_bgr = cv2.cvtColor(image_bgr, cv2.COLOR_RGB2BGR)

        x = int(bbox.get('x', 0))
        y = int(bbox.get('y', 0))
        w = int(bbox.get('width', 0))
        h = int(bbox.get('height', 0))

        # Clamp to image bounds
        img_h, img_w = image_bgr.shape[:2]
        x = max(0, min(x, img_w - 1))
        y = max(0, min(y, img_h - 1))
        w = min(w, img_w - x)
        h = min(h, img_h - y)

        if w < 5 or h < 5:
            return {
                'quality_score': 0.0,
                'sharpness': 0.0,
                'brightness': 0.0,
                'contrast': 0.0,
                'area': w * h,
                'usable': False,
                'flags': ['too_small'],
            }

        # Extract crop
        crop = image_bgr[y:y+h, x:x+w]

        # Convert to grayscale for metrics
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

        # Sharpness: variance of Laplacian (higher = sharper)
        laplacian = cv2.Laplacian(gray, cv2.CV_64F)
        sharpness = float(laplacian.var())

        # Brightness: mean pixel intensity
        brightness = float(gray.mean())

        # Contrast: standard deviation of pixel intensities
        contrast = float(gray.std())

        # Area
        area = w * h

        # Determine usability flags
        flags = []
        if sharpness < 50:
            flags.append('too_blurry')
        if brightness < 30:
            flags.append('too_dark')
        if brightness > 240:
            flags.append('washed_out')
        if contrast < 15:
            flags.append('low_contrast')
        if area < 3600:  # 60x60
            flags.append('too_small')

        usable = len(flags) == 0

        # Composite quality score: normalized product of sharpness and contrast
        # Clamped to [0, 1]
        quality_score = min(1.0, (sharpness / 500.0) * (contrast / 80.0))
        quality_score = max(0.0, quality_score)

        return {
            'quality_score': round(quality_score, 4),
            'sharpness': round(sharpness, 2),
            'brightness': round(brightness, 2),
            'contrast': round(contrast, 2),
            'area': area,
            'usable': usable,
            'flags': flags,
        }

    except Exception as e:
        logger.warning(f"Failed to compute crop quality: {e}")
        return {
            'quality_score': 0.0,
            'sharpness': 0.0,
            'brightness': 0.0,
            'contrast': 0.0,
            'area': 0,
            'usable': False,
            'flags': ['error'],
        }
