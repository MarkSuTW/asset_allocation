import re
from datetime import datetime
from typing import Any, List, Optional

from fastapi import HTTPException


def normalize_stock_id(raw: str) -> str:
    return (raw or "").strip().upper()


def normalize_date(raw: str) -> str:
    try:
        parsed = datetime.fromisoformat(raw.replace("/", "-"))
        return parsed.date().isoformat()
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be ISO format YYYY-MM-DD")


def parse_numeric(value: Any) -> float:
    if value is None:
        return 0.0
    s = str(value).strip().replace(",", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_quote_price(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def get_yahoo_symbol_candidates(stock_id: str) -> List[str]:
    sid = normalize_stock_id(stock_id)
    if not sid:
        return []

    base_sid = sid.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ") or sid
    symbol_candidates = [sid]
    if base_sid != sid:
        symbol_candidates.append(base_sid)

    symbols: List[str] = []
    for code in symbol_candidates:
        symbols.extend([f"{code}.TW", f"{code}.TWO"])
    return symbols