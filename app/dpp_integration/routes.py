"""Rute API untuk sinkronisasi dan penerbitan e-KTA langsung ke portal DPP Partai NasDem."""
import logging
from flask import Blueprint, jsonify, request
from ..auth import login_required, current_claims
from ..models import Member
from ..extensions import db
from .client import get_dpp_client

bp = Blueprint("dpp", __name__)
logger = logging.getLogger("nasdem.dpp")


@bp.get("/dpp/status")
@login_required
def check_dpp_connection_status():
    """Mengecek status koneksi akun Admin Sulut ke server Digital NasDem DPP."""
    client = get_dpp_client()
    if not client.is_configured():
        return jsonify(
            configured=False,
            message="Akun Admin DPP belum dikonfigurasi di file .env server.",
        ), 200

    ok = client.ensure_authenticated()
    return jsonify(
        configured=True,
        connected=ok,
        adminEmail=client.email,
        message="Terhubung ke portal Digital NasDem DPP" if ok else "Gagal mengautentikasi ke portal DPP",
    ), 200


@bp.post("/dpp/create-kta")
@login_required
def create_kta_endpoint():
    """Menerbitkan nomor e-KTA resmi dari DPP untuk anggota baru atau anggota yang sudah ada."""
    claims = current_claims()
    if claims.get("role") == "ANGGOTA":
        return jsonify(error="Hanya pengurus resmi yang berwenang menerbitkan e-KTA."), 403

    data = request.get_json(silent=True) or {}
    nik = data.get("nik")
    if not nik:
        return jsonify(error="NIK anggota wajib disertakan"), 400

    # 1. Cek apakah kader sudah ada di database lokal
    clean_nik = str(nik).strip()
    member = db.session.get(Member, clean_nik)

    client = get_dpp_client()
    result = client.create_kta_for_member(
        member_dict=data if not member else {
            "nik": member.nik,
            "namaLengkap": member.nama_lengkap,
            "tempatLahir": member.tempat_lahir,
            "tanggalLahir": member.tanggal_lahir,
            "jenisKelamin": member.jenis_kelamin,
            "alamatLengkap": member.alamat_lengkap,
            "dpd": member.dpd,
            "dpc": member.dpc,
            "dprt": member.dprt,
            "agama": member.agama,
            "pekerjaan": member.pekerjaan,
            "nomorHP": member.nomor_hp,
            "email": member.email,
            "fotoKtpUrl": member.foto_ktp_url,
            "fotoProfilUrl": member.foto_profil_url,
        },
        foto_ktp_source=data.get("fotoKtpUrl"),
        foto_profil_source=data.get("fotoProfilUrl"),
    )

    # 2. Jika nomor e-KTA resmi berhasil diterbitkan, update database lokal
    if result.get("success") and result.get("nomor_ekta") and member:
        member.nomor_ekta = result["nomor_ekta"]
        db.session.commit()
        logger.info(f"[DPP-SYNC-SUCCESS] Nomor e-KTA resmi {result['nomor_ekta']} disimpan ke database lokal untuk NIK {clean_nik}")

    return jsonify(result), (200 if result.get("success") else 400)

