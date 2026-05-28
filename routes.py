# myproject/app/camera_traps/routes.py

from flask import render_template, g, flash, redirect, url_for, jsonify, request, current_app, send_from_directory, abort, Response
from datetime import datetime, date, timedelta
from flask_login import login_required, current_user
from flask_babel import gettext as _
from sqlalchemy import func, distinct, extract, select, text, or_
import io
import csv
import os
import json
from functools import lru_cache

from . import camera_traps_bp
from .forms import UploadForm, IdentificationForm
from .background_tasks import cleanup_old_photos
from .analytics_calculator import update_analytics_tables
from .utils import process_photo_batch, check_consensus_for_observation, calculate_total_effort, get_institution_filter
from .database import get_ct_session, close_ct_session
from .models import Location, Species, Photo, Observation, Identification, BehaviorType, Biotope, SpeciesYearlyTrend, LocationMonthlyActivity
from .models import ServiceVisit, BatteryType, VisitPurpose, LocationStats, location_institutions, identification_behaviors
from .models import Deployment
from app.models import User, Institution
from .decorators import role_required
from .data_export import get_ct_occurrence_data
from .daily_analytics import fetch_raw_daily_data, calculate_activity_curve, generate_csv_export, calculate_overlap_matrix

#
# --- СТАТИЧНІ ФАЙЛИ МОДУЛЯ ---
#
@camera_traps_bp.route('/ct-static/<path:filename>')
def serve_ct_static(lang_code, filename):
    static_dir = os.path.join(os.path.dirname(__file__), 'static')
    return send_from_directory(static_dir, filename)


#
# --- СТАРТОВА СТОРІНКА МОДУЛЯ (картковий хаб) ---
#
@camera_traps_bp.route('/')
def overview(lang_code):
    """Стартова сторінка модуля з картками-входами до всіх розділів."""
    # Картки рендеряться залежно від ролі користувача
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
# --- АДМІН-ПАНЕЛЬ (технічні дії) ---
#
@camera_traps_bp.route('/admin')
@login_required
@role_required('admin')
def admin_panel(lang_code):
    """Сторінка з адмін-діями: перерахунок аналітики, очищення фото тощо."""
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

    return render_template(
        'admin.html',
        ai_available=ai_available,
        ai_max_per_run=ai_max_per_run,
        ai_recent=ai_recent,
        ai_model=ai_model,
        ai_stats=ai_stats,
    )


@camera_traps_bp.route('/admin/ai/run', methods=['POST'])
@login_required
@role_required('admin')
def admin_ai_run(lang_code):
    """Створює запит у ai_run_queue. Worker (cron) підхопить за 2-3 хв."""
    from .ai_runner import is_ai_available, request_run

    if not is_ai_available():
        flash(_('AI-класифікатор не доступний.'), 'danger')
        return redirect(url_for('camera_traps.admin_panel', lang_code=g.lang_code))

    # Валідація N
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
    upper_bound = max_per_run * 5   # одноразово до 5× нічного ліміту
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
# --- АНАЛІТИЧНИЙ ДАШБОРД ---
#
@camera_traps_bp.route('/dashboard')
def dashboard(lang_code):
    """Відображає дашборд з основною статистикою, ФІЛЬТРОВАНОЮ ЗА ДАТОЮ, ЛОКАЦІЯМИ ТА БІОТОПАМИ."""
    ct_session = get_ct_session()
    try:

        # Дати
        start_date_str = request.args.get('start_date', '2020-08-01')
        end_date_str = request.args.get('end_date', date.today().strftime('%Y-%m-%d'))
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            start_date_str, end_date_str = '2020-08-01', date.today().strftime('%Y-%m-%d')
            start_date, end_date = datetime.strptime(start_date_str, '%Y-%m-%d').date(), date.today()
        
        # Локації та Біотопи
        location_ids_str = request.args.get('locations', '')
        biotope_ids_str_list = request.args.getlist('biotopes')
        # Перетворюємо список рядків у список чисел
        biotope_ids = [int(id) for id in biotope_ids_str_list if id.isdigit()]
        location_ids = [int(id) for id in location_ids_str.split(',') if id.isdigit()]
        
        # Отримуємо список біотопів для передачі в шаблон
        biotopes_list = ct_session.query(Biotope).order_by(Biotope.name_ua).all()

        raw_inst_ids = request.args.getlist('institution_id')
        if not raw_inst_ids:
            raw_inst_ids = request.args.get('institution_id', '').split(',')
        selected_inst_ids =[int(i) for i in raw_inst_ids if str(i).isdigit()]
        institution_id_str = ','.join(map(str, selected_inst_ids))

        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else[]
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        
        # Передаємо table_alias='locations' (без ніяких .replace)
        inst_condition, inst_params = get_institution_filter(
            user_inst_ids, is_admin, selected_inst_id=selected_inst_ids, table_alias='locations'
        )
        inst_condition_orm = text(inst_condition)

        if is_admin:
            institutions_list = Institution.query.order_by(Institution.name_uk).all()
        elif current_user.is_authenticated:
            institutions_list = current_user.institutions
        else:
            institutions_list =[]
        
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
        # --- КІНЕЦЬ ЗАПИТІВ ДО БД ---

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
                             selected_institutions=selected_inst_ids)
        
    except Exception as e:
        current_app.logger.error(f"Error in dashboard: {str(e)}")
        stats = {'total_photos': 0, 'total_locations': 0, 'total_identifications': 0, 'identified_species_count': 0, 'top_contributors': []}
        flash(_('Помилка завантаження статистики.'), 'warning')
        return render_template('dashboard.html', stats=stats, start_date='2020-08-01', end_date=date.today().strftime('%Y-%m-%d'), biotopes=[], selected_locations='', selected_biotopes=[])
    finally:
        close_ct_session()

#
# --- СТОРІНКА ДЕТАЛЬНОГО АНАЛІЗУ ПО ВИДАХ ---
#
# ЗАМІНІТЬ ВАШУ ІСНУЮЧУ ФУНКЦІЮ species_dashboard НА ЦЮ

@camera_traps_bp.route('/analysis/species-dashboard')
def species_dashboard(lang_code):
    """Сторінка детального аналізу трендів по видах з фільтрацією по установах."""
    ct_session = get_ct_session()
    try:
        MIN_OBSERVATIONS = 30
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []

        # Список видів з мінімальною кількістю спостережень
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

        # Установи та екорегіони для фільтру
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
# --- СТОРІНКА ПОРІВНЯННЯ ДВОХ РЕГІОНІВ ---
#
@camera_traps_bp.route('/analysis/comparison')
def comparison_dashboard(lang_code):
    """Сторінка порівняння статистики двох регіонів/установ."""
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
# --- СТОРІНКА АНАЛІЗУ ПОВЕДІНКИ ---
#
@camera_traps_bp.route('/analysis/behavior')
def behavior_analysis(lang_code):
    """Сторінка аналізу поведінкових тегів. Доступна всім."""
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

        # Менеджер = автентифікований користувач з хоча б однією установою
        is_manager = current_user.is_authenticated and bool(current_user.institutions)

        # Лише види з хоча б одним behavior-тегом
        species_q = (
            ct_session.query(Species)
            .join(Identification, Identification.species_id == Species.id)
            .join(identification_behaviors,
                  identification_behaviors.c.identification_id == Identification.id)
            .filter(Species.is_active == True)
        )
        # Для звичайних користувачів (не адмін, не менеджер) ховаємо
        # "технічні" види (мотоцикл, авто, людина тощо) — їх id < 0
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
    """API: усі дані для трьох графіків поведінки одним запитом."""
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

        # Фільтр установ
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

        # Базова вибірка ідентифікацій
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

        # Графік 1: розподіл поведінок
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

        # Графік 2: сезонна структура (по місяцях)
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

        # Графік 3: гістограма кількості особин
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

        # Кількість ідентифікацій без жодного поведінкового тегу
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
    """API: список видів з хоча б одним behavior-тегом."""
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
# --- СТОРІНКА ІДЕНТИФІКАЦІЇ ---
#

