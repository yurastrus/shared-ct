# SPDX-License-Identifier: AGPL-3.0-only
"""Flask-side helpers for the AI runner.

Flask imports nothing from services/biomon_ai/ — that lives in a separate venv.
This module only reads/writes the ai_* tables via the main CT engine.

Exports:
    is_ai_available()      — feature flag for the template (button and filter visibility)
    request_run()          — create a request in ai_run_queue (admin button)
    get_recent_requests()  — show recent requests with their statuses
    get_active_model()     — which classifier is currently active
"""

from typing import Optional

from flask import current_app
from sqlalchemy import inspect, text
from sqlalchemy.exc import ProgrammingError

from .database import get_ct_engine, get_ct_session
from .models import AIModel, AIModelLevel, AIRunQueue


_AI_TABLES = ('ai_models', 'ai_predictions', 'ai_run_queue')

# Process-lifetime cache — avoids querying information_schema on every request.
_tables_checked: bool = False
_tables_exist: bool = False


def is_ai_available() -> bool:
    """Return True if the AI runner is configured and reachable from Flask.

    Checks:
      1. config AI_RUNNER.ENABLED (always False on a dev machine)
      2. All 3 ai_* tables exist in ct_db (also False on servers where the
         camera-traps module is installed but the AI schema has not been applied)

    The table check is cached (runs once per process lifetime).
    """
    cfg = (current_app.config.get('CAMERA_TRAP_CONFIG') or {}).get('AI_RUNNER') or {}
    if not cfg.get('ENABLED', False):
        return False

    return _ai_tables_exist()


def _ai_tables_exist() -> bool:
    global _tables_checked, _tables_exist
    if _tables_checked:
        return _tables_exist

    try:
        engine = get_ct_engine()
        insp = inspect(engine)
        existing = set(insp.get_table_names())
        _tables_exist = all(t in existing for t in _AI_TABLES)
    except Exception as e:
        current_app.logger.warning(f"AI: cannot inspect ct_db schema: {e}")
        _tables_exist = False

    _tables_checked = True
    return _tables_exist


def _reset_cache():
    """Force a re-check of the tables (for tests)."""
    global _tables_checked, _tables_exist
    _tables_checked = False
    _tables_exist = False


# ─────────────────────────────────────────────────────────────────────
# Pause lease for the AI worker during uploads
# ─────────────────────────────────────────────────────────────────────
# Classification is a heavy cron job (loads a ~2 GB ViT, ~30 s, sustained CPU).
# When a large camera-trap upload runs it competed for DB/CPU/RAM and made
# uploads slow and error out. So Flask sets a short "pause lease" in
# ai_control.pause_until while an upload is in progress and keeps refreshing it;
# the worker skips its run while the lease is in the future. If the uploader
# process dies, the lease expires on its own and the worker resumes — no manual
# intervention and no stuck-forever pause.

AI_PAUSE_UPLOAD_TTL_MIN = 10    # lease while photos upload (refreshed by activity)
AI_PAUSE_GROUPING_TTL_MIN = 35  # lease covering background grouping (≥ 30-min stale threshold)

# Process-lifetime flag: once we learn ai_control is absent (AI schema not yet
# applied on this server), stop hammering the DB / log on every uploaded photo.
_ai_control_missing: bool = False


def _pause_enabled() -> bool:
    """Pausing is a no-op unless the AI runner is configured (is_ai_available,
    cached) AND the ai_control table exists. The missing-table flag avoids
    retrying a failing UPDATE on every uploaded photo (e.g. on a dev machine or
    in the window between code deploy and running scripts.init_ai_tables)."""
    if _ai_control_missing:
        return False
    return is_ai_available()


