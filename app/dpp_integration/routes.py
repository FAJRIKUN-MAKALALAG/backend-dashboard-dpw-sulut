"""Rute API terpisah untuk pendaftaran, pengecekan, dan penerbitan e-KTA langsung ke portal DPP Partai NasDem."""
import logging
import re
from flask import Blueprint, Response, jsonify, request
from ..auth import login_required, current_claims
from ..cache import invalidate_all_data_cache
from ..models import AuditLog, Member
from ..extensions import db
from ..services.image_storage import intercept_and_store_member_photos
from ..utils import payload_to_member_values, member_to_dict
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
            connected=False,
            message="Akun Admin DPP belum dikonfigurasi di file .env server.",
        ), 200

    ok = client.ensure_authenticated()
    return jsonify(
        configured=True,
        connected=ok,
        adminEmail=client.email,
        baseUrl=client.base_url,
        message="Terhubung ke portal Digital NasDem DPP" if ok else "Gagal mengautentikasi ke portal DPP",
    ), 200


@bp.get("/dpp/wilayah")
@login_required
def get_dpp_wilayah():
    """Mengambil daftar Kabupaten/Kota, Kecamatan, atau Kelurahan langsung dari referensi portal DPP."""
    client = get_dpp_client()
    kab_id = request.args.get("kabupaten_id", "").strip()
    kec_id = request.args.get("kecamatan_id", "").strip()

    if kec_id:
        items = client.get_kelurahan_list(kec_id)
        return jsonify(level="kelurahan", items=items), 200
    if kab_id:
        items = client.get_kecamatan_list(kab_id)
        return jsonify(level="kecamatan", items=items), 200

    items = client.get_kabupaten_list("71")
    return jsonify(level="kabupaten", items=items), 200


@bp.post("/dpp/check-ktp")
@login_required
def check_dpp_ktp():
    """Mengecek apakah NIK sudah terdaftar di database pusat Digital NasDem DPP."""
    data = request.get_json(silent=True) or {}
    nik = re.sub(r"\D", "", str(data.get("nik") or ""))
    if len(nik) < 9:
        return jsonify(error="NIK tidak valid", exists=False), 400

    client = get_dpp_client()
    exists_in_dpp = client.check_ktp_exists(nik)
    local_member = db.session.get(Member, nik)

    return jsonify(
        nik=nik,
        existsInDpp=exists_in_dpp,
        existsInLocal=bool(local_member),
        localMember=member_to_dict(local_member) if local_member else None,
    ), 200


