"""Route endpoints setoran iuran fraksi (manual, autodebet kolektif, rincian & pembatalan)."""

from __future__ import annotations

from flask import jsonify, request
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app.auth import current_claims, finance_access_required
from app.models_finance import FinanceDuesPayment, FinanceLegislator
from app.routes.finance import bp
from app.services.finance import (
    cancel_autodebet_payments_tx,
    create_autodebet_batch_tx,
    create_manual_payment_tx,
    delete_single_payment_tx,
    is_valid_ym,
    payment_to_dict,
)


@bp.get("/finance/payments")
@finance_access_required
def list_payments():
    legislator_id = (request.args.get("legislatorId") or "").strip()
    period_month = (request.args.get("periodMonth") or "").strip()
    daerah = (request.args.get("daerah") or "").strip()
    method = (request.args.get("method") or "").strip().upper()
    limit = min(max(int(request.args.get("limit") or 100), 1), 500)

    q = FinanceDuesPayment.query.options(selectinload(FinanceDuesPayment.legislator))
    if legislator_id:
        q = q.filter(FinanceDuesPayment.legislator_id == legislator_id)
    if is_valid_ym(period_month):
        q = q.filter(FinanceDuesPayment.period_month == period_month)
    if daerah and daerah.upper() != "ALL":
        q = q.join(
            FinanceLegislator, FinanceDuesPayment.legislator_id == FinanceLegislator.id
        ).filter(func.lower(FinanceLegislator.daerah) == daerah.lower())
    if method in ("MANUAL", "AUTODEBET"):
        q = q.filter(FinanceDuesPayment.payment_method == method)

    rows = q.order_by(
        FinanceDuesPayment.payment_date.desc(),
        FinanceDuesPayment.created_at.desc(),
    ).limit(limit).all()

    return jsonify(items=[payment_to_dict(p) for p in rows])


@bp.post("/finance/payments/manual")
@finance_access_required
def record_manual_payment():
    claims = current_claims()
    actor = claims.get("adminName") or "Wakil Bendahara"
    data = request.get_json(silent=True) or {}
    return jsonify(create_manual_payment_tx(data, actor)), 201


@bp.post("/finance/payments/autodebet")
@finance_access_required
def record_autodebet_batch():
    claims = current_claims()
    actor = claims.get("adminName") or "Wakil Bendahara"
    data = request.get_json(silent=True) or {}
    return jsonify(create_autodebet_batch_tx(data, actor)), 201


@bp.post("/finance/payments/autodebet/cancel")
@finance_access_required
def cancel_autodebet_batch():
    claims = current_claims()
    actor = claims.get("adminName") or "Wakil Bendahara"
    data = request.get_json(silent=True) or {}
    result = cancel_autodebet_payments_tx(
        period_month=str(data.get("periodMonth") or ""),
        daerah=str(data.get("daerah") or ""),
        legislator_id=str(data.get("legislatorId") or ""),
        only_autodebet=bool(data.get("onlyAutodebet", True)),
        actor=actor,
    )
    return jsonify(result), 200


@bp.delete("/finance/payments/<payment_id>")
@finance_access_required
def delete_payment(payment_id: str):
    claims = current_claims()
    actor = claims.get("adminName") or "Wakil Bendahara"
    return jsonify(delete_single_payment_tx(payment_id, actor)), 200