# Кеш для рейтингу видів
_species_ranking_cache = {
    'data': None,
    'timestamp': None,
    'ttl_hours': 24
}

def get_species_ranking():
    """Отримує рейтинг видів за частотою ідентифікацій з кешуванням."""
    global _species_ranking_cache
    
    now = datetime.now()
    
    # Перевіряємо, чи актуальний кеш
    if (_species_ranking_cache['data'] is not None and 
        _species_ranking_cache['timestamp'] is not None and
        (now - _species_ranking_cache['timestamp']).total_seconds() < _species_ranking_cache['ttl_hours'] * 3600):
        return _species_ranking_cache['data']
    
    # Оновлюємо кеш
    ct_session = get_ct_session()
    try:
        # Підраховуємо кількість ідентифікацій для кожного виду
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
        
        # Створюємо словник для швидкого пошуку
        ranking = {species_id: count for species_id, count in species_counts}
        
        # Зберігаємо в кеш
        _species_ranking_cache['data'] = ranking
        _species_ranking_cache['timestamp'] = now
        
        current_app.logger.info(f"Species ranking cache updated with {len(ranking)} species")
        return ranking
        
    except Exception as e:
        current_app.logger.error(f"Error updating species ranking cache: {e}")
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

        # Установи та екорегіони для фільтру scope
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
        
        # --- ПОЧАТОК НОВОЇ, ДИНАМІЧНОЇ ЛОГІКИ ---

        # 1. Отримуємо ОДНИМ ЗАПИТОМ всі активні опції з бази даних
        all_options = ct_session.query(Species).filter(Species.is_active==True).all()
        
        # 2. Готуємо порожні списки, які будуть заповнені динамічно
        grouped_species = {'mammals': [], 'birds': [], 'other': []}
        empty_choices = []
        other_special_choices = []
        
        # 3. Розподіляємо кожну опцію з бази даних у відповідний список
        for s in all_options:
            # Формуємо назву для відображення
            display_name = s.scientific_name
            if g.lang_code == 'uk' and s.common_name_ua:
                # Для спеціальних опцій, де наукова назва може бути 'empty', 'vehicle' і т.д.
                # ми не хочемо показувати її в дужках, якщо є українська назва.
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

            # Логіка розподілу по списках
            if s.id == -1: # Спеціальна обробка для "Пусто"
                empty_choices.append(choice)
            elif s.id < 0: # Всі інші спеціальні опції
                other_special_choices.append(choice)
            elif s.category in grouped_species: # Реальні види тварин
                grouped_species[s.category].append(choice)
            else: # Якщо у виду невідома категорія, додаємо його в 'other'
                if 'other' in grouped_species:
                    grouped_species['other'].append(choice)

        # 4. Сортуємо реальні види за популярністю (як і раніше)
        species_ranking = get_species_ranking()
        for category in grouped_species:
            grouped_species[category].sort(
                key=lambda x: species_ranking.get(x[0], 0), 
                reverse=True
            )
            
        # 5. Сортуємо інші спеціальні опції за ID для стабільного порядку
        other_special_choices.sort(key=lambda x: x[0], reverse=True)

        # --- КІНЕЦЬ НОВОЇ ЛОГІКИ ---
        
        # Заповнюємо вибір для поведінки (залишається без змін)
        behavior_types = ct_session.query(BehaviorType).order_by(BehaviorType.name_ua).all()
        form.behaviors.choices = [(bt.id, bt.get_name(g.lang_code)) for bt in behavior_types]

        # AI-фільтр: список видів з AI-прогнозами (тільки якщо AI доступний).
        # Враховуємо доступ юзера до локацій і вже зроблені ним ідентифікації —
        # лічильники в дужках («(42)») зменшуватимуться по мірі роботи.
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

        # Передаємо в шаблон вже заповнені динамічно списки
        return render_template('identification.html',
                             form=form,
                             grouped_species=grouped_species,
                             empty_choices=empty_choices,
                             other_special_choices=other_special_choices,
                             can_review=can_review,
                             institutions=institutions,
                             ecoregions=ecoregions,
                             ai_available=ai_available,
                             ai_species_list=ai_species_list)
    finally:
        close_ct_session()

@camera_traps_bp.route('/api/identify/ai-species', methods=['GET'])
@login_required
@role_required('ct_verifier')
def identify_ai_species_list(lang_code):
    """JSON-список AI-видів з лічильниками, опційно звужений до scope.

    Викликається фронтом при зміні `#scope-select` на /identify, щоб
    каскадно оновити `#ai-species-select` (відображає лише ті види, які
    мають pending AI-прогноз у локаціях вибраної установи/екорегіону,
    з актуальними числами).

    Query params (взаємно виключні):
      - scope_institution_id: int — підрізає до однієї установи
      - scope_ecoregion: str — підрізає до екорегіону (uk-ключ)

    Якщо жоден scope не передано — повертає повний список (з урахуванням
    прав доступу юзера), еквівалентний тому, що рендериться на сторінці.
    """
    ct_session = get_ct_session()
    try:
        scope_institution_id = request.args.get('scope_institution_id', type=int)
        scope_ecoregion = request.args.get('scope_ecoregion', '') or None

        is_admin = current_user.has_role('admin')
        user_inst_ids = [inst.id for inst in current_user.institutions]

        # Захист: non-admin не має тягнути списки чужих установ.
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

        # --- ПОЧАТОК НОВОЇ, БЕЗПЕЧНОЇ ЛОГІКИ ---
        if is_admin:
            # Адміністратор бачить абсолютно всі локації для завантаження
            locations = ct_session.query(Location).order_by(Location.name).all()
        elif user_inst_ids:
            # Модератор бачить ТІЛЬКИ ті локації, які належать його установам.
            # Публічні локації (visibility_level=0) сюди не потраплять, якщо вони не прив'язані до установи.
            locations = ct_session.query(Location)\
                .join(location_institutions, Location.id == location_institutions.c.location_id)\
                .filter(location_institutions.c.institution_id.in_(user_inst_ids))\
                .order_by(Location.name).distinct().all()
        else:
            # Якщо користувач - модератор, але без жодної установи, він не бачить жодної локації
            locations = []
        # --- КІНЕЦЬ НОВОЇ ЛОГІКИ ---

        form.location.choices = [(-1, _('-- Будь ласка, виберіть --'))] + [(loc.id, loc.name) for loc in locations] + [(0, _('*** СТВОРИТИ НОВЕ МІСЦЕ ***'))]
        
        # Отримуємо установи поточного користувача (цей код вже правильний)
        if is_admin:
            institutions_list = Institution.query.order_by(Institution.name_uk).all()
        else:
            institutions_list = current_user.institutions

        # Цей блок залишається без змін, він потрібен для JavaScript-фільтрації
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
        
        locations_json_string = json.dumps(locations_data)
        geoserver_url = current_app.config['GEOSERVER_URL']
        
        return render_template('upload.html', 
                               form=form,
                               locations_json_string=locations_json_string,
                               geoserver_url=geoserver_url,
                               institutions=institutions_list)
    finally:
        close_ct_session()

