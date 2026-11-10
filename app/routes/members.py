from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import func, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import defer, selectinload

from ..auth import current_claims, login_required, super_admin_required
from ..cache import get_or_compute, invalidate_all_data_cache
from ..extensions import db
from ..models import AppUser, AuditLog, Member, Position
from ..services.referral_service import find_direct_leader
from ..services.image_storage import intercept_and_store_member_photos, delete_physical_image
from ..dpp_integration import get_dpp_client
from ..utils import (
    HEAVY_COLS,
    POSITION_COLS,
    STATUS_VALUES,
    SUMMARY_COLS,
    apply_scope,
    in_scope,
    is_acceptable_nik,
    is_generated_nik,
    is_subsequence_digits,
    make_position_id,
    member_in_scope,
    member_scope_query,
    member_to_dict,
    payload_to_member_values,
    position_values_from_row,
    refresh_member_summary,
    rename_member_nik_safely,
)

bp = Blueprint("members", __name__)

_MEMBER_TABLE = Member.__table__
_POSITION_TABLE = Position.__table__


def _audit(member_nik, member_name, action, reason, actor, dpd, prev=None, new=None):
    db.session.add(
        AuditLog(
            member_nik=member_nik,
            member_name=member_name,
            action=action,
            previous_status=prev,
            new_status=new,
            reason=reason,
            actor=actor,
            dpd=dpd or "Sulawesi Utara",
        )
    )


def _apply_referral_and_leader(values: dict):
    """Mendeteksi perekrut dan menentukan Ketua Langsung secara hierarkis otomatis."""
    ref_code = (values.get("kader_perekrut_kode_referral") or "").strip().upper()
    recruiter = None
    if ref_code:
        recruiter = Member.query.filter(func.upper(Member.kode_referral) == ref_code).first()
    elif values.get("kader_perekrut_nik"):
        recruiter = Member.query.filter(Member.nik == values.get("kader_perekrut_nik")).first()

    if recruiter:
        values["kader_perekrut_kode_referral"] = recruiter.kode_referral
        values["kader_perekrut_nama"] = recruiter.nama_lengkap
        values["kader_perekrut_nik"] = recruiter.nik
        values["kader_perekrut_ekta"] = recruiter.nomor_ekta
        values["kader_perekrut_jabatan"] = recruiter.jabatan
        leader = find_direct_leader(recruiter)
        if leader:
            values["ketua_langsung_nik"] = leader.get("nik")
            values["ketua_langsung_nama"] = leader.get("nama")
            values["ketua_langsung_jabatan"] = leader.get("jabatan")
            values["ketua_langsung_tingkat"] = leader.get("tingkat")


def _filtered_query(claims):
    q = member_scope_query(claims)
    a = request.args
    for param, col in (("status", Member.status), ("dpd", Member.dpd), ("dpc", Member.dpc), ("dprt", Member.dprt)):
        if a.get(param):
            q = q.filter(col == a[param])
    if a.get("is_pengurus") in ("true", "false"):
        q = q.filter(Member.is_pengurus == (a["is_pengurus"] == "true"))

    term = (a.get("q") or "").strip()
    if term:
        by = a.get("search_by", "all")
        like = f"%{term}%"
        if by == "nik":
            q = q.filter(Member.nik.like(f"{term}%"))
        elif by == "nama":
            q = q.filter(Member.nama_lengkap.ilike(like))
        elif by == "dpc":
            q = q.filter(Member.dpc.ilike(like))
        else:
            q = q.filter(
                or_(
                    Member.nik.like(f"{term}%"),
                    Member.nama_lengkap.ilike(like),
                    Member.nomor_ekta.ilike(like),
                    Member.dpc.ilike(like),
                    Member.dprt.ilike(like),
                    Member.kader_perekrut_nama.ilike(like),
                )
            )
    return q


