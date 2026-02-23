#!/usr/bin/env python3
"""
Convert MIDV-500 dataset to YOLO object detection format.

MIDV-500 contains 50 document types, each with 10 video clips of 30 frames.
Each frame has a ground truth JSON with a 4-point quad marking the document
boundary. This script converts those quads to axis-aligned bounding boxes
in YOLO format (class x_center y_center width height, normalized 0-1).

Dataset:  https://arxiv.org/abs/1807.05786
Source:   /mnt/storage/training-material/documents/datasets/midv-500/
Output:   /mnt/storage/training-material/documents/yolo-doc-detect/midv500/

Class mapping (5-class document detection):
    0: passport
    1: drivers_license
    2: twic_card              (not in MIDV-500)
    3: merchant_mariner_credential  (not in MIDV-500)
    4: id_card_generic
"""

import argparse
import io
import json
import os
import sys
import zipfile
from pathlib import Path

from PIL import Image


# ---------------------------------------------------------------------------
# Class mapping
# ---------------------------------------------------------------------------

# Our unified 5-class scheme for document detection
CLASS_NAMES = {
    0: "passport",
    1: "drivers_license",
    2: "twic_card",
    3: "merchant_mariner_credential",
    4: "id_card_generic",
}


def classify_document(doc_code: str) -> int:
    """
    Map a MIDV-500 document code to one of our YOLO class IDs.

    Rules:
        - Contains 'passport'          -> 0 (passport)
        - Contains 'drvlic' or 'driving' -> 1 (drivers_license)
        - Everything else               -> 4 (id_card_generic)

    Examples:
        '05_aze_passport'    -> 0
        '02_aut_drvlic_new'  -> 1
        '01_alb_id'          -> 4
    """
    lower = doc_code.lower()
    if "passport" in lower:
        return 0
    if "drvlic" in lower or "driving" in lower:
        return 1
    return 4


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def quad_to_bbox(quad: list[list[float]]) -> tuple[float, float, float, float]:
    """
    Convert a 4-point quad to an axis-aligned bounding box.

    Args:
        quad: [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]

    Returns:
        (x_min, y_min, x_max, y_max) in pixel coordinates.
    """
    xs = [pt[0] for pt in quad]
    ys = [pt[1] for pt in quad]
    return min(xs), min(ys), max(xs), max(ys)