@camera_traps_bp.route('/api/create-batch', methods=['POST'])
@login_required
@role_required('manager')
def create_batch(lang_code):
    """Створює новий batch для завантаження файлів."""
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
    """Обробляє один файл у складі batch."""
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
    """Завершує batch і групує фото в серії."""
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
    """Повертає статус batch."""
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
# /upload-fast — паралельний шлях для великих наборів фото (10k–100k+).
# Існує разом зі старим /upload. Спільно використовує create-batch /
# process-single / batch-status. Відрізняється:
#   • окрема сторінка з паралельним JS-аплоадером і polling-фіналізацією
#   • finalize-batch-async — повертає 202, групування у фоновому потоці
#   • uploaded-files endpoint для resumable-відновлення
# ═════════════════════════════════════════════════════════════════════════════

@camera_traps_bp.route('/upload-fast', methods=['GET'])
@login_required
@role_required('manager')
def upload_fast(lang_code):
    """Нова сторінка завантаження (Beta). Логіка вибору локацій — та сама,
    що в legacy `upload`, лише інший шаблон + інший фінал."""
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
            locations_json_string=json.dumps(locations_data),
            geoserver_url=current_app.config['GEOSERVER_URL'],
            institutions=institutions_list,
        )
    finally:
        close_ct_session()


@camera_traps_bp.route('/api/finalize-batch-async', methods=['POST'])
@login_required
@role_required('manager')
def finalize_batch_async(lang_code):
    """Переводить batch у 'ready_to_group' і стартує фонове групування.
    Повертає 202 Accepted з batch_id; клієнт polling-ом тягне /api/batch-status."""
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
                # 'failed' дозволяємо як retry; 'completed' / 'grouping' / 'ready_to_group' — ні
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
    """Список (original_filename, captured_at) уже залитих файлів цього batchʼа.
    Використовується upload_fast.html для resumable: при відновленні сесії
    JS пропускає файли, які вже на сервері."""
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


@camera_traps_bp.route('/photo/<int:photo_id>')
@camera_traps_bp.route('/observation/<int:observation_id>/photo/<int:photo_index>')
@login_required
def view_photo(lang_code, photo_id=None, observation_id=None, photo_index=None):
    ct_session = get_ct_session()
    try:
        observation = None
        
        # Крок 1: Визначаємо спостереження (Observation)
        if observation_id:
            observation = ct_session.query(Observation).get(observation_id)
        elif photo_id:
            temp_photo = ct_session.query(Photo).get(photo_id)
            if temp_photo:
                observation = temp_photo.observation
        
        if not observation:
            flash(_('Спостереження або фотографію не знайдено.'), 'danger')
            return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))

        # Крок 2: Отримуємо та гарантовано сортуємо список фото
        photos_sorted = sorted(list(observation.photos), key=lambda p: p.captured_at)
        
        if not photos_sorted:
             flash(_('У цьому спостереженні немає фотографій.'), 'warning')
             return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))

        # Крок 3: Визначаємо індекс поточного фото
        current_photo_index = 0
        if photo_index is not None:
            if 0 <= photo_index < len(photos_sorted):
                current_photo_index = photo_index
        elif photo_id:
            # Шукаємо індекс фото з потрібним ID у відсортованому списку
            for i, p in enumerate(photos_sorted):
                if p.id == photo_id:
                    current_photo_index = i
                    break
        
        # Отримуємо фінальний, правильний об'єкт фото з відсортованого списку
        photo = photos_sorted[current_photo_index]

        # Крок 4: БЕЗПЕЧНО завантажуємо імена користувачів
        if photo.identifications:
            user_ids = [ident.user_id for ident in photo.identifications]
            
            # Робимо запит до основної бази даних
            from app.models import User # Переконайтесь, що цей імпорт є на початку файлу
            users = User.query.filter(User.id.in_(user_ids)).all()
            user_map = {user.id: user for user in users} # Зберігаємо цілі об'єкти User
            
            # Додаємо об'єкт користувача як новий атрибут до кожної ідентифікації
            for ident in photo.identifications:
                ident.user = user_map.get(ident.user_id)

        # Крок 5: Готуємо дані для навігації по серії
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
        current_app.logger.error(f"Error in view_photo: {e}", exc_info=True) # Додано exc_info для кращої діагностики
        flash(_('Помилка завантаження фотографії.'), 'danger')
        return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))
    finally:
        close_ct_session()

# --- API для дашборду ---
@camera_traps_bp.route('/api/stats/top-species')
def stats_top_species(lang_code):
    """
    Повертає дані для діаграми на основі консенсусу по завершених спостереженнях.
    (Фінальна версія: логіка фільтрів і дати за замовчуванням збережені).
    """
    session = None
    try:
        session = get_ct_session()
        conn = session.connection()

        # --- Крок 1: ПОВЕРНУЛИ ВАШУ ЛОГІКУ ОБРОБКИ ДАТИ ---
        # ВИПРАВЛЕНО: Повертаємо вашу дату за замовчуванням '2020-08-01'
        start_date_str = request.args.get('start_date', '2020-08-01')
        end_date_str = request.args.get('end_date', date.today().strftime('%Y-%m-%d'))
        institution_id_str = request.args.get('institution_id', '')
        
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
        # Передаємо table_alias='l' для сирого SQL
        inst_condition, inst_params = get_institution_filter(
            user_inst_ids, is_admin, selected_inst_id=institution_id_str, table_alias='l'
        )
        params.update(inst_params)

        # --- Крок 2: Новий, коректний SQL-запит з логікою консенсусу ---
        
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
        
        # --- Крок 3: Обробка результатів ---
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
        
        # Отримуємо ID установ (це може бути список або рядок через кому)
        raw_inst_ids = request.args.getlist('institution_id')
        if not raw_inst_ids:
            raw_inst_ids = request.args.get('institution_id', '').split(',')
        selected_inst_ids = [int(i) for i in raw_inst_ids if str(i).isdigit()]

        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        
        # Викликаємо фільтр, ОДРАЗУ вказуючи правильний аліас 'locations'
        inst_condition, inst_params = get_institution_filter(
            user_inst_ids, is_admin, selected_inst_id=selected_inst_ids, table_alias='locations'
        )
        
        biotope_ids_str = request.args.get('biotopes', '')
        biotope_ids = [int(id) for id in biotope_ids_str.split(',') if id.isdigit()]

        # Використовуємо inst_condition ЯК ВІН Є, без замін
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

