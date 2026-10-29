# SPDX-License-Identifier: AGPL-3.0-only
"""
Admin editor for the CT species table (/<lang>/camera-traps/admin/species).

Every column of `species` is editable. Free text only where values are unique
per species (Latin and common names); taxonomy, category and establishment
means are dropdowns of the values already in the table plus "add new", so
nobody retypes "Mammalia". The Red Book category is a fixed list (the
2021 order defines it, a new value would be a typo).

Plain SQL over a whitelisted column list keeps it testable on SQLite. The PAM
module has its own copy (submodules are independent, copies may diverge).
"""
from flask import render_template, request, redirect, url_for, flash, g, abort
from flask_login import login_required
from sqlalchemy import text

from . import camera_traps_bp
from .database import get_ct_engine
from .decorators import role_required
from .domain import _, _l

NEW_OPTION = '__new__'

# Red Data Book of Ukraine 2021 (order No. 29 of 19.01.2021), stored as codes.
REDBOOK_CODES = ('extinct', 'extinct_in_wild', 'endangered', 'vulnerable',
                 'rare', 'not_evaluated', 'data_deficient')
REDBOOK_LABELS = {
    'extinct': _l('зниклий'),
    'extinct_in_wild': _l('зниклий в природі'),
    'endangered': _l('зникаючий'),
    'vulnerable': _l('вразливий'),
    'rare': _l('рідкісний'),
    'not_evaluated': _l('неоцінений'),
    'data_deficient': _l('недостатньо відомий'),
}

# Taxonomic ranks from the top; picking a lower one fills the ones above it.
TAXON_RANKS = ('kingdom', 'phylum', 'class', 'order_rank', 'family', 'genus')

# (column, label, kind, max length, required). kind: text | choice | redbook | bool
FIELDS = (
    ('scientific_name', _l('Латинська назва'), 'text', 200, True),
    ('common_name_ua', _l('Українська назва'), 'text', 200, False),
    ('common_name_en', _l('Англійська назва'), 'text', 200, False),
    ('category', _l('Категорія'), 'choice', 50, True),
    ('kingdom', _l('Царство'), 'choice', 100, False),
    ('phylum', _l('Тип'), 'choice', 100, False),
    ('class', _l('Клас'), 'choice', 100, False),
    ('order_rank', _l('Ряд'), 'choice', 100, False),
    ('family', _l('Родина'), 'choice', 100, False),
    ('genus', _l('Рід'), 'choice', 100, False),
    ('establishment_means', _l('Статус присутності'), 'choice', 100, False),
    ('redbook2021', _l('Червона книга України (2021)'), 'redbook', 30, False),
    ('is_active', _l('Активний'), 'bool', None, False),
)
COLUMNS = tuple(f[0] for f in FIELDS)
CHOICE_COLUMNS = tuple(f[0] for f in FIELDS if f[2] == 'choice')

# ai_runner looks these rows up by Latin name; renaming them breaks AI runs.
LOCKED_NAMES = ('empty', 'Homo sapiens')


def _q(col):
    return '"class"' if col == 'class' else col


def list_species(conn):
    cols = ', '.join(_q(c) for c in COLUMNS)
    rows = conn.execute(text(f'SELECT id, {cols} FROM species ORDER BY scientific_name'))
    return [dict(r._mapping) for r in rows]


def get_species(conn, species_id):
    cols = ', '.join(_q(c) for c in COLUMNS)
    row = conn.execute(text(f'SELECT id, {cols} FROM species WHERE id = :i'),
                       {'i': species_id}).first()
    return dict(row._mapping) if row else None


def choice_values(rows):
    """Distinct non-empty values per dropdown column, sorted."""
    return {c: sorted({r[c] for r in rows if r.get(c)}) for c in CHOICE_COLUMNS}


def taxonomy_parents(rows):
    """{rank: {value: {higher_rank: value}}} from the first species that has them.

    Lets the form fill family/order/class... once a genus is picked.
    """
    out = {rank: {} for rank in TAXON_RANKS}
    for r in rows:
        for i, rank in enumerate(TAXON_RANKS):
            val = r.get(rank)
            if not val:
                continue
            parents = out[rank].setdefault(val, {})
            for higher in TAXON_RANKS[:i]:
                if r.get(higher) and higher not in parents:
                    parents[higher] = r[higher]
    return out


