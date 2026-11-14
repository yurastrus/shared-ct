# SPDX-License-Identifier: AGPL-3.0-only
"""Service intervals and estimated photo counts used by the CT service views."""
from datetime import timedelta
from math import ceil

INACTIVE_PURPOSE_IDS = {3, 4}
TIME_WARNING_DAYS = 180
TIME_CRITICAL_DAYS = 300
PHOTO_WARNING_COUNT = 5000
PHOTO_CRITICAL_COUNT = 10000


def forecast_service(visit_day, today, photos_per_day=None):
    """Use the earlier of the elapsed-time and estimated photo thresholds."""
    elapsed = max(0, (today - visit_day).days)
    predicted_photos = int(elapsed * photos_per_day) if photos_per_day else None
    time_status = (elapsed >= TIME_CRITICAL_DAYS) + (elapsed >= TIME_WARNING_DAYS)
    photos = predicted_photos or 0
    photo_status = (photos >= PHOTO_CRITICAL_COUNT) + (photos >= PHOTO_WARNING_COUNT)
    photo_days = ceil(PHOTO_CRITICAL_COUNT / photos_per_day) if photos_per_day else None
    visit_by = visit_day + timedelta(days=min(TIME_CRITICAL_DAYS, photo_days or TIME_CRITICAL_DAYS))
    return {
        'status': ('ok', 'warning', 'critical')[max(time_status, photo_status)],
        'status_by_photos': photo_status > time_status,
        'limiting': 'photos' if photo_days and photo_days < TIME_CRITICAL_DAYS else 'time',
        'visit_by': visit_by,
        'predicted_photos': predicted_photos,
        'days_since_visit': elapsed,
    }
