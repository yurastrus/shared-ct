# SPDX-License-Identifier: AGPL-3.0-only
"""Settings an admin may change in the interface, kept in ct_db.

Table ``ct_settings`` (key, value, updated_at, updated_by), created by
scripts/init_ct_settings.py. ct_db is shared by every host (biomon,
myproject), so a value saved on one site applies on all of them; the host
config stays the fallback while the table or the key is missing, and a
stored value outside its allowed range is ignored.

Values are cached per process for CACHE_SECONDS, so a change reaches the
other workers within that time; the worker that saves it sees it at once.

Owner's request 2026-11-23: the weighted rule's closing threshold and
DeepFaune's vote factor are edited on /admin/verifier-weights instead of in
config files.
"""
import time
from datetime import datetime

from flask import current_app
from sqlalchemy import inspect, text

from .consensus import AI_VOTE_FACTOR_DEFAULT, WEIGHTED_THRESHOLD_DEFAULT

TABLE = 'ct_settings'
CACHE_SECONDS = 30

THRESHOLD_KEY = 'weighted_consensus_threshold'
AI_FACTOR_KEY = 'ai_vote_factor'

# key -> (config key, code default, min, max)
SETTINGS = {
    # Below 1.0 one person could close a series alone (a weight is < 1);
    # above 3.0 every series would wait for four or more people.
    THRESHOLD_KEY: ('WEIGHTED_CONSENSUS_THRESHOLD', WEIGHTED_THRESHOLD_DEFAULT, 1.0, 3.0),
    # 0 = DeepFaune ignored; 1 = a confident DeepFaune counts as a whole person.
    AI_FACTOR_KEY: ('AI_VOTE_FACTOR', AI_VOTE_FACTOR_DEFAULT, 0.0, 1.0),
}
THRESHOLD_MIN, THRESHOLD_MAX = SETTINGS[THRESHOLD_KEY][2:]

_cache = {}   # key -> (read_at, value or None)


def forget():
    _cache.clear()


def table_ready(session):
    try:
        return inspect(session.connection()).has_table(TABLE)
    except Exception:
        return False


def get(session, key):
    """Stored text value, or None (no table, no row, or an error)."""
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = None
    try:
        if table_ready(session):
            value = session.execute(text(f'SELECT value FROM {TABLE} WHERE key = :k'),
                                    {'k': key}).scalar()
    except Exception as exc:  # never let a settings read break a vote
        current_app.logger.warning(f'ct_settings read {key} failed: {exc}')
        value = None
    _cache[key] = (now, value)
    return value


def info(session, key):
    """(value, updated_at, updated_by) of a stored key, uncached, or None."""
    if not table_ready(session):
        return None
    row = session.execute(text(
        f'SELECT value, updated_at, updated_by FROM {TABLE} WHERE key = :k'), {'k': key}).first()
    if row is None:
        return None
    updated_at = row[1]
    if isinstance(updated_at, str):  # SQLite returns text
        updated_at = datetime.fromisoformat(updated_at)
    return row[0], updated_at, row[2]


def put(session, key, value, user):
    """Insert or update a key (caller commits). Works on Postgres and SQLite."""
    now = datetime.now()
    updated = session.execute(text(
        f'UPDATE {TABLE} SET value = :v, updated_at = :t, updated_by = :u WHERE key = :k'),
        {'k': key, 'v': str(value), 't': now, 'u': user}).rowcount
    if not updated:
        session.execute(text(
            f'INSERT INTO {TABLE} (key, value, updated_at, updated_by) VALUES (:k, :v, :t, :u)'),
            {'k': key, 'v': str(value), 't': now, 'u': user})
    _cache.pop(key, None)


def _config(config):
    if config is None:
        config = current_app.config.get('CAMERA_TRAP_CONFIG', {})
    return config


def fallback(key, config=None):
    """The config value (or the code default) of a setting."""
    cfg_key, default, _lo, _hi = SETTINGS[key]
    return float(_config(config).get(cfg_key, default))


def parse(key, raw):
    """Float (2 decimals) within the setting's range, or None."""
    _cfg, _default, lo, hi = SETTINGS[key]
    try:
        value = round(float(str(raw).replace(',', '.')), 2)
    except (TypeError, ValueError):
        return None
    return value if lo <= value <= hi else None


def value(session, key, config=None):
    """The value in force: the stored one if valid, else the config's."""
    raw = get(session, key)
    if raw is not None:
        parsed = parse(key, raw)
        if parsed is not None:
            return parsed
        current_app.logger.warning(f'ct_settings {key}={raw!r} ignored (out of range)')
    return fallback(key, config)


def weighted_threshold(session, config=None):
    return value(session, THRESHOLD_KEY, config)


def ai_vote_factor(session, config=None):
    return value(session, AI_FACTOR_KEY, config)


def config_threshold(config=None):
    return fallback(THRESHOLD_KEY, config)


def parse_threshold(raw):
    return parse(THRESHOLD_KEY, raw)
