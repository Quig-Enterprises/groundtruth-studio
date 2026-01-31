let allCategories = [];
let allSuggestions = [];
let currentCategory = null;
let editingId = null;

async function loadCategories() {
    try {
        const response = await fetch('/api/tag-suggestions/categories');
        const data = await response.json();

        if (data.success) {
            allCategories = data.categories;
            displayCategories();
        }
    } catch (error) {
        console.error('Error loading categories:', error);
    }
}

function displayCategories() {
    const container = document.getElementById('categories-list');

    if (allCategories.length === 0) {
        container.innerHTML = '<div class="empty-state">No categories yet. Add your first tag suggestion to create a category.</div>';
        return;
    }

    const allChip = `<div class="category-chip all ${currentCategory === null ? 'active' : ''}" onclick="filterByCategory(null)">All Categories</div>`;

    const categoryChips = allCategories.map(cat => `
        <div class="category-chip ${currentCategory === cat ? 'active' : ''}" onclick="filterByCategory('${escapeHtml(cat)}')">
            ${escapeHtml(cat)}
        </div>
    `).join('');

    container.innerHTML = allChip + categoryChips;
}

async function loadSuggestions(category = null) {
    try {
        const url = category ? `/api/tag-suggestions?category=${encodeURIComponent(category)}` : '/api/tag-suggestions';
        const response = await fetch(url);
        const data = await response.json();

        if (data.success) {
            allSuggestions = data.suggestions;
            displaySuggestions();
        }
    } catch (error) {
        console.error('Error loading suggestions:', error);
    }
}

function displaySuggestions() {
    const container = document.getElementById('suggestions-list');
    const title = document.getElementById('current-category-title');

    title.textContent = currentCategory ? `Tag Suggestions: ${currentCategory}` : 'All Tag Suggestions';

    if (allSuggestions.length === 0) {
        container.innerHTML = '<div class="empty-state">No tag suggestions yet. Click "+ Add Tag Suggestion" to create one.</div>';
        return;
    }

    container.innerHTML = allSuggestions.map(suggestion => {
        const isNegative = suggestion.is_negative === 1 || suggestion.is_negative === true;

        return `
            <div class="suggestion-item ${isNegative ? 'negative' : ''}">
                <div class="suggestion-header">
                    <div>
                        <span class="suggestion-tag-text">${escapeHtml(suggestion.tag_text)}</span>
                        ${!currentCategory ? `<span class="suggestion-category">${escapeHtml(suggestion.category)}</span>` : ''}
                    </div>
                    <div class="suggestion-actions">
                        <button onclick="editSuggestion(${suggestion.id})" class="btn-edit">Edit</button>
                        <button onclick="deleteSuggestion(${suggestion.id})" class="btn-delete-small">Delete</button>
                    </div>
                </div>
                ${suggestion.description ? `<div class="suggestion-description">${escapeHtml(suggestion.description)}</div>` : ''}
                <div class="suggestion-meta">
                    <span>Sort Order: ${suggestion.sort_order}</span>
                    <span>${isNegative ? 'Negative Example' : 'Positive Example'}</span>
                </div>
            </div>
        `;
    }).join('');
}

function filterByCategory(category) {
    currentCategory = category;
    displayCategories();
    loadSuggestions(category);
}

function showAddModal() {
    editingId = null;
    document.getElementById('modal-title').textContent = 'Add Tag Suggestion';
    document.getElementById('modal-category').value = currentCategory || '';
    document.getElementById('modal-tag-text').value = '';
    document.getElementById('modal-is-negative').checked = false;
    document.getElementById('modal-description').value = '';
    document.getElementById('modal-sort-order').value = '0';
    document.getElementById('modal-suggestion-id').value = '';

    updateCategorySuggestions();

    document.getElementById('suggestion-modal').style.display = 'flex';
    document.getElementById('modal-category').focus();
}

function editSuggestion(id) {
    const suggestion = allSuggestions.find(s => s.id === id);
    if (!suggestion) return;

    editingId = id;
    document.getElementById('modal-title').textContent = 'Edit Tag Suggestion';
    document.getElementById('modal-category').value = suggestion.category;
    document.getElementById('modal-tag-text').value = suggestion.tag_text;
    document.getElementById('modal-is-negative').checked = suggestion.is_negative === 1 || suggestion.is_negative === true;
    document.getElementById('modal-description').value = suggestion.description || '';
    document.getElementById('modal-sort-order').value = suggestion.sort_order;
    document.getElementById('modal-suggestion-id').value = id;

    updateCategorySuggestions();

    document.getElementById('suggestion-modal').style.display = 'flex';
    document.getElementById('modal-category').focus();
}

async function saveSuggestion() {
    const category = document.getElementById('modal-category').value.trim();
    const tagText = document.getElementById('modal-tag-text').value.trim();
    const isNegative = document.getElementById('modal-is-negative').checked;
    const description = document.getElementById('modal-description').value.trim();
    const sortOrder = parseInt(document.getElementById('modal-sort-order').value) || 0;

    if (!category || !tagText) {
        alert('Category and Tag Text are required');
        return;
    }

    const payload = {
        category,
        tag_text: tagText,
        is_negative: isNegative,
        description: description || null,
        sort_order: sortOrder
    };

    try {
        let response;
        if (editingId) {
            response = await fetch(`/api/tag-suggestions/${editingId}`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
        } else {
            response = await fetch('/api/tag-suggestions', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
        }

        const data = await response.json();

        if (data.success) {
            closeModal();
            loadCategories();
            loadSuggestions(currentCategory);
        } else {
            alert('Error saving suggestion: ' + data.error);
        }
    } catch (error) {
        alert('Error: ' + error.message);
    }
}

async function deleteSuggestion(id) {
    if (!confirm('Delete this tag suggestion?')) return;

    try {
        const response = await fetch(`/api/tag-suggestions/${id}`, {
            method: 'DELETE'
        });

        const data = await response.json();

        if (data.success) {
            loadCategories();
            loadSuggestions(currentCategory);
        } else {
            alert('Error deleting suggestion');
        }
    } catch (error) {
        alert('Error: ' + error.message);
    }
}

async function seedDefaults() {
    if (!confirm('This will add default tag suggestions for power_loading, license_plate, and person_face categories. Continue?')) {
        return;
    }

    try {
        const response = await fetch('/api/tag-suggestions/seed', {
            method: 'POST'
        });

        const data = await response.json();

        if (data.success) {
            alert('Default tag suggestions added successfully');
            loadCategories();
            loadSuggestions(currentCategory);
        } else {
            alert('Error seeding suggestions');
        }
    } catch (error) {
        alert('Error: ' + error.message);
    }
}

function updateCategorySuggestions() {
    const datalist = document.getElementById('category-suggestions');
    datalist.innerHTML = allCategories.map(cat =>
        `<option value="${escapeHtml(cat)}">`
    ).join('');
}

function closeModal() {
    document.getElementById('suggestion-modal').style.display = 'none';
    editingId = null;
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

window.onclick = function(event) {
    const modal = document.getElementById('suggestion-modal');
    if (event.target === modal) {
        closeModal();
    }
}

loadCategories();
loadSuggestions();
