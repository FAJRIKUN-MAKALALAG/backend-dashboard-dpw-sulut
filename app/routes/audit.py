from flask import Blueprint, jsonify, request

from ..auth import current_claims, login_required
from ..extensions import db
from ..models import AuditLog
from ..utils import apply_scope, row_to_dict

bp = Blueprint("audit", __name__)

AUDIT_FIELDS = {
    "id": "id", "timestamp": "timestamp", "member_nik": "memberNik", "member_name": "memberName",
    "action": "action", "previous_status": "previousStatus", "new_status": "newStatus",
    "reason": "reason", "actor": "actor", "dpd": "dpd",
}


@bp.get("/audit-logs")
@login_required
def list_audit():
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(max(request.args.get("per_page", 50, type=int), 1), 200)
    q = apply_scope(AuditLog.query, current_claims(), AuditLog)
    # audit mencakup IMPORT_MASSAL tanpa dpd; hanya dibatasi untuk non-global
    if request.args.get("action"):
        q = q.filter(AuditLog.action == request.args["action"])
    if request.args.get("nik"):
        q = q.filter(AuditLog.member_nik == request.args["nik"])
    total = q.count()
    rows = q.order_by(AuditLog.timestamp.desc()).offset((page - 1) * per_page).limit(per_page).all()
    return jsonify(items=[row_to_dict(r, AUDIT_FIELDS) for r in rows], page=page, perPage=per_page, total=total)


@bp.delete("/audit-logs")
@login_required
def clear_audit():
    claims = current_claims()
    if claims.get("role") != "SUPER_ADMIN":
        return jsonify(error="Hanya Super Administrator yang berwenang menghapus seluruh riwayat audit."), 403

    deleted_count = db.session.query(AuditLog).delete()
    db.session.commit()
    return jsonify(
        message=f"Berhasil membersihkan seluruh riwayat audit ({deleted_count} log dihapus).",
        count=deleted_count
    )
