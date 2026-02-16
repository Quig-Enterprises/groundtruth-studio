#!/usr/bin/env python3
"""
Import supplemental training images for vehicle classification/detection.

This script handles bringing in additional images from external sources
(downloads, manual photos, etc.) into the training directory structure.

Examples:
    # Import sedan images for classification
    ./import_supplemental_images.py --source ~/Downloads/sedan_photos --class sedan

    # Import with resizing
    ./import_supplemental_images.py --source ~/Downloads/trucks --class "pickup truck" --max-size 640

    # Import for detection (creates empty labels for manual annotation)
    ./import_supplemental_images.py --source ~/Downloads/vehicles --class SUV --mode detection

    # Dry run
    ./import_supplemental_images.py --source ~/Downloads/boats --class kayak --dry-run
"""

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import List, Tuple

try:
    from PIL import Image
except ImportError:
    print("Error: PIL/Pillow is required. Install with: pip install Pillow")
    sys.exit(1)

# Canonical vehicle types list (matching export script)
VEHICLE_TYPES = [
    'sedan', 'pickup truck', 'SUV', 'minivan', 'van',
    'tractor', 'ATV', 'UTV', 'snowmobile', 'golf cart', 'motorcycle', 'trailer',
    'bus', 'semi truck', 'dump truck',
    'rowboat', 'fishing boat', 'speed boat', 'pontoon boat', 'kayak', 'canoe', 'sailboat', 'jet ski'
]

# Supported image formats
SUPPORTED_FORMATS = {'.jpg', '.jpeg', '.png', '.bmp', '.webp'}


def validate_vehicle_type(vehicle_type: str) -> str:
    """Validate and normalize vehicle type."""
    vehicle_type_lower = vehicle_type.lower()
    if vehicle_type_lower not in VEHICLE_TYPES:
        print(f"Error: '{vehicle_type}' is not a valid vehicle type.")
        print(f"\nValid types: {', '.join(VEHICLE_TYPES)}")
        sys.exit(1)
    return vehicle_type_lower


def get_image_files(source_dir: Path) -> List[Path]:
    """Get all supported image files from source directory."""
    image_files = []
    for file_path in source_dir.iterdir():
        if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_FORMATS:
            image_files.append(file_path)
    return sorted(image_files)


def resize_image(image: Image.Image, max_size: int) -> Image.Image:
    """Resize image maintaining aspect ratio if larger than max_size."""
    width, height = image.size
    max_dimension = max(width, height)

    if max_dimension <= max_size:
        return image

    # Calculate new dimensions
    scale = max_size / max_dimension
    new_width = int(width * scale)
    new_height = int(height * scale)

    return image.resize((new_width, new_height), Image.Resampling.LANCZOS)


def get_next_sequential_number(output_dir: Path) -> int:
    """Get next sequential number for filename (sup_NNN.jpg)."""
    if not output_dir.exists():
        return 1

    max_num = 0
    for file_path in output_dir.glob("sup_*.jpg"):
        try:
            num = int(file_path.stem.split('_')[1])
            max_num = max(max_num, num)
        except (ValueError, IndexError):
            continue

    return max_num + 1


def import_classification(
    source_files: List[Path],
    vehicle_class: str,
    output_base: Path,
    max_size: int | None,
    dry_run: bool
) -> Tuple[int, int]:
    """Import images for classification training."""
    output_dir = output_base / "train" / vehicle_class

    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    next_num = get_next_sequential_number(output_dir)
    imported = 0
    errors = 0

    print(f"\nImporting to: {output_dir}")
    print(f"Starting number: {next_num}")

    for source_file in source_files:
        output_filename = f"sup_{next_num:03d}.jpg"
        output_path = output_dir / output_filename

        try:
            # Open and process image
            with Image.open(source_file) as img:
                # Convert to RGB if needed
                if img.mode != 'RGB':
                    img = img.convert('RGB')

                # Resize if requested
                if max_size:
                    img = resize_image(img, max_size)

                # Save
                if dry_run:
                    print(f"[DRY RUN] Would import: {source_file.name} -> {output_filename}")
                else:
                    img.save(output_path, 'JPEG', quality=95)
                    print(f"Imported: {source_file.name} -> {output_filename}")

                imported += 1
                next_num += 1

        except Exception as e:
            print(f"Error processing {source_file.name}: {e}")
            errors += 1

    return imported, errors


