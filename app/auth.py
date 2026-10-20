from functools import wraps

from flask import jsonify
from flask_jwt_extended import get_jwt, verify_jwt_in_request


def current_claims() -> dict:
    return get_jwt()


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        verify_jwt_in_request()
        return fn(*a, **kw)

    return wrapper


def roles_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            verify_jwt_in_request()
            if get_jwt().get("role") not in roles:
                return jsonify(error="Anda tidak memiliki hak akses untuk aksi ini"), 403
            return fn(*a, **kw)

        return wrapper

    return deco


super_admin_required = roles_required("SUPER_ADMIN")
