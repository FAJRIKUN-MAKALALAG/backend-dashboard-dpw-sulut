from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

from app.auth import current_claims, login_required
from app.cache import get_or_compute, invalidate_all_data_cache
from app.extensions import db
from app.models import AuditLog
from app.utils import apply_scope, row_to_dict

bp = Blueprint("audit", __name__)

AUDIT_FIELDS = {
    "id": "id", "timestamp": "timestamp", "member_nik": "memberNik", "member_name": "memberName",
    "action": "action", "previous_status": "previousStatus", "new_status": "newStatus",
    "reason": "reason", "actor": "actor", "dpd": "dpd",
}


def _parse_iso_since(raw: str | None) -> datetime | None:
    if not raw:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Buffer 2 detik untuk memastikan transaksi paralel tidak terlewat di batas milidetik
        return dt - timedelta(seconds=2)
    except ValueError:
        return None


@bp.get("/audit-logs")
@login_required
def list_audit():
    claims = current_claims()
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(max(request.args.get("per_page", 50, type=int), 1), 200)
    action_filter = request.args.get("action") or ""
    nik_filter = request.args.get("nik") or ""
    since_raw = request.args.get("since") or ""
    skip_count = request.args.get("skip_count", "").lower() == "true"

    cache_subkey = f"p={page}&pp={per_page}&act={action_filter}&nik={nik_filter}&since={since_raw}&sc={skip_count}"

    def _compute():
        q = apply_scope(AuditLog.query, claims, AuditLog)
        if action_filter:
            q = q.filter(AuditLog.action == action_filter)
        if nik_filter:
            q = q.filter(AuditLog.member_nik == nik_filter)

        since_dt = _parse_iso_since(since_raw)
        if since_dt is not None:
            q = q.filter(AuditLog.timestamp >= since_dt)

        rows = q.order_by(AuditLog.timestamp.desc()).offset((page - 1) * per_page).limit(per_page).all()
        items = [row_to_dict(r, AUDIT_FIELDS) for r in rows]
        total = len(items) if skip_count else q.count()
        return {
            "items": items,
            "page": page,
            "perPage": per_page,
            "total": total,
            "serverTime": datetime.now(timezone.utc).isoformat(),
        }

    payload = get_or_compute("audit", claims, cache_subkey, ttl_seconds=3.0, compute_fn=_compute)
    return jsonify(payload)


@bp.delete("/audit-logs")
@login_required
def clear_audit():
    claims = current_claims()
    if claims.get("role") != "SUPER_ADMIN":
        return jsonify(error="Hanya Super Administrator yang berwenang menghapus seluruh riwayat audit."), 403

    deleted_count = db.session.query(AuditLog).delete()
    db.session.commit()
    invalidate_all_data_cache()
    return jsonify(
        message=f"Berhasil membersihkan seluruh riwayat audit ({deleted_count} log dihapus).",
        count=deleted_count
    )
