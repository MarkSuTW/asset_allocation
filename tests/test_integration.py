"""
Integration tests for business logic that requires a real SQLite DB connection.
"""
import sqlite3
import pytest
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_stock(conn: sqlite3.Connection, stock_id: str, name: str = "", price: float = 100.0,
                  asset_type: str = "個股", sector: str = "其他") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO stock_info (stock_id, chinese_name, asset_type, sector, current_price) VALUES (?, ?, ?, ?, ?)",
        (stock_id, name, asset_type, sector, price),
    )
    conn.commit()


def _insert_tx(conn: sqlite3.Connection, stock_id: str, date: str, action: str,
               shares: float, price: float, fees: float = 0, tax: float = 0) -> int:
    cur = conn.execute(
        "INSERT INTO transactions (date, stock_id, action, shares, price, fees, transaction_tax) VALUES (?,?,?,?,?,?,?)",
        (date, stock_id, action, shares, price, fees, tax),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# FIFO: rebuild_holdings_and_realized
# ---------------------------------------------------------------------------

from main import rebuild_holdings_and_realized, DEFAULT_TAX_SETTINGS


class TestRebuildHoldingsAndRealized:
    def test_simple_buy(self, db_conn):
        _insert_stock(db_conn, "2330")
        _insert_tx(db_conn, "2330", "2026-01-01", "buy", 1000, 500.0, fees=71)

        rebuild_holdings_and_realized(db_conn)
        db_conn.commit()

        h = db_conn.execute("SELECT shares, total_cost FROM holdings WHERE stock_id='2330'").fetchone()
        assert h["shares"] == 1000
        assert h["total_cost"] == pytest.approx(1000 * 500 + 71)

    def test_buy_then_sell_fifo(self, db_conn):
        _insert_stock(db_conn, "2330")
        _insert_tx(db_conn, "2330", "2026-01-01", "buy", 2000, 500.0, fees=142)
        _insert_tx(db_conn, "2330", "2026-02-01", "sell", 1000, 600.0, fees=85, tax=1800)

        rebuild_holdings_and_realized(db_conn)
        db_conn.commit()

        h = db_conn.execute("SELECT shares FROM holdings WHERE stock_id='2330'").fetchone()
        assert h["shares"] == 1000.0

        tx = db_conn.execute(
            "SELECT realized_profit FROM transactions WHERE action='sell'"
        ).fetchone()
        # unit_cost = (2000*500 + 142) / 2000 = 500.071
        unit_cost = (2000 * 500 + 142) / 2000
        proceeds = 1000 * 600 - 85 - 1800
        expected_realized = round(proceeds - 1000 * unit_cost, 6)
        assert tx["realized_profit"] == pytest.approx(expected_realized, abs=0.01)

    def test_oversell_clamped_to_zero(self, db_conn):
        _insert_stock(db_conn, "2330")
        _insert_tx(db_conn, "2330", "2026-01-01", "buy", 500, 100.0)
        _insert_tx(db_conn, "2330", "2026-02-01", "sell", 1000, 120.0)  # oversell

        rebuild_holdings_and_realized(db_conn)
        db_conn.commit()

        h = db_conn.execute("SELECT shares FROM holdings WHERE stock_id='2330'").fetchone()
        assert h is None or h["shares"] == pytest.approx(0, abs=1e-6)

        tx = db_conn.execute(
            "SELECT realized_profit FROM transactions WHERE action='sell'"
        ).fetchone()
        # oversell: realized stays from partial fill only
        assert tx is not None

    def test_multiple_stocks_independent(self, db_conn):
        _insert_stock(db_conn, "2330")
        _insert_stock(db_conn, "0050")
        _insert_tx(db_conn, "2330", "2026-01-01", "buy", 1000, 500.0)
        _insert_tx(db_conn, "0050", "2026-01-01", "buy", 500, 150.0)

        rebuild_holdings_and_realized(db_conn)
        db_conn.commit()

        h1 = db_conn.execute("SELECT shares FROM holdings WHERE stock_id='2330'").fetchone()
        h2 = db_conn.execute("SELECT shares FROM holdings WHERE stock_id='0050'").fetchone()
        assert h1["shares"] == 1000
        assert h2["shares"] == 500


# ---------------------------------------------------------------------------
# get_shares_on_date
# ---------------------------------------------------------------------------

from main import get_shares_on_date


class TestGetSharesOnDate:
    def test_shares_before_any_tx(self, db_conn):
        _insert_stock(db_conn, "2330")
        _insert_tx(db_conn, "2330", "2026-06-01", "buy", 1000, 100.0)
        shares = get_shares_on_date(db_conn, "2330", "2026-05-31")
        assert shares == 0.0

    def test_shares_on_buy_date(self, db_conn):
        _insert_stock(db_conn, "2330")
        _insert_tx(db_conn, "2330", "2026-06-01", "buy", 1000, 100.0)
        shares = get_shares_on_date(db_conn, "2330", "2026-06-01")
        assert shares == 1000.0

    def test_shares_after_partial_sell(self, db_conn):
        _insert_stock(db_conn, "2330")
        _insert_tx(db_conn, "2330", "2026-01-01", "buy", 2000, 100.0)
        _insert_tx(db_conn, "2330", "2026-03-01", "sell", 500, 110.0)
        shares = get_shares_on_date(db_conn, "2330", "2026-04-01")
        assert shares == 1500.0

    def test_unknown_stock_returns_zero(self, db_conn):
        shares = get_shares_on_date(db_conn, "9999", "2026-06-01")
        assert shares == 0.0


# ---------------------------------------------------------------------------
# NHI settings from DB
# ---------------------------------------------------------------------------

from main import get_nhi_settings, DEFAULT_NHI_SETTINGS


class TestGetNhiSettings:
    def test_returns_defaults_when_empty(self, db_conn):
        settings = get_nhi_settings(db_conn)
        assert settings["rate"] == DEFAULT_NHI_SETTINGS["nhi_supplement_rate"]
        assert settings["threshold"] == DEFAULT_NHI_SETTINGS["nhi_supplement_threshold"]

    def test_reads_custom_settings(self, db_conn):
        db_conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            ("nhi_supplement_rate", "0.025"),
        )
        db_conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            ("nhi_supplement_threshold", "30000"),
        )
        db_conn.commit()
        settings = get_nhi_settings(db_conn)
        assert settings["rate"] == pytest.approx(0.025)
        assert settings["threshold"] == 30000.0


