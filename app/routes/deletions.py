from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from ..auth import current_claims, login_required, super_admin_required
from ..extensions import db
from ..models import AuditLog, DeletionRequest, Member
from ..utils import apply_scope, in_scope, row_to_dict

bp = Blueprint("deletions", __name__)

DEL_FIELDS = {
    "id": "id", "member_nik": "memberNik", "member_name": "memberName", "dpd": "dpd", "dpc": "dpc",
    "dprt": "dprt", "requested_by": "requestedByName", "requested_at": "requestedAt", "reason": "reason",
    "status": "status", "reviewed_by": "reviewedBy", "reviewed_at": "reviewedAt", "review_notes": "reviewNotes",
}


@bp.get("/deletion-requests")
@login_required
def list_requests():
    q = apply_scope(DeletionRequest.query, current_claims(), DeletionRequest)
    if request.args.get("status"):
        q = q.filter(DeletionRequest.status == request.args["status"])
    rows = q.order_by(DeletionRequest.created_at.desc()).limit(500).all()
    return jsonify(items=[row_to_dict(r, DEL_FIELDS) for r in rows])


@bp.post("/deletion-requests")
@login_required
def create_request():
    claims = current_claims()
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    if len(reason) < 15:
        return jsonify(error="Alasan penghapusan minimal 15 karakter"), 400
    m = db.session.get(Member, data.get("nik") or data.get("memberNik") or "")
    if not m or not in_scope(claims, m.dpd, m.dpc, m.dprt):
        return jsonify(error="Anggota tidak ditemukan"), 404
    if DeletionRequest.query.filter_by(member_nik=m.nik, status="PENDING").first():
        return jsonify(error="Sudah ada permohonan hapus yang menunggu persetujuan"), 409

    req = DeletionRequest(
        member_nik=m.nik, member_name=m.nama_lengkap, dpd=m.dpd, dpc=m.dpc, dprt=m.dprt,
        requested_by=claims.get("adminName"), reason=reason, status="PENDING",
    )
    db.session.add(req)
    db.session.add(AuditLog(
        member_nik=m.nik, member_name=m.nama_lengkap, action="DELETE_REQUEST",
        reason=reason, actor=claims.get("adminName"), dpd=m.dpd,
    ))
    db.session.commit()
    return jsonify(row_to_dict(req, DEL_FIELDS)), 201


@bp.post("/deletion-requests/<req_id>/review")
@super_admin_required
def review_request(req_id):
    claims = current_claims()
    data = request.get_json(silent=True) or {}
    decision = (data.get("decision") or "").upper()
    if decision not in ("APPROVED", "REJECTED"):
        return jsonify(error="decision harus APPROVED atau REJECTED"), 400
    req = db.session.get(DeletionRequest, req_id)
    if not req:
        return jsonify(error="Permohonan tidak ditemukan"), 404
    if req.status != "PENDING":
        return jsonify(error="Permohonan sudah diproses"), 409

    req.status = decision
    req.reviewed_by = claims.get("adminName")
    req.reviewed_at = datetime.now(timezone.utc)
    req.review_notes = (data.get("notes") or "").strip() or None

    if decision == "APPROVED":
        m = db.session.get(Member, req.member_nik)
        if m:
            db.session.delete(m)
    db.session.add(AuditLog(
        member_nik=req.member_nik, member_name=req.member_name,
        action="DELETE" if decision == "APPROVED" else "DELETE_REQUEST",
        reason=f"[{decision}] {req.review_notes or req.reason}",
        actor=claims.get("adminName"), dpd=req.dpd,
    ))
    db.session.commit()
    return jsonify(row_to_dict(req, DEL_FIELDS))
