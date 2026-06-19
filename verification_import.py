# SPDX-License-Identifier: AGPL-3.0-only
"""Import HUMAN-VERIFIED DeepFaune classifications into ct_db as identifications.

This is the "level 4" import mode, distinct from the AI-prediction import in
`classification_import.py` (which writes per-photo auto predictions into
`ai_predictions`). Here the CSV rows have already been manually corrected by a
person, so every row is treated as a human verification and written into
`identifications`, credited to a chosen verifier (a real system user).

Decisions (agreed with the user — see the Notion task):
* A. Per-photo → per-series = MODE. For each pending series with >=1 matched
     row, take the most frequent mapped species_id among the matched photos.
     Tie-break: real species (id>0) before special (id<0); then highest summed
     base_score; then smaller species_id. That single species is written as the
     verifier's identification on ALL photos of the series (like
     submit_identification).
* B. Verifier: a real system user, chosen from a dropdown; all verifications are
     credited to them. On conflict (photo_id, user_id) the species_id is updated
     (idempotent re-import of the same file).
* C. quantity = NULL (the verifier did not count); comment = NULL; behaviors
     untouched (DeepFaune does not provide them).
* D. Special labels (empty/human/vehicle) map via `ai_label_map` as-is.

CRITICAL: series with status 'completed' or 'archived' (already at consensus)
are skipped entirely — after consensus the photos may have been removed, so a
conflicting verification cannot be checked. Only 'pending' series are imported.

The AI-prediction import flow is NOT touched by this module.
"""
from collections import defaultdict

from sqlalchemy import text

from .models import Observation, Photo, Identification
from .classification_import import (
    _load_location_photo_index,
    _match,
    load_label_map,
)
from .utils import check_consensus_for_observation


def _group_matched_by_observation(matched):
    """matched: list of (row, photo_id, observation_id) → {obs_id: [(row, photo_id), ...]}."""
    by_obs = defaultdict(list)
    for row, photo_id, obs_id in matched:
        by_obs[obs_id].append((row, photo_id))
    return by_obs


def _series_mode_species(rows_for_obs, label_map):
    """Pick one species_id for a series from its matched rows (decision A).

    Returns (species_id, is_ambiguous) or (None, False) when no row maps to a
    species. ``is_ambiguous`` is True when several species tie on vote count
    (resolved by the tie-break, but worth reporting in the preview).
    """
    counts = defaultdict(int)
    scores = defaultdict(float)
    for row, _photo_id in rows_for_obs:
        sid = label_map.get((row.get('base_label') or '').strip().lower())
        if sid is None:
            continue
        counts[sid] += 1
        scores[sid] += (row.get('base_score') or 0.0)

    if not counts:
        return None, False

    max_votes = max(counts.values())
    candidates = [sid for sid, c in counts.items() if c == max_votes]
    is_ambiguous = len(candidates) > 1
    # Tie-break: real species (id>0) first, then highest summed score, then smaller id.
    candidates.sort(key=lambda sid: (0 if sid > 0 else 1, -scores[sid], sid))
    return candidates[0], is_ambiguous


def _statuses_for(session, obs_ids):
    """{observation_id -> status} for the given ids."""
    if not obs_ids:
        return {}
    rows = session.execute(
        text("SELECT id, status FROM observations WHERE id = ANY(:ids)"),
        {'ids': list(obs_ids)},
    ).fetchall()
    return {oid: status for oid, status in rows}


_SKIP_STATUSES = {'completed', 'archived'}


def preview_verification_import(session, location_id, rows):
    """Dry-run statistics for the human-verification import."""
    label_map = load_label_map(session)
    index = _load_location_photo_index(session, location_id)
    matched, csv_unmatched, _ = _match(rows, index)
    by_obs = _group_matched_by_observation(matched)
    statuses = _statuses_for(session, by_obs.keys())

    pending_series = skipped_series = ambiguous_series = no_species_series = 0
    matched_photos_pending = 0
    for obs_id, rows_for_obs in by_obs.items():
        if statuses.get(obs_id) in _SKIP_STATUSES:
            skipped_series += 1
            continue
        pending_series += 1
        matched_photos_pending += len(rows_for_obs)
        sid, ambiguous = _series_mode_species(rows_for_obs, label_map)
        if sid is None:
            no_species_series += 1
        elif ambiguous:
            ambiguous_series += 1

    return {
        'csv_rows': len(rows),
        'location_photos': len(index),
        'matched_photos': sum(len(v) for v in by_obs.values()),
        'matched_photos_pending': matched_photos_pending,
        'pending_series': pending_series,
        'skipped_consensus_series': skipped_series,
        'ambiguous_series': ambiguous_series,
        'no_species_series': no_species_series,
        'csv_unmatched': len(csv_unmatched),
        'sample_unmatched': [
            {'filename': r['original_filename'],
             'captured_at': r['captured_at'].strftime('%Y-%m-%d %H:%M:%S')}
            for r in csv_unmatched[:10]
        ],
    }


def run_verification_import(session, location_id, rows, verifier_user_id):
    """Write human verifications into `identifications`, credited to verifier_user_id.

    Only 'pending' series are written; 'completed'/'archived' are skipped.
    One species (mode) per series, on all of its photos. quantity/comment NULL.
    Idempotent: re-importing updates species_id on conflict (photo_id, user_id).
    Must run within a transaction; the caller (route) commits.
    """
    if not verifier_user_id:
        raise ValueError('Не вказано верифікатора')

    label_map = load_label_map(session)
    index = _load_location_photo_index(session, location_id)
    matched, csv_unmatched, _ = _match(rows, index)
    by_obs = _group_matched_by_observation(matched)
    statuses = _statuses_for(session, by_obs.keys())

    pending_ids = [oid for oid in by_obs if statuses.get(oid) not in _SKIP_STATUSES]
    skipped_consensus = sum(1 for oid in by_obs if statuses.get(oid) in _SKIP_STATUSES)

    observations = {
        o.id: o for o in session.query(Observation)
        .filter(Observation.id.in_(pending_ids)).all()
    } if pending_ids else {}

    series_written = no_species = ids_added = ids_updated = 0
    consensus_reached = 0

    for obs_id in pending_ids:
        obs = observations.get(obs_id)
        if obs is None:
            continue
        species_id, _ambiguous = _series_mode_species(by_obs[obs_id], label_map)
        if species_id is None:
            no_species += 1
            continue

        for photo in obs.photos:
            existing = (session.query(Identification)
                        .filter_by(user_id=verifier_user_id, photo_id=photo.id)
                        .first())
            if existing is None:
                session.add(Identification(
                    photo_id=photo.id,
                    user_id=verifier_user_id,
                    species_id=species_id,
                    quantity=None,   # the verifier did not count (decision C)
                    comment=None,
                ))
                photo.identification_count = (photo.identification_count or 0) + 1
                ids_added += 1
            elif existing.species_id != species_id:
                existing.species_id = species_id  # idempotent re-import (decision B)
                ids_updated += 1

        session.flush()
        status_before = obs.status
        check_consensus_for_observation(obs_id, db_session=session)
        if status_before == 'pending' and obs.status == 'completed':
            consensus_reached += 1
        series_written += 1

    return {
        'verifier_user_id': verifier_user_id,
        'matched_photos': sum(len(v) for v in by_obs.values()),
        'series_written': series_written,
        'skipped_consensus_series': skipped_consensus,
        'no_species_series': no_species,
        'identifications_added': ids_added,
        'identifications_updated': ids_updated,
        'consensus_reached': consensus_reached,
        'csv_unmatched': len(csv_unmatched),
    }
