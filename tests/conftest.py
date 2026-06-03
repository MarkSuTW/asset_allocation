"""
Test fixtures: in-memory SQLite DB + FastAPI TestClient.

Each test gets a fresh temporary DB so tests are fully isolated.
"""
import sqlite3
import tempfile
import os
from pathlib import Path
from typing import Generator

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# DB fixture helpers
# ---------------------------------------------------------------------------

def _apply_base_schema(conn: sqlite3.Connection) -> None:
    """Apply the base schema (from init_db.create_schema logic)."""
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS stock_info (
            stock_id TEXT PRIMARY KEY,
            chinese_name TEXT,
            asset_type TEXT NOT NULL DEFAULT '個股',
            sector TEXT NOT NULL DEFAULT '其他',
            current_price REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS holdings (
            stock_id TEXT PRIMARY KEY,
            shares REAL NOT NULL DEFAULT 0,
            total_cost REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (stock_id) REFERENCES stock_info(stock_id)
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            stock_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('buy', 'sell')),
            shares REAL NOT NULL,
            price REAL NOT NULL,
            fees REAL NOT NULL DEFAULT 0,
            transaction_tax REAL NOT NULL DEFAULT 0,
            realized_profit REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (stock_id) REFERENCES stock_info(stock_id)
        );
        CREATE TABLE IF NOT EXISTS dividend_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_id TEXT NOT NULL,
            date TEXT NOT NULL,
            amount REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS loans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lender TEXT NOT NULL,
            collateral TEXT NOT NULL,
            collateral_lots REAL NOT NULL DEFAULT 0,
            principal REAL NOT NULL,
            interest_rate REAL NOT NULL,
            start_date TEXT NOT NULL,
            due_date TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
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
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
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
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS system_audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'INFO',
            actor TEXT NOT NULL DEFAULT 'system',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now')),
            description TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_transactions_stock_id ON transactions(stock_id);
        CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);
        CREATE INDEX IF NOT EXISTS idx_cash_dividends_stock_id ON cash_dividends(stock_id);
        CREATE INDEX IF NOT EXISTS idx_stock_dividends_stock_id ON stock_dividends(stock_id);
        """
    )
    conn.commit()


@pytest.fixture()
def db_path(tmp_path: Path) -> Generator[Path, None, None]:
    """Temporary SQLite DB with full schema applied."""
    p = tmp_path / "test_wealth.db"
    conn = sqlite3.connect(p)
    _apply_base_schema(conn)
    conn.close()
    yield p


@pytest.fixture()
def db_conn(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Open connection to the test DB, closed after test."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# FastAPI TestClient fixture (patches DB_PATH)
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """FastAPI TestClient backed by an isolated test DB."""
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    # Reset schema ready flag so it re-runs on first request
    monkeypatch.setattr(main, "_RUNTIME_SCHEMA_READY", False)
    monkeypatch.setattr(main, "_AUTO_STOCK_INFO_REPAIR_DONE", False)
    monkeypatch.setattr(main, "_LOCAL_STOCK_NAME_MAP", None)

    with TestClient(main.app, raise_server_exceptions=True) as c:
        yield c
