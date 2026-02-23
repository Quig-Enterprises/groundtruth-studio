"""
VLM Reviewer — Vision Language Model integration for AI-assisted reclassification.
Uses Ollama's llama3.2-vision to pre-analyze YOLO detections and suggest reclassifications
for false positives (trees, shadows, signs detected as vehicles).
"""
import base64
import json
import logging
import subprocess
import requests
from io import BytesIO
from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

# ---- Configuration ----
OLLAMA_URL = 'http://localhost:11434'
VLM_MODEL = 'llama3.2-vision'
VLM_TIMEOUT = 30
VLM_ENABLED = True
VLM_CONFIDENCE_THRESHOLD = 0.6  # Minimum VLM confidence to suggest reclassification
SKIP_ABOVE_CONFIDENCE = 0.95    # Skip VLM for high-confidence YOLO detections


def classify_detection(video_path, timestamp, bbox, predicted_class, confidence, scenario='vehicle_detection'):
    """
    Main entry point. Analyzes a detection using VLM.

    Args:
        video_path: Path to the video file
        timestamp: Detection timestamp in seconds
        bbox: dict with x, y, width, height keys
        predicted_class: YOLO's predicted class string
        confidence: YOLO confidence score (0-1)
        scenario: Detection scenario string

    Returns:
        dict with keys: suggested_class, confidence, reasoning, is_vehicle
        or None on failure (non-blocking)
    """
    if not VLM_ENABLED:
        return None

    # Skip high-confidence detections
    if confidence >= SKIP_ABOVE_CONFIDENCE:
        return None

    try:
        # Step 1: Extract frame from video at timestamp
        frame_bytes = _extract_frame(video_path, timestamp)
        if not frame_bytes:
            return None

        # Step 2: Create cropped detection region (with 20% padding)
        full_frame = Image.open(BytesIO(frame_bytes)).convert('RGB')
        crop_img = _crop_with_padding(full_frame, bbox, padding=0.2)

        # Step 3: Draw red box on full frame at detection location
        annotated_frame = _draw_detection_box(full_frame.copy(), bbox)

        # Step 4: Create composite image and encode as base64
        composite = _make_composite(crop_img, annotated_frame)
        composite_b64 = _image_to_base64(composite)

        # Step 5: Send to Ollama API
        result = _query_ollama(composite_b64, predicted_class, confidence, scenario)

        return result

    except Exception as e:
        logger.warning(f"VLM classify_detection failed: {e}")
        return None


