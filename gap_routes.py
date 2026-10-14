# SPDX-License-Identifier: AGPL-3.0-only
"""Routes of the gap analysis page (Beta).

Where does a non-detection mean something? Effort, completeness and time on an
EEA grid, a kernel effort surface, and per-species "could it have been
missed". Computation lives in gap_analysis.py / gap_metrics.py; these routes
only parse the page's filters, enforce access and cache.

Kept out of routes.py on purpose (it is 7.8k lines already); the shared
helpers for scope and access are imported from there.
"""
import threading
import time
from datetime import datetime

from flask import current_app, flash, g, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from . import camera_traps_bp
from . import access as ct_access
from . import gap_analysis as ga
from . import gap_metrics as gm
from .database import get_ct_session, close_ct_session
from .decorators import role_required
from .domain import _
from .routes import (DOMESTIC_SPECIES_IDS, build_ecoregions, get_accessible_institutions,
                     resolve_scope)
from .utils import get_institution_filter

# Page and API are open to every signed-in user; the data each user sees is
# limited by institution access (public locations + their institutions, admin
# sees all), the same rule as the rest of the module. Set a role here (e.g.
# 'manager') to restrict the page again, and guard the card in overview.html.
GAP_ANALYSIS_MIN_ROLE = None

GAP_CACHE_TTL_SECONDS = 600
GAP_CACHE_MAX_ENTRIES = 32
GAP_KERNEL_BANDWIDTHS_M = (1000, 2500, 5000, 10000)

_gap_cache = {}
_gap_cache_lock = threading.Lock()


def _cache_get(key, build):
    """Tiny per-process TTL cache. Every key carries the caller's access rights."""
    now = time.monotonic()
    with _gap_cache_lock:
        hit = _gap_cache.get(key)
        if hit and now - hit[0] < GAP_CACHE_TTL_SECONDS:
            return hit[1]
    value = build()
    with _gap_cache_lock:
        if len(_gap_cache) >= GAP_CACHE_MAX_ENTRIES:
            oldest = min(_gap_cache, key=lambda k: _gap_cache[k][0])
            _gap_cache.pop(oldest, None)
        _gap_cache[key] = (now, value)
    return value


def _parse_date(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date() if value else None
    except ValueError:
        return None


def _request_params():
    """Validated filters shared by every gap-analysis endpoint."""
    args = request.args
    scale = args.get('scale', type=int)
    unit = args.get('unit', ga.DEFAULT_UNIT)
    taxon = args.get('taxon', 'mammals')
    start = _parse_date(args.get('start_date'))
    end = _parse_date(args.get('end_date'))
    if not (start and end) or start > end:
        start = end = None
    # QC exclusion is opt-in and for signed-in users only, as on the other pages.
    qc_exclude = (sorted(f for f in args.get('qc_exclude', '').split(',') if f)
                  if current_user.is_authenticated else [])
    return {
        'scale': scale if scale in gm.GRID_SCALES_M else 5000,
        'unit': unit if unit in (ga.UNIT_DAY, ga.UNIT_WEEK) else ga.DEFAULT_UNIT,
        'taxon': taxon if taxon in ga.TAXON_GROUPS else 'mammals',
        'species_id': args.get('species_id', type=int),
        'scope': args.get('scope', ''),
        'start': start, 'end': end,
        'qc_exclude': tuple(qc_exclude),
    }


def _gap_data(ct_session, p):
    """(cache key, GapData) for the request's filters and the user's access."""
    is_admin = current_user.is_authenticated and current_user.has_role('admin')
    user_inst_ids = ct_access.allowed_institution_ids(current_user)
    institutions = get_accessible_institutions(is_admin)
    scope, selected_inst_ids = resolve_scope(p['scope'], institutions)
    access_key = ('admin',) if is_admin else tuple(sorted(user_inst_ids or ()))
    key = ('data', access_key, scope, p['start'], p['end'], p['qc_exclude'], p['taxon'])

    def build():
        inst_condition, inst_params = get_institution_filter(
            user_inst_ids, is_admin, selected_inst_id=selected_inst_ids, table_alias='l')
        max_gap = current_app.config.get('CAMERA_TRAP_CONFIG', {}).get('COVERAGE_MAX_GAP_DAYS', 15)
        return ga.fetch_gap_data(
            ct_session, inst_condition, inst_params,
            start_date=p['start'], end_date=p['end'], qc_exclude=list(p['qc_exclude']),
            taxon=p['taxon'], exclude_species_ids=DOMESTIC_SPECIES_IDS, max_gap_days=max_gap)
    return key, _cache_get(key, build)


def _gap_grid(ct_session, p):
    data_key, data = _gap_data(ct_session, p)
    key = ('grid', data_key, p['scale'], p['unit'], p['species_id'], g.lang_code)
    return data, _cache_get(key, lambda: ga.build_grid(
        data, p['scale'], unit=p['unit'], focus_species_id=p['species_id'], lang=g.lang_code))


@camera_traps_bp.route('/analysis/gap-analysis')
def gap_analysis_page(lang_code):
    """Gap analysis page: where the camera-trap data are thin, and in what sense."""
    # login_required first: role_required redirects anonymous users to a
    # non-existent endpoint, which the video routes avoid the same way.
    if GAP_ANALYSIS_MIN_ROLE:
        return login_required(role_required(GAP_ANALYSIS_MIN_ROLE)(_render_gap_page))(lang_code)
    return login_required(_render_gap_page)(lang_code)


def _render_gap_page(lang_code):
    try:
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        institutions = get_accessible_institutions(is_admin)
        selected_scope, _ids = resolve_scope(
            request.args.get('scope', ''), institutions,
            current_app.config['CAMERA_TRAP_CONFIG'].get('CT_DEFAULT_SCOPE', ''))
        return render_template(
            'gap_analysis.html',
            institutions=institutions,
            ecoregions=build_ecoregions(institutions, g.lang_code),
            selected_scope=selected_scope,
            grid_scales=gm.GRID_SCALES_M,
            kernel_bandwidths=[(bw, round(gm.home_range_km2(bw))) for bw in GAP_KERNEL_BANDWIDTHS_M],
            min_units=gm.MIN_UNITS_FOR_ESTIMATE,
            pool_radius_km=ga.POOL_RADIUS_M // 1000,
        )
    except Exception as e:
        current_app.logger.error(f"Error loading gap analysis page: {e}", exc_info=True)
        flash(_("Помилка завантаження сторінки."), 'danger')
        return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))


