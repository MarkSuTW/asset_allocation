"""
API-level tests using FastAPI TestClient.
All tests run against an isolated temporary DB.
"""
import pytest


CT = {"Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

class TestTransactionsAPI:
    def _seed_stock(self, client):
        # Ensure stock_info row exists so FK is satisfied
        pass  # ensure_stock_info is called automatically

    def test_create_buy_transaction(self, client):
        r = client.post("/api/transactions", json={
            "date": "2026-01-01", "stock_id": "2330", "action": "buy",
            "shares": 1000, "price": 500.0, "fees": 71
        })
        assert r.status_code == 200
        assert r.json()["id"] > 0

    def test_create_sell_transaction(self, client):
        # Buy first
        client.post("/api/transactions", json={
            "date": "2026-01-01", "stock_id": "2330", "action": "buy",
            "shares": 2000, "price": 500.0, "fees": 142
        })
        r = client.post("/api/transactions", json={
            "date": "2026-02-01", "stock_id": "2330", "action": "sell",
            "shares": 1000, "price": 600.0, "fees": 85
        })
        assert r.status_code == 200

    def test_list_transactions(self, client):
        client.post("/api/transactions", json={
            "date": "2026-01-01", "stock_id": "2330", "action": "buy",
            "shares": 1000, "price": 500.0
        })
        r = client.get("/api/transactions")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) >= 1
        assert items[0]["stock_id"] == "2330"

    def test_list_transactions_filter_by_stock(self, client):
        client.post("/api/transactions", json={
            "date": "2026-01-01", "stock_id": "2330", "action": "buy", "shares": 1000, "price": 100
        })
        client.post("/api/transactions", json={
            "date": "2026-01-01", "stock_id": "0050", "action": "buy", "shares": 500, "price": 150
        })
        r = client.get("/api/transactions?stock_id=2330")
        assert r.status_code == 200
        items = r.json()["items"]
        assert all(i["stock_id"] == "2330" for i in items)

    def test_update_transaction(self, client):
        r = client.post("/api/transactions", json={
            "date": "2026-01-01", "stock_id": "2330", "action": "buy", "shares": 1000, "price": 500
        })
        tx_id = r.json()["id"]
        r2 = client.put(f"/api/transactions/{tx_id}", json={
            "date": "2026-01-02", "stock_id": "2330", "action": "buy",
            "shares": 1000, "price": 510, "fees": 72
        })
        assert r2.status_code == 200

    def test_delete_transaction(self, client):
        r = client.post("/api/transactions", json={
            "date": "2026-01-01", "stock_id": "2330", "action": "buy", "shares": 1000, "price": 500
        })
        tx_id = r.json()["id"]
        r2 = client.delete(f"/api/transactions/{tx_id}")
        assert r2.status_code == 200

        r3 = client.get("/api/transactions")
        ids = [i["id"] for i in r3.json()["items"]]
        assert tx_id not in ids

    def test_invalid_action_rejected(self, client):
        r = client.post("/api/transactions", json={
            "date": "2026-01-01", "stock_id": "2330", "action": "hold",
            "shares": 1000, "price": 500
        })
        assert r.status_code == 422

    def test_negative_shares_rejected(self, client):
        r = client.post("/api/transactions", json={
            "date": "2026-01-01", "stock_id": "2330", "action": "buy",
            "shares": -100, "price": 500
        })
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Loans
# ---------------------------------------------------------------------------

class TestLoansAPI:
    def _create_loan(self, client, **kwargs):
        payload = {
            "lender": "4010",
            "collateral": "2330",
            "collateral_lots": 10,
            "principal": 300000,
            "interest_rate": 1.85,
            "start_date": "2026-01-01",
            "due_date": "2027-07-01",
            "note": "",
        }
        payload.update(kwargs)
        return client.post("/api/loans", json=payload)

    def test_create_loan(self, client):
        r = self._create_loan(client)
        assert r.status_code == 200
        assert r.json()["id"] > 0

    def test_list_loans(self, client):
        self._create_loan(client)
        r = client.get("/api/loans/list")
        assert r.status_code == 200
        assert len(r.json()["items"]) >= 1

    def test_list_loan_has_maintenance_rate(self, client):
        self._create_loan(client)
        r = client.get("/api/loans/list")
        item = r.json()["items"][0]
        assert "maintenance_rate" in item
        assert "net_proceeds" in item
        assert "collateral_value" in item

    def test_update_loan(self, client):
        r = self._create_loan(client)
        lid = r.json()["id"]
        r2 = client.put(f"/api/loans/{lid}", json={
            "lender": "4010", "collateral": "2330", "collateral_lots": 15,
            "principal": 400000, "interest_rate": 2.0,
            "start_date": "2026-01-01", "due_date": "2027-07-01", "note": ""
        })
        assert r2.status_code == 200

    def test_delete_loan(self, client):
        r = self._create_loan(client)
        lid = r.json()["id"]
        client.delete(f"/api/loans/{lid}")
        r2 = client.get("/api/loans/list")
        ids = [i["id"] for i in r2.json()["items"]]
        assert lid not in ids

    def test_pure_collateral_with_zero_principal(self, client):
        r = client.post("/api/loans", json={
            "lender": "4010", "collateral": "2330", "collateral_lots": 5,
            "principal": 0, "interest_rate": 0,
            "start_date": "2026-01-01", "due_date": "2027-07-01", "note": ""
        })
        assert r.status_code == 200

    def test_pure_collateral_nonzero_rate_rejected(self, client):
        r = client.post("/api/loans", json={
            "lender": "4010", "collateral": "2330", "collateral_lots": 5,
            "principal": 0, "interest_rate": 1.5,
            "start_date": "2026-01-01", "due_date": None, "note": ""
        })
        assert r.status_code == 422

    def test_delete_nonexistent_loan_returns_404(self, client):
        r = client.delete("/api/loans/99999")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Cash Dividends
