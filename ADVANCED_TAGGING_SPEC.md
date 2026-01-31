# Advanced Multi-Type Tagging System - Specification

## Overview

This document specifies a comprehensive, multi-type tagging system for video annotation with support for:
- **Checkbox groups** (multi-select)
- **Dropdown groups** (single-select)
- **Text fields** (free-form)
- **Dynamic UI generation** based on tag group definitions

## Database Schema

### Tables

**tag_groups**
- `id` - Primary key
- `group_name` - Unique identifier (e.g., "ground_truth", "confidence_level")
- `display_name` - Human-readable name (e.g., "Ground Truth", "Confidence Level")
- `group_type` - Control type: "checkbox", "dropdown", "text", "textarea"
- `description` - Help text for annotators
- `is_required` - Whether group must be completed
- `applies_to` - Comma-separated: "time_range", "keyframe", "both"
- `sort_order` - Display order in UI

**tag_options**
- `id` - Primary key
- `group_id` - Foreign key to tag_groups
- `option_value` - Value stored in database
- `display_text` - Text shown to user
- `is_negative` - Whether this is a negative example indicator
- `description` - Tooltip/help text
- `sort_order` - Display order within group

**annotation_tags**
- `id` - Primary key
- `annotation_id` - ID of time_range_tag or keyframe_annotation
- `annotation_type` - "time_range" or "keyframe"
- `group_id` - Foreign key to tag_groups
- `tag_value` - Stored value (can be multiple for checkboxes, comma-separated)

## Tag Taxonomy

### 1. Ground Truth (Dropdown, Required)
**Group:** `ground_truth`
**Type:** dropdown
**Applies to:** both

Options:
- power_loading
- normal_loading
- normal_approach
- license_plate
- boat_registration
- face_detected

### 2. Confidence Level (Dropdown, Required)
**Group:** `confidence_level`
**Type:** dropdown
**Applies to:** both

Options:
- certain
- likely
- unsure
- needs_expert_review
- ambiguous_case

### 3. False Positive Type - Power Loading (Checkbox)
**Group:** `false_positive_power_loading`
**Type:** checkbox
**Applies to:** both
**Show when:** Ground truth is power_loading AND is_negative is checked

Options:
- motor_running_legitimately
- natural_water_movement
- visual_confusion
- similar_activity

### 4. False Positive Type - License Plate (Checkbox)
**Group:** `false_positive_license_plate`
**Type:** checkbox
**Applies to:** both
**Show when:** Ground truth is license_plate AND is_negative is checked

Options:
- vehicle_text_graphics
- plate_lookalike_object
- poor_plate_visibility

### 5. Lighting Conditions (Checkbox, Optional)
**Group:** `lighting_conditions`
**Type:** checkbox
**Applies to:** both

Options:
- bright_overexposed
- low_light_dusk
- night_conditions
- sun_glare
- shadows

### 6. Weather Conditions (Checkbox, Optional)
**Group:** `weather_conditions`
**Type:** checkbox
**Applies to:** both

Options:
- rain
- snow
- fog
- ice_on_ramp
- wind_driven_water

### 7. Water Conditions (Checkbox, Optional)
**Group:** `water_conditions`
**Type:** checkbox
**Applies to:** both

Options:
- rough_water
- strong_current
- wave_action
- calm_water

### 8. Camera Issues (Checkbox, Optional)
**Group:** `camera_issues`
**Type:** checkbox
**Applies to:** both

Options:
- camera_angle_suboptimal
- ptz_camera_moving
- out_of_focus
- motion_blur
- compression_artifacts
- frame_rate_insufficient

### 9. Visibility Issues (Checkbox, Optional)
**Group:** `visibility_issues`
**Type:** checkbox
**Applies to:** both

Options:
- obstructed_view
- distance_too_far
- partial_view_only
- multiple_subjects_overlapping

### 10. Violation Context (Checkbox, Optional)
**Group:** `violation_context`
**Type:** checkbox
**Applies to:** time_range
**Show when:** Ground truth is power_loading

Options:
- pre_violation_positioning
- violation_in_progress
- post_violation_departure
- brief_momentary_contact
- extended_violation
- repeated_attempts

### 11. Motor State (Dropdown, Optional)
**Group:** `motor_state`
**Type:** dropdown
**Applies to:** both
**Show when:** Ground truth is power_loading

Options:
- motor_off
- motor_idling
- motor_propelling
- motor_trimming

### 12. Boat Motion (Dropdown, Optional)
**Group:** `boat_motion`
**Type:** dropdown
**Applies to:** both
**Show when:** Ground truth is power_loading

Options:
- stationary
- backing
- forward_motion
- lateral_movement

