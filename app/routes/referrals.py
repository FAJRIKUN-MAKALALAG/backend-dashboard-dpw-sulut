from datetime import datetime, timezone
from flask import Blueprint, jsonify, request
from sqlalchemy import func, desc, or_

from ..auth import current_claims, login_required
from ..extensions import db
from ..models import Member, AppUser
from ..services.referral_service import (
    find_direct_leader,
    generate_unique_referral_code,
    is_ketua_position,
)
from ..utils import member_to_dict

bp = Blueprint("referrals", __name__)


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
        u = AppUser.query.filter(AppUser.id == user_id).first()
        if u and u.member_nik:
            m = Member.query.filter(Member.nik == u.member_nik).first()
            if m:
                return m

    return None


@bp.get("/referrals/my-code")
@login_required
def get_my_referral_code():
    """Mengambil kode referral milik pengurus yang sedang login beserta info ketua langsungnya."""
    claims = current_claims()
    m = get_current_member(claims)

    if not m:
        # Fallback untuk akun Super Admin/Admin sistem yang belum ditautkan ke data anggota fisik
        role = claims.get("role", "ADMIN")
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
            "ketuaLangsung": {
                "nik": "0000000000000000",
                "nama": "Ketua Umum DPP Partai NasDem",
                "jabatan": "Ketua Umum DPP",
                "tingkat": "DPP",
            },
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
        "totalRecruits": recruits_count,
    })


@bp.get("/referrals/preview-by-nik/<nik>")
@login_required
def get_referral_preview_by_nik(nik: str):
    """Mengambil preview profil referral untuk pengurus tertentu berdasarkan NIK (simulasi/inspeksi portal)."""
    m = Member.query.filter(Member.nik == str(nik)).first()
    if not m:
        return jsonify(error="Data pengurus tidak ditemukan"), 404

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
def validate_referral_code(code: str):
    """
    Validasi kode referral saat calon anggota baru mendaftar atau admin menginput.
    Mengembalikan data perekrut dan ketua langsungnya secara real-time.
    """
    clean_code = (code or "").strip().upper()
    if not clean_code or len(clean_code) < 4:
        return jsonify(valid=False, error="Kode referral tidak valid"), 400

    # Khusus kode super admin
    if clean_code in ("NSD-SUP-SUPER", "NSD-ADM-SUPER"):
        return jsonify({
            "valid": True,
            "recruiter": {
                "nik": "ADMIN-DPW",
                "namaLengkap": "Pengurus DPW Partai NasDem",
                "jabatan": "Pengurus DPW",
                "tingkatKepengurusan": "DPW",
                "dpd": "Sulawesi Utara",
                "dpc": "-",
                "dprt": "-",
                "kodeReferral": clean_code,
            },
            "directLeader": {
                "nik": "0000000000000000",
                "nama": "Ketua Umum DPP Partai NasDem",
                "jabatan": "Ketua Umum DPP",
                "tingkat": "DPP",
            },
        })

    m = Member.query.filter(func.upper(Member.kode_referral) == clean_code).first()
    if not m:
        return jsonify(valid=False, error=f"Kode referral '{clean_code}' tidak ditemukan di database"), 404

    leader = find_direct_leader(m)

    return jsonify({
        "valid": True,
        "recruiter": {
            "nik": m.nik,
            "namaLengkap": m.nama_lengkap,
            "jabatan": m.jabatan,
            "tingkatKepengurusan": m.tingkat_kepengurusan or "DPD",
            "dpd": m.dpd,
            "dpc": m.dpc,
            "dprt": m.dprt,
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
    m = get_current_member(claims)

    user_nik = m.nik if m else claims.get("adminNik")
    user_name = m.nama_lengkap if m else claims.get("adminName")
    user_code = m.kode_referral if m else f"NSD-{claims.get('role', 'ADM')[:3]}-SUPER"
    user_jabatan = m.jabatan if m else claims.get("jabatan", "")
    is_ketua = is_ketua_position(user_jabatan) or claims.get("role") in ("SUPER_ADMIN", "ADMIN_DPW", "ADMIN_DPD")

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

    # 2. Supervisi Ketua / Pimpinan (Rekap Tim)
    team_recruits = []
    total_team = 0
    leaderboard = []

    if is_ketua:
        # Jika Ketua, cari rekrutmen di mana ketua_langsung_nik adalah dia,
        # ATAU (jika Admin DPD/DPW/DPC) rekrutmen di wilayah cakupannya
        team_filter = [Member.ketua_langsung_nik == user_nik]
        
        # Tambahan filter wilayah jika admin daerah
        dpd_access = claims.get("dpdAccess")
        dpc_access = claims.get("dpcAccess")
        if dpd_access and dpd_access != "ALL":
            if dpc_access and dpc_access != "ALL":
                team_filter.append((Member.dpd == dpd_access) & (Member.dpc == dpc_access) & (Member.kader_perekrut_nama.isnot(None)))
            else:
                team_filter.append((Member.dpd == dpd_access) & (Member.kader_perekrut_nama.isnot(None)))
        elif claims.get("role") in ("SUPER_ADMIN", "ADMIN_DPW"):
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
        "myRecruits": [member_to_dict(x, include_photos=False) for x in direct_recruits],
        "teamRecruits": [member_to_dict(x, include_photos=False) for x in team_recruits],
        "leaderboard": leaderboard,
    })
