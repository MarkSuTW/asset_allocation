# 有價證券資產儀表板

個人 / 家族辦公室的證券資產管理系統。以 FastAPI + SQLite + Vue 3 為核心，提供即時損益、持股彙整、借貸管理、股息追蹤，並可透過 Tailscale 讓多人從任何裝置安全存取。

**技術棧：** `FastAPI` · `SQLite (WAL)` · `Vue 3 (CDN)` · `Bootstrap 5` · `Chart.js` · `Tailscale`

---

## 功能特色

| 模組             | 說明                                                                                           |
| ---------------- | ---------------------------------------------------------------------------------------------- |
| **KPI 儀表板**   | 庫存總市值、累計淨損益（含未實現）、已實現損益（含股息）、預估股息（本年度）、應繳利息         |
| **彙整明細**     | 全部歷史交易標的（含已出清）FIFO 成本、未實現損益；可切換「只看目前有庫存」                    |
| **交易明細**     | 完整交易記錄，含 FIFO 逐筆已實現損益；支援股票/日期篩選                                        |
| **借貸明細**     | 融資借貸管理，自動計算應付利息、維持率；以擔保品下拉篩選；顯示最後償還日                       |
| **圖表**         | 資產配置圓餅圖、損益分析長條圖                                                                 |
| **CSV 匯出**     | 三張表格均可一鍵匯出 CSV                                                                       |
| **即時報價**     | 背景每 15 分鐘自動更新台股報價（盤中交易時間），支援 TWSE MIS / Stooq / Yahoo Finance 三源回退 |
| **股息自動同步** | 從 Yahoo Finance / MOPS 同步現金股息與股票事件                                                 |
| **AI 顧問**      | 整合 OpenAI / Anthropic，無 API Key 時自動切換本地規則引擎                                     |
| **資料備份**     | 本機定時備份 + Google Drive 異地備份                                                           |
| **多人存取**     | SQLite WAL 模式 + Tailscale VPN，多裝置同時連線                                                |

---

## 快速開始（本機開發）

### 環境需求

- Python 3.11+
- Windows / macOS / Linux

### 安裝

```bash
# 1. 複製專案
git clone https://github.com/MarkSuTW/asset_allocation.git
cd asset_allocation

# 2. 安裝相依套件
pip install -r requirements.txt

# 3. 建立資料庫（從 data/*.csv 匯入，或建立空白 DB）
python init_db.py

# 4. 設定環境變數（選填）
cp .env.example .env
# 編輯 .env 填入 API Keys

# 5. 啟動
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8001
```

開啟瀏覽器：**http://127.0.0.1:8001**

---

## 資料匯入（init_db.py）

將券商匯出的 CSV 放入 `data/` 目錄，檔名格式為 `{股票代號}{中文名稱}.csv`（例：`2330台積電.csv`）。

```
data/
├── 2330台積電.csv        ← 交易明細 + 除權息記錄
├── 0050元大台灣50.csv
├── 借款維持率.csv         ← 融資借貸（檔名含「借款」或「維持率」）
└── ...
```

CSV 支援的欄位（模糊比對，無需完全一致）：

| 類型 | 必要欄位                                |
| ---- | --------------------------------------- |
| 交易 | 交易日期、類別（買/賣）、股數、交易價格 |
| 股息 | 除權息日、實領（或總計股息）            |
| 借貸 | 股票代號、已借金額、利率、起息日        |

---

## 生產部署（Ubuntu Server + Tailscale）

詳細說明請見 [docs/DEPLOY.md](docs/DEPLOY.md)。

### 架構概觀

```
任意裝置（手機 / 平板 / 電腦）
       │  Tailscale VPN
       ▼
Ubuntu Server :8001
├── uvicorn (systemd 管理，開機自啟)
├── FastAPI + SQLite WAL
└── 背景排程（股價更新）
```

### 一鍵初始化伺服器

```bash
# 在 Ubuntu Server 上執行
chmod +x setup-ubuntu.sh
./setup-ubuntu.sh
```

