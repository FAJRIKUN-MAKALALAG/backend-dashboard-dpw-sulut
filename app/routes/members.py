from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import func, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import defer, selectinload

from ..auth import current_claims, login_required, super_admin_required
from ..extensions import db
from ..models import AuditLog, Member, Position
from ..services.referral_service import find_direct_leader
from ..utils import (
    HEAVY_COLS,
    POSITION_COLS,
    STATUS_VALUES,
    SUMMARY_COLS,
    apply_scope,
    in_scope,
    is_generated_nik,
    make_position_id,
    member_in_scope,
    member_scope_query,
    member_to_dict,
    payload_to_member_values,
    position_values_from_row,
    refresh_member_summary,
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

    q = _filtered_query(claims).options(
        *[defer(getattr(Member, c)) for c in HEAVY_COLS], selectinload(Member.positions)
    )
    total = q.order_by(None).count()
    rows = q.order_by(Member.nama_lengkap, Member.nik).offset((page - 1) * per_page).limit(per_page).all()

    return jsonify(
        items=[member_to_dict(m, include_photos=False) for m in rows],
        page=page,
        perPage=per_page,
        total=total,
        totalPages=(total + per_page - 1) // per_page,
    )


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


@bp.post("/members")
@login_required
def create_member():
    claims = current_claims()
    values = payload_to_member_values(request.get_json(silent=True) or {})
    nik = values.get("nik")
    if not nik or not nik.isdigit() or len(nik) != 16:
        return jsonify(error="NIK harus 16 digit angka"), 400
    for req in ("nama_lengkap", "dpd"):
        if not values.get(req):
            return jsonify(error=f"Field {req} wajib diisi"), 400
    values.setdefault("dpc", "-")
    values.setdefault("dprt", "-")
    if not in_scope(claims, values["dpd"], values["dpc"], values["dprt"]):
        return jsonify(error="Wilayah di luar cakupan akses akun Anda"), 403
    if db.session.get(Member, nik):
        return jsonify(error="NIK sudah terdaftar"), 409

    _apply_referral_and_leader(values)
    values["diubah_oleh"] = claims.get("adminName")
    pos = position_values_from_row(values)
    m = Member(**values)
    if pos:
        m.positions.append(Position(**pos))
    db.session.add(m)
    _audit(nik, m.nama_lengkap, "CREATE", "Registrasi anggota baru", claims.get("adminName"), m.dpd)
    db.session.commit()
    return jsonify(member_to_dict(m)), 201


@bp.put("/members/<nik>")
@login_required
def update_member(nik):
    claims = current_claims()
    m = db.session.get(Member, nik)
    if not m or not member_in_scope(claims, m):
        return jsonify(error="Anggota tidak ditemukan"), 404

    values = payload_to_member_values(request.get_json(silent=True) or {})
    values.pop("nik", None)
    if not in_scope(claims, values.get("dpd", m.dpd), values.get("dpc", m.dpc), values.get("dprt", m.dprt)):
        if not member_in_scope(claims, m):
            return jsonify(error="Wilayah tujuan di luar cakupan akses akun Anda"), 403
    if "status" in values and values["status"] not in STATUS_VALUES:
        return jsonify(error="Status tidak valid"), 400

    _apply_referral_and_leader(values)
    pos = position_values_from_row({**values, "nik": m.nik})
    has_positions = bool(pos) or bool(m.positions)
    for k, v in values.items():
        # Ringkasan jabatan dikelola otomatis dari tabel positions
        if k in SUMMARY_COLS and has_positions:
            continue
        setattr(m, k, v)
    if pos:
        db.session.merge(Position(**pos))
        db.session.flush()
        db.session.expire(m, ["positions"])
    if m.positions:
        refresh_member_summary(m)
    m.diubah_oleh = claims.get("adminName")
    _audit(m.nik, m.nama_lengkap, "UPDATE", "Perubahan data anggota", claims.get("adminName"), m.dpd)
    db.session.commit()
    return jsonify(member_to_dict(m))


@bp.post("/members/<nik>/status")
@login_required
def change_status(nik):
    claims = current_claims()
    m = db.session.get(Member, nik)
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
    m.diubah_oleh = claims.get("adminName")
    _audit(m.nik, m.nama_lengkap, "STATUS_CHANGE", reason, claims.get("adminName"), m.dpd, prev, new_status)
    db.session.commit()
    return jsonify(member_to_dict(m))


@bp.delete("/members/<nik>")
@super_admin_required
def delete_member(nik):
    """Hapus orang beserta seluruh jabatannya: hanya Super Admin. Admin daerah memakai /deletion-requests."""
    claims = current_claims()
    m = db.session.get(Member, nik)
    if not m:
        return jsonify(error="Anggota tidak ditemukan"), 404
    _audit(m.nik, m.nama_lengkap, "DELETE", "Dihapus langsung oleh Super Admin", claims.get("adminName"), m.dpd)
    db.session.delete(m)
    db.session.commit()
    return jsonify(deleted=nik)


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
        skip |= {"dpd", "dpc", "dprt", "status", "tanggal_bergabung"}
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


def _upsert_rows(raw_rows, claims):
    """Inti import: baris flat (orang + jabatan) -> members (1 per NIK) + positions (1 per kursi)."""
    errors, people, people_pos, positions = [], {}, {}, {}
    for i, raw in enumerate(raw_rows):
        v = payload_to_member_values(raw)
        v.setdefault("dpc", "-")
        v.setdefault("dprt", "-")
        nik = v.get("nik") or ""
        if not (nik.isdigit() and len(nik) == 16):
            errors.append({"row": i, "nik": nik, "error": "NIK harus 16 digit angka"})
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

    if positions:
        update_cols = list(POSITION_COLS)
        for part in _chunks(list(positions.values()), chunk):
            cols = ["id", "member_nik", *POSITION_COLS]
            stmt = pg_insert(_POSITION_TABLE).values([{c: p.get(c) for c in cols} for p in part])
            stmt = stmt.on_conflict_do_update(
                index_elements=["id"], set_={c: stmt.excluded[c] for c in update_cols}
            )
            db.session.execute(stmt)

    # Hitung ulang ringkasan jabatan utama untuk orang yang punya jabatan
    touched = list({*people, *people_pos})
    if touched:
        db.session.expire_all()
        for part in _chunks(touched, 500):
            for m in Member.query.options(selectinload(Member.positions)).filter(Member.nik.in_(part)):
                if m.positions or m.is_pengurus:
                    refresh_member_summary(m)
    return errors, len(people) + len(people_pos), len(positions)


@bp.post("/members/bulk")
@login_required
def bulk_upsert():
    """Import massal. Orang disimpan sekali per NIK; jabatan disimpan terpisah (tanpa saling menimpa)."""
    claims = current_claims()
    rows = (request.get_json(silent=True) or {}).get("members")
    if not isinstance(rows, list) or not rows:
        return jsonify(error="Body harus berisi 'members': [ ... ]"), 400
    max_rows = current_app.config["BULK_MAX_ROWS"]
    if len(rows) > max_rows:
        return jsonify(error=f"Maksimal {max_rows} baris per request. Pecah menjadi beberapa batch."), 413

    errors, people_count, position_count = _upsert_rows(rows, claims)
    if people_count:
        _audit(
            "-", f"{people_count} anggota / {position_count} jabatan", "IMPORT_MASSAL",
            f"Import massal {people_count} anggota, {position_count} jabatan ({len(errors)} ditolak)",
            claims.get("adminName"), claims.get("dpdAccess") if claims.get("dpdAccess") != "ALL" else None,
        )
    db.session.commit()
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
    return jsonify(
        deleted=deleted_positions + deleted_members,
        deletedPositions=deleted_positions,
        deletedMembers=deleted_members,
    )