@bp.get("/members")
@login_required
def list_members():
    """Daftar ORANG (1 baris per NIK) beserta seluruh jabatannya di `positions`."""
    claims = current_claims()
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(max(request.args.get("per_page", 50, type=int), 1), current_app.config["MAX_PAGE_SIZE"])

    # Berkas foto tersimpan sebagai URL fisik WebP singkat (~58 bytes) pada server disk.
    # Selalu sertakan foto profil agar tampil di tabel anggota dan portal struktur secara default.
    include_photos = request.args.get("include_photos", "true").lower() != "false"

    a = request.args
    filter_sig = (
        f"st={a.get('status','')}&dpd={a.get('dpd','')}&dpc={a.get('dpc','')}"
        f"&dprt={a.get('dprt','')}&ip={a.get('is_pengurus','')}"
        f"&q={(a.get('q') or '').strip()}&sb={a.get('search_by','all')}"
    )
    page_sig = f"p={page}&pp={per_page}&ph={include_photos}&{filter_sig}"
    ttl = float(current_app.config.get("CACHE_TTL_SECONDS", 8))

    def _compute_page():
        if include_photos:
            q = _filtered_query(claims).options(selectinload(Member.positions))
        else:
            q = _filtered_query(claims).options(
                *[defer(getattr(Member, c)) for c in HEAVY_COLS], selectinload(Member.positions)
            )
        # Cache total count per filter sehingga paralel page 1..N hanya menjalankan COUNT(*) satu kali
        total = get_or_compute(
            "members_count",
            claims,
            filter_sig,
            ttl_seconds=ttl,
            compute_fn=lambda: q.order_by(None).count(),
        )
        rows = q.order_by(Member.nama_lengkap, Member.nik).offset((page - 1) * per_page).limit(per_page).all()
        return {
            "items": [member_to_dict(m, include_photos=include_photos) for m in rows],
            "page": page,
            "perPage": per_page,
            "total": total,
            "totalPages": (total + per_page - 1) // per_page,
        }

    payload = get_or_compute("members", claims, page_sig, ttl_seconds=ttl, compute_fn=_compute_page)
    return jsonify(payload)


@bp.get("/members/<nik>")
@login_required
def get_member(nik):
    m = db.session.get(Member, nik)
    if not m or not member_in_scope(current_claims(), m):
        return jsonify(error="Anggota tidak ditemukan"), 404
    return jsonify(member_to_dict(m))


@bp.get("/members/check-nik/<nik>")
@login_required
def check_nik(nik):
    """Deteksi duplikasi NIK realtime untuk form registrasi."""
    exists = db.session.query(Member.nik).filter(Member.nik == nik).first() is not None
    return jsonify(nik=nik, exists=exists)


def check_tingkat_permission(role: str, tingkat: str) -> bool:
    """Memastikan admin daerah tidak mengangkat pengurus di atas level wewenangnya."""
    t = (tingkat or "").strip().upper()
    if not t or t in ("BUKAN PENGURUS", "UMUM", "-", ""):
        return True
    if role in ("SUPER_ADMIN", "ADMIN_DPW"):
        return True
    if role == "ADMIN_DPD":
        return t in ("DPD", "DPC", "DPRT", "RANTING")
    if role == "ADMIN_DPC":
        return t in ("DPC", "DPRT", "RANTING")
    if role == "ADMIN_DPRT":
        return t in ("DPRT", "RANTING")
    return False


