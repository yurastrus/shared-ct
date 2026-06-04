# myproject/app/camera_traps/utils.py

import os
import uuid
import hashlib
import calendar
import exifread
from datetime import datetime, timedelta, date as _date
from werkzeug.utils import secure_filename
from PIL import Image
from sqlalchemy import func, text

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


# Sanity-межі для EXIF-дат (Idea 1). Дата поза межами означає скинутий
# або збитий годинник камери → timestamp недостовірний → повертаємо None,
# і process_single_photo підставить помітний placeholder (1900-01-01 + offset)
# так само, як для фото зовсім без EXIF.
EXIF_MIN_VALID_DATE = datetime(2010, 1, 1)
EXIF_MAX_FUTURE_DRIFT = timedelta(hours=24)


def extract_datetime_from_exif(file_stream):
    """Зчитує дату та час з EXIF-даних файлу, включаючи долі секунди.

    Повертає None, якщо тегу немає, він не парситься АБО дата неправдоподібна
    (раніше EXIF_MIN_VALID_DATE чи далі ніж EXIF_MAX_FUTURE_DRIFT у майбутнє).
    """
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

            # Sanity-guard: збитий годинник камери (2000-й рік, майбутнє)
            if (dt_object < EXIF_MIN_VALID_DATE
                    or dt_object > datetime.now() + EXIF_MAX_FUTURE_DRIFT):
                current_app.logger.warning(
                    f"Implausible EXIF DateTimeOriginal {dt_object.isoformat()} "
                    f"(allowed {EXIF_MIN_VALID_DATE.date()} … now+"
                    f"{int(EXIF_MAX_FUTURE_DRIFT.total_seconds() // 3600)}h) — "
                    f"treating as missing"
                )
                return None

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

    Race-safety (виправлено 2026-05-24, після /upload-fast Beta):
      • processed_files оновлюється атомарним UPDATE ... RETURNING —
        замість read-modify-write, який втрачав інкременти при
        паралельних воркерах (фікс 0.4% «зниклих» лічильникових
        інкрементів на 900 фото).
      • Дублікат-перевірка обгорнута в pg_advisory_xact_lock на
        тріплеті (location_id, original_filename, captured_at) —
        дві паралельні спроби завантажити ТЕ Ж САМЕ фото тепер
        серіалізовані; різні фото обробляються паралельно як і раніше.
      • Файли, що були записані на диск перед невдалим commit, тепер
        видаляються в except — без сиріт у raw/ і thumbnails/.
    """
    ct_session = get_ct_session()

    # Шляхи — на верхньому рівні для cleanup в except.
    raw_path = None
    thumb_path = None

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

        # ─── АТОМАРНИЙ ІНКРЕМЕНТ processed_files ─────────────────────────
        # Замість ORM read-modify-write (race-prone з 4 паралельними
        # воркерами) — один SQL: UPDATE ... RETURNING. Транзакційно
        # ізольовано: якщо нижче впадемо й зробимо rollback, інкремент
        # відкотиться разом із усім іншим.
        # Бонус: повертає унікальний 1-based номер фото у межах batchʼа —
        # використовуємо як seconds-offset для placeholder-captured_at,
        # коли EXIF немає (раніше для всіх таких фото був той самий
        # offset → дублікат-конфлікти).
        new_count_row = ct_session.execute(
            text(
                "UPDATE upload_batches "
                "SET processed_files = COALESCE(processed_files, 0) + 1 "
                "WHERE id = :b "
                "RETURNING processed_files"
            ),
            {"b": batch_id},
        ).first()
        if new_count_row is None:
            raise ValueError(f"Batch {batch_id} not found")
        photo_offset = int(new_count_row[0])

        captured_at = extract_datetime_from_exif(file)

        if captured_at is None:
            placeholder_date = datetime(1900, 1, 1)
            captured_at = placeholder_date + timedelta(seconds=photo_offset)
            current_app.logger.warning(
                f"Could not read EXIF datetime for '{file.filename}'. "
                f"Falling back to placeholder time: {captured_at}"
            )

        # 1. Формування оригінального імені файлу
        original_filename = secure_filename(file.filename)
        if not original_filename:
            original_filename = f"photo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"

        # ─── ADVISORY LOCK на (location, filename, time) ─────────────────
        # Серіалізуємо обробку фото з однаковим ключем дублікату
        # (lock-key — той самий, що перевіряє preflight нижче).
        # Два паралельні воркери з тим самим (location, filename,
        # captured_at) тепер чекатимуть один одного → перший INSERT-ить,
        # другий бачить дублікат і чесно повертає ValueError.
        # Різні (location, filename, captured_at) → різні lock-keys →
        # ніякого блокування, паралелізм збережено.
        # На SQLite функції pg_advisory_xact_lock немає — у тестах
        # просто пропускаємо (юніт-тести однопотокові, race немає).
        _key_src = (
            f"{location.id}|{original_filename}|{captured_at.isoformat()}"
        )
        _h = hashlib.md5(_key_src.encode('utf-8')).digest()
        try:
            ct_session.execute(
                text("SELECT pg_advisory_xact_lock(:k1, :k2)"),
                {
                    "k1": int.from_bytes(_h[0:4], 'big', signed=True),
                    "k2": int.from_bytes(_h[4:8], 'big', signed=True),
                },
            )
        except Exception:
            # SQLite або інший двіжок без advisory-locks — race лишається,
            # але в тестах він не відтворюється (один потік).
            pass

        # 2. Перевірка на дублікат — тепер race-safe всередині lock
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

        # === НАДІЙНИЙ БЛОК ПЕРЕВІРКИ УНІКАЛЬНОСТІ ІМЕНІ ===
        # У межах advisory-lock конкурент із тим самим ключем тут не зайде —
        # тож counter гарантовано не зіткнеться.
        counter = 1
        while True:
            system_filename = f"{base_name}_{counter:02d}{ext}"
            raw_path = os.path.join(raw_folder, system_filename)
            thumb_path = os.path.join(thumb_folder, system_filename)

            disk_exists = os.path.exists(raw_path) or os.path.exists(thumb_path)

            if not disk_exists:
                db_exists = ct_session.query(Photo.id).filter_by(system_filename=system_filename).first()
                if not db_exists:
                    break

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
                with Image.open(file) as img:
                    target_size = config['THUMBNAIL_SIZE']
                    is_correct_res = img.width <= target_size[0] and img.height <= target_size[1]
                    is_jpeg = img.format == 'JPEG'

                file.seek(0)

                if is_correct_res and is_jpeg:
                    file.save(thumb_path)
                else:
                    create_thumbnail(file, thumb_path)
                    current_app.logger.warning(f"Файл {system_filename} був стиснутий сервером (клієнт надіслав невідповідний файл)")

            except Exception as e:
                current_app.logger.error(f"Помилка перевірки зображення {original_filename}: {e}")
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

        # processed_files уже атомарно інкрементовано на початку функції —
        # повторно НЕ оновлюємо (раніше тут був ще один read-modify-write).

        ct_session.commit()

        return photo.id

    except Exception as e:
        current_app.logger.error(f"Error processing file {file.filename}: {e}")
        ct_session.rollback()
        # Cleanup файлів на диску, якщо commit не пройшов — щоб не лишати
        # сирітські JPEG-и в raw/ і thumbnails/.
        for _p in (raw_path, thumb_path):
            if _p:
                try:
                    if os.path.exists(_p):
                        os.remove(_p)
                except Exception:
                    pass
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
                mark_observation_complete(observation_id, db_session=db_session,
                                          winner_species_id=winner_species)
                current_app.logger.info(f"Completed observation {observation_id}")
                    
    except Exception as e:
        current_app.logger.error(f"Error in check_consensus_for_observation {observation_id}: {e}")
        raise

def mark_observation_complete(observation_id, db_session, winner_species_id=None):
    """Позначає спостереження як завершене.

    Якщо передано winner_species_id (консенсусний вид) — фіксує
    правильність AI-прогнозів для цієї серії (Idea 4):
      was_correct = (prediction_species_id == winner_species_id),
      None — якщо AI не визначив вид (prediction_species_id IS NULL).
    """
    try:
        # ЗМІНЕНО: використовуємо передану сесію 'db_session'
        observation = db_session.query(Observation).get(observation_id)
        if observation:
            for photo in observation.photos:
                photo.status = 'completed'
            observation.status = 'completed'

            # Фіксуємо правильність AI на момент консенсусу (Idea 4).
            # Обгорнуто окремо: на інсталяціях без AI-схеми (ai_predictions
            # не існує) це не повинно зривати сам консенсус.
            if winner_species_id is not None:
                try:
                    from .models import AIPrediction
                    preds = db_session.query(AIPrediction).filter(
                        AIPrediction.observation_id == observation_id
                    ).all()
                    for pred in preds:
                        if pred.prediction_species_id is None:
                            pred.was_correct = None  # AI не визначив вид → невизначено
                        else:
                            pred.was_correct = (
                                pred.prediction_species_id == winner_species_id
                            )
                except Exception as ai_exc:
                    current_app.logger.warning(
                        f"was_correct skip for obs {observation_id}: {ai_exc}"
                    )

            current_app.logger.info(f"Observation {observation_id} marked as complete")
    except Exception as e:
        current_app.logger.error(f"Error marking observation complete: {e}")
        raise # Повторно генеруємо виняток

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
            mark_observation_complete(obs_id, db_session=ct_session,
                                      winner_species_id=species_id)
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

def calculate_total_effort(session, start_date, end_date, location_ids=None):
    """
    Розраховує загальну кількість трап-днів (сума активних днів по всіх локаціях)
    за вказаний період.

    Args:
        location_ids: опційний список Location.id для обмеження ефорту
                      конкретними локаціями (для фільтрації по установі/екорегіону).
                      None означає "всі локації" (старе дефолтне поведінка).

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
     )

    if location_ids is not None:
        dates_query = dates_query.filter(Location.id.in_(location_ids))

    dates_query = dates_query.distinct().order_by(Location.id, 'cap_date').all()

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




