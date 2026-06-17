# SPDX-License-Identifier: AGPL-3.0-only
"""Camera-traps blueprint package."""

from flask import Blueprint

# Blueprint name doubles as the templates folder name.
camera_traps_bp = Blueprint('camera_traps', __name__,
                            template_folder='templates')

from .domain import ct_domain, _ as _ct


@camera_traps_bp.context_processor
def _inject_ct_translations():
    from flask_babel import ngettext as _nmsg

    def _ngettext(string, plural, n):
        t = ct_domain.ngettext(string, plural, n)
        fallback = string if n == 1 else plural
        return t if t != fallback else _nmsg(string, plural, n)

    return {'_': _ct, 'gettext': _ct, 'ngettext': _ngettext}


from . import routes
from .database import init_ct_database