// SPDX-License-Identifier: AGPL-3.0-only
// Species editor (admin): list filter + sorting + dropdowns with "add new" and
// taxonomy autofill. A copy lives in shared-pam; copies may diverge.
(function () {
    'use strict';

    // ── list page: text + class filter ──
    var filter = document.getElementById('sp-filter');
    var classFilter = document.getElementById('sp-class-filter');
    var tbody = document.querySelector('.sp-table tbody');
    function listRows() {
        // Re-read every time: sorting reorders the rows in the DOM.
        return tbody ? Array.prototype.slice.call(tbody.querySelectorAll('tr[data-search]')) : [];
    }
    function applyFilter() {
        var q = (filter && filter.value || '').trim().toLowerCase();
        var cls = classFilter ? classFilter.value : '';
        var shown = 0;
        listRows().forEach(function (tr) {
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
    if (tbody) applyFilter();

    // ── list page: click a header to sort, again to reverse ──
    // A cell's data-sort overrides its text (Red Book: by severity, not by
    // label). Empty cells always go last.
    var collator = new Intl.Collator(document.documentElement.lang || 'uk',
                                     {numeric: true, sensitivity: 'base'});
    function sortKey(td) {
        if (!td) return '';
        return (td.dataset.sort !== undefined ? td.dataset.sort : td.textContent).trim();
    }
    document.querySelectorAll('.sp-table th[data-sortable]').forEach(function (th) {
        th.addEventListener('click', function () {
            var col = Array.prototype.indexOf.call(th.parentNode.children, th);
            var asc = !th.classList.contains('sp-sort-asc');
            th.parentNode.querySelectorAll('th').forEach(function (h) {
                h.classList.remove('sp-sort-asc', 'sp-sort-desc');
                h.removeAttribute('aria-sort');
            });
            th.classList.add(asc ? 'sp-sort-asc' : 'sp-sort-desc');
            th.setAttribute('aria-sort', asc ? 'ascending' : 'descending');
            listRows().sort(function (a, b) {
                var x = sortKey(a.children[col]), y = sortKey(b.children[col]);
                if (!x || !y) return x ? -1 : (y ? 1 : 0);
                return asc ? collator.compare(x, y) : collator.compare(y, x);
            }).forEach(function (tr) { tbody.appendChild(tr); });
            applyFilter();
        });
    });

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