# --- API для сторінки детального аналізу ---
@camera_traps_bp.route('/api/stats/species-dynamics')
def api_species_dynamics(lang_code):
    """API для отримання даних для графіків з попередньо розрахованих таблиць."""
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

        # Перевірка прав доступу до запитаного скоупу
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
                # Для не-адмінів global — фільтруємо сезонні дані по їх локаціях,
                # але тренд беремо з global скоупу (найближче що є)
                pass

        # Фільтр локацій для сезонних даних
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

        # 1. Сезонна активність
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

        # 2. Річна динаміка (попередньо розрахована для скоупу)
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
        
# --- API для сторінки порівняння регіонів ---
@camera_traps_bp.route('/api/stats/comparison')
def api_comparison(lang_code):
    """Повертає статистику, RAI видів та екологічні індекси для двох вибраних регіонів."""
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

        # Фільтр біотопів по локаціях
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

        # Сирі ідентифікації для Venn-аналізу (ловить рідкісні види, яких немає в pre-computed таблиці)
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

        # species_map для ВСІХ видів (pre-computed + сирі)
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

        # Venn: за сирими ідентифікаціями (повніше, ніж pre-computed таблиця)
        left_only_spp = left_all_spp - right_all_spp
        right_only_spp = right_all_spp - left_all_spp
        shared_spp = left_all_spp & right_all_spp
        union_spp = left_all_spp | right_all_spp

        # Jaccard і Sørensen — за присутністю/відсутністю (сирі дані)
        jaccard = round(len(shared_spp) / len(union_spp), 3) if union_spp else 0
        sorensen_denom = len(left_all_spp) + len(right_all_spp)
        sorensen = round(2 * len(shared_spp) / sorensen_denom, 3) if sorensen_denom else 0

        # Bray-Curtis і Morisita-Horn — за чисельністю (pre-computed RAI)
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

        # Барчарт: топ-30 за сумарним RAI
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

# --- API для ідентифікації та завантаження ---
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
            current_photo_index = int(data.get('current_photo_index', 0))  # ← ДОДАНО
            behavior_ids = data.get('behaviors', [])
        except (ValueError, TypeError, KeyError):
            return jsonify({'success': False, 'error': _('Неправильний формат даних.')}), 400

        observation = ct_session.query(Observation).get(observation_id)
        if not observation:
            return jsonify({'success': False, 'error': _('Серію не знайдено.')}), 404
        
        is_moderator = current_user.has_role('manager')

        moderator_override = is_moderator and observation.status == 'completed'

        selected_behaviors = ct_session.query(BehaviorType).filter(BehaviorType.id.in_(behavior_ids)).all() if behavior_ids else []
        
        photos_sorted = sorted(list(observation.photos), key=lambda p: p.captured_at)
        # Перевірка валідності індексу
        if current_photo_index >= len(photos_sorted):
            current_photo_index = 0  # Fallback to first photo
                
        for i, photo in enumerate(photos_sorted):
            # Встановлюємо is_favorite тільки для поточного фото
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
                    quantity=quantity
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
    """API для отримання деталей конкретної локації, включаючи її біотопи."""
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
    """Знаходить наступну серію для ідентифікації."""
    ct_session = get_ct_session()
    try:
        review_mode = request.args.get('review', 'false').lower() == 'true'
        review_user_id = request.args.get('review_user_id', type=int)
        review_species_id = request.args.get('review_species_id', type=int)
        sort_by = request.args.get('sort_by', 'random') # За замовчуванням 'random'
        scope_institution_id = request.args.get('scope_institution_id', type=int)
        scope_ecoregion = request.args.get('scope_ecoregion', '')
        ai_species_id = request.args.get('ai_species_id', type=int)  # фільтр "AI: вид"

        # Перевіряємо права доступу для review режиму
        if review_mode and not current_user.has_role('manager'):
            return jsonify({'error': _('Недостатньо прав для режиму перегляду')}), 403

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

        # Додатковий фільтр scope (вибрана установа або екорегіон)
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

        # AI-фільтр: показуємо лише серії, де AI визначив вибраний вид
        # (від активної моделі). Працює тихо: якщо AI ще не використовувався
        # на цій інсталяції — параметр просто ігнорується.
        ai_observation_subq = None
        if ai_species_id is not None:
            from .ai_runner import is_ai_available
            from .models import AIModel, AIPrediction
            if is_ai_available():
                active_model = ct_session.query(AIModel).filter_by(is_active=True).first()
                if active_model is not None:
                    ai_observation_subq = (
                        select(AIPrediction.observation_id)
                        .where(
                            AIPrediction.model_id == active_model.id,
                            AIPrediction.prediction_species_id == ai_species_id,
                        )
                        .distinct()
                    )

        if review_mode:
            # В review режимі показуємо як pending так і completed з ідентифікаціями
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

            # Додаємо фільтр по користувачу
            if review_user_id:
                query = query.filter(
                    Observation.photos.any(
                        Photo.identifications.any(Identification.user_id == review_user_id)
                    )
                )

            # Додаємо фільтр по виду
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
            # Звичайний режим - тільки pending, завжди випадково
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

            observation = query.order_by(func.random()).first()
        
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

        # Debug логування
        current_app.logger.info(f"Photos order for observation {observation.id}:")
        for i, photo_info in enumerate(photos_data):
            current_app.logger.info(f"  Index {i}: {photo_info['debug_filename']} - {photo_info['captured_at']}")   
        
        existing_identifications = []
        if review_mode:
            user_identifications = {}
            
            # Тепер перебираємо фото в хронологічному порядку
            for photo in photos_sorted:
                for ident in photo.identifications:
                    user_id = ident.user_id
                    if user_id not in user_identifications:
                        # Тепер ми гарантовано беремо ідентифікацію з найранішого фото
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
                            'created_at': ident.created_at.strftime('%d.%m.%Y %H:%M')
                        }
            
            existing_identifications = list(user_identifications.values())
        
        response_data = {
            'observation_id': observation.id,
            'location_name': observation.location.name,
            'photos': photos_data
        }

        # AI-прогноз для цієї observation (якщо є): передаємо в фронтенд,
        # щоб JS міг pre-fill species + показати бейдж впевненості.
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
    """Приймає файли та передає їх на обробку."""
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
    """Маршрут для ручного запуску процесу очищення старих фотографій."""
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

    # Спершу формуємо повний шлях до оригінального raw-файлу
    full_raw_path = os.path.join(raw_dir, filename)
    
    # Перевіряємо, чи існує цей файл на диску
    if os.path.exists(full_raw_path):
        # Якщо він є, віддаємо його, як і раніше
        return send_from_directory(raw_dir, filename)
    else:
        # Якщо raw-файлу немає, намагаємося віддати мініатюру як заміну.
        # Функція send_from_directory автоматично поверне помилку 404, 
        # якщо і мініатюри з таким іменем не існує.
        return send_from_directory(thumb_dir, filename)

# ═════════════════════════════════════════════════════════════════════════════
# CLEANUP (новий, з 2026-05-25) — заміна старого manual_batch_cleanup.
# Двофазний: POST /admin/cleanup/analyze → JS показує звіт → POST execute.
# Polling /admin/cleanup/task/<id> для прогресу обох фаз.
# ═════════════════════════════════════════════════════════════════════════════

