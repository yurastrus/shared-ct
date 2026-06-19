# SPDX-License-Identifier: AGPL-3.0-only
"""Shared helpers for the admin-only location data-validity flag.

A location with ``is_valid = False`` is excluded from dashboards, exports, and
analytics aggregation. Use these helpers at every query site so the rule stays
consistent across the codebase.
"""
from sqlalchemy import select

from .models import Location


def valid_location_id_subquery():
    """Scalar subquery of location ids that are valid (for ORM ``.in_(...)``)."""
    return select(Location.id).where(Location.is_valid.is_(True))


# For raw-SQL query sites that alias the locations table (e.g. ``locations l``).
# ``IS NOT FALSE`` treats a NULL flag as valid, which is defensive even though
# the column is NOT NULL DEFAULT TRUE.
VALID_LOCATION_SQL = "is_valid IS NOT FALSE"