### 13. Training Priority (Dropdown, Optional)
**Group:** `training_priority`
**Type:** dropdown
**Applies to:** both

Options:
- critical_edge_case
- common_false_positive
- rare_but_important
- typical_example
- redundant_frame

### 14. Dataset Usage (Dropdown, Optional)
**Group:** `dataset_usage`
**Type:** dropdown
**Applies to:** both

Options:
- include_training
- validation_only
- exclude_low_quality
- gold_standard_example

### 15. Boat Type (Dropdown, Optional)
**Group:** `boat_type`
**Type:** dropdown
**Applies to:** keyframe
**Show when:** Boat visible

Options:
- pontoon
- bowrider
- fishing
- jetski
- sailboat
- kayak_canoe

### 16. Boat Size (Dropdown, Optional)
**Group:** `boat_size`
**Type:** dropdown
**Applies to:** keyframe

Options:
- small
- medium
- large

### 17. Propeller Visible (Dropdown, Optional)
**Group:** `propeller_visible`
**Type:** dropdown
**Applies to:** keyframe

Options:
- yes
- no
- uncertain

### 18. Registration Visible (Dropdown, Optional)
**Group:** `registration_visible`
**Type:** dropdown
**Applies to:** keyframe

Options:
- yes_clearly
- yes_partially
- no
- uncertain

### 19. Vehicle Type (Dropdown, Optional)
**Group:** `vehicle_type`
**Type:** dropdown
**Applies to:** keyframe
**Show when:** Ground truth is license_plate

Options:
- truck
- suv
- car
- trailer_only
- motorcycle

### 20. Plate State (Dropdown, Optional)
**Group:** `plate_state`
**Type:** dropdown
**Applies to:** keyframe
**Show when:** Ground truth is license_plate

Options:
- visible
- obstructed
- missing
- uncertain

### 21. Commercial Vehicle (Dropdown, Optional)
**Group:** `commercial_vehicle`
**Type:** dropdown
**Applies to:** keyframe

Options:
- yes
- no
- uncertain

### 22. Face Angle (Dropdown, Optional)
**Group:** `face_angle`
**Type:** dropdown
**Applies to:** keyframe
**Show when:** Ground truth is face_detected

Options:
- front
- side
- back
- three_quarter

### 23. Face Obstruction (Checkbox, Optional)
**Group:** `face_obstruction`
**Type:** checkbox
**Applies to:** keyframe
**Show when:** Ground truth is face_detected

Options:
- hat
- glasses
- mask
- hand
- hair

### 24. Number of People (Dropdown, Optional)
**Group:** `number_of_people`
**Type:** dropdown
**Applies to:** keyframe

Options:
- one
- two
- three_plus

### 25. Extenuating Circumstances (Checkbox, Optional)
**Group:** `extenuating_circumstances`
**Type:** checkbox
**Applies to:** time_range

Options:
- elderly_disabled_operator
- mechanical_issue_visible
- emergency_situation
- assisting_another_boater
- instructional_situation
- first_time_user_evident
- ramp_conditions_difficult

### 26. Present Indicators (Checkbox)
**Group:** `present_indicators`
**Type:** checkbox
**Applies to:** both
**Show when:** Ground truth is power_loading AND is_negative is FALSE

Options:
- propeller_spray_visible
- forward_thrust_evident
- boat_climbing_trailer
- motor_sound_audible

### 27. Absent Indicators (Checkbox)
**Group:** `absent_indicators`
**Type:** checkbox
**Applies to:** both
**Show when:** Ground truth is power_loading AND is_negative is TRUE

Options:
- no_propeller_spray
- no_forward_motion
- boat_stationary
- winch_only

### 28. Reviewer Notes (Textarea)
**Group:** `reviewer_notes`
**Type:** textarea
**Applies to:** both

### 29. Flagged for Discussion (Checkbox)
**Group:** `flags`
**Type:** checkbox
**Applies to:** both

Options:
- flagged_for_discussion
- consensus_needed
- expert_review_required

## UI Implementation

### Annotation Modal Layout

