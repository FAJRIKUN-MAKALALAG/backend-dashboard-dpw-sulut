"""Perhitungan matriks iuran DPW dan DPP per daerah, anggota, dan bulan."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app.models_finance import FinanceDuesPayment, FinanceLegislator, FinanceTariffRule
from app.services.finance.helpers import format_month_label, list_months_range
from app.services.finance.serializers import autodebet_history_to_dict, get_tariff_for_month


def compute_matrix(
    kind: str,
    start_ym: str,
    end_ym: str,
    daerah_filter: str = "ALL",
    tingkat_filter: str = "ALL",
    preloaded: Optional[Tuple[List[FinanceTariffRule], List[FinanceLegislator], List[FinanceDuesPayment]]] = None,
) -> Dict[str, Any]:
    """Menyusun matriks iuran DPW atau DPP lengkap per daerah, per anggota, per bulan."""
    norm_kind = "DPP" if str(kind).upper() == "DPP" else "DPW"
    norm_tingkat = str(tingkat_filter or "ALL").strip().upper()
    months = list_months_range(start_ym, end_ym)

    if preloaded is not None:
        rules, base_legs, raw_payments = preloaded
        legislators = [
            leg
            for leg in base_legs
            if leg.is_active
            and (norm_tingkat not in ("PROVINSI", "KAB_KOTA") or leg.tingkat == norm_tingkat)
            and (
                not daerah_filter
                or daerah_filter == "ALL"
                or leg.daerah.strip().lower() == daerah_filter.strip().lower()
            )
        ]
        if months:
            m_start, m_end = months[0], months[-1]
            all_payments = [p for p in raw_payments if m_start <= p.period_month <= m_end]
        else:
            all_payments = list(raw_payments)
    else:
        rules = FinanceTariffRule.query.all()
        leg_query = FinanceLegislator.query.options(
            selectinload(FinanceLegislator.autodebet_histories)
        ).filter(FinanceLegislator.is_active.is_(True))

        if norm_tingkat in ("PROVINSI", "KAB_KOTA"):
            leg_query = leg_query.filter(FinanceLegislator.tingkat == norm_tingkat)
        if daerah_filter and daerah_filter != "ALL":
            leg_query = leg_query.filter(func.lower(FinanceLegislator.daerah) == daerah_filter.strip().lower())

        legislators = leg_query.order_by(
            FinanceLegislator.tingkat.desc(),
            FinanceLegislator.urutan.asc(),
            FinanceLegislator.nama_lengkap.asc(),
        ).all()

        payments_q = FinanceDuesPayment.query
        if months:
            payments_q = payments_q.filter(
                FinanceDuesPayment.period_month >= months[0],
                FinanceDuesPayment.period_month <= months[-1],
            )
        all_payments = payments_q.all()

    pay_map: Dict[Tuple[str, str], List[FinanceDuesPayment]] = defaultdict(list)
    for p in all_payments:
        pay_map[(p.legislator_id, p.period_month)].append(p)

    # Pertahankan urutan kemunculan daerah (Provinsi di atas, lalu Kab/Kota)
    region_order: List[Tuple[str, str]] = []
    grouped_legs: Dict[Tuple[str, str], List[FinanceLegislator]] = defaultdict(list)
    for leg in legislators:
        key = (leg.tingkat, leg.daerah)
        if key not in grouped_legs:
            region_order.append(key)
        grouped_legs[key].append(leg)

    monthly_totals: Dict[str, Dict[str, int]] = {
        m: {"billed": 0, "paid": 0, "paidManual": 0, "paidAutodebet": 0} for m in months
    }
    groups = []
    grand_billed = 0
    grand_paid = 0
    grand_manual = 0
    grand_auto = 0
    grand_paid_count = 0
    grand_debt_count = 0

    for tingkat, daerah in region_order:
        monthly_tariffs: Dict[str, int] = {}
        dpw_tariffs: Dict[str, int] = {}
        dpp_tariffs: Dict[str, int] = {}
        region_billed_per_leg = 0

        for m in months:
            dpw_t, dpp_t = get_tariff_for_month(rules, tingkat, daerah, m)
            dpw_tariffs[m] = dpw_t
            dpp_tariffs[m] = dpp_t
            t_val = dpp_t if norm_kind == "DPP" else dpw_t
            monthly_tariffs[m] = t_val
            region_billed_per_leg += t_val

        leg_rows = []
        group_billed = 0
        group_paid = 0
        group_manual = 0
        group_auto = 0
        group_monthly_paid: Dict[str, int] = {m: 0 for m in months}

        for leg in grouped_legs[(tingkat, daerah)]:
            cells: Dict[str, Dict[str, Any]] = {}
            leg_paid_total = 0
            leg_manual_total = 0
            leg_auto_total = 0

            for m in months:
                target = monthly_tariffs[m]
                plist = pay_map.get((leg.id, m), [])
                paid_dpw = 0
                paid_dpp = 0
                cell_manual = 0
                cell_auto = 0

                for p in plist:
                    p_dpw = int(p.amount_dpw or 0)
                    p_dpp = int(p.amount_dpp or 0)
                    paid_dpw += p_dpw
                    paid_dpp += p_dpp
                    kind_amt = p_dpp if norm_kind == "DPP" else p_dpw
                    if p.payment_method == "AUTODEBET":
                        cell_auto += kind_amt
                    else:
                        cell_manual += kind_amt

                paid_val = paid_dpp if norm_kind == "DPP" else paid_dpw
                has_autodebet = cell_auto > 0
                dpp_entries = [p for p in plist if int(p.amount_dpp or 0) > 0]
                remitted_all = bool(dpp_entries) and all(p.dpp_remittance_id is not None for p in dpp_entries)

                if paid_val <= 0:
                    status = "UNPAID"
                elif target == 0 or paid_val >= target:
                    status = "PAID"
                else:
                    status = "PARTIAL"

                leg_paid_total += paid_val
                leg_manual_total += cell_manual
                leg_auto_total += cell_auto
                group_monthly_paid[m] += paid_val

                monthly_totals[m]["billed"] += target
                monthly_totals[m]["paid"] += paid_val
                monthly_totals[m]["paidManual"] += cell_manual
                monthly_totals[m]["paidAutodebet"] += cell_auto

                cells[m] = {
                    "amount": paid_val,
                    "target": target,
                    "amountDpw": paid_dpw,
                    "amountDpp": paid_dpp,
                    "targetDpw": dpw_tariffs[m],
                    "targetDpp": dpp_tariffs[m],
                    "method": "AUTODEBET" if has_autodebet else ("MANUAL" if paid_val > 0 else None),
                    "status": status,
                    "remitted": remitted_all,
                    "hasRemittedDpp": any(p.dpp_remittance_id is not None for p in plist),
                    "isHistorical": bool(plist) and all(p.is_historical for p in plist),
                    "paymentIds": [
                        p.id
                        for p in plist
                        if (int(p.amount_dpp or 0) if norm_kind == "DPP" else int(p.amount_dpw or 0)) > 0
                    ],
                    "allPaymentIds": [p.id for p in plist],
                }

            balance_delta = region_billed_per_leg - leg_paid_total
            debt_val = max(0, balance_delta)
            if debt_val == 0:
                grand_paid_count += 1
            else:
                grand_debt_count += 1

            group_billed += region_billed_per_leg
            group_paid += leg_paid_total
            group_manual += leg_manual_total
            group_auto += leg_auto_total

            leg_rows.append(
                {
                    "id": leg.id,
                    "namaLengkap": leg.nama_lengkap,
                    "tingkat": leg.tingkat,
                    "daerah": leg.daerah,
                    "isAutodebetActive": bool(leg.is_autodebet_active),
                    "autodebetStartMonth": leg.autodebet_start_month,
                    "autodebetEndMonth": leg.autodebet_end_month,
                    "autodebetHistories": [autodebet_history_to_dict(h) for h in leg.autodebet_histories],
                    "totalBilled": region_billed_per_leg,
                    "totalPaid": leg_paid_total,
                    "paidManual": leg_manual_total,
                    "paidAutodebet": leg_auto_total,
                    "debt": debt_val,
                    "overpaid": max(0, -balance_delta),
                    "balanceDelta": balance_delta,
                    "cells": cells,
                }
            )

        grand_billed += group_billed
        grand_paid += group_paid
        grand_manual += group_manual
        grand_auto += group_auto
        group_delta = group_billed - group_paid

        groups.append(
            {
                "tingkat": tingkat,
                "daerah": daerah,
                "monthlyTariffs": monthly_tariffs,
                "legislators": leg_rows,
                "totals": {
                    "totalBilled": group_billed,
                    "totalPaid": group_paid,
                    "paidManual": group_manual,
                    "paidAutodebet": group_auto,
                    "debt": max(0, group_delta),
                    "balanceDelta": group_delta,
                    "monthlyPaid": group_monthly_paid,
                },
            }
        )

    grand_delta = grand_billed - grand_paid
    return {
        "kind": norm_kind,
        "startMonth": months[0] if months else start_ym,
        "endMonth": months[-1] if months else end_ym,
        "months": [{"ym": m, "label": format_month_label(m)} for m in months],
        "monthlyTotals": monthly_totals,
        "groups": groups,
        "grandTotals": {
            "legislatorCount": len(legislators),
            "totalBilled": grand_billed,
            "totalPaid": grand_paid,
            "paidManual": grand_manual,
            "paidAutodebet": grand_auto,
            "paidCount": grand_paid_count,
            "debtCount": grand_debt_count,
            "debt": max(0, grand_delta),
            "balanceDelta": grand_delta,
        },
    }
