# myproject/app/camera_traps/background_tasks.py

import os, shutil
from datetime import datetime, timedelta
from flask import current_app
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from .database import get_ct_session, close_ct_session
from .models import Photo, Identification, Observation, UploadBatch, Species

def cleanup_old_photos():
    """
    Знаходить завершені спостереження старше заданого періоду, які не є "вибраними".
    Видаляє їх raw + thumbnail файли та оновлює статус на 'archived'.
    Працює на рівні спостережень.

    Оптимізовано (2026-05-25): семантика не змінена, лише швидкість/надійність.
      1. selectinload(Observation.photos) — забирає photos одним додатковим
         запитом замість N+1 (1 на серії + N на photos).
      2. Chunked commit кожні CHUNK_OBS_SIZE серій — при збої втрачається
         максимум остання пачка, а не весь прогон.
      3. Файлові операції — ПІСЛЯ успішного commit пачки: якщо commit
         падає, файли цілі (повторити безпечно); якщо os.remove падає
         після успішного commit, файл стає orphan-сиротою (підбирається
         новим cleanup-модулем у cleanup.py).
      4. species_id=-2 — це категорія "Інше" в довіднику species
         (Identification без визначеного виду); такі серії свідомо НЕ
         архівуємо, бо потребують перегляду оператором.
    """
    ct_session = get_ct_session()

    try:
        config = current_app.config['CAMERA_TRAP_CONFIG']
        cleanup_days = config.get('CLEANUP_DAYS', 30)
        upload_path = config.get('UPLOAD_PATH')

        if not upload_path:
            current_app.logger.error("CAMERA_TRAP_CONFIG 'UPLOAD_PATH' is not defined. Aborting cleanup task.")
            return {'success': False, 'error': 'UPLOAD_PATH not defined'}

    except KeyError:
        current_app.logger.error("CAMERA_TRAP_CONFIG is not defined. Aborting cleanup task.")
        return {'success': False, 'error': 'CAMERA_TRAP_CONFIG not defined'}

    raw_dir = os.path.join(upload_path, 'pending_photos', 'raw')
    thumb_dir = os.path.join(upload_path, 'pending_photos', 'thumbnails')
    threshold_date = datetime.utcnow() - timedelta(days=cleanup_days)

    CHUNK_OBS_SIZE = 50  # ~ кілька сотень фото на пачку при середньому розмірі серії

    # Лічильники просуваються ЛИШЕ після successful commit пачки —
    # на випадок failure повертаємо чесну кількість успішно архівованих.
    archived_photos_count = 0
    archived_observations_count = 0

    try:
        # ОПТИМІЗАЦІЯ #1: selectinload — photos завантажуються одним
        # запитом `WHERE observation_id IN (...)`, а не за N+1 round-trip.
        # ОПТИМІЗАЦІЯ #4: species_id=-2 — категорія "Інше" в довіднику
        # species; такі серії свідомо НЕ архівуємо, потребують перегляду.
        old_observations = ct_session.query(Observation).join(
            Photo, Observation.id == Photo.observation_id
        ).join(
            Identification, Photo.id == Identification.photo_id
        ).options(
            selectinload(Observation.photos)
        ).filter(
            Observation.status == 'completed',
            ~Observation.photos.any(
                Photo.identifications.any(
                    Identification.species_id.in_([-2])
                )
            )
        ).group_by(
            Observation.id
        ).having(
            func.max(Identification.created_at) < threshold_date
        ).all()

        if not old_observations:
            current_app.logger.info("Background task: No completed observations found for archival.")
            return {
                'success': True,
                'photos_deleted': 0,
                'observations_archived': 0
            }

        current_app.logger.info(
            f"Background task: Found {len(old_observations)} observations to archive."
        )

        # Стан поточної пачки (між викликами flush_chunk).
        chunk_state = {
            'pending_photos': 0,
            'pending_obs': 0,
            'files': [],  # шляхи до видалення ПІСЛЯ commit-у пачки
        }

        def flush_chunk():
            """Commit поточну пачку → видалити файли пачки → reset."""
            nonlocal archived_photos_count, archived_observations_count
            if chunk_state['pending_photos'] == 0 and chunk_state['pending_obs'] == 0:
                return
            # ОПТИМІЗАЦІЯ #2: короткий commit пачки. Виняток далі —
            # обробить outer except, rollback скине лише поточну.
            ct_session.commit()
            # Promote лічильники після успішного commit.
            archived_photos_count += chunk_state['pending_photos']
            archived_observations_count += chunk_state['pending_obs']
            # ОПТИМІЗАЦІЯ #3: видалення файлів — ТІЛЬКИ після успішного
            # commit. Якщо commit упав — файли цілі, можна повторити.
            for p in chunk_state['files']:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError as e:
                    # Файл-сирота (БД сказала archived, файл лишився) —
                    # підбере cleanup.find_orphan_files_on_disk.
                    current_app.logger.warning(
                        f"Failed to remove archived file {p}: {e}"
                    )
            chunk_state['pending_photos'] = 0
            chunk_state['pending_obs'] = 0
            chunk_state['files'] = []

        for observation in old_observations:
            observation_has_favorites = False
            photos_archived_in_observation = 0

            try:
                for photo in observation.photos:
                    if photo.is_favorite:
                        observation_has_favorites = True
                        continue
                    if photo.status == 'completed':
                        # Спершу мітимо в ORM (закомітиться в flush_chunk).
                        photo.status = 'archived'
                        photos_archived_in_observation += 1
                        chunk_state['pending_photos'] += 1
                        # Файлові шляхи накопичуємо для post-commit видалення.
                        chunk_state['files'].append(
                            os.path.join(raw_dir, photo.system_filename))
                        chunk_state['files'].append(
                            os.path.join(thumb_dir, photo.system_filename))

                # Серія → archived лише якщо всі її фото архівовані/favorite.
                # Семантика повторює оригінал (включно з умовою-OR на рядку
                # `> 0 or not observation_has_favorites` — це покриває
                # випадок, коли всі фото вже були архівовані до цього прогону).
                if photos_archived_in_observation > 0 or not observation_has_favorites:
                    all_photos_archived = all(
                        photo.status == 'archived' or photo.is_favorite
                        for photo in observation.photos
                    )
                    if all_photos_archived:
                        observation.status = 'archived'
                        chunk_state['pending_obs'] += 1

                # Якщо пачка заповнена — комітимо й видаляємо файли.
                if chunk_state['pending_obs'] >= CHUNK_OBS_SIZE:
                    flush_chunk()

            except Exception as e:
                current_app.logger.error(
                    f"Error processing observation {observation.id} for archival: {e}"
                )
                continue

        # Фінальний commit того, що лишилось у поточній пачці.
        flush_chunk()

        current_app.logger.info(
            f"Successfully archived {archived_photos_count} photos "
            f"from {archived_observations_count} observations."
        )
        return {
            'success': True,
            'photos_deleted': archived_photos_count,
            'observations_archived': archived_observations_count
        }

    except Exception as e:
        current_app.logger.error(f"Error in cleanup_old_photos: {e}")
        ct_session.rollback()
        return {
            'success': False,
            'error': str(e),
            # Повертаємо ЧЕСНУ кількість успішно архівованих
            # (пачки, що пройшли commit до моменту збою).
            'photos_deleted': archived_photos_count,
            'observations_archived': archived_observations_count,
        }

    finally:
        close_ct_session()

