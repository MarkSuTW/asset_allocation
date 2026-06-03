"""
Portfolio calculations: FIFO, holdings rebuild, summary/performance/allocation data.
"""
import sqlite3
from collections import defaultdict, deque
from datetime import date
from typing import Any, Dict, List, Optional

from app.core.utils import normalize_stock_id, normalize_date
from app.core.database import (
    ensure_stock_info,
    get_transaction_tax_settings,
    get_nhi_settings,
)
from app.services.calculations import (
    calculate_transaction_tax,
    compute_nhi_premium,
    compute_net_cash_dividend,
)
from app.services.quotes import fetch_latest_quote, upsert_stock_quote


# ---------------------------------------------------------------------------
# Stock event helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Holdings rebuild
# ---------------------------------------------------------------------------

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
            fifo_cost = round(fifo_cost, 6)
            proceeds = round(sell_shares * price - (fees * ratio) - (transaction_tax * ratio), 6)
            realized = round(proceeds - fifo_cost, 6)

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


# ---------------------------------------------------------------------------
# FIFO transaction metrics
# ---------------------------------------------------------------------------

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
        transaction_tax = float(r["transaction_tax"])
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
            fifo_cost = round(fifo_cost, 6)
            proceeds = round(sell_shares * price - (fees * ratio) - (transaction_tax * ratio), 6)
            realized = round(proceeds - fifo_cost, 6)

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


# ---------------------------------------------------------------------------
# Dividend sum helpers
# ---------------------------------------------------------------------------

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


def get_latest_price(conn: sqlite3.Connection, stock_id: str) -> float:
    row = conn.execute(
        "SELECT price FROM transactions WHERE stock_id = ? ORDER BY date DESC, id DESC LIMIT 1",
        (stock_id,),
    ).fetchone()
    return float(row[0]) if row else 0.0


def get_stock_market_value(conn: sqlite3.Connection, stock_id: str) -> float:
    h = conn.execute("SELECT shares FROM holdings WHERE stock_id = ?", (stock_id,)).fetchone()
    if not h:
        return 0.0
    shares = float(h[0])
    return shares * get_latest_price(conn, stock_id)


def _resolve_collateral_value(conn: sqlite3.Connection, collateral_sid: str, collateral_lots: float) -> tuple:
    """Returns (current_price, collateral_value) using lots*1000*price when lots>0, else total holdings value."""
    stock_row = conn.execute(
        "SELECT current_price FROM stock_info WHERE stock_id = ?", (collateral_sid,)
    ).fetchone()
    current_price = float(stock_row["current_price"] or 0) if stock_row else 0.0
    if collateral_lots > 0 and current_price > 0:
        collateral_value = collateral_lots * 1000 * current_price
    else:
        collateral_value = get_stock_market_value(conn, collateral_sid)
    return current_price, collateral_value


# ---------------------------------------------------------------------------
# Portfolio data functions
# ---------------------------------------------------------------------------

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
    rows = conn.execute(
        """
        SELECT t.stock_id,
               COALESCE(s.chinese_name, t.stock_id) AS chinese_name,
               COALESCE(h.shares, 0) AS shares,
               COALESCE(h.total_cost, 0) AS total_cost,
               COALESCE(s.current_price, 0) AS current_price
        FROM (SELECT DISTINCT stock_id FROM transactions) t
        LEFT JOIN holdings h ON h.stock_id = t.stock_id
        LEFT JOIN stock_info s ON s.stock_id = t.stock_id
        ORDER BY t.stock_id
        """
    ).fetchall()
    items = []

    for row in rows:
        stock_id = row["stock_id"]
        chinese_name = row["chinese_name"] or stock_id
        shares = float(row["shares"])
        total_cost = float(row["total_cost"])
        current_price = float(row["current_price"])
        if shares > 0 and current_price <= 0:
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

    # Totals for realized P&L and dividends must include ALL stocks ever traded,
    # not just current holdings — otherwise sold-out positions are silently excluded.
    all_realized = float(conn.execute(
        "SELECT COALESCE(SUM(realized_profit), 0) AS v FROM transactions"
    ).fetchone()["v"])

    nhi = get_nhi_settings(conn)
    all_div_rows = conn.execute("SELECT cash_amount FROM cash_dividends").fetchall()
    all_dividends = round(sum(
        compute_net_cash_dividend(float(r["cash_amount"] or 0), nhi["rate"], nhi["threshold"])
        for r in all_div_rows
    ), 2)
    all_bonus_shares = float(conn.execute(
        "SELECT COALESCE(SUM(bonus_shares), 0) AS v FROM stock_dividends"
    ).fetchone()["v"])

    return {
        "items": items,
        "totals": {
            "realized_profit": round(all_realized, 2),
            "realized_with_dividends": round(all_realized + all_dividends, 2),
            "dividends_received": all_dividends,
            "bonus_shares_received": round(all_bonus_shares, 4),
            # market_value and unrealized are current-holdings-only (correct)
            "market_value": round(sum(i["market_value"] for i in items), 2),
            "unrealized_profit": round(sum(i["unrealized_profit"] for i in items), 2),
            "total_profit_including_dividends": round(
                all_realized + all_dividends + sum(i["unrealized_profit"] for i in items), 2
            ),
        },
    }


def portfolio_allocation_data(conn: sqlite3.Connection) -> Dict[str, List[Dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT s.stock_id, s.asset_type, s.sector,
               COALESCE(h.shares, 0) AS shares,
               COALESCE(s.current_price, 0) AS current_price,
               COALESCE(h.total_cost, 0) AS total_cost
        FROM holdings h
        LEFT JOIN stock_info s ON s.stock_id = h.stock_id
        WHERE h.shares > 0
        """
    ).fetchall()

    asset_type_map: Dict[str, float] = {}
    sector_map: Dict[str, float] = {}

    for row in rows:
        stock_id = row["stock_id"]
        current_price = float(row["current_price"] or 0)
        shares = float(row["shares"])
        if current_price <= 0:
            quote = fetch_latest_quote(stock_id)
            if quote.get("close_price") is not None:
                current_price = float(quote["close_price"])
                upsert_stock_quote(conn, stock_id, quote.get("chinese_name"), current_price)
        value = shares * current_price
        if value <= 0:
            value = float(row["total_cost"])

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