@bp.post("/members")
@login_required
def create_member():
    claims = current_claims()
    # Pembatasan: Anggota biasa tidak berwenang menambah anggota baru. Hanya pengurus resmi ber-SK.
    if claims.get("role") == "ANGGOTA" or not claims.get("canAddMembers", True):
        return jsonify(error="Hanya pengurus resmi pemegang SK yang berwenang menambahkan anggota baru."), 403

    raw_payload = request.get_json(silent=True) or {}
    values = payload_to_member_values(raw_payload)
    nik = values.get("nik")
    if not is_acceptable_nik(nik):
        return jsonify(error="NIK harus berupa angka (8-20 digit)"), 400
    for req in ("nama_lengkap", "dpd"):
        if not values.get(req):
            return jsonify(error=f"Field {req} wajib diisi"), 400
    values.setdefault("dpc", "-")
    values.setdefault("dprt", "-")
    if not in_scope(claims, values["dpd"], values["dpc"], values["dprt"]):
        return jsonify(error="Wilayah di luar cakupan akses akun Anda"), 403

    tingkat = values.get("tingkat_kepengurusan")
    if not check_tingkat_permission(claims.get("role"), tingkat):
        return jsonify(error=f"Akun Anda tidak memiliki wewenang untuk menetapkan tingkat kepengurusan {tingkat}"), 403

    if db.session.get(Member, nik):
        return jsonify(error="NIK sudah terdaftar"), 409

    # Otomatis simpan foto Base64 ke berkas fisik WebP di disk server
    values = intercept_and_store_member_photos(values, nik)

    _apply_referral_and_leader(values)
    values["diubah_oleh"] = claims.get("adminName")
    pos = position_values_from_row(values)
    m = Member(**values)
    if pos:
        m.positions.append(Position(**pos))
    db.session.add(m)

    # Otomatisasi Penerbitan e-KTA Resmi DPP Digital NasDem jika diaktifkan
    dpp_result = None
    should_register_dpp = raw_payload.get("autoRegisterDpp", True)
    if should_register_dpp:
        try:
            dpp_client = get_dpp_client()
            if dpp_client.is_configured():
                current_app.logger.info(f"[DPP-TRIGGER] Memulai permohonan e-KTA resmi ke DPP untuk NIK {nik}...")
                dpp_res = dpp_client.create_kta_for_member(
                    member_dict=values,
                    foto_ktp_source=values.get("foto_ktp_url"),
                    foto_profil_source=values.get("foto_profil_url"),
                )
                dpp_result = dpp_res
                if dpp_res.get("success") and dpp_res.get("nomor_ekta"):
                    m.nomor_ekta = dpp_res["nomor_ekta"]
                    current_app.logger.info(
                        f"[DPP-KTA-LINKED] NIK {nik} sukses dikaitkan dengan e-KTA resmi DPP: {dpp_res['nomor_ekta']}"
                    )
        except Exception as exc:
            current_app.logger.warning(f"[DPP-AUTO-REGISTER-FAILED] Gagal menerbitkan e-KTA DPP: {exc}")

    _audit(nik, m.nama_lengkap, "CREATE", "Registrasi anggota baru", claims.get("adminName"), m.dpd)
    db.session.commit()
    invalidate_all_data_cache()
    if m.foto_profil_url or m.foto_ktp_url:
        current_app.logger.info(
            f"[DB-PHOTO-SAVED] Registrasi anggota '{m.nama_lengkap}' (NIK: {m.nik}) berhasil tersimpan dengan foto di database: "
            f"Profil: {m.foto_profil_url or '-'} | KTP: {m.foto_ktp_url or '-'}"
        )
    resp_data = member_to_dict(m)
    if dpp_result:
        resp_data["dppResult"] = dpp_result
    return jsonify(resp_data), 201


