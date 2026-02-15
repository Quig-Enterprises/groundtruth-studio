#!/usr/bin/env python3
"""
Export corrected vehicle predictions from GT Studio as training data.

This script exports vehicle predictions (both human-corrected and approved) from the
GT Studio database in formats suitable for training classification and detection models.
"""

import argparse
import os
import sys
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Tuple, Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from PIL import Image


# Vehicle type classes in canonical order for detection training
VEHICLE_TYPES = [
    'sedan', 'pickup truck', 'SUV', 'minivan', 'van',
    'tractor', 'ATV', 'UTV', 'snowmobile', 'golf cart', 'motorcycle', 'trailer',
    'bus', 'semi truck', 'dump truck',
    'rowboat', 'fishing boat', 'speed boat', 'pontoon boat', 'kayak', 'canoe', 'sailboat', 'jet ski'
]

# Database connection string
DB_CONN_STRING = "postgresql://groundtruth:bZv6QbJ8KCAQubJFb+frmbGNKUiPm7lBUg0XgMvEzNQ=@localhost:5432/groundtruth_studio"

# Thumbnails directory
THUMBNAILS_DIR = "/opt/groundtruth-studio/thumbnails/"


def sanitize_class_name(class_name: str) -> str:
    """Convert class name to valid folder name."""
    return class_name.replace(' ', '_').replace('/', '_')


def get_vehicle_class(prediction: dict) -> Optional[str]:
    """
    Extract vehicle class from prediction.
    Uses corrected_tags if available, otherwise predicted_tags.
    """
    # Try corrected tags first
    if prediction.get('corrected_tags') and prediction['corrected_tags'].get('vehicle_type'):
        return prediction['corrected_tags']['vehicle_type']

    # Fall back to predicted tags
    if prediction.get('predicted_tags') and prediction['predicted_tags'].get('vehicle_type'):
        return prediction['predicted_tags']['vehicle_type']

    return None


def get_class_id(vehicle_type: str) -> int:
    """Get class ID for vehicle type (for YOLO format)."""
    try:
        return VEHICLE_TYPES.index(vehicle_type)
    except ValueError:
        # Unknown class, assign to last index + 1
        return len(VEHICLE_TYPES)


def crop_bbox_with_padding(image: Image.Image, bbox: dict, padding: float = 0.1) -> Image.Image:
    """
    Crop bounding box from image with padding.

    Args:
        image: PIL Image
        bbox: Dict with bbox_x, bbox_y, bbox_width, bbox_height (pixel coordinates)
        padding: Fraction of bbox size to add as padding (default 10%)

    Returns:
        Cropped PIL Image
    """
    img_width, img_height = image.size

    x = bbox['bbox_x']
    y = bbox['bbox_y']
    w = bbox['bbox_width']
    h = bbox['bbox_height']

    # Add padding
    pad_w = w * padding
    pad_h = h * padding

    x1 = max(0, x - pad_w)
    y1 = max(0, y - pad_h)
    x2 = min(img_width, x + w + pad_w)
    y2 = min(img_height, y + h + pad_h)

    if x2 <= x1 or y2 <= y1:
        # Fallback: use raw bbox without padding
        x1, y1 = max(0, x), max(0, y)
        x2 = min(img_width, x + w)
        y2 = min(img_height, y + h)

    return image.crop((x1, y1, x2, y2))


def fetch_predictions(conn, corrections_only: bool, include_approved: bool, min_confidence: float) -> List[dict]:
    """
    Fetch vehicle predictions from database.

    Args:
        conn: Database connection
        corrections_only: Only fetch human-corrected predictions
        include_approved: Include approved (non-corrected) predictions
        min_confidence: Minimum confidence for approved predictions

    Returns:
        List of prediction dictionaries
    """
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    # Build WHERE clause
    conditions = []
    params = []

    if corrections_only:
        # Only human-corrected vehicle reclassifications
        conditions.append("correction_type = %s")
        params.append('vehicle_reclass')
    else:
        if include_approved:
            # Both corrected and approved vehicle predictions
            conditions.append("""(
                correction_type = %s OR
                (correction_type IS NULL AND predicted_tags->>'vehicle_type' IS NOT NULL AND confidence >= %s)
            )""")
            params.extend(['vehicle_reclass', min_confidence])
        else:
            # Only corrected
            conditions.append("correction_type = %s")
            params.append('vehicle_reclass')

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    query = f"""
        SELECT
            p.id,
            p.video_id,
            p.bbox_x,
            p.bbox_y,
            p.bbox_width,
            p.bbox_height,
            p.predicted_tags,
            p.corrected_tags,
            p.correction_type,
            p.confidence,
            v.thumbnail_path
        FROM ai_predictions p
        JOIN videos v ON p.video_id = v.id
        WHERE {where_clause}
        ORDER BY p.id
    """

    cursor.execute(query, params)
    results = cursor.fetchall()
    cursor.close()

    return [dict(row) for row in results]