def import_detection(
    source_files: List[Path],
    vehicle_class: str,
    output_base: Path,
    max_size: int | None,
    dry_run: bool
) -> Tuple[int, int]:
    """Import images for detection training (creates empty label files)."""
    images_dir = output_base / "images"
    labels_dir = output_base / "labels"

    if not dry_run:
        images_dir.mkdir(parents=True, exist_ok=True)
        labels_dir.mkdir(parents=True, exist_ok=True)

    # Get next number from existing images
    existing_images = list(images_dir.glob("sup_*.jpg")) if images_dir.exists() else []
    max_num = 0
    for img_path in existing_images:
        try:
            num = int(img_path.stem.split('_')[1])
            max_num = max(max_num, num)
        except (ValueError, IndexError):
            continue

    next_num = max_num + 1
    imported = 0
    errors = 0

    print(f"\nImporting to: {images_dir}")
    print(f"Creating empty labels in: {labels_dir}")
    print(f"Starting number: {next_num}")
    print(f"Note: You will need to manually annotate these images for detection training")

    for source_file in source_files:
        image_filename = f"sup_{next_num:03d}.jpg"
        label_filename = f"sup_{next_num:03d}.txt"
        image_path = images_dir / image_filename
        label_path = labels_dir / label_filename

        try:
            # Open and process image
            with Image.open(source_file) as img:
                # Convert to RGB if needed
                if img.mode != 'RGB':
                    img = img.convert('RGB')

                # Resize if requested
                if max_size:
                    img = resize_image(img, max_size)

                # Save image and create empty label
                if dry_run:
                    print(f"[DRY RUN] Would import: {source_file.name} -> {image_filename}")
                    print(f"[DRY RUN] Would create empty label: {label_filename}")
                else:
                    img.save(image_path, 'JPEG', quality=95)
                    label_path.touch()  # Create empty label file
                    print(f"Imported: {source_file.name} -> {image_filename} (+ empty label)")

                imported += 1
                next_num += 1

        except Exception as e:
            print(f"Error processing {source_file.name}: {e}")
            errors += 1

    return imported, errors


def main():
    parser = argparse.ArgumentParser(
        description="Import supplemental training images for vehicle classification/detection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --source ~/Downloads/sedan_photos --class sedan
  %(prog)s --source ~/Downloads/trucks --class "pickup truck" --max-size 640
  %(prog)s --source ~/Downloads/vehicles --class SUV --mode detection
  %(prog)s --source ~/Downloads/boats --class kayak --dry-run

Valid vehicle types:
  """ + ', '.join(VEHICLE_TYPES)
    )

    parser.add_argument(
        '--source',
        type=Path,
        required=True,
        help='Source directory containing images to import'
    )

    parser.add_argument(
        '--class',
        dest='vehicle_class',
        required=True,
        help='Vehicle class/type for these images'
    )

    parser.add_argument(
        '--output',
        type=Path,
        default=Path('/models/custom/people-vehicles-objects/data/supplemental'),
        help='Output base directory (default: /models/custom/people-vehicles-objects/data/supplemental)'
    )

    parser.add_argument(
        '--mode',
        choices=['classification', 'detection'],
        default='classification',
        help='Import mode: classification (organized by class) or detection (with empty labels)'
    )

    parser.add_argument(
        '--max-size',
        type=int,
        metavar='PIXELS',
        help='Resize images to this max dimension (longest edge) while maintaining aspect ratio'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be imported without actually doing it'
    )

    args = parser.parse_args()

    # Validate inputs
    if not args.source.exists():
        print(f"Error: Source directory does not exist: {args.source}")
        sys.exit(1)

    if not args.source.is_dir():
        print(f"Error: Source path is not a directory: {args.source}")
        sys.exit(1)

    vehicle_class = validate_vehicle_type(args.vehicle_class)

    # Get image files
    source_files = get_image_files(args.source)

    if not source_files:
        print(f"Error: No supported image files found in {args.source}")
        print(f"Supported formats: {', '.join(SUPPORTED_FORMATS)}")
        sys.exit(1)

    # Print summary
    print("=" * 70)
    print("SUPPLEMENTAL IMAGE IMPORT")
    print("=" * 70)
    print(f"Source directory: {args.source}")
    print(f"Vehicle class: {vehicle_class}")
    print(f"Mode: {args.mode}")
    print(f"Images found: {len(source_files)}")
    if args.max_size:
        print(f"Max size: {args.max_size}px")
    if args.dry_run:
        print("\n*** DRY RUN MODE - NO FILES WILL BE MODIFIED ***")
    print("=" * 70)

    # Import based on mode
    if args.mode == 'classification':
        imported, errors = import_classification(
            source_files,
            vehicle_class,
            args.output,
            args.max_size,
            args.dry_run
        )
    else:  # detection
        imported, errors = import_detection(
            source_files,
            vehicle_class,
            args.output,
            args.max_size,
            args.dry_run
        )

    # Print final summary
    print("\n" + "=" * 70)
    print("IMPORT SUMMARY")
    print("=" * 70)
    print(f"Successfully imported: {imported}")
    if errors:
        print(f"Errors: {errors}")
    print("=" * 70)

    if args.dry_run:
        print("\nThis was a dry run. Re-run without --dry-run to perform the import.")
    elif args.mode == 'detection':
        print("\nNext steps:")
        print(f"1. Annotate images in: {args.output / 'images'}")
        print(f"2. Save labels to: {args.output / 'labels'}")
        print("3. Use your detection training pipeline")
    else:
        print(f"\nImages imported to: {args.output / 'train' / vehicle_class}")
        print("Ready for classification training!")

    sys.exit(0 if errors == 0 else 1)


if __name__ == '__main__':
    main()