@bp.put("/members/<nik>")
@login_required
def update_member(nik):
    claims = current_claims()
    clean_nik = str(nik).strip()
    m = db.session.get(Member, clean_nik)
    if not m:
        m = Member.query.filter(Member.nik == clean_nik).first()
    if not m or not member_in_scope(claims, m):
        return jsonify(error="Anggota tidak ditemukan"), 404

    data = request.get_json(silent=True) or {}
    new_nik = data.get("nik")
    values = payload_to_member_values(data)
    values.pop("nik", None)

    # Dukungan koreksi NIK jika admin mengubah NIK pada formulir edit (misal koreksi 15 digit -> 16 digit)
    if new_nik and str(new_nik).strip() != clean_nik:
        clean_new_nik = str(new_nik).strip()
        if not is_acceptable_nik(clean_new_nik):
            return jsonify(error="NIK baru harus berupa angka (8-20 digit)"), 400
        existing = db.session.get(Member, clean_new_nik)
        if existing and existing.nik != clean_nik:
            return jsonify(error="NIK baru sudah terdaftar pada anggota lain"), 409
        m = rename_member_nik_safely(db.session, m, clean_new_nik)

    if not in_scope(claims, values.get("dpd", m.dpd), values.get("dpc", m.dpc), values.get("dprt", m.dprt)):
        if not member_in_scope(claims, m):
            return jsonify(error="Wilayah tujuan di luar cakupan akses akun Anda"), 403

    tingkat = values.get("tingkat_kepengurusan")
    if tingkat and not check_tingkat_permission(claims.get("role"), tingkat):
        return jsonify(error=f"Akun Anda tidak memiliki wewenang untuk menetapkan tingkat kepengurusan {tingkat}"), 403

    if "status" in values and values["status"] not in STATUS_VALUES:
        return jsonify(error="Status tidak valid"), 400

    # Simpan berkas foto fisik WebP jika ada update foto
    values = intercept_and_store_member_photos(values, m.nik)

    # Bersihkan berkas fisik lama jika foto eksplisit dihapus
    if "foto_profil_url" in values and not values["foto_profil_url"] and m.foto_profil_url:
        delete_physical_image(m.foto_profil_url)
    if "foto_ktp_url" in values and not values["foto_ktp_url"] and m.foto_ktp_url:
        delete_physical_image(m.foto_ktp_url)

    _apply_referral_and_leader(values)
    is_pengurus_val = values.get("is_pengurus")
    pos = position_values_from_row({**values, "nik": m.nik})

    # Update seluruh field umum
    for k, v in values.items():
        if k not in SUMMARY_COLS:
            if hasattr(m, k):
                setattr(m, k, v)

    # Kelola perubahan jabatan struktural (tabel positions)
    if is_pengurus_val is False:
        # Jika diubah menjadi Anggota Biasa (non-pengurus), bersihkan seluruh jabatan struktural
        Position.query.filter(Position.member_nik == m.nik).delete(synchronize_session=False)
        m.positions = []
        m.is_pengurus = False
        m.tingkat_kepengurusan = "Bukan Pengurus"
        m.wilayah_penugasan = None
        m.jabatan = values.get("jabatan") or "Anggota Biasa"
        m.nomor_sk = None
        m.tanggal_sk = None
        m.urutan = None
    elif pos:
        # Perbarui posisi struktural secara selektif tanpa menghapus rangkap jabatan lainnya milik kader
        existing_exact = db.session.get(Position, pos["id"])
        if existing_exact:
            for k, v in pos.items():
                if k != "id" and hasattr(existing_exact, k):
                    setattr(existing_exact, k, v)
        else:
            same_suborg_positions = Position.query.filter(
                Position.member_nik == m.nik,
                Position.tingkat_kepengurusan == pos["tingkat_kepengurusan"],
                Position.dpd == pos["dpd"],
                Position.dpc == pos["dpc"],
                Position.dprt == pos["dprt"],
                Position.wilayah_penugasan == pos["wilayah_penugasan"],
            ).all()
            if len(same_suborg_positions) == 1:
                db.session.delete(same_suborg_positions[0])
                db.session.flush()
            db.session.add(Position(**pos))
        db.session.flush()
        db.session.expire(m, ["positions"])
        refresh_member_summary(m)
    else:
        # Jika tidak ada posisi baru tetapi mempertahankan pengurus/anggota
        if m.positions:
            refresh_member_summary(m)
        else:
            for k in SUMMARY_COLS:
                if k in values and hasattr(m, k):
                    setattr(m, k, values[k])

    m.diubah_oleh = claims.get("adminName") or "Administrator"
    _audit(m.nik, m.nama_lengkap, "UPDATE", "Perubahan data anggota via manajemen kader", m.diubah_oleh, m.dpd)
    db.session.commit()
    invalidate_all_data_cache()
    if m.foto_profil_url or m.foto_ktp_url:
        current_app.logger.info(
            f"[DB-PHOTO-SAVED] Perubahan data anggota '{m.nama_lengkap}' (NIK: {m.nik}) berhasil tersimpan dengan foto di database: "
            f"Profil: {m.foto_profil_url or '-'} | KTP: {m.foto_ktp_url or '-'}"
        )
    return jsonify(member_to_dict(m))


