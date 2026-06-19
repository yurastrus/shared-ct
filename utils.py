# SPDX-License-Identifier: AGPL-3.0-only
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
from .database import get_ct_session, close_ct_session, get_ct_engine
from .models import Location, Observation, Photo, UploadBatch, Identification


def get_user_ct_stats(user_id, lang='uk'):
    """#31: personal CT statistics for a user (read-only).

    Uses a separate engine connection (context manager guarantees it is
    closed — does not touch the scoped ct_session). Returns a dict:
      series          — unique series in which the user made identifications,
      identifications — total identifications,
      species_count   — unique species (species_id > 0),
      top_species     — top-5 [{name, count}] by number of series.
    """
    engine = get_ct_engine()
    with engine.connect() as conn:
        agg = conn.execute(text("""
            SELECT COUNT(*) AS idents,
                   COUNT(DISTINCT p.observation_id) AS series,
                   COUNT(DISTINCT CASE WHEN i.species_id > 0 THEN i.species_id END) AS species
            FROM identifications i
            JOIN photos p ON p.id = i.photo_id
            WHERE i.user_id = :uid
        """), {"uid": user_id}).fetchone()

        top_rows = conn.execute(text("""
            SELECT i.species_id AS sid, COUNT(DISTINCT p.observation_id) AS n
            FROM identifications i
            JOIN photos p ON p.id = i.photo_id
            WHERE i.user_id = :uid AND i.species_id > 0
            GROUP BY i.species_id
            ORDER BY n DESC
            LIMIT 5
        """), {"uid": user_id}).fetchall()

        top_species = []
        if top_rows:
            ids = [r.sid for r in top_rows]
            name_rows = conn.execute(text("""
                SELECT id, common_name_ua, common_name_en, scientific_name
                FROM species WHERE id = ANY(:ids)
            """), {"ids": ids}).fetchall()
            names = {}
            for nr in name_rows:
                names[nr.id] = ((nr.common_name_en if lang == 'en' else nr.common_name_ua)
                                or nr.scientific_name or f'#{nr.id}')
            top_species = [{'name': names.get(r.sid, f'#{r.sid}'), 'count': r.n} for r in top_rows]

    return {
        'series': (agg.series if agg else 0) or 0,
        'identifications': (agg.idents if agg else 0) or 0,
        'species_count': (agg.species if agg else 0) or 0,
        'top_species': top_species,
    }


def get_institution_filter(user_inst_ids=None, is_admin=False, selected_inst_id=None, table_alias='l'):
    """Generate a SQL condition for filtering by access rights AND selected institutions."""
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


# Sanity bounds for EXIF dates (Idea 1). A date outside these bounds means a
# reset or drifted camera clock → the timestamp is unreliable → return None,
# and process_single_photo will substitute a visible placeholder (1900-01-01 +
# offset), the same as for photos with no EXIF at all.
EXIF_MIN_VALID_DATE = datetime(2010, 1, 1)
EXIF_MAX_FUTURE_DRIFT = timedelta(hours=24)


