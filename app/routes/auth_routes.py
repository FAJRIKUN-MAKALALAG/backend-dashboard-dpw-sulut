from flask import Blueprint, jsonify, request
from flask_jwt_extended import create_access_token
from sqlalchemy import func
from werkzeug.security import check_password_hash, generate_password_hash

from ..auth import current_claims, login_required
from ..extensions import db
from ..models import AppUser, Member

bp = Blueprint("auth", __name__)


def determine_role(tingkat: str) -> str:
    """Memetakan tingkat kepengurusan organisasi ke level hak akses (Role) sistem."""
    t = (tingkat or "").strip().upper()
    if t == "DPW":
        return "ADMIN_DPW"
    elif t == "DPD":
        return "ADMIN_DPD"
    elif t == "DPC":
        return "ADMIN_DPC"
    elif t in ("DPRT", "RANTING"):
        return "ADMIN_DPRT"
    return "ADMIN_DPRT"


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
    """
    Login sistem Partai NasDem Sulut:
    - Akun Super Admin / Pengurus yang sudah punya password custom -> langsung diverifikasi.
    - Pengurus baru dari data impor -> otomatis login & aktivasi menggunakan Password Default:
      1. 6 digit terakhir NIK KTP (misal: NIK 7105011204850003 -> password '850003')
      2. 'nasdem123'
      3. 'Nasdem@[4 digit terakhir NIK]'
    """
    data = request.get_json(silent=True) or {}
    raw_ident = (data.get("username") or "").strip()
    ident = raw_ident.lower()
    clean_ident = raw_ident.replace(" ", "").replace("-", "").lower()
    password = (data.get("password") or "").strip()

    if not ident or not password:
        return jsonify(error="Username/NIK dan password wajib diisi"), 400

    # 1. Cari di tabel AppUser (akun yang sudah aktif)
    user = AppUser.query.filter(
        (func.lower(AppUser.username) == ident)
        | (func.lower(AppUser.username) == clean_ident)
        | (func.lower(AppUser.member_nik) == ident)
        | (func.lower(AppUser.member_nik) == clean_ident)
        | (func.lower(AppUser.nomor_ekta) == ident)
        | (func.lower(AppUser.nomor_ekta) == clean_ident)
    ).first()

    if user:
        if not user.is_active:
            return jsonify(error="Akun dinonaktifkan oleh Super Administrator. Hubungi DPW."), 403

        # Cek kecocokan password hash ATAU fallback default password
        pwd_match = check_password_hash(user.password_hash, password)
        if not pwd_match:
            # Fallback default password jika belum pernah diganti atau lupa
            nik_clean = (user.member_nik or user.username or "")
            is_default_pwd = (
                password == "nasdem123"
                or (len(nik_clean) >= 6 and password == nik_clean[-6:])
                or (len(nik_clean) >= 4 and password == f"Nasdem@{nik_clean[-4:]}")
            )
            if is_default_pwd:
                user.password_hash = generate_password_hash(password)
                db.session.commit()
                pwd_match = True

        if not pwd_match:
            return jsonify(
                error="Kata sandi salah. Silakan coba 6 digit terakhir NIK atau 'nasdem123', atau klik 'Aktivasi Akun Pengurus'."
            ), 401

        claims = claims_for(user)
        token = create_access_token(identity=str(user.id), additional_claims=claims)
        return jsonify(token=token, session=claims)

    # 2. Jika akun belum ada di AppUser, cari di tabel Member
    member = Member.query.filter(
        (func.lower(Member.nik) == ident)
        | (func.lower(Member.nik) == clean_ident)
        | (func.lower(Member.nomor_ekta) == ident)
        | (func.lower(Member.nomor_ekta) == clean_ident)
    ).first()

    if not member:
        return jsonify(error="NIK atau akun tidak ditemukan di database kepengurusan Partai NasDem Sulut"), 404

    # Cek apakah password cocok dengan format Default Password (Opsi 2):
    nik_str = member.nik or ""
    is_default_match = (
        password == "nasdem123"
        or (len(nik_str) >= 6 and password == nik_str[-6:])
        or (len(nik_str) >= 4 and password == f"Nasdem@{nik_str[-4:]}")
        or (member.nomor_ekta and len(member.nomor_ekta) >= 6 and password == member.nomor_ekta[-6:])
    )

    if not is_default_match:
        hint = nik_str[-6:] if len(nik_str) >= 6 else "nasdem123"
        return jsonify(
            error=f"Kata sandi salah. Pengurus {member.nama_lengkap} dapat menggunakan kata sandi awal (6 digit terakhir NIK: {hint} atau 'nasdem123'), atau klik tombol 'Aktivasi Akun' di bawah."
        ), 401

    # Otomatis buatkan akun AppUser untuk pengurus ini!
    user_role = determine_role(member.tingkat_kepengurusan)
    new_user = AppUser(
        member_nik=member.nik,
        nomor_ekta=member.nomor_ekta,
        username=member.nik,
        password_hash=generate_password_hash(password),
        full_name=member.nama_lengkap,
        role=user_role,
        dpd_access=member.dpd or "ALL",
        dpc_access=member.dpc or "ALL",
        dprt_access=member.dprt or "ALL",
        jabatan=member.jabatan or f"Pengurus {member.tingkat_kepengurusan}",
        is_active=True,
    )
    db.session.add(new_user)
    db.session.commit()

    claims = claims_for(new_user)
    token = create_access_token(identity=str(new_user.id), additional_claims=claims)
    return jsonify(token=token, session=claims, isDefaultPassword=True)


