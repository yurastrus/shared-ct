# SPDX-License-Identifier: AGPL-3.0-only
"""GBIF mammal records for the gap analysis page, cached in ct_db.

Why GBIF here: the cameras say where a species was *recorded*. GBIF is an
independent source of presence, so the page can tell "present but the camera
missed it" from "nobody knows anything about this cell", and it can name
species the cameras never recorded anywhere (the camera-only species pool is
blind to them by construction).

What is fetched
---------------
* Taxa a camera trap can plausibly record: the orders and families in
  CAMERA_DETECTABLE_TAXA, plus every mammal species the cameras did record
  (if it falls outside those groups). Bats, shrews, moles and small rodents are
  left out on purpose: their absence in camera data means nothing.
* Inside the study area: all valid camera locations buffered and closed the
  same way as on the page (gap_analysis.study_area). GBIF is queried per
  bounding box of each connected part of that area; points are then tested
  against the exact area.
* Records from FETCH_YEARS_BACK years. The page shows the last
  GBIF_YEARS_BACK of them (see gbif_year_window), so N can change without a
  refetch as long as it stays within the fetched range.

Filters (each one closes a known trap)
--------------------------------------
* EXCLUDED_DATASET_KEYS: biomon's own camera data published to GBIF by the
  Roztochya reserve. Counting it would let the cameras confirm themselves.
* EXCLUDED_TAXON_KEYS: domestic forms. GBIF folds a dog record into Canis lupus
  at species rank and a domestic pig into Sus scrofa, so the record's own
  taxonKey is checked, not only its speciesKey.
* Coordinate uncertainty: stored in buckets (UNCERTAINTY_BUCKETS_M). A record
  counts on a grid only if its uncertainty fits the cell; records with no stated
  uncertainty count from UNKNOWN_UNCERTAINTY_MIN_SCALE_M up. Anything coarser
  than the largest bucket is dropped (obscured iNaturalist records, atlas
  squares). On the real data (Oct 2026) only 276 of 787 Ukrainian lynx records
  had uncertainty <= 1 km.
* Fossil and zoo specimens, absences, records with geospatial issues.

Storage: gbif_taxa (names, order, family, the matching camera species) and
gbif_cell_counts (taxon × 1 km EEA cell × year × uncertainty bucket → records).
Coarser grids are summed from 1 km cells. Refresh replaces both tables in one
transaction, so readers never see a half-written cache. Tables are created by
scripts/init_gbif_cache.py (ct_db is not managed by Alembic).
"""
import logging
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Optional

import requests
from flask import current_app
from sqlalchemy import text

from . import gap_metrics as gm

log = logging.getLogger(__name__)

GBIF_API = 'https://api.gbif.org/v1'

# How many years back the page shows, and how many the cache holds.
GBIF_YEARS_BACK = 20
FETCH_YEARS_BACK = 40

# GBIF backbone keys. Orders and families a camera trap can record.
CAMERA_DETECTABLE_TAXA = {
    732: 'Carnivora', 731: 'Artiodactyla', 795: 'Perissodactyla', 785: 'Lagomorpha',
    5722: 'Erinaceidae', 5493: 'Castoridae', 9456: 'Sciuridae',
    3240572: 'Myocastoridae', 9705: 'Hystricidae',
}
# Seals sit inside Carnivora; checked against the record's familyKey.
EXCLUDED_FAMILY_KEYS = {5310: 'Phocidae'}
# Domestic forms, checked against the record's taxonKey and speciesKey.
EXCLUDED_TAXON_KEYS = {
    6164210: 'Canis lupus familiaris', 11374585: 'Sus scrofa domesticus',
    2441022: 'Bos taurus', 2435035: 'Felis catus', 2440886: 'Equus caballus',
    2441056: 'Capra hircus', 2441110: 'Ovis aries',
}
# biomon's own camera-trap data published to GBIF. Extend through
# CAMERA_TRAP_CONFIG['GBIF_EXCLUDED_DATASETS'] without a code change.
EXCLUDED_DATASET_KEYS = {
    'b587c2e3-0252-4940-947f-5899a27dcaf9',  # Observations from camera trap monitoring project in Roztochya Nature Reserve
}
BASIS_OF_RECORD = ('HUMAN_OBSERVATION', 'MACHINE_OBSERVATION', 'OBSERVATION',
                   'PRESERVED_SPECIMEN', 'MATERIAL_SAMPLE', 'OCCURRENCE')

