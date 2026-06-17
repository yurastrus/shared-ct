# SPDX-License-Identifier: AGPL-3.0-only
import logging
import random
import threading
from collections import defaultdict
import numpy as np
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import func, extract, select, distinct, text

from flask import current_app

# Import models and session helpers from the project's existing files.
from .database import get_ct_session, close_ct_session, get_ct_engine
from .models import (
    Observation, Photo, Identification, Species, Location,
    LocationMonthlyActivity, CalculationLog, SpeciesYearlyTrend,
    location_institutions
)

# Configure logging so progress is visible during execution.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def _calculate_monthly_activity():
    """Perform the main calculation and populate the location_monthly_activity table.

    This version correctly handles zero-detection entries.
    """
    session = get_ct_session()
    try:
        logging.info("Starting calculation of monthly activity...")

        # Step 1: Truncate the table.
        logging.info("Truncating LocationMonthlyActivity table...")
        session.query(LocationMonthlyActivity).delete()
        session.commit()

        # Step 2: Calculate TRAP DAYS for all active locations.
        logging.info("Calculating trap days for ALL active locations per month...")
        trap_days_query = session.query(
            Observation.location_id,
            extract('year', Photo.captured_at).label('year'),
            extract('month', Photo.captured_at).label('month'),
            func.count(func.distinct(func.date(Photo.captured_at))).label('trap_days')
        ).join(Photo, Observation.id == Photo.observation_id)\
        .filter(Observation.status.in_(['completed', 'archived']))\
        .group_by(Observation.location_id, 'year', 'month')\
        .all()

        # Build a dict: {(loc, year, month): trap_days}.
        trap_days_map = {
            (r.location_id, r.year, r.month): r.trap_days
            for r in trap_days_query
        }
        logging.info(f"Calculated trap days for {len(trap_days_map)} unique location-months.")

        # Step 3: Calculate DETECTION COUNTS.
        logging.info("Calculating detection counts where species were present...")
        detection_query = session.query(
            Identification.species_id,
            Observation.location_id,
            extract('year', Photo.captured_at).label('year'),
            extract('month', Photo.captured_at).label('month'),
            func.count(func.distinct(func.date(Photo.captured_at))).label('detection_count')
        ).join(Photo, Identification.photo_id == Photo.id)\
        .join(Observation, Photo.observation_id == Observation.id)\
        .filter(
            Observation.status.in_(['completed', 'archived']),
            Identification.species_id.isnot(None),
            Identification.species_id > 0
        )\
        .group_by(Identification.species_id, Observation.location_id, 'year', 'month')\
        .all()

        # Build a dict: {(species, loc, year, month): detection_count}.
        detection_map = {
            (r.species_id, r.location_id, r.year, r.month): r.detection_count
            for r in detection_query
        }
        logging.info(f"Found {len(detection_map)} non-zero detection records.")

        # Step 4: Assemble final records, including zero-detection entries.
        logging.info("Assembling final records, including zero-detection entries...")
        all_species_ids = [row[0] for row in session.query(Species.id).filter(Species.id > 0).all()]
        new_activity_records = []

        # Iterate over all (active location/month) × (all species) combinations.
        for (location_id, year, month), trap_days in trap_days_map.items():
            for species_id in all_species_ids:
                key = (species_id, location_id, year, month)
                detection_count = detection_map.get(key, 0)  # Use 0 if no detections.

                record = LocationMonthlyActivity(
                    species_id=species_id,
                    location_id=location_id,
                    year=year,
                    month=month,
                    detection_count=detection_count,
                    trap_days=trap_days
                )
                new_activity_records.append(record)

        # Step 5: Save to DB.
        if new_activity_records:
            logging.info(f"Adding {len(new_activity_records)} records to the database (this may take a moment)...")
            session.bulk_save_objects(new_activity_records)
            session.commit()
            logging.info("Successfully saved all monthly activity records.")
        else:
            logging.info("No activity records to save.")

        return True

    except Exception as e:
        logging.error(f"An error occurred during monthly activity calculation: {e}", exc_info=True)
        session.rollback()
        return False
    finally:
        close_ct_session()