@camera_traps_bp.route('/admin/cleanup/analyze', methods=['POST'])
@login_required
@role_required('admin')
def cleanup_analyze(lang_code):
    """Стартує асинхронний dry-run. Повертає report_id для polling."""
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
    """Виконує очищення на основі готового звіту. Повторно перевіряє active."""
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
    """Polling: повертає поточний стан analyze/execute."""
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
    """Перерахує консенсус для всіх pending спостережень згідно поточної конфігурації"""
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
    """Повертає список користувачів та видів для review фільтрів."""
    ct_session = get_ct_session()
    try:
        # ВИПРАВЛЕННЯ: Користувачі з основної бази через Flask-SQLAlchemy
        from app.models import User  # Основна база
        
        # Отримуємо user_id з CT бази, потім шукаємо користувачів в основній
        user_ids_query = ct_session.query(Identification.user_id).distinct().all()
        user_ids = [uid[0] for uid in user_ids_query]
        
        users = User.query.filter(User.id.in_(user_ids)).order_by(User.username).all()
        users_data = [{'id': u.id, 'username': u.username} for u in users]
        
        # Види з CT бази - це працює правильно
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
    """Галерея класифікованих фото з фільтрацією прав доступу."""
    ct_session = get_ct_session()
    try:
        # Визначаємо права користувача
        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        
        # Отримуємо SQL-фільтр (таблиця locations має аліас 'l')
        inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, table_alias='locations')

        can_manage_favorites = current_user.is_authenticated and current_user.has_role('manager')

        # Базовий запит для списку видів
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
   """API для отримання фото галереї з врахуванням прав доступу."""
   ct_session = get_ct_session()
   try:
       species_id = request.args.get('species_id', type=int)
       if species_id is None:
           return jsonify({'error': 'Species ID is required'}), 400

       # Права доступу
       user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
       is_admin = current_user.is_authenticated and current_user.has_role('admin')
       
       # Генеруємо фільтр
       inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, table_alias='locations')

       can_manage_favorites = current_user.is_authenticated and current_user.has_role('manager')

       # Формуємо запит
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

       # Фільтр по виду
       if species_id > 0:
           query = query.filter(Species.id == species_id)

       # Приховуємо спец-категорії (Пусто, Людина і т.д.) для звичайних користувачів
       if not can_manage_favorites:
           query = query.filter(Species.id > 0)

       photos = query.order_by(Photo.captured_at.desc()).all()

       if not photos:
           return jsonify({'message': _('Для вибраного виду немає фото у вибраному.')}), 404

       photos_data = []
       for photo in photos:
           # Отримуємо інформацію про того, хто додав у вибране
           first_identification = ct_session.query(Identification)\
               .filter(Identification.photo_id == photo.id)\
               .order_by(Identification.created_at)\
               .first()
           
           # Показуємо ім'я тільки модераторам/адмінам
           added_by_username = None
           if can_manage_favorites and first_identification:
               user = User.query.get(first_identification.user_id)
               if user:
                   added_by_username = user.username

           # Отримуємо назву виду
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
           
           # Додаємо added_by тільки для модераторів
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
    """API для видалення фото з вибраного (тільки для модераторів)."""
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

@camera_traps_bp.route('/manage-locations')
@login_required
@role_required('manager')
def manage_locations(lang_code):
    """Відображає об'єднану сторінку управління локаціями та журналу обслуговування."""
    ct_session = get_ct_session()
    try:
        is_admin = current_user.has_role('admin')
        user_inst_ids = [inst.id for inst in current_user.institutions]

        # Фільтрація локацій за установами, як в upload
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

        # Підтягуємо зв'язки локацій з установами одним запитом
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

        # Установи для фільтра — тільки ті, що є у видимих локаціях
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

        locations_json_string = json.dumps(locations_data)
        geoserver_url = current_app.config['GEOSERVER_URL']

        # Менеджер з установами теж може редагувати/створювати локації
        can_edit = is_admin or bool(user_inst_ids)

        # Список установ для форми створення локації
        if is_admin:
            user_institutions = Institution.query.order_by(Institution.name_uk).all()
        else:
            user_institutions = list(current_user.institutions)

        return render_template('manage_locations.html',
                               locations=locations_data,
                               biotopes=biotopes,
                               battery_types=battery_types,
                               visit_purposes=visit_purposes,
                               locations_json_string=locations_json_string,
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

# ── Деплойменти ───────────────────────────────────────────────────────────────
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
    """Застосовує надіслані поля до деплойменту (спільне для create/update).
    Кидає ValueError на невірний формат дати/числа."""
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
    """admin/quality_control -> завжди; manager -> лише якщо локація належить його установі."""
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
    """Сторінка управління деплойментами: карта локацій + таблиця/форма деплойментів."""
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

        # Зв'язки локацій з установами (для фільтра)
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

        # Деплойменти видимих локацій. Адмін / quality_control також бачать деплойменти
        # без GPS (location_id IS NULL); звичайний менеджер — лише з локацією.
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
                               locations_json_string=json.dumps(locations_data),
                               deployments_json_string=json.dumps(deployments_data),
                               geoserver_url=current_app.config['GEOSERVER_URL'],
                               filter_institutions=filter_institutions,
                               years=years,
                               is_admin=is_full_access,  # quality_control теж бачить усе
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
    """Оновлення полів деплойменту. Менеджер — лише деплойменти локацій своїх установ."""
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
    """Створення нового деплойменту на вибраній локації."""
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
    """Видалення деплойменту. Менеджер — лише деплойменти локацій своїх установ."""
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


# Експорт: (заголовок як у вихідному Екселі -> атрибут моделі Deployment)
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
    """Множина location_id з урахуванням ролі та (опційного) фільтра установи."""
    if institution_id:
        if not is_admin and institution_id not in user_inst_ids:
            return None  # немає доступу
        target = [institution_id]
    elif is_admin:
        target = None  # усі
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
    """Експорт деплойментів в Ексель з урахуванням фільтрів (установа, рік).
    Структура файлу повторює вихідний ARD-Ексель + назва установи й природний регіон."""
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

        # Локації (координати) + мапа location -> institution
        locs = {l.id: l for l in ct_session.query(Location).filter(Location.id.in_(loc_ids)).all()} if loc_ids else {}
        loc_inst = {}
        if loc_ids:
            for row in ct_session.execute(
                select(location_institutions.c.location_id, location_institutions.c.institution_id)
                .where(location_institutions.c.location_id.in_(loc_ids))
            ).fetchall():
                loc_inst.setdefault(row.location_id, row.institution_id)  # перша установа

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


# Похідні QC-поля (обчислюються на льоту, як у R-скрипті аналізу локацій).
# Критичні / некритичні — для впорядкування й кольору на графіках.
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

# Порядок QC-фільтрів як у вихідному Екселі (+ похідні зведені — в кінці).
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
    """None трактуємо як False для логіки якості (де треба обчислити «чи проблема»)."""
    return bool(v) if v is not None else False


def _kor(*vals):
    """3-значне OR (як в R). True переважає; за відсутності True NA дає NA."""
    has_na = False
    for v in vals:
        if v is True:
            return True
        if v is None:
            has_na = True
    return None if has_na else False


def _kand(a, b):
    """3-значне AND. False переважає; за відсутності False NA дає NA."""
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


@camera_traps_bp.route('/data-quality')
@login_required
@role_required('manager', 'quality_control')
def data_quality(lang_code):
    """Сторінка оцінки якості даних: карта + інтерактивні графіки QC.
    Похідні поля рахуються як у R-скрипті 01_Camera_trap_location_analysis."""
    ct_session = get_ct_session()
    try:
        is_admin = current_user.has_role('admin')
        is_full_access = is_admin or current_user.has_role('quality_control')
        user_inst_ids = [inst.id for inst in current_user.institutions]
        # _resolve_export_location_ids приймає is_admin — для quality_control «увесь доступ»
        # передаємо True, щоб отримати ВСІ локації.
        loc_ids = _resolve_export_location_ids(ct_session, is_full_access, user_inst_ids, None)
        if not loc_ids:
            loc_ids = []

        # Адмін / quality_control бачать також деплойменти без GPS (location_id IS NULL).
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

            qc_no_gps = (lat is None or lon is None)  # завжди True/False
            # 3-значна логіка (як в R-скрипті): NA OR NA = NA; NA OR TRUE = TRUE; FALSE OR FALSE = FALSE.
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

            # Для статусу маркера на мапі трактуємо None як «без проблеми» (None != True).
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
                # похідні QC
                'qc_no_gps_coordinates': qc_no_gps,
                'qc_data_not_usable': data_not_usable,
                'qc_summary': qc_summary,
                'qc_min_days_not_reached': min_days_not_reached,
            }
            for f in DEPLOYMENT_BOOL_FIELDS:
                if f not in rec:  # не перетираємо похідні
                    rec[f] = dep.__getattribute__(f)
            records.append(rec)

        # Опції категоріальних фільтрів
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
                               records_json=json.dumps(records),
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
    """API для оновлення даних локації. Менеджер може оновлювати тільки локації своїх установ."""
    ct_session = get_ct_session()
    try:
        is_admin = current_user.has_role('admin')

        # Перевірка доступу до локації за установою
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

        # Оновлення установ
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
    """API для створення нової локації. Менеджер створює тільки для своїх установ."""
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

        # Перевірка: менеджер може призначати тільки свої установи
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
    """Маршрут для ручного запуску процесу видалення оригіналів, що не є у вибраному."""
    try:
        current_app.logger.info(f"Manual deletion of unfavorited originals triggered by admin: {current_user.username}")
        
        # Імпортуємо нову функцію
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
    """Маршрут для ручного запуску повного перерахунку аналітики."""
    try:
        current_app.logger.info(f"Manual analytics recalculation triggered by admin: {current_user.username}")
        
        # Викликаємо нашу функцію з прапором force_run=True
        update_analytics_tables(force_run=True)
        
        flash(_('Процес перерахунку аналітики успішно завершено.'), 'success')
        
    except Exception as e:
        current_app.logger.error(f"Manual analytics recalculation failed: {e}", exc_info=True)
        flash(_('Під час перерахунку аналітики сталася помилка. Перевірте логи.'), 'danger')

    return redirect(url_for('camera_traps.admin_panel', lang_code=g.lang_code))

