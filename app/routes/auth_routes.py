from flask import Blueprint, jsonify, request
from flask_jwt_extended import create_access_token
from sqlalchemy import func
from werkzeug.security import check_password_hash

from ..auth import current_claims, login_required
from ..models import AppUser

bp = Blueprint("auth", __name__)


def claims_for(u: AppUser) -> dict:
    return {
        "role": u.role,
        "dpdAccess": u.dpd_access or "ALL",
        "dpcAccess": u.dpc_access or "ALL",
        "dprtAccess": u.dprt_access or "ALL",
        "adminName": u.full_name,
        "adminNik": u.member_nik or u.username,
        "jabatan": u.jabatan or f"Admin {u.role}",
    }


@bp.post("/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    ident = (data.get("username") or "").strip().lower()
    password = (data.get("password") or "").strip()
    if not ident or not password:
        return jsonify(error="Username/NIK dan password wajib diisi"), 400

    user = AppUser.query.filter(
        (func.lower(AppUser.username) == ident)
        | (func.lower(AppUser.member_nik) == ident)
        | (func.lower(AppUser.nomor_ekta) == ident)
    ).first()

    if not user or not check_password_hash(user.password_hash, password):
        return jsonify(error="Akun tidak ditemukan atau password salah"), 401
    if not user.is_active:
        return jsonify(error="Akun dinonaktifkan oleh Super Administrator. Hubungi DPW."), 403

    claims = claims_for(user)
    token = create_access_token(identity=str(user.id), additional_claims=claims)
    return jsonify(token=token, session=claims)


@bp.get("/auth/me")
@login_required
def me():
    c = current_claims()
    keys = ("role", "dpdAccess", "dpcAccess", "dprtAccess", "adminName", "adminNik", "jabatan")
    return jsonify(session={k: c.get(k) for k in keys})
