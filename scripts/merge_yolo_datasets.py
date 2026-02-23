#!/usr/bin/env python3
"""
Merge converted YOLO datasets into a unified train/val/test split.

Scans one or more input directories (each containing images/ and labels/
subdirectories in YOLO format), validates label files, handles filename
collisions, performs stratified splitting by class, and writes the merged
output as symlinks (or copies) with a YOLOv8-compatible data.yaml.

Input directories (from converter scripts):
    /mnt/storage/training-material/documents/yolo-doc-detect/midv500/
    /mnt/storage/training-material/documents/yolo-doc-detect/midv2020/
    /mnt/storage/training-material/documents/yolo-doc-detect/idnet/

Output:
    merged/
    +-- train/images/  train/labels/
    +-- val/images/    val/labels/
    +-- test/images/   test/labels/
    +-- data.yaml

Usage:
    python merge_yolo_datasets.py
    python merge_yolo_datasets.py --dry-run
    python merge_yolo_datasets.py --copy --seed 123
    python merge_yolo_datasets.py --input-dirs /path/a /path/b --output-dir /path/merged
"""

import argparse
import os
import random
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Class definitions (5-class document detection scheme)
# ---------------------------------------------------------------------------

NUM_CLASSES = 5
CLASS_NAMES = {
    0: "passport",
    1: "drivers_license",
    2: "twic_card",
    3: "merchant_mariner_credential",
    4: "id_card_generic",
}
VALID_CLASS_IDS = set(CLASS_NAMES.keys())

# Supported image extensions (lowercase)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}

# Default input directories
DEFAULT_INPUT_DIRS = [
    "/mnt/storage/training-material/documents/yolo-doc-detect/midv500",
    "/mnt/storage/training-material/documents/yolo-doc-detect/midv2020",
    "/mnt/storage/training-material/documents/yolo-doc-detect/idnet",
]
DEFAULT_OUTPUT_DIR = "/mnt/storage/training-material/documents/yolo-doc-detect/merged"


# ---------------------------------------------------------------------------
# Label validation
# ---------------------------------------------------------------------------

def validate_label_file(label_path: Path) -> Tuple[bool, Optional[int], List[str]]:
    """
    Parse and validate a YOLO label file.

    Checks:
        - File is readable and non-empty
        - Each line has exactly 5 fields: class_id x_center y_center width height
        - class_id is an integer in VALID_CLASS_IDS (0-4)
        - x_center, y_center, width, height are floats in [0, 1]

    Returns:
        (is_valid, primary_class_id, list_of_warnings)
        primary_class_id is the class of the first annotation line, or None
        if the file is empty/invalid.
    """
    warnings = []
    primary_class = None

    try:
        text = label_path.read_text().strip()
    except OSError as e:
        return False, None, [f"Cannot read file: {e}"]

    if not text:
        return False, None, ["Empty label file"]

    for line_num, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) != 5:
            warnings.append(
                f"Line {line_num}: expected 5 fields, got {len(parts)}"
            )
            return False, primary_class, warnings

        # Validate class ID
        try:
            class_id = int(parts[0])
        except ValueError:
            warnings.append(f"Line {line_num}: class ID '{parts[0]}' is not an integer")
            return False, primary_class, warnings

        if class_id not in VALID_CLASS_IDS:
            warnings.append(
                f"Line {line_num}: class ID {class_id} not in valid range 0-{NUM_CLASSES - 1}"
            )
            return False, primary_class, warnings

        if primary_class is None:
            primary_class = class_id

        # Validate coordinates
        for i, name in enumerate(["x_center", "y_center", "width", "height"], 1):
            try:
                val = float(parts[i])
            except ValueError:
                warnings.append(f"Line {line_num}: {name} '{parts[i]}' is not a float")
                return False, primary_class, warnings

            if val < 0.0 or val > 1.0:
                warnings.append(
                    f"Line {line_num}: {name}={val:.6f} outside [0, 1] range"
                )
                return False, primary_class, warnings

    return True, primary_class, warnings


# ---------------------------------------------------------------------------
# Dataset scanning
# ---------------------------------------------------------------------------

