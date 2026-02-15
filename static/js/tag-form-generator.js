/**
 * Dynamic Tag Form Generator
 * Generates annotation forms based on tag schema from database
 */

class TagFormGenerator {
    constructor() {
        this.currentGroundTruth = null;
        this.currentIsNegative = false;
        this.currentAnnotationType = 'time_range';
        this.tagValues = {};
    }

    /**
     * Fetch tag schema from API
     */
    async fetchSchema(annotationType, groundTruth = null, isNegative = false) {
        const params = new URLSearchParams({
            annotation_type: annotationType
        });

        if (groundTruth) {
            params.append('ground_truth', groundTruth);
        }
        if (isNegative !== null) {
            params.append('is_negative', isNegative.toString());
        }

        const response = await fetch(`/api/tag-schema?${params}`);
        const data = await response.json();
        return data.groups;
    }

    /**
     * Generate form HTML for tag groups
     */
    async generateForm(annotationType, containerId, groundTruth = null, isNegative = false) {
        this.currentAnnotationType = annotationType;
        this.currentGroundTruth = groundTruth;
        this.currentIsNegative = isNegative;

        const container = document.getElementById(containerId);
        if (!container) {
            console.error(`Container ${containerId} not found`);
            return;
        }

        const groups = await this.fetchSchema(annotationType, groundTruth, isNegative);

        // Clear existing content
        container.innerHTML = '';

        // Group tags by section for better organization
        const sections = this.organizeIntoSections(groups);

        // Generate HTML for each section
        for (const [sectionName, sectionGroups] of Object.entries(sections)) {
            const sectionEl = this.createSection(sectionName, sectionGroups);
            container.appendChild(sectionEl);
        }
    }

    /**
     * Organize tag groups into logical sections
     */
    organizeIntoSections(groups) {
        const sections = {
            'Required Fields': [],
            'Environmental Factors': [],
            'Technical/Quality Issues': [],
            'Behavioral Context': [],
            'Distinguishing Features': [],
            'Object Attributes': [],
            'Training Metadata': [],
            'Review & Notes': []
        };

        for (const group of groups) {
            if (group.is_required) {
                sections['Required Fields'].push(group);
            } else if (['lighting_conditions', 'weather_conditions', 'water_conditions'].includes(group.group_name)) {
                sections['Environmental Factors'].push(group);
            } else if (['camera_issues', 'visibility_issues'].includes(group.group_name)) {
                sections['Technical/Quality Issues'].push(group);
            } else if (['violation_context', 'motor_state', 'boat_motion', 'extenuating_circumstances'].includes(group.group_name)) {
                sections['Behavioral Context'].push(group);
            } else if (['present_indicators', 'absent_indicators', 'false_positive_power_loading', 'false_positive_license_plate'].includes(group.group_name)) {
                sections['Distinguishing Features'].push(group);
            } else if (['boat_type', 'boat_size', 'propeller_visible', 'registration_visible',
                        'vehicle_type', 'plate_state', 'commercial_vehicle',
                        'face_angle', 'face_obstruction', 'number_of_people'].includes(group.group_name)) {
                sections['Object Attributes'].push(group);
            } else if (['training_priority', 'dataset_usage'].includes(group.group_name)) {
                sections['Training Metadata'].push(group);
            } else if (['reviewer_notes', 'flags'].includes(group.group_name)) {
                sections['Review & Notes'].push(group);
            }
        }

        // Remove empty sections
        for (const [sectionName, sectionGroups] of Object.entries(sections)) {
            if (sectionGroups.length === 0) {
                delete sections[sectionName];
            }
        }

        return sections;
    }

    /**
     * Create a collapsible section element
     */
    createSection(sectionName, groups) {
        const section = document.createElement('div');
        section.className = 'tag-form-section';

        // Section header
        const header = document.createElement('div');
        header.className = 'tag-form-section-header';
        header.textContent = sectionName;

        // Required fields are always expanded, others start collapsed
        const isRequired = sectionName === 'Required Fields';
        const isExpanded = isRequired;

        header.onclick = () => {
            content.classList.toggle('collapsed');
            header.classList.toggle('collapsed');
        };

        if (!isExpanded) {
            header.classList.add('collapsed');
        }

        section.appendChild(header);

        // Section content
        const content = document.createElement('div');
        content.className = 'tag-form-section-content';
        if (!isExpanded) {
            content.classList.add('collapsed');
        }

        for (const group of groups) {
            const groupEl = this.createFormGroup(group);
            content.appendChild(groupEl);
        }

        section.appendChild(content);
        return section;
    }

    /**
     * Create a form group (label + control)
     */
    createFormGroup(group) {
        const formGroup = document.createElement('div');
        formGroup.className = 'form-group';
        formGroup.id = `group-${group.group_name}`;

        // Label
        const label = document.createElement('label');
        label.textContent = group.display_name;
        if (group.is_required) {
            label.innerHTML += ' <span class="required">*</span>';
        }
        formGroup.appendChild(label);

        // Description (help text)
        if (group.description) {
            const helpText = document.createElement('div');
            helpText.className = 'help-text-small';
            helpText.textContent = group.description;
            formGroup.appendChild(helpText);
        }

        // Form control based on group type
        let control;
        switch (group.group_type) {
            case 'dropdown':
                control = this.createDropdown(group);
                break;
            case 'checkbox':
                control = this.createCheckboxGroup(group);
                break;
            case 'text':
                control = this.createTextInput(group);
                break;
            case 'textarea':
                control = this.createTextarea(group);
                break;
            default:
                console.error(`Unknown group type: ${group.group_type}`);
                return formGroup;
        }

        formGroup.appendChild(control);
        return formGroup;
    }

