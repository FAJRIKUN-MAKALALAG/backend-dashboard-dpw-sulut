"""Rute API terpisah untuk pendaftaran, pengecekan, dan penerbitan e-KTA langsung ke portal DPP Partai NasDem."""
import logging
import re
from flask import Blueprint, Response, jsonify, request
from app.auth import login_required, current_claims
from app.cache import invalidate_all_data_cache
from app.rate_limit import rate_limit
from app.models import AuditLog, Member
from app.extensions import db
from app.services.image_storage import intercept_and_store_member_photos
from app.utils import (
    in_scope,
    member_in_scope,
    member_to_dict,
    payload_to_member_values,
)
from app.dpp_integration.client import get_dpp_client

bp = Blueprint("dpp", __name__)
logger = logging.getLogger("nasdem.dpp")


def _can_use_dpp_ekta(claims: dict) -> bool:
    role = claims.get("role")
    if role in ("ANGGOTA", "ADMIN_INVENTORY", "INVENTORY", "WAKIL_BENDAHARA"):
        return False
    return bool(claims.get("canAddMembers", True))


def _scoped_fallback_territory(claims: dict) -> tuple[str, str, str]:
    dpd = (claims.get("dpdAccess") or "").strip()
    if not dpd or dpd == "ALL":
        dpd = "Kota Manado"
    dpc = (claims.get("dpcAccess") or "").strip()
    if not dpc or dpc in ("ALL", "Umum (DPD)", "-"):
        dpc = "Umum"
    dprt = (claims.get("dprtAccess") or "").strip()
    if not dprt or dprt in ("ALL", "Pusat/Umum", "-"):
        dprt = "Umum"
    return dpd, dpc, dprt


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
@rate_limit(15, 60, scope="user_or_ip", key_prefix="dpp_check")
def check_dpp_ktp():
    """Mengecek apakah NIK sudah terdaftar di database pusat Digital NasDem DPP beserta nomor e-KTA resminya."""
    data = request.get_json(silent=True) or {}
    nik = re.sub(r"\D", "", str(data.get("nik") or ""))
    if len(nik) < 9:
        return jsonify(error="NIK tidak valid", exists=False), 400

    client = get_dpp_client()
    dpp_info = client.find_existing_dpp_member(nik)
    exists_in_dpp = bool(dpp_info.get("exists"))
    nomor_ekta_dpp = dpp_info.get("nomor_ekta")
    nama_dpp = dpp_info.get("nama")

    # Filter identitas admin
    if client._is_admin_identity(nama_dpp):
        nama_dpp = None

    claims = current_claims()
    local_member = db.session.get(Member, nik)
    scoped_local = local_member if (local_member and member_in_scope(claims, local_member)) else None

    return jsonify(
        nik=nik,
        existsInDpp=exists_in_dpp,
        nomorEktaDpp=nomor_ekta_dpp,
        namaDpp=nama_dpp,
        fotoProfilDpp=dpp_info.get("foto_profil"),
        fotoKtpDpp=dpp_info.get("foto_ktp"),
        phoneDpp=dpp_info.get("phone"),
        wilayahTextDpp=dpp_info.get("wilayah_text"),
        existsInLocal=bool(scoped_local),
        localMember=member_to_dict(scoped_local) if scoped_local else None,
        message=(
            f"NIK sudah terdaftar di DPP Pusat dengan e-KTA Resmi: {nomor_ekta_dpp}"
            if nomor_ekta_dpp
            else (
                "NIK sudah terdaftar di database Digital NasDem Pusat."
                if exists_in_dpp
                else "NIK belum terdaftar di DPP Pusat."
            )
        ),
    ), 200


