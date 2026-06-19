# SPDX-License-Identifier: AGPL-3.0-only
from sqlalchemy import text


def _build_matrix(rows):
    """Build the 24×12 count matrix from raw (hour, month, count) rows.

    Pure Python — no DB access. Testable independently from the SQL query.

    Args:
        rows: iterable of (hour_of_day: int, month_of_year: int, count: int)
              hour 0–23, month 1–12.

    Returns:
        dict with keys:
            'matrix': list[list[int]], shape 24 × 12 (hour × month-index 0-based)
            'max_count': int
            'total': int
    """
    matrix = [[0] * 12 for _ in range(24)]

    for hour, month, count in rows:
        h = int(hour)
        m = int(month) - 1  # month 1–12 → index 0–11
        if 0 <= h <= 23 and 0 <= m <= 11:
            matrix[h][m] += int(count)

    max_count = max(
        (matrix[h][m] for h in range(24) for m in range(12)),
        default=0,
    )
    total = sum(matrix[h][m] for h in range(24) for m in range(12))

    return {'matrix': matrix, 'max_count': max_count, 'total': total}


def fetch_date_range(session):
    """Return (min_date, max_date) of completed/archived observations.

    Fast query — series_start_time is indexed. Returns (None, None) if the
    table is empty.
    """
    row = session.execute(text("""
        SELECT MIN(DATE(series_start_time)), MAX(DATE(series_start_time))
        FROM observations
        WHERE status IN ('completed', 'archived')
          AND location_id IN (SELECT id FROM locations WHERE is_valid IS NOT FALSE)
    """)).fetchone()
    if row and row[0] and row[1]:
        return row[0], row[1]
    return None, None


def fetch_heatmap_data(session, species_id, location_ids=None,
                       start_date=None, end_date=None):
    """Query ct_db and return a 24×12 matrix of verified-registration counts.

    Uses the same consensus CTE as fetch_raw_daily_data in daily_analytics.py:
    winner per observation = highest vote_count, ties broken by max_quantity.
    Only observations with status 'completed' or 'archived' are counted.

    Args:
        session: SQLAlchemy session connected to ct_db (PostgreSQL).
        species_id: int — the species to aggregate.
        location_ids: optional list[int] of Location.id to restrict the query.
        start_date: optional date/str — lower bound (inclusive).
        end_date: optional date/str — upper bound (inclusive).
    """
    location_clause = "AND o.location_id IN :location_ids" if location_ids else ""
    date_clause = (
        "AND DATE(o.series_start_time) BETWEEN :start_date AND :end_date"
        if start_date and end_date else ""
    )

    sql = f"""
        WITH ObservationConsensus AS (
            SELECT
                p.observation_id,
                i.species_id,
                COUNT(DISTINCT i.user_id) AS vote_count,
                MAX(i.quantity)           AS max_quantity
            FROM identifications i
            JOIN photos p ON i.photo_id = p.id
            GROUP BY p.observation_id, i.species_id
        ),
        RankedConsensus AS (
            SELECT
                observation_id,
                species_id,
                ROW_NUMBER() OVER (
                    PARTITION BY observation_id
                    ORDER BY vote_count DESC, max_quantity DESC
                ) AS rn
            FROM ObservationConsensus
        )
        SELECT
            CAST(EXTRACT(HOUR  FROM o.series_start_time) AS INTEGER) AS hour_of_day,
            CAST(EXTRACT(MONTH FROM o.series_start_time) AS INTEGER) AS month_of_year,
            COUNT(*) AS detection_count
        FROM observations o
        JOIN RankedConsensus rc ON o.id = rc.observation_id AND rc.rn = 1
        WHERE rc.species_id = :species_id
          AND o.status IN ('completed', 'archived')
          AND o.location_id IN (SELECT id FROM locations WHERE is_valid IS NOT FALSE)
          {location_clause}
          {date_clause}
        GROUP BY hour_of_day, month_of_year
    """

    params = {'species_id': species_id}
    if location_ids:
        params['location_ids'] = tuple(location_ids)
    if start_date and end_date:
        params['start_date'] = str(start_date)
        params['end_date'] = str(end_date)

    rows = session.execute(text(sql), params).fetchall()
    return _build_matrix(rows)
