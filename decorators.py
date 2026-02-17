# myproject/app/camera_traps/decorators.py

from functools import wraps
from flask import flash, redirect, url_for, g
from flask_login import current_user
from flask_babel import gettext as _

def role_required(required_role):
    """
    Декоратор для перевірки, чи має користувач необхідну роль для доступу.
    Враховує ієрархію ролей.
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Ієрархія ролей: кожна наступна роль включає права попередньої.
            role_hierarchy = ['viewer', 'identifier', 'data_user', 'moderator', 'admin']

            if not current_user.is_authenticated:
                return redirect(url_for('auth.login', lang_code=g.lang_code))

            user_profile = current_user.get_ct_profile()
            user_role = user_profile.camera_trap_role
            
            # Якщо роль користувача відсутня в ієрархії, забороняємо доступ
            if user_role not in role_hierarchy:
                flash(_('У вас недостатньо прав для доступу до цієї сторінки.'), 'danger')
                return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))

            # Перевіряємо, чи рівень доступу користувача не нижчий за необхідний
            if role_hierarchy.index(user_role) >= role_hierarchy.index(required_role):
                return f(*args, **kwargs) # Доступ дозволено
            else:
                flash(_('У вас недостатньо прав для доступу до цієї сторінки.'), 'danger')
                return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))
        
        return decorated_function
    return decorator