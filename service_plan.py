# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only field plan for camera traps, using the PAM service-plan row format."""
from collections import defaultdict
from datetime import datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from .domain import _
from .models import Deployment, Location, LocationStats, Observation, ServiceVisit, location_institutions
from .service_forecast import INACTIVE_PURPOSE_IDS, forecast_service
from .validity import valid_capture_time_clause


def _display_date(day):
    return day.strftime('%d.%m.%Y') if day else None


def _plan_row(location, stats, inst_ids, visits, deployment, last_capture, lang_code, today):
    latest = visits[0] if visits else None
    last_day = latest.visit_datetime.date() if latest else None
    if deployment and deployment.end_date and last_day and last_day > deployment.end_date:
        deployment = None
    start = deployment.start_date if deployment else None
    end = deployment.end_date if deployment else None
    current_visits = []
    for visit in visits:
        if start and visit.visit_datetime.date() < start:
            break
        if visit.visit_purpose_id in INACTIVE_PURPOSE_IDS:
            break
        current_visits.append(visit)
    current = current_visits[0] if current_visits else None
    battery = next((v for v in current_visits if v.battery_type), None)
    card = next((v for v in current_visits if v.sd_card_changed), None)
    count = next((v for v in current_visits if v.photos_on_card is not None), None)
    baseline = current.visit_datetime.date() if current else start
    rate = stats.avg_photos_per_day if stats else None
    status, reason = 'no_log', _('Немає даних про обслуговування')
    deadline = predicted_photos = limiting = None
    if end and end <= today and (not last_day or end >= last_day):
        status, reason = 'removed', _('Завершене розгортання')
    elif latest and latest.visit_purpose_id in INACTIVE_PURPOSE_IDS and (not start or last_day >= start):
        status, reason = 'inactive', latest.visit_purpose.get_name(lang_code)
    elif current and current.is_camera_operational is False:
        status, reason = 'stopped', _('Камера не працювала під час останнього візиту')
        deadline = baseline
    elif baseline:
        forecast = forecast_service(baseline, today, rate if rate and rate > 0 else None)
        status = 'overdue' if today > forecast['visit_by'] else forecast['status']
        deadline = forecast['visit_by']
        if end and end < deadline:
            deadline = end
            limiting = 'deployment_end'
            days_left = (end - today).days
            if days_left <= 14 and status == 'ok':
                status = 'warning'
            if days_left <= 3 and status in ('ok', 'warning'):
                status = 'critical'
        else:
            limiting = forecast['limiting']
        reason = {
            'deployment_end': _('Завершення розгортання'),
            'photos': _('Прогноз за кількістю фото'),
            'time': _('Прогноз за часом'),
        }[limiting]
        predicted_photos = forecast['predicted_photos']

    return {
        'module': 'ct', 'key': f'ct:{location.id}', 'id': location.id,
        'name': location.name, 'latitude': float(location.latitude),
        'longitude': float(location.longitude), 'inst_ids': inst_ids,
        'status': status, 'status_reason': reason,
        'visit_by': _display_date(deadline),
        'visit_by_iso': deadline.isoformat() if deadline else None,
        'last_visit_date': _display_date(last_day), 'limiting': limiting,
        'device': ' · '.join(filter(None, (deployment.camera_model, deployment.camera_id))) or None
        if deployment else None,
        'battery': battery.battery_type.get_name(lang_code) if battery else None,
        'battery_since': _display_date(battery.visit_datetime) if battery else None,
        'card_since': _display_date(card.visit_datetime) if card else None,
        'last_recording': _display_date(last_capture),
        'battery_days_left': None, 'sd_card_days_left': None, 'card_gb': None,
        'hours_per_day': None, 'lost_days': None,
        'predicted_photos': predicted_photos,
        'photos_on_card': count.photos_on_card if count else None,
        'photos_on_card_date': _display_date(count.visit_datetime) if count else None,
    }


def load_service_plan(session, lang_code, today, *, managed_ids):
    """Return field rows for CT institution IDs. Use None for an authorized admin."""
    if managed_ids is not None and not managed_ids:
        return []
    links = location_institutions
    locations_query = select(Location, LocationStats).outerjoin(LocationStats)
    if managed_ids is not None:
        locations_query = locations_query.where(Location.id.in_(
            select(links.c.location_id).where(links.c.institution_id.in_(managed_ids))))
    locations = session.execute(locations_query.order_by(Location.name, Location.id)).all()
    if not locations:
        return []
    ids = [loc.id for loc, _stats in locations]
    inst_map = defaultdict(list)
    inst_query = select(links.c.location_id, links.c.institution_id).where(links.c.location_id.in_(ids))
    if managed_ids is not None:
        inst_query = inst_query.where(links.c.institution_id.in_(managed_ids))
    for loc_id, inst_id in session.execute(inst_query):
        inst_map[loc_id].append(inst_id)

    visits = defaultdict(list)
    visit_query = (
        select(ServiceVisit).options(joinedload(ServiceVisit.battery_type), joinedload(ServiceVisit.visit_purpose))
        .where(ServiceVisit.location_id.in_(ids),
               ServiceVisit.visit_datetime < datetime.combine(today + timedelta(days=1), time.min))
        .order_by(ServiceVisit.visit_datetime.desc(), ServiceVisit.id.desc())
    )
    for visit in session.scalars(visit_query):
        visits[visit.location_id].append(visit)

    deployments = {}
    deployment_query = (
        select(Deployment).where(Deployment.location_id.in_(ids), Deployment.start_date <= today)
        .order_by(Deployment.start_date.desc(), Deployment.id.desc())
    )
    for deployment in session.scalars(deployment_query):
        deployments.setdefault(deployment.location_id, deployment)

    last_captures = dict(session.execute(
        select(Observation.location_id, func.max(Observation.series_end_time))
        .where(Observation.location_id.in_(ids), valid_capture_time_clause(Observation.series_end_time),
               Observation.series_end_time < datetime.combine(today + timedelta(days=1), time.min))
        .group_by(Observation.location_id)
    ).all())
    return [
        _plan_row(loc, stats, inst_map[loc.id], visits[loc.id], deployments.get(loc.id),
                  last_captures.get(loc.id), lang_code, today)
        for loc, stats in locations
    ]
