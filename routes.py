# myproject/app/camera_traps/routes.py

from flask import render_template, g, flash, redirect, url_for, jsonify, request, current_app, send_from_directory, abort, Response
from datetime import datetime, date, timedelta
from flask_login import login_required, current_user
from flask_babel import gettext as _
from sqlalchemy import func, distinct, extract, select, text
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
from .models import Location, Species, Photo, Observation, Identification, BehaviorType, UserProfile, Biotope, SpeciesYearlyTrend, LocationMonthlyActivity
from .models import ServiceVisit, BatteryType, VisitPurpose, LocationStats, location_institutions
from app.models import User, Institution
from .decorators import role_required
from .data_export import get_ct_occurrence_data
from .daily_analytics import fetch_raw_daily_data, calculate_activity_curve, generate_csv_export, calculate_overlap_matrix

#
# --- ГОЛОВНИЙ АНАЛІТИЧНИЙ ДАШБОРД ---
#
@camera_traps_bp.route('/dashboard')
@camera_traps_bp.route('/')
def dashboard(lang_code):
    """Відображає дашборд з основною статистикою, ФІЛЬТРОВАНОЮ ЗА ДАТОЮ, ЛОКАЦІЯМИ ТА БІОТОПАМИ."""
    ct_session = get_ct_session()
    ct_profile = None
    try:
        if current_user.is_authenticated:
            ct_profile = current_user.get_ct_profile()

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
                             ct_profile=ct_profile,
                             biotopes=biotopes_list,
                             selected_locations=location_ids_str,
                             selected_biotopes=biotope_ids,
                             institutions=institutions_list,
                             selected_institutions=selected_inst_ids)
        
    except Exception as e:
        current_app.logger.error(f"Error in dashboard: {str(e)}")
        stats = {'total_photos': 0, 'total_locations': 0, 'total_identifications': 0, 'identified_species_count': 0, 'top_contributors': []}
        flash(_('Помилка завантаження статистики.'), 'warning')
        return render_template('dashboard.html', stats=stats, start_date='2020-08-01', end_date=date.today().strftime('%Y-%m-%d'), ct_profile=ct_profile, biotopes=[], selected_locations='', selected_biotopes=[])
    finally:
        close_ct_session()

#
# --- СТОРІНКА ДЕТАЛЬНОГО АНАЛІЗУ ПО ВИДАХ ---
#
# ЗАМІНІТЬ ВАШУ ІСНУЮЧУ ФУНКЦІЮ species_dashboard НА ЦЮ

@camera_traps_bp.route('/analysis/species-dashboard')
def species_dashboard(lang_code):
    """Сторінка детального аналізу по видах."""
    ct_session = get_ct_session()
    try:
        # Встановлюємо мінімальну кількість спостережень
        MIN_OBSERVATIONS = 30

        # Крок 1: Створюємо підзапит, щоб знайти ID видів,
        # у яких кількість унікальних спостережень >= MIN_OBSERVATIONS.
        # Цей запит рахує по "сирим" даних, щоб отримати точну загальну кількість.
        subquery = ct_session.query(
            Identification.species_id
        ).join(Photo, Identification.photo_id == Photo.id)\
         .join(Observation, Photo.observation_id == Observation.id)\
         .filter(Observation.status.in_(['completed', 'archived']))\
         .filter(Identification.species_id > 0) \
         .group_by(Identification.species_id)\
         .having(func.count(distinct(Photo.observation_id)) >= MIN_OBSERVATIONS)\
         .subquery()

        # Крок 2: Тепер вибираємо об'єкти Species, ID яких є в результатах підзапиту.
        species_query = ct_session.query(Species)\
            .join(subquery, Species.id == subquery.c.species_id)\
            .order_by(Species.common_name_ua)\
            .all()
            
        species_list = []
        for s in species_query:
            display_name = s.scientific_name
            if g.lang_code == 'uk' and s.common_name_ua:
                display_name = f"{s.common_name_ua} ({s.scientific_name})"
            elif g.lang_code == 'en' and s.common_name_en:
                display_name = f"{s.common_name_en} ({s.scientific_name})"
            species_list.append({'id': s.id, 'text': display_name})
        
        all_years = ct_session.query(SpeciesYearlyTrend.year).distinct().order_by(SpeciesYearlyTrend.year).all()
        available_years = [y[0] for y in all_years]
        start_year = available_years[0] if available_years else date.today().year - 5
        end_year = available_years[-1] if available_years else date.today().year

        return render_template('species_dashboard.html', 
                             available_species=species_list, 
                             available_years=available_years,
                             start_year=start_year, 
                             end_year=end_year)
    except Exception:
        current_app.logger.error("Error loading species dashboard", exc_info=True)
        flash(_("Помилка завантаження сторінки аналізу."), 'danger')
        return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))
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
@role_required('identifier')
def identify(lang_code):
    ct_session = get_ct_session()
    try:
        form = IdentificationForm()
        ct_profile = current_user.get_ct_profile()
        # ТУТ ТРЕБА БУДЕ ПОПРАВИТИ ПЕРЕВІРКУ ПІСЛЯ ОСТАТОЧНОГО ПРЕХОДУ НА НОВУ СИСТЕМУ АУТЕНТИФІКАЦІЇ
        can_review_old = ct_profile and ct_profile.camera_trap_role in ['moderator', 'admin']
        can_review_new = current_user.has_role('moderator') or current_user.has_role('admin')
        can_review = can_review_old or can_review_new
        
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

        # Передаємо в шаблон вже заповнені динамічно списки
        return render_template('identification.html', 
                             form=form, 
                             grouped_species=grouped_species, 
                             empty_choices=empty_choices,
                             other_special_choices=other_special_choices,
                             can_review=can_review)
    finally:
        close_ct_session()

