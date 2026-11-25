from datetime import datetime, timezone
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt, verify_jwt_in_request
from sqlalchemy import func, desc, or_

from app.auth import current_claims, login_required
from app.extensions import db
from app.rate_limit import rate_limit
from app.models import Member, AppUser
from app.services.referral_service import (
    find_direct_leader,
    find_ketua_dpc,
    find_ketua_dpd,
    find_ketua_dprt,
    find_ketua_dpw,
    generate_unique_referral_code,
    is_ketua_position,
)
from app.utils import (
    DELEGATED_ADMIN_ROLES,
    TERRITORIAL_MANAGER_ROLES,
    member_in_scope,
    member_to_dict,
)

bp = Blueprint("referrals", __name__)


def resolve_delegated_ketua(claims: dict, own_member: Member | None) -> dict | None:
    """Mengambil profil & kode referral Ketua Wilayah bagi akun Admin Delegasi/Kepercayaan Ketua."""
    role = claims.get("role")
    if role not in DELEGATED_ADMIN_ROLES:
        return None

    ketua_member = None
    explicit_nik = (claims.get("delegatedForKetuaNik") or "").strip()
    if not explicit_nik and claims.get("sub"):
        try:
            import uuid as _uuid_lib
            _uuid_lib.UUID(str(claims.get("sub")))
            u = db.session.get(AppUser, claims.get("sub"))
            if u and u.delegated_for_ketua_nik:
                explicit_nik = u.delegated_for_ketua_nik.strip()
        except (ValueError, TypeError):
            pass

    if explicit_nik:
        ketua_member = Member.query.filter(Member.nik == explicit_nik).first()

    if not ketua_member:
        dpd_acc = (claims.get("dpdAccess") or "").strip()
        dpc_acc = (claims.get("dpcAccess") or "").strip()
        dprt_acc = (claims.get("dprtAccess") or "").strip()
        if role == "ADMIN_DPW":
            ketua_member = find_ketua_dpw()
        elif role == "ADMIN_DPD" and dpd_acc and dpd_acc != "ALL":
            ketua_member = find_ketua_dpd(dpd_acc)
        elif role == "ADMIN_DPC" and dpd_acc and dpc_acc and dpc_acc != "ALL":
            ketua_member = find_ketua_dpc(dpd_acc, dpc_acc)
        elif role == "ADMIN_DPRT" and dpd_acc and dpc_acc and dprt_acc and dprt_acc != "ALL":
            ketua_member = find_ketua_dprt(dpd_acc, dpc_acc, dprt_acc)

    if not ketua_member:
        return None
    if own_member and str(ketua_member.nik) == str(own_member.nik):
        return None

    code = generate_unique_referral_code(ketua_member)
    return {
        "nik": ketua_member.nik,
        "nama": ketua_member.nama_lengkap,
        "jabatan": ketua_member.jabatan or f"Ketua {ketua_member.tingkat_kepengurusan or ''}".strip(),
        "tingkat": ketua_member.tingkat_kepengurusan or role.replace("ADMIN_", ""),
        "kodeReferral": code,
    }


def get_current_member(claims):
    """Mendapatkan data Member dari sesi akun yang sedang login."""
    admin_nik = claims.get("adminNik")
    admin_name = claims.get("adminName")
    
    # 1. Cari via NIK jika tersedia
    if admin_nik:
        m = Member.query.filter(Member.nik == str(admin_nik)).first()
        if m:
            return m
        # Coba cari via nomor E-KTA
        m = Member.query.filter(Member.nomor_ekta == str(admin_nik)).first()
        if m:
            return m
            
    # 2. Cari via Nama Lengkap
    if admin_name:
        m = Member.query.filter(func.lower(Member.nama_lengkap) == admin_name.lower().strip()).first()
        if m:
            return m
            
    # 3. Cari dari AppUser -> member_nik
    user_id = claims.get("sub")
    if user_id:
        try:
            import uuid as _uuid_lib
            _uuid_lib.UUID(str(user_id))
            u = AppUser.query.filter(AppUser.id == user_id).first()
            if u and u.member_nik:
                m = Member.query.filter(Member.nik == u.member_nik).first()
                if m:
                    return m
        except (ValueError, TypeError):
            pass

    return None


