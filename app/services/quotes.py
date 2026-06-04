"""
Market price fetching from Yahoo Finance, TWSE, and Stooq.
"""
import json
import ssl
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from app.core.utils import (
    normalize_stock_id,
    parse_numeric,
    parse_quote_price,
    get_yahoo_symbol_candidates,
)
from app.core.database import ensure_stock_info, get_local_stock_name


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
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Referer": "https://mis.twse.com.tw/stock/index.jsp",
        })
        try:
            with urllib.request.urlopen(req, timeout=8, context=ssl._create_unverified_context()) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ssl.SSLError, OSError):
            continue

        msg_arr = payload.get("msgArray", []) if isinstance(payload, dict) else []
        for row in msg_arr:
            # Only use z (current intraday price). y is yesterday's close —
            # skip it here so the caller falls through to Yahoo Finance instead.
            price = parse_market_price_token(row.get("z"))
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


def _is_taiwan_listed(sid: str) -> bool:
    """Return True for Taiwan stock codes (3-6 digit numeric, optionally trailing letters)."""
    base = sid.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    return base.isdigit() and 3 <= len(base) <= 6


def _fetch_yahoo_v7(symbols: List[str]) -> tuple:
    """Try Yahoo Finance v7 quote API. Returns (close_price, chinese_name) or (None, None)."""
    url = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=" + urllib.parse.quote(",".join(symbols))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None, None

    results = payload.get("quoteResponse", {}).get("result", [])
    if not results:
        return None, None

    best = results[0]
    for q in results:
        if parse_quote_price(q.get("regularMarketPrice")) is not None or parse_quote_price(q.get("regularMarketPreviousClose")) is not None:
            best = q
            break

    price = parse_quote_price(best.get("regularMarketPrice")) or parse_quote_price(best.get("regularMarketPreviousClose"))
    name = (best.get("shortName") or best.get("longName") or "").strip() or None
    return price, name


def _fetch_yahoo_v8_chart(symbols: List[str]) -> Optional[float]:
    """Try Yahoo Finance v8 chart API. Returns close_price or None."""
    for symbol in symbols:
        chart_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?interval=1d&range=5d"
        req = urllib.request.Request(chart_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            result = data.get("chart", {}).get("result", [])
            if not result:
                continue
            closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
            valid = [parse_quote_price(v) for v in closes if parse_quote_price(v) is not None]
            if valid:
                return valid[-1]
        except Exception:
            continue
    return None


_twse_last_call: float = 0.0
_TWSE_CALL_INTERVAL = 0.8  # seconds between TWSE openapi calls


def _fetch_twse_openapi(code_candidates: List[str]) -> Optional[float]:
    """Try TWSE rwd API (recent daily data). Returns close_price or None."""
    global _twse_last_call
    elapsed = time.monotonic() - _twse_last_call
    if elapsed < _TWSE_CALL_INTERVAL:
        time.sleep(_TWSE_CALL_INTERVAL - elapsed)
    _twse_last_call = time.monotonic()

    for code in code_candidates:
        url = (
            "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
            f"?stockNo={urllib.parse.quote(code)}&response=json"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=8, context=ssl._create_unverified_context()) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                rows = payload.get("data", [])
                if rows:
                    prices = [parse_quote_price(str(r[6]).replace(",", "")) for r in rows if len(r) > 6]
                    prices = [p for p in prices if p is not None]
                    if prices:
                        return prices[-1]
                break
            except Exception:
                if attempt == 0:
                    time.sleep(1.5)
    return None


def fetch_latest_quote(stock_id: str) -> Dict[str, Any]:
    sid = normalize_stock_id(stock_id)
    if not sid:
        return {"stock_id": sid, "chinese_name": None, "close_price": None, "source": "invalid_stock_id"}

    base_sid = sid.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ") or sid
    code_candidates = list(dict.fromkeys([sid, base_sid]))  # deduplicated
    symbols = get_yahoo_symbol_candidates(sid)

    close_price: Optional[float] = None
    chinese_name: Optional[str] = None
    source = "quote_unavailable"

    is_tw = _is_taiwan_listed(sid)

    # --- Taiwan stocks: TWSE MIS realtime FIRST (most accurate intraday) ---
    if is_tw:
        twse_rt = fetch_twse_realtime_quote(sid)
        if twse_rt.get("close_price") is not None:
            close_price = float(twse_rt["close_price"])
            chinese_name = twse_rt.get("chinese_name")
            source = str(twse_rt.get("source") or "twse_realtime")

    # --- Yahoo Finance v7: real-time regularMarketPrice (works even when MIS is blocked) ---
    if close_price is None:
        yp, yn = _fetch_yahoo_v7(symbols)
        if yp is not None:
            close_price = float(yp)
            chinese_name = chinese_name or yn
            source = "yahoo_quote"

    # --- Yahoo Finance v8 chart ---
    if close_price is None:
        yp8 = _fetch_yahoo_v8_chart(symbols)
        if yp8 is not None:
            close_price = float(yp8)
            source = "yahoo_chart"

    # --- Stooq CSV ---
    if close_price is None:
        stooq = fetch_stooq_quote(sid)
        if stooq.get("close_price") is not None:
            close_price = float(stooq["close_price"])
            chinese_name = chinese_name or stooq.get("chinese_name")
            source = str(stooq.get("source") or "stooq_csv")

    # --- TWSE after-trading daily data (yesterday's close, last resort) ---
    if close_price is None and is_tw:
        twse_p = _fetch_twse_openapi(code_candidates)
        if twse_p is not None:
            close_price = float(twse_p)
            source = "twse_openapi"

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
            ltp.price AS last_trade_price
        FROM holdings h
        LEFT JOIN stock_info s ON s.stock_id = h.stock_id
        LEFT JOIN (
            SELECT t.stock_id, t.price
            FROM transactions t
            INNER JOIN (
                SELECT stock_id, MAX(id) AS max_id FROM transactions GROUP BY stock_id
            ) latest ON latest.stock_id = t.stock_id AND latest.max_id = t.id
        ) ltp ON ltp.stock_id = h.stock_id
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

    resolved_stock_ids = [normalize_stock_id(r["stock_id"]) for r in rows if normalize_stock_id(r["stock_id"])]

    updated = 0
    items: List[Dict[str, Any]] = []
    for sid in resolved_stock_ids:
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
        "checked": len(resolved_stock_ids),
        "updated": updated,
        "failed": failed,
        "items": items,
    }


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