@bp.get("/members/me/profile")
@login_required
def get_my_profile():
    """Mengambil profil lengkap akun yang sedang login untuk portal mandiri."""
    claims = current_claims()
    my_nik = claims.get("adminNik")
    if not my_nik:
        return jsonify(error="Sesi akun tidak memiliki identitas NIK anggota"), 400

    m = db.session.get(Member, my_nik)
    if not m:
        m = Member.query.filter(Member.nik == my_nik).first()
    if not m:
        return jsonify(error="Data profil Anda tidak ditemukan di database"), 404

    return jsonify(member=member_to_dict(m, include_photos=True))


@bp.put("/members/me/profile")
@login_required
def update_my_profile():
    """Memperbolehkan setiap kader/pengurus mengedit biodata kontak & foto profil/KTP mereka sendiri."""
    claims = current_claims()
    my_nik = claims.get("adminNik")
    if not my_nik:
        return jsonify(error="Sesi akun tidak memiliki identitas NIK anggota"), 400

    m = db.session.get(Member, my_nik)
    if not m:
        m = Member.query.filter(Member.nik == my_nik).first()
    if not m:
        return jsonify(error="Data profil Anda tidak ditemukan di database"), 404

    data = request.get_json(silent=True) or {}
    values = payload_to_member_values(data)

    # Hanya field kontak, biodata pribadi, dan foto yang diizinkan diubah mandiri oleh pemilik akun:
    allowed_cols = {
        "nomor_hp", "email", "sosmed", "alamat_lengkap", "agama",
        "pekerjaan", "tempat_lahir", "tanggal_lahir", "jenis_kelamin",
        "foto_profil_url", "foto_ktp_url"
    }
    filtered_values = {k: v for k, v in values.items() if k in allowed_cols}

    # Intercept Base64 photos -> simpan ke disk WebP
    filtered_values = intercept_and_store_member_photos(filtered_values, m.nik)

    # Bersihkan berkas fisik lama jika foto eksplisit dihapus
    if "foto_profil_url" in filtered_values and not filtered_values["foto_profil_url"] and m.foto_profil_url:
        delete_physical_image(m.foto_profil_url)
    if "foto_ktp_url" in filtered_values and not filtered_values["foto_ktp_url"] and m.foto_ktp_url:
        delete_physical_image(m.foto_ktp_url)

    for k, v in filtered_values.items():
        if hasattr(m, k):
            setattr(m, k, v)

    m.diubah_oleh = claims.get("adminName") or m.nama_lengkap
    _audit(m.nik, m.nama_lengkap, "SELF_UPDATE", "Pembaruan profil mandiri oleh kader", m.nama_lengkap, m.dpd)
    db.session.commit()
    invalidate_all_data_cache()
    if m.foto_profil_url or m.foto_ktp_url:
        current_app.logger.info(
            f"[DB-PHOTO-SAVED] Profil mandiri '{m.nama_lengkap}' (NIK: {m.nik}) berhasil tersimpan dengan foto di database: "
            f"Profil: {m.foto_profil_url or '-'} | KTP: {m.foto_ktp_url or '-'}"
        )
    return jsonify(member=member_to_dict(m, include_photos=True), message="Profil Anda berhasil diperbarui")