def parse_form(form):
    """Form -> (values, errors). Pure; uniqueness is checked by save_species."""
    values, errors = {}, []
    for col, label, kind, max_len, required in FIELDS:
        if kind == 'bool':
            values[col] = form.get(col) in ('1', 'on', 'true')
            continue
        raw = form.get(col, '')
        if kind == 'choice' and raw == NEW_OPTION:
            raw = form.get(f'{col}__new', '')
        val = ' '.join(raw.split()) or None
        if kind == 'redbook' and val is not None and val not in REDBOOK_CODES:
            errors.append(_('Невідома категорія Червоної книги: %(v)s', v=val))
            val = None
        if required and not val:
            errors.append(_('Поле «%(f)s» обов\'язкове.', f=label))
        elif val and max_len and len(val) > max_len:
            errors.append(_('Поле «%(f)s» довше за %(n)s символів.', f=label, n=max_len))
        values[col] = val
    return values, errors


def save_species(conn, species_id, values):
    """Insert (species_id None) or update. Returns (id, errors)."""
    dup = conn.execute(
        text('SELECT id FROM species WHERE lower(scientific_name) = lower(:n) AND id <> :i'),
        {'n': values['scientific_name'], 'i': species_id or -1}).first()
    if dup:
        return None, [_('Вид з такою латинською назвою вже є.')]
    if species_id is not None:
        old = get_species(conn, species_id)
        if old is None:
            return None, [_('Вид не знайдено.')]
        if old['scientific_name'] in LOCKED_NAMES and values['scientific_name'] != old['scientific_name']:
            return None, [_('Латинську назву службового запису «%(n)s» змінювати не можна: '
                            'за нею його шукає AI.', n=old['scientific_name'])]
        sets = ', '.join(f'{_q(c)} = :{c}' for c in COLUMNS)
        conn.execute(text(f'UPDATE species SET {sets} WHERE id = :id'),
                     dict(values, id=species_id))
        return species_id, []
    cols = ', '.join(_q(c) for c in COLUMNS)
    params = ', '.join(f':{c}' for c in COLUMNS)
    new_id = conn.execute(text(f'INSERT INTO species ({cols}) VALUES ({params}) RETURNING id'),
                          values).scalar()
    return new_id, []


def _form_context(conn, species, errors=()):
    rows = list_species(conn)
    return dict(species=species, fields=FIELDS, choices=choice_values(rows),
                taxonomy=taxonomy_parents(rows), redbook_codes=REDBOOK_CODES,
                redbook_labels=REDBOOK_LABELS, new_option=NEW_OPTION,
                locked_names=LOCKED_NAMES, errors=list(errors))


@camera_traps_bp.route('/admin/species')
@login_required
@role_required('admin')
def species_admin_list(lang_code):
    with get_ct_engine().connect() as conn:
        rows = list_species(conn)
    return render_template('species_admin_list.html', rows=rows,
                           redbook_labels=REDBOOK_LABELS,
                           redbook_rank={c: f'{i:02d}' for i, c in enumerate(REDBOOK_CODES)})


@camera_traps_bp.route('/admin/species/new', methods=['GET', 'POST'])
@camera_traps_bp.route('/admin/species/<int:species_id>', methods=['GET', 'POST'])
@login_required
@role_required('admin')
def species_admin_edit(lang_code, species_id=None):
    engine = get_ct_engine()
    if request.method == 'POST':
        values, errors = parse_form(request.form)
        if not errors:
            with engine.begin() as conn:
                saved_id, errors = save_species(conn, species_id, values)
            if not errors:
                flash(_('Збережено: %(n)s', n=values['scientific_name']), 'success')
                return redirect(url_for('camera_traps.species_admin_list',
                                        lang_code=g.lang_code) + f'#sp-{saved_id}')
        with engine.connect() as conn:
            ctx = _form_context(conn, dict(values, id=species_id), errors)
        return render_template('species_admin_edit.html', **ctx), 400

    with engine.connect() as conn:
        if species_id is None:
            species = {c: None for c in COLUMNS}
            species.update(id=None, is_active=True)
        else:
            species = get_species(conn, species_id)
            if species is None:
                abort(404)
        ctx = _form_context(conn, species)
    return render_template('species_admin_edit.html', **ctx)