@bp.post("/dpp/sync-existing-kta")
@login_required
@rate_limit(15, 60, scope="user_or_ip", key_prefix="dpp_sync")
def sync_existing_kta():
    """Menarik dan menyinkronkan e-KTA resmi dari DPP Pusat ke data profil di database PostgreSQL."""
    claims = current_claims()
    if not _can_use_dpp_ekta(claims):
        return jsonify(error="Hanya pengurus resmi atau admin wilayah yang berwenang menyinkronkan e-KTA DPP."), 403

    data = request.get_json(silent=True) or {}
    raw_nik = str(data.get("nik") or "").strip()
    clean_nik = re.sub(r"\D", "", raw_nik)
    provided_ekta = re.sub(r"\D", "", str(data.get("nomorEkta") or "").strip())
    input_nama = str(data.get("namaLengkap") or "").strip()

    if len(clean_nik) < 9:
        return jsonify(error="NIK wajib diisi dengan valid (9-16 digit)"), 400

    # Validasi cakupan wilayah sebelum menyentuh portal DPP atau database
    member = db.session.get(Member, clean_nik)
    if member and not member_in_scope(claims, member):
        return jsonify(error="Data kader ini berada di luar wilayah akses akun Anda."), 403

    fallback_dpd, fallback_dpc, fallback_dprt = _scoped_fallback_territory(claims)
    target_dpd = (data.get("dpd") or (member.dpd if member else "") or fallback_dpd).strip()
    target_dpc = (data.get("dpc") or (member.dpc if member else "") or fallback_dpc).strip()
    target_dprt = (data.get("dprt") or (member.dprt if member else "") or fallback_dprt).strip()
    if not member and not in_scope(claims, target_dpd, target_dpc, target_dprt):
        return jsonify(error="Wilayah sinkronisasi e-KTA berada di luar cakupan akses akun Anda."), 403

    client = get_dpp_client()
    target_ekta = provided_ekta

    # Jangan pernah terima nama milik admin Gabriela
    if client._is_admin_identity(input_nama):
        input_nama = ""

    # Jika nomor e-KTA belum disediakan, cari otomatis ke server DPP
    if not target_ekta:
        dpp_info = client.find_existing_dpp_member(clean_nik)
        target_ekta = dpp_info.get("nomor_ekta")
        if not input_nama and dpp_info.get("nama") and not client._is_admin_identity(dpp_info.get("nama")):
            input_nama = dpp_info.get("nama")

    if not target_ekta:
        return jsonify(
            error="Nomor e-KTA resmi belum dapat dideteksi otomatis dari portal DPP. Silakan masukkan nomor e-KTA 16 digit Anda (contoh: 2574055961675481) untuk disinkronkan langsung.",
            needsManualEkta=True,
        ), 404

    if member:
        member.nomor_ekta = target_ekta
        member.status_ktp_verified = True
        # Perbaiki jika nama kader sebelumnya sempat tertimpa nama Gabriela
        if input_nama and client._is_admin_identity(member.nama_lengkap):
            member.nama_lengkap = input_nama
        elif input_nama and not member.nama_lengkap:
            member.nama_lengkap = input_nama

        member.catatan_mutasi = (
            (member.catatan_mutasi or "") + f" | Sinkron e-KTA Resmi DPP ({target_ekta})"
        ).strip(" |")
        db.session.add(
            AuditLog(
                member_nik=clean_nik,
                member_name=member.nama_lengkap,
                action="UPDATE",
                new_status=member.status,
                reason=f"Tarik & Sinkron e-KTA Resmi DPP Pusat ({target_ekta})",
                actor=claims.get("adminName") or "Admin",
                dpd=member.dpd,
            )
        )
        db.session.commit()
        invalidate_all_data_cache()
        return jsonify(
            success=True,
            nomor_ekta=target_ekta,
            member=member_to_dict(member),
            message=f"Nomor e-KTA Resmi DPP ({target_ekta}) berhasil disinkronkan ke profil Anda!",
        ), 200
    else:
        # Jika belum ada di tabel member, buat data baru di PostgreSQL
        nama = input_nama or f"Kader NasDem ({clean_nik})"
        raw_vals = payload_to_member_values(
            {
                "nik": clean_nik,
                "namaLengkap": nama,
                "nomorEKTA": target_ekta,
                "status": "Aktif",
                "dpd": target_dpd,
                "dpc": target_dpc,
                "dprt": target_dprt,
                "isPengurus": False,
                "tingkatKepengurusan": "Bukan Pengurus",
                "jabatan": "Anggota Biasa",
                "statusKtpVerified": True,
                "catatanMutasi": f"Tarik & Sinkron e-KTA Resmi DPP Pusat ({target_ekta})",
            }
        )
        raw_vals["diubah_oleh"] = claims.get("adminName") or "Admin"
        raw_vals["nomor_ekta"] = target_ekta
        new_m = Member(**raw_vals)
        db.session.add(new_m)
        db.session.add(
            AuditLog(
                member_nik=clean_nik,
                member_name=new_m.nama_lengkap,
                action="CREATE",
                new_status="Aktif",
                reason=f"Tarik & Sinkron e-KTA Resmi DPP Pusat ({target_ekta})",
                actor=claims.get("adminName") or "Admin",
                dpd=new_m.dpd,
            )
        )
        db.session.commit()
        invalidate_all_data_cache()
        return jsonify(
            success=True,
            nomor_ekta=target_ekta,
            member=member_to_dict(new_m),
            message=f"Data kader dan Nomor e-KTA Resmi DPP ({target_ekta}) berhasil disimpan ke database!",
        ), 200