def pause_ai_classification(ttl_minutes: int = AI_PAUSE_UPLOAD_TTL_MIN,
                            reason: str = 'upload') -> None:
    """Set/extend the pause lease to NOW()+ttl. Idempotent; creates the singleton
    row if missing. NEVER raises into the caller — a failure here must not break
    an upload (the lease exists to *help* uploads, not add a failure mode)."""
    if not _pause_enabled():
        return
    global _ai_control_missing
    try:
        engine = get_ct_engine()
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO ai_control (id, pause_until, pause_reason, updated_at)
                VALUES (1, NOW() + (INTERVAL '1 minute' * :ttl), :reason, NOW())
                ON CONFLICT (id) DO UPDATE
                   SET pause_until  = EXCLUDED.pause_until,
                       pause_reason = EXCLUDED.pause_reason,
                       updated_at   = NOW()
            """), {'ttl': ttl_minutes, 'reason': reason})
    except ProgrammingError:
        _ai_control_missing = True
        current_app.logger.warning(
            "AI pause: ai_control table is missing — pausing disabled "
            "(run `python -m scripts.init_ai_tables` to enable)."
        )
    except Exception as e:
        current_app.logger.warning(f"AI pause: could not set lease: {e}")


def heartbeat_ai_pause(ttl_minutes: int = AI_PAUSE_UPLOAD_TTL_MIN) -> None:
    """Refresh the lease ONLY if it is past its half-life. Called on every
    uploaded photo, so it must be near-free: the WHERE is a single-row PK lookup
    and an actual write happens at most ~once per (ttl/2) minutes. The lease row
    is already armed by create_batch → pause_ai_classification(), so a plain
    UPDATE (no upsert) is enough here."""
    if not _pause_enabled():
        return
    try:
        engine = get_ct_engine()
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE ai_control
                   SET pause_until = NOW() + (INTERVAL '1 minute' * :ttl),
                       updated_at  = NOW()
                 WHERE id = 1
                   AND (pause_until IS NULL
                        OR pause_until < NOW() + (INTERVAL '1 minute' * :half))
            """), {'ttl': ttl_minutes, 'half': ttl_minutes / 2.0})
    except Exception:
        # Best-effort heartbeat: create_batch already armed the lease, and the
        # TTL is the real backstop. Swallow to never disturb the upload.
        pass


def resume_ai_classification(force: bool = False) -> None:
    """Clear the pause lease so the worker can resume immediately.

    By default this is *conditional*: it clears only if no other upload batch is
    still active (uploading / ready_to_group / grouping). This prevents one
    finished upload from un-pausing classification while a concurrent upload is
    still running. The lease TTL remains the ultimate backstop for crashed or
    abandoned uploads — it expires on its own even if this is never called."""
    if not _pause_enabled():
        return
    try:
        engine = get_ct_engine()
        with engine.begin() as conn:
            if force:
                conn.execute(text(
                    "UPDATE ai_control SET pause_until=NULL, pause_reason=NULL, "
                    "updated_at=NOW() WHERE id=1"
                ))
            else:
                conn.execute(text("""
                    UPDATE ai_control
                       SET pause_until=NULL, pause_reason=NULL, updated_at=NOW()
                     WHERE id=1
                       AND NOT EXISTS (
                           SELECT 1 FROM upload_batches
                           WHERE status IN ('uploading', 'ready_to_group', 'grouping')
                       )
                """))
    except Exception as e:
        current_app.logger.warning(f"AI pause: could not clear lease: {e}")


def is_ai_paused() -> bool:
    """True if classification is currently paused (lease in the future). For
    admin/status display. Safe: returns False on any error."""
    if _ai_control_missing:
        return False
    try:
        engine = get_ct_engine()
        with engine.connect() as conn:
            return bool(conn.execute(text(
                "SELECT pause_until IS NOT NULL AND pause_until > NOW() "
                "FROM ai_control WHERE id = 1"
            )).scalar())
    except Exception:
        return False


