# SPDX-License-Identifier: AGPL-3.0-only
import numpy as np
from scipy.stats import gaussian_kde
from sqlalchemy import text
import io
import csv

def fetch_raw_daily_data(session, start_date, end_date, species_ids, location_ids=None,
                         qc_exclude=None):
    """Fetch the exact observation times (in decimal hours) for each location.

    Returns a dict: { species_id: { location_id: [12.5, 14.2, ...], ... } }.

    Args:
        location_ids: optional list of Location.id to restrict the result to
            specific locations (for institution/ecoregion filtering).
            None means "all locations".
        qc_exclude: optional list of QC flag names; observations whose
            overlapping deployment raises any selected flag are excluded
            (reuses the shared #30 exclusion logic). Affects detections only,
            not effort/trap-days.
    """
    from .data_export import _build_qc_exclusion_cond
    # SQL query that returns decimal hours (e.g. 13:30 -> 13.5).
    # Uses the consensus CTE.
    location_clause = "AND o.location_id IN :location_ids" if location_ids else ""
    qc_pred = _build_qc_exclusion_cond(qc_exclude or [], obs_alias='o')
    qc_clause = ("AND " + qc_pred) if qc_pred else ""

    query_sql = f"""
        WITH ObservationConsensus AS (
            SELECT
                p.observation_id, i.species_id,
                COUNT(DISTINCT i.user_id) as vote_count,
                MAX(i.quantity) as max_quantity
            FROM identifications i JOIN photos p ON i.photo_id = p.id
            GROUP BY p.observation_id, i.species_id
        ),
        RankedConsensus AS (
            SELECT
                observation_id, species_id,
                ROW_NUMBER() OVER(PARTITION BY observation_id ORDER BY vote_count DESC, max_quantity DESC) as rn
            FROM ObservationConsensus
        )
        SELECT
            rc.species_id,
            o.location_id,
            EXTRACT(EPOCH FROM o.series_start_time::time)/3600.0 as decimal_hour
        FROM observations o
        JOIN RankedConsensus rc ON o.id = rc.observation_id AND rc.rn = 1
        WHERE
            rc.species_id IN :species_ids
            AND o.status IN ('completed', 'archived')
            AND DATE(o.series_start_time) BETWEEN :start_date AND :end_date
            AND o.location_id IN (SELECT id FROM locations WHERE is_valid IS NOT FALSE)
            {location_clause}
            {qc_clause}
    """

    params = {
        'species_ids': tuple(species_ids),
        'start_date': start_date,
        'end_date': end_date
    }
    if location_ids:
        params['location_ids'] = tuple(location_ids)

    # Run the query.
    rows = session.execute(text(query_sql), params).fetchall()

    # Group the data: Species -> Location -> [Hours].
    data = {sp_id: {} for sp_id in species_ids}

    for row in rows:
        sp_id, loc_id, dec_hour = row
        if sp_id not in data:
            data[sp_id] = {}
        if loc_id not in data[sp_id]:
            data[sp_id][loc_id] = []
        data[sp_id][loc_id].append(float(dec_hour))

    return data