def extract_datetime_from_exif(file_stream):
    """Read date and time from the EXIF data of a file, including sub-seconds.

    Returns None if the tag is absent, cannot be parsed, OR the date is
    implausible (earlier than EXIF_MIN_VALID_DATE or further than
    EXIF_MAX_FUTURE_DRIFT into the future).
    """
    try:
        file_stream.seek(0)
        # Read all required tags in one pass
        tags = exifread.process_file(file_stream, details=False, stop_tag='EXIF SubSecTimeOriginal')

        if 'EXIF DateTimeOriginal' in tags:
            date_str = str(tags['EXIF DateTimeOriginal'])
            # First parse the base time (up to seconds)
            dt_object = datetime.strptime(date_str, '%Y:%m:%d %H:%M:%S')

            # Check whether the sub-second tag is present
            if 'EXIF SubSecTimeOriginal' in tags:
                subsec_str = str(tags['EXIF SubSecTimeOriginal']).strip()
                if subsec_str.isdigit():
                    # SubSecTimeOriginal value represents fractional seconds.
                    # For example, '123' means 123 milliseconds.
                    # We need to convert this to microseconds for the timedelta object.
                    # String length matters: '5' -> 0.5s, '50' -> 0.50s, '500' -> 0.500s.
                    # Normalise to 6 digits (microseconds) by right-padding with zeros.
                    subsec_normalized = subsec_str.ljust(6, '0')
                    microseconds = int(subsec_normalized)

                    # Add microseconds to the datetime object
                    dt_object += timedelta(microseconds=microseconds)

            # Sanity-guard: camera clock reset (e.g. year 2000) or drifted into the future
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
    """Create a thumbnail for an image. source can be a file path or a file stream."""
    try:
        size = current_app.config['CAMERA_TRAP_CONFIG']['THUMBNAIL_SIZE']
        with Image.open(source) as img:
            img.thumbnail(size)
            img.save(thumbnail_path, "JPEG", quality=85)
    except Exception as e:
        # Use the .name attribute if source is a stream, otherwise source itself
        source_name = getattr(source, 'name', source)
        current_app.logger.error(f"Failed to create thumbnail for {source_name}: {e}")

