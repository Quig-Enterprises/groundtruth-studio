#!/usr/bin/env python3
"""
Extract vehicle images from COCO dataset for baseline training data.

Supports two modes:
- classification: Crop individual vehicle bboxes into class-specific directories
- detection: Copy full images with YOLO format annotations
"""

import argparse
import json
import os
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image

# Canonical vehicle types for our pipeline
VEHICLE_TYPES = [
    'sedan', 'pickup truck', 'SUV', 'minivan', 'van',
    'tractor', 'ATV', 'UTV', 'snowmobile', 'golf cart', 'motorcycle', 'trailer',
    'bus', 'semi truck', 'dump truck',
    'rowboat', 'fishing boat', 'speed boat', 'pontoon boat', 'kayak', 'canoe', 'sailboat', 'jet ski'
]

# COCO category mapping to our vehicle types
COCO_TO_VEHICLE = {
    'car': 'sedan',
    'truck': 'pickup truck',
    'bus': 'bus',
    'motorcycle': 'motorcycle',
    'boat': 'fishing boat',
    'train': None,      # Skip
    'airplane': None,   # Skip
}

# COCO category IDs (standard COCO 2017)
COCO_VEHICLE_CATEGORIES = {
    2: 'car',           # bicycle -> skip for now
    3: 'car',
    4: 'motorcycle',
    6: 'bus',
    7: 'train',
    8: 'truck',
    9: 'boat',
}


