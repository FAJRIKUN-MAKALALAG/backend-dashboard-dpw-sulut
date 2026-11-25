from datetime import datetime, timezone
from functools import wraps
import threading
import time
import uuid

from flask import jsonify
from flask_jwt_extended import get_jwt, verify_jwt_in_request

from app.extensions import db

_AUTH_STATE_TTL = 5.0
_auth_state_lock = threading.Lock()
_auth_state_cache: dict[str, tuple[float, bool, bool, int]] = {}

_PRESENCE_UPDATE_INTERVAL = 60.0
_presence_lock = threading.Lock()
_presence_last_write: dict[str, float] = {}


def touch_user_presence(user_id_str: str, force: bool = False) -> None:
    """Memperbarui timestamp `last_seen_at` pengguna secara ter-throttle (maks. 1x/60 dtk)."""
    clean_id = str(user_id_str or "").strip()
    if not clean_id:
        return
    try:
        parsed_uuid = uuid.UUID(clean_id)
    except ValueError:
        return

    now_mono = time.monotonic()
    with _presence_lock:
        last_ts = _presence_last_write.get(clean_id)
        if not force and last_ts is not None and (now_mono - last_ts) < _PRESENCE_UPDATE_INTERVAL:
            return
        if len(_presence_last_write) > 2048:
            _presence_last_write.clear()
        _presence_last_write[clean_id] = now_mono

    from app.models import AppUser

    try:
        db.session.query(AppUser).filter(AppUser.id == parsed_uuid).update(
            {"last_seen_at": datetime.now(timezone.utc)},
            synchronize_session=False,
        )
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def clear_user_presence(user_id_str: str) -> None:
    """Mengosongkan `last_seen_at` saat user logout eksplisit atau dinonaktifkan."""
    clean_id = str(user_id_str or "").strip()
    if not clean_id:
        return
    with _presence_lock:
        _presence_last_write.pop(clean_id, None)
    try:
        parsed_uuid = uuid.UUID(clean_id)
    except ValueError:
        return

    from app.models import AppUser

    try:
        db.session.query(AppUser).filter(AppUser.id == parsed_uuid).update(
            {"last_seen_at": None},
            synchronize_session=False,
        )
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def invalidate_user_auth_state(user_id: str | None = None) -> None:
    """Menghapus cache status aktif/token_version agar revokasi langsung berlaku."""
    with _auth_state_lock:
        if user_id is None:
            _auth_state_cache.clear()
        else:
            _auth_state_cache.pop(str(user_id).strip(), None)


def get_user_auth_state(user_id_str: str) -> tuple[bool, bool, int]:
    """Mengembalikan `(exists, is_active, token_version)` untuk suatu `app_users.id` (UUID)."""
    clean_id = str(user_id_str or "").strip()
    if not clean_id:
        return True, True, 1

    try:
        parsed_uuid = uuid.UUID(clean_id)
    except ValueError:
        # Subjek test sintetis non-UUID diizinkan tanpa query DB
        return True, True, 1

    now = time.monotonic()
    with _auth_state_lock:
        cached = _auth_state_cache.get(clean_id)
        if cached and now < cached[0]:
            return cached[1], cached[2], cached[3]

    from app.models import AppUser

    try:
        row = (
            db.session.query(AppUser.is_active, AppUser.token_version)
            .filter(AppUser.id == parsed_uuid)
            .first()
        )
    except Exception:
        # Jangan memutus endpoint publik/dev saat DB sementara tidak dapat dijangkau
        return True, True, 1

    if row is None:
        state = (False, False, 0)
    else:
        state = (True, bool(row[0]), int(row[1] or 1))

    with _auth_state_lock:
        if len(_auth_state_cache) > 2048:
            _auth_state_cache.clear()
        _auth_state_cache[clean_id] = (time.monotonic() + _AUTH_STATE_TTL, *state)

    return state


def is_token_revoked(jwt_header: dict, jwt_payload: dict) -> bool:
    """Menolak token JWT jika akun dihapus, dinonaktifkan, atau token_version telah dinaikkan."""
    uid = str(jwt_payload.get("sub") or "").strip()
    if not uid:
        return False

    exists, is_active, db_version = get_user_auth_state(uid)
    if not exists or not is_active:
        return True

    claim_ver = jwt_payload.get("tokenVersion")
    if claim_ver is None:
        revoked = db_version > 1
    else:
        try:
            revoked = int(claim_ver) != int(db_version)
        except (TypeError, ValueError):
            revoked = True

    if not revoked:
        touch_user_presence(uid)
    return revoked


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
inventory_access_required = roles_required("SUPER_ADMIN", "ADMIN_INVENTORY", "INVENTORY", "WAKIL_BENDAHARA")
finance_access_required = roles_required("SUPER_ADMIN", "WAKIL_BENDAHARA")