def _run_bootstrap(species_id, location_data, scope_locations, all_years, scope_type, scope_id, N_ITERATIONS):
    """Run bootstrap for one species and one location scope.

    Returns a list of SpeciesYearlyTrend objects.
    """
    if not scope_locations or not all_years:
        return []

    n = len(scope_locations)
    bootstrap_results = defaultdict(list)

    for _ in range(N_ITERATIONS):
        sampled = random.choices(scope_locations, k=n)
        for year in all_years:
            total_det = total_trap = 0
            for loc_id in sampled:
                det, trap = location_data.get(loc_id, {}).get(year, (0, 0))
                total_det += det
                total_trap += trap
            if total_trap > 0:
                bootstrap_results[year].append((total_det * 100) / total_trap)

    return [
        SpeciesYearlyTrend(
            species_id=species_id, year=year,
            scope_type=scope_type, scope_id=scope_id,
            mean_dr_index=float(np.mean(results)),
            lower_ci=float(np.percentile(results, 2.5)),
            upper_ci=float(np.percentile(results, 97.5))
        )
        for year, results in bootstrap_results.items() if results
    ]


def _calculate_yearly_trends_with_bootstrap():
    """Calculate yearly trends with bootstrap for three scopes:
      - global (all locations)
      - institution (separately for each institution)
      - ecoregion (for each ecoregion)
    """
    session = get_ct_session()
    N_ITERATIONS = 10000

    try:
        logging.info("Starting yearly trend calculation with bootstrap...")

        session.query(SpeciesYearlyTrend).delete()
        session.commit()

        # Load the location → institution mapping from ct_db.
        loc_inst_rows = session.execute(
            select(location_institutions.c.location_id, location_institutions.c.institution_id)
        ).fetchall()

        from collections import defaultdict as _dd
        inst_locations = _dd(set)   # {institution_id: {location_id, ...}}
        for loc_id, inst_id in loc_inst_rows:
            inst_locations[inst_id].add(loc_id)

        # Load institution ecoregions from the main DB.
        from app.models import Institution
        institutions = Institution.query.filter(Institution.ecoregion_uk.isnot(None)).all()
        eco_locations = _dd(set)    # {ecoregion_uk: {location_id, ...}}
        for inst in institutions:
            eco_locations[inst.ecoregion_uk].update(inst_locations.get(inst.id, set()))

        # All species.
        species_ids = [s[0] for s in session.query(LocationMonthlyActivity.species_id).distinct().all()]
        logging.info(f"Found {len(species_ids)} species, "
                     f"{len(inst_locations)} institutions, "
                     f"{len(eco_locations)} ecoregions.")

        final_trends = []

        for species_id in species_ids:
            logging.info(f"  Processing species ID: {species_id}...")

            yearly_rows = session.query(
                LocationMonthlyActivity.location_id,
                LocationMonthlyActivity.year,
                func.sum(LocationMonthlyActivity.detection_count).label('total_detections'),
                func.sum(LocationMonthlyActivity.trap_days).label('total_trap_days')
            ).filter(LocationMonthlyActivity.species_id == species_id)\
             .group_by(LocationMonthlyActivity.location_id, LocationMonthlyActivity.year)\
             .all()

            if not yearly_rows:
                continue

            location_data = _dd(dict)
            for row in yearly_rows:
                location_data[row.location_id][row.year] = (row.total_detections, row.total_trap_days)

            available_locs = set(location_data.keys())
            all_years = sorted(set(r.year for r in yearly_rows))

            # 1. Global
            final_trends.extend(_run_bootstrap(
                species_id, location_data, list(available_locs),
                all_years, 'global', '', N_ITERATIONS))

            # 2. Per institution
            for inst_id, locs in inst_locations.items():
                scope_locs = list(available_locs & locs)
                if scope_locs:
                    final_trends.extend(_run_bootstrap(
                        species_id, location_data, scope_locs,
                        all_years, 'institution', str(inst_id), N_ITERATIONS))

            # 3. Per ecoregion
            for eco_uk, locs in eco_locations.items():
                scope_locs = list(available_locs & locs)
                if scope_locs:
                    final_trends.extend(_run_bootstrap(
                        species_id, location_data, scope_locs,
                        all_years, 'ecoregion', eco_uk, N_ITERATIONS))

        if final_trends:
            logging.info(f"Saving {len(final_trends)} trend records...")
            session.bulk_save_objects(final_trends)
            session.commit()

        return True
    except Exception as e:
        logging.error(f"Error in bootstrap calculation: {e}", exc_info=True)
        session.rollback()
        return False
    finally:
        close_ct_session()