# ─────────────────────────────────────────────────────────────────────────
# NOTE (2026-05-25): функцію cleanup_stale_batches() видалено.
# Її замінено модулем `cleanup.py` з повноцінним 3-категорійним dry-run,
# probe-захистом активних batchʼів і фоновим execute. Маршрут
# /admin/cleanup-batches теж видалено; нові маршрути:
# /admin/cleanup/{analyze, execute/<id>, task/<id>}.
# get_batch_statistics нижче залишена — використовується в інших місцях.
# ─────────────────────────────────────────────────────────────────────────

def get_batch_statistics():
    """
    Повертає статистику по батчах для моніторингу.
    """
    ct_session = get_ct_session()
    
    try:
        stats = {}
        
        # Кількість батчів по статусах
        batch_stats = ct_session.query(
            UploadBatch.status,
            func.count(UploadBatch.id).label('count')
        ).group_by(UploadBatch.status).all()
        
        stats['batches_by_status'] = {stat.status: stat.count for stat in batch_stats}
        
        # Кількість фото без спостережень
        orphaned_photos = ct_session.query(func.count(Photo.id)).filter(
            Photo.observation_id == None,
            Photo.status == 'uploaded'
        ).scalar()
        
        stats['orphaned_photos'] = orphaned_photos
        
        # Найстаріший незавершений батч
        oldest_batch = ct_session.query(UploadBatch).filter(
            UploadBatch.status.in_(['uploading', 'processing'])
        ).order_by(UploadBatch.created_at).first()
        
        if oldest_batch:
            stats['oldest_pending_batch'] = {
                'id': oldest_batch.id,
                'created_at': oldest_batch.created_at.isoformat(),
                'status': oldest_batch.status,
                'age_hours': (datetime.utcnow() - oldest_batch.created_at).total_seconds() / 3600
            }
        else:
            stats['oldest_pending_batch'] = None
            
        return stats
        
    except Exception as e:
        current_app.logger.error(f"Error getting batch statistics: {e}")
        return {}
        
    finally:
        close_ct_session()

