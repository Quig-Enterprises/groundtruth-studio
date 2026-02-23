#!/usr/bin/env python3
"""
Convert IDNet US driver's license dataset to YOLO format for document detection.

IDNet contains synthetic US driver's license images for 8 states (AZ, CA, NC, NV,
PA, SD, WI, WV), each packaged as a ZIP archive. Each archive includes legitimate
("positive") images and several fraud variant directories. This script processes
ONLY the positive (legitimate) images.

Since IDNet images are full-document crops (the entire image IS the document),
the YOLO bounding box for each image is trivially the full frame:
    class_id  0.5  0.5  1.0  1.0

Images are converted from PNG to JPG to save disk space, and filenames are
prefixed with the state code to avoid collisions across states.

Dataset:  IDNet (https://arxiv.org/abs/2408.02011)
Source:   /mnt/storage/training-material/documents/datasets/idnet/
Output:   /mnt/storage/training-material/documents/yolo-doc-detect/idnet/

Class mapping (groundtruth-studio document detection convention):
    0: passport             (not present in IDNet)
    1: drivers_license      <-- all IDNet images
    4: id_card_generic      (not present in IDNet)
"""

import argparse
import io
import random
import sys
import zipfile
from pathlib import Path

from PIL import Image

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# All IDNet images are US driver's licenses -> class 1
CLASS_ID = 1
CLASS_NAME = "drivers_license"

# All 8 states in the dataset
ALL_STATES = ["AZ", "CA", "NC", "NV", "PA", "SD", "WI", "WV"]

# JPEG quality for PNG -> JPG conversion
JPEG_QUALITY = 95


# ---------------------------------------------------------------------------
# ZIP processing
# ---------------------------------------------------------------------------

def list_positive_images(zf: zipfile.ZipFile, state: str) -> list[str]:
    """
    Return sorted list of ZIP entry paths for positive (legitimate) images.

    Filters to entries matching: {STATE}/positive/*.png
    Skips directory entries and non-PNG files.

    Args:
        zf:    Open ZipFile handle.
        state: Two-letter state code (e.g. "AZ").

    Returns:
        Sorted list of ZIP-internal paths to positive PNG images.
    """
    prefix = f"{state}/positive/"
    entries = []

    for info in zf.infolist():
        # Skip directories
        if info.is_dir():
            continue
        # Match positive PNGs only
        if info.filename.startswith(prefix) and info.filename.lower().endswith(".png"):
            entries.append(info.filename)

    return sorted(entries)


def process_state_zip(
    zip_path: Path,
    state: str,
    output_images_dir: Path,
    output_labels_dir: Path,
    max_images: int,
    dry_run: bool,
    seed: int,
) -> dict:
    """
    Process one state ZIP: extract positive images, convert to JPG, write YOLO labels.

    Each image is a full-document crop, so the YOLO label is always:
        1 0.500000 0.500000 1.000000 1.000000

    Images are streamed one at a time from the ZIP to keep memory usage low.

    Args:
        zip_path:          Path to the state ZIP file (e.g. SD.zip).
        state:             Two-letter state code.
        output_images_dir: Directory for output JPG images.
        output_labels_dir: Directory for output YOLO label .txt files.
        max_images:        Maximum number of images to extract for this state.
        dry_run:           If True, count only -- don't write any files.
        seed:              Random seed for reproducible sampling.

    Returns:
        Dict with keys: converted, skipped_limit, total.
    """
    stats = {
        "converted": 0,
        "skipped_limit": 0,
        "skipped_error": 0,
        "total": 0,
    }

    # The YOLO label is the same for every image: full-frame bounding box
    label_line = f"{CLASS_ID} 0.500000 0.500000 1.000000 1.000000\n"

    with zipfile.ZipFile(zip_path, "r") as zf:
        # Collect all positive image paths
        positive_paths = list_positive_images(zf, state)
        stats["total"] = len(positive_paths)

        if not positive_paths:
            print(f"  WARNING: No positive images found for {state}", file=sys.stderr)
            return stats

        # Sample up to max_images with reproducible randomness
        rng = random.Random(seed)
        if len(positive_paths) > max_images:
            selected = rng.sample(positive_paths, max_images)
            stats["skipped_limit"] = len(positive_paths) - max_images
        else:
            selected = positive_paths[:]

        # Sort selected for deterministic output order
        selected.sort()

        # Process each selected image one at a time (memory-efficient)
        for idx, zip_entry_path in enumerate(selected, start=1):
            # Build output filename: {STATE}_{NNNNN}.jpg
            out_stem = f"{state}_{idx:05d}"

            try:
                # Read image bytes from ZIP
                img_bytes = zf.read(zip_entry_path)

                if not dry_run:
                    # Convert PNG to JPG
                    img = Image.open(io.BytesIO(img_bytes))
                    if img.mode != "RGB":
                        img = img.convert("RGB")

                    # Write JPG
                    jpg_path = output_images_dir / f"{out_stem}.jpg"
                    img.save(str(jpg_path), "JPEG", quality=JPEG_QUALITY)

                    # Write YOLO label
                    txt_path = output_labels_dir / f"{out_stem}.txt"
                    txt_path.write_text(label_line)

                stats["converted"] += 1

            except Exception as e:
                print(f"  WARNING: Error processing {zip_entry_path}: {e}",
                      file=sys.stderr)
                stats["skipped_error"] += 1
                continue

    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert IDNet US driver's license dataset to YOLO format.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Dataset structure (per state ZIP):
  {STATE}/positive/           -- legitimate license images (PNG)
  {STATE}/fraud1_*/           -- fraud variants (skipped)
  {STATE}/meta/detailed_with_fraud_info/{STATE}_original_annotation.json

