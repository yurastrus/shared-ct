# SPDX-License-Identifier: AGPL-3.0-only
"""Admin page: people's records behind the weighted closing rule.

Shows verifier_species_weights (see verifier_weights.py, consensus.py):
per person a summary, and per person x species n, confirmed, precision and
the weight confirmed / (n + 1). A button rebuilds the table now (in the
background); otherwise it rebuilds itself once a day.
"""
from datetime import datetime

from flask import current_app, flash, g, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import inspect, text

from . import camera_traps_bp
from .consensus import (AI_VOTE_FACTOR_DEFAULT, WEIGHTED_THRESHOLD_DEFAULT,
                        verifier_weight)
from .database import close_ct_session, get_ct_session
from .decorators import role_required
from .domain import _
from . import verifier_weights as vw

EMPTY_ID = -1


def _people(user_ids):
    """{user_id: (login, full name)} from the main database."""
    try:
        from app.models import User
    except ImportError:  # host without the main-DB model
        return {}
    users = User.query.filter(User.id.in_(list(user_ids))).all() if user_ids else []
    return {u.id: (u.username, getattr(u, 'full_name', '') or '') for u in users}


def _int_arg(name):
    try:
        return int(request.args.get(name)) if request.args.get(name) else None
    except ValueError:
        return None


@camera_traps_bp.route('/admin/verifier-weights')
@login_required
@role_required('admin')
def verifier_weights_page(lang_code):
    cfg = current_app.config.get('CAMERA_TRAP_CONFIG', {})
    threshold = cfg.get('WEIGHTED_CONSENSUS_THRESHOLD', WEIGHTED_THRESHOLD_DEFAULT)
    ai_factor = cfg.get('AI_VOTE_FACTOR', AI_VOTE_FACTOR_DEFAULT)
    # One person + DeepFaune (score 1.0) close a series from this weight on.
    solo_weight = threshold - ai_factor
    k = vw.experience_k()
    sel_user, sel_species = _int_arg('user'), _int_arg('species')

    ct = get_ct_session()
    try:
        if not inspect(ct.connection()).has_table(vw.TABLE):
            return render_template('verifier_weights.html', missing=True, rows=[],
                                   people=[], users=[], species_opts=[],
                                   cfg=cfg, threshold=threshold, ai_factor=ai_factor,
                                   solo_weight=solo_weight, updated_at=None, k=k,
                                   running=vw.refresh_running(),
                                   sel_user=None, sel_species=None)
        records = ct.execute(text(
            f'SELECT user_id, species_id, n, confirmed FROM {vw.TABLE}')).fetchall()
        updated_at = ct.execute(text(f'SELECT MAX(updated_at) FROM {vw.TABLE}')).scalar()
        if isinstance(updated_at, str):  # SQLite returns text
            updated_at = datetime.fromisoformat(updated_at)
        lang = g.get('lang_code', 'uk')
        species = {}
        for sid, ua, en, sci in ct.execute(text(
                'SELECT id, common_name_ua, common_name_en, scientific_name FROM species')):
            species[sid] = (en if lang == 'en' else ua) or ua or en or sci
        ct.rollback()
    finally:
        close_ct_session()

    names = _people({u for u, _s, _n, _c in records})

    def person(uid):
        login, full = names.get(uid, (f'#{uid}', ''))
        return login, full

    summary = {}
    rows = []
    for uid, sid, n, conf in records:
        w = verifier_weight(conf, n, k)
        s = summary.setdefault(uid, {'n': 0, 'confirmed': 0, 'species': 0, 'empty': None,
                                     'solo': 0})
        s['n'] += n
        s['confirmed'] += conf
        s['species'] += 1
        if sid == EMPTY_ID:
            s['empty'] = w
        if w >= solo_weight - 1e-9:
            s['solo'] += 1
        if (sel_user is None or uid == sel_user) and (sel_species is None or sid == sel_species):
            login, full = person(uid)
            rows.append({'user_id': uid, 'login': login, 'full_name': full,
                         'species_id': sid, 'species': species.get(sid, f'#{sid}'),
                         'n': n, 'confirmed': conf,
                         'precision': conf / n if n else None, 'weight': w,
                         'solo': w >= solo_weight - 1e-9})
    rows.sort(key=lambda r: (r['login'].lower(), -r['n']))

    people = []
    for uid, s in summary.items():
        login, full = person(uid)
        people.append({'user_id': uid, 'login': login, 'full_name': full,
                       'n': s['n'], 'confirmed': s['confirmed'],
                       'precision': s['confirmed'] / s['n'] if s['n'] else None,
                       'species': s['species'], 'empty': s['empty'], 'solo': s['solo']})
    people.sort(key=lambda p: -p['n'])

    species_opts = sorted({(sid, species.get(sid, f'#{sid}')) for _u, sid, _n, _c in records},
                          key=lambda x: x[1].lower())
    users = sorted(((p['user_id'], p['login']) for p in people), key=lambda x: x[1].lower())
    return render_template('verifier_weights.html', missing=False, rows=rows, people=people,
                           users=users, species_opts=species_opts, cfg=cfg,
                           threshold=threshold, ai_factor=ai_factor, solo_weight=solo_weight, k=k,
                           updated_at=updated_at, running=vw.refresh_running(),
                           sel_user=sel_user, sel_species=sel_species)


@camera_traps_bp.route('/admin/verifier-weights/refresh', methods=['POST'])
@login_required
@role_required('admin')
def verifier_weights_refresh(lang_code):
    ct = get_ct_session()
    try:
        if not inspect(ct.connection()).has_table(vw.TABLE):
            flash(_('Таблиці ваг ще немає: запустіть scripts.init_verifier_weights --apply.'),
                  'warning')
        elif vw.start_refresh(ct):
            flash(_('Перерахунок ваг запущено у фоні. Оновіть сторінку за хвилину.'), 'success')
        else:
            flash(_('Перерахунок ваг уже триває.'), 'info')
    finally:
        close_ct_session()
    return redirect(url_for('camera_traps.verifier_weights_page', lang_code=g.lang_code))