def scan_dataset(
    input_dir: Path,
    dataset_name: str,
) -> Tuple[List[Tuple[str, Path, Path, int]], List[str]]:
    """
    Scan an input directory for valid image/label pairs.

    Args:
        input_dir:    Root of the YOLO dataset (must contain images/ and labels/).
        dataset_name: Short name for this dataset (used for collision prefixing).

    Returns:
        (samples, warnings)
        samples: list of (unique_stem, image_path, label_path, class_id)
        warnings: list of warning strings for skipped files
    """
    images_dir = input_dir / "images"
    labels_dir = input_dir / "labels"
    samples = []
    warnings = []

    if not images_dir.is_dir():
        warnings.append(f"WARNING: {images_dir} does not exist, skipping dataset")
        return samples, warnings

    if not labels_dir.is_dir():
        warnings.append(f"WARNING: {labels_dir} does not exist, skipping dataset")
        return samples, warnings

    # Collect all image files
    image_files = {}
    for f in sorted(images_dir.iterdir()):
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
            image_files[f.stem] = f

    if not image_files:
        warnings.append(f"WARNING: No image files found in {images_dir}")
        return samples, warnings

    # Match each image with its label
    matched = 0
    missing_labels = 0
    invalid_labels = 0

    for stem, img_path in sorted(image_files.items()):
        label_path = labels_dir / f"{stem}.txt"

        if not label_path.exists():
            warnings.append(f"WARNING: Missing label for {img_path.name}, skipping")
            missing_labels += 1
            continue

        # Validate label content
        is_valid, class_id, label_warnings = validate_label_file(label_path)

        if not is_valid:
            for w in label_warnings:
                warnings.append(f"WARNING: {label_path.name}: {w}")
            invalid_labels += 1
            continue

        if class_id is None:
            warnings.append(f"WARNING: {label_path.name}: no annotations found")
            invalid_labels += 1
            continue

        # Build a unique stem: prefix with dataset name to avoid collisions.
        # The converter scripts already prefix filenames (e.g. midv500_*, midv2020_*),
        # but we add the dataset_name prefix if the stem doesn't already start with it,
        # to be safe against collisions from different source datasets.
        if stem.startswith(dataset_name):
            unique_stem = stem
        else:
            unique_stem = f"{dataset_name}_{stem}"

        samples.append((unique_stem, img_path, label_path, class_id))
        matched += 1

    print(f"  [{dataset_name}] {matched} valid pairs, "
          f"{missing_labels} missing labels, {invalid_labels} invalid labels "
          f"(of {len(image_files)} images)")

    return samples, warnings


# ---------------------------------------------------------------------------
# Stratified splitting
# ---------------------------------------------------------------------------

def stratified_split(
    samples: List[Tuple[str, Path, Path, int]],
    train_ratio: float,
    val_ratio: float,
    seed: int,
) -> Tuple[
    List[Tuple[str, Path, Path, int]],
    List[Tuple[str, Path, Path, int]],
    List[Tuple[str, Path, Path, int]],
]:
    """
    Split samples into train/val/test sets, stratified by class.

    Each class is independently shuffled and split so that the class
    proportions are maintained across all three splits.

    Args:
        samples:     List of (unique_stem, image_path, label_path, class_id).
        train_ratio: Fraction for training set (e.g. 0.8).
        val_ratio:   Fraction for validation set (e.g. 0.1).
        seed:        Random seed for reproducibility.

    Returns:
        (train_samples, val_samples, test_samples)
    """
    # Group by class
    by_class: Dict[int, List[Tuple[str, Path, Path, int]]] = defaultdict(list)
    for sample in samples:
        by_class[sample[3]].append(sample)

    rng = random.Random(seed)

    train_set = []
    val_set = []
    test_set = []

    for class_id in sorted(by_class.keys()):
        class_samples = by_class[class_id]
        rng.shuffle(class_samples)

        n = len(class_samples)
        n_train = int(round(n * train_ratio))
        n_val = int(round(n * val_ratio))
        # Test gets the remainder, ensuring no samples are lost
        n_test = n - n_train - n_val

        # Edge case: if rounding pushed counts over total, adjust test
        if n_test < 0:
            n_train = n - n_val
            n_test = 0

        train_set.extend(class_samples[:n_train])
        val_set.extend(class_samples[n_train : n_train + n_val])
        test_set.extend(class_samples[n_train + n_val :])

    return train_set, val_set, test_set


