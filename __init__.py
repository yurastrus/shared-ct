# myproject/app/camera_traps/__init__.py

from flask import Blueprint

# Створюємо Blueprint. 'camera_traps' - це і ім'я Blueprint, і папка з шаблонами
camera_traps_bp = Blueprint('camera_traps', __name__,
                            template_folder='templates')

from . import routes
from .database import init_ct_database