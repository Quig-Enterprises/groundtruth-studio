// Vehicle Metrics Page JavaScript

const vehicleMetrics = {
    data: null,
    sortColumn: 'correctionRate',
    sortDirection: 'desc',

    init() {
        this.loadMetrics();
    },

    async loadMetrics() {
        try {
            const response = await fetch('/api/ai/vehicle-metrics');
            const result = await response.json();

            if (!result.success) {
                this.showError(result.error || 'Failed to load metrics');
                return;
            }

            this.data = result;
            this.render();
        } catch (error) {
            console.error('Error loading metrics:', error);
            this.showError('Failed to load vehicle metrics. Please try again.');
        }
    },

    showError(message) {
        const errorContainer = document.getElementById('error-container');
        errorContainer.textContent = '';

        const errorDiv = document.createElement('div');
        errorDiv.className = 'error-state';

        const strong = document.createElement('strong');
        strong.textContent = 'Error: ';
        errorDiv.appendChild(strong);
        errorDiv.appendChild(document.createTextNode(message));

        errorContainer.appendChild(errorDiv);
    },

    // Current per-class thresholds from vehicle_detect_runner.py
    CURRENT_THRESHOLDS: {
        'sedan': 0.15, 'pickup truck': 0.15, 'SUV': 0.15, 'minivan': 0.15, 'van': 0.15,
        'tractor': 0.12, 'ATV': 0.10, 'UTV': 0.10, 'snowmobile': 0.10, 'golf cart': 0.10, 'skid loader': 0.12,
        'motorcycle': 0.12, 'trailer': 0.12,
        'bus': 0.20, 'semi truck': 0.20, 'dump truck': 0.18,
        'rowboat': 0.12, 'fishing boat': 0.12, 'speed boat': 0.12, 'pontoon boat': 0.12,
        'kayak': 0.10, 'canoe': 0.10, 'sailboat': 0.10, 'jet ski': 0.10
    },

    render() {
        this.renderSummary();
        this.renderClassStats();
        this.renderConfusionMatrix();
        this.renderReadiness();
        this.renderWeeklyTrends();
        this.renderRecommendations();
    },

    renderSummary() {
        const summary = this.data.summary;

        document.getElementById('total-detections').textContent = this.formatNumber(summary.total);
        document.getElementById('approved-count').textContent = this.formatNumber(summary.approved);
        document.getElementById('pending-count').textContent = this.formatNumber(summary.pending);
        document.getElementById('corrected-count').textContent = this.formatNumber(summary.corrected);

        document.getElementById('class-count-text').textContent =
            `${summary.class_count} vehicle ${summary.class_count === 1 ? 'class' : 'classes'}`;

        const autoApprovedPct = summary.total > 0
            ? ((summary.auto_approved / summary.total) * 100).toFixed(1)
            : '0.0';
        document.getElementById('auto-approved-text').textContent =
            `${this.formatNumber(summary.auto_approved)} auto-approved (${autoApprovedPct}%)`;

        document.getElementById('reviewed-today-text').textContent =
            `${this.formatNumber(summary.reviewed_today)} reviewed today`;
    },

    renderClassStats() {
        const container = document.getElementById('class-stats-container');
        const classStats = this.data.class_stats;

        if (!classStats || Object.keys(classStats).length === 0) {
            this.renderEmptyState(container, 'No class statistics available');
            return;
        }

        // Convert to array and sort
        const statsArray = Object.entries(classStats).map(([className, stats]) => {
            const total = stats.total || 0;
            const reviewed = (stats.approved || 0) + (stats.rejected || 0);
            const corrected = stats.corrected || 0;
            const accuracy = reviewed > 0 ? (Math.max(0, reviewed - corrected) / reviewed) * 100 : 0;
            const correctionRate = reviewed > 0 ? (corrected / reviewed) * 100 : 0;

            return {
                className,
                total,
                approved: stats.approved || 0,
                rejected: stats.rejected || 0,
                corrected: stats.corrected || 0,
                pending: stats.pending || 0,
                autoApproved: stats.auto_approved || 0,
                accuracy,
                correctionRate
            };
        });

        this.sortStats(statsArray);

        const table = document.createElement('table');
        table.className = 'stats-table';

        // Create thead
        const thead = document.createElement('thead');
        const headerRow = document.createElement('tr');

        const headers = [
            { text: 'Class', key: 'className' },
            { text: 'Total', key: 'total' },
            { text: 'Approved', key: 'approved' },
            { text: 'Rejected', key: 'rejected' },
            { text: 'Corrected', key: 'corrected' },
            { text: 'Pending', key: 'pending' },
            { text: 'Accuracy %', key: 'accuracy' },
            { text: 'Correction Rate %', key: 'correctionRate' }
        ];

        headers.forEach(header => {
            const th = document.createElement('th');
            th.className = 'sortable';
            if (this.sortColumn === header.key) {
                th.classList.add('sort-' + this.sortDirection);
            }
            th.textContent = header.text;
            th.onclick = () => this.sortBy(header.key);
            headerRow.appendChild(th);
        });

        thead.appendChild(headerRow);
        table.appendChild(thead);

        // Create tbody
        const tbody = document.createElement('tbody');
        statsArray.forEach(stat => {
            const row = document.createElement('tr');

            // Class name (clickable link to detail page)
            const classCell = document.createElement('td');
            classCell.className = 'class-name';
            const classLink = document.createElement('a');
            classLink.textContent = stat.className;
            classLink.href = '/vehicle-metrics/' + encodeURIComponent(stat.className);
            classLink.style.color = 'inherit';
            classLink.style.textDecoration = 'underline';
            classLink.style.cursor = 'pointer';
            classCell.appendChild(classLink);
            row.appendChild(classCell);

            // Total
            const totalCell = document.createElement('td');
            totalCell.textContent = this.formatNumber(stat.total);
            row.appendChild(totalCell);

            // Approved
            const approvedCell = document.createElement('td');
            approvedCell.textContent = this.formatNumber(stat.approved);
            row.appendChild(approvedCell);

            // Rejected
            const rejectedCell = document.createElement('td');
            rejectedCell.textContent = this.formatNumber(stat.rejected);
            row.appendChild(rejectedCell);

            // Corrected
            const correctedCell = document.createElement('td');
            correctedCell.textContent = this.formatNumber(stat.corrected);
            row.appendChild(correctedCell);

            // Pending
            const pendingCell = document.createElement('td');
            pendingCell.textContent = this.formatNumber(stat.pending);
            row.appendChild(pendingCell);

            // Accuracy
            const accuracyCell = document.createElement('td');
            accuracyCell.className = this.getAccuracyClass(stat.accuracy);
            accuracyCell.textContent = stat.accuracy > 0 ? stat.accuracy.toFixed(1) + '%' : 'N/A';
            row.appendChild(accuracyCell);

            // Correction Rate
            const correctionCell = document.createElement('td');
            correctionCell.textContent = stat.correctionRate > 0 ? stat.correctionRate.toFixed(1) + '%' : '0.0%';
            row.appendChild(correctionCell);

            tbody.appendChild(row);
        });

        table.appendChild(tbody);
        container.textContent = '';
        container.appendChild(table);
    },

    renderConfusionMatrix() {
        const container = document.getElementById('confusion-matrix-container');
        const confusionMatrix = this.data.confusion_matrix;

        if (!confusionMatrix || confusionMatrix.length === 0) {
            this.renderEmptyState(container, 'No corrections recorded yet',
                'Confusion matrix will appear when users reclassify detections');
            return;
        }

        const table = document.createElement('table');
        table.className = 'confusion-table';

        // Create thead
        const thead = document.createElement('thead');
        const headerRow = document.createElement('tr');
        ['Original Class', 'Corrected To', 'Count'].forEach(headerText => {
            const th = document.createElement('th');
            th.textContent = headerText;
            headerRow.appendChild(th);
        });
        thead.appendChild(headerRow);
        table.appendChild(thead);

        // Create tbody
        const tbody = document.createElement('tbody');
        confusionMatrix.forEach(entry => {
            const row = document.createElement('tr');

            const originalCell = document.createElement('td');
            originalCell.className = 'class-name';
            const origLink = document.createElement('a');
            origLink.textContent = entry.original_class;
            origLink.href = '/vehicle-metrics/' + encodeURIComponent(entry.original_class);
            origLink.style.color = 'inherit';
            origLink.style.textDecoration = 'underline';
            origLink.style.cursor = 'pointer';
            originalCell.appendChild(origLink);
            row.appendChild(originalCell);

            const correctedCell = document.createElement('td');
            correctedCell.className = 'class-name';
            const corrLink = document.createElement('a');
            corrLink.textContent = entry.corrected_class;
            corrLink.href = '/vehicle-metrics/' + encodeURIComponent(entry.corrected_class);
            corrLink.style.color = 'inherit';
            corrLink.style.textDecoration = 'underline';
            corrLink.style.cursor = 'pointer';
            correctedCell.appendChild(corrLink);
            row.appendChild(correctedCell);

            const countCell = document.createElement('td');
            countCell.textContent = this.formatNumber(entry.count);
            row.appendChild(countCell);

            tbody.appendChild(row);
        });

        table.appendChild(tbody);
        container.textContent = '';
        container.appendChild(table);
    },

    renderReadiness() {
        const container = document.getElementById('readiness-container');
        const readiness = this.data.readiness;

        if (!readiness || Object.keys(readiness).length === 0) {
            this.renderEmptyState(container, 'No readiness data available');
            return;
        }

        // Sort by progress percentage descending
        const readinessArray = Object.entries(readiness)
            .map(([className, data]) => ({ className, ...data }))
            .sort((a, b) => b.progress_pct - a.progress_pct);

        container.textContent = '';

        readinessArray.forEach(item => {
            const progressPct = Math.min(item.progress_pct, 100);
            const isReady = item.ready || item.progress_pct >= 100;

            const itemDiv = document.createElement('div');
            itemDiv.className = 'readiness-item';

            // Header
            const headerDiv = document.createElement('div');
            headerDiv.className = 'readiness-header';

            const classSpan = document.createElement('span');
            classSpan.className = 'readiness-class';
            classSpan.textContent = item.className;
            headerDiv.appendChild(classSpan);

            const countSpan = document.createElement('span');
            countSpan.className = 'readiness-count';
            countSpan.textContent =
                `${this.formatNumber(item.reviewed_count)} / ${this.formatNumber(item.target)} ${isReady ? '✓' : ''}`;
            headerDiv.appendChild(countSpan);

            itemDiv.appendChild(headerDiv);

            // Progress bar
            const progressContainer = document.createElement('div');
            progressContainer.className = 'progress-bar-container';

            const progressBar = document.createElement('div');
            progressBar.className = 'progress-bar' + (isReady ? ' ready' : '');
            progressBar.style.width = progressPct + '%';
            progressContainer.appendChild(progressBar);

            const progressLabel = document.createElement('div');
            progressLabel.className = 'progress-bar-label';
            progressLabel.textContent = progressPct.toFixed(1) + '%';
            progressContainer.appendChild(progressLabel);

            itemDiv.appendChild(progressContainer);
            container.appendChild(itemDiv);
        });
    },

    renderWeeklyTrends() {
        const container = document.getElementById('weekly-trends-container');
        const trends = this.data.weekly_trends;

        if (!trends || trends.length === 0) {
            this.renderEmptyState(container, 'No weekly trend data available',
                'Trends will appear as detections are reviewed over time');
            return;
        }

        // Find max value for scaling
        const maxDetections = Math.max(...trends.map(t => t.detections || 0));

        const chartContainer = document.createElement('div');
        chartContainer.className = 'chart-container';

        const chartBars = document.createElement('div');
        chartBars.className = 'chart-bars';

        trends.forEach(week => {
            const height = maxDetections > 0
                ? ((week.detections / maxDetections) * 100).toFixed(1)
                : 0;
            const weekLabel = this.formatWeekLabel(week.week);

            const wrapper = document.createElement('div');
            wrapper.className = 'chart-bar-wrapper';

            const bar = document.createElement('div');
            bar.className = 'chart-bar';
            bar.style.height = height + '%';

            const value = document.createElement('div');
            value.className = 'chart-bar-value';
            value.textContent = this.formatNumber(week.detections);
            bar.appendChild(value);

            wrapper.appendChild(bar);

            const label = document.createElement('div');
            label.className = 'chart-bar-label';
            label.textContent = weekLabel;
            wrapper.appendChild(label);

            chartBars.appendChild(wrapper);
        });

        chartContainer.appendChild(chartBars);
        container.textContent = '';
        container.appendChild(chartContainer);
    },

    renderEmptyState(container, message, submessage) {
        container.textContent = '';

        const emptyDiv = document.createElement('div');
        emptyDiv.className = 'empty-state';

        const p = document.createElement('p');
        p.textContent = message;
        emptyDiv.appendChild(p);

        if (submessage) {
            const small = document.createElement('small');
            small.textContent = submessage;
            emptyDiv.appendChild(small);
        }

        container.appendChild(emptyDiv);
    },

    sortBy(column) {
        if (this.sortColumn === column) {
            this.sortDirection = this.sortDirection === 'asc' ? 'desc' : 'asc';
        } else {
            this.sortColumn = column;
            this.sortDirection = 'desc';
        }

        this.renderClassStats();
    },

    sortStats(statsArray) {
        const column = this.sortColumn;
        const direction = this.sortDirection === 'asc' ? 1 : -1;

        statsArray.sort((a, b) => {
            let aVal = a[column];
            let bVal = b[column];

            // Handle string sorting for className
            if (column === 'className') {
                return direction * aVal.localeCompare(bVal);
            }

            // Numeric sorting
            if (aVal === bVal) return 0;
            return direction * (aVal > bVal ? 1 : -1);
        });
    },

    getAccuracyClass(accuracy) {
        if (accuracy >= 90) return 'accuracy-high';
        if (accuracy >= 70) return 'accuracy-medium';
        if (accuracy > 0) return 'accuracy-low';
        return '';
    },

    formatNumber(num) {
        if (num === null || num === undefined) return '0';
        return num.toLocaleString();
    },

    renderRecommendations() {
        const container = document.getElementById('recommendations-container');
        if (!container) return;

        const classStats = this.data.class_stats || {};
        const confusionMatrix = this.data.confusion_matrix || [];
        const recommendations = [];

        // Analyze each class
        for (const [className, stats] of Object.entries(classStats)) {
            const total = stats.total || 0;
            const approved = (stats.approved || 0) + (stats.auto_approved || 0);
            const rejected = stats.rejected || 0;
            const corrected = stats.corrected || 0;
            const reviewed = approved + rejected + corrected;
            if (reviewed < 3) continue; // Not enough data

            const rejectRate = rejected / reviewed;
            const approveRate = approved / reviewed;
            const currentThresh = this.CURRENT_THRESHOLDS[className] || 0.15;

            // High rejection rate → raise threshold (too many false positives)
            if (rejectRate > 0.40 && reviewed >= 5) {
                const suggested = Math.min(0.35, currentThresh + 0.05);
                recommendations.push({
                    type: 'rec-raise-threshold',
                    title: 'Raise threshold for "' + className + '"',
                    detail: Math.round(rejectRate * 100) + '% rejection rate (' + rejected + '/' + reviewed +
                        ' rejected). Current threshold: ' + currentThresh.toFixed(2) +
                        '. Consider raising to ' + suggested.toFixed(2) + ' to reduce false positives.'
                });
            }

            // Very high approval rate with decent volume → could auto-approve more
            if (approveRate > 0.95 && reviewed >= 10 && currentThresh < 0.20) {
                recommendations.push({
                    type: 'rec-good',
                    title: '"' + className + '" performing well',
                    detail: Math.round(approveRate * 100) + '% approval rate (' + approved + '/' + reviewed +
                        '). Consider enabling auto-approve for high-confidence detections (>= 0.60).'
                });
            }
        }

        // Analyze confusion pairs
        for (const entry of confusionMatrix) {
            if (entry.count >= 2) {
                recommendations.push({
                    type: 'rec-confusion',
                    title: 'Confusion: "' + entry.original_class + '" misclassified as "' + entry.corrected_class + '"',
                    detail: entry.count + ' correction(s). Review YOLO-World prompts for these classes. ' +
                        'Consider adding more distinguishing keywords or adjusting confusion pair rules in vehicle_detect_runner.py.'
                });
            }
        }

        // Check for classes with no data
        const allClasses = Object.keys(this.CURRENT_THRESHOLDS);
        const detectedClasses = Object.keys(classStats);
        const missingClasses = allClasses.filter(c => !detectedClasses.includes(c));
        if (missingClasses.length > 0) {
            recommendations.push({
                type: 'rec-data-needed',
                title: 'No detections for ' + missingClasses.length + ' class(es)',
                detail: 'Missing: ' + missingClasses.join(', ') + '. ' +
                    'These may be seasonal (snowmobile, boats) or rare in your environment. Consider supplemental training images.'
            });
        }

        // Check for classes with very few samples
        for (const [className, stats] of Object.entries(classStats)) {
            const readiness = this.data.readiness[className];
            if (readiness && readiness.progress_pct < 5 && (stats.total || 0) < 10) {
                recommendations.push({
                    type: 'rec-data-needed',
                    title: 'Low sample count for "' + className + '"',
                    detail: 'Only ' + (stats.total || 0) + ' detections (' + readiness.progress_pct.toFixed(1) +
                        '% of fine-tuning target). Use import_supplemental_images.py or extract_coco_vehicles.py to add training data.'
                });
            }
        }

        // Render
        container.textContent = '';

        if (recommendations.length === 0) {
            const noRec = document.createElement('div');
            noRec.className = 'no-recommendations';
            noRec.textContent = 'No recommendations at this time. Continue collecting data and reviewing predictions.';
            container.appendChild(noRec);
            return;
        }

        recommendations.forEach(rec => {
            const div = document.createElement('div');
            div.className = 'recommendation ' + rec.type;

            const title = document.createElement('div');
            title.className = 'rec-title';
            title.textContent = rec.title;
            div.appendChild(title);

            const detail = document.createElement('div');
            detail.className = 'rec-detail';
            detail.textContent = rec.detail;
            div.appendChild(detail);

            container.appendChild(div);
        });
    },

    formatWeekLabel(weekStr) {
        // Format YYYY-MM-DD to MM/DD
        try {
            const parts = weekStr.split('-');
            if (parts.length === 3) {
                return `${parts[1]}/${parts[2]}`;
            }
            return weekStr;
        } catch (e) {
            return weekStr;
        }
    }
};

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    vehicleMetrics.init();
});