class COCOVehicleExtractor:
    """Extract vehicle images from COCO dataset."""

    def __init__(self, coco_dir: Path, output_dir: Path, mode: str,
                 split: str, max_per_class: int, dry_run: bool):
        self.coco_dir = coco_dir
        self.output_dir = output_dir
        self.mode = mode
        self.split = split
        self.max_per_class = max_per_class
        self.dry_run = dry_run

        self.images_dir = coco_dir / f"{split}2017"
        self.annot_file = coco_dir / "annotations" / f"instances_{split}2017.json"

        self.stats = defaultdict(int)
        self.vehicle_class_to_id = {v: i for i, v in enumerate(VEHICLE_TYPES)}

    def run(self):
        """Main extraction workflow."""
        print(f"COCO Vehicle Extraction")
        print(f"  Mode: {self.mode}")
        print(f"  Split: {self.split}")
        print(f"  COCO dir: {self.coco_dir}")
        print(f"  Output dir: {self.output_dir}")
        print(f"  Max per class: {self.max_per_class}")
        print(f"  Dry run: {self.dry_run}")
        print()

        # Validate paths
        if not self.images_dir.exists():
            raise FileNotFoundError(f"Images directory not found: {self.images_dir}")
        if not self.annot_file.exists():
            raise FileNotFoundError(f"Annotation file not found: {self.annot_file}")

        # Load annotations
        print(f"Loading annotations from {self.annot_file}...")
        with open(self.annot_file) as f:
            coco_data = json.load(f)

        # Build image ID to filename mapping
        image_map = {img['id']: img for img in coco_data['images']}
        print(f"  Loaded {len(image_map)} images")

        # Filter vehicle annotations
        vehicle_annotations = self._filter_vehicle_annotations(coco_data['annotations'])
        print(f"  Found {len(vehicle_annotations)} vehicle annotations")
        print()

        # Create output directories
        if not self.dry_run:
            self._create_output_dirs()

        # Extract by mode
        if self.mode == 'classification':
            self._extract_classification(vehicle_annotations, image_map)
        elif self.mode == 'detection':
            self._extract_detection(vehicle_annotations, image_map)
        else:
            raise ValueError(f"Unknown mode: {self.mode}")

        # Print summary
        self._print_summary()

    def _filter_vehicle_annotations(self, annotations: List[dict]) -> List[dict]:
        """Filter annotations to only vehicle categories."""
        vehicle_annots = []
        for ann in annotations:
            cat_id = ann['category_id']
            if cat_id in COCO_VEHICLE_CATEGORIES:
                coco_name = COCO_VEHICLE_CATEGORIES[cat_id]
                our_name = COCO_TO_VEHICLE.get(coco_name)
                if our_name:  # Skip None mappings (train, airplane)
                    ann['vehicle_class'] = our_name
                    vehicle_annots.append(ann)
        return vehicle_annots

    def _create_output_dirs(self):
        """Create output directory structure."""
        if self.mode == 'classification':
            for vehicle_type in VEHICLE_TYPES:
                class_dir = self.output_dir / self.split / vehicle_type
                class_dir.mkdir(parents=True, exist_ok=True)
        elif self.mode == 'detection':
            (self.output_dir / self.split / 'images').mkdir(parents=True, exist_ok=True)
            (self.output_dir / self.split / 'labels').mkdir(parents=True, exist_ok=True)

    def _extract_classification(self, annotations: List[dict], image_map: Dict):
        """Extract cropped vehicle bboxes for classification."""
        # Group by vehicle class
        class_annotations = defaultdict(list)
        for ann in annotations:
            class_annotations[ann['vehicle_class']].append(ann)

        print("Extracting classification crops...")
        for vehicle_class, annots in sorted(class_annotations.items()):
            print(f"  {vehicle_class}: {len(annots)} instances", end='')

            # Limit per class
            annots_to_process = annots[:self.max_per_class]
            if len(annots) > self.max_per_class:
                print(f" (limiting to {self.max_per_class})", end='')
            print()

            class_dir = self.output_dir / self.split / vehicle_class
            count = 0

            for ann in annots_to_process:
                img_id = ann['image_id']
                img_info = image_map.get(img_id)
                if not img_info:
                    continue

                img_filename = img_info['file_name']
                img_path = self.images_dir / img_filename

                if not img_path.exists():
                    continue

                # Crop and save
                if not self.dry_run:
                    try:
                        self._crop_and_save(img_path, ann, class_dir, img_id)
                        count += 1
                    except Exception as e:
                        print(f"    Error processing {img_filename}: {e}")
                else:
                    count += 1

            self.stats[vehicle_class] = count

    def _extract_detection(self, annotations: List[dict], image_map: Dict):
        """Extract full images with YOLO format labels for detection."""
        # Group annotations by image
        image_annotations = defaultdict(list)
        for ann in annotations:
            image_annotations[ann['image_id']].append(ann)

        print("Extracting detection images and labels...")

        images_dir = self.output_dir / self.split / 'images'
        labels_dir = self.output_dir / self.split / 'labels'

        processed_images = set()
        total_count = 0

        for img_id, annots in image_annotations.items():
            img_info = image_map.get(img_id)
            if not img_info:
                continue

            img_filename = img_info['file_name']
            img_path = self.images_dir / img_filename

            if not img_path.exists():
                continue

            # Check per-class limits
            class_counts = defaultdict(int)
            for ann in annots:
                class_counts[ann['vehicle_class']] += 1

            skip = False
            for vehicle_class, count in class_counts.items():
                if self.stats[vehicle_class] >= self.max_per_class:
                    skip = True
                    break

            if skip:
                continue

            if img_filename in processed_images:
                continue

            # Copy image and create YOLO label
            if not self.dry_run:
                try:
                    self._copy_image_and_labels(img_path, annots, img_info, images_dir, labels_dir)
                    for ann in annots:
                        self.stats[ann['vehicle_class']] += 1
                    processed_images.add(img_filename)
                    total_count += 1
                except Exception as e:
                    print(f"  Error processing {img_filename}: {e}")
            else:
                for ann in annots:
                    self.stats[ann['vehicle_class']] += 1
                total_count += 1

        print(f"  Processed {total_count} images")

    def _crop_and_save(self, img_path: Path, annotation: dict, output_dir: Path, img_id: int):
        """Crop bbox from image and save."""
        img = Image.open(img_path)

        # COCO bbox format: [x, y, width, height]
        bbox = annotation['bbox']
        x, y, w, h = bbox
        x1, y1, x2, y2 = int(x), int(y), int(x + w), int(y + h)

        # Crop
        crop = img.crop((x1, y1, x2, y2))

        # Save with unique filename
        ann_id = annotation['id']
        output_filename = f"{img_id:012d}_{ann_id:08d}.jpg"
        output_path = output_dir / output_filename

        crop.save(output_path, 'JPEG', quality=95)

    def _copy_image_and_labels(self, img_path: Path, annotations: List[dict],
                                img_info: dict, images_dir: Path, labels_dir: Path):
        """Copy image and create YOLO format label file."""
        # Copy image
        img_filename = img_info['file_name']
        dest_img = images_dir / img_filename
        shutil.copy2(img_path, dest_img)

        # Create YOLO label
        label_filename = Path(img_filename).stem + '.txt'
        label_path = labels_dir / label_filename

        img_width = img_info['width']
        img_height = img_info['height']

        with open(label_path, 'w') as f:
            for ann in annotations:
                # Convert COCO bbox to YOLO format
                bbox = ann['bbox']
                x, y, w, h = bbox

                # YOLO format: class_id center_x center_y width height (normalized)
                center_x = (x + w / 2) / img_width
                center_y = (y + h / 2) / img_height
                norm_w = w / img_width
                norm_h = h / img_height

                vehicle_class = ann['vehicle_class']
                class_id = self.vehicle_class_to_id[vehicle_class]

                f.write(f"{class_id} {center_x:.6f} {center_y:.6f} {norm_w:.6f} {norm_h:.6f}\n")

    def _print_summary(self):
        """Print extraction summary statistics."""
        print()
        print("=" * 60)
        print("EXTRACTION SUMMARY")
        print("=" * 60)

        if self.mode == 'classification':
            print(f"Mode: Classification (cropped bboxes)")
            print(f"Total crops extracted:")
        else:
            print(f"Mode: Detection (full images + YOLO labels)")
            print(f"Total instances per class:")

        total = 0
        for vehicle_class in VEHICLE_TYPES:
            count = self.stats.get(vehicle_class, 0)
            if count > 0:
                print(f"  {vehicle_class:20s}: {count:6d}")
                total += count

        print(f"  {'TOTAL':20s}: {total:6d}")
        print()

        if self.dry_run:
            print("DRY RUN - No files were written")
        else:
            print(f"Output directory: {self.output_dir}")


