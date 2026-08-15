from functools import wraps
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied

def role_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated: return redirect_to_login(request.get_full_path())
            if request.user.role not in roles: raise PermissionDenied
            return view(request, *args, **kwargs)
        return wrapped
    return decorator