UNCERTAINTY_BUCKETS_M = (1000, 5000, 10000)
UNKNOWN_UNCERTAINTY_MIN_SCALE_M = 5000

PAGE_SIZE = 300                # GBIF search maximum
OFFSET_LIMIT = 100000          # GBIF search: offset + limit must stay below this
REQUEST_TIMEOUT = 60
RETRIES = 3

GBIF_SOURCE = 'gbif_cache'     # calculation_log.source_name
GBIF_STUCK_MINUTES = 90


# ─── Pure helpers ─────────────────────────────────────────────────────────────

def gbif_year_window(today=None, years_back=None):
    """(first_year, last_year) of GBIF records the page shows.

    The single place that decides the window: change the rule here (or
    GBIF_YEARS_BACK) and the page follows; the cache holds FETCH_YEARS_BACK.
    """
    today = today or date.today()
    years_back = GBIF_YEARS_BACK if years_back is None else years_back
    return today.year - years_back + 1, today.year


def uncertainty_bucket(value):
    """Bucket of a coordinate uncertainty in metres.

    Returns the smallest UNCERTAINTY_BUCKETS_M value that holds it, None when
    the uncertainty is not stated, or False when it is too coarse to keep.
    """
    if value is None or value == '':
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    for bucket in UNCERTAINTY_BUCKETS_M:
        if value <= bucket:
            return bucket
    return False


def usable_at_scale(bucket, res_m):
    """Whether a record with this uncertainty bucket can be placed in a res_m cell."""
    if bucket is None:
        return res_m >= UNKNOWN_UNCERTAINTY_MIN_SCALE_M
    return bucket <= res_m