# ---------------------------------------------------------------------------
# _resolve_collateral_value
# ---------------------------------------------------------------------------

from main import _resolve_collateral_value


class TestResolveCollateralValue:
    def test_uses_lots_when_price_available(self, db_conn):
        _insert_stock(db_conn, "2330", price=600.0)
        db_conn.execute(
            "INSERT INTO holdings (stock_id, shares, total_cost) VALUES ('2330', 5000, 2500000)"
        )
        db_conn.commit()

        price, value = _resolve_collateral_value(db_conn, "2330", collateral_lots=3)
        assert price == 600.0
        assert value == 3 * 1000 * 600  # 1,800,000

    def test_falls_back_to_holdings_when_no_lots(self, db_conn):
        """When collateral_lots=0, falls back to get_stock_market_value which uses
        last transaction price (not current_price). Insert a transaction so it has a price."""
        _insert_stock(db_conn, "2330", price=600.0)
        db_conn.execute(
            "INSERT INTO holdings (stock_id, shares, total_cost) VALUES ('2330', 5000, 2500000)"
        )
        db_conn.execute(
            "INSERT INTO transactions (date, stock_id, action, shares, price, fees, transaction_tax)"
            " VALUES ('2026-01-01', '2330', 'buy', 5000, 600.0, 0, 0)"
        )
        db_conn.commit()

        price, value = _resolve_collateral_value(db_conn, "2330", collateral_lots=0)
        # get_stock_market_value = shares (5000) * get_latest_price (600) = 3_000_000
        assert value == pytest.approx(5000 * 600, abs=1)

    def test_unknown_stock_returns_zero(self, db_conn):
        price, value = _resolve_collateral_value(db_conn, "9999", collateral_lots=0)
        assert price == 0.0
        assert value == 0.0