def _json_endpoint(name, produce):
    """Run ``produce(ct_session, params)`` with session cleanup and uniform errors."""
    ct_session = get_ct_session()
    try:
        return produce(ct_session, _request_params())
    except Exception as e:
        current_app.logger.error(f"Error in gap analysis {name} API: {e}", exc_info=True)
        return jsonify({'error': 'Server error'}), 500
    finally:
        close_ct_session()


@camera_traps_bp.route('/api/gap-analysis/grid')
@login_required
def api_gap_grid(lang_code):
    """Grid layer (cells with metrics + empty cells), summary and location points."""
    def produce(ct_session, p):
        data, grid = _gap_grid(ct_session, p)
        return jsonify({'summary': grid['summary'], 'cells': grid['cells'],
                        'locations': ga.location_points(data)})
    return _json_endpoint('grid', produce)


@camera_traps_bp.route('/api/gap-analysis/cell/<code>')
@login_required
def api_gap_cell(lang_code, code):
    """Accumulation curve, species and locations of one grid cell."""
    def produce(ct_session, p):
        data, grid = _gap_grid(ct_session, p)
        detail = ga.cell_detail(grid, code, data, lang=g.lang_code)
        if detail is None:
            return jsonify({'error': 'Cell not found'}), 404
        return jsonify(detail)
    return _json_endpoint('cell', produce)


@camera_traps_bp.route('/api/gap-analysis/kernel')
@login_required
def api_gap_kernel(lang_code):
    """Kernel effort surface (camera-days per km²) for the chosen bandwidth."""
    def produce(ct_session, p):
        bandwidth = request.args.get('bandwidth', type=int)
        if bandwidth not in GAP_KERNEL_BANDWIDTHS_M:
            bandwidth = 2500
        data_key, data = _gap_data(ct_session, p)
        return jsonify(_cache_get(('kernel', data_key, bandwidth),
                                  lambda: ga.build_kernel(data, bandwidth)))
    return _json_endpoint('kernel', produce)


@camera_traps_bp.route('/api/gap-analysis/biotopes')
@login_required
def api_gap_biotopes(lang_code):
    """Effort and completeness pooled by biotope (gaps in environmental space)."""
    def produce(ct_session, p):
        data_key, data = _gap_data(ct_session, p)
        rows = _cache_get(('biotopes', data_key, p['unit'], g.lang_code),
                          lambda: ga.biotope_summary(data, p['unit'], g.lang_code))
        return jsonify({'rows': rows})
    return _json_endpoint('biotopes', produce)
