# SPDX-License-Identifier: AGPL-3.0-only
"""Admin page: people's records behind the weighted closing rule.

Shows verifier_species_weights (see verifier_weights.py, consensus.py):
per person a summary, and per person x species n, confirmed, precision and
the weight confirmed / (n + k). A button rebuilds the table now (in the
background); otherwise it rebuilds itself once a day.

The closing threshold and DeepFaune's vote factor are edited here (owner,
2026-11-23): kept in ct_db.ct_settings (ct_settings.py), config values as the
fallback. "Estimate" shows what new values would change before they are
saved: who gains or loses "alone + AI" on which species, and how many pending
series they would close.
"""
from datetime import datetime

from flask import current_app, flash, g, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import inspect, text

from . import camera_traps_bp
from . import ct_settings
from .consensus import verifier_weight, weighted_close
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
    k = vw.experience_k()
    sel_user, sel_species = _int_arg('user'), _int_arg('species')

    ct = get_ct_session()
    try:
        ct_settings.forget()
        threshold = ct_settings.weighted_threshold(ct, cfg)
        ai_factor = ct_settings.ai_vote_factor(ct, cfg)
        setting = {'editable': ct_settings.table_ready(ct), 'fields': {}}
        for key in (ct_settings.THRESHOLD_KEY, ct_settings.AI_FACTOR_KEY):
            _cfg_key, _default, lo, hi = ct_settings.SETTINGS[key]
            setting['fields'][key] = {
                'value': ct_settings.value(ct, key, cfg),
                'stored': ct_settings.info(ct, key),
                'fallback': ct_settings.fallback(key, cfg),
                'min': lo, 'max': hi,
            }
        # One person + DeepFaune (score 1.0) close a series from this weight on.
        solo_weight = threshold - ai_factor
        if not inspect(ct.connection()).has_table(vw.TABLE):
            return render_template('verifier_weights.html', missing=True, rows=[],
                                   people=[], users=[], species_opts=[],
                                   cfg=cfg, threshold=threshold, ai_factor=ai_factor,
                                   solo_weight=solo_weight, updated_at=None, k=k,
                                   setting=setting, running=vw.refresh_running(),
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
                           setting=setting, updated_at=updated_at, running=vw.refresh_running(),
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


@camera_traps_bp.route('/admin/verifier-weights/settings', methods=['POST'])
@login_required
@role_required('admin')
def verifier_weights_save_settings(lang_code):
    """Save the closing threshold and the DeepFaune factor (the page asks
    for confirmation first). Only values that changed are written."""
    back = redirect(url_for('camera_traps.verifier_weights_page', lang_code=g.lang_code))
    fields = ((ct_settings.THRESHOLD_KEY, 'threshold', _('Поріг закриття')),
              (ct_settings.AI_FACTOR_KEY, 'ai_factor', _('Коефіцієнт DeepFaune')))
    new = {}
    for key, form_name, label in fields:
        if request.form.get(form_name) in (None, ''):
            continue
        parsed = ct_settings.parse(key, request.form.get(form_name))
        if parsed is None:
            _c, _d, lo, hi = ct_settings.SETTINGS[key]
            flash(_('%(name)s має бути числом від %(lo)s до %(hi)s.', name=label, lo=lo, hi=hi),
                  'danger')
            return back
        new[key] = (parsed, label)
    ct = get_ct_session()
    try:
        if not ct_settings.table_ready(ct):
            flash(_('Таблиці налаштувань ще немає: запустіть scripts.init_ct_settings --apply.'),
                  'warning')
            return back
        changes = []
        for key, (val, label) in new.items():
            old = ct_settings.value(ct, key)
            if abs(old - val) < 1e-9:
                continue
            ct_settings.put(ct, key, val, current_user.username)
            changes.append(f'{label}: {old} → {val}')
            current_app.logger.info(
                f'[ct_settings] {key}: {old} -> {val} by {current_user.username}')
        ct.commit()
        if changes:
            flash(_('Збережено: %(changes)s. Діє для нових голосів; щоб застосувати до серій, '
                    'що вже чекають, натисніть «Перерахувати консенсус» в адмін-панелі.',
                    changes='; '.join(changes)), 'success')
        else:
            flash(_('Нічого не змінилось.'), 'info')
    except Exception as exc:
        ct.rollback()
        current_app.logger.error(f'saving ct settings failed: {exc}')
        flash(_('Не вдалося зберегти налаштування.'), 'danger')
    finally:
        close_ct_session()
    return back


def _pending_votes(ct):
    """{obs_id: {(user, species)}} for pending series with at least one vote."""
    by_obs = {}
    for obs, u, sp in ct.execute(text("""
            SELECT DISTINCT p.observation_id, i.user_id, i.species_id
            FROM identifications i JOIN photos p ON p.id = i.photo_id
            JOIN observations o ON o.id = p.observation_id
            WHERE o.status = 'pending' AND i.species_id IS NOT NULL""")):
        by_obs.setdefault(obs, set()).add((u, sp))
    return by_obs


@camera_traps_bp.route('/admin/verifier-weights/estimate')
@login_required
@role_required('admin')
def verifier_weights_estimate(lang_code):
    """What a new threshold and DeepFaune factor would change, read-only (JSON)."""
    cfg = current_app.config.get('CAMERA_TRAP_CONFIG', {})
    min_ids = cfg.get('MIN_IDENTIFICATIONS', 3)
    k = vw.experience_k()
    lang = g.get('lang_code', 'uk')
    ct = get_ct_session()
    try:
        old = ct_settings.weighted_threshold(ct, cfg)
        old_ai = ct_settings.ai_vote_factor(ct, cfg)
        new = ct_settings.parse(ct_settings.THRESHOLD_KEY, request.args.get('threshold', old))
        new_ai = ct_settings.parse(ct_settings.AI_FACTOR_KEY, request.args.get('ai_factor', old_ai))
        if new is None or new_ai is None:
            return jsonify(error=_('Значення поза допустимими межами.')), 400
        records = []
        if inspect(ct.connection()).has_table(vw.TABLE):
            records = ct.execute(text(
                f'SELECT user_id, species_id, n, confirmed FROM {vw.TABLE}')).fetchall()
        species = {sid: ((en if lang == 'en' else ua) or ua or en or sci)
                   for sid, ua, en, sci in ct.execute(text(
                       'SELECT id, common_name_ua, common_name_en, scientific_name FROM species'))}
        pending = _pending_votes(ct)
        candidates = {o: v for o, v in pending.items() if len({u for u, _s in v}) >= min_ids}
        ai = vw.series_ai_answers(ct, candidates)
        ct.rollback()
    finally:
        close_ct_session()

    weights = {(u, s): verifier_weight(c, n, k) for u, s, n, c in records}
    solo_old, solo_new = old - old_ai, new - new_ai
    names = _people({u for u, _s in weights})
    gained, lost = [], []
    for (u, s), w in weights.items():
        was, now = w >= solo_old - 1e-9, w >= solo_new - 1e-9
        if was != now:
            item = {'login': names.get(u, (f'#{u}', ''))[0], 'species': species.get(s, f'#{s}'),
                    'weight': round(w, 3)}
            (gained if now else lost).append(item)
    for lst in (gained, lost):
        lst.sort(key=lambda r: (r['login'].lower(), r['species'].lower()))

    def closes(threshold, ai_factor):
        """{species_id: series it would close now}."""
        out = {}
        for obs, pairs in candidates.items():
            sp = weighted_close(pairs, lambda u, x: weights.get((u, x), 0.0), ai.get(obs),
                                threshold=threshold, ai_factor=ai_factor)
            if sp is not None:
                out[sp] = out.get(sp, 0) + 1
        return out

    now_old, now_new = closes(old, old_ai), closes(new, new_ai)
    by_species = sorted(({'species': species.get(sp, f'#{sp}'), 'count': n}
                         for sp, n in now_new.items()), key=lambda r: -r['count'])

    return jsonify(old=old, new=new, old_ai=old_ai, new_ai=new_ai,
                   solo_old=round(solo_old, 3), solo_new=round(solo_new, 3),
                   gained=gained, lost=lost, pending_total=len(pending),
                   close_now_old=sum(now_old.values()), close_now_new=sum(now_new.values()),
                   close_now_species=by_species)