def get_ai_pause_status() -> dict:
    """Detail for the admin live indicator: whether classification is paused,
    why, and how many seconds the lease still has. Safe: returns paused=False on
    any error (missing table, DB hiccup) so the indicator degrades gracefully.

    Shape: {'paused': bool, 'reason': str|None, 'seconds_left': int|None}."""
    off = {'paused': False, 'reason': None, 'seconds_left': None}
    if _ai_control_missing:
        return off
    try:
        engine = get_ct_engine()
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT pause_reason,
                       (pause_until IS NOT NULL AND pause_until > NOW()) AS paused,
                       EXTRACT(EPOCH FROM (pause_until - NOW()))         AS secs_left
                  FROM ai_control WHERE id = 1
            """)).fetchone()
        if row is None or not row.paused:
            return off
        return {
            'paused': True,
            'reason': row.pause_reason,
            'seconds_left': int(row.secs_left) if row.secs_left is not None else None,
        }
    except Exception:
        return off


def request_run(user_id: int, n_observations: int) -> AIRunQueue:
    """Create a record in ai_run_queue with status 'pending'.

    The worker (cron) will pick it up on the next pass. Returns the created
    object (not detached from the session — the caller must commit OR use a
    separate context manager).
    """
    sess = get_ct_session()
    req = AIRunQueue(
        requested_by=user_id,
        n_observations=n_observations,
        status='pending',
    )
    sess.add(req)
    sess.commit()
    sess.refresh(req)
    return req


def get_recent_requests(limit: int = 5) -> list:
    """Return the most recent requests for status display on the admin page."""
    sess = get_ct_session()
    return (
        sess.query(AIRunQueue)
        .order_by(AIRunQueue.requested_at.desc())
        .limit(limit)
        .all()
    )


def get_active_model() -> Optional[AIModel]:
    """Return the currently active AI model, or None.

    None means the worker has not run yet or no model has been registered.
    """
    sess = get_ct_session()
    return sess.query(AIModel).filter_by(is_active=True).first()


def get_classification_stats() -> dict:
    """Return overall classification progress statistics for the admin page.

    Returns:
        {'classified': N, 'remaining': M}
          classified — observations with predictions from the active model (any status)
          remaining  — pending observations WITHOUT predictions that have at least
                       one live (non-archived) photo — i.e. real AI candidates.

    If no active model exists yet → both figures are 0.
    """
    from sqlalchemy import text
    sess = get_ct_session()
    active = sess.query(AIModel).filter_by(is_active=True).first()
    if active is None:
        return {'classified': 0, 'remaining': 0}

    classified = sess.execute(text("""
        SELECT COUNT(DISTINCT observation_id) FROM ai_predictions
        WHERE model_id = :mid
    """), {'mid': active.id}).scalar() or 0

    remaining = sess.execute(text("""
        SELECT COUNT(*) FROM observations o
        WHERE o.status = 'pending'
          AND NOT EXISTS (
              SELECT 1 FROM ai_predictions ap
              WHERE ap.observation_id = o.id AND ap.model_id = :mid
          )
          -- Selectivity (as in worker pick_pending_observations): do not count
          -- series that already have a better/equal local classification.
          AND NOT EXISTS (
              SELECT 1 FROM ai_predictions ap2
              JOIN ai_models m2 ON m2.id = ap2.model_id
              LEFT JOIN ai_model_levels l2 ON l2.id = m2.level_id
              WHERE ap2.observation_id = o.id
                AND COALESCE(l2.accuracy_rank, 0) >= (
                    SELECT COALESCE(l.accuracy_rank, 0)
                    FROM ai_models m
                    LEFT JOIN ai_model_levels l ON l.id = m.level_id
                    WHERE m.id = :mid
                )
          )
          AND EXISTS (
              SELECT 1 FROM photos p
              WHERE p.observation_id = o.id
                AND p.status IN ('grouped', 'pending', 'completed')
          )
    """), {'mid': active.id}).scalar() or 0

    return {'classified': int(classified), 'remaining': int(remaining)}


def get_species_with_ai_predictions(
    lang_code: str = 'uk',
    user_id: Optional[int] = None,
    user_inst_ids: Optional[list] = None,
    is_admin: bool = False,
    scope_institution_id: Optional[int] = None,
    scope_institution_ids: Optional[list] = None,
) -> list:
    """Return [(species_id, display_name)] for species that have **pending for
    this user** AI predictions from the active model. That is:
      - observation.status='pending'
      - the user has not yet identified any photo in this series
      - the location is accessible to the user (admin — all; otherwise
        visibility_level=0 OR the location belongs to the user's institutions)

    display_name includes the pending series count in parentheses, e.g.
    "Козуля (Capreolus capreolus) (42)".

    If user_id=None — returns all species without a user filter
    (for tests / debug).

    The `scope_institution_id` / `scope_institution_ids` parameters further
    narrow the list to locations belonging to the chosen institution(s). Both
    are mutually exclusive — if both are passed, the single-institution
    `scope_institution_id` takes precedence.

    `scope_institution_ids` is an already-resolved, access-checked list of
    institution IDs (e.g. an ecoregion expanded to its institutions). The
    resolution is done by the caller, which has access to the main-DB
    `Institution` model — this module only has the CT engine, and the
    `institutions` table lives in the main DB, NOT in ct_db (querying it here
    would raise `relation "institutions" does not exist`).
    """
    from sqlalchemy import bindparam, text as sql_text
    sess = get_ct_session()
    # Previously filtered by the active model. Now we take the prediction from
    # the model with the HIGHEST accuracy_rank per observation (e.g. an imported
    # MDR beats the server DF+MDS where both exist). If no model exists —
    # AI is not yet configured.
    if sess.query(AIModel.id).first() is None:
        return []

    # Build the access clause (same logic as next_observation_for_identification).
    if is_admin:
        access_clause = ""
        access_params = {}
    elif user_inst_ids:
        access_clause = """
            AND (l.visibility_level = 0 OR EXISTS (
                SELECT 1 FROM location_institutions li
                WHERE li.location_id = l.id
                  AND li.institution_id IN :inst_ids
            ))
        """
        access_params = {'inst_ids': tuple(user_inst_ids)}
    else:
        access_clause = "AND l.visibility_level = 0"
        access_params = {}

    user_clause = ""
    user_params = {}
    if user_id is not None:
        user_clause = """
            AND NOT EXISTS (
                SELECT 1 FROM photos pu
                JOIN identifications i ON i.photo_id = pu.id
                WHERE pu.observation_id = o.id AND i.user_id = :uid
            )
        """
        user_params = {'uid': user_id}

    # Scope filter: narrow down to a specific institution or ecoregion.
    # Implemented as a subquery on location_institutions/institutions —
    # same semantics as next_observation_for_identification.
    scope_clause = ""
    scope_params = {}
    if scope_institution_id is not None:
        if is_admin or (user_inst_ids and scope_institution_id in user_inst_ids):
            scope_clause = """
                AND EXISTS (
                    SELECT 1 FROM location_institutions li_sc
                    WHERE li_sc.location_id = l.id
                      AND li_sc.institution_id = :scope_inst_id
                )
            """
            scope_params = {'scope_inst_id': scope_institution_id}
        else:
            # User has no access to this institution — return empty.
            return []
    elif scope_institution_ids is not None:
        # Pre-resolved, access-checked list of institutions (e.g. an ecoregion
        # expanded to its institutions by the caller). An empty list means no
        # accessible institution matched → no results.
        if not scope_institution_ids:
            return []
        scope_clause = """
            AND EXISTS (
                SELECT 1 FROM location_institutions li_sc
                WHERE li_sc.location_id = l.id
                  AND li_sc.institution_id IN :scope_inst_ids
            )
        """
        scope_params = {'scope_inst_ids': tuple(scope_institution_ids)}

    # win — one winning row per observation: prediction from the model with the
    # highest accuracy_rank (tie-break: newer model.id). COALESCE(...,0) —
    # models without a level are treated as the lowest rank.
    sql = sql_text(f"""
        WITH win AS (
            SELECT DISTINCT ON (ap.observation_id)
                   ap.observation_id,
                   ap.prediction_species_id
            FROM ai_predictions ap
            JOIN observations o2 ON o2.id = ap.observation_id AND o2.status = 'pending'
            JOIN ai_models m ON m.id = ap.model_id
            LEFT JOIN ai_model_levels lvl ON lvl.id = m.level_id
            ORDER BY ap.observation_id, COALESCE(lvl.accuracy_rank, 0) DESC, m.id DESC
        )
        SELECT s.id,
               s.common_name_ua,
               s.common_name_en,
               s.scientific_name,
               COUNT(DISTINCT win.observation_id) AS pending_count
        FROM win
        JOIN species s ON s.id = win.prediction_species_id
        JOIN observations o ON o.id = win.observation_id
        JOIN locations l ON l.id = o.location_id
        WHERE o.status = 'pending'
          {user_clause}
          {access_clause}
          {scope_clause}
        GROUP BY s.id, s.common_name_ua, s.common_name_en, s.scientific_name
        HAVING COUNT(DISTINCT win.observation_id) > 0
        ORDER BY s.common_name_ua
    """)

    # IN-clause with a dynamic list requires an expanding bindparam.
    expanding = []
    if 'inst_ids' in access_params:
        expanding.append(bindparam('inst_ids', expanding=True))
    if 'scope_inst_ids' in scope_params:
        expanding.append(bindparam('scope_inst_ids', expanding=True))
    if expanding:
        sql = sql.bindparams(*expanding)

    rows = sess.execute(sql, {
        **user_params,
        **access_params,
        **scope_params,
    }).fetchall()

    result = []
    for s in rows:
        if lang_code == 'en':
            name = s.common_name_en or s.common_name_ua or s.scientific_name
        else:
            name = s.common_name_ua or s.common_name_en or s.scientific_name
        if s.id > 0 and s.scientific_name:
            name = f"{name} ({s.scientific_name})"
        # Append the pending count in parentheses.
        name = f"{name} ({s.pending_count})"
        result.append({'id': s.id, 'text': name})
    return result


def get_observation_ai_prediction(observation_id: int) -> Optional[dict]:
    """Return the best AI prediction for an observation, or None.

    If there are predictions from multiple models for the series (e.g. server
    DF+MDS and imported MDR), we pick the one from the model with the HIGHEST
    accuracy_rank; within a model — the row with the highest score. If
    prediction_species_id IS NULL (no mapping to our Species) — return only
    the raw label.

    Return structure:
        {
            'species_id':       int or None,
            'species_label':    str (DeepFaune raw label, e.g. 'roe deer'),
            'score':            float (0..1),
            'animal_count':     int,
        }
    """
    from sqlalchemy import func
    from .models import AIPrediction

    sess = get_ct_session()

    row = (
        sess.query(AIPrediction)
        .join(AIModel, AIModel.id == AIPrediction.model_id)
        .outerjoin(AIModelLevel, AIModelLevel.id == AIModel.level_id)
        .filter(AIPrediction.observation_id == observation_id)
        .order_by(
            func.coalesce(AIModelLevel.accuracy_rank, 0).desc(),
            AIPrediction.prediction_score.desc().nullslast(),
        )
        .first()
    )
    if row is None:
        return None

    # #35: individual count for the series = MAX(animal_count) within the WINNING
    # model (same model_id) across all photos in the series. ai_predictions is
    # per-photo, so two individuals could appear on only one frame; we take the
    # maximum (NULL is ignored by SQL MAX). If all are NULL → fall back to
    # row.animal_count.
    max_count = (
        sess.query(func.max(AIPrediction.animal_count))
        .filter(
            AIPrediction.observation_id == observation_id,
            AIPrediction.model_id == row.model_id,
        )
        .scalar()
    )

    return {
        'species_id':    row.prediction_species_id,
        'species_label': row.prediction_label,
        'score':         row.prediction_score,
        'animal_count':  max_count if max_count is not None else row.animal_count,
    }


def observations_subq_for_ai_species(ai_species_id: int):
    """SQLAlchemy select(observation_id) for series where the WINNING prediction
    (model with the highest accuracy_rank, tie-break — newer model.id) identified
    species ai_species_id.

    Consistent with get_species_with_ai_predictions / get_observation_ai_prediction:
    if a series has predictions from multiple models (DF+MDS + imported MDR),
    only the most accurate one is considered the "winner" — so the AI filter on
    /identify shows exactly the series listed in the species reference."""
    from sqlalchemy import select, func
    from .models import AIPrediction

    win = (
        select(
            AIPrediction.observation_id.label('observation_id'),
            AIPrediction.prediction_species_id.label('species_id'),
        )
        .join(AIModel, AIModel.id == AIPrediction.model_id)
        .outerjoin(AIModelLevel, AIModelLevel.id == AIModel.level_id)
        .distinct(AIPrediction.observation_id)
        .order_by(
            AIPrediction.observation_id,
            func.coalesce(AIModelLevel.accuracy_rank, 0).desc(),
            AIModel.id.desc(),
        )
        .subquery()
    )
    return select(win.c.observation_id).where(win.c.species_id == ai_species_id)
