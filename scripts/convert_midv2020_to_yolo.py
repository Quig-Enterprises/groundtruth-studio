#!/usr/bin/env python3
"""
Convert MIDV-2020 dataset to YOLO format for document detection.

MIDV-2020 contains photos and video clips of 10 document types with VIA v2.0.11
polygon annotations. This script extracts images from TAR archives, parses the
doc_quad polygon annotations, converts them to axis-aligned bounding boxes in
YOLO format, and writes the output to a standard images/labels directory layout.

Sources processed:
  - photo.tar: 100 photos per document type (1000 total)
  - clips.tar: ~6800 video frames per document type (~68000 total)

Class mapping follows the groundtruth-studio document detection convention:
  0: passport
  1: drivers_license  (not present in MIDV-2020)
  4: id_card_generic
"""

import argparse
import io
import json
import logging
import os
import random
import sys
import tarfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

# ---------------------------------------------------------------------------
# Class mapping
# ---------------------------------------------------------------------------

# Document types in MIDV-2020 mapped to YOLO class IDs
DOC_TYPE_TO_CLASS: Dict[str, int] = {
    # Passports -> class 0
    "aze_passport": 0,
    "grc_passport": 0,
    "lva_passport": 0,
    "srb_passport": 0,
    "rus_internalpassport": 0,
    # ID cards -> class 4
    "alb_id": 4,
    "esp_id": 4,
    "est_id": 4,
    "fin_id": 4,
    "svk_id": 4,
}

CLASS_NAMES = {
    0: "passport",
    1: "drivers_license",
    4: "id_card_generic",
}