def calculate_activity_curve(raw_data_by_loc, total_effort, mode='rai', n_boot=1000, compute_ci=False, bw_adjust=0.1):
    """Universal activity-curve builder.

    If bw_adjust > 0: uses KDE (smoothing).
    If bw_adjust == 0: uses binning (raw histograms).
    """
    locations_with_detections = list(raw_data_by_loc.keys())
    if not locations_with_detections:
        return None

    # Convert the data into numpy arrays.
    loc_arrays = [np.array(raw_data_by_loc[lid]) for lid in locations_with_detections]
    num_locs = len(loc_arrays)

    # === OPTION 1: BINNING (RAW DATA) ===
    if bw_adjust <= 0.001:
        # X grid - just the hours from 0 to 23.
        x_grid = np.arange(24)

        # 1. Build a histogram for EACH location separately.
        # This is needed for a correct per-location bootstrap.
        loc_histograms = []
        for times in loc_arrays:
            # bins=24, range=(0,24) splits into hours [0,1), [1,2)...
            hist, _ = np.histogram(times, bins=24, range=(0, 24))
            loc_histograms.append(hist)

        loc_histograms = np.array(loc_histograms)  # shape: (num_locs, 24)

        # Aggregate and normalize a single sample.
        def process_sample_binning(hist_matrix):
            # Sum activity across all locations in the sample.
            total_hist = np.sum(hist_matrix, axis=0)  # shape: (24,)

            if mode == 'rai':
                # RAI = (Count / Effort) * 100
                # total_effort - the total effort for the period.
                return (total_hist * 100.0) / max(1, total_effort)
            else:
                # Percent = (Count / Total Counts) * 100
                total_counts = np.sum(total_hist)
                return (total_hist * 100.0) / max(1, total_counts)

        # If CI is not needed, just compute over all available data.
        if not compute_ci:
            mean_curve = process_sample_binning(loc_histograms)
            return {
                'hours': x_grid.tolist(),
                'mean': mean_curve.tolist(),
                'ci_lower': None, 'ci_upper': None
            }

        # If CI is needed - bootstrap.
        boot_curves = []
        for _ in range(n_boot):
            # Sample location indices with replacement.
            indices = np.random.randint(0, num_locs, num_locs)
            sample_hists = loc_histograms[indices]

            curve = process_sample_binning(sample_hists)
            boot_curves.append(curve)

        boot_matrix = np.array(boot_curves)

    # === OPTION 2: KDE (SMOOTHING) ===
    else:
        # Detailed X grid (128 points).
        x_grid = np.linspace(0, 24, 128)
        boot_curves = []

        # KDE for a single sample.
        def process_sample_kde(times_array):
             # Wrap-around (circular) handling.
            extended_data = np.concatenate([times_array - 24, times_array, times_array + 24])
            try:
                kde = gaussian_kde(extended_data, bw_method=bw_adjust)
                y_curve = kde(x_grid) * 3  # *3 because the data was tripled

                if mode == 'rai':
                    scale = (len(times_array) * 100.0) / max(1, total_effort)
                    return y_curve * scale
                else:
                    return y_curve * 100.0
            except:
                return np.zeros_like(x_grid)

        if not compute_ci:
            all_times = np.concatenate(loc_arrays)
            if len(all_times) < 2: return None
            mean_curve = process_sample_kde(all_times)
            return {
                'hours': x_grid.tolist(),
                'mean': mean_curve.tolist(),
                'ci_lower': None, 'ci_upper': None
            }

        # Bootstrap.
        for _ in range(n_boot):
            indices = np.random.randint(0, num_locs, num_locs)
            sample_times = np.concatenate([loc_arrays[i] for i in indices])

            if len(sample_times) < 2: continue

            curve = process_sample_kde(sample_times)
            boot_curves.append(curve)

        boot_matrix = np.array(boot_curves)

    # === FINAL STATISTICS (shared by both methods) ===
    if len(boot_curves) == 0: return None

    # Convert to np.array if not already done.
    boot_matrix = np.array(boot_curves)

    mean_curve = np.mean(boot_matrix, axis=0)

    # If compute_ci=True, compute the percentiles.
    if compute_ci:
        ci_lower = np.percentile(boot_matrix, 2.5, axis=0)
        ci_upper = np.percentile(boot_matrix, 97.5, axis=0)
    else:
        ci_lower = None
        ci_upper = None

    return {
        'hours': x_grid.tolist(),
        'mean': mean_curve.tolist(),
        'ci_lower': ci_lower.tolist() if ci_lower is not None else None,
        'ci_upper': ci_upper.tolist() if ci_upper is not None else None,
        # Return the raw matrix (1000 rows); needed to compute the overlap CI.
        'boot_matrix': boot_matrix
    }

def calculate_overlap_coefficient(curve_a, curve_b):
    """Compute the overlap coefficient (Ridout & Linkie Δ).

    Input: arrays of Y values.
    Output: a number in 0..1 (or 0..100 in %).
    """
    a = np.array(curve_a)
    b = np.array(curve_b)

    # Guard against empty arrays.
    if len(a) == 0 or len(b) == 0 or np.sum(a) == 0 or np.sum(b) == 0:
        return 0.0

    # Normalize: the area under the curve must equal 1 (so these become probabilities).
    prob_a = a / np.sum(a)
    prob_b = b / np.sum(b)

    # Δ = the sum of the pointwise minima.
    mins = np.minimum(prob_a, prob_b)
    overlap = np.sum(mins)

    return float(overlap)  # Return as float (0.0 - 1.0)

