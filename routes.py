# SPDX-License-Identifier: AGPL-3.0-only
from flask import render_template, g, flash, redirect, url_for, jsonify, request, current_app, send_from_directory, abort, Response
from datetime import datetime, date, timedelta
from dateutil.relativedelta import relativedelta
from flask_login import login_required, current_user
from app.camera_traps.domain import _
from sqlalchemy import func, distinct, extract, select, text, or_, case
import io
import csv
import os
from functools import lru_cache

from . import camera_traps_bp
from .forms import UploadForm, IdentificationForm
from .background_tasks import cleanup_old_photos
from .utils import process_photo_batch, check_consensus_for_observation, calculate_total_effort, get_institution_filter
from .database import get_ct_session, close_ct_session
from .models import Location, Species, Photo, Observation, Identification, BehaviorType, Biotope, SpeciesYearlyTrend, LocationMonthlyActivity
from .models import ServiceVisit, BatteryType, VisitPurpose, LocationStats, location_institutions, identification_behaviors
from .models import Deployment
from app.models import User, Institution
from .decorators import role_required
from .data_export import get_ct_occurrence_data
from .daily_analytics import fetch_raw_daily_data, calculate_activity_curve, generate_csv_export, calculate_overlap_matrix
from .activity_heatmap import fetch_heatmap_data, fetch_date_range

#
# --- MODULE STATIC FILES ---
#
@camera_traps_bp.route('/ct-static/<path:filename>')
def serve_ct_static(lang_code, filename):
    static_dir = os.path.join(os.path.dirname(__file__), 'static')
    return send_from_directory(static_dir, filename)


#
# --- MODULE START PAGE (card hub) ---
#
@camera_traps_bp.route('/')
def overview(lang_code):
    """Module start page with entry cards for all sections."""
    # Cards are rendered based on the user's role
    if current_user.is_authenticated:
        can_identify = current_user.has_role('ct_verifier')
        can_upload   = current_user.has_role('manager')
        can_manage   = current_user.has_role('manager') or current_user.has_role('admin')
        can_export   = current_user.has_role('analyst')
        is_admin     = current_user.has_role('admin')
    else:
        can_identify = can_upload = can_manage = can_export = is_admin = False

    return render_template(
        'overview.html',
        can_identify=can_identify,
        can_upload=can_upload,
        can_manage=can_manage,
        can_export=can_export,
        is_admin=is_admin,
    )


#
# --- ADMIN PANEL (technical actions) ---
#
@camera_traps_bp.route('/admin')
@login_required
@role_required('admin')
def admin_panel(lang_code):
    """Admin panel page: recalculate analytics, clean photos, etc."""
    from .ai_runner import (
        is_ai_available, get_recent_requests, get_active_model,
        get_classification_stats,
    )

    ai_available   = is_ai_available()
    ai_max_per_run = (
        current_app.config.get('CAMERA_TRAP_CONFIG', {})
        .get('AI_RUNNER', {})
        .get('MAX_PER_RUN', 100)
    )

    ai_recent = []
    ai_model  = None
    ai_stats  = {'classified': 0, 'remaining': 0}
    if ai_available:
        try:
            ai_recent = get_recent_requests(limit=5)
            ai_model  = get_active_model()
            ai_stats  = get_classification_stats()
        except Exception as e:
            current_app.logger.warning(f"AI: cannot load admin status: {e}")
        finally:
            close_ct_session()

    # Storage and batch health metrics (both functions manage their own ct-session
    # and close it in their own finally; get_cleanup_statistics may raise an
    # exception, get_batch_statistics returns {} — catching both cases).
    storage_stats = {}
    batch_stats = {}
    try:
        from .background_tasks import get_cleanup_statistics, get_batch_statistics
        storage_stats = get_cleanup_statistics() or {}
        batch_stats = get_batch_statistics() or {}
    except Exception as e:
        current_app.logger.warning(f"CT admin: cannot load storage/batch stats: {e}")

    # Counter of series flagged for re-review (Idea 6) — badge.
    flagged_count = 0
    ct_session = get_ct_session()
    try:
        flagged_count = ct_session.query(func.count(Observation.id)).filter(
            Observation.flagged.is_(True)
        ).scalar() or 0
    except Exception as e:
        current_app.logger.warning(f"CT admin: cannot load flagged count: {e}")
    finally:
        close_ct_session()

    return render_template(
        'admin.html',
        ai_available=ai_available,
        ai_max_per_run=ai_max_per_run,
        ai_recent=ai_recent,
        ai_model=ai_model,
        ai_stats=ai_stats,
        storage_stats=storage_stats,
        batch_stats=batch_stats,
        flagged_count=flagged_count,
    )


@camera_traps_bp.route('/observation/<int:obs_id>/flag', methods=['POST'])
@login_required
@role_required('ct_verifier')
def flag_observation(lang_code, obs_id):
    """Flag a series for re-review (Idea 6). note is optional."""
    ct_session = get_ct_session()
    try:
        obs = ct_session.query(Observation).get(obs_id)
        if not obs:
            flash(_('Серію не знайдено.'), 'danger')
        else:
            obs.flagged = True
            obs.flag_note = (request.form.get('note') or '').strip() or None
            ct_session.commit()
            flash(_('Серію позначено на повторний розгляд.'), 'success')
    except Exception as e:
        ct_session.rollback()
        current_app.logger.error(f"Error flagging observation {obs_id}: {e}")
        flash(_('Помилка позначення серії.'), 'danger')
    finally:
        close_ct_session()
    return redirect(request.referrer or url_for('camera_traps.dashboard', lang_code=g.lang_code))


@camera_traps_bp.route('/observation/<int:obs_id>/unflag', methods=['POST'])
@login_required
@role_required('ct_verifier')
def unflag_observation(lang_code, obs_id):
    """Remove the re-review flag (Idea 6)."""
    ct_session = get_ct_session()
    try:
        obs = ct_session.query(Observation).get(obs_id)
        if not obs:
            flash(_('Серію не знайдено.'), 'danger')
        else:
            obs.flagged = False
            obs.flag_note = None
            ct_session.commit()
            flash(_('Позначку знято.'), 'success')
    except Exception as e:
        ct_session.rollback()
        current_app.logger.error(f"Error unflagging observation {obs_id}: {e}")
        flash(_('Помилка зняття позначки.'), 'danger')
    finally:
        close_ct_session()
    return redirect(request.referrer or url_for('camera_traps.dashboard', lang_code=g.lang_code))


@camera_traps_bp.route('/admin/flagged')
@login_required
@role_required('admin')
def admin_flagged_list(lang_code):
    """List of series flagged for re-review (Idea 6, admin)."""
    ct_session = get_ct_session()
    try:
        flagged = (
            ct_session.query(Observation)
            .filter(Observation.flagged.is_(True))
            .order_by(Observation.series_start_time.desc())
            .all()
        )
        items = []
        for obs in flagged:
            first_photo = (
                ct_session.query(Photo)
                .filter(Photo.observation_id == obs.id)
                .order_by(Photo.captured_at)
                .first()
            )
            items.append({
                'id': obs.id,
                'location_name': obs.location.name if obs.location else '',
                'series_start_time': obs.series_start_time,
                'flag_note': obs.flag_note,
                'thumb': first_photo.system_filename if first_photo else None,
            })
        return render_template('flagged_list.html', items=items)
    finally:
        close_ct_session()


