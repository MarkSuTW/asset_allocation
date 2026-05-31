import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

DB_PATH = Path("wealth.db")
APP_DIR = Path(__file__).resolve().parent
_RUNTIME_SCHEMA_READY = False
_LOCAL_STOCK_NAME_MAP: Optional[Dict[str, str]] = None
_AUTO_STOCK_INFO_REPAIR_DONE = False

app = FastAPI(title="Family Office Dashboard API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


class CashDividendCreate(BaseModel):
    stock_id: str
    ex_date: str
    pay_date: Optional[str] = None
    amount_per_share: float = Field(ge=0)
    holding_shares: Optional[float] = Field(default=None, ge=0)
    cash_amount: Optional[float] = Field(default=None, ge=0)
    source: str = Field(default="manual")
    note: str = Field(default="")


class CashDividendUpdate(BaseModel):
    ex_date: str
    pay_date: Optional[str] = None
    amount_per_share: float = Field(ge=0)
    holding_shares: float = Field(ge=0)
    cash_amount: float = Field(ge=0)
    source: str = Field(default="manual")
    note: str = Field(default="")


class StockDividendCreate(BaseModel):
    stock_id: str
    ex_date: str
    allot_date: Optional[str] = None
    ratio: float = Field(ge=0)
    holding_shares: Optional[float] = Field(default=None, ge=0)
    bonus_shares: Optional[float] = Field(default=None, ge=0)
    source: str = Field(default="manual")
    note: str = Field(default="")


class StockDividendUpdate(BaseModel):
    ex_date: str
    allot_date: Optional[str] = None
    ratio: float = Field(ge=0)
    holding_shares: float = Field(ge=0)
    bonus_shares: float = Field(ge=0)
    source: str = Field(default="manual")
    note: str = Field(default="")


DEFAULT_TAX_SETTINGS = {
    "stock_buy_tax_rate": 0.0,
    "stock_sell_tax_rate": 0.003,
    "etf_buy_tax_rate": 0.0,
    "etf_sell_tax_rate": 0.001,
    "bond_buy_tax_rate": 0.0,
    "bond_sell_tax_rate": 0.001,
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


def get_shares_on_date(conn: sqlite3.Connection, stock_id: str, on_date: str) -> float:
    sid = normalize_stock_id(stock_id)
    d = normalize_date(on_date)
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN action='buy' THEN shares ELSE 0 END), 0) AS buy_shares,
            COALESCE(SUM(CASE WHEN action='sell' THEN shares ELSE 0 END), 0) AS sell_shares
        FROM transactions
        WHERE stock_id = ? AND date <= ?
        """,
        (sid, d),
    ).fetchone()
    if not row:
        return 0.0
    bonus_row = conn.execute(
        """
        SELECT COALESCE(SUM(bonus_shares), 0) AS bonus_shares
        FROM stock_dividends
        WHERE stock_id = ? AND allot_date <= ?
        """,
        (sid, d),
    ).fetchone()
    bonus = float(bonus_row["bonus_shares"]) if bonus_row else 0.0
    return max(float(row["buy_shares"]) - float(row["sell_shares"]) + bonus, 0.0)


def get_cash_dividend_sum(conn: sqlite3.Connection, stock_id: str, date_from: Optional[str] = None) -> float:
    sid = normalize_stock_id(stock_id)
    params: List[Any] = [sid]
    where = ["stock_id = ?"]
    if date_from:
        where.append("pay_date >= ?")
        params.append(normalize_date(date_from))

    row = conn.execute(
        f"SELECT COALESCE(SUM(cash_amount), 0) AS v FROM cash_dividends WHERE {' AND '.join(where)}",
        params,
    ).fetchone()
    return float(row["v"] if row else 0.0)


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
            source TEXT NOT NULL DEFAULT 'manual',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (stock_id) REFERENCES stock_info(stock_id)
        )
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


def normalize_stock_id(raw: str) -> str:
    return (raw or "").strip().upper()


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
            if ratio <= 1e-9:
                continue
            ex_date = datetime.fromtimestamp(int(ts)).date().isoformat()
            key = (ex_date, round(float(ratio), 6))
            stock_events[key] = {
                "ex_date": ex_date,
                "ratio": round(float(ratio), 6),
            }

    return {
        "cash": sorted(cash_events.values(), key=lambda x: x["ex_date"]),
        "stock": sorted(stock_events.values(), key=lambda x: x["ex_date"]),
        "source_symbol": source_symbol,
        "attempted_symbols": symbols,
    }


def sync_dividends_from_market(conn: sqlite3.Connection, years: int = 2) -> Dict[str, Any]:
    rows = conn.execute(
        """
        SELECT stock_id
        FROM holdings
        WHERE COALESCE(shares, 0) > 0
        ORDER BY stock_id
        """
    ).fetchall()
    stock_ids = [normalize_stock_id(r["stock_id"]) for r in rows if normalize_stock_id(r["stock_id"])]

    inserted_cash = 0
    inserted_stock = 0
    stock_details: List[Dict[str, Any]] = []

    for sid in stock_ids:
        market_events = fetch_yahoo_dividend_events(sid, years=years)
        stock_inserted_cash = 0
        stock_inserted_stock = 0

        for event in market_events["cash"]:
            ex_date = event["ex_date"]
            amount_per_share = float(event["amount_per_share"])
            exists = conn.execute(
                """
                SELECT 1
                FROM cash_dividends
                WHERE stock_id = ? AND ex_date = ? AND ABS(amount_per_share - ?) < 0.000001
                LIMIT 1
                """,
                (sid, ex_date, amount_per_share),
            ).fetchone()
            if exists:
                continue

            holding_shares = get_shares_on_date(conn, sid, ex_date)
            if holding_shares <= 1e-9:
                continue

            cash_amount = round(holding_shares * amount_per_share, 2)
            conn.execute(
                """
                INSERT INTO cash_dividends (stock_id, ex_date, pay_date, amount_per_share, holding_shares, cash_amount, source, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (sid, ex_date, ex_date, amount_per_share, holding_shares, cash_amount, "yahoo_auto", "auto synced from market"),
            )
            inserted_cash += 1
            stock_inserted_cash += 1

        for event in market_events["stock"]:
            ex_date = event["ex_date"]
            ratio = float(event["ratio"])
            exists = conn.execute(
                """
                SELECT 1
                FROM stock_dividends
                WHERE stock_id = ? AND ex_date = ? AND ABS(ratio - ?) < 0.000001
                LIMIT 1
                """,
                (sid, ex_date, ratio),
            ).fetchone()
            if exists:
                continue

            holding_shares = get_shares_on_date(conn, sid, ex_date)
            if holding_shares <= 1e-9:
                continue

            bonus_shares = round(holding_shares * ratio, 4)
            if bonus_shares <= 1e-9:
                continue

            conn.execute(
                """
                INSERT INTO stock_dividends (stock_id, ex_date, allot_date, ratio, holding_shares, bonus_shares, source, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (sid, ex_date, ex_date, ratio, holding_shares, bonus_shares, "yahoo_split_auto", "auto synced from market"),
            )
            inserted_stock += 1
            stock_inserted_stock += 1

        stock_details.append(
            {
                "stock_id": sid,
                "source": market_events.get("source_symbol") or "unavailable",
                "attempted_symbols": market_events.get("attempted_symbols") or [],
                "fetched_cash_events": len(market_events.get("cash", [])),
                "fetched_stock_events": len(market_events.get("stock", [])),
                "inserted_cash_events": stock_inserted_cash,
                "inserted_stock_events": stock_inserted_stock,
            }
        )

    if inserted_stock > 0:
        rebuild_holdings_and_realized(conn)

    return {
        "processed_stocks": len(stock_ids),
        "inserted_cash_dividends": inserted_cash,
        "inserted_stock_dividends": inserted_stock,
        "stock_details": stock_details,
    }


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
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
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

        close_price = parse_quote_price(best.get("regularMarketPreviousClose"))
        if close_price is None:
            close_price = parse_quote_price(best.get("regularMarketPrice"))
        chinese_name = (best.get("shortName") or best.get("longName") or "").strip() or None
        if close_price is not None:
            source = "yahoo_quote"

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
                with urllib.request.urlopen(twse_req, timeout=8) as resp:
                    twse_payload = json.loads(resp.read().decode("utf-8"))
                data = twse_payload.get("data", [])
                if data:
                    closes = [parse_quote_price(row[6] if len(row) > 6 else None) for row in data]
                    closes = [v for v in closes if v is not None]
                    if closes:
                        close_price = closes[-1]
                        source = "twse_openapi"
                        break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, TypeError):
                continue

    if close_price is None:
        twse_rt = fetch_twse_realtime_quote(sid)
        if twse_rt.get("close_price") is not None:
            close_price = float(twse_rt["close_price"])
            if not chinese_name:
                chinese_name = twse_rt.get("chinese_name")
            source = str(twse_rt.get("source") or "twse_realtime")

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


def rebuild_holdings_and_realized(conn: sqlite3.Connection) -> None:
    tx_rows = conn.execute(
        "SELECT id, date, stock_id, action, shares, price, fees, transaction_tax FROM transactions ORDER BY date, id"
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

    lots_by_stock: Dict[str, deque] = defaultdict(deque)
    tax_settings = get_transaction_tax_settings(conn)
    conn.execute("DELETE FROM holdings")

    for event in events:
        if event["kind"] == "stock_div":
            r = event["row"]
            sid = normalize_stock_id(r["stock_id"])
            bonus_shares = float(r["bonus_shares"])
            if bonus_shares > 1e-9:
                lots_by_stock[sid].append(
                    {
                        "buy_tx_id": None,
                        "shares": bonus_shares,
                        "unit_cost": 0.0,
                    }
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
            if shares > available + 1e-9:
                raise HTTPException(status_code=400, detail=f"Insufficient shares to sell: {sid}")

            remaining = shares
            fifo_cost = 0.0
            while remaining > 1e-9 and lots_by_stock[sid]:
                lot = lots_by_stock[sid][0]
                take = min(remaining, lot["shares"])
                fifo_cost += take * lot["unit_cost"]
                lot["shares"] -= take
                remaining -= take
                if lot["shares"] <= 1e-9:
                    lots_by_stock[sid].popleft()

            proceeds = shares * price - fees - transaction_tax
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
        "SELECT id, allot_date, stock_id, bonus_shares FROM stock_dividends ORDER BY allot_date, id"
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
            if bonus_shares > 1e-9:
                lots_by_stock[sid].append(
                    {
                        "buy_tx_id": None,
                        "shares": bonus_shares,
                        "unit_cost": 0.0,
                    }
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
            remaining = shares
            while remaining > 1e-9 and lots_by_stock[sid]:
                lot = lots_by_stock[sid][0]
                take = min(remaining, lot["shares"])
                fifo_cost += take * lot["unit_cost"]
                lot["shares"] -= take
                remaining -= take
                if lot["shares"] <= 1e-9:
                    lots_by_stock[sid].popleft()
            proceeds = shares * price - fees - transaction_tax
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

        items.append(
            {
                "stock_id": stock_id,
                "chinese_name": chinese_name,
                "current_price": round(float(current_price), 2),
                "realized_profit": round(float(realized), 2),
                "dividends_received": round(float(dividend), 2),
                "bonus_shares_received": round(float(bonus_shares), 4),
                "market_value": round(float(market_value), 2),
                "unrealized_profit": round(float(unrealized), 2),
                "shares": round(float(shares), 4),
            }
        )

    return {
        "items": items,
        "totals": {
            "realized_profit": round(sum(i["realized_profit"] for i in items), 2),
            "dividends_received": round(sum(i["dividends_received"] for i in items), 2),
            "bonus_shares_received": round(sum(i["bonus_shares_received"] for i in items), 4),
            "market_value": round(sum(i["market_value"] for i in items), 2),
            "unrealized_profit": round(sum(i["unrealized_profit"] for i in items), 2),
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
    for row in rows:
        stock_id = row["stock_id"]
        shares = float(row["shares"])
        if shares <= 0:
            continue

        market_value = shares * get_latest_price(conn, stock_id)
        one_year_ago = today.replace(year=today.year - 1).isoformat()
        historical = get_cash_dividend_sum(conn, stock_id, one_year_ago)

        y = default_yield.get(row["asset_type"], 0.03)
        estimate = float(historical)
        if estimate <= 0:
            estimate = market_value * y

        result.append(
            {
                "stock_id": stock_id,
                "expected_dividend": round(estimate, 2),
                "method": "historical" if float(historical) > 0 else "yield_estimate",
            }
        )

    total = round(sum(r["expected_dividend"] for r in result), 2)
    return {"items": result, "total_expected_dividend": total}


def loans_health_data(conn: sqlite3.Connection) -> Dict[str, Any]:
    rows = conn.execute("SELECT lender, collateral, principal, interest_rate, start_date FROM loans").fetchall()

    total_principal = 0.0
    total_collateral = 0.0
    total_interest = 0.0
    loan_items = []
    today = date.today()

    for row in rows:
        principal = float(row["principal"])
        total_principal += principal

        collateral_raw = str(row["collateral"]).strip()
        collateral_value = parse_numeric(collateral_raw)
        if collateral_value <= 0:
            collateral_value = get_stock_market_value(conn, collateral_raw)

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

        loan_items.append(
            {
                "lender": row["lender"],
                "collateral": row["collateral"],
                "principal": round(principal, 2),
                "interest_rate": round(rate, 6),
                "start_date": start_date,
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
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, lender, collateral, principal, interest_rate, start_date FROM loans {where_sql} ORDER BY start_date DESC",
            params
        ).fetchall()

    today = date.today()
    items = []
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
        interest = (principal * rate / 365.0) * days

        items.append(
            {
                "id": row["id"],
                "lender": row["lender"],
                "collateral": row["collateral"],
                "principal": round(principal, 2),
                "interest_rate": round(rate, 6),
                "start_date": start_date,
                "elapsed_days": days,
                "accrued_interest": round(interest, 2),
            }
        )

    return {"items": items}


@app.get("/api/loans/health")
def get_loans_health() -> Dict[str, Any]:
    with get_conn() as conn:
        return loans_health_data(conn)


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
            "cash_amount": float(r["cash_amount"]),
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
        cash_amount = float(payload.cash_amount) if payload.cash_amount is not None else round(holding_shares * float(payload.amount_per_share), 2)

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
                float(payload.cash_amount),
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
                   d.bonus_shares, d.source, d.note
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
        holding_shares = float(payload.holding_shares) if payload.holding_shares is not None else get_shares_on_date(conn, sid, ex_date)
        bonus_shares = float(payload.bonus_shares) if payload.bonus_shares is not None else round(holding_shares * float(payload.ratio), 4)

        cur = conn.execute(
            """
            INSERT INTO stock_dividends (stock_id, ex_date, allot_date, ratio, holding_shares, bonus_shares, source, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sid,
                ex_date,
                allot_date,
                float(payload.ratio),
                holding_shares,
                bonus_shares,
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
    with get_conn() as conn:
        exists = conn.execute("SELECT id FROM stock_dividends WHERE id = ?", (dividend_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="stock dividend not found")

        conn.execute(
            """
            UPDATE stock_dividends
            SET ex_date = ?, allot_date = ?, ratio = ?, holding_shares = ?, bonus_shares = ?, source = ?, note = ?
            WHERE id = ?
            """,
            (
                ex_date,
                allot_date,
                float(payload.ratio),
                float(payload.holding_shares),
                float(payload.bonus_shares),
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


@app.get("/api/stock/{stock_id}/detail")
def get_stock_detail(stock_id: str) -> Dict[str, Any]:
    stock_id = normalize_stock_id(stock_id)
    with get_conn() as conn:
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

        transactions = conn.execute(
            """
            SELECT id, date, action, shares, price, fees, transaction_tax, realized_profit
            FROM transactions WHERE stock_id = ?
            ORDER BY date DESC, id DESC
            """,
            (stock_id,),
        ).fetchall()

        return {
            "stock_id": stock_id,
            "chinese_name": info["chinese_name"] if info and info["chinese_name"] else stock_id,
            "asset_type": info["asset_type"] if info else "個股",
            "sector": info["sector"] if info else "其他",
            "shares": round(shares, 4),
            "total_cost": round(total_cost, 2),
            "current_price": round(current_price, 2),
            "market_value": round(market_value, 2),
            "unrealized_profit": round(market_value - total_cost, 2),
            "realized_profit": round(float(realized), 2),
            "dividends_received": round(float(dividend), 2),
            "bonus_shares_received": round(float(bonus_shares), 4),
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
def advisor(payload: AdvisorRequest) -> Dict[str, Any]:
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
