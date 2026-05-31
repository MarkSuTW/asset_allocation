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
- `GET /api/system/data-health`
- `POST /api/system/backup-db`
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

## 5) 資料庫備份（本地 + Google Drive 異地）

### 本地備份

- API：`POST /api/system/backup-db`
- 備份檔會存到 `backups/wealth_backup_YYYYMMDD_HHMMSS.db`

### 備份到 Google Drive

1. 在 Google Cloud 建立 Service Account，啟用 Drive API。
2. 下載 service account JSON 憑證（例如 `service-account.json`）。
3. 將目標 Google Drive 資料夾分享給該 service account 的 email。
4. 設定環境變數：

```bash
GOOGLE_SERVICE_ACCOUNT_FILE=service-account.json
GOOGLE_DRIVE_FOLDER_ID=<your_folder_id>
```

5. 呼叫：

```bash
POST /api/system/backup-db?offsite=true
```

### Windows 每日排程範例（22:00）

```powershell
schtasks /Create /SC DAILY /ST 22:00 /TN "asset_allocation_backup" /TR "powershell -NoProfile -Command \"Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/system/backup-db?offsite=true\"" /F
```

注意：排程執行時需確保 API 服務在執行，且上述 Google Drive 環境變數對該執行帳號可見。
