import json
import http.client
import html
import math
import os
import re
import ssl
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict, deque
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.utils import (
    get_yahoo_symbol_candidates,
    normalize_date,
    normalize_stock_id,
    parse_numeric,
    parse_quote_price,
)

DB_PATH = Path("wealth.db")
APP_DIR = Path(__file__).resolve().parent
_RUNTIME_SCHEMA_READY = False
_LOCAL_STOCK_NAME_MAP: Optional[Dict[str, str]] = None
_AUTO_STOCK_INFO_REPAIR_DONE = False
_DIVIDEND_RECALC_JOBS: Dict[str, Dict[str, Any]] = {}
_DIVIDEND_RECALC_JOBS_LOCK = threading.Lock()

app = FastAPI(title="Family Office Dashboard API", version="1.0.0")

_ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["Content-Type", "Authorization"],
)

_limiter = Limiter(key_func=get_remote_address)
app.state.limiter = _limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


class TransactionCreate(BaseModel):
    date: str
    stock_id: str
    action: str = Field(pattern="^(buy|sell)$")
    shares: float = Field(gt=0)
    price: float = Field(gt=0)
    fees: float = Field(default=0, ge=0)
    transaction_tax: float = Field(default=0, ge=0)


class AdvisorRequest(BaseModel):
    question: str


class TransactionUpdate(BaseModel):
    date: str
    stock_id: str
    action: str = Field(pattern="^(buy|sell)$")
    shares: float = Field(gt=0)
    price: float = Field(gt=0)
    fees: float = Field(default=0, ge=0)
    transaction_tax: float = Field(default=0, ge=0)


class TaxSettingsUpdate(BaseModel):
    stock_buy_tax_rate: float = Field(ge=0)
    stock_sell_tax_rate: float = Field(ge=0)
    etf_buy_tax_rate: float = Field(ge=0)
    etf_sell_tax_rate: float = Field(ge=0)
    bond_buy_tax_rate: float = Field(ge=0)
    bond_sell_tax_rate: float = Field(ge=0)


class NhiSettingsUpdate(BaseModel):
    nhi_supplement_rate: float = Field(ge=0)
    nhi_supplement_threshold: float = Field(ge=0)


class CashDividendCreate(BaseModel):
    stock_id: str
    ex_date: str
    pay_date: Optional[str] = None
    amount_per_share: float = Field(ge=0)
    holding_shares: Optional[float] = Field(default=None, ge=0)
    source: str = Field(default="manual")
    note: str = Field(default="")


class CashDividendUpdate(BaseModel):
    ex_date: str
    pay_date: Optional[str] = None
    amount_per_share: float = Field(ge=0)
    holding_shares: float = Field(ge=0)
    source: str = Field(default="manual")
    note: str = Field(default="")


class StockDividendCreate(BaseModel):
    stock_id: str
    ex_date: str
    allot_date: Optional[str] = None
    ratio: float
    holding_shares: Optional[float] = Field(default=None, ge=0)
    bonus_shares: Optional[float] = None
    event_type: str = Field(default="stock_dividend")
    cash_return_per_share: float = Field(default=0, ge=0)
    cash_return_amount: Optional[float] = Field(default=None, ge=0)
    source: str = Field(default="manual")
    note: str = Field(default="")


class StockDividendUpdate(BaseModel):
    ex_date: str
    allot_date: Optional[str] = None
    ratio: float
    holding_shares: float = Field(ge=0)
    bonus_shares: float
    event_type: str = Field(default="stock_dividend")
    cash_return_per_share: float = Field(default=0, ge=0)
    cash_return_amount: float = Field(default=0, ge=0)
    source: str = Field(default="manual")
    note: str = Field(default="")


class LoanCreate(BaseModel):
    lender: str
    collateral: str
    collateral_lots: float = Field(default=0, ge=0)
    principal: float = Field(ge=0)
    interest_rate: float = Field(ge=0)
    start_date: str
    due_date: Optional[str] = None
    note: str = Field(default="")


class LoanUpdate(BaseModel):
    lender: str
    collateral: str
    collateral_lots: float = Field(default=0, ge=0)
    principal: float = Field(ge=0)
    interest_rate: float = Field(ge=0)
    start_date: str
    due_date: Optional[str] = None
    note: str = Field(default="")


DEFAULT_TAX_SETTINGS = {
    "stock_buy_tax_rate": 0.0,
    "stock_sell_tax_rate": 0.003,
    "etf_buy_tax_rate": 0.0,
    "etf_sell_tax_rate": 0.001,
    "bond_buy_tax_rate": 0.0,
    "bond_sell_tax_rate": 0.001,
}

DEFAULT_NHI_SETTINGS = {
    "nhi_supplement_rate": 0.0211,
    "nhi_supplement_threshold": 20000.0,
}


def get_transaction_tax_settings(conn: sqlite3.Connection) -> Dict[str, float]:
    keys = tuple(DEFAULT_TAX_SETTINGS.keys())
    placeholders = ",".join(["?"] * len(keys))
    rows = conn.execute(
        f"SELECT key, value FROM app_settings WHERE key IN ({placeholders})",
        keys,
    ).fetchall()
    result = dict(DEFAULT_TAX_SETTINGS)
    for r in rows:
        try:
            result[r["key"]] = float(r["value"])
        except (TypeError, ValueError):
            continue
    return result


