# SPDX-License-Identifier: AGPL-3.0-only
"""Data layer of the camera-trap gap analysis page.

Two halves:

* ``fetch_gap_data`` reads ct_db (PostgreSQL) once and returns a ``GapData``:
  locations, camera-days per location, and per-location detection days per
  species. All filters of the page (access, scope, period, QC) apply here.
* ``build_grid`` / ``build_kernel`` / ``biotope_summary`` / ``cell_detail``
  are pure and turn that into map layers using gap_metrics.py.

What counts as a camera-day
---------------------------
The same rule as the location coverage calendar: days inside deployment
intervals plus days with series, with gaps of up to COVERAGE_MAX_GAP_DAYS
filled (a trigger-based camera that saw nothing for a week was still there).
Two stricter guards, because here a camera-day is a claim that a species
*would have been recorded* had it passed:

* a day whose series are all still unverified is not effort: nothing on it
  can be a record yet;
* a deployment interval counts only if it holds at least one verified series
  day, so deployments whose photos never reached the database add nothing.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import text

from . import gap_metrics as gm

VERIFIED_STATUSES = ('completed', 'archived')

# Deployments with these flags never count as effort, whatever the user picks.
ALWAYS_EXCLUDED_DEPLOYMENT_FLAGS = (
    'qc_data_not_usable', 'qc_non_functional', 'qc_stolen', 'qc_no_data_uploaded_by_pa',
)

# Photos without EXIF carry a 1900 placeholder; anything before this is noise.
MIN_VALID_YEAR = 2010

TAXON_GROUPS = {
    'mammals': ('mammals',),
    'birds': ('birds',),
    'all': ('mammals', 'birds', 'other'),
}

# Study area = locations buffered by STUDY_AREA_BUFFER_M, then closed by
# STUDY_AREA_CLOSING_M (buffer out, buffer back in) so holes between camera
# clusters up to twice that distance apart become part of the area and show up
# as empty cells instead of vanishing from the map.
STUDY_AREA_BUFFER_M = 5000
STUDY_AREA_CLOSING_M = 10000
MAX_EMPTY_CELLS = 20000

UNIT_DAY = 'day'
UNIT_WEEK = 'week'


@dataclass
class GapData:
    locations: dict = field(default_factory=dict)    # id -> {name, lat, lon, x, y}
    effort_days: dict = field(default_factory=dict)  # id -> set[date]
    detections: dict = field(default_factory=dict)   # id -> {species_id: set[date]}
    species: dict = field(default_factory=dict)      # id -> {name_uk, name_en, scientific}
    biotopes: dict = field(default_factory=dict)     # location id -> [biotope_id]
    biotope_names: dict = field(default_factory=dict)  # biotope id -> (name_uk, name_en)


# ─── Reading ct_db ────────────────────────────────────────────────────────────

def _as_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()


def _deployment_flag_sql(qc_exclude):
    from .data_export import _VALID_QC_FLAGS, _QC_TEXT_FLAG
    parts = [f"COALESCE(d.{f}, FALSE) = FALSE" for f in ALWAYS_EXCLUDED_DEPLOYMENT_FLAGS]
    for flag in qc_exclude or []:
        if flag not in _VALID_QC_FLAGS or flag in ALWAYS_EXCLUDED_DEPLOYMENT_FLAGS:
            continue
        if flag == _QC_TEXT_FLAG:
            parts.append(f"COALESCE(d.{flag}, '') = ''")
        else:
            parts.append(f"COALESCE(d.{flag}, FALSE) = FALSE")
    return ' AND '.join(parts)


def fetch_gap_data(session, inst_condition, inst_params, start_date=None, end_date=None,
                   qc_exclude=None, taxon='mammals', exclude_species_ids=(),
                   max_gap_days=15):
    """Read everything the gap analysis needs in four queries.

    Args:
        session: ct_db session.
        inst_condition / inst_params: output of get_institution_filter with
            ``table_alias='l'`` (access rights plus the selected scope).
        start_date, end_date: optional ``date`` bounds, inclusive.
        qc_exclude: QC flags chosen on the page.
        taxon: key of TAXON_GROUPS.
        exclude_species_ids: species never counted (domestic animals).
        max_gap_days: gap filling between series days, as in the coverage calendar.
    """
    from .data_export import _build_qc_exclusion_cond

    params = dict(inst_params or {})
    date_clause = ''
    if start_date and end_date:
        date_clause = 'AND DATE(o.series_start_time) BETWEEN :start_date AND :end_date'
        params.update(start_date=start_date, end_date=end_date)
    qc_pred = _build_qc_exclusion_cond(qc_exclude or [], obs_alias='o')
    qc_clause = ('AND ' + qc_pred) if qc_pred else ''
    loc_where = f"l.is_valid IS NOT FALSE AND ({inst_condition})"

    data = GapData()

    # 1. Locations in scope.
    rows = session.execute(text(f"""
        SELECT l.id, l.name, l.latitude, l.longitude
        FROM locations l
        WHERE {loc_where}
    """), params).fetchall()
    for lid, name, lat, lon in rows:
        if lat is None or lon is None:
            continue
        x, y = gm.to_laea(lon, lat)
        data.locations[lid] = {'name': name, 'lat': float(lat), 'lon': float(lon), 'x': x, 'y': y}
    if not data.locations:
        return data

    # 2. Series days per location, split by verification state.
    rows = session.execute(text(f"""
        SELECT o.location_id, DATE(o.series_start_time) AS d,
               BOOL_OR(o.status IN ('completed', 'archived')) AS verified
        FROM observations o
        JOIN locations l ON l.id = o.location_id
        WHERE {loc_where} {date_clause} {qc_clause}
        GROUP BY o.location_id, DATE(o.series_start_time)
    """), params).fetchall()
    verified_days = defaultdict(set)
    unverified_days = defaultdict(set)
    for lid, d, verified in rows:
        d = _as_date(d)
        if d is None or d.year < MIN_VALID_YEAR:
            continue
        (verified_days if verified else unverified_days)[lid].add(d)

    # 3. Deployment intervals that may count as effort.
    rows = session.execute(text(f"""
        SELECT d.location_id, d.start_date, d.end_date
        FROM deployments d
        JOIN locations l ON l.id = d.location_id
        WHERE {loc_where}
          AND d.start_date IS NOT NULL AND d.end_date IS NOT NULL
          AND d.end_date >= d.start_date
          AND {_deployment_flag_sql(qc_exclude)}
    """), params).fetchall()
    deployments = defaultdict(list)
    for lid, s, e in rows:
        deployments[lid].append((_as_date(s), _as_date(e)))

    data.effort_days = build_effort_days(
        verified_days, unverified_days, deployments,
        start_date=_as_date(start_date), end_date=_as_date(end_date),
        max_gap_days=max_gap_days)

    # 4. Detection days per species (consensus winner of verified series).
    categories = TAXON_GROUPS.get(taxon, TAXON_GROUPS['mammals'])
    params.update(categories=list(categories))
    rows = session.execute(text(f"""
        WITH obs AS (
            SELECT o.id, o.location_id, DATE(o.series_start_time) AS d
            FROM observations o
            JOIN locations l ON l.id = o.location_id
            WHERE o.status IN ('completed', 'archived')
              AND {loc_where} {date_clause} {qc_clause}
        ),
        oc AS (
            SELECT p.observation_id, i.species_id,
                   COUNT(DISTINCT i.user_id) AS vote_count,
                   MAX(i.quantity) AS max_quantity
            FROM identifications i
            JOIN photos p ON p.id = i.photo_id
            WHERE p.observation_id IN (SELECT id FROM obs)
            GROUP BY p.observation_id, i.species_id
        ),
        rc AS (
            SELECT observation_id, species_id,
                   ROW_NUMBER() OVER (PARTITION BY observation_id
                                      ORDER BY vote_count DESC, max_quantity DESC) AS rn
            FROM oc
        )
        SELECT DISTINCT obs.location_id, obs.d, s.id, s.common_name_ua,
               s.common_name_en, s.scientific_name
        FROM obs
        JOIN rc ON rc.observation_id = obs.id AND rc.rn = 1
        JOIN species s ON s.id = rc.species_id
        WHERE s.id > 0 AND s.category = ANY(:categories)
    """), params).fetchall()
    excluded = set(exclude_species_ids or ())
    for lid, d, sid, name_uk, name_en, sci in rows:
        d = _as_date(d)
        if sid in excluded or d is None or d.year < MIN_VALID_YEAR:
            continue
        data.detections.setdefault(lid, {}).setdefault(sid, set()).add(d)
        data.effort_days.setdefault(lid, set()).add(d)  # a record is always effort
        data.species.setdefault(sid, {'name_uk': name_uk or sci, 'name_en': name_en or sci,
                                      'scientific': sci})

    # 5. Biotopes, for the environmental summary.
    rows = session.execute(text(f"""
        SELECT lb.location_id, b.id, b.name_ua, b.name_en
        FROM location_biotopes lb
        JOIN biotopes b ON b.id = lb.biotope_id
        JOIN locations l ON l.id = lb.location_id
        WHERE {loc_where}
    """), params).fetchall()
    for lid, bid, name_uk, name_en in rows:
        data.biotopes.setdefault(lid, []).append(bid)
        data.biotope_names[bid] = (name_uk, name_en)

    return data


def build_effort_days(verified_days, unverified_days, deployments,
                      start_date=None, end_date=None, max_gap_days=15):
    """Camera-days per location, by the rule in the module docstring.

    Args:
        verified_days / unverified_days: {location_id: set[date]} of days with
            at least one verified series / only unverified series.
        deployments: {location_id: [(start, end)]}.
    """
    from .utils import fill_day_gaps
    out = {}
    for lid in set(verified_days) | set(deployments):
        verified = verified_days.get(lid, set())
        days = fill_day_gaps(verified, max_gap_days)
        for s, e in deployments.get(lid, ()):
            if s is None or e is None or e < s:
                continue
            if not any(s <= d <= e for d in verified):
                continue
            days.update(s + timedelta(days=k) for k in range((e - s).days + 1))
        days -= (unverified_days.get(lid, set()) - verified)
        if start_date:
            days = {d for d in days if d >= start_date}
        if end_date:
            days = {d for d in days if d <= end_date}
        if days:
            out[lid] = days
    return out


# ─── Sampling units ───────────────────────────────────────────────────────────

def unit_key(d, unit):
    """Bucket of a date: the day itself, or a fixed 7-day calendar block."""
    return d.toordinal() if unit == UNIT_DAY else d.toordinal() // 7


def _species_label(info, lang):
    if not info:
        return ''
    return info['name_en'] if lang == 'en' else info['name_uk']


def species_detection_probabilities(data, unit, min_units=1):
    """{species_id: per-unit detection probability} over the whole scope.

    Species recorded in fewer than ``min_units`` units are left out: one lucky
    record gives a p that says more about chance than about the species.
    """
    units_by_loc = {lid: {unit_key(d, unit) for d in days}
                    for lid, days in data.effort_days.items()}
    hits = defaultdict(int)
    exposure = defaultdict(int)
    for lid, per_species in data.detections.items():
        n_units = len(units_by_loc.get(lid, ()))
        for sid, days in per_species.items():
            hits[sid] += len({unit_key(d, unit) for d in days})
            exposure[sid] += n_units
    return {sid: gm.per_unit_detection_probability(hits[sid], exposure[sid])
            for sid in hits if hits[sid] >= min_units}


# A species joins the regional pool once recorded in this many sampling units.
MIN_POOL_UNITS = 3

# The pool a cell is judged against holds only species recorded within this
# distance of the cell centre. A scope-wide pool would fault a Carpathian cell
# for missing the kulan of a steppe reserve.
POOL_RADIUS_M = 50000


def local_pool(data, pool, x, y, radius_m=None):
    """Subset of ``pool`` recorded at some location within ``radius_m`` of (x, y)."""
    radius_m = POOL_RADIUS_M if radius_m is None else radius_m
    r2 = radius_m * radius_m
    out = {}
    for lid, per_species in data.detections.items():
        loc = data.locations.get(lid)
        if loc is None or (loc['x'] - x) ** 2 + (loc['y'] - y) ** 2 > r2:
            continue
        for sid in per_species:
            if sid in pool:
                out[sid] = pool[sid]
    return out


def _pool(data, location_ids, unit):
    """Pool several locations into one sample: units, incidences, camera-days."""
    units = set()
    camera_days = []
    incidence = defaultdict(set)
    for lid in location_ids:
        days = data.effort_days.get(lid, set())
        camera_days.extend(days)
        units.update((lid, unit_key(d, unit)) for d in days)
        for sid, ddays in data.detections.get(lid, {}).items():
            incidence[sid].update((lid, unit_key(d, unit)) for d in ddays)
    return units, camera_days, {sid: len(u) for sid, u in incidence.items()}


def _sample_metrics(data, location_ids, unit, res_m=None, pool=None):
    units, camera_days, incidence = _pool(data, location_ids, unit)
    T = len(units)
    cd = len(camera_days)
    rich = gm.richness_stats(incidence.values(), T)
    temporal = gm.temporal_coverage(camera_days)
    cd_per_unit = cd / T if T else 0
    extra_units = gm.units_to_coverage(list(incidence.values()), T, 0.95)
    sensitivity = gm.pool_sensitivity(pool or {}, T, incidence.keys())
    out = {
        'n_locations': len(location_ids),
        'camera_days': cd,
        'T': T,
        'S_obs': rich['S_obs'],
        'Q1': rich['Q1'],
        'Q2': rich['Q2'],
        'chao2': _r(rich['chao2'], 2),
        'coverage': _r(rich['coverage'], 3),
        'completeness': _r(rich['completeness'], 3),
        'estimable': rich['estimable'],
        'extra_camera_days_cov95': (None if extra_units is None
                                    else int(round(extra_units * cd_per_unit))),
        'months': temporal['months'],
        'months_covered': temporal['months_covered'],
        'seasons_covered': temporal['seasons_covered'],
        'year_first': temporal['years'][0] if temporal['years'] else None,
        'year_last': temporal['years'][-1] if temporal['years'] else None,
        'n_years': len(temporal['years']),
        'temporal_score': _r(temporal['score'], 3),
        'pool_sensitivity': _r(sensitivity, 3),
    }
    if res_m:
        es = gm.effort_score(cd, res_m)
        out['effort_score'] = _r(es, 3)
        out['composite'] = _r(gm.composite_score(es, sensitivity, temporal['score']), 3)
    return out, incidence


def _r(value, ndigits):
    return None if value is None else round(value, ndigits)


# ─── Map layers ───────────────────────────────────────────────────────────────

def study_area(data):
    """Buffered and closed hull of the locations, a shapely geometry in EPSG:3035."""
    from shapely.geometry import Point
    from shapely.ops import unary_union
    pts = [Point(loc['x'], loc['y']).buffer(STUDY_AREA_BUFFER_M, 16)
           for loc in data.locations.values()]
    if not pts:
        return None
    area = unary_union(pts)
    return area.buffer(STUDY_AREA_CLOSING_M, 8).buffer(-STUDY_AREA_CLOSING_M, 8)


def empty_cells(area, occupied, res_m, limit=MAX_EMPTY_CELLS):
    """Grid cells intersecting the study area that hold no effort.

    Returns (cells, truncated). Enumeration is per connected part of the area,
    so scattered reserves do not drag in the whole country's bounding box.
    """
    from shapely.geometry import box
    from shapely.prepared import prep
    if area is None or area.is_empty:
        return [], False
    parts = list(getattr(area, 'geoms', [area]))
    out = []
    seen = set(occupied)
    for part in parts:
        p = prep(part)
        minx, miny, maxx, maxy = part.bounds
        x = (minx // res_m) * res_m
        while x < maxx:
            y = (miny // res_m) * res_m
            while y < maxy:
                key = (int(x), int(y))
                if key not in seen and p.intersects(box(x, y, x + res_m, y + res_m)):
                    seen.add(key)
                    out.append(key)
                    if len(out) >= limit:
                        return out, True
                y += res_m
            x += res_m
    return out, False


def build_grid(data, res_m, unit=UNIT_DAY, focus_species_id=None, lang='uk',
               include_empty=True):
    """Grid layer: one feature per occupied cell, plus empty cells of the study area.

    Returns a dict with ``cells`` (list of feature dicts), ``incidence`` (cell
    code → {species_id: incidence}, kept server-side for the cell detail) and
    ``summary``.
    """
    by_cell = defaultdict(list)
    for lid, loc in data.locations.items():
        if lid not in data.effort_days:
            continue
        by_cell[gm.cell_origin(loc['x'], loc['y'], res_m)].append(lid)

    p_by_species = species_detection_probabilities(data, unit)
    focus_p = p_by_species.get(focus_species_id) if focus_species_id else None
    pool = species_detection_probabilities(data, unit, MIN_POOL_UNITS)

    cells = []
    incidence_by_code = {}
    for (x0, y0), lids in by_cell.items():
        code = gm.eea_cell_code(x0, y0, res_m)
        cell_pool = local_pool(data, pool, x0 + res_m / 2, y0 + res_m / 2)
        metrics, incidence = _sample_metrics(data, lids, unit, res_m, cell_pool)
        incidence_by_code[code] = {'incidence': incidence, 'T': metrics['T'],
                                   'camera_days': metrics['camera_days'],
                                   'location_ids': sorted(lids)}
        top = sorted(incidence.items(), key=lambda kv: -kv[1])
        metrics.update(
            code=code, status='data',
            ring=gm.cell_polygon_wgs84(x0, y0, res_m),
            species=[_species_label(data.species.get(sid), lang) for sid, _ in top],
            likely_missed=likely_missed(data, cell_pool, incidence, metrics['T'], lang),
            pool_size=len(cell_pool),
        )
        if focus_species_id:
            detected = focus_species_id in incidence
            metrics['focus_detected'] = detected
            metrics['focus_confidence'] = 1.0 if detected else _r(
                gm.detection_confidence(focus_p, metrics['T']), 3)
        cells.append(metrics)

    truncated = False
    if include_empty:
        area = study_area(data)
        empties, truncated = empty_cells(area, set(by_cell), res_m)
        for x0, y0 in empties:
            cells.append({'code': gm.eea_cell_code(x0, y0, res_m), 'status': 'empty',
                          'ring': gm.cell_polygon_wgs84(x0, y0, res_m),
                          'camera_days': 0, 'composite': 0, 'effort_score': 0})

    all_lids = [lid for lids in by_cell.values() for lid in lids]
    total, _ = _sample_metrics(data, all_lids, unit, pool=pool)
    unit_days = total['camera_days'] / total['T'] if total['T'] else 1

    def _detectability(sid, p):
        need = gm.units_for_confidence(p, 0.95)
        return {'species_id': sid,
                'name': _species_label(data.species.get(sid), lang),
                'p_unit': _r(p, 4),
                'camera_days_for_95': None if need is None else int(round(need * unit_days))}

    summary = {
        'scale_m': res_m, 'unit': unit,
        'n_cells_with_data': len(by_cell),
        'n_empty_cells': sum(1 for c in cells if c['status'] == 'empty'),
        'empty_truncated': truncated,
        'total': total,
        'focus': _detectability(focus_species_id, focus_p) if focus_species_id else None,
        # Hardest to detect first: this is the list that sets how much effort
        # a cell needs before its silence means anything.
        'pool': [_detectability(sid, p) for sid, p in sorted(pool.items(), key=lambda kv: kv[1])],
        'species_options': species_options(data, lang),
    }
    return {'cells': cells, 'incidence': incidence_by_code, 'summary': summary}


def likely_missed(data, pool, incidence, T, lang='uk', limit=3):
    """Pool species not recorded here, least likely to have been detected first."""
    rows = [(gm.detection_confidence(p, T), sid) for sid, p in pool.items() if sid not in incidence]
    rows.sort()
    return [{'name': _species_label(data.species.get(sid), lang), 'confidence': round(c, 3)}
            for c, sid in rows[:limit]]


def species_options(data, lang='uk'):
    """Species recorded in scope, most detection-days first, for the focus picker."""
    counts = defaultdict(int)
    for per_species in data.detections.values():
        for sid, days in per_species.items():
            counts[sid] += len(days)
    return [{'id': sid, 'name': _species_label(data.species.get(sid), lang),
             'scientific': data.species.get(sid, {}).get('scientific'),
             'detection_days': n}
            for sid, n in sorted(counts.items(), key=lambda kv: -kv[1])]


def build_kernel(data, bandwidth_m):
    """Kernel effort surface as pixel features (ring + camera-days per km²)."""
    points = [(loc['x'], loc['y'], len(data.effort_days.get(lid, ())))
              for lid, loc in data.locations.items()]
    pixel, surface = gm.kernel_effort_surface(points, bandwidth_m)
    if not surface:
        return {'pixel_m': pixel, 'bandwidth_m': bandwidth_m, 'pixels': [], 'max': 0}
    peak = max(surface.values())
    floor = peak * 1e-3   # drop the far tails, they only cost bytes
    pixels = [{'ring': gm.cell_polygon_wgs84(x0, y0, pixel, ndigits=5), 'value': round(v, 3)}
              for (x0, y0), v in surface.items() if v >= floor]
    return {'pixel_m': pixel, 'bandwidth_m': bandwidth_m, 'pixels': pixels, 'max': round(peak, 3)}


def location_points(data):
    """Camera locations with their effort and richness, for the point layer."""
    out = []
    for lid, loc in data.locations.items():
        days = data.effort_days.get(lid)
        if not days:
            continue
        out.append({'id': lid, 'name': loc['name'], 'lat': loc['lat'], 'lon': loc['lon'],
                    'camera_days': len(days),
                    'S_obs': len(data.detections.get(lid, {})),
                    'first': min(days).isoformat(), 'last': max(days).isoformat()})
    return out


def biotope_summary(data, unit=UNIT_DAY, lang='uk'):
    """Effort and completeness pooled by biotope (a location counts in each of its biotopes).

    The environmental side of the gap: a biotope with little effort or low
    coverage is under-sampled wherever on the map it happens to be.
    """
    members = defaultdict(list)
    for lid in data.effort_days:
        for bid in data.biotopes.get(lid, []) or [None]:
            members[bid].append(lid)
    rows = []
    pool = species_detection_probabilities(data, unit, MIN_POOL_UNITS)
    for bid, lids in members.items():
        metrics, _ = _sample_metrics(data, lids, unit, pool=pool)
        if bid is None:
            name = None
        else:
            uk, en = data.biotope_names.get(bid, ('?', '?'))
            name = en if lang == 'en' else uk
        metrics.update(biotope_id=bid, name=name)
        metrics.pop('months', None)
        rows.append(metrics)
    rows.sort(key=lambda r: -r['camera_days'])
    return rows


def cell_detail(grid_result, code, data, lang='uk'):
    """Accumulation curve and species list for one cell of a built grid."""
    entry = grid_result['incidence'].get(code)
    if entry is None:
        return None
    incidence = entry['incidence']
    T = entry['T']
    ys = list(incidence.values())
    rich = gm.richness_stats(ys, T)
    cd_per_unit = entry['camera_days'] / T if T else 0
    curve = [{'units': u, 'camera_days': int(round(u * cd_per_unit)), 'S': s}
             for u, s in gm.accumulation_curve(ys, T)]
    species = [{'name': _species_label(data.species.get(sid), lang),
                'scientific': data.species.get(sid, {}).get('scientific'),
                'units': n}
               for sid, n in sorted(incidence.items(), key=lambda kv: -kv[1])]
    locations = [{'id': lid, 'name': data.locations[lid]['name'],
                  'camera_days': len(data.effort_days.get(lid, ()))}
                 for lid in entry['location_ids'] if lid in data.locations]
    return {'code': code, 'T': T, 'camera_days': entry['camera_days'],
            'S_obs': rich['S_obs'], 'chao2': _r(rich['chao2'], 2),
            'coverage': _r(rich['coverage'], 3), 'estimable': rich['estimable'],
            'curve': curve, 'species': species, 'locations': locations}