### 後續更新部署

```bash
# Windows / macOS（推送到 GitHub）
git push

# Ubuntu Server（拉取最新版並重啟）
./deploy.sh

# 若需卸載服務/刪除部署
./remove-ubuntu.sh
```

若要改成「前端按鈕一鍵重部署」（不手動下指令），請先於 Ubuntu 的 `.env` 加上：

```env
WEB_REDEPLOY_ENABLED=true
WEB_REDEPLOY_TOKEN=<請改成長隨機字串>
# 可選：預設為 wealth-app-redeploy
# WEB_REDEPLOY_UNIT=wealth-app-redeploy
```

---

## 環境變數（.env）

| 變數                      | 說明                                   | 預設值                     |
| ------------------------- | -------------------------------------- | -------------------------- |
| `ALLOWED_ORIGINS`         | CORS 允許來源，Tailscale 環境設為 `*`  | `http://localhost:8001`    |
| `DB_PATH`                 | SQLite 資料庫路徑                      | `wealth.db`                |
| `OPENAI_API_KEY`          | OpenAI API Key（AI 顧問用）            | -                          |
| `OPENAI_MODEL`            | OpenAI 模型                            | `gpt-4.1-mini`             |
| `ANTHROPIC_API_KEY`       | Anthropic API Key（備援）              | -                          |
| `ANTHROPIC_MODEL`         | Anthropic 模型                         | `claude-3-5-sonnet-latest` |
| `GDRIVE_CREDENTIALS_PATH` | Google Drive Service Account JSON 路徑 | -                          |
| `GDRIVE_FOLDER_ID`        | Google Drive 備份資料夾 ID             | -                          |
| `GDRIVE_KEEP_VERSIONS`    | Drive 上保留幾份備份                   | `7`                        |
| `WEB_REDEPLOY_ENABLED`    | 啟用網頁一鍵重部署 API                 | `false`                    |
| `WEB_REDEPLOY_TOKEN`      | 觸發重部署所需 Token                   | -                          |
| `WEB_REDEPLOY_UNIT`       | systemd transient unit 名稱            | `wealth-app-redeploy`      |

---

## 備份

### 本機備份（API）

```bash
curl -X POST http://localhost:8001/api/system/backup-db
```

備份檔存放於 `backups/wealth_backup_YYYYMMDD_HHMMSS.db`。

### Google Drive 異地備份

```bash
# 設定好 .env 後執行
python backup_to_gdrive.py
```

### 自動排程（Ubuntu cron）

```bash
# 每天凌晨 2:00 備份並上傳到 Google Drive
0 2 * * * cd /opt/asset_allocation && .venv/bin/python backup_to_gdrive.py >> /var/log/wealth-backup.log 2>&1
```

---

## API 參考

互動式文件請開啟：**http://127.0.0.1:8001/docs**

