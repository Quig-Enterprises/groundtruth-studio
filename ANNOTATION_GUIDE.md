# Video Annotation Guide

Complete guide to using the advanced video annotation features for AI training dataset preparation.

## Overview

The Video Archive System includes a professional-grade annotation interface designed for preparing training data for machine learning models. The system supports:

1. **Time Range Tags** - Tag periods of video with behavior labels
2. **Keyframe Annotations** - Mark specific frames with bounding boxes and event tags

## Accessing the Annotation Interface

1. From the main library page, click any video card
2. Click the **"Annotate Video"** button
3. Or navigate directly to: `http://localhost:5000/annotate?id=VIDEO_ID`

## Interface Layout

### Left Panel: Video Player
- **Video Display**: Full video player with standard controls
- **Playback Controls**:
  - Play/Pause
  - Seek backward/forward (±1s, ±5s)
  - Draw BBox toggle for keyframe annotation
- **Time Display**: Current time / Total duration
- **Metadata**: Duration and resolution info

### Right Panel: Annotations
- **Time Range Tags Section**: List of all period-based tags
- **Keyframe Annotations Section**: List of all frame-based annotations

## Time Range Tags

Time range tags allow you to mark periods in the video where specific behaviors occur. Tags can overlap, enabling complex multi-behavior annotation.

### Creating a Time Range Tag

1. **Seek to Start Point**: Use playback controls to find the beginning of the behavior
2. **Click "+ Tag" button**
3. **Tag Dialog Opens**:
   - **Tag Name**: Type or select from existing tags (auto-suggest enabled)
   - **Start Time**: Auto-populated with current video timestamp
   - **End Time**: Optional - leave empty to close later
   - **Comment**: Optional notes about this tag instance
4. **Click "Save Tag"**

### Auto-Suggest Feature

As you type tag names, the system suggests previously used tags. This ensures:
- Consistent naming across annotations
- Faster tagging workflow
- Easy reuse of common tags

**Example tags for power loading analysis:**
- `power_loading`
- `motor_running`
- `spray_active`
- `operator_visible`
- `equipment_setup`

### Closing Open Tags

If you left the end time empty:

1. **Seek to End Point**: Navigate to where the behavior ends
2. **Click "Close at Current Time"** on the tag
3. The tag's end time is set to the current video timestamp

This workflow is ideal for:
- Real-time annotation while watching
- Long-duration behaviors
- Activities with uncertain end points

### Tag Overlap

Multiple tags can be active simultaneously:

```
Timeline: |-------- 60 seconds ---------|
Tag 1:    |===== power_loading =====|
Tag 2:         |== motor_starts ==|
Tag 3:              |====== spray_visible ======|
```

This enables multi-level annotation:
- **Activity level**: Overall task being performed
- **Component level**: Individual system states
- **Event level**: Specific occurrences

### Managing Time Range Tags

Each tag shows:
- **Tag name** (bold)
- **Time range** (start - end, or "open" if not closed)
- **Comment** (if provided)

**Actions:**
- **Seek Start**: Jump video to tag start time
- **Close at Current Time**: Set end time (for open tags)
- **Delete**: Remove the tag

## Keyframe Annotations

Keyframe annotations combine spatial information (bounding boxes) with temporal tags. Use these to mark specific moments with precise object localization.

### Creating a Keyframe Annotation

1. **Seek to Target Frame**: Find the exact moment to annotate
2. **Click "+ Keyframe" button**
   - Video automatically pauses
   - Drawing mode activates
3. **Draw Bounding Box**:
   - Click and drag on the video to draw a rectangle
   - The box highlights the region of interest (ROI)
   - Minimum size: 5x5 pixels
4. **Keyframe Dialog Opens**:
   - **Timestamp**: Auto-populated (frame time)
   - **Bounding Box**: Auto-populated (x, y, width, height)
   - **Activity Tag**: Overall activity (e.g., `power_loading`)
   - **Moment Tag**: Specific event (e.g., `motor_starts`, `spray_visible`)
   - **Comment**: Optional notes
5. **Click "Save Annotation"**

### Tag Hierarchy

