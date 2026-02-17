# myproject/app/camera_traps/background_tasks.py

import os, shutil
from datetime import datetime, timedelta
from flask import current_app
from sqlalchemy import func

from .database import get_ct_session, close_ct_session
from .models import Photo, Identification, Observation, UploadBatch, Species

def cleanup_old_photos():
    """
    Знаходить завершені спостереження старше заданого періоду, які не є "вибраними".
    Видаляє їх оригінальні файли та оновлює статус на 'archived'.
    Тепер працює на рівні спостережень, а не окремих фото.
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

    try:
        # Визначаємо порогову дату
        threshold_date = datetime.utcnow() - timedelta(days=cleanup_days)

        # Знаходимо завершені спостереження старше порогової дати
        old_observations = ct_session.query(Observation).join(
            Photo, Observation.id == Photo.observation_id
        ).join(
            Identification, Photo.id == Identification.photo_id
        ).filter(
            Observation.status == 'completed',
            # Цей фільтр залишається, але його треба застосувати трохи інакше
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

        current_app.logger.info(f"Background task: Found {len(old_observations)} observations to archive.")

        archived_photos_count = 0
        archived_observations_count = 0
        
        for observation in old_observations:
            observation_has_favorites = False
            photos_archived_in_observation = 0
            
            try:
                # Перевіряємо та архівуємо фото в спостереженні
                for photo in observation.photos:
                    if photo.is_favorite:
                        observation_has_favorites = True
                        continue
                        
                    if photo.status == 'completed':
                        try:
                            # Видаляємо файли
                            raw_path = os.path.join(upload_path, 'pending_photos', 'raw', photo.system_filename)
                            thumb_path = os.path.join(upload_path, 'pending_photos', 'thumbnails', photo.system_filename)

                            if os.path.exists(raw_path):
                                os.remove(raw_path)
                                current_app.logger.debug(f"Deleted raw file: {raw_path}")
                            
                            if os.path.exists(thumb_path):
                                os.remove(thumb_path)
                                current_app.logger.debug(f"Deleted thumbnail: {thumb_path}")

                            photo.status = 'archived'
                            photos_archived_in_observation += 1
                            archived_photos_count += 1
                            
                        except Exception as e:
                            current_app.logger.error(f"Error archiving photo {photo.id}: {e}")
                            continue

                # Якщо всі фото в спостереженні архівовані (або є вибраними), 
                # позначаємо спостереження як архівоване
                if photos_archived_in_observation > 0 or not observation_has_favorites:
                    all_photos_archived = all(
                        photo.status == 'archived' or photo.is_favorite 
                        for photo in observation.photos
                    )
                    
                    if all_photos_archived:
                        observation.status = 'archived'
                        archived_observations_count += 1
                        current_app.logger.info(f"Archived observation {observation.id} with {photos_archived_in_observation} photos")
                
            except Exception as e:
                current_app.logger.error(f"Error processing observation {observation.id} for archival: {e}")
                continue

        if archived_photos_count > 0:
            try:
                ct_session.commit()
                current_app.logger.info(
                    f"Successfully archived {archived_photos_count} photos "
                    f"from {archived_observations_count} observations."
                )
                # Повертаємо статистику
                return {
                    'success': True,
                    'photos_deleted': archived_photos_count,
                    'observations_archived': archived_observations_count
                }
            except Exception as e:
                ct_session.rollback()
                current_app.logger.error(f"Failed to commit archival changes to the database: {e}")
                return {'success': False, 'error': str(e)}
        else:
            current_app.logger.info("Background task: No photos were archived.")
            return {
                'success': True,
                'photos_deleted': 0,
                'observations_archived': 0
            }
                
    except Exception as e:
        current_app.logger.error(f"Error in cleanup_old_photos: {e}")
        ct_session.rollback()
        return {'success': False, 'error': str(e)}
        
    finally:
        close_ct_session()

def cleanup_stale_batches():
    """
    Очищає "застрялі" батчі, які довго знаходяться в статусі 'uploading'.
    Також видаляє файли з незавершених батчів.
    """
    ct_session = get_ct_session()
    
    try:
        config = current_app.config['CAMERA_TRAP_CONFIG']
        stale_batch_hours = config.get('STALE_BATCH_HOURS', 24)  # 24 години за замовчуванням
        upload_path = config.get('UPLOAD_PATH')
        
        if not upload_path:
            current_app.logger.error("CAMERA_TRAP_CONFIG 'UPLOAD_PATH' is not defined. Aborting batch cleanup.")
            return

        # Знаходимо застрялі батчі
        threshold_time = datetime.utcnow() - timedelta(hours=stale_batch_hours)
        
        stale_batches = ct_session.query(UploadBatch).filter(
            UploadBatch.status.in_(['uploading', 'processing']),
            UploadBatch.created_at < threshold_time
        ).all()

        if not stale_batches:
            current_app.logger.info("Batch cleanup: No stale batches found.")
            return

        current_app.logger.info(f"Batch cleanup: Found {len(stale_batches)} stale batches.")

        cleaned_batches = 0
        for batch in stale_batches:
            try:
                # Знаходимо всі фото цього батча
                photos_to_clean = ct_session.query(Photo).filter(
                    Photo.upload_batch_id == batch.id,
                    Photo.status == 'uploaded'  # Тільки негруповані фото
                ).all()

                # Видаляємо файли
                raw_dir = os.path.join(upload_path, 'pending_photos', 'raw')
                thumb_dir = os.path.join(upload_path, 'pending_photos', 'thumbnails')
                
                for photo in photos_to_clean:
                    try:
                        raw_path = os.path.join(raw_dir, photo.system_filename)
                        thumb_path = os.path.join(thumb_dir, photo.system_filename)
                        
                        if os.path.exists(raw_path):
                            os.remove(raw_path)
                            current_app.logger.debug(f"Deleted raw file: {raw_path}")
                            
                        if os.path.exists(thumb_path):
                            os.remove(thumb_path)
                            current_app.logger.debug(f"Deleted thumbnail: {thumb_path}")
                            
                    except Exception as e:
                        current_app.logger.error(f"Error deleting files for photo {photo.id}: {e}")
                        continue

                # Видаляємо записи фото з БД
                for photo in photos_to_clean:
                    ct_session.delete(photo)

                # Оновлюємо статус батча
                batch.status = 'failed'
                batch.error_message = f'Автоматично очищено через неактивність більше {stale_batch_hours} годин'
                batch.completed_at = datetime.utcnow()
                
                cleaned_batches += 1
                current_app.logger.info(f"Cleaned stale batch {batch.id} with {len(photos_to_clean)} photos")
                
            except Exception as e:
                current_app.logger.error(f"Error cleaning batch {batch.id}: {e}")
                continue

        if cleaned_batches > 0:
            try:
                ct_session.commit()
                current_app.logger.info(f"Successfully cleaned {cleaned_batches} stale batches.")
            except Exception as e:
                ct_session.rollback()
                current_app.logger.error(f"Failed to commit batch cleanup changes: {e}")
                
    except Exception as e:
        current_app.logger.error(f"Error in cleanup_stale_batches: {e}")
        ct_session.rollback()
        
    finally:
        close_ct_session()

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