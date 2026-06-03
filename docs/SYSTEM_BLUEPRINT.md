# 系統架構說明

## 技術架構

```
前端                        後端                        資料層
─────────────────────       ─────────────────────       ──────────────────
index.html                  main.py (FastAPI)            wealth.db (SQLite)
  Vue 3 (CDN global)   ←→    API Routes               ←→  WAL mode
  Bootstrap 5               ├── app/core/database.py      ├── transactions
  Chart.js                  ├── app/services/             ├── holdings
  Glassmorphism UI          │   ├── portfolio.py          ├── stock_info
                            │   ├── quotes.py             ├── cash_dividends
                            │   ├── dividends.py          ├── stock_dividends
                            │   ├── loans.py              ├── loans
                            │   ├── calculations.py       ├── app_settings
                            │   ├── ai.py                 ├── system_audit_logs
                            │   └── backup.py             └── schema_migrations
                            ├── app/models/schemas.py
                            └── app/core/utils.py
```

## 目錄結構

```
app/
├── core/
│   ├── database.py      連線管理、Schema 遷移、設定 CRUD、稽核日誌
│   └── utils.py         股票代號標準化、日期解析、報價解析工具
├── models/
│   └── schemas.py       Pydantic 請求/回應模型（TransactionCreate 等）
└── services/
    ├── portfolio.py     FIFO 成本計算、持股重建、彙整損益、配置資料
    ├── quotes.py        多源報價（TWSE MIS → Stooq → Yahoo Finance 回退鏈）
    ├── dividends.py     現金股息/股票事件同步（Yahoo Finance / MOPS）、重算工作
    ├── loans.py         借貸健康計算（維持率、應付利息）
    ├── calculations.py  交易稅、補充健保費計算
    ├── ai.py            OpenAI / Anthropic 整合，本地規則引擎備援
    └── backup.py        Google Drive 備份（Service Account，drive.file scope）
```

## 關鍵設計決策

### FIFO 成本計算

`portfolio.py::rebuild_holdings_and_realized()` 在每次交易寫入後全量重建持股，確保歷史一致性。成本批次（lots）以 `deque` 儲存，賣出時從最舊批次扣除。

### 彙整明細：全量歷史標的

SQL 基底表用 `FROM (SELECT DISTINCT stock_id FROM transactions)` 而非 `FROM holdings WHERE shares > 0`，確保已出清的股票（shares = 0）仍包含在損益計算中。

### SQLite 多人並發

- 啟動時執行 `PRAGMA journal_mode=WAL`（持久化，跨連線有效）
- `sqlite3.connect(timeout=15)` 防止寫入衝突時立即報錯
- 每次 API 請求建立獨立連線並在 `with` 結束後關閉，符合 SQLite 多執行緒規範

### 報價回退鏈

```
台股（.TW / .TWO） → TWSE MIS（優先，盤中即時）
                   → Stooq（備援）
                   → Yahoo Finance（最終備援）
外股 / ETF         → Yahoo Finance
```

背景執行緒每 15 分鐘在台股交易時間（週一至週五 09:00–13:30 台灣時間）自動刷新持倉報價。

### 最後償還日邏輯

借貸明細顯示的「最後償還日」= `due_date - 1 天`，前端以 `lastRepayDate()` 計算。剩餘天數倒計時同樣扣除 1 天。

### 備份安全

- Google Drive 備份使用 Service Account（`drive.file` scope，最小權限）
- 憑證 JSON 存放於專案目錄外，`.gitignore` 排除所有 `*_key.json`
- 備份前先 `sqlite3.Connection.backup()` 建立熱備份，再 gzip 壓縮後上傳

## 資料庫 Schema

```sql
stock_info       -- 股票基本資料（代號、名稱、類型、板塊、現價）
transactions     -- 交易記錄（買/賣、FIFO 已實現損益）
holdings         -- 持倉快照（重建計算，非直接維護）
cash_dividends   -- 現金股息記錄
stock_dividends  -- 股票事件（除股息、減資、股票股利）
loans            -- 借貸記錄（融資）
app_settings     -- 系統設定（稅率、補充健保）
system_audit_logs -- 操作稽核日誌
schema_migrations -- DB 版本管理
```

## 開發原則

- `main.py` 為 FastAPI 入口，業務邏輯集中在 `app/services/`
- 測試透過 `main.DB_PATH` patch 指向測試用 DB，不污染 production 資料
- Schema 遷移通過 `_SCHEMA_MIGRATIONS` 清單管理，啟動時自動 apply
- 前端無建置步驟，Vue 3 / Bootstrap / Chart.js 全部從 CDN 載入
