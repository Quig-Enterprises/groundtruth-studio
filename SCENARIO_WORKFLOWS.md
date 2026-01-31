# Scenario-Based Annotation Workflows

## Overview

Groundtruth Studio now uses scenario-based workflows for creating annotations. Instead of generic forms, users select from predefined scenarios that guide them through multi-step annotation processes optimized for specific situations.

## How It Works

### For Keyframe Annotations

1. **Click "+ Keyframe"** button
2. **Select Scenario** from the list (sorted by most recently used):
   - Loading Boat onto Trailer
   - Boat Operating on Water
   - Other Scenario
3. **Complete Multi-Step Workflow** - Draw bounding boxes for each required element
4. **Provide Additional Details** - Fill in scenario-specific metadata
5. **Save** - All data is stored with the annotation

### For Time Range Tags

1. **Click "+ Tag"** button
2. **Select Scenario** from the list:
   - Power Loading
   - Boat Operation
   - No Wake Zone
   - Other Activity
3. **Set Time Range** - Start and end times
4. **Provide Details** - Fill in scenario-specific metadata
5. **Save** - Tag is created with structured data

## Defined Scenarios

### Keyframe Scenarios

#### 1. Loading Boat onto Trailer
**Use Case**: Boat being loaded onto or unloaded from trailer at ramp

**Bounding Box Steps**:
1. Boat Registration Number (optional, can mark "Not Visible")
2. Entire Boat (required)
3. Tow Vehicle License Plate (optional, can mark "Not Visible")
4. Boat Operator (optional, can mark "Not Visible")
5. Propwash/Wake (optional, can mark "Not Visible")
6. Tow Vehicle (optional, can mark "Not Visible")

**Additional Tags**:
- Loading Direction: loading_onto_trailer | unloading_from_trailer | unclear (required)

#### 2. Boat Operating on Water
**Use Case**: Boat in operation on open water

**Bounding Box Steps**:
1. Boat Registration Number (optional, can mark "Not Visible")
2. Entire Boat (required)
3. Boat Operator (optional, can mark "Not Visible")
4. Boat Wake (optional, can mark "Not Visible")

**Additional Tags**:
- Wake Type: normal_wake | enhanced_wake | minimal_wake | no_wake | not_visible (required)
- Boat Activity: cruising | towing_watersports | fishing | anchored | other (optional)

#### 3. Other Scenario
**Use Case**: Generic annotation for scenarios not covered above

**Bounding Box Steps**:
1. Primary Subject (required)

**Additional Tags**:
- Scenario Description: Free text (required)

### Time Range Scenarios

#### 1. Power Loading
**Use Case**: Boat being power loaded onto trailer

**Tags**:
- Loading Type: power_loading | winch_loading | combination (required)
- Ramp Angle: steep | moderate | gentle | unclear (optional)

#### 2. Boat Operation
**Use Case**: General boat operation period

**Tags**:
- Operation Type: cruising | towing_watersports | fishing | racing | other (required)
- Water Conditions: calm | choppy | rough | unclear (optional)

#### 3. No Wake Zone
**Use Case**: Boat operating in designated no-wake zone

**Tags**:
- Compliance: compliant | non_compliant | unclear (required)

#### 4. Other Activity
**Use Case**: Other time range annotation

**Tags**:
- Activity Description: Free text (required)

## Workflow Features

### Progressive Disclosure
Each scenario only shows relevant steps and fields, reducing cognitive load.

### Smart Ordering
Scenarios are ordered by most recently used, making frequent annotations faster.

### Visual Feedback
- Progress bar shows completion percentage
- Clear step-by-step instructions
- Visual indicators for required vs. optional steps

### Flexible Workflows
- Optional steps can be skipped
- "Not Visible" option for elements that aren't in frame
- Additional notes field for all scenarios

## Data Storage

All workflow data is stored as structured tags in the `annotation_tags` table:

```json
{
  "scenario": "loading_boat_trailer",
  "bboxes": {
    "boat_registration": {"x": 100, "y": 50, "width": 200, "height": 80},
    "entire_boat": {"x": 50, "y": 20, "width": 400, "height": 300},
    "license_plate": {"x": 300, "y": 400, "width": 150, "height": 40}
  },
  "notVisible": ["propwash", "boat_operator"],
  "loading_direction": "loading_onto_trailer"
}
```

## Extending Scenarios

To add new scenarios, edit `/static/js/annotation-scenarios.js`:

1. Add scenario definition to `annotationScenarios.keyframe` or `annotationScenarios.time_range`
2. Define steps (for keyframe) or tags (for both)
3. Specify required vs. optional fields
4. Add notVisible options where applicable

## Files

- `/static/js/annotation-scenarios.js` - Scenario definitions
- `/static/js/scenario-workflow.js` - Workflow controller
- `/static/css/scenario-workflow.css` - Workflow UI styles
- `/static/js/annotate-integration.js` - Integration with main app

## Usage Analytics

The system tracks scenario usage via localStorage to provide "most recent first" ordering. This data is stored client-side and persists across sessions.
