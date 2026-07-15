# SPDX-License-Identifier: AGPL-3.0-only
"""
Automatic assignment of biotopes to camera-trap locations from landcover.

WHY
===
Biotopes power the biotope filters on dashboards, but they are assigned to
``locations`` entirely by hand. Whoever did not physically place a camera does
not know its surrounding biotopes, so the vast majority of locations end up with
none — which breaks the biotope filters. This module fills the gap
automatically from remote sensing.

HOW
===
For every location we sample the ESA WorldCover landcover raster in a radius
around the point (default 100 m) via Google Earth Engine, build a histogram of
class → pixel-count, take the top-N classes (default 3, to shrug off noise),
map each class to a biotope through ``biotope_landcover_map``, and **add** the
resulting biotopes to the location. Existing assignments are never removed —
only missing biotopes are appended (``ON CONFLICT DO NOTHING`` on the
``location_biotopes`` M2M).

PORTABILITY / GRACEFUL DEGRADATION
==================================
This module is self-contained: it does NOT import the SDM package (``app.sdm``),
because ``camera_traps`` is a public submodule (shared-ct) reused in other
deployments where SDM may be absent. Earth Engine is imported lazily, so an
installation without ``earthengine-api`` or without a GEE service-account key
keeps working — the feature simply reports itself unavailable
(``gee_landcover_available()`` → False) and the admin button is hidden/disabled.
Any failure during a run is caught and recorded as status ``failed``; it never
propagates to break a request.

ASYNC
=====
``start_async_assign`` launches a daemon ``threading.Thread`` and returns
immediately; the admin page polls ``get_autoassign_status()``. Status is stored
in the generic ``calculation_log`` table under ``source_name =
'biotope_autoassign'`` — the same mechanism the analytics recalculation uses.
The thin threading layer is intentionally swappable for Celery ``.delay()``
later (mirrors ``analytics_calculator.start_async_analytics``).
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta
from typing import Optional

from flask import current_app
from sqlalchemy import text

from .database import get_ct_engine


# ── Constants ──────────────────────────────────────────────────────────────

#: calculation_log.source_name used for this tool's status row.
AUTOASSIGN_SOURCE = 'biotope_autoassign'

#: A run older than this (minutes) is considered stuck and may be reclaimed.
AUTOASSIGN_STUCK_MINUTES = 30

#: ESA WorldCover v200 (2021), 10 m — the same asset the SDM module uses.
WORLDCOVER_ASSET = 'ESA/WorldCover/v200/2021'
WORLDCOVER_BAND = 'Map'
WORLDCOVER_SCALE = 10

DEFAULT_RADIUS_M = 100
DEFAULT_TOP_N = 3

#: ESA WorldCover class codes → bilingual labels (for the admin mapping UI).
WORLDCOVER_CLASSES: dict[int, tuple[str, str]] = {
    10:  ('Дерева (ліс)', 'Tree cover'),
    20:  ('Чагарники', 'Shrubland'),
    30:  ('Трав’яниста рослинність', 'Grassland'),
    40:  ('Рілля', 'Cropland'),
    50:  ('Забудова', 'Built-up'),
    60:  ('Оголений / розріджений ґрунт', 'Bare / sparse vegetation'),
    70:  ('Сніг та лід', 'Snow and ice'),
    80:  ('Постійні водойми', 'Permanent water bodies'),
    90:  ('Трав’янисте водно-болотне угіддя', 'Herbaceous wetland'),
    95:  ('Мангри', 'Mangroves'),
    100: ('Мохи та лишайники', 'Moss and lichen'),
}

#: General landcover biotopes seeded into the ``biotopes`` table so every
#: ecologically-relevant ESA WorldCover class (for Ukraine) has a biotope to map
#: to: ``(name_ua, name_en)``. Created idempotently (ON CONFLICT (name_ua)) by
#: scripts/init_biotope_autoassign.py. "Лука" (class 30) is a pre-existing
#: standard biotope, so it is not created here — only mapped. Snow/ice (70),
#: mangroves (95) and moss/lichen (100) are omitted as irrelevant for Ukraine.
DEFAULT_LANDCOVER_BIOTOPES: list[tuple[str, str]] = [
    ('Ліс', 'Forest'),
    ('Чагарники', 'Shrubland'),
    ('Рілля', 'Cropland'),
    ('Забудова', 'Built-up'),
    ('Оголений ґрунт', 'Bare / sparse vegetation'),
    ('Водойми', 'Water bodies'),
    ('Водно-болотне угіддя', 'Wetland'),
]

#: Seed defaults: ESA WorldCover class → biotope ``name_ua`` (see
#: scripts/init_biotope_autoassign.py). Maps each class to a GENERAL biotope,
#: because WorldCover is too coarse to tell coniferous/deciduous/mixed forest
#: apart, or a pond from a lake, or peatland from a marsh. The specific biotopes
#: (forest sub-types, Стави, Торфовище, Берег річки) stay in the table for manual
#: tagging. Every name here must exist in DEFAULT_LANDCOVER_BIOTOPES or already
#: in the biotopes table ("Лука").
DEFAULT_SEED_BY_NAME_UA: dict[int, str] = {
    10: 'Ліс',
    20: 'Чагарники',
    30: 'Лука',
    40: 'Рілля',
    50: 'Забудова',
    60: 'Оголений ґрунт',
    80: 'Водойми',
    90: 'Водно-болотне угіддя',
}


# ── GEE availability & initialisation (self-contained) ──────────────────────

_gee_initialized = False


def _resolve_gee_key_path() -> Optional[str]:
    """Path to the GEE service-account JSON, or None if not configured/missing.

    Flask config ``GEE_SERVICE_ACCOUNT_KEY`` → env var → None. Never raises.
    """
    path = None
    try:
        path = current_app.config.get('GEE_SERVICE_ACCOUNT_KEY')
    except Exception:
        path = None
    if not path:
        path = os.environ.get('GEE_SERVICE_ACCOUNT_KEY')
    if path and os.path.exists(path):
        return path
    return None


def gee_landcover_available() -> bool:
    """Cheap check used to gate the admin button. Never raises, never calls the
    network. True only if ``earthengine-api`` is importable AND a service-account
    key file is configured and exists on disk.
    """
    try:
        import ee  # noqa: F401
    except Exception:
        return False
    return _resolve_gee_key_path() is not None


def _initialize_gee() -> None:
    """Initialise Earth Engine via the service-account key. Singleton.

    Mirrors app/sdm/adapters/gee_backend.py: do NOT pass ``project=`` — GEE reads
    project_id from the key JSON (passing a foreign project triggers a 403 on
    serviceusage.services.use). Raises RuntimeError if the key is missing.
    """
    global _gee_initialized
    if _gee_initialized:
        return
    import ee
    key_path = _resolve_gee_key_path()
    if not key_path:
        raise RuntimeError(
            'GEE_SERVICE_ACCOUNT_KEY is not configured or the file does not '
            'exist — landcover-based biotope assignment is unavailable.'
        )
    credentials = ee.ServiceAccountCredentials(None, key_file=key_path)
    ee.Initialize(credentials=credentials)
    _gee_initialized = True


# ── Landcover sampling ──────────────────────────────────────────────────────

def get_landcover_histograms(
    points: list[tuple[int, float, float]],
    radius_m: int = DEFAULT_RADIUS_M,
    chunk_size: int = 200,
) -> dict[int, dict[int, float]]:
    """Sample the ESA WorldCover histogram in a radius around each point.

    Args:
        points: ``[(location_id, latitude, longitude), ...]``.
        radius_m: buffer radius around each point, in metres.
        chunk_size: locations per GEE ``reduceRegions`` request.

    Returns:
        ``{location_id: {class_code: pixel_count}}``. Locations outside raster
        coverage map to an empty dict.

    Side effects:
        Initialises GEE on first call. Raises on GEE/auth failure (the caller
        records it as a failed run).
    """
    import ee

    _initialize_gee()
    img = ee.Image(WORLDCOVER_ASSET).select(WORLDCOVER_BAND)

    results: dict[int, dict[int, float]] = {}
    for i in range(0, len(points), chunk_size):
        chunk = points[i:i + chunk_size]
        fc = ee.FeatureCollection([
            ee.Feature(
                ee.Geometry.Point([float(lon), float(lat)]).buffer(radius_m),
                {'loc_id': int(loc_id)},
            )
            for (loc_id, lat, lon) in chunk
        ])
        stats = img.reduceRegions(
            collection=fc,
            reducer=ee.Reducer.frequencyHistogram(),
            scale=WORLDCOVER_SCALE,
            crs='EPSG:4326',
        ).getInfo()

        for feat in stats.get('features', []):
            props = feat.get('properties', {})
            loc_id = props.get('loc_id')
            if loc_id is None:
                continue
            hist = props.get('histogram') or {}
            # Keys come back as string class codes → cast to int.
            results[int(loc_id)] = {
                int(cls): float(cnt) for cls, cnt in hist.items()
            }
    return results


# ── Mapping helpers ──────────────────────────────────────────────────────────

def get_biotope_mapping() -> dict[int, int]:
    """Return ``{worldcover_class: biotope_id}`` from biotope_landcover_map."""
    engine = get_ct_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text('SELECT worldcover_class, biotope_id FROM biotope_landcover_map')
        ).all()
    return {int(r.worldcover_class): int(r.biotope_id) for r in rows}


def set_biotope_mapping(worldcover_class: int, biotope_id: Optional[int]) -> None:
    """Upsert (or clear, when biotope_id is None) one class→biotope mapping."""
    engine = get_ct_engine()
    with engine.begin() as conn:
        if biotope_id is None:
            conn.execute(
                text('DELETE FROM biotope_landcover_map WHERE worldcover_class = :c'),
                {'c': int(worldcover_class)},
            )
        else:
            conn.execute(
                text("""
                    INSERT INTO biotope_landcover_map (worldcover_class, biotope_id)
                    VALUES (:c, :b)
                    ON CONFLICT (worldcover_class)
                    DO UPDATE SET biotope_id = EXCLUDED.biotope_id
                """),
                {'c': int(worldcover_class), 'b': int(biotope_id)},
            )


# ── Core assignment ───────────────────────────────────────────────────────────

def select_biotopes_from_histogram(
    histogram: dict[int, float],
    mapping: dict[int, int],
    top_n: int,
) -> list[int]:
    """Pick up to ``top_n`` biotope ids from a landcover histogram.

    Classes are ordered by pixel count (descending); each is translated to a
    biotope via ``mapping``. Unmapped classes (noise like built-up / bare) are
    skipped, biotopes are de-duplicated, and at most ``top_n`` are returned.
    So ``top_n`` counts *biotopes*, not raw classes — this is what makes the
    top-N cut robust to landcover noise.
    """
    ordered = sorted(histogram.items(), key=lambda kv: kv[1], reverse=True)
    biotope_ids: list[int] = []
    for cls, _cnt in ordered:
        bid = mapping.get(cls)
        if bid is not None and bid not in biotope_ids:
            biotope_ids.append(bid)
        if len(biotope_ids) >= top_n:
            break
    return biotope_ids


def assign_biotopes(
    radius_m: int = DEFAULT_RADIUS_M,
    top_n: int = DEFAULT_TOP_N,
    only_missing_locations: bool = False,
) -> dict:
    """Assign biotopes to locations from landcover. Additive — never removes.

    Args:
        radius_m: sampling radius around each location point.
        top_n: how many biotopes (from the most-abundant mapped classes) to
            assign per location. Noise/unmapped classes (e.g. built-up) are
            skipped, so this counts biotopes, not raw classes.
        only_missing_locations: when True, process only locations that currently
            have no biotopes at all (leaves already-tagged locations untouched
            entirely — faster, fewer GEE calls).

    Returns:
        Summary dict: locations_processed, locations_updated, links_added,
        locations_no_data, and a human note.

    Raises:
        Propagates GEE errors so the async wrapper can record a failed run.
    """
    mapping = get_biotope_mapping()
    if not mapping:
        return {
            'locations_processed': 0, 'locations_updated': 0, 'links_added': 0,
            'locations_no_data': 0,
            'note': 'Не налаштовано жодної відповідності клас лендковеру → біотоп. '
                    'Заповніть таблицю відповідностей в адмінпанелі.',
        }

    engine = get_ct_engine()
    with engine.connect() as conn:
        if only_missing_locations:
            loc_rows = conn.execute(text("""
                SELECT l.id, l.latitude, l.longitude
                  FROM locations l
                 WHERE l.latitude IS NOT NULL AND l.longitude IS NOT NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM location_biotopes lb WHERE lb.location_id = l.id
                   )
            """)).all()
        else:
            loc_rows = conn.execute(text("""
                SELECT id, latitude, longitude
                  FROM locations
                 WHERE latitude IS NOT NULL AND longitude IS NOT NULL
            """)).all()

    points = [(int(r.id), float(r.latitude), float(r.longitude)) for r in loc_rows]
    if not points:
        return {
            'locations_processed': 0, 'locations_updated': 0, 'links_added': 0,
            'locations_no_data': 0, 'note': 'Немає локацій для обробки.',
        }

    histograms = get_landcover_histograms(points, radius_m=radius_m)

    locations_updated = 0
    links_added = 0
    locations_no_data = 0

    engine = get_ct_engine()
    with engine.begin() as conn:
        for loc_id, _lat, _lon in points:
            hist = histograms.get(loc_id) or {}
            if not hist:
                locations_no_data += 1
                continue

            biotope_ids = select_biotopes_from_histogram(hist, mapping, top_n)
            if not biotope_ids:
                locations_no_data += 1
                continue

            res = conn.execute(
                text("""
                    INSERT INTO location_biotopes (location_id, biotope_id)
                    SELECT :loc, b FROM unnest(CAST(:bids AS integer[])) AS b
                    ON CONFLICT (location_id, biotope_id) DO NOTHING
                """),
                {'loc': loc_id, 'bids': biotope_ids},
            )
            added = res.rowcount or 0
            if added > 0:
                locations_updated += 1
                links_added += added

    return {
        'locations_processed': len(points),
        'locations_updated': locations_updated,
        'links_added': links_added,
        'locations_no_data': locations_no_data,
        'note': (f'Оброблено {len(points)} локацій: оновлено {locations_updated}, '
                 f'додано {links_added} звʼязків, без даних лендковеру '
                 f'{locations_no_data}.'),
    }


# ── Status row (calculation_log) — mirrors analytics_calculator ───────────────

def _ensure_log_row(conn) -> None:
    conn.execute(
        text("""
            INSERT INTO calculation_log (source_name, last_count, status)
            VALUES (:src, 0, 'idle')
            ON CONFLICT (source_name) DO NOTHING
        """),
        {'src': AUTOASSIGN_SOURCE},
    )


def try_start_autoassign_run() -> bool:
    """Atomic compare-and-set to claim the run across gunicorn workers.

    Returns True if this call claimed the run; False if one is already running.
    """
    engine = get_ct_engine()
    stuck_cutoff = datetime.utcnow() - timedelta(minutes=AUTOASSIGN_STUCK_MINUTES)
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
            {'src': AUTOASSIGN_SOURCE, 'cutoff': stuck_cutoff},
        )
        return (result.rowcount or 0) == 1


def _finish_autoassign_run(status: str, error_message: Optional[str] = None,
                           last_count: Optional[int] = None) -> None:
    engine = get_ct_engine()
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE calculation_log
                   SET status = :st,
                       error_message = :err,
                       last_count = COALESCE(:cnt, last_count),
                       last_calculated_at = CASE
                           WHEN :st = 'completed' THEN NOW()
                           ELSE last_calculated_at
                       END
                 WHERE source_name = :src
            """),
            {'src': AUTOASSIGN_SOURCE, 'st': status,
             'err': (error_message[:500] if error_message else None),
             'cnt': last_count},
        )


