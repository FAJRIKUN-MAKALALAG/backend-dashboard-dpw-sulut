from flask import Blueprint, current_app, jsonify, request
from werkzeug.security import generate_password_hash

from app.auth import invalidate_user_auth_state, super_admin_required
from app.extensions import db
from app.models import AppUser
from app.utils import DELEGATED_ADMIN_ROLES, DPC_ROLES, DPRT_ROLES, DPW_ROLES, row_to_dict

bp = Blueprint("users", __name__)

ROLES = (
    "SUPER_ADMIN",
    "KETUA_DPW",
    "KETUA_DPD",
    "KETUA_DPC",
    "KETUA_DPRT",
    "ADMIN_DPW",
    "ADMIN_DPD",
    "ADMIN_DPC",
    "ADMIN_DPRT",
    "PENGURUS_DPW",
    "PENGURUS_DPD",
    "PENGURUS_DPC",
    "PENGURUS_DPRT",
    "ADMIN_INVENTORY",
    "WAKIL_BENDAHARA",
)
USER_FIELDS = {
    "id": "id", "member_nik": "memberNik", "nomor_ekta": "nomorEkta", "username": "username",
    "full_name": "fullName", "role": "role", "dpd_access": "dpdAccess", "dpc_access": "dpcAccess",
    "dprt_access": "dprtAccess", "jabatan": "jabatan",
    "delegated_for_ketua_nik": "delegatedForKetuaNik", "is_active": "isActive",
    "created_at": "createdAt", "updated_at": "updatedAt",
}  # password_hash sengaja TIDAK pernah dikirim ke klien
INPUT_MAP = {
    "memberNik": "member_nik", "nomorEkta": "nomor_ekta", "username": "username", "fullName": "full_name",
    "role": "role", "dpdAccess": "dpd_access", "dpcAccess": "dpc_access", "dprtAccess": "dprt_access",
    "jabatan": "jabatan", "delegatedForKetuaNik": "delegated_for_ketua_nik", "isActive": "is_active",
}


def _apply(user: AppUser, data: dict):
    for camel, col in INPUT_MAP.items():
        if camel in data:
            val = data[camel]
            if col == "delegated_for_ketua_nik":
                val = (str(val).strip() or None) if val is not None else None
            setattr(user, col, val)
    if user.role == "INVENTORY":
        user.role = "ADMIN_INVENTORY"
    if user.role not in DELEGATED_ADMIN_ROLES:
        user.delegated_for_ketua_nik = None
    if user.role in (("SUPER_ADMIN", "ADMIN_INVENTORY", "WAKIL_BENDAHARA") + DPW_ROLES):
        user.dpd_access = "ALL"
        user.dpc_access = "ALL"
        user.dprt_access = "ALL"
        if user.role in ("ADMIN_INVENTORY", "WAKIL_BENDAHARA") and not user.jabatan:
            user.jabatan = (
                "Admin Inventaris Kantor DPW Sulut"
                if user.role == "ADMIN_INVENTORY"
                else "Wakil Bendahara DPW Sulut"
            )
    if data.get("password"):
        user.password_hash = generate_password_hash(data["password"].strip())


@bp.get("/users")
@super_admin_required
def list_users():
    rows = AppUser.query.order_by(AppUser.created_at.desc()).all()
    return jsonify(items=[row_to_dict(u, USER_FIELDS) for u in rows])