Since images are full-document crops, the YOLO label is:
  1 0.5 0.5 1.0 1.0  (class 1 = drivers_license, full-frame bbox)

Class mapping:
  0: passport           (not present)
  1: drivers_license    (all IDNet images)
  4: id_card_generic    (not present)
        """,
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("/mnt/storage/training-material/documents/datasets/idnet"),
        help="Directory containing the state ZIP files (default: %(default)s).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/mnt/storage/training-material/documents/yolo-doc-detect/idnet"),
        help="Output directory for YOLO images/ and labels/ (default: %(default)s).",
    )
    parser.add_argument(
        "--max-per-state",
        type=int,
        default=500,
        help="Maximum images to extract per state (default: 500).",
    )
    parser.add_argument(
        "--states",
        type=str,
        default=None,
        help="Comma-separated list of state codes to process (default: all 8 states).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be converted without writing any files.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducible sampling (default: 42).",
    )
    args = parser.parse_args()

    # Parse states
    if args.states:
        states = [s.strip().upper() for s in args.states.split(",")]
        # Validate
        for s in states:
            if s not in ALL_STATES:
                print(f"ERROR: Unknown state '{s}'. Valid states: {ALL_STATES}",
                      file=sys.stderr)
                sys.exit(1)
    else:
        states = ALL_STATES[:]

    # Validate source directory
    if not args.source_dir.is_dir():
        print(f"ERROR: Source directory does not exist: {args.source_dir}",
              file=sys.stderr)
        sys.exit(1)

    # Check that ZIP files exist for requested states
    missing = []
    for state in states:
        zip_path = args.source_dir / f"{state}.zip"
        if not zip_path.exists():
            missing.append(str(zip_path))
    if missing:
        print(f"ERROR: Missing ZIP files:", file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)
        sys.exit(1)

    # Create output directories
    output_images = args.output_dir / "images"
    output_labels = args.output_dir / "labels"
    if not args.dry_run:
        output_images.mkdir(parents=True, exist_ok=True)
        output_labels.mkdir(parents=True, exist_ok=True)

    # Print configuration
    print("IDNet to YOLO converter")
    print(f"  Source:         {args.source_dir}")
    print(f"  Output:         {args.output_dir}")
    print(f"  States:         {', '.join(states)}")
    print(f"  Max per state:  {args.max_per_state}")
    print(f"  Class:          {CLASS_ID} ({CLASS_NAME})")
    print(f"  Dry run:        {args.dry_run}")
    print(f"  Seed:           {args.seed}")
    print()

    # Process each state
    total_converted = 0
    total_skipped_limit = 0
    total_skipped_error = 0
    total_images = 0
    state_results = {}

    for i, state in enumerate(states, 1):
        zip_path = args.source_dir / f"{state}.zip"
        zip_size_gb = zip_path.stat().st_size / (1024 ** 3)

        print(f"[{i}/{len(states)}] {state} ({zip_size_gb:.1f} GB) ...")

        stats = process_state_zip(
            zip_path=zip_path,
            state=state,
            output_images_dir=output_images,
            output_labels_dir=output_labels,
            max_images=args.max_per_state,
            dry_run=args.dry_run,
            seed=args.seed,
        )

        state_results[state] = stats
        total_converted += stats["converted"]
        total_skipped_limit += stats["skipped_limit"]
        total_skipped_error += stats["skipped_error"]
        total_images += stats["total"]

        print(f"         {stats['converted']} converted, "
              f"{stats['skipped_limit']} over-limit, "
              f"{stats['skipped_error']} errors "
              f"(of {stats['total']} positive images)")

    # Summary
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  States processed:       {len(states)}")
    print(f"  Total positive images:  {total_images}")
    print(f"  Converted:              {total_converted}")
    print(f"  Skipped (over limit):   {total_skipped_limit}")
    print(f"  Skipped (errors):       {total_skipped_error}")
    print()

    # Per-state breakdown
    print("  Per-state breakdown:")
    for state in states:
        s = state_results[state]
        print(f"    {state}: {s['converted']:>5d} / {s['total']:>5d} positive images")

    print()
    print(f"  Class: {CLASS_ID} ({CLASS_NAME}): {total_converted} images")
    print()

    if args.dry_run:
        print("DRY RUN -- no files were written.")
    else:
        # Verify output counts
        img_count = len(list(output_images.glob("*.jpg")))
        lbl_count = len(list(output_labels.glob("*.txt")))
        print(f"Output written to: {args.output_dir}")
        print(f"  images/ : {img_count} JPG files")
        print(f"  labels/ : {lbl_count} TXT files")
        if img_count != lbl_count:
            print("  WARNING: Image/label count mismatch!", file=sys.stderr)


if __name__ == "__main__":
    main()