@camera_traps_bp.route('/admin/ai/accuracy')
@login_required
@role_required('admin')
def ai_calibration(lang_code):
    """AI calibration dashboard: per-species accuracy on verified series.

    Based on ai_predictions.was_correct (Idea 4), recorded at the time of
    consensus. ?min=N — minimum sample size per species (default 1, because
    verified AI series are still scarce; reliability is visible in the 'sample'
    column).
    """
    from .database import get_ct_engine

    min_samples = request.args.get('min', default=1, type=int)
    if min_samples < 1:
        min_samples = 1

    rows_data = []
    try:
        engine = get_ct_engine()
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT p.prediction_species_id            AS species_id,
                       s.scientific_name,
                       s.common_name_ua,
                       s.common_name_en,
                       COUNT(*)                            AS total,
                       COUNT(*) FILTER (WHERE p.was_correct) AS correct,
                       AVG(p.prediction_score) FILTER (WHERE p.was_correct)        AS mean_score_correct,
                       AVG(p.prediction_score) FILTER (WHERE p.was_correct = FALSE) AS mean_score_wrong
                FROM ai_predictions p
                JOIN species s ON s.id = p.prediction_species_id
                WHERE p.was_correct IS NOT NULL
                GROUP BY p.prediction_species_id, s.scientific_name,
                         s.common_name_ua, s.common_name_en
                HAVING COUNT(*) >= :min_samples
                ORDER BY total DESC, correct DESC
            """), {'min_samples': min_samples}).mappings().fetchall()

        for r in rows:
            total = r['total'] or 0
            correct = r['correct'] or 0
            name = r['scientific_name']
            if lang_code == 'uk' and r['common_name_ua']:
                name = f"{r['common_name_ua']} ({r['scientific_name']})"
            elif lang_code == 'en' and r['common_name_en']:
                name = f"{r['common_name_en']} ({r['scientific_name']})"
            rows_data.append({
                'species_name': name,
                'total': total,
                'correct': correct,
                'accuracy': round(correct / total * 100, 1) if total else 0.0,
                'mean_score_correct': (round(r['mean_score_correct'], 3)
                                       if r['mean_score_correct'] is not None else None),
                'mean_score_wrong': (round(r['mean_score_wrong'], 3)
                                     if r['mean_score_wrong'] is not None else None),
            })
    except Exception as e:
        current_app.logger.warning(f"AI calibration query failed: {e}")

    return render_template('ai_calibration.html',
                           rows=rows_data, min_samples=min_samples)


@camera_traps_bp.route('/admin/ai/run', methods=['POST'])
@login_required
@role_required('admin')
def admin_ai_run(lang_code):
    """Create a request in ai_run_queue. The worker (cron) will pick it up within 2–3 min."""
    from .ai_runner import is_ai_available, request_run

    if not is_ai_available():
        flash(_('AI-класифікатор не доступний.'), 'danger')
        return redirect(url_for('camera_traps.admin_panel', lang_code=g.lang_code))

    # Validate N
    try:
        n = int(request.form.get('n_observations', 100))
    except (TypeError, ValueError):
        flash(_('Некоректна кількість серій.'), 'danger')
        return redirect(url_for('camera_traps.admin_panel', lang_code=g.lang_code))

    max_per_run = (
        current_app.config.get('CAMERA_TRAP_CONFIG', {})
        .get('AI_RUNNER', {})
        .get('MAX_PER_RUN', 100)
    )
    upper_bound = max_per_run * 5   # up to 5× the nightly limit for a single run
    if not (1 <= n <= upper_bound):
        flash(
            _('Кількість серій має бути від 1 до %(max)d.') % {'max': upper_bound},
            'danger',
        )
        return redirect(url_for('camera_traps.admin_panel', lang_code=g.lang_code))

    try:
        req = request_run(user_id=current_user.id, n_observations=n)
        flash(
            _('Запит №%(id)d додано в чергу. Worker оброблятиме до %(n)d серій. '
              'Перевір статус нижче через 1-3 хвилини.') % {'id': req.id, 'n': n},
            'success',
        )
    except Exception as e:
        current_app.logger.error(f"AI: failed to create run request: {e}", exc_info=True)
        flash(_('Помилка створення запиту. Перевір логи сервера.'), 'danger')
    finally:
        close_ct_session()

    return redirect(url_for('camera_traps.admin_panel', lang_code=g.lang_code))


#
# --- ANALYTICS DASHBOARD ---
#
@camera_traps_bp.route('/dashboard')
def dashboard(lang_code):
    """Render the dashboard with main statistics, FILTERED BY DATE, LOCATIONS AND BIOTOPES."""
    ct_session = get_ct_session()
    try:

        # Dates
        start_date_str = request.args.get('start_date', '2020-08-01')
        end_date_str = request.args.get('end_date', date.today().strftime('%Y-%m-%d'))
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            start_date_str, end_date_str = '2020-08-01', date.today().strftime('%Y-%m-%d')
            start_date, end_date = datetime.strptime(start_date_str, '%Y-%m-%d').date(), date.today()
        
        # Locations and Biotopes
        location_ids_str = request.args.get('locations', '')
        biotope_ids_str_list = request.args.getlist('biotopes')
        # Convert lists of strings to lists of integers
        biotope_ids = [int(id) for id in biotope_ids_str_list if id.isdigit()]
        location_ids = [int(id) for id in location_ids_str.split(',') if id.isdigit()]
        
        # Fetch biotope list to pass to the template
        biotopes_list = ct_session.query(Biotope).order_by(Biotope.name_ua).all()

        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else[]
        is_admin = current_user.is_authenticated and current_user.has_role('admin')

        # Combined "Institution / Ecoregion" filter (same as on the species-dashboard).
        institutions_list = get_accessible_institutions(is_admin)
        ecoregions = build_ecoregions(institutions_list, g.lang_code)
        selected_scope, selected_inst_ids = resolve_scope(
            request.args.get('scope', ''), institutions_list,
            current_app.config['CAMERA_TRAP_CONFIG'].get('CT_DEFAULT_SCOPE', ''))

        # Pass table_alias='locations' (no .replace needed)
        inst_condition, inst_params = get_institution_filter(
            user_inst_ids, is_admin, selected_inst_id=selected_inst_ids, table_alias='locations'
        )
        inst_condition_orm = text(inst_condition)

        # Resolved institution IDs for the map/chart API (JS passes them as institution_id).
        effective_inst_ids = [i for i in (selected_inst_ids or []) if i > 0]

        # Total Photos
        query_photos = ct_session.query(func.count(Photo.id)).join(Observation).join(Location)\
            .filter(Photo.captured_at.between(start_date, end_date))\
            .filter(inst_condition_orm).params(**inst_params)
        if location_ids:
            query_photos = query_photos.filter(Location.id.in_(location_ids))
        if biotope_ids:
            query_photos = query_photos.join(Location.biotopes).filter(Biotope.id.in_(biotope_ids))
        total_photos = query_photos.scalar() or 0

        # Total Locations
        query_locations = ct_session.query(func.count(distinct(Observation.location_id))).join(Photo).join(Location)\
            .filter(Photo.captured_at.between(start_date, end_date))\
            .filter(inst_condition_orm).params(**inst_params)
        if location_ids:
            query_locations = query_locations.filter(Location.id.in_(location_ids))
        if biotope_ids:
            query_locations = query_locations.join(Location.biotopes).filter(Biotope.id.in_(biotope_ids))
        total_locations = query_locations.scalar() or 0
        
        # Total Observations
        query_observations = ct_session.query(func.count(func.distinct(Observation.id))).join(Photo)\
            .join(Identification, Photo.id == Identification.photo_id).join(Location)\
            .filter(Photo.captured_at.between(start_date, end_date), Identification.species_id > 0)\
            .filter(inst_condition_orm).params(**inst_params)
        if location_ids:
            query_observations = query_observations.filter(Location.id.in_(location_ids))
        if biotope_ids:
            query_observations = query_observations.join(Location.biotopes).filter(Biotope.id.in_(biotope_ids))
        total_observations = query_observations.scalar() or 0

        # Identified Species Count
        query_species_count = ct_session.query(func.count(distinct(Identification.species_id)))\
            .join(Photo, Identification.photo_id == Photo.id)\
            .join(Observation, Photo.observation_id == Observation.id).join(Location)\
            .filter(Identification.species_id > 0, Photo.captured_at.between(start_date, end_date), Observation.status.in_(['completed', 'archived']))\
            .filter(inst_condition_orm).params(**inst_params)
        if location_ids:
            query_species_count = query_species_count.filter(Location.id.in_(location_ids))
        if biotope_ids:
            query_species_count = query_species_count.join(Location.biotopes).filter(Biotope.id.in_(biotope_ids))
        identified_species_count = query_species_count.scalar() or 0

        # Pending Observations
        query_pending = ct_session.query(func.count(Observation.id)).join(Location)\
            .filter(Observation.series_start_time.between(start_date, end_date + timedelta(days=1)), ~Observation.photos.any(Photo.identifications.any()))\
            .filter(inst_condition_orm).params(**inst_params)
        if location_ids:
            query_pending = query_pending.filter(Location.id.in_(location_ids))
        if biotope_ids:
            query_pending = query_pending.join(Location.biotopes).filter(Biotope.id.in_(biotope_ids))
        pending_observations = query_pending.scalar() or 0

        # Unique Capture Days
        query_capture_days = ct_session.query(func.count(func.distinct(func.date(Photo.captured_at))))\
            .join(Observation).join(Location).filter(Photo.captured_at.between(start_date, end_date))\
            .filter(inst_condition_orm).params(**inst_params)
        if location_ids:
            query_capture_days = query_capture_days.filter(Location.id.in_(location_ids))
        if biotope_ids:
            query_capture_days = query_capture_days.join(Location.biotopes).filter(Biotope.id.in_(biotope_ids))
        unique_capture_days = query_capture_days.scalar() or 0

        # Top Contributors
        top_contributors_raw_query = ct_session.query(
            Identification.user_id,
            func.count(distinct(Photo.observation_id)).label('observation_count')
        ).join(Photo, Identification.photo_id == Photo.id).join(Observation).join(Location)\
        .filter(Photo.captured_at.between(start_date, end_date))\
        .filter(inst_condition_orm).params(**inst_params)
        if location_ids:
            top_contributors_raw_query = top_contributors_raw_query.filter(Location.id.in_(location_ids))
        if biotope_ids:
            top_contributors_raw_query = top_contributors_raw_query.join(Location.biotopes).filter(Biotope.id.in_(biotope_ids))
        
        # Top Contributors
        top_contributors_raw_query = ct_session.query(
            Identification.user_id,
            func.count(distinct(Photo.observation_id)).label('observation_count')
        ).join(Photo, Identification.photo_id == Photo.id).join(Observation).join(Location)\
        .filter(Photo.captured_at.between(start_date, end_date))
        if location_ids:
            top_contributors_raw_query = top_contributors_raw_query.filter(Location.id.in_(location_ids))
        if biotope_ids:
            top_contributors_raw_query = top_contributors_raw_query.join(Location.biotopes).filter(Biotope.id.in_(biotope_ids))
        
        top_contributors_raw = top_contributors_raw_query.group_by(Identification.user_id)\
            .order_by(func.count(distinct(Photo.observation_id)).desc()).limit(10).all()
        # --- END OF DB QUERIES ---

        top_contributors = []
        if top_contributors_raw:
            user_ids = [item.user_id for item in top_contributors_raw]
            users = User.query.filter(User.id.in_(user_ids)).all()
            user_map = {user.id: user.username for user in users}
            for item in top_contributors_raw:
                top_contributors.append({
                    'username': user_map.get(item.user_id, f"Користувач (ID: {item.user_id})"),
                    'observation_count': item.observation_count
                })
        
        stats = {
            'total_photos': total_photos,
            'total_locations': total_locations,
            'total_observations': total_observations,
            'identified_species_count': identified_species_count,
            'pending_observations': pending_observations,
            'unique_capture_days': unique_capture_days,
            'top_contributors': top_contributors
        }
        
        return render_template('dashboard.html',
                             stats=stats,
                             start_date=start_date_str,
                             end_date=end_date_str,
                             biotopes=biotopes_list,
                             selected_locations=location_ids_str,
                             selected_biotopes=biotope_ids,
                             institutions=institutions_list,
                             ecoregions=ecoregions,
                             selected_scope=selected_scope,
                             effective_inst_ids=effective_inst_ids,
                             is_admin=is_admin)

    except Exception as e:
        current_app.logger.error(f"Error in dashboard: {str(e)}")
        stats = {'total_photos': 0, 'total_locations': 0, 'total_identifications': 0, 'identified_species_count': 0, 'top_contributors': []}
        flash(_('Помилка завантаження статистики.'), 'warning')
        return render_template('dashboard.html', stats=stats, start_date='2020-08-01', end_date=date.today().strftime('%Y-%m-%d'), biotopes=[], selected_locations='', selected_biotopes=[], institutions=[], ecoregions={}, selected_scope='global:', effective_inst_ids=[], is_admin=False)
    finally:
        close_ct_session()


#
# --- SHARED "INSTITUTION / ECOREGION" FILTER HELPERS ---
#
def get_accessible_institutions(is_admin):
    """Institutions within access scope: admin — all; authenticated — own; anonymous — none."""
    if is_admin:
        return Institution.query.order_by(Institution.name_uk).all()
    if current_user.is_authenticated:
        return sorted(current_user.institutions, key=lambda i: i.name_uk or '')
    return []


def build_ecoregions(institutions, lang):
    """{ecoregion_uk: localised_name} for the given institutions."""
    ecoregions = {}
    for inst in institutions:
        if inst.ecoregion_uk:
            display = inst.ecoregion_uk if lang != 'en' else (inst.ecoregion_en or inst.ecoregion_uk)
            ecoregions[inst.ecoregion_uk] = display
    return ecoregions


def resolve_scope(scope_arg, accessible_institutions, default_scope=''):
    """
    Parse the combined `scope` filter ('institution:<id>' | 'ecoregion:<key>'
    | 'global:') into a set of institutions for get_institution_filter().

    Returns (normalized_scope, selected_inst_ids):
      - global         → (None) — no additional narrowing (all accessible);
      - institution:id → [id];
      - ecoregion:key  → list of institutions in that ecoregion (within access),
                         or [-1] if none (guaranteed empty result).

    default_scope (#49): scope applied when scope_arg is empty
    (value from CAMERA_TRAP_CONFIG['CT_DEFAULT_SCOPE']). An ecoregion default
    is ignored if it is not accessible to the user (silent fallback to global)
    so an empty page is never shown. An explicit scope_arg always takes priority.
    """
    scope_arg = (scope_arg or '').strip()
    # #49: when the URL has no scope — apply the default from config (CT_DEFAULT_SCOPE).
    # Use the ecoregion default only if it is accessible to the user; otherwise silently
    # fall back to "all" (global) so an empty page is never shown.
    if not scope_arg and default_scope:
        ds = (default_scope or '').strip()
        if ds.startswith('ecoregion:'):
            key = ds.split(':', 1)[1]
            if any(i.ecoregion_uk == key for i in accessible_institutions):
                scope_arg = ds
        elif ds:
            scope_arg = ds
    if ':' in scope_arg:
        scope_type, scope_id = scope_arg.split(':', 1)
    else:
        scope_type, scope_id = 'global', ''

    if scope_type == 'institution' and scope_id.isdigit():
        return f'institution:{scope_id}', [int(scope_id)]
    if scope_type == 'ecoregion' and scope_id:
        ids = [i.id for i in accessible_institutions if i.ecoregion_uk == scope_id]
        return f'ecoregion:{scope_id}', (ids or [-1])
    return 'global:', None


#
# --- FULL CONTRIBUTORS LIST ---
#
def query_contributor_stats(ct_session, today, inst_condition_orm, inst_params,
                            location_ids=None, biotope_ids=None, species_id=None):
    """
    Return contribution statistics per user with rolling windows anchored to
    `today`. Metric — number of unique observations (distinct observation_id)
    in which the user made an identification.

    IMPORTANT: windows are counted by IDENTIFICATION TIME (`Identification.created_at`,
    when the user processed the series), NOT by photo capture time
    (`Photo.captured_at`, when the camera recorded the animal — that may be long ago).

    Windows (rolling from `today`):
      - d_today : today
      - d_week  : last 7 days
      - d_month : last calendar month
      - d_year  : last calendar year
      - total   : all time

    Access is filtered via `inst_condition_orm`/`inst_params`
    (result of get_institution_filter), so the function does not resolve
    permissions itself. Returns a list of Rows sorted by total desc.

    Optional `species_id` narrows results to identifications of that species only.
    """
    start_today = datetime.combine(today, datetime.min.time())
    start_week = datetime.combine(today - timedelta(days=6), datetime.min.time())
    start_month = datetime.combine(today - relativedelta(months=1), datetime.min.time())
    start_year = datetime.combine(today - relativedelta(years=1), datetime.min.time())

    def _window(threshold):
        return func.count(distinct(
            case((Identification.created_at >= threshold, Photo.observation_id))
        ))

    query = ct_session.query(
        Identification.user_id,
        _window(start_today).label('d_today'),
        _window(start_week).label('d_week'),
        _window(start_month).label('d_month'),
        _window(start_year).label('d_year'),
        func.count(distinct(Photo.observation_id)).label('total'),
    ).join(Photo, Identification.photo_id == Photo.id)\
        .join(Observation).join(Location)\
        .filter(inst_condition_orm).params(**inst_params)

    if location_ids:
        query = query.filter(Location.id.in_(location_ids))
    if biotope_ids:
        query = query.join(Location.biotopes).filter(Biotope.id.in_(biotope_ids))
    if species_id:
        query = query.filter(Identification.species_id == species_id)

    return query.group_by(Identification.user_id)\
        .order_by(func.count(distinct(Photo.observation_id)).desc())\
        .all()


@camera_traps_bp.route('/contributors')
def contributors(lang_code):
    """
    Full list of contributors with contribution statistics for rolling periods
    (today / week / month / year / all time).

    Visibility:
      - anonymous / regular user — username + statistics only;
      - manager / admin — full name (User.full_name).
    Combined `scope` filter ('institution:<id>' | 'ecoregion:<key>' | 'global:')
    is limited to the user's access.
    """
    ct_session = get_ct_session()
    try:
        today = date.today()

        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        is_manager = current_user.is_authenticated and current_user.has_role('manager')
        user_inst_ids = [inst.id for inst in current_user.institutions] \
            if current_user.is_authenticated else []

        accessible_institutions = get_accessible_institutions(is_admin)
        ecoregions = build_ecoregions(accessible_institutions, g.lang_code)

        selected_scope, selected_inst_ids = resolve_scope(
            request.args.get('scope', ''), accessible_institutions,
            current_app.config['CAMERA_TRAP_CONFIG'].get('CT_DEFAULT_SCOPE', ''))

        inst_condition, inst_params = get_institution_filter(
            user_inst_ids, is_admin, selected_inst_id=selected_inst_ids, table_alias='locations'
        )
        inst_condition_orm = text(inst_condition)

        # --- Species filter ---
        selected_species_id = request.args.get('species_id', type=int)

        # Build species list (same pattern as species_dashboard, routes.py line ~730)
        species_list = []
        species_q = ct_session.query(Species)\
            .join(Identification, Species.id == Identification.species_id)\
            .join(Photo, Identification.photo_id == Photo.id)\
            .join(Observation, Photo.observation_id == Observation.id)\
            .join(Location, Observation.location_id == Location.id)\
            .filter(
                inst_condition_orm,
                Identification.species_id > 0,
            ).params(**inst_params)\
            .distinct()\
            .order_by(Species.common_name_ua)
        for s in species_q:
            display_name = s.scientific_name
            if g.lang_code == 'uk' and s.common_name_ua:
                display_name = f"{s.common_name_ua} ({s.scientific_name})"
            elif g.lang_code == 'en' and s.common_name_en:
                display_name = f"{s.common_name_en} ({s.scientific_name})"
            species_list.append({'id': s.id, 'text': display_name})

        rows = query_contributor_stats(
            ct_session, today, inst_condition_orm, inst_params,
            species_id=selected_species_id,
        )

        # --- Fetch names from the main database ---
        contributors = []
        if rows:
            user_ids = [r.user_id for r in rows]
            users = User.query.filter(User.id.in_(user_ids)).all()
            if is_manager:
                name_map = {u.id: u.full_name for u in users}
            else:
                name_map = {u.id: u.username for u in users}
            for r in rows:
                contributors.append({
                    'name': name_map.get(r.user_id, f"Користувач (ID: {r.user_id})"),
                    'd_today': r.d_today or 0,
                    'd_week': r.d_week or 0,
                    'd_month': r.d_month or 0,
                    'd_year': r.d_year or 0,
                    'total': r.total or 0,
                })

        return render_template(
            'contributors.html',
            contributors=contributors,
            institutions=accessible_institutions,
            ecoregions=ecoregions,
            selected_scope=selected_scope,
            is_admin=is_admin,
            show_full_name=is_manager,
            available_species=species_list,
            selected_species_id=selected_species_id,
        )
    except Exception as e:
        current_app.logger.error(f"Error in contributors: {str(e)}")
        flash(_('Помилка завантаження списку учасників.'), 'warning')
        return render_template(
            'contributors.html', contributors=[], institutions=[],
            ecoregions={}, selected_scope='global:', is_admin=False,
            show_full_name=False, available_species=[], selected_species_id=None,
        )
    finally:
        close_ct_session()


#
# --- SPECIES DETAIL ANALYSIS PAGE ---
#
@camera_traps_bp.route('/analysis/species-dashboard')
def species_dashboard(lang_code):
    """Species trend detail analysis page with institution filtering."""
    ct_session = get_ct_session()
    try:
        MIN_OBSERVATIONS = 30
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []

        # Species list with a minimum number of observations
        species_q = ct_session.query(Identification.species_id)\
            .join(Photo, Identification.photo_id == Photo.id)\
            .join(Observation, Photo.observation_id == Observation.id)\
            .filter(Observation.status.in_(['completed', 'archived']),
                    Identification.species_id > 0)

        if not is_admin and user_inst_ids:
            allowed_locs = select(location_institutions.c.location_id).where(
                location_institutions.c.institution_id.in_(user_inst_ids))
            species_q = species_q.filter(Observation.location_id.in_(allowed_locs))

        species_subq = species_q.group_by(Identification.species_id)\
            .having(func.count(distinct(Photo.observation_id)) >= MIN_OBSERVATIONS)\
            .subquery()

        species_query = ct_session.query(Species)\
            .join(species_subq, Species.id == species_subq.c.species_id)\
            .order_by(Species.common_name_ua).all()

        species_list = []
        for s in species_query:
            display_name = s.scientific_name
            if g.lang_code == 'uk' and s.common_name_ua:
                display_name = f"{s.common_name_ua} ({s.scientific_name})"
            elif g.lang_code == 'en' and s.common_name_en:
                display_name = f"{s.common_name_en} ({s.scientific_name})"
            species_list.append({'id': s.id, 'text': display_name})

        all_years = ct_session.query(SpeciesYearlyTrend.year)\
            .filter(SpeciesYearlyTrend.scope_type == 'global')\
            .distinct().order_by(SpeciesYearlyTrend.year).all()
        available_years = [y[0] for y in all_years]
        start_year = available_years[0] if available_years else date.today().year - 5
        end_year = available_years[-1] if available_years else date.today().year

        # Institutions and ecoregions for the filter
        if is_admin:
            institutions = Institution.query.order_by(Institution.name_uk).all()
        elif current_user.is_authenticated:
            institutions = sorted(current_user.institutions,
                                  key=lambda i: i.name_uk or '')
        else:
            institutions = []

        lang = g.lang_code
        ecoregions = {}
        for inst in institutions:
            if inst.ecoregion_uk:
                display = inst.ecoregion_uk if lang != 'en' else (inst.ecoregion_en or inst.ecoregion_uk)
                ecoregions[inst.ecoregion_uk] = display

        return render_template('species_dashboard.html',
                               available_species=species_list,
                               available_years=available_years,
                               start_year=start_year,
                               end_year=end_year,
                               institutions=institutions,
                               ecoregions=ecoregions,
                               is_admin=is_admin)
    except Exception:
        current_app.logger.error("Error loading species dashboard", exc_info=True)
        flash(_("Помилка завантаження сторінки аналізу."), 'danger')
        return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))
    finally:
        close_ct_session()
#
# --- TWO-REGION COMPARISON PAGE ---
#
@camera_traps_bp.route('/analysis/comparison')
def comparison_dashboard(lang_code):
    """Statistics comparison page for two regions/institutions."""
    ct_session = get_ct_session()
    try:
        is_admin = current_user.is_authenticated and current_user.has_role('admin')

        if is_admin:
            institutions = Institution.query.order_by(Institution.name_uk).all()
        elif current_user.is_authenticated:
            institutions = sorted(current_user.institutions, key=lambda i: i.name_uk or '')
        else:
            institutions = []

        lang = g.lang_code
        ecoregions = {}
        for inst in institutions:
            if inst.ecoregion_uk:
                display = inst.ecoregion_uk if lang != 'en' else (inst.ecoregion_en or inst.ecoregion_uk)
                ecoregions[inst.ecoregion_uk] = display

        biotopes_list = ct_session.query(Biotope).order_by(Biotope.name_ua).all()

        return render_template('comparison.html',
                               institutions=institutions,
                               ecoregions=ecoregions,
                               biotopes=biotopes_list,
                               is_admin=is_admin,
                               start_date='2020-08-01',
                               end_date=date.today().strftime('%Y-%m-%d'))
    except Exception:
        current_app.logger.error("Error loading comparison dashboard", exc_info=True)
        flash(_("Помилка завантаження сторінки порівняння."), 'danger')
        return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))
    finally:
        close_ct_session()

#
# --- BEHAVIOURAL ANALYSIS PAGE ---
#
@camera_traps_bp.route('/analysis/behavior')
def behavior_analysis(lang_code):
    """Behaviour tag analysis page. Accessible to all."""
    ct_session = get_ct_session()
    try:
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        if is_admin:
            institutions = Institution.query.order_by(Institution.name_uk).all()
        elif current_user.is_authenticated:
            institutions = sorted(current_user.institutions, key=lambda i: i.name_uk or '')
        else:
            institutions = []

        lang = g.lang_code
        ecoregions = {}
        for inst in institutions:
            if inst.ecoregion_uk:
                display = inst.ecoregion_uk if lang != 'en' else (inst.ecoregion_en or inst.ecoregion_uk)
                ecoregions[inst.ecoregion_uk] = display

        biotopes_list = ct_session.query(Biotope).order_by(Biotope.name_ua).all()

        # Manager = authenticated user with at least one institution
        is_manager = current_user.is_authenticated and bool(current_user.institutions)

        # Only species with at least one behaviour tag
        species_q = (
            ct_session.query(Species)
            .join(Identification, Identification.species_id == Species.id)
            .join(identification_behaviors,
                  identification_behaviors.c.identification_id == Identification.id)
            .filter(Species.is_active == True)
        )
        # For regular users (not admin, not manager) hide
        # "technical" species (motorcycle, car, person, etc.) — their id < 0
        if not (is_admin or is_manager):
            species_q = species_q.filter(Species.id > 0)

        species_with_behaviors = (
            species_q.distinct().order_by(Species.common_name_ua).all()
        )
        species_list = []
        for s in species_with_behaviors:
            display = s.common_name_ua if lang != 'en' else (s.common_name_en or s.common_name_ua)
            if s.scientific_name:
                display = f"{display} ({s.scientific_name})"
            species_list.append({'id': s.id, 'text': display})

        return render_template(
            'behavior_analysis.html',
            species_list=species_list,
            biotopes=biotopes_list,
            institutions=institutions,
            ecoregions=ecoregions,
            is_admin=is_admin,
            start_date='2020-08-01',
            end_date=date.today().strftime('%Y-%m-%d'),
        )
    except Exception:
        current_app.logger.error("Error loading behavior analysis", exc_info=True)
        flash(_("Помилка завантаження сторінки аналізу поведінки."), 'danger')
        return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))
    finally:
        close_ct_session()


@camera_traps_bp.route('/api/behavior/data')
def api_behavior_data(lang_code):
    """API: all data for the three behaviour charts in a single request."""
    ct_session = get_ct_session()
    try:
        species_id = request.args.get('species_id', type=int)
        if not species_id:
            return jsonify({'error': 'species_id required'}), 400

        start_date_str = request.args.get('start_date', '2020-08-01')
        end_date_str   = request.args.get('end_date', date.today().strftime('%Y-%m-%d'))
        try:
            start_dt = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_dt   = datetime.strptime(end_date_str,   '%Y-%m-%d').date()
        except ValueError:
            start_dt = date(2020, 8, 1)
            end_dt   = date.today()

        # Institution filter
        raw_inst = request.args.get('institution_id', '').split(',')
        selected_inst_ids = [int(i) for i in raw_inst if i.strip().isdigit()]

        ecoregion = request.args.get('ecoregion', '').strip()
        if ecoregion and not selected_inst_ids:
            eco_insts = Institution.query.filter_by(ecoregion_uk=ecoregion).all()
            selected_inst_ids = [i.id for i in eco_insts]

        is_admin    = current_user.is_authenticated and current_user.has_role('admin')
        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []

        inst_condition, inst_params = get_institution_filter(
            user_inst_ids, is_admin,
            selected_inst_id=selected_inst_ids,
            table_alias='locations'
        )

        raw_biotope = request.args.get('biotope_id', '')
        biotope_ids = [int(raw_biotope)] if raw_biotope.isdigit() else []

        # Base identification query
        base_q = (
            ct_session.query(Identification)
            .join(Photo, Photo.id == Identification.photo_id)
            .join(Observation, Observation.id == Photo.observation_id)
            .join(Location, Location.id == Observation.location_id)
            .filter(
                Identification.species_id == species_id,
                Observation.status.in_(['completed', 'archived']),
                Photo.captured_at.between(start_dt, end_dt),
                text(inst_condition),
            )
            .params(**inst_params)
        )
        if biotope_ids:
            base_q = base_q.join(Location.biotopes).filter(Biotope.id.in_(biotope_ids))

        identification_ids = [row.id for row in base_q.all()]

        if not identification_ids:
            return jsonify({
                'behavior_distribution': [],
                'seasonal_behaviors': [],
                'group_size_histogram': [],
                'total_identifications': 0,
            })

        lang = g.lang_code

        # Chart 1: behaviour distribution
        behavior_counts = (
            ct_session.query(
                BehaviorType.id,
                BehaviorType.name_ua,
                BehaviorType.name_en,
                func.count(func.distinct(Photo.observation_id)).label('obs_count')
            )
            .join(identification_behaviors,
                  identification_behaviors.c.behavior_type_id == BehaviorType.id)
            .join(Identification,
                  Identification.id == identification_behaviors.c.identification_id)
            .join(Photo, Photo.id == Identification.photo_id)
            .filter(Identification.id.in_(identification_ids))
            .group_by(BehaviorType.id, BehaviorType.name_ua, BehaviorType.name_en)
            .order_by(func.count(func.distinct(Photo.observation_id)).desc())
            .all()
        )
        behavior_distribution = [
            {
                'behavior_id': row.id,
                'label': row.name_ua if lang != 'en' else (row.name_en or row.name_ua),
                'count': row.obs_count,
            }
            for row in behavior_counts
        ]

        # Chart 2: seasonal structure (by month)
        seasonal_rows = (
            ct_session.query(
                extract('month', Photo.captured_at).label('month'),
                BehaviorType.id.label('behavior_id'),
                BehaviorType.name_ua,
                BehaviorType.name_en,
                func.count(func.distinct(Photo.observation_id)).label('obs_count')
            )
            .join(identification_behaviors,
                  identification_behaviors.c.behavior_type_id == BehaviorType.id)
            .join(Identification,
                  Identification.id == identification_behaviors.c.identification_id)
            .join(Photo, Photo.id == Identification.photo_id)
            .filter(Identification.id.in_(identification_ids))
            .group_by(
                extract('month', Photo.captured_at),
                BehaviorType.id, BehaviorType.name_ua, BehaviorType.name_en,
            )
            .order_by('month', BehaviorType.id)
            .all()
        )
        seasonal_behaviors = [
            {
                'month': int(row.month),
                'behavior_id': row.behavior_id,
                'label': row.name_ua if lang != 'en' else (row.name_en or row.name_ua),
                'count': row.obs_count,
            }
            for row in seasonal_rows
        ]

        # Chart 3: individual-count histogram
        from collections import Counter
        qty_rows = (
            ct_session.query(
                func.max(Identification.quantity).label('qty'),
                Photo.observation_id
            )
            .join(Photo, Photo.id == Identification.photo_id)
            .filter(
                Identification.id.in_(identification_ids),
                Identification.quantity.isnot(None),
                Identification.quantity > 0,
            )
            .group_by(Photo.observation_id)
            .all()
        )
        qty_counter = Counter()
        for row in qty_rows:
            qty_counter[int(row.qty)] += 1
        group_size_histogram = [
            {'quantity': qty, 'frequency': freq}
            for qty, freq in sorted(qty_counter.items())
        ]

        # Number of identifications with no behaviour tag
        tagged_count = (
            ct_session.query(
                func.count(func.distinct(identification_behaviors.c.identification_id))
            )
            .filter(identification_behaviors.c.identification_id.in_(identification_ids))
            .scalar()
        ) or 0
        untagged_count = len(identification_ids) - tagged_count

        return jsonify({
            'behavior_distribution': behavior_distribution,
            'seasonal_behaviors': seasonal_behaviors,
            'group_size_histogram': group_size_histogram,
            'total_identifications': len(identification_ids),
            'untagged_count': untagged_count,
        })
    except Exception:
        current_app.logger.error("Error in api_behavior_data", exc_info=True)
        return jsonify({'error': 'Internal error'}), 500
    finally:
        close_ct_session()


@camera_traps_bp.route('/api/behavior/species-with-behaviors')
def api_behavior_species(lang_code):
    """API: list of species with at least one behaviour tag."""
    ct_session = get_ct_session()
    try:
        lang = g.lang_code
        rows = (
            ct_session.query(
                Species.id,
                Species.common_name_ua,
                Species.common_name_en,
                Species.scientific_name,
            )
            .join(Identification, Identification.species_id == Species.id)
            .join(identification_behaviors,
                  identification_behaviors.c.identification_id == Identification.id)
            .filter(Species.is_active == True)
            .distinct()
            .order_by(Species.common_name_ua)
            .all()
        )
        result = []
        for s in rows:
            display = s.common_name_ua if lang != 'en' else (s.common_name_en or s.common_name_ua)
            if s.scientific_name:
                display = f"{display} ({s.scientific_name})"
            result.append({'id': s.id, 'text': display})
        return jsonify(result)
    finally:
        close_ct_session()

#
# --- IDENTIFICATION PAGE ---
#

# Cache for species ranking
_species_ranking_cache = {
    'data': None,
    'timestamp': None,
    'ttl_hours': 24
}

def get_species_ranking():
    """Return a species ranking by identification frequency, with caching."""
    global _species_ranking_cache
    
    now = datetime.now()
    
    # Check whether the cache is still valid
    if (_species_ranking_cache['data'] is not None and
        _species_ranking_cache['timestamp'] is not None and
        (now - _species_ranking_cache['timestamp']).total_seconds() < _species_ranking_cache['ttl_hours'] * 3600):
        return _species_ranking_cache['data']

    # Refresh the cache
    ct_session = get_ct_session()
    try:
        # Count the number of identifications for each species
        species_counts = ct_session.query(
            Species.id,
            func.count(distinct(Observation.id)).label('observation_count')
        ).select_from(Species)\
         .join(Identification, Species.id == Identification.species_id)\
         .join(Photo, Identification.photo_id == Photo.id)\
         .join(Observation, Photo.observation_id == Observation.id)\
         .filter(
             Species.id > 0,
             Observation.status.in_(['completed', 'archived'])
        )\
         .group_by(Species.id)\
         .all()
        
        # Build a lookup dict for fast access
        ranking = {species_id: count for species_id, count in species_counts}

        # Store in cache
        _species_ranking_cache['data'] = ranking
        _species_ranking_cache['timestamp'] = now
        
        current_app.logger.info(f"Species ranking cache updated with {len(ranking)} species")
        return ranking
        
    except Exception as e:
        current_app.logger.error(f"Error updating species ranking cache: {e}")
        return {}
    finally:
        close_ct_session()

# Cache for behaviour-tag ranking (same pattern as species ranking)
_behavior_ranking_cache = {
    'data': None,
    'timestamp': None,
    'ttl_hours': 24
}

def get_behavior_ranking():
    """Behaviour-tag ranking by usage frequency (with caching)."""
    global _behavior_ranking_cache

    now = datetime.now()

    # Check whether the cache is still valid
    if (_behavior_ranking_cache['data'] is not None and
        _behavior_ranking_cache['timestamp'] is not None and
        (now - _behavior_ranking_cache['timestamp']).total_seconds() < _behavior_ranking_cache['ttl_hours'] * 3600):
        return _behavior_ranking_cache['data']

    # Refresh the cache
    ct_session = get_ct_session()
    try:
        # Number of series (observations) in which each tag was used
        behavior_counts = ct_session.query(
            BehaviorType.id,
            func.count(distinct(Observation.id)).label('observation_count')
        ).select_from(BehaviorType)\
         .join(identification_behaviors,
               identification_behaviors.c.behavior_type_id == BehaviorType.id)\
         .join(Identification,
               Identification.id == identification_behaviors.c.identification_id)\
         .join(Photo, Identification.photo_id == Photo.id)\
         .join(Observation, Photo.observation_id == Observation.id)\
         .filter(Observation.status.in_(['completed', 'archived']))\
         .group_by(BehaviorType.id)\
         .all()

        ranking = {behavior_id: count for behavior_id, count in behavior_counts}

        _behavior_ranking_cache['data'] = ranking
        _behavior_ranking_cache['timestamp'] = now

        current_app.logger.info(f"Behavior ranking cache updated with {len(ranking)} behaviors")
        return ranking

    except Exception as e:
        current_app.logger.error(f"Error updating behavior ranking cache: {e}")
        return {}
    finally:
        close_ct_session()

@camera_traps_bp.route('/identify', methods=['GET'])
@login_required
@role_required('ct_verifier')
def identify(lang_code):
    ct_session = get_ct_session()
    try:
        form = IdentificationForm()
        can_review = current_user.has_role('manager')
        is_admin = current_user.has_role('admin')

        # Institutions and ecoregions for the scope filter
        if is_admin:
            institutions = Institution.query.order_by(Institution.name_uk).all()
        else:
            institutions = sorted(current_user.institutions, key=lambda i: i.name_uk or '')
        lang = g.lang_code
        ecoregions = {}
        for inst in institutions:
            if inst.ecoregion_uk:
                display = inst.ecoregion_uk if lang != 'en' else (inst.ecoregion_en or inst.ecoregion_uk)
                ecoregions[inst.ecoregion_uk] = display
        
        # --- START OF NEW DYNAMIC LOGIC ---

        # 1. Fetch all active options from the DB in a single query
        all_options = ct_session.query(Species).filter(Species.is_active==True).all()
        
        # 2. Prepare empty lists to be populated dynamically
        grouped_species = {'mammals': [], 'birds': [], 'other': []}
        empty_choices = []
        other_special_choices = []
        
        # 3. Assign each option from the DB to the appropriate list
        for s in all_options:
            # Build the display name
            display_name = s.scientific_name
            if g.lang_code == 'uk' and s.common_name_ua:
                # For special options whose scientific name can be 'empty', 'vehicle', etc.
                # we don't want to show it in brackets if a Ukrainian name is available.
                if s.id < 0:
                    display_name = s.common_name_ua
                else:
                    display_name = f"{s.common_name_ua} ({s.scientific_name})"
            elif g.lang_code == 'en' and s.common_name_en:
                if s.id < 0:
                    display_name = s.common_name_en
                else:
                    display_name = f"{s.common_name_en} ({s.scientific_name})"

            choice = (s.id, display_name)

            # Sorting logic
            if s.id == -1: # Special handling for "Empty"
                empty_choices.append(choice)
            elif s.id < 0: # All other special options
                other_special_choices.append(choice)
            elif s.category in grouped_species: # Real animal species
                grouped_species[s.category].append(choice)
            else: # Unknown category — add to 'other'
                if 'other' in grouped_species:
                    grouped_species['other'].append(choice)

        # 4. Sort real species by popularity (as before)
        species_ranking = get_species_ranking()
        for category in grouped_species:
            grouped_species[category].sort(
                key=lambda x: species_ranking.get(x[0], 0), 
                reverse=True
            )
            
        # 5. Sort other special options by ID for stable ordering
        other_special_choices.sort(key=lambda x: x[0], reverse=True)

        # --- END OF NEW LOGIC ---

        # Populate behaviour choices — sort by usage frequency
        # (like the species list: most-frequent tags first, then descending;
        #  equally-frequent entries retain alphabetical order — stable sort).
        behavior_ranking = get_behavior_ranking()
        behavior_types = ct_session.query(BehaviorType).order_by(BehaviorType.name_ua).all()
        behavior_choices = [(bt.id, bt.get_name(g.lang_code)) for bt in behavior_types]
        behavior_choices.sort(key=lambda c: behavior_ranking.get(c[0], 0), reverse=True)
        form.behaviors.choices = behavior_choices

        # AI filter: list of species with AI predictions (only if AI is available).
        # Takes into account the user's access to locations and already-made
        # identifications — counts in brackets ("(42)") shrink as work progresses.
        from .ai_runner import is_ai_available, get_species_with_ai_predictions
        ai_available = is_ai_available()
        ai_species_list = []
        if ai_available:
            try:
                user_inst_ids_list = [inst.id for inst in current_user.institutions]
                ai_species_list = get_species_with_ai_predictions(
                    lang_code=g.lang_code,
                    user_id=current_user.id,
                    user_inst_ids=user_inst_ids_list,
                    is_admin=is_admin,
                )
            except Exception as e:
                current_app.logger.warning(f"AI: cannot load species list: {e}")

        # If start_obs_id is passed, send it to the template (for JS initialization).
        start_obs_id = request.args.get('start_obs_id', type=int)

        # Pass the dynamically populated lists to the template
        return render_template('identification.html',
                             form=form,
                             grouped_species=grouped_species,
                             empty_choices=empty_choices,
                             other_special_choices=other_special_choices,
                             can_review=can_review,
                             institutions=institutions,
                             ecoregions=ecoregions,
                             ai_available=ai_available,
                             ai_species_list=ai_species_list,
                             start_obs_id=start_obs_id)
    finally:
        close_ct_session()

@camera_traps_bp.route('/api/identify/ai-species', methods=['GET'])
@login_required
@role_required('ct_verifier')
def identify_ai_species_list(lang_code):
    """JSON list of AI species with counts, optionally narrowed to a scope.

    Called by the front end when `#scope-select` changes on /identify, to
    cascade-update `#ai-species-select` (shows only species that have a
    pending AI prediction in locations of the selected institution/ecoregion,
    with up-to-date counts).

    Query params (mutually exclusive):
      - scope_institution_id: int — narrow to a single institution
      - scope_ecoregion: str — narrow to an ecoregion (uk key)

    If neither scope is provided, returns the full list (respecting the
    user's access rights), equivalent to what is rendered on the page.
    """
    ct_session = get_ct_session()
    try:
        scope_institution_id = request.args.get('scope_institution_id', type=int)
        scope_ecoregion = request.args.get('scope_ecoregion', '') or None

        is_admin = current_user.has_role('admin')
        user_inst_ids = [inst.id for inst in current_user.institutions]

        # Guard: non-admins must not access other institutions' species lists.
        if scope_institution_id is not None and not is_admin:
            if scope_institution_id not in user_inst_ids:
                return jsonify({'ai_available': True, 'items': []}), 200

        from .ai_runner import is_ai_available, get_species_with_ai_predictions
        ai_available = is_ai_available()
        if not ai_available:
            return jsonify({'ai_available': False, 'items': []}), 200

        try:
            items = get_species_with_ai_predictions(
                lang_code=g.lang_code,
                user_id=current_user.id,
                user_inst_ids=user_inst_ids,
                is_admin=is_admin,
                scope_institution_id=scope_institution_id,
                scope_ecoregion=scope_ecoregion,
            )
        except Exception as e:
            current_app.logger.warning(f"AI: cannot load species list: {e}")
            items = []

        return jsonify({'ai_available': True, 'items': items}), 200
    finally:
        close_ct_session()


@camera_traps_bp.route('/upload', methods=['GET', 'POST'])
@login_required
@role_required('manager')
def upload(lang_code):
    ct_session = get_ct_session()
    try:
        form = UploadForm()
        
        user_inst_ids = [inst.id for inst in current_user.institutions]
        is_admin = current_user.has_role('admin')

        # --- START OF NEW SECURE LOGIC ---
        if is_admin:
            # Administrators see all locations for upload
            locations = ct_session.query(Location).order_by(Location.name).all()
        elif user_inst_ids:
            # Managers see ONLY locations belonging to their institutions.
            # Public locations (visibility_level=0) are excluded unless tied to an institution.
            locations = ct_session.query(Location)\
                .join(location_institutions, Location.id == location_institutions.c.location_id)\
                .filter(location_institutions.c.institution_id.in_(user_inst_ids))\
                .order_by(Location.name).distinct().all()
        else:
            # Manager with no institution assigned sees no locations
            locations = []
        # --- END OF NEW SECURE LOGIC ---

        form.location.choices = [(-1, _('-- Будь ласка, виберіть --'))] + [(loc.id, loc.name) for loc in locations] + [(0, _('*** СТВОРИТИ НОВЕ МІСЦЕ ***'))]
        
        # Fetch institutions of the current user (this code is correct as-is)
        if is_admin:
            institutions_list = Institution.query.order_by(Institution.name_uk).all()
        else:
            institutions_list = current_user.institutions

        # This block is preserved as-is; it is used for JavaScript-side filtering
        all_loc_inst_records = ct_session.query(location_institutions).all()
        loc_to_inst = {}
        for record in all_loc_inst_records:
            if record.location_id not in loc_to_inst:
                loc_to_inst[record.location_id] = []
            loc_to_inst[record.location_id].append(record.institution_id)
        
        locations_data = []
        for loc in locations:
            locations_data.append({
                'id': loc.id,
                'name': loc.name,
                'latitude': float(loc.latitude),
                'longitude': float(loc.longitude),
                'institution_ids': loc_to_inst.get(loc.id, [])
            })
        
        geoserver_url = current_app.config['GEOSERVER_URL']

        return render_template('upload.html',
                               form=form,
                               locations_data=locations_data,
                               geoserver_url=geoserver_url,
                               institutions=institutions_list)
    finally:
        close_ct_session()

@camera_traps_bp.route('/api/create-batch', methods=['POST'])
@login_required
@role_required('manager')
def create_batch(lang_code):
    """Create a new batch for file upload."""
    try:
        data = request.json
        location_id = data.get('location_id')
        total_files = data.get('total_files', 0)
        
        if not location_id:
            return jsonify({'error': _('Location ID is required')}), 400
            
        from .utils import create_upload_batch
        batch_id = create_upload_batch(int(location_id), current_user.id, total_files)
        
        return jsonify({
            'success': True, 
            'batch_id': batch_id,
            'message': _('Batch створено успішно')
        }), 201
        
    except Exception as e:
        current_app.logger.error(f"Error creating batch: {e}")
        return jsonify({'error': _('Помилка створення batch')}), 500

@camera_traps_bp.route('/upload/process-single', methods=['POST'])
@login_required
@role_required('manager')
def process_single_upload(lang_code):
    """Process a single file as part of a batch."""
    try:
        location_id = request.form.get('location_id')
        batch_id = request.form.get('batch_id')
        uploaded_file = request.files.get('file')
        save_original = request.form.get('save_original', 'true').lower() == 'true'
        
        if not all([location_id, batch_id, uploaded_file]):
            return jsonify({'error': _('Відсутні обов\'язкові параметри')}), 400
            
        if not uploaded_file.filename:
            return jsonify({'error': _('Файл не передано')}), 400
            
        from .utils import process_single_photo
        photo_id = process_single_photo(
            uploaded_file, 
            int(location_id), 
            current_user.id, 
            batch_id,
            save_original=save_original
        )
        
        return jsonify({
            'success': True, 
            'photo_id': photo_id,
            'message': _('Файл успішно завантажено')
        }), 200
        
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"Error processing single file: {e}")
        return jsonify({'error': _('Помилка обробки файлу')}), 500

@camera_traps_bp.route('/api/finalize-batch', methods=['POST'])
@login_required
@role_required('manager')
def finalize_batch(lang_code):
    """Finalise the batch and group photos into series."""
    try:
        data = request.json
        batch_id = data.get('batch_id')
        
        if not batch_id:
            return jsonify({'error': _('Необхідний ID для batch')}), 400
            
        from .utils import group_batch_into_series
        grouped_photos = group_batch_into_series(batch_id)
        
        return jsonify({
            'success': True,
            'grouped_photos': grouped_photos,
            'message': _('Batch успішно завершено та згруповано в серії')
        }), 200
        
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"Error finalizing batch: {e}")
        return jsonify({'error': _('Помилка завершення batch')}), 500

@camera_traps_bp.route('/api/batch-status/<batch_id>')
@login_required
@role_required('manager')
def get_batch_status_api(lang_code, batch_id):
    """Return the batch status."""
    try:
        from .utils import get_batch_status
        status = get_batch_status(batch_id)
        
        if not status:
            return jsonify({'error': _('Batch не знайдено')}), 404
            
        return jsonify(status), 200
        
    except Exception as e:
        current_app.logger.error(f"Error getting batch status: {e}")
        return jsonify({'error': _('Помилка отримання статусу batch')}), 500

# ═════════════════════════════════════════════════════════════════════════════
# /upload-fast — parallel path for large photo sets (10k–100k+).
# Co-exists with the legacy /upload. Shares create-batch /
# process-single / batch-status. Differs in:
#   • separate page with a parallel JS uploader and polling finalisation
#   • finalize-batch-async — returns 202, grouping runs in a background thread
#   • uploaded-files endpoint for resumable recovery
# ═════════════════════════════════════════════════════════════════════════════

@camera_traps_bp.route('/upload-fast', methods=['GET'])
@login_required
@role_required('manager')
def upload_fast(lang_code):
    """New upload page (Beta). Location-selection logic is identical to
    legacy `upload`; only the template and finalisation step differ."""
    ct_session = get_ct_session()
    try:
        form = UploadForm()
        user_inst_ids = [inst.id for inst in current_user.institutions]
        is_admin = current_user.has_role('admin')

        if is_admin:
            locations = ct_session.query(Location).order_by(Location.name).all()
        elif user_inst_ids:
            locations = ct_session.query(Location)\
                .join(location_institutions, Location.id == location_institutions.c.location_id)\
                .filter(location_institutions.c.institution_id.in_(user_inst_ids))\
                .order_by(Location.name).distinct().all()
        else:
            locations = []

        form.location.choices = (
            [(-1, _('-- Будь ласка, виберіть --'))]
            + [(loc.id, loc.name) for loc in locations]
            + [(0, _('*** СТВОРИТИ НОВЕ МІСЦЕ ***'))]
        )

        if is_admin:
            institutions_list = Institution.query.order_by(Institution.name_uk).all()
        else:
            institutions_list = current_user.institutions

        all_loc_inst_records = ct_session.query(location_institutions).all()
        loc_to_inst = {}
        for record in all_loc_inst_records:
            loc_to_inst.setdefault(record.location_id, []).append(record.institution_id)

        locations_data = [{
            'id': loc.id,
            'name': loc.name,
            'latitude': float(loc.latitude),
            'longitude': float(loc.longitude),
            'institution_ids': loc_to_inst.get(loc.id, [])
        } for loc in locations]

        return render_template(
            'upload_fast.html',
            form=form,
            locations_data=locations_data,
            geoserver_url=current_app.config['GEOSERVER_URL'],
            institutions=institutions_list,
        )
    finally:
        close_ct_session()


@camera_traps_bp.route('/api/finalize-batch-async', methods=['POST'])
@login_required
@role_required('manager')
def finalize_batch_async(lang_code):
    """Move the batch to 'ready_to_group' and start background grouping.
    Returns 202 Accepted with batch_id; the client polls /api/batch-status."""
    try:
        data = request.json or {}
        batch_id = data.get('batch_id')
        if not batch_id:
            return jsonify({'error': _('Необхідний ID для batch')}), 400

        ct_session = get_ct_session()
        try:
            from .models import UploadBatch
            batch = ct_session.query(UploadBatch).get(batch_id)
            if not batch:
                return jsonify({'error': _('Batch не знайдено')}), 404
            if batch.status not in ('uploading', 'failed'):
                # 'failed' is allowed as a retry; 'completed' / 'grouping' / 'ready_to_group' are not
                return jsonify({
                    'error': _("Batch у стані '%(s)s', фіналізація неможлива",
                               s=batch.status)
                }), 409
            batch.status = 'ready_to_group'
            batch.error_message = None
            ct_session.commit()
        finally:
            close_ct_session()

        from .fast_upload import start_async_grouping
        start_async_grouping(batch_id)

        return jsonify({
            'success': True,
            'batch_id': batch_id,
            'message': _('Групування запущено у фоні. Очікуйте...')
        }), 202

    except Exception as e:
        current_app.logger.exception(f"Error in finalize_batch_async: {e}")
        return jsonify({'error': _('Помилка фіналізації batch')}), 500


@camera_traps_bp.route('/api/batch/<batch_id>/uploaded-files', methods=['GET'])
@login_required
@role_required('manager')
def batch_uploaded_files(lang_code, batch_id):
    """List of (original_filename, captured_at) for files already uploaded in this batch.
    Used by upload_fast.html for resumable uploads: on session restore the JS
    skips files that are already on the server."""
    ct_session = get_ct_session()
    try:
        rows = ct_session.query(Photo.original_filename, Photo.captured_at)\
            .filter(Photo.upload_batch_id == batch_id).all()
        return jsonify({
            'batch_id': batch_id,
            'count': len(rows),
            'files': [
                {'original_filename': fn,
                 'captured_at': ts.isoformat() if ts else None}
                for fn, ts in rows
            ]
        }), 200
    except Exception as e:
        current_app.logger.error(f"Error listing batch files: {e}")
        return jsonify({'error': _('Помилка отримання списку файлів')}), 500
    finally:
        close_ct_session()


# ═════════════════════════════════════════════════════════════════════════════
# EXTERNAL DEEPFAUNE CLASSIFICATION IMPORT (CSV) — per location.
# Overlays the best local results (MDR) onto ALREADY uploaded photos. Does not
# touch server-side DF+MDS predictions (separate ai_models by level_id). Logic
# lives in classification_import.py. Two-step UX: preview (dry-run) → run (commit).
# ═════════════════════════════════════════════════════════════════════════════
def _accessible_locations(ct_session):
    """Return locations accessible to the current user, plus JS-filter data
    keyed by institution. Shared logic with upload/upload_fast."""
    user_inst_ids = [inst.id for inst in current_user.institutions]
    is_admin = current_user.has_role('admin')
    if is_admin:
        locations = ct_session.query(Location).order_by(Location.name).all()
        institutions_list = Institution.query.order_by(Institution.name_uk).all()
    elif user_inst_ids:
        locations = ct_session.query(Location)\
            .join(location_institutions, Location.id == location_institutions.c.location_id)\
            .filter(location_institutions.c.institution_id.in_(user_inst_ids))\
            .order_by(Location.name).distinct().all()
        institutions_list = current_user.institutions
    else:
        locations, institutions_list = [], []

    loc_to_inst = {}
    for record in ct_session.query(location_institutions).all():
        loc_to_inst.setdefault(record.location_id, []).append(record.institution_id)

    locations_data = [{
        'id': loc.id,
        'name': loc.name,
        'institution_ids': loc_to_inst.get(loc.id, []),
    } for loc in locations]
    return locations, locations_data, institutions_list


@camera_traps_bp.route('/import-classification', methods=['GET'])
@login_required
@role_required('manager')
def import_classification(lang_code):
    ct_session = get_ct_session()
    try:
        from .classification_import import get_import_levels
        locations, locations_data, institutions_list = _accessible_locations(ct_session)
        location_choices = [(-1, _('-- Будь ласка, виберіть локацію --'))] + \
                           [(loc.id, loc.name) for loc in locations]
        levels = [
            {'id': lv.id, 'code': lv.code, 'name': lv.name}
            for lv in get_import_levels(ct_session)
        ]
        return render_template(
            'import_classification.html',
            location_choices=location_choices,
            level_choices=levels,
            locations_data=locations_data,
            institutions=institutions_list,
        )
    finally:
        close_ct_session()


def _read_uploaded_csv():
    """Parse the uploaded CSV from request.files['file'].
    Returns (rows, errors). rows=None means a fatal error (errors[0] contains the message)."""
    from .classification_import import parse_deepfaune_csv
    f = request.files.get('file')
    if not f or not f.filename:
        return None, [_('Файл не передано')]
    return parse_deepfaune_csv(f.stream)


@camera_traps_bp.route('/import-classification/preview', methods=['POST'])
@login_required
@role_required('manager')
def import_classification_preview(lang_code):
    from .classification_import import preview_import
    location_id = request.form.get('location_id', type=int)
    if not location_id or location_id <= 0:
        return jsonify({'error': _('Оберіть локацію')}), 400
    rows, errors = _read_uploaded_csv()
    if rows is None:
        return jsonify({'error': errors[0]}), 400
    ct_session = get_ct_session()
    try:
        stats = preview_import(ct_session, location_id, rows)
        stats['parse_errors'] = errors[:20]
        stats['parse_error_count'] = len(errors)
        return jsonify(stats), 200
    except Exception as e:
        current_app.logger.exception(f"import-classification preview failed: {e}")
        return jsonify({'error': _('Помилка прев’ю імпорту')}), 500
    finally:
        close_ct_session()


@camera_traps_bp.route('/import-classification/run', methods=['POST'])
@login_required
@role_required('manager')
def import_classification_run(lang_code):
    from .classification_import import run_import, get_import_levels
    location_id = request.form.get('location_id', type=int)
    level_id = request.form.get('level_id', type=int)
    if not location_id or location_id <= 0:
        return jsonify({'error': _('Оберіть локацію')}), 400
    if not level_id:
        return jsonify({'error': _('Оберіть рівень моделі')}), 400
    rows, errors = _read_uploaded_csv()
    if rows is None:
        return jsonify({'error': errors[0]}), 400
    ct_session = get_ct_session()
    try:
        # Validate: the level must be among those allowed for import.
        allowed_level_ids = {lv.id for lv in get_import_levels(ct_session)}
        if level_id not in allowed_level_ids:
            return jsonify({'error': _('Недопустимий рівень моделі')}), 400
        report = run_import(ct_session, location_id, rows, level_id, user_id=current_user.id)
        ct_session.commit()
        report['success'] = True
        report['parse_error_count'] = len(errors)
        return jsonify(report), 200
    except Exception as e:
        ct_session.rollback()
        current_app.logger.exception(f"import-classification run failed: {e}")
        return jsonify({'error': _('Помилка імпорту класифікації')}), 500
    finally:
        close_ct_session()


@camera_traps_bp.route('/photo/<int:photo_id>')
@camera_traps_bp.route('/observation/<int:observation_id>/photo/<int:photo_index>')
@login_required
def view_photo(lang_code, photo_id=None, observation_id=None, photo_index=None):
    ct_session = get_ct_session()
    try:
        observation = None
        
        # Step 1: Identify the Observation
        if observation_id:
            observation = ct_session.query(Observation).get(observation_id)
        elif photo_id:
            temp_photo = ct_session.query(Photo).get(photo_id)
            if temp_photo:
                observation = temp_photo.observation
        
        if not observation:
            flash(_('Спостереження або фотографію не знайдено.'), 'danger')
            return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))

        # Step 2: Retrieve and sort the photo list
        photos_sorted = sorted(list(observation.photos), key=lambda p: p.captured_at)
        
        if not photos_sorted:
             flash(_('У цьому спостереженні немає фотографій.'), 'warning')
             return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))

        # Step 3: Determine the current photo index
        current_photo_index = 0
        if photo_index is not None:
            if 0 <= photo_index < len(photos_sorted):
                current_photo_index = photo_index
        elif photo_id:
            # Find the index of the photo with the requested ID in the sorted list
            for i, p in enumerate(photos_sorted):
                if p.id == photo_id:
                    current_photo_index = i
                    break
        
        # Retrieve the final, correct photo object from the sorted list
        photo = photos_sorted[current_photo_index]

        # Step 4: Safely load user names
        if photo.identifications:
            user_ids = [ident.user_id for ident in photo.identifications]
            
            # Query the main database
            from app.models import User # Ensure this import exists at the top of the file
            users = User.query.filter(User.id.in_(user_ids)).all()
            user_map = {user.id: user for user in users} # Store full User objects

            # Attach the user object as a new attribute on each identification
            for ident in photo.identifications:
                ident.user = user_map.get(ident.user_id)

        # Step 5: Prepare navigation data for the series
        series_photos = []
        for idx, p in enumerate(photos_sorted):
            series_photos.append({
                'id': p.id,
                'index': idx,
                'sequence_number': p.sequence_number,
                'captured_at': p.captured_at.strftime('%H:%M:%S'),
                'is_current': idx == current_photo_index
            })
        
        return render_template('photo_viewer.html', 
                             photo=photo, 
                             observation=observation,
                             series_photos=series_photos,
                             current_photo_index=current_photo_index)
    except Exception as e:
        current_app.logger.error(f"Error in view_photo: {e}", exc_info=True) # exc_info added for better diagnostics
        flash(_('Помилка завантаження фотографії.'), 'danger')
        return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))
    finally:
        close_ct_session()

# --- DASHBOARD API ---
@camera_traps_bp.route('/api/stats/top-species')
def stats_top_species(lang_code):
    """Return chart data based on consensus across completed observations."""
    session = None
    try:
        session = get_ct_session()
        conn = session.connection()

        start_date_str = request.args.get('start_date', '2020-08-01')
        end_date_str = request.args.get('end_date', date.today().strftime('%Y-%m-%d'))
        # institution_id may arrive as multiple values (e.g. ecoregion → set of institutions)
        # OR as a comma-separated string. Collect into a list, as in stats_locations.
        raw_inst_ids = request.args.getlist('institution_id')
        if not raw_inst_ids:
            raw_inst_ids = request.args.get('institution_id', '').split(',')
        selected_inst_ids = [int(i) for i in raw_inst_ids if str(i).isdigit()]

        params = {
            'start_date': start_date_str,
            'end_date': end_date_str
        }

        location_ids_str = request.args.get('locations', '')
        biotope_ids_str = request.args.get('biotopes', '')
        location_ids = [int(id) for id in location_ids_str.split(',') if id.isdigit()]
        biotope_ids = [int(id) for id in biotope_ids_str.split(',') if id.isdigit()]

        user_inst_ids =[inst.id for inst in current_user.institutions] if current_user.is_authenticated else[]
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        # Pass table_alias='l' for raw SQL
        inst_condition, inst_params = get_institution_filter(
            user_inst_ids, is_admin, selected_inst_id=selected_inst_ids, table_alias='l'
        )
        params.update(inst_params)

        # --- Step 2: SQL query with consensus logic ---
        
        consensus_cte = """
            WITH ObservationConsensus AS (
                SELECT
                    p.observation_id, i.species_id,
                    COUNT(DISTINCT i.user_id) as vote_count,
                    MAX(i.quantity) as max_quantity
                FROM identifications i JOIN photos p ON i.photo_id = p.id
                GROUP BY p.observation_id, i.species_id
            ),
            RankedConsensus AS (
                SELECT
                    observation_id, species_id,
                    ROW_NUMBER() OVER(PARTITION BY observation_id ORDER BY vote_count DESC, max_quantity DESC) as rn
                FROM ObservationConsensus
            )
        """
        
        query_base = """
            SELECT
                s.id, s.scientific_name, s.common_name_ua, s.common_name_en,
                COUNT(o.id) as observation_count
            FROM observations o
            JOIN RankedConsensus rc ON o.id = rc.observation_id AND rc.rn = 1
            JOIN species s ON s.id = rc.species_id
            JOIN locations l ON o.location_id = l.id
        """
        
        conditions =[
            "o.status IN ('completed', 'archived')",
            "s.id > 0",
            "DATE(o.series_start_time) BETWEEN :start_date AND :end_date",
            inst_condition
        ]

        if biotope_ids:
            query_base += " JOIN location_biotopes lb ON l.id = lb.location_id"
            conditions.append("lb.biotope_id IN :biotope_ids")
            params['biotope_ids'] = tuple(biotope_ids)

        if location_ids:
            conditions.append("l.id IN :location_ids")
            params['location_ids'] = tuple(location_ids)
            
        where_clause = " WHERE " + " AND ".join(conditions)
        group_by_clause = " GROUP BY s.id, s.scientific_name, s.common_name_ua, s.common_name_en"
        order_by_clause = " ORDER BY observation_count DESC, s.scientific_name ASC"
        limit_clause = " LIMIT 15"

        final_query = consensus_cte + query_base + where_clause + group_by_clause + order_by_clause + limit_clause
        
        result = conn.execute(text(final_query), params).mappings().fetchall()
        
        # --- Step 3: Process results ---
        labels = []
        data = []
        for row in result:
            display_name = row['scientific_name']
            if g.lang_code == 'uk' and row['common_name_ua']:
                display_name = row['common_name_ua']
            elif g.lang_code == 'en' and row['common_name_en']:
                display_name = row['common_name_en']
            
            labels.append(display_name)
            data.append(row['observation_count'])
            
        return jsonify({'labels': labels, 'data': data})

    except Exception as e:
        current_app.logger.error(f"Error fetching top species stats: {e}", exc_info=True)
        return jsonify({'error': 'Failed to load chart data'}), 500
    finally:
        if session:
            close_ct_session()

@camera_traps_bp.route('/api/stats/locations')
def stats_locations(lang_code):
    ct_session = get_ct_session()
    try:
        start_date_str = request.args.get('start_date', '2020-08-01')
        end_date_str = request.args.get('end_date', date.today().strftime('%Y-%m-%d'))
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        
        # Fetch institution IDs (can arrive as a list or a comma-separated string)
        raw_inst_ids = request.args.getlist('institution_id')
        if not raw_inst_ids:
            raw_inst_ids = request.args.get('institution_id', '').split(',')
        selected_inst_ids = [int(i) for i in raw_inst_ids if str(i).isdigit()]

        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        
        # Apply the filter, specifying the correct alias 'locations' upfront
        inst_condition, inst_params = get_institution_filter(
            user_inst_ids, is_admin, selected_inst_id=selected_inst_ids, table_alias='locations'
        )
        
        biotope_ids_str = request.args.get('biotopes', '')
        biotope_ids = [int(id) for id in biotope_ids_str.split(',') if id.isdigit()]

        # Use inst_condition as-is, without substitution
        query = ct_session.query(
            Location.id, Location.name, Location.latitude, Location.longitude,
            func.count(Photo.id).label('photo_count')
        ).join(Observation, Location.id == Observation.location_id)\
        .join(Photo, Observation.id == Photo.observation_id)\
        .filter(Photo.captured_at.between(start_date, end_date))\
        .filter(text(inst_condition)).params(**inst_params)
        
        if biotope_ids:
            query = query.join(Location.biotopes).filter(Biotope.id.in_(biotope_ids))

        res = query.group_by(Location.id).all()
        data = [{'id': l.id, 'name': l.name, 'lat': float(l.latitude), 'lon': float(l.longitude), 'photo_count': l.photo_count} for l in res]
        return jsonify(data)
    except Exception as e:
        current_app.logger.error(f"Error in stats_locations: {e}", exc_info=True)
        return jsonify({'error': 'Error'}), 500
    finally:
        close_ct_session()

# --- API FOR THE SPECIES DETAIL ANALYSIS PAGE ---
@camera_traps_bp.route('/api/stats/species-dynamics')
def api_species_dynamics(lang_code):
    """API for fetching chart data from pre-computed analytics tables."""
    ct_session = get_ct_session()
    try:
        species_id = request.args.get('species_id', type=int)
        start_year = request.args.get('start_year', type=int)
        end_year = request.args.get('end_year', type=int)
        scope_type = request.args.get('scope_type', 'global')
        scope_id = request.args.get('scope_id', '')

        if not all([species_id, start_year, end_year]):
            return jsonify({'error': 'Species ID, start year, and end year are required'}), 400

        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []

        # Check access rights for the requested scope
        if not is_admin:
            if scope_type == 'institution':
                if not scope_id or int(scope_id) not in user_inst_ids:
                    return jsonify({'error': 'Access denied'}), 403
            elif scope_type == 'ecoregion':
                user_ecoregions = {
                    inst.ecoregion_uk for inst in current_user.institutions
                    if inst.ecoregion_uk
                } if current_user.is_authenticated else set()
                if scope_id not in user_ecoregions:
                    return jsonify({'error': 'Access denied'}), 403
            elif scope_type == 'global' and user_inst_ids:
                # For non-admins with global scope — filter seasonal data by their locations,
                # but use the global scope for trends (best available approximation)
                pass

        # Location filter for seasonal data
        loc_filter = None
        if scope_type == 'institution' and scope_id:
            loc_filter = select(location_institutions.c.location_id).where(
                location_institutions.c.institution_id == int(scope_id))
        elif scope_type == 'ecoregion' and scope_id:
            eco_inst_ids = [i.id for i in Institution.query.filter_by(ecoregion_uk=scope_id).all()]
            if eco_inst_ids:
                loc_filter = select(location_institutions.c.location_id).where(
                    location_institutions.c.institution_id.in_(eco_inst_ids))
        elif scope_type == 'global' and not is_admin and user_inst_ids:
            loc_filter = select(location_institutions.c.location_id).where(
                location_institutions.c.institution_id.in_(user_inst_ids))

        # 1. Seasonal activity
        seasonal_q = ct_session.query(
            LocationMonthlyActivity.year,
            LocationMonthlyActivity.month,
            func.sum(LocationMonthlyActivity.detection_count).label('observation_count')
        ).filter(
            LocationMonthlyActivity.species_id == species_id,
            LocationMonthlyActivity.year.between(start_year, end_year)
        )
        if loc_filter is not None:
            seasonal_q = seasonal_q.filter(LocationMonthlyActivity.location_id.in_(loc_filter))
        seasonal_q = seasonal_q.group_by(LocationMonthlyActivity.year, LocationMonthlyActivity.month)\
            .order_by(LocationMonthlyActivity.year, LocationMonthlyActivity.month).all()

        seasonal_data = [{'year': r.year, 'month': r.month, 'count': r.observation_count}
                         for r in seasonal_q]

        # 2. Annual trend (pre-computed for the scope)
        yearly_q = ct_session.query(SpeciesYearlyTrend).filter(
            SpeciesYearlyTrend.species_id == species_id,
            SpeciesYearlyTrend.year.between(start_year, end_year),
            SpeciesYearlyTrend.scope_type == scope_type,
            SpeciesYearlyTrend.scope_id == scope_id
        ).order_by(SpeciesYearlyTrend.year).all()

        yearly_data = [{
            'year': r.year,
            'mean_dr_index': float(r.mean_dr_index),
            'lower_ci': float(r.lower_ci),
            'upper_ci': float(r.upper_ci)
        } for r in yearly_q]

        return jsonify({'seasonal_activity': seasonal_data, 'yearly_trend': yearly_data})
    except Exception as e:
        current_app.logger.error(f"Error in api_species_dynamics: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500
    finally:
        close_ct_session()
        
# --- API FOR THE REGION COMPARISON PAGE ---
@camera_traps_bp.route('/api/stats/comparison')
def api_comparison(lang_code):
    """Return statistics, species RAI, and ecological indices for two selected regions."""
    import math

    ct_session = get_ct_session()
    try:
        start_date_str = request.args.get('start_date', '2020-08-01')
        end_date_str = request.args.get('end_date', date.today().strftime('%Y-%m-%d'))
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({'error': 'Invalid date format'}), 400

        biotope_ids_str = request.args.get('biotopes', '')
        biotope_ids = [int(x) for x in biotope_ids_str.split(',') if x.isdigit()]

        left_scope_type = request.args.get('left_scope_type', '')
        left_scope_id = request.args.get('left_scope_id', '')
        right_scope_type = request.args.get('right_scope_type', '')
        right_scope_id = request.args.get('right_scope_id', '')

        if not left_scope_id or not right_scope_id:
            return jsonify({'error': 'Both scopes must be selected'}), 400

        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []

        def check_scope_access(scope_type, scope_id):
            if is_admin:
                return True
            if scope_type == 'institution':
                return str(scope_id).isdigit() and int(scope_id) in user_inst_ids
            elif scope_type == 'ecoregion':
                user_ecos = {i.ecoregion_uk for i in current_user.institutions if i.ecoregion_uk} if current_user.is_authenticated else set()
                return scope_id in user_ecos
            return False

        if not check_scope_access(left_scope_type, left_scope_id):
            return jsonify({'error': 'Access denied to left scope'}), 403
        if not check_scope_access(right_scope_type, right_scope_id):
            return jsonify({'error': 'Access denied to right scope'}), 403

        def get_scope_label(scope_type, scope_id):
            if scope_type == 'institution' and str(scope_id).isdigit():
                inst = Institution.query.get(int(scope_id))
                if inst:
                    return inst.name_uk if g.lang_code != 'en' else (inst.name_en or inst.name_uk)
            elif scope_type == 'ecoregion':
                return scope_id
            return scope_id

        def get_scope_location_ids(scope_type, scope_id):
            if scope_type == 'institution' and str(scope_id).isdigit():
                q = select(location_institutions.c.location_id).where(
                    location_institutions.c.institution_id == int(scope_id))
            elif scope_type == 'ecoregion':
                eco_inst_ids = [i.id for i in Institution.query.filter_by(ecoregion_uk=scope_id).all()]
                if not eco_inst_ids:
                    return []
                q = select(location_institutions.c.location_id).where(
                    location_institutions.c.institution_id.in_(eco_inst_ids))
            else:
                return []
            return [r[0] for r in ct_session.execute(q).fetchall()]

        left_loc_ids = get_scope_location_ids(left_scope_type, left_scope_id)
        right_loc_ids = get_scope_location_ids(right_scope_type, right_scope_id)

        if not left_loc_ids and not right_loc_ids:
            return jsonify({'error': 'No locations found for selected scopes'}), 404

        # Filter biotopes by location
        if biotope_ids:
            bio_q = select(Location.id).join(Location.biotopes).filter(Biotope.id.in_(biotope_ids))
            bio_loc_ids = {r[0] for r in ct_session.execute(bio_q).fetchall()}
            left_loc_ids = [l for l in left_loc_ids if l in bio_loc_ids]
            right_loc_ids = [r for r in right_loc_ids if r in bio_loc_ids]

        start_ym = start_date.year * 100 + start_date.month
        end_ym = end_date.year * 100 + end_date.month

        def get_scope_data(loc_ids):
            if not loc_ids:
                return {}, 0, 0

            ym_expr = LocationMonthlyActivity.year * 100 + LocationMonthlyActivity.month

            species_rows = ct_session.query(
                LocationMonthlyActivity.species_id,
                func.sum(LocationMonthlyActivity.detection_count).label('total')
            ).filter(
                LocationMonthlyActivity.location_id.in_(loc_ids),
                LocationMonthlyActivity.species_id > 0,
                ym_expr >= start_ym,
                ym_expr <= end_ym
            ).group_by(LocationMonthlyActivity.species_id).all()
            species_counts = {r.species_id: int(r.total) for r in species_rows}

            trap_rows = ct_session.query(
                LocationMonthlyActivity.location_id,
                LocationMonthlyActivity.year,
                LocationMonthlyActivity.month,
                func.max(LocationMonthlyActivity.trap_days).label('td')
            ).filter(
                LocationMonthlyActivity.location_id.in_(loc_ids),
                ym_expr >= start_ym,
                ym_expr <= end_ym
            ).group_by(
                LocationMonthlyActivity.location_id,
                LocationMonthlyActivity.year,
                LocationMonthlyActivity.month
            ).all()

            total_trap_days = int(sum(r.td for r in trap_rows)) if trap_rows else 0
            active_locs = len({r.location_id for r in trap_rows})
            return species_counts, total_trap_days, active_locs

        left_counts, left_trap_days, left_locs = get_scope_data(left_loc_ids)
        right_counts, right_trap_days, right_locs = get_scope_data(right_loc_ids)

        # Raw identifications for Venn analysis (catches rare species absent from the pre-computed table)
        def get_all_species_ids(loc_ids):
            if not loc_ids:
                return set()
            rows = ct_session.query(func.distinct(Identification.species_id)).join(
                Photo, Identification.photo_id == Photo.id
            ).join(
                Observation, Photo.observation_id == Observation.id
            ).filter(
                Observation.location_id.in_(loc_ids),
                Observation.status.in_(['completed', 'archived']),
                Identification.species_id > 0,
                Photo.captured_at >= start_date,
                Photo.captured_at < end_date + timedelta(days=1)
            ).all()
            return {r[0] for r in rows}

        left_all_spp = get_all_species_ids(left_loc_ids)
        right_all_spp = get_all_species_ids(right_loc_ids)

        # species_map for ALL species (pre-computed + raw)
        all_ids_for_names = set(left_counts.keys()) | set(right_counts.keys()) | left_all_spp | right_all_spp
        species_map = {}
        if all_ids_for_names:
            for s in ct_session.query(Species).filter(Species.id.in_(all_ids_for_names)).all():
                if g.lang_code == 'uk' and s.common_name_ua:
                    species_map[s.id] = s.common_name_ua
                elif g.lang_code == 'en' and s.common_name_en:
                    species_map[s.id] = s.common_name_en
                else:
                    species_map[s.id] = s.scientific_name

        def compute_rai(counts, trap_days):
            if not trap_days:
                return {}
            return {sid: (cnt / trap_days) * 100 for sid, cnt in counts.items()}

        left_rai = compute_rai(left_counts, left_trap_days)
        right_rai = compute_rai(right_counts, right_trap_days)

        def diversity_indices(rai_dict):
            if not rai_dict:
                return {'shannon': None, 'simpson': None, 'pielou': None}
            total = sum(rai_dict.values())
            if total == 0:
                return {'shannon': None, 'simpson': None, 'pielou': None}
            props = [v / total for v in rai_dict.values()]
            shannon = -sum(p * math.log(p) for p in props if p > 0)
            simpson = 1 - sum(p ** 2 for p in props)
            s = len(props)
            pielou = (shannon / math.log(s)) if s > 1 else None
            return {
                'shannon': round(shannon, 3),
                'simpson': round(simpson, 3),
                'pielou': round(pielou, 3) if pielou is not None else None
            }

        # Venn: based on raw identifications (more complete than the pre-computed table)
        left_only_spp = left_all_spp - right_all_spp
        right_only_spp = right_all_spp - left_all_spp
        shared_spp = left_all_spp & right_all_spp
        union_spp = left_all_spp | right_all_spp

        # Jaccard and Sørensen — presence/absence based (raw data)
        jaccard = round(len(shared_spp) / len(union_spp), 3) if union_spp else 0
        sorensen_denom = len(left_all_spp) + len(right_all_spp)
        sorensen = round(2 * len(shared_spp) / sorensen_denom, 3) if sorensen_denom else 0

        # Bray-Curtis and Morisita-Horn — abundance based (pre-computed RAI)
        rai_union = set(left_counts.keys()) | set(right_counts.keys())
        sum_min = sum(min(left_counts.get(s, 0), right_counts.get(s, 0)) for s in rai_union)
        sum_all = sum(left_counts.get(s, 0) for s in rai_union) + sum(right_counts.get(s, 0) for s in rai_union)
        bray_curtis = round(1 - (2 * sum_min / sum_all), 3) if sum_all else 0

        n_a = sum(left_counts.values()) or 1
        n_b = sum(right_counts.values()) or 1
        da = sum(v ** 2 for v in left_counts.values()) / (n_a ** 2) if n_a else 0
        db = sum(v ** 2 for v in right_counts.values()) / (n_b ** 2) if n_b else 0
        mh_num = 2 * sum(left_counts.get(s, 0) * right_counts.get(s, 0) for s in rai_union)
        mh_den = (da + db) * n_a * n_b
        morisita_horn = round(mh_num / mh_den, 3) if mh_den else 0

        # Bar chart: top-30 by total RAI
        sorted_species = sorted(rai_union,
                                key=lambda s: (left_rai.get(s, 0) + right_rai.get(s, 0)),
                                reverse=True)
        species_list = []
        for sid in sorted_species[:30]:
            species_list.append({
                'id': sid,
                'name': species_map.get(sid, f'Species {sid}'),
                'left_count': left_counts.get(sid, 0),
                'right_count': right_counts.get(sid, 0),
                'left_rai': round(left_rai.get(sid, 0), 3),
                'right_rai': round(right_rai.get(sid, 0), 3)
            })

        left_only_names = sorted(species_map.get(s, f'Species {s}') for s in left_only_spp)
        right_only_names = sorted(species_map.get(s, f'Species {s}') for s in right_only_spp)
        shared_names = sorted(species_map.get(s, f'Species {s}') for s in shared_spp)

        return jsonify({
            'left_label': get_scope_label(left_scope_type, left_scope_id),
            'right_label': get_scope_label(right_scope_type, right_scope_id),
            'left': {
                'trap_days': left_trap_days,
                'species_count': len(left_all_spp),
                'total_detections': sum(left_counts.values()),
                'active_locations': left_locs,
                **diversity_indices(left_rai)
            },
            'right': {
                'trap_days': right_trap_days,
                'species_count': len(right_all_spp),
                'total_detections': sum(right_counts.values()),
                'active_locations': right_locs,
                **diversity_indices(right_rai)
            },
            'species': species_list,
            'similarity': {
                'jaccard': jaccard,
                'sorensen': sorensen,
                'bray_curtis': bray_curtis,
                'morisita_horn': morisita_horn,
                'shared_count': len(shared_spp),
                'left_only_count': len(left_only_spp),
                'right_only_count': len(right_only_spp),
                'left_only_species': left_only_names[:20],
                'right_only_species': right_only_names[:20],
                'shared_species': shared_names[:20]
            }
        })

    except Exception as e:
        current_app.logger.error(f"Error in api_comparison: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500
    finally:
        close_ct_session()

# --- IDENTIFICATION AND UPLOAD API ---
@camera_traps_bp.route('/api/submit-identification', methods=['POST'])
@login_required
@role_required('ct_verifier')
def submit_identification(lang_code):
    ct_session = get_ct_session()
    try:
        try:
            data = request.json
            observation_id = int(data['observation_id'])
            species_id = int(data['species_id'])
            quantity = int(data.get('quantity', 1))
            is_favorite = bool(data.get('is_favorite', False))
            current_photo_index = int(data.get('current_photo_index', 0))
            behavior_ids = data.get('behaviors', [])
            # #47: optional comment field; back-end guard — truncate to 200 chars
            comment = (data.get('comment') or '').strip()[:200] or None
        except (ValueError, TypeError, KeyError):
            return jsonify({'success': False, 'error': _('Неправильний формат даних.')}), 400

        observation = ct_session.query(Observation).get(observation_id)
        if not observation:
            return jsonify({'success': False, 'error': _('Серію не знайдено.')}), 404
        
        is_moderator = current_user.has_role('manager')

        moderator_override = is_moderator and observation.status == 'completed'

        selected_behaviors = ct_session.query(BehaviorType).filter(BehaviorType.id.in_(behavior_ids)).all() if behavior_ids else []
        
        photos_sorted = sorted(list(observation.photos), key=lambda p: p.captured_at)
        # Validate the index
        if current_photo_index >= len(photos_sorted):
            current_photo_index = 0  # Fallback to first photo
                
        for i, photo in enumerate(photos_sorted):
            # Set is_favorite only for the current photo
            if is_favorite and i == current_photo_index:
                photo.is_favorite = True
            elif not is_favorite and i == current_photo_index:
                photo.is_favorite = False

            existing_id = ct_session.query(Identification).filter_by(user_id=current_user.id, photo_id=photo.id).first()
            if not existing_id:
                new_id = Identification(
                    photo_id=photo.id,
                    user_id=current_user.id,
                    species_id=species_id,
                    quantity=quantity,
                    comment=comment
                )
                if selected_behaviors:
                    new_id.behaviors.extend(selected_behaviors)
                ct_session.add(new_id)
                photo.identification_count += 1
                
        ct_session.flush()
        
        check_consensus_for_observation(
            observation_id, 
            db_session=ct_session,
            moderator_override=moderator_override
        )
                
        ct_session.commit()
        return jsonify({'success': True, 'message': _('Ідентифікацію для серії успішно збережено!')}), 201
    except Exception as e:
        ct_session.rollback()
        current_app.logger.error(f"Error saving series identification: {e}")
        return jsonify({'success': False, 'error': _('Помилка бази даних.')}), 500
    finally:
        close_ct_session()

@camera_traps_bp.route('/api/location/<int:location_id>')
@login_required
def get_location_details(lang_code, location_id):
    """API for fetching details of a specific location, including its biotopes."""
    ct_session = get_ct_session()
    try:
        location = ct_session.query(Location).get(location_id)
        if not location:
            return jsonify({'error': _('Локацію не знайдено.')}), 404
        
        biotope_ids = [biotope.id for biotope in location.biotopes]
        inst_rows = ct_session.execute(
            select(location_institutions.c.institution_id)
            .where(location_institutions.c.location_id == location_id)
        ).fetchall()
        institution_ids = [row.institution_id for row in inst_rows]

        return jsonify({
            'id': location.id,
            'name': location.name,
            'latitude': float(location.latitude),
            'longitude': float(location.longitude),
            'biotope_ids': biotope_ids,
            'description': location.description or '',
            'institution_ids': institution_ids
        }), 200
        
    except Exception as e:
        current_app.logger.error(f"Error fetching location details: {e}")
        return jsonify({'error': _('Помилка отримання даних локації.')}), 500
    finally:
        close_ct_session()

@camera_traps_bp.route('/api/next-observation-for-identification', methods=['GET'])
@login_required
def next_observation_for_identification(lang_code):
    """Find the next series for identification."""
    ct_session = get_ct_session()
    try:
        review_mode = request.args.get('review', 'false').lower() == 'true'
        review_user_id = request.args.get('review_user_id', type=int)
        review_species_id = request.args.get('review_species_id', type=int)
        sort_by = request.args.get('sort_by', 'random') # Default: 'random'
        scope_institution_id = request.args.get('scope_institution_id', type=int)
        scope_ecoregion = request.args.get('scope_ecoregion', '')
        ai_species_id = request.args.get('ai_species_id', type=int)  # filter "AI: species"
        start_obs_id = request.args.get('start_obs_id', type=int)  # open a specific series first

        # Check access rights for review mode
        if review_mode and not current_user.has_role('manager'):
            return jsonify({'error': _('Недостатньо прав для режиму перегляду')}), 403

        # If start_obs_id is passed, return that specific series (e.g. from the flagged page).
        if start_obs_id is not None:
            observation = ct_session.query(Observation).get(start_obs_id)
            if observation is None:
                return jsonify({'message': _('Серію не знайдено.')}), 404
            # Check access (admin sees everything; others only their own institutions/public)
            is_admin_check = current_user.has_role('admin')
            if not is_admin_check:
                user_inst_ids_check = [inst.id for inst in current_user.institutions]
                loc = observation.location
                if loc:
                    loc_inst_ids = [inst.id for inst in loc.institutions] if hasattr(loc, 'institutions') else []
                    if loc.visibility_level != 0 and not any(i in user_inst_ids_check for i in loc_inst_ids):
                        return jsonify({'message': _('Немає доступу до цієї серії.')}), 403
            photos_sorted = sorted(list(observation.photos), key=lambda p: p.captured_at)
            photos_data = []
            for i, photo in enumerate(photos_sorted):
                photos_data.append({
                    'id': photo.id,
                    'thumbnail_url': url_for('camera_traps.serve_thumbnail',
                                            lang_code=g.lang_code,
                                            filename=photo.system_filename,
                                            _external=True),
                    'captured_at': photo.captured_at.strftime('%d.%m.%Y %H:%M:%S'),
                    'debug_index': i,
                    'debug_filename': photo.system_filename
                })
            response_data = {
                'observation_id': observation.id,
                'location_name': observation.location.name if observation.location else '',
                'photos': photos_data
            }
            try:
                from .ai_runner import is_ai_available, get_observation_ai_prediction
                if is_ai_available():
                    ai_pred = get_observation_ai_prediction(observation.id)
                    if ai_pred is not None:
                        response_data['ai_prediction'] = ai_pred
            except Exception as e:
                current_app.logger.warning(f"AI: cannot load prediction for obs {observation.id}: {e}")
            return jsonify(response_data)

        user_identified_photos = ct_session.query(Identification.photo_id).filter_by(user_id=current_user.id)

        user_inst_ids = [inst.id for inst in current_user.institutions]
        is_admin = current_user.has_role('admin')

        if not is_admin:
            if user_inst_ids:
                allowed_location_ids = select(location_institutions.c.location_id).where(
                    location_institutions.c.institution_id.in_(user_inst_ids)
                )
                location_filter = or_(
                    Location.visibility_level == 0,
                    Location.id.in_(allowed_location_ids)
                )
            else:
                location_filter = (Location.visibility_level == 0)

        # Additional scope filter (selected institution or ecoregion)
        scope_location_subq = None
        if scope_institution_id:
            if is_admin or scope_institution_id in user_inst_ids:
                scope_location_subq = select(location_institutions.c.location_id).where(
                    location_institutions.c.institution_id == scope_institution_id
                )
        elif scope_ecoregion:
            eco_inst_ids = [i.id for i in Institution.query.filter_by(ecoregion_uk=scope_ecoregion).all()]
            if not is_admin:
                eco_inst_ids = [i for i in eco_inst_ids if i in user_inst_ids]
            if eco_inst_ids:
                scope_location_subq = select(location_institutions.c.location_id).where(
                    location_institutions.c.institution_id.in_(eco_inst_ids)
                )

        # AI filter: show only series where AI identified the selected species
        # (from the WINNING model — highest accuracy_rank per series). Works
        # silently: if AI has not been used yet, the parameter is ignored.
        ai_observation_subq = None
        if ai_species_id is not None:
            from .ai_runner import is_ai_available, observations_subq_for_ai_species
            if is_ai_available():
                ai_observation_subq = observations_subq_for_ai_species(ai_species_id)

        if review_mode:
            # In review mode show both pending and completed with identifications
            query = ct_session.query(Observation).filter(
                Observation.status.in_(['pending', 'completed']),
                Observation.photos.any(Photo.identifications.any()),
                ~Observation.photos.any(Photo.id.in_(user_identified_photos))
            )

            if not is_admin:
                query = query.join(Location, Observation.location_id == Location.id)\
                    .filter(location_filter)

            if scope_location_subq is not None:
                query = query.filter(Observation.location_id.in_(scope_location_subq))

            if ai_observation_subq is not None:
                query = query.filter(Observation.id.in_(ai_observation_subq))

            # Add user filter
            if review_user_id:
                query = query.filter(
                    Observation.photos.any(
                        Photo.identifications.any(Identification.user_id == review_user_id)
                    )
                )

            # Add species filter
            if review_species_id:
                query = query.filter(
                    Observation.photos.any(
                        Photo.identifications.any(Identification.species_id == review_species_id)
                    )
                )

            if sort_by == 'date_desc':
                query = query.order_by(Observation.series_start_time.desc())
            elif sort_by == 'date_asc':
                query = query.order_by(Observation.series_start_time.asc())
            elif sort_by == 'photo_count_desc':
                query = query.order_by(Observation.photo_count.desc())
            else: # 'random' or any other value
                query = query.order_by(func.random())

            observation = query.first()

        else:
            # Normal mode — pending only
            query = ct_session.query(Observation).filter(
                Observation.status == 'pending',
                ~Observation.photos.any(Photo.id.in_(user_identified_photos))
            )

            if not is_admin:
                query = query.join(Location, Observation.location_id == Location.id)\
                    .filter(location_filter)

            if scope_location_subq is not None:
                query = query.filter(Observation.location_id.in_(scope_location_subq))

            if ai_observation_subq is not None:
                query = query.filter(Observation.id.in_(ai_observation_subq))

            # Prioritise series closest to consensus: disputed → with votes → recent,
            # within a group — random. Aggregate scoped to pending series,
            # because most identifications belong to already-completed ones.
            pending_votes_subq = (
                select(
                    Photo.observation_id.label('observation_id'),
                    func.count(func.distinct(Identification.user_id)).label('votes'),
                )
                .select_from(Identification)
                .join(Photo, Photo.id == Identification.photo_id)
                .join(Observation, Observation.id == Photo.observation_id)
                .where(Observation.status == 'pending')
                .group_by(Photo.observation_id)
                .subquery()
            )
            query = query.outerjoin(
                pending_votes_subq,
                pending_votes_subq.c.observation_id == Observation.id
            )
            observation = query.order_by(
                func.coalesce(pending_votes_subq.c.votes, 0).desc(),
                func.random(),
            ).first()
        
        if not observation:
            if review_mode:
                message = _('Немає серій для перегляду, які б відповідали критеріям.')
            else:
                message = _('Вітаємо! Ви ідентифікували всі доступні серії фотографій.')
            return jsonify({'message': message}), 404
        
        photos_sorted = sorted(list(observation.photos), key=lambda p: p.captured_at)
        photos_data = []
        for i, photo in enumerate(photos_sorted):
            photos_data.append({
                'id': photo.id,
                'thumbnail_url': url_for('camera_traps.serve_thumbnail', 
                                        lang_code=g.lang_code, 
                                        filename=photo.system_filename, 
                                        _external=True),
                'captured_at': photo.captured_at.strftime('%d.%m.%Y %H:%M:%S'),
                'debug_index': i,
                'debug_filename': photo.system_filename
            })   

        # Debug logging
        current_app.logger.info(f"Photos order for observation {observation.id}:")
        for i, photo_info in enumerate(photos_data):
            current_app.logger.info(f"  Index {i}: {photo_info['debug_filename']} - {photo_info['captured_at']}")   
        
        existing_identifications = []
        if review_mode:
            user_identifications = {}
            
            # Iterate photos in chronological order
            for photo in photos_sorted:
                for ident in photo.identifications:
                    user_id = ident.user_id
                    if user_id not in user_identifications:
                        # We are guaranteed to take the identification from the earliest photo
                        user = User.query.get(ident.user_id)
                        species_name = "Невідомо"
                        if ident.species_id and ident.species_id > 0:
                            species = ct_session.query(Species).get(ident.species_id)
                            if species:
                                if g.lang_code == 'uk' and species.common_name_ua:
                                    species_name = species.common_name_ua
                                elif g.lang_code == 'en' and species.common_name_en:
                                    species_name = species.common_name_en
                                else:
                                    species_name = species.scientific_name
                        elif ident.species_id == -1:
                            species_name = _('Пусто (немає тварин)')
                        elif ident.species_id == -2:
                            species_name = _('Інший вид')
                        elif ident.species_id == -5:
                            species_name = _('Людина')
                        elif ident.species_id == -3:
                            species_name = _('Автомобіль')
                        elif ident.species_id == -4:
                            species_name = _('Мотоцикл')
                        
                        user_identifications[user_id] = {
                            'username': user.username if user else f"User {ident.user_id}",
                            'species_name': species_name,
                            'quantity': ident.quantity,
                            'comment': ident.comment or '',
                            'created_at': ident.created_at.strftime('%d.%m.%Y %H:%M')
                        }
            
            existing_identifications = list(user_identifications.values())
        
        response_data = {
            'observation_id': observation.id,
            'location_name': observation.location.name,
            'photos': photos_data
        }

        # AI prediction for this observation (if present): pass to the front end
        # so JS can pre-fill the species and show the confidence badge.
        try:
            from .ai_runner import is_ai_available, get_observation_ai_prediction
            if is_ai_available():
                ai_pred = get_observation_ai_prediction(observation.id)
                if ai_pred is not None:
                    response_data['ai_prediction'] = ai_pred
        except Exception as e:
            current_app.logger.warning(f"AI: cannot load prediction for obs {observation.id}: {e}")

        if review_mode:
            response_data['existing_identifications'] = existing_identifications
            response_data['review_mode'] = True

        return jsonify(response_data)
    finally:
        close_ct_session()

@camera_traps_bp.route('/api/create-location', methods=['POST'])
@login_required
@role_required('ct_verifier')
def create_location(lang_code):
    ct_session = get_ct_session()
    try:
        data = request.json
        name = data.get('name')
        lat = data.get('latitude')
        lon = data.get('longitude')
        description = data.get('description')
        institution_id = data.get('institution_id')

        if not all([name, lat, lon]):
            return jsonify({'success': False, 'message': _('Назва та координати є обов\'язковими.')}), 400

        existing = ct_session.query(Location).filter(
            func.round(Location.latitude, 5) == round(float(lat), 5),
            func.round(Location.longitude, 5) == round(float(lon), 5)
        ).first()
        if existing:
            return jsonify({'success': False, 'message': _('Місце з такими координатами вже існує: ') + existing.name}), 409

        new_location = Location(
            name=name,
            latitude=lat,
            longitude=lon,
            description=description,
            created_by_id=current_user.id
        )
        ct_session.add(new_location)
        ct_session.flush()

        if institution_id:
            ct_session.execute(
                location_institutions.insert().values(
                    location_id=new_location.id,
                    institution_id=int(institution_id)
                )
            )

        ct_session.commit()
        return jsonify({'success': True, 'message': _('Нове місце успішно створено!'), 'location': {'id': new_location.id, 'name': new_location.name}}), 201
    except Exception as e:
        ct_session.rollback()
        current_app.logger.error(f"Error creating location: {e}")
        return jsonify({'success': False, 'message': _('Помилка створення місця.')}), 500
    finally:
        close_ct_session()

@camera_traps_bp.route('/upload/process', methods=['POST'])
@login_required
@role_required('ct_verifier')
def process_upload(lang_code):
    """Accept uploaded files and pass them for processing."""
    location_id = request.form.get('location_id')
    uploaded_files = request.files.getlist('file')
    if not location_id or location_id in ['-1', '0']:
        return jsonify({'error': _('Неправильно вказане місце.')}), 400
    if not uploaded_files or all(not f.filename for f in uploaded_files):
        return jsonify({'error': _('Файли не були передані.')}), 400
    try:
        process_photo_batch(uploaded_files, int(location_id), current_user)
        return jsonify({'success': True, 'message': _('Файли успішно оброблені!')}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"An unexpected error occurred during upload: {e}")
        return jsonify({'error': _('Сталася невідома помилка під час обробки файлів.')}), 500

@camera_traps_bp.route('/admin/run-cleanup', methods=['POST'])
@login_required
@role_required('admin')
def manual_cleanup(lang_code):
    """Route for manually triggering the old-photo cleanup process."""
    try:
        current_app.logger.info(f"Manual cleanup task triggered by admin: {current_user.username}")
        result = cleanup_old_photos()
        
        if result['success']:
            photos_count = result['photos_deleted']
            observations_count = result['observations_archived']
            
            if photos_count > 0:
                flash(_(f'Очищення завершено успішно. Видалено {photos_count} фотографій з {observations_count} спостережень.'), 'success')
            else:
                flash(_('Очищення завершено. Файлів для видалення не знайдено.'), 'info')
        else:
            flash(_(f'Помилка очищення: {result["error"]}'), 'danger')
            
    except Exception as e:
        current_app.logger.error(f"Manual cleanup task failed. Error: {e}")
        flash(_('Під час процесу очищення сталася несподівана помилка. Перевірте логи.'), 'danger')

    return redirect(url_for('camera_traps.admin_panel', lang_code=g.lang_code))

@camera_traps_bp.route('/thumbnails/<path:filename>')
def serve_thumbnail(lang_code, filename):
    config = current_app.config['CAMERA_TRAP_CONFIG']
    thumbnail_dir = os.path.join(config['UPLOAD_PATH'], 'pending_photos', 'thumbnails')
    return send_from_directory(thumbnail_dir, filename)

@camera_traps_bp.route('/photos/raw/<path:filename>')
def serve_raw_photo(lang_code, filename):
    config = current_app.config['CAMERA_TRAP_CONFIG']
    raw_dir = os.path.join(config['UPLOAD_PATH'], 'pending_photos', 'raw')
    thumb_dir = os.path.join(config['UPLOAD_PATH'], 'pending_photos', 'thumbnails')

    # Build the full path to the original raw file
    full_raw_path = os.path.join(raw_dir, filename)

    # Check whether this file exists on disk
    if os.path.exists(full_raw_path):
        # If it exists, serve it as before
        return send_from_directory(raw_dir, filename)
    else:
        # If the raw file is missing, try serving the thumbnail as a fallback.
        # send_from_directory will automatically return 404 if the thumbnail
        # with this filename does not exist either.
        return send_from_directory(thumb_dir, filename)

# ═════════════════════════════════════════════════════════════════════════════
# CLEANUP (new, since 2026-05-25) — replaces the old manual_batch_cleanup.
# Two-phase: POST /admin/cleanup/analyze → JS shows report → POST execute.
# Polling /admin/cleanup/task/<id> for progress of both phases.
# ═════════════════════════════════════════════════════════════════════════════

@camera_traps_bp.route('/admin/cleanup/analyze', methods=['POST'])
@login_required
@role_required('admin')
def cleanup_analyze(lang_code):
    """Start an asynchronous dry-run. Returns report_id for polling."""
    try:
        config = current_app.config.get('CAMERA_TRAP_CONFIG', {})
        threshold_hours = int(config.get('STALE_BATCH_HOURS', 0))
        probe_seconds = int(config.get('ACTIVE_PROBE_SECONDS', 10))
        from .cleanup import analyze_cleanup
        report_id = analyze_cleanup(
            triggered_by=current_user.id,
            threshold_hours=threshold_hours,
            probe_seconds=probe_seconds,
        )
        return jsonify({
            'success': True,
            'report_id': report_id,
            'message': _('Аналіз запущено. Очікуйте...'),
        }), 202
    except Exception as e:
        current_app.logger.exception(f"cleanup_analyze failed: {e}")
        return jsonify({'error': _('Помилка запуску аналізу')}), 500


@camera_traps_bp.route('/admin/cleanup/execute/<report_id>', methods=['POST'])
@login_required
@role_required('admin')
def cleanup_execute(lang_code, report_id):
    """Execute cleanup based on a completed report. Re-checks active batches."""
    try:
        config = current_app.config.get('CAMERA_TRAP_CONFIG', {})
        probe_seconds = int(config.get('ACTIVE_PROBE_SECONDS', 10))
        from .cleanup import start_execute
        start_execute(report_id=report_id, probe_seconds=probe_seconds)
        return jsonify({
            'success': True,
            'report_id': report_id,
            'message': _('Виконання запущено у фоні.'),
        }), 202
    except ValueError as e:
        # 'not found' / 'expired' / 'wrong status'
        msg = str(e)
        if 'not found' in msg:
            return jsonify({'error': msg}), 404
        if 'expired' in msg:
            return jsonify({'error': msg}), 410
        return jsonify({'error': msg}), 409
    except Exception as e:
        current_app.logger.exception(f"cleanup_execute failed: {e}")
        return jsonify({'error': _('Помилка запуску виконання')}), 500


@camera_traps_bp.route('/admin/cleanup/task/<report_id>', methods=['GET'])
@login_required
@role_required('admin')
def cleanup_task_status(lang_code, report_id):
    """Polling endpoint: return current state of an analyze/execute task."""
    try:
        from .cleanup import get_log
        data = get_log(report_id)
        if data is None:
            return jsonify({'error': _('Запис не знайдено')}), 404
        return jsonify(data), 200
    except Exception as e:
        current_app.logger.exception(f"cleanup_task_status failed: {e}")
        return jsonify({'error': _('Помилка отримання статусу')}), 500

@camera_traps_bp.route('/admin/recalculate-consensus', methods=['POST'])
@login_required
@role_required('admin')
def recalculate_consensus(lang_code):
    """Recalculate consensus for all pending observations according to the current configuration."""
    try:
        current_app.logger.info(f"Consensus recalculation triggered by admin: {current_user.username}")
        from .utils import migrate_pending_observations_to_single_identification
        completed_count = migrate_pending_observations_to_single_identification()
        flash(_(f'Перерахунок завершено. Оновлено {completed_count} спостережень.'), 'success')
    except Exception as e:
        current_app.logger.error(f"Consensus recalculation failed: {e}")
        flash(_('Помилка перерахунку консенсусу. Перевірте логи.'), 'danger')

    return redirect(url_for('camera_traps.admin_panel', lang_code=g.lang_code))

@camera_traps_bp.route('/api/review-filters')
@login_required
@role_required('manager')
def get_review_filters(lang_code):
    """Return the list of users and species for review filters."""
    ct_session = get_ct_session()
    try:
        from app.models import User  # Main database

        # Fetch user_ids from CT DB, then look up users in the main DB
        user_ids_query = ct_session.query(Identification.user_id).distinct().all()
        user_ids = [uid[0] for uid in user_ids_query]
        
        users = User.query.filter(User.id.in_(user_ids)).order_by(User.username).all()
        users_data = [{'id': u.id, 'username': u.username} for u in users]
        
        # Species from CT DB — this works correctly
        species_query = ct_session.query(Species)\
        .join(Identification, Species.id == Identification.species_id)\
        .distinct().order_by(Species.common_name_ua).all()
        
        species = []
        for s in species_query:
            display_name = s.scientific_name
            if g.lang_code == 'uk' and s.common_name_ua:
                display_name = f"{s.common_name_ua} ({s.scientific_name})"
            elif g.lang_code == 'en' and s.common_name_en:
                display_name = f"{s.common_name_en} ({s.scientific_name})"
            species.append({'id': s.id, 'name': display_name})
        
        return jsonify({'users': users_data, 'species': species})
        
    except Exception as e:
        current_app.logger.error(f"Error getting review filters: {e}")
        return jsonify({'error': 'Failed to load filters'}), 500
    finally:
        close_ct_session()

@camera_traps_bp.route('/gallery')
def gallery(lang_code):
    """Gallery of classified photos with access-rights filtering."""
    ct_session = get_ct_session()
    try:
        # Determine user permissions
        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        is_admin = current_user.is_authenticated and current_user.has_role('admin')

        # Build SQL filter (locations table uses alias 'locations')
        inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, table_alias='locations')

        can_manage_favorites = current_user.is_authenticated and current_user.has_role('manager')

        # Base query for the species list
        species_query = ct_session.query(Species)\
            .join(Identification, Species.id == Identification.species_id)\
            .join(Photo, Identification.photo_id == Photo.id)\
            .join(Observation, Photo.observation_id == Observation.id)\
            .join(Location, Observation.location_id == Location.id)\
            .filter(
                Photo.is_favorite == True,
                Photo.status.in_(['completed', 'pending', 'archived']),
                text(inst_condition)
            ).params(**inst_params)

        if not can_manage_favorites:
            species_query = species_query.filter(Species.id > 0)

        species_objects = species_query.distinct().order_by(Species.common_name_ua).all()

        species_list = [{'id': 0, 'text': _('-- Всі види --')}]
        
        for s in species_query:
            display_name = s.scientific_name
            if g.lang_code == 'uk' and s.common_name_ua:
                if s.id < 0:
                    display_name = s.common_name_ua
                else:
                    display_name = f"{s.common_name_ua} ({s.scientific_name})"
            elif g.lang_code == 'en' and s.common_name_en:
                if s.id < 0:
                    display_name = s.common_name_en
                else:
                    display_name = f"{s.common_name_en} ({s.scientific_name})"
            
            species_list.append({'id': s.id, 'text': display_name})

        return render_template('gallery.html', 
                             available_species=species_list, 
                             can_manage_favorites=can_manage_favorites)
        
    except Exception as e:
        current_app.logger.error(f"Error loading gallery: {e}")
        flash(_('Помилка завантаження галереї.'), 'danger')
        return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))
    finally:
        close_ct_session()

@camera_traps_bp.route('/api/gallery/photos')
def get_gallery_photos(lang_code):
   """API for fetching gallery photos respecting access rights."""
   ct_session = get_ct_session()
   try:
       species_id = request.args.get('species_id', type=int)
       if species_id is None:
           return jsonify({'error': 'Species ID is required'}), 400

       # Access rights
       user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
       is_admin = current_user.is_authenticated and current_user.has_role('admin')

       # Build filter
       inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, table_alias='locations')

       can_manage_favorites = current_user.is_authenticated and current_user.has_role('manager')

       # Build query
       query = ct_session.query(Photo)\
           .join(Identification, Photo.id == Identification.photo_id)\
           .join(Species, Identification.species_id == Species.id)\
           .join(Observation, Photo.observation_id == Observation.id)\
           .join(Location, Observation.location_id == Location.id)\
           .filter(
               Photo.is_favorite == True,
               Photo.status.in_(['completed', 'pending', 'archived']),
               text(inst_condition)
           ).params(**inst_params)

       # Filter by species
       if species_id > 0:
           query = query.filter(Species.id == species_id)

       # Hide special categories (Empty, Human, etc.) for regular users
       if not can_manage_favorites:
           query = query.filter(Species.id > 0)

       photos = query.order_by(Photo.captured_at.desc()).all()

       if not photos:
           return jsonify({'message': _('Для вибраного виду немає фото у вибраному.')}), 404

       photos_data = []
       for photo in photos:
           # Fetch info about who marked the photo as favourite
           first_identification = ct_session.query(Identification)\
               .filter(Identification.photo_id == photo.id)\
               .order_by(Identification.created_at)\
               .first()
           
           # Show the name only to managers/admins
           added_by_username = None
           if can_manage_favorites and first_identification:
               user = User.query.get(first_identification.user_id)
               if user:
                   added_by_username = user.username

           # Build species display name
           species_name = "Невідомий вид"
           if first_identification and first_identification.species:
               species = first_identification.species
               if g.lang_code == 'uk' and species.common_name_ua:
                   species_name = species.common_name_ua
               elif g.lang_code == 'en' and species.common_name_en:
                   species_name = species.common_name_en
               else:
                   species_name = species.scientific_name

           photo_data = {
               'id': photo.id,
               'thumbnail_url': url_for('camera_traps.serve_thumbnail', 
                                      lang_code=g.lang_code, 
                                      filename=photo.system_filename, 
                                      _external=True),
               'raw_url': url_for('camera_traps.serve_raw_photo',
                                lang_code=g.lang_code,
                                filename=photo.system_filename,
                                _external=True),
               'captured_at': photo.captured_at.strftime('%d.%m.%Y %H:%M:%S'),
               'location_name': photo.observation.location.name,
               'species_name': species_name,
               'observation_id': photo.observation_id,
               'sequence_number': photo.sequence_number
           }
           
           # Include added_by only for managers
           if added_by_username:
               photo_data['added_by'] = added_by_username
               
           photos_data.append(photo_data)

       return jsonify({
           'photos': photos_data,
           'total_count': len(photos_data),
           'can_manage_favorites': can_manage_favorites
       })

   except Exception as e:
       current_app.logger.error(f"Error getting gallery photos: {e}")
       return jsonify({'error': 'Server error'}), 500
   finally:
       close_ct_session()

@camera_traps_bp.route('/api/gallery/remove-favorite', methods=['POST'])
@login_required
@role_required('manager')
def remove_from_favorites(lang_code):
    """API for removing a photo from favourites (managers only)."""
    ct_session = get_ct_session()
    
    try:
        data = request.json
        photo_id = data.get('photo_id')
        
        if not photo_id:
            return jsonify({'success': False, 'error': 'Photo ID is required'}), 400

        photo = ct_session.query(Photo).get(photo_id)
        if not photo:
            return jsonify({'success': False, 'error': 'Photo not found'}), 404

        photo.is_favorite = False
        ct_session.commit()
        
        current_app.logger.info(f"Photo {photo_id} removed from favorites by user {current_user.username}")
        
        return jsonify({
            'success': True, 
            'message': _('Фото видалено з вибраного')
        })

    except Exception as e:
        ct_session.rollback()
        current_app.logger.error(f"Error removing photo from favorites: {e}")
        return jsonify({'success': False, 'error': 'Server error'}), 500
    finally:
        close_ct_session()

@camera_traps_bp.route('/location/<int:location_id>/coverage')
@login_required
@role_required('manager')
def ct_location_coverage(lang_code, location_id):
    """Camera-trap coverage calendar by day (#38).

    Coverage = "camera was operating": days within deployment intervals
    (start..end) ∪ days with photos, with gaps ≤ COVERAGE_MAX_GAP_DAYS filled
    (photos are trigger-based, so gaps between them don't imply camera downtime).
    Intensity (for shading #43) — photo count per day.
    """
    ct_session = get_ct_session()
    try:
        location = ct_session.query(Location).get(location_id)
        if not location:
            flash(_('Локацію не знайдено.'), 'danger')
            return redirect(url_for('camera_traps.manage_locations', lang_code=g.lang_code))

        # Access: admin sees everything; otherwise the location must belong to the user's institution
        if not current_user.has_role('admin'):
            user_inst_ids = [i.id for i in current_user.institutions]
            allowed = False
            if user_inst_ids:
                allowed = ct_session.execute(
                    select(location_institutions.c.location_id).where(
                        location_institutions.c.location_id == location_id,
                        location_institutions.c.institution_id.in_(user_inst_ids),
                    ).limit(1)
                ).first()
            if not allowed:
                flash(_('Немає доступу до цієї локації.'), 'danger')
                return redirect(url_for('camera_traps.manage_locations', lang_code=g.lang_code))

        cfg = current_app.config.get('CAMERA_TRAP_CONFIG', {})
        max_gap = cfg.get('COVERAGE_MAX_GAP_DAYS', 10)
        good_photos = cfg.get('COVERAGE_GOOD_PHOTOS', 1)

        # 1) Days within deployment intervals (camera was physically present)
        deps = ct_session.query(Deployment.start_date, Deployment.end_date).filter(
            Deployment.location_id == location_id,
            Deployment.start_date.isnot(None),
            Deployment.end_date.isnot(None),
        ).all()
        deployment_days = set()
        for s, e in deps:
            if s and e and e >= s:
                for k in range((e - s).days + 1):
                    deployment_days.add(s + timedelta(days=k))

        # 2) Photos per day (exclude 1900 placeholder dates from photos without EXIF)
        photo_rows = ct_session.query(
            func.date(Photo.captured_at).label('day'),
            func.count(Photo.id).label('cnt'),
        ).join(Observation, Photo.observation_id == Observation.id).filter(
            Observation.location_id == location_id
        ).group_by(func.date(Photo.captured_at)).all()
        photo_counts = {}
        for r in photo_rows:
            d = r.day
            if isinstance(d, datetime):
                d = d.date()
            elif isinstance(d, str):  # SQLite returns DATE() as a string
                d = datetime.strptime(d, '%Y-%m-%d').date()
            if d and d.year >= 2010:
                photo_counts[d] = r.cnt

        mode = request.args.get('mode', 'all')
        if mode not in ('all', 'aggregated'):
            mode = 'all'
        from .utils import build_ct_coverage_calendar, fill_day_gaps
        covered = deployment_days | fill_day_gaps(set(photo_counts), max_gap)
        coverage = build_ct_coverage_calendar(covered, photo_counts,
                                               good_photos=good_photos, mode=mode)

        return render_template(
            'ct_location_coverage.html',
            location_id=location_id,
            location_name=location.name,
            coverage=coverage,
            good_photos=good_photos,
            max_gap=max_gap,
        )
    finally:
        close_ct_session()


@camera_traps_bp.route('/manage-locations')
@login_required
@role_required('manager')
def manage_locations(lang_code):
    """Render the combined location management and maintenance log page."""
    ct_session = get_ct_session()
    try:
        is_admin = current_user.has_role('admin')
        user_inst_ids = [inst.id for inst in current_user.institutions]

        # Filter locations by institution, same logic as in upload
        if is_admin:
            locations_objects = ct_session.query(Location).order_by(Location.name).all()
        elif user_inst_ids:
            locations_objects = (
                ct_session.query(Location)
                .join(location_institutions, Location.id == location_institutions.c.location_id)
                .filter(location_institutions.c.institution_id.in_(user_inst_ids))
                .order_by(Location.name).distinct().all()
            )
        else:
            locations_objects = []

        biotopes = ct_session.query(Biotope).order_by(Biotope.name_ua).all()
        battery_types = ct_session.query(BatteryType).order_by(BatteryType.name_ua).all()
        visit_purposes = ct_session.query(VisitPurpose).order_by(VisitPurpose.name_ua).all()

        # Fetch location–institution links in a single query
        loc_ids = [loc.id for loc in locations_objects]
        if loc_ids:
            inst_rows = ct_session.execute(
                select(location_institutions.c.location_id, location_institutions.c.institution_id)
                .where(location_institutions.c.location_id.in_(loc_ids))
            ).fetchall()
            loc_inst_map = {}
            for row in inst_rows:
                loc_inst_map.setdefault(row.location_id, []).append(row.institution_id)
        else:
            loc_inst_map = {}

        # Institutions for the filter — only those present in visible locations
        used_inst_ids = set()
        for ids in loc_inst_map.values():
            used_inst_ids.update(ids)
        if is_admin and used_inst_ids:
            filter_institutions = Institution.query.filter(
                Institution.id.in_(used_inst_ids)
            ).order_by(Institution.name_uk).all()
        elif used_inst_ids:
            filter_institutions = [i for i in current_user.institutions if i.id in used_inst_ids]
        else:
            filter_institutions = []

        locations_data = []
        for loc in locations_objects:
            locations_data.append({
                'id': loc.id,
                'name': loc.name,
                'latitude': float(loc.latitude),
                'longitude': float(loc.longitude),
                'biotope_ids': [b.id for b in loc.biotopes],
                'has_description': bool(loc.description and loc.description.strip()),
                'institution_ids': loc_inst_map.get(loc.id, [])
            })

        geoserver_url = current_app.config['GEOSERVER_URL']

        # Managers with institutions can also edit/create locations
        can_edit = is_admin or bool(user_inst_ids)

        # List of institutions for the location creation form
        if is_admin:
            user_institutions = Institution.query.order_by(Institution.name_uk).all()
        else:
            user_institutions = list(current_user.institutions)

        return render_template('manage_locations.html',
                               locations=locations_data,
                               biotopes=biotopes,
                               battery_types=battery_types,
                               visit_purposes=visit_purposes,
                               geoserver_url=geoserver_url,
                               can_edit=can_edit,
                               user_institutions=user_institutions,
                               is_admin=is_admin,
                               filter_institutions=filter_institutions)

    except Exception as e:
        current_app.logger.error(f"Error loading location management page: {e}", exc_info=True)
        flash(_("Помилка завантаження сторінки управління локаціями."), 'danger')
        return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))
    finally:
        close_ct_session()

# ── Deployments ──────────────────────────────────────────────────────────────
DEPLOYMENT_STR_FIELDS = ['name', 'study_season', 'study_design', 'camera_id',
                         'camera_model', 'serial_number']
DEPLOYMENT_INT_FIELDS = ['study_year', 'n_days_working', 'n_photos']
DEPLOYMENT_DATE_FIELDS = ['start_date', 'end_date']
DEPLOYMENT_TIME_FIELDS = ['start_time', 'end_time']
DEPLOYMENT_TEXT_FIELDS = ['qc_local_datetime_issue', 'qc_comment']
DEPLOYMENT_BOOL_FIELDS = [
    'qc_non_functional', 'qc_stolen', 'qc_hardware_issue', 'qc_firmware_issue',
    'qc_settings_issue', 'qc_battery_issue', 'qc_sd_issue', 'qc_no_data_uploaded_by_pa',
    'qc_uploaded_data_is_not_raw', 'qc_no_gps_coordinates', 'qc_no_species_captured',
    'qc_placement_incorrect', 'qc_poor_placement', 'qc_feeding_location',
    'qc_installation_incorrect', 'qc_lapse_photos_missed', 'qc_installation_photos_missed',
    'qc_deinstallation_photos_missed', 'qc_distance_reference_photos_missed',
    'qc_datetime_photos_missed', 'qc_local_datetime_not_set', 'qc_data_not_usable',
    'qc_used_brf',
]


def _deployment_to_dict(dep):
    d = {
        'id': dep.id,
        'location_id': dep.location_id,
        'n_days_calc': dep.n_days_calc,
        'history_unknown': dep.history_unknown,
    }
    for f in DEPLOYMENT_STR_FIELDS + DEPLOYMENT_INT_FIELDS + DEPLOYMENT_TEXT_FIELDS:
        d[f] = getattr(dep, f)
    for f in DEPLOYMENT_BOOL_FIELDS:
        d[f] = getattr(dep, f)
    for f in DEPLOYMENT_DATE_FIELDS:
        v = getattr(dep, f)
        d[f] = v.isoformat() if v else None
    for f in DEPLOYMENT_TIME_FIELDS:
        v = getattr(dep, f)
        d[f] = v.strftime('%H:%M') if v else None
    return d


def _apply_deployment_fields(dep, data):
    """Apply submitted fields to a deployment (shared by create/update).
    Raises ValueError on invalid date/number format."""
    from datetime import datetime as _dt
    for f in DEPLOYMENT_STR_FIELDS:
        if f in data:
            val = data.get(f)
            val = val.strip() if isinstance(val, str) and val.strip() else None
            if f == 'name' and not val:
                continue
            setattr(dep, f, val)
    for f in DEPLOYMENT_INT_FIELDS:
        if f in data:
            v = data.get(f)
            setattr(dep, f, int(v) if v not in (None, '', []) else None)
    for f in DEPLOYMENT_TEXT_FIELDS:
        if f in data:
            v = data.get(f)
            setattr(dep, f, v.strip() if isinstance(v, str) and v.strip() else None)
    for f in DEPLOYMENT_BOOL_FIELDS:
        if f in data:
            v = data.get(f)
            setattr(dep, f, None if v is None else bool(v))
    for f in DEPLOYMENT_DATE_FIELDS:
        if f in data:
            v = data.get(f)
            setattr(dep, f, _dt.strptime(v, '%Y-%m-%d').date() if v else None)
    for f in DEPLOYMENT_TIME_FIELDS:
        if f in data:
            v = data.get(f)
            setattr(dep, f, _dt.strptime(v, '%H:%M').time() if v else None)


def _user_can_access_location(ct_session, location_id):
    """admin/quality_control → always; manager → only if the location belongs to their institution."""
    if current_user.has_role('admin') or current_user.has_role('quality_control'):
        return True
    user_inst_ids = [inst.id for inst in current_user.institutions]
    if not user_inst_ids:
        return False
    access = ct_session.execute(
        select(location_institutions.c.location_id).where(
            (location_institutions.c.location_id == location_id) &
            (location_institutions.c.institution_id.in_(user_inst_ids))
        ).limit(1)
    ).fetchone()
    return access is not None


@camera_traps_bp.route('/manage-deployments')
@login_required
@role_required('manager', 'quality_control')
def manage_deployments(lang_code):
    """Deployment management page: location map + deployments table/form."""
    ct_session = get_ct_session()
    try:
        is_admin = current_user.has_role('admin')
        is_full_access = is_admin or current_user.has_role('quality_control')
        user_inst_ids = [inst.id for inst in current_user.institutions]

        if is_full_access:
            locations_objects = ct_session.query(Location).order_by(Location.name).all()
        elif user_inst_ids:
            locations_objects = (
                ct_session.query(Location)
                .join(location_institutions, Location.id == location_institutions.c.location_id)
                .filter(location_institutions.c.institution_id.in_(user_inst_ids))
                .order_by(Location.name).distinct().all()
            )
        else:
            locations_objects = []

        loc_ids = [loc.id for loc in locations_objects]

        # Location–institution links (for the filter)
        if loc_ids:
            inst_rows = ct_session.execute(
                select(location_institutions.c.location_id, location_institutions.c.institution_id)
                .where(location_institutions.c.location_id.in_(loc_ids))
            ).fetchall()
            loc_inst_map = {}
            for row in inst_rows:
                loc_inst_map.setdefault(row.location_id, []).append(row.institution_id)
        else:
            loc_inst_map = {}

        used_inst_ids = set()
        for ids in loc_inst_map.values():
            used_inst_ids.update(ids)
        if is_full_access and used_inst_ids:
            filter_institutions = Institution.query.filter(
                Institution.id.in_(used_inst_ids)).order_by(Institution.name_uk).all()
        elif used_inst_ids:
            filter_institutions = [i for i in current_user.institutions if i.id in used_inst_ids]
        else:
            filter_institutions = []

        # Deployments for visible locations. Admin / quality_control also see deployments
        # without GPS (location_id IS NULL); regular managers — only those with a location.
        dep_q = ct_session.query(Deployment)
        if is_full_access:
            cond = Deployment.location_id.is_(None)
            if loc_ids:
                cond = Deployment.location_id.in_(loc_ids) | cond
            deps = dep_q.filter(cond).order_by(Deployment.location_id.nullslast(),
                                               Deployment.start_date).all()
        elif loc_ids:
            deps = (dep_q.filter(Deployment.location_id.in_(loc_ids))
                    .order_by(Deployment.location_id, Deployment.start_date).all())
        else:
            deps = []
        dep_count_map = {}
        for dep in deps:
            dep_count_map[dep.location_id] = dep_count_map.get(dep.location_id, 0) + 1

        locations_data = []
        for loc in locations_objects:
            locations_data.append({
                'id': loc.id,
                'name': loc.name,
                'latitude': float(loc.latitude),
                'longitude': float(loc.longitude),
                'institution_ids': loc_inst_map.get(loc.id, []),
                'deployment_count': dep_count_map.get(loc.id, 0),
            })

        deployments_data = [{
            'id': dep.id,
            'location_id': dep.location_id,
            'name': dep.name,
            'study_year': dep.study_year,
            'study_season': dep.study_season,
            'start_date': dep.start_date.isoformat() if dep.start_date else None,
            'end_date': dep.end_date.isoformat() if dep.end_date else None,
            'camera_id': dep.camera_id,
            'qc_data_not_usable': dep.qc_data_not_usable,
        } for dep in deps]

        years = sorted({dep.study_year for dep in deps if dep.study_year}, reverse=True)

        return render_template('manage_deployments.html',
                               locations=locations_data,
                               deployments_data=deployments_data,
                               geoserver_url=current_app.config['GEOSERVER_URL'],
                               filter_institutions=filter_institutions,
                               years=years,
                               is_admin=is_full_access,  # quality_control also sees everything
                               bool_fields=DEPLOYMENT_BOOL_FIELDS)
    except Exception as e:
        current_app.logger.error(f"Error loading deployment management page: {e}", exc_info=True)
        flash(_("Помилка завантаження сторінки управління деплойментами."), 'danger')
        return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))
    finally:
        close_ct_session()


@camera_traps_bp.route('/api/deployment/<int:deployment_id>')
@login_required
@role_required('manager', 'quality_control')
def api_get_deployment(lang_code, deployment_id):
    ct_session = get_ct_session()
    try:
        dep = ct_session.query(Deployment).get(deployment_id)
        if not dep:
            return jsonify({'error': _('Деплоймент не знайдено.')}), 404
        if not _user_can_access_location(ct_session, dep.location_id):
            return jsonify({'error': _('Немає доступу.')}), 403
        return jsonify(_deployment_to_dict(dep))
    finally:
        close_ct_session()


@camera_traps_bp.route('/api/update-deployment/<int:deployment_id>', methods=['POST'])
@login_required
@role_required('manager', 'quality_control')
def update_deployment(lang_code, deployment_id):
    """Update deployment fields. Managers may only edit deployments for their institution's locations."""
    ct_session = get_ct_session()
    try:
        dep = ct_session.query(Deployment).get(deployment_id)
        if not dep:
            return jsonify({'success': False, 'error': _('Деплоймент не знайдено.')}), 404
        if not _user_can_access_location(ct_session, dep.location_id):
            return jsonify({'success': False, 'error': _('Немає доступу до цього деплойменту.')}), 403

        data = request.json or {}
        if 'name' in data and not (data.get('name') or '').strip():
            return jsonify({'success': False, 'error': _('Назва деплойменту обов\'язкова.')}), 400

        _apply_deployment_fields(dep, data)
        ct_session.commit()
        return jsonify({'success': True, 'message': _('Деплоймент оновлено успішно!'),
                        'deployment': _deployment_to_dict(dep)})
    except ValueError:
        ct_session.rollback()
        return jsonify({'success': False, 'error': _('Невірний формат дати/числа.')}), 400
    except Exception as e:
        ct_session.rollback()
        current_app.logger.error(f"Error updating deployment {deployment_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': _('Помилка збереження даних.')}), 500
    finally:
        close_ct_session()


@camera_traps_bp.route('/api/deployment/create', methods=['POST'])
@login_required
@role_required('manager', 'quality_control')
def api_create_deployment(lang_code):
    """Create a new deployment for the selected location."""
    ct_session = get_ct_session()
    try:
        data = request.json or {}
        location_id = data.get('location_id')
        if not location_id:
            return jsonify({'success': False, 'error': _('Не вказано локацію.')}), 400
        location_id = int(location_id)
        if not _user_can_access_location(ct_session, location_id):
            return jsonify({'success': False, 'error': _('Немає доступу до цієї локації.')}), 403
        if not ct_session.query(Location).get(location_id):
            return jsonify({'success': False, 'error': _('Локацію не знайдено.')}), 404

        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({'success': False, 'error': _('Назва деплойменту обов\'язкова.')}), 400

        dep = Deployment(location_id=location_id, name=name, created_by_id=current_user.id)
        _apply_deployment_fields(dep, data)
        ct_session.add(dep)
        ct_session.commit()
        return jsonify({'success': True, 'message': _('Деплоймент створено успішно!'),
                        'deployment': _deployment_to_dict(dep)})
    except ValueError:
        ct_session.rollback()
        return jsonify({'success': False, 'error': _('Невірний формат дати/числа.')}), 400
    except Exception as e:
        ct_session.rollback()
        current_app.logger.error(f"Error creating deployment: {e}", exc_info=True)
        return jsonify({'success': False, 'error': _('Помилка збереження даних.')}), 500
    finally:
        close_ct_session()


@camera_traps_bp.route('/api/deployment/<int:deployment_id>/delete', methods=['POST'])
@login_required
@role_required('manager', 'quality_control')
def delete_deployment(lang_code, deployment_id):
    """Delete a deployment. Managers may only delete deployments for their institution's locations."""
    ct_session = get_ct_session()
    try:
        dep = ct_session.query(Deployment).get(deployment_id)
        if not dep:
            return jsonify({'success': False, 'error': _('Деплоймент не знайдено.')}), 404
        if not _user_can_access_location(ct_session, dep.location_id):
            return jsonify({'success': False, 'error': _('Немає доступу до цього деплойменту.')}), 403
        location_id = dep.location_id
        ct_session.delete(dep)
        ct_session.commit()
        return jsonify({'success': True, 'message': _('Деплоймент видалено.'),
                        'location_id': location_id})
    except Exception as e:
        ct_session.rollback()
        current_app.logger.error(f"Error deleting deployment {deployment_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': _('Помилка видалення.')}), 500
    finally:
        close_ct_session()


# Export: (column header as in the original Excel → Deployment model attribute)
DEPLOYMENT_EXPORT_QC = [
    ('qc_non_functional', 'qc_non_functional'),
    ('qc_stolen', 'qc_stolen'),
    ('qc_hardware_issue', 'qc_hardware_issue'),
    ('qc_firmware_issue', 'qc_firmware_issue'),
    ('qc_settings_issue', 'qc_settings_issue'),
    ('qc_battery_issue', 'qc_battery_issue'),
    ('qc_sd_issue', 'qc_sd_issue'),
    ('qc_no_data_uploaded_by_PA', 'qc_no_data_uploaded_by_pa'),
    ('qc_uploaded_data_is_not_raw', 'qc_uploaded_data_is_not_raw'),
    ('qc_no_GPS_coordinates', 'qc_no_gps_coordinates'),
    ('qc_no_species_captured', 'qc_no_species_captured'),
    ('qc_placement_incorrect', 'qc_placement_incorrect'),
    ('qc_poor_placement', 'qc_poor_placement'),
    ('qc_feeding_location', 'qc_feeding_location'),
    ('qc_installation_incorrect', 'qc_installation_incorrect'),
    ('qc_lapse_photos_missed', 'qc_lapse_photos_missed'),
    ('qc_installation_photos_missed', 'qc_installation_photos_missed'),
    ('qc_deinstallation_photos_missed', 'qc_deinstallation_photos_missed'),
    ('qc_distance_reference_photos_missed', 'qc_distance_reference_photos_missed'),
    ('qc_datetime_photos_missed', 'qc_datetime_photos_missed'),
    ('qc_local_datetime_not_set', 'qc_local_datetime_not_set'),
    ('qc_local_datetime_issue', 'qc_local_datetime_issue'),
    ('qc_data_not_usable', 'qc_data_not_usable'),
    ('qc_used_brf', 'qc_used_brf'),
    ('qc_comment', 'qc_comment'),
]


def _resolve_export_location_ids(ct_session, is_admin, user_inst_ids, institution_id):
    """Return a list of location_ids respecting the user's role and an optional institution filter."""
    if institution_id:
        if not is_admin and institution_id not in user_inst_ids:
            return None  # no access
        target = [institution_id]
    elif is_admin:
        target = None  # all locations
    else:
        target = user_inst_ids
    if target is None:
        return [lid for (lid,) in ct_session.query(Location.id).all()]
    rows = ct_session.execute(
        select(location_institutions.c.location_id)
        .where(location_institutions.c.institution_id.in_(target)).distinct()
    ).fetchall()
    return [r[0] for r in rows]


@camera_traps_bp.route('/export-deployments')
@login_required
@role_required('manager', 'quality_control')
def export_deployments(lang_code):
    """Export deployments to Excel respecting filters (institution, year).
    File structure mirrors the original ARD Excel plus institution name and ecoregion."""
    import io
    import pandas as pd

    ct_session = get_ct_session()
    try:
        is_admin = current_user.has_role('admin')
        is_full_access = is_admin or current_user.has_role('quality_control')
        user_inst_ids = [inst.id for inst in current_user.institutions]

        institution_id = request.args.get('institution_id', type=int)
        year = request.args.get('year', type=int)

        loc_ids = _resolve_export_location_ids(ct_session, is_full_access, user_inst_ids, institution_id)
        if loc_ids is None:
            flash(_('Немає доступу до цієї установи.'), 'danger')
            return redirect(url_for('camera_traps.manage_deployments', lang_code=lang_code))

        q = ct_session.query(Deployment).filter(Deployment.location_id.in_(loc_ids)) if loc_ids else None
        deps = []
        if loc_ids:
            if year:
                q = q.filter(Deployment.study_year == year)
            deps = q.order_by(Deployment.study_year, Deployment.location_id, Deployment.name).all()

        # Locations (coordinates) + location → institution map
        locs = {l.id: l for l in ct_session.query(Location).filter(Location.id.in_(loc_ids)).all()} if loc_ids else {}
        loc_inst = {}
        if loc_ids:
            for row in ct_session.execute(
                select(location_institutions.c.location_id, location_institutions.c.institution_id)
                .where(location_institutions.c.location_id.in_(loc_ids))
            ).fetchall():
                loc_inst.setdefault(row.location_id, row.institution_id)  # first institution

        inst_ids = set(loc_inst.values())
        inst_map = {i.id: i for i in Institution.query.filter(Institution.id.in_(inst_ids)).all()} if inst_ids else {}

        uk = (lang_code == 'uk')

        def inst_name(inst):
            if not inst:
                return None
            return inst.name_uk if uk else (inst.name_en or inst.name_uk)

        def region_name(inst):
            if not inst:
                return None
            return inst.ecoregion_uk if uk else (inst.ecoregion_en or inst.ecoregion_uk)

        rows = []
        for dep in deps:
            loc = locs.get(dep.location_id)
            inst = inst_map.get(loc_inst.get(dep.location_id))
            row = {
                'study_area_id': inst.code if inst else None,
                'study_area_name_EN': inst_name(inst),
                'region_name_EN': region_name(inst),
                'study_year': dep.study_year,
                'study_season': dep.study_season,
                'study_design': dep.study_design,
                'camera_id': dep.camera_id,
                'latitude': float(loc.latitude) if loc else None,
                'longitude': float(loc.longitude) if loc else None,
                'start_date': dep.start_date.isoformat() if dep.start_date else None,
                'start_time': dep.start_time.strftime('%H:%M') if dep.start_time else None,
                'end_date': dep.end_date.isoformat() if dep.end_date else None,
                'end_time': dep.end_time.strftime('%H:%M') if dep.end_time else None,
                'n_days_working': dep.n_days_working,
                'n_photos': dep.n_photos,
                'camera_model': dep.camera_model,
                'serial_number': dep.serial_number,
                'deployment_id': dep.name,
            }
            for header, attr in DEPLOYMENT_EXPORT_QC:
                row[header] = getattr(dep, attr)
            rows.append(row)

        columns = ['study_area_id', 'study_area_name_EN', 'region_name_EN', 'study_year',
                   'study_season', 'study_design', 'camera_id', 'latitude', 'longitude',
                   'start_date', 'start_time', 'end_date', 'end_time', 'n_days_working',
                   'n_photos', 'camera_model', 'serial_number', 'deployment_id'] + \
                  [h for h, _a in DEPLOYMENT_EXPORT_QC]

        df = pd.DataFrame(rows, columns=columns)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='deployments', index=False)
        buf.seek(0)

        parts = ['deployments']
        if institution_id and inst_map:
            code = next(iter(inst_map.values())).code
            if code:
                parts.append(str(code))
        if year:
            parts.append(str(year))
        filename = '_'.join(parts) + '.xlsx'

        return Response(
            buf.getvalue(),
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': f'attachment;filename={filename}'}
        )
    except Exception as e:
        current_app.logger.error(f"Error exporting deployments: {e}", exc_info=True)
        flash(_('Помилка експорту.'), 'danger')
        return redirect(url_for('camera_traps.manage_deployments', lang_code=lang_code))
    finally:
        close_ct_session()


# Derived QC fields (computed on the fly, as in the R location-analysis script).
# Critical / non-critical — for sort order and chart colouring.
QUALITY_FIELDS_CRITICAL = [
    'qc_summary', 'qc_data_not_usable', 'qc_no_gps_coordinates', 'qc_feeding_location',
    'qc_installation_incorrect', 'qc_placement_incorrect', 'qc_hardware_issue',
    'qc_no_data_uploaded_by_pa', 'qc_uploaded_data_is_not_raw', 'qc_lapse_photos_missed',
    'qc_local_datetime_not_set', 'qc_sd_issue', 'qc_battery_issue', 'qc_non_functional',
]
QUALITY_FIELDS_NONCRITICAL = [
    'qc_poor_placement', 'qc_no_species_captured', 'qc_min_days_not_reached',
    'qc_installation_photos_missed', 'qc_deinstallation_photos_missed',
    'qc_distance_reference_photos_missed', 'qc_datetime_photos_missed',
    'qc_settings_issue', 'qc_firmware_issue', 'qc_stolen',
]

# QC filter order as in the original Excel (+ derived summary fields at the end).
QC_FILTER_ORDER = [
    'qc_non_functional', 'qc_stolen', 'qc_hardware_issue', 'qc_firmware_issue',
    'qc_settings_issue', 'qc_battery_issue', 'qc_sd_issue', 'qc_no_data_uploaded_by_pa',
    'qc_uploaded_data_is_not_raw', 'qc_no_gps_coordinates', 'qc_no_species_captured',
    'qc_placement_incorrect', 'qc_poor_placement', 'qc_feeding_location',
    'qc_installation_incorrect', 'qc_lapse_photos_missed', 'qc_installation_photos_missed',
    'qc_deinstallation_photos_missed', 'qc_distance_reference_photos_missed',
    'qc_datetime_photos_missed', 'qc_local_datetime_not_set', 'qc_data_not_usable',
    'qc_summary', 'qc_min_days_not_reached',
]


def _b(v):
    """Treat None as False for quality logic (where we need to check whether there is a problem)."""
    return bool(v) if v is not None else False


def _kor(*vals):
    """Three-valued OR (as in R). True dominates; in the absence of True, NA yields NA."""
    has_na = False
    for v in vals:
        if v is True:
            return True
        if v is None:
            has_na = True
    return None if has_na else False


def _kand(a, b):
    """Three-valued AND. False dominates; in the absence of False, NA yields NA."""
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


@camera_traps_bp.route('/data-quality')
@login_required
@role_required('manager', 'quality_control')
def data_quality(lang_code):
    """Data quality assessment page: map + interactive QC charts.
    Derived fields are computed as in R script 01_Camera_trap_location_analysis."""
    ct_session = get_ct_session()
    try:
        is_admin = current_user.has_role('admin')
        is_full_access = is_admin or current_user.has_role('quality_control')
        user_inst_ids = [inst.id for inst in current_user.institutions]
        # _resolve_export_location_ids takes is_admin — pass True for quality_control so
        # it gets ALL locations (full access).
        loc_ids = _resolve_export_location_ids(ct_session, is_full_access, user_inst_ids, None)
        if not loc_ids:
            loc_ids = []

        # Admin / quality_control also see deployments without GPS (location_id IS NULL).
        dep_q = ct_session.query(Deployment)
        if is_full_access:
            cond = Deployment.location_id.is_(None)
            if loc_ids:
                cond = Deployment.location_id.in_(loc_ids) | cond
            deps = dep_q.filter(cond).all()
        else:
            deps = dep_q.filter(Deployment.location_id.in_(loc_ids)).all() if loc_ids else []
        locs = {l.id: l for l in ct_session.query(Location).filter(Location.id.in_(loc_ids)).all()} \
            if loc_ids else {}
        loc_inst = {}
        if loc_ids:
            for row in ct_session.execute(
                select(location_institutions.c.location_id, location_institutions.c.institution_id)
                .where(location_institutions.c.location_id.in_(loc_ids))
            ).fetchall():
                loc_inst.setdefault(row.location_id, row.institution_id)
        inst_map = {i.id: i for i in Institution.query.filter(
            Institution.id.in_(set(loc_inst.values()))).all()} if loc_inst else {}
        uk = (lang_code == 'uk')

        records = []
        for dep in deps:
            loc = locs.get(dep.location_id)
            inst = inst_map.get(loc_inst.get(dep.location_id))
            lat = float(loc.latitude) if loc and loc.latitude is not None else None
            lon = float(loc.longitude) if loc and loc.longitude is not None else None

            n_days = dep.n_days_calc
            if n_days is None and dep.start_date and dep.end_date:
                n_days = (dep.end_date - dep.start_date).days

            qc_no_gps = (lat is None or lon is None)  # always True/False
            # Three-valued logic (as in R): NA OR NA = NA; NA OR TRUE = TRUE; FALSE OR FALSE = FALSE.
            data_not_usable = _kor(
                dep.qc_data_not_usable,
                qc_no_gps,
                dep.qc_feeding_location,
                dep.qc_hardware_issue,
                _kand(dep.qc_installation_incorrect, dep.qc_no_species_captured),
                _kand(dep.qc_placement_incorrect,    dep.qc_no_species_captured),
                _kand(dep.qc_poor_placement,         dep.qc_no_species_captured),
            )
            qc_summary = _kor(
                data_not_usable, dep.qc_no_data_uploaded_by_pa,
                dep.qc_sd_issue, dep.qc_stolen, dep.qc_non_functional,
            )
            min_days_not_reached = None
            if n_days is not None and dep.study_season:
                if dep.study_season == 'Winter':
                    min_days_not_reached = n_days < 100
                elif dep.study_season == 'Summer':
                    min_days_not_reached = n_days < 60

            # For map marker status, treat None as "no problem" (None != True).
            if qc_summary is True:
                status = 'issue'
            elif dep.study_season == 'Summer':
                status = 'normal_summer'
            else:
                status = 'normal_winter'

            rec = {
                'id': dep.id, 'name': dep.name, 'location_id': dep.location_id,
                'lat': lat, 'lon': lon, 'status': status,
                'region': (inst.ecoregion_uk if uk else (inst.ecoregion_en or inst.ecoregion_uk)) if inst else None,
                'study_area': (inst.name_uk if uk else (inst.name_en or inst.name_uk)) if inst else None,
                'study_area_id': inst.code if inst else None,
                'study_year': dep.study_year, 'study_season': dep.study_season,
                'study_design': dep.study_design, 'camera_model': dep.camera_model,
                'camera_id': dep.camera_id, 'n_days_working': n_days, 'n_photos': dep.n_photos,
                # derived QC fields
                'qc_no_gps_coordinates': qc_no_gps,
                'qc_data_not_usable': data_not_usable,
                'qc_summary': qc_summary,
                'qc_min_days_not_reached': min_days_not_reached,
            }
            for f in DEPLOYMENT_BOOL_FIELDS:
                if f not in rec:  # don't overwrite derived fields
                    rec[f] = dep.__getattribute__(f)
            records.append(rec)

        # Categorical filter options
        def distinct(key):
            return sorted({r[key] for r in records if r[key] not in (None, '')},
                          key=lambda x: str(x))
        filter_options = {
            'region': distinct('region'),
            'study_area': distinct('study_area'),
            'study_year': sorted({r['study_year'] for r in records if r['study_year']}, reverse=True),
            'study_season': distinct('study_season'),
            'study_design': distinct('study_design'),
            'camera_model': distinct('camera_model'),
        }

        return render_template('data_quality.html',
                               records=records,
                               filter_options=filter_options,
                               qc_fields=QC_FILTER_ORDER,
                               geoserver_url=current_app.config['GEOSERVER_URL'])
    except Exception as e:
        current_app.logger.error(f"Error loading data quality page: {e}", exc_info=True)
        flash(_("Помилка завантаження сторінки якості даних."), 'danger')
        return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))
    finally:
        close_ct_session()


@camera_traps_bp.route('/api/update-location/<int:location_id>', methods=['POST'])
@login_required
@role_required('manager')
def update_location(lang_code, location_id):
    """API for updating location data. Managers may only update locations of their own institutions."""
    ct_session = get_ct_session()
    try:
        is_admin = current_user.has_role('admin')

        # Check access to the location by institution
        if not is_admin:
            user_inst_ids = [inst.id for inst in current_user.institutions]
            if not user_inst_ids:
                return jsonify({'success': False, 'error': _('Немає доступу.')}), 403
            access = ct_session.execute(
                select(location_institutions.c.location_id).where(
                    (location_institutions.c.location_id == location_id) &
                    (location_institutions.c.institution_id.in_(user_inst_ids))
                ).limit(1)
            ).fetchone()
            if not access:
                return jsonify({'success': False, 'error': _('Немає доступу до цієї локації.')}), 403

        data = request.json
        location = ct_session.query(Location).get(location_id)
        if not location:
            return jsonify({'success': False, 'error': _('Локацію не знайдено.')}), 404

        location.name = data.get('name', location.name)
        location.latitude = data.get('latitude', location.latitude)
        location.longitude = data.get('longitude', location.longitude)
        location.description = data.get('description', location.description)

        biotope_ids = data.get('biotope_ids', [])
        selected_biotopes = ct_session.query(Biotope).filter(Biotope.id.in_(biotope_ids)).all()
        location.biotopes = selected_biotopes

        # Update institutions
        new_inst_ids = data.get('institution_ids')
        if new_inst_ids is not None:
            if not is_admin:
                if not all(i_id in user_inst_ids for i_id in new_inst_ids):
                    return jsonify({'success': False, 'error': _('Немає доступу.')}), 403
                ct_session.execute(
                    location_institutions.delete().where(
                        (location_institutions.c.location_id == location_id) &
                        (location_institutions.c.institution_id.in_(user_inst_ids))
                    )
                )
            else:
                ct_session.execute(
                    location_institutions.delete().where(
                        location_institutions.c.location_id == location_id
                    )
                )
            if new_inst_ids:
                ct_session.execute(
                    location_institutions.insert(),
                    [{'location_id': location_id, 'institution_id': i_id} for i_id in new_inst_ids]
                )

        ct_session.commit()
        return jsonify({'success': True, 'message': _('Дані локації оновлено успішно!')})
    except Exception as e:
        ct_session.rollback()
        current_app.logger.error(f"Error updating location {location_id}: {e}")
        return jsonify({'success': False, 'error': _('Помилка збереження даних.')}), 500
    finally:
        close_ct_session()

@camera_traps_bp.route('/api/location/create', methods=['POST'])
@login_required
@role_required('manager')
def api_create_location_admin(lang_code):
    """API for creating a new location. Managers may only create locations for their own institutions."""
    ct_session = get_ct_session()
    try:
        is_admin = current_user.has_role('admin')
        user_inst_ids = [inst.id for inst in current_user.institutions]

        data = request.json
        name = (data.get('name') or '').strip()
        description = (data.get('description') or '').strip()
        lat = data.get('lat')
        lon = data.get('lon')
        biotope_ids = data.get('biotope_ids', [])
        institution_ids = [int(i) for i in (data.get('institution_ids') or []) if i]

        if not name:
            return jsonify({'success': False, 'error': _('Вкажіть назву локації.')}), 400
        if lat is None or lon is None:
            return jsonify({'success': False, 'error': _('Вкажіть координати.')}), 400

        # Validation: managers can only assign their own institutions
        if institution_ids and not is_admin:
            if not all(i_id in user_inst_ids for i_id in institution_ids):
                return jsonify({'success': False, 'error': _('Немає доступу до цієї установи.')}), 403

        new_location = Location(
            name=name,
            description=description or None,
            latitude=lat,
            longitude=lon,
            created_by_id=current_user.id
        )
        if biotope_ids:
            selected_biotopes = ct_session.query(Biotope).filter(Biotope.id.in_(biotope_ids)).all()
            new_location.biotopes = selected_biotopes

        ct_session.add(new_location)
        ct_session.flush()
        location_id = new_location.id

        if institution_ids:
            ct_session.execute(
                location_institutions.insert(),
                [{'location_id': location_id, 'institution_id': i_id} for i_id in institution_ids]
            )

        ct_session.commit()

        current_app.logger.info(
            f"User {current_user.username} created CT location '{name}' (id={location_id}, inst={institution_ids})"
        )
        return jsonify({'success': True, 'message': _('Локацію створено успішно!'), 'location_id': location_id}), 201

    except Exception as e:
        ct_session.rollback()
        current_app.logger.error(f"Error creating CT location: {e}", exc_info=True)
        return jsonify({'success': False, 'error': _('Помилка сервера при створенні локації.')}), 500
    finally:
        close_ct_session()

@camera_traps_bp.route('/admin/delete-unfavorited-originals', methods=['POST'])
@login_required
@role_required('admin')
def manual_delete_originals(lang_code):
    """Route for manually triggering deletion of original files not marked as favourite."""
    try:
        current_app.logger.info(f"Manual deletion of unfavorited originals triggered by admin: {current_user.username}")

        # Import the helper
        from .background_tasks import delete_unfavorited_raw_files
        
        result = delete_unfavorited_raw_files()
        
        if result.get('success'):
            flash(_(result.get('message', 'Процес видалення файлів успішно завершено.')), 'success')
        else:
            flash(_(f"Помилка під час видалення: {result.get('error', 'Невідома помилка.')}"), 'danger')
            
    except Exception as e:
        current_app.logger.error(f"Manual deletion task failed. Error: {e}")
        flash(_('Під час процесу видалення файлів сталася несподівана помилка. Перевірте логи.'), 'danger')

    return redirect(url_for('camera_traps.admin_panel', lang_code=g.lang_code))

@camera_traps_bp.route('/admin/run-analytics', methods=['POST'])
@login_required
@role_required('admin')
def manual_run_analytics(lang_code):
    """
    Start a FULL analytics recalculation in the background and return immediately.

    Previously the recalculation (~3 min) ran synchronously and exceeded
    gunicorn --timeout → worker killed → 500. Now start_async_analytics
    launches a threading thread while the client polls /admin/analytics/status.

    Responds in two ways:
      • AJAX (Accept: application/json) → 202/409 JSON (for polling UI);
      • regular form POST → flash + redirect (graceful fallback without JS).
    """
    from .analytics_calculator import start_async_analytics

    wants_json = request.accept_mimetypes.best == 'application/json' \
        or request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    try:
        current_app.logger.info(
            f"Manual analytics recalculation triggered by admin: {current_user.username}"
        )
        started = start_async_analytics(triggered_by=current_user.id)

        if started:
            if wants_json:
                return jsonify({'success': True, 'status': 'running',
                                'message': _('Перерахунок аналітики запущено у фоні.')}), 202
            flash(_('Перерахунок аналітики запущено у фоні. Це може зайняти кілька '
                    'хвилин — статус оновиться на цій сторінці.'), 'info')
        else:
            if wants_json:
                return jsonify({'success': False, 'status': 'running',
                                'message': _('Перерахунок аналітики вже виконується.')}), 409
            flash(_('Перерахунок аналітики вже виконується. Зачекайте завершення.'), 'warning')

    except Exception as e:
        current_app.logger.error(f"Manual analytics recalculation failed to start: {e}", exc_info=True)
        if wants_json:
            return jsonify({'error': _('Не вдалося запустити перерахунок аналітики.')}), 500
        flash(_('Не вдалося запустити перерахунок аналітики. Перевірте логи.'), 'danger')

    return redirect(url_for('camera_traps.admin_panel', lang_code=g.lang_code))


@camera_traps_bp.route('/admin/analytics/status', methods=['GET'])
@login_required
@role_required('admin')
def analytics_status(lang_code):
    """Polling endpoint: return current state of the background analytics recalculation."""
    from .analytics_calculator import get_analytics_status
    try:
        return jsonify(get_analytics_status()), 200
    except Exception as e:
        current_app.logger.exception(f"analytics_status failed: {e}")
        return jsonify({'error': _('Помилка отримання статусу')}), 500

@camera_traps_bp.route('/data-export')
@login_required
@role_required('analyst')
def ct_data_export(lang_code):
    """
    Page for preparing and exporting data from the camera-traps module.
    Access: any user with can_export=True for at least one institution; admin — no restrictions.
    """
    g.lang_code = lang_code
    try:
        is_admin = current_user.has_role('admin')
        name_col = Institution.name_en if lang_code == 'en' else Institution.name_uk
        if is_admin:
            user_institutions = Institution.query.order_by(name_col).all()
        else:
            sort_key = (lambda i: i.name_en or i.name_uk) if lang_code == 'en' else (lambda i: i.name_uk)
            user_institutions = sorted(current_user.export_institutions, key=sort_key)
        return render_template('ct_data_export.html',
                               user_institutions=user_institutions,
                               is_admin=is_admin)
    except Exception as e:
        current_app.logger.error(f"Error loading CT Data export page: {e}", exc_info=True)
        flash('Помилка завантаження сторінки експорту.', 'danger')
        return redirect(url_for('camera_traps.dashboard', lang_code=lang_code))

@camera_traps_bp.route('/api/get-taxonomic-filters')
@login_required
def api_get_ct_taxonomic_filters(lang_code):
    """API for fetching dynamic taxonomic filters for camera traps."""
    session = None
    try:
        session = get_ct_session()
        conn = session.connection()

        p = {
            'class': request.args.get('class'),
            'order': request.args.get('order'),
            'family': request.args.get('family'),
            'genus': request.args.get('genus'),
        }

        filter_type = request.args.get('filter_type', 'species_only')
        
        def fetch_distinct(column, filter_by):
            conditions = [f"s.{column} IS NOT NULL"]
            if filter_type == 'species_only':
                conditions.append("s.id > 0")
            params = {}
            for key, value in filter_by.items():
                if value:
                    # Map request parameter names to model column names
                    db_column = 'class' if key == 'class' else 'order_rank' if key == 'order' else key
                    conditions.append(f"s.{db_column} = :{key}")
                    params[key] = value
            
            where_clause = "WHERE " + " AND ".join(conditions)
            # Quote the "class" column name to avoid an SQL keyword conflict
            query_column = '"class"' if column == 'class' else column
            query = text(f"SELECT DISTINCT s.{query_column} FROM species s {where_clause} ORDER BY s.{query_column}")
            return [row[0] for row in conn.execute(query, params).fetchall()]

        def fetch_distinct_species(conn, lang_code, filters):
            conditions = []
            if filter_type == 'species_only':
                conditions.append("s.id > 0")
            params = {}
            for key, value in filters.items():
                if value:
                    db_column = '"class"' if key == 'class' else 'order_rank' if key == 'order' else key
                    conditions.append(f"s.{db_column} = :{key}")
                    params[key] = value

            where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
            query = text(f"""
                SELECT s.id, s.scientific_name, s.common_name_ua, s.common_name_en
                FROM species s {where_clause} ORDER BY s.scientific_name
            """)
            result = conn.execute(query, params).fetchall()
            
            species_list = []
            for row in result:
                display_name = row.scientific_name
                if lang_code == 'uk' and row.common_name_ua:
                    display_name = f"{row.common_name_ua} ({row.scientific_name})"
                elif lang_code == 'en' and row.common_name_en:
                    display_name = f"{row.common_name_en} ({row.scientific_name})"
                species_list.append({'id': row.id, 'text': display_name})
            return species_list

        response_data = {
            'classes': fetch_distinct('class', {}),
            'orders': fetch_distinct('order_rank', {'class': p['class']}),
            'families': fetch_distinct('family', {'class': p['class'], 'order': p['order']}),
            'genera': fetch_distinct('genus', {'class': p['class'], 'order': p['order'], 'family': p['family']}),
        }
        
        species_filters = {k: v for k, v in p.items() if v}
        response_data['species'] = fetch_distinct_species(conn, lang_code, species_filters)
        
        return jsonify(response_data)
    except Exception as e:
        current_app.logger.error(f"Error fetching CT taxonomic filters: {e}", exc_info=True)
        return jsonify({'error': 'Failed to load filter data'}), 500
    finally:
        if session:
            session.close()

def _get_export_institution_ids():
    """
    Return the institution_ids for the current request, respecting access rights.
    - admin: may select any; if not provided — None (no restriction).
    - others: intersection of requested and those where can_export=True; if not provided — all allowed.
    """
    is_admin = current_user.has_role('admin')
    allowed_ids = None if is_admin else {i.id for i in current_user.export_institutions}

    raw = request.args.get('institution_ids', '')
    if raw:
        requested = [int(x) for x in raw.split(',') if x.strip().isdigit()]
        if is_admin:
            return requested if requested else None
        # Filter to those that are in the export-allowed set
        valid = [i for i in requested if i in allowed_ids]
        return valid if valid else list(allowed_ids)
    else:
        # Nothing passed — admin: no restriction; others: all export-allowed
        return None if is_admin else list(allowed_ids)


@camera_traps_bp.route('/api/data-preview')
@login_required
@role_required('analyst')
def api_ct_data_preview(lang_code):
    """API for previewing camera-trap occurrence data."""
    try:
        filters = {
            'species_ids': [int(sid) for sid in request.args.get('species_ids', '').split(',') if sid],
            'genus': request.args.get('genus'),
            'family': request.args.get('family'),
            'order': request.args.get('order'),
            'class': request.args.get('class'),
            'start_date': request.args.get('start_date'),
            'end_date': request.args.get('end_date'),
            'aggregation': request.args.get('aggregation', 'none'),
            'aggregation_minutes': request.args.get('aggregation_minutes', 5),
            'institution_code': request.args.get('institution_code', 'RSNR'),
            'filter_type': request.args.get('filter_type', 'species_only'),
            'institution_ids': _get_export_institution_ids(),
            'qc_exclude': [f for f in request.args.get('qc_exclude', '').split(',') if f],
        }

        result = get_ct_occurrence_data(filters, limit=20)

        return jsonify({
            'preview_data': result['data'],
            'total_count': result['total_count']
        })
    except Exception as e:
        current_app.logger.error(f"Error previewing CT data: {e}", exc_info=True)
        return jsonify({'error': 'Помилка на сервері при підготовці даних.'}), 500

@camera_traps_bp.route('/api/data-download')
@login_required
@role_required('analyst')
def api_ct_data_download(lang_code):
    """Download camera-trap occurrence data as a CSV file."""
    try:
        filters = {
            'species_ids': [int(sid) for sid in request.args.get('species_ids', '').split(',') if sid],
            'genus': request.args.get('genus'),
            'family': request.args.get('family'),
            'order': request.args.get('order'),
            'class': request.args.get('class'),
            'start_date': request.args.get('start_date'),
            'end_date': request.args.get('end_date'),
            'aggregation': request.args.get('aggregation', 'none'),
            'aggregation_minutes': request.args.get('aggregation_minutes', 5),
            'institution_code': request.args.get('institution_code', 'WNBO-CT'),
            'filter_type': request.args.get('filter_type', 'species_only'),
            'institution_ids': _get_export_institution_ids(),
            'qc_exclude': [f for f in request.args.get('qc_exclude', '').split(',') if f],
        }
        
        result = get_ct_occurrence_data(filters, limit=None)
        data = result['data']

        if not data:
            # Return a response that JavaScript can handle.
            return "Дані за вибраними критеріями не знайдено.", 404

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)
        output.seek(0)
        
        return Response(
            output, 
            mimetype="text/csv", 
            headers={"Content-Disposition": "attachment;filename=ct_occurrence_data.csv"}
        )
    except Exception as e:
        current_app.logger.error(f"Error downloading CT data: {e}", exc_info=True)
        return "Помилка на сервері при генерації файлу.", 500
    
@camera_traps_bp.route('/api/identification-stats')
@login_required
@role_required('ct_verifier')
def api_get_identification_stats(lang_code):
    """Return the number of observation series still pending identification by the current user."""
    ct_session = get_ct_session()
    try:
        scope_institution_id = request.args.get('scope_institution_id', type=int)
        scope_ecoregion = request.args.get('scope_ecoregion', '')
        ai_species_id = request.args.get('ai_species_id', type=int)

        # IDs of photos already identified by this user.
        user_identified_photos = ct_session.query(Identification.photo_id)\
                                             .filter_by(user_id=current_user.id)

        user_inst_ids = [inst.id for inst in current_user.institutions]
        is_admin = current_user.has_role('admin')

        if not is_admin:
            if user_inst_ids:
                allowed_location_ids = select(location_institutions.c.location_id).where(
                    location_institutions.c.institution_id.in_(user_inst_ids)
                )
                location_filter = or_(
                    Location.visibility_level == 0,
                    Location.id.in_(allowed_location_ids)
                )
            else:
                location_filter = (Location.visibility_level == 0)

        # Additional scope filter (selected institution or ecoregion)
        scope_location_subq = None
        if scope_institution_id:
            if is_admin or scope_institution_id in user_inst_ids:
                scope_location_subq = select(location_institutions.c.location_id).where(
                    location_institutions.c.institution_id == scope_institution_id
                )
        elif scope_ecoregion:
            eco_inst_ids = [i.id for i in Institution.query.filter_by(ecoregion_uk=scope_ecoregion).all()]
            if not is_admin:
                eco_inst_ids = [i for i in eco_inst_ids if i in user_inst_ids]
            if eco_inst_ids:
                scope_location_subq = select(location_institutions.c.location_id).where(
                    location_institutions.c.institution_id.in_(eco_inst_ids)
                )

        # AI filter: count only series where the winning model (highest accuracy_rank)
        # identified the selected species. Silently ignored if AI is not yet active.
        ai_observation_subq = None
        if ai_species_id is not None:
            from .ai_runner import is_ai_available, observations_subq_for_ai_species
            if is_ai_available():
                ai_observation_subq = observations_subq_for_ai_species(ai_species_id)

        # Count observations in 'pending' status that do NOT contain
        # any photo already identified by this user.
        query = ct_session.query(Observation.id)\
                                    .filter(
                                        Observation.status == 'pending',
                                        ~Observation.photos.any(Photo.id.in_(user_identified_photos))
                                    )

        if not is_admin:
            query = query.join(Location, Observation.location_id == Location.id)\
                .filter(location_filter)

        if scope_location_subq is not None:
            query = query.filter(Observation.location_id.in_(scope_location_subq))

        if ai_observation_subq is not None:
            query = query.filter(Observation.id.in_(ai_observation_subq))

        remaining_count = query.count()

        return jsonify({'remaining_count': remaining_count})

    except Exception as e:
        current_app.logger.error(f"Error getting identification stats for user {current_user.id}: {e}")
        return jsonify({'error': 'Failed to load statistics'}), 500
    finally:
        close_ct_session()

# ── SERVICE LOG PAGE ─────────────────────────────────────────────────────────

@camera_traps_bp.route('/service-log')
@login_required
@role_required('manager')
def service_log(lang_code):
    """Redirect to the combined location-management page."""
    return redirect(url_for('camera_traps.manage_locations', lang_code=lang_code))

@camera_traps_bp.route('/api/locations-with-status')
@login_required
@role_required('manager')
def api_get_locations_with_status(lang_code):
    """
    Return a list of locations with their predicted service status.
    Accounts for inactive cameras and picks the worst-case forecast.
    Filtered by the current user's institutions (admin sees all).
    """
    ct_session = get_ct_session()
    try:
        INACTIVE_PURPOSE_IDS = {3, 4}
        TIME_WARNING_DAYS = 180
        TIME_CRITICAL_DAYS = 300
        PHOTO_WARNING_COUNT = 5000
        PHOTO_CRITICAL_COUNT = 10000

        # Map status values to severity weights for worst-case comparison.
        status_severity = {'ok': 0, 'warning': 1, 'critical': 2}

        is_admin = current_user.has_role('admin')
        user_inst_ids = [inst.id for inst in current_user.institutions]

        if is_admin:
            locations = ct_session.query(Location).all()
        elif user_inst_ids:
            locations = (
                ct_session.query(Location)
                .join(location_institutions, Location.id == location_institutions.c.location_id)
                .filter(location_institutions.c.institution_id.in_(user_inst_ids))
                .distinct().all()
            )
        else:
            return jsonify([])
        response_data = []
        
        for loc in locations:
            last_visit = ct_session.query(ServiceVisit)\
                .filter(ServiceVisit.location_id == loc.id)\
                .order_by(ServiceVisit.visit_datetime.desc())\
                .first()

            status = 'unknown'
            days_since_visit = None
            predicted_photos = None
            status_reason = _("Немає даних про обслуговування")

            if last_visit:
                # Check 1: is the camera still active?
                if last_visit.visit_purpose_id in INACTIVE_PURPOSE_IDS:
                    status = 'inactive'
                    status_reason = last_visit.visit_purpose.get_name(g.lang_code)
                    days_since_visit = (datetime.now().date() - last_visit.visit_datetime.date()).days
                
                else:
                    # Camera is active — proceed with forecasting.
                    days_since_visit = (datetime.now().date() - last_visit.visit_datetime.date()).days
                    stats = loc.stats
                    
                    # Worst-case scenario: compute time-based and photo-based status, then pick the worse one.

                    # 1. Time-based status.
                    time_status = 'ok'
                    if days_since_visit >= TIME_CRITICAL_DAYS:
                        time_status = 'critical'
                    elif days_since_visit >= TIME_WARNING_DAYS:
                        time_status = 'warning'

                    # 2. Photo-count-based status (if data is available).
                    photo_status = 'ok'  # Default.
                    if stats and stats.avg_photos_per_day > 0:
                        predicted_photos = int(days_since_visit * float(stats.avg_photos_per_day))
                        if predicted_photos >= PHOTO_CRITICAL_COUNT:
                            photo_status = 'critical'
                        elif predicted_photos >= PHOTO_WARNING_COUNT:
                            photo_status = 'warning'

                    # 3. Pick the worst of the two statuses.
                    if status_severity[photo_status] > status_severity[time_status]:
                        # Photo-count status is worse — use it.
                        status = photo_status
                        status_reason = _("Прогноз за кількістю фото")
                    else:
                        # Time status is worse or equal — use it.
                        status = time_status
                        status_reason = _("Прогноз за часом")

            response_data.append({
                'id': loc.id,
                'name': loc.name,
                'latitude': float(loc.latitude),
                'longitude': float(loc.longitude),
                'status': status,
                'last_visit_date': last_visit.visit_datetime.strftime('%d.%m.%Y') if last_visit else '---',
                'days_since_visit': days_since_visit,
                'predicted_photos': predicted_photos,
                'status_reason': status_reason
            })
            
        return jsonify(response_data)

    except Exception as e:
        current_app.logger.error(f"Error fetching locations with status: {e}", exc_info=True)
        return jsonify({'error': 'Failed to load location status data'}), 500
    finally:
        close_ct_session()

@camera_traps_bp.route('/api/location/<int:location_id>/service-history')
@login_required
@role_required('manager')
def api_get_service_history(lang_code, location_id):
    """Return the service-visit history for a specific location."""
    ct_session = get_ct_session()
    try:
        # Access check: admin sees all; manager sees only their own institutions.
        if not current_user.has_role('admin'):
            user_inst_ids = [inst.id for inst in current_user.institutions]
            if not user_inst_ids:
                return jsonify({'error': _('Немає доступу.')}), 403
            access = ct_session.execute(
                select(location_institutions.c.location_id).where(
                    (location_institutions.c.location_id == location_id) &
                    (location_institutions.c.institution_id.in_(user_inst_ids))
                ).limit(1)
            ).fetchone()
            if not access:
                return jsonify({'error': _('Немає доступу до цієї локації.')}), 403

        visits = ct_session.query(ServiceVisit)\
            .filter(ServiceVisit.location_id == location_id)\
            .order_by(ServiceVisit.visit_datetime.desc())\
            .limit(20).all()

        history_data = []
        for v in visits:
            # Resolve the username from the main database.
            user = User.query.get(v.user_id)
            username = user.username if user else f"User ID: {v.user_id}"
            
            history_data.append({
                'id': v.id,
                'visit_datetime': v.visit_datetime.strftime('%d.%m.%Y %H:%M'),
                'visit_datetime_raw': v.visit_datetime.strftime('%Y-%m-%dT%H:%M'),
                'purpose': v.visit_purpose.get_name(g.lang_code),
                'visit_purpose_id': v.visit_purpose_id,
                'user': username,
                'is_operational': v.is_camera_operational,
                'battery_info': v.battery_type.get_name(g.lang_code) if v.battery_type else _('Не замінювались'),
                'battery_type_id': v.battery_type_id,
                'sd_card_changed': v.sd_card_changed,
                'photos_on_card': v.photos_on_card,
                'comments': v.comments,
                'is_own': v.user_id == current_user.id
            })
        
        return jsonify(history_data)
        
    except Exception as e:
        current_app.logger.error(f"Error fetching service history for location {location_id}: {e}", exc_info=True)
        return jsonify({'error': 'Failed to load history'}), 500
    finally:
        close_ct_session()

@camera_traps_bp.route('/api/service-log/create', methods=['POST'])
@login_required
@role_required('manager')
def api_create_service_visit(lang_code):
    """Create a new service-visit record."""
    ct_session = get_ct_session()
    try:
        data = request.json

        # Validate and extract request data.
        location_id = data.get('location_id')
        visit_purpose_id = data.get('visit_purpose_id')
        visit_datetime_str = data.get('visit_datetime')

        if not all([location_id, visit_purpose_id, visit_datetime_str]):
            return jsonify({'success': False, 'error': _('Не всі обов\'язкові поля заповнені.')}), 400

        # Location-by-institution access check.
        if not current_user.has_role('admin'):
            user_inst_ids = [inst.id for inst in current_user.institutions]
            if not user_inst_ids:
                return jsonify({'success': False, 'error': _('Немає доступу.')}), 403
            access = ct_session.execute(
                select(location_institutions.c.location_id).where(
                    (location_institutions.c.location_id == int(location_id)) &
                    (location_institutions.c.institution_id.in_(user_inst_ids))
                ).limit(1)
            ).fetchone()
            if not access:
                return jsonify({'success': False, 'error': _('Немає доступу до цієї локації.')}), 403

        # Type conversion.
        visit_datetime = datetime.fromisoformat(visit_datetime_str)
        
        # Optional fields.
        battery_type_id = data.get('battery_type_id')
        battery_type_id = int(battery_type_id) if battery_type_id else None
        
        photos_on_card = data.get('photos_on_card')
        photos_on_card = int(photos_on_card) if photos_on_card else None

        # Convert 'true'/'false'/''/None strings to Python booleans.
        is_operational_str = data.get('is_camera_operational')
        if is_operational_str == 'true':
            is_camera_operational = True
        elif is_operational_str == 'false':
            is_camera_operational = False
        else:
            is_camera_operational = None

        # Create the record and persist it.
        new_visit = ServiceVisit(
            location_id=int(location_id),
            user_id=current_user.id,
            visit_datetime=visit_datetime,
            visit_purpose_id=int(visit_purpose_id),
            battery_type_id=battery_type_id,
            is_camera_operational=is_camera_operational,
            sd_card_changed=bool(data.get('sd_card_changed', False)),
            photos_on_card=photos_on_card,
            comments=data.get('comments', '').strip() or None
        )
        
        ct_session.add(new_visit)
        ct_session.commit()
        
        current_app.logger.info(f"User {current_user.username} created new service visit for location {location_id}")
        
        return jsonify({
            'success': True, 
            'message': _('Запис успішно додано до журналу!')
        }), 201

    except (ValueError, TypeError) as e:
        ct_session.rollback()
        current_app.logger.warning(f"Invalid data for service visit creation: {e}")
        return jsonify({'success': False, 'error': _('Передано некоректні дані.')}), 400
    except Exception as e:
        ct_session.rollback()
        current_app.logger.error(f"Error creating service visit: {e}", exc_info=True)
        return jsonify({'success': False, 'error': _('Помилка сервера при збереженні запису.')}), 500
    finally:
        close_ct_session()

@camera_traps_bp.route('/api/service-visit/<int:visit_id>/update', methods=['POST'])
@login_required
@role_required('manager')
def api_update_service_visit(lang_code, visit_id):
    """Edit an existing service-visit record."""
    ct_session = get_ct_session()
    try:
        visit = ct_session.query(ServiceVisit).get(visit_id)
        if not visit:
            return jsonify({'success': False, 'error': _('Запис не знайдено.')}), 404

        is_admin = current_user.has_role('admin')

        if not is_admin:
            # Ownership check.
            if visit.user_id != current_user.id:
                return jsonify({'success': False, 'error': _('Недостатньо прав для редагування цього запису.')}), 403
            # Check access to the location by institution
            user_inst_ids = [inst.id for inst in current_user.institutions]
            if not user_inst_ids:
                return jsonify({'success': False, 'error': _('Немає доступу.')}), 403
            access = ct_session.execute(
                select(location_institutions.c.location_id).where(
                    (location_institutions.c.location_id == visit.location_id) &
                    (location_institutions.c.institution_id.in_(user_inst_ids))
                ).limit(1)
            ).fetchone()
            if not access:
                return jsonify({'success': False, 'error': _('Немає доступу до цієї локації.')}), 403

        data = request.json
        visit_datetime_str = data.get('visit_datetime')
        visit_purpose_id = data.get('visit_purpose_id')

        if not all([visit_datetime_str, visit_purpose_id]):
            return jsonify({'success': False, 'error': _("Не всі обов'язкові поля заповнені.")}), 400

        visit.visit_datetime = datetime.fromisoformat(visit_datetime_str)
        visit.visit_purpose_id = int(visit_purpose_id)

        battery_type_id = data.get('battery_type_id')
        visit.battery_type_id = int(battery_type_id) if battery_type_id else None

        is_operational_str = data.get('is_camera_operational')
        if is_operational_str == 'true':
            visit.is_camera_operational = True
        elif is_operational_str == 'false':
            visit.is_camera_operational = False
        else:
            visit.is_camera_operational = None

        visit.sd_card_changed = bool(data.get('sd_card_changed', False))

        photos_on_card = data.get('photos_on_card')
        visit.photos_on_card = int(photos_on_card) if photos_on_card else None

        visit.comments = (data.get('comments') or '').strip() or None

        ct_session.commit()
        current_app.logger.info(f"User {current_user.username} updated CT service visit {visit_id}")
        return jsonify({'success': True, 'message': _('Запис оновлено успішно!')})

    except (ValueError, TypeError) as e:
        ct_session.rollback()
        return jsonify({'success': False, 'error': _('Некоректні дані.')}), 400
    except Exception as e:
        ct_session.rollback()
        current_app.logger.error(f"Error updating CT service visit {visit_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': _('Помилка сервера.')}), 500
    finally:
        close_ct_session()

@camera_traps_bp.route('/api/run-stats-calculation', methods=['POST'])
@login_required
@role_required('manager')  # managers and admins
def run_stats_calculation(lang_code):
    """Trigger a full statistics recalculation for all locations."""
    try:
        from .service_analytics import update_all_location_stats
        
        # Run with force=True since the user explicitly triggered this action.
        update_all_location_stats(force_run=True)
        
        return jsonify({'success': True, 'message': _('Перерахунок статистики успішно завершено! Карта буде оновлена.')})
        
    except Exception as e:
        current_app.logger.error(f"Manual stats calculation failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': _('Під час перерахунку сталася помилка.')}), 500
    
# Global cache for the species list used in filter dropdowns.
# Structure: {'data': [...], 'timestamp': datetime object}
_species_list_cache = {
    'data': None,
    'timestamp': None
}

# Cache for the observed date range (min/max series_start_time).
# The query is fast but runs on every page load, so cache for 24 h.
_heatmap_date_range_cache = {
    'data': None,   # (min_date_str, max_date_str) or None
    'timestamp': None
}


def get_cached_heatmap_date_range():
    """Return (min_date_str, max_date_str) of verified observations, cached 24 h."""
    global _heatmap_date_range_cache
    now = datetime.now()
    cached = _heatmap_date_range_cache

    if (cached['data'] is not None and
            cached['timestamp'] is not None and
            (now - cached['timestamp']).total_seconds() < 86400):
        return cached['data']

    ct_session = get_ct_session()
    try:
        min_d, max_d = fetch_date_range(ct_session)
        if min_d and max_d:
            result = (str(min_d), str(max_d))
        else:
            today = date.today()
            result = (
                (today.replace(year=today.year - 1)).strftime('%Y-%m-%d'),
                today.strftime('%Y-%m-%d'),
            )
        _heatmap_date_range_cache['data'] = result
        _heatmap_date_range_cache['timestamp'] = now
        return result
    finally:
        close_ct_session()

# Minimum number of verified registrations a species must have to appear in the
# daily-activity filter dropdown. Species below this threshold never yield a
# meaningful activity curve, so they are hidden from the list.
MIN_DETECTIONS_FOR_ACTIVITY = 30

def get_cached_species_for_filter():
    """Return the species list for filter dropdowns, cached for 7 days.

    Only species with at least MIN_DETECTIONS_FOR_ACTIVITY verified registrations
    are included, so the dropdown lists species the activity chart can plot.
    """
    global _species_list_cache
    now = datetime.now()
    CACHE_TTL_DAYS = 7

    # Check whether the cache exists and is still fresh.
    if (_species_list_cache['data'] is not None and 
        _species_list_cache['timestamp'] is not None and 
        (now - _species_list_cache['timestamp']).days < CACHE_TTL_DAYS):
        return _species_list_cache['data']

    # Cache is absent or stale — query the database.
    ct_session = get_ct_session()
    try:
        # Species ids with at least MIN_DETECTIONS_FOR_ACTIVITY verified
        # registrations. A registration is the consensus-winning species of a
        # completed/archived observation — the same definition used by
        # fetch_raw_daily_data, so the dropdown matches what the chart plots.
        count_sql = text("""
            WITH ObservationConsensus AS (
                SELECT
                    p.observation_id, i.species_id,
                    COUNT(DISTINCT i.user_id) AS vote_count,
                    MAX(i.quantity) AS max_quantity
                FROM identifications i JOIN photos p ON i.photo_id = p.id
                GROUP BY p.observation_id, i.species_id
            ),
            RankedConsensus AS (
                SELECT
                    observation_id, species_id,
                    ROW_NUMBER() OVER(
                        PARTITION BY observation_id
                        ORDER BY vote_count DESC, max_quantity DESC
                    ) AS rn
                FROM ObservationConsensus
            )
            SELECT rc.species_id
            FROM observations o
            JOIN RankedConsensus rc ON o.id = rc.observation_id AND rc.rn = 1
            WHERE o.status IN ('completed', 'archived')
            GROUP BY rc.species_id
            HAVING COUNT(*) >= :min_detections
        """)
        eligible_ids = {
            row[0] for row in ct_session.execute(
                count_sql, {'min_detections': MIN_DETECTIONS_FOR_ACTIVITY}
            ).fetchall()
        }

        # Select only species that actually occur (id > 0),
        # ordered by Ukrainian common name if available, otherwise by Latin name.
        species_query = ct_session.query(Species)\
            .filter(Species.id > 0)\
            .order_by(Species.common_name_ua, Species.scientific_name)\
            .all()

        species_list = []
        for s in species_query:
            # Skip species without enough verified registrations to plot a curve.
            if s.id not in eligible_ids:
                continue

            # Build the display name from available translations.
            name_ua = s.common_name_ua if s.common_name_ua else s.scientific_name
            name_en = s.common_name_en if s.common_name_en else s.scientific_name
            scientific = s.scientific_name
            
            # Store all name variants so the template can pick the right language.
            species_list.append({
                'id': s.id,
                'name_ua': f"{name_ua} ({scientific})",
                'name_en': f"{name_en} ({scientific})",
                'scientific': scientific
            })

        # Update the cache.
        _species_list_cache['data'] = species_list
        _species_list_cache['timestamp'] = now
        
        current_app.logger.info(f"Species list cache updated: {len(species_list)} species.")
        return species_list

    except Exception as e:
        current_app.logger.error(f"Error caching species list: {e}")
        return []
    finally:
        close_ct_session()

@camera_traps_bp.route('/analysis/species-detailed')
def species_detailed(lang_code):
    """Species detailed analysis page with available-species filtering."""
    ct_session = get_ct_session()
    try:
        # 1. Access rights.
        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        is_admin = current_user.is_authenticated and current_user.has_role('admin')

        # Build the location filter (table alias 'locations').
        inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, table_alias='locations')

        # 2. Fetch only species present at accessible locations,
        #    using a JOIN to filter by permitted locations.
        species_query = ct_session.query(Species)\
            .join(Identification, Species.id == Identification.species_id)\
            .join(Photo, Identification.photo_id == Photo.id)\
            .join(Observation, Photo.observation_id == Observation.id)\
            .join(Location, Observation.location_id == Location.id)\
            .filter(
                Species.id > 0,
                Observation.status.in_(['completed', 'archived']),
                text(inst_condition)  # institution filter
            ).params(**inst_params).distinct()

        species_objects = species_query.order_by(Species.common_name_ua, Species.scientific_name).all()

        species_list = []
        for s in species_objects:
            name_ua = s.common_name_ua if s.common_name_ua else s.scientific_name
            name_en = s.common_name_en if s.common_name_en else s.scientific_name
            species_list.append({
                'id': s.id,
                'name_ua': f"{name_ua} ({s.scientific_name})",
                'name_en': f"{name_en} ({s.scientific_name})"
            })
        
        # Default date range.
        today = date.today()
        default_start = (today - timedelta(days=365)).strftime('%Y-%m-%d')
        default_end = today.strftime('%Y-%m-%d')

        return render_template(
            'species_detailed.html',
            species_list=species_list,
            start_date=request.args.get('start_date', default_start),
            end_date=request.args.get('end_date', default_end)
        )
    finally:
        close_ct_session()

@camera_traps_bp.route('/api/stats/distribution-map')
def api_distribution_map(lang_code):
    session = get_ct_session()
    conn = session.connection()
    try:
        species_id = request.args.get('species_id', type=int)
        start_date_str = request.args.get('start_date')
        end_date_str = request.args.get('end_date')

        if not all([species_id, start_date_str, end_date_str]):
            return jsonify({'error': 'Missing parameters'}), 400
            
        # Access control.
        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        
        # Important: use alias 'l' because it is referenced in the SQL below.
        inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, table_alias='l')

        params = {
            'species_id': species_id,
            'start_date': start_date_str,
            'end_date': end_date_str
        }
        params.update(inst_params)  # add institution parameters (:user_inst_ids)

        # Consensus CTE.
        consensus_cte = """
            WITH ObservationConsensus AS (
                SELECT
                    p.observation_id, i.species_id,
                    COUNT(DISTINCT i.user_id) as vote_count,
                    MAX(i.quantity) as max_quantity
                FROM identifications i JOIN photos p ON i.photo_id = p.id
                GROUP BY p.observation_id, i.species_id
            ),
            RankedConsensus AS (
                SELECT
                    observation_id, species_id,
                    ROW_NUMBER() OVER(PARTITION BY observation_id ORDER BY vote_count DESC, max_quantity DESC) as rn
                FROM ObservationConsensus
            )
        """
        
        # Main query with the institution filter applied.
        query_sql = f"""
            SELECT
                l.id, l.name, l.latitude, l.longitude,
                COUNT(o.id) as det_count
            FROM observations o
            JOIN RankedConsensus rc ON o.id = rc.observation_id AND rc.rn = 1
            JOIN locations l ON o.location_id = l.id
            WHERE 
                rc.species_id = :species_id
                AND o.status IN ('completed', 'archived')
                AND DATE(o.series_start_time) BETWEEN :start_date AND :end_date
                AND ({inst_condition})
            GROUP BY l.id, l.name, l.latitude, l.longitude
        """
        
        final_query = consensus_cte + query_sql
        detections_result = conn.execute(text(final_query), params).mappings().fetchall()

        if not detections_result:
            return jsonify({'summary': {'total_detections': 0, 'total_locations': 0, 'avg_rai': 0}, 'locations': []})

        # Build the result set.
        locations_map = {}
        target_location_ids = []
        total_detections = 0
        
        for row in detections_result:
            loc_id = row['id']
            count = row['det_count']
            locations_map[loc_id] = {
                'id': loc_id, 'name': row['name'], 
                'lat': float(row['latitude']), 'lon': float(row['longitude']),
                'detections': count, 'effort': 0, 'rai': 0.0
            }
            target_location_ids.append(loc_id)
            total_detections += count

        # Effort calculation (trap-days).
        # Fetch active dates only for locations where the species was detected.
        dates_query = session.query(
            Location.id,
            func.date(Photo.captured_at).label('cap_date')
        ).join(Observation, Location.id == Observation.location_id)\
         .join(Photo, Observation.id == Photo.observation_id)\
         .filter(
             Location.id.in_(target_location_ids),
             Photo.captured_at.between(
                 datetime.strptime(start_date_str, '%Y-%m-%d').date(), 
                 datetime.strptime(end_date_str, '%Y-%m-%d').date()
             )
         ).distinct().order_by(Location.id, 'cap_date').all()

        loc_dates = {}
        for row in dates_query:
            lid = row.id
            if lid not in loc_dates: loc_dates[lid] = []
            loc_dates[lid].append(row.cap_date)

        MAX_GAP_DAYS = 15
        
        for lid, dates in loc_dates.items():
            if not dates: continue
            
            effort_days = 0
            if len(dates) == 1:
                effort_days = 1
            else:
                calculated_days = 1
                prev_date = dates[0]
                for i in range(1, len(dates)):
                    curr_date = dates[i]
                    diff = (curr_date - prev_date).days
                    if diff <= MAX_GAP_DAYS:
                        calculated_days += diff
                    else:
                        calculated_days += 1
                    prev_date = curr_date
                effort_days = calculated_days

            if lid in locations_map:
                locations_map[lid]['effort'] = effort_days
                if effort_days > 0:
                    locations_map[lid]['rai'] = round(locations_map[lid]['detections'] / effort_days, 4)

        final_locations = list(locations_map.values())
        
        avg_rai = 0
        if final_locations:
            total_rai = sum(loc['rai'] for loc in final_locations)
            avg_rai = round(total_rai / len(final_locations), 4)

        response = {
            'summary': {
                'total_detections': total_detections,
                'total_locations': len(final_locations),
                'avg_rai': avg_rai
            },
            'locations': final_locations
        }
        
        return jsonify(response)

    except Exception as e:
        current_app.logger.error(f"Error in distribution map API: {e}", exc_info=True)
        return jsonify({'error': 'Server error calculating map data'}), 500
    finally:
        # Close the session since we opened a raw connection.
        close_ct_session()


@camera_traps_bp.route('/api/stats/daily-activity')
def api_daily_activity(lang_code):
    ct_session = get_ct_session()
    try:
        start_date_str = request.args.get('start_date')
        end_date_str = request.args.get('end_date')
        species_ids_str = request.args.get('species_ids', '')
        scope_type = request.args.get('scope_type', 'global')
        scope_id = request.args.get('scope_id', '')

        # Read the CI flag.
        compute_ci_param = request.args.get('compute_ci', 'false').lower() == 'true'

        # CI is available only for authenticated users.
        compute_ci = compute_ci_param and current_user.is_authenticated

        try:
            bw_adjust = float(request.args.get('bw_adjust', 0.25))
            if bw_adjust < 0: bw_adjust = 0  # minimum 0
            if bw_adjust > 1.0: bw_adjust = 1.0
        except ValueError:
            bw_adjust = 0.1

        if not all([start_date_str, end_date_str, species_ids_str]):
            return jsonify({'error': 'Missing parameters'}), 400

        species_ids = [int(x) for x in species_ids_str.split(',') if x.isdigit()]
        if not species_ids: return jsonify({'error': 'No valid species'}), 400

        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

        # ── Scope filter (institution / ecoregion) ───────────────────────────────
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []

        # Access control: non-admins may only query within their own institutions.
        if not is_admin:
            if scope_type == 'institution':
                if not scope_id or int(scope_id) not in user_inst_ids:
                    return jsonify({'error': 'Access denied'}), 403
            elif scope_type == 'ecoregion':
                user_ecoregions = {
                    inst.ecoregion_uk for inst in current_user.institutions
                    if inst.ecoregion_uk
                } if current_user.is_authenticated else set()
                if scope_id not in user_ecoregions:
                    return jsonify({'error': 'Access denied'}), 403

        # Resolve location_ids list; None means "all locations".
        location_ids = None
        if scope_type == 'institution' and scope_id:
            loc_subq = ct_session.query(location_institutions.c.location_id).filter(
                location_institutions.c.institution_id == int(scope_id)
            ).distinct().all()
            location_ids = [row[0] for row in loc_subq]
        elif scope_type == 'ecoregion' and scope_id:
            eco_inst_ids = [i.id for i in Institution.query.filter_by(ecoregion_uk=scope_id).all()]
            if eco_inst_ids:
                loc_subq = ct_session.query(location_institutions.c.location_id).filter(
                    location_institutions.c.institution_id.in_(eco_inst_ids)
                ).distinct().all()
                location_ids = [row[0] for row in loc_subq]
        elif scope_type == 'global' and not is_admin and user_inst_ids:
            # Non-admin in "global" mode: restrict to their accessible locations.
            loc_subq = ct_session.query(location_institutions.c.location_id).filter(
                location_institutions.c.institution_id.in_(user_inst_ids)
            ).distinct().all()
            location_ids = [row[0] for row in loc_subq]

        # Scope was specified but no locations matched — return empty response.
        if location_ids is not None and not location_ids:
            return jsonify({
                'total_effort': 0, 'species_data': {}, 'species_names': {},
                'ci_computed': compute_ci, 'overlap_matrix': None
            })

        total_effort = calculate_total_effort(ct_session, start_date, end_date, location_ids=location_ids)
        raw_data = fetch_raw_daily_data(ct_session, start_date_str, end_date_str, species_ids, location_ids=location_ids)
        
        results = {}
        species_info = {}
        overlap_matrix = None

        for sp_id in species_ids:
            sp_raw_data = raw_data.get(sp_id, {})
            total_points = sum(len(v) for v in sp_raw_data.values())

            # Allow curves with as few as 2 data points when CI is disabled (just shows peaks).
            min_points = 5 if compute_ci else 2
            if total_points < min_points: continue

            # Pass compute_ci and bw_adjust to the curve calculator.
            stats_rai = calculate_activity_curve(
                sp_raw_data, total_effort, mode='rai',
                n_boot=1000 if compute_ci else 0,
                compute_ci=compute_ci,
                bw_adjust=bw_adjust
            )

            stats_pct = calculate_activity_curve(
                sp_raw_data, total_effort, mode='percent',
                n_boot=1000 if compute_ci else 0,
                compute_ci=compute_ci,
                bw_adjust=bw_adjust
            )

            if stats_rai and stats_pct:
                results[sp_id] = {'rai': stats_rai, 'percent': stats_pct}

                # Species display name.
                species = ct_session.query(Species).get(sp_id)
                name = species.scientific_name
                if g.lang_code == 'uk' and species.common_name_ua:
                    name = species.common_name_ua
                elif g.lang_code == 'en' and species.common_name_en:
                    name = species.common_name_en
                species_info[sp_id] = name
        # Compute the overlap matrix only when data for 2 or more species is available.
        if len(results) >= 2:
            from .daily_analytics import calculate_overlap_matrix  # lazy import
            overlap_matrix = calculate_overlap_matrix(results)
        
        for sp_id in results:
            for mode in ['rai', 'percent']:
                if 'boot_matrix' in results[sp_id][mode]:
                    del results[sp_id][mode]['boot_matrix']

        return jsonify({
            'total_effort': total_effort,
            'species_data': results,
            'species_names': species_info,
            'ci_computed': compute_ci,
            'overlap_matrix': overlap_matrix
        })

    except Exception as e:
        current_app.logger.error(f"Error in daily activity API: {e}", exc_info=True)
        return jsonify({'error': 'Server error'}), 500
    finally:
        close_ct_session()

@camera_traps_bp.route('/api/stats/daily-activity/download')
@login_required
def api_daily_activity_download(lang_code):
    """
    Generate a CSV export of the daily-activity data.
    Respects bw_adjust (0 = raw data) and compute_ci (False = fast, no confidence intervals).
    """
    ct_session = get_ct_session()
    try:
        start_date_str = request.args.get('start_date')
        end_date_str = request.args.get('end_date')
        species_ids_str = request.args.get('species_ids', '')
        scope_type = request.args.get('scope_type', 'global')
        scope_id = request.args.get('scope_id', '')

        # 1. Read smoothing parameters.
        try:
            bw_adjust = float(request.args.get('bw_adjust', 0.25))
            if bw_adjust < 0: bw_adjust = 0
            if bw_adjust > 1.0: bw_adjust = 1.0
        except ValueError:
            bw_adjust = 0.1

        # 2. Read the CI parameter.
        # Default False unless the client explicitly passed 'true'.
        compute_ci_param = request.args.get('compute_ci', 'false').lower() == 'true'
        compute_ci = compute_ci_param  # @login_required, so no further auth check needed

        if not all([start_date_str, end_date_str, species_ids_str]):
            return "Missing parameters", 400

        species_ids = [int(x) for x in species_ids_str.split(',') if x.isdigit()]
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

        # ── Scope filter (same logic as api_daily_activity) ──────────────────────
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        if not is_admin:
            if scope_type == 'institution':
                if not scope_id or int(scope_id) not in user_inst_ids:
                    return "Access denied", 403
            elif scope_type == 'ecoregion':
                user_ecoregions = {
                    inst.ecoregion_uk for inst in current_user.institutions
                    if inst.ecoregion_uk
                }
                if scope_id not in user_ecoregions:
                    return "Access denied", 403

        location_ids = None
        if scope_type == 'institution' and scope_id:
            location_ids = [r[0] for r in ct_session.query(location_institutions.c.location_id).filter(
                location_institutions.c.institution_id == int(scope_id)
            ).distinct().all()]
        elif scope_type == 'ecoregion' and scope_id:
            eco_inst_ids = [i.id for i in Institution.query.filter_by(ecoregion_uk=scope_id).all()]
            if eco_inst_ids:
                location_ids = [r[0] for r in ct_session.query(location_institutions.c.location_id).filter(
                    location_institutions.c.institution_id.in_(eco_inst_ids)
                ).distinct().all()]
        elif scope_type == 'global' and not is_admin and user_inst_ids:
            location_ids = [r[0] for r in ct_session.query(location_institutions.c.location_id).filter(
                location_institutions.c.institution_id.in_(user_inst_ids)
            ).distinct().all()]

        if location_ids is not None and not location_ids:
            # Scope specified but empty — return an empty CSV.
            return Response("", mimetype="text/csv")

        # Fetch and process data.
        total_effort = calculate_total_effort(ct_session, start_date, end_date, location_ids=location_ids)
        raw_data = fetch_raw_daily_data(ct_session, start_date_str, end_date_str, species_ids, location_ids=location_ids)
        
        results = {}
        species_info = {}
        
        for sp_id in species_ids:
            sp_raw_data = raw_data.get(sp_id, {})
            # Allow a low point count for exports without CI.
            min_points = 5 if compute_ci else 1
            if sum(len(v) for v in sp_raw_data.values()) < min_points: 
                continue
            
            # Run the curve calculation.
            # When compute_ci=False, n_boot=0 and the bootstrap loop is skipped — fast path.
            stats_rai = calculate_activity_curve(
                sp_raw_data, total_effort, mode='rai', 
                n_boot=1000 if compute_ci else 0, 
                compute_ci=compute_ci, 
                bw_adjust=bw_adjust
            )
            
            if stats_rai:
                results[sp_id] = {'rai': stats_rai}
                
                species = ct_session.query(Species).get(sp_id)
                name = species.scientific_name
                if g.lang_code == 'uk' and species.common_name_ua:
                    name = species.common_name_ua
                elif g.lang_code == 'en' and species.common_name_en:
                    name = species.common_name_en
                species_info[sp_id] = name
        
        csv_content = generate_csv_export(results, species_info)
        
        filename_prefix = "daily_activity_raw" if bw_adjust == 0 else "daily_activity_kde"
        filename = f"{filename_prefix}_{start_date_str}_{end_date_str}.csv"

        return Response(
            csv_content,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment;filename={filename}"}
        )

    except Exception as e:
        current_app.logger.error(f"Error exporting CSV: {e}", exc_info=True)
        return "Export error", 500
    finally:
        close_ct_session()

@camera_traps_bp.route('/analysis/daily-activity')
def daily_activity_page(lang_code):
    """Daily-activity analysis page."""
    try:
        # Default dates: 1 January of the current year through today.
        today = date.today()
        default_start = (today - timedelta(days=365)).strftime('%Y-%m-%d')
        default_end = today.strftime('%Y-%m-%d')

        # Load the species list using the caching helper.
        species_list = get_cached_species_for_filter()

        # Build institutions and ecoregions for the scope filter (same logic as species_dashboard).
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        if is_admin:
            institutions = Institution.query.order_by(Institution.name_uk).all()
        elif current_user.is_authenticated:
            institutions = sorted(current_user.institutions,
                                  key=lambda i: i.name_uk or '')
        else:
            institutions = []

        lang = g.lang_code
        ecoregions = {}
        for inst in institutions:
            if inst.ecoregion_uk:
                display = inst.ecoregion_uk if lang != 'en' else (inst.ecoregion_en or inst.ecoregion_uk)
                ecoregions[inst.ecoregion_uk] = display

        return render_template(
            'daily_activity.html',
            species_list=species_list,
            default_start=default_start,
            default_end=default_end,
            institutions=institutions,
            ecoregions=ecoregions,
            is_admin=is_admin,
        )
    except Exception as e:
        current_app.logger.error(f"Error loading daily activity page: {e}", exc_info=True)
        flash(_("Помилка завантаження сторінки."), 'danger')
        return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))


@camera_traps_bp.route('/api/stats/activity-heatmap')
def api_activity_heatmap(lang_code):
    """JSON API: 24-hour × 12-month activity heatmap for one species."""
    ct_session = get_ct_session()
    try:
        species_id_str = request.args.get('species_id', '')
        scope_type = request.args.get('scope_type', 'global')
        scope_id = request.args.get('scope_id', '')
        start_date_str = request.args.get('start_date', '')
        end_date_str = request.args.get('end_date', '')

        start_date = None
        end_date = None
        if start_date_str:
            try:
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            except ValueError:
                pass
        if end_date_str:
            try:
                end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            except ValueError:
                pass

        if not species_id_str or not species_id_str.isdigit():
            return jsonify({'error': 'Missing or invalid species_id'}), 400

        species_id = int(species_id_str)

        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []

        # Access control mirrors api_daily_activity.
        if not is_admin:
            if scope_type == 'institution':
                if not scope_id or int(scope_id) not in user_inst_ids:
                    return jsonify({'error': 'Access denied'}), 403
            elif scope_type == 'ecoregion':
                user_ecoregions = {
                    inst.ecoregion_uk for inst in current_user.institutions
                    if inst.ecoregion_uk
                } if current_user.is_authenticated else set()
                if scope_id not in user_ecoregions:
                    return jsonify({'error': 'Access denied'}), 403

        location_ids = None
        if scope_type == 'institution' and scope_id:
            loc_subq = ct_session.query(location_institutions.c.location_id).filter(
                location_institutions.c.institution_id == int(scope_id)
            ).distinct().all()
            location_ids = [row[0] for row in loc_subq]
        elif scope_type == 'ecoregion' and scope_id:
            eco_inst_ids = [i.id for i in Institution.query.filter_by(ecoregion_uk=scope_id).all()]
            if eco_inst_ids:
                loc_subq = ct_session.query(location_institutions.c.location_id).filter(
                    location_institutions.c.institution_id.in_(eco_inst_ids)
                ).distinct().all()
                location_ids = [row[0] for row in loc_subq]
        elif scope_type == 'global' and not is_admin and user_inst_ids:
            loc_subq = ct_session.query(location_institutions.c.location_id).filter(
                location_institutions.c.institution_id.in_(user_inst_ids)
            ).distinct().all()
            location_ids = [row[0] for row in loc_subq]

        if location_ids is not None and not location_ids:
            empty = [[0] * 12 for _ in range(24)]
            return jsonify({'matrix': empty, 'max_count': 0, 'total': 0, 'species_name': ''})

        species = ct_session.query(Species).get(species_id)
        if not species:
            return jsonify({'error': 'Species not found'}), 404

        if g.lang_code == 'uk' and species.common_name_ua:
            species_name = species.common_name_ua
        elif g.lang_code == 'en' and species.common_name_en:
            species_name = species.common_name_en
        else:
            species_name = species.scientific_name

        result = fetch_heatmap_data(
            ct_session, species_id,
            location_ids=location_ids,
            start_date=start_date,
            end_date=end_date,
        )
        result['species_name'] = species_name

        return jsonify(result)

    except Exception as e:
        current_app.logger.error(f"Error in activity heatmap API: {e}", exc_info=True)
        return jsonify({'error': 'Server error'}), 500
    finally:
        close_ct_session()


@camera_traps_bp.route('/analysis/activity-heatmap')
def activity_heatmap_page(lang_code):
    """Activity heatmap page: 24-hour × 12-month grid per species."""
    try:
        species_list = get_cached_species_for_filter()
        default_start, default_end = get_cached_heatmap_date_range()

        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        if is_admin:
            institutions = Institution.query.order_by(Institution.name_uk).all()
        elif current_user.is_authenticated:
            institutions = sorted(current_user.institutions, key=lambda i: i.name_uk or '')
        else:
            institutions = []

        lang = g.lang_code
        ecoregions = {}
        for inst in institutions:
            if inst.ecoregion_uk:
                display = inst.ecoregion_uk if lang != 'en' else (inst.ecoregion_en or inst.ecoregion_uk)
                ecoregions[inst.ecoregion_uk] = display

        return render_template(
            'activity_heatmap.html',
            species_list=species_list,
            institutions=institutions,
            ecoregions=ecoregions,
            is_admin=is_admin,
            default_start=default_start,
            default_end=default_end,
        )
    except Exception as e:
        current_app.logger.error(f"Error loading activity heatmap page: {e}", exc_info=True)
        flash(_("Помилка завантаження сторінки."), 'danger')
        return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))