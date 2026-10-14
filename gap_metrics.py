# SPDX-License-Identifier: AGPL-3.0-only
"""Pure computations behind the camera-trap gap analysis page.

No Flask, no database: everything here takes plain Python structures and is
unit-tested directly (tests/test_ct_gap_metrics.py). The data layer that feeds
it lives in gap_analysis.py.

The question the page answers is *where does a non-detection mean something*.
Three separate dimensions are measured, never collapsed silently:

* effort       camera-days, on a grid and as a kernel surface;
* completeness incidence-based richness estimation (Chao2, sample coverage),
               the formal version of "the accumulation curve has levelled off";
* time         how many calendar months and years the effort spans.

Per species, the probability that the species would have been detected in a
cell had it been present there turns the same effort into a "could it have
been missed" map.

Incidence formulas follow Chao et al. (2014, Ecol. Monogr. 84:45-67) and
Chao & Jost (2012, Ecology 93:2533-2547), i.e. what iNEXT computes for
``datatype = "incidence_freq"``. A sampling unit is one camera-day (or one
camera-week); a species' incidence is the number of units it was recorded in.
"""
import math
from functools import lru_cache

# Grid scales offered on the page, in metres. EEA reference grid on ETRS89-LAEA
# (EPSG:3035), the same grid the SDM module uses, so cell codes are compatible.
GRID_SCALES_M = (1000, 5000, 10000)

# Coverage and Chao2 are meaningless on a handful of units. Below this many
# sampling units a cell is reported as "too little data to estimate".
MIN_UNITS_FOR_ESTIMATE = 20

# A calendar month counts as covered once it holds this many camera-days.
MIN_CAMERA_DAYS_PER_MONTH = 7

# Camera-days at which the effort score saturates at 1, per grid scale. Chosen
# as roughly one camera running all year per 1 km cell, two per 5 km and four
# per 10 km cell. These are tunable conventions, not biology.
EFFORT_TARGET_CAMERA_DAYS = {1000: 365, 5000: 730, 10000: 1460}

SEASON_OF_MONTH = {12: 'winter', 1: 'winter', 2: 'winter',
                   3: 'spring', 4: 'spring', 5: 'spring',
                   6: 'summer', 7: 'summer', 8: 'summer',
                   9: 'autumn', 10: 'autumn', 11: 'autumn'}


# ─── EEA grid ─────────────────────────────────────────────────────────────────

@lru_cache(maxsize=2)
def _transformer(inverse=False):
    from pyproj import Transformer
    if inverse:
        return Transformer.from_crs('EPSG:3035', 'EPSG:4326', always_xy=True)
    return Transformer.from_crs('EPSG:4326', 'EPSG:3035', always_xy=True)


def to_laea(lon, lat):
    """(lon, lat) in WGS84 → (x, y) metres in EPSG:3035."""
    return _transformer(False).transform(float(lon), float(lat))


def to_wgs84(x, y):
    """(x, y) metres in EPSG:3035 → (lon, lat)."""
    return _transformer(True).transform(float(x), float(y))


def cell_origin(x, y, res_m):
    """Lower-left corner of the grid cell containing (x, y)."""
    return (math.floor(x / res_m) * res_m, math.floor(y / res_m) * res_m)


def eea_cell_code(x0, y0, res_m):
    """Official EEA cell code, e.g. ``5kmE5300N3070``.

    For cells of 1 km and larger, east/north are always given in kilometres,
    whatever the resolution (the convention the SDM module learned the hard way).
    """
    return f"{res_m // 1000}kmE{int(x0 // 1000)}N{int(y0 // 1000)}"


def cell_polygon_wgs84(x0, y0, res_m, ndigits=5):
    """Closed lon/lat ring of a LAEA grid cell (5 decimals ≈ 1 m), for the map."""
    corners = [(x0, y0), (x0 + res_m, y0), (x0 + res_m, y0 + res_m),
               (x0, y0 + res_m), (x0, y0)]
    ring = []
    for cx, cy in corners:
        lon, lat = to_wgs84(cx, cy)
        ring.append([round(lon, ndigits), round(lat, ndigits)])
    return ring