| 方法   | 路徑                                  | 說明                                           |
| ------ | ------------------------------------- | ---------------------------------------------- |
| GET    | `/api/portfolio/summary`              | 投資組合總覽 KPI                               |
| GET    | `/api/portfolio/performance`          | 彙整明細（含損益）                             |
| GET    | `/api/portfolio/allocation`           | 資產配置資料（圖表用）                         |
| GET    | `/api/portfolio/expected-dividends`   | 預估股息                                       |
| GET    | `/api/transactions`                   | 交易明細列表                                   |
| POST   | `/api/transactions`                   | 新增交易                                       |
| PUT    | `/api/transactions/{id}`              | 更新交易                                       |
| DELETE | `/api/transactions/{id}`              | 刪除交易                                       |
| GET    | `/api/loans/list`                     | 借貸明細                                       |
| GET    | `/api/loans/health`                   | 借貸健康報告                                   |
| POST   | `/api/loans`                          | 新增借貸                                       |
| PUT    | `/api/loans/{id}`                     | 更新借貸                                       |
| DELETE | `/api/loans/{id}`                     | 刪除借貸                                       |
| GET    | `/api/dividends/cash`                 | 現金股息記錄                                   |
| POST   | `/api/dividends/cash`                 | 新增現金股息                                   |
| DELETE | `/api/dividends/cash/{id}`            | 刪除現金股息                                   |
| GET    | `/api/dividends/stock`                | 股票事件記錄                                   |
| POST   | `/api/dividends/stock`                | 新增股票事件                                   |
| PUT    | `/api/dividends/stock/{id}`           | 更新股票事件                                   |
| DELETE | `/api/dividends/stock/{id}`           | 刪除股票事件                                   |
| POST   | `/api/dividends/auto-sync`            | 自動同步股息（Yahoo/MOPS）                     |
| POST   | `/api/dividends/recalc-jobs`          | 啟動股息重算工作                               |
| GET    | `/api/dividends/recalc-jobs/{job_id}` | 查詢重算進度                                   |
| GET    | `/api/stock/{id}/quote`               | 取得單一股票報價                               |
| POST   | `/api/stock/refresh-prices`           | 批次刷新報價                                   |
| GET    | `/api/settings/transaction-tax`       | 取得交易稅設定                                 |
| PUT    | `/api/settings/transaction-tax`       | 更新交易稅設定                                 |
| GET    | `/api/settings/dividend-nhi`          | 取得補充健保設定                               |
| PUT    | `/api/settings/dividend-nhi`          | 更新補充健保設定                               |
| POST   | `/api/system/backup-db`               | 建立本機備份（`?offsite=true` 同時上傳 Drive） |
| POST   | `/api/system/redeploy/start`          | 啟動網頁一鍵重部署（需 `X-Deploy-Token`）      |
| GET    | `/api/system/redeploy/status`         | 查詢重部署工作狀態與最近 log                    |
| GET    | `/api/system/audit-logs`              | 系統稽核日誌                                   |
| GET    | `/api/system/data-health`             | 資料健康報告                                   |
| POST   | `/api/ai/advisor`                     | AI 投資顧問                                    |

---

## 目錄結構

```
asset_allocation/
├── main.py                  # FastAPI 入口，API 路由
├── init_db.py               # ETL：從 CSV 匯入資料建立 DB
├── index.html               # 前端（Vue 3 SPA，由 FastAPI 直接提供）
├── backup_to_gdrive.py      # Google Drive 備份腳本
├── setup-ubuntu.sh          # Ubuntu 伺服器初始化腳本
├── deploy.sh                # 部署更新腳本（在 server 執行）
├── remove-ubuntu.sh         # 卸載服務 / 清理部署腳本
├── wealth-app.service       # systemd 服務設定
├── requirements.txt
├── .env.example
├── app/
│   ├── core/
│   │   ├── database.py      # 連線、Schema 遷移、設定 CRUD
│   │   └── utils.py         # 標準化工具函式
│   ├── models/
│   │   └── schemas.py       # Pydantic 請求/回應模型
│   └── services/
│       ├── portfolio.py     # FIFO 成本、持股重建、損益計算
│       ├── quotes.py        # 多源報價取得（TWSE MIS / Stooq / Yahoo）
│       ├── dividends.py     # 股息同步、股票事件計算
│       ├── loans.py         # 借貸健康計算
│       ├── calculations.py  # 稅率、補充健保計算
│       ├── ai.py            # AI 顧問整合
│       └── backup.py        # Google Drive 備份
├── data/                    # CSV 原始資料（匯入後不需保留）
├── backups/                 # 本機備份檔
├── docs/
│   ├── DEPLOY.md            # 詳細部署說明
│   └── SYSTEM_BLUEPRINT.md  # 系統架構說明
└── tests/
    ├── test_unit.py
    ├── test_api.py
    ├── test_integration.py
    └── conftest.py
```

---

## 測試

```bash
pytest tests/ -v
```

詳見 [tests/README.md](tests/README.md)。