# ── Календар покриття фотопасток (#38) ───────────────────────────────────────
#
# Покриття дня = «камера працювала». Фото у CT тригерні, тож «дні з фото»
# занижують покриття. Тому covered_days = об'єднання deployment-інтервалів
# (start..end) ∪ gap-filled дні з фото (для локацій/деплойментів без дат).
# Інтенсивність клітинки (під градацію #43) — к-сть фото за день.


def fill_day_gaps(days, max_gap_days):
    """Повертає set дат: вхідні дні + заповнені прогалини між сусідніми
    датами, якщо прогалина <= max_gap_days (камера стояла, тварини не йшли).

    days: iterable[date]; max_gap_days: int (0 = без заповнення).
    """
    days = sorted({d for d in days if d is not None})
    if not days:
        return set()
    out = set(days)
    if max_gap_days and max_gap_days > 0:
        for a, b in zip(days, days[1:]):
            gap = (b - a).days
            if 1 < gap <= max_gap_days + 1:
                for k in range(1, gap):
                    out.add(a + timedelta(days=k))
    return out


def build_ct_coverage_calendar(covered_days, photo_counts, good_photos=1):
    """Помісячний календар покриття фотопасток. Чиста функція (без БД).

    covered_days: set[date] — дні, коли камера працювала.
    photo_counts: {date: int} — к-сть фото за день (для інтенсивності/градації).
    good_photos: поріг «добре» у фото/день.

    Повертає dict як PAM build_coverage_calendar:
        months[], total_photos, active_camera_days, days_with_photos, day_range.
    cell = {day, date, covered(bool), photos(int), level: good|partial|missing}
      level: missing — камера не працювала; partial — працювала, 0..<good фото;
             good — працювала і >= good_photos фото.
    """
    covered_days = {d for d in (covered_days or set()) if d is not None}
    photo_counts = {d: c for d, c in (photo_counts or {}).items() if d is not None}

    all_days = covered_days | set(photo_counts)
    if not all_days:
        return {'months': [], 'total_photos': 0, 'active_camera_days': 0,
                'days_with_photos': 0, 'day_range': None}

    first, last = min(all_days), max(all_days)
    total_photos = sum(photo_counts.values())

    cal = calendar.Calendar(firstweekday=0)
    months = []
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        weeks = []
        for week in cal.monthdatescalendar(y, m):
            row = []
            for d in week:
                if d.month != m:
                    row.append(None)
                    continue
                covered = d in covered_days
                photos = photo_counts.get(d, 0)
                if not covered:
                    level = 'missing'
                elif photos >= good_photos:
                    level = 'good'
                else:
                    level = 'partial'
                row.append({'day': d.day, 'date': d, 'covered': covered,
                            'photos': photos, 'level': level})
            weeks.append(row)
        months.append({'year': y, 'month': m, 'label': f'{y}-{m:02d}', 'weeks': weeks})
        m += 1
        if m > 12:
            m, y = 1, y + 1

    return {
        'months': months,
        'total_photos': total_photos,
        'active_camera_days': len(covered_days),
        'days_with_photos': len(photo_counts),
        'day_range': (first, last),
    }