# ─── Incidence-based richness ─────────────────────────────────────────────────

def _f0_hat(T, Q1, Q2):
    """Estimated number of species never detected (Chao2 bias-corrected)."""
    if T <= 0 or Q1 <= 0:
        return 0.0
    k = (T - 1) / T
    if Q2 > 0:
        return k * Q1 * Q1 / (2 * Q2)
    return k * Q1 * (Q1 - 1) / 2


def _coverage_A(T, Q1, Q2):
    """The A term shared by the coverage estimator and its extrapolation."""
    if Q2 > 0:
        return (T - 1) * Q1 / ((T - 1) * Q1 + 2 * Q2)
    if Q1 > 0:
        return (T - 1) * (Q1 - 1) / ((T - 1) * (Q1 - 1) + 2)
    return 1.0


def richness_stats(incidence, T):
    """Chao2, sample coverage and completeness from incidence frequencies.

    Args:
        incidence: iterable of per-species incidence counts (number of
            sampling units each detected species was recorded in), all >= 1.
        T: number of sampling units.

    Returns:
        dict with S_obs, Q1, Q2, U, T, chao2, coverage, completeness and
        ``estimable`` (False when T is below MIN_UNITS_FOR_ESTIMATE, in which
        case chao2/coverage/completeness are None).
    """
    ys = [int(y) for y in incidence if y and y > 0]
    S = len(ys)
    Q1 = sum(1 for y in ys if y == 1)
    Q2 = sum(1 for y in ys if y == 2)
    U = sum(ys)
    out = {'S_obs': S, 'Q1': Q1, 'Q2': Q2, 'U': U, 'T': int(T),
           'chao2': None, 'coverage': None, 'completeness': None,
           'estimable': T >= MIN_UNITS_FOR_ESTIMATE}
    if not out['estimable']:
        return out
    if S == 0:
        # A long, clean run of units with nothing in them: the sample is as
        # complete as it can be, there is simply nothing to find.
        out.update(chao2=0.0, coverage=1.0, completeness=1.0)
        return out
    chao2 = S + _f0_hat(T, Q1, Q2)
    coverage = 1 - (Q1 / U) * _coverage_A(T, Q1, Q2)
    out.update(chao2=chao2, coverage=max(0.0, min(1.0, coverage)),
               completeness=S / chao2 if chao2 > 0 else 1.0)
    return out


def _log_choose(n, k):
    if k < 0 or k > n:
        return float('-inf')
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def expected_richness(incidence, T, t):
    """Expected species richness after ``t`` sampling units.

    Sample-based rarefaction for t <= T, Chao et al. (2014) extrapolation
    beyond T. Returns a float.
    """
    ys = [int(y) for y in incidence if y and y > 0]
    S = len(ys)
    if t <= 0 or T <= 0:
        return 0.0
    if t <= T:
        total = 0.0
        denom = _log_choose(T, t)
        for y in ys:
            num = _log_choose(T - y, t)
            total += 1 - (math.exp(num - denom) if num != float('-inf') else 0.0)
        return total
    Q1 = sum(1 for y in ys if y == 1)
    Q2 = sum(1 for y in ys if y == 2)
    f0 = _f0_hat(T, Q1, Q2)
    if f0 <= 0:
        return float(S)
    m = t - T
    return S + f0 * (1 - (1 - Q1 / (T * f0 + Q1)) ** m)


def accumulation_curve(incidence, T, points=40, extrapolate_to=2.0):
    """[(units, expected S)] from 1 unit up to ``extrapolate_to`` × T.

    The interpolated part ends exactly at (T, S_obs); the rest is the
    extrapolation. ``observed_until`` in the caller is simply T.
    """
    if T <= 0:
        return []
    t_max = max(T, int(round(T * extrapolate_to)))
    steps = sorted({max(1, int(round(t_max * i / points))) for i in range(1, points + 1)} | {T})
    return [(t, round(expected_richness(incidence, T, t), 3)) for t in steps]


