# myproject/app/camera_traps/utils.py

import os
import uuid
import exifread
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from PIL import Image
from sqlalchemy import func

from flask import current_app
from .database import get_ct_session, close_ct_session
from .models import Location, Observation, Photo, UploadBatch, Identification

def get_institution_filter(user_inst_ids=None, is_admin=False, selected_inst_id=None, table_alias='l'):
    """
    Генерує SQL-умову для фільтрації за правами доступу ТА вибраними установами.
    """
    prefix = f"{table_alias}." if table_alias else ""
    
    if is_admin:
        base_condition = "1=1"
        params = {}
    elif not user_inst_ids:
        base_condition = f"{prefix}visibility_level = 0"
        params = {}
    else:
        base_condition = f"""
            ({prefix}visibility_level = 0 OR EXISTS (
                SELECT 1 FROM location_institutions li_perm 
                WHERE li_perm.location_id = {prefix}id 
                AND li_perm.institution_id = ANY(:user_inst_ids)
            ))
        """
        params = {"user_inst_ids": user_inst_ids}

    if selected_inst_id:
        if isinstance(selected_inst_id, str):
            ids =[int(i) for i in selected_inst_id.split(',') if i.strip().isdigit()]
        elif isinstance(selected_inst_id, (int, float)):
            ids = [int(selected_inst_id)]
        else:
            ids =[int(i) for i in selected_inst_id if str(i).isdigit()]

        if ids:
            base_condition += f"""
                AND EXISTS (
                    SELECT 1 FROM location_institutions li_sel 
                    WHERE li_sel.location_id = {prefix}id 
                    AND li_sel.institution_id = ANY(:selected_inst_id)
                )
            """
            params['selected_inst_id'] = ids

    return base_condition, params


def extract_datetime_from_exif(file_stream):
    """Зчитує дату та час з EXIF-даних файлу, включаючи долі секунди."""
    try:
        file_stream.seek(0)
        # Зчитуємо всі необхідні теги за один раз
        tags = exifread.process_file(file_stream, details=False, stop_tag='EXIF SubSecTimeOriginal')
        
        if 'EXIF DateTimeOriginal' in tags:
            date_str = str(tags['EXIF DateTimeOriginal'])
            # Спершу парсимо основний час (до секунд)
            dt_object = datetime.strptime(date_str, '%Y:%m:%d %H:%M:%S')

            # Перевіряємо, чи є тег для долей секунди
            if 'EXIF SubSecTimeOriginal' in tags:
                subsec_str = str(tags['EXIF SubSecTimeOriginal']).strip()
                if subsec_str.isdigit():
                    # Значення SubSecTimeOriginal - це долі секунди. 
                    # Наприклад, '123' означає 123 мілісекунди.
                    # Ми повинні перетворити це в мікросекунди для об'єкта timedelta.
                    # Довжина рядка важлива: '5' -> 0.5с, '50' -> 0.50с, '500' -> 0.500с.
                    # Ми нормалізуємо це до 6 знаків (мікросекунди), доповнюючи нулями справа.
                    subsec_normalized = subsec_str.ljust(6, '0')
                    microseconds = int(subsec_normalized)
                    
                    # Додаємо мікросекунди до нашого об'єкта datetime
                    dt_object += timedelta(microseconds=microseconds)
            
            return dt_object

    except Exception as e:
        current_app.logger.error(f"Could not read EXIF data with subseconds: {e}")
    
    return None

def create_thumbnail(source, thumbnail_path):
    """Створює мініатюру для зображення. source може бути шляхом або файловим потоком."""
    try:
        size = current_app.config['CAMERA_TRAP_CONFIG']['THUMBNAIL_SIZE']
        with Image.open(source) as img:
            img.thumbnail(size)
            img.save(thumbnail_path, "JPEG", quality=85)
    except Exception as e:
        # Використовуємо .name атрибут, якщо source - це потік, інакше просто source
        source_name = getattr(source, 'name', source)
        current_app.logger.error(f"Failed to create thumbnail for {source_name}: {e}")

