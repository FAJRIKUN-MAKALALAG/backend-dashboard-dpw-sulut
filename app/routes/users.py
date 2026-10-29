from flask import Blueprint, current_app, jsonify, request
from werkzeug.security import generate_password_hash

from ..auth import super_admin_required
from ..extensions import db
from ..models import AppUser
from ..utils import row_to_dict

bp = Blueprint("users", __name__)

ROLES = ("SUPER_ADMIN", "ADMIN_DPW", "ADMIN_DPD", "ADMIN_DPC", "ADMIN_DPRT")
USER_FIELDS = {
    "id": "id", "member_nik": "memberNik", "nomor_ekta": "nomorEkta", "username": "username",
    "full_name": "fullName", "role": "role", "dpd_access": "dpdAccess", "dpc_access": "dpcAccess",
    "dprt_access": "dprtAccess", "jabatan": "jabatan", "is_active": "isActive",
    "created_at": "createdAt", "updated_at": "updatedAt",
}  # password_hash sengaja TIDAK pernah dikirim ke klien
INPUT_MAP = {
    "memberNik": "member_nik", "nomorEkta": "nomor_ekta", "username": "username", "fullName": "full_name",
    "role": "role", "dpdAccess": "dpd_access", "dpcAccess": "dpc_access", "dprtAccess": "dprt_access",
    "jabatan": "jabatan", "isActive": "is_active",
}


def _apply(user: AppUser, data: dict):
    for camel, col in INPUT_MAP.items():
        if camel in data:
            setattr(user, col, data[camel])
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
    if role not in ROLES:
        return jsonify(error="Role tidak valid"), 400
    if role == "ADMIN_DPC" and (not data.get("dpcAccess") or data.get("dpcAccess") == "ALL"):
        return jsonify(error="Akun Admin DPC wajib memilih wilayah Kecamatan (DPC) spesifik."), 400
    if role == "ADMIN_DPRT":
        if not data.get("dpcAccess") or data.get("dpcAccess") == "ALL":
            return jsonify(error="Akun Admin DPRt wajib memilih wilayah Kecamatan (DPC)."), 400
        if not data.get("dprtAccess") or data.get("dprtAccess") == "ALL":
            return jsonify(error="Akun Admin DPRt wajib memilih wilayah Desa/Kelurahan (DPRt) spesifik."), 400
    if AppUser.query.filter_by(username=data["username"].strip()).first():
        return jsonify(error="Username sudah dipakai"), 409
    u = AppUser(username=data["username"].strip(), full_name=data["fullName"], password_hash="")
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
    role = data.get("role", u.role)
    if "role" in data and role not in ROLES:
        return jsonify(error="Role tidak valid"), 400
    if role == "ADMIN_DPC" and ("dpcAccess" in data and (not data.get("dpcAccess") or data.get("dpcAccess") == "ALL")):
        return jsonify(error="Akun Admin DPC wajib memilih wilayah Kecamatan (DPC) spesifik."), 400
    if role == "ADMIN_DPRT":
        if "dpcAccess" in data and (not data.get("dpcAccess") or data.get("dpcAccess") == "ALL"):
            return jsonify(error="Akun Admin DPRt wajib memilih wilayah Kecamatan (DPC)."), 400
        if "dprtAccess" in data and (not data.get("dprtAccess") or data.get("dprtAccess") == "ALL"):
            return jsonify(error="Akun Admin DPRt wajib memilih wilayah Desa/Kelurahan (DPRt) spesifik."), 400
    _apply(u, data)
    db.session.commit()
    return jsonify(row_to_dict(u, USER_FIELDS))


@bp.delete("/users/<uid>")
@super_admin_required
def delete_user(uid):
    u = db.session.get(AppUser, uid)
    if not u:
        return jsonify(error="Akun tidak ditemukan"), 404
    if u.username == current_app.config.get("MASTER_USERNAME", "7106033003050001"):
        return jsonify(error="Akun Super Administrator Utama tidak boleh dihapus"), 403
    db.session.delete(u)
    db.session.commit()
    return jsonify(deleted=uid)
