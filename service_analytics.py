# SPDX-License-Identifier: AGPL-3.0-only
"""Per-location statistics: consensus-based observation counts → LocationStats."""

from datetime import datetime
from sqlalchemy import func, case, text
from flask import current_app

from .database import get_ct_session, close_ct_session
from .consensus import consensus_ctes
from .models import Location, Photo, Identification, Species, LocationStats, Observation, CalculationLog

def calculate_stats_for_location(location_id, db_session):
    """Compute and persist all statistics for a single location.

    Observation counts use consensus logic (the top-voted species per observation).
    """
    try:
        # --- 1. Basic photo metrics ---
        photo_stats = db_session.query(
            func.count(Photo.id),
            func.min(Photo.captured_at),
            func.max(Photo.captured_at)
        ).join(Observation).filter(Observation.location_id == location_id).one_or_none()

        if not photo_stats or not photo_stats[0]:
            # No photos → nothing to compute.
            return True

        total_photos, first_photo_date, last_photo_date = photo_stats
        
        duration_days = (last_photo_date - first_photo_date).total_seconds() / (24 * 3600)
        avg_photos_per_day = total_photos / duration_days if duration_days >= 1 else total_photos

        # --- 2. Consensus-based observation statistics ---
        # Raw SQL: more efficient for this windowed consensus logic.
        
        # Every accepted species of a series counts (several since multi-species
        # identification, consensus.py). Series are counted DISTINCT: a series
        # with a roe deer and a human is one animal series and one "other"
        # series, never two animal series. Ranking keeps its historic key
        # (votes only) plus species_id DESC, so a tie no longer resolves at random.
        consensus_query = text("WITH " + consensus_ctes(
            """FROM identifications i
                JOIN photos p ON i.photo_id = p.id
                WHERE p.observation_id IN (SELECT id FROM observations WHERE location_id = :location_id)""",
            'vote_count DESC, species_id DESC') + """
            SELECT
                COUNT(DISTINCT rc.observation_id) as total_observations,
                COUNT(DISTINCT s.id) FILTER (WHERE s.id > 0) as total_species,
                COUNT(DISTINCT rc.observation_id) FILTER (WHERE s.id > 0) as animal_observations,
                COUNT(DISTINCT rc.observation_id) FILTER (WHERE s.id = -1) as empty_observations,
                COUNT(DISTINCT rc.observation_id) FILTER (WHERE s.id < -1) as other_observations
            FROM RankedConsensus rc
            JOIN observations o ON rc.observation_id = o.id
            JOIN species s ON rc.species_id = s.id
            WHERE rc.accepted
              AND o.location_id = :location_id
              AND o.status IN ('completed', 'archived');
        """)
        
        conn = db_session.connection()
        observation_stats = conn.execute(consensus_query, {'location_id': location_id}).mappings().one()

        # --- 3. Upsert the LocationStats row ---
        stats_record = db_session.query(LocationStats).get(location_id)
        if not stats_record:
            stats_record = LocationStats(location_id=location_id)
            db_session.add(stats_record)
        
        stats_record.total_photos = total_photos
        stats_record.avg_photos_per_day = round(avg_photos_per_day, 2)
        stats_record.total_species = observation_stats['total_species']
        stats_record.animal_observations = observation_stats['animal_observations']
        stats_record.empty_observations = observation_stats['empty_observations']
        stats_record.other_observations = observation_stats['other_observations']
        stats_record.last_calculated_at = datetime.utcnow()
        
        return True

    except Exception as e:
        current_app.logger.error(f"Failed to calculate stats for location {location_id}: {e}", exc_info=True)
        db_session.rollback()
        return False

def update_all_location_stats(force_run=False):
    """Recompute stats for all locations; skip when there are no new completed observations."""
    ct_session = get_ct_session()
    try:
        # --- Skip if nothing changed since last run ---
        log_entry_name = 'completed_observations'
        current_completed_count = ct_session.query(Observation).filter(Observation.status.in_(['completed', 'archived'])).count()

        log_entry = ct_session.query(CalculationLog).filter_by(source_name=log_entry_name).first()
        if not log_entry:
            log_entry = CalculationLog(source_name=log_entry_name, last_count=0)
            ct_session.add(log_entry)
        
        if not force_run and log_entry.last_count == current_completed_count:
            print("No new completed observations since last calculation. Skipping.")
            current_app.logger.info("Skipping stats calculation: no new data.")
            return

        # --- Run the calculation ---
        print("Starting location stats calculation...")
        locations = ct_session.query(Location).all()
        processed_count = 0
        for loc in locations:
            if calculate_stats_for_location(loc.id, ct_session):
                processed_count += 1
        
        # Update the calculation log.
        log_entry.last_count = current_completed_count
        log_entry.last_calculated_at = datetime.utcnow()
        
        ct_session.commit()
        print(f"Successfully calculated stats for {processed_count}/{len(locations)} locations.")
        current_app.logger.info(f"Location stats updated for {processed_count} locations.")

    finally:
        close_ct_session()