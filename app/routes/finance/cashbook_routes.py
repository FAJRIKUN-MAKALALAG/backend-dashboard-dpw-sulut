"""Route endpoints buku kas Bank DPW, Kas Operasional Kantor, penarikan dana, dan saldo awal."""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from datetime import date
from typing import Any, Dict, Optional

from flask import jsonify, request
from sqlalchemy import func, not_, or_

from app.auth import current_claims, finance_access_required
from app.cache import invalidate_all_data_cache
from app.extensions import db
from app.models_finance import FinanceDuesPayment, FinanceTransaction
from app.routes.finance import bp
from app.services.finance import (
    NON_DPW_EXPENSE_CATEGORIES,
    get_account_balance_before,
    is_real_dpw_expense,
    is_valid_ym,
    log_finance_audit,
    parse_date_safe,
    process_proof_image,
    transaction_to_dict,
)


@bp.get("/finance/transactions")
@finance_access_required
def list_transactions():
    account = (request.args.get("account") or "ALL").strip().upper()
    month = (request.args.get("month") or "").strip()
    year = (request.args.get("year") or "").strip().upper()
    category = (request.args.get("category") or "ALL").strip()
    direction = (request.args.get("direction") or "ALL").strip().upper()
    expense_only = (request.args.get("expenseOnly") or "").strip().lower() in ("1", "true", "yes")
    search = (request.args.get("search") or "").strip()
    sort_dir = (request.args.get("sort") or "desc").strip().lower()
    has_year_only = (not is_valid_ym(month)) and bool(re.fullmatch(r"20\d{2}", year))
    default_limit = 800 if has_year_only else 400
    if expense_only:
        default_limit = 1500
    limit = min(max(int(request.args.get("limit") or default_limit), 1), 1500)

    q = FinanceTransaction.query
    if account in ("BANK_DPW", "PETTY_CASH"):
        q = q.filter(FinanceTransaction.account_type == account)

    opening_balance: Optional[int] = None
    if is_valid_ym(month):
        y, m = map(int, month.split("-"))
        start_date = date(y, m, 1)
        end_m = m + 1 if m < 12 else 1
        end_y = y if m < 12 else y + 1
        end_date = date(end_y, end_m, 1)
        q = q.filter(FinanceTransaction.tx_date >= start_date, FinanceTransaction.tx_date < end_date)
        if not expense_only and account in ("BANK_DPW", "PETTY_CASH"):
            opening_balance = get_account_balance_before(account, start_date)
    elif has_year_only:
        y = int(year)
        start_date = date(y, 1, 1)
        end_date = date(y + 1, 1, 1)
        q = q.filter(FinanceTransaction.tx_date >= start_date, FinanceTransaction.tx_date < end_date)
        if not expense_only and account in ("BANK_DPW", "PETTY_CASH"):
            opening_balance = get_account_balance_before(account, start_date)
    elif not expense_only and account in ("BANK_DPW", "PETTY_CASH"):
        opening_balance = 0

    if expense_only:
        q = q.filter(
            FinanceTransaction.direction == "OUT",
            or_(
                FinanceTransaction.related_remittance_id.is_(None),
                FinanceTransaction.related_remittance_id == "",
            ),
            or_(
                FinanceTransaction.related_batch_id.is_(None),
                not_(FinanceTransaction.related_batch_id.startswith("XFER-")),
            ),
            or_(
                FinanceTransaction.category.is_(None),
                not_(FinanceTransaction.category.in_(NON_DPW_EXPENSE_CATEGORIES)),
            ),
        )
    elif direction in ("IN", "OUT"):
        q = q.filter(FinanceTransaction.direction == direction)

    if category and category != "ALL":
        q = q.filter(func.lower(FinanceTransaction.category) == category.lower())
    if search:
        like_pat = f"%{search.lower()}%"
        q = q.filter(
            or_(
                func.lower(func.coalesce(FinanceTransaction.description, "")).like(like_pat),
                func.lower(func.coalesce(FinanceTransaction.category, "")).like(like_pat),
            )
        )

    if sort_dir == "asc":
        q = q.order_by(FinanceTransaction.tx_date.asc(), FinanceTransaction.created_at.asc())
    else:
        q = q.order_by(FinanceTransaction.tx_date.desc(), FinanceTransaction.created_at.desc())

    rows = q.limit(limit).all()

    row_ids = [t.id for t in rows]
    locked_tx_ids = set()
    if row_ids:
        linked = (
            db.session.query(FinanceDuesPayment.transaction_id)
            .filter(FinanceDuesPayment.transaction_id.in_(row_ids))
            .distinct()
            .all()
        )
        locked_tx_ids = {row[0] for row in linked if row[0]}

    total_in = sum(int(t.amount or 0) for t in rows if t.direction == "IN")
    total_out = sum(int(t.amount or 0) for t in rows if t.direction == "OUT")
    net_delta = total_in - total_out
    closing_balance = (opening_balance + net_delta) if opening_balance is not None else None

    cat_buckets: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"category": "", "total": 0, "bankTotal": 0, "pettyTotal": 0, "count": 0}
    )
    month_buckets: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"ym": "", "total": 0, "bankTotal": 0, "pettyTotal": 0, "count": 0}
    )
    total_expense = 0
    bank_expense = 0
    petty_expense = 0
    expense_count = 0

    for t in rows:
        if is_real_dpw_expense(t.direction, t.category, t.related_remittance_id, t.related_batch_id):
            amt = int(t.amount or 0)
            cat_name = (t.category or "Operasional Kantor").strip() or "Operasional Kantor"
            ym_str = t.tx_date.strftime("%Y-%m") if t.tx_date else ""
            total_expense += amt
            expense_count += 1
            if t.account_type == "BANK_DPW":
                bank_expense += amt
            elif t.account_type == "PETTY_CASH":
                petty_expense += amt

            c_item = cat_buckets[cat_name]
            c_item["category"] = cat_name
            c_item["total"] += amt
            c_item["count"] += 1
            if t.account_type == "BANK_DPW":
                c_item["bankTotal"] += amt
            elif t.account_type == "PETTY_CASH":
                c_item["pettyTotal"] += amt

            if ym_str:
                m_item = month_buckets[ym_str]
                m_item["ym"] = ym_str
                m_item["total"] += amt
                m_item["count"] += 1
                if t.account_type == "BANK_DPW":
                    m_item["bankTotal"] += amt
                elif t.account_type == "PETTY_CASH":
                    m_item["pettyTotal"] += amt

    by_category = sorted(cat_buckets.values(), key=lambda x: (-x["total"], x["category"]))
    by_month = sorted(month_buckets.values(), key=lambda x: x["ym"], reverse=(sort_dir != "asc"))

    return jsonify(
        items=[transaction_to_dict(t, locked_tx_ids=locked_tx_ids) for t in rows],
        openingBalance=opening_balance,
        closingBalance=closing_balance,
        summary={"totalIn": total_in, "totalOut": total_out, "netDelta": net_delta},
        byCategory=by_category,
        byMonth=by_month,
        expenseTotals={
            "totalExpense": total_expense,
            "bankExpense": bank_expense,
            "pettyExpense": petty_expense,
            "count": expense_count,
        },
    )