@camera_traps_bp.route('/upload', methods=['GET', 'POST'])
@login_required
@role_required('moderator','manager')
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
@role_required('moderator')
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
@role_required('moderator')
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
@role_required('moderator')
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
@role_required('moderator')
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
        # --- ЗМІНЕНО: Отримуємо роки замість дат ---
        start_year = request.args.get('start_year', type=int)
        end_year = request.args.get('end_year', type=int)

        if not all([species_id, start_year, end_year]):
            return jsonify({'error': 'Species ID, start year, and end year are required'}), 400

        # 1. Сезонна активність (з проміжної таблиці)
        seasonal_query = ct_session.query(
            LocationMonthlyActivity.year,
            LocationMonthlyActivity.month,
            func.sum(LocationMonthlyActivity.detection_count).label('observation_count')
        ).filter(
            LocationMonthlyActivity.species_id == species_id,
            LocationMonthlyActivity.year.between(start_year, end_year)
        ).group_by(LocationMonthlyActivity.year, LocationMonthlyActivity.month)\
         .order_by(LocationMonthlyActivity.year, LocationMonthlyActivity.month)\
         .all()
        
        seasonal_data = [{'year': r.year, 'month': r.month, 'count': r.observation_count} for r in seasonal_query]
            
        # 2. Річна динаміка (з фінальної "вітринної" таблиці)
        yearly_query = ct_session.query(SpeciesYearlyTrend).filter(
            SpeciesYearlyTrend.species_id == species_id,
            SpeciesYearlyTrend.year.between(start_year, end_year)
        ).order_by(SpeciesYearlyTrend.year).all()

        yearly_data = [{
            'year': r.year, 
            'mean_dr_index': r.mean_dr_index,
            'lower_ci': r.lower_ci,
            'upper_ci': r.upper_ci
        } for r in yearly_query]

        return jsonify({'seasonal_activity': seasonal_data, 'yearly_trend': yearly_data})
    except Exception as e:
        current_app.logger.error(f"Error in api_species_dynamics: {e}")
        return jsonify({'error': 'Internal server error'}), 500
    finally:
        close_ct_session()
        