def create_upload_batch(location_id, user_id, total_files=None):
    """Create a new batch for file uploads."""
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
    Process a single file and save it with status 'uploaded'.
    Grouping into series happens later.

    Race-safety (fixed 2026-05-24, after /upload-fast Beta):
      • processed_files is updated with an atomic UPDATE ... RETURNING —
        instead of read-modify-write, which lost increments under
        4 parallel workers (fixed 0.4% "missing" counter increments
        across 900 photos).
      • Duplicate check is wrapped in pg_advisory_xact_lock on the
        triplet (location_id, original_filename, captured_at) —
        two parallel attempts to upload THE SAME photo are now
        serialised; different photos are still processed in parallel.
      • Files written to disk before a failed commit are now deleted
        in except — no orphaned JPEGs left in raw/ or thumbnails/.
    """
    ct_session = get_ct_session()

    # Paths — declared at the top level for cleanup in except.
    raw_path = None
    thumb_path = None

    try:
        config = current_app.config['CAMERA_TRAP_CONFIG']
        location = ct_session.query(Location).get(location_id)
        if not location:
            raise ValueError("Invalid Location ID")

        # Ensure the required directories exist
        raw_folder = os.path.join(config['UPLOAD_PATH'], 'pending_photos', 'raw')
        thumb_folder = os.path.join(config['UPLOAD_PATH'], 'pending_photos', 'thumbnails')

        os.makedirs(raw_folder, exist_ok=True)
        os.makedirs(thumb_folder, exist_ok=True)

        if not file or not file.filename:
            raise ValueError("Empty file")

        # ─── ATOMIC INCREMENT of processed_files ──────────────────────────
        # Instead of ORM read-modify-write (race-prone with 4 parallel
        # workers) — a single SQL: UPDATE ... RETURNING. Transactionally
        # isolated: if we fail below and rollback, the increment rolls
        # back with everything else.
        # Bonus: returns a unique 1-based photo number within the batch —
        # used as a seconds-offset for the placeholder captured_at when
        # there is no EXIF (previously all such photos got the same offset
        # → duplicate conflicts).
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

        # 1. Build the original filename
        original_filename = secure_filename(file.filename)
        if not original_filename:
            original_filename = f"photo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"

        # ─── ADVISORY LOCK on (location, filename, time) ──────────────────
        # Serialise processing of photos with the same duplicate key
        # (lock key — the same one checked by the preflight below).
        # Two parallel workers with the same (location, filename,
        # captured_at) will now wait for each other → the first INSERTs,
        # the second detects a duplicate and correctly raises ValueError.
        # Different (location, filename, captured_at) → different lock keys →
        # no blocking, parallelism preserved.
        # SQLite does not have pg_advisory_xact_lock — in tests we simply
        # skip it (unit tests are single-threaded, no race condition).
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
            # SQLite or another engine without advisory locks — race condition
            # remains, but it does not reproduce in tests (single thread).
            pass

        # 2. Duplicate check — now race-safe inside the advisory lock
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

        # 3. Build the system filename
        lat_str = str(location.latitude).replace('.', '_')
        lon_str = str(location.longitude).replace('.', '_')
        timestamp_str = captured_at.strftime('%Y%m%d_%H%M%S_%f')

        base_name = f"{lat_str}_{lon_str}_{timestamp_str}_{batch_id[:8]}"
        ext = os.path.splitext(original_filename)[1] or '.jpg'

        # === FILENAME UNIQUENESS CHECK ===
        # Inside the advisory lock no competing worker with the same key can
        # reach this point — so the counter is guaranteed to be collision-free.
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

        # Save files
        file.seek(0)

        file.seek(0)

        if save_original:
            # Scenario 1: user explicitly wants to keep the original (checkbox ticked)
            file.save(raw_path)
            # Create thumbnail from the already-saved local file (faster)
            create_thumbnail(raw_path, thumb_path)
        else:
            # Scenario 2: browser is expected to have compressed the file already (checkbox not ticked)
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
                    current_app.logger.warning(f"File {system_filename} was compressed by the server (client sent a mismatched file)")

            except Exception as e:
                current_app.logger.error(f"Image validation error for {original_filename}: {e}")
                file.seek(0)
                create_thumbnail(file, thumb_path)

        # Create database record
        photo = Photo(
            upload_batch_id=batch_id,
            original_filename=original_filename,
            system_filename=system_filename,
            captured_at=captured_at,
            status='uploaded'
        )
        ct_session.add(photo)

        # processed_files was already atomically incremented at the top of this
        # function — do NOT update it again here (previously there was another
        # read-modify-write at this point).

        ct_session.commit()

        return photo.id

    except Exception as e:
        current_app.logger.error(f"Error processing file {file.filename}: {e}")
        ct_session.rollback()
        # Clean up disk files if commit failed — to avoid leaving orphaned
        # JPEGs in raw/ and thumbnails/.
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
    Group all photos from the batch into observation series.
    Called after all files in the batch have been uploaded.
    """
    ct_session = get_ct_session()

    try:
        config = current_app.config['CAMERA_TRAP_CONFIG']

        # Fetch the batch
        batch = ct_session.query(UploadBatch).get(batch_id)
        if not batch:
            raise ValueError(f"Batch {batch_id} not found")

        if batch.status != 'uploading':
            raise ValueError(f"Batch {batch_id} is not in uploading status")

        batch.status = 'processing'
        ct_session.flush()

        # Fetch all photos from the batch ordered by time
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
                # Check if we can add to the current series
                if (current_observation and
                    current_observation.location_id == batch.location_id and
                    photo.captured_at <= current_observation.series_end_time + series_window):

                    # Add to current series
                    current_observation.series_end_time = photo.captured_at
                    current_observation.photo_count = (current_observation.photo_count or 0) + 1

                else:
                    # Create a new series
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

                # Attach photo to the observation
                photo.observation_id = current_observation.id
                photo.status = 'pending'

                # Set sequence_number within the observation
                photo.sequence_number = current_observation.photo_count

            except Exception as e:
                current_app.logger.error(f"Error processing photo {photo.id} in batch {batch_id}: {e}")
                continue

        # Update location counters
        location = ct_session.query(Location).get(batch.location_id)
        if location:
            location.photo_count = ct_session.query(Photo).join(Observation).filter(
                Observation.location_id == location.id,
                Photo.status.in_(['pending', 'completed', 'needs_review'])
            ).count()

        # Finalise the batch
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
    """Return the status of the batch."""
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
    DEPRECATED: Use process_single_photo + group_batch_into_series instead.
    """
    current_app.logger.warning(
        "process_photo_batch is deprecated. Use process_single_photo + group_batch_into_series instead."
    )

    # Create the batch
    batch_id = create_upload_batch(location_id, user.id, len(files))

    try:
        # Process files one by one
        for file in files:
            process_single_photo(file, location_id, user.id, batch_id)

        # Group into series
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

        # Keep only this logging call
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
    """Mark an observation as complete.

    If winner_species_id (consensus species) is provided, records the
    correctness of AI predictions for this series (Idea 4):
      was_correct = (prediction_species_id == winner_species_id),
      None — if AI did not identify a species (prediction_species_id IS NULL).
    """
    try:
        # Use the passed db_session
        observation = db_session.query(Observation).get(observation_id)
        if observation:
            for photo in observation.photos:
                photo.status = 'completed'
            observation.status = 'completed'

            # Record AI correctness at the moment of consensus (Idea 4).
            # Wrapped separately: on installations without the AI schema
            # (ai_predictions does not exist) this must not abort the
            # consensus itself.
            if winner_species_id is not None:
                try:
                    from .models import AIPrediction
                    preds = db_session.query(AIPrediction).filter(
                        AIPrediction.observation_id == observation_id
                    ).all()
                    for pred in preds:
                        if pred.prediction_species_id is None:
                            pred.was_correct = None  # AI did not identify a species → undetermined
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
        raise  # Re-raise the exception

def migrate_pending_observations_to_single_identification():
    """
    Optimised version — one query instead of hundreds.
    """
    ct_session = get_ct_session()

    try:
        config = current_app.config.get('CAMERA_TRAP_CONFIG', {})
        min_identifications = config.get('MIN_IDENTIFICATIONS', 3)

        # Single query for all pending observations with enough identifications
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

        # Find observations with consensus
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
    Calculate the total number of trap-days (sum of active days across all locations)
    for the given period.

    Args:
        location_ids: optional list of Location.id to restrict effort to specific
                      locations (for filtering by institution/ecoregion).
                      None means "all locations" (old default behaviour).

    Logic is based on photo presence. If the gap between photos is less than
    MAX_GAP_DAYS, the period is considered active.
    """
    # Fetch activity dates for all locations that had observations
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

    # Exclude admin-invalidated locations so the RAI effort denominator stays
    # consistent with the (already filtered) detection numerator.
    dates_query = dates_query.filter(Location.is_valid.is_(True))

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
            # Count days between first and last photo, accounting for gaps
            calculated_days = 1
            prev_date = dates[0]
            for i in range(1, len(dates)):
                curr_date = dates[i]
                diff = (curr_date - prev_date).days

                # If the gap is small, add all days in the interval
                if diff <= MAX_GAP_DAYS:
                    calculated_days += diff
                else:
                    # If the gap is large, treat the camera as inactive during that time
                    # and add only 1 day for the fact of a photo
                    calculated_days += 1
                prev_date = curr_date
            effort_days = calculated_days

        total_effort_days += effort_days

    return total_effort_days