def classify(rec, area=None, excluded_datasets=()):
    """(key, None) for a kept record or (None, reason) for a filtered one.

    key = (species_key, e_km, n_km, year, bucket); ``area`` is a prepared
    shapely geometry in EPSG:3035 (or None to skip the test).
    """
    species_key = rec.get('speciesKey')
    lat, lon, year = rec.get('decimalLatitude'), rec.get('decimalLongitude'), rec.get('year')
    if not species_key or lat is None or lon is None or not year:
        return None, 'incomplete'
    if rec.get('datasetKey') in excluded_datasets:
        return None, 'own_dataset'
    if (rec.get('taxonKey') in EXCLUDED_TAXON_KEYS or species_key in EXCLUDED_TAXON_KEYS
            or rec.get('familyKey') in EXCLUDED_FAMILY_KEYS):
        return None, 'excluded_taxon'
    if rec.get('occurrenceStatus') and rec['occurrenceStatus'] != 'PRESENT':
        return None, 'absent'
    bucket = uncertainty_bucket(rec.get('coordinateUncertaintyInMeters'))
    if bucket is False:
        return None, 'coarse'
    x, y = gm.to_laea(lon, lat)
    if area is not None:
        from shapely.geometry import Point
        if not area.contains(Point(x, y)):
            return None, 'outside_area'
    x0, y0 = gm.cell_origin(x, y, 1000)
    return (int(species_key), int(x0 // 1000), int(y0 // 1000), int(year), bucket), None


def record_key(rec, area=None, excluded_datasets=()):
    """classify() without the reason: the key, or None if the record is filtered out."""
    return classify(rec, area, excluded_datasets)[0]


def aggregate(records, area=None, excluded_datasets=()):
    """(Counter of key → records, Counter of drop reason → records)."""
    counts = Counter()
    dropped = Counter()
    for rec in records:
        key, reason = classify(rec, area, excluded_datasets)
        if key is None:
            dropped[reason] += 1
        else:
            counts[key] += 1
    return counts, dropped


def area_bboxes(area, pad_deg=0.01):
    """WGS84 bounding boxes (w, s, e, n) of the area's connected parts, overlapping ones merged."""
    if area is None or area.is_empty:
        return []
    boxes = []
    for part in getattr(area, 'geoms', [area]):
        minx, miny, maxx, maxy = part.bounds
        corners = [gm.to_wgs84(x, y) for x, y in ((minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy))]
        lons = [c[0] for c in corners]
        lats = [c[1] for c in corners]
        boxes.append([min(lons) - pad_deg, min(lats) - pad_deg, max(lons) + pad_deg, max(lats) + pad_deg])
    merged = True
    while merged:
        merged = False
        out = []
        for b in boxes:
            for m in out:
                if b[0] <= m[2] and m[0] <= b[2] and b[1] <= m[3] and m[1] <= b[3]:
                    m[0], m[1] = min(m[0], b[0]), min(m[1], b[1])
                    m[2], m[3] = max(m[2], b[2]), max(m[3], b[3])
                    merged = True
                    break
            else:
                out.append(list(b))
        boxes = out
    return [tuple(round(v, 5) for v in b) for b in boxes]


def bbox_wkt(box):
    w, s, e, n = box
    return f'POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))'


# ─── GBIF API ─────────────────────────────────────────────────────────────────

def _get(session, path, params=None):
    last = None
    for attempt in range(RETRIES):
        try:
            resp = session.get(f'{GBIF_API}/{path}', params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f'GBIF {resp.status_code}')
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f'GBIF request failed: {path}: {last}')


def _search_params(taxon_key, box, year_from, year_to):
    return {
        'taxonKey': taxon_key, 'geometry': bbox_wkt(box),
        'year': f'{year_from},{year_to}',
        'hasCoordinate': 'true', 'hasGeospatialIssue': 'false',
        'occurrenceStatus': 'PRESENT', 'basisOfRecord': list(BASIS_OF_RECORD),
    }


def fetch_records(session, taxon_key, box, year_from, year_to):
    """All records of a taxon in a box and year range, paging and splitting by years.

    GBIF search refuses offsets past OFFSET_LIMIT, so a range holding more
    records than that is split in two by year and each half fetched separately.
    """
    params = _search_params(taxon_key, box, year_from, year_to)
    count = _get(session, 'occurrence/search', dict(params, limit=0)).get('count', 0)
    if count == 0:
        return
    if count >= OFFSET_LIMIT and year_from < year_to:
        mid = (year_from + year_to) // 2
        yield from fetch_records(session, taxon_key, box, year_from, mid)
        yield from fetch_records(session, taxon_key, box, mid + 1, year_to)
        return
    offset = 0
    while offset < min(count, OFFSET_LIMIT):
        page = _get(session, 'occurrence/search',
                    dict(params, limit=PAGE_SIZE, offset=offset)).get('results', [])
        if not page:
            break
        yield from page
        offset += len(page)
        if len(page) < PAGE_SIZE:
            break


def match_species(session, scientific_name):
    """GBIF backbone match of a camera species: (speciesKey, orderKey, familyKey) or None."""
    m = _get(session, 'species/match', {'name': scientific_name, 'class': 'Mammalia', 'strict': 'true'})
    if m.get('matchType') not in ('EXACT', 'FUZZY') or not m.get('speciesKey'):
        return None
    return m['speciesKey'], m.get('orderKey'), m.get('familyKey')


def pick_vernacular(vernacular, lang):
    """The most agreed-on vernacular name in a language.

    GBIF lists names from many sources in no useful order (the first Ukrainian
    name of the rabbit is "кролик свійський декоративний"). A name flagged
    preferred wins; otherwise the one most sources share, then the shortest.
    """
    names = [v for v in vernacular if v.get('language') == lang and (v.get('vernacularName') or '').strip()]
    preferred = [v for v in names if v.get('preferred')]
    if preferred:
        return preferred[0]['vernacularName'].strip()
    votes = Counter(v['vernacularName'].strip().lower() for v in names)
    if not votes:
        return None
    best = sorted(votes.items(), key=lambda kv: (-kv[1], len(kv[0])))[0][0]
    original = next(v['vernacularName'].strip() for v in names if v['vernacularName'].strip().lower() == best)
    return original[:1].upper() + original[1:]


def taxon_info(session, species_key):
    """Scientific name, order, family and Ukrainian/English vernacular names of a species."""
    sp = _get(session, f'species/{species_key}')
    try:
        vern = _get(session, f'species/{species_key}/vernacularNames', {'limit': 200}).get('results', [])
    except RuntimeError:
        vern = []
    names = {lang: pick_vernacular(vern, lang) for lang in ('ukr', 'eng')}
    sci = sp.get('canonicalName') or sp.get('scientificName') or str(species_key)
    return {'scientific_name': sci, 'name_uk': names['ukr'], 'name_en': names['eng'],
            'order_name': sp.get('order'), 'family_name': sp.get('family')}


# ─── Refresh ──────────────────────────────────────────────────────────────────

def _excluded_datasets():
    extra = (current_app.config.get('CAMERA_TRAP_CONFIG', {}) or {}).get('GBIF_EXCLUDED_DATASETS', ())
    return set(EXCLUDED_DATASET_KEYS) | set(extra or ())


def _study_area(conn):
    from . import gap_analysis as ga
    rows = conn.execute(text("""
        SELECT id, latitude, longitude FROM locations
        WHERE is_valid IS NOT FALSE AND latitude IS NOT NULL AND longitude IS NOT NULL
    """)).fetchall()
    data = ga.GapData()
    for lid, lat, lon in rows:
        x, y = gm.to_laea(lon, lat)
        data.locations[lid] = {'name': '', 'lat': float(lat), 'lon': float(lon), 'x': x, 'y': y}
    return ga.study_area(data)


def refresh_gbif_cache(engine, session=None, today=None, progress=None, write=True):
    """Fetch, filter and aggregate GBIF records, then replace the cache tables.

    Returns a summary dict. ``progress`` (optional) is called with a short text.
    ``write=False`` reads ct_db and GBIF only (probe on a production database);
    the aggregated counts and taxa are then returned in the summary.
    """
    from shapely.prepared import prep
    session = session or requests.Session()
    today = today or date.today()
    year_from, year_to = today.year - FETCH_YEARS_BACK + 1, today.year
    excluded = _excluded_datasets()

    with engine.connect() as conn:
        area = _study_area(conn)
        ct_species = conn.execute(text("""
            SELECT DISTINCT s.id, s.scientific_name, s.common_name_ua, s.common_name_en
            FROM species s JOIN identifications i ON i.species_id = s.id
            WHERE s.category = 'mammals' AND s.id > 0 AND s.scientific_name IS NOT NULL
        """)).fetchall()
    boxes = area_bboxes(area)
    prepared = prep(area) if area is not None else None

    # Camera species: match to the backbone; fetch separately those outside the groups.
    taxon_keys = dict(CAMERA_DETECTABLE_TAXA)
    ct_by_key = {}
    for sid, sci, name_uk, name_en in ct_species:
        try:
            match = match_species(session, sci)
        except RuntimeError:
            match = None
        if not match:
            continue
        key, order_key, family_key = match
        if key in EXCLUDED_TAXON_KEYS:
            continue
        ct_by_key[key] = (sid, name_uk, name_en)
        if order_key not in CAMERA_DETECTABLE_TAXA and family_key not in CAMERA_DETECTABLE_TAXA:
            taxon_keys[key] = sci

    counts = Counter()
    dropped = Counter()
    fetched = 0
    for i, box in enumerate(boxes):
        for tkey, tname in taxon_keys.items():
            if progress:
                progress(f'{tname}: box {i + 1}/{len(boxes)}')
            c, d = aggregate(fetch_records(session, tkey, box, year_from, year_to), prepared, excluded)
            fetched += sum(c.values()) + sum(d.values())
            dropped.update(d)
            counts.update(c)

    taxa = {}
    for key in sorted({k[0] for k in counts}):
        info = taxon_info(session, key)
        sid, name_uk, name_en = ct_by_key.get(key, (None, None, None))
        info.update(ct_species_id=sid,
                    name_uk=name_uk or info['name_uk'] or info['scientific_name'],
                    name_en=name_en or info['name_en'] or info['scientific_name'])
        taxa[key] = info

    summary = {'records_kept': sum(counts.values()), 'records_fetched': fetched,
               'records_dropped': sum(dropped.values()), 'dropped_by_reason': dict(dropped),
               'species': len(taxa), 'boxes': len(boxes),
               'taxon_queries': len(taxon_keys) * len(boxes), 'years': [year_from, year_to]}
    if not write:
        summary.update(counts=counts, taxa=taxa, area=area)
        return summary

    with engine.begin() as conn:
        conn.execute(text('DELETE FROM gbif_cell_counts'))
        conn.execute(text('DELETE FROM gbif_taxa'))
        if taxa:
            conn.execute(text("""
                INSERT INTO gbif_taxa (taxon_key, scientific_name, name_uk, name_en,
                                       order_name, family_name, ct_species_id, updated_at)
                VALUES (:taxon_key, :scientific_name, :name_uk, :name_en,
                        :order_name, :family_name, :ct_species_id, NOW())
            """), [dict(info, taxon_key=k) for k, info in taxa.items()])
        if counts:
            conn.execute(text("""
                INSERT INTO gbif_cell_counts (taxon_key, e_km, n_km, year, uncertainty_m, n_records)
                VALUES (:t, :e, :n, :y, :u, :c)
            """), [{'t': t, 'e': e, 'n': n, 'y': y, 'u': u, 'c': c}
                   for (t, e, n, y, u), c in counts.items()])

    return summary


# ─── Reading the cache ────────────────────────────────────────────────────────

class GbifData:
    """Cached GBIF records inside a year window, ready for the grid."""

    def __init__(self, taxa=None, rows=None, updated_at=None, window=None):
        self.taxa = taxa or {}          # taxon_key -> {scientific_name, name_uk, name_en, ct_species_id}
        self.rows = rows or []          # (taxon_key, e_km, n_km, uncertainty_m, n_records)
        self.updated_at = updated_at
        self.window = window

    def by_cell(self, res_m):
        """{(x0, y0): {taxon_key: records}} for a grid scale, uncertainty rule applied."""
        step = res_m // 1000
        out = {}
        for taxon, e_km, n_km, unc, n in self.rows:
            if not usable_at_scale(unc, res_m):
                continue
            key = ((e_km // step) * res_m, (n_km // step) * res_m)
            cell = out.setdefault(key, {})
            cell[taxon] = cell.get(taxon, 0) + n
        return out

    def name(self, taxon_key, lang='uk'):
        t = self.taxa.get(taxon_key) or {}
        return (t.get('name_en') if lang == 'en' else t.get('name_uk')) or t.get('scientific_name') or str(taxon_key)


def load_gbif(engine, window=None):
    """GbifData for the year window, or None if the cache is missing or empty.

    Never raises: the page works without GBIF (tables not created yet, no run yet).
    """
    window = window or gbif_year_window()
    try:
        with engine.connect() as conn:
            taxa = {r.taxon_key: {'scientific_name': r.scientific_name, 'name_uk': r.name_uk,
                                  'name_en': r.name_en, 'ct_species_id': r.ct_species_id}
                    for r in conn.execute(text("""
                        SELECT taxon_key, scientific_name, name_uk, name_en, ct_species_id FROM gbif_taxa
                    """))}
            if not taxa:
                return None
            rows = [tuple(r) for r in conn.execute(text("""
                SELECT taxon_key, e_km, n_km, uncertainty_m, SUM(n_records)
                FROM gbif_cell_counts WHERE year BETWEEN :y0 AND :y1
                GROUP BY taxon_key, e_km, n_km, uncertainty_m
            """), {'y0': window[0], 'y1': window[1]})]
            updated = conn.execute(text("SELECT MAX(updated_at) FROM gbif_taxa")).scalar()
    except Exception as e:  # noqa: BLE001 - missing tables are an expected state
        log.info('GBIF cache unavailable: %s', e)
        return None
    return GbifData(taxa, rows, updated, window)


# ─── Background run (status in calculation_log, as biotope_autoassign) ───────

def try_start_run(engine) -> bool:
    cutoff = datetime.utcnow() - timedelta(minutes=GBIF_STUCK_MINUTES)
    with engine.begin() as conn:
        result = conn.execute(text("""
            INSERT INTO calculation_log (source_name, last_count, status, started_at, error_message)
            VALUES (:src, 0, 'running', NOW(), NULL)
            ON CONFLICT (source_name) DO UPDATE
               SET status = 'running', started_at = NOW(), error_message = NULL
             WHERE calculation_log.status IS DISTINCT FROM 'running'
                OR calculation_log.started_at IS NULL
                OR calculation_log.started_at < :cutoff
        """), {'src': GBIF_SOURCE, 'cutoff': cutoff})
        return (result.rowcount or 0) == 1


def _finish_run(engine, status: str, message: Optional[str] = None,
                last_count: Optional[int] = None) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE calculation_log
               SET status = :st, error_message = :msg,
                   last_count = COALESCE(:cnt, last_count),
                   last_calculated_at = CASE WHEN :st = 'completed' THEN NOW() ELSE last_calculated_at END
             WHERE source_name = :src
        """), {'src': GBIF_SOURCE, 'st': status,
               'msg': (message[:500] if message else None), 'cnt': last_count})


def get_status(engine) -> dict:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT status, started_at, last_calculated_at, last_count, error_message
            FROM calculation_log WHERE source_name = :src
        """), {'src': GBIF_SOURCE}).first()
    if row is None:
        return {'status': 'idle', 'started_at': None, 'last_calculated_at': None,
                'last_count': None, 'message': None}
    return {'status': row.status or 'idle',
            'started_at': row.started_at.isoformat() if row.started_at else None,
            'last_calculated_at': row.last_calculated_at.isoformat() if row.last_calculated_at else None,
            'last_count': row.last_count, 'message': row.error_message}


def _run_in_thread(app, engine):
    with app.app_context():
        try:
            summary = refresh_gbif_cache(engine)
            _finish_run(engine, 'completed',
                        f"{summary['species']} species, {summary['records_kept']} records kept, "
                        f"{summary['records_dropped']} filtered out",
                        summary['records_kept'])
            current_app.logger.info(f'[gbif-cache] done: {summary}')
        except Exception as e:
            current_app.logger.exception('[gbif-cache] refresh crashed')
            try:
                _finish_run(engine, 'failed', str(e))
            except Exception:
                pass


def start_async_refresh(engine) -> bool:
    """Start a background refresh. Returns False if one is already running."""
    if not try_start_run(engine):
        return False
    app = current_app._get_current_object()  # type: ignore[attr-defined]
    threading.Thread(target=_run_in_thread, args=(app, engine),
                     name='ct-gbif-cache', daemon=True).start()
    return True