# --- СЕКЦІЯ ЕКСПОРТУ ДАНИХ (додати в кінець файлу routes.py) ---

@camera_traps_bp.route('/data-export')
@login_required
@role_required('analyst')
def ct_data_export(lang_code):
    """
    Сторінка для підготовки та експорту даних з модуля фотопасток.
    Доступно: будь-який користувач з can_export=True хоча б для однієї установи; admin — без обмежень.
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
    """
    API для отримання динамічних таксономічних фільтрів для фотопасток.
    """
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
                    # Адаптуємо назви колонок до моделі
                    db_column = 'class' if key == 'class' else 'order_rank' if key == 'order' else key
                    conditions.append(f"s.{db_column} = :{key}")
                    params[key] = value
            
            where_clause = "WHERE " + " AND ".join(conditions)
            # Звертаємось до колонки "class" в лапках, щоб уникнути помилки SQL
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
    Повертає список institution_ids для поточного запиту з урахуванням прав.
    - admin: може вибирати будь-які; якщо не передано — None (без обмеження).
    - інші: перетин запитаних і тих, де can_export=True; якщо не передано — всі дозволені.
    """
    is_admin = current_user.has_role('admin')
    allowed_ids = None if is_admin else {i.id for i in current_user.export_institutions}

    raw = request.args.get('institution_ids', '')
    if raw:
        requested = [int(x) for x in raw.split(',') if x.strip().isdigit()]
        if is_admin:
            return requested if requested else None
        # Фільтруємо лише ті, що входять в дозволені для експорту
        valid = [i for i in requested if i in allowed_ids]
        return valid if valid else list(allowed_ids)
    else:
        # Нічого не передано — для admin без обмеження, для інших — всі дозволені
        return None if is_admin else list(allowed_ids)


@camera_traps_bp.route('/api/data-preview')
@login_required
@role_required('analyst')
def api_ct_data_preview(lang_code):
    """API для попереднього перегляду даних з фотопасток."""
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
            'institution_code': request.args.get('institution_code', 'RSNR'),
            'filter_type': request.args.get('filter_type', 'species_only'),
            'institution_ids': _get_export_institution_ids(),
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
    """API для завантаження CSV-файлу з даними фотопасток."""
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
            'institution_code': request.args.get('institution_code', 'WNBO-CT'),
            'filter_type': request.args.get('filter_type', 'species_only'),
            'institution_ids': _get_export_institution_ids(),
        }
        
        result = get_ct_occurrence_data(filters, limit=None)
        data = result['data']

        if not data:
            # Повертаємо відповідь, яку може обробити JavaScript
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
    """
    Підраховує та повертає кількість серій, що залишились для ідентифікації поточним користувачем.
    """
    ct_session = get_ct_session()
    try:
        scope_institution_id = request.args.get('scope_institution_id', type=int)
        scope_ecoregion = request.args.get('scope_ecoregion', '')
        ai_species_id = request.args.get('ai_species_id', type=int)

        # Отримуємо ID фотографій, вже ідентифікованих цим користувачем
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

        # Додатковий фільтр scope (вибрана установа або екорегіон)
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

        # AI-фільтр: рахуємо лише серії, де AI визначив вибраний вид
        # (від активної моделі). Тихо ігнорується, якщо AI ще не активний.
        ai_observation_subq = None
        if ai_species_id is not None:
            from .ai_runner import is_ai_available
            from .models import AIModel, AIPrediction
            if is_ai_available():
                active_model = ct_session.query(AIModel).filter_by(is_active=True).first()
                if active_model is not None:
                    ai_observation_subq = (
                        select(AIPrediction.observation_id)
                        .where(
                            AIPrediction.model_id == active_model.id,
                            AIPrediction.prediction_species_id == ai_species_id,
                        )
                        .distinct()
                    )

        # Рахуємо кількість спостережень, які в статусі 'pending' і
        # НЕ містять жодного фото, яке користувач вже ідентифікував
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

