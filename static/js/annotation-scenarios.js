/**
 * Unified Annotation Scenario Definitions
 * Each scenario can optionally have:
 * - Bounding boxes (spatial annotation)
 * - Event boundaries (temporal annotation)
 * - Neither (whole-clip contextual data)
 */

const annotationScenarios = {
    // Boat/vessel detection and tracking
    'loading_boat_trailer': {
        label: 'Loading Boat onto Trailer',
        description: 'Boat being loaded onto or unloaded from trailer at ramp',
        category: 'Vessel Activity',
        requiresBoundingBox: true,
        allowEventBoundaries: true,
        eventBoundaryPrompt: 'Mark the start and end of the loading/unloading activity',
        steps: [
            {
                id: 'entire_boat',
                label: 'Entire Boat',
                prompt: 'Draw bounding box around the entire boat',
                optional: false,
                notVisibleOption: false
            },
            {
                id: 'boat_registration',
                label: 'Boat Registration Number',
                prompt: 'Draw bounding box around the boat registration number',
                optional: true,
                notVisibleOption: true
            },
            {
                id: 'license_plate',
                label: 'Tow Vehicle License Plate',
                prompt: 'Draw bounding box around the tow vehicle license plate',
                optional: true,
                notVisibleOption: true
            },
            {
                id: 'boat_operator',
                label: 'Boat Operator',
                prompt: 'Draw bounding box around the boat operator',
                optional: true,
                notVisibleOption: true
            },
            {
                id: 'propwash',
                label: 'Propwash/Wake',
                prompt: 'Draw bounding box around visible propwash or wake',
                optional: true,
                notVisibleOption: true
            },
            {
                id: 'tow_vehicle',
                label: 'Tow Vehicle',
                prompt: 'Draw bounding box around the tow vehicle',
                optional: true,
                notVisibleOption: true
            },
            {
                id: 'boat_trailer',
                label: 'Boat Trailer',
                prompt: 'Draw bounding box around the boat trailer',
                optional: true,
                notVisibleOption: true
            }
        ],
        tags: {
            // Loading direction and ramp angle removed - not needed
        }
    },

    'boat_operating_water': {
        label: 'Boat Operating on Water',
        description: 'Boat in operation on open water',
        category: 'Vessel Activity',
        requiresBoundingBox: true,
        allowEventBoundaries: true,
        eventBoundaryPrompt: 'Mark the start and end of this vessel operation',
        steps: [
            {
                id: 'entire_boat',
                label: 'Entire Boat',
                prompt: 'Draw bounding box around the entire boat',
                optional: false,
                notVisibleOption: false
            },
            {
                id: 'boat_registration',
                label: 'Boat Registration Number',
                prompt: 'Draw bounding box around the boat registration number',
                optional: true,
                notVisibleOption: true
            },
            {
                id: 'boat_operator',
                label: 'Boat Operator',
                prompt: 'Draw bounding box around the boat operator',
                optional: true,
                notVisibleOption: true
            },
            {
                id: 'wake',
                label: 'Boat Wake',
                prompt: 'Draw bounding box around the boat wake',
                optional: true,
                notVisibleOption: true
            }
        ],
        tags: {
            'wake_type': {
                type: 'dropdown',
                label: 'Wake Type (if visible)',
                options: ['normal_wake', 'enhanced_wake', 'minimal_wake', 'no_wake'],
                required: false
            },
            'boat_activity': {
                type: 'dropdown',
                label: 'Boat Activity',
                options: ['cruising', 'towing_watersports', 'fishing', 'anchored', 'drifting', 'other'],
                required: false
            },
            'speed_estimate': {
                type: 'dropdown',
                label: 'Speed Estimate',
                options: ['stationary', 'slow', 'moderate', 'fast', 'unclear'],
                required: false
            }
        }
    },

    // Person identification — bbox a person and feed into person-manager
    'person_identification': {
        label: 'Person Identification',
        description: 'Draw a bounding box around a person to identify them via Person Manager',
        category: 'Person Activity',
        requiresBoundingBox: true,
        allowEventBoundaries: false,
        allowDynamicSteps: true, // Allow identifying multiple people
        steps: [
            {
                id: 'person_full_body',
                label: 'Person',
                prompt: 'Draw bounding box around the person you want to identify',
                optional: false,
                notVisibleOption: false
            }
        ],
        dynamicStepTemplate: {
            idPrefix: 'person_',
            label: 'Additional Person',
            prompt: 'Draw bounding box around another person in the scene',
            optional: true,
            notVisibleOption: false
        },
        tags: {}
    },

    // AI-detected person (from auto-detect)
    'person_detection': {
        label: 'Person Detection',
        description: 'AI-detected person bounding box',
        category: 'Person Activity',
        requiresBoundingBox: true,
        allowEventBoundaries: false,
        allowDynamicSteps: false,
        steps: [
            {
                id: 'person_full_body',
                label: 'Person',
                prompt: 'Bounding box around detected person',
                optional: false,
                notVisibleOption: false
            }
        ],
        tags: {}
    },

    // AI-detected face (from auto-detect)
    'face_detection': {
        label: 'Face Detection',
        description: 'AI-detected face bounding box',
        category: 'Person Activity',
        requiresBoundingBox: true,
        allowEventBoundaries: false,
        allowDynamicSteps: false,
        steps: [
            {
                id: 'face',
                label: 'Face',
                prompt: 'Bounding box around detected face',
                optional: false,
                notVisibleOption: false
            }
        ],
        tags: {}
    },

    // AI-detected license plate (two-stage: detected on vehicle crops)
    'license_plate': {
        label: 'License Plate Detection',
        description: 'AI-detected license plate bounding box on vehicle',
        category: 'Vehicle Activity',
        requiresBoundingBox: true,
        allowEventBoundaries: false,
        allowDynamicSteps: false,
        steps: [
            {
                id: 'license_plate',
                label: 'License Plate',
                prompt: 'Bounding box around detected license plate',
                optional: false,
                notVisibleOption: false
            }
        ],
        tags: {}
    },

    // AI-detected boat registration mark (two-stage: detected on boat crops)
    'boat_registration': {
        label: 'Boat Registration Detection',
        description: 'AI-detected boat registration number bounding box',
        category: 'Vessel Activity',
        requiresBoundingBox: true,
        allowEventBoundaries: false,
        allowDynamicSteps: false,
        steps: [
            {
                id: 'boat_registration',
                label: 'Boat Registration',
                prompt: 'Bounding box around detected boat registration number',
                optional: false,
                notVisibleOption: false
            }
        ],
        tags: {}
    },

    // Vehicle identification
    'vehicle_identification': {
        label: 'Vehicle Identification',
        description: 'Comprehensive vehicle identification with fleet tracking',
        category: 'Vehicle Activity',
        requiresBoundingBox: true,
        allowEventBoundaries: false,
        allowDynamicSteps: false,
        steps: [
            {
                id: 'entire_vehicle',
                label: 'Entire Vehicle',
                prompt: 'Draw bounding box around the entire vehicle (all visible parts)',
                optional: false,
                notVisibleOption: false
            },
            {
                id: 'license_plate_front',
                label: 'License Plate - Front',
                prompt: 'Draw tight box around front license plate',
                optional: true,
                notVisibleOption: true
            },
            {
                id: 'license_plate_rear',
                label: 'License Plate - Rear',
                prompt: 'Draw tight box around rear license plate',
                optional: true,
                notVisibleOption: true
            },
            {
                id: 'fleet_number',
                label: 'Fleet Number/ID',
                prompt: 'Draw box around unit number on door/hood (e.g., "POLICE 123")',
                optional: true,
                notVisibleOption: true
            },
            {
                id: 'fleet_number_2',
                label: 'Fleet Number/ID (2)',
                prompt: 'Draw box around additional unit number (e.g., opposite door, rear)',
                optional: true,
                notVisibleOption: true
            },
            {
                id: 'fleet_number_3',
                label: 'Fleet Number/ID (3)',
                prompt: 'Draw box around additional unit number (e.g., roof, bumper)',
                optional: true,
                notVisibleOption: true
            },
            {
                id: 'fleet_logo',
                label: 'Fleet Logo/Markings',
                prompt: 'Draw box around police badge, company logo, or distinguishing graphics',
                optional: true,
                notVisibleOption: true
            },
            {
                id: 'fleet_logo_2',
                label: 'Fleet Logo/Markings (2)',
                prompt: 'Draw box around additional logo or marking (e.g., opposite side)',
                optional: true,
                notVisibleOption: true
            },
            {
                id: 'fleet_logo_3',
                label: 'Fleet Logo/Markings (3)',
                prompt: 'Draw box around additional logo or marking (e.g., rear, roof)',
                optional: true,
                notVisibleOption: true
            },
            {
                id: 'vehicle_operator',
                label: 'Vehicle Operator',
                prompt: 'Draw bounding box around the vehicle driver/operator (if visible)',
                optional: true,
                notVisibleOption: true
            }
        ],
        tags: {
            'vehicle_type': {
                type: 'dropdown',
                label: 'Vehicle Type',
                options: ['passenger_car', 'pickup_truck', 'suv', 'van_cargo', 'van_passenger', 'semi_truck', 'box_truck', 'garbage_truck', 'dump_truck', 'tow_truck', 'bus_school', 'bus_transit', 'bus_coach', 'emergency_vehicle', 'utility_vehicle', 'motorcycle', 'other'],
                required: true
            },
            'vehicle_make': {
                type: 'text_autocomplete',
                label: 'Make (Brand)',
                placeholder: 'Ford, Chevy, Toyota...',
                required: false,
                showRecentValues: true,
                recentCount: 20
            },
            'vehicle_model': {
                type: 'text_autocomplete',
                label: 'Model',
                placeholder: 'F-150, Silverado, Camry...',
                required: false,
                showRecentValues: true,
                recentCount: 20
            },
            'fleet_type': {
                type: 'configurable_dropdown',
                label: 'Fleet Type',
                defaultOptions: ['none', 'police', 'sheriff', 'fire_department', 'ambulance_ems', 'ups', 'fedex', 'usps', 'dhl', 'amazon_delivery', 'municipal', 'utility_company', 'waste_management', 'construction', 'commercial_delivery', 'taxi_rideshare', 'rental_car', 'other_fleet'],
                required: false,
                allowCustom: true,
                customPrompt: 'Enter new fleet type'
            },
            'fleet_id_number': {
                type: 'text_autocomplete',
                label: 'Fleet ID Number',
                placeholder: 'Unit 123, Car 45, Truck 789...',
                required: false,
                showRecentValues: true,
                recentCount: 10
            },
            'company_agency_name': {
                type: 'text_autocomplete',
                label: 'Company/Agency Name',
                placeholder: 'City Police, ABC Delivery...',
                required: false,
                showRecentValues: true,
                recentCount: 10
            },
            'vehicle_color_primary': {
                type: 'dropdown',
                label: 'Primary Color',
                options: ['white', 'black', 'silver', 'gray', 'red', 'blue', 'green', 'yellow', 'orange', 'brown', 'tan', 'gold', 'purple', 'multicolor'],
                required: false
            },
            'vehicle_color_secondary': {
                type: 'dropdown',
                label: 'Secondary Color (if two-tone)',
                options: ['none', 'white', 'black', 'silver', 'gray', 'red', 'blue', 'green', 'yellow', 'orange', 'brown', 'tan', 'gold', 'purple'],
                required: false
            },
            'distinguishing_features': {
                type: 'checkbox',
                label: 'Distinguishing Features',
                options: ['roof_lights', 'emergency_lights', 'company_graphics', 'vehicle_wrap', 'ladder_rack', 'toolbox', 'light_bar', 'push_bumper', 'roof_cargo', 'tinted_windows', 'damaged', 'modified'],
                required: false
            },
            'plate_state': {
                type: 'text_autocomplete',
                label: 'Plate State/Province',
                placeholder: 'MI, CA, ON...',
                required: false,
                showRecentValues: true,
                recentCount: 10
            },
            'plate_type': {
                type: 'dropdown',
                label: 'Plate Type',
                options: ['standard', 'government', 'municipal', 'commercial', 'disabled', 'veteran', 'custom_vanity', 'temporary', 'dealer', 'not_visible'],
                required: false
            },
            'view_angle': {
                type: 'dropdown',
                label: 'View Angle',
                options: ['front', 'rear', 'front_quarter', 'rear_quarter', 'side_profile', 'overhead', 'unclear'],
                required: false
            },
            'occlusion_level': {
                type: 'dropdown',
                label: 'Occlusion Level',
                options: ['none', 'partial', 'heavy'],
                required: false
            },
            'visibility_quality': {
                type: 'dropdown',
                label: 'Visibility Quality',
                options: ['clear', 'blurry', 'dark', 'bright_overexposed', 'motion_blur', 'weather_obscured'],
                required: false
            },
            'distance_category': {
                type: 'dropdown',
                label: 'Distance Category',
                options: ['close', 'medium', 'far'],
                required: false
            },
            'vehicle_state': {
                type: 'dropdown',
                label: 'Vehicle State',
                options: ['parked', 'moving_slow', 'moving_moderate', 'moving_fast', 'stopped_traffic', 'loading_unloading', 'unclear'],
                required: false
            },
            'lights_active': {
                type: 'checkbox',
                label: 'Lights Active',
                options: ['headlights', 'brake_lights', 'turn_signals', 'emergency_lights', 'hazard_lights', 'reverse_lights', 'fog_lights'],
                required: false
            },
            'linked_person': {
                type: 'name_autocomplete',
                label: 'Driver/Operator (if known)',
                placeholder: 'Link to person name...',
                required: false,
                showRecentNames: true,
                recentCount: 10
            }
        }
    },

    // Trailer identification (separate from vehicle)
    'trailer_identification': {
        label: 'Trailer Identification',
        description: 'Identify trailer independent of towing vehicle',
        category: 'Vehicle Activity',
        requiresBoundingBox: true,
        allowEventBoundaries: false,
        steps: [
            {
                id: 'entire_trailer',
                label: 'Entire Trailer',
                prompt: 'Draw bounding box around the entire trailer',
                optional: false,
                notVisibleOption: false
            },
            {
                id: 'trailer_license_plate',
                label: 'Trailer License Plate',
                prompt: 'Draw tight box around trailer license plate',
                optional: true,
                notVisibleOption: true
            },
            {
                id: 'trailer_markings',
                label: 'Trailer ID/Markings',
                prompt: 'Draw box around trailer identification numbers or markings',
                optional: true,
                notVisibleOption: true
            },
            {
                id: 'towing_vehicle',
                label: 'Towing Vehicle',
                prompt: 'Draw box around vehicle towing this trailer (if visible)',
                optional: true,
                notVisibleOption: true
            }
        ],
        tags: {
            'trailer_type': {
                type: 'dropdown',
                label: 'Trailer Type',
                options: ['boat_trailer', 'utility_trailer', 'cargo_trailer', 'camper_trailer', 'flatbed_trailer', 'equipment_trailer', 'horse_trailer', 'other'],
                required: true
            },
            'trailer_id': {
                type: 'text_autocomplete',
                label: 'Trailer ID Number',
                placeholder: 'Trailer identification...',
                required: false,
                showRecentValues: true,
                recentCount: 10
            },
            'trailer_color': {
                type: 'dropdown',
                label: 'Trailer Color',
                options: ['white', 'black', 'silver', 'gray', 'red', 'blue', 'green', 'yellow', 'orange', 'brown', 'tan', 'multicolor'],
                required: false
            },
            'plate_state': {
                type: 'text_autocomplete',
                label: 'Plate State/Province',
                placeholder: 'MI, CA, ON...',
                required: false,
                showRecentValues: true,
                recentCount: 10
            },
            'linked_vehicle': {
                type: 'text_autocomplete',
                label: 'Towing Vehicle (if known)',
                placeholder: 'Link to vehicle ID...',
                required: false,
                showRecentValues: true,
                recentCount: 10
            }
        }
    },

    // Environmental/Contextual annotations (no bounding boxes)
    'environmental_conditions': {
        label: 'Environmental Conditions',
        description: 'Weather, lighting, and water conditions for entire clip',
        category: 'Environmental',
        requiresBoundingBox: false,
        allowEventBoundaries: false,
        appliesToEntireClip: true,
        tags: {
            'weather': {
                type: 'dropdown',
                label: 'Weather Conditions',
                options: ['clear', 'partly_cloudy', 'overcast', 'rain', 'fog', 'snow'],
                required: false
            },
            'lighting': {
                type: 'dropdown',
                label: 'Lighting',
                options: ['bright_sun', 'sun_glare', 'overcast_diffuse', 'golden_hour', 'dusk_dawn', 'night', 'artificial_lights'],
                required: false
            },
            'water_conditions': {
                type: 'dropdown',
                label: 'Water Conditions',
                options: ['calm_glass', 'small_ripples', 'choppy', 'rough_waves', 'very_rough'],
                required: false
            },
            'visibility': {
                type: 'dropdown',
                label: 'Visibility',
                options: ['excellent', 'good', 'moderate', 'poor', 'very_poor'],
                required: false
            },
            'wind': {
                type: 'dropdown',
                label: 'Wind Conditions',
                options: ['calm', 'light_breeze', 'moderate_wind', 'strong_wind', 'very_strong'],
                required: false
            }
        }
    },

    'camera_quality': {
        label: 'Camera/Recording Quality',
        description: 'Camera angle, stability, and recording quality assessment',
        category: 'Environmental',
        requiresBoundingBox: false,
        allowEventBoundaries: false,
        appliesToEntireClip: true,
        tags: {
            'camera_angle': {
                type: 'dropdown',
                label: 'Camera Angle',
                options: ['overhead', 'elevated_angled', 'eye_level', 'low_angle', 'ground_level', 'variable'],
                required: false
            },
            'camera_stability': {
                type: 'dropdown',
                label: 'Camera Stability',
                options: ['stable_fixed', 'stable_gimbal', 'minor_shake', 'moderate_shake', 'very_shaky'],
                required: false
            },
            'video_quality': {
                type: 'dropdown',
                label: 'Video Quality',
                options: ['excellent_4k', 'good_1080p', 'acceptable_720p', 'low_480p', 'very_low'],
                required: false
            },
            'focus_quality': {
                type: 'dropdown',
                label: 'Focus Quality',
                options: ['sharp', 'mostly_sharp', 'soft', 'out_of_focus', 'variable'],
                required: false
            },
            'compression_artifacts': {
                type: 'dropdown',
                label: 'Compression Artifacts',
                options: ['none', 'minimal', 'noticeable', 'severe'],
                required: false
            }
        }
    },

    'location_context': {
        label: 'Location Context',
        description: 'Location type and surrounding environment',
        category: 'Environmental',
        requiresBoundingBox: false,
        allowEventBoundaries: false,
        appliesToEntireClip: true,
        tags: {
            'location_name': {
                type: 'dropdown',
                label: 'Location Name',
                options: [],
                dynamicOptions: '/api/camera-locations',
                dynamicOptionsMap: function(data) {
                    if (data.locations) {
                        return data.locations.map(function(loc) { return loc.location_name; });
                    }
                    return [];
                },
                allowCustom: true,
                required: true
            },
            'location_type': {
                type: 'dropdown',
                label: 'Location Type',
                options: ['boat_ramp', 'marina', 'open_water', 'lake', 'river', 'ocean', 'canal', 'harbor'],
                required: true
            },
            'shore_visibility': {
                type: 'dropdown',
                label: 'Shore Visibility',
                options: ['shore_visible', 'shore_distant', 'open_water_no_shore', 'not_applicable'],
                required: false
            },
            'traffic_density': {
                type: 'dropdown',
                label: 'Boat Traffic Density',
                options: ['none', 'light', 'moderate', 'heavy', 'very_heavy'],
                required: false
            },
            'infrastructure_visible': {
                type: 'checkbox',
                label: 'Visible Infrastructure',
                options: ['dock', 'pier', 'buoys', 'markers', 'ramp', 'parking_lot', 'buildings'],
                required: false
            }
        }
    },

    // Event-based with optional boundaries
    'compliance_violation': {
        label: 'Compliance Violation',
        description: 'Safety or regulatory violation observed',
        category: 'Compliance',
        requiresBoundingBox: false,
        allowEventBoundaries: true,
        eventBoundaryPrompt: 'Mark when the violation starts and ends (if applicable)',
        tags: {
            'violation_type': {
                type: 'dropdown',
                label: 'Violation Type',
                options: ['excessive_wake', 'no_wake_zone', 'speed_limit', 'restricted_area', 'unsafe_loading', 'missing_safety_equipment', 'other'],
                required: true
            },
            'severity': {
                type: 'dropdown',
                label: 'Severity',
                options: ['minor', 'moderate', 'serious', 'dangerous'],
                required: true
            },
            'violation_details': {
                type: 'textarea',
                label: 'Violation Details',
                placeholder: 'Describe what rule/regulation was violated and how...',
                required: true
            }
        }
    },

    'interesting_event': {
        label: 'Interesting Event',
        description: 'Notable event or behavior worth documenting',
        category: 'Events',
        requiresBoundingBox: false,
        allowEventBoundaries: true,
        eventBoundaryPrompt: 'Mark when the event starts and ends',
        tags: {
            'event_type': {
                type: 'dropdown',
                label: 'Event Type',
                options: ['near_miss', 'unusual_maneuver', 'wildlife', 'rescue', 'accident', 'equipment_failure', 'other'],
                required: true
            },
            'event_description': {
                type: 'textarea',
                label: 'Event Description',
                placeholder: 'Describe what happened...',
                required: true
            }
        }
    },

    'audio_event': {
        label: 'Audio Event',
        description: 'Audio-based event detection (siren, gunshot, engine sounds, etc.)',
        category: 'Events',
        requiresBoundingBox: false,
        allowEventBoundaries: true,
        eventBoundaryPrompt: 'Mark when the audio event starts (end time optional for short sounds)',
        allowOptionalEndTime: true,
        tags: {
            'audio_type': {
                type: 'dropdown',
                label: 'Audio Event Type',
                options: ['siren', 'gunshot', 'vehicle_backfire', 'crash', 'boat_motor_idling', 'boat_motor_revving', 'horn', 'whistle', 'alarm', 'other'],
                required: true
            },
            'audio_intensity': {
                type: 'dropdown',
                label: 'Sound Intensity',
                options: ['faint', 'moderate', 'loud', 'very_loud'],
                required: false
            },
            'audio_description': {
                type: 'textarea',
                label: 'Additional Details',
                placeholder: 'Describe the audio event, direction, pattern, etc...',
                required: false
            }
        }
    },

    // Movement tracking (guided keyframe interpolation)
    'movement_tracking': {
        label: 'Movement Tracking',
        description: 'Track an object across keyframes to generate interpolated detections',
        category: 'Tracking',
        requiresBoundingBox: true,
        allowEventBoundaries: false,
        allowDynamicSteps: false,
        isMovementTracking: true,
        steps: [
            {
                id: 'tracked_object',
                label: 'Tracked Object',
                prompt: 'Draw bounding box around the object you want to track',
                optional: false,
                notVisibleOption: false
            }
        ],
        tags: {
            'track_number': {
                type: 'dropdown',
                label: 'Track',
                options: [],
                required: true,
                dynamicOptions: 'movement_tracks',
                placeholder: 'Select or create track...'
            },
            'track_label': {
                type: 'text_autocomplete',
                label: 'Track Label (optional)',
                placeholder: 'e.g., red truck, blue SUV...',
                required: false,
                showRecentValues: true,
                recentCount: 10
            },
            'class': {
                type: 'configurable_dropdown',
                label: 'Object Class',
                defaultOptions: ['sedan', 'pickup truck', 'SUV', 'minivan', 'van', 'tractor', 'ATV', 'UTV', 'motorcycle', 'trailer', 'bus', 'semi truck', 'dump truck', 'rowboat', 'fishing boat', 'speed boat', 'pontoon boat', 'kayak', 'canoe', 'sailboat', 'jet ski', 'person', 'other'],
                required: true,
                allowCustom: true,
                customPrompt: 'Enter object class'
            }
        }
    },

    // Generic fallback
    'other': {
        label: 'Other Annotation',
        description: 'Custom annotation for scenarios not covered above',
        category: 'Other',
        requiresBoundingBox: false,
        allowEventBoundaries: true,
        eventBoundaryPrompt: 'Mark event boundaries if applicable',
        allowBoundingBoxOptIn: true, // User can choose to add bounding boxes
        steps: [
            {
                id: 'primary_subject',
                label: 'Primary Subject',
                prompt: 'Draw bounding box around the subject (if applicable)',
                optional: true,
                notVisibleOption: false
            }
        ],
        tags: {
            'annotation_description': {
                type: 'textarea',
                label: 'Description',
                placeholder: 'Describe what you are annotating...',
                required: true
            }
        }
    }
};

