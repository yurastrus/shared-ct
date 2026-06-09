"""Camera-traps blueprint package."""

from flask import Blueprint

# Blueprint name doubles as the templates folder name.
camera_traps_bp = Blueprint('camera_traps', __name__,
                            template_folder='templates')

from . import routes
from .database import init_ct_database