# SPDX-License-Identifier: AGPL-3.0-only
"""Fast upload path for large photo sets (10k–100k+).

Runs in parallel with the old `utils.group_batch_into_series` —
nothing in the old path is changed; only `process_single_photo` is shared.

Architecture:
    /upload-fast (HTML)
        ├── /api/create-batch           (old, reused)
        ├── /upload/process-single      (old, reused)   ← N parallel workers
        ├── /api/finalize-batch-async   (202 → start_async_grouping)
        ├── /api/batch-status           (old, reused for polling)
        └── /api/batch/<id>/uploaded-files (resumable)

Invariant:
    Grouping is called only AFTER all photos of the batch have been
    committed to the DB with status='uploaded'. The CTE query does a
    single-pass LAG over the full set, so series boundaries are built
    globally.

Replacing threading with Celery later — a one-liner: swap the body of
`start_async_grouping` for `finalize_batch_task.delay(batch_id)`;
routes and JS stay unchanged.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Optional

from flask import current_app
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, DBAPIError

from .database import get_ct_engine, get_ct_session, close_ct_session
from .models import UploadBatch


# ─────────────────────────────────────────────────────────────────────────────
# 1. GROUPING IN A SINGLE SQL QUERY (single-pass over the entire batch)
# ─────────────────────────────────────────────────────────────────────────────

def group_batch_into_series_sql(batch_id: str) -> int:
    """Group all photos of a batch into series in one CTE query.

    Returns the number of photos that received an observation_id.

    Algorithm:
        1. WITH ordered: sort the batch's photos by (captured_at, id),
           compute LAG(captured_at) — the timestamp of the previous photo.
        2. WITH marked: flag a series boundary where
           captured_at - prev_captured > SERIES_TIME_WINDOW
           (or where prev IS NULL — the first photo). A cumulative SUM(flag)
           gives series_idx — the series number within the batch.
        3. INSERT INTO observations one row per series_idx:
           MIN/MAX(captured_at), COUNT(*). RETURNING id for the mapping.
        4. UPDATE photos: set observation_id, sequence_number
           (ROW_NUMBER within the series), status='pending'.
        5. A separate short UPDATE for locations.photo_count.

    Idempotency:
        - If the batch is already 'completed' — return 0 with no action.
        - If partial Observations exist from a previous failed attempt
          (batch in 'failed' / 'grouping') — delete them first:
          DELETE FROM observations WHERE id IN (...) — linked photos
          automatically lose their observation_id via nullable FK.

    Transactionality:
        The entire body is one transaction via engine.begin(). No
        per-photo flushes, no ORM objects in memory. For 50k photos
        this runs in seconds, not minutes.
    """
    config = current_app.config['CAMERA_TRAP_CONFIG']
    series_window_seconds = int(config['SERIES_TIME_WINDOW'])

    engine = get_ct_engine()

    with engine.begin() as conn:
        # ─── 0. Check batch state + read context ─────────────────────────
        row = conn.execute(
            text("""
                SELECT location_id, uploaded_by_id, status
                  FROM upload_batches
                 WHERE id = :b
                 FOR UPDATE
            """),
            {"b": batch_id},
        ).first()

        if row is None:
            raise ValueError(f"Batch {batch_id} not found")

        if row.status == 'completed':
            current_app.logger.info(
                f"[fast-upload] Batch {batch_id} already completed; skip grouping"
            )
            return 0

        location_id = row.location_id
        uploaded_by_id = row.uploaded_by_id

        # ─── 1. Clean up partial results from a previous attempt ─────────
        # ORDER MATTERS: first null the FK in this batch's photos
        # (to avoid violating the foreign-key constraint), then delete
        # the observations that now have no photos.

        # 1a. Save to tmp the observation_ids that previously had photos
        # from this batch — candidates for deletion.
        conn.execute(text("DROP TABLE IF EXISTS tmp_partial_obs"))
        conn.execute(
            text("""
                CREATE TEMP TABLE tmp_partial_obs ON COMMIT DROP AS
                SELECT DISTINCT observation_id AS id
                  FROM photos
                 WHERE upload_batch_id = :b
                   AND observation_id IS NOT NULL
            """),
            {"b": batch_id},
        )

        # 1b. Reset this batch's photos to 'uploaded' —
        # the observation_id FK is nulled out, no longer blocking DELETE.
        conn.execute(
            text("""
                UPDATE photos
                   SET observation_id = NULL,
                       sequence_number = NULL,
                       status = 'uploaded'
                 WHERE upload_batch_id = :b
            """),
            {"b": batch_id},
        )

        # 1c. Delete candidates that now have no photos
        # (i.e. all their photos belonged to this batch — orphan
        # observations from a previous failed attempt).
        conn.execute(
            text("""
                DELETE FROM observations o
                 WHERE o.id IN (SELECT id FROM tmp_partial_obs)
                   AND NOT EXISTS (
                       SELECT 1 FROM photos p
                        WHERE p.observation_id = o.id
                   )
            """),
        )

        # ─── 2. Compute series_idx with window functions, aggregate to tmp ──
        # Create a temp table with series_idx for each photo —
        # needed because combining INSERT...RETURNING with UPDATE photos
        # in one statement without a unique key is awkward.
        conn.execute(text("DROP TABLE IF EXISTS tmp_batch_groups"))
        conn.execute(
            text("""
                CREATE TEMP TABLE tmp_batch_groups
                ON COMMIT DROP
                AS
                WITH ordered AS (
                    SELECT
                        p.id          AS photo_id,
                        p.captured_at AS captured_at,
                        LAG(p.captured_at) OVER (
                            ORDER BY p.captured_at, p.id
                        ) AS prev_t
                      FROM photos p
                     WHERE p.upload_batch_id = :b
                       AND p.status = 'uploaded'
                ),
                marked AS (
                    SELECT
                        photo_id,
                        captured_at,
                        CASE
                            WHEN prev_t IS NULL
                              OR captured_at - prev_t
                                 > make_interval(secs => :win)
                            THEN 1 ELSE 0
                        END AS new_series_flag
                      FROM ordered
                ),
                numbered AS (
                    -- Cumulative sum of boundary flags = series index
                    SELECT
                        photo_id,
                        captured_at,
                        SUM(new_series_flag) OVER (
                            ORDER BY captured_at, photo_id
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                        ) AS series_idx
                      FROM marked
                )
                -- ROW_NUMBER PARTITION BY series_idx as a separate step, because
                -- PostgreSQL does not allow a window function inside
                -- the PARTITION BY of another window function.
                SELECT
                    photo_id,
                    captured_at,
                    series_idx,
                    ROW_NUMBER() OVER (
                        PARTITION BY series_idx
                        ORDER BY captured_at, photo_id
                    ) AS seq_in_series
                  FROM numbered
            """),
            {"b": batch_id, "win": series_window_seconds},
        )

        # If the temp table is empty — no photos found.
        photo_count = conn.execute(
            text("SELECT COUNT(*) FROM tmp_batch_groups")
        ).scalar() or 0

        if photo_count == 0:
            raise ValueError(
                f"No 'uploaded' photos found in batch {batch_id}"
            )

        # ─── 3. INSERT series into observations + RETURNING for mapping ───
        # Create another tmp table with series_idx -> observation_id mapping.
        conn.execute(text("DROP TABLE IF EXISTS tmp_series_obs"))
        conn.execute(
            text("""
                CREATE TEMP TABLE tmp_series_obs
                ON COMMIT DROP
                AS
                WITH agg AS (
                    SELECT
                        series_idx,
                        MIN(captured_at) AS series_start,
                        MAX(captured_at) AS series_end,
                        COUNT(*)         AS cnt
                      FROM tmp_batch_groups
                     GROUP BY series_idx
                ),
                ins AS (
                    INSERT INTO observations (
                        location_id, series_start_time, series_end_time,
                        uploaded_by_id, photo_count, status, created_at
                    )
                    SELECT
                        :loc, series_start, series_end,
                        :uid, cnt, 'pending', NOW()
                      FROM agg
                     ORDER BY series_idx
                    RETURNING
                        id, series_start_time, series_end_time
                )
                SELECT
                    a.series_idx,
                    ins.id AS observation_id
                  FROM agg a
                  JOIN ins
                    ON ins.series_start_time = a.series_start
                   AND ins.series_end_time   = a.series_end
            """),
            {"loc": location_id, "uid": uploaded_by_id},
        )

        # ─── 4. UPDATE photos: observation_id + sequence_number ──────────
        result = conn.execute(
            text("""
                UPDATE photos AS p
                   SET observation_id  = so.observation_id,
                       sequence_number = bg.seq_in_series,
                       status          = 'pending'
                  FROM tmp_batch_groups bg
                  JOIN tmp_series_obs   so ON so.series_idx = bg.series_idx
                 WHERE p.id = bg.photo_id
            """),
        )
        grouped_photos = result.rowcount or 0

        # ─── 5. Recompute photo_count for the location in one short UPDATE ─
        # Status set — bit-for-bit identical to the legacy group_batch_into_series
        # (utils.py): 'pending', 'completed', 'needs_review'. 'grouped' is NOT
        # included — to keep the DB result identical to the old path.
        conn.execute(
            text("""
                UPDATE locations
                   SET photo_count = (
                       SELECT COUNT(*) FROM photos p
                         JOIN observations o ON o.id = p.observation_id
                        WHERE o.location_id = :loc
                          AND p.status IN ('pending', 'completed',
                                           'needs_review')
                   )
                 WHERE id = :loc
            """),
            {"loc": location_id},
        )

    current_app.logger.info(
        f"[fast-upload] Batch {batch_id}: grouped {grouped_photos} photos"
    )
    return grouped_photos


# ─────────────────────────────────────────────────────────────────────────────
# 2. ASYNC LAUNCH (threading; Celery replacement point in the future)
# ─────────────────────────────────────────────────────────────────────────────

# UploadBatch.status states added by this module:
#   'ready_to_group' — client sent finalize, task not yet started
#   'grouping'       — background thread is running group_batch_into_series_sql
# The rest ('uploading' / 'completed' / 'failed') — same as the old path.


def _set_batch_status(batch_id: str, status: str,
                      error_message: Optional[str] = None) -> None:
    """Short separate commit that updates the batch status."""
    engine = get_ct_engine()
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE upload_batches
                   SET status = :s,
                       error_message = COALESCE(:err, error_message),
                       completed_at = CASE
                           WHEN :s IN ('completed', 'failed') THEN NOW()
                           ELSE completed_at
                       END
                 WHERE id = :b
            """),
            {"b": batch_id, "s": status,
             "err": (error_message[:500] if error_message else None)},
        )


