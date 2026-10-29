from flask import Blueprint, jsonify, request
from flask_jwt_extended import create_access_token
from sqlalchemy import func
from werkzeug.security import check_password_hash, generate_password_hash

from ..auth import current_claims, login_required
from ..extensions import db
from ..models import AppUser, Member, Position

bp = Blueprint("auth", __name__)


def determine_role(member_or_tingkat, member: Member = None) -> str:
    """Memetakan status kepengurusan ke level hak akses (Role) sistem.
    Hanya pengurus struktural ber-SK yang mendapatkan role Admin (DPW, DPD, DPC, DPRt).
    Anggota biasa yang belum/tidak memiliki SK resmi mendapatkan role 'ANGGOTA'."""
    m = member_or_tingkat if isinstance(member_or_tingkat, Member) else member
    tingkat = member_or_tingkat.tingkat_kepengurusan if isinstance(member_or_tingkat, Member) else member_or_tingkat

    t = (tingkat or "").strip().upper()
    has_sk = False
    if m:
        has_sk = bool(m.is_pengurus or (m.nomor_sk and str(m.nomor_sk).strip()))
    else:
        has_sk = t in ("DPW", "DPD", "DPC", "DPRT", "RANTING")

    if not has_sk or t in ("BUKAN PENGURUS", "ANGGOTA", "KADER", "-", ""):
        return "ANGGOTA"

    if t == "DPW":
        return "ADMIN_DPW"
    elif t == "DPD":
        return "ADMIN_DPD"
    elif t == "DPC":
        return "ADMIN_DPC"
    elif t in ("DPRT", "RANTING"):
        return "ADMIN_DPRT"
    return "ANGGOTA"