@bp.get("/referrals/my-code")
@login_required
def get_my_referral_code():
    """Mengambil kode referral milik pengurus yang sedang login beserta info ketua langsungnya."""
    claims = current_claims()
    m = get_current_member(claims)

    # Anggota biasa tanpa SK tidak memiliki kode referral & tidak memiliki rekrutmen
    role = claims.get("role", "ADMIN")
    if role in ("ANGGOTA", "ADMIN_INVENTORY", "WAKIL_BENDAHARA"):
        return jsonify({
            "isMemberLinked": bool(m),
            "kodeReferral": None,
            "namaLengkap": m.nama_lengkap if m else claims.get("adminName"),
            "nik": m.nik if m else claims.get("adminNik"),
            "nomorEkta": m.nomor_ekta if m else None,
            "jabatan": m.jabatan if m else ("Anggota Biasa" if role == "ANGGOTA" else role),
            "tingkatKepengurusan": "Bukan Pengurus",
            "dpd": m.dpd if m else claims.get("dpdAccess"),
            "dpc": m.dpc if m else "-",
            "dprt": m.dprt if m else "-",
            "ketuaLangsung": None,
            "isDelegatedAdmin": False,
            "delegatedKetua": None,
            "totalRecruits": 0,
        })

    delegated_ketua = resolve_delegated_ketua(claims, m)

    if not m:
        # Fallback untuk akun Super Admin/Admin sistem yang belum ditautkan ke data anggota fisik
        admin_name = claims.get("adminName", "Admin Partai NasDem")
        admin_nik = claims.get("adminNik", "ADMIN-01")
        code = f"NSD-{role[:3]}-SUPER"

        # Hitung anggota yang direkrut oleh kode admin ini
        recruits_count = Member.query.filter(
            or_(
                Member.kader_perekrut_kode_referral == code,
                Member.kader_perekrut_nama == admin_name,
            )
        ).count()

        is_super = role == "SUPER_ADMIN"
        return jsonify({
            "isMemberLinked": False,
            "kodeReferral": code,
            "namaLengkap": admin_name,
            "nik": admin_nik,
            "jabatan": claims.get("jabatan", f"Pengurus {role}"),
            "tingkatKepengurusan": "DPW",
            "dpd": claims.get("dpdAccess", "Sulawesi Utara"),
            "dpc": "-",
            "dprt": "-",
            "ketuaLangsung": None if is_super else {
                "nik": "0000000000000000",
                "nama": "Ketua Umum DPP Partai NasDem",
                "jabatan": "Ketua Umum DPP",
                "tingkat": "DPP",
            },
            "isDelegatedAdmin": role in DELEGATED_ADMIN_ROLES,
            "delegatedKetua": delegated_ketua,
            "totalRecruits": recruits_count,
        })

    code = generate_unique_referral_code(m)
    leader = find_direct_leader(m)

    recruits_count = Member.query.filter(
        or_(
            Member.kader_perekrut_kode_referral == code,
            Member.kader_perekrut_nik == m.nik,
        )
    ).count()

    return jsonify({
        "isMemberLinked": True,
        "kodeReferral": code,
        "namaLengkap": m.nama_lengkap,
        "nik": m.nik,
        "nomorEkta": m.nomor_ekta,
        "jabatan": m.jabatan,
        "tingkatKepengurusan": m.tingkat_kepengurusan or "DPD",
        "dpd": m.dpd,
        "dpc": m.dpc,
        "dprt": m.dprt,
        "ketuaLangsung": leader,
        "isDelegatedAdmin": role in DELEGATED_ADMIN_ROLES,
        "delegatedKetua": delegated_ketua,
        "totalRecruits": recruits_count,
    })


