"""Import external DeepFaune classification results (CSV) into ct_db.

Purpose
-------
The user runs DeepFaune locally (on CUDA, with the more accurate MDR detector —
MegaDetector Redwood) and obtains a CSV. The biomon server classifies with the
weaker ensemble DF+MDS. This module lets better local results be overlaid onto
photos ALREADY uploaded to biomon — one location at a time.

Key design decisions (agreed with the user)
------------------------------------------
* A separate model `DeepFaune 1.4.1 @ MDR` (is_active=False) — does not touch
  server predictions DF+MDS; both sets coexist (distinguished by
  `ai_models.level_id`).
* Per-photo matching: `basename(filename)` + `captured_at` (to the second)
  within the chosen location. `captured_at` in the DB has no sub-seconds,
  no collisions — the key is precise.
* In `ai_predictions` we write per-photo `base_label/base_score` (+ top1,
  counts) — a series-independent source of truth.
* Series-level `prediction_*` is NOT taken from the CSV (its series are 10 s),
  but recomputed from the current `observations` in the DB (rule: animal >
  human > empty; among animals — the highest base_score). Re-runnable when
  grouping changes.
* Label→species mapping comes from the `ai_label_map` reference table (single
  source of truth, shared with the worker).
"""

import csv
import io
import os
from datetime import datetime

from sqlalchemy import text, literal_column
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .models import AIModel, AIModelLevel, AIPrediction

IMPORT_MODEL_NAME = 'DeepFaune'
IMPORT_MODEL_VERSION = '1.4.1'
# The detector level is chosen by the user on the page (required) — it used to
# be hard-coded as MDR. The CSV does not contain the level, so it cannot be
# determined automatically.

# Special classes that are NOT animals (for series aggregation).
NON_ANIMAL_LABELS = {'empty', 'human', 'vehicle'}

CSV_DATE_FMT = '%Y:%m:%d %H:%M:%S'
# Only the truly required columns. The rest (top1, count, humancount) are
# optional: different DeepFaune exports/versions include different sets
# (e.g. without `count`).
REQUIRED_COLUMNS = {'filename', 'date', 'predictionbase', 'scorebase'}


# ──────────────────────────────────────────────────────────────────────────
# CSV parsing
# ──────────────────────────────────────────────────────────────────────────
def _to_float(v):
    try:
        return float(v) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _to_int(v):
    try:
        return int(float(v)) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def parse_deepfaune_csv(file_obj):
    """Parse a DeepFaune CSV file.

    Args:
        file_obj: file-like (binary or text), or bytes/str.

    Returns:
        (rows, errors): rows — list of dicts with normalised fields:
            original_filename (basename), captured_at (datetime to the second),
            base_label, base_score, top1_label, animal_count, human_count.
        errors — list of string messages about problematic rows.
    """
    if isinstance(file_obj, bytes):
        text_stream = io.StringIO(file_obj.decode('utf-8-sig'))
    elif isinstance(file_obj, str):
        text_stream = io.StringIO(file_obj)
    else:
        raw = file_obj.read()
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8-sig')
        text_stream = io.StringIO(raw)

    reader = csv.DictReader(text_stream)
    if reader.fieldnames is None:
        return [], ['Порожній або нечитабельний CSV']
    missing = REQUIRED_COLUMNS - {c.strip() for c in reader.fieldnames}
    if missing:
        return [], [f'Бракує колонок: {", ".join(sorted(missing))}']

    rows, errors = [], []
    for i, raw in enumerate(reader, start=2):  # row 1 is the header
        fn = (raw.get('filename') or '').strip()
        if not fn:
            continue
        date_str = (raw.get('date') or '').strip()
        try:
            captured = datetime.strptime(date_str, CSV_DATE_FMT).replace(microsecond=0)
        except ValueError:
            errors.append(f'рядок {i}: некоректна дата {date_str!r}')
            continue
        base_label = (raw.get('predictionbase') or '').strip().lower()
        rows.append({
            'original_filename': os.path.basename(fn.replace('\\', '/')),
            'captured_at': captured,
            'base_label': base_label or None,
            'base_score': _to_float(raw.get('scorebase')),
            'top1_label': (raw.get('top1') or '').strip().lower() or None,
            'animal_count': _to_int(raw.get('count')),
            'human_count': _to_int(raw.get('humancount')),
        })
    return rows, errors


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────
def get_import_levels(session):
    """Return levels available for IMPORT — all from ai_model_levels EXCEPT those
    already used by the active (server) model. This ensures that import never
    writes into the server set (e.g. DF+MDS) and only offers DF / MDS / MDR.

    Returns a list of AIModelLevel ordered by ascending accuracy_rank."""
    active_level_ids = [
        lid for (lid,) in session.query(AIModel.level_id).filter(AIModel.is_active.is_(True)).all()
        if lid is not None
    ]
    q = session.query(AIModelLevel)
    if active_level_ids:
        q = q.filter(~AIModelLevel.id.in_(active_level_ids))
    return q.order_by(AIModelLevel.accuracy_rank).all()