    /**
     * Create a dropdown (select) element
     */
    createDropdown(group) {
        const select = document.createElement('select');
        select.id = `tag-${group.group_name}`;
        select.name = group.group_name;
        select.className = 'tag-form-dropdown';

        // Add empty option for optional fields
        if (!group.is_required) {
            const emptyOption = document.createElement('option');
            emptyOption.value = '';
            emptyOption.textContent = '-- Select --';
            select.appendChild(emptyOption);
        }

        // Add options
        for (const option of group.options) {
            const optionEl = document.createElement('option');
            optionEl.value = option.option_value;
            optionEl.textContent = option.display_text;
            if (option.description) {
                optionEl.title = option.description;
            }
            select.appendChild(optionEl);
        }

        // Handle ground truth changes to trigger schema refresh
        if (group.group_name === 'ground_truth') {
            select.addEventListener('change', (e) => {
                this.handleGroundTruthChange(e.target.value);
            });
        }

        return select;
    }

    /**
     * Create checkbox group for multi-select
     */
    createCheckboxGroup(group) {
        const container = document.createElement('div');
        container.className = 'checkbox-group';

        for (const option of group.options) {
            const checkboxWrapper = document.createElement('div');
            checkboxWrapper.className = 'checkbox-item';

            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.id = `tag-${group.group_name}-${option.option_value}`;
            checkbox.name = group.group_name;
            checkbox.value = option.option_value;
            checkbox.className = 'tag-form-checkbox';

            const label = document.createElement('label');
            label.htmlFor = checkbox.id;
            label.textContent = option.display_text;
            if (option.description) {
                label.title = option.description;
            }

            checkboxWrapper.appendChild(checkbox);
            checkboxWrapper.appendChild(label);
            container.appendChild(checkboxWrapper);
        }

        return container;
    }

    /**
     * Create text input
     */
    createTextInput(group) {
        const input = document.createElement('input');
        input.type = 'text';
        input.id = `tag-${group.group_name}`;
        input.name = group.group_name;
        input.className = 'tag-form-text';
        input.placeholder = group.description || '';
        return input;
    }

    /**
     * Create textarea
     */
    createTextarea(group) {
        const textarea = document.createElement('textarea');
        textarea.id = `tag-${group.group_name}`;
        textarea.name = group.group_name;
        textarea.className = 'tag-form-textarea';
        textarea.rows = 4;
        textarea.placeholder = group.description || 'Enter notes...';
        return textarea;
    }

    /**
     * Handle ground truth selection change
     * Regenerates form with conditional fields
     */
    async handleGroundTruthChange(groundTruth) {
        this.currentGroundTruth = groundTruth;

        // Preserve current form values before regenerating
        const currentValues = this.collectFormValues();

        // Regenerate form with new conditional fields
        const containerId = this.currentAnnotationType === 'time_range'
            ? 'time-range-tag-form'
            : 'keyframe-tag-form';

        await this.generateForm(
            this.currentAnnotationType,
            containerId,
            groundTruth,
            this.currentIsNegative
        );

        // Restore values
        this.restoreFormValues(currentValues);
    }

    /**
     * Collect all current form values
     */
    collectFormValues() {
        const values = {};

        // Dropdowns and text inputs
        const inputs = document.querySelectorAll('.tag-form-dropdown, .tag-form-text, .tag-form-textarea');
        inputs.forEach(input => {
            if (input.value) {
                values[input.name] = input.value;
            }
        });

        // Checkboxes
        const checkboxGroups = {};
        const checkboxes = document.querySelectorAll('.tag-form-checkbox:checked');
        checkboxes.forEach(checkbox => {
            if (!checkboxGroups[checkbox.name]) {
                checkboxGroups[checkbox.name] = [];
            }
            checkboxGroups[checkbox.name].push(checkbox.value);
        });

        Object.assign(values, checkboxGroups);
        return values;
    }

    /**
     * Restore form values after regeneration
     */
    restoreFormValues(values) {
        for (const [name, value] of Object.entries(values)) {
            if (Array.isArray(value)) {
                // Checkboxes
                value.forEach(val => {
                    const checkbox = document.getElementById(`tag-${name}-${val}`);
                    if (checkbox) {
                        checkbox.checked = true;
                    }
                });
            } else {
                // Dropdowns and text inputs
                const input = document.getElementById(`tag-${name}`);
                if (input) {
                    input.value = value;
                }
            }
        }
    }

    /**
     * Get all tag values from the current form
     */
    getFormValues() {
        return this.collectFormValues();
    }

    /**
     * Validate required fields
     */
    validateForm() {
        const requiredGroups = document.querySelectorAll('.form-group .required');
        const missing = [];

        requiredGroups.forEach(span => {
            const formGroup = span.closest('.form-group');
            const select = formGroup.querySelector('select');
            if (select && !select.value) {
                const label = formGroup.querySelector('label').textContent.replace(' *', '');
                missing.push(label);
            }
        });

        return {
            valid: missing.length === 0,
            missingFields: missing
        };
    }

    /**
     * Load dynamic options from API endpoint
     */
    async loadDynamicOptions(groupName, selectElement, apiUrl, mapFn) {
        try {
            const response = await fetch(apiUrl);
            const data = await response.json();

            let options = [];
            if (mapFn) {
                options = mapFn(data);
            } else if (data.locations) {
                options = data.locations.map(loc => loc.location_name);
            }

            // Add fetched options to the select
            options.forEach(opt => {
                const optionEl = document.createElement('option');
                optionEl.value = opt;
                optionEl.textContent = opt;
                selectElement.appendChild(optionEl);
            });
        } catch (error) {
            console.error(`Failed to load dynamic options for ${groupName}:`, error);
        }
    }
}

// Create global instance
const tagFormGenerator = new TagFormGenerator();