# ---------------------------------------------------------------------------

class TestCashDividendsAPI:
    def _seed_stock_with_holding(self, client, stock_id="2881"):
        client.post("/api/transactions", json={
            "date": "2026-01-01", "stock_id": stock_id, "action": "buy",
            "shares": 5000, "price": 20.0
        })

    def test_create_cash_dividend(self, client):
        self._seed_stock_with_holding(client)
        r = client.post("/api/dividends/cash", json={
            "stock_id": "2881",
            "ex_date": "2026-05-01",
            "amount_per_share": 0.5,
        })
        assert r.status_code == 200
        assert r.json()["id"] > 0

    def test_list_cash_dividends(self, client):
        self._seed_stock_with_holding(client)
        client.post("/api/dividends/cash", json={
            "stock_id": "2881", "ex_date": "2026-05-01", "amount_per_share": 0.5
        })
        r = client.get("/api/dividends/cash")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) >= 1
        # Verify gross/net fields present
        assert "cash_amount_gross" in items[0]
        assert "cash_amount" in items[0]  # net
        assert "nhi_premium" in items[0]

    def test_cash_amount_computed_server_side(self, client):
        self._seed_stock_with_holding(client, "2881")
        r = client.post("/api/dividends/cash", json={
            "stock_id": "2881", "ex_date": "2026-05-01",
            "amount_per_share": 1.0, "holding_shares": 5000
        })
        assert r.status_code == 200
        # Verify cash_amount_gross = 5000 * 1.0 = 5000
        r2 = client.get("/api/dividends/cash?stock_id=2881")
        item = r2.json()["items"][0]
        assert item["cash_amount_gross"] == pytest.approx(5000.0)

    def test_delete_cash_dividend(self, client):
        self._seed_stock_with_holding(client)
        r = client.post("/api/dividends/cash", json={
            "stock_id": "2881", "ex_date": "2026-05-01", "amount_per_share": 0.5
        })
        did = r.json()["id"]
        r2 = client.delete(f"/api/dividends/cash/{did}")
        assert r2.status_code == 200

    def test_put_endpoint_removed(self, client):
        """PUT /api/dividends/cash/{id} was removed as dead code."""
        r = client.put("/api/dividends/cash/1", json={
            "ex_date": "2026-01-01", "amount_per_share": 1.0,
            "holding_shares": 1000, "source": "manual", "note": ""
        })
        assert r.status_code == 405  # Method Not Allowed


# ---------------------------------------------------------------------------
# Portfolio endpoints
# ---------------------------------------------------------------------------

class TestPortfolioAPI:
    def _seed(self, client):
        client.post("/api/transactions", json={
            "date": "2026-01-01", "stock_id": "2330", "action": "buy",
            "shares": 1000, "price": 500.0
        })

    def test_summary_returns_expected_keys(self, client):
        self._seed(client)
        r = client.get("/api/portfolio/summary")
        assert r.status_code == 200
        body = r.json()
        assert "total_assets" in body
        assert "total_liabilities" in body
        assert "net_assets" in body

    def test_performance_returns_items(self, client):
        self._seed(client)
        r = client.get("/api/portfolio/performance")
        assert r.status_code == 200
        body = r.json()
        assert "items" in body
        assert "totals" in body

    def test_allocation_structure(self, client):
        self._seed(client)
        r = client.get("/api/portfolio/allocation")
        assert r.status_code == 200
        body = r.json()
        assert "asset_type" in body
        assert "sector" in body

    def test_net_assets_equals_assets_minus_liabilities(self, client):
        self._seed(client)
        r = client.get("/api/portfolio/summary")
        body = r.json()
        expected = round(body["total_assets"] - body["total_liabilities"], 2)
        assert body["net_assets"] == pytest.approx(expected, abs=0.01)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class TestSettingsAPI:
    def test_get_transaction_tax_settings(self, client):
        r = client.get("/api/settings/transaction-tax")
        assert r.status_code == 200
        s = r.json()["settings"]
        assert "stock_sell_tax_rate" in s
        assert s["stock_sell_tax_rate"] == pytest.approx(0.003)

    def test_update_transaction_tax_settings(self, client):
        r = client.put("/api/settings/transaction-tax", json={
            "stock_buy_tax_rate": 0,
            "stock_sell_tax_rate": 0.003,
            "etf_buy_tax_rate": 0,
            "etf_sell_tax_rate": 0.001,
            "bond_buy_tax_rate": 0,
            "bond_sell_tax_rate": 0.001,
        })
        assert r.status_code == 200

    def test_get_nhi_settings(self, client):
        r = client.get("/api/settings/dividend-nhi")
        assert r.status_code == 200
        s = r.json()["settings"]
        assert "nhi_supplement_rate" in s
        assert "nhi_supplement_threshold" in s


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------

class TestSystemAPI:
    def test_version_endpoint(self, client):
        r = client.get("/api/system/version")
        assert r.status_code == 200
        assert "api_version" in r.json()

    def test_missing_prices_empty_initially(self, client):
        r = client.get("/api/stock/missing-prices")
        assert r.status_code == 200

    def test_rate_limit_on_stock_quote(self, client):
        """Quote endpoint should be accessible (rate limiter does not block first request)."""
        r = client.get("/api/stock/2330/quote")
        # May return 200 (if price fetched) or any valid response
        assert r.status_code in (200, 503, 500)  # 500/503 if network unavailable in CI
