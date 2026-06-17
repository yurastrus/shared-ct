# SPDX-License-Identifier: AGPL-3.0-only
import os
from flask_babel import Domain

_here = os.path.dirname(os.path.abspath(__file__))
ct_domain = Domain(
    translation_directories=[os.path.join(_here, 'translations')],
    domain='camera_traps',
)


def _(string, **kw):
    """gettext with fallback to the messages domain for cross-module strings."""
    raw = ct_domain.get_translations().gettext(string)
    if raw != string:
        return (raw % kw) if kw else raw
    from flask_babel import gettext
    return gettext(string, **kw)


_l = ct_domain.lazy_gettext