```
┌─────────────────────────────────────────────┐
│ Add Time Range Tag / Keyframe Annotation   │
├─────────────────────────────────────────────┤
│                                             │
│ ** REQUIRED FIELDS **                       │
│                                             │
│ Ground Truth: [Dropdown ▼]                 │
│ Confidence Level: [Dropdown ▼]             │
│                                             │
│ ☐ Negative Example                         │
│                                             │
│ ** ENVIRONMENTAL FACTORS **                 │
│ (collapsible section)                       │
│                                             │
│ Lighting: ☐ bright ☐ low_light ☐ glare    │
│ Weather: ☐ rain ☐ snow ☐ fog              │
│ Water: ☐ rough ☐ current ☐ calm           │
│                                             │
│ ** TECHNICAL/QUALITY ISSUES **              │
│ (collapsible section)                       │
│                                             │
│ Camera: ☐ angle ☐ moving ☐ blur           │
│ Visibility: ☐ obstructed ☐ far ☐ partial  │
│                                             │
│ ** BEHAVIORAL CONTEXT **                    │
│ (show when ground_truth=power_loading)      │
│ (collapsible section)                       │
│                                             │
│ Violation Context: ☐ in_progress ☐ pre ... │
│ Motor State: [Dropdown ▼]                  │
│ Boat Motion: [Dropdown ▼]                  │
│                                             │
│ ** DISTINGUISHING FEATURES **               │
│ (show when ground_truth=power_loading)      │
│ (collapsible section)                       │
│                                             │
│ Present: ☐ spray ☐ thrust ☐ climbing      │
│ Absent: ☐ no_spray ☐ stationary           │
│                                             │
│ ** OBJECT ATTRIBUTES **                     │
│ (show for keyframe only)                    │
│ (collapsible section)                       │
│                                             │
│ Boat Type: [Dropdown ▼]                    │
│ Boat Size: [Dropdown ▼]                    │
│ Propeller Visible: [Dropdown ▼]            │
│                                             │
│ ** TRAINING METADATA **                     │
│ (collapsible section)                       │
│                                             │
│ Priority: [Dropdown ▼]                     │
│ Dataset Usage: [Dropdown ▼]                │
│                                             │
│ ** REVIEWER NOTES **                        │
│                                             │
│ Notes: [Textarea]                           │
│                                             │
│ Flags: ☐ discussion ☐ consensus ☐ expert   │
│                                             │
│ [Save] [Cancel]                             │
└─────────────────────────────────────────────┘
```

### Dynamic Behavior

1. **Conditional Display**: Sections show/hide based on:
   - Ground truth selection
   - Negative example checkbox
   - Annotation type (time_range vs keyframe)

2. **Collapsible Sections**: All optional sections start collapsed

3. **Required Validation**: Cannot save without Ground Truth and Confidence

4. **Smart Defaults**: Remember last-used values per session

## API Design

### Get Tag Schema
```
GET /api/tag-schema?annotation_type=time_range&ground_truth=power_loading&is_negative=false

Response:
{
  "groups": [
    {
      "id": 1,
      "group_name": "ground_truth",
      "display_name": "Ground Truth",
      "group_type": "dropdown",
      "is_required": true,
      "description": "Select the primary classification",
      "options": [...]
    },
    ...
  ]
}
```

### Save Annotation with Tags
```
POST /api/videos/1/time-range-tags

{
  "tag_name": "power_loading",
  "start_time": 0.0,
  "end_time": 45.0,
  "is_negative": false,
  "comment": "Clear violation with all indicators",
  "tags": {
    "ground_truth": "power_loading",
    "confidence_level": "certain",
    "lighting_conditions": ["sun_glare", "bright_overexposed"],
    "weather_conditions": [],
    "motor_state": "motor_propelling",
    "boat_motion": "forward_motion",
    "present_indicators": ["propeller_spray_visible", "forward_thrust_evident"],
    "training_priority": "gold_standard_example",
    "dataset_usage": "include_training",
    "reviewer_notes": "Perfect example for training",
    "flags": ["gold_standard_example"]
  }
}
```

## Implementation Priority

**Phase 1** (Core):
1. Database schema ✓
2. Tag group CRUD API
3. Dynamic form generator
4. Ground Truth + Confidence (required fields)

**Phase 2** (Environmental):
1. Lighting, Weather, Water conditions
2. Camera and Visibility issues

**Phase 3** (Context-Specific):
1. Behavioral context (power_loading specific)
2. Distinguishing features
3. False positive types

**Phase 4** (Object Attributes):
1. Boat, Vehicle, Face attributes
2. Conditional display logic

**Phase 5** (Training Metadata):
1. Training priority
2. Dataset usage
3. Reviewer notes and flags

## Benefits

1. **Structured Data**: Queryable, analyzable tag data
2. **Consistency**: Standardized vocabulary across annotators
3. **Speed**: Checkboxes faster than typing
4. **Flexibility**: Easy to add new tag groups
5. **ML-Ready**: Direct export to training formats
6. **Quality**: Required fields ensure minimum data quality
7. **Context**: Conditional display shows only relevant tags

This system transforms annotation from free-form tagging into a structured, comprehensive data collection process suitable for professional ML training pipelines.
