function createClassSearchWidget(options) {
    const {
        classes = [],
        onSelect,
        placeholder = 'Search classes...',
        excludeClasses = [],
        showApplyButton = true
    } = options;

    const container = document.createElement('div');

    const label = document.createElement('div');
    label.className = 'classify-divider';
    label.textContent = 'All classifications:';
    container.appendChild(label);

    const row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:8px;align-items:center;';

    const datalistId = 'class-datalist-' + Math.random().toString(36).slice(2, 9);

    const input = document.createElement('input');
    input.type = 'text';
    input.placeholder = placeholder;
    input.setAttribute('list', datalistId);
    input.style.cssText = [
        'background:#1a1a2e',
        'border:1px solid #3f3f46',
        'color:#e8e8e8',
        'padding:8px 12px',
        'border-radius:6px',
        'width:100%',
        'font-size:14px'
    ].join(';');

    const datalist = document.createElement('datalist');
    datalist.id = datalistId;

    const excludeLower = excludeClasses.map(c => c.toLowerCase());
    classes.forEach(cls => {
        if (!excludeLower.includes(cls.name.toLowerCase())) {
            const option = document.createElement('option');
            option.value = cls.name;
            datalist.appendChild(option);
        }
    });

    input.appendChild(datalist);
    row.appendChild(input);

    function applySelection() {
        const val = input.value.trim();
        if (val && typeof onSelect === 'function') {
            onSelect(val);
        }
    }

    input.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') applySelection();
    });

    if (showApplyButton) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'classify-chip';
        btn.textContent = 'Apply';
        btn.addEventListener('click', applySelection);
        row.appendChild(btn);
    }

    container.appendChild(row);
    return container;
}

function populateClassDatalist(datalistId, classes) {
    const datalist = document.getElementById(datalistId);
    if (!datalist) return;
    while (datalist.firstChild) {
        datalist.removeChild(datalist.firstChild);
    }
    classes.forEach(cls => {
        const option = document.createElement('option');
        option.value = cls.name;
        datalist.appendChild(option);
    });
}