def get_cleanup_statistics():
    """
    Повертає статистику файлів для видалення без фактичного видалення
    """
    ct_session = get_ct_session()
    
    try:
        config = current_app.config['CAMERA_TRAP_CONFIG']
        cleanup_days = config.get('CLEANUP_DAYS', 30)
        upload_path = config.get('UPLOAD_PATH')
        
        threshold_date = datetime.utcnow() - timedelta(days=cleanup_days)
        
        # Знаходимо спостереження для архівації (без змін у БД)
        old_observations = ct_session.query(Observation).filter(
            Observation.status == 'completed',
            Observation.series_end_time < threshold_date,
            ~Observation.photos.any(
                Photo.identifications.any(
                    Identification.species_id.in_([-2])
                )
            )
        ).all()
        
        total_photos = 0
        estimated_size = 0
        
        for observation in old_observations:
            for photo in observation.photos:
                if not photo.is_favorite and photo.status == 'completed':
                    total_photos += 1
                    # Примірний розрахунок розміру (2MB на фото)
                    estimated_size += 2
        
        return {
            'observations_count': len(old_observations),
            'photos_count': total_photos,
            'estimated_size_mb': estimated_size
        }
        
    except Exception as e:
        current_app.logger.error(f"Error getting cleanup statistics: {e}")
        raise
    finally:
        close_ct_session()

def delete_unfavorited_raw_files():
    """
    Знаходить ВСІ фотографії, які не позначені як "вибрані" (is_favorite = False),
    і видаляє їхні оригінальні raw-файли для економії місця.
    Ця дія НЕЗВОРОТНЯ.
    """
    ct_session = get_ct_session()
    
    deleted_count = 0
    skipped_count = 0
    
    try:
        config = current_app.config['CAMERA_TRAP_CONFIG']
        upload_path = config.get('UPLOAD_PATH')
        
        if not upload_path:
            current_app.logger.error("UPLOAD_PATH is not defined. Aborting deletion task.")
            return {'success': False, 'error': 'UPLOAD_PATH not defined'}

        raw_folder = os.path.join(upload_path, 'pending_photos', 'raw')

        # --- Крок 1: Знаходимо фото-кандидати в базі даних ---
        # Критерій: будь-яке фото, де is_favorite = False
        photos_to_delete_raw = ct_session.query(Photo).filter(
            Photo.is_favorite == False
        ).all()
        
        if not photos_to_delete_raw:
            current_app.logger.info("Deletion task: No unfavorited photos found to delete.")
            return {
                'success': True,
                'message': 'Фотографій для видалення не знайдено.',
                'deleted_count': 0
            }
        
        current_app.logger.info(f"Deletion task: Found {len(photos_to_delete_raw)} photos whose raw files will be deleted.")

        # --- Крок 2: Обробляємо кожного кандидата ---
        for photo in photos_to_delete_raw:
            try:
                raw_path = os.path.join(raw_folder, photo.system_filename)

                if os.path.exists(raw_path):
                    # --- Крок 3: Виконуємо видалення файлу ---
                    os.remove(raw_path)
                    deleted_count += 1
                    current_app.logger.debug(f"Successfully deleted raw file for photo {photo.id} (file: {photo.system_filename}).")
                else:
                    # Файлу вже немає, просто пропускаємо
                    skipped_count += 1
                    continue

            except Exception as e:
                current_app.logger.error(f"Error processing photo {photo.id} for deletion: {e}")
                continue
        
        # --- Фінальний результат ---
        final_message = (f"Видалення завершено. Видалено файлів: {deleted_count}. "
                         f"Пропущено (вже були відсутні): {skipped_count}.")
        current_app.logger.info(final_message)
        
        return {
            'success': True,
            'message': final_message,
            'deleted_count': deleted_count
        }

    except Exception as e:
        current_app.logger.error(f"Fatal error in delete_unfavorited_raw_files: {e}")
        return {'success': False, 'error': str(e)}
        
    finally:
        close_ct_session()