# ---------------------------------------------------------------------------
# File operations (symlink or copy)
# ---------------------------------------------------------------------------

def link_or_copy(
    src: Path,
    dst: Path,
    use_copy: bool,
    dry_run: bool,
) -> None:
    """
    Create a symlink or copy from src to dst.

    Args:
        src:      Source file path (absolute).
        dst:      Destination file path.
        use_copy: If True, copy the file. If False, create a symlink.
        dry_run:  If True, do nothing (just preview).
    """
    if dry_run:
        return

    # Ensure parent directory exists
    dst.parent.mkdir(parents=True, exist_ok=True)

    # Remove existing file/symlink to allow re-runs
    if dst.exists() or dst.is_symlink():
        dst.unlink()

    if use_copy:
        shutil.copy2(str(src), str(dst))
    else:
        # Use absolute path for symlink target
        os.symlink(src.resolve(), dst)


def write_split(
    samples: List[Tuple[str, Path, Path, int]],
    split_dir: Path,
    use_copy: bool,
    dry_run: bool,
) -> None:
    """
    Write image/label pairs into a split directory (train/, val/, or test/).

    Args:
        samples:   List of (unique_stem, image_path, label_path, class_id).
        split_dir: e.g. merged/train/
        use_copy:  Whether to copy files instead of symlinking.
        dry_run:   If True, skip file operations.
    """
    images_dir = split_dir / "images"
    labels_dir = split_dir / "labels"

    if not dry_run:
        images_dir.mkdir(parents=True, exist_ok=True)
        labels_dir.mkdir(parents=True, exist_ok=True)

    for unique_stem, img_path, lbl_path, _class_id in samples:
        # Preserve original image extension
        img_ext = img_path.suffix
        dst_img = images_dir / f"{unique_stem}{img_ext}"
        dst_lbl = labels_dir / f"{unique_stem}.txt"

        link_or_copy(img_path, dst_img, use_copy, dry_run)
        link_or_copy(lbl_path, dst_lbl, use_copy, dry_run)


# ---------------------------------------------------------------------------
# data.yaml generation
# ---------------------------------------------------------------------------

def write_data_yaml(output_dir: Path, dry_run: bool) -> None:
    """
    Write a YOLOv8-compatible data.yaml file.

    Args:
        output_dir: The merged output directory.
        dry_run:    If True, print what would be written instead.
    """
    yaml_content = (
        f"# YOLOv8 dataset configuration\n"
        f"# Auto-generated by merge_yolo_datasets.py\n"
        f"\n"
        f"path: {output_dir.resolve()}\n"
        f"train: train/images\n"
        f"val: val/images\n"
        f"test: test/images\n"
        f"\n"
        f"nc: {NUM_CLASSES}\n"
        f"names:\n"
    )
    for cid in sorted(CLASS_NAMES.keys()):
        yaml_content += f"  {cid}: {CLASS_NAMES[cid]}\n"

    yaml_path = output_dir / "data.yaml"

    if dry_run:
        print(f"\n[DRY RUN] Would write {yaml_path}:")
        print(yaml_content)
    else:
        yaml_path.write_text(yaml_content)
        print(f"Wrote {yaml_path}")


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def print_class_distribution(
    train_samples: List[Tuple[str, Path, Path, int]],
    val_samples: List[Tuple[str, Path, Path, int]],
    test_samples: List[Tuple[str, Path, Path, int]],
) -> None:
    """Print a table of class counts across train/val/test splits."""

    def count_classes(
        samples: List[Tuple[str, Path, Path, int]],
    ) -> Dict[int, int]:
        counts: Dict[int, int] = defaultdict(int)
        for _, _, _, cid in samples:
            counts[cid] += 1
        return counts

    train_counts = count_classes(train_samples)
    val_counts = count_classes(val_samples)
    test_counts = count_classes(test_samples)

    total = len(train_samples) + len(val_samples) + len(test_samples)

    print()
    print("=" * 76)
    print("CLASS DISTRIBUTION")
    print("=" * 76)
    print(f"{'ID':<4} {'Class':<28} {'Train':>7} {'Val':>7} {'Test':>7} {'Total':>7}")
    print("-" * 76)

    for cid in sorted(CLASS_NAMES.keys()):
        t = train_counts.get(cid, 0)
        v = val_counts.get(cid, 0)
        te = test_counts.get(cid, 0)
        row_total = t + v + te
        name = CLASS_NAMES[cid]

        if row_total == 0:
            note = " (no data yet)"
        else:
            note = ""

        print(f"{cid:<4} {name:<28} {t:>7} {v:>7} {te:>7} {row_total:>7}{note}")

    print("-" * 76)
    print(f"{'':4} {'TOTAL':<28} {len(train_samples):>7} {len(val_samples):>7} "
          f"{len(test_samples):>7} {total:>7}")
    print()

    # Percentages
    if total > 0:
        print(f"Split ratios:  "
              f"train {len(train_samples)/total*100:.1f}%  "
              f"val {len(val_samples)/total*100:.1f}%  "
              f"test {len(test_samples)/total*100:.1f}%")
    print()


