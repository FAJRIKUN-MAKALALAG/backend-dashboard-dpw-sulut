"""Agregasi dashboard dihitung di database (GROUP BY), bukan di browser."""
from datetime import date, timedelta

from flask import Blueprint, jsonify, request
from sqlalchemy import case, func

from ..auth import current_claims, login_required
from ..cache import get_or_compute
from ..extensions import db
from ..models import Member
from ..utils import apply_scope, sanitize_date

bp = Blueprint("stats", __name__)


def _scoped(*cols):
    return apply_scope(db.session.query(*cols), current_claims())


@bp.get("/stats/summary")
@login_required
def summary():
    claims = current_claims()

    def _compute():
        by_status = dict(
            apply_scope(db.session.query(Member.status, func.count()), claims).group_by(Member.status).all()
        )
        total = sum(by_status.values())
        aktif = by_status.get("Aktif", 0)

        per_dpd = (
            apply_scope(
                db.session.query(
                    Member.dpd,
                    func.count().label("total"),
                    func.sum(case((Member.status == "Aktif", 1), else_=0)).label("aktif"),
                ),
                claims,
            )
            .group_by(Member.dpd)
            .order_by(Member.dpd)
            .all()
        )
        pengurus_cnt = apply_scope(db.session.query(func.count()), claims).filter(Member.is_pengurus.is_(True)).scalar()
        return {
            "total": total,
            "aktif": aktif,
            "nonAktif": total - aktif,
            "activeRate": round(aktif * 100 / total, 2) if total else 0,
            "byStatus": by_status,
            "pengurus": pengurus_cnt,
            "perDpd": [{"dpd": d, "total": t, "aktif": int(a or 0), "nonAktif": t - int(a or 0)} for d, t, a in per_dpd],
        }

    return jsonify(get_or_compute("stats", claims, "summary", ttl_seconds=10.0, compute_fn=_compute))


@bp.get("/stats/recruitment")
@login_required
def recruitment():
    """Laju rekrutmen: kartu hari ini/7/30 hari, tren harian, dan top kader perekrut."""
    claims = current_claims()
    today = date.today()
    days = request.args.get("days", 30, type=int)
    start = sanitize_date(request.args.get("from")) or (today - timedelta(days=days - 1))
    end = sanitize_date(request.args.get("to")) or today
    subkey = f"rec:{start.isoformat()}:{end.isoformat()}"

    def _compute():
        def count_since(d):
            return apply_scope(db.session.query(func.count()), claims).filter(Member.tanggal_bergabung >= d).scalar()

        daily = (
            apply_scope(db.session.query(Member.tanggal_bergabung, func.count()), claims)
            .filter(Member.tanggal_bergabung.between(start, end))
            .group_by(Member.tanggal_bergabung)
            .order_by(Member.tanggal_bergabung)
            .all()
        )
        top = (
            apply_scope(
                db.session.query(Member.kader_perekrut_nama, Member.kader_perekrut_jabatan, func.count().label("n")),
                claims,
            )
            .filter(Member.kader_perekrut_nama.isnot(None), Member.tanggal_bergabung.between(start, end))
            .group_by(Member.kader_perekrut_nama, Member.kader_perekrut_jabatan)
            .order_by(func.count().desc())
            .limit(20)
            .all()
        )
        return {
            "today": count_since(today),
            "last7": count_since(today - timedelta(days=6)),
            "last30": count_since(today - timedelta(days=29)),
            "daily": [{"date": d.isoformat(), "count": c} for d, c in daily],
            "topRecruiters": [{"nama": n, "jabatan": j, "count": c} for n, j, c in top],
        }

    return jsonify(get_or_compute("stats", claims, subkey, ttl_seconds=10.0, compute_fn=_compute))


@bp.get("/stats/territory")
@login_required
def territory():
    """Drilldown teritorial: tanpa param -> per DPD; ?dpd= -> per DPC; ?dpd=&dpc= -> per DPRt."""
    claims = current_claims()
    dpd, dpc = request.args.get("dpd"), request.args.get("dpc")
    start, end = sanitize_date(request.args.get("from")), sanitize_date(request.args.get("to"))
    subkey = f"terr:{dpd or ''}:{dpc or ''}:{start or ''}:{end or ''}"

    def _compute():
        if dpd and dpc:
            level, col = "dprt", Member.dprt
        elif dpd:
            level, col = "dpc", Member.dpc
        else:
            level, col = "dpd", Member.dpd

        q = apply_scope(
            db.session.query(col, func.count(), func.sum(case((Member.status == "Aktif", 1), else_=0))),
            claims,
        )
        if dpd:
            q = q.filter(Member.dpd == dpd)
        if dpc:
            q = q.filter(Member.dpc == dpc)
        if start:
            q = q.filter(Member.tanggal_bergabung >= start)
        if end:
            q = q.filter(Member.tanggal_bergabung <= end)
        rows = q.group_by(col).order_by(func.count().desc()).all()
        return {
            "level": level,
            "items": [{"name": n, "total": t, "aktif": int(a or 0), "nonAktif": t - int(a or 0)} for n, t, a in rows],
        }

    return jsonify(get_or_compute("stats", claims, subkey, ttl_seconds=10.0, compute_fn=_compute))


@bp.get("/stats/reconciliation")
@login_required
def reconciliation():
    """Rekonsiliasi NIK & e-KTA: NIK tidak valid, duplikat e-KTA, dan nomor non-standar."""
    claims = current_claims()

    def _compute():
        invalid_nik = apply_scope(db.session.query(func.count()), claims).filter(~Member.nik.op("~")(r"^\d{16}$")).scalar()
        non_standard = (
            apply_scope(db.session.query(func.count()), claims)
            .filter(
                (Member.nomor_ekta.is_(None))
                | ~(Member.nomor_ekta.like("ND-%") | Member.nomor_ekta.like("KT-%"))
            )
            .scalar()
        )
        dup = (
            apply_scope(db.session.query(Member.nomor_ekta, func.count().label("n")), claims)
            .filter(Member.nomor_ekta.isnot(None))
            .group_by(Member.nomor_ekta)
            .having(func.count() > 1)
            .limit(100)
            .all()
        )
        return {
            "invalidNik": invalid_nik,
            "nonStandardEkta": non_standard,
            "duplicateEkta": [{"nomorEkta": e, "count": n} for e, n in dup],
        }

    return jsonify(get_or_compute("stats", claims, "reconciliation", ttl_seconds=10.0, compute_fn=_compute))
