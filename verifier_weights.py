# SPDX-License-Identifier: AGPL-3.0-only
"""People's records per species and the AI's series answers for the weighted
closing rule (consensus.weighted_close, owner's decision 2026-11-23).

The table ``verifier_species_weights`` holds (user, species, n, confirmed)
for every person and species, graded against the other people of each series
(consensus.grade_votes). It is rebuilt as a whole:
  - by itself, in a background thread, when the newest row is older than
    VERIFIER_WEIGHTS_MAX_AGE_HOURS (checked at most every few minutes per
    process; a Postgres advisory lock lets only one worker of all hosts run it);
  - or by hand: ``python -m scripts.refresh_verifier_weights``.

When the table does not exist or is empty, ``load_weights`` returns None and
the caller falls back to the majority rule, so a host that has not run
scripts/init_verifier_weights.py keeps working exactly as before.
"""
import threading
import time
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import func, inspect, text

from .consensus import EXPERIENCE_K_DEFAULT, grade_votes, pick_series_ai, verifier_weight

TABLE = 'verifier_species_weights'
LOCK_KEY = 72_310_517          # arbitrary, constant: pg advisory lock for refresh()
AVAILABILITY_TTL = 300         # seconds a has-table / has-rows answer is reused
REFRESH_CHECK_EVERY = 600      # seconds between staleness checks per process
MAX_AGE_HOURS_DEFAULT = 24

VOTES_SQL = """
SELECT DISTINCT p.observation_id, i.user_id, i.species_id
FROM identifications i
JOIN photos p ON p.id = i.photo_id
JOIN observations o ON o.id = p.observation_id
WHERE o.status IN ('completed', 'archived', 'pending')
  AND i.species_id IS NOT NULL
"""

_available = {}                # id(engine) -> (checked_at, bool)
_refresh_state = {'checked_at': 0.0, 'running': False}
_refresh_lock = threading.Lock()


def _engine_of(session):
    return session.get_bind()


def weights_available(session):
    """True when the weights table exists and has rows (cached briefly)."""
    engine = _engine_of(session)
    now = time.monotonic()
    hit = _available.get(id(engine))
    if hit and now - hit[0] < AVAILABILITY_TTL:
        return hit[1]
    ok = False
    try:
        # The session's own connection: no extra connection per vote, and no
        # reset-on-return that could touch the request's transaction.
        conn = session.connection()
        if inspect(conn).has_table(TABLE):
            ok = conn.execute(text(f'SELECT 1 FROM {TABLE} LIMIT 1')).first() is not None
    except Exception as exc:  # never let the check break a vote
        current_app.logger.warning(f'verifier weights availability check failed: {exc}')
        ok = False
    _available[id(engine)] = (now, ok)
    return ok


def forget_availability():
    """Drop the cached availability answers (after init / refresh, in tests)."""
    _available.clear()


def load_weights(session, user_ids):
    """{(user_id, species_id): weight} for these people, or None (no table)."""
    if not weights_available(session):
        return None
    user_ids = sorted({u for u in user_ids if u is not None})
    if not user_ids:
        return {}
    from .models import VerifierSpeciesWeight as W
    rows = session.query(W.user_id, W.species_id, W.n, W.confirmed) \
        .filter(W.user_id.in_(user_ids)).all()
    k = experience_k(session)
    return {(u, s): verifier_weight(c, n, k) for u, s, n, c in rows}


def experience_k(session=None):
    """k in force: stored in ct_settings (with a session), else the config's."""
    from . import ct_settings
    if session is not None:
        return ct_settings.experience_k(session)
    cfg = current_app.config.get('CAMERA_TRAP_CONFIG', {})
    return cfg.get('VERIFIER_EXPERIENCE_K', EXPERIENCE_K_DEFAULT)