@bp.post("/users")
@super_admin_required
def create_user():
    data = request.get_json(silent=True) or {}
    if not data.get("username") or not data.get("password") or not data.get("fullName"):
        return jsonify(error="username, password, dan fullName wajib diisi"), 400
    role = data.get("role", "ADMIN_DPD")
    if role == "INVENTORY":
        role = "ADMIN_INVENTORY"
        data["role"] = "ADMIN_INVENTORY"
    if role not in ROLES:
        return jsonify(error="Role tidak valid"), 400
    if role in DPC_ROLES and (not data.get("dpcAccess") or data.get("dpcAccess") == "ALL"):
        return jsonify(error=f"Akun {role} wajib memilih wilayah Kecamatan (DPC) spesifik."), 400
    if role in DPRT_ROLES:
        if not data.get("dpcAccess") or data.get("dpcAccess") == "ALL":
            return jsonify(error=f"Akun {role} wajib memilih wilayah Kecamatan (DPC)."), 400
        if not data.get("dprtAccess") or data.get("dprtAccess") == "ALL":
            return jsonify(error=f"Akun {role} wajib memilih wilayah Desa/Kelurahan (DPRt) spesifik."), 400
    if AppUser.query.filter_by(username=data["username"].strip()).first():
        return jsonify(error="Username sudah dipakai"), 409
    u = AppUser()
    u.username = data["username"].strip()
    u.full_name = data["fullName"].strip()
    u.password_hash = ""
    _apply(u, data)
    db.session.add(u)
    db.session.commit()
    return jsonify(row_to_dict(u, USER_FIELDS)), 201


@bp.put("/users/<uid>")
@super_admin_required
def update_user(uid):
    u = db.session.get(AppUser, uid)
    if not u:
        return jsonify(error="Akun tidak ditemukan"), 404
    data = request.get_json(silent=True) or {}

    master_username = current_app.config.get("MASTER_USERNAME", "7106033003050001")
    is_master = u.username == master_username or u.member_nik == master_username
    if is_master:
        if data.get("isActive") is False:
            return jsonify(error="Akun Super Administrator Utama tidak dapat dinonaktifkan"), 403
        if "role" in data and str(data.get("role") or "").strip().upper() != "SUPER_ADMIN":
            return jsonify(error="Hak akses Super Administrator Utama tidak dapat diturunkan"), 403

    role = data.get("role", u.role)
    if role == "INVENTORY":
        role = "ADMIN_INVENTORY"
        data["role"] = "ADMIN_INVENTORY"
    if "role" in data and role not in ROLES:
        return jsonify(error="Role tidak valid"), 400
    if role in DPC_ROLES and ("dpcAccess" in data and (not data.get("dpcAccess") or data.get("dpcAccess") == "ALL")):
        return jsonify(error=f"Akun {role} wajib memilih wilayah Kecamatan (DPC) spesifik."), 400
    if role in DPRT_ROLES:
        if "dpcAccess" in data and (not data.get("dpcAccess") or data.get("dpcAccess") == "ALL"):
            return jsonify(error=f"Akun {role} wajib memilih wilayah Kecamatan (DPC)."), 400
        if "dprtAccess" in data and (not data.get("dprtAccess") or data.get("dprtAccess") == "ALL"):
            return jsonify(error=f"Akun {role} wajib memilih wilayah Desa/Kelurahan (DPRt) spesifik."), 400

    old_snapshot = (u.role, u.dpd_access, u.dpc_access, u.dprt_access, bool(u.is_active))
    _apply(u, data)
    new_snapshot = (u.role, u.dpd_access, u.dpc_access, u.dprt_access, bool(u.is_active))
    if bool(data.get("password")) or old_snapshot != new_snapshot or not u.is_active:
        u.token_version = int(getattr(u, "token_version", 1) or 1) + 1

    db.session.commit()
    invalidate_user_auth_state(str(u.id))
    return jsonify(row_to_dict(u, USER_FIELDS))


@bp.delete("/users/<uid>")
@super_admin_required
def delete_user(uid):
    u = db.session.get(AppUser, uid)
    if not u:
        return jsonify(error="Akun tidak ditemukan"), 404
    master_username = current_app.config.get("MASTER_USERNAME", "7106033003050001")
    if u.username == master_username or u.member_nik == master_username:
        return jsonify(error="Akun Super Administrator Utama tidak boleh dihapus"), 403
    user_id_str = str(u.id)
    db.session.delete(u)
    db.session.commit()
    invalidate_user_auth_state(user_id_str)
    return jsonify(deleted=uid)
