# Release Notes

## v2.4 — 多人部署與備份 (2026-06)

### 新功能
- **Ubuntu + Tailscale 部署支援**：`setup-ubuntu.sh`、`wealth-app.service`、`deploy.sh` 三件套，一鍵初始化生產環境
- **SQLite WAL 模式**：啟動時自動設定 WAL + 15 秒 busy timeout，支援多裝置同時讀取
- **Google Drive 異地備份模組**：`app/services/backup.py`，Service Account 最小權限，自動保留 N 份並清除舊版本
- **快速備份腳本**：`backup_to_gdrive.py` 可直接執行或排程

### 設定變更
- `.env.example` 新增 `ALLOWED_ORIGINS`、`GDRIVE_*` 系列變數
- `.gitignore` 新增 `*_key.json` 等憑證檔案排除規則

---

## v2.3 — UI/UX 全面重設計 (2026-06)

### 介面重構
- **KPI 整併**：7 張卡片 → 5 張（移除重複指標），版面更清晰
- **新增 KPI**：「累計淨損益（含未實現）」，解釋已實現損益與含未實現損益的差距
- **KPI 卡片**：彩色條紋設計（cyan / green / lime / amber / red），glassmorphism 風格
- **導航列**：更換為帶 emoji 的 pill 式按鈕
- **移除維持率量表圖**：借貸健康數字移至 KPI 卡片子標題，減少視覺雜訊

### 借貸明細
- 「到期日」改為「最後償還日」（= due_date − 1 天）
- 篩選條件改為擔保品下拉選單（原：借款機構 + 日期範圍）
- 下拉選項同時顯示「證券代號 + 中文名稱」（例：`2330 台積電`）

### 資料表
- 三張表格均新增 CSV 匯出按鈕
- 交易明細新增匯總 strip（總買入 / 總賣出 / 淨損益）

---

## v2.2 — 彙整明細修正 (2026-06)

### 修正
- **彙整明細顯示全量標的**：修正「只看目前有庫存」取消後仍只顯示 15 筆的問題
- 根本原因：後端 SQL 原先以 `FROM holdings WHERE shares > 0` 為基底，改為 `FROM (SELECT DISTINCT stock_id FROM transactions)` 確保 69 檔（含已出清 54 檔）全部回傳
- 已出清股票不向外部請求即時報價（避免不必要的 API 呼叫）

---

## v2.1 — 後端模組化重構 (2026-05)

### 架構改動
- `main.py`（~1500 行）拆分為 `app/` 模組層：`core/`、`models/`、`services/`
- 75 個自動化測試全數通過（`tests/test_unit.py`、`test_api.py`、`test_integration.py`）
- 維持 `main.py` 作為入口，向下相容既有測試的 patch 路徑

---

## v2.0 — 伺服器端排程與測試套件 (2026-05)

### 新功能
- **背景報價排程**：台股交易時間（週一至週五 09:00–13:30）每 15 分鐘自動更新持倉報價
- **手動刷新按鈕**：前端右上角刷新按鈕，同步中禁止重複點擊
- **報價競賽修正**：前端報價更新競態條件（race condition）修正
- **TWSE MIS 優先**：台股報價優先使用台灣證交所 MIS 即時資料
- **完整 pytest 套件**：75 個測試，覆蓋 FIFO 計算、API 端點、整合場景

---

## v1.x — 初始版本

- FastAPI + SQLite 基礎架構
- Vue 3 + Bootstrap 5 前端儀表板
- FIFO 持倉計算
- 現金股息 / 股票事件管理
- AI 顧問（OpenAI / Anthropic / 規則引擎）
- 資料稽核日誌
- Google Drive 備份 API（`POST /api/system/backup-db?offsite=true`）
