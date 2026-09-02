# SPDX-License-Identifier: AGPL-3.0-only
"""Email reminders to verifiers about pending (unidentified) camera-trap series."""
from flask import current_app
from flask_mail import Message
from sqlalchemy import select, or_
from sqlalchemy.orm import sessionmaker

from app.extensions import mail, db
from app.models import Role
from app.camera_traps.access import allowed_institution_ids
from app.camera_traps.database import get_ct_engine
from app.camera_traps.models import (
    Observation, Photo, Identification, Location, location_institutions
)

# Per-user opt-outs live in the host application (biomon: app/utils/
# notification_prefs.py + a `notify_*` column on User). shared-ct must keep
# working in a host that has no such registry, so the import is soft and the
# fallback treats "no preference stored" as still subscribed.
try:
    from app.utils.notification_prefs import is_enabled as _notifications_enabled
except ImportError:  # pragma: no cover - host without the registry
    def _notifications_enabled(user, key):
        return bool(getattr(user, f'notify_{key}', True))

#: Key of this reminder in the host's notification registry.
NOTIFICATION_KEY = 'ct_pending'


def send_identification_reminders():
    """Email every ct_verifier whose pending-series count is high enough.

    Iterates ct_verifier users that have an email, counts their pending series,
    and sends a reminder to those at or above the threshold.

    Returns:
        tuple[int, int]: (sent, skipped) — emails sent and users skipped.
    """
    ct_verifier_role = Role.query.filter_by(name='ct_verifier').first()
    if not ct_verifier_role:
        current_app.logger.info("Role ct_verifier not found, skipping reminders")
        return 0, 0

    users_with_email = [u for u in ct_verifier_role.users.all() if u.email]

    # Opted-out users are dropped before any counting: an unsubscribe must hold
    # even when the person has thousands of pending series.
    opted_out = [u for u in users_with_email
                 if not _notifications_enabled(u, NOTIFICATION_KEY)]
    if opted_out:
        current_app.logger.info(
            "Reminders: %d user(s) opted out of %s", len(opted_out), NOTIFICATION_KEY)
    users_with_email = [u for u in users_with_email
                        if _notifications_enabled(u, NOTIFICATION_KEY)]

    if not users_with_email:
        current_app.logger.info(
            "No subscribed users with role ct_verifier and an email")
        return 0, 0

    engine = get_ct_engine()
    Session = sessionmaker(bind=engine)

    sent = 0
    skipped = 0

    for user in users_with_email:
        ct_session = Session()
        try:
            count = _count_pending_for_user(ct_session, user)
            if count >= 10:
                _send_reminder_email(user, count)
                sent += 1
                current_app.logger.info(
                    f"Reminder sent: {user.email} ({count} series)"
                )
            else:
                skipped += 1
        except Exception as e:
            current_app.logger.error(
                f"Error processing user {user.id} ({user.email}): {e}"
            )
        finally:
            ct_session.close()

    return sent, skipped


def _count_pending_for_user(ct_session, user):
    """Count pending series still available for this user to identify.

    Mirrors the logic of /api/identification-stats.
    """
    user_identified_photos = (
        ct_session.query(Identification.photo_id)
        .filter_by(user_id=user.id)
    )

    is_admin = user.has_role('admin')
    # Camera-trap access only: somebody granted PAM alone must not be reminded
    # about photo series they cannot see.
    user_inst_ids = allowed_institution_ids(user)

    if not is_admin:
        if user_inst_ids:
            allowed_location_ids = select(location_institutions.c.location_id).where(
                location_institutions.c.institution_id.in_(user_inst_ids)
            )
            location_filter = or_(
                Location.visibility_level == 0,
                Location.id.in_(allowed_location_ids)
            )
        else:
            location_filter = (Location.visibility_level == 0)

    query = ct_session.query(Observation.id).filter(
        Observation.status == 'pending',
        ~Observation.photos.any(Photo.id.in_(user_identified_photos))
    )

    if not is_admin:
        query = (
            query
            .join(Location, Observation.location_id == Location.id)
            .filter(location_filter)
        )

    return query.count()


def _send_reminder_email(user, count):
    site_url = current_app.config.get('SITE_URL', 'http://localhost:5000')
    identify_url = f"{site_url}/uk/camera-traps/identify"
    profile_url = f"{site_url}/uk/profile"
    name = user.full_name

    series_word = _pluralize_uk(count, 'серія', 'серії', 'серій')

    msg = Message(
        subject=f"У вас {count} {series_word} для ідентифікації — biomon",
        recipients=[user.email],
    )
    # The opt-out instructions are spelled out click by click on purpose: there
    # is no one-click unsubscribe link (that would need a signed token and a
    # public route), so the letter has to be enough to find the checkbox.
    msg.body = f"""Вітаю, {name}!

У системі фотопасток є {count} {series_word} фотографій, що очікують на вашу ідентифікацію.

Перейдіть за посиланням, щоб розпочати:
{identify_url}

---
Це автоматичне тижневе нагадування від системи biomon.

Не хочете отримувати ці листи? Це можна вимкнути самостійно:
1. Зайдіть на сайт і увійдіть у свій обліковий запис.
2. Відкрийте «Мій профіль»: {profile_url}
3. У розділі «Сповіщення» зніміть галочку про нагадування щодо фотопасток
   і натисніть «Зберегти налаштування сповіщень».
Після цього нагадування більше не надходитимуть; доступ до системи та ваші
права лишаються без змін, і галочку можна повернути будь-коли.

Якщо у вас є питання, зверніться до адміністратора.
"""
    mail.send(msg)


def _pluralize_uk(n, form1, form2, form5):
    """Return the correct Ukrainian noun form for a numeral (1 / 2-4 / 5+ rule)."""
    n = abs(n) % 100
    n1 = n % 10
    if 11 <= n <= 19:
        return form5
    if n1 == 1:
        return form1
    if 2 <= n1 <= 4:
        return form2
    return form5