def check_stem_collisions(
    all_samples: List[Tuple[str, Path, Path, int]],
) -> List[str]:
    """
    Check for duplicate unique_stems across all datasets.

    Returns list of warning strings for any collisions found.
    """
    seen: Dict[str, Path] = {}
    warnings = []

    for stem, img_path, _, _ in all_samples:
        if stem in seen:
            warnings.append(
                f"COLLISION: stem '{stem}' from {img_path} "
                f"conflicts with {seen[stem]}"
            )
        else:
            seen[stem] = img_path

    return warnings


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge YOLO datasets into a unified train/val/test split.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Merge default datasets with symlinks
  python merge_yolo_datasets.py

  # Preview what would happen
  python merge_yolo_datasets.py --dry-run

  # Use file copies instead of symlinks
  python merge_yolo_datasets.py --copy

  # Custom directories and split ratios
  python merge_yolo_datasets.py \\
      --input-dirs /path/to/dataset_a /path/to/dataset_b \\
      --output-dir /path/to/merged \\
      --train-ratio 0.7 --val-ratio 0.15

Classes:
  0: passport
  1: drivers_license
  2: twic_card               (placeholder -- data added later)
  3: merchant_mariner_credential  (placeholder -- data added later)
  4: id_card_generic
        """,
    )
    parser.add_argument(
        "--input-dirs",
        nargs="+",
        type=Path,
        default=[Path(d) for d in DEFAULT_INPUT_DIRS],
        help="Input dataset directories, each with images/ and labels/ subdirs. "
             "Default: midv500, midv2020, idnet.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(DEFAULT_OUTPUT_DIR),
        help=f"Output directory for merged dataset (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--train-ratio",
        type=float,
        default=0.8,
        help="Fraction of data for training (default: 0.8).",
    )
    parser.add_argument(
        "--val-ratio",
        type=float,
        default=0.1,
        help="Fraction of data for validation (default: 0.1). "
             "Test ratio = 1 - train - val.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducible splits (default: 42).",
    )
    parser.add_argument(
        "--copy",
        action="store_true",
        help="Copy files instead of creating symlinks (uses more disk space).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created without writing anything.",
    )

    args = parser.parse_args()

    # -----------------------------------------------------------------------
    # Validate arguments
    # -----------------------------------------------------------------------

    test_ratio = 1.0 - args.train_ratio - args.val_ratio
    if test_ratio < -1e-9:
        print(f"ERROR: train_ratio ({args.train_ratio}) + val_ratio ({args.val_ratio}) "
              f"exceeds 1.0", file=sys.stderr)
        return 1

    # Clamp floating-point rounding
    test_ratio = max(0.0, test_ratio)

    # Print configuration
    print("=" * 76)
    print("YOLO Dataset Merger")
    print("=" * 76)
    print(f"  Input dirs:   {len(args.input_dirs)}")
    for d in args.input_dirs:
        exists = d.is_dir()
        status = "" if exists else " (NOT FOUND)"
        print(f"                  {d}{status}")
    print(f"  Output dir:   {args.output_dir}")
    print(f"  Split:        train={args.train_ratio:.0%} / "
          f"val={args.val_ratio:.0%} / test={test_ratio:.0%}")
    print(f"  Link mode:    {'copy' if args.copy else 'symlink'}")
    print(f"  Seed:         {args.seed}")
    print(f"  Dry run:      {args.dry_run}")
    print()

    # -----------------------------------------------------------------------
    # Scan all input directories
    # -----------------------------------------------------------------------

    all_samples: List[Tuple[str, Path, Path, int]] = []
    all_warnings: List[str] = []

    print("Scanning input directories...")
    for input_dir in args.input_dirs:
        if not input_dir.is_dir():
            print(f"  [{input_dir.name}] SKIPPED (directory not found)")
            all_warnings.append(f"WARNING: Input directory not found: {input_dir}")
            continue

        # Use the directory name as the dataset identifier for collision prefixing
        dataset_name = input_dir.name

        samples, warnings = scan_dataset(input_dir, dataset_name)
        all_samples.extend(samples)
        all_warnings.extend(warnings)

    print()

    if not all_samples:
        print("ERROR: No valid image/label pairs found across all input directories.",
              file=sys.stderr)
        return 1

    print(f"Total valid samples: {len(all_samples)}")

    # -----------------------------------------------------------------------
    # Check for filename collisions
    # -----------------------------------------------------------------------

    collision_warnings = check_stem_collisions(all_samples)
    if collision_warnings:
        print()
        print(f"WARNING: {len(collision_warnings)} filename collision(s) detected!")
        for w in collision_warnings:
            print(f"  {w}")
        print("Resolve collisions before merging. The later entry will overwrite "
              "the earlier one.")
        print()
    all_warnings.extend(collision_warnings)

    # -----------------------------------------------------------------------
    # Stratified split
    # -----------------------------------------------------------------------

    train_samples, val_samples, test_samples = stratified_split(
        all_samples,
        train_ratio=args.train_ratio,
        val_ratio=args.val_ratio,
        seed=args.seed,
    )

    # Print class distribution
    print_class_distribution(train_samples, val_samples, test_samples)

    # -----------------------------------------------------------------------
    # Write output
    # -----------------------------------------------------------------------

    if args.dry_run:
        print("[DRY RUN] Would create the following structure:")
        print(f"  {args.output_dir}/")
        print(f"    train/images/  ({len(train_samples)} files)")
        print(f"    train/labels/  ({len(train_samples)} files)")
        print(f"    val/images/    ({len(val_samples)} files)")
        print(f"    val/labels/    ({len(val_samples)} files)")
        print(f"    test/images/   ({len(test_samples)} files)")
        print(f"    test/labels/   ({len(test_samples)} files)")
        print(f"    data.yaml")
        link_type = "copies" if args.copy else "symlinks"
        print(f"  Total files: {2 * len(all_samples)} ({link_type})")
    else:
        print("Writing merged dataset...")

        # Create output directory structure
        args.output_dir.mkdir(parents=True, exist_ok=True)

        # Write each split
        for split_name, split_samples in [
            ("train", train_samples),
            ("val", val_samples),
            ("test", test_samples),
        ]:
            split_dir = args.output_dir / split_name
            print(f"  Writing {split_name}/ ({len(split_samples)} pairs)...")
            write_split(split_samples, split_dir, args.copy, dry_run=False)

    # Write data.yaml
    write_data_yaml(args.output_dir, args.dry_run)

    # -----------------------------------------------------------------------
    # Print warnings summary
    # -----------------------------------------------------------------------

    if all_warnings:
        print()
        print(f"Warnings ({len(all_warnings)}):")
        # Show first 20 warnings to avoid flooding terminal
        for w in all_warnings[:20]:
            print(f"  {w}")
        if len(all_warnings) > 20:
            print(f"  ... and {len(all_warnings) - 20} more warnings")

    # -----------------------------------------------------------------------
    # Final summary
    # -----------------------------------------------------------------------

    print()
    print("=" * 76)
    if args.dry_run:
        print("DRY RUN complete. No files were written.")
    else:
        link_type = "copied" if args.copy else "symlinked"
        print(f"Merge complete. {len(all_samples)} samples {link_type} into:")
        print(f"  {args.output_dir.resolve()}")
        print()
        print(f"  train: {len(train_samples):>6} samples")
        print(f"  val:   {len(val_samples):>6} samples")
        print(f"  test:  {len(test_samples):>6} samples")
        print()
        print(f"  data.yaml: {args.output_dir / 'data.yaml'}")
    print("=" * 76)

    return 0


if __name__ == "__main__":
    sys.exit(main())