def create_upload_batch(location_id, user_id, total_files=None):
    """Створює новий batch для завантаження файлів."""
    ct_session = get_ct_session()
    
    try:
        batch_id = str(uuid.uuid4())
        
        batch = UploadBatch(
            id=batch_id,
            location_id=location_id,
            uploaded_by_id=user_id,
            status='uploading',
            total_files=total_files or 0
        )
        
        ct_session.add(batch)
        ct_session.commit()
        
        current_app.logger.info(f"Created upload batch {batch_id} for user {user_id}")
        return batch_id
        
    except Exception as e:
        current_app.logger.error(f"Error creating upload batch: {e}")
        ct_session.rollback()
        raise
    finally:
        close_ct_session()

def process_single_photo(file, location_id, user_id, batch_id, save_original=True):
    """
    Обробляє один файл і зберігає його в статусі 'uploaded'.
    Групування в серії відбудеться пізніше.
    """
    ct_session = get_ct_session()
    
    try:
        config = current_app.config['CAMERA_TRAP_CONFIG']
        location = ct_session.query(Location).get(location_id)
        if not location:
            raise ValueError("Invalid Location ID")

        # Перевіряємо, що папки існують
        raw_folder = os.path.join(config['UPLOAD_PATH'], 'pending_photos', 'raw')
        thumb_folder = os.path.join(config['UPLOAD_PATH'], 'pending_photos', 'thumbnails')
        
        os.makedirs(raw_folder, exist_ok=True)
        os.makedirs(thumb_folder, exist_ok=True)

        if not file or not file.filename:
            raise ValueError("Empty file")
            
        captured_at = extract_datetime_from_exif(file)

        if captured_at is None:
            placeholder_date = datetime(1900, 1, 1)
            batch = ct_session.query(UploadBatch).get(batch_id)
            seconds_offset = batch.processed_files or 0 if batch else 0
            captured_at = placeholder_date + timedelta(seconds=seconds_offset)

            current_app.logger.warning(
                f"Could not read EXIF datetime for '{file.filename}'. "
                f"Falling back to placeholder time: {captured_at}"
            )

        # 1. Формування оригінального імені файлу
        original_filename = secure_filename(file.filename)
        if not original_filename:
            original_filename = f"photo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        
        # 2. Покращена перевірка на дублікат
        existing_photo = ct_session.query(Photo).join(Observation).filter(
            Observation.location_id == location.id,
            Photo.captured_at == captured_at,
            Photo.original_filename == original_filename
        ).first()
        
        if not existing_photo:
            existing_photo = ct_session.query(Photo).join(UploadBatch).filter(
                UploadBatch.location_id == location.id,
                Photo.captured_at == captured_at,
                Photo.original_filename == original_filename
            ).first()

        if existing_photo:
            raise ValueError(
                f"Duplicate photo detected. File '{original_filename}' for location '{location.name}' "
                f"at {captured_at} already exists."
            )

        # 3. Формування системного імені файлу
        lat_str = str(location.latitude).replace('.', '_')
        lon_str = str(location.longitude).replace('.', '_')
        timestamp_str = captured_at.strftime('%Y%m%d_%H%M%S_%f')
        
        base_name = f"{lat_str}_{lon_str}_{timestamp_str}_{batch_id[:8]}"
        ext = os.path.splitext(original_filename)[1] or '.jpg'
        
        # === НОВИЙ НАДІЙНИЙ БЛОК ПЕРЕВІРКИ УНІКАЛЬНОСТІ ІМЕНІ ===
        counter = 1
        while True:
            system_filename = f"{base_name}_{counter:02d}{ext}"
            raw_path = os.path.join(raw_folder, system_filename)
            thumb_path = os.path.join(thumb_folder, system_filename)
            
            # Перевіряємо чи є файл на диску (в сирих АБО в мініатюрах)
            disk_exists = os.path.exists(raw_path) or os.path.exists(thumb_path)
            
            if not disk_exists:
                # Перевіряємо чи немає такого системного імені вже в базі даних!
                db_exists = ct_session.query(Photo.id).filter_by(system_filename=system_filename).first()
                if not db_exists:
                    break # Знайшли абсолютно унікальне ім'я!
            
            counter += 1
        # ========================================================

        # Збереження файлів
        file.seek(0)
    
        file.seek(0)

        if save_original:
            # Сценарій 1: Користувач явно хоче зберегти оригінал (галочка стоїть)
            file.save(raw_path)
            # Створюємо мініатюру з уже збереженого локального файлу (це швидше)
            create_thumbnail(raw_path, thumb_path)
        else:
            # Сценарій 2: Очікуємо, що браузер уже стиснув файл (галочка не стоїть)
            try:
                # Відкриваємо зображення для перевірки параметрів
                with Image.open(file) as img:
                    target_size = config['THUMBNAIL_SIZE']
                    
                    # Перевіряємо, чи роздільна здатність вже відповідає нормі
                    # (Pillow повертає (width, height))
                    is_correct_res = img.width <= target_size[0] and img.height <= target_size[1]
                    
                    # Додаткова перевірка формату (на випадок, якщо прийшов не JPEG)
                    is_jpeg = img.format == 'JPEG'

                file.seek(0) # Повертаємо покажчик після відкриття Image.open

                if is_correct_res and is_jpeg:
                    # Файл уже ідеальний (стиснутий браузером): просто зберігаємо
                    file.save(thumb_path)
                    # current_app.logger.info(f"Файл {system_filename} збережено як є (вже стиснутий)")
                else:
                    # Файл не відповідає критеріям (завеликий або не той формат):
                    # стискаємо його на сервері
                    create_thumbnail(file, thumb_path)
                    current_app.logger.warning(f"Файл {system_filename} був стиснутий сервером (клієнт надіслав невідповідний файл)")

            except Exception as e:
                # Якщо файл пошкоджений або Image.open не зміг його прочитати
                current_app.logger.error(f"Помилка перевірки зображення {original_filename}: {e}")
                # Спробуємо останній шанс - метод create_thumbnail, 
                # якщо і він впаде, спрацює загальний try/except функції
                file.seek(0)
                create_thumbnail(file, thumb_path)

        # Створення запису в БД
        photo = Photo(
            upload_batch_id=batch_id,
            original_filename=original_filename,
            system_filename=system_filename,
            captured_at=captured_at,
            status='uploaded'
        )
        ct_session.add(photo)
        
        batch = ct_session.query(UploadBatch).get(batch_id)
        if batch:
            batch.processed_files = (batch.processed_files or 0) + 1
        
        ct_session.commit()
        
        return photo.id
        
    except Exception as e:
        current_app.logger.error(f"Error processing file {file.filename}: {e}")
        ct_session.rollback()
        raise ValueError(f"Failed to process file {file.filename}: {str(e)}")
    finally:
        close_ct_session()