def units_to_coverage(incidence, T, target=0.95):
    """Additional sampling units needed to reach ``target`` sample coverage.

    Uses the coverage extrapolation C(T+m) = 1 - Q1/U * A^(m+1) (Chao et al.
    2014). Returns 0 if already reached, None if it cannot be estimated.
    """
    ys = [int(y) for y in incidence if y and y > 0]
    if T < MIN_UNITS_FOR_ESTIMATE or not ys:
        return None
    Q1 = sum(1 for y in ys if y == 1)
    Q2 = sum(1 for y in ys if y == 2)
    U = sum(ys)
    if Q1 == 0:
        return 0
    A = _coverage_A(T, Q1, Q2)
    current = 1 - Q1 / U * A
    if current >= target:
        return 0
    if not 0 < A < 1:
        return None
    m = math.log((1 - target) * U / Q1) / math.log(A) - 1
    return max(0, math.ceil(m))


def units_to_completeness(incidence, T, target=0.9):
    """Additional sampling units until ``target`` × Chao2 species are expected.

    Inverts the richness extrapolation S(T+m) = S + f0 [1 - (1 - a)^m] with
    a = Q1 / (T f0 + Q1). Returns 0 if already reached, None if not estimable.
    """
    ys = [int(y) for y in incidence if y and y > 0]
    if T < MIN_UNITS_FOR_ESTIMATE or not ys:
        return None
    S = len(ys)
    Q1 = sum(1 for y in ys if y == 1)
    Q2 = sum(1 for y in ys if y == 2)
    f0 = _f0_hat(T, Q1, Q2)
    if f0 <= 0:
        return 0
    goal = target * (S + f0)
    if S >= goal:
        return 0
    a = Q1 / (T * f0 + Q1)
    remaining = 1 - (goal - S) / f0
    if not 0 < remaining < 1 or not 0 < a < 1:
        return None
    return max(0, math.ceil(math.log(remaining) / math.log(1 - a)))


# ─── Detection probability ────────────────────────────────────────────────────

def per_unit_detection_probability(units_with_detection, units_at_detected_sites):
    """Naive per-unit detection probability of a species.

    Proportion of sampling units with a record, counted only at locations where
    the species was recorded at least once (i.e. where it is known present).
    Conditioning on known presence biases it slightly upward, so the resulting
    "would have been detected" probabilities are optimistic. Good enough to
    rank cells, not a substitute for an occupancy model.
    """
    if units_at_detected_sites <= 0:
        return None
    return min(1.0, units_with_detection / units_at_detected_sites)


def detection_confidence(p, units):
    """P(at least one record in ``units`` units | species present)."""
    if p is None or units <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    return 1 - (1 - p) ** units


def pool_sensitivity(probabilities, units, detected=()):
    """Share of the regional species pool this sample would have detected.

    For every species of the pool (``{species_id: per-unit p}``) take the
    probability it would have been recorded in ``units`` units if present;
    species actually recorded count as 1. The mean is the expected fraction of
    the pool that the effort here is able to reveal.

    Why this and not only sample coverage: coverage weights species by how
    often they are recorded, so a cell dominated by roe deer and wild boar
    reads as complete long before a lynx could have shown up. On the real
    data (Oct 2026) more than half of the estimable cells had coverage 1.0.
    This measure gives a rare species the same weight as a common one.

    Returns None when the pool is empty.
    """
    if not probabilities:
        return None
    detected = set(detected)
    total = 0.0
    for sid, p in probabilities.items():
        total += 1.0 if sid in detected else detection_confidence(p, units)
    return total / len(probabilities)


