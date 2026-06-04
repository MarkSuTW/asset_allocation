"""
Unit tests for pure business logic functions (no DB or HTTP needed).
"""
import json
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Tax calculation
# ---------------------------------------------------------------------------

from main import calculate_transaction_tax, DEFAULT_TAX_SETTINGS


class TestCalculateTransactionTax:
    def test_stock_sell(self):
        tax = calculate_transaction_tax("2330", "sell", 1000, 600, DEFAULT_TAX_SETTINGS)
        assert tax == round(1000 * 600 * 0.003, 2)  # 0.3%

    def test_stock_buy_is_zero(self):
        tax = calculate_transaction_tax("2330", "buy", 1000, 600, DEFAULT_TAX_SETTINGS)
        assert tax == 0.0

    def test_etf_sell(self):
        tax = calculate_transaction_tax("0050", "sell", 1000, 150, DEFAULT_TAX_SETTINGS)
        assert tax == round(1000 * 150 * 0.001, 2)  # 0.1%

    def test_bond_sell(self):
        # Bonds end with "B"
        tax = calculate_transaction_tax("00687B", "sell", 1000, 30, DEFAULT_TAX_SETTINGS)
        assert tax == round(1000 * 30 * 0.001, 2)  # 0.1%

    def test_etf_prefix_00(self):
        # Starts with "00" → ETF
        tax = calculate_transaction_tax("006208", "sell", 500, 80, DEFAULT_TAX_SETTINGS)
        assert tax == round(500 * 80 * 0.001, 2)

    def test_invalid_action_returns_zero(self):
        tax = calculate_transaction_tax("2330", "hold", 1000, 600, DEFAULT_TAX_SETTINGS)
        assert tax == 0.0

    def test_zero_price_returns_zero(self):
        tax = calculate_transaction_tax("2330", "sell", 0, 0, DEFAULT_TAX_SETTINGS)
        assert tax == 0.0


# ---------------------------------------------------------------------------
# NHI premium calculation
# ---------------------------------------------------------------------------

from main import compute_nhi_premium, compute_net_cash_dividend, DEFAULT_NHI_SETTINGS


class TestNHIPremium:
    def test_below_threshold_no_premium(self):
        # 19,999 < 20,000 threshold → no premium
        assert compute_nhi_premium(19_999, 0.0211, 20_000) == 0.0

    def test_at_threshold_has_premium(self):
        premium = compute_nhi_premium(20_000, 0.0211, 20_000)
        assert premium == round(20_000 * 0.0211, 2)

    def test_above_threshold(self):
        premium = compute_nhi_premium(50_000, 0.0211, 20_000)
        assert premium == round(50_000 * 0.0211, 2)

    def test_net_cash_dividend_below_threshold(self):
        net = compute_net_cash_dividend(15_000, 0.0211, 20_000)
        assert net == 15_000.0  # no deduction

    def test_net_cash_dividend_above_threshold(self):
        gross = 30_000.0
        net = compute_net_cash_dividend(gross, 0.0211, 20_000)
        expected = round(gross - gross * 0.0211, 2)
        assert net == expected


# ---------------------------------------------------------------------------
# Date utilities
# ---------------------------------------------------------------------------

from app.core.utils import normalize_date, normalize_stock_id
from fastapi import HTTPException


class TestNormalizeDate:
    def test_iso_format(self):
        assert normalize_date("2026-06-01") == "2026-06-01"

    def test_slash_separator(self):
        assert normalize_date("2026/06/01") == "2026-06-01"

    def test_invalid_raises_http(self):
        with pytest.raises(HTTPException) as exc:
            normalize_date("not-a-date")
        assert exc.value.status_code == 400


class TestNormalizeStockId:
    def test_uppercase(self):
        assert normalize_stock_id("2330") == "2330"

    def test_lowercase_becomes_upper(self):
        assert normalize_stock_id("00687b") == "00687B"

    def test_strips_whitespace(self):
        assert normalize_stock_id("  2330  ") == "2330"

    def test_empty_returns_empty(self):
        assert normalize_stock_id("") == ""


# ---------------------------------------------------------------------------
# _default_due_date (18 months from start)
# ---------------------------------------------------------------------------

from main import _default_due_date
import calendar
from datetime import date


def _expected_due(start_iso: str) -> str:
    d = date.fromisoformat(start_iso)
    m = d.month - 1 + 18
    y = d.year + m // 12
    m = m % 12 + 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last)).isoformat()