def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp a value to [lo, hi]."""
    return max(lo, min(hi, value))


def bbox_to_yolo(
    x_min: float, y_min: float, x_max: float, y_max: float,
    img_w: int, img_h: int,
) -> tuple[float, float, float, float] | None:
    """
    Convert pixel bbox to YOLO normalized format, clamping to image bounds.

    Returns:
        (x_center, y_center, width, height) all in [0, 1], or None if the
        resulting box has zero area after clamping.
    """
    # Clamp to image bounds
    x_min = clamp(x_min, 0, img_w)
    y_min = clamp(y_min, 0, img_h)
    x_max = clamp(x_max, 0, img_w)
    y_max = clamp(y_max, 0, img_h)

    w = x_max - x_min
    h = y_max - y_min

    # Skip degenerate boxes
    if w <= 0 or h <= 0:
        return None

    x_center = (x_min + x_max) / 2.0 / img_w
    y_center = (y_min + y_max) / 2.0 / img_h
    norm_w = w / img_w
    norm_h = h / img_h

    return x_center, y_center, norm_w, norm_h


def quad_is_fully_visible(
    quad: list[list[float]], img_w: int, img_h: int,
) -> bool:
    """
    Check whether all 4 quad points fall within image bounds.

    We reject frames where the document is partially out of frame because
    those produce misleading bounding boxes for training.
    """
    for pt in quad:
        x, y = pt[0], pt[1]
        if x < 0 or x > img_w or y < 0 or y > img_h:
            return False
    return True


# ---------------------------------------------------------------------------
# ZIP processing
# ---------------------------------------------------------------------------

def find_frame_pairs(zf: zipfile.ZipFile, doc_prefix: str) -> list[tuple[str, str]]:
    """
    Find all (image_path, gt_path) pairs for video frames inside a ZIP.

    We skip the reference image (e.g. '01_alb_id/images/01_alb_id.tif')
    and its field-level annotation. We only want the clip frames which live
    under two-letter subdirectories (CA, CS, HA, HS, KA, KS, PA, PS, TA, TS).

    Returns:
        Sorted list of (image_zip_path, gt_zip_path) tuples.
    """
    names = set(zf.namelist())
    pairs = []

    for name in sorted(names):
        # Match pattern: {prefix}/images/{CLIP}/{CLIP}{NN}_{FF}.tif
        if not name.endswith(".tif"):
            continue
        # Must be under a clip subdirectory (2-letter code), not the root image
        parts = name.split("/")
        # Expected: ['01_alb_id', 'images', 'CA', 'CA01_01.tif']
        if len(parts) != 4:
            continue
        if parts[1] != "images":
            continue

        # Build corresponding ground truth path
        clip_dir = parts[2]
        frame_name = parts[3].replace(".tif", ".json")
        gt_path = f"{doc_prefix}/ground_truth/{clip_dir}/{frame_name}"

        if gt_path in names:
            pairs.append((name, gt_path))

    return pairs


def process_zip(
    zip_path: Path,
    output_images_dir: Path,
    output_labels_dir: Path,
    max_per_doc: int,
    dry_run: bool,
) -> dict:
    """
    Process one MIDV-500 ZIP file: extract frames, convert to JPG + YOLO labels.

    Args:
        zip_path:           Path to the ZIP file.
        output_images_dir:  Where to write JPG images.
        output_labels_dir:  Where to write YOLO label .txt files.
        max_per_doc:        Maximum frames to export per document type.
        dry_run:            If True, count only -- don't write anything.

    Returns:
        Dict with statistics: converted, skipped_oob, skipped_degenerate, total.
    """
    stats = {
        "converted": 0,
        "skipped_oob": 0,       # out of bounds (partially visible)
        "skipped_degenerate": 0, # zero-area box after clamping
        "skipped_limit": 0,     # exceeded max_per_doc
        "total": 0,
    }

    zip_name = zip_path.stem  # e.g. '01_alb_id'
    doc_code = zip_name       # includes numeric prefix
    class_id = classify_document(doc_code)

    with zipfile.ZipFile(zip_path, "r") as zf:
        pairs = find_frame_pairs(zf, zip_name)
        stats["total"] = len(pairs)

        for img_path, gt_path in pairs:
            # Check per-document limit
            if stats["converted"] >= max_per_doc:
                stats["skipped_limit"] += len(pairs) - stats["converted"] - stats["skipped_oob"] - stats["skipped_degenerate"]
                break

            # Read ground truth
            gt_data = json.loads(zf.read(gt_path))
            quad = gt_data.get("quad")
            if quad is None:
                # This shouldn't happen for frame-level GT, but be safe
                stats["skipped_degenerate"] += 1
                continue

            # Read image to get dimensions (we need this even for dry_run)
            img_bytes = zf.read(img_path)
            img = Image.open(io.BytesIO(img_bytes))
            img_w, img_h = img.size

            # Check if document is fully visible
            if not quad_is_fully_visible(quad, img_w, img_h):
                stats["skipped_oob"] += 1
                continue

            # Convert quad -> axis-aligned bbox -> YOLO format
            x_min, y_min, x_max, y_max = quad_to_bbox(quad)
            yolo = bbox_to_yolo(x_min, y_min, x_max, y_max, img_w, img_h)

            if yolo is None:
                stats["skipped_degenerate"] += 1
                continue

            # Build output filenames
            # Turn 01_alb_id/images/CA/CA01_01.tif -> midv500_01_alb_id_CA01_01
            frame_basename = Path(img_path).stem  # CA01_01
            out_stem = f"midv500_{zip_name}_{frame_basename}"

            if not dry_run:
                # Save JPG
                jpg_path = output_images_dir / f"{out_stem}.jpg"
                img_rgb = img.convert("RGB") if img.mode != "RGB" else img
                img_rgb.save(str(jpg_path), "JPEG", quality=95)

                # Save YOLO label
                txt_path = output_labels_dir / f"{out_stem}.txt"
                x_c, y_c, w, h = yolo
                with open(txt_path, "w") as f:
                    f.write(f"{class_id} {x_c:.6f} {y_c:.6f} {w:.6f} {h:.6f}\n")

            stats["converted"] += 1

    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert MIDV-500 dataset to YOLO object detection format.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Class mapping:
  0: passport           (codes containing 'passport')
  1: drivers_license    (codes containing 'drvlic' or 'driving')
  4: id_card_generic    (everything else)

YOLO label format (per line):
  class_id x_center y_center width height
  (all values except class_id are normalized to [0,1])
        """,
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("/mnt/storage/training-material/documents/datasets/midv-500"),
        help="Root of the MIDV-500 dataset (contains dataset/ subdir with ZIPs).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/mnt/storage/training-material/documents/yolo-doc-detect/midv500"),
        help="Output directory (will contain images/ and labels/ subdirs).",
    )
    parser.add_argument(
        "--max-per-doc",
        type=int,
        default=200,
        help="Maximum number of frames to export per document type (default: 200).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count what would be converted without writing any files.",
    )
    args = parser.parse_args()

    # Validate source directory
    dataset_dir = args.source_dir / "dataset"
    if not dataset_dir.is_dir():
        print(f"ERROR: Dataset directory not found: {dataset_dir}", file=sys.stderr)
        sys.exit(1)

    # Find all ZIP files
    zip_files = sorted(dataset_dir.glob("*.zip"))
    if not zip_files:
        print(f"ERROR: No ZIP files found in {dataset_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"MIDV-500 to YOLO converter")
    print(f"  Source:       {args.source_dir}")
    print(f"  Output:       {args.output_dir}")
    print(f"  Max/doc:      {args.max_per_doc}")
    print(f"  Dry run:      {args.dry_run}")
    print(f"  ZIP files:    {len(zip_files)}")
    print()

    # Create output directories
    output_images = args.output_dir / "images"
    output_labels = args.output_dir / "labels"
    if not args.dry_run:
        output_images.mkdir(parents=True, exist_ok=True)
        output_labels.mkdir(parents=True, exist_ok=True)

    # Class distribution tracking
    class_counts = {cid: 0 for cid in CLASS_NAMES}

    # Totals
    total_converted = 0
    total_skipped_oob = 0
    total_skipped_degenerate = 0
    total_skipped_limit = 0
    total_frames = 0

    for i, zip_path in enumerate(zip_files, 1):
        doc_code = zip_path.stem
        class_id = classify_document(doc_code)
        class_name = CLASS_NAMES[class_id]

        print(f"[{i:2d}/{len(zip_files)}] {doc_code} -> class {class_id} ({class_name})")

        stats = process_zip(
            zip_path=zip_path,
            output_images_dir=output_images,
            output_labels_dir=output_labels,
            max_per_doc=args.max_per_doc,
            dry_run=args.dry_run,
        )

        class_counts[class_id] += stats["converted"]
        total_converted += stats["converted"]
        total_skipped_oob += stats["skipped_oob"]
        total_skipped_degenerate += stats["skipped_degenerate"]
        total_skipped_limit += stats["skipped_limit"]
        total_frames += stats["total"]

        print(
            f"         {stats['converted']} converted, "
            f"{stats['skipped_oob']} oob, "
            f"{stats['skipped_degenerate']} degenerate, "
            f"{stats['skipped_limit']} over-limit "
            f"(of {stats['total']} frames)"
        )

    # Summary
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Total frames found:     {total_frames}")
    print(f"  Converted:              {total_converted}")
    print(f"  Skipped (out of bounds):{total_skipped_oob}")
    print(f"  Skipped (degenerate):   {total_skipped_degenerate}")
    print(f"  Skipped (over limit):   {total_skipped_limit}")
    print()
    print("Class distribution:")
    for cid in sorted(CLASS_NAMES):
        count = class_counts[cid]
        if count > 0:
            print(f"  {cid}: {CLASS_NAMES[cid]:<30s} {count:>6d}")
    print()

    if args.dry_run:
        print("DRY RUN -- no files were written.")
    else:
        print(f"Output written to: {args.output_dir}")
        print(f"  images/ : {total_converted} JPG files")
        print(f"  labels/ : {total_converted} TXT files")


if __name__ == "__main__":
    main()