def calculate_overlap_matrix(species_data):
    """Compute the overlap matrix.

    If 'boot_matrix' is present (CI was enabled), also compute the overlap CI.
    """
    ids = sorted(list(species_data.keys()))
    matrix = {}

    for id_a in ids:
        matrix[id_a] = {}
        for id_b in ids:
            if id_a == id_b:
                # Self-overlap = 1.0 (no CI).
                matrix[id_a][id_b] = {'mean': 1.0, 'lower': None, 'upper': None}
                continue

            # Data for species A and B (use the 'percent' branch, though 'rai' is also fine after normalization).
            data_a = species_data[id_a]['percent']
            data_b = species_data[id_b]['percent']

            # --- OPTION 1: Bootstrap matrices present (compute the overlap CI) ---
            if 'boot_matrix' in data_a and 'boot_matrix' in data_b and \
               data_a['boot_matrix'] is not None and data_b['boot_matrix'] is not None:

                mat_a = data_a['boot_matrix']
                mat_b = data_b['boot_matrix']

                # Check whether the iteration count matches (e.g. 1000).
                n_boot = min(len(mat_a), len(mat_b))

                overlaps = []
                # Iterate over each bootstrap iteration.
                for i in range(n_boot):
                    # Compute the overlap between the i-th curve A variant and the i-th curve B variant.
                    ov = calculate_overlap_coefficient(mat_a[i], mat_b[i])
                    overlaps.append(ov)

                overlaps = np.array(overlaps)

                # Compute statistics over the 1000 coefficients.
                mean_ov = np.mean(overlaps)
                lower_ov = np.percentile(overlaps, 2.5)
                upper_ov = np.percentile(overlaps, 97.5)

                matrix[id_a][id_b] = {
                    'mean': float(mean_ov),
                    'lower': float(lower_ov),
                    'upper': float(upper_ov)
                }

            # --- OPTION 2: No bootstrap (mean only) ---
            else:
                score = calculate_overlap_coefficient(data_a['mean'], data_b['mean'])
                matrix[id_a][id_b] = {
                    'mean': float(score),
                    'lower': None,
                    'upper': None
                }

    return {
        'order': ids,
        'data': matrix
    }

def generate_csv_export(species_data, species_names):
    """Generate a CSV file; correctly handles a missing CI."""
    output = io.StringIO()
    writer = csv.writer(output)

    # Headers.
    header = ['Hour']
    sp_ids = sorted(species_data.keys())

    for sp_id in sp_ids:
        name = species_names.get(sp_id, f"Species {sp_id}")
        header.extend([f"{name} (Mean)", f"{name} (Lower CI)", f"{name} (Upper CI)"])

    writer.writerow(header)

    # Guard against empty data.
    if not sp_ids:
        return ""

    # Take the hours from the first species.
    first_sp_data = species_data[sp_ids[0]]['rai']
    if not first_sp_data: return ""

    hours = first_sp_data['hours']

    for i, hour in enumerate(hours):
        # Format the time: integers (0, 1...) as integers, otherwise with a fractional part.
        val = float(hour)  # Force conversion to float
        time_str = f"{val:.0f}" if val.is_integer() else f"{val:.2f}"
        row = [time_str]

        for sp_id in sp_ids:
            data = species_data[sp_id]['rai']
            if data:
                mean = f"{data['mean'][i]:.4f}"

                # Check whether the intervals exist.
                lower = f"{data['ci_lower'][i]:.4f}" if data['ci_lower'] is not None else ""
                upper = f"{data['ci_upper'][i]:.4f}" if data['ci_upper'] is not None else ""

                row.extend([mean, lower, upper])
            else:
                row.extend(["", "", ""])
        writer.writerow(row)

    output.seek(0)
    return output.getvalue()
