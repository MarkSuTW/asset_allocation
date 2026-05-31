# Family Office 資產戰情儀表板

技術棧：FastAPI + SQLite + Vue 3 + Bootstrap 5 + Chart.js

## 1) 安裝與初始化

```bash
pip install -r requirements.txt
python init_db.py
```

執行完成後會產生 `wealth.db`。

## 2) 啟動 API + 前端

```bash
uvicorn main:app --reload
```

啟動後打開：

- http://127.0.0.1:8000/

## 3) API 清單

- `GET /api/portfolio/summary`
- `GET /api/portfolio/performance`
- `GET /api/portfolio/allocation`
- `GET /api/portfolio/expected-dividends`
- `GET /api/loans/health`
- `GET /api/transactions`
- `POST /api/transactions`
- `PUT /api/transactions/{tx_id}`
- `DELETE /api/transactions/{tx_id}`
- `GET /api/stock/{stock_id}/quote`
- `GET /api/settings/transaction-tax`
- `PUT /api/settings/transaction-tax`
- `POST /api/settings/repair-stock-info`
- `POST /api/dividends/auto-sync`
- `GET /api/system/version`
- `GET /api/system/audit-logs`
- `GET /api/dividends/cash`
- `POST /api/dividends/cash`
- `PUT /api/dividends/cash/{dividend_id}`
- `DELETE /api/dividends/cash/{dividend_id}`
- `GET /api/dividends/stock`
- `POST /api/dividends/stock`
- `PUT /api/dividends/stock/{dividend_id}`
- `DELETE /api/dividends/stock/{dividend_id}`
- `GET /api/stock/missing-prices`
- `POST /api/stock/refresh-missing-prices`
- `POST /api/ai/advisor`

## 4) LLM 設定（擇一）

可建立 `.env` 並填入：

- `OPENAI_API_KEY`（可搭配 `OPENAI_MODEL`）
- `ANTHROPIC_API_KEY`（可搭配 `ANTHROPIC_MODEL`）

若未設定 API Key，`/api/ai/advisor` 會自動使用本地規則引擎回覆。
