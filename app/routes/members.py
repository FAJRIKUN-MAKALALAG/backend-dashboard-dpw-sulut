from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import defer

from ..auth import current_claims, login_required, super_admin_required
from ..extensions import db
from ..models import AuditLog, Member
from ..utils import (
    HEAVY_COLS,
    STATUS_VALUES,
    apply_scope,
    in_scope,
    member_to_dict,
    payload_to_member_values,
)

bp = Blueprint("members", __name__)


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


def _filtered_query(claims):
    q = apply_scope(Member.query, claims)
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
    """Daftar anggota dengan pagination di sisi server (foto tidak ikut dikirim)."""
    claims = current_claims()
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(max(request.args.get("per_page", 50, type=int), 1), current_app.config["MAX_PAGE_SIZE"])

    q = _filtered_query(claims).options(*[defer(getattr(Member, c)) for c in HEAVY_COLS])
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
    if not m or not in_scope(current_claims(), m.dpd, m.dpc, m.dprt):
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

    values["diubah_oleh"] = claims.get("adminName")
    m = Member(**values)
    db.session.add(m)
    _audit(nik, m.nama_lengkap, "CREATE", "Registrasi anggota baru", claims.get("adminName"), m.dpd)
    db.session.commit()
    return jsonify(member_to_dict(m)), 201


@bp.put("/members/<nik>")
@login_required
def update_member(nik):
    claims = current_claims()
    m = db.session.get(Member, nik)
    if not m or not in_scope(claims, m.dpd, m.dpc, m.dprt):
        return jsonify(error="Anggota tidak ditemukan"), 404

    values = payload_to_member_values(request.get_json(silent=True) or {})
    values.pop("nik", None)
    if not in_scope(claims, values.get("dpd", m.dpd), values.get("dpc", m.dpc), values.get("dprt", m.dprt)):
        return jsonify(error="Wilayah tujuan di luar cakupan akses akun Anda"), 403
    if "status" in values and values["status"] not in STATUS_VALUES:
        return jsonify(error="Status tidak valid"), 400

    for k, v in values.items():
        setattr(m, k, v)
    m.diubah_oleh = claims.get("adminName")
    _audit(nik, m.nama_lengkap, "UPDATE", "Perubahan data anggota", claims.get("adminName"), m.dpd)
    db.session.commit()
    return jsonify(member_to_dict(m))


@bp.post("/members/<nik>/status")
@login_required
def change_status(nik):
    claims = current_claims()
    m = db.session.get(Member, nik)
    if not m or not in_scope(claims, m.dpd, m.dpc, m.dprt):
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
    _audit(nik, m.nama_lengkap, "STATUS_CHANGE", reason, claims.get("adminName"), m.dpd, prev, new_status)
    db.session.commit()
    return jsonify(member_to_dict(m))


@bp.delete("/members/<nik>")
@super_admin_required
def delete_member(nik):
    """Hapus langsung: hanya Super Admin. Admin daerah memakai /deletion-requests."""
    claims = current_claims()
    m = db.session.get(Member, nik)
    if not m:
        return jsonify(error="Anggota tidak ditemukan"), 404
    _audit(nik, m.nama_lengkap, "DELETE", "Dihapus langsung oleh Super Admin", claims.get("adminName"), m.dpd)
    db.session.delete(m)
    db.session.commit()
    return jsonify(deleted=nik)


@bp.post("/members/bulk")
@login_required
def bulk_upsert():
    """Import massal (upsert per chunk memakai INSERT .. ON CONFLICT)."""
    claims = current_claims()
    rows = (request.get_json(silent=True) or {}).get("members")
    if not isinstance(rows, list) or not rows:
        return jsonify(error="Body harus berisi 'members': [ ... ]"), 400
    max_rows = current_app.config["BULK_MAX_ROWS"]
    if len(rows) > max_rows:
        return jsonify(error=f"Maksimal {max_rows} baris per request. Pecah menjadi beberapa batch."), 413

    valid, errors, seen = {}, [], set()
    for i, raw in enumerate(rows):
        v = payload_to_member_values(raw)
        v.setdefault("dpc", "-")
        v.setdefault("dprt", "-")
        nik = v.get("nik") or ""
        if not (nik.isdigit() and len(nik) == 16):
            errors.append({"row": i, "nik": nik, "error": "NIK harus 16 digit angka"})
        elif nik in seen:
            errors.append({"row": i, "nik": nik, "error": "NIK ganda di dalam file"})
        elif not all(v.get(k) for k in ("nama_lengkap", "dpd")):
            errors.append({"row": i, "nik": nik, "error": "Nama/DPD wajib diisi"})
        elif not in_scope(claims, v["dpd"], v["dpc"], v["dprt"]):
            errors.append({"row": i, "nik": nik, "error": "Di luar cakupan wilayah akun"})
        else:
            seen.add(nik)
            v["diubah_oleh"] = claims.get("adminName")
            valid[nik] = v

    # PostgreSQL mensyaratkan seluruh baris dalam 1 statement punya kolom sama
    cols = sorted({k for v in valid.values() for k in v})
    items = [{c: v.get(c) for c in cols} for v in valid.values()]
    chunk = current_app.config["BULK_CHUNK_SIZE"]
    update_cols = [c for c in cols if c != "nik"]
    for i in range(0, len(items), chunk):
        stmt = pg_insert(Member.__table__).values(items[i : i + chunk])
        stmt = stmt.on_conflict_do_update(
            index_elements=["nik"], set_={c: stmt.excluded[c] for c in update_cols}
        )
        db.session.execute(stmt)

    if items:
        _audit(
            "-", f"{len(items)} anggota", "IMPORT_MASSAL",
            f"Import massal {len(items)} baris ({len(errors)} ditolak)",
            claims.get("adminName"), claims.get("dpdAccess") if claims.get("dpdAccess") != "ALL" else None,
        )
    db.session.commit()
    return jsonify(imported=len(items), rejected=len(errors), errors=errors[:200])


@bp.post("/members/bulk-delete")
@login_required
def bulk_delete():
    """Hapus massal data pengurus teritorial (sebelum import ulang). Hanya dalam cakupan wilayah akun."""
    claims = current_claims()
    niks = (request.get_json(silent=True) or {}).get("niks")
    if not isinstance(niks, list) or not niks:
        return jsonify(error="Body harus berisi 'niks': [ ... ]"), 400
    max_rows = current_app.config["BULK_MAX_ROWS"]
    if len(niks) > max_rows:
        return jsonify(error=f"Maksimal {max_rows} NIK per request."), 413

    niks = [str(n).strip() for n in niks if str(n).strip()]
    rows = apply_scope(Member.query, claims).filter(Member.nik.in_(niks)).all()
    for m in rows:
        db.session.delete(m)
    if rows:
        _audit(
            "-", f"{len(rows)} anggota", "DELETE",
            f"Penghapusan massal {len(rows)} data pengurus teritorial",
            claims.get("adminName"),
            rows[0].dpd,
        )
    db.session.commit()
    return jsonify(deleted=len(rows), requested=len(niks))
