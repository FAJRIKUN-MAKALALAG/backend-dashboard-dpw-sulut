"""Perhitungan ringkasan saldo 3 pos, perbandingan tahunan, saldo awal periode, dan status autodebet."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models_finance import (
    FinanceDppRemittance,
    FinanceDuesPayment,
    FinanceLegislator,
    FinanceTariffRule,
    FinanceTransaction,
)
from app.services.finance.helpers import (
    HISTORICAL_EXCEL_END_MONTH,
    HISTORICAL_START_MONTH,
    current_year_month,
    is_real_dpw_expense,
    is_valid_ym,
)
from app.services.finance.matrix_service import compute_matrix
from app.services.finance.serializers import get_tariff_for_month, transaction_to_dict


def _account_balance(account_type: str) -> int:
    in_sum = (
        db.session.query(func.coalesce(func.sum(FinanceTransaction.amount), 0))
        .filter(FinanceTransaction.account_type == account_type, FinanceTransaction.direction == "IN")
        .scalar()
    )
    out_sum = (
        db.session.query(func.coalesce(func.sum(FinanceTransaction.amount), 0))
        .filter(FinanceTransaction.account_type == account_type, FinanceTransaction.direction == "OUT")
        .scalar()
    )
    return int(in_sum or 0) - int(out_sum or 0)


def get_account_balance_before(account_type: str, before_date: date) -> int:
    """Menghitung sisa saldo akun kas sebelum tanggal `before_date` untuk baris Sisa Saldo Periode Lalu."""
    in_sum = (
        db.session.query(func.coalesce(func.sum(FinanceTransaction.amount), 0))
        .filter(
            FinanceTransaction.account_type == account_type,
            FinanceTransaction.direction == "IN",
            FinanceTransaction.tx_date < before_date,
        )
        .scalar()
    )
    out_sum = (
        db.session.query(func.coalesce(func.sum(FinanceTransaction.amount), 0))
        .filter(
            FinanceTransaction.account_type == account_type,
            FinanceTransaction.direction == "OUT",
            FinanceTransaction.tx_date < before_date,
        )
        .scalar()
    )
    return int(in_sum or 0) - int(out_sum or 0)


def _resolve_year_period(year_filter: str, active_month: str) -> Tuple[str, str, str]:
    """Menentukan rentang bulan (from, to) dari filter tahun: ALL | 2026..."""
    max_end = max(HISTORICAL_EXCEL_END_MONTH, active_month)
    yf = str(year_filter or "ALL").strip().upper()
    if re.fullmatch(r"20\d{2}", yf) and int(yf) >= 2026:
        start = f"{yf}-01"
        end = min(f"{yf}-12", max_end) if start <= max_end else f"{yf}-12"
        return (yf, start, end)
    return ("ALL", HISTORICAL_START_MONTH, max_end)


def _load_tx_stats_by_month_and_year() -> Tuple[Dict[str, Dict[str, int]], Dict[str, Dict[str, int]]]:
    """Agregasi pemasukan & pengeluaran Rekening Bank, Kas Operasional, serta Pengeluaran Riil DPW per bulan dan per tahun."""
    rows = (
        db.session.query(
            func.extract("year", FinanceTransaction.tx_date),
            func.extract("month", FinanceTransaction.tx_date),
            FinanceTransaction.account_type,
            FinanceTransaction.direction,
            FinanceTransaction.category,
            FinanceTransaction.related_remittance_id,
            FinanceTransaction.related_batch_id,
            func.coalesce(func.sum(FinanceTransaction.amount), 0),
        )
        .filter(FinanceTransaction.tx_date.isnot(None))
        .group_by(
            func.extract("year", FinanceTransaction.tx_date),
            func.extract("month", FinanceTransaction.tx_date),
            FinanceTransaction.account_type,
            FinanceTransaction.direction,
            FinanceTransaction.category,
            FinanceTransaction.related_remittance_id,
            FinanceTransaction.related_batch_id,
        )
        .all()
    )

    blank = lambda: {
        "bankIn": 0,
        "bankOut": 0,
        "pettyIn": 0,
        "pettyOut": 0,
        "dpwExpense": 0,
        "dpwExpenseBank": 0,
        "dpwExpensePetty": 0,
        "dppRemittedOut": 0,
    }
    stats_by_month: Dict[str, Dict[str, int]] = defaultdict(blank)
    stats_by_year: Dict[str, Dict[str, int]] = defaultdict(blank)

    for raw_yr, raw_mo, acc, direction, category, rem_id, batch_id, total in rows:
        yr = f"{int(raw_yr):04d}"
        ym = f"{yr}-{int(raw_mo):02d}"
        amt = int(total or 0)
        key = None
        if acc == "BANK_DPW" and direction == "IN":
            key = "bankIn"
        elif acc == "BANK_DPW" and direction == "OUT":
            key = "bankOut"
        elif acc == "PETTY_CASH" and direction == "IN":
            key = "pettyIn"
        elif acc == "PETTY_CASH" and direction == "OUT":
            key = "pettyOut"
        if key:
            stats_by_month[ym][key] += amt
            stats_by_year[yr][key] += amt
            stats_by_year["ALL"][key] += amt

        if is_real_dpw_expense(direction, category, rem_id, batch_id):
            stats_by_month[ym]["dpwExpense"] += amt
            stats_by_year[yr]["dpwExpense"] += amt
            stats_by_year["ALL"]["dpwExpense"] += amt
            if acc == "BANK_DPW":
                stats_by_month[ym]["dpwExpenseBank"] += amt
                stats_by_year[yr]["dpwExpenseBank"] += amt
                stats_by_year["ALL"]["dpwExpenseBank"] += amt
            elif acc == "PETTY_CASH":
                stats_by_month[ym]["dpwExpensePetty"] += amt
                stats_by_year[yr]["dpwExpensePetty"] += amt
                stats_by_year["ALL"]["dpwExpensePetty"] += amt
        elif str(direction or "").upper() == "OUT" and (
            bool(rem_id) or str(category or "").strip() == "Setoran ke DPP"
        ):
            stats_by_month[ym]["dppRemittedOut"] += amt
            stats_by_year[yr]["dppRemittedOut"] += amt
            stats_by_year["ALL"]["dppRemittedOut"] += amt

    return stats_by_month, stats_by_year


def _build_period_metrics(
    yr_key: str,
    start_ym: str,
    end_ym: str,
    preloaded: Tuple[List[FinanceTariffRule], List[FinanceLegislator], List[FinanceDuesPayment]],
    tx_stats_by_year: Dict[str, Dict[str, int]],
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    dpw_matrix = compute_matrix("DPW", start_ym, end_ym, "ALL", preloaded=preloaded)
    dpp_matrix = compute_matrix("DPP", start_ym, end_ym, "ALL", preloaded=preloaded)
    dpw_gt = dpw_matrix["grandTotals"]
    dpp_gt = dpp_matrix["grandTotals"]
    tx_stat = tx_stats_by_year[yr_key]

    metrics = {
        "year": yr_key,
        "from": start_ym,
        "to": end_ym,
        "dpwBilled": dpw_gt["totalBilled"],
        "dpwPaid": dpw_gt["totalPaid"],
        "dpwPaidManual": dpw_gt["paidManual"],
        "dpwPaidAutodebet": dpw_gt["paidAutodebet"],
        "dpwDebt": dpw_gt["debt"],
        "dppBilled": dpp_gt["totalBilled"],
        "dppPaid": dpp_gt["totalPaid"],
        "dppPaidManual": dpp_gt["paidManual"],
        "dppPaidAutodebet": dpp_gt["paidAutodebet"],
        "dppDebt": dpp_gt["debt"],
        "totalPaid": dpw_gt["totalPaid"] + dpp_gt["totalPaid"],
        "totalDebt": dpw_gt["debt"] + dpp_gt["debt"],
        "bankIn": tx_stat["bankIn"],
        "bankOut": tx_stat["bankOut"],
        "pettyIn": tx_stat["pettyIn"],
        "pettyOut": tx_stat["pettyOut"],
        "dpwExpense": tx_stat["dpwExpense"],
        "dpwExpenseBank": tx_stat["dpwExpenseBank"],
        "dpwExpensePetty": tx_stat["dpwExpensePetty"],
        "dppRemittedOut": tx_stat["dppRemittedOut"],
        "netDpwSurplus": int(dpw_gt["totalPaid"] or 0) - int(tx_stat["dpwExpense"] or 0),
    }
    return metrics, dpw_matrix, dpp_matrix


def _build_autodebet_summary(
    active_month: str,
    rules: List[FinanceTariffRule],
    all_legs: List[FinanceLegislator],
    all_payments: List[FinanceDuesPayment],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    paid_leg_lists: Dict[str, List[FinanceDuesPayment]] = defaultdict(list)
    for p in all_payments:
        if p.period_month == active_month:
            paid_leg_lists[p.legislator_id].append(p)

    autodebet_groups_map: Dict[Tuple[str, str], List[FinanceLegislator]] = defaultdict(list)
    inactive_autodebet_regions: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for leg in all_legs:
        if leg.is_autodebet_active:
            autodebet_groups_map[(leg.tingkat, leg.daerah)].append(leg)
        elif leg.autodebet_histories or leg.autodebet_end_month:
            inactive_autodebet_regions[leg.daerah].append(
                {
                    "id": leg.id,
                    "namaLengkap": leg.nama_lengkap,
                    "startMonth": leg.autodebet_start_month,
                    "endMonth": leg.autodebet_end_month,
                }
            )

    autodebet_region_groups = []
    for (tingkat, daerah), members in autodebet_groups_map.items():
        dpw_t, dpp_t = get_tariff_for_month(rules, tingkat, daerah, active_month)
        member_items = []
        recorded_count = 0
        cancellable_count = 0
        cancellable_any_count = 0
        remitted_locked_count = 0

        for leg in members:
            leg_pays = paid_leg_lists.get(leg.id, [])
            paid_dpw = sum(int(p.amount_dpw or 0) for p in leg_pays)
            paid_dpp = sum(int(p.amount_dpp or 0) for p in leg_pays)
            already_paid = paid_dpw >= dpw_t and (dpp_t == 0 or paid_dpp >= dpp_t)
            if already_paid:
                recorded_count += 1

            cancellable_any_ids = [
                p.id for p in leg_pays if p.dpp_remittance_id is None and not p.is_historical
            ]
            cancellable_ad_ids = [
                p.id
                for p in leg_pays
                if p.payment_method == "AUTODEBET" and p.dpp_remittance_id is None and not p.is_historical
            ]
            has_remitted = any(p.dpp_remittance_id is not None for p in leg_pays)

            if cancellable_ad_ids:
                cancellable_count += 1
            if cancellable_any_ids:
                cancellable_any_count += 1
            if has_remitted:
                remitted_locked_count += 1

            member_items.append(
                {
                    "id": leg.id,
                    "namaLengkap": leg.nama_lengkap,
                    "amountDpw": dpw_t,
                    "amountDpp": dpp_t,
                    "alreadyPaid": already_paid,
                    "paidDpw": paid_dpw,
                    "paidDpp": paid_dpp,
                    "paymentIds": [p.id for p in leg_pays],
                    "cancellablePaymentIds": cancellable_any_ids,
                    "cancellableAutodebetIds": cancellable_ad_ids,
                    "hasRemittedDpp": has_remitted,
                }
            )

        autodebet_region_groups.append(
            {
                "tingkat": tingkat,
                "daerah": daerah,
                "periodMonth": active_month,
                "memberCount": len(members),
                "alreadyRecordedCount": recorded_count,
                "isFullyRecorded": recorded_count == len(members),
                "cancellableCount": cancellable_count,
                "cancellableAnyCount": cancellable_any_count,
                "remittedLockedCount": remitted_locked_count,
                "tariffDpwPerMember": dpw_t,
                "tariffDppPerMember": dpp_t,
                "totalDpw": dpw_t * len(members),
                "totalDpp": dpp_t * len(members),
                "grandTotal": (dpw_t + dpp_t) * len(members),
                "members": member_items,
            }
        )

    inactive_list = [{"daerah": k, "members": v} for k, v in inactive_autodebet_regions.items()]
    return autodebet_region_groups, inactive_list


def compute_dashboard_summary(ref_month: Optional[str] = None, year_filter: str = "ALL") -> Dict[str, Any]:
    """Menghitung ringkasan saldo 3 pos, perbandingan tahunan, tunggakan per daerah, & status autodebet."""
    ym_now = current_year_month()
    active_month = ref_month if (ref_month and is_valid_ym(ref_month)) else ym_now
    max_end = max(HISTORICAL_EXCEL_END_MONTH, active_month)
    resolved_year, sel_start, sel_end = _resolve_year_period(year_filter, active_month)

    bank_balance = _account_balance("BANK_DPW")
    petty_cash_balance = _account_balance("PETTY_CASH")
    total_remitted_dpp = int(
        db.session.query(func.coalesce(func.sum(FinanceDppRemittance.total_dpp_amount), 0)).scalar() or 0
    )

    # Single-pass preload agar summary, regionBreakdown, dan yearlyComparison cepat via SSH tunnel
    rules = FinanceTariffRule.query.all()
    all_legs = (
        FinanceLegislator.query.options(selectinload(FinanceLegislator.autodebet_histories))
        .filter(FinanceLegislator.is_active.is_(True))
        .order_by(
            FinanceLegislator.tingkat.desc(),
            FinanceLegislator.urutan.asc(),
            FinanceLegislator.nama_lengkap.asc(),
        )
        .all()
    )
    all_payments = FinanceDuesPayment.query.all()
    preloaded = (rules, all_legs, all_payments)
    tx_stats_by_month, tx_stats_by_year = _load_tx_stats_by_month_and_year()

    pending_dpp_rows = [
        p for p in all_payments if int(p.amount_dpp or 0) > 0 and p.dpp_remittance_id is None
    ]
    pending_dpp_total = sum(int(p.amount_dpp or 0) for p in pending_dpp_rows)
    net_dpw_bank = bank_balance - pending_dpp_total

    total_collected_dpw = sum(int(p.amount_dpw or 0) for p in all_payments)
    total_collected_dpp = sum(int(p.amount_dpp or 0) for p in all_payments)

    available_years = [str(y) for y in range(2026, max(2026, int(max_end[:4])) + 1)]
    year_cache: Dict[str, Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]] = {}
    yearly_comparison: List[Dict[str, Any]] = []
    for yr in available_years:
        _, y_start, y_end = _resolve_year_period(yr, active_month)
        bundle = _build_period_metrics(yr, y_start, y_end, preloaded, tx_stats_by_year)
        year_cache[yr] = bundle
        yearly_comparison.append(bundle[0])

    all_metrics, dpw_matrix_all, dpp_matrix_all = _build_period_metrics(
        "ALL", HISTORICAL_START_MONTH, max_end, preloaded, tx_stats_by_year
    )
    year_cache["ALL"] = (all_metrics, dpw_matrix_all, dpp_matrix_all)

    if resolved_year in year_cache:
        sel_metrics, sel_dpw_matrix, sel_dpp_matrix = year_cache[resolved_year]
    else:
        sel_metrics, sel_dpw_matrix, sel_dpp_matrix = _build_period_metrics(
            resolved_year, sel_start, sel_end, preloaded, tx_stats_by_year
        )

    dpp_by_region = {(g["tingkat"], g["daerah"]): g for g in sel_dpp_matrix["groups"]}
    region_breakdown = []
    for g_dpw in sel_dpw_matrix["groups"]:
        key = (g_dpw["tingkat"], g_dpw["daerah"])
        g_dpp = dpp_by_region.get(key)
        autodebet_active_cnt = sum(1 for l in g_dpw["legislators"] if l["isAutodebetActive"])

        dpw_billed = g_dpw["totals"]["totalBilled"]
        dpw_paid = g_dpw["totals"]["totalPaid"]
        dpp_billed = g_dpp["totals"]["totalBilled"] if g_dpp else 0
        dpp_paid = g_dpp["totals"]["totalPaid"] if g_dpp else 0
        total_billed_reg = dpw_billed + dpp_billed
        compliance_pct = (
            round(min(100.0, ((dpw_paid + dpp_paid) / total_billed_reg) * 100.0), 1)
            if total_billed_reg > 0
            else 100.0
        )

        region_breakdown.append(
            {
                "tingkat": g_dpw["tingkat"],
                "daerah": g_dpw["daerah"],
                "legislatorCount": len(g_dpw["legislators"]),
                "autodebetActiveCount": autodebet_active_cnt,
                "dpwBilled": dpw_billed,
                "dpwPaid": dpw_paid,
                "dpwPaidManual": g_dpw["totals"]["paidManual"],
                "dpwPaidAutodebet": g_dpw["totals"]["paidAutodebet"],
                "dpwDebt": g_dpw["totals"]["debt"],
                "dppBilled": dpp_billed,
                "dppPaid": dpp_paid,
                "dppPaidManual": g_dpp["totals"]["paidManual"] if g_dpp else 0,
                "dppPaidAutodebet": g_dpp["totals"]["paidAutodebet"] if g_dpp else 0,
                "dppDebt": g_dpp["totals"]["debt"] if g_dpp else 0,
                "compliancePct": compliance_pct,
            }
        )

    autodebet_region_groups, inactive_list = _build_autodebet_summary(
        active_month, rules, all_legs, all_payments
    )

    recent_txs = (
        FinanceTransaction.query.order_by(FinanceTransaction.tx_date.desc(), FinanceTransaction.created_at.desc())
        .limit(12)
        .all()
    )
    recent_ids = [t.id for t in recent_txs]
    locked_tx_ids = set()
    if recent_ids:
        linked = (
            db.session.query(FinanceDuesPayment.transaction_id)
            .filter(FinanceDuesPayment.transaction_id.in_(recent_ids))
            .distinct()
            .all()
        )
        locked_tx_ids = {row[0] for row in linked if row[0]}

    monthly_breakdown: List[Dict[str, Any]] = []
    sel_dpw_monthly = sel_dpw_matrix.get("monthlyTotals") or {}
    sel_dpp_monthly = sel_dpp_matrix.get("monthlyTotals") or {}
    for m_info in sel_dpw_matrix.get("months") or []:
        ym = m_info["ym"]
        dpw_m = sel_dpw_monthly.get(ym, {})
        dpp_m = sel_dpp_monthly.get(ym, {})
        tx_m = tx_stats_by_month[ym]
        dpw_billed = int(dpw_m.get("billed", 0))
        dpw_paid = int(dpw_m.get("paid", 0))
        dpp_billed = int(dpp_m.get("billed", 0))
        dpp_paid = int(dpp_m.get("paid", 0))
        monthly_breakdown.append(
            {
                "ym": ym,
                "label": m_info["label"],
                "dpwBilled": dpw_billed,
                "dpwPaid": dpw_paid,
                "dpwPaidManual": int(dpw_m.get("paidManual", 0)),
                "dpwPaidAutodebet": int(dpw_m.get("paidAutodebet", 0)),
                "dpwDebt": max(0, dpw_billed - dpw_paid),
                "dppBilled": dpp_billed,
                "dppPaid": dpp_paid,
                "dppPaidManual": int(dpp_m.get("paidManual", 0)),
                "dppPaidAutodebet": int(dpp_m.get("paidAutodebet", 0)),
                "dppDebt": max(0, dpp_billed - dpp_paid),
                "totalPaid": dpw_paid + dpp_paid,
                "bankIn": tx_m["bankIn"],
                "bankOut": tx_m["bankOut"],
                "pettyIn": tx_m["pettyIn"],
                "pettyOut": tx_m["pettyOut"],
                "dpwExpense": tx_m["dpwExpense"],
                "dpwExpenseBank": tx_m["dpwExpenseBank"],
                "dpwExpensePetty": tx_m["dpwExpensePetty"],
                "dppRemittedOut": tx_m["dppRemittedOut"],
                "netDpwSurplus": dpw_paid - tx_m["dpwExpense"],
            }
        )

    return {
        "refMonth": active_month,
        "yearFilter": resolved_year,
        "availableYears": available_years,
        "debtPeriod": {"from": sel_start, "to": sel_end},
        "periodMetrics": sel_metrics,
        "yearlyComparison": yearly_comparison,
        "monthlyBreakdown": monthly_breakdown,
        "balances": {
            "bankBalance": bank_balance,
            "pendingDppTotal": pending_dpp_total,
            "pendingDppCount": len(pending_dpp_rows),
            "netDpwBankBalance": net_dpw_bank,
            "pettyCashBalance": petty_cash_balance,
            "totalCollectedDpw": total_collected_dpw,
            "totalCollectedDpp": total_collected_dpp,
            "totalRemittedDpp": total_remitted_dpp,
            "totalDpwDebt": dpw_matrix_all["grandTotals"]["debt"],
            "totalDppDebt": dpp_matrix_all["grandTotals"]["debt"],
            "legislatorCount": len(all_legs),
        },
        "regionBreakdown": region_breakdown,
        "autodebetRegionGroups": autodebet_region_groups,
        "autodebetInactiveRegions": inactive_list,
        "recentTransactions": [transaction_to_dict(t, locked_tx_ids=locked_tx_ids) for t in recent_txs],
    }
