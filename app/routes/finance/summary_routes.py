"""Route endpoints ringkasan dashboard dan matriks iuran DPW/DPP."""

from __future__ import annotations

from flask import jsonify, request

from app.auth import finance_access_required
from app.routes.finance import bp
from app.services.finance import (
    HISTORICAL_START_MONTH,
    compute_dashboard_summary,
    compute_matrix,
    current_year_month,
    is_valid_ym,
)


@bp.get("/finance/summary")
@finance_access_required
def get_finance_summary():
    ref_month = (request.args.get("month") or "").strip() or None
    year = (request.args.get("year") or "ALL").strip().upper()
    return jsonify(compute_dashboard_summary(ref_month=ref_month, year_filter=year))


@bp.get("/finance/matrix")
@finance_access_required
def get_finance_matrix():
    kind = request.args.get("type", "DPW").upper()
    start_ym = (request.args.get("from") or HISTORICAL_START_MONTH).strip()
    end_ym = (request.args.get("to") or max("2026-12", current_year_month())).strip()
    daerah = (request.args.get("daerah") or "ALL").strip()
    tingkat = (request.args.get("tingkat") or "ALL").strip().upper()

    if not is_valid_ym(start_ym) or not is_valid_ym(end_ym):
        return jsonify(error="Parameter bulan harus berformat YYYY-MM."), 400

    return jsonify(
        compute_matrix(
            kind=kind,
            start_ym=start_ym,
            end_ym=end_ym,
            daerah_filter=daerah,
            tingkat_filter=tingkat,
        )
    )
