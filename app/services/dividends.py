"""
Dividend sync, corporate actions, and related helpers.
"""
import html
import http.client
import json
import math
import re
import sqlite3
import ssl
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from app.core.utils import normalize_stock_id, normalize_date, parse_quote_price, parse_numeric
from app.core.database import (
    get_nhi_settings,
    write_audit_log,
    _DIVIDEND_RECALC_JOBS,
    _DIVIDEND_RECALC_JOBS_LOCK,
)
from app.services.calculations import compute_net_cash_dividend
from app.services.quotes import fetch_yahoo_dividend_events
from app.services.portfolio import (
    infer_stock_event_type,
    rebuild_holdings_and_realized,
)


# ---------------------------------------------------------------------------
# TWSE/MOPS date parsing
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# MOPS / TWSE dividend fetching
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Shares on date
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Stock event settlement
# ---------------------------------------------------------------------------

def compute_stock_event_settlement(holding_shares: float, ratio: float) -> Dict[str, float]:
    base_shares = float(holding_shares)
    if ratio > 0:
        base_shares = float(math.floor(max(holding_shares, 0.0) / 1000.0) * 1000)
    delta = float(int(base_shares * ratio))
    return {"base_shares": base_shares, "share_delta": delta}


# ---------------------------------------------------------------------------
# Oversell detection
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Dividend sync
# ---------------------------------------------------------------------------

def sync_dividends_for_stock(conn: sqlite3.Connection, sid: str, years: int = 2) -> Dict[str, Any]:
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
                                AND ABS(julianday(o.ex_date) - julianday(y.ex_date)) <= 3
                    )
                """,
                (sid,),
        ).fetchall()
        for row in near_date_duplicated_yahoo_rows:
                conn.execute("DELETE FROM cash_dividends WHERE id = ?", (int(row["id"]),))

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


def auto_sync_dividends(conn: sqlite3.Connection, years: int = 2) -> Dict[str, Any]:
    stock_ids = _list_dividend_sync_stock_ids(conn)

    inserted_cash = 0
    inserted_stock = 0
    updated_stock = 0
    stock_details: List[Dict[str, Any]] = []
    failed_stocks: List[Dict[str, Any]] = []

    for sid in stock_ids:
        try:
            one = sync_dividends_for_stock(conn, sid, years=years)
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


def reconcile_dividend_events(conn: sqlite3.Connection, include_manual: bool = False) -> Dict[str, Any]:
    """
    Reconcile dividend event holding snapshots against transaction history.

    - cash_dividends: refresh holding_shares/cash_amount by ex_date holdings.
    - stock_dividends: refresh holding_shares/bonus_shares/cash_return_amount.
    - For non-manual sources, rows with zero holdings are removed as stale events.
    - Manual rows are skipped by default unless include_manual=True.
    """
    updated_cash = 0
    deleted_cash = 0
    updated_stock = 0
    deleted_stock = 0

    cash_rows = conn.execute(
        """
        SELECT id, stock_id, ex_date, amount_per_share, holding_shares, cash_amount, source
        FROM cash_dividends
        ORDER BY id
        """
    ).fetchall()

    for r in cash_rows:
        source = str(r["source"] or "")
        if (not include_manual) and source == "manual":
            continue

        rid = int(r["id"])
        sid = normalize_stock_id(r["stock_id"])
        ex_date = normalize_date(r["ex_date"])
        amount_per_share = float(r["amount_per_share"] or 0.0)
        actual_holding = float(get_shares_on_date(conn, sid, ex_date))
        expected_cash_amount = round(actual_holding * amount_per_share, 2)

        if actual_holding <= 1e-9:
            if include_manual or source != "manual":
                conn.execute("DELETE FROM cash_dividends WHERE id = ?", (rid,))
                deleted_cash += 1
            continue

        stored_holding = float(r["holding_shares"] or 0.0)
        stored_cash_amount = float(r["cash_amount"] or 0.0)
        if (
            abs(stored_holding - actual_holding) > 1e-9
            or abs(stored_cash_amount - expected_cash_amount) > 1e-6
        ):
            conn.execute(
                """
                UPDATE cash_dividends
                SET holding_shares = ?, cash_amount = ?
                WHERE id = ?
                """,
                (actual_holding, expected_cash_amount, rid),
            )
            updated_cash += 1

    stock_rows = conn.execute(
        """
        SELECT id, stock_id, ex_date, ratio, holding_shares, bonus_shares,
               event_type, cash_return_per_share, cash_return_amount, source
        FROM stock_dividends
        ORDER BY id
        """
    ).fetchall()

    for r in stock_rows:
        source = str(r["source"] or "")
        if (not include_manual) and source == "manual":
            continue

        rid = int(r["id"])
        sid = normalize_stock_id(r["stock_id"])
        ex_date = normalize_date(r["ex_date"])
        ratio = float(r["ratio"] or 0.0)
        event_type = str(r["event_type"] or "stock_dividend")
        cash_return_per_share = float(r["cash_return_per_share"] or 0.0)

        actual_holding = float(
            get_shares_on_date(
                conn,
                sid,
                ex_date,
                stock_dividend_before_tx=False,
                exclude_stock_dividend_id=rid,
            )
        )

        if actual_holding <= 1e-9:
            if include_manual or source != "manual":
                conn.execute("DELETE FROM stock_dividends WHERE id = ?", (rid,))
                deleted_stock += 1
            continue

        settlement = compute_stock_event_settlement(actual_holding, ratio)
        expected_holding = float(settlement["base_shares"])
        expected_bonus = float(settlement["share_delta"])

        if expected_bonus < 0 and actual_holding + expected_bonus < 0:
            expected_bonus = -float(int(actual_holding))

        expected_cash_return_amount = (
            round(expected_holding * cash_return_per_share, 2)
            if event_type == "capital_reduction_cash"
            else 0.0
        )

        stored_holding = float(r["holding_shares"] or 0.0)
        stored_bonus = float(r["bonus_shares"] or 0.0)
        stored_cash_return_amount = float(r["cash_return_amount"] or 0.0)

        if (
            abs(stored_holding - expected_holding) > 1e-9
            or abs(stored_bonus - expected_bonus) > 1e-9
            or abs(stored_cash_return_amount - expected_cash_return_amount) > 1e-6
        ):
            conn.execute(
                """
                UPDATE stock_dividends
                SET holding_shares = ?, bonus_shares = ?, cash_return_amount = ?
                WHERE id = ?
                """,
                (expected_holding, expected_bonus, expected_cash_return_amount, rid),
            )
            updated_stock += 1

    if updated_stock > 0 or deleted_stock > 0:
        rebuild_holdings_and_realized(conn)

    return {
        "updated_cash_dividends": updated_cash,
        "deleted_cash_dividends": deleted_cash,
        "updated_stock_dividends": updated_stock,
        "deleted_stock_dividends": deleted_stock,
        "include_manual": bool(include_manual),
    }


# ---------------------------------------------------------------------------
# Dividend recalc job helpers
# ---------------------------------------------------------------------------

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
        import main as _main_module
        with _main_module.get_conn() as conn:
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
                    one = sync_dividends_for_stock(conn, sid, years=years)
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