def get_or_create_import_model(session, level_id):
    """Return (creating if needed) an AIModel row for the chosen level.

    The level is required. Models are distinguished by (name, version, level_id),
    so each level gets its own ai_models row, all with is_active=False
    (to avoid touching the worker / active server model)."""
    if not level_id:
        raise ValueError('Не вказано рівень моделі')
    level = session.query(AIModelLevel).get(level_id)
    if level is None:
        raise ValueError('Невідомий рівень моделі')

    model = (session.query(AIModel)
             .filter_by(name=IMPORT_MODEL_NAME, version=IMPORT_MODEL_VERSION, level_id=level.id)
             .one_or_none())
    if model is not None and model.is_active:
        # Safety guard: never write into the active (server) model.
        raise ValueError('Цей рівень належить активній серверній моделі — імпорт заборонено')
    if model is None:
        model = AIModel(
            name=IMPORT_MODEL_NAME,
            version=IMPORT_MODEL_VERSION,
            level_id=level.id,
            is_active=False,
            config_json={'detector': level.detector, 'source': 'local-import', 'device': 'cuda'},
        )
        session.add(model)
        session.flush()
    return model


def load_label_map(session):
    """{label(lower) -> species_id|None} from the ai_label_map reference table."""
    rows = session.execute(text("SELECT label, species_id FROM ai_label_map")).fetchall()
    return {(lbl or '').strip().lower(): sid for lbl, sid in rows}


def _load_location_photo_index(session, location_id):
    """{(filename_lower, captured_naive_sec) -> (photo_id, observation_id)}.

    Only grouped photos (observation_id NOT NULL), because ai_predictions
    requires observation_id.
    """
    rows = session.execute(text("""
        SELECT p.id, p.original_filename, p.captured_at, p.observation_id
        FROM photos p
        JOIN observations o ON o.id = p.observation_id
        WHERE o.location_id = :loc
    """), {'loc': location_id}).fetchall()
    index = {}
    for pid, fn, captured, obs_id in rows:
        if not fn or captured is None:
            continue
        key = ((fn).strip().lower(), captured.replace(tzinfo=None, microsecond=0))
        index[key] = (pid, obs_id)
    return index


def _match(rows, photo_index):
    """Return (matched, csv_unmatched, db_keys_without_csv)."""
    matched, csv_unmatched = [], []
    seen = set()
    for r in rows:
        key = (r['original_filename'].strip().lower(), r['captured_at'])
        hit = photo_index.get(key)
        if hit:
            matched.append((r, hit[0], hit[1]))
            seen.add(key)
        else:
            csv_unmatched.append(r)
    db_without = [k for k in photo_index if k not in seen]
    return matched, csv_unmatched, db_without


def _dedupe_matched(matched):
    """Remove duplicate photo_ids (possible when the CSV has multiple rows with the
    same (filename, captured_at) — e.g. a burst with the same EXIF second:
    only one photo remained in the DB because duplicates are rejected on upload).

    Without this, a single INSERT...ON CONFLICT would fail with
    'cannot affect row a second time'. The LAST row wins.
    Returns (unique_list, n_duplicates)."""
    by_pid = {}
    for item in matched:
        by_pid[item[1]] = item          # item[1] == photo_id
    return list(by_pid.values()), len(matched) - len(by_pid)


# ──────────────────────────────────────────────────────────────────────────
# Preview (dry-run)
# ──────────────────────────────────────────────────────────────────────────
def preview_import(session, location_id, rows):
    index = _load_location_photo_index(session, location_id)
    matched, csv_unmatched, db_without = _match(rows, index)
    unique_matched, n_dup = _dedupe_matched(matched)
    # Placeholder photos (no EXIF timestamp): captured_at = 1900-01-01 + offset.
    # These will NEVER match the CSV — warn if any are present for this location.
    placeholder_photos = sum(1 for _fn, dt in index if dt.year == 1900)
    return {
        'csv_rows': len(rows),
        'location_photos': len(index),
        'matched': len(unique_matched),
        'csv_unmatched': len(csv_unmatched),
        'csv_duplicate_keys': n_dup,
        'db_without_prediction': len(db_without),
        'location_placeholder_photos': placeholder_photos,
        'sample_unmatched': [
            {'filename': r['original_filename'],
             'captured_at': r['captured_at'].strftime('%Y-%m-%d %H:%M:%S')}
            for r in csv_unmatched[:10]
        ],
    }


