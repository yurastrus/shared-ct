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
from sqlalchemy import inspect

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
    scope_ecoregion: Optional[str] = None,
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

    The `scope_institution_id` / `scope_ecoregion` parameters further narrow
    the list to locations belonging to the chosen institution or ecoregion
    (among the user's institutions, unless admin). The parameters are mutually
    exclusive — if both are passed, institution takes precedence.
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
    elif scope_ecoregion:
        # Institutions that belong to the chosen ecoregion (by uk key).
        eco_q = sess.execute(sql_text("""
            SELECT id FROM institutions WHERE ecoregion_uk = :eco
        """), {'eco': scope_ecoregion}).fetchall()
        eco_inst_ids = [r[0] for r in eco_q]
        if not is_admin and user_inst_ids:
            eco_inst_ids = [i for i in eco_inst_ids if i in user_inst_ids]
        elif not is_admin:
            eco_inst_ids = []
        if not eco_inst_ids:
            return []
        scope_clause = """
            AND EXISTS (
                SELECT 1 FROM location_institutions li_sc
                WHERE li_sc.location_id = l.id
                  AND li_sc.institution_id IN :scope_inst_ids
            )
        """
        scope_params = {'scope_inst_ids': tuple(eco_inst_ids)}

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
