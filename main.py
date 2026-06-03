"""
Family Office Dashboard API - main entry point.
All business logic lives in app/ modules; this file wires them together.
"""
import json
import os
import sqlite3
import threading
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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

# ---------------------------------------------------------------------------
# Re-export from app modules so existing tests (from main import X) still work
# ---------------------------------------------------------------------------
from app.core.database import (
    DEFAULT_TAX_SETTINGS,
    DEFAULT_NHI_SETTINGS,
    get_app_setting,
    set_app_setting,
    get_transaction_tax_settings,
    set_transaction_tax_settings,
    get_nhi_settings,
    set_nhi_settings,
    write_audit_log,
    load_local_stock_name_map,
    get_local_stock_name,
    ensure_stock_info,
    ensure_stock_info_integrity,
    _SCHEMA_MIGRATIONS,
    _apply_pending_migrations,
    ensure_runtime_schema as _ensure_runtime_schema_impl,
)
from app.models.schemas import (
    TransactionCreate,
    TransactionUpdate,
    AdvisorRequest,
    TaxSettingsUpdate,
    NhiSettingsUpdate,
    CashDividendCreate,
    StockDividendCreate,
    StockDividendUpdate,
    LoanCreate,
    LoanUpdate,
)
from app.services.calculations import (
    calculate_transaction_tax,
    compute_nhi_premium,
    compute_net_cash_dividend,
)
from app.services.quotes import (
    fetch_latest_quote,
    fetch_twse_realtime_quote,
    fetch_stooq_quote,
    upsert_stock_quote,
    build_missing_price_list,
    refresh_missing_prices,
    refresh_market_prices,
    fetch_yahoo_dividend_events,
)
from app.services.portfolio import (
    apply_stock_event_to_lots,
    infer_stock_event_type,
    rebuild_holdings_and_realized,
    build_fifo_transaction_metrics,
    portfolio_summary_data,
    portfolio_performance_data,
    portfolio_allocation_data,
    expected_dividends_data,
    get_cash_dividend_sum,
    get_bonus_shares_sum,
    get_latest_price,
    get_stock_market_value,
    _resolve_collateral_value,
)
from app.services.dividends import (
    fetch_mops_dividend_announcement_events,
    _extract_mops_detail_stock_event,
    sync_dividends_for_stock as _sync_dividends_for_stock,
    auto_sync_dividends as sync_dividends_from_market,
    detect_oversell_events,
    get_shares_on_date,
    compute_stock_event_settlement,
    _set_dividend_recalc_job_state,
    _get_dividend_recalc_job_state,
    _run_dividend_recalc_job,
    _list_dividend_sync_stock_ids,
)
from app.services.loans import (
    loans_health_data,
    _default_due_date,
)
from app.services.ai import (
    call_openai,
    call_anthropic,
    local_rule_based_advice,
    build_advisor_snapshot,
)

# ---------------------------------------------------------------------------
# Module-level globals (kept here so tests can patch main.DB_PATH etc.)
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path("wealth.db")
_RUNTIME_SCHEMA_READY = False
_LOCAL_STOCK_NAME_MAP: Optional[Dict[str, str]] = None
_AUTO_STOCK_INFO_REPAIR_DONE = False
_DIVIDEND_RECALC_JOBS: Dict[str, Dict[str, Any]] = {}
_DIVIDEND_RECALC_JOBS_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Family Office Dashboard API", version="1.0.0")

_DEFAULT_ORIGINS = ["http://localhost:8000", "http://127.0.0.1:8000"]
_ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()] or _DEFAULT_ORIGINS
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

_TZ_TAIPEI = __import__("datetime").timezone(__import__("datetime").timedelta(hours=8))
_PRICE_REFRESH_STOP = threading.Event()
_PRICE_REFRESH_INTERVAL_SEC = 15 * 60  # 15 minutes


# ---------------------------------------------------------------------------
# get_conn: lives here so tests can patch main.DB_PATH / main._RUNTIME_SCHEMA_READY
# ---------------------------------------------------------------------------