@bp.get("/referrals/preview-by-nik/<nik>")
@login_required
def get_referral_preview_by_nik(nik: str):
    """Mengambil preview profil referral untuk pengurus tertentu (dibatasi RBAC & scope wilayah)."""
    claims = current_claims()
    role = claims.get("role")
    my_nik = str(claims.get("adminNik") or "").strip()

    m = Member.query.filter(Member.nik == str(nik)).first()
    if not m:
        return jsonify(error="Data pengurus tidak ditemukan"), 404

    is_allowed = (
        role == "SUPER_ADMIN"
        or (my_nik and str(m.nik).strip() == my_nik)
        or (role in TERRITORIAL_MANAGER_ROLES and member_in_scope(claims, m))
    )
    if not is_allowed:
        return jsonify(error="Anda tidak memiliki izin melihat pratinjau referral pengurus di luar wilayah Anda"), 403

    code = generate_unique_referral_code(m)
    leader = find_direct_leader(m)

    recruits_count = Member.query.filter(
        or_(
            Member.kader_perekrut_kode_referral == code,
            Member.kader_perekrut_nik == m.nik,
        )
    ).count()

    return jsonify({
        "isMemberLinked": True,
        "kodeReferral": code,
        "namaLengkap": m.nama_lengkap,
        "nik": m.nik,
        "nomorEkta": m.nomor_ekta,
        "jabatan": m.jabatan,
        "tingkatKepengurusan": m.tingkat_kepengurusan or "DPD",
        "dpd": m.dpd,
        "dpc": m.dpc,
        "dprt": m.dprt,
        "ketuaLangsung": leader,
        "totalRecruits": recruits_count,
    })


@bp.get("/referrals/validate/<code>")
@rate_limit(20, 60, scope="ip", key_prefix="ref_validate")
def validate_referral_code(code: str):
    """
    Validasi kode referral saat calon anggota baru mendaftar atau admin menginput.
    Untuk pemanggil publik tanpa JWT, NIK dan alamat granular tidak diekspos.
    """
    clean_code = (code or "").strip().upper()
    if not clean_code or len(clean_code) < 4:
        return jsonify(valid=False, error="Kode referral tidak valid"), 400

    is_authenticated = False
    try:
        verify_jwt_in_request(optional=True)
        is_authenticated = bool(get_jwt())
    except Exception:
        is_authenticated = False

    # Khusus kode super admin
    if clean_code in ("NSD-SUP-SUPER", "NSD-ADM-SUPER"):
        return jsonify({
            "valid": True,
            "recruiter": {
                "nik": "ADMIN-DPW" if is_authenticated else "",
                "namaLengkap": "Pengurus DPW Partai NasDem",
                "jabatan": "Pengurus DPW",
                "tingkatKepengurusan": "DPW",
                "dpd": "Sulawesi Utara",
                "dpc": "-" if is_authenticated else "",
                "dprt": "-" if is_authenticated else "",
                "kodeReferral": clean_code,
            },
            "directLeader": {
                "nik": "0000000000000000" if is_authenticated else "",
                "nama": "Ketua Umum DPP Partai NasDem",
                "jabatan": "Ketua Umum DPP",
                "tingkat": "DPP",
            },
        })

    m = Member.query.filter(func.upper(Member.kode_referral) == clean_code).first()
    if not m:
        return jsonify(valid=False, error=f"Kode referral '{clean_code}' tidak ditemukan di database"), 404

    leader = find_direct_leader(m)
    if leader and not is_authenticated:
        leader = {k: v for k, v in leader.items() if k != "nik"}

    return jsonify({
        "valid": True,
        "recruiter": {
            "nik": m.nik if is_authenticated else "",
            "namaLengkap": m.nama_lengkap,
            "jabatan": m.jabatan,
            "tingkatKepengurusan": m.tingkat_kepengurusan or "DPD",
            "dpd": m.dpd,
            "dpc": m.dpc if is_authenticated else "",
            "dprt": m.dprt if is_authenticated else "",
            "kodeReferral": m.kode_referral,
        },
        "directLeader": leader,
    })