@bp.get("/dpp/kta-card/<nokta>")
def get_dpp_kta_card(nokta: str):
    """Mengambil citra kartu e-KTA resmi langsung dari server DPP (PNG)."""
    clean_kta = re.sub(r"\D", "", str(nokta or ""))
    if not clean_kta:
        return jsonify(error="Nomor e-KTA tidak valid"), 400

    client = get_dpp_client()
    img_bytes = client.fetch_kta_card_image(clean_kta)
    if not img_bytes:
        return jsonify(error="Kartu e-KTA belum tersedia atau tidak ditemukan di server DPP"), 404

    download = request.args.get("download") == "1"
    headers = {"Cache-Control": "public, max-age=3600"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="eKTA_NasDem_{clean_kta}.png"'

    return Response(img_bytes, mimetype="image/png", headers=headers)


@bp.post("/dpp/create-kta")
@login_required
def create_kta_endpoint():
    """Menerbitkan nomor e-KTA resmi dari portal Digital NasDem DPP secara mandiri/terpisah."""
    claims = current_claims()
    if claims.get("role") == "ANGGOTA":
        return jsonify(error="Hanya pengurus resmi atau admin yang berwenang menerbitkan e-KTA DPP."), 403

    data = request.get_json(silent=True) or {}
    raw_nik = str(data.get("nik") or "").strip()
    clean_nik = re.sub(r"\D", "", raw_nik)
    if len(clean_nik) < 9 or len(clean_nik) > 16:
        return jsonify(error="NIK wajib 9-16 digit angka yang valid"), 400

    # 1. Cek apakah kader sudah ada di database lokal
    member = db.session.get(Member, clean_nik)

    # Gabungkan data input form dengan data member lokal (data form menjadi prioritas jika diisi)
    merged_dict = {
        "nik": clean_nik,
        "namaLengkap": data.get("namaLengkap") or (member.nama_lengkap if member else ""),
        "tempatLahir": data.get("tempatLahir") or (member.tempat_lahir if member else "MANADO"),
        "tanggalLahir": data.get("tanggalLahir") or (member.tanggal_lahir if member else None),
        "jenisKelamin": data.get("jenisKelamin") or (member.jenis_kelamin if member else "Laki-laki"),
        "alamatLengkap": data.get("alamatLengkap") or (member.alamat_lengkap if member else ""),
        "dpd": data.get("dpd") or (member.dpd if member else "Kota Manado"),
        "dpc": data.get("dpc") or (member.dpc if member else "Umum"),
        "dprt": data.get("dprt") or (member.dprt if member else "Umum"),
        "agama": data.get("agama") or (member.agama if member else "Kristen Protestan"),
        "pekerjaan": data.get("pekerjaan") or (member.pekerjaan if member else "Wiraswasta"),
        "nomorHP": data.get("nomorHP") or (member.nomor_hp if member else ""),
        "email": data.get("email") or (member.email if member else ""),
        "statusPerkawinan": data.get("statusPerkawinan") or "B",
        "pendidikan": data.get("pendidikan") or "4",
        "golonganDarah": data.get("golonganDarah") or "O",
        "sayap": data.get("sayap") or "",
        "rtNumber": data.get("rtNumber"),
        "rwNumber": data.get("rwNumber"),
        "rtRw": data.get("rtRw"),
        "kabupatenId": data.get("kabupatenId"),
        "kecamatanId": data.get("kecamatanId"),
        "kelurahanId": data.get("kelurahanId"),
        "password": data.get("password"),
        "fotoKtpUrl": data.get("fotoKtpUrl") or (member.foto_ktp_url if member else None),
        "fotoProfilUrl": data.get("fotoProfilUrl") or (member.foto_profil_url if member else None),
    }

    client = get_dpp_client()
    result = client.create_kta_for_member(
        member_dict=merged_dict,
        foto_ktp_source=merged_dict.get("fotoKtpUrl"),
        foto_profil_source=merged_dict.get("fotoProfilUrl"),
    )

    if not result.get("success"):
        return jsonify(result), 400

    nomor_ekta = result.get("nomor_ekta")
    save_to_local = bool(data.get("saveToLocalMembers", True))

    # 2. Sinkronisasi hasil ke database lokal DPW Sulut
    if member:
        if nomor_ekta:
            member.nomor_ekta = nomor_ekta
        if merged_dict.get("tempatLahir") and not member.tempat_lahir:
            member.tempat_lahir = merged_dict["tempatLahir"]
        if merged_dict.get("alamatLengkap") and not member.alamat_lengkap:
            member.alamat_lengkap = merged_dict["alamatLengkap"]
        if merged_dict.get("agama") and not member.agama:
            member.agama = merged_dict["agama"]
        if merged_dict.get("pekerjaan") and not member.pekerjaan:
            member.pekerjaan = merged_dict["pekerjaan"]
        if result.get("email") and not member.email:
            member.email = result["email"]

        # Simpan foto jika baru diunggah pada form e-KTA
        if data.get("fotoKtpUrl") or data.get("fotoProfilUrl"):
            photo_vals = intercept_and_store_member_photos(
                {
                    "foto_ktp_url": data.get("fotoKtpUrl"),
                    "foto_profil_url": data.get("fotoProfilUrl"),
                },
                clean_nik,
            )
            if photo_vals.get("foto_ktp_url"):
                member.foto_ktp_url = photo_vals["foto_ktp_url"]
                member.status_ktp_verified = True
            if photo_vals.get("foto_profil_url"):
                member.foto_profil_url = photo_vals["foto_profil_url"]

        db.session.add(
            AuditLog(
                member_nik=clean_nik,
                member_name=member.nama_lengkap,
                action="UPDATE",
                new_status=member.status,
                reason=f"Penerbitan e-KTA Resmi DPP Pusat ({nomor_ekta or 'Terverifikasi DPP'})",
                actor=claims.get("adminName") or "Admin",
                dpd=member.dpd,
            )
        )
        db.session.commit()
        invalidate_all_data_cache()
        result["member"] = member_to_dict(member)

    elif save_to_local and merged_dict.get("namaLengkap"):
        raw_values = payload_to_member_values(
            {
                **merged_dict,
                "nomorEKTA": nomor_ekta or clean_nik,
                "status": "Aktif",
                "jabatan": "Anggota Biasa",
                "tingkatKepengurusan": "Bukan Pengurus",
                "isPengurus": False,
                "catatanMutasi": f"Terdaftar melalui Pendaftaran e-KTA DPP Pusat ({nomor_ekta or clean_nik})",
            }
        )
        raw_values = intercept_and_store_member_photos(raw_values, clean_nik)
        raw_values["diubah_oleh"] = claims.get("adminName") or "Admin"
        if nomor_ekta:
            raw_values["nomor_ekta"] = nomor_ekta
        new_m = Member(**raw_values)
        db.session.add(new_m)
        db.session.add(
            AuditLog(
                member_nik=clean_nik,
                member_name=new_m.nama_lengkap,
                action="CREATE",
                new_status="Aktif",
                reason=f"Pendaftaran e-KTA Resmi DPP Pusat ({nomor_ekta or clean_nik})",
                actor=claims.get("adminName") or "Admin",
                dpd=new_m.dpd,
            )
        )
        db.session.commit()
        invalidate_all_data_cache()
        result["member"] = member_to_dict(new_m)

    return jsonify(result), 200
