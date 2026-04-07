from functools import wraps
from flask import flash, redirect, url_for, g
from flask_login import current_user
from flask_babel import gettext as _

def role_required(*required_roles):
    """
    Декоратор для перевірки, чи має користувач необхідну роль для доступу.
    Підтримує передачу декількох ролей, наприклад: @role_required('analyst', 'manager').
    Ролі перевіряються тільки по новій системі (головна база даних).
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Ієрархія ролей нової системи.
            role_hierarchy = ['viewer', 'ct_verifier', 'analyst', 'manager', 'admin']

            if not current_user.is_authenticated:
                return redirect(url_for('auth.login', lang_code=g.lang_code))

            # Формуємо список усіх дозволених ролей (передані + ті, що вище за ієрархією)
            allowed_roles = set()
            for req_role in required_roles:
                if req_role in role_hierarchy:
                    req_index = role_hierarchy.index(req_role)
                    # Додаємо цю роль і всі, що вище
                    allowed_roles.update(role_hierarchy[req_index:])
                else:
                    # Якщо роль нестандартна (не з ієрархії), просто додаємо її
                    allowed_roles.add(req_role)

            # Перевірка за новою системою (головна база)
            has_new_access = any(current_user.has_role(role) for role in allowed_roles)

            if has_new_access:
                return f(*args, **kwargs)
            else:
                flash(_('У вас недостатньо прав для доступу до цієї сторінки.'), 'danger')
                return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))
        
        return decorated_function
    return decorator