@bp.get("/referrals/tracking")
@login_required
def referral_tracking():
    """
    Dashboard tracking rekrutmen referral:
    - Perekrut melihat anggota yang direkrutnya langsung.
    - Ketua/Pimpinan melihat rekapitulasi tim di bawah supervisinya (berdasarkan ketua_langsung_nik atau wilayah).
    - Dilengkapi Leaderboard perekrut teraktif.
    """
    claims = current_claims()

    # Anggota biasa tidak memiliki akses ke tracking rekrutmen atau referral siapapun
    if claims.get("role") == "ANGGOTA":
        return jsonify({
            "myStats": {
                "kodeReferral": None,
                "totalDirect": 0,
                "totalTeam": 0,
                "isLeader": False,
            },
            "myRecruits": [],
            "teamRecruits": [],
            "leaderboard": [],
        })

    m = get_current_member(claims)
    role = claims.get("role")
    delegated_ketua = resolve_delegated_ketua(claims, m)

    user_nik = m.nik if m else claims.get("adminNik")
    user_name = m.nama_lengkap if m else claims.get("adminName")
    user_code = m.kode_referral if m else f"NSD-{(role or 'ADM')[:3]}-SUPER"
    is_ketua = role in TERRITORIAL_MANAGER_ROLES

    # 1. Perekrutan Langsung oleh Pengguna
    direct_query = Member.query.filter(
        or_(
            Member.kader_perekrut_kode_referral == user_code,
            Member.kader_perekrut_nik == user_nik,
            Member.kader_perekrut_nama == user_name,
        )
    )
    direct_recruits = direct_query.order_by(desc(Member.tanggal_bergabung), desc(Member.created_at)).limit(100).all()
    total_direct = direct_query.count()

    # 2. Supervisi Ketua / Admin Delegasi (Rekap Tim Wilayah)
    team_recruits = []
    total_team = 0
    leaderboard = []

    if is_ketua:
        # Cari rekrutmen di mana ketua_langsung_nik adalah dia atau Ketua wilayahnya,
        # ditambah seluruh rekrutmen ter-referral di wilayah cakupannya
        team_filter = [Member.ketua_langsung_nik == user_nik]
        if delegated_ketua and delegated_ketua.get("nik"):
            team_filter.append(Member.ketua_langsung_nik == delegated_ketua["nik"])
            team_filter.append(Member.kader_perekrut_nik == delegated_ketua["nik"])

        dpd_access = claims.get("dpdAccess")
        dpc_access = claims.get("dpcAccess")
        dprt_access = claims.get("dprtAccess")
        if dpd_access and dpd_access != "ALL":
            if dprt_access and dprt_access != "ALL" and dpc_access and dpc_access != "ALL":
                team_filter.append(
                    (Member.dpd == dpd_access)
                    & (Member.dpc == dpc_access)
                    & (Member.dprt == dprt_access)
                    & (Member.kader_perekrut_nama.isnot(None))
                )
            elif dpc_access and dpc_access != "ALL":
                team_filter.append((Member.dpd == dpd_access) & (Member.dpc == dpc_access) & (Member.kader_perekrut_nama.isnot(None)))
            else:
                team_filter.append((Member.dpd == dpd_access) & (Member.kader_perekrut_nama.isnot(None)))
        elif role in ("SUPER_ADMIN", "KETUA_DPW", "ADMIN_DPW"):
            team_filter.append(Member.kader_perekrut_nama.isnot(None))

        team_q = Member.query.filter(or_(*team_filter))
        total_team = team_q.count()
        team_recruits = team_q.order_by(desc(Member.tanggal_bergabung), desc(Member.created_at)).limit(50).all()

        # Leaderboard Perekrut Terbanyak di Bawah Supervisi
        lb_raw = (
            db.session.query(
                Member.kader_perekrut_nama.label("nama"),
                Member.kader_perekrut_jabatan.label("jabatan"),
                Member.kader_perekrut_kode_referral.label("kode"),
                Member.dpd.label("dpd"),
                func.count(Member.nik).label("total_rekrut"),
            )
            .filter(or_(*team_filter), Member.kader_perekrut_nama.isnot(None), Member.kader_perekrut_nama != "")
            .group_by(
                Member.kader_perekrut_nama,
                Member.kader_perekrut_jabatan,
                Member.kader_perekrut_kode_referral,
                Member.dpd,
            )
            .order_by(desc("total_rekrut"))
            .limit(10)
            .all()
        )
        leaderboard = [
            {
                "nama": r.nama,
                "jabatan": r.jabatan or "Pengurus",
                "kodeReferral": r.kode or "-",
                "dpd": r.dpd,
                "totalRekrut": r.total_rekrut,
            }
            for r in lb_raw
        ]

    return jsonify({
        "myStats": {
            "kodeReferral": user_code,
            "totalDirect": total_direct,
            "totalTeam": total_team,
            "isLeader": is_ketua,
        },
        "delegatedKetua": delegated_ketua,
        "myRecruits": [member_to_dict(x, include_photos=False) for x in direct_recruits],
        "teamRecruits": [member_to_dict(x, include_photos=False) for x in team_recruits],
        "leaderboard": leaderboard,
    })
