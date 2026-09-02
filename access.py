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