def group_batch_into_series(batch_id):
    """
    Групує всі фото з батча в серії спостережень.
    Викликається після завантаження всіх файлів батча.
    """
    ct_session = get_ct_session()
    
    try:
        config = current_app.config['CAMERA_TRAP_CONFIG']
        
        # Отримуємо batch
        batch = ct_session.query(UploadBatch).get(batch_id)
        if not batch:
            raise ValueError(f"Batch {batch_id} not found")
        
        if batch.status != 'uploading':
            raise ValueError(f"Batch {batch_id} is not in uploading status")
        
        batch.status = 'processing'
        ct_session.flush()
        
        # Отримуємо всі фото з батча, сортовані за часом
        photos = ct_session.query(Photo).filter(
            Photo.upload_batch_id == batch_id,
            Photo.status == 'uploaded'
        ).order_by(Photo.captured_at).all()
        
        if not photos:
            raise ValueError(f"No uploaded photos found in batch {batch_id}")
        
        current_observation = None
        series_window = timedelta(seconds=config['SERIES_TIME_WINDOW'])
        
        for photo in photos:
            try:
                # Перевіряємо, чи можемо додати до поточної серії
                if (current_observation and 
                    current_observation.location_id == batch.location_id and
                    photo.captured_at <= current_observation.series_end_time + series_window):
                    
                    # Додаємо до поточної серії
                    current_observation.series_end_time = photo.captured_at
                    current_observation.photo_count = (current_observation.photo_count or 0) + 1
                    
                else:
                    # Створюємо нову серію
                    current_observation = Observation(
                        location_id=batch.location_id,
                        series_start_time=photo.captured_at,
                        series_end_time=photo.captured_at,
                        uploaded_by_id=batch.uploaded_by_id,
                        photo_count=1,
                        status='pending'
                    )
                    ct_session.add(current_observation)
                    ct_session.flush()
                
                # Прив'язуємо фото до спостереження
                photo.observation_id = current_observation.id
                photo.status = 'pending'
                
                # Визначаємо sequence_number в межах спостереження
                photo.sequence_number = current_observation.photo_count
                
            except Exception as e:
                current_app.logger.error(f"Error processing photo {photo.id} in batch {batch_id}: {e}")
                continue
             
        # Оновлюємо лічильники локації
        location = ct_session.query(Location).get(batch.location_id)
        if location:
            location.photo_count = ct_session.query(Photo).join(Observation).filter(
                Observation.location_id == location.id,
                Photo.status.in_(['pending', 'completed', 'needs_review'])
            ).count()
        
        # Завершуємо батч
        batch.status = 'completed'
        batch.completed_at = datetime.utcnow()
        
        ct_session.commit()
        
        grouped_photos = len([p for p in photos if p.observation_id])
        current_app.logger.info(
            f"Successfully grouped {grouped_photos} photos from batch {batch_id} "
            f"for location {batch.location.name}"
        )
        
        return grouped_photos
        
    except Exception as e:
        current_app.logger.error(f"Error grouping batch {batch_id} into series: {e}")
        batch.status = 'failed'
        batch.error_message = str(e)
        ct_session.rollback()
        raise
    finally:
        close_ct_session()

