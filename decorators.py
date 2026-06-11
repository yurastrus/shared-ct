from functools import wraps
from flask import flash, redirect, url_for, g
from flask_login import current_user
from app.camera_traps.domain import _

def role_required(*required_roles):
    """Restrict a view to users holding one of the given roles (or higher).

    Accepts several roles, e.g. ``@role_required('analyst', 'manager')``. Roles are
    resolved against the main database through the privilege hierarchy below.
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Role hierarchy, lowest → highest privilege.
            role_hierarchy = ['viewer', 'ct_verifier', 'analyst', 'manager', 'admin']

            if not current_user.is_authenticated:
                return redirect(url_for('auth.login', lang_code=g.lang_code))

            # Allowed = requested roles plus everything above them in the hierarchy.
            allowed_roles = set()
            for req_role in required_roles:
                if req_role in role_hierarchy:
                    req_index = role_hierarchy.index(req_role)
                    # this role and all higher ones
                    allowed_roles.update(role_hierarchy[req_index:])
                else:
                    # non-hierarchical role: allow as-is
                    allowed_roles.add(req_role)

            has_new_access = any(current_user.has_role(role) for role in allowed_roles)

            if has_new_access:
                return f(*args, **kwargs)
            else:
                flash(_('У вас недостатньо прав для доступу до цієї сторінки.'), 'danger')
                return redirect(url_for('camera_traps.dashboard', lang_code=g.lang_code))
        
        return decorated_function
    return decorator