@bp.post("/finance/transactions")
@finance_access_required
def create_transaction():
    """Catat pemasukan lain-lain atau pengeluaran operasional kantor + foto nota."""
    claims = current_claims()
    actor = claims.get("adminName") or "Wakil Bendahara"
    data = request.get_json(silent=True) or {}

    account_type = str(data.get("accountType") or "PETTY_CASH").strip().upper()
    direction = str(data.get("direction") or "OUT").strip().upper()
    category = str(data.get("category") or "Operasional Kantor").strip()
    description = str(data.get("description") or "").strip()
    amount = int(data.get("amount") or 0)

    if account_type not in ("BANK_DPW", "PETTY_CASH"):
        return jsonify(error="Akun kas harus BANK_DPW atau PETTY_CASH."), 400
    if direction not in ("IN", "OUT"):
        return jsonify(error="Arah transaksi harus IN atau OUT."), 400
    if not description or amount <= 0:
        return jsonify(error="Uraian transaksi dan nominal (> 0) wajib diisi."), 400

    tx_id = f"TX-{uuid.uuid4().hex[:12].upper()}"
    proof_url = process_proof_image(data.get("proofUrl"), f"tx_{tx_id}")

    tx = FinanceTransaction(
        id=tx_id,
        tx_date=parse_date_safe(data.get("txDate")),
        account_type=account_type,
        direction=direction,
        category=category,
        description=description,
        amount=amount,
        proof_url=proof_url or None,
        is_historical=False,
        created_by=actor,
    )
    db.session.add(tx)
    log_finance_audit("CREATE", f"Kas {account_type}", f"{direction} {category}: {description} (Rp{amount:,})", actor)
    db.session.commit()
    invalidate_all_data_cache()
    return jsonify(transaction_to_dict(tx)), 201