def export_classification(predictions: List[dict], output_dir: Path, dry_run: bool) -> Dict[str, int]:
    """
    Export vehicle crops sorted into class folders.

    Args:
        predictions: List of prediction dicts
        output_dir: Output directory path
        dry_run: If True, don't write files

    Returns:
        Dict mapping class names to counts
    """
    train_dir = output_dir / 'train'
    stats = defaultdict(int)
    skipped = 0

    for pred in predictions:
        # Get vehicle class
        vehicle_class = get_vehicle_class(pred)
        if not vehicle_class:
            skipped += 1
            continue

        # Get thumbnail path
        if not pred.get('thumbnail_path'):
            skipped += 1
            continue
        thumb_rel = os.path.basename(pred['thumbnail_path'])
        thumbnail_path = Path(THUMBNAILS_DIR) / thumb_rel
        if not thumbnail_path.exists():
            print(f"Warning: Thumbnail not found: {thumbnail_path}", file=sys.stderr)
            skipped += 1
            continue

        # Create class folder
        class_folder = train_dir / sanitize_class_name(vehicle_class)
        if not dry_run:
            class_folder.mkdir(parents=True, exist_ok=True)

        # Load and crop image
        try:
            image = Image.open(thumbnail_path)
            cropped = crop_bbox_with_padding(image, pred)

            # Generate output filename
            output_filename = f"pred_{pred['id']}.jpg"
            output_path = class_folder / output_filename

            if not dry_run:
                cropped.save(output_path, 'JPEG', quality=95)

            stats[vehicle_class] += 1

        except Exception as e:
            print(f"Error processing prediction {pred['id']}: {e}", file=sys.stderr)
            skipped += 1
            continue

    if skipped > 0:
        print(f"\nSkipped {skipped} predictions due to errors or missing data", file=sys.stderr)

    return dict(stats)


def export_detection(predictions: List[dict], output_dir: Path, dry_run: bool) -> Dict[str, int]:
    """
    Export full images + YOLO format labels for detection training.

    Args:
        predictions: List of prediction dicts
        output_dir: Output directory path
        dry_run: If True, don't write files

    Returns:
        Dict mapping class names to counts
    """
    images_dir = output_dir / 'images'
    labels_dir = output_dir / 'labels'

    if not dry_run:
        images_dir.mkdir(parents=True, exist_ok=True)
        labels_dir.mkdir(parents=True, exist_ok=True)

    stats = defaultdict(int)
    skipped = 0

    # Group predictions by video_id (thumbnail)
    by_video = defaultdict(list)
    for pred in predictions:
        vehicle_class = get_vehicle_class(pred)
        if vehicle_class:
            by_video[pred['video_id']].append(pred)

    # Process each unique image
    for video_id, video_predictions in by_video.items():
        # Get thumbnail path from first prediction
        if not video_predictions[0].get('thumbnail_path'):
            skipped += len(video_predictions)
            continue
        thumb_rel = os.path.basename(video_predictions[0]['thumbnail_path'])
        thumbnail_path = Path(THUMBNAILS_DIR) / thumb_rel
        if not thumbnail_path.exists():
            print(f"Warning: Thumbnail not found: {thumbnail_path}", file=sys.stderr)
            skipped += len(video_predictions)
            continue

        # Copy image
        output_image_name = f"video_{video_id}.jpg"
        output_image_path = images_dir / output_image_name

        try:
            if not dry_run:
                image = Image.open(thumbnail_path)
                image.save(output_image_path, 'JPEG', quality=95)
                img_width, img_height = image.size
            else:
                # For dry run, still need dimensions
                image = Image.open(thumbnail_path)
                img_width, img_height = image.size

            # Create YOLO format label file
            output_label_name = f"video_{video_id}.txt"
            output_label_path = labels_dir / output_label_name

            label_lines = []
            for pred in video_predictions:
                vehicle_class = get_vehicle_class(pred)
                if not vehicle_class:
                    continue

                class_id = get_class_id(vehicle_class)

                # YOLO format: class_id center_x center_y width height (all normalized 0-1)
                # Our bbox is: x, y, width, height (pixel coordinates)
                # Convert to normalized center coordinates
                cx = (pred['bbox_x'] + pred['bbox_width'] / 2) / img_width
                cy = (pred['bbox_y'] + pred['bbox_height'] / 2) / img_height
                w = pred['bbox_width'] / img_width
                h = pred['bbox_height'] / img_height

                label_lines.append(f"{class_id} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}\n")
                stats[vehicle_class] += 1

            if not dry_run and label_lines:
                with open(output_label_path, 'w') as f:
                    f.writelines(label_lines)

        except Exception as e:
            print(f"Error processing video {video_id}: {e}", file=sys.stderr)
            skipped += len(video_predictions)
            continue

    if skipped > 0:
        print(f"\nSkipped {skipped} predictions due to errors or missing data", file=sys.stderr)

    return dict(stats)


