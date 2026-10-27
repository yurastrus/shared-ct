# SPDX-License-Identifier: AGPL-3.0-only
"""Which institutions the current person may work with in **this** module.

Institution access used to be one grant covering every module: a row in the
host's ``user_institutions`` meant "sees this institution", full stop. Since
biomon split it per module, the same row carries separate flags for camera traps
and PAM, and this package must ask for the camera-trap ones.

The host is asked through duck typing rather than an import, because shared-ct
also runs in hosts whose ``User`` predates the split (``/var/www/myproject``).
Where the method is missing, the fallback is exactly the old meaning, so nothing
changes for such a host.

Use these helpers instead of touching ``current_user.institutions`` directly —
that attribute is module-blind and would hand camera-trap data to somebody
granted PAM only.
"""

#: Module code this package asks the host about.
MODULE = 'ct'


def _is_authenticated(user):
    return bool(user is not None and getattr(user, 'is_authenticated', False))


def allowed_institution_ids(user):
    """Institution ids visible to ``user`` in camera traps.

    Returns an empty list for anonymous visitors, which every caller already
    treats as "public locations only".
    """
    if not _is_authenticated(user):
        return []
    getter = getattr(user, 'allowed_institution_ids', None)
    if callable(getter):
        return list(getter(MODULE))
    return [inst.id for inst in getattr(user, 'institutions', [])]


def allowed_institutions(user):
    """Institution objects visible to ``user`` in camera traps.

    For the pickers that show institution names; sorting stays with the caller.
    """
    if not _is_authenticated(user):
        return []
    getter = getattr(user, 'module_institutions', None)
    if callable(getter):
        return list(getter(MODULE))
    return list(getattr(user, 'institutions', []))


def export_institution_ids(user):
    """Institution ids ``user`` may download camera-trap data for."""
    if not _is_authenticated(user):
        return []
    getter = getattr(user, 'export_institution_ids', None)
    if callable(getter):
        return list(getter(MODULE))
    return [inst.id for inst in getattr(user, 'export_institutions', [])]


def export_institutions(user):
    """Institution objects ``user`` may download camera-trap data for."""
    if not _is_authenticated(user):
        return []
    getter = getattr(user, 'export_institutions_for', None)
    if callable(getter):
        return list(getter(MODULE))
    return list(getattr(user, 'export_institutions', []))


def has_module_access(user):
    """True if the person holds camera-trap access to at least one institution.

    Used where the old code asked ``bool(current_user.institutions)`` to tell an
    institution-affiliated person from an outside volunteer.
    """
    return bool(allowed_institution_ids(user))


# ── Manager rights, per institution (since 2026-10-27) ──────────────────────
# The ``manager`` role used to make a person manager of every institution they
# could see. The host now keeps a separate per-institution "manages" flag; the
# role is only the gate. Every manager action (upload, editing locations,
# deployments, service log, moderation…) must be scoped by these helpers, not by
# the visibility ones above.


def manage_institution_ids(user):
    """Institution ids ``user`` manages in this module.

    Hosts without the per-institution split fall back to the old meaning: a
    holder of the ``manager`` role manages everything they can see. Admins are
    not special-cased here; callers keep their own ``has_role('admin')`` branch.
    """
    if not _is_authenticated(user):
        return []
    getter = getattr(user, 'manage_institution_ids', None)
    if callable(getter):
        return list(getter(MODULE))
    has_role = getattr(user, 'has_role', None)
    if callable(has_role) and has_role('manager'):
        return allowed_institution_ids(user)
    return []


def manage_institutions(user):
    """Institution objects ``user`` manages in this module."""
    if not _is_authenticated(user):
        return []
    getter = getattr(user, 'manage_institutions', None)
    if callable(getter):
        return list(getter(MODULE))
    has_role = getattr(user, 'has_role', None)
    if callable(has_role) and has_role('manager'):
        return allowed_institutions(user)
    return []


def is_admin(user):
    return bool(_is_authenticated(user) and user.has_role('admin'))


def manages_any(user):
    """True for an admin, or a manager of at least one institution here.

    For showing manager controls (cards, buttons). Routes still check the
    concrete institution of the object they touch.
    """
    return is_admin(user) or bool(manage_institution_ids(user))


def can_manage_institutions(user, institution_ids):
    """True if ``user`` manages at least one of ``institution_ids``.

    "At least one" because a location may belong to several institutions, and
    managing any of them was always enough to edit it.
    """
    if is_admin(user):
        return True
    managed = set(manage_institution_ids(user))
    return bool(managed.intersection(int(i) for i in institution_ids or ()))
