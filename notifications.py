"""Email reminders to verifiers about pending (unidentified) camera-trap series."""
from flask import current_app
from flask_mail import Message
from sqlalchemy import select, or_
from sqlalchemy.orm import sessionmaker

from app.extensions import mail, db
from app.models import Role
from app.camera_traps.database import get_ct_engine
from app.camera_traps.models import (
    Observation, Photo, Identification, Location, location_institutions
)


def send_identification_reminders():
    """Email every ct_verifier whose pending-series count is high enough.

    Iterates ct_verifier users that have an email, counts their pending series,
    and sends a reminder to those at or above the threshold.

    Returns:
        tuple[int, int]: (sent, skipped) — emails sent and users skipped.
    """
    ct_verifier_role = Role.query.filter_by(name='ct_verifier').first()
    if not ct_verifier_role:
        current_app.logger.info("Роль ct_verifier не знайдена, пропускаємо нагадування")
        return 0, 0

    users_with_email = [u for u in ct_verifier_role.users.all() if u.email]
    if not users_with_email:
        current_app.logger.info("Немає користувачів з роллю ct_verifier та email")
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
                    f"Нагадування надіслано: {user.email} ({count} серій)"
                )
            else:
                skipped += 1
        except Exception as e:
            current_app.logger.error(
                f"Помилка при обробці користувача {user.id} ({user.email}): {e}"
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
    user_inst_ids = [inst.id for inst in user.institutions]

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
    site_url = current_app.config.get('SITE_URL', 'http://91.99.138.240:82')
    identify_url = f"{site_url}/uk/camera-traps/identify"
    name = user.full_name

    series_word = _pluralize_uk(count, 'серія', 'серії', 'серій')

    msg = Message(
        subject=f"У вас {count} {series_word} для ідентифікації — biomon",
        recipients=[user.email],
    )
    msg.body = f"""Вітаю, {name}!

У системі фотопасток є {count} {series_word} фотографій, що очікують на вашу ідентифікацію.

Перейдіть за посиланням, щоб розпочати:
{identify_url}

---
Це автоматичне тижневе нагадування від системи biomon.
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