# ──────────────────────────────────────────────────────────────────────────
# Import
# ──────────────────────────────────────────────────────────────────────────
def run_import(session, location_id, rows, level_id, user_id=None, chunk_size=5000):
    """Write per-photo base predictions and recompute the series-level prediction.

    level_id — required detector level (chosen by the user on the page).
    Must be called WITHIN a transaction; the caller (route) issues the commit.
    """
    model = get_or_create_import_model(session, level_id)
    index = _load_location_photo_index(session, location_id)
    matched, csv_unmatched, _ = _match(rows, index)
    matched, n_dup = _dedupe_matched(matched)

    payload = [{
        'photo_id': pid,
        'observation_id': obs_id,
        'model_id': model.id,
        'base_label': r['base_label'],
        'base_score': r['base_score'],
        'top1_label': r['top1_label'],
        'top1_score': None,           # CSV does not include a separate top1_score
        'animal_count': r['animal_count'],
        'human_count': r['human_count'],
    } for (r, pid, obs_id) in matched]

    tbl = AIPrediction.__table__
    added = updated = skipped = 0
    for start in range(0, len(payload), chunk_size):
        chunk = payload[start:start + chunk_size]
        stmt = pg_insert(tbl).values(chunk)
        stmt = stmt.on_conflict_do_update(
            constraint='uq_ai_predictions_photo_model',
            set_={
                'observation_id': stmt.excluded.observation_id,
                'base_label': stmt.excluded.base_label,
                'base_score': stmt.excluded.base_score,
                'top1_label': stmt.excluded.top1_label,
                'top1_score': stmt.excluded.top1_score,
                'animal_count': stmt.excluded.animal_count,
                'human_count': stmt.excluded.human_count,
                'processed_at': text('now()'),
            },
            # Do not lower confidence: for the same level (model_id) we
            # overwrite only when the new base_score is HIGHER (or the existing
            # one is absent). Otherwise the conflicting row is skipped
            # (idempotent: re-uploading the same/worse CSV changes nothing).
            where=(tbl.c.base_score.is_(None)) | (stmt.excluded.base_score > tbl.c.base_score),
        ).returning(literal_column('(xmax = 0)').label('inserted'))
        # RETURNING only returns actually inserted/updated rows; skipped rows
        # (WHERE=false) are not returned. xmax=0 → freshly inserted.
        res = session.execute(stmt).fetchall()
        chunk_added = sum(1 for r in res if r.inserted)
        added += chunk_added
        updated += len(res) - chunk_added
        skipped += len(chunk) - len(res)

    affected_obs = {obs_id for (_, _, obs_id) in matched}
    recomputed = recompute_observation_predictions(session, model.id, affected_obs)

    return {
        'model_id': model.id,
        'matched': len(matched),
        'added': added,
        'updated': updated,
        'skipped_lower_score': skipped,
        'csv_unmatched': len(csv_unmatched),
        'csv_duplicate_keys': n_dup,
        'observations_recomputed': recomputed,
    }


def _aggregate_series(pairs):
    """Series aggregation rule: animal > human > empty.

    pairs — list of (base_label, base_score) for an observation.
    Returns (label, score).
    """
    animals = [(l, s if s is not None else 0.0) for l, s in pairs if l and l not in NON_ANIMAL_LABELS]
    if animals:
        return max(animals, key=lambda t: t[1])
    if any(l == 'human' for l, _ in pairs):
        humans = [s for l, s in pairs if l == 'human' and s is not None]
        return 'human', (max(humans) if humans else 1.0)
    if any(l == 'vehicle' for l, _ in pairs):
        return 'vehicle', 1.0
    return 'empty', 1.0


def recompute_observation_predictions(session, model_id, observation_ids=None):
    """Recompute the series-level prediction_* from the current observations in the DB.

    Reads per-photo base_label/base_score (source of truth), aggregates using
    the animal>human>empty rule, maps label→species_id via ai_label_map, and
    writes the same prediction_* to all ai_predictions rows of that series.

    observation_ids=None → recompute ALL series for this model.
    Returns the number of processed observations.
    """
    label_map = load_label_map(session)

    params = {'mid': model_id}
    where = "model_id = :mid"
    if observation_ids is not None:
        ids = list(observation_ids)
        if not ids:
            return 0
        where += " AND observation_id = ANY(:ids)"
        params['ids'] = ids

    rows = session.execute(
        text(f"SELECT observation_id, base_label, base_score FROM ai_predictions WHERE {where}"),
        params,
    ).fetchall()

    by_obs = {}
    for obs_id, base_label, base_score in rows:
        by_obs.setdefault(obs_id, []).append((base_label, base_score))

    updates = []
    for obs_id, pairs in by_obs.items():
        label, score = _aggregate_series(pairs)
        updates.append({
            'obs': obs_id,
            'mid': model_id,
            'label': label,
            'score': score,
            'sid': label_map.get((label or '').strip().lower()),
        })

    if updates:
        session.execute(text("""
            UPDATE ai_predictions
               SET prediction_label = :label,
                   prediction_score = :score,
                   prediction_species_id = :sid
             WHERE observation_id = :obs AND model_id = :mid
        """), updates)

    return len(updates)