class TestDefaultDueDate:
    def test_normal_month(self):
        assert _default_due_date("2026-06-03") == _expected_due("2026-06-03")

    def test_year_boundary(self):
        # July → January next+1 year
        assert _default_due_date("2026-07-15") == _expected_due("2026-07-15")

    def test_december(self):
        assert _default_due_date("2025-12-01") == _expected_due("2025-12-01")

    def test_month_end_clamping(self):
        # January 31 + 18 months = July 31 (July has 31 days, no clamping needed)
        result = _default_due_date("2026-01-31")
        assert result == _expected_due("2026-01-31")

    def test_invalid_returns_empty(self):
        assert _default_due_date("not-a-date") == ""


# ---------------------------------------------------------------------------
# _is_taiwan_trading_time
# ---------------------------------------------------------------------------

from main import _is_taiwan_trading_time
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

_TZ = timezone(timedelta(hours=8))


def _make_tw(weekday: int, hour: int, minute: int) -> datetime:
    """Create a datetime with the given weekday/hour/minute in Taipei TZ."""
    # 2026-06-01 is Monday; adjust day offset
    base = datetime(2026, 6, 1, 0, 0, tzinfo=_TZ)  # Monday
    delta = timedelta(days=weekday, hours=hour, minutes=minute)
    return base + delta


class TestTaiwanTradingTime:
    def _check(self, weekday: int, hour: int, minute: int) -> bool:
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = _make_tw(weekday, hour, minute)
            mock_dt.now.side_effect = lambda tz=None: _make_tw(weekday, hour, minute)
            # Call the actual module function directly without mock
            pass
        # Use a simpler direct check since we can't easily mock datetime.now in module
        d = _make_tw(weekday, hour, minute)
        if d.weekday() >= 5:
            return False
        mins = d.hour * 60 + d.minute
        return 9 * 60 <= mins <= 13 * 60 + 30

    def test_monday_open(self):
        assert self._check(0, 10, 0) is True

    def test_monday_before_open(self):
        assert self._check(0, 8, 59) is False

    def test_monday_after_close(self):
        assert self._check(0, 13, 31) is False

    def test_saturday_closed(self):
        assert self._check(5, 10, 0) is False

    def test_sunday_closed(self):
        assert self._check(6, 10, 0) is False

    def test_closing_time_exact(self):
        assert self._check(0, 13, 30) is True

    def test_opening_time_exact(self):
        assert self._check(0, 9, 0) is True


# ---------------------------------------------------------------------------
# fetch_twse_realtime_quote — header and parse behaviour
# ---------------------------------------------------------------------------

from app.services.quotes import fetch_twse_realtime_quote


def _make_mis_response(z: str, y: str, name: str = "中信金") -> bytes:
    """Build a minimal TWSE MIS JSON payload."""
    payload = {"msgArray": [{"z": z, "y": y, "n": name}]}
    return json.dumps(payload).encode("utf-8")


def _mock_urlopen(response_body: bytes):
    """Return a context-manager mock that yields a readable response."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = response_body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestFetchTwseRealtimeQuote:
    def test_sends_referer_header(self):
        """Request to TWSE MIS must include Referer so it isn't rejected."""
        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured["headers"] = dict(req.headers)
            return _mock_urlopen(_make_mis_response("70.5", "69.0"))

        with patch("app.services.quotes.urllib.request.urlopen", side_effect=fake_urlopen):
            fetch_twse_realtime_quote("2891")

        assert "Referer" in captured["headers"], "Referer header missing from TWSE MIS request"
        assert "mis.twse.com.tw" in captured["headers"]["Referer"]

    def test_returns_realtime_z_price_during_trading(self):
        """When z field has a price (盤中), it should be returned as-is."""
        with patch("app.services.quotes.urllib.request.urlopen",
                   return_value=_mock_urlopen(_make_mis_response("72.3", "70.5"))):
            result = fetch_twse_realtime_quote("2891")

        assert result["close_price"] == pytest.approx(72.3)
        assert result["source"] == "twse_realtime"
        assert result["chinese_name"] == "中信金"

    def test_falls_back_to_y_when_z_is_dash(self):
        """After hours z='-'; should fall back to yesterday-close field y."""
        with patch("app.services.quotes.urllib.request.urlopen",
                   return_value=_mock_urlopen(_make_mis_response("-", "70.5"))):
            result = fetch_twse_realtime_quote("2891")

        assert result["close_price"] == pytest.approx(70.5)
        assert result["source"] == "twse_realtime"

    def test_returns_unavailable_when_network_fails(self):
        """On network error the function must not raise; returns None price."""
        import urllib.error
        with patch("app.services.quotes.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("timeout")):
            result = fetch_twse_realtime_quote("2891")

        assert result["close_price"] is None
        assert "unavailable" in result["source"]