def main():
    parser = argparse.ArgumentParser(
        description="Extract vehicle images from COCO dataset",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Extract classification crops from COCO train split
  %(prog)s --coco-dir /data/coco --mode classification --split train

  # Extract detection images with YOLO labels
  %(prog)s --coco-dir /data/coco --mode detection --split val

  # Limit to 100 images per class, dry run
  %(prog)s --coco-dir /data/coco --max-per-class 100 --dry-run
        """
    )

    parser.add_argument(
        '--coco-dir',
        type=Path,
        required=True,
        help='Path to COCO dataset directory (with annotations/ and train2017/ subdirs)'
    )

    parser.add_argument(
        '--output',
        type=Path,
        default=Path('/models/custom/people-vehicles-objects/data/coco_vehicles'),
        help='Output directory (default: /models/custom/people-vehicles-objects/data/coco_vehicles)'
    )

    parser.add_argument(
        '--mode',
        choices=['classification', 'detection'],
        default='classification',
        help='Extraction mode: classification (crop bboxes) or detection (full images + labels)'
    )

    parser.add_argument(
        '--split',
        choices=['train', 'val'],
        default='train',
        help='COCO split to use (default: train)'
    )

    parser.add_argument(
        '--max-per-class',
        type=int,
        default=500,
        help='Maximum images/instances per class (default: 500)'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be extracted without writing files'
    )

    args = parser.parse_args()

    # Run extraction
    extractor = COCOVehicleExtractor(
        coco_dir=args.coco_dir,
        output_dir=args.output,
        mode=args.mode,
        split=args.split,
        max_per_class=args.max_per_class,
        dry_run=args.dry_run
    )

    extractor.run()


if __name__ == '__main__':
    main()
