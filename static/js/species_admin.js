// SPDX-License-Identifier: AGPL-3.0-only
// Species editor (admin): list filter + dropdowns with "add new" and
// taxonomy autofill. A copy lives in shared-pam; copies may diverge.
(function () {
    'use strict';

    // ── list page: text + class filter ──
    var filter = document.getElementById('sp-filter');
    var classFilter = document.getElementById('sp-class-filter');
    var rows = document.querySelectorAll('tr[data-search]');
    function applyFilter() {
        var q = (filter && filter.value || '').trim().toLowerCase();
        var cls = classFilter ? classFilter.value : '';
        var shown = 0;
        rows.forEach(function (tr) {
            var ok = (!q || tr.dataset.search.indexOf(q) !== -1) &&
                     (!cls || tr.dataset.class === cls);
            tr.hidden = !ok;
            if (ok) shown++;
            tr.classList.toggle('sp-odd', ok && shown % 2 === 0);
        });
        var counter = document.getElementById('sp-count');
        if (counter) counter.textContent = shown;
    }
    if (filter) filter.addEventListener('input', applyFilter);
    if (classFilter) classFilter.addEventListener('change', applyFilter);
    if (rows.length) applyFilter();

    // ── edit page: "add new" toggles a text input ──
    var form = document.getElementById('sp-form');
    if (!form) return;
    var NEW = form.dataset.newOption;
    var ranks = JSON.parse(form.dataset.ranks || '[]');
    var taxonomyEl = document.getElementById('sp-taxonomy');
    var taxonomy = taxonomyEl ? JSON.parse(taxonomyEl.textContent) : {};

    function toggleNew(select) {
        var input = form.querySelector('[name="' + select.name + '__new"]');
        if (!input) return;
        var on = select.value === NEW;
        input.hidden = !on;
        input.required = on && select.required;
        if (on) input.focus();
    }

    function setSelect(col, value) {
        var select = form.querySelector('select[name="' + col + '"]');
        if (!select || !value) return;
        select.value = value;
        toggleNew(select);
    }

    form.querySelectorAll('select.sp-choice').forEach(function (select) {
        toggleNew(select);
        select.addEventListener('change', function () {
            toggleNew(select);
            // Picking a known genus/family/... fills the ranks above it.
            var parents = (taxonomy[select.name] || {})[select.value];
            if (ranks.indexOf(select.name) !== -1 && parents) {
                Object.keys(parents).forEach(function (rank) {
                    setSelect(rank, parents[rank]);
                });
            }
        });
    });
})();