**Activity Tags** - High-level context:
- `power_loading`
- `equipment_inspection`
- `maintenance_operation`
- `calibration_procedure`

**Moment Tags** - Specific events:
- `motor_starts`
- `motor_stops`
- `spray_visible`
- `spray_ends`
- `indicator_light_on`
- `gauge_reading_change`
- `operator_gesture`

### Bounding Box Best Practices

1. **Tight Fit**: Draw boxes close to object boundaries
2. **Consistent Object**: Keep box on same object across frames
3. **Avoid Overlap**: Use separate annotations for multiple objects
4. **Include Context**: If needed, box can include surrounding area

**Example use cases:**
- Motor housing during startup
- Spray nozzle during activation
- Gauge display during reading
- Operator hand during control interaction

### Managing Keyframe Annotations

Each annotation shows:
- **Timestamp** (green, clickable)
- **Bounding box coordinates** (x, y, width, height)
- **Activity tag** (blue pill)
- **Moment tag** (orange pill)
- **Comment** (if provided)

**Actions:**
- **Seek to Frame**: Jump video to annotation timestamp
- **Show BBox**: Display the bounding box on video for 3 seconds
- **Delete**: Remove the annotation

## Workflow Examples

### Example 1: Annotating Power Loading Sequence

**Objective**: Tag a 45-second power loading operation with key moments

**Time Range Tags:**
1. Start video at 0:00
2. Add tag `power_loading` starting at 0:00
3. When motor starts (0:08), leave the tag open
4. Watch until operation completes (0:45)
5. Close `power_loading` tag at 0:45

**Keyframe Annotations:**
1. Seek to motor start (0:08)
2. Add keyframe, draw box around motor
3. Activity: `power_loading`, Moment: `motor_starts`
4. Seek to first spray visible (0:15)
5. Add keyframe, draw box around nozzle
6. Activity: `power_loading`, Moment: `spray_visible`
7. Seek to spray end (0:42)
8. Add keyframe, draw box around nozzle
9. Activity: `power_loading`, Moment: `spray_ends`

### Example 2: Multi-Behavior Overlap

**Objective**: Annotate video with overlapping behaviors

```
0:00 - Operator enters frame
0:05 - Equipment setup begins
0:15 - Motor starts (setup continues)
0:30 - Setup completes, operation begins
0:45 - Operation continues
1:00 - Operation ends
```

**Tags:**
- `operator_visible`: 0:00 - 1:00
- `equipment_setup`: 0:05 - 0:30
- `motor_running`: 0:15 - 1:00
- `active_operation`: 0:30 - 1:00

### Example 3: Rapid Event Sequence

**Objective**: Mark multiple quick events with keyframes

For a 10-second sequence with 5 distinct events:

1. Pause at each event
2. Add keyframe with tight bounding box
3. Use consistent activity tag: `rapid_sequence`
4. Use descriptive moment tags:
   - `event_1_indicator`
   - `event_2_spray`
   - `event_3_rotation`
   - `event_4_gauge_change`
   - `event_5_shutdown`

## Data Export

All annotations are stored in PostgreSQL database tables:

### Time Range Tags Table
```sql
SELECT video_id, tag_name, start_time, end_time, comment
FROM time_range_tags
WHERE video_id = ?
ORDER BY start_time
```

### Keyframe Annotations Table
```sql
SELECT video_id, timestamp, bbox_x, bbox_y, bbox_width, bbox_height,
       activity_tag, moment_tag, comment
FROM keyframe_annotations
WHERE video_id = ?
ORDER BY timestamp
```

### Export Script Example

```python
from app.database import VideoDatabase

db = VideoDatabase('video_archive.db')

# Export for video ID 1
video_id = 1
time_tags = db.get_time_range_tags(video_id)
keyframes = db.get_keyframe_annotations(video_id)

# Convert to training format
for tag in time_tags:
    print(f"Behavior: {tag['tag_name']}")
    print(f"  Duration: {tag['start_time']:.2f}s - {tag['end_time']:.2f}s")

for kf in keyframes:
    print(f"Frame: {kf['timestamp']:.2f}s")
    print(f"  BBox: ({kf['bbox_x']}, {kf['bbox_y']}, {kf['bbox_width']}, {kf['bbox_height']})")
    print(f"  Activity: {kf['activity_tag']}, Moment: {kf['moment_tag']}")
```