# --- API для ідентифікації та завантаження ---
@camera_traps_bp.route('/api/submit-identification', methods=['POST'])
@login_required
@role_required('identifier')
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
        
        ct_profile = ct_session.query(UserProfile).get(current_user.id)
        if not ct_profile:
            return jsonify({'success': False, 'error': _('Профіль користувача для фотопасток не знайдено.')}), 404

        is_moderator_old = ct_profile and ct_profile.camera_trap_role in ['moderator', 'admin']
        is_moderator_new = current_user.has_role('moderator') or current_user.has_role('admin')
        is_moderator = is_moderator_old or is_moderator_new

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
        
        # Отримуємо ID біотопів, пов'язаних з цією локацією
        biotope_ids = [biotope.id for biotope in location.biotopes]
        
        return jsonify({
            'id': location.id,
            'name': location.name,
            'latitude': float(location.latitude),
            'longitude': float(location.longitude),
            'biotope_ids': biotope_ids,
            'description': location.description or '' # <-- ДОДАНО: повертаємо опис або порожній рядок
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
        
        # Перевіряємо права доступу для review режиму
        if review_mode:
            ct_profile = current_user.get_ct_profile()
            has_old_access = ct_profile and ct_profile.camera_trap_role in ['moderator', 'admin']
            has_new_access = current_user.has_role('moderator') or current_user.has_role('admin')
            
            if not (has_old_access or has_new_access):
                return jsonify({'error': _('Недостатньо прав для режиму перегляду')}), 403
        
        user_identified_photos = ct_session.query(Identification.photo_id).filter_by(user_id=current_user.id)
        
        if review_mode:
            # В review режимі показуємо як pending так і completed з ідентифікаціями
            query = ct_session.query(Observation).filter(
                Observation.status.in_(['pending', 'completed']),
                Observation.photos.any(Photo.identifications.any()),
                ~Observation.photos.any(Photo.id.in_(user_identified_photos))
            )
            
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
            observation = ct_session.query(Observation).filter(
                Observation.status == 'pending', 
                ~Observation.photos.any(Photo.id.in_(user_identified_photos))
            ).order_by(func.random()).first()
        
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
        
        if review_mode:
            response_data['existing_identifications'] = existing_identifications
            response_data['review_mode'] = True
            
        return jsonify(response_data)
    finally:
        close_ct_session()

@camera_traps_bp.route('/api/create-location', methods=['POST'])
@login_required
@role_required('identifier')
def create_location(lang_code):
    ct_session = get_ct_session()
    try:
        data = request.json
        # -- ОСЬ ВИПРАВЛЕННЯ --
        name = data.get('name')
        lat = data.get('latitude')
        lon = data.get('longitude')
        description = data.get('description') 

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
@role_required('identifier')
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
    
    return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))

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

@camera_traps_bp.route('/admin/batch-stats')
@login_required
@role_required('admin')
def batch_statistics(lang_code):
    """Сторінка статистики батчів для адмінів."""
    try:
        from .background_tasks import get_batch_statistics
        stats = get_batch_statistics()
        return render_template('admin/batch_stats.html', stats=stats)
    except Exception as e:
        current_app.logger.error(f"Error loading batch statistics: {e}")
        flash(_("Помилка завантаження статистики батчів."), 'danger')
        return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))

@camera_traps_bp.route('/admin/cleanup-batches', methods=['POST'])
@login_required
@role_required('admin')
def manual_batch_cleanup(lang_code):
    """Ручний запуск очищення застрялих батчів."""
    try:
        from .background_tasks import cleanup_stale_batches
        current_app.logger.info(f"Manual batch cleanup triggered by admin: {current_user.username}")
        cleanup_stale_batches()
        flash(_('Процес очищення батчів успішно запущено та завершено.'), 'success')
    except Exception as e:
        current_app.logger.error(f"Manual batch cleanup failed. Error: {e}")
        flash(_('Під час очищення батчів сталася помилка. Перевірте логи.'), 'danger')
    
    return redirect(url_for('camera_traps.batch_statistics', lang_code=g.lang_code))

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
    
    return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))

