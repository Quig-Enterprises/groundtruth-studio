"""Compact HSV color histogram for vehicle color matching.

Produces a 60-bin normalized histogram:
  - 12 hue bins (achromatic pixels excluded)
  - 3 saturation bins x 3 value bins for achromatic pixels (S < 0.15)
  - Plus 12 hue x 4 saturation-value combos = 48 chromatic bins
  Total: 12 achromatic + 48 chromatic = 60 bins

Returns a list of floats summing to ~1.0, suitable for JSON storage and
histogram intersection comparison.
"""
import json
import numpy as np
from PIL import Image


# Thresholds
SAT_THRESHOLD = 38   # out of 255; below this, pixel is "achromatic"
N_HUE_BINS = 12
N_ACHRO_BINS = 9     # 3 sat levels x 3 val levels for low-sat pixels
HIST_SIZE = N_HUE_BINS * 4 + N_ACHRO_BINS  # 48 + 9 = 57


def compute_color_hist(image_path):
    """Compute a compact color histogram from a crop image file.

    Args:
        image_path: Path to JPEG crop file.

    Returns:
        list of floats (length HIST_SIZE), or None on failure.
    """
    try:
        img = Image.open(image_path).convert('RGB')
        # Resize to small size for speed (color doesn't need resolution)
        img = img.resize((64, 64), Image.BILINEAR)
        arr = np.array(img, dtype=np.float32)

        # Convert RGB to HSV manually for speed (avoid opencv dependency)
        r, g, b = arr[:, :, 0] / 255.0, arr[:, :, 1] / 255.0, arr[:, :, 2] / 255.0
        cmax = np.maximum(np.maximum(r, g), b)
        cmin = np.minimum(np.minimum(r, g), b)
        delta = cmax - cmin

        # Hue (0-360)
        hue = np.zeros_like(cmax)
        mask_r = (cmax == r) & (delta > 0)
        mask_g = (cmax == g) & (delta > 0)
        mask_b = (cmax == b) & (delta > 0)
        hue[mask_r] = 60.0 * (((g[mask_r] - b[mask_r]) / delta[mask_r]) % 6)
        hue[mask_g] = 60.0 * (((b[mask_g] - r[mask_g]) / delta[mask_g]) + 2)
        hue[mask_b] = 60.0 * (((r[mask_b] - g[mask_b]) / delta[mask_b]) + 4)

        # Saturation (0-255 scale)
        sat = np.zeros_like(cmax)
        nonzero = cmax > 0
        sat[nonzero] = (delta[nonzero] / cmax[nonzero]) * 255.0

        # Value (0-255 scale)
        val = cmax * 255.0

        # Flatten
        hue_flat = hue.ravel()
        sat_flat = sat.ravel()
        val_flat = val.ravel()

        hist = np.zeros(HIST_SIZE, dtype=np.float64)

        # Achromatic pixels (low saturation)
        achro_mask = sat_flat < SAT_THRESHOLD
        if achro_mask.any():
            # 3x3 grid of sat x val
            s_bins = np.clip((sat_flat[achro_mask] / SAT_THRESHOLD * 3).astype(int), 0, 2)
            v_bins = np.clip((val_flat[achro_mask] / 256.0 * 3).astype(int), 0, 2)
            achro_idx = s_bins * 3 + v_bins
            for idx in achro_idx:
                hist[idx] += 1

        # Chromatic pixels
        chro_mask = ~achro_mask
        if chro_mask.any():
            h_bins = np.clip((hue_flat[chro_mask] / 360.0 * N_HUE_BINS).astype(int), 0, N_HUE_BINS - 1)
            # 2 sat levels x 2 val levels = 4 combos
            s_bins = np.clip(((sat_flat[chro_mask] - SAT_THRESHOLD) / (255 - SAT_THRESHOLD) * 2).astype(int), 0, 1)
            v_bins = np.clip((val_flat[chro_mask] / 256.0 * 2).astype(int), 0, 1)
            sv_idx = s_bins * 2 + v_bins
            chro_idx = N_ACHRO_BINS + h_bins * 4 + sv_idx
            for idx in chro_idx:
                hist[idx] += 1

        # Normalize
        total = hist.sum()
        if total > 0:
            hist /= total

        return [round(float(v), 6) for v in hist]
    except Exception:
        return None


def hist_intersection(h1, h2):
    """Histogram intersection similarity (0 to 1).

    Args:
        h1, h2: lists of floats (same length), normalized histograms.

    Returns:
        float between 0 and 1 (1 = identical color distribution).
    """
    if not h1 or not h2 or len(h1) != len(h2):
        return 0.0
    return sum(min(a, b) for a, b in zip(h1, h2))