## Tips for Efficient Annotation

### Speed Tips

1. **Use Keyboard Shortcuts**:
   - Spacebar: Play/Pause
   - Arrow keys: Frame-by-frame (browser default)
   - Use +1s/-1s buttons for quick seeking

2. **Pre-plan Tag Taxonomy**: Define your tag list before starting:
   ```
   Activities:
   - power_loading
   - maintenance
   - inspection

   Moments:
   - motor_starts
   - motor_stops
   - spray_visible
   - spray_ends
   - gauge_reading
   ```

3. **Batch Similar Videos**: Annotate videos of same type together to maintain consistency

4. **Leave Comments**: Add context that's not obvious from tags alone

### Quality Tips

1. **Consistent Naming**: Always use auto-suggest for existing tags
2. **Clear Boundaries**: Close tags precisely at behavior end
3. **Verify Boxes**: Use "Show BBox" to confirm bounding box placement
4. **Document Edge Cases**: Use comments for unusual situations

### Organization Tips

1. **Tag Hierarchies**: Use consistent prefixes:
   - `motor_*` for motor-related events
   - `spray_*` for spray system events
   - `operator_*` for human actions

2. **Activity + Moment Pattern**: Always pair activity context with specific moments

3. **Review Workflow**: After annotating, review annotations:
   - Check all time ranges are closed
   - Verify bounding boxes make sense
   - Ensure tag names are consistent

## API Integration

For automated processing or custom tools:

### Get All Annotations
```javascript
// Time range tags
const response = await fetch(`/api/videos/${videoId}/time-range-tags`);
const data = await response.json();

// Keyframe annotations
const response = await fetch(`/api/videos/${videoId}/keyframe-annotations`);
const data = await response.json();
```

### Add Annotation Programmatically
```javascript
// Add time range tag
await fetch(`/api/videos/${videoId}/time-range-tags`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
        tag_name: 'power_loading',
        start_time: 0.0,
        end_time: 45.5,
        comment: 'Full operation cycle'
    })
});

// Add keyframe
await fetch(`/api/videos/${videoId}/keyframe-annotations`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
        timestamp: 8.2,
        bbox_x: 150,
        bbox_y: 200,
        bbox_width: 300,
        bbox_height: 250,
        activity_tag: 'power_loading',
        moment_tag: 'motor_starts'
    })
});
```

## Troubleshooting

### Bounding Box Not Drawing
- Ensure "Draw BBox" button is active (should be red)
- Click and drag, don't just click
- Draw must be at least 5x5 pixels

### Auto-Suggest Not Working
- Ensure you have existing tags in the database
- Type at least one character
- Suggestions load after first use

### Video Won't Play
- Check video file exists in downloads folder
- Verify browser supports MP4 format
- Check browser console for errors

### Annotations Not Saving
- Check browser console for API errors
- Verify database file has write permissions
- Ensure all required fields are filled

## Best Practices for AI Training

1. **Consistent Labeling**: Use same tag names for same behaviors across all videos

2. **Balanced Dataset**: Annotate diverse examples:
   - Different lighting conditions
   - Various angles
   - Multiple operators (if applicable)
   - Normal and edge cases

3. **Temporal Precision**:
   - Mark exact start/end frames for time-critical events
   - Use keyframes for instantaneous events
   - Use time ranges for sustained behaviors

4. **Spatial Precision**:
   - Draw tight bounding boxes
   - Be consistent with box sizing
   - Include full object, avoid cropping

5. **Documentation**:
   - Use comments for ambiguous cases
   - Note data quality issues
   - Record special conditions

## Next Steps

After annotation:
1. Export annotations to training format
2. Extract frames at keyframe timestamps
3. Generate crops from bounding boxes
4. Create training/validation split
5. Train your model!

For questions or issues, check the main README.md or open an issue on GitHub.