@bp.post("/members/<nik>/status")
@login_required
def change_status(nik):
    claims = current_claims()
    clean_nik = str(nik).strip()
    m = db.session.get(Member, clean_nik)
    if not m:
        m = Member.query.filter(Member.nik == clean_nik).first()
    if not m or not member_in_scope(claims, m):
        return jsonify(error="Anggota tidak ditemukan"), 404
    data = request.get_json(silent=True) or {}
    new_status, reason = data.get("status"), (data.get("reason") or "").strip()
    if new_status not in STATUS_VALUES:
        return jsonify(error="Status tidak valid"), 400
    if not reason:
        return jsonify(error="Catatan/alasan mutasi wajib diisi"), 400

    prev = m.status
    m.status = new_status
    m.catatan_mutasi = reason
    m.diubah_oleh = claims.get("adminName") or "Administrator"
    _audit(m.nik, m.nama_lengkap, "STATUS_CHANGE", reason, m.diubah_oleh, m.dpd, prev, new_status)
    db.session.commit()
    invalidate_all_data_cache()
    return jsonify(member_to_dict(m))


@bp.delete("/members/<nik>")
@login_required
def delete_member(nik):
    """Hapus orang beserta seluruh jabatannya: Super Admin & Admin DPW memiliki hak penuh langsung."""
    claims = current_claims()
    role = claims.get("role")
    if role not in ("SUPER_ADMIN", "ADMIN_DPW"):
        return jsonify(
            error="Hanya Super Admin dan Admin DPW yang memiliki wewenang untuk menghapus kader secara langsung. Pengurus DPD/DPC/DPRt silakan gunakan fitur Pengajuan Penghapusan."
        ), 403

    clean_nik = str(nik).strip()
    m = db.session.get(Member, clean_nik)
    if not m:
        m = Member.query.filter(Member.nik == clean_nik).first()
    if not m:
        return jsonify(error="Anggota tidak ditemukan"), 404

    # Hapus juga positions jika ada
    Position.query.filter(Position.member_nik == m.nik).delete(synchronize_session=False)

    actor_name = claims.get("adminName") or role
    _audit(m.nik, m.nama_lengkap, "DELETE", f"Dihapus langsung oleh {actor_name}", actor_name, m.dpd)
    db.session.delete(m)
    db.session.commit()
    invalidate_all_data_cache()
    return jsonify(deleted=clean_nik)


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _upsert_people(rows: dict, *, keep_domicile: bool, chunk: int):
    """Upsert orang per NIK. Nilai kosong tidak menimpa data yang sudah ada (COALESCE).

    keep_domicile=True dipakai untuk baris yang membawa jabatan: orang yang sudah ada tidak dipindah
    domisilinya dan statusnya tidak diubah oleh import struktur.
    """
    if not rows:
        return
    cols = sorted({k for v in rows.values() for k in v})
    skip = {"nik", *SUMMARY_COLS}
    if keep_domicile:
        skip |= {"status", "tanggal_bergabung"}
    update_cols = [c for c in cols if c not in skip]
    items = [{c: v.get(c) for c in cols} for v in rows.values()]
    for part in _chunks(items, chunk):
        stmt = pg_insert(_MEMBER_TABLE).values(part)
        if update_cols:
            stmt = stmt.on_conflict_do_update(
                index_elements=["nik"],
                set_={c: func.coalesce(stmt.excluded[c], _MEMBER_TABLE.c[c]) for c in update_cols},
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=["nik"])
        db.session.execute(stmt)