def update_analytics_tables(force_run=False):
    """Main entry point. Check whether a recalculation is needed and run both stages.

    Returns:
        True  — recalculation completed successfully (or skipped: no changes);
        False — one of the stages failed or an error occurred.
    Previously the function returned None and swallowed all errors, so the
    /admin/run-analytics call always reported success. The return value is now
    explicit — both the HTTP route and the background thread can set the status
    correctly.
    """
    session = get_ct_session()
    try:
        source_name = 'completed_observations'
        current_count = session.query(func.count(Observation.id))\
            .filter(Observation.status.in_(['completed', 'archived'])).scalar()
        log_entry = session.query(CalculationLog).filter_by(source_name=source_name).first()
        last_count = log_entry.last_count if log_entry else -1

        logging.info(f"Checking for updates. Current completed observations: {current_count}. Last recorded: {last_count}.")

        if not force_run and current_count == last_count:
            logging.info("No changes detected. Skipping calculation.")
            return True

        logging.info("Changes detected or force_run=True. Starting analytics calculation...")

        # Stage 1: Monthly activity calculation.
        success_monthly = _calculate_monthly_activity()
        if not success_monthly:
            logging.error("Monthly activity calculation failed. Aborting further calculations.")
            return False

        # Stage 2: Yearly trend calculation.
        success_yearly = _calculate_yearly_trends_with_bootstrap()
        if not success_yearly:
            logging.error("Yearly trend calculation failed. Log will not be updated.")
            return False

        # Stage 3: If everything succeeded, update the log.
        logging.info("All calculations successful. Updating log.")
        if log_entry:
            log_entry.last_count = current_count
            log_entry.last_calculated_at = datetime.utcnow()
        else:
            new_log_entry = CalculationLog(
                source_name=source_name,
                last_count=current_count,
                last_calculated_at=datetime.utcnow()
            )
            session.add(new_log_entry)

        session.commit()
        logging.info("Log updated successfully.")
        return True

    except Exception as e:
        logging.error(f"An error occurred in the main update function: {e}", exc_info=True)
        session.rollback()
        return False
    finally:
        close_ct_session()

# ─────────────────────────────────────────────────────────────────────────────
# ASYNC LAUNCH (threading; Celery replacement point in the future)
#
# Problem solved: update_analytics_tables() takes ~3 min on production data.
# Calling it synchronously from an HTTP request exceeded gunicorn --timeout →
# the worker was killed → 500. Now the route starts a background thread and
# returns immediately; the client polls for status. Mirrors the pattern used
# in cleanup.py / fast_upload.py.
# ─────────────────────────────────────────────────────────────────────────────

# Source name tracked in calculation_log (single analytics row).
ANALYTICS_SOURCE = 'completed_observations'
# A 'running' row older than this threshold is considered "stuck" (worker killed) —
# it can be restarted / cleared by recover_stuck_analytics().
ANALYTICS_STUCK_MINUTES = 30


def _ensure_log_row(conn) -> None:
    """Ensure the calculation_log row for ANALYTICS_SOURCE exists."""
    conn.execute(
        text("""
            INSERT INTO calculation_log (source_name, last_count, status)
            VALUES (:src, 0, 'idle')
            ON CONFLICT (source_name) DO NOTHING
        """),
        {"src": ANALYTICS_SOURCE},
    )


def try_start_analytics_run(triggered_by: Optional[int] = None) -> bool:
    """Atomically compare-and-set to "claim" the right to run a recalculation.

    A single UPSERT via ON CONFLICT ... WHERE: moves the row to 'running'
    ONLY if it is currently NOT 'running' (or is 'running' but stuck —
    started_at is older than ANALYTICS_STUCK_MINUTES). Works across workers
    (3 gunicorn workers) without an advisory lock: two simultaneous "Run"
    clicks are resolved at the DB level — only one gets rowcount==1.

    Returns True if this call claimed the run; False if a recalculation is
    already in progress.
    """
    engine = get_ct_engine()
    stuck_cutoff = datetime.utcnow() - timedelta(minutes=ANALYTICS_STUCK_MINUTES)
    with engine.begin() as conn:
        result = conn.execute(
            text("""
                INSERT INTO calculation_log
                    (source_name, last_count, status, started_at, error_message)
                VALUES
                    (:src, 0, 'running', NOW(), NULL)
                ON CONFLICT (source_name) DO UPDATE
                   SET status = 'running',
                       started_at = NOW(),
                       error_message = NULL
                 WHERE calculation_log.status IS DISTINCT FROM 'running'
                    OR calculation_log.started_at IS NULL
                    OR calculation_log.started_at < :cutoff
            """),
            {"src": ANALYTICS_SOURCE, "cutoff": stuck_cutoff},
        )
        # rowcount==1 → either a fresh INSERT or DO UPDATE fired (WHERE true).
        # rowcount==0 → conflict existed but WHERE was false → already 'running'.
        return (result.rowcount or 0) == 1