def _run_grouping_in_thread(app, batch_id: str) -> None:
    """Background thread body. Runs outside the HTTP context."""
    with app.app_context():
        try:
            _set_batch_status(batch_id, 'grouping')
            group_batch_into_series_sql(batch_id)
            _set_batch_status(batch_id, 'completed')
        except (OperationalError, DBAPIError) as e:
            # Transient DB errors — simple single retry.
            current_app.logger.warning(
                f"[fast-upload] Batch {batch_id} hit DB error, retry once: {e}"
            )
            try:
                group_batch_into_series_sql(batch_id)
                _set_batch_status(batch_id, 'completed')
            except Exception as e2:
                current_app.logger.exception(
                    f"[fast-upload] Batch {batch_id} failed after retry"
                )
                _set_batch_status(batch_id, 'failed', str(e2))
        except Exception as e:
            current_app.logger.exception(
                f"[fast-upload] Batch {batch_id} failed during grouping"
            )
            _set_batch_status(batch_id, 'failed', str(e))


def start_async_grouping(batch_id: str) -> None:
    """Start background grouping. Called from the HTTP route after the batch
    has been moved to 'ready_to_group'. Returns immediately.

    NB: this layer is intentionally thin — so that in the future it can be
    replaced with `finalize_batch_task.delay(batch_id)` without touching
    routes or JS.
    """
    app = current_app._get_current_object()  # type: ignore[attr-defined]
    thread = threading.Thread(
        target=_run_grouping_in_thread,
        args=(app, batch_id),
        name=f"ct-fast-group-{batch_id[:8]}",
        daemon=True,
    )
    thread.start()
    current_app.logger.info(
        f"[fast-upload] Started async grouping thread for batch {batch_id}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. RECOVERY: on worker restart — pick up "stuck" batches
# ─────────────────────────────────────────────────────────────────────────────

def recover_stale_grouping_batches() -> int:
    """Called from the app factory on startup. If the worker was killed during
    'grouping' — no one will restart the batch. This helper moves stale batches
    to 'failed' so the client sees an error and can retry
    (group_batch_into_series_sql is idempotent).

    Threshold — 30 minutes: a realistic upper bound for 100k photos in SQL.
    """
    try:
        engine = get_ct_engine()
        cutoff = datetime.utcnow() - timedelta(minutes=30)
        with engine.begin() as conn:
            result = conn.execute(
                text("""
                    UPDATE upload_batches
                       SET status = 'failed',
                           error_message = COALESCE(error_message,
                               'Worker restarted while grouping; please retry'),
                           completed_at = NOW()
                     WHERE status = 'grouping'
                       AND created_at < :cutoff
                """),
                {"cutoff": cutoff},
            )
            n = result.rowcount or 0
            if n:
                current_app.logger.warning(
                    f"[fast-upload] Recovered {n} stale 'grouping' batches"
                )
            return n
    except Exception as e:
        # Do not crash the entire app on startup — log only.
        try:
            current_app.logger.error(
                f"[fast-upload] recover_stale_grouping_batches failed: {e}"
            )
        except Exception:
            pass
        return 0
    finally:
        close_ct_session()