def claims_for(u: AppUser) -> dict:
    dpd_acc = u.dpd_access or "ALL"
    dpc_acc = u.dpc_access or "ALL"
    dprt_acc = u.dprt_access or "ALL"

    # Peran DPC: dpc_access WAJIB spesifik ke kecamatannya, tidak boleh ALL
    if u.role == "ADMIN_DPC":
        dprt_acc = "ALL"  # Membawahi seluruh DPRt di kecamatannya
        if not dpc_acc or dpc_acc in ("ALL", "Umum (DPD)", "-", ""):
            pos_dpc = Position.query.filter(
                Position.member_nik == u.member_nik,
                Position.tingkat_kepengurusan == "DPC"
            ).first() if u.member_nik else None
            if pos_dpc and pos_dpc.dpc and pos_dpc.dpc not in ("ALL", "Umum (DPD)", "-", ""):
                dpc_acc = pos_dpc.dpc
                u.dpc_access = pos_dpc.dpc
                if pos_dpc.jabatan:
                    u.jabatan = pos_dpc.jabatan
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
            else:
                m = Member.query.filter(Member.nik == u.member_nik).first() if u.member_nik else None
                if m and m.dpc and m.dpc not in ("ALL", "Umum (DPD)", "-", ""):
                    dpc_acc = m.dpc
                    u.dpc_access = m.dpc
                    try:
                        db.session.commit()
                    except Exception:
                        db.session.rollback()

    # Peran DPRt: dpc_access dan dprt_access WAJIB spesifik
    elif u.role == "ADMIN_DPRT":
        if not dprt_acc or dprt_acc in ("ALL", "Pusat/Umum", "-", "") or not dpc_acc or dpc_acc in ("ALL", "Umum (DPD)", "-", ""):
            pos_dprt = Position.query.filter(
                Position.member_nik == u.member_nik,
                Position.tingkat_kepengurusan == "DPRt"
            ).first() if u.member_nik else None
            if pos_dprt:
                if pos_dprt.dpc and pos_dprt.dpc not in ("ALL", "Umum (DPD)", "-", ""):
                    dpc_acc = pos_dprt.dpc
                    u.dpc_access = pos_dprt.dpc
                if pos_dprt.dprt and pos_dprt.dprt not in ("ALL", "Pusat/Umum", "-", ""):
                    dprt_acc = pos_dprt.dprt
                    u.dprt_access = pos_dprt.dprt
                if pos_dprt.jabatan:
                    u.jabatan = pos_dprt.jabatan
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()

    # Peran DPD membawahi SELURUH DPC dan DPRt di DPD-nya
    elif u.role == "ADMIN_DPD":
        dpc_acc = "ALL"
        dprt_acc = "ALL"
    elif u.role in ("SUPER_ADMIN", "ADMIN_DPW"):
        dpd_acc = "ALL"
        dpc_acc = "ALL"
        dprt_acc = "ALL"

    # Hanya Super Admin dan Pengurus struktural ber-SK yang berwenang menambahkan anggota baru
    is_structural_leader = bool(
        u.role in ("SUPER_ADMIN", "ADMIN_DPW", "ADMIN_DPD", "ADMIN_DPC", "ADMIN_DPRT") and u.role != "ANGGOTA"
    )

    return {
        "role": u.role,
        "dpdAccess": dpd_acc,
        "dpcAccess": dpc_acc,
        "dprtAccess": dprt_acc,
        "adminName": u.full_name,
        "adminNik": u.member_nik or u.username,
        "jabatan": u.jabatan or ("Anggota Biasa" if u.role == "ANGGOTA" else f"Admin {u.role}"),
        "canAddMembers": is_structural_leader,
        "hasSk": is_structural_leader,
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
                error="Kata sandi salah. Silakan periksa kembali kata sandi Anda atau gunakan menu 'Aktivasi Pengurus'."
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
        return jsonify(
            error="Kata sandi salah. Silakan periksa kembali kata sandi Anda atau gunakan menu 'Aktivasi Pengurus'."
        ), 401

def resolve_territory_access(member: Member, user_role: str):
    dpd = member.dpd or "ALL"
    dpc = member.dpc or "ALL"
    dprt = member.dprt or "ALL"
    jabatan = member.jabatan or f"Pengurus {member.tingkat_kepengurusan}"

    if user_role == "ADMIN_DPC":
        pos_dpc = next((p for p in member.positions if p.tingkat_kepengurusan == "DPC" and p.dpc and p.dpc not in ("ALL", "Umum (DPD)", "-", "")), None)
        if pos_dpc:
            dpc = pos_dpc.dpc
            if pos_dpc.jabatan:
                jabatan = pos_dpc.jabatan
        elif dpc in ("Umum (DPD)", "-", ""):
            dpc = "ALL"
    elif user_role == "ADMIN_DPRT":
        pos_dprt = next((p for p in member.positions if p.tingkat_kepengurusan == "DPRt" and p.dprt and p.dprt not in ("ALL", "Pusat/Umum", "-", "")), None)
        if pos_dprt:
            if pos_dprt.dpc and pos_dprt.dpc not in ("ALL", "Umum (DPD)", "-", ""):
                dpc = pos_dprt.dpc
            if pos_dprt.dprt and pos_dprt.dprt not in ("ALL", "Pusat/Umum", "-", ""):
                dprt = pos_dprt.dprt
            if pos_dprt.jabatan:
                jabatan = pos_dprt.jabatan

    return dpd, dpc, dprt, jabatan


    # Otomatis buatkan akun AppUser untuk pengurus atau anggota ini!
    user_role = determine_role(member)
    dpd_val, dpc_val, dprt_val, jab_val = resolve_territory_access(member, user_role)
    new_user = AppUser(
        member_nik=member.nik,
        nomor_ekta=member.nomor_ekta,
        username=member.nik,
        password_hash=generate_password_hash(password),
        full_name=member.nama_lengkap,
        role=user_role,
        dpd_access=dpd_val,
        dpc_access=dpc_val,
        dprt_access=dprt_val,
        jabatan=jab_val,
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
        "defaultPasswordHint": "",
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

    user_role = determine_role(member)
    dpd_val, dpc_val, dprt_val, jab_val = resolve_territory_access(member, user_role)

    if user:
        user.password_hash = generate_password_hash(new_password)
        user.full_name = member.nama_lengkap
        user.role = user_role
        user.dpd_access = dpd_val
        user.dpc_access = dpc_val
        user.dprt_access = dprt_val
        user.jabatan = jab_val
        user.is_active = True
    else:
        user = AppUser(
            member_nik=member.nik,
            nomor_ekta=member.nomor_ekta,
            username=member.nik,
            password_hash=generate_password_hash(new_password),
            full_name=member.nama_lengkap,
            role=user_role,
            dpd_access=dpd_val,
            dpc_access=dpc_val,
            dprt_access=dprt_val,
            jabatan=jab_val,
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