@bp.post("/finance/transactions/transfer-to-petty")
@finance_access_required
def transfer_bank_to_petty_cash():
    """Penarikan / pemindahan dana dari Rekening Bank DPW ke Kas Operasional Kantor (1 aksi atomik)."""
    claims = current_claims()
    actor = claims.get("adminName") or "Wakil Bendahara"
    data = request.get_json(silent=True) or {}

    amount = int(data.get("amount") or 0)
    if amount <= 0:
        return jsonify(error="Nominal penarikan dana wajib lebih besar dari Rp0."), 400

    tx_date = parse_date_safe(data.get("txDate"))
    description = str(data.get("description") or "Penarikan Dana Operasional Kantor DPW").strip()
    batch_id = f"XFER-{uuid.uuid4().hex[:10].upper()}"
    proof_url = process_proof_image(data.get("proofUrl"), f"xfer_{batch_id}")

    out_tx = FinanceTransaction(
        id=f"TX-{uuid.uuid4().hex[:12].upper()}",
        tx_date=tx_date,
        account_type="BANK_DPW",
        direction="OUT",
        category="Penarikan ke Kas Operasional",
        description=description,
        amount=amount,
        proof_url=proof_url or None,
        related_batch_id=batch_id,
        is_historical=False,
        created_by=actor,
    )
    in_tx = FinanceTransaction(
        id=f"TX-{uuid.uuid4().hex[:12].upper()}",
        tx_date=tx_date,
        account_type="PETTY_CASH",
        direction="IN",
        category="Penerimaan dari Rekening DPW",
        description=description,
        amount=amount,
        proof_url=proof_url or None,
        related_batch_id=batch_id,
        is_historical=False,
        created_by=actor,
    )
    db.session.add(out_tx)
    db.session.add(in_tx)
    log_finance_audit("CREATE", "Tarik Dana Operasional", f"{description}: Rp{amount:,}", actor)
    db.session.commit()
    invalidate_all_data_cache()

    return jsonify(
        ok=True,
        outTransaction=transaction_to_dict(out_tx),
        inTransaction=transaction_to_dict(in_tx),
    ), 201


@bp.delete("/finance/transactions/<tx_id>")
@finance_access_required
def delete_transaction(tx_id: str):
    claims = current_claims()
    actor = claims.get("adminName") or "Wakil Bendahara"
    tx = db.session.get(FinanceTransaction, tx_id)
    if not tx:
        return jsonify(error="Transaksi kas tidak ditemukan."), 404

    if tx.related_remittance_id:
        return jsonify(error="Transaksi terkait pengiriman ke DPP tidak dapat dihapus langsung."), 409
    if FinanceDuesPayment.query.filter_by(transaction_id=tx_id).count() > 0:
        return jsonify(error="Transaksi terkait setoran iuran fraksi; batalkan lewat matriks iuran atau autodebet."), 409

    if tx.related_batch_id and tx.related_batch_id.startswith("XFER-"):
        paired = FinanceTransaction.query.filter_by(related_batch_id=tx.related_batch_id).all()
        for row in paired:
            db.session.delete(row)
    else:
        db.session.delete(tx)

    log_finance_audit("DELETE", tx.category, f"Hapus transaksi: {tx.description}", actor)
    db.session.commit()
    invalidate_all_data_cache()
    return jsonify(ok=True)
