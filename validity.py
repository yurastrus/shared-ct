# SPDX-License-Identifier: AGPL-3.0-only
"""Shared helpers for the admin-only location data-validity flag.

A location with ``is_valid = False`` is excluded from dashboards, exports, and
analytics aggregation. Use these helpers at every query site so the rule stays
consistent across the codebase.
"""
from datetime import date, datetime, timedelta

from sqlalchemy import and_, select

from .models import Location
from .utils import EXIF_MIN_VALID_DATE


def valid_location_id_subquery():
    """Scalar subquery of location ids that are valid (for ORM ``.in_(...)``)."""
    return select(Location.id).where(Location.is_valid.is_(True))


# For raw-SQL query sites that alias the locations table (e.g. ``locations l``).
# ``IS NOT FALSE`` treats a NULL flag as valid, which is defensive even though
# the column is NOT NULL DEFAULT TRUE.
VALID_LOCATION_SQL = "is_valid IS NOT FALSE"


# Capture-time validity. Photos without EXIF are stored with a 1900-01-01
# placeholder (see utils.process_single_photo) so they stay visible on
# /identify; once verified, nothing else stops them from reaching the
# analytics as a "1900" year. Precomputed analytics count only photos inside
# EXIF_MIN_VALID_DATE .. today + 2 days; the placeholder series stay in the database.
MIN_VALID_CAPTURE_YEAR = EXIF_MIN_VALID_DATE.year


def valid_capture_time_clause(column):
    """SQL condition: ``column`` (a capture timestamp) is a plausible date."""
    upper = datetime.combine(date.today() + timedelta(days=2), datetime.min.time())
    return and_(column >= EXIF_MIN_VALID_DATE, column < upper)


def plausible_years(years):
    """Drop years outside MIN_VALID_CAPTURE_YEAR .. current year (for UI lists)."""
    this_year = date.today().year
    return [y for y in years
            if y is not None and MIN_VALID_CAPTURE_YEAR <= int(y) <= this_year]