def get_batch_status(batch_id):
    """Повертає статус батча."""
    ct_session = get_ct_session()
    
    try:
        batch = ct_session.query(UploadBatch).get(batch_id)
        if not batch:
            return None
            
        return {
            'id': batch.id,
            'status': batch.status,
            'total_files': batch.total_files,
            'processed_files': batch.processed_files,
            'created_at': batch.created_at.isoformat(),
            'completed_at': batch.completed_at.isoformat() if batch.completed_at else None,
            'error_message': batch.error_message
        }
        
    except Exception as e:
        current_app.logger.error(f"Error getting batch status {batch_id}: {e}")
        return None
    finally:
        close_ct_session()

def process_photo_batch(files, location_id, user):
    """
    DEPRECATED: Використовуйте process_single_photo + group_batch_into_series
    """
    current_app.logger.warning(
        "process_photo_batch is deprecated. Use process_single_photo + group_batch_into_series instead."
    )
    
    # Створюємо батч
    batch_id = create_upload_batch(location_id, user.id, len(files))
    
    try:
        # Обробляємо файли по одному
        for file in files:
            process_single_photo(file, location_id, user.id, batch_id)
        
        # Групуємо в серії
        group_batch_into_series(batch_id)
        
    except Exception as e:
        current_app.logger.error(f"Error in deprecated process_photo_batch: {e}")
        raise

def check_consensus_for_observation(observation_id, db_session, moderator_override=False):
    try:
        observation = db_session.query(Observation).get(observation_id)
        if not observation:
            return

        if moderator_override and observation.status == 'completed':
            observation.status = 'pending'
            for photo in observation.photos:
                photo.status = 'pending'
            db_session.flush() 
            return

        if observation.status != 'pending':
            return

        config = current_app.config.get('CAMERA_TRAP_CONFIG', {})
        min_identifications = config.get('MIN_IDENTIFICATIONS', 3)
        
        user_identifications = db_session.query(
            Identification.user_id,
            Identification.species_id
        ).join(Photo).filter(
            Photo.observation_id == observation_id
        ).distinct().all()

        # ТІЛЬКИ це логування залиште
        if len(user_identifications) >= min_identifications:
            current_app.logger.info(f"Processing observation {observation_id}: {len(user_identifications)} identifications")
        
        if len(user_identifications) < min_identifications:
            return

        votes = {}
        for user_id, species_id in user_identifications:
            votes[species_id] = votes.get(species_id, 0) + 1
        
        if votes:
            winner_species, winner_votes = max(votes.items(), key=lambda x: x[1])
            vote_percentage = winner_votes / len(user_identifications)
            
            if vote_percentage > 0.5:
                mark_observation_complete(observation_id, db_session=db_session)
                current_app.logger.info(f"Completed observation {observation_id}")
                    
    except Exception as e:
        current_app.logger.error(f"Error in check_consensus_for_observation {observation_id}: {e}")
        raise