def _upsert_rows(raw_rows, claims, replace_territory_suborgs: bool = False):
    """Inti import: baris flat (orang + jabatan) -> members (1 per NIK) + positions (1 per kursi)."""
    errors, people, people_pos, positions = [], {}, {}, {}
    for i, raw in enumerate(raw_rows):
        v = payload_to_member_values(raw)
        v.setdefault("dpc", "-")
        v.setdefault("dprt", "-")
        nik = v.get("nik") or ""
        if not is_acceptable_nik(nik):
            errors.append({"row": i, "nik": nik, "error": "NIK harus berupa angka (8-20 digit)"})
            continue
        if not all(v.get(k) for k in ("nama_lengkap", "dpd")):
            errors.append({"row": i, "nik": nik, "error": "Nama/DPD wajib diisi"})
            continue
        if not in_scope(claims, v["dpd"], v["dpc"], v["dprt"]):
            errors.append({"row": i, "nik": nik, "error": "Di luar cakupan wilayah akun"})
            continue
        _apply_referral_and_leader(v)
        v["diubah_oleh"] = claims.get("adminName")
        pos = position_values_from_row(v)
        if pos:
            positions[pos["id"]] = pos  # kursi sama dalam 1 file: baris terakhir menang
            people_pos.setdefault(nik, v)  # baris pertama menentukan domisili orang baru
        else:
            people[nik] = v

    for nik in people_pos:
        people.pop(nik, None)

    chunk = current_app.config["BULK_CHUNK_SIZE"]
    _upsert_people(people, keep_domicile=False, chunk=chunk)
    _upsert_people(people_pos, keep_domicile=True, chunk=chunk)

    replaced_old_niks = set()
    if positions and replace_territory_suborgs:
        target_scopes = {
            (
                p.get("tingkat_kepengurusan"),
                p.get("dpd"),
                p.get("dpc") or "-",
                p.get("dprt") or "-",
                p.get("wilayah_penugasan"),
            )
            for p in positions.values()
        }
        incoming_pos_ids = set(positions.keys())
        for t_lvl, t_dpd, t_dpc, t_dprt, t_wil in target_scopes:
            if not t_lvl or not t_dpd:
                continue
            q = Position.query.filter(
                Position.tingkat_kepengurusan == t_lvl,
                func.lower(Position.dpd) == str(t_dpd).lower(),
            )
            if t_lvl in ("DPC", "DPRt"):
                q = q.filter(func.lower(Position.dpc) == str(t_dpc).lower())
            if t_lvl == "DPRt":
                q = q.filter(func.lower(Position.dprt) == str(t_dprt).lower())
            if t_wil:
                q = q.filter(func.lower(Position.wilayah_penugasan) == str(t_wil).lower())
            for old_pos in q.all():
                if old_pos.id not in incoming_pos_ids:
                    if old_pos.member_nik:
                        replaced_old_niks.add(old_pos.member_nik)
                    db.session.delete(old_pos)
        db.session.flush()

    if positions:
        update_cols = list(POSITION_COLS)
        for part in _chunks(list(positions.values()), chunk):
            cols = ["id", "member_nik", *POSITION_COLS]
            stmt = pg_insert(_POSITION_TABLE).values([{c: p.get(c) for c in cols} for p in part])
            stmt = stmt.on_conflict_do_update(
                index_elements=["id"], set_={c: stmt.excluded[c] for c in update_cols}
            )
            db.session.execute(stmt)

    # Hitung ulang ringkasan jabatan utama untuk orang yang punya jabatan atau yang jabatannya digantikan
    touched = list({*people, *people_pos, *replaced_old_niks})
    if touched:
        db.session.expire_all()
        for part in _chunks(touched, 500):
            for m in Member.query.options(selectinload(Member.positions)).filter(Member.nik.in_(part)):
                if m.positions or m.is_pengurus:
                    refresh_member_summary(m)
                # Sinkronkan akun login AppUser lama yang memakai NIK <16 digit bila kini diperbarui ke 16 digit
                if len(m.nik) == 16 and not is_generated_nik(m.nik):
                    short_users = AppUser.query.filter(
                        AppUser.member_nik.like(f"{m.nik[:6]}%"),
                        func.length(AppUser.member_nik) != 16,
                    ).all()
                    for su in short_users:
                        snik = (su.member_nik or "").strip()
                        same_name = (su.full_name or "").strip().lower() == (m.nama_lengkap or "").strip().lower()
                        if same_name and 10 <= len(snik) < 16 and is_subsequence_digits(snik, m.nik):
                            taken = AppUser.query.filter(func.lower(AppUser.username) == m.nik.lower()).first()
                            su.member_nik = m.nik
                            if su.username == snik and (not taken or taken.id == su.id):
                                su.username = m.nik
    return errors, len(people) + len(people_pos), len(positions)