# TAR files to process (photo and clips are realistic for detection)
TAR_SOURCES = ["photo.tar", "clips.tar"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Annotation parsing
# ---------------------------------------------------------------------------

def find_doc_quad_region(regions: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Find the doc_quad polygon region in a list of VIA annotation regions.

    Returns the shape_attributes dict for the first region that has
    field_name == "doc_quad" and shape name == "polygon", or None.
    """
    for region in regions:
        attrs = region.get("region_attributes", {})
        shape = region.get("shape_attributes", {})
        if attrs.get("field_name") == "doc_quad" and shape.get("name") == "polygon":
            return shape
    return None


def polygon_to_yolo_bbox(
    all_points_x: List[int],
    all_points_y: List[int],
    img_width: int,
    img_height: int,
) -> Tuple[float, float, float, float]:
    """Convert a polygon to an axis-aligned bounding box in YOLO format.

    Args:
        all_points_x: X coordinates of the polygon vertices.
        all_points_y: Y coordinates of the polygon vertices.
        img_width: Image width in pixels.
        img_height: Image height in pixels.

    Returns:
        Tuple of (x_center, y_center, width, height) normalized to [0, 1].
    """
    x_min = max(0, min(all_points_x))
    x_max = min(img_width, max(all_points_x))
    y_min = max(0, min(all_points_y))
    y_max = min(img_height, max(all_points_y))

    # Skip degenerate boxes (polygon entirely outside image or too small)
    if x_max <= x_min or y_max <= y_min:
        return None
    w = (x_max - x_min) / img_width
    h = (y_max - y_min) / img_height
    if w < 0.01 or h < 0.01:
        return None

    # YOLO format: center_x, center_y, width, height (all normalized)
    cx = (x_min + x_max) / 2.0 / img_width
    cy = (y_min + y_max) / 2.0 / img_height

    return (cx, cy, w, h)


# ---------------------------------------------------------------------------
# TAR processing: photo.tar
# ---------------------------------------------------------------------------

def load_photo_annotations(
    tar: tarfile.TarFile,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Load all annotation JSONs from photo.tar.

    photo.tar layout:
        annotations/{doc_type}.json   -- one JSON per doc type
        images/{doc_type}/NN.jpg

    Returns:
        Dict mapping doc_type -> {filename: annotation_entry}
    """
    annotations: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for member in tar.getmembers():
        if member.name.startswith("annotations/") and member.name.endswith(".json"):
            doc_type = Path(member.name).stem  # e.g. "alb_id"
            f = tar.extractfile(member)
            if f is None:
                continue
            data = json.load(f)
            meta = data.get("_via_img_metadata", {})

            # Build a lookup by bare filename (e.g. "00.jpg")
            by_filename: Dict[str, Dict[str, Any]] = {}
            for _key, entry in meta.items():
                fname = entry.get("filename", "")
                by_filename[fname] = entry

            annotations[doc_type] = by_filename
            logger.debug("photo annotations: %s -> %d entries", doc_type, len(by_filename))

    return annotations


def process_photo_tar(
    tar_path: Path,
    output_dir: Path,
    max_per_doc: int,
    dry_run: bool,
) -> Dict[str, int]:
    """Process photo.tar and write YOLO images + labels.

    Returns:
        Dict mapping doc_type -> number of images written.
    """
    stats: Dict[str, int] = defaultdict(int)

    logger.info("Opening %s", tar_path)
    with tarfile.open(tar_path, "r") as tar:
        # Step 1: Load all annotations
        annotations = load_photo_annotations(tar)

        # Step 2: Collect image members grouped by doc_type
        image_members: Dict[str, List[tarfile.TarInfo]] = defaultdict(list)
        for member in tar.getmembers():
            if member.name.startswith("images/") and member.name.endswith(".jpg"):
                parts = member.name.split("/")
                if len(parts) >= 3:
                    doc_type = parts[1]
                    image_members[doc_type].append(member)

        # Step 3: Process each doc type
        for doc_type, members in sorted(image_members.items()):
            if doc_type not in DOC_TYPE_TO_CLASS:
                logger.warning("Unknown doc type in photo.tar: %s (skipping)", doc_type)
                continue

            class_id = DOC_TYPE_TO_CLASS[doc_type]
            doc_annotations = annotations.get(doc_type, {})

            # Shuffle and limit
            random.shuffle(members)
            selected = members[:max_per_doc]

            for member in selected:
                bare_name = Path(member.name).name  # e.g. "00.jpg"

                # Look up annotation
                ann_entry = doc_annotations.get(bare_name)
                if ann_entry is None:
                    logger.debug("No annotation for photo %s/%s, skipping", doc_type, bare_name)
                    continue

                regions = ann_entry.get("regions", [])
                shape = find_doc_quad_region(regions)
                if shape is None:
                    logger.debug("No doc_quad for photo %s/%s, skipping", doc_type, bare_name)
                    continue

                # Read image to get dimensions
                img_file = tar.extractfile(member)
                if img_file is None:
                    continue
                img_data = img_file.read()
                img = Image.open(io.BytesIO(img_data))
                img_w, img_h = img.size

                # Convert polygon to YOLO bbox
                result = polygon_to_yolo_bbox(
                    shape["all_points_x"],
                    shape["all_points_y"],
                    img_w,
                    img_h,
                )
                if result is None:
                    stats["skipped_degenerate"] += 1
                    continue
                cx, cy, w, h = result

                # Output filename: midv2020_photo_{doc_type}_{nn}.jpg
                stem = bare_name.replace(".jpg", "")
                out_name = f"midv2020_photo_{doc_type}_{stem}"

                if dry_run:
                    logger.info("[DRY RUN] Would write %s (class %d: %s, bbox %.3f %.3f %.3f %.3f)",
                                out_name, class_id, CLASS_NAMES.get(class_id, "?"), cx, cy, w, h)
                else:
                    # Write image
                    img_out = output_dir / "images" / f"{out_name}.jpg"
                    img_out.write_bytes(img_data)

                    # Write label
                    lbl_out = output_dir / "labels" / f"{out_name}.txt"
                    lbl_out.write_text(f"{class_id} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}\n")

                stats[doc_type] += 1

    return dict(stats)


# ---------------------------------------------------------------------------
# TAR processing: clips.tar
# ---------------------------------------------------------------------------

def load_clip_annotation(
    tar: tarfile.TarFile,
    ann_member: tarfile.TarInfo,
) -> Dict[str, Dict[str, Any]]:
    """Load a single clip annotation JSON from clips.tar.

    clips.tar annotation layout:
        annotations/{doc_type}/{clip_id}.json  -- one JSON per clip

    Returns:
        Dict mapping bare filename -> annotation entry.
    """
    f = tar.extractfile(ann_member)
    if f is None:
        return {}
    data = json.load(f)
    meta = data.get("_via_img_metadata", {})

    by_filename: Dict[str, Dict[str, Any]] = {}
    for _key, entry in meta.items():
        fname = entry.get("filename", "")
        by_filename[fname] = entry

    return by_filename


def process_clips_tar(
    tar_path: Path,
    output_dir: Path,
    max_per_doc: int,
    dry_run: bool,
) -> Dict[str, int]:
    """Process clips.tar and write YOLO images + labels.

    clips.tar has a different structure than photo.tar:
      - annotations/{doc_type}/{clip_id}.json (per-clip annotation)
      - images/{doc_type}/{clip_id}/{frame}.jpg

    We sample frames across all clips for a doc type, up to max_per_doc.

    Returns:
        Dict mapping doc_type -> number of images written.
    """
    stats: Dict[str, int] = defaultdict(int)

    logger.info("Opening %s", tar_path)
    with tarfile.open(tar_path, "r") as tar:
        # Step 1: Index all annotation files by (doc_type, clip_id)
        ann_members: Dict[Tuple[str, str], tarfile.TarInfo] = {}
        # Step 2: Index all image files by (doc_type, clip_id) -> list of members
        image_members: Dict[Tuple[str, str], List[tarfile.TarInfo]] = defaultdict(list)

        logger.info("Indexing clips.tar members...")
        for member in tar.getmembers():
            if member.name.startswith("annotations/") and member.name.endswith(".json"):
                # annotations/{doc_type}/{clip_id}.json
                parts = member.name.split("/")
                if len(parts) >= 3:
                    doc_type = parts[1]
                    clip_id = Path(parts[2]).stem
                    ann_members[(doc_type, clip_id)] = member

            elif member.name.startswith("images/") and member.name.endswith(".jpg"):
                # images/{doc_type}/{clip_id}/{frame}.jpg
                parts = member.name.split("/")
                if len(parts) >= 4:
                    doc_type = parts[1]
                    clip_id = parts[2]
                    image_members[(doc_type, clip_id)].append(member)

        # Step 3: Group clips by doc_type and sample frames
        clips_by_doc: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        for (doc_type, clip_id) in sorted(ann_members.keys()):
            clips_by_doc[doc_type].append((doc_type, clip_id))

        for doc_type, clip_keys in sorted(clips_by_doc.items()):
            if doc_type not in DOC_TYPE_TO_CLASS:
                logger.warning("Unknown doc type in clips.tar: %s (skipping)", doc_type)
                continue

            class_id = DOC_TYPE_TO_CLASS[doc_type]

            # Collect all (clip_key, image_member) pairs for this doc type,
            # then sample max_per_doc from the pool
            all_frames: List[Tuple[Tuple[str, str], tarfile.TarInfo]] = []
            for clip_key in clip_keys:
                for img_member in image_members.get(clip_key, []):
                    all_frames.append((clip_key, img_member))

            random.shuffle(all_frames)
            selected = all_frames[:max_per_doc]
            logger.info("clips %s: %d total frames, selected %d",
                        doc_type, len(all_frames), len(selected))

            # Cache loaded annotations per clip to avoid re-reading
            ann_cache: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}

            for clip_key, img_member in selected:
                dt, clip_id = clip_key

                # Load annotation for this clip (cached)
                if clip_key not in ann_cache:
                    ann_member = ann_members.get(clip_key)
                    if ann_member is None:
                        continue
                    ann_cache[clip_key] = load_clip_annotation(tar, ann_member)

                clip_ann = ann_cache[clip_key]

                # Match by bare filename
                bare_name = Path(img_member.name).name  # e.g. "000001.jpg"
                ann_entry = clip_ann.get(bare_name)
                if ann_entry is None:
                    logger.debug("No annotation for clip frame %s, skipping", img_member.name)
                    continue

                regions = ann_entry.get("regions", [])
                shape = find_doc_quad_region(regions)
                if shape is None:
                    logger.debug("No doc_quad for clip frame %s, skipping", img_member.name)
                    continue

                # Read image for dimensions
                img_file = tar.extractfile(img_member)
                if img_file is None:
                    continue
                img_data = img_file.read()
                img = Image.open(io.BytesIO(img_data))
                img_w, img_h = img.size

                # Convert polygon to YOLO bbox
                result = polygon_to_yolo_bbox(
                    shape["all_points_x"],
                    shape["all_points_y"],
                    img_w,
                    img_h,
                )
                if result is None:
                    stats["skipped_degenerate"] += 1
                    continue
                cx, cy, w, h = result

                # Output filename: midv2020_clip_{doc_type}_{clip_id}_{frame}.jpg
                frame_stem = bare_name.replace(".jpg", "")
                out_name = f"midv2020_clip_{doc_type}_{clip_id}_{frame_stem}"

                if dry_run:
                    logger.info("[DRY RUN] Would write %s (class %d: %s, bbox %.3f %.3f %.3f %.3f)",
                                out_name, class_id, CLASS_NAMES.get(class_id, "?"), cx, cy, w, h)
                else:
                    # Write image
                    img_out = output_dir / "images" / f"{out_name}.jpg"
                    img_out.write_bytes(img_data)

                    # Write label
                    lbl_out = output_dir / "labels" / f"{out_name}.txt"
                    lbl_out.write_text(f"{class_id} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}\n")

                stats[doc_type] += 1

    return dict(stats)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert MIDV-2020 dataset to YOLO format for document detection.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Class mapping:
  0  passport          (aze_passport, grc_passport, lva_passport, srb_passport, rus_internalpassport)
  1  drivers_license   (not present in MIDV-2020)
  4  id_card_generic   (alb_id, esp_id, est_id, fin_id, svk_id)

TAR sources processed:
  photo.tar   100 photos per doc type (realistic handheld photos)
  clips.tar   ~6800 frames per doc type (video frame captures)
        """,
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("/mnt/storage/training-material/documents/datasets/midv-2020/dataset"),
        help="Path to the MIDV-2020 dataset/ directory containing TAR files",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/mnt/storage/training-material/documents/yolo-doc-detect/midv2020"),
        help="Output directory for YOLO images/ and labels/",
    )
    parser.add_argument(
        "--max-per-doc",
        type=int,
        default=200,
        help="Maximum images per document type per TAR source (default: 200)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be written without extracting files",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducible sampling (default: 42)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    # Set up logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    random.seed(args.seed)

    # Validate source directory
    if not args.source_dir.is_dir():
        logger.error("Source directory does not exist: %s", args.source_dir)
        sys.exit(1)

    # Check that expected TAR files exist
    for tar_name in TAR_SOURCES:
        tar_path = args.source_dir / tar_name
        if not tar_path.exists():
            logger.error("Expected TAR file not found: %s", tar_path)
            sys.exit(1)

    # Create output directories
    if not args.dry_run:
        (args.output_dir / "images").mkdir(parents=True, exist_ok=True)
        (args.output_dir / "labels").mkdir(parents=True, exist_ok=True)
        logger.info("Output directory: %s", args.output_dir)

    # Print configuration
    logger.info("MIDV-2020 to YOLO converter")
    logger.info("  Source:       %s", args.source_dir)
    logger.info("  Output:       %s", args.output_dir)
    logger.info("  Max per doc:  %d (per TAR source)", args.max_per_doc)
    logger.info("  Dry run:      %s", args.dry_run)
    logger.info("  Seed:         %d", args.seed)
    logger.info("")

    # Process each TAR source
    total_stats: Dict[str, int] = defaultdict(int)

    # photo.tar
    photo_path = args.source_dir / "photo.tar"
    logger.info("=== Processing photo.tar ===")
    photo_stats = process_photo_tar(photo_path, args.output_dir, args.max_per_doc, args.dry_run)
    for doc_type, count in photo_stats.items():
        total_stats[doc_type] += count

    # clips.tar
    clips_path = args.source_dir / "clips.tar"
    logger.info("=== Processing clips.tar ===")
    clips_stats = process_clips_tar(clips_path, args.output_dir, args.max_per_doc, args.dry_run)
    for doc_type, count in clips_stats.items():
        total_stats[doc_type] += count

    # Print summary
    logger.info("")
    logger.info("=== Summary ===")
    grand_total = 0
    class_totals: Dict[int, int] = defaultdict(int)

    for doc_type in sorted(total_stats.keys()):
        count = total_stats[doc_type]
        class_id = DOC_TYPE_TO_CLASS.get(doc_type, -1)
        class_name = CLASS_NAMES.get(class_id, "unknown")
        photo_count = photo_stats.get(doc_type, 0)
        clip_count = clips_stats.get(doc_type, 0)
        logger.info("  %-25s class %d (%s): %4d (photo: %d, clips: %d)",
                     doc_type, class_id, class_name, count, photo_count, clip_count)
        grand_total += count
        class_totals[class_id] += count

    logger.info("")
    for class_id in sorted(class_totals.keys()):
        logger.info("  Class %d (%s): %d images",
                     class_id, CLASS_NAMES.get(class_id, "?"), class_totals[class_id])

    logger.info("")
    logger.info("  Total images: %d", grand_total)

    if args.dry_run:
        logger.info("  (dry run -- no files written)")
    else:
        # Verify output
        img_count = len(list((args.output_dir / "images").glob("*.jpg")))
        lbl_count = len(list((args.output_dir / "labels").glob("*.txt")))
        logger.info("  Written: %d images, %d labels", img_count, lbl_count)
        if img_count != lbl_count:
            logger.warning("  Image/label count mismatch!")


if __name__ == "__main__":
    main()