@bp.post("/auth/check-pengurus")
def check_pengurus():
    """Memeriksa apakah NIK/e-KTA terdaftar sebagai pengurus resmi Partai NasDem Sulut."""
    data = request.get_json(silent=True) or {}
    ident = (data.get("nik") or "").strip()
    clean_ident = ident.replace(" ", "").replace("-", "").strip()
    if not clean_ident:
        return jsonify(error="Nomor NIK KTP atau e-KTA wajib diisi"), 400

    member = Member.query.filter(
        (func.lower(Member.nik) == clean_ident.lower())
        | (func.lower(Member.nomor_ekta) == clean_ident.lower())
    ).first()

    if not member:
        return jsonify(
            found=False,
            error="NIK / Nomor e-KTA tidak ditemukan dalam database kepengurusan Partai NasDem Sulut. Silakan hubungi Sekretariat DPW.",
        ), 404

    # Cek apakah sudah terdaftar di AppUser
    user = AppUser.query.filter(
        (AppUser.member_nik == member.nik) | (func.lower(AppUser.username) == member.nik.lower())
    ).first()

    return jsonify({
        "found": True,
        "nik": member.nik,
        "nomorEkta": member.nomor_ekta or "-",
        "namaLengkap": member.nama_lengkap,
        "jabatan": member.jabatan or "Pengurus",
        "tingkatKepengurusan": member.tingkat_kepengurusan or "DPC",
        "dpd": member.dpd,
        "dpc": member.dpc,
        "dprt": member.dprt,
        "hasCustomPassword": user is not None and bool(user.password_hash),
        "defaultPasswordHint": member.nik[-6:] if len(member.nik) >= 6 else "nasdem123",
    })


@bp.post("/auth/activate-pengurus")
def activate_pengurus():
    """Aktivasi akun pengurus atau buat kata sandi baru mandiri (Opsi 1)."""
    data = request.get_json(silent=True) or {}
    ident = (data.get("nik") or "").strip()
    clean_ident = ident.replace(" ", "").replace("-", "").strip()
    new_password = (data.get("newPassword") or "").strip()

    if not clean_ident:
        return jsonify(error="Nomor NIK KTP wajib diisi"), 400
    if not new_password or len(new_password) < 6:
        return jsonify(error="Kata sandi baru minimal 6 karakter"), 400

    member = Member.query.filter(
        (func.lower(Member.nik) == clean_ident.lower())
        | (func.lower(Member.nomor_ekta) == clean_ident.lower())
    ).first()

    if not member:
        return jsonify(error="Data pengurus dengan NIK tersebut tidak ditemukan di database"), 404

    # Cari apakah sudah ada AppUser
    user = AppUser.query.filter(
        (AppUser.member_nik == member.nik) | (func.lower(AppUser.username) == member.nik.lower())
    ).first()

    user_role = determine_role(member.tingkat_kepengurusan)

    if user:
        user.password_hash = generate_password_hash(new_password)
        user.full_name = member.nama_lengkap
        user.role = user_role
        user.dpd_access = member.dpd or "ALL"
        user.dpc_access = member.dpc or "ALL"
        user.dprt_access = member.dprt or "ALL"
        user.jabatan = member.jabatan or f"Pengurus {member.tingkat_kepengurusan}"
        user.is_active = True
    else:
        user = AppUser(
            member_nik=member.nik,
            nomor_ekta=member.nomor_ekta,
            username=member.nik,
            password_hash=generate_password_hash(new_password),
            full_name=member.nama_lengkap,
            role=user_role,
            dpd_access=member.dpd or "ALL",
            dpc_access=member.dpc or "ALL",
            dprt_access=member.dprt or "ALL",
            jabatan=member.jabatan or f"Pengurus {member.tingkat_kepengurusan}",
            is_active=True,
        )
        db.session.add(user)

    db.session.commit()

    claims = claims_for(user)
    token = create_access_token(identity=str(user.id), additional_claims=claims)
    return jsonify(
        token=token,
        session=claims,
        message=f"Akun pengurus berhasil diaktivasi untuk {member.nama_lengkap} ({member.jabatan})!",
    )


@bp.get("/auth/me")
@login_required
def me():
    c = current_claims()
    keys = ("role", "dpdAccess", "dpcAccess", "dprtAccess", "adminName", "adminNik", "jabatan")
    return jsonify(session={k: c.get(k) for k in keys})