@bp.post("/members/bulk")
@login_required
def bulk_upsert():
    """Import massal. Orang disimpan sekali per NIK; jabatan disimpan terpisah (tanpa saling menimpa)."""
    claims = current_claims()
    body = request.get_json(silent=True) or {}
    rows = body.get("members")
    replace_territory_suborgs = bool(body.get("replaceTerritorySubOrgs"))
    if not isinstance(rows, list) or not rows:
        return jsonify(error="Body harus berisi 'members': [ ... ]"), 400
    max_rows = current_app.config["BULK_MAX_ROWS"]
    if len(rows) > max_rows:
        return jsonify(error=f"Maksimal {max_rows} baris per request. Pecah menjadi beberapa batch."), 413

    errors, people_count, position_count = _upsert_rows(
        rows, claims, replace_territory_suborgs=replace_territory_suborgs
    )
    if people_count:
        _audit(
            "-", f"{people_count} anggota / {position_count} jabatan", "IMPORT_MASSAL",
            f"Import massal {people_count} anggota, {position_count} jabatan ({len(errors)} ditolak)",
            claims.get("adminName"), claims.get("dpdAccess") if claims.get("dpdAccess") != "ALL" else None,
        )
    db.session.commit()
    invalidate_all_data_cache()
    return jsonify(
        imported=people_count, positions=position_count, rejected=len(errors), errors=errors[:200]
    )


@bp.post("/members/bulk-delete")
@login_required
def bulk_delete():
    """Hapus massal.

    - `positionIds`: hapus JABATAN saja (orangnya tetap sebagai anggota; kecuali NIK sementara buatan
      sistem yang tidak punya jabatan lagi, ikut dibersihkan agar tidak menumpuk saat import ulang).
    - `niks`: hapus ORANG beserta seluruh jabatannya (dalam cakupan wilayah akun).
    """
    claims = current_claims()
    body = request.get_json(silent=True) or {}
    position_ids = [str(x).strip() for x in (body.get("positionIds") or []) if str(x).strip()]
    niks = [str(n).strip() for n in (body.get("niks") or []) if str(n).strip()]
    if not position_ids and not niks:
        return jsonify(error="Body harus berisi 'positionIds' atau 'niks'"), 400
    max_rows = current_app.config["BULK_MAX_ROWS"]
    if len(position_ids) > max_rows or len(niks) > max_rows:
        return jsonify(error=f"Maksimal {max_rows} data per request."), 413

    deleted_positions = deleted_members = 0
    first_dpd = None

    if position_ids:
        pos_rows = apply_scope(Position.query, claims, Position).filter(Position.id.in_(position_ids)).all()
        affected = {p.member_nik for p in pos_rows}
        first_dpd = pos_rows[0].dpd if pos_rows else None
        for p in pos_rows:
            db.session.delete(p)
        deleted_positions = len(pos_rows)
        db.session.flush()
        db.session.expire_all()
        for nik in affected:
            m = db.session.get(Member, nik)
            if not m:
                continue
            if not m.positions and is_generated_nik(m.nik):
                db.session.delete(m)
                deleted_members += 1
            else:
                refresh_member_summary(m)

    if niks:
        members = [m for m in Member.query.filter(Member.nik.in_(niks)).all() if member_in_scope(claims, m)]
        for m in members:
            first_dpd = first_dpd or m.dpd
            db.session.delete(m)
        deleted_members += len(members)

    if deleted_positions or deleted_members:
        _audit(
            "-", f"{deleted_positions} jabatan / {deleted_members} anggota", "DELETE",
            f"Penghapusan massal {deleted_positions} jabatan dan {deleted_members} anggota teritorial",
            claims.get("adminName"), first_dpd,
        )
    db.session.commit()
    invalidate_all_data_cache()
    return jsonify(
        deleted=deleted_positions + deleted_members,
        deletedPositions=deleted_positions,
        deletedMembers=deleted_members,
    )