def get_autoassign_status() -> dict:
    """Current state for admin-page polling."""
    engine = get_ct_engine()
    with engine.begin() as conn:
        _ensure_log_row(conn)
        row = conn.execute(
            text("""
                SELECT status, started_at, last_calculated_at, last_count, error_message
                  FROM calculation_log
                 WHERE source_name = :src
            """),
            {'src': AUTOASSIGN_SOURCE},
        ).first()

    if row is None:
        return {'status': 'idle', 'started_at': None, 'last_calculated_at': None,
                'last_count': None, 'error_message': None}
    return {
        'status': row.status or 'idle',
        'started_at': row.started_at.isoformat() if row.started_at else None,
        'last_calculated_at': row.last_calculated_at.isoformat() if row.last_calculated_at else None,
        'last_count': row.last_count,
        'error_message': row.error_message,
    }


# ── Async wrapper ─────────────────────────────────────────────────────────────

def _run_autoassign_in_thread(app, radius_m: int, top_n: int,
                              only_missing_locations: bool) -> None:
    """Background thread body. Runs outside the HTTP context."""
    with app.app_context():
        try:
            summary = assign_biotopes(
                radius_m=radius_m,
                top_n=top_n,
                only_missing_locations=only_missing_locations,
            )
            _finish_autoassign_run(
                'completed',
                error_message=summary.get('note'),
                last_count=summary.get('links_added'),
            )
            current_app.logger.info(f'[biotope-autoassign] done: {summary}')
        except Exception as e:
            current_app.logger.exception('[biotope-autoassign] background run crashed')
            try:
                _finish_autoassign_run('failed', str(e))
            except Exception:
                pass


def start_async_assign(radius_m: int = DEFAULT_RADIUS_M,
                       top_n: int = DEFAULT_TOP_N,
                       only_missing_locations: bool = False) -> bool:
    """Start a background biotope auto-assignment. Returns IMMEDIATELY.

    Returns True if a run was started, False if one is already in progress.
    """
    if not try_start_autoassign_run():
        return False

    app = current_app._get_current_object()  # type: ignore[attr-defined]
    threading.Thread(
        target=_run_autoassign_in_thread,
        args=(app, radius_m, top_n, only_missing_locations),
        name='ct-biotope-autoassign',
        daemon=True,
    ).start()
    current_app.logger.info(
        f'[biotope-autoassign] started (radius={radius_m}m, top_n={top_n}, '
        f'only_missing={only_missing_locations})'
    )
    return True