def mark_observation_complete(observation_id, db_session):
    """Позначає спостереження як завершене"""
    # ВИДАЛЕНО: ct_session = get_ct_session()
    try:
        # ЗМІНЕНО: використовуємо передану сесію 'db_session'
        observation = db_session.query(Observation).get(observation_id)
        if observation:
            for photo in observation.photos:
                photo.status = 'completed'
            observation.status = 'completed'
            # ВИДАЛЕНО: ct_session.commit()
            current_app.logger.info(f"Observation {observation_id} marked as complete")
    except Exception as e:
        current_app.logger.error(f"Error marking observation complete: {e}")
        # ВИДАЛЕНО: ct_session.rollback()
        raise # Повторно генеруємо виняток
    # ВИДАЛЕНО: finally: close_ct_session()

def migrate_pending_observations_to_single_identification():
    """
    Оптимізована версія - один запит замість сотень
    """
    ct_session = get_ct_session()
    
    try:
        config = current_app.config.get('CAMERA_TRAP_CONFIG', {})
        min_identifications = config.get('MIN_IDENTIFICATIONS', 3)
        
        # ОДИН запит для всіх pending спостережень з достатньою кількістю ідентифікацій
        subquery = ct_session.query(
            Photo.observation_id,
            func.count(func.distinct(Identification.user_id)).label('user_count'),
            Identification.species_id,
            func.count(Identification.species_id).label('species_votes')
        ).join(Identification)\
        .filter(
            Photo.observation_id.in_(
                ct_session.query(Observation.id).filter(Observation.status == 'pending')
            )
        )\
        .group_by(Photo.observation_id, Identification.species_id)\
        .subquery()
        
        # Знаходимо спостереження з консенсусом
        consensus_observations = ct_session.query(
            subquery.c.observation_id,
            subquery.c.species_id,
            subquery.c.species_votes,
            subquery.c.user_count
        ).filter(
            subquery.c.user_count >= min_identifications,
            subquery.c.species_votes > (subquery.c.user_count / 2)
        ).all()
        
        completed_count = 0
        for obs_id, species_id, votes, total_users in consensus_observations:
            mark_observation_complete(obs_id, db_session=ct_session)
            completed_count += 1
            current_app.logger.info(f"Completed observation {obs_id}: {votes}/{total_users} votes for species {species_id}")
        
        ct_session.commit()
        return completed_count
        
    except Exception as e:
        ct_session.rollback()
        current_app.logger.error(f"Error in optimized consensus migration: {e}")
        raise
    finally:
        close_ct_session()

def calculate_total_effort(session, start_date, end_date):
    """
    Розраховує загальну кількість трап-днів (сума активних днів по всіх локаціях)
    за вказаний період.
    
    Логіка базується на наявності фотографій. Якщо розрив між фото менше MAX_GAP_DAYS,
    період вважається активним.
    """
    # Отримуємо дати активності для всіх локацій, де були спостереження
    dates_query = session.query(
        Location.id,
        func.date(Photo.captured_at).label('cap_date')
    ).join(Observation, Location.id == Observation.location_id)\
     .join(Photo, Observation.id == Photo.observation_id)\
     .filter(
         Photo.captured_at.between(start_date, end_date)
     ).distinct().order_by(Location.id, 'cap_date').all()

    loc_dates = {}
    for row in dates_query:
        lid = row.id
        if lid not in loc_dates: 
            loc_dates[lid] = []
        loc_dates[lid].append(row.cap_date)

    MAX_GAP_DAYS = 15
    total_effort_days = 0
    
    for lid, dates in loc_dates.items():
        if not dates: 
            continue
            
        effort_days = 0
        if len(dates) == 1:
            effort_days = 1
        else:
            # Рахуємо дні між першим і останнім фото, враховуючи розриви
            calculated_days = 1
            prev_date = dates[0]
            for i in range(1, len(dates)):
                curr_date = dates[i]
                diff = (curr_date - prev_date).days
                
                # Якщо розрив невеликий, додаємо всі дні проміжку
                if diff <= MAX_GAP_DAYS:
                    calculated_days += diff
                else:
                    # Якщо розрив великий, вважаємо камеру неактивною в цей час
                    # Додаємо лише 1 день за факт фотографії
                    calculated_days += 1
                prev_date = curr_date
            effort_days = calculated_days
            
        total_effort_days += effort_days

    return total_effort_days


