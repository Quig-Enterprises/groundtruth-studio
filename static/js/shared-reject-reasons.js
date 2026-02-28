/**
 * Centralized rejection/removal reasons for all review pages.
 * Usage: RejectReasons.get('training'), RejectReasons.buildChips('clip'), etc.
 */
(function() {
    'use strict';

    var REASONS = [
        { value: 'false_positive',      label: 'False Positive',         contexts: ['training', 'prediction', 'clip', 'cross_camera'] },
        { value: 'bad_image_quality',    label: 'Bad Image Quality',      contexts: ['training', 'prediction', 'clip', 'cross_camera'] },
        { value: 'motion_blur',          label: 'Motion Blur',            contexts: ['training', 'prediction', 'clip', 'cross_camera'] },
        { value: 'bbox_too_large',       label: 'BBox Too Large',         contexts: ['training', 'prediction', 'clip'] },
        { value: 'bbox_too_small',       label: 'BBox Too Small',         contexts: ['training', 'prediction', 'clip'] },
        { value: 'bbox_misaligned',      label: 'BBox Misaligned',        contexts: ['prediction', 'clip'] },
        { value: 'poor_localization',    label: 'Poor Localization',      contexts: ['prediction'] },
        { value: 'wrong_class',          label: 'Wrong Classification',   contexts: ['prediction', 'clip'] },
        { value: 'duplicate',            label: 'Duplicate',              contexts: ['training', 'prediction'] },
        { value: 'not_representative',   label: 'Not Representative',     contexts: ['training'] },
        { value: 'occluded',             label: 'Occluded / Obstructed',  contexts: ['training', 'prediction', 'clip'] },
        { value: 'wrong_location',       label: 'Wrong Location',         contexts: ['prediction'] },
        { value: 'track_split',          label: 'Track Should Be Merged', contexts: ['clip'] },
        { value: 'not_same_vehicle',     label: 'Not Same Vehicle',       contexts: ['cross_camera'] },
        { value: 'not_same_person',      label: 'Not Same Person',        contexts: ['cross_camera'] },
        { value: 'not_same_object',      label: 'Not Same Object',        contexts: ['cross_camera'] },
        { value: 'wrong_time_window',    label: 'Wrong Time Window',      contexts: ['cross_camera'] },
        { value: 'other',                label: 'Other',                  contexts: ['training', 'prediction', 'clip', 'cross_camera'] }
    ];

    window.RejectReasons = {
        /**
         * Get filtered reasons for a context.
         * @param {string} context - 'training'|'prediction'|'clip'|'cross_camera'
         * @returns {Array<{value: string, label: string}>}
         */
        get: function(context) {
            return REASONS
                .filter(function(r) { return r.contexts.indexOf(context) !== -1; })
                .map(function(r) { return { value: r.value, label: r.label }; });
        },

        /**
         * Build reason chip buttons as a DocumentFragment.
         * @param {string} context
         * @returns {DocumentFragment}
         */
        buildChips: function(context) {
            var frag = document.createDocumentFragment();
            var reasons = this.get(context);
            for (var i = 0; i < reasons.length; i++) {
                var btn = document.createElement('button');
                btn.className = 'reason-chip';
                btn.setAttribute('data-reason', reasons[i].value);
                btn.textContent = reasons[i].label;
                frag.appendChild(btn);
            }
            return frag;
        },

        /**
         * Look up the display label for a reason value.
         * @param {string} value - e.g. 'false_positive'
         * @returns {string|null}
         */
        labelFor: function(value) {
            for (var i = 0; i < REASONS.length; i++) {
                if (REASONS[i].value === value) return REASONS[i].label;
            }
            return null;
        },

        /**
         * Build <option> elements as a DocumentFragment for dropdowns.
         * @param {string} context
         * @param {string} [emptyLabel='-- Skip (no reason) --']
         * @returns {DocumentFragment}
         */
        buildOptions: function(context, emptyLabel) {
            var frag = document.createDocumentFragment();
            var empty = document.createElement('option');
            empty.value = '';
            empty.textContent = emptyLabel || '\u2014 Skip (no reason) \u2014';
            frag.appendChild(empty);

            var reasons = this.get(context);
            for (var i = 0; i < reasons.length; i++) {
                var opt = document.createElement('option');
                opt.value = reasons[i].value;
                opt.textContent = reasons[i].label;
                frag.appendChild(opt);
            }
            return frag;
        }
    };
})();