def print_statistics(stats: Dict[str, int], mode: str):
    """Print export statistics table."""
    if not stats:
        print("\nNo data exported.")
        return

    print(f"\n{'='*60}")
    print(f"Export Statistics - {mode.upper()} mode")
    print(f"{'='*60}")
    print(f"{'Class':<30} {'Count':>10}")
    print(f"{'-'*60}")

    total = 0
    for class_name in sorted(stats.keys()):
        count = stats[class_name]
        print(f"{class_name:<30} {count:>10}")
        total += count

    print(f"{'-'*60}")
    print(f"{'TOTAL':<30} {total:>10}")
    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(
        description='Export corrected vehicle predictions from GT Studio as training data',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Export classification data (default)
  %(prog)s

  # Export detection data
  %(prog)s --mode detection

  # Export only human corrections
  %(prog)s --corrections-only

  # Include approved predictions with min confidence
  %(prog)s --include-approved --min-confidence 0.7

  # Dry run to see what would be exported
  %(prog)s --dry-run
        """
    )

    parser.add_argument(
        '-o', '--output',
        type=Path,
        default=Path('/models/custom/people-vehicles-objects/data/vehicle_corrections'),
        help='Output directory (default: /models/custom/people-vehicles-objects/data/vehicle_corrections)'
    )

    parser.add_argument(
        '--mode',
        choices=['classification', 'detection', 'both'],
        default='classification',
        help='Export mode (default: classification)'
    )

    parser.add_argument(
        '--min-confidence',
        type=float,
        default=0.5,
        help='Minimum confidence for approved (non-corrected) predictions (default: 0.5)'
    )

    parser.add_argument(
        '--corrections-only',
        action='store_true',
        help='Only export predictions that were human-corrected'
    )

    parser.add_argument(
        '--include-approved',
        action='store_true',
        help='Also include approved (non-corrected) vehicle predictions as training data'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be exported without writing files'
    )

    args = parser.parse_args()

    # Validate arguments
    if args.corrections_only and args.include_approved:
        parser.error("--corrections-only and --include-approved are mutually exclusive")

    if args.dry_run:
        print("DRY RUN MODE - No files will be written\n")

    # Connect to database
    try:
        conn = psycopg2.connect(DB_CONN_STRING)
    except Exception as e:
        print(f"Error connecting to database: {e}", file=sys.stderr)
        return 1

    try:
        # Fetch predictions
        print("Fetching predictions from database...")
        predictions = fetch_predictions(
            conn,
            args.corrections_only,
            args.include_approved,
            args.min_confidence
        )
        print(f"Found {len(predictions)} predictions")

        if not predictions:
            print("No predictions found matching criteria")
            return 0

        # Export based on mode
        if args.mode in ('classification', 'both'):
            print(f"\nExporting classification data to {args.output / 'train'}...")
            stats_class = export_classification(predictions, args.output, args.dry_run)
            print_statistics(stats_class, 'classification')

        if args.mode in ('detection', 'both'):
            print(f"\nExporting detection data to {args.output}...")
            stats_det = export_detection(predictions, args.output, args.dry_run)
            print_statistics(stats_det, 'detection')

        if args.dry_run:
            print("\nDRY RUN COMPLETE - No files were written")
        else:
            print(f"\nExport complete! Data saved to {args.output}")

        return 0

    except Exception as e:
        print(f"Error during export: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1

    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