@camera_traps_bp.route('/api/review-filters')
@login_required
@role_required('moderator')
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

        can_manage_favorites = False
        if current_user.is_authenticated:
            ct_profile = current_user.get_ct_profile()
            can_manage_old = ct_profile and ct_profile.camera_trap_role in ['moderator', 'admin']
            can_manage_new = current_user.has_role('moderator') or current_user.has_role('admin')
            can_manage_favorites = can_manage_old or can_manage_new

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

       can_manage_favorites = False
       if current_user.is_authenticated:
           ct_profile = current_user.get_ct_profile()
           can_manage_old = ct_profile and ct_profile.camera_trap_role in ['moderator', 'admin']
           can_manage_new = current_user.has_role('moderator') or current_user.has_role('admin')
           can_manage_favorites = can_manage_old or can_manage_new

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
@role_required('moderator')
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
@role_required('admin')
def manage_locations(lang_code):
    """Відображає сторінку для редагування локацій."""
    ct_session = get_ct_session()
    try:
        locations_objects = ct_session.query(Location).order_by(Location.name).all()
        biotopes = ct_session.query(Biotope).order_by(Biotope.name_ua).all()

        # Створюємо список словників, як і раніше
        locations_data = []
        for loc in locations_objects:
            locations_data.append({
                'id': loc.id,
                'name': loc.name,
                'latitude': float(loc.latitude),
                'longitude': float(loc.longitude)
            })
        
        # <-- НОВИЙ НАДІЙНИЙ ПІДХІД -->
        # Серіалізуємо дані в JSON-рядок за допомогою стандартної бібліотеки Python
        locations_json_string = json.dumps(locations_data)

        geoserver_url = current_app.config['GEOSERVER_URL']
        
        return render_template('manage_locations.html', 
                               locations=locations_data, # Ця змінна для HTML-списку зліва
                               biotopes=biotopes,
                               locations_json_string=locations_json_string,
                               geoserver_url=geoserver_url) # Цей готовий рядок для JavaScript
                               
    except Exception as e:
        current_app.logger.error(f"Error loading location management page: {e}", exc_info=True)
        flash(_("Помилка завантаження сторінки управління локаціями."), 'danger')
        return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))
    finally:
        close_ct_session()

@camera_traps_bp.route('/api/update-location/<int:location_id>', methods=['POST'])
@login_required
@role_required('admin')
def update_location(lang_code, location_id):
    """API для оновлення даних локації."""
    ct_session = get_ct_session()
    try:
        data = request.json
        location = ct_session.query(Location).get(location_id)
        if not location:
            return jsonify({'success': False, 'error': _('Локацію не знайдено.')}), 404

        location.name = data.get('name', location.name)
        location.latitude = data.get('latitude', location.latitude)
        location.longitude = data.get('longitude', location.longitude)
        location.description = data.get('description', location.description) # <-- ДОДАНО: оновлюємо поле опису

        # Оновлення біотопів (зв'язок M2M)
        biotope_ids = data.get('biotope_ids', [])
        # Отримуємо об'єкти біотопів з БД
        selected_biotopes = ct_session.query(Biotope).filter(Biotope.id.in_(biotope_ids)).all()
        # Повністю замінюємо список біотопів для цієї локації
        location.biotopes = selected_biotopes

        ct_session.commit()
        return jsonify({'success': True, 'message': _('Дані локації оновлено успішно!')})
    except Exception as e:
        ct_session.rollback()
        current_app.logger.error(f"Error updating location {location_id}: {e}")
        return jsonify({'success': False, 'error': _('Помилка збереження даних.')}), 500
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
    
    return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))

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
    
    return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))

# --- СЕКЦІЯ ЕКСПОРТУ ДАНИХ (додати в кінець файлу routes.py) ---

@camera_traps_bp.route('/data-export')
@login_required
@role_required('data_user')
def ct_data_export(lang_code):
    """
    Сторінка для підготовки та експорту даних з модуля фотопасток.
    """
    g.lang_code = lang_code
    try:
        return render_template('ct_data_export.html')
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

@camera_traps_bp.route('/api/data-preview')
@login_required
@role_required('data_user')
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
            'filter_type': request.args.get('filter_type', 'species_only')
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
@role_required('data_user')
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
            'filter_type': request.args.get('filter_type', 'species_only')
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
@role_required('identifier')
def api_get_identification_stats(lang_code):
    """
    Підраховує та повертає кількість серій, що залишились для ідентифікації поточним користувачем.
    """
    ct_session = get_ct_session()
    try:
        # Отримуємо ID фотографій, вже ідентифікованих цим користувачем
        user_identified_photos = ct_session.query(Identification.photo_id)\
                                             .filter_by(user_id=current_user.id)

        # Рахуємо кількість спостережень, які в статусі 'pending' і
        # НЕ містять жодного фото, яке користувач вже ідентифікував
        remaining_count = ct_session.query(Observation.id)\
                                    .filter(
                                        Observation.status == 'pending',
                                        ~Observation.photos.any(Photo.id.in_(user_identified_photos))
                                    ).count()

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
@role_required('identifier') # Доступ для ідентифікаторів і вище
def service_log(lang_code):
    """Відображає сторінку для ведення журналу обслуговування фотопасток."""
    ct_session = get_ct_session()
    try:
        # Отримуємо довідники для заповнення випадаючих списків у формі
        battery_types = ct_session.query(BatteryType).order_by(BatteryType.name_ua).all()
        visit_purposes = ct_session.query(VisitPurpose).order_by(VisitPurpose.name_ua).all()
        
        # Отримуємо URL Geoserver з конфігурації
        geoserver_url = current_app.config['GEOSERVER_URL']

        return render_template('service_log.html',
                               battery_types=battery_types,
                               visit_purposes=visit_purposes,
                               geoserver_url=geoserver_url)
                               
    except Exception as e:
        current_app.logger.error(f"Error loading service log page: {e}", exc_info=True)
        flash(_("Помилка завантаження сторінки журналу обслуговування."), 'danger')
        return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))
    finally:
        close_ct_session()

