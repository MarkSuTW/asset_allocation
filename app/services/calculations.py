"""
Tax, NHI, and app_settings calculation helpers.
"""
import sqlite3
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app.core.utils import normalize_stock_id
from app.core.database import (
    DEFAULT_TAX_SETTINGS,
    DEFAULT_NHI_SETTINGS,
    get_app_setting,
    set_app_setting,
    get_transaction_tax_settings,
    set_transaction_tax_settings,
    get_nhi_settings,
    set_nhi_settings,
)


def calculate_transaction_tax(
    stock_id: str,
    action: str,
    shares: float,
    price: float,
    tax_settings: Optional[Dict[str, float]] = None,
) -> float:
    if action not in {"buy", "sell"}:
        return 0.0
    sid = normalize_stock_id(stock_id)
    cfg = tax_settings or DEFAULT_TAX_SETTINGS

    if sid.endswith("B"):
        group = "bond"
    elif sid.startswith("00"):
        group = "etf"
    else:
        group = "stock"

    key = f"{group}_{action}_tax_rate"
    rate = float(cfg.get(key, 0.0))
    tax = shares * price * rate
    return round(tax, 2)


def compute_nhi_premium(gross_cash_amount: float, nhi_rate: float, nhi_threshold: float) -> float:
    gross = float(gross_cash_amount or 0.0)
    if gross < float(nhi_threshold) or float(nhi_rate) <= 0:
        return 0.0
    return round(gross * float(nhi_rate), 2)


def compute_net_cash_dividend(gross_cash_amount: float, nhi_rate: float, nhi_threshold: float) -> float:
    premium = compute_nhi_premium(gross_cash_amount, nhi_rate, nhi_threshold)
    return round(float(gross_cash_amount or 0.0) - premium, 2)
