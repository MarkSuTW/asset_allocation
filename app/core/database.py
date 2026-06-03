"""
Database helper functions: settings, audit log, stock info, migrations.
These are pure helper functions; the global state (DB_PATH, etc.) and get_conn()
live in main.py to keep test patching simple.
"""
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import HTTPException

from app.core.utils import normalize_stock_id


# ---------------------------------------------------------------------------
# Module-level globals (canonical location; main.py re-exports these)
# ---------------------------------------------------------------------------

DB_PATH = Path("wealth.db")
APP_DIR = Path(__file__).resolve().parent.parent.parent

_RUNTIME_SCHEMA_READY = False
_LOCAL_STOCK_NAME_MAP: Optional[Dict[str, str]] = None
_AUTO_STOCK_INFO_REPAIR_DONE = False
_DIVIDEND_RECALC_JOBS: Dict[str, Dict[str, Any]] = {}
_DIVIDEND_RECALC_JOBS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Default settings constants
# ---------------------------------------------------------------------------

DEFAULT_TAX_SETTINGS: Dict[str, float] = {
    "stock_buy_tax_rate": 0.0,
    "stock_sell_tax_rate": 0.003,
    "etf_buy_tax_rate": 0.0,
    "etf_sell_tax_rate": 0.001,
    "bond_buy_tax_rate": 0.0,
    "bond_sell_tax_rate": 0.001,
}

DEFAULT_NHI_SETTINGS: Dict[str, float] = {
    "nhi_supplement_rate": 0.0211,
    "nhi_supplement_threshold": 20000.0,
}


# ---------------------------------------------------------------------------
# app_settings helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Tax / NHI settings
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Local stock name map
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# stock_info helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Schema migrations
# ---------------------------------------------------------------------------

_SCHEMA_MIGRATIONS = [
    (1, "add loan fields: due_date, note, collateral_lots"),
    (2, "add cash_dividends and stock_dividends indexes"),
]


def _apply_pending_migrations(conn: sqlite3.Connection) -> None:
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    for version, description in _SCHEMA_MIGRATIONS:
        if version not in applied:
            conn.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                (version, description),
            )


def ensure_runtime_schema(conn: sqlite3.Connection) -> None:
    global _RUNTIME_SCHEMA_READY
    if _RUNTIME_SCHEMA_READY:
        return

    # Lazy import to avoid circular dependency: portfolio imports database
    from app.services.portfolio import rebuild_holdings_and_realized

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

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now')),
            description TEXT NOT NULL DEFAULT ''
        )
        """
    )
    _apply_pending_migrations(conn)
    conn.commit()

    _RUNTIME_SCHEMA_READY = True


def get_conn(auto_repair: bool = True) -> sqlite3.Connection:
    """
    NOTE: In main.py, get_conn is re-defined using the module-level DB_PATH,
    _RUNTIME_SCHEMA_READY, and _AUTO_STOCK_INFO_REPAIR_DONE from main.py
    so tests can patch those names on main.  This version is for non-main usage.
    """
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
            pass
    return conn