// Organize scenarios by category for better UI
const scenarioCategories = {
    'Vessel Activity': ['loading_boat_trailer', 'boat_operating_water', 'boat_registration'],
    'Vehicle Activity': ['vehicle_identification', 'trailer_identification', 'license_plate'],
    'Person Activity': ['person_identification'],
    'Environmental': ['environmental_conditions', 'camera_quality', 'location_context'],
    'Compliance': ['compliance_violation'],
    'Events': ['interesting_event', 'audio_event'],
    'Tracking': ['movement_tracking'],
    'Other': ['other']
};

// Scenario usage tracking for "most recent first" ordering
class ScenarioTracker {
    constructor() {
        this.usageKey = 'groundtruth_scenario_usage';
        this.usageData = this.loadUsageData();
    }

    loadUsageData() {
        const data = localStorage.getItem(this.usageKey);
        return data ? JSON.parse(data) : {};
    }

    saveUsageData() {
        localStorage.setItem(this.usageKey, JSON.stringify(this.usageData));
    }

    trackUsage(scenarioId) {
        this.usageData[scenarioId] = Date.now();
        this.saveUsageData();
    }

    getSortedScenarios() {
        const usage = this.usageData;

        // Convert to array and sort by usage timestamp (most recent first)
        return Object.keys(annotationScenarios).sort((a, b) => {
            const timeA = usage[a] || 0;
            const timeB = usage[b] || 0;
            return timeB - timeA; // Descending order
        });
    }

    getSortedByCategory() {
        const usage = this.usageData;
        const categorized = {};

        for (const [category, scenarioIds] of Object.entries(scenarioCategories)) {
            categorized[category] = scenarioIds.sort((a, b) => {
                const timeA = usage[a] || 0;
                const timeB = usage[b] || 0;
                return timeB - timeA;
            });
        }

        return categorized;
    }
}

const scenarioTracker = new ScenarioTracker();
