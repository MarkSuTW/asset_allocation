"""
Loan health calculations and due date helpers.
"""
import calendar
import sqlite3
from datetime import date, datetime
from typing import Any, Dict, Optional

from app.core.utils import normalize_stock_id
from app.services.portfolio import _resolve_collateral_value


def _default_due_date(start_iso: str) -> str:
    try:
        d = datetime.fromisoformat(start_iso).date()
        year = d.year + ((d.month + 17) // 12)
        month = ((d.month + 17) % 12) + 1
        last_day = calendar.monthrange(year, month)[1]
        day = min(d.day, last_day)
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def loans_health_data(conn: sqlite3.Connection) -> Dict[str, Any]:
    rows = conn.execute("SELECT lender, collateral, collateral_lots, principal, interest_rate, start_date, due_date FROM loans").fetchall()

    total_principal = 0.0
    total_collateral = 0.0
    total_interest = 0.0
    loan_items = []
    today = date.today()

    for row in rows:
        principal = float(row["principal"])
        total_principal += principal

        collateral_sid = normalize_stock_id(str(row["collateral"]).strip())
        collateral_lots = float(row["collateral_lots"] or 0)
        _, collateral_value = _resolve_collateral_value(conn, collateral_sid, collateral_lots)

        total_collateral += collateral_value

        start_date = row["start_date"]
        try:
            d0 = datetime.fromisoformat(start_date).date()
        except ValueError:
            d0 = today

        days = max((today - d0).days, 0)
        rate = float(row["interest_rate"])
        if rate > 1:
            rate = rate / 100.0
        interest = (principal * rate / 365.0) * days
        total_interest += interest
        due_date = str(row["due_date"] or "").strip()
        loan_items.append(
            {
                "lender": row["lender"],
                "collateral": row["collateral"],
                "principal": round(principal, 2),
                "interest_rate": round(rate, 6),
                "start_date": start_date,
                "due_date": due_date,
                "elapsed_days": days,
                "accrued_interest": round(interest, 2),
                "collateral_value": round(collateral_value, 2),
            }
        )

    if total_principal <= 0:
        maintenance_rate = None
        status = "無借款"
    else:
        maintenance_rate = (total_collateral / total_principal) * 100
        if maintenance_rate < 140:
            status = "危險"
        elif maintenance_rate < 167:
            status = "警戒"
        else:
            status = "安全"

    return {
        "maintenance_rate": round(maintenance_rate, 2) if maintenance_rate is not None else None,
        "status": status,
        "total_principal": round(total_principal, 2),
        "total_collateral_value": round(total_collateral, 2),
        "total_accrued_interest": round(total_interest, 2),
        "loans": loan_items,
    }