def set_transaction_tax_settings(conn: sqlite3.Connection, settings: Dict[str, float]) -> Dict[str, float]:
    safe = {k: float(settings.get(k, v)) for k, v in DEFAULT_TAX_SETTINGS.items()}
    if any(v < 0 for v in safe.values()):
        raise HTTPException(status_code=400, detail="tax rates must be >= 0")

    for k, v in safe.items():
        conn.execute(
            """
            INSERT INTO app_settings(key, value)
            VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (k, str(v)),
        )
    return safe


def set_nhi_settings(conn: sqlite3.Connection, settings: Dict[str, float]) -> Dict[str, float]:
    safe_rate = float(settings.get("nhi_supplement_rate", DEFAULT_NHI_SETTINGS["nhi_supplement_rate"]))
    safe_threshold = float(settings.get("nhi_supplement_threshold", DEFAULT_NHI_SETTINGS["nhi_supplement_threshold"]))
    if safe_rate < 0 or safe_threshold < 0:
        raise HTTPException(status_code=400, detail="nhi settings must be >= 0")

    set_app_setting(conn, "nhi_supplement_rate", str(safe_rate))
    set_app_setting(conn, "nhi_supplement_threshold", str(safe_threshold))
    return {
        "nhi_supplement_rate": safe_rate,
        "nhi_supplement_threshold": safe_threshold,
    }


def get_app_setting(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    return str(row["value"])


def set_app_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO app_settings(key, value)
        VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )


def write_audit_log(
    conn: sqlite3.Connection,
    event_type: str,
    payload: Dict[str, Any],
    severity: str = "INFO",
    actor: str = "system",
) -> None:
    conn.execute(
        """
        INSERT INTO system_audit_logs (event_type, severity, actor, payload_json)
        VALUES (?, ?, ?, ?)
        """,
        (
            (event_type or "unknown").strip() or "unknown",
            (severity or "INFO").strip().upper() or "INFO",
            (actor or "system").strip() or "system",
            json.dumps(payload or {}, ensure_ascii=False),
        ),
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


def get_shares_on_date(
    conn: sqlite3.Connection,
    stock_id: str,
    on_date: str,
    stock_dividend_before_tx: bool = True,
    exclude_stock_dividend_id: Optional[int] = None,
) -> float:
    sid = normalize_stock_id(stock_id)
    d = normalize_date(on_date)
    tx_rows = conn.execute(
        "SELECT id, date, action, shares FROM transactions WHERE stock_id = ? AND date <= ? ORDER BY date, id",
        (sid, d),
    ).fetchall()
    div_rows = conn.execute(
        "SELECT id, allot_date, bonus_shares FROM stock_dividends WHERE stock_id = ? AND allot_date <= ? ORDER BY allot_date, id",
        (sid, d),
    ).fetchall()

    events: List[Dict[str, Any]] = []
    for r in tx_rows:
        events.append({"kind": "tx", "date": r["date"], "id": int(r["id"]), "row": r})
    for r in div_rows:
        if exclude_stock_dividend_id is not None and int(r["id"]) == int(exclude_stock_dividend_id):
            continue
        events.append({"kind": "stock_div", "date": r["allot_date"], "id": int(r["id"]), "row": r})
    div_priority = 0 if stock_dividend_before_tx else 1
    tx_priority = 1 if stock_dividend_before_tx else 0
    events.sort(key=lambda e: (e["date"], div_priority if e["kind"] == "stock_div" else tx_priority, e["id"]))

    inventory = 0.0
    for event in events:
        if event["kind"] == "stock_div":
            bonus_shares = float(event["row"]["bonus_shares"])
            if inventory > 1e-9 and abs(bonus_shares) > 1e-9:
                inventory = max(inventory + bonus_shares, 0.0)
            continue

        r = event["row"]
        shares = float(r["shares"])
        if r["action"] == "buy":
            inventory += shares
        else:
            inventory = max(inventory - shares, 0.0)

    return max(inventory, 0.0)


def compute_stock_event_settlement(holding_shares: float, ratio: float) -> Dict[str, float]:
    # Taiwan stock dividends are typically allocated by whole-lot (1000 shares) entitlement;
    # capital reduction events still apply to all shares.
    base_shares = float(holding_shares)
    if ratio > 0:
        base_shares = float(math.floor(max(holding_shares, 0.0) / 1000.0) * 1000)
    delta = float(int(base_shares * ratio))
    return {"base_shares": base_shares, "share_delta": delta}


def infer_stock_event_type(ratio: float, cash_return_per_share: float = 0.0, explicit_event_type: Optional[str] = None) -> str:
    explicit = str(explicit_event_type or "").strip().lower()
    if explicit in {"stock_dividend", "capital_reduction_cash", "capital_reduction_other"}:
        return explicit
    if float(ratio) < -1e-9:
        return "capital_reduction_cash" if float(cash_return_per_share or 0.0) > 1e-9 else "capital_reduction_other"
    return "stock_dividend"


def apply_stock_event_to_lots(
    lots: deque,
    bonus_shares: float,
    event_type: str = "stock_dividend",
    cash_return_amount: float = 0.0,
) -> None:
    if abs(float(bonus_shares)) <= 1e-9:
        return

    current_inventory = sum(float(lot["shares"]) for lot in lots)
    if current_inventory <= 1e-9:
        return

    if bonus_shares > 0:
        lots.append(
            {
                "buy_tx_id": None,
                "shares": float(bonus_shares),
                "unit_cost": 0.0,
            }
        )
        return

    target_inventory = max(current_inventory + float(bonus_shares), 0.0)
    if target_inventory <= 1e-9:
        lots.clear()
        return

    lot_costs = [float(lot["shares"]) * float(lot["unit_cost"]) for lot in lots]
    total_cost = sum(lot_costs)
    if (
        infer_stock_event_type(float(bonus_shares), 0.0, event_type) == "capital_reduction_cash"
        and total_cost > 1e-9
        and float(cash_return_amount or 0.0) > 1e-9
    ):
        reduced_total_cost = max(total_cost - min(float(cash_return_amount), total_cost), 0.0)
        reduce_factor = reduced_total_cost / total_cost if total_cost > 0 else 1.0
        lot_costs = [c * reduce_factor for c in lot_costs]

    share_factor = target_inventory / current_inventory if current_inventory > 0 else 0.0
    for idx, lot in enumerate(lots):
        new_shares = max(float(lot["shares"]) * share_factor, 0.0)
        new_cost = float(lot_costs[idx]) if idx < len(lot_costs) else 0.0
        lot["shares"] = new_shares
        lot["unit_cost"] = (new_cost / new_shares) if new_shares > 1e-9 else 0.0

    while lots and float(lots[0]["shares"]) <= 1e-9:
        lots.popleft()
    for idx in range(len(lots) - 1, -1, -1):
        if float(lots[idx]["shares"]) <= 1e-9:
            del lots[idx]


def get_nhi_settings(conn: sqlite3.Connection) -> Dict[str, float]:
    rate_raw = get_app_setting(conn, "nhi_supplement_rate", str(DEFAULT_NHI_SETTINGS["nhi_supplement_rate"])) or str(DEFAULT_NHI_SETTINGS["nhi_supplement_rate"])
    threshold_raw = get_app_setting(conn, "nhi_supplement_threshold", str(DEFAULT_NHI_SETTINGS["nhi_supplement_threshold"])) or str(DEFAULT_NHI_SETTINGS["nhi_supplement_threshold"])
    try:
        rate = max(0.0, float(rate_raw))
    except (TypeError, ValueError):
        rate = 0.0211
    try:
        threshold = max(0.0, float(threshold_raw))
    except (TypeError, ValueError):
        threshold = 20000.0
    return {"rate": rate, "threshold": threshold}


def compute_nhi_premium(gross_cash_amount: float, nhi_rate: float, nhi_threshold: float) -> float:
    gross = float(gross_cash_amount or 0.0)
    if gross < float(nhi_threshold) or float(nhi_rate) <= 0:
        return 0.0
    return round(gross * float(nhi_rate), 2)


def compute_net_cash_dividend(gross_cash_amount: float, nhi_rate: float, nhi_threshold: float) -> float:
    premium = compute_nhi_premium(gross_cash_amount, nhi_rate, nhi_threshold)
    return round(float(gross_cash_amount or 0.0) - premium, 2)


def get_cash_dividend_sum(conn: sqlite3.Connection, stock_id: str, date_from: Optional[str] = None) -> float:
    sid = normalize_stock_id(stock_id)
    params: List[Any] = [sid]
    where = ["stock_id = ?"]
    if date_from:
        where.append("pay_date >= ?")
        params.append(normalize_date(date_from))

    rows = conn.execute(
        f"SELECT cash_amount FROM cash_dividends WHERE {' AND '.join(where)}",
        params,
    ).fetchall()
    nhi = get_nhi_settings(conn)
    return round(
        sum(
            compute_net_cash_dividend(float(r["cash_amount"] or 0.0), nhi_rate=nhi["rate"], nhi_threshold=nhi["threshold"])
            for r in rows
        ),
        2,
    )


def get_bonus_shares_sum(conn: sqlite3.Connection, stock_id: str, date_from: Optional[str] = None) -> float:
    sid = normalize_stock_id(stock_id)
    params: List[Any] = [sid]
    where = ["stock_id = ?"]
    if date_from:
        where.append("allot_date >= ?")
        params.append(normalize_date(date_from))

    row = conn.execute(
        f"SELECT COALESCE(SUM(bonus_shares), 0) AS v FROM stock_dividends WHERE {' AND '.join(where)}",
        params,
    ).fetchone()
    return float(row["v"] if row else 0.0)


def detect_oversell_events(conn: sqlite3.Connection, limit: int = 200) -> List[Dict[str, Any]]:
    tx_rows = conn.execute(
        "SELECT id, date, stock_id, action, shares FROM transactions ORDER BY date, id"
    ).fetchall()
    div_rows = conn.execute(
        "SELECT id, allot_date, stock_id, bonus_shares FROM stock_dividends ORDER BY allot_date, id"
    ).fetchall()

    events: List[Dict[str, Any]] = []
    for r in tx_rows:
        events.append({"kind": "tx", "date": r["date"], "id": int(r["id"]), "row": r})
    for r in div_rows:
        events.append({"kind": "stock_div", "date": r["allot_date"], "id": int(r["id"]), "row": r})
    events.sort(key=lambda e: (e["date"], 0 if e["kind"] == "stock_div" else 1, e["id"]))

    inventory_by_stock: Dict[str, float] = defaultdict(float)
    issues: List[Dict[str, Any]] = []

    for event in events:
        if len(issues) >= max(1, limit):
            break

        r = event["row"]
        sid = normalize_stock_id(r["stock_id"])
        inv = float(inventory_by_stock[sid])

        if event["kind"] == "stock_div":
            bonus = float(r["bonus_shares"])
            if inv > 1e-9 and abs(bonus) > 1e-9:
                inventory_by_stock[sid] = max(inv + bonus, 0.0)
            continue

        shares = float(r["shares"])
        action = r["action"]
        if action == "buy":
            inventory_by_stock[sid] = inv + shares
            continue

        if shares > inv + 1e-9:
            issues.append(
                {
                    "tx_id": int(r["id"]),
                    "date": r["date"],
                    "stock_id": sid,
                    "requested_sell_shares": round(shares, 4),
                    "available_shares": round(inv, 4),
                    "excess_shares": round(shares - inv, 4),
                }
            )
        inventory_by_stock[sid] = max(inv - shares, 0.0)

    return issues


def build_data_health_report(conn: sqlite3.Connection, deep: bool = False, max_stocks: int = 80) -> Dict[str, Any]:
    checked_at = datetime.now().isoformat(timespec="seconds")

    active_rows = conn.execute(
        """
        SELECT stock_id, shares
        FROM holdings
        WHERE COALESCE(shares, 0) > 0
        ORDER BY stock_id
        """
    ).fetchall()
    active_holdings = [
        {
            "stock_id": normalize_stock_id(r["stock_id"]),
            "shares": round(float(r["shares"]), 4),
        }
        for r in active_rows
    ]

    missing_price_rows = conn.execute(
        """
        SELECT h.stock_id, COALESCE(s.chinese_name, h.stock_id) AS chinese_name, h.shares, COALESCE(s.current_price, 0) AS current_price
        FROM holdings h
        LEFT JOIN stock_info s ON s.stock_id = h.stock_id
        WHERE COALESCE(h.shares, 0) > 0 AND COALESCE(s.current_price, 0) <= 0
        ORDER BY h.stock_id
        """
    ).fetchall()
    missing_prices = [
        {
            "stock_id": normalize_stock_id(r["stock_id"]),
            "chinese_name": r["chinese_name"],
            "shares": round(float(r["shares"]), 4),
        }
        for r in missing_price_rows
    ]

    orphan_rows = conn.execute(
        """
        SELECT h.stock_id, h.shares
        FROM holdings h
        LEFT JOIN (
            SELECT stock_id, COUNT(1) AS c
            FROM transactions
            GROUP BY stock_id
        ) t ON t.stock_id = h.stock_id
        WHERE COALESCE(h.shares, 0) > 0 AND COALESCE(t.c, 0) = 0
        ORDER BY h.stock_id
        """
    ).fetchall()
    orphan_holdings = [
        {
            "stock_id": normalize_stock_id(r["stock_id"]),
            "shares": round(float(r["shares"]), 4),
        }
        for r in orphan_rows
    ]

    oversell_issues = detect_oversell_events(conn, limit=500)

    missing_stock_events: List[Dict[str, Any]] = []
    fetch_errors: List[Dict[str, Any]] = []

    if deep:
        checked_stock_ids = [x["stock_id"] for x in active_holdings][: max(1, int(max_stocks))]
        for sid in checked_stock_ids:
            min_tx = conn.execute(
                "SELECT MIN(date) AS min_date FROM transactions WHERE stock_id = ?",
                (sid,),
            ).fetchone()
            dynamic_years = 2
            if min_tx and min_tx["min_date"]:
                try:
                    tx_year = datetime.fromisoformat(str(min_tx["min_date"])).date().year
                    dynamic_years = max(dynamic_years, date.today().year - tx_year + 1)
                except ValueError:
                    pass

            try:
                source_events = fetch_yahoo_dividend_events(sid, years=dynamic_years).get("stock") or []
            except Exception as exc:
                fetch_errors.append({"stock_id": sid, "message": str(exc)})
                continue

            db_rows = conn.execute(
                "SELECT ex_date, ratio FROM stock_dividends WHERE stock_id = ?",
                (sid,),
            ).fetchall()
            db_keys = {(str(r["ex_date"]), round(float(r["ratio"]), 6)) for r in db_rows}

            for event in source_events:
                ex_date = str(event.get("ex_date") or "")
                ratio = round(float(event.get("ratio") or 0.0), 6)
                if not ex_date or abs(ratio) <= 1e-9:
                    continue
                if (ex_date, ratio) in db_keys:
                    continue

                holding_shares = get_shares_on_date(conn, sid, ex_date)
                if holding_shares <= 1e-9:
                    continue

                missing_stock_events.append(
                    {
                        "stock_id": sid,
                        "ex_date": ex_date,
                        "ratio": ratio,
                        "holding_shares": round(float(holding_shares), 4),
                        "estimated_share_delta": round(float(holding_shares) * ratio, 4),
                        "source": str(event.get("source") or "yahoo_split_auto"),
                    }
                )

    issue_count = (
        len(missing_prices)
        + len(orphan_holdings)
        + len(oversell_issues)
        + len(missing_stock_events)
    )

    return {
        "checked_at": checked_at,
        "deep_mode": bool(deep),
        "active_holding_count": len(active_holdings),
        "issue_count": issue_count,
        "issues": {
            "missing_prices": missing_prices,
            "orphan_holdings": orphan_holdings,
            "oversell_transactions": oversell_issues,
            "missing_stock_events": missing_stock_events,
        },
        "meta": {
            "fetch_errors": fetch_errors,
            "notes": [
                "deep_mode 會比對 Yahoo 股票事件來源，可能花較長時間。",
                "oversell_transactions 代表歷史交易資料出現賣超，會影響重建精準度。",
            ],
        },
    }


def ensure_runtime_schema(conn: sqlite3.Connection) -> None:
    global _RUNTIME_SCHEMA_READY
    if _RUNTIME_SCHEMA_READY:
        return

    cols = conn.execute("PRAGMA table_info(transactions)").fetchall()
    col_names = {str(c[1]).lower() for c in cols}
    needs_rebuild = False

    if "transaction_tax" not in col_names:
        conn.execute("ALTER TABLE transactions ADD COLUMN transaction_tax REAL NOT NULL DEFAULT 0")
        needs_rebuild = True

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cash_dividends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_id TEXT NOT NULL,
            ex_date TEXT NOT NULL,
            pay_date TEXT NOT NULL,
            amount_per_share REAL NOT NULL DEFAULT 0,
            holding_shares REAL NOT NULL DEFAULT 0,
            cash_amount REAL NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'manual',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (stock_id) REFERENCES stock_info(stock_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_dividends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_id TEXT NOT NULL,
            ex_date TEXT NOT NULL,
            allot_date TEXT NOT NULL,
            ratio REAL NOT NULL DEFAULT 0,
            holding_shares REAL NOT NULL DEFAULT 0,
            bonus_shares REAL NOT NULL DEFAULT 0,
            event_type TEXT NOT NULL DEFAULT 'stock_dividend',
            cash_return_per_share REAL NOT NULL DEFAULT 0,
            cash_return_amount REAL NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'manual',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (stock_id) REFERENCES stock_info(stock_id)
        )
        """
    )
    stock_div_cols = conn.execute("PRAGMA table_info(stock_dividends)").fetchall()
    stock_div_col_names = {str(c[1]).lower() for c in stock_div_cols}
    if "event_type" not in stock_div_col_names:
        conn.execute("ALTER TABLE stock_dividends ADD COLUMN event_type TEXT NOT NULL DEFAULT 'stock_dividend'")
    if "cash_return_per_share" not in stock_div_col_names:
        conn.execute("ALTER TABLE stock_dividends ADD COLUMN cash_return_per_share REAL NOT NULL DEFAULT 0")
    if "cash_return_amount" not in stock_div_col_names:
        conn.execute("ALTER TABLE stock_dividends ADD COLUMN cash_return_amount REAL NOT NULL DEFAULT 0")
    conn.execute(
        """
        UPDATE stock_dividends
        SET event_type = CASE
            WHEN COALESCE(ratio, 0) < 0 THEN 'capital_reduction_other'
            ELSE 'stock_dividend'
        END
        WHERE TRIM(COALESCE(event_type, '')) = '' OR event_type = 'stock_dividend'
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'INFO',
            actor TEXT NOT NULL DEFAULT 'system',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    set_transaction_tax_settings(conn, get_transaction_tax_settings(conn))

    missing_tax = conn.execute(
        "SELECT COUNT(1) AS c FROM transactions WHERE transaction_tax IS NULL"
    ).fetchone()
    if missing_tax and int(missing_tax["c"]) > 0:
        needs_rebuild = True

    if needs_rebuild:
        rebuild_holdings_and_realized(conn)
        conn.commit()

    loan_cols = {str(c[1]).lower() for c in conn.execute("PRAGMA table_info(loans)").fetchall()}
    if "due_date" not in loan_cols:
        conn.execute("ALTER TABLE loans ADD COLUMN due_date TEXT NOT NULL DEFAULT ''")
    if "note" not in loan_cols:
        conn.execute("ALTER TABLE loans ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    if "collateral_lots" not in loan_cols:
        conn.execute("ALTER TABLE loans ADD COLUMN collateral_lots REAL NOT NULL DEFAULT 0")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_stock_id ON transactions(stock_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cash_dividends_stock_id ON cash_dividends(stock_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cash_dividends_ex_date ON cash_dividends(ex_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stock_dividends_stock_id ON stock_dividends(stock_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stock_dividends_allot_date ON stock_dividends(allot_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_event_type ON system_audit_logs(event_type)")
    conn.commit()

    _RUNTIME_SCHEMA_READY = True


def get_conn(auto_repair: bool = True) -> sqlite3.Connection:
    global _AUTO_STOCK_INFO_REPAIR_DONE
    if not DB_PATH.exists():
        raise HTTPException(status_code=500, detail="Database wealth.db not found. Run init_db.py first.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_runtime_schema(conn)
    if auto_repair and not _AUTO_STOCK_INFO_REPAIR_DONE:
        try:
            repaired = ensure_stock_info_integrity(conn)
            if repaired > 0:
                conn.commit()
            _AUTO_STOCK_INFO_REPAIR_DONE = True
        except sqlite3.OperationalError:
            # Avoid interrupting API responses when SQLite is temporarily write-locked.
            pass
    return conn


def load_local_stock_name_map() -> Dict[str, str]:
    global _LOCAL_STOCK_NAME_MAP
    if _LOCAL_STOCK_NAME_MAP is not None:
        return _LOCAL_STOCK_NAME_MAP

    data_dir = APP_DIR / "data"
    mapping: Dict[str, str] = {}
    if data_dir.exists():
        for p in data_dir.glob("*.csv"):
            stem = p.stem.strip()
            m = re.match(r"^([0-9A-Z]+)(.+)$", stem)
            if not m:
                continue
            sid = normalize_stock_id(m.group(1))
            cname = m.group(2).strip()
            if sid and cname and sid not in mapping:
                mapping[sid] = cname

    _LOCAL_STOCK_NAME_MAP = mapping
    return mapping


def get_local_stock_name(stock_id: str) -> Optional[str]:
    sid = normalize_stock_id(stock_id)
    if not sid:
        return None
    return load_local_stock_name_map().get(sid)


def ensure_stock_info(conn: sqlite3.Connection, stock_id: str) -> None:
    sid = normalize_stock_id(stock_id)
    if not sid:
        return
    exists = conn.execute("SELECT 1 FROM stock_info WHERE stock_id = ?", (sid,)).fetchone()
    if not exists:
        cname = get_local_stock_name(sid)
        conn.execute(
            "INSERT INTO stock_info (stock_id, chinese_name, asset_type, sector, current_price) VALUES (?, ?, '個股', '其他', 0.0)",
            (sid, cname),
        )


def ensure_stock_info_integrity(conn: sqlite3.Connection) -> int:
    conn.execute(
        """
        INSERT INTO stock_info (stock_id, chinese_name, asset_type, sector, current_price)
        SELECT src.stock_id, NULL, '個股', '其他', 0.0
        FROM (
            SELECT DISTINCT UPPER(TRIM(stock_id)) AS stock_id
            FROM transactions
            WHERE TRIM(COALESCE(stock_id, '')) <> ''

            UNION

            SELECT DISTINCT UPPER(TRIM(stock_id)) AS stock_id
            FROM holdings
            WHERE TRIM(COALESCE(stock_id, '')) <> ''

            UNION

            SELECT DISTINCT UPPER(TRIM(stock_id)) AS stock_id
            FROM cash_dividends
            WHERE TRIM(COALESCE(stock_id, '')) <> ''

            UNION

            SELECT DISTINCT UPPER(TRIM(stock_id)) AS stock_id
            FROM stock_dividends
            WHERE TRIM(COALESCE(stock_id, '')) <> ''
        ) src
        WHERE src.stock_id <> ''
          AND NOT EXISTS (
              SELECT 1
              FROM stock_info s
              WHERE UPPER(TRIM(s.stock_id)) = src.stock_id
          )
        """
    )
    changed = conn.execute("SELECT changes() AS c").fetchone()
    inserted_count = int(changed["c"]) if changed else 0

    name_updated_count = 0
    missing_name_rows = conn.execute(
        """
        SELECT stock_id
        FROM stock_info
        WHERE COALESCE(TRIM(chinese_name), '') = ''
        """
    ).fetchall()
    for row in missing_name_rows:
        sid = normalize_stock_id(row["stock_id"])
        cname = get_local_stock_name(sid)
        if not cname:
            continue
        conn.execute(
            """
            UPDATE stock_info
            SET chinese_name = ?
            WHERE stock_id = ? AND COALESCE(TRIM(chinese_name), '') = ''
            """,
            (cname, sid),
        )
        c = conn.execute("SELECT changes() AS c").fetchone()
        name_updated_count += int(c["c"]) if c else 0

    return inserted_count + name_updated_count


def get_latest_price(conn: sqlite3.Connection, stock_id: str) -> float:
    row = conn.execute(
        "SELECT price FROM transactions WHERE stock_id = ? ORDER BY date DESC, id DESC LIMIT 1",
        (stock_id,),
    ).fetchone()
    return float(row[0]) if row else 0.0




def fetch_yahoo_dividend_events(stock_id: str, years: int = 2) -> Dict[str, List[Dict[str, Any]]]:
    sid = normalize_stock_id(stock_id)
    if not sid:
        return {"cash": [], "stock": []}

    safe_years = max(1, min(int(years), 10))
    symbols = get_yahoo_symbol_candidates(sid)
    cash_events: Dict[tuple, Dict[str, Any]] = {}
    stock_events: Dict[tuple, Dict[str, Any]] = {}
    source_symbol: Optional[str] = None

    for symbol in symbols:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
            f"?interval=1d&range={safe_years}y&events=div,splits"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            continue

        chart_result = payload.get("chart", {}).get("result", [])
        if not chart_result:
            continue
        source_symbol = symbol

        events = chart_result[0].get("events", {}) or {}
        dividends = events.get("dividends", {}) or {}
        splits = events.get("splits", {}) or {}

        for d in dividends.values():
            amount = parse_quote_price(d.get("amount"))
            ts = d.get("date")
            if amount is None or ts is None:
                continue
            ex_date = datetime.fromtimestamp(int(ts)).date().isoformat()
            key = (ex_date, round(float(amount), 6))
            cash_events[key] = {
                "ex_date": ex_date,
                "amount_per_share": round(float(amount), 6),
            }

        for sp in splits.values():
            ts = sp.get("date")
            numerator = parse_numeric(sp.get("numerator"))
            denominator = parse_numeric(sp.get("denominator"))
            if ts is None or numerator <= 0 or denominator <= 0:
                continue
            ratio = numerator / denominator - 1.0
            if abs(ratio) <= 1e-9:
                continue
            ex_date = datetime.fromtimestamp(int(ts)).date().isoformat()
            key = (ex_date, round(float(ratio), 6))
            stock_events[key] = {
                "ex_date": ex_date,
                "ratio": round(float(ratio), 6),
                "allot_date": ex_date,
                "source": "yahoo_split_auto",
            }

    return {
        "cash": sorted(cash_events.values(), key=lambda x: x["ex_date"]),
        "stock": sorted(stock_events.values(), key=lambda x: x["ex_date"]),
        "source_symbol": source_symbol,
        "attempted_symbols": symbols,
    }


def parse_twse_roc_date(raw: str) -> Optional[str]:
    text = html.unescape(str(raw or "")).strip()
    if not text:
        return None
    match = re.search(r"(\d+)年\s*(\d+)月\s*(\d+)日", text)
    if not match:
        return None
    year = int(match.group(1)) + 1911
    month = int(match.group(2))
    day = int(match.group(3))
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_roc_flexible_date(raw: Any) -> Optional[str]:
    text = html.unescape(str(raw or "")).strip()
    if not text:
        return None

    # yyyy/mm/dd or yyy/mm/dd(民國)
    slash_match = re.search(r"(\d{2,4})[/-](\d{1,2})[/-](\d{1,2})", text)
    if slash_match:
        year = int(slash_match.group(1))
        month = int(slash_match.group(2))
        day = int(slash_match.group(3))
        if year < 1911:
            year += 1911
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    return parse_twse_roc_date(text)


def _mops_post_json(api_name: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = f"https://mops.twse.com.tw/mops/api/{api_name}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://mops.twse.com.tw",
            "Referer": f"https://mops.twse.com.tw/mops/#/web/{'t108sb19' if api_name == 't108sb19_detail' else api_name}",
        },
    )
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15, context=ssl._create_unverified_context()) as resp:
                return json.loads(resp.read().decode("utf-8", errors="ignore"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, http.client.RemoteDisconnected, OSError):
            continue
    return None


def _extract_mops_detail_cash_event(detail_result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    rows = detail_result.get("data") or []
    if not rows:
        return None

    text_chunks: List[str] = []
    for row in rows:
        for cell in row:
            if isinstance(cell, dict):
                continue
            if cell is None:
                continue
            text_chunks.append(html.unescape(str(cell)))
    joined = "\n".join(text_chunks)

    ex_match = re.search(r"除權/除息交易日：\s*(\d+年\s*\d+月\s*\d+日)", joined)
    pay_match = re.search(r"現金股利發放日：\s*(\d+年\s*\d+月\s*\d+日)", joined)
    amount_match = re.search(r"每壹股配發現金(?:\(股利\))?([0-9.,]+)元", joined)

    ex_date = parse_twse_roc_date(ex_match.group(1)) if ex_match else None
    pay_date = parse_twse_roc_date(pay_match.group(1)) if pay_match else None
    amount = parse_quote_price(amount_match.group(1)) if amount_match else None
    if ex_date is None or amount is None:
        return None
    if pay_date is None:
        pay_date = ex_date

    return {
        "ex_date": ex_date,
        "pay_date": pay_date,
        "amount_per_share": round(float(amount), 6),
        "source": "mops_t108sb19",
        "source_label": "MOPS 除權息公告",
    }


def _extract_mops_detail_text(detail_result: Dict[str, Any]) -> str:
    rows = detail_result.get("data") or []
    text_chunks: List[str] = []
    for row in rows:
        for cell in row:
            if isinstance(cell, dict) or cell is None:
                continue
            text_chunks.append(html.unescape(str(cell)))
    return "\n".join(text_chunks)


def _extract_mops_detail_stock_event(detail_result: Dict[str, Any], announcement_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    joined = _extract_mops_detail_text(detail_result)
    if not joined:
        return None

    if not re.search(r"減資|彌補虧損|註銷|換發", joined):
        return None

    ratio: Optional[float] = None
    pct_match = re.search(r"減資(?:比率|比例)?\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)\s*%", joined)
    if pct_match:
        ratio = -float(pct_match.group(1)) / 100.0

    if ratio is None:
        per_share_reduce = re.search(r"每(?:壹|一)股(?:減少|註銷)\s*([0-9]+(?:\.[0-9]+)?)\s*股", joined)
        if per_share_reduce:
            ratio = -float(per_share_reduce.group(1))

    if ratio is None:
        per_thousand_reduce = re.search(r"每(?:壹仟|一千|千)股(?:減少|註銷)\s*([0-9]+(?:\.[0-9]+)?)\s*股", joined)
        if per_thousand_reduce:
            ratio = -float(per_thousand_reduce.group(1)) / 1000.0

    if ratio is None:
        per_thousand_exchange = re.search(r"每(?:壹仟|一千|千)股換發\s*([0-9]+(?:\.[0-9]+)?)\s*股", joined)
        if per_thousand_exchange:
            ratio = float(per_thousand_exchange.group(1)) / 1000.0 - 1.0

    if ratio is None or abs(ratio) <= 1e-9:
        return None

    cash_return_per_share = 0.0
    cash_per_share_match = re.search(r"每(?:壹|一)股(?:退還|返還|退回)(?:現金|股款)?\s*([0-9]+(?:\.[0-9]+)?)\s*元", joined)
    if cash_per_share_match:
        cash_return_per_share = float(cash_per_share_match.group(1))
    else:
        cash_per_thousand_match = re.search(r"每(?:壹仟|一千|千)股(?:退還|返還|退回)(?:現金|股款)?\s*([0-9]+(?:\.[0-9]+)?)\s*元", joined)
        if cash_per_thousand_match:
            cash_return_per_share = float(cash_per_thousand_match.group(1)) / 1000.0

    has_cash_reduction_hint = bool(re.search(r"現金減資|退還股款|返還股款|退回股款|退還現金", joined))
    event_type = "stock_dividend"
    if ratio < -1e-9:
        event_type = "capital_reduction_cash" if (cash_return_per_share > 1e-9 or has_cash_reduction_hint) else "capital_reduction_other"

    ex_match = re.search(r"減資換發股票基準日[:：]\s*(\d+年\s*\d+月\s*\d+日)", joined)
    if not ex_match:
        ex_match = re.search(r"權利分派基準日[:：]\s*(\d+年\s*\d+月\s*\d+日)", joined)
    ex_date = parse_twse_roc_date(ex_match.group(1)) if ex_match else parse_roc_flexible_date(announcement_date)
    if ex_date is None:
        return None

    return {
        "ex_date": ex_date,
        "allot_date": ex_date,
        "ratio": round(float(ratio), 6),
        "event_type": event_type,
        "cash_return_per_share": round(float(cash_return_per_share), 6),
        "source": "mops_capital_reduction_auto",
        "source_label": "MOPS 減資公告",
    }


def fetch_mops_capital_reduction_events(stock_id: str, start_roc_year: int, end_roc_year: int) -> Dict[str, Any]:
    sid = normalize_stock_id(stock_id)
    if not sid:
        return {"stock": [], "source": "invalid_stock_id", "attempted_urls": []}

    start = max(1, int(start_roc_year))
    end = max(start, int(end_roc_year))
    attempted_urls: List[str] = []
    stock_events: Dict[tuple, Dict[str, Any]] = {}

    for roc_year in range(start, end + 1):
        payload = {
            "companyId": sid,
            "dataType": "2",
            "year": str(roc_year),
            "month": "all",
            "firstDay": "01",
            "lastDay": "31",
        }
        attempted_urls.append(f"https://mops.twse.com.tw/mops/api/t108sb19?companyId={sid}&year={roc_year}&month=all")
        response = _mops_post_json("t108sb19", payload)
        if not response or int(response.get("code") or 0) != 200:
            continue

        result = response.get("result") or {}
        record_rows = (result.get("recordDateAnnouncement") or {}).get("data") or []
        for row in record_rows:
            if len(row) < 5:
                continue
            announcement_date = parse_roc_flexible_date(row[0])
            detail_ref = row[4]
            if not isinstance(detail_ref, dict):
                continue
            params = detail_ref.get("parameters") or {}
            detail_payload = {
                "companyId": params.get("companyId") or sid,
                "serialNumber": params.get("serialNumber"),
                "detailReportKind": params.get("detailReportKind") or "A",
                "declarationDate": params.get("declarationDate") or "",
                "etn_no": params.get("etn_no") or "",
            }
            attempted_urls.append(
                "https://mops.twse.com.tw/mops/api/t108sb19_detail"
                f"?companyId={sid}&serialNumber={detail_payload['serialNumber']}"
            )
            detail_response = _mops_post_json("t108sb19_detail", detail_payload)
            if not detail_response or int(detail_response.get("code") or 0) != 200:
                continue

            detail_result = detail_response.get("result") or {}
            event = _extract_mops_detail_stock_event(detail_result, announcement_date=announcement_date)
            if not event:
                continue

            key = (event["ex_date"], round(float(event["ratio"]), 6))
            stock_events[key] = event

    return {
        "stock": sorted(stock_events.values(), key=lambda x: (x["ex_date"], x["ratio"])),
        "source": "mops_capital_reduction_auto",
        "source_symbol": sid,
        "attempted_urls": attempted_urls,
    }


def fetch_mops_dividend_announcement_events(stock_id: str, years: int = 2) -> Dict[str, Any]:
    sid = normalize_stock_id(stock_id)
    if not sid:
        return {"cash": [], "stock": [], "source": "invalid_stock_id", "attempted_urls": []}

    safe_years = max(1, min(int(years), 10))
    current_roc_year = date.today().year - 1911
    attempted_urls: List[str] = []
    cash_events: Dict[tuple, Dict[str, Any]] = {}

    for roc_year in range(current_roc_year, current_roc_year - safe_years, -1):
        payload = {
            "companyId": sid,
            "dataType": "2",
            "year": str(roc_year),
            "month": "all",
            "firstDay": "",
            "lastDay": "",
        }
        attempted_urls.append(f"https://mops.twse.com.tw/mops/api/t108sb19?companyId={sid}&year={roc_year}")
        response = _mops_post_json("t108sb19", payload)
        if not response or int(response.get("code") or 0) != 200:
            continue

        result = response.get("result") or {}
        record_rows = (result.get("recordDateAnnouncement") or {}).get("data") or []
        for row in record_rows:
            if len(row) < 5:
                continue
            detail_ref = row[4]
            if not isinstance(detail_ref, dict):
                continue
            params = detail_ref.get("parameters") or {}
            detail_payload = {
                "companyId": params.get("companyId") or sid,
                "serialNumber": params.get("serialNumber"),
                "detailReportKind": params.get("detailReportKind") or "A",
                "declarationDate": params.get("declarationDate") or "",
                "etn_no": params.get("etn_no") or "",
            }
            attempted_urls.append(f"https://mops.twse.com.tw/mops/api/t108sb19_detail?companyId={sid}&serialNumber={detail_payload['serialNumber']}")
            detail_response = _mops_post_json("t108sb19_detail", detail_payload)
            if not detail_response or int(detail_response.get("code") or 0) != 200:
                continue

            detail_result = detail_response.get("result") or {}
            event = _extract_mops_detail_cash_event(detail_result)
            if not event:
                continue

            key = (event["ex_date"], round(float(event["amount_per_share"]), 6), event["pay_date"])
            cash_events[key] = event

    return {
        "cash": sorted(cash_events.values(), key=lambda x: (x["ex_date"], x["pay_date"], x["amount_per_share"])),
        "stock": [],
        "source": "mops_t108sb19",
        "source_symbol": sid,
        "attempted_urls": attempted_urls,
    }


def fetch_twse_dividend_list_events(stock_id: str, years: int = 2) -> Dict[str, Any]:
    sid = normalize_stock_id(stock_id)
    if not sid:
        return {"cash": [], "stock": [], "source": "invalid_stock_id", "attempted_urls": []}

    safe_years = max(1, min(int(years), 10))
    current_roc_year = date.today().year - 1911
    start_year = max(1, current_roc_year - safe_years + 1)
    attempted_urls: List[str] = []
    cash_events: Dict[tuple, Dict[str, Any]] = {}

    url_candidates = [
        (
            "https://www.twse.com.tw/zh/ETFortune/dividendList"
            f"?stkNo={urllib.parse.quote(sid)}&startDate={start_year}&endDate={current_roc_year}"
        ),
        f"https://www.twse.com.tw/zh/ETFortune/dividendList?stkNo={urllib.parse.quote(sid)}",
    ]

    for url in url_candidates:
        attempted_urls.append(url)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=12, context=ssl._create_unverified_context()) as resp:
                payload = resp.read().decode("utf-8", errors="ignore")
        except (urllib.error.URLError, TimeoutError):
            continue

        row_matches = re.findall(r'<tr[^>]*onclick="document\.location = [^"]+;">(.*?)</tr>', payload, re.S)
        for row_html in row_matches:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
            if len(cells) < 6:
                continue

            row_sid = html.unescape(re.sub(r"<[^>]+>", "", cells[0])).strip().upper()
            if row_sid != sid:
                continue

            ex_date = parse_twse_roc_date(cells[2])
            pay_date = parse_twse_roc_date(cells[4])
            amount = parse_quote_price(html.unescape(re.sub(r"<[^>]+>", "", cells[5])).strip())
            if ex_date is None or amount is None:
                continue
            if pay_date is None:
                pay_date = ex_date

            key = (ex_date, round(float(amount), 6), pay_date)
            cash_events[key] = {
                "ex_date": ex_date,
                "pay_date": pay_date,
                "amount_per_share": round(float(amount), 6),
                "source": "twse_etfortune",
                "source_label": "TWSE 配息清單",
            }

        if cash_events:
            break

    if cash_events:
        return {
            "cash": sorted(cash_events.values(), key=lambda x: (x["ex_date"], x["pay_date"], x["amount_per_share"])),
            "stock": [],
            "source": "twse_etfortune",
            "source_symbol": sid,
            "attempted_urls": attempted_urls,
        }

    mops_events = fetch_mops_dividend_announcement_events(sid, years=years)
    attempted_urls.extend(mops_events.get("attempted_urls") or [])
    return {
        "cash": mops_events.get("cash") or [],
        "stock": [],
        "source": mops_events.get("source") or "mops_t108sb19",
        "source_symbol": sid,
        "attempted_urls": attempted_urls,
    }


def sync_dividends_from_market(conn: sqlite3.Connection, years: int = 2) -> Dict[str, Any]:
    stock_ids = _list_dividend_sync_stock_ids(conn)

    inserted_cash = 0
    inserted_stock = 0
    updated_stock = 0
    stock_details: List[Dict[str, Any]] = []
    failed_stocks: List[Dict[str, Any]] = []

    for sid in stock_ids:
        try:
            one = _sync_dividends_for_stock(conn, sid, years=years)
            inserted_cash += int(one.get("inserted_cash_events", 0))
            inserted_stock += int(one.get("inserted_stock_events", 0))
            updated_stock += int(one.get("updated_stock_events", 0))
            stock_details.append(one)
        except Exception as exc:
            failed_stocks.append({"stock_id": sid, "error": str(exc)})

    if inserted_stock > 0 or updated_stock > 0:
        rebuild_holdings_and_realized(conn)

    return {
        "processed_stocks": len(stock_ids),
        "inserted_cash_dividends": inserted_cash,
        "inserted_stock_dividends": inserted_stock,
        "updated_stock_dividends": updated_stock,
        "failed_stocks": failed_stocks,
        "stock_details": stock_details,
    }


def _list_dividend_sync_stock_ids(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        """
        SELECT stock_id
        FROM (
            SELECT DISTINCT stock_id AS stock_id
            FROM transactions
            WHERE TRIM(COALESCE(stock_id, '')) <> ''

            UNION

            SELECT DISTINCT stock_id AS stock_id
            FROM holdings
            WHERE TRIM(COALESCE(stock_id, '')) <> ''

            UNION

            SELECT DISTINCT stock_id AS stock_id
            FROM cash_dividends
            WHERE TRIM(COALESCE(stock_id, '')) <> ''

            UNION

            SELECT DISTINCT stock_id AS stock_id
            FROM stock_dividends
            WHERE TRIM(COALESCE(stock_id, '')) <> ''
        ) src
        ORDER BY stock_id
        """
    ).fetchall()
    return [normalize_stock_id(r["stock_id"]) for r in rows if normalize_stock_id(r["stock_id"])]


def _sync_dividends_for_stock(conn: sqlite3.Connection, sid: str, years: int = 2) -> Dict[str, Any]:
    sid = normalize_stock_id(sid)
    if not sid:
        raise ValueError("invalid stock id")

    min_tx_date_row = conn.execute(
        "SELECT MIN(date) AS min_date FROM transactions WHERE stock_id = ?",
        (sid,),
    ).fetchone()
    requested_years = max(1, int(years))
    current_roc_year = date.today().year - 1911
    start_roc_year = max(current_roc_year - requested_years + 1, 1)
    dynamic_years = requested_years
    if min_tx_date_row and min_tx_date_row["min_date"]:
        try:
            tx_year_ad = datetime.fromisoformat(str(min_tx_date_row["min_date"])).date().year
            tx_year_roc = tx_year_ad - 1911
            start_roc_year = max(1, min(start_roc_year, tx_year_roc))
            dynamic_years = max(dynamic_years, date.today().year - tx_year_ad + 1)
        except ValueError:
            pass

    twse_events = fetch_twse_dividend_list_events(sid, years=dynamic_years)
    yahoo_events = fetch_yahoo_dividend_events(sid, years=dynamic_years)
    mops_reduction_events = fetch_mops_capital_reduction_events(sid, start_roc_year, current_roc_year)
    cash_source_events = twse_events.get("cash") or []
    if not cash_source_events:
        cash_source_events = yahoo_events.get("cash") or []

    merged_stock_events: Dict[tuple, Dict[str, Any]] = {}
    for event in yahoo_events.get("stock") or []:
        ratio = float(event["ratio"])
        event_type = infer_stock_event_type(ratio, float(event.get("cash_return_per_share") or 0.0), event.get("event_type"))
        key = (event["ex_date"], round(float(event["ratio"]), 6))
        merged_stock_events[key] = {
            "ex_date": event["ex_date"],
            "allot_date": event.get("allot_date") or event["ex_date"],
            "ratio": ratio,
            "event_type": event_type,
            "cash_return_per_share": round(float(event.get("cash_return_per_share") or 0.0), 6),
            "source": str(event.get("source") or "yahoo_split_auto"),
        }

    for event in mops_reduction_events.get("stock") or []:
        ratio = float(event["ratio"])
        event_type = infer_stock_event_type(ratio, float(event.get("cash_return_per_share") or 0.0), event.get("event_type"))
        key = (event["ex_date"], round(float(event["ratio"]), 6))
        merged_stock_events[key] = {
            "ex_date": event["ex_date"],
            "allot_date": event.get("allot_date") or event["ex_date"],
            "ratio": ratio,
            "event_type": event_type,
            "cash_return_per_share": round(float(event.get("cash_return_per_share") or 0.0), 6),
            "source": str(event.get("source") or "mops_capital_reduction_auto"),
        }

    stock_source_events = sorted(merged_stock_events.values(), key=lambda x: (x["ex_date"], x["ratio"]))
    stock_inserted_cash = 0
    stock_inserted_stock = 0
    stock_updated_stock = 0

    for event in cash_source_events:
        ex_date = event["ex_date"]
        amount_per_share = float(event["amount_per_share"])
        pay_date = normalize_date(event.get("pay_date") or ex_date)
        source_name = str(event.get("source") or ("yahoo_auto" if event in (yahoo_events.get("cash") or []) else "twse_etfortune"))
        holding_shares = get_shares_on_date(conn, sid, ex_date)
        cash_amount = round(holding_shares * amount_per_share, 2)
        exists = conn.execute(
            """
            SELECT id, pay_date, source, holding_shares, cash_amount
            FROM cash_dividends
            WHERE stock_id = ? AND ex_date = ? AND ABS(amount_per_share - ?) < 0.000001
            LIMIT 1
            """,
            (sid, ex_date, amount_per_share),
        ).fetchone()
        if exists:
            existing_pay_date = normalize_date(exists["pay_date"] or ex_date)
            existing_source = str(exists["source"] or "")
            if holding_shares <= 1e-9:
                if existing_source != "manual":
                    conn.execute("DELETE FROM cash_dividends WHERE id = ?", (int(exists["id"]),))
                continue

            if (
                existing_source != "manual"
                and (
                    existing_pay_date != pay_date
                    or existing_source != source_name
                    or abs(float(exists["holding_shares"] or 0.0) - holding_shares) > 1e-9
                    or abs(float(exists["cash_amount"] or 0.0) - cash_amount) > 1e-6
                )
            ):
                conn.execute(
                    """
                    UPDATE cash_dividends
                    SET pay_date = ?, holding_shares = ?, cash_amount = ?, source = ?, note = ?
                    WHERE id = ?
                    """,
                    (
                        pay_date,
                        holding_shares,
                        cash_amount,
                        source_name,
                        "backfilled from official dividend source",
                        int(exists["id"]),
                    ),
                )
            continue

        if holding_shares <= 1e-9:
            continue

        conn.execute(
            """
            INSERT INTO cash_dividends (stock_id, ex_date, pay_date, amount_per_share, holding_shares, cash_amount, source, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sid, ex_date, pay_date, amount_per_share, holding_shares, cash_amount, source_name, "auto synced from official source" if source_name != "yahoo_auto" else "auto synced from market"),
        )
        stock_inserted_cash += 1

    # Keep official source as single truth when yahoo_auto has the same ex-date.
    duplicated_yahoo_rows = conn.execute(
        """
        SELECT y.id
        FROM cash_dividends y
        WHERE y.stock_id = ?
          AND y.source = 'yahoo_auto'
          AND EXISTS (
              SELECT 1
              FROM cash_dividends o
              WHERE o.stock_id = y.stock_id
                AND o.ex_date = y.ex_date
                AND o.id <> y.id
                AND o.source <> 'yahoo_auto'
          )
        """,
        (sid,),
    ).fetchall()
    for row in duplicated_yahoo_rows:
        conn.execute("DELETE FROM cash_dividends WHERE id = ?", (int(row["id"]),))

        # Treat near-date Yahoo vs official rows in the same year as duplicate representations
        # of the same dividend event (common one-day shift from source timezone differences).
        near_date_duplicated_yahoo_rows = conn.execute(
                """
                SELECT y.id
                FROM cash_dividends y
                WHERE y.stock_id = ?
                    AND y.source = 'yahoo_auto'
                    AND EXISTS (
                            SELECT 1
                            FROM cash_dividends o
                            WHERE o.stock_id = y.stock_id
                                AND o.id <> y.id
                                AND o.source <> 'yahoo_auto'
                                AND SUBSTR(o.ex_date, 1, 4) = SUBSTR(y.ex_date, 1, 4)
                                AND ABS(julianday(o.ex_date) - julianday(y.ex_date)) <= 10
                    )
                """,
                (sid,),
        ).fetchall()
        for row in near_date_duplicated_yahoo_rows:
                conn.execute("DELETE FROM cash_dividends WHERE id = ?", (int(row["id"]),))

        # Yahoo split/capital-reduction artifacts may appear as fake cash dividends on the same
        # ex-date as a negative stock event; remove those cash rows.
        reduction_artifact_rows = conn.execute(
                """
                SELECT y.id
                FROM cash_dividends y
                WHERE y.stock_id = ?
                    AND y.source = 'yahoo_auto'
                    AND EXISTS (
                            SELECT 1
                            FROM stock_dividends s
                            WHERE s.stock_id = y.stock_id
                                AND s.ex_date = y.ex_date
                                AND COALESCE(s.ratio, 0) < 0
                    )
                """,
                (sid,),
        ).fetchall()
        for row in reduction_artifact_rows:
            conn.execute("DELETE FROM cash_dividends WHERE id = ?", (int(row["id"]),))

    for event in stock_source_events:
        ex_date = event["ex_date"]
        allot_date = normalize_date(event.get("allot_date") or ex_date)
        ratio = float(event["ratio"])
        cash_return_per_share = round(float(event.get("cash_return_per_share") or 0.0), 6)
        event_type = infer_stock_event_type(ratio, cash_return_per_share, event.get("event_type"))
        source_name = str(event.get("source") or "yahoo_split_auto")
        exists = conn.execute(
            """
            SELECT id, allot_date, holding_shares, bonus_shares, event_type, cash_return_per_share, cash_return_amount, source
            FROM stock_dividends
            WHERE stock_id = ? AND ex_date = ? AND ABS(ratio - ?) < 0.000001
            LIMIT 1
            """,
            (sid, ex_date, ratio),
        ).fetchone()
        holding_shares = get_shares_on_date(
            conn,
            sid,
            ex_date,
            stock_dividend_before_tx=False,
            exclude_stock_dividend_id=int(exists["id"]) if exists else None,
        )
        if holding_shares <= 1e-9:
            if exists and str(exists["source"] or "") != "manual":
                conn.execute("DELETE FROM stock_dividends WHERE id = ?", (int(exists["id"]),))
                stock_updated_stock += 1
            continue

        settlement = compute_stock_event_settlement(holding_shares, ratio)
        base_holding_shares = float(settlement["base_shares"])
        bonus_shares = float(settlement["share_delta"])
        cash_return_amount = round(base_holding_shares * cash_return_per_share, 2) if event_type == "capital_reduction_cash" else 0.0
        if abs(bonus_shares) <= 1e-9:
            continue
        if bonus_shares < 0 and holding_shares + bonus_shares < 0:
            bonus_shares = -float(int(holding_shares))

        if exists:
            needs_update = (
                normalize_date(exists["allot_date"] or ex_date) != allot_date
                or abs(float(exists["holding_shares"] or 0.0) - base_holding_shares) > 1e-9
                or abs(float(exists["bonus_shares"] or 0.0) - bonus_shares) > 1e-9
                or str(exists["event_type"] or "stock_dividend") != event_type
                or abs(float(exists["cash_return_per_share"] or 0.0) - cash_return_per_share) > 1e-9
                or abs(float(exists["cash_return_amount"] or 0.0) - cash_return_amount) > 1e-6
                or str(exists["source"] or "") != source_name
            )
            if needs_update:
                conn.execute(
                    """
                    UPDATE stock_dividends
                    SET allot_date = ?, holding_shares = ?, bonus_shares = ?, event_type = ?, cash_return_per_share = ?, cash_return_amount = ?, source = ?, note = ?
                    WHERE id = ?
                    """,
                    (
                        allot_date,
                        base_holding_shares,
                        bonus_shares,
                        event_type,
                        cash_return_per_share,
                        cash_return_amount,
                        source_name,
                        "auto synced from market",
                        int(exists["id"]),
                    ),
                )
                stock_updated_stock += 1
            continue

        conn.execute(
            """
            INSERT INTO stock_dividends (stock_id, ex_date, allot_date, ratio, holding_shares, bonus_shares, event_type, cash_return_per_share, cash_return_amount, source, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sid,
                ex_date,
                allot_date,
                ratio,
                base_holding_shares,
                bonus_shares,
                event_type,
                cash_return_per_share,
                cash_return_amount,
                source_name,
                "auto synced from market",
            ),
        )
        stock_inserted_stock += 1

    return {
        "stock_id": sid,
        "source": (cash_source_events[0].get("source") if cash_source_events else (yahoo_events.get("source_symbol") or "unavailable")),
        "attempted_symbols": yahoo_events.get("attempted_symbols") or [],
        "fetched_cash_events": len(cash_source_events),
        "fetched_stock_events": len(stock_source_events),
        "inserted_cash_events": stock_inserted_cash,
        "inserted_stock_events": stock_inserted_stock,
        "updated_stock_events": stock_updated_stock,
    }


def _snapshot_cash_dividend_totals(conn: sqlite3.Connection, start_year: int) -> Dict[str, float]:
    start_date = f"{max(1900, int(start_year))}-01-01"
    rows = conn.execute(
        """
        SELECT stock_id, cash_amount
        FROM cash_dividends
        WHERE pay_date >= ?
        ORDER BY stock_id
        """,
        (start_date,),
    ).fetchall()
    nhi = get_nhi_settings(conn)
    totals: Dict[str, float] = {}
    for r in rows:
        sid = normalize_stock_id(r["stock_id"])
        if not sid:
            continue
        net_amount = compute_net_cash_dividend(float(r["cash_amount"] or 0.0), nhi_rate=nhi["rate"], nhi_threshold=nhi["threshold"])
        totals[sid] = round(totals.get(sid, 0.0) + net_amount, 2)
    return totals


def _build_cash_dividend_diff_report(before_map: Dict[str, float], after_map: Dict[str, float], conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    stock_ids = sorted(set(before_map.keys()) | set(after_map.keys()))
    if not stock_ids:
        return []

    placeholders = ",".join(["?"] * len(stock_ids))
    rows = conn.execute(
        f"SELECT stock_id, COALESCE(chinese_name, stock_id) AS chinese_name FROM stock_info WHERE stock_id IN ({placeholders})",
        stock_ids,
    ).fetchall()
    name_map = {normalize_stock_id(r["stock_id"]): (r["chinese_name"] or r["stock_id"]) for r in rows}

    result: List[Dict[str, Any]] = []
    for sid in stock_ids:
        before_v = float(before_map.get(sid, 0.0))
        after_v = float(after_map.get(sid, 0.0))
        delta = round(after_v - before_v, 2)
        if abs(delta) <= 0.0001:
            continue
        result.append(
            {
                "stock_id": sid,
                "chinese_name": name_map.get(sid, sid),
                "before_cash_dividend": round(before_v, 2),
                "after_cash_dividend": round(after_v, 2),
                "delta_cash_dividend": delta,
            }
        )

    result.sort(key=lambda x: abs(float(x["delta_cash_dividend"])), reverse=True)
    return result


def _set_dividend_recalc_job_state(job_id: str, patch: Dict[str, Any]) -> None:
    with _DIVIDEND_RECALC_JOBS_LOCK:
        current = _DIVIDEND_RECALC_JOBS.get(job_id, {})
        current.update(patch)
        _DIVIDEND_RECALC_JOBS[job_id] = current


def _get_dividend_recalc_job_state(job_id: str) -> Optional[Dict[str, Any]]:
    with _DIVIDEND_RECALC_JOBS_LOCK:
        item = _DIVIDEND_RECALC_JOBS.get(job_id)
        return dict(item) if item else None


def _run_dividend_recalc_job(job_id: str, years: int, start_year: int) -> None:
    started_at = datetime.now().isoformat(timespec="seconds")
    _set_dividend_recalc_job_state(job_id, {"status": "running", "started_at": started_at})

    try:
        with get_conn() as conn:
            before_map = _snapshot_cash_dividend_totals(conn, start_year=start_year)
            stock_ids = _list_dividend_sync_stock_ids(conn)

            total = len(stock_ids)
            inserted_cash = 0
            inserted_stock = 0
            updated_stock = 0
            succeeded: List[Dict[str, Any]] = []
            failed: List[Dict[str, Any]] = []
            stock_details: List[Dict[str, Any]] = []

            _set_dividend_recalc_job_state(
                job_id,
                {
                    "total_stocks": total,
                    "processed_stocks": 0,
                    "success_count": 0,
                    "failed_count": 0,
                    "current_stock_id": "",
                    "progress_pct": 0.0,
                },
            )

            for idx, sid in enumerate(stock_ids, start=1):
                state = _get_dividend_recalc_job_state(job_id) or {}
                if state.get("cancel_requested"):
                    _set_dividend_recalc_job_state(
                        job_id,
                        {
                            "status": "cancelled",
                            "finished_at": datetime.now().isoformat(timespec="seconds"),
                            "current_stock_id": "",
                            "processed_stocks": idx - 1,
                            "success_count": len(succeeded),
                            "failed_count": len(failed),
                            "progress_pct": round(((idx - 1) / total * 100.0), 2) if total else 100.0,
                            "cancelled": True,
                        },
                    )
                    conn.commit()
                    return

                _set_dividend_recalc_job_state(
                    job_id,
                    {
                        "current_stock_id": sid,
                        "processed_stocks": idx - 1,
                        "progress_pct": round(((idx - 1) / total * 100.0), 2) if total else 100.0,
                    },
                )

                try:
                    one = _sync_dividends_for_stock(conn, sid, years=years)
                    inserted_cash += int(one.get("inserted_cash_events", 0))
                    inserted_stock += int(one.get("inserted_stock_events", 0))
                    updated_stock += int(one.get("updated_stock_events", 0))
                    stock_details.append(one)
                    succeeded.append(
                        {
                            "stock_id": sid,
                            "inserted_cash_events": int(one.get("inserted_cash_events", 0)),
                            "inserted_stock_events": int(one.get("inserted_stock_events", 0)),
                            "updated_stock_events": int(one.get("updated_stock_events", 0)),
                        }
                    )
                except Exception as exc:
                    failed.append({"stock_id": sid, "error": str(exc)})

                # Frequent commits avoid long transaction locks and keep progress deterministic.
                conn.commit()
                _set_dividend_recalc_job_state(
                    job_id,
                    {
                        "processed_stocks": idx,
                        "success_count": len(succeeded),
                        "failed_count": len(failed),
                        "progress_pct": round((idx / total * 100.0), 2) if total else 100.0,
                        "recent_failed": failed[-20:],
                    },
                )

            if inserted_stock > 0 or updated_stock > 0:
                rebuild_holdings_and_realized(conn)
                conn.commit()

            after_map = _snapshot_cash_dividend_totals(conn, start_year=start_year)
            diff_rows = _build_cash_dividend_diff_report(before_map, after_map, conn)
            finished_at = datetime.now().isoformat(timespec="seconds")
            duration_sec = round(max(0.0, time.time() - datetime.fromisoformat(started_at).timestamp()), 2)

            write_audit_log(
                conn,
                event_type="dividend_recalc_job",
                payload={
                    "job_id": job_id,
                    "years": int(years),
                    "start_year": int(start_year),
                    "processed_stocks": total,
                    "success_count": len(succeeded),
                    "failed_count": len(failed),
                    "inserted_cash_dividends": inserted_cash,
                    "inserted_stock_dividends": inserted_stock,
                    "updated_stock_dividends": updated_stock,
                    "diff_changed_count": len(diff_rows),
                },
                severity="INFO",
                actor="api",
            )
            conn.commit()

            _set_dividend_recalc_job_state(
                job_id,
                {
                    "status": "completed",
                    "finished_at": finished_at,
                    "duration_sec": duration_sec,
                    "processed_stocks": total,
                    "total_stocks": total,
                    "current_stock_id": "",
                    "progress_pct": 100.0,
                    "success_count": len(succeeded),
                    "failed_count": len(failed),
                    "inserted_cash_dividends": inserted_cash,
                    "inserted_stock_dividends": inserted_stock,
                    "updated_stock_dividends": updated_stock,
                    "failed_stocks": failed,
                    "stock_details": stock_details,
                    "diff_report": {
                        "start_year": int(start_year),
                        "changed_count": len(diff_rows),
                        "top_changes": diff_rows[:100],
                    },
                },
            )
    except Exception as exc:
        _set_dividend_recalc_job_state(
            job_id,
            {
                "status": "failed",
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "error": str(exc),
            },
        )


def parse_market_price_token(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    token = str(raw).strip()
    if not token or token == "-":
        return None
    token = token.split("_")[0].replace(",", "")
    return parse_quote_price(token)


def fetch_twse_realtime_quote(stock_id: str) -> Dict[str, Any]:
    sid = normalize_stock_id(stock_id)
    if not sid:
        return {"close_price": None, "chinese_name": None, "source": "invalid_stock_id"}

    code_candidates: List[str] = [sid]
    base_sid = sid.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ") or sid
    if base_sid != sid:
        code_candidates.append(base_sid)

    for code in code_candidates:
        ex_list = [f"tse_{code}.tw", f"otc_{code}.tw"]
        url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?json=1&delay=0&ex_ch=" + urllib.parse.quote("|".join(ex_list))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8, context=ssl._create_unverified_context()) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ssl.SSLError, OSError):
            continue

        msg_arr = payload.get("msgArray", []) if isinstance(payload, dict) else []
        for row in msg_arr:
            price = parse_market_price_token(row.get("z"))
            if price is None:
                price = parse_market_price_token(row.get("y"))
            if price is None:
                continue
            return {
                "close_price": price,
                "chinese_name": (row.get("n") or "").strip() or None,
                "source": "twse_realtime",
            }

    return {"close_price": None, "chinese_name": None, "source": "twse_realtime_unavailable"}


def fetch_stooq_quote(stock_id: str) -> Dict[str, Any]:
    sid = normalize_stock_id(stock_id)
    if not sid:
        return {"close_price": None, "chinese_name": None, "source": "invalid_stock_id"}

    symbols = [f"{sid.lower()}.tw", f"{sid.lower()}.two"]
    for symbol in symbols:
        url = f"https://stooq.com/q/l/?s={urllib.parse.quote(symbol)}&f=sd2t2ohlcvn&e=csv"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv"})
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
        except (urllib.error.URLError, TimeoutError):
            continue

        lines = [x.strip() for x in raw.splitlines() if x.strip()]
        if len(lines) < 2:
            continue

        parts = [p.strip() for p in lines[1].split(",")]
        if len(parts) < 7:
            continue

        close_price = parse_quote_price(parts[6])
        if close_price is None:
            continue

        cname = parts[8] if len(parts) > 8 else None
        cname = (cname or "").strip() or None
        return {
            "close_price": close_price,
            "chinese_name": cname,
            "source": "stooq_csv",
        }

    return {"close_price": None, "chinese_name": None, "source": "stooq_unavailable"}


def fetch_latest_quote(stock_id: str) -> Dict[str, Any]:
    sid = normalize_stock_id(stock_id)
    if not sid:
        return {"stock_id": sid, "chinese_name": None, "close_price": None, "source": "invalid_stock_id"}

    base_sid = sid.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ") or sid
    symbol_candidates = [sid]
    if base_sid != sid:
        symbol_candidates.append(base_sid)
    symbols = get_yahoo_symbol_candidates(sid)

    url = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=" + urllib.parse.quote(",".join(symbols))

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        payload = {"quoteResponse": {"result": []}}

    results = payload.get("quoteResponse", {}).get("result", [])
    close_price = None
    chinese_name = None
    source = "quote_unavailable"

    if results:
        best = results[0]
        for q in results:
            prev_close = parse_quote_price(q.get("regularMarketPreviousClose"))
            market_price = parse_quote_price(q.get("regularMarketPrice"))
            if prev_close is not None or market_price is not None:
                best = q
                break

        # Prefer market price to better reflect current quote, fallback to previous close.
        close_price = parse_quote_price(best.get("regularMarketPrice"))
        if close_price is None:
            close_price = parse_quote_price(best.get("regularMarketPreviousClose"))
        chinese_name = (best.get("shortName") or best.get("longName") or "").strip() or None
        if close_price is not None:
            source = "yahoo_quote"

    if close_price is None:
        twse_rt = fetch_twse_realtime_quote(sid)
        if twse_rt.get("close_price") is not None:
            close_price = float(twse_rt["close_price"])
            if not chinese_name:
                chinese_name = twse_rt.get("chinese_name")
            source = str(twse_rt.get("source") or "twse_realtime")

    if close_price is None:
        for symbol in symbols:
            chart_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?interval=1d&range=1mo"
            chart_req = urllib.request.Request(chart_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            try:
                with urllib.request.urlopen(chart_req, timeout=8) as resp:
                    chart_payload = json.loads(resp.read().decode("utf-8"))
                chart_result = chart_payload.get("chart", {}).get("result", [])
                if not chart_result:
                    continue
                closes = chart_result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
                valid = [parse_quote_price(v) for v in closes]
                valid = [v for v in valid if v is not None]
                if valid:
                    close_price = valid[-1]
                    source = "yahoo_chart"
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, TypeError):
                continue

    if close_price is None:
        for code in symbol_candidates:
            twse_url = (
                "https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json"
                f"&date={date.today().strftime('%Y%m%d')}&stockNo={urllib.parse.quote(code)}"
            )
            twse_req = urllib.request.Request(twse_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            try:
                with urllib.request.urlopen(twse_req, timeout=8, context=ssl._create_unverified_context()) as resp:
                    twse_payload = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, TypeError, ssl.SSLError, OSError):
                continue
            try:
                data = twse_payload.get("data", [])
                if data:
                    closes = [parse_quote_price(row[6] if len(row) > 6 else None) for row in data]
                    closes = [v for v in closes if v is not None]
                    if closes:
                        close_price = closes[-1]
                        source = "twse_openapi"
                        break
            except (KeyError, IndexError, TypeError):
                continue

    if close_price is None:
        stooq = fetch_stooq_quote(sid)
        if stooq.get("close_price") is not None:
            close_price = float(stooq["close_price"])
            if not chinese_name:
                chinese_name = stooq.get("chinese_name")
            source = str(stooq.get("source") or "stooq_csv")

    if not chinese_name:
        chinese_name = get_local_stock_name(sid)

    return {
        "stock_id": sid,
        "chinese_name": chinese_name,
        "close_price": close_price,
        "source": source,
    }


def upsert_stock_quote(conn: sqlite3.Connection, stock_id: str, chinese_name: Optional[str], close_price: Optional[float]) -> None:
    sid = normalize_stock_id(stock_id)
    if not sid:
        return

    ensure_stock_info(conn, sid)

    updates: List[str] = []
    params: List[Any] = []

    if chinese_name:
        updates.append("chinese_name = ?")
        params.append(chinese_name)
    if close_price is not None:
        updates.append("current_price = ?")
        params.append(close_price)

    if not updates:
        return

    params.append(sid)
    conn.execute(f"UPDATE stock_info SET {', '.join(updates)} WHERE stock_id = ?", params)


def get_stock_market_value(conn: sqlite3.Connection, stock_id: str) -> float:
    h = conn.execute("SELECT shares FROM holdings WHERE stock_id = ?", (stock_id,)).fetchone()
    if not h:
        return 0.0
    shares = float(h[0])
    return shares * get_latest_price(conn, stock_id)


def build_missing_price_list(conn: sqlite3.Connection, limit: int = 500) -> List[Dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 5000))
    rows = conn.execute(
        """
        SELECT
            h.stock_id,
            COALESCE(s.chinese_name, h.stock_id) AS chinese_name,
            COALESCE(h.shares, 0) AS shares,
            COALESCE(h.total_cost, 0) AS total_cost,
            COALESCE(s.current_price, 0) AS current_price,
            (
                SELECT t.price
                FROM transactions t
                WHERE t.stock_id = h.stock_id
                ORDER BY t.date DESC, t.id DESC
                LIMIT 1
            ) AS last_trade_price
        FROM holdings h
        LEFT JOIN stock_info s ON s.stock_id = h.stock_id
        WHERE COALESCE(h.shares, 0) > 0
          AND COALESCE(s.current_price, 0) <= 0
        ORDER BY h.stock_id
        LIMIT ?
        """,
        (safe_limit,),
    ).fetchall()

    items: List[Dict[str, Any]] = []
    for r in rows:
        items.append(
            {
                "stock_id": normalize_stock_id(r["stock_id"]),
                "chinese_name": (r["chinese_name"] or r["stock_id"]),
                "shares": float(r["shares"]),
                "total_cost": float(r["total_cost"]),
                "current_price": float(r["current_price"]),
                "last_trade_price": float(r["last_trade_price"]) if r["last_trade_price"] is not None else None,
            }
        )
    return items


def refresh_missing_prices(conn: sqlite3.Connection, limit: int = 500) -> Dict[str, Any]:
    missing_items = build_missing_price_list(conn, limit=limit)
    refreshed: List[Dict[str, Any]] = []
    updated_count = 0

    for item in missing_items:
        sid = normalize_stock_id(item["stock_id"])
        quote = fetch_latest_quote(sid)
        close_price = quote.get("close_price")
        source = str(quote.get("source") or "unknown")
        updated_price: Optional[float] = None

        if close_price is not None and float(close_price) > 0:
            updated_price = float(close_price)
            upsert_stock_quote(conn, sid, quote.get("chinese_name"), updated_price)
            updated_count += 1
        else:
            last_trade_price = item.get("last_trade_price")
            if last_trade_price is not None and float(last_trade_price) > 0:
                updated_price = float(last_trade_price)
                ensure_stock_info(conn, sid)
                conn.execute(
                    "UPDATE stock_info SET current_price = ? WHERE stock_id = ?",
                    (updated_price, sid),
                )
                updated_count += 1
                source = "last_trade_fallback"

        row = conn.execute(
            "SELECT COALESCE(current_price, 0) AS current_price, COALESCE(chinese_name, '') AS chinese_name FROM stock_info WHERE stock_id = ?",
            (sid,),
        ).fetchone()
        final_price = float(row["current_price"]) if row else 0.0
        final_name = (row["chinese_name"] if row and row["chinese_name"] else item["chinese_name"]) or sid
        refreshed.append(
            {
                "stock_id": sid,
                "chinese_name": final_name,
                "before_price": float(item["current_price"]),
                "after_price": final_price,
                "last_trade_price": item.get("last_trade_price"),
                "source": source,
                "updated": final_price > 0,
            }
        )

    still_missing = sum(1 for x in refreshed if not x["updated"])
    return {
        "checked": len(missing_items),
        "updated": updated_count,
        "still_missing": still_missing,
        "items": refreshed,
    }


def refresh_market_prices(
    conn: sqlite3.Connection,
    limit: int = 500,
    scope: str = "transactions",
    stock_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    safe_limit = max(1, min(limit, 5000))
    scope_key = (scope or "transactions").strip().lower()

    explicit_stock_ids = [normalize_stock_id(sid) for sid in (stock_ids or []) if normalize_stock_id(sid)]
    if explicit_stock_ids:
        rows = [{"stock_id": sid} for sid in dict.fromkeys(explicit_stock_ids)]
    elif scope_key == "holdings":
        rows = conn.execute(
            """
            SELECT DISTINCT stock_id
            FROM holdings
            WHERE COALESCE(shares, 0) > 0
            ORDER BY stock_id
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT DISTINCT stock_id
            FROM transactions
            ORDER BY stock_id
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()

    stock_ids = [normalize_stock_id(r["stock_id"]) for r in rows if normalize_stock_id(r["stock_id"])]

    updated = 0
    items: List[Dict[str, Any]] = []
    for sid in stock_ids:
        quote = fetch_latest_quote(sid)
        close_price = parse_quote_price(quote.get("close_price"))
        source = str(quote.get("source") or "unknown")

        if close_price is not None and close_price > 0:
            upsert_stock_quote(conn, sid, quote.get("chinese_name"), float(close_price))
            updated += 1

        row = conn.execute(
            "SELECT COALESCE(chinese_name, '') AS chinese_name, COALESCE(current_price, 0) AS current_price FROM stock_info WHERE stock_id = ?",
            (sid,),
        ).fetchone()
        items.append(
            {
                "stock_id": sid,
                "chinese_name": (row["chinese_name"] if row and row["chinese_name"] else sid),
                "current_price": float(row["current_price"]) if row else 0.0,
                "source": source,
                "updated": close_price is not None and close_price > 0,
            }
        )

    failed = sum(1 for x in items if not x["updated"])
    return {
        "scope": "explicit" if explicit_stock_ids else scope_key,
        "checked": len(stock_ids),
        "updated": updated,
        "failed": failed,
        "items": items,
    }


def rebuild_holdings_and_realized(conn: sqlite3.Connection) -> None:
    tx_rows = conn.execute(
        "SELECT id, date, stock_id, action, shares, price, fees, transaction_tax FROM transactions ORDER BY date, id"
    ).fetchall()
    div_rows = conn.execute(
        "SELECT id, allot_date, stock_id, bonus_shares, event_type, cash_return_amount FROM stock_dividends ORDER BY allot_date, id"
    ).fetchall()

    events: List[Dict[str, Any]] = []
    for r in tx_rows:
        events.append({"kind": "tx", "date": r["date"], "id": int(r["id"]), "row": r})
    for r in div_rows:
        events.append({"kind": "stock_div", "date": r["allot_date"], "id": int(r["id"]), "row": r})
    events.sort(key=lambda e: (e["date"], 0 if e["kind"] == "stock_div" else 1, e["id"]))

    lots_by_stock: Dict[str, deque] = defaultdict(deque)
    tax_settings = get_transaction_tax_settings(conn)
    conn.execute("DELETE FROM holdings")

    for event in events:
        if event["kind"] == "stock_div":
            r = event["row"]
            sid = normalize_stock_id(r["stock_id"])
            bonus_shares = float(r["bonus_shares"])
            apply_stock_event_to_lots(
                lots_by_stock[sid],
                bonus_shares,
                event_type=str(r["event_type"] or "stock_dividend"),
                cash_return_amount=float(r["cash_return_amount"] or 0.0),
            )
            continue

        r = event["row"]
        sid = normalize_stock_id(r["stock_id"])
        ensure_stock_info(conn, sid)

        action = r["action"]
        shares = float(r["shares"])
        price = float(r["price"])
        fees = float(r["fees"])
        transaction_tax = float(r["transaction_tax"])
        expected_tax = calculate_transaction_tax(sid, action, shares, price, tax_settings)
        if abs(transaction_tax - expected_tax) > 1e-9:
            transaction_tax = expected_tax
            conn.execute("UPDATE transactions SET transaction_tax = ? WHERE id = ?", (transaction_tax, int(r["id"])))
        realized = 0.0

        if action == "buy":
            unit_cost = ((shares * price) + fees) / shares if shares > 0 else 0.0
            lots_by_stock[sid].append(
                {
                    "buy_tx_id": int(r["id"]),
                    "shares": shares,
                    "unit_cost": unit_cost,
                }
            )
        else:
            available = sum(lot["shares"] for lot in lots_by_stock[sid])
            sell_shares = min(shares, available)
            if sell_shares <= 1e-9:
                conn.execute("UPDATE transactions SET realized_profit = ? WHERE id = ?", (0.0, int(r["id"])))
                continue

            remaining = sell_shares
            fifo_cost = 0.0
            while remaining > 1e-9 and lots_by_stock[sid]:
                lot = lots_by_stock[sid][0]
                take = min(remaining, lot["shares"])
                fifo_cost += take * lot["unit_cost"]
                lot["shares"] -= take
                remaining -= take
                if lot["shares"] <= 1e-9:
                    lots_by_stock[sid].popleft()

            ratio = sell_shares / shares if shares > 0 else 0.0
            proceeds = sell_shares * price - (fees * ratio) - (transaction_tax * ratio)
            realized = proceeds - fifo_cost

        conn.execute("UPDATE transactions SET realized_profit = ? WHERE id = ?", (round(realized, 6), int(r["id"])))

    for sid, lots in lots_by_stock.items():
        remaining_shares = sum(lot["shares"] for lot in lots)
        total_cost = sum(lot["shares"] * lot["unit_cost"] for lot in lots)
        if remaining_shares <= 1e-9:
            continue

        conn.execute(
            """
            INSERT INTO holdings (stock_id, shares, total_cost)
            VALUES (?, ?, ?)
            ON CONFLICT(stock_id) DO UPDATE SET
                shares=excluded.shares,
                total_cost=excluded.total_cost
            """,
            (sid, remaining_shares, max(total_cost, 0.0)),
        )


def build_fifo_transaction_metrics(conn: sqlite3.Connection) -> Dict[int, Dict[str, Any]]:
    tx_rows = conn.execute(
        "SELECT id, date, stock_id, action, shares, price, fees, transaction_tax FROM transactions ORDER BY date, id"
    ).fetchall()
    div_rows = conn.execute(
        "SELECT id, allot_date, stock_id, bonus_shares, event_type, cash_return_amount FROM stock_dividends ORDER BY allot_date, id"
    ).fetchall()

    events: List[Dict[str, Any]] = []
    for r in tx_rows:
        events.append({"kind": "tx", "date": r["date"], "id": int(r["id"]), "row": r})
    for r in div_rows:
        events.append({"kind": "stock_div", "date": r["allot_date"], "id": int(r["id"]), "row": r})
    events.sort(key=lambda e: (e["date"], 0 if e["kind"] == "stock_div" else 1, e["id"]))
    price_rows = conn.execute("SELECT stock_id, current_price FROM stock_info").fetchall()
    current_price_map = {normalize_stock_id(r["stock_id"]): float(r["current_price"] or 0.0) for r in price_rows}
    tax_settings = get_transaction_tax_settings(conn)

    lots_by_stock: Dict[str, deque] = defaultdict(deque)
    realized_totals: Dict[str, float] = defaultdict(float)
    metrics_by_tx: Dict[int, Dict[str, Any]] = {}

    for event in events:
        if event["kind"] == "stock_div":
            r = event["row"]
            sid = normalize_stock_id(r["stock_id"])
            bonus_shares = float(r["bonus_shares"])
            apply_stock_event_to_lots(
                lots_by_stock[sid],
                bonus_shares,
                event_type=str(r["event_type"] or "stock_dividend"),
                cash_return_amount=float(r["cash_return_amount"] or 0.0),
            )
            continue

        r = event["row"]
        tx_id = int(r["id"])
        sid = normalize_stock_id(r["stock_id"])
        action = r["action"]
        shares = float(r["shares"])
        price = float(r["price"])
        fees = float(r["fees"])
        transaction_tax = calculate_transaction_tax(sid, action, shares, price, tax_settings)
        fifo_cost = 0.0

        realized = 0.0
        if action == "buy":
            unit_cost = ((shares * price) + fees) / shares if shares > 0 else 0.0
            lots_by_stock[sid].append(
                {
                    "buy_tx_id": tx_id,
                    "shares": shares,
                    "unit_cost": unit_cost,
                }
            )
        else:
            available = sum(lot["shares"] for lot in lots_by_stock[sid])
            sell_shares = min(shares, available)
            if sell_shares <= 1e-9:
                metrics_by_tx[tx_id] = {
                    "inventory_after": round(sum(lot["shares"] for lot in lots_by_stock[sid]), 4),
                    "realized_total": round(realized_totals[sid], 2),
                    "unrealized_profit_tx": None,
                    "return_rate_tx": None,
                }
                continue

            remaining = sell_shares
            while remaining > 1e-9 and lots_by_stock[sid]:
                lot = lots_by_stock[sid][0]
                take = min(remaining, lot["shares"])
                fifo_cost += take * lot["unit_cost"]
                lot["shares"] -= take
                remaining -= take
                if lot["shares"] <= 1e-9:
                    lots_by_stock[sid].popleft()
            ratio = sell_shares / shares if shares > 0 else 0.0
            proceeds = sell_shares * price - (fees * ratio) - (transaction_tax * ratio)
            realized = proceeds - fifo_cost

        rate = None
        if action == "sell" and fifo_cost > 0:
            rate = (realized / fifo_cost) * 100.0

        realized_totals[sid] += realized
        inventory_after = sum(lot["shares"] for lot in lots_by_stock[sid])
        metrics_by_tx[tx_id] = {
            "inventory_after": round(inventory_after, 4),
            "realized_total": round(realized_totals[sid], 2),
            "unrealized_profit_tx": None,
            "return_rate_tx": round(rate, 4) if rate is not None else None,
        }

    for sid, lots in lots_by_stock.items():
        current_price = current_price_map.get(sid, 0.0)
        if current_price <= 0:
            continue

        for lot in lots:
            remaining_shares = float(lot["shares"])
            if remaining_shares <= 1e-9:
                continue
            remaining_cost = remaining_shares * float(lot["unit_cost"])
            unrealized = remaining_shares * current_price - remaining_cost
            return_rate = (unrealized / remaining_cost * 100.0) if remaining_cost > 0 else None

            tx_id = lot.get("buy_tx_id")
            if tx_id is None or tx_id not in metrics_by_tx:
                continue
            prev_unrealized = metrics_by_tx[tx_id].get("unrealized_profit_tx")
            if prev_unrealized is None:
                metrics_by_tx[tx_id]["unrealized_profit_tx"] = round(unrealized, 2)
                metrics_by_tx[tx_id]["return_rate_tx"] = round(return_rate, 4) if return_rate is not None else None
            else:
                metrics_by_tx[tx_id]["unrealized_profit_tx"] = round(float(prev_unrealized) + unrealized, 2)
                metrics_by_tx[tx_id]["return_rate_tx"] = None

    return metrics_by_tx


def portfolio_summary_data(conn: sqlite3.Connection) -> Dict[str, float]:
    rows = conn.execute(
        """
        SELECT h.stock_id, h.shares, h.total_cost, COALESCE(s.current_price, 0) AS current_price
        FROM holdings h
        LEFT JOIN stock_info s ON s.stock_id = h.stock_id
        WHERE h.shares > 0
        """
    ).fetchall()
    total_assets = 0.0
    for r in rows:
        stock_id = r["stock_id"]
        shares = float(r["shares"])
        price = float(r["current_price"] or 0)
        if price <= 0:
            quote = fetch_latest_quote(stock_id)
            if quote.get("close_price") is not None:
                price = float(quote["close_price"])
                upsert_stock_quote(conn, stock_id, quote.get("chinese_name"), price)
        market_value = shares * price
        # 若無價格資料，退回成本法
        if market_value <= 0:
            market_value = float(r["total_cost"])
        total_assets += market_value

    debt = conn.execute("SELECT COALESCE(SUM(principal), 0) AS v FROM loans").fetchone()["v"]
    total_liabilities = float(debt)
    net_assets = total_assets - total_liabilities

    return {
        "total_assets": round(total_assets, 2),
        "total_liabilities": round(total_liabilities, 2),
        "net_assets": round(net_assets, 2),
    }


def portfolio_performance_data(conn: sqlite3.Connection) -> Dict[str, Any]:
    rows = conn.execute("SELECT stock_id, chinese_name, current_price FROM stock_info ORDER BY stock_id").fetchall()
    items = []

    for row in rows:
        stock_id = row["stock_id"]
        chinese_name = row["chinese_name"] or stock_id
        current_price = float(row["current_price"]) if row["current_price"] else 0.0
        
        h = conn.execute(
            "SELECT COALESCE(shares, 0) AS shares, COALESCE(total_cost, 0) AS total_cost FROM holdings WHERE stock_id = ?",
            (stock_id,),
        ).fetchone()
        shares = float(h["shares"]) if h else 0.0
        total_cost = float(h["total_cost"]) if h else 0.0
        if current_price <= 0 and shares > 0:
            quote = fetch_latest_quote(stock_id)
            if quote.get("close_price") is not None:
                current_price = float(quote["close_price"])
                upsert_stock_quote(conn, stock_id, quote.get("chinese_name"), current_price)
        market_value = shares * current_price

        realized = conn.execute(
            "SELECT COALESCE(SUM(realized_profit), 0) AS v FROM transactions WHERE stock_id = ?",
            (stock_id,),
        ).fetchone()["v"]
        dividend = get_cash_dividend_sum(conn, stock_id)
        bonus_shares = get_bonus_shares_sum(conn, stock_id)

        unrealized = market_value - total_cost
        avg_cost_per_share = (total_cost / shares) if shares > 1e-9 else 0.0
        realized_with_dividends = float(realized) + float(dividend)
        total_profit_including_dividends = realized_with_dividends + unrealized

        items.append(
            {
                "stock_id": stock_id,
                "chinese_name": chinese_name,
                "current_price": round(float(current_price), 2),
                "avg_cost_per_share": round(float(avg_cost_per_share), 4),
                "realized_profit": round(float(realized), 2),
                "realized_with_dividends": round(float(realized_with_dividends), 2),
                "dividends_received": round(float(dividend), 2),
                "bonus_shares_received": round(float(bonus_shares), 4),
                "market_value": round(float(market_value), 2),
                "unrealized_profit": round(float(unrealized), 2),
                "total_profit_including_dividends": round(float(total_profit_including_dividends), 2),
                "shares": round(float(shares), 4),
            }
        )

    return {
        "items": items,
        "totals": {
            "realized_profit": round(sum(i["realized_profit"] for i in items), 2),
            "realized_with_dividends": round(sum(i["realized_with_dividends"] for i in items), 2),
            "dividends_received": round(sum(i["dividends_received"] for i in items), 2),
            "bonus_shares_received": round(sum(i["bonus_shares_received"] for i in items), 4),
            "market_value": round(sum(i["market_value"] for i in items), 2),
            "unrealized_profit": round(sum(i["unrealized_profit"] for i in items), 2),
            "total_profit_including_dividends": round(sum(i["total_profit_including_dividends"] for i in items), 2),
        },
    }


def portfolio_allocation_data(conn: sqlite3.Connection) -> Dict[str, List[Dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT s.stock_id, s.asset_type, s.sector, COALESCE(h.shares,0) AS shares, COALESCE(s.current_price, 0) AS current_price
        FROM stock_info s
        LEFT JOIN holdings h ON h.stock_id = s.stock_id
        """
    ).fetchall()

    asset_type_map: Dict[str, float] = {}
    sector_map: Dict[str, float] = {}

    for row in rows:
        stock_id = row["stock_id"]
        current_price = float(row["current_price"] or 0)
        if current_price <= 0 and float(row["shares"]) > 0:
            quote = fetch_latest_quote(stock_id)
            if quote.get("close_price") is not None:
                current_price = float(quote["close_price"])
                upsert_stock_quote(conn, stock_id, quote.get("chinese_name"), current_price)
        value = float(row["shares"]) * current_price
        if value <= 0:
            # 缺價時避免全為0，改採成本法
            h = conn.execute("SELECT total_cost FROM holdings WHERE stock_id = ?", (stock_id,)).fetchone()
            value = float(h[0]) if h else 0.0

        asset_type = row["asset_type"] or "其他"
        sector = row["sector"] or "其他"
        asset_type_map[asset_type] = asset_type_map.get(asset_type, 0.0) + value
        sector_map[sector] = sector_map.get(sector, 0.0) + value

    asset_type_list = [{"name": k, "value": round(v, 2)} for k, v in sorted(asset_type_map.items()) if v > 0]
    sector_list = [{"name": k, "value": round(v, 2)} for k, v in sorted(sector_map.items()) if v > 0]

    return {
        "asset_type": asset_type_list,
        "sector": sector_list,
    }


def expected_dividends_data(conn: sqlite3.Connection) -> Dict[str, Any]:
    today = date.today()
    year_start = date(today.year, 1, 1).isoformat()
    year_end = date(today.year, 12, 31).isoformat()
    default_yield = {
        "個股": 0.03,
        "ETF": 0.045,
        "債券": 0.04,
    }

    rows = conn.execute(
        """
        SELECT s.stock_id, s.asset_type, COALESCE(h.shares,0) AS shares
        FROM stock_info s
        LEFT JOIN holdings h ON h.stock_id = s.stock_id
        """
    ).fetchall()

    result = []
    breakdown = {
        "current_year_schedule_total": 0.0,
        "yield_estimate_total": 0.0,
    }
    nhi = get_nhi_settings(conn)
    for row in rows:
        stock_id = row["stock_id"]
        shares = float(row["shares"])
        if shares <= 0:
            continue

        market_value = shares * get_latest_price(conn, stock_id)
        current_year_rows = conn.execute(
            """
            SELECT cash_amount
            FROM cash_dividends
            WHERE stock_id = ? AND pay_date >= ? AND pay_date <= ?
            """,
            (stock_id, year_start, year_end),
        ).fetchall()
        current_year_estimate = sum(
            compute_net_cash_dividend(float(r["cash_amount"] or 0.0), nhi_rate=nhi["rate"], nhi_threshold=nhi["threshold"])
            for r in current_year_rows
        )

        y = default_yield.get(row["asset_type"], 0.03)
        estimate = float(current_year_estimate)
        if estimate <= 0:
            estimate = market_value * y

        method = "current_year_schedule" if float(current_year_estimate) > 0 else "yield_estimate"
        if method == "current_year_schedule":
            breakdown["current_year_schedule_total"] += float(estimate)
        else:
            breakdown["yield_estimate_total"] += float(estimate)

        result.append(
            {
                "stock_id": stock_id,
                "expected_dividend": round(estimate, 2),
                "method": method,
            }
        )

    total = round(sum(r["expected_dividend"] for r in result), 2)
    return {
        "items": result,
        "total_expected_dividend": total,
        "breakdown": {
            "current_year_schedule_total": round(float(breakdown["current_year_schedule_total"]), 2),
            "yield_estimate_total": round(float(breakdown["yield_estimate_total"]), 2),
        },
    }


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
        stock_row = conn.execute("SELECT current_price FROM stock_info WHERE stock_id = ?", (collateral_sid,)).fetchone()
        current_price = float(stock_row["current_price"] or 0) if stock_row else 0.0

        if collateral_lots > 0 and current_price > 0:
            collateral_value = collateral_lots * 1000 * current_price
        else:
            collateral_value = get_stock_market_value(conn, collateral_sid)

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
        if maintenance_rate < 130:
            status = "危險"
        elif maintenance_rate < 167:
            status = "警戒"
        elif maintenance_rate > 200:
            status = "安全"
        else:
            status = "注意"

    return {
        "maintenance_rate": round(maintenance_rate, 2) if maintenance_rate is not None else None,
        "status": status,
        "total_principal": round(total_principal, 2),
        "total_collateral_value": round(total_collateral, 2),
        "total_accrued_interest": round(total_interest, 2),
        "loans": loan_items,
    }


def build_advisor_snapshot(conn: sqlite3.Connection) -> Dict[str, Any]:
    return {
        "summary": portfolio_summary_data(conn),
        "performance": portfolio_performance_data(conn),
        "allocation": portfolio_allocation_data(conn),
        "expected_dividends": expected_dividends_data(conn),
        "loans_health": loans_health_data(conn),
    }


def call_openai(question: str, snapshot: Dict[str, Any]) -> Optional[str]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    system_prompt = (
        "你是家族辦公室投資顧問，請根據提供的投資快照，提出具體、可執行、風險分級的資產配置與再平衡建議。"
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"投資快照JSON:\n{json.dumps(snapshot, ensure_ascii=False)}\n\n問題:\n{question}",
            },
        ],
        "temperature": 0.2,
    }

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
    except Exception:
        return None


def call_anthropic(question: str, snapshot: Dict[str, Any]) -> Optional[str]:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    model = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
    system_prompt = "你是家族辦公室投資顧問，請用條列與風險分級回答。"

    payload = {
        "model": model,
        "max_tokens": 900,
        "temperature": 0.2,
        "system": system_prompt,
        "messages": [
            {
                "role": "user",
                "content": f"投資快照JSON:\n{json.dumps(snapshot, ensure_ascii=False)}\n\n問題:\n{question}",
            }
        ],
    }

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return "\n".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None


def local_rule_based_advice(question: str, snapshot: Dict[str, Any]) -> str:
    summary = snapshot["summary"]
    loans = snapshot["loans_health"]
    allocation = snapshot["allocation"]

    top_asset_types = sorted(allocation["asset_type"], key=lambda x: x["value"], reverse=True)
    concentration = top_asset_types[0]["name"] if top_asset_types else "未知"

    lines = [
        f"問題：{question}",
        f"目前淨資產約 {summary['net_assets']:.2f}，總資產 {summary['total_assets']:.2f}，總負債 {summary['total_liabilities']:.2f}。",
        f"目前最大資產類別集中於「{concentration}」。",
    ]

    mr = loans.get("maintenance_rate")
    if mr is not None:
        if mr < 167:
            lines.append("建議優先降低槓桿：減少高波動持倉或提前償還部分借款，將維持率拉回 200% 以上。")
        else:
            lines.append("槓桿風險目前可控，可採分批再平衡，不建議一次性大幅調整。")

    lines.append("再平衡建議：將單一產業或資產類別權重控制在可承受範圍，並保留現金緩衝以覆蓋至少 6-12 個月利息。")
    lines.append("配息規劃建議：以預估股息優先覆蓋借款利息，剩餘再投入低相關性資產。")
    return "\n".join(lines)


@app.get("/")
def root() -> FileResponse:
    html_path = APP_DIR / "index.html"
    if html_path.exists():
        return FileResponse(html_path)
    raise HTTPException(status_code=404, detail="index.html not found")


@app.get("/api/portfolio/summary")
def get_portfolio_summary() -> Dict[str, float]:
    with get_conn() as conn:
        return portfolio_summary_data(conn)


@app.get("/api/portfolio/performance")
def get_portfolio_performance() -> Dict[str, Any]:
    with get_conn() as conn:
        return portfolio_performance_data(conn)


@app.get("/api/portfolio/allocation")
def get_portfolio_allocation() -> Dict[str, Any]:
    with get_conn() as conn:
        return portfolio_allocation_data(conn)


@app.get("/api/portfolio/expected-dividends")
def get_expected_dividends() -> Dict[str, Any]:
    with get_conn() as conn:
        return expected_dividends_data(conn)


@app.get("/api/loans/list")
def list_loans(lender: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, Any]:
    where_clauses = []
    params: List[Any] = []
    
    if lender:
        where_clauses.append("lender = ?")
        params.append(lender)
    if date_from:
        where_clauses.append("start_date >= ?")
        params.append(normalize_date(date_from))
    if date_to:
        where_clauses.append("start_date <= ?")
        params.append(normalize_date(date_to))
    
    where_sql = " WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    today = date.today()
    items = []
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, lender, collateral, collateral_lots, principal, interest_rate, start_date, due_date, note FROM loans {where_sql} ORDER BY start_date DESC",
            params
        ).fetchall()

        for row in rows:
            start_date = row["start_date"]
            try:
                d0 = datetime.fromisoformat(start_date).date()
            except ValueError:
                d0 = today

            days = max((today - d0).days, 0)
            rate = float(row["interest_rate"])
            if rate > 1:
                rate = rate / 100.0
            principal = float(row["principal"])
            interest = round((principal * rate / 365.0) * days, 2)
            collateral_lots = float(row["collateral_lots"] or 0)

            due_date = str(row["due_date"] or "").strip()
            days_to_due: Optional[int] = None
            if due_date:
                try:
                    dd = datetime.fromisoformat(due_date).date()
                    days_to_due = (dd - today).days
                except ValueError:
                    pass

            collateral_sid = normalize_stock_id(str(row["collateral"]).strip())
            stock_row = conn.execute(
                "SELECT chinese_name, current_price FROM stock_info WHERE stock_id = ?", (collateral_sid,)
            ).fetchone()
            collateral_name = str(stock_row["chinese_name"] or "").strip() if stock_row else ""
            current_price = float(stock_row["current_price"] or 0) if stock_row else 0.0

            # 維持率分子：以抵押張數 × 1000 × 市價為準；若未填張數則嘗試直接金額
            if collateral_lots > 0 and current_price > 0:
                collateral_value = collateral_lots * 1000 * current_price
            else:
                collateral_value = get_stock_market_value(conn, collateral_sid)

            maintenance_rate = round((collateral_value / principal) * 100, 2) if principal > 0 else None
            # 純擔保（principal=0）：實拿 = 抵押市值；有借款：實拿 = 抵押市值 - 本金 - 利息
            net_proceeds = round(collateral_value - principal - interest, 2)

            items.append(
                {
                    "id": row["id"],
                    "lender": row["lender"],
                    "collateral": row["collateral"],
                    "collateral_name": collateral_name,
                    "collateral_lots": collateral_lots,
                    "current_price": round(current_price, 2),
                    "principal": round(principal, 2),
                    "interest_rate": round(rate, 6),
                    "start_date": start_date,
                    "due_date": due_date,
                    "days_to_due": days_to_due,
                    "elapsed_days": days,
                    "accrued_interest": interest,
                    "collateral_value": round(collateral_value, 2),
                    "maintenance_rate": maintenance_rate,
                    "net_proceeds": net_proceeds,
                    "note": str(row["note"] or ""),
                }
            )

    return {"items": items}


@app.get("/api/loans/health")
def get_loans_health() -> Dict[str, Any]:
    with get_conn() as conn:
        return loans_health_data(conn)


def _default_due_date(start_iso: str) -> str:
    try:
        d = datetime.fromisoformat(start_iso).date()
        year = d.year + ((d.month + 17) // 12)
        month = ((d.month + 17) % 12) + 1
        import calendar
        last_day = calendar.monthrange(year, month)[1]
        day = min(d.day, last_day)
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


@app.post("/api/loans")
def create_loan(payload: LoanCreate) -> Dict[str, Any]:
    start_date = normalize_date(payload.start_date)
    due_date = normalize_date(payload.due_date) if payload.due_date else _default_due_date(start_date)
    rate = float(payload.interest_rate)
    if rate > 1:
        rate = rate / 100.0
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO loans (lender, collateral, collateral_lots, principal, interest_rate, start_date, due_date, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (payload.lender.strip(), payload.collateral.strip(), float(payload.collateral_lots), float(payload.principal), rate, start_date, due_date, payload.note or ""),
        )
        conn.commit()
    return {"message": "loan created", "id": int(cur.lastrowid)}


@app.put("/api/loans/{loan_id}")
def update_loan(loan_id: int, payload: LoanUpdate) -> Dict[str, Any]:
    start_date = normalize_date(payload.start_date)
    due_date = normalize_date(payload.due_date) if payload.due_date else _default_due_date(start_date)
    rate = float(payload.interest_rate)
    if rate > 1:
        rate = rate / 100.0
    with get_conn() as conn:
        exists = conn.execute("SELECT id FROM loans WHERE id = ?", (loan_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="loan not found")
        conn.execute(
            "UPDATE loans SET lender = ?, collateral = ?, collateral_lots = ?, principal = ?, interest_rate = ?, start_date = ?, due_date = ?, note = ? WHERE id = ?",
            (payload.lender.strip(), payload.collateral.strip(), float(payload.collateral_lots), float(payload.principal), rate, start_date, due_date, payload.note or "", loan_id),
        )
        conn.commit()
    return {"message": "loan updated", "id": loan_id}


@app.delete("/api/loans/{loan_id}")
def delete_loan(loan_id: int) -> Dict[str, Any]:
    with get_conn() as conn:
        exists = conn.execute("SELECT id FROM loans WHERE id = ?", (loan_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="loan not found")
        conn.execute("DELETE FROM loans WHERE id = ?", (loan_id,))
        conn.commit()
    return {"message": "loan deleted", "id": loan_id}


@app.get("/api/settings/transaction-tax")
def get_transaction_tax_settings_api() -> Dict[str, Any]:
    with get_conn() as conn:
        settings = get_transaction_tax_settings(conn)
    return {"settings": settings}


@app.put("/api/settings/transaction-tax")
def update_transaction_tax_settings_api(payload: TaxSettingsUpdate) -> Dict[str, Any]:
    with get_conn() as conn:
        settings = set_transaction_tax_settings(
            conn,
            {
                "stock_buy_tax_rate": payload.stock_buy_tax_rate,
                "stock_sell_tax_rate": payload.stock_sell_tax_rate,
                "etf_buy_tax_rate": payload.etf_buy_tax_rate,
                "etf_sell_tax_rate": payload.etf_sell_tax_rate,
                "bond_buy_tax_rate": payload.bond_buy_tax_rate,
                "bond_sell_tax_rate": payload.bond_sell_tax_rate,
            },
        )
        rebuild_holdings_and_realized(conn)
        conn.commit()
    return {"message": "transaction tax settings updated", "settings": settings}


@app.get("/api/settings/dividend-nhi")
def get_dividend_nhi_settings_api() -> Dict[str, Any]:
    with get_conn() as conn:
        nhi = get_nhi_settings(conn)
    return {
        "settings": {
            "nhi_supplement_rate": float(nhi["rate"]),
            "nhi_supplement_threshold": float(nhi["threshold"]),
        }
    }


@app.put("/api/settings/dividend-nhi")
def update_dividend_nhi_settings_api(payload: NhiSettingsUpdate) -> Dict[str, Any]:
    with get_conn() as conn:
        settings = set_nhi_settings(
            conn,
            {
                "nhi_supplement_rate": float(payload.nhi_supplement_rate),
                "nhi_supplement_threshold": float(payload.nhi_supplement_threshold),
            },
        )
        conn.commit()
    return {"message": "dividend NHI settings updated", "settings": settings}


@app.get("/api/system/version")
def get_system_version() -> Dict[str, Any]:
    return {
        "system_name": "Family Office Asset Allocation Platform",
        "profile": "financial-enterprise-v2",
        "api_version": app.version,
        "server_date": date.today().isoformat(),
        "features": [
            "fifo_costing",
            "multi_source_market_quote",
            "dividend_auto_sync",
            "stock_info_self_healing",
            "audit_log",
        ],
    }


@app.get("/api/system/data-health")
def get_data_health(deep: bool = False, max_stocks: int = 80) -> Dict[str, Any]:
    with get_conn() as conn:
        report = build_data_health_report(conn, deep=deep, max_stocks=max_stocks)
    return report


def upload_backup_to_google_drive(local_file: Path) -> Dict[str, Any]:
    service_account_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    if not service_account_file:
        raise HTTPException(status_code=400, detail="GOOGLE_SERVICE_ACCOUNT_FILE is not configured")
    if not folder_id:
        raise HTTPException(status_code=400, detail="GOOGLE_DRIVE_FOLDER_ID is not configured")

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"Google Drive dependencies missing: {exc}")

    scopes = ["https://www.googleapis.com/auth/drive.file"]
    credentials = service_account.Credentials.from_service_account_file(service_account_file, scopes=scopes)
    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)

    metadata = {"name": local_file.name, "parents": [folder_id]}
    media = MediaFileUpload(str(local_file), mimetype="application/x-sqlite3", resumable=False)
    created = drive.files().create(body=metadata, media_body=media, fields="id,name,webViewLink").execute()
    return {
        "uploaded": True,
        "drive_file_id": created.get("id"),
        "drive_file_name": created.get("name"),
        "drive_web_view_link": created.get("webViewLink"),
    }


@app.post("/api/system/backup-db")
def backup_database_api(offsite: bool = False) -> Dict[str, Any]:
    backup_dir = APP_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"wealth_backup_{ts}.db"

    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(backup_file)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    size_bytes = backup_file.stat().st_size if backup_file.exists() else 0

    offsite_result: Dict[str, Any] = {"requested": bool(offsite), "uploaded": False}
    if offsite:
        offsite_result = upload_backup_to_google_drive(backup_file)
        offsite_result["requested"] = True

    with get_conn() as conn:
        write_audit_log(
            conn,
            event_type="backup_database",
            payload={
                "backup_file": str(backup_file.name),
                "size_bytes": int(size_bytes),
                "offsite": offsite_result,
            },
            severity="INFO",
            actor="api",
        )
        conn.commit()

    return {
        "message": "database backup created",
        "backup_file": backup_file.name,
        "backup_path": str(backup_file),
        "size_bytes": int(size_bytes),
        "offsite": offsite_result,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/system/audit-logs")
def list_audit_logs(limit: int = 200) -> Dict[str, Any]:
    safe_limit = max(1, min(limit, 5000))
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, event_type, severity, actor, payload_json, created_at
            FROM system_audit_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()

    items: List[Dict[str, Any]] = []
    for r in rows:
        payload_raw = r["payload_json"] or "{}"
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            payload = {"raw": payload_raw}

        items.append(
            {
                "id": int(r["id"]),
                "event_type": r["event_type"],
                "severity": r["severity"],
                "actor": r["actor"],
                "payload": payload,
                "created_at": r["created_at"],
            }
        )

    return {
        "count": len(items),
        "items": items,
    }


@app.post("/api/settings/repair-stock-info")
def repair_stock_info_api() -> Dict[str, Any]:
    with get_conn(auto_repair=False) as conn:
        repaired_count = ensure_stock_info_integrity(conn)
        write_audit_log(
            conn,
            event_type="repair_stock_info",
            payload={"repaired_count": repaired_count},
            severity="INFO",
            actor="api",
        )
        if repaired_count > 0:
            conn.commit()
        else:
            conn.commit()
    return {"message": "stock_info repaired", "repaired_count": repaired_count}


@app.post("/api/dividends/auto-sync")
def auto_sync_dividends_api(force: bool = False, years: int = 2) -> Dict[str, Any]:
    with get_conn() as conn:
        today = date.today().isoformat()
        last_sync_date = get_app_setting(conn, "last_dividend_auto_sync_date", "") or ""
        last_sync_detail_raw = get_app_setting(conn, "last_dividend_auto_sync_detail", "") or ""
        try:
            last_sync_detail = json.loads(last_sync_detail_raw) if last_sync_detail_raw else []
        except json.JSONDecodeError:
            last_sync_detail = []

        if (not force) and last_sync_date == today:
            write_audit_log(
                conn,
                event_type="auto_sync_dividends_skipped",
                payload={"date": today, "reason": "already_synced"},
                severity="INFO",
                actor="api",
            )
            conn.commit()
            return {
                "message": "dividends already synced today",
                "synced": False,
                "last_sync_date": last_sync_date,
                "processed_stocks": 0,
                "inserted_cash_dividends": 0,
                "inserted_stock_dividends": 0,
                "stock_details": last_sync_detail,
            }

        sync_result = sync_dividends_from_market(conn, years=years)
        set_app_setting(conn, "last_dividend_auto_sync_date", today)
        set_app_setting(conn, "last_dividend_auto_sync_detail", json.dumps(sync_result.get("stock_details", []), ensure_ascii=False))
        write_audit_log(
            conn,
            event_type="auto_sync_dividends",
            payload={
                "date": today,
                "force": bool(force),
                "years": int(years),
                "processed_stocks": sync_result.get("processed_stocks", 0),
                "inserted_cash_dividends": sync_result.get("inserted_cash_dividends", 0),
                "inserted_stock_dividends": sync_result.get("inserted_stock_dividends", 0),
            },
            severity="INFO",
            actor="api",
        )
        conn.commit()

    return {
        "message": "dividends auto sync completed",
        "synced": True,
        "last_sync_date": today,
        **sync_result,
    }


@app.post("/api/dividends/recalc-jobs")
def create_dividend_recalc_job_api(years: int = 2, start_year: int = 2019) -> Dict[str, Any]:
    safe_years = max(1, min(int(years), 30))
    safe_start_year = max(1900, min(int(start_year), date.today().year))

    with _DIVIDEND_RECALC_JOBS_LOCK:
        for existing_job in _DIVIDEND_RECALC_JOBS.values():
            if existing_job.get("status") == "running":
                raise HTTPException(status_code=409, detail="已有重算工作執行中，請稍後或先查詢現有工作進度")

    job_id = uuid.uuid4().hex
    created_at = datetime.now().isoformat(timespec="seconds")
    _set_dividend_recalc_job_state(
        job_id,
        {
            "job_id": job_id,
            "status": "queued",
            "created_at": created_at,
            "years": safe_years,
            "start_year": safe_start_year,
            "total_stocks": 0,
            "processed_stocks": 0,
            "progress_pct": 0.0,
            "current_stock_id": "",
            "success_count": 0,
            "failed_count": 0,
            "recent_failed": [],
            "failed_stocks": [],
            "stock_details": [],
            "diff_report": {"start_year": safe_start_year, "changed_count": 0, "top_changes": []},
        },
    )

    worker = threading.Thread(target=_run_dividend_recalc_job, args=(job_id, safe_years, safe_start_year), daemon=True)
    worker.start()

    return {
        "message": "dividend recalc job started",
        "job_id": job_id,
        "status": "queued",
        "created_at": created_at,
        "years": safe_years,
        "start_year": safe_start_year,
    }


@app.get("/api/dividends/recalc-jobs/{job_id}")
def get_dividend_recalc_job_api(job_id: str) -> Dict[str, Any]:
    payload = _get_dividend_recalc_job_state(job_id)
    if not payload:
        raise HTTPException(status_code=404, detail="找不到指定的重算工作")
    return payload


@app.post("/api/dividends/recalc-jobs/{job_id}/cancel")
def cancel_dividend_recalc_job_api(job_id: str) -> Dict[str, Any]:
    state = _get_dividend_recalc_job_state(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="找不到指定的重算工作")

    status = str(state.get("status") or "").lower()
    if status in {"completed", "failed", "cancelled"}:
        return {
            "message": f"job already {status}",
            "job_id": job_id,
            "status": status,
            "cancel_requested": bool(state.get("cancel_requested", False)),
        }

    _set_dividend_recalc_job_state(
        job_id,
        {
            "cancel_requested": True,
            "cancel_requested_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    return {
        "message": "cancel requested",
        "job_id": job_id,
        "status": "cancelling",
        "cancel_requested": True,
    }


@app.get("/api/dividends/cash")
def list_cash_dividends(
    limit: int = 200,
    stock_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Dict[str, Any]:
    safe_limit = max(1, min(limit, 5000))
    where = []
    params: List[Any] = []

    if stock_id:
        where.append("d.stock_id = ?")
        params.append(normalize_stock_id(stock_id))
    if date_from:
        where.append("d.pay_date >= ?")
        params.append(normalize_date(date_from))
    if date_to:
        where.append("d.pay_date <= ?")
        params.append(normalize_date(date_to))

    where_sql = " WHERE " + " AND ".join(where) if where else ""

    with get_conn() as conn:
        nhi = get_nhi_settings(conn)
        rows = conn.execute(
            f"""
            SELECT d.id, d.stock_id, COALESCE(s.chinese_name, d.stock_id) AS chinese_name,
                   d.ex_date, d.pay_date, d.amount_per_share, d.holding_shares,
                   d.cash_amount, d.source, d.note
            FROM cash_dividends d
            LEFT JOIN stock_info s ON s.stock_id = d.stock_id
            {where_sql}
            ORDER BY d.pay_date DESC, d.id DESC
            LIMIT ?
            """,
            params + [safe_limit],
        ).fetchall()

    items = [
        {
            "id": r["id"],
            "stock_id": r["stock_id"],
            "chinese_name": r["chinese_name"],
            "ex_date": r["ex_date"],
            "pay_date": r["pay_date"],
            "amount_per_share": float(r["amount_per_share"]),
            "holding_shares": float(r["holding_shares"]),
            "cash_amount_gross": float(r["cash_amount"]),
            "nhi_premium": compute_nhi_premium(float(r["cash_amount"]), nhi_rate=nhi["rate"], nhi_threshold=nhi["threshold"]),
            "cash_amount": compute_net_cash_dividend(float(r["cash_amount"]), nhi_rate=nhi["rate"], nhi_threshold=nhi["threshold"]),
            "source": r["source"],
            "note": r["note"],
        }
        for r in rows
    ]
    return {"items": items}


@app.post("/api/dividends/cash")
def create_cash_dividend(payload: CashDividendCreate) -> Dict[str, Any]:
    sid = normalize_stock_id(payload.stock_id)
    ex_date = normalize_date(payload.ex_date)
    pay_date = normalize_date(payload.pay_date) if payload.pay_date else ex_date

    with get_conn() as conn:
        ensure_stock_info(conn, sid)
        holding_shares = float(payload.holding_shares) if payload.holding_shares is not None else get_shares_on_date(conn, sid, ex_date)
        cash_amount = round(holding_shares * float(payload.amount_per_share), 2)

        cur = conn.execute(
            """
            INSERT INTO cash_dividends (stock_id, ex_date, pay_date, amount_per_share, holding_shares, cash_amount, source, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sid,
                ex_date,
                pay_date,
                float(payload.amount_per_share),
                holding_shares,
                cash_amount,
                (payload.source or "manual").strip() or "manual",
                payload.note or "",
            ),
        )
        conn.commit()

    return {"message": "cash dividend created", "id": int(cur.lastrowid)}


@app.put("/api/dividends/cash/{dividend_id}")
def update_cash_dividend(dividend_id: int, payload: CashDividendUpdate) -> Dict[str, Any]:
    ex_date = normalize_date(payload.ex_date)
    pay_date = normalize_date(payload.pay_date) if payload.pay_date else ex_date
    cash_amount_gross = round(float(payload.holding_shares) * float(payload.amount_per_share), 2)
    with get_conn() as conn:
        exists = conn.execute("SELECT id FROM cash_dividends WHERE id = ?", (dividend_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="cash dividend not found")

        conn.execute(
            """
            UPDATE cash_dividends
            SET ex_date = ?, pay_date = ?, amount_per_share = ?, holding_shares = ?, cash_amount = ?, source = ?, note = ?
            WHERE id = ?
            """,
            (
                ex_date,
                pay_date,
                float(payload.amount_per_share),
                float(payload.holding_shares),
                cash_amount_gross,
                (payload.source or "manual").strip() or "manual",
                payload.note or "",
                dividend_id,
            ),
        )
        conn.commit()

    return {"message": "cash dividend updated", "id": dividend_id}


@app.delete("/api/dividends/cash/{dividend_id}")
def delete_cash_dividend(dividend_id: int) -> Dict[str, Any]:
    with get_conn() as conn:
        exists = conn.execute("SELECT id FROM cash_dividends WHERE id = ?", (dividend_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="cash dividend not found")
        conn.execute("DELETE FROM cash_dividends WHERE id = ?", (dividend_id,))
        conn.commit()
    return {"message": "cash dividend deleted", "id": dividend_id}


@app.get("/api/dividends/stock")
def list_stock_dividends(
    limit: int = 200,
    stock_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Dict[str, Any]:
    safe_limit = max(1, min(limit, 5000))
    where = []
    params: List[Any] = []

    if stock_id:
        where.append("d.stock_id = ?")
        params.append(normalize_stock_id(stock_id))
    if date_from:
        where.append("d.allot_date >= ?")
        params.append(normalize_date(date_from))
    if date_to:
        where.append("d.allot_date <= ?")
        params.append(normalize_date(date_to))

    where_sql = " WHERE " + " AND ".join(where) if where else ""

    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT d.id, d.stock_id, COALESCE(s.chinese_name, d.stock_id) AS chinese_name,
                 d.ex_date, d.allot_date, d.ratio, d.holding_shares,
                 d.bonus_shares, d.event_type, d.cash_return_per_share, d.cash_return_amount, d.source, d.note
            FROM stock_dividends d
            LEFT JOIN stock_info s ON s.stock_id = d.stock_id
            {where_sql}
            ORDER BY d.allot_date DESC, d.id DESC
            LIMIT ?
            """,
            params + [safe_limit],
        ).fetchall()

    items = [
        {
            "id": r["id"],
            "stock_id": r["stock_id"],
            "chinese_name": r["chinese_name"],
            "ex_date": r["ex_date"],
            "allot_date": r["allot_date"],
            "ratio": float(r["ratio"]),
            "holding_shares": float(r["holding_shares"]),
            "bonus_shares": float(r["bonus_shares"]),
            "event_type": str(r["event_type"] or "stock_dividend"),
            "cash_return_per_share": float(r["cash_return_per_share"] or 0.0),
            "cash_return_amount": float(r["cash_return_amount"] or 0.0),
            "source": r["source"],
            "note": r["note"],
        }
        for r in rows
    ]
    return {"items": items}


@app.post("/api/dividends/stock")
def create_stock_dividend(payload: StockDividendCreate) -> Dict[str, Any]:
    sid = normalize_stock_id(payload.stock_id)
    ex_date = normalize_date(payload.ex_date)
    allot_date = normalize_date(payload.allot_date) if payload.allot_date else ex_date

    with get_conn() as conn:
        ensure_stock_info(conn, sid)
        holding_shares = float(payload.holding_shares) if payload.holding_shares is not None else get_shares_on_date(conn, sid, ex_date, stock_dividend_before_tx=False)
        if payload.bonus_shares is not None:
            bonus_shares = float(payload.bonus_shares)
        else:
            settlement = compute_stock_event_settlement(holding_shares, float(payload.ratio))
            holding_shares = float(settlement["base_shares"])
            bonus_shares = float(settlement["share_delta"])
        cash_return_per_share = round(float(payload.cash_return_per_share or 0.0), 6)
        event_type = infer_stock_event_type(float(payload.ratio), cash_return_per_share, payload.event_type)
        cash_return_amount = (
            float(payload.cash_return_amount)
            if payload.cash_return_amount is not None
            else (round(holding_shares * cash_return_per_share, 2) if event_type == "capital_reduction_cash" else 0.0)
        )

        cur = conn.execute(
            """
            INSERT INTO stock_dividends (stock_id, ex_date, allot_date, ratio, holding_shares, bonus_shares, event_type, cash_return_per_share, cash_return_amount, source, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sid,
                ex_date,
                allot_date,
                float(payload.ratio),
                holding_shares,
                bonus_shares,
                event_type,
                cash_return_per_share,
                cash_return_amount,
                (payload.source or "manual").strip() or "manual",
                payload.note or "",
            ),
        )
        rebuild_holdings_and_realized(conn)
        conn.commit()

    return {"message": "stock dividend created", "id": int(cur.lastrowid)}


@app.put("/api/dividends/stock/{dividend_id}")
def update_stock_dividend(dividend_id: int, payload: StockDividendUpdate) -> Dict[str, Any]:
    ex_date = normalize_date(payload.ex_date)
    allot_date = normalize_date(payload.allot_date) if payload.allot_date else ex_date
    cash_return_per_share = round(float(payload.cash_return_per_share or 0.0), 6)
    event_type = infer_stock_event_type(float(payload.ratio), cash_return_per_share, payload.event_type)
    cash_return_amount = float(payload.cash_return_amount or 0.0)
    with get_conn() as conn:
        exists = conn.execute("SELECT id FROM stock_dividends WHERE id = ?", (dividend_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="stock dividend not found")

        conn.execute(
            """
            UPDATE stock_dividends
            SET ex_date = ?, allot_date = ?, ratio = ?, holding_shares = ?, bonus_shares = ?, event_type = ?, cash_return_per_share = ?, cash_return_amount = ?, source = ?, note = ?
            WHERE id = ?
            """,
            (
                ex_date,
                allot_date,
                float(payload.ratio),
                float(payload.holding_shares),
                float(payload.bonus_shares),
                event_type,
                cash_return_per_share,
                cash_return_amount,
                (payload.source or "manual").strip() or "manual",
                payload.note or "",
                dividend_id,
            ),
        )
        rebuild_holdings_and_realized(conn)
        conn.commit()

    return {"message": "stock dividend updated", "id": dividend_id}


@app.delete("/api/dividends/stock/{dividend_id}")
def delete_stock_dividend(dividend_id: int) -> Dict[str, Any]:
    with get_conn() as conn:
        exists = conn.execute("SELECT id FROM stock_dividends WHERE id = ?", (dividend_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="stock dividend not found")
        conn.execute("DELETE FROM stock_dividends WHERE id = ?", (dividend_id,))
        rebuild_holdings_and_realized(conn)
        conn.commit()
    return {"message": "stock dividend deleted", "id": dividend_id}


@app.post("/api/transactions")
def create_transaction(payload: TransactionCreate) -> Dict[str, Any]:
    with get_conn() as conn:
        payload.stock_id = normalize_stock_id(payload.stock_id)
        ensure_stock_info(conn, payload.stock_id)
        tax_settings = get_transaction_tax_settings(conn)

        tx_date = normalize_date(payload.date)
        payload.transaction_tax = calculate_transaction_tax(payload.stock_id, payload.action, payload.shares, payload.price, tax_settings)
        cur = conn.execute(
            """
            INSERT INTO transactions (date, stock_id, action, shares, price, fees, transaction_tax, realized_profit)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tx_date,
                payload.stock_id,
                payload.action,
                payload.shares,
                payload.price,
                payload.fees,
                payload.transaction_tax,
                0.0,
            ),
        )

        tx_id = int(cur.lastrowid)
        rebuild_holdings_and_realized(conn)
        conn.commit()

        tx_row = conn.execute("SELECT realized_profit FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        realized_profit = float(tx_row["realized_profit"]) if tx_row else 0.0

        holding = conn.execute(
            "SELECT stock_id, shares, total_cost FROM holdings WHERE stock_id = ?",
            (payload.stock_id,),
        ).fetchone()

        return {
            "message": "transaction created",
            "id": tx_id,
            "realized_profit": round(realized_profit, 2),
            "transaction_tax": payload.transaction_tax,
            "holding": {
                "stock_id": payload.stock_id,
                "shares": round(float(holding["shares"]), 4) if holding else 0.0,
                "total_cost": round(float(holding["total_cost"]), 2) if holding else 0.0,
            },
        }


@app.get("/api/transactions")
def list_transactions(limit: int = 200, stock_id: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, Any]:
    safe_limit = max(1, min(limit, 20000))
    
    # Build WHERE clause
    where_clauses = []
    params: List[Any] = []
    
    if stock_id:
        stock_id = normalize_stock_id(stock_id)
        where_clauses.append("t.stock_id = ?")
        params.append(stock_id)
    if date_from:
        where_clauses.append("t.date >= ?")
        params.append(normalize_date(date_from))
    if date_to:
        where_clauses.append("t.date <= ?")
        params.append(normalize_date(date_to))
    
    where_sql = " WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT
                t.id,
                t.date,
                t.stock_id,
                s.chinese_name,
                t.action,
                t.shares,
                t.price,
                t.fees,
                t.transaction_tax,
                t.realized_profit,
                COALESCE(s.current_price, 0) AS current_price
            FROM transactions t
            LEFT JOIN stock_info s ON t.stock_id = s.stock_id
            {where_sql}
            ORDER BY t.date DESC, t.id DESC
            LIMIT ?
            """,
            params + [safe_limit],
        ).fetchall()

        quote_refresh_ids: List[str] = []
        seen: set[str] = set()
        for r in rows:
            sid = normalize_stock_id(r["stock_id"])
            cname = (r["chinese_name"] or "").strip()
            current_price = float(r["current_price"] or 0.0)
            if sid and (not cname or current_price <= 0) and sid not in seen:
                quote_refresh_ids.append(sid)
                seen.add(sid)
            if len(quote_refresh_ids) >= 30:
                break

        if quote_refresh_ids:
            for sid in quote_refresh_ids:
                quote = fetch_latest_quote(sid)
                close_price = quote.get("close_price")
                upsert_stock_quote(conn, sid, quote.get("chinese_name"), close_price)
            conn.commit()

            rows = conn.execute(
                f"""
                SELECT
                    t.id,
                    t.date,
                    t.stock_id,
                    s.chinese_name,
                    t.action,
                    t.shares,
                    t.price,
                    t.fees,
                    t.transaction_tax,
                    t.realized_profit,
                    COALESCE(s.current_price, 0) AS current_price
                FROM transactions t
                LEFT JOIN stock_info s ON t.stock_id = s.stock_id
                {where_sql}
                ORDER BY t.date DESC, t.id DESC
                LIMIT ?
                """,
                params + [safe_limit],
            ).fetchall()

        metrics_by_tx = build_fifo_transaction_metrics(conn)

    return {
        "items": [
            {
                "id": r["id"],
                "date": r["date"],
                "stock_id": r["stock_id"],
                "chinese_name": r["chinese_name"] or r["stock_id"],
                "action": r["action"],
                "action_label": "買" if r["action"] == "buy" else "賣",
                "shares": float(r["shares"]),
                "price": float(r["price"]),
                "fees": float(r["fees"]),
                "transaction_tax": float(r["transaction_tax"]),
                "realized_profit": float(r["realized_profit"]),
                "current_price": round(float(r["current_price"]), 2),
                "inventory_after": float(metrics_by_tx.get(int(r["id"]), {}).get("inventory_after", 0.0)),
                "realized_total": float(metrics_by_tx.get(int(r["id"]), {}).get("realized_total", 0.0)),
                "unrealized_profit_tx": metrics_by_tx.get(int(r["id"]), {}).get("unrealized_profit_tx"),
                "return_rate_tx": metrics_by_tx.get(int(r["id"]), {}).get("return_rate_tx"),
            }
            for r in rows
        ]
    }


@app.put("/api/transactions/{tx_id}")
def update_transaction(tx_id: int, payload: TransactionUpdate) -> Dict[str, Any]:
    tx_date = normalize_date(payload.date)

    with get_conn() as conn:
        exists = conn.execute("SELECT id FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="transaction not found")

        payload.stock_id = normalize_stock_id(payload.stock_id)
        tax_settings = get_transaction_tax_settings(conn)
        payload.transaction_tax = calculate_transaction_tax(payload.stock_id, payload.action, payload.shares, payload.price, tax_settings)
        ensure_stock_info(conn, payload.stock_id)
        conn.execute(
            """
            UPDATE transactions
            SET date = ?, stock_id = ?, action = ?, shares = ?, price = ?, fees = ?, transaction_tax = ?
            WHERE id = ?
            """,
            (
                tx_date,
                payload.stock_id,
                payload.action,
                payload.shares,
                payload.price,
                payload.fees,
                payload.transaction_tax,
                tx_id,
            ),
        )
        rebuild_holdings_and_realized(conn)
        conn.commit()

    return {"message": "transaction updated", "id": tx_id}


@app.delete("/api/transactions/{tx_id}")
def delete_transaction(tx_id: int) -> Dict[str, Any]:
    with get_conn() as conn:
        exists = conn.execute("SELECT id FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="transaction not found")

        conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
        rebuild_holdings_and_realized(conn)
        conn.commit()

    return {"message": "transaction deleted", "id": tx_id}


@app.get("/api/stock/{stock_id}/quote")
def get_stock_quote(stock_id: str) -> Dict[str, Any]:
    sid = normalize_stock_id(stock_id)
    if not sid:
        raise HTTPException(status_code=400, detail="stock_id is required")

    quote = fetch_latest_quote(sid)
    with get_conn() as conn:
        upsert_stock_quote(conn, sid, quote.get("chinese_name"), quote.get("close_price"))

        row = conn.execute(
            "SELECT chinese_name, current_price FROM stock_info WHERE stock_id = ?",
            (sid,),
        ).fetchone()
        conn.commit()

    return {
        "stock_id": sid,
        "chinese_name": (row["chinese_name"] if row and row["chinese_name"] else sid),
        "close_price": float(row["current_price"]) if row and row["current_price"] is not None else 0.0,
        "source": quote.get("source", "database"),
    }


@app.get("/api/stock/missing-prices")
def list_missing_prices(limit: int = 500) -> Dict[str, Any]:
    with get_conn() as conn:
        items = build_missing_price_list(conn, limit=limit)
    return {
        "count": len(items),
        "items": items,
    }


@app.post("/api/stock/refresh-missing-prices")
def refresh_missing_prices_api(limit: int = 500) -> Dict[str, Any]:
    with get_conn() as conn:
        result = refresh_missing_prices(conn, limit=limit)
        write_audit_log(
            conn,
            event_type="refresh_missing_prices",
            payload={
                "limit": int(limit),
                "checked": result.get("checked", 0),
                "updated": result.get("updated", 0),
                "still_missing": result.get("still_missing", 0),
            },
            severity="INFO",
            actor="api",
        )
        conn.commit()
    return {
        "message": "missing price refresh completed",
        **result,
    }


@app.post("/api/stock/refresh-prices")
def refresh_prices_api(limit: int = 500, scope: str = "transactions", stock_ids: Optional[str] = None) -> Dict[str, Any]:
    explicit_stock_ids = []
    if stock_ids:
        explicit_stock_ids = [normalize_stock_id(part) for part in stock_ids.split(",") if normalize_stock_id(part)]
    with get_conn() as conn:
        result = refresh_market_prices(conn, limit=limit, scope=scope, stock_ids=explicit_stock_ids)
        write_audit_log(
            conn,
            event_type="refresh_prices",
            payload={
                "limit": int(limit),
                "scope": result.get("scope"),
                "stock_ids": explicit_stock_ids[:50],
                "checked": result.get("checked", 0),
                "updated": result.get("updated", 0),
                "failed": result.get("failed", 0),
            },
            severity="INFO",
            actor="api",
        )
        conn.commit()
    return {
        "message": "price refresh completed",
        **result,
    }


@app.get("/api/stock/{stock_id}/detail")
def get_stock_detail(stock_id: str) -> Dict[str, Any]:
    stock_id = normalize_stock_id(stock_id)
    with get_conn() as conn:
        nhi = get_nhi_settings(conn)
        info = conn.execute("SELECT asset_type, sector, chinese_name, current_price FROM stock_info WHERE stock_id = ?", (stock_id,)).fetchone()
        holding = conn.execute(
            "SELECT shares, total_cost FROM holdings WHERE stock_id = ?",
            (stock_id,),
        ).fetchone()

        shares = float(holding["shares"]) if holding else 0.0
        total_cost = float(holding["total_cost"]) if holding else 0.0
        current_price = float(info["current_price"]) if info and info["current_price"] else 0.0
        market_value = shares * current_price if current_price > 0 else total_cost

        realized = conn.execute(
            "SELECT COALESCE(SUM(realized_profit), 0) AS v FROM transactions WHERE stock_id = ?",
            (stock_id,),
        ).fetchone()["v"]
        dividend = get_cash_dividend_sum(conn, stock_id)
        bonus_shares = get_bonus_shares_sum(conn, stock_id)
        avg_cost_per_share = (total_cost / shares) if shares > 1e-9 else 0.0
        realized_with_dividends = float(realized) + float(dividend)

        transactions = conn.execute(
            """
            SELECT id, date, action, shares, price, fees, transaction_tax, realized_profit
            FROM transactions WHERE stock_id = ?
            ORDER BY date DESC, id DESC
            """,
            (stock_id,),
        ).fetchall()

        yearly_cash_rows = conn.execute(
            """
             SELECT SUBSTR(pay_date, 1, 4) AS year,
                 cash_amount,
                 holding_shares
            FROM cash_dividends
            WHERE stock_id = ? AND COALESCE(holding_shares, 0) > 0
             ORDER BY year DESC
            """,
            (stock_id,),
        ).fetchall()
        yearly_stock_rows = conn.execute(
            """
            SELECT SUBSTR(allot_date, 1, 4) AS year,
                   COALESCE(SUM(bonus_shares), 0) AS bonus_shares,
                   COALESCE(SUM(holding_shares), 0) AS base_shares,
                   COUNT(*) AS event_count
            FROM stock_dividends
            WHERE stock_id = ? AND ABS(COALESCE(bonus_shares, 0)) > 0.0000001
            GROUP BY SUBSTR(allot_date, 1, 4)
            ORDER BY year DESC
            """,
            (stock_id,),
        ).fetchall()

        by_year: Dict[str, Dict[str, Any]] = {}
        for row in yearly_cash_rows:
            y = str(row["year"] or "")
            if not y:
                continue
            if y not in by_year:
                by_year[y] = {
                    "year": y,
                    "cash_dividend": 0.0,
                    "cash_event_count": 0,
                    "cash_base_shares": 0.0,
                    "stock_dividend_shares": 0.0,
                    "stock_event_count": 0,
                    "stock_base_shares": 0.0,
                }
            gross = float(row["cash_amount"] or 0.0)
            by_year[y]["cash_dividend"] = round(
                float(by_year[y]["cash_dividend"]) + compute_net_cash_dividend(gross, nhi_rate=nhi["rate"], nhi_threshold=nhi["threshold"]),
                2,
            )
            by_year[y]["cash_event_count"] = int(by_year[y]["cash_event_count"]) + 1
            by_year[y]["cash_base_shares"] = round(float(by_year[y]["cash_base_shares"]) + float(row["holding_shares"] or 0.0), 2)
        for row in yearly_stock_rows:
            y = str(row["year"] or "")
            if not y:
                continue
            if y not in by_year:
                by_year[y] = {
                    "year": y,
                    "cash_dividend": 0.0,
                    "cash_event_count": 0,
                    "cash_base_shares": 0.0,
                    "stock_dividend_shares": 0.0,
                    "stock_event_count": 0,
                    "stock_base_shares": 0.0,
                }
            by_year[y]["stock_dividend_shares"] = round(float(row["bonus_shares"] or 0.0), 4)
            by_year[y]["stock_event_count"] = int(row["event_count"] or 0)
            by_year[y]["stock_base_shares"] = round(float(row["base_shares"] or 0.0), 2)

        yearly_dividends = sorted(by_year.values(), key=lambda x: x["year"], reverse=True)

        return {
            "stock_id": stock_id,
            "chinese_name": info["chinese_name"] if info and info["chinese_name"] else stock_id,
            "asset_type": info["asset_type"] if info else "個股",
            "sector": info["sector"] if info else "其他",
            "shares": round(shares, 4),
            "total_cost": round(total_cost, 2),
            "current_price": round(current_price, 2),
            "avg_cost_per_share": round(avg_cost_per_share, 4),
            "market_value": round(market_value, 2),
            "unrealized_profit": round(market_value - total_cost, 2),
            "realized_profit": round(float(realized), 2),
            "realized_with_dividends": round(realized_with_dividends, 2),
            "dividends_received": round(float(dividend), 2),
            "bonus_shares_received": round(float(bonus_shares), 4),
            "yearly_dividends": yearly_dividends,
            "transactions": [
                {
                    "id": tx["id"],
                    "date": tx["date"],
                    "action": tx["action"],
                    "action_label": "買" if tx["action"] == "buy" else "賣",
                    "shares": float(tx["shares"]),
                    "price": float(tx["price"]),
                    "fees": float(tx["fees"]),
                    "transaction_tax": float(tx["transaction_tax"]),
                    "realized_profit": float(tx["realized_profit"]),
                }
                for tx in transactions
            ],
        }


@app.post("/api/ai/advisor")
@_limiter.limit("10/minute")
def advisor(request: Request, payload: AdvisorRequest) -> Dict[str, Any]:
    with get_conn() as conn:
        snapshot = build_advisor_snapshot(conn)

    answer = call_openai(payload.question, snapshot)
    source = "openai"

    if not answer:
        answer = call_anthropic(payload.question, snapshot)
        source = "anthropic"

    if not answer:
        answer = local_rule_based_advice(payload.question, snapshot)
        source = "rule-based"

    return {
        "source": source,
        "answer": answer,
        "snapshot": snapshot,
    }
