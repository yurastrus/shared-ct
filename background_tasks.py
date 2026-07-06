# SPDX-License-Identifier: AGPL-3.0-only
import os, shutil
from datetime import datetime, timedelta
from flask import current_app
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from .database import get_ct_session, close_ct_session
from .models import Photo, Identification, Observation, UploadBatch, Species

def cleanup_old_photos():
    """Find completed observations older than the configured period that are not "favorites".

    Deletes their raw + thumbnail files and updates their status to 'archived'.
    Operates at the observation level.

    Optimized (2026-05-25): semantics unchanged, only speed/reliability improved.
      1. selectinload(Observation.photos) — fetches photos in one additional
         query instead of N+1 (1 per series + N per photos).
      2. Chunked commit every CHUNK_OBS_SIZE series — on failure the loss is
         at most the last chunk, not the entire run.
      3. File operations happen AFTER a successful chunk commit: if the commit
         fails, the files are intact (safe to retry); if os.remove fails after
         a successful commit, the file becomes an orphan (picked up by the
         new cleanup module in cleanup.py).
      4. species_id=-2 is the "Other" category in the species reference
         (Identification without a determined species); such series are
         intentionally NOT archived because they need operator review.
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

    CHUNK_OBS_SIZE = 50  # ~several hundred photos per chunk at average series size

    # Counters advance ONLY after a successful chunk commit —
    # on failure we return the honest count of successfully archived items.
    archived_photos_count = 0
    archived_observations_count = 0

    try:
        # OPTIMIZATION #1: selectinload — photos are loaded in one
        # query `WHERE observation_id IN (...)`, not via N+1 round-trips.
        # OPTIMIZATION #4: species_id=-2 is the "Other" category in the
        # species reference; such series are intentionally NOT archived.
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

        # Current chunk state (between flush_chunk calls).
        chunk_state = {
            'pending_photos': 0,
            'pending_obs': 0,
            'files': [],  # paths to delete AFTER the chunk commit
        }

        def flush_chunk():
            """Commit the current chunk → delete its files → reset."""
            nonlocal archived_photos_count, archived_observations_count
            if chunk_state['pending_photos'] == 0 and chunk_state['pending_obs'] == 0:
                return
            # OPTIMIZATION #2: short chunk commit. An exception here is
            # handled by the outer except; rollback resets only this chunk.
            ct_session.commit()
            # Promote counters after a successful commit.
            archived_photos_count += chunk_state['pending_photos']
            archived_observations_count += chunk_state['pending_obs']
            # OPTIMIZATION #3: file deletion happens ONLY after a successful
            # commit. If the commit failed, files are intact and can be retried.
            for p in chunk_state['files']:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError as e:
                    # Orphan file (DB says archived, file remained) —
                    # will be picked up by cleanup.find_orphan_files_on_disk.
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
                        # Mark in ORM first (will be committed in flush_chunk).
                        photo.status = 'archived'
                        photos_archived_in_observation += 1
                        chunk_state['pending_photos'] += 1
                        # Accumulate file paths for post-commit deletion.
                        chunk_state['files'].append(
                            os.path.join(raw_dir, photo.system_filename))
                        chunk_state['files'].append(
                            os.path.join(thumb_dir, photo.system_filename))

                # Series → archived only if all its photos are archived/favorite.
                # Semantics mirror the original (including the OR condition on
                # `> 0 or not observation_has_favorites` — covers the case where
                # all photos were already archived in a previous run).
                if photos_archived_in_observation > 0 or not observation_has_favorites:
                    all_photos_archived = all(
                        photo.status == 'archived' or photo.is_favorite
                        for photo in observation.photos
                    )
                    if all_photos_archived:
                        observation.status = 'archived'
                        chunk_state['pending_obs'] += 1

                # If the chunk is full — commit and delete files.
                if chunk_state['pending_obs'] >= CHUNK_OBS_SIZE:
                    flush_chunk()

            except Exception as e:
                current_app.logger.error(
                    f"Error processing observation {observation.id} for archival: {e}"
                )
                continue

        # Final commit for whatever remains in the current chunk.
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
            # Return the HONEST count of successfully archived items
            # (chunks that completed their commit before the failure).
            'photos_deleted': archived_photos_count,
            'observations_archived': archived_observations_count,
        }

    finally:
        close_ct_session()

# ─────────────────────────────────────────────────────────────────────────
# NOTE (2026-05-25): cleanup_stale_batches() has been removed.
# It was replaced by the `cleanup.py` module with a full 3-category dry-run,
# probe-protection for active batches, and background execution. The
# /admin/cleanup-batches route was also removed; new routes are:
# /admin/cleanup/{analyze, execute/<id>, task/<id>}.
# get_batch_statistics below is kept — used elsewhere.
# ─────────────────────────────────────────────────────────────────────────

def get_batch_statistics():
    """Return batch statistics for monitoring."""
    ct_session = get_ct_session()

    try:
        stats = {}

        # Batch counts by status.
        batch_stats = ct_session.query(
            UploadBatch.status,
            func.count(UploadBatch.id).label('count')
        ).group_by(UploadBatch.status).all()

        stats['batches_by_status'] = {stat.status: stat.count for stat in batch_stats}

        # Photos with no associated observation.
        orphaned_photos = ct_session.query(func.count(Photo.id)).filter(
            Photo.observation_id == None,
            Photo.status == 'uploaded'
        ).scalar()

        stats['orphaned_photos'] = orphaned_photos

        # Oldest incomplete batch.
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

def get_storage_disk_usage():
    """Return free/total disk space for the filesystem holding the CT uploads.

    A page-side equivalent of `df -h` for the photo storage, so admins don't
    need to SSH in to check. The path comes from CAMERA_TRAP_CONFIG['UPLOAD_PATH']
    (read from the host's .env). Returns a dict with byte counts, or {} if the
    path is not configured / not reachable — the template renders '—' then.
    """
    try:
        config = current_app.config['CAMERA_TRAP_CONFIG']
        upload_path = config.get('UPLOAD_PATH')
    except KeyError:
        current_app.logger.warning("CAMERA_TRAP_CONFIG not defined; cannot report disk usage.")
        return {}

    if not upload_path:
        current_app.logger.warning("UPLOAD_PATH not defined; cannot report disk usage.")
        return {}

    # disk_usage needs an existing path; walk up to the nearest existing parent
    # so a not-yet-created uploads subfolder still reports the right filesystem.
    probe = upload_path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent

    try:
        usage = shutil.disk_usage(probe)
    except OSError as e:
        current_app.logger.warning(f"Cannot stat disk for '{upload_path}': {e}")
        return {}

    return {
        'path': upload_path,
        'total_bytes': usage.total,
        'used_bytes': usage.used,
        'free_bytes': usage.free,
    }

def get_cleanup_statistics():
    """Return file deletion statistics without actually deleting anything.

    The eligibility query MUST mirror cleanup_old_photos() exactly, and sizes
    are measured from the actual files on disk. Previously this used
    `Observation.series_end_time < threshold_date` (when the series was
    captured) instead of cleanup_old_photos()'s real criterion — the most
    recent *identification* being old enough — and a flat 2 MB/photo guess.
    Since identification can lag well behind capture, that counted many
    series as "ready" that the real job would not touch yet, and the flat
    per-photo guess didn't reflect actual file sizes — together inflating
    the "estimated size to be freed" figure roughly 10x versus what
    cleanup_old_photos() actually freed.
    """
    ct_session = get_ct_session()

    try:
        config = current_app.config['CAMERA_TRAP_CONFIG']
        cleanup_days = config.get('CLEANUP_DAYS', 30)
        upload_path = config.get('UPLOAD_PATH')

        threshold_date = datetime.utcnow() - timedelta(days=cleanup_days)

        # Same join + HAVING as cleanup_old_photos(): eligibility is based on
        # the most recent identification for the series, not its capture time.
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

        raw_dir = os.path.join(upload_path, 'pending_photos', 'raw') if upload_path else None
        thumb_dir = os.path.join(upload_path, 'pending_photos', 'thumbnails') if upload_path else None

        total_photos = 0
        estimated_size_bytes = 0

        for observation in old_observations:
            for photo in observation.photos:
                if not photo.is_favorite and photo.status == 'completed':
                    total_photos += 1
                    # cleanup_old_photos() deletes both the raw file and its
                    # thumbnail — measure both from disk instead of guessing.
                    if raw_dir and thumb_dir:
                        for directory in (raw_dir, thumb_dir):
                            path = os.path.join(directory, photo.system_filename)
                            try:
                                estimated_size_bytes += os.path.getsize(path)
                            except OSError:
                                pass  # file already gone — contributes 0

        return {
            'observations_count': len(old_observations),
            'photos_count': total_photos,
            'estimated_size_mb': round(estimated_size_bytes / (1024 * 1024), 1)
        }

    except Exception as e:
        current_app.logger.error(f"Error getting cleanup statistics: {e}")
        raise
    finally:
        close_ct_session()

def delete_unfavorited_raw_files():
    """Find ALL photos not marked as favorites (is_favorite = False) and delete their original raw files to save disk space.

    This action is IRREVERSIBLE.
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

        # --- Step 1: Find candidate photos in the database ---
        # Criterion: any photo where is_favorite = False.
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

        # --- Step 2: Process each candidate ---
        for photo in photos_to_delete_raw:
            try:
                raw_path = os.path.join(raw_folder, photo.system_filename)

                if os.path.exists(raw_path):
                    # --- Step 3: Delete the file ---
                    os.remove(raw_path)
                    deleted_count += 1
                    current_app.logger.debug(f"Successfully deleted raw file for photo {photo.id} (file: {photo.system_filename}).")
                else:
                    # File is already gone, skip silently.
                    skipped_count += 1
                    continue

            except Exception as e:
                current_app.logger.error(f"Error processing photo {photo.id} for deletion: {e}")
                continue

        # --- Final result ---
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