# ── Camera trap coverage calendar (#38) ──────────────────────────────────────
#
# Day coverage = "camera was active". CT photos are trigger-based, so
# "days with photos" underestimates coverage. Therefore:
# covered_days = union of deployment intervals (start..end) ∪ gap-filled
# days with photos (for locations/deployments without dates).
# Cell intensity (for gradient shading #43) = number of photos per day.


def fill_day_gaps(days, max_gap_days):
    """Return a set of dates: input days plus gaps filled between adjacent
    dates when the gap is <= max_gap_days (camera was present, animals just
    did not trigger it).

    days: iterable[date]; max_gap_days: int (0 = no gap filling).
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


def _apply_coverage_intensity(months, value_of, include):
    """cell['intensity'] ∈ [0,1] linearly scaled min→max (#43). include(cell) → in scale,
    otherwise None. All equal values → 1.0."""
    vals = [value_of(c) for mo in months for wk in mo['weeks']
            for c in wk if c and include(c)]
    if vals:
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1.0
    else:
        lo, span = 0, 1.0
    for mo in months:
        for wk in mo['weeks']:
            for c in wk:
                if not c:
                    continue
                c['intensity'] = ((value_of(c) - lo) / span) if include(c) else None


def build_ct_coverage_calendar(covered_days, photo_counts, good_photos=1, mode='all'):
    """Month-by-month camera trap coverage calendar. Pure function (no DB access).

    mode='all' (default): all years month by month. mode='aggregated': a single
    synthetic year (12 months) where each (month, day) aggregates all years +
    cell['years'] = number of years with data.

    covered_days: set[date] — days when the camera was active.
    photo_counts: {date: int} — number of photos per day (for intensity/gradient).
    good_photos: threshold for "good" in photos/day.

    Returns a dict like PAM build_coverage_calendar:
        months[], total_photos, active_camera_days, days_with_photos, day_range.
    cell = {day, date, covered(bool), photos(int), level: good|partial|missing}
      level: missing — camera not active; partial — active, 0..<good photos;
             good — active and >= good_photos photos.
    """
    covered_days = {d for d in (covered_days or set()) if d is not None}
    photo_counts = {d: c for d, c in (photo_counts or {}).items() if d is not None}

    all_days = covered_days | set(photo_counts)
    if not all_days:
        return {'months': [], 'total_photos': 0, 'active_camera_days': 0,
                'days_with_photos': 0, 'day_range': None, 'mode': mode}

    first, last = min(all_days), max(all_days)
    total_photos = sum(photo_counts.values())
    cal = calendar.Calendar(firstweekday=0)

    def _level(covered, photos):
        if not covered:
            return 'missing'
        return 'good' if photos >= good_photos else 'partial'

    if mode == 'aggregated':
        # Aggregate by (month, day) across all years.
        agg = {}  # (m, d) -> {'cov_years': set, 'photos': int, 'years': set}
        for d in covered_days:
            a = agg.setdefault((d.month, d.day),
                               {'cov_years': set(), 'photos': 0, 'years': set()})
            a['cov_years'].add(d.year)
            a['years'].add(d.year)
        for d, cnt in photo_counts.items():
            a = agg.setdefault((d.month, d.day),
                               {'cov_years': set(), 'photos': 0, 'years': set()})
            a['photos'] += cnt
            a['years'].add(d.year)
        months = []
        for m in range(1, 13):
            weeks = []
            for week in cal.monthdatescalendar(2000, m):  # 2000 is a leap year
                row = []
                for d in week:
                    if d.month != m:
                        row.append(None)
                        continue
                    a = agg.get((m, d.day))
                    covered = bool(a and a['cov_years'])
                    photos = a['photos'] if a else 0
                    row.append({'day': d.day, 'date': d, 'covered': covered,
                                'photos': photos,
                                'years': len(a['years']) if a else 0,
                                'level': _level(covered, photos)})
                weeks.append(row)
            months.append({'year': 2000, 'month': m, 'label': f'{m:02d}', 'weeks': weeks})
        _apply_coverage_intensity(months, lambda c: c['photos'], lambda c: c['covered'])
        return {
            'months': months, 'total_photos': total_photos,
            'active_camera_days': len(covered_days),
            'days_with_photos': len(photo_counts),
            'day_range': (first, last), 'mode': 'aggregated',
            'years': sorted({d.year for d in all_days}),
        }

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
                row.append({'day': d.day, 'date': d, 'covered': covered,
                            'photos': photos, 'level': _level(covered, photos)})
            weeks.append(row)
        months.append({'year': y, 'month': m, 'label': f'{y}-{m:02d}', 'weeks': weeks})
        m += 1
        if m > 12:
            m, y = 1, y + 1

    _apply_coverage_intensity(months, lambda c: c['photos'], lambda c: c['covered'])
    return {
        'months': months,
        'total_photos': total_photos,
        'active_camera_days': len(covered_days),
        'days_with_photos': len(photo_counts),
        'day_range': (first, last),
        'mode': 'all',
    }