def series_ai_answers(session, observation_ids):
    """{observation_id: (species_id, score)} by the shared series-pick rule.

    Empty when the AI tables are absent (installations without the AI schema).
    """
    ids = sorted(set(observation_ids))
    if not ids:
        return {}
    try:
        if not inspect(session.connection()).has_table('ai_predictions'):
            return {}
    except Exception:
        return {}
    from .models import AIModel, AIModelLevel, AIPrediction
    rank = func.coalesce(AIModelLevel.accuracy_rank, 0)
    grouped = {}
    for start in range(0, len(ids), 5000):
        chunk = ids[start:start + 5000]
        rows = session.query(
            AIPrediction.observation_id, rank, AIPrediction.model_id,
            AIPrediction.prediction_species_id, func.count(),
            func.max(AIPrediction.prediction_score),
        ).join(AIModel, AIModel.id == AIPrediction.model_id) \
            .outerjoin(AIModelLevel, AIModelLevel.id == AIModel.level_id) \
            .filter(AIPrediction.observation_id.in_(chunk)) \
            .group_by(AIPrediction.observation_id, rank, AIPrediction.model_id,
                      AIPrediction.prediction_species_id).all()
        for obs, r, model_id, sp, frames, score in rows:
            grouped.setdefault(obs, []).append((r, model_id, sp, frames, score))
    return {obs: pick_series_ai(rows) for obs, rows in grouped.items()}


def compute(conn):
    """{(user_id, species_id): (n, confirmed)} from every graded vote."""
    by_obs = {}
    for obs, user_id, species_id in conn.execute(text(VOTES_SQL)):
        by_obs.setdefault(obs, []).append((user_id, species_id))
    return grade_votes(by_obs.values())


def refresh(engine):
    """Rebuild the table in one transaction. Returns the row count, or None
    when another worker holds the lock (it is doing the same job)."""
    with engine.begin() as conn:
        if conn.dialect.name == 'postgresql':
            got = conn.execute(text('SELECT pg_try_advisory_xact_lock(:k)'),
                               {'k': LOCK_KEY}).scalar()
            if not got:
                return None
        graded = compute(conn)
        now = datetime.now()
        conn.execute(text(f'DELETE FROM {TABLE}'))
        if graded:
            conn.execute(
                text(f'INSERT INTO {TABLE} (user_id, species_id, n, confirmed, updated_at) '
                     'VALUES (:u, :s, :n, :c, :t)'),
                [{'u': u, 's': s, 'n': n, 'c': c, 't': now}
                 for (u, s), (n, c) in graded.items()])
    forget_availability()
    return len(graded)


def maybe_refresh_async(session):
    """Start a background rebuild when the table is stale. Cheap to call often."""
    now = time.monotonic()
    with _refresh_lock:
        if _refresh_state['running'] or now - _refresh_state['checked_at'] < REFRESH_CHECK_EVERY:
            return False
        _refresh_state['checked_at'] = now
    if not weights_available(session):
        return False
    engine = _engine_of(session)
    config = current_app.config.get('CAMERA_TRAP_CONFIG', {})
    max_age = timedelta(hours=config.get('VERIFIER_WEIGHTS_MAX_AGE_HOURS', MAX_AGE_HOURS_DEFAULT))
    try:
        newest = session.execute(text(f'SELECT MAX(updated_at) FROM {TABLE}')).scalar()
    except Exception as exc:
        current_app.logger.warning(f'verifier weights staleness check failed: {exc}')
        return False
    if isinstance(newest, str):  # sqlite returns text
        newest = datetime.fromisoformat(newest)
    if newest is not None and datetime.now() - newest < max_age:
        return False
    return _start_refresh(engine)


def start_refresh(session):
    """Rebuild now in the background (admin button). False if one is running."""
    with _refresh_lock:
        if _refresh_state['running']:
            return False
        _refresh_state['checked_at'] = time.monotonic()
    return _start_refresh(_engine_of(session))


def refresh_running():
    return _refresh_state['running']


def _start_refresh(engine):
    app = current_app._get_current_object()

    def _run():
        with app.app_context():
            try:
                rows = refresh(engine)
                app.logger.info(f'verifier weights refreshed: {rows} rows')
            except Exception as exc:
                app.logger.error(f'verifier weights refresh failed: {exc}')
            finally:
                _refresh_state['running'] = False

    _refresh_state['running'] = True
    threading.Thread(target=_run, name='verifier-weights-refresh', daemon=True).start()
    return True