#
# --- СТОРІНКА ЖУРНАЛУ ОБСЛУГОВУВАННЯ ---
#

@camera_traps_bp.route('/service-log')
@login_required
@role_required('manager')
def service_log(lang_code):
    """Перенаправляє на об'єднану сторінку управління локаціями."""
    return redirect(url_for('camera_traps.manage_locations', lang_code=lang_code))

@camera_traps_bp.route('/api/locations-with-status')
@login_required
@role_required('manager')
def api_get_locations_with_status(lang_code):
    """
    API, що повертає список локацій з їхнім прогнозованим статусом.
    ФІНАЛЬНА ВЕРСІЯ: Враховує неактивні камери та обирає "найгірший" прогноз.
    Фільтрує за установами поточного користувача (адмін бачить все).
    """
    ct_session = get_ct_session()
    try:
        INACTIVE_PURPOSE_IDS = {3, 4}
        TIME_WARNING_DAYS = 180
        TIME_CRITICAL_DAYS = 300
        PHOTO_WARNING_COUNT = 5000
        PHOTO_CRITICAL_COUNT = 10000

        # Створюємо словник для визначення "ваги" статусу
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
                # Перевірка №1: Чи є камера активною?
                if last_visit.visit_purpose_id in INACTIVE_PURPOSE_IDS:
                    status = 'inactive'
                    status_reason = last_visit.visit_purpose.get_name(g.lang_code)
                    days_since_visit = (datetime.now().date() - last_visit.visit_datetime.date()).days
                
                else:
                    # Камера активна, продовжуємо з прогнозуванням
                    days_since_visit = (datetime.now().date() - last_visit.visit_datetime.date()).days
                    stats = loc.stats
                    
                    # --- ПОЧАТОК НОВОЇ ЛОГІКИ "НАЙГІРШОГО СЦЕНАРІЮ" ---

                    # 1. Розраховуємо статус на основі ЧАСУ
                    time_status = 'ok'
                    if days_since_visit >= TIME_CRITICAL_DAYS:
                        time_status = 'critical'
                    elif days_since_visit >= TIME_WARNING_DAYS:
                        time_status = 'warning'

                    # 2. Розраховуємо статус на основі ФОТО (якщо можливо)
                    photo_status = 'ok' # За замовчуванням
                    if stats and stats.avg_photos_per_day > 0:
                        predicted_photos = int(days_since_visit * float(stats.avg_photos_per_day))
                        if predicted_photos >= PHOTO_CRITICAL_COUNT:
                            photo_status = 'critical'
                        elif predicted_photos >= PHOTO_WARNING_COUNT:
                            photo_status = 'warning'

                    # 3. Порівнюємо статуси і обираємо НАЙГІРШИЙ
                    if status_severity[photo_status] > status_severity[time_status]:
                        # Якщо статус по фото гірший, обираємо його
                        status = photo_status
                        status_reason = _("Прогноз за кількістю фото")
                    else:
                        # В іншому випадку (статус по часу гірший або однаковий), обираємо час
                        status = time_status
                        status_reason = _("Прогноз за часом")
                        
                    # --- КІНЕЦЬ НОВОЇ ЛОГІКИ ---

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
    """API для отримання історії обслуговування для конкретної локації."""
    ct_session = get_ct_session()
    try:
        # Перевірка доступу: адмін бачить все, менеджер — тільки свої установи
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
            # Отримуємо ім'я користувача з основної БД
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
    """API для створення нового запису в журналі обслуговування."""
    ct_session = get_ct_session()
    try:
        data = request.json

        # --- Валідація та отримання даних ---
        location_id = data.get('location_id')
        visit_purpose_id = data.get('visit_purpose_id')
        visit_datetime_str = data.get('visit_datetime')

        if not all([location_id, visit_purpose_id, visit_datetime_str]):
            return jsonify({'success': False, 'error': _('Не всі обов\'язкові поля заповнені.')}), 400

        # --- Перевірка доступу до локації за установою ---
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

        # --- Перетворення типів даних ---
        visit_datetime = datetime.fromisoformat(visit_datetime_str)
        
        # Необов'язкові поля
        battery_type_id = data.get('battery_type_id')
        battery_type_id = int(battery_type_id) if battery_type_id else None
        
        photos_on_card = data.get('photos_on_card')
        photos_on_card = int(photos_on_card) if photos_on_card else None

        # Конвертація 'true'/'false'/'' в True/False/None
        is_operational_str = data.get('is_camera_operational')
        if is_operational_str == 'true':
            is_camera_operational = True
        elif is_operational_str == 'false':
            is_camera_operational = False
        else:
            is_camera_operational = None

        # --- Створення об'єкта та збереження в БД ---
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
    """API для редагування існуючого запису в журналі обслуговування."""
    ct_session = get_ct_session()
    try:
        visit = ct_session.query(ServiceVisit).get(visit_id)
        if not visit:
            return jsonify({'success': False, 'error': _('Запис не знайдено.')}), 404

        is_admin = current_user.has_role('admin')

        if not is_admin:
            # Перевірка власності запису
            if visit.user_id != current_user.id:
                return jsonify({'success': False, 'error': _('Недостатньо прав для редагування цього запису.')}), 403
            # Перевірка доступу до локації за установою
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
@role_required('manager') # Доступ для модераторів та адмінів
def run_stats_calculation(lang_code):
    """Запускає повний перерахунок статистики для локацій."""
    try:
        from .service_analytics import update_all_location_stats
        
        # Запускаємо в режимі --force, оскільки користувач сам ініціює дію
        update_all_location_stats(force_run=True)
        
        return jsonify({'success': True, 'message': _('Перерахунок статистики успішно завершено! Карта буде оновлена.')})
        
    except Exception as e:
        current_app.logger.error(f"Manual stats calculation failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': _('Під час перерахунку сталася помилка.')}), 500
    
# Глобальна змінна для кешування списку видів
# Структура: {'data': [...], 'timestamp': datetime object}
_species_list_cache = {
    'data': None,
    'timestamp': None
}

def get_cached_species_for_filter():
    """
    Повертає список видів для фільтру.
    Кешує результат на 7 днів.
    """
    global _species_list_cache
    now = datetime.now()
    CACHE_TTL_DAYS = 7

    # Перевіряємо, чи є кеш і чи він свіжий
    if (_species_list_cache['data'] is not None and 
        _species_list_cache['timestamp'] is not None and 
        (now - _species_list_cache['timestamp']).days < CACHE_TTL_DAYS):
        return _species_list_cache['data']

    # Якщо кешу немає або він застарів - робимо запит до БД
    ct_session = get_ct_session()
    try:
        # Вибираємо тільки види, які реально зустрічаються (id > 0)
        # Сортуємо за українською назвою, якщо вона є, інакше за латиною
        species_query = ct_session.query(Species)\
            .filter(Species.id > 0)\
            .order_by(Species.common_name_ua, Species.scientific_name)\
            .all()

        species_list = []
        for s in species_query:
            # Формуємо назву залежно від наявності перекладів (логіка як у вас була)
            name_ua = s.common_name_ua if s.common_name_ua else s.scientific_name
            name_en = s.common_name_en if s.common_name_en else s.scientific_name
            scientific = s.scientific_name
            
            # Зберігаємо всі варіанти, щоб шаблон міг вибрати потрібну мову
            species_list.append({
                'id': s.id,
                'name_ua': f"{name_ua} ({scientific})",
                'name_en': f"{name_en} ({scientific})",
                'scientific': scientific
            })

        # Оновлюємо кеш
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
    """Сторінка детального аналізу з фільтрацією доступних видів."""
    ct_session = get_ct_session()
    try:
        # 1. Права доступу
        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        
        # Отримуємо фільтр для локацій (аліас 'l')
        inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, table_alias='locations')

        # 2. Отримуємо тільки ті види, які є на доступних локаціях
        # Використовуємо JOIN для перевірки наявності детекцій на дозволених місцях
        species_query = ct_session.query(Species)\
            .join(Identification, Species.id == Identification.species_id)\
            .join(Photo, Identification.photo_id == Photo.id)\
            .join(Observation, Photo.observation_id == Observation.id)\
            .join(Location, Observation.location_id == Location.id)\
            .filter(
                Species.id > 0,
                Observation.status.in_(['completed', 'archived']),
                text(inst_condition) # Фільтр установ
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
        
        # Дати за замовчуванням
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
            
        # --- ПРАВА ДОСТУПУ ---
        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        
        # Важливо: використовуємо аліас 'l', бо він прописаний у SQL нижче
        inst_condition, inst_params = get_institution_filter(user_inst_ids, is_admin, table_alias='l')

        params = {
            'species_id': species_id,
            'start_date': start_date_str,
            'end_date': end_date_str
        }
        params.update(inst_params) # Додаємо параметри установ (:user_inst_ids)

        # Консенсус CTE (без змін)
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
        
        # Основний запит з доданим фільтром inst_condition
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

        # Формування результатів (тут логіка залишається такою ж)
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

        # Розрахунок EFFORT (Трап-днів)
        # Отримуємо дати активності тільки для локацій, де знайшли вид
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
        # Важливо закрити сесію, бо ми відкривали connection напряму
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

        # Читаємо прапорець CI
        compute_ci_param = request.args.get('compute_ci', 'false').lower() == 'true'

        # Перевірка прав: CI тільки для авторизованих
        compute_ci = compute_ci_param and current_user.is_authenticated

        try:
            bw_adjust = float(request.args.get('bw_adjust', 0.25))
            if bw_adjust < 0: bw_adjust = 0 # Мінімум 0
            if bw_adjust > 1.0: bw_adjust = 1.0
        except ValueError:
            bw_adjust = 0.1

        if not all([start_date_str, end_date_str, species_ids_str]):
            return jsonify({'error': 'Missing parameters'}), 400

        species_ids = [int(x) for x in species_ids_str.split(',') if x.isdigit()]
        if not species_ids: return jsonify({'error': 'No valid species'}), 400

        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

        # ── Scope-фільтр (установа / екорегіон) ──
        is_admin = current_user.is_authenticated and current_user.has_role('admin')
        user_inst_ids = [inst.id for inst in current_user.institutions] if current_user.is_authenticated else []

        # Access control: non-admin може запитувати лише в межах своїх установ
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

        # Обчислюємо list location_ids; None означає "усі локації"
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
            # Не-адмін у "global" режимі — обмежимось доступними йому локаціями
            loc_subq = ct_session.query(location_institutions.c.location_id).filter(
                location_institutions.c.institution_id.in_(user_inst_ids)
            ).distinct().all()
            location_ids = [row[0] for row in loc_subq]

        # Якщо scope обраний, але жодної локації не знайшлося — повертаємо порожньо
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

            # Дозволяємо будувати навіть по 2 точках, якщо без CI (просто покаже піки)
            min_points = 5 if compute_ci else 2
            if total_points < min_points: continue

            # Передаємо compute_ci та bw_adjust
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

                # Назва виду
                species = ct_session.query(Species).get(sp_id)
                name = species.scientific_name
                if g.lang_code == 'uk' and species.common_name_ua:
                    name = species.common_name_ua
                elif g.lang_code == 'en' and species.common_name_en:
                    name = species.common_name_en
                species_info[sp_id] = name
        # Рахуємо матрицю тільки якщо є дані для 2 і більше видів
        if len(results) >= 2:
            from .daily_analytics import calculate_overlap_matrix # Можна і тут імпортнути
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
    Генерує CSV файл.
    Враховує bw_adjust (0 = сирі дані) та compute_ci (False = швидко, без інтервалів).
    """
    ct_session = get_ct_session()
    try:
        start_date_str = request.args.get('start_date')
        end_date_str = request.args.get('end_date')
        species_ids_str = request.args.get('species_ids', '')
        scope_type = request.args.get('scope_type', 'global')
        scope_id = request.args.get('scope_id', '')

        # 1. Зчитуємо параметри згладжування
        try:
            bw_adjust = float(request.args.get('bw_adjust', 0.25))
            if bw_adjust < 0: bw_adjust = 0
            if bw_adjust > 1.0: bw_adjust = 1.0
        except ValueError:
            bw_adjust = 0.1

        # 2. Зчитуємо параметр CI (чи треба рахувати інтервали)
        # За замовчуванням False, якщо користувач не передав явне 'true'
        compute_ci_param = request.args.get('compute_ci', 'false').lower() == 'true'
        compute_ci = compute_ci_param # Оскільки це @login_required, додаткова перевірка не критична

        if not all([start_date_str, end_date_str, species_ids_str]):
            return "Missing parameters", 400

        species_ids = [int(x) for x in species_ids_str.split(',') if x.isdigit()]
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

        # ── Scope-фільтр (та сама логіка що в api_daily_activity) ──
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
            # Скоуп є, але порожній — повертаємо порожній CSV з заголовком
            return Response("", mimetype="text/csv")

        # Отримуємо дані
        total_effort = calculate_total_effort(ct_session, start_date, end_date, location_ids=location_ids)
        raw_data = fetch_raw_daily_data(ct_session, start_date_str, end_date_str, species_ids, location_ids=location_ids)
        
        results = {}
        species_info = {}
        
        for sp_id in species_ids:
            sp_raw_data = raw_data.get(sp_id, {})
            # Дозволяємо навіть малу кількість точок, якщо це експорт без CI
            min_points = 5 if compute_ci else 1
            if sum(len(v) for v in sp_raw_data.values()) < min_points: 
                continue
            
            # Викликаємо розрахунок
            # Якщо compute_ci=False, то n_boot=0, і цикл бутстрепу пропускається -> ШВИДКО
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
    """
    Сторінка аналізу добової активності.
    """
    try:
        # Дати за замовчуванням: 1 січня поточного року - сьогодні
        today = date.today()
        default_start = (today - timedelta(days=365)).strftime('%Y-%m-%d')
        default_end = today.strftime('%Y-%m-%d')

        # Отримуємо список видів (використовуємо існуючу функцію кешування)
        species_list = get_cached_species_for_filter()

        # Установи та екорегіони для scope-фільтру (та сама логіка що в species_dashboard)
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