@camera_traps_bp.route('/api/locations-with-status')
@login_required
def api_get_locations_with_status(lang_code):
    """
    API, що повертає список локацій з їхнім прогнозованим статусом.
    ФІНАЛЬНА ВЕРСІЯ: Враховує неактивні камери та обирає "найгірший" прогноз.
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
        
        locations = ct_session.query(Location).all()
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
def api_get_service_history(lang_code, location_id):
    """API для отримання історії обслуговування для конкретної локації."""
    ct_session = get_ct_session()
    try:
        visits = ct_session.query(ServiceVisit)\
            .filter(ServiceVisit.location_id == location_id)\
            .order_by(ServiceVisit.visit_datetime.desc())\
            .limit(20).all() # Обмежимо 20 останніми записами для швидкодії

        history_data = []
        for v in visits:
            # Отримуємо ім'я користувача з основної БД
            user = User.query.get(v.user_id)
            username = user.username if user else f"User ID: {v.user_id}"
            
            history_data.append({
                'id': v.id,
                'visit_datetime': v.visit_datetime.strftime('%d.%m.%Y %H:%M'),
                'purpose': v.visit_purpose.get_name(g.lang_code),
                'user': username,
                'is_operational': v.is_camera_operational,
                'battery_info': v.battery_type.get_name(g.lang_code) if v.battery_type else _('Не замінювались'),
                'sd_card_changed': v.sd_card_changed,
                'photos_on_card': v.photos_on_card,
                'comments': v.comments
            })
        
        return jsonify(history_data)
        
    except Exception as e:
        current_app.logger.error(f"Error fetching service history for location {location_id}: {e}", exc_info=True)
        return jsonify({'error': 'Failed to load history'}), 500
    finally:
        close_ct_session()

@camera_traps_bp.route('/api/service-log/create', methods=['POST'])
@login_required
@role_required('identifier')
def api_create_service_visit(lang_code):
    """API для створення нового запису в журналі обслуговування."""
    print("---!!! ЗАПИТ ДІЙШОВ ДО ФУНКЦІЇ api_create_service_visit !!!---")
    ct_session = get_ct_session()
    try:
        data = request.json
        
        # --- Валідація та отримання даних ---
        location_id = data.get('location_id')
        visit_purpose_id = data.get('visit_purpose_id')
        visit_datetime_str = data.get('visit_datetime')

        if not all([location_id, visit_purpose_id, visit_datetime_str]):
            return jsonify({'success': False, 'error': _('Не всі обов\'язкові поля заповнені.')}), 400

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

@camera_traps_bp.route('/api/run-stats-calculation', methods=['POST'])
@login_required
@role_required('moderator') # Доступ для модераторів та адмінів
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
        
        total_effort = calculate_total_effort(ct_session, start_date, end_date)
        raw_data = fetch_raw_daily_data(ct_session, start_date_str, end_date_str, species_ids)
        
        results = {}
        species_info = {}

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

            overlap_matrix = None
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
        
        # Отримуємо дані
        total_effort = calculate_total_effort(ct_session, start_date, end_date)
        raw_data = fetch_raw_daily_data(ct_session, start_date_str, end_date_str, species_ids)
        
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
        
        return render_template(
            'daily_activity.html',
            species_list=species_list,
            default_start=default_start,
            default_end=default_end
        )
    except Exception as e:
        current_app.logger.error(f"Error loading daily activity page: {e}", exc_info=True)
        flash(_("Помилка завантаження сторінки."), 'danger')
        return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))