def units_for_confidence(p, confidence=0.95):
    """Units needed to detect a present species with ``confidence``."""
    if p is None or p <= 0:
        return None
    if p >= 1:
        return 1
    return math.ceil(math.log(1 - confidence) / math.log(1 - p))


# ─── Time ─────────────────────────────────────────────────────────────────────

def temporal_coverage(days):
    """Calendar coverage of a set of camera-days.

    Args:
        days: iterable of ``date`` objects; duplicates allowed (several cameras
            on the same day count as several camera-days).

    Returns:
        dict: months (list of 12 camera-day counts, Jan..Dec), months_covered,
        seasons_covered, years (sorted list), score (months_covered / 12).
    """
    months = [0] * 12
    years = set()
    for d in days:
        months[d.month - 1] += 1
        years.add(d.year)
    covered = [i + 1 for i, n in enumerate(months) if n >= MIN_CAMERA_DAYS_PER_MONTH]
    seasons = {SEASON_OF_MONTH[m] for m in covered}
    return {'months': months, 'months_covered': len(covered),
            'seasons_covered': len(seasons), 'years': sorted(years),
            'score': len(covered) / 12}


# ─── Scores and composite ─────────────────────────────────────────────────────

def effort_score(camera_days, res_m):
    """Effort mapped to 0..1 on a log scale, saturating at the scale target."""
    target = EFFORT_TARGET_CAMERA_DAYS.get(res_m, 365)
    if camera_days <= 0:
        return 0.0
    return min(1.0, math.log10(1 + camera_days) / math.log10(1 + target))


def composite_score(effort, completeness, temporal):
    """Geometric mean of the three dimensions, each in 0..1.

    Geometric, not arithmetic: a cell that is strong on two dimensions and
    empty on the third is still a gap. A missing value counts as 0. The page
    passes pool sensitivity as ``completeness`` (see pool_sensitivity).
    """
    parts = [effort or 0.0, completeness or 0.0, temporal or 0.0]
    if min(parts) <= 0:
        return 0.0
    return (parts[0] * parts[1] * parts[2]) ** (1 / 3)


# ─── Kernel effort surface ────────────────────────────────────────────────────

def kernel_effort_surface(points, bandwidth_m, pixel_m=None, cutoff_sigmas=3.0):
    """Gaussian kernel density of camera-days.

    Args:
        points: iterable of (x, y, camera_days) in EPSG:3035 metres.
        bandwidth_m: kernel standard deviation; read it as the home-range
            scale of the animals in question.
        pixel_m: output pixel size; defaults to half the bandwidth.
        cutoff_sigmas: kernels are truncated at this many bandwidths.

    Returns:
        (pixel_m, {(x0, y0): camera_days_per_km2}) with x0, y0 the lower-left
        corner of each pixel. Integrating the surface returns total camera-days.
    """
    if pixel_m is None:
        pixel_m = max(250, int(bandwidth_m / 2))
    radius = cutoff_sigmas * bandwidth_m
    two_s2 = 2 * bandwidth_m * bandwidth_m
    norm = 1e6 / (math.pi * two_s2)   # per m² → per km²
    surface = {}
    for x, y, w in points:
        if not w:
            continue
        ix0 = math.floor((x - radius) / pixel_m)
        ix1 = math.floor((x + radius) / pixel_m)
        iy0 = math.floor((y - radius) / pixel_m)
        iy1 = math.floor((y + radius) / pixel_m)
        for ix in range(ix0, ix1 + 1):
            cx = (ix + 0.5) * pixel_m
            dx2 = (cx - x) ** 2
            for iy in range(iy0, iy1 + 1):
                cy = (iy + 0.5) * pixel_m
                d2 = dx2 + (cy - y) ** 2
                if d2 > radius * radius:
                    continue
                key = (ix * pixel_m, iy * pixel_m)
                surface[key] = surface.get(key, 0.0) + w * norm * math.exp(-d2 / two_s2)
    return pixel_m, surface