@bp.get("/dpp/kta-card/<nokta>")
@login_required
def get_dpp_kta_card(nokta: str):
    """Mengambil citra kartu e-KTA resmi langsung dari server DPP (PNG) secara terautentikasi."""
    claims = current_claims()
    clean_kta = re.sub(r"\D", "", str(nokta or ""))
    if not clean_kta:
        return jsonify(error="Nomor e-KTA tidak valid"), 400

    role = claims.get("role")
    try:
        local_m = Member.query.filter(Member.nomor_ekta == clean_kta).first()
    except Exception:
        local_m = None

    if role == "ANGGOTA":
        my_nik = str(claims.get("adminNik") or "").strip()
        if not local_m or local_m.nik != my_nik:
            return jsonify(error="Akses kartu e-KTA kader lain ditolak"), 403
    elif role in ("ADMIN_INVENTORY", "INVENTORY", "WAKIL_BENDAHARA"):
        return jsonify(error="Akun operasional tidak memiliki akses ke kartu e-KTA DPP"), 403
    elif local_m and not member_in_scope(claims, local_m):
        return jsonify(error="Kartu e-KTA kader ini berada di luar cakupan wilayah Anda"), 403

    client = get_dpp_client()
    img_bytes = client.fetch_kta_card_image(clean_kta)
    if not img_bytes:
        return jsonify(error="Kartu e-KTA belum tersedia atau tidak ditemukan di server DPP"), 404

    download = request.args.get("download") == "1"
    headers = {"Cache-Control": "private, no-store"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="eKTA_NasDem_{clean_kta}.png"'

    return Response(img_bytes, mimetype="image/png", headers=headers)


@bp.post("/dpp/create-kta")
@login_required
@rate_limit(15, 60, scope="user_or_ip", key_prefix="dpp_create")
def create_kta_endpoint():
    """Menerbitkan nomor e-KTA resmi dari portal Digital NasDem DPP dengan pembatasan wilayah ketat."""
    claims = current_claims()
    if not _can_use_dpp_ekta(claims):
        return jsonify(error="Hanya pengurus resmi atau admin wilayah yang berwenang menerbitkan e-KTA DPP."), 403

    data = request.get_json(silent=True) or {}
    raw_nik = str(data.get("nik") or "").strip()
    clean_nik = re.sub(r"\D", "", raw_nik)
    if len(clean_nik) < 9 or len(clean_nik) > 16:
        return jsonify(error="NIK wajib 9-16 digit angka yang valid"), 400

    # 1. Cek apakah kader sudah ada di database lokal & pastikan dalam wilayah akses akun
    member = db.session.get(Member, clean_nik)
    if member and not member_in_scope(claims, member):
        return jsonify(error="Data kader ini berada di luar wilayah akses akun Anda."), 403

    fallback_dpd, fallback_dpc, fallback_dprt = _scoped_fallback_territory(claims)

    # Gabungkan data input form dengan data member lokal (data form menjadi prioritas jika diisi)
    merged_dict = {
        "nik": clean_nik,
        "namaLengkap": data.get("namaLengkap") or (member.nama_lengkap if member else ""),
        "tempatLahir": data.get("tempatLahir") or (member.tempat_lahir if member else "MANADO"),
        "tanggalLahir": data.get("tanggalLahir") or (member.tanggal_lahir if member else None),
        "jenisKelamin": data.get("jenisKelamin") or (member.jenis_kelamin if member else "Laki-laki"),
        "alamatLengkap": data.get("alamatLengkap") or (member.alamat_lengkap if member else ""),
        "dpd": data.get("dpd") or (member.dpd if member else fallback_dpd),
        "dpc": data.get("dpc") or (member.dpc if member else fallback_dpc),
        "dprt": data.get("dprt") or (member.dprt if member else fallback_dprt),
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

    if not in_scope(claims, merged_dict["dpd"], merged_dict["dpc"], merged_dict["dprt"]):
        return jsonify(error="Wilayah pendaftaran e-KTA berada di luar cakupan akses akun Anda."), 403

    client = get_dpp_client()

    # Cegah duplikasi penerbitan jika NIK ternyata SUDAH MEMILIKI e-KTA di DPP Pusat
    dpp_existing = client.find_existing_dpp_member(clean_nik)
    if dpp_existing.get("exists") and dpp_existing.get("nomor_ekta"):
        found_ekta = dpp_existing["nomor_ekta"]
        logger.info(
            f"[DPP-AUTO-SYNC] NIK {clean_nik} sudah terdaftar dengan e-KTA {found_ekta} di DPP. Menghindari duplikasi createKTA!"
        )
        if member:
            member.nomor_ekta = found_ekta
            member.status_ktp_verified = True
            db.session.add(
                AuditLog(
                    member_nik=clean_nik,
                    member_name=member.nama_lengkap,
                    action="UPDATE",
                    new_status=member.status,
                    reason=f"Auto-Sync e-KTA Resmi dari DPP Pusat ({found_ekta})",
                    actor=claims.get("adminName") or "Admin",
                    dpd=member.dpd,
                )
            )
            db.session.commit()
            invalidate_all_data_cache()
            return jsonify({
                "success": True,
                "alreadyRegistered": True,
                "nomor_ekta": found_ekta,
                "member": member_to_dict(member),
                "message": f"NIK {clean_nik} sudah memiliki e-KTA Resmi di DPP Pusat ({found_ekta}). Berhasil disinkronkan langsung ke data diri Anda tanpa perlu bikin baru!",
            }), 200

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