def get_conn(auto_repair: bool = True) -> sqlite3.Connection:
    global _AUTO_STOCK_INFO_REPAIR_DONE, _RUNTIME_SCHEMA_READY
    if not DB_PATH.exists():
        raise HTTPException(status_code=500, detail="Database wealth.db not found. Run init_db.py first.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Call the implementation but patch its module-level flag through our local flag
    import app.core.database as _db_mod
    _db_mod._RUNTIME_SCHEMA_READY = _RUNTIME_SCHEMA_READY
    _ensure_runtime_schema_impl(conn)
    _RUNTIME_SCHEMA_READY = _db_mod._RUNTIME_SCHEMA_READY
    if auto_repair and not _AUTO_STOCK_INFO_REPAIR_DONE:
        try:
            repaired = ensure_stock_info_integrity(conn)
            if repaired > 0:
                conn.commit()
            _AUTO_STOCK_INFO_REPAIR_DONE = True
        except sqlite3.OperationalError:
            pass
    return conn


# ---------------------------------------------------------------------------
# Trading time / background scheduler
# ---------------------------------------------------------------------------

def _is_taiwan_trading_time() -> bool:
    from datetime import datetime as _dt
    now = _dt.now(_TZ_TAIPEI)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 <= mins <= 13 * 60 + 30


def _price_refresh_worker() -> None:
    """Background thread: refresh holdings prices every 15 min during trading hours."""
    while not _PRICE_REFRESH_STOP.wait(_PRICE_REFRESH_INTERVAL_SEC):
        if not _is_taiwan_trading_time():
            continue
        if not DB_PATH.exists():
            continue
        try:
            with get_conn() as conn:
                result = refresh_market_prices(conn, limit=200, scope="holdings")
                conn.commit()
            updated = result.get("updated", 0)
            if updated:
                print(f"[scheduler] auto price refresh: {updated} stocks updated", flush=True)
        except Exception as e:
            print(f"[scheduler] price refresh error: {type(e).__name__}: {e}", flush=True)


@app.on_event("startup")
def _startup_init() -> None:
    """Pre-warm DB schema, start background price scheduler."""
    if DB_PATH.exists():
        with get_conn() as conn:
            conn  # get_conn() already calls ensure_runtime_schema + auto_repair

    _PRICE_REFRESH_STOP.clear()
    t = threading.Thread(target=_price_refresh_worker, daemon=True, name="price-scheduler")
    t.start()


@app.on_event("shutdown")
def _shutdown_cleanup() -> None:
    _PRICE_REFRESH_STOP.set()


# ---------------------------------------------------------------------------
# Data health report (uses multiple services, kept here)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Google Drive backup helper (kept here; only used by backup endpoint)
# ---------------------------------------------------------------------------

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


# ===========================================================================
# API Route Handlers
# ===========================================================================

@app.get("/")
def root() -> FileResponse:
    html_path = APP_DIR / "index.html"
    if html_path.exists():
        return FileResponse(html_path)
    raise HTTPException(status_code=404, detail="index.html not found")


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Loans
# ---------------------------------------------------------------------------

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
            collateral_name_row = conn.execute(
                "SELECT chinese_name FROM stock_info WHERE stock_id = ?", (collateral_sid,)
            ).fetchone()
            collateral_name = str(collateral_name_row["chinese_name"] or "").strip() if collateral_name_row else ""
            current_price, collateral_value = _resolve_collateral_value(conn, collateral_sid, collateral_lots)

            maintenance_rate = round((collateral_value / principal) * 100, 2) if principal > 0 else None
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


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Dividends
# ---------------------------------------------------------------------------

@app.post("/api/dividends/auto-sync")
@_limiter.limit("3/minute")
def auto_sync_dividends_api(request: Request, force: bool = False, years: int = 2) -> Dict[str, Any]:
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
@_limiter.limit("3/minute")
def create_dividend_recalc_job_api(request: Request, years: int = 2, start_year: int = 2019) -> Dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

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
        seen: set = set()
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


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------

@app.get("/api/stock/{stock_id}/quote")
@_limiter.limit("30/minute")
def get_stock_quote(request: Request, stock_id: str) -> Dict[str, Any]:
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
@_limiter.limit("20/minute")
def refresh_prices_api(request: Request, limit: int = 500, scope: str = "transactions", stock_ids: Optional[str] = None) -> Dict[str, Any]:
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


# ---------------------------------------------------------------------------
# AI Advisor
# ---------------------------------------------------------------------------

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