def _extract_frame(video_path, timestamp):
    """Extract a single frame from video at the given timestamp using ffmpeg."""
    try:
        cmd = [
            'ffmpeg', '-ss', str(float(timestamp)),
            '-i', str(video_path),
            '-frames:v', '1',
            '-f', 'image2pipe',
            '-vcodec', 'mjpeg',
            '-q:v', '2',
            '-'
        ]
        result = subprocess.run(
            cmd, capture_output=True, timeout=10
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
        logger.warning(f"ffmpeg frame extraction failed: rc={result.returncode}")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg frame extraction timed out")
        return None
    except Exception as e:
        logger.warning(f"Frame extraction error: {e}")
        return None


def _crop_with_padding(image, bbox, padding=0.2):
    """Crop the detection region with padding for context."""
    x, y, w, h = bbox.get('x', 0), bbox.get('y', 0), bbox.get('width', 0), bbox.get('height', 0)

    # Add padding
    pad_w = w * padding
    pad_h = h * padding

    x1 = max(0, int(x - pad_w))
    y1 = max(0, int(y - pad_h))
    x2 = min(image.width, int(x + w + pad_w))
    y2 = min(image.height, int(y + h + pad_h))

    if x2 <= x1 or y2 <= y1:
        return image  # Return full image if crop is invalid

    return image.crop((x1, y1, x2, y2))


def _draw_detection_box(image, bbox):
    """Draw a red rectangle on the image at the detection location."""
    draw = ImageDraw.Draw(image)
    x, y, w, h = bbox.get('x', 0), bbox.get('y', 0), bbox.get('width', 0), bbox.get('height', 0)
    draw.rectangle(
        [int(x), int(y), int(x + w), int(y + h)],
        outline='red', width=3
    )
    return image


def _image_to_base64(image):
    """Convert PIL Image to base64 string."""
    buf = BytesIO()
    image.save(buf, format='JPEG', quality=85)
    return base64.b64encode(buf.getvalue()).decode('utf-8')


def _make_composite(crop_img, annotated_frame):
    """Create side-by-side composite: crop on left, annotated full frame on right.

    Many vision models (including llama3.2-vision) only support a single image,
    so we combine both views into one image.
    """
    # Scale crop to match annotated frame height
    frame_w, frame_h = annotated_frame.size
    crop_w, crop_h = crop_img.size
    if crop_h > 0:
        scale = frame_h / crop_h
        scaled_crop = crop_img.resize((max(1, int(crop_w * scale)), frame_h), Image.LANCZOS)
    else:
        scaled_crop = crop_img

    sc_w, sc_h = scaled_crop.size
    gap = 10
    composite = Image.new('RGB', (sc_w + gap + frame_w, frame_h), (40, 40, 40))
    composite.paste(scaled_crop, (0, 0))
    composite.paste(annotated_frame, (sc_w + gap, 0))
    return composite


def _query_ollama(composite_b64, predicted_class, confidence, scenario):
    """Send composite image to Ollama API and parse structured JSON response."""
    conf_pct = round(confidence * 100, 1)

    prompt = (
        f'You are a strict quality-control reviewer for a vehicle detection AI.\n'
        f'The AI detected this as: "{predicted_class}" (confidence: {conf_pct}%)\n\n'
        f'The image shows two views side by side:\n'
        f'LEFT: Cropped detection region\n'
        f'RIGHT: Full camera frame (detection marked with red box)\n\n'
        f'RULES:\n'
        f'- is_vehicle=true means ANY motorized transport: cars, trucks, SUVs, vans, buses, motorcycles, ATVs, UTVs, sedans, semi trucks, trailers, snowmobiles, etc.\n'
        f'- IMPORTANT: A car IS a vehicle. A sedan IS a vehicle. A minivan IS a vehicle. A trailer IS a vehicle. ALL motorized transport counts as a vehicle.\n'
        f'- Trailers (flatbed, enclosed, utility) are vehicles even when parked or unhitched\n'
        f'- Snowmobiles are vehicles. A snowmobile on a trailer counts as a vehicle\n'
        f'- is_vehicle=false ONLY for non-motorized objects: tree, shadow, sign, person, building, snow, mailbox, animal, fence, chair, table, ladder, flag, etc.\n'
        f'- If it IS a vehicle but a different type than predicted, set is_vehicle=true with the correct type\n'
        f'- Be SKEPTICAL: low-confidence detections ({conf_pct}%) are often false positives like shadows, snow, or background objects\n'
        f'- Look carefully at the LEFT crop — can you clearly see a real motor vehicle, trailer, or snowmobile?\n\n'
        f'Respond ONLY with JSON: {{"is_vehicle": bool, "suggested_class": "...", "confidence": 0.0-1.0, "reasoning": "..."}}'
    )

    payload = {
        'model': VLM_MODEL,
        'prompt': prompt,
        'images': [composite_b64],
        'stream': False,
        'options': {
            'temperature': 0.1,
            'num_predict': 256
        }
    }

    try:
        resp = requests.post(
            f'{OLLAMA_URL}/api/generate',
            json=payload,
            timeout=VLM_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()

        response_text = data.get('response', '')
        return _parse_vlm_response(response_text)

    except requests.Timeout:
        logger.warning(f"Ollama request timed out after {VLM_TIMEOUT}s")
        return None
    except requests.ConnectionError:
        logger.warning("Cannot connect to Ollama — is it running?")
        return None
    except Exception as e:
        logger.warning(f"Ollama query failed: {e}")
        return None


def _parse_vlm_response(response_text):
    """Parse VLM response text, extracting JSON from potentially noisy output."""
    try:
        # Try direct JSON parse first
        result = json.loads(response_text.strip())
    except json.JSONDecodeError:
        # Try to find JSON in the response
        import re
        match = re.search(r'\{[^{}]*"is_vehicle"[^{}]*\}', response_text, re.DOTALL)
        if not match:
            logger.warning(f"Could not parse VLM response as JSON: {response_text[:200]}")
            return None
        try:
            result = json.loads(match.group())
        except json.JSONDecodeError:
            logger.warning(f"JSON extraction failed from VLM response: {response_text[:200]}")
            return None

    # Validate required fields
    if 'is_vehicle' not in result:
        logger.warning(f"VLM response missing 'is_vehicle' field: {result}")
        return None

    # Normalize and validate
    return {
        'is_vehicle': bool(result.get('is_vehicle', True)),
        'suggested_class': str(result.get('suggested_class', 'unknown')),
        'confidence': max(0.0, min(1.0, float(result.get('confidence', 0.5)))),
        'reasoning': str(result.get('reasoning', ''))[:500]
    }


def get_config():
    """Return current VLM configuration."""
    return {
        'enabled': VLM_ENABLED,
        'model': VLM_MODEL,
        'ollama_url': OLLAMA_URL,
        'timeout': VLM_TIMEOUT,
        'confidence_threshold': VLM_CONFIDENCE_THRESHOLD,
        'skip_above_confidence': SKIP_ABOVE_CONFIDENCE
    }


def update_config(new_config):
    """Update VLM configuration at runtime."""
    global VLM_ENABLED, VLM_MODEL, VLM_TIMEOUT, VLM_CONFIDENCE_THRESHOLD, SKIP_ABOVE_CONFIDENCE

    if 'enabled' in new_config:
        VLM_ENABLED = bool(new_config['enabled'])
    if 'model' in new_config:
        VLM_MODEL = str(new_config['model'])
    if 'timeout' in new_config:
        VLM_TIMEOUT = max(5, min(120, int(new_config['timeout'])))
    if 'confidence_threshold' in new_config:
        VLM_CONFIDENCE_THRESHOLD = max(0.0, min(1.0, float(new_config['confidence_threshold'])))
    if 'skip_above_confidence' in new_config:
        SKIP_ABOVE_CONFIDENCE = max(0.0, min(1.0, float(new_config['skip_above_confidence'])))

    return get_config()


def check_ollama_status():
    """Check if Ollama is running and the VLM model is available."""
    try:
        resp = requests.get(f'{OLLAMA_URL}/api/tags', timeout=5)
        resp.raise_for_status()
        models = resp.json().get('models', [])
        model_names = [m.get('name', '') for m in models]
        has_model = any(VLM_MODEL in name for name in model_names)
        return {
            'ollama_running': True,
            'model_available': has_model,
            'available_models': model_names
        }
    except Exception as e:
        return {
            'ollama_running': False,
            'model_available': False,
            'error': str(e)
        }