def _finish_analytics_run(status: str, error_message: Optional[str] = None) -> None:
    """Set the final status ('completed' / 'failed') after a background run.

    On 'completed', also update last_calculated_at = NOW() via raw SQL.
    Why not rely on the ORM update in update_analytics_tables: there,
    log_entry is read BEFORE the _calculate_* calls, each of which in its
    own finally calls close_ct_session() → scoped_session.remove(). After
    that log_entry is detached, and the ORM counter commit silently does
    nothing (the same lifecycle bug described in CLAUDE.md). A raw UPDATE
    here is reliable and needed so that the "Last successful recalculation"
    badge in the admin panel shows the correct time.
    last_count is not touched — that is separate (pre-existing) change-detection
    logic.
    """
    engine = get_ct_engine()
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE calculation_log
                   SET status = :st,
                       error_message = :err,
                       last_calculated_at = CASE
                           WHEN :st = 'completed' THEN NOW()
                           ELSE last_calculated_at
                       END
                 WHERE source_name = :src
            """),
            {"src": ANALYTICS_SOURCE, "st": status,
             "err": (error_message[:500] if error_message else None)},
        )


def _run_analytics_in_thread(app, triggered_by: Optional[int]) -> None:
    """Background thread body. Runs outside the HTTP context."""
    with app.app_context():
        try:
            ok = update_analytics_tables(force_run=True)
            if ok:
                _finish_analytics_run('completed')
            else:
                _finish_analytics_run(
                    'failed',
                    'Перерахунок завершився з помилкою — деталі у логах сервера.'
                )
        except Exception as e:
            current_app.logger.exception("[analytics] background run crashed")
            try:
                _finish_analytics_run('failed', str(e))
            except Exception:
                pass


def start_async_analytics(triggered_by: Optional[int] = None) -> bool:
    """Start a background analytics recalculation. Returns IMMEDIATELY.

    Returns:
        True  — run started in the background (thread created);
        False — a recalculation is already running, no new one started.

    NB: this layer is intentionally thin — so that in the future the body
    can be replaced with `recalc_analytics_task.delay()` (Celery) without
    changing the route or JS.
    """
    if not try_start_analytics_run(triggered_by):
        return False

    app = current_app._get_current_object()  # type: ignore[attr-defined]
    threading.Thread(
        target=_run_analytics_in_thread,
        args=(app, triggered_by),
        name="ct-analytics-recalc",
        daemon=True,
    ).start()
    current_app.logger.info(
        f"[analytics] started async recalculation (triggered_by={triggered_by})"
    )
    return True


def get_analytics_status() -> dict:
    """Return the current recalculation state for admin-page polling."""
    engine = get_ct_engine()
    with engine.begin() as conn:
        _ensure_log_row(conn)
        row = conn.execute(
            text("""
                SELECT status, started_at, last_calculated_at,
                       last_count, error_message
                  FROM calculation_log
                 WHERE source_name = :src
            """),
            {"src": ANALYTICS_SOURCE},
        ).first()

    if row is None:
        return {"status": "idle", "started_at": None, "last_calculated_at": None,
                "last_count": None, "error_message": None}

    return {
        "status": row.status or "idle",
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "last_calculated_at": row.last_calculated_at.isoformat() if row.last_calculated_at else None,
        "last_count": row.last_count,
        "error_message": row.error_message,
    }


def recover_stuck_analytics() -> int:
    """Call at app startup. If the worker was killed during 'running' — move the
    stuck row (started_at older than the threshold) to 'failed' so that the
    admin panel shows an error and the next click can start a fresh run.
    """
    try:
        engine = get_ct_engine()
        cutoff = datetime.utcnow() - timedelta(minutes=ANALYTICS_STUCK_MINUTES)
        with engine.begin() as conn:
            result = conn.execute(
                text("""
                    UPDATE calculation_log
                       SET status = 'failed',
                           error_message = COALESCE(error_message,
                               'Сервер перезапущено під час перерахунку; запустіть знову')
                     WHERE source_name = :src
                       AND status = 'running'
                       AND (started_at IS NULL OR started_at < :cutoff)
                """),
                {"src": ANALYTICS_SOURCE, "cutoff": cutoff},
            )
            n = result.rowcount or 0
            if n:
                current_app.logger.warning(
                    "[analytics] recovered stuck 'running' calculation_log row"
                )
            return n
    except Exception as e:
        try:
            current_app.logger.error(f"[analytics] recover_stuck_analytics failed: {e}")
        except Exception:
            pass
        return 0
    finally:
        close_ct_session()


if __name__ == '__main__':
    # This block allows running the file directly from the command line for testing.
    # The Flask app context must be properly configured for DB access.

    # Create a Flask app context first so SQLAlchemy knows which DB to connect to.
    from app import create_app
    app = create_app()
    with app.app_context():
        # Run the update with force_run=True for the first time.
        update_analytics_tables(force_run=True)
