# 部署指南：Ubuntu Server + Tailscale

本文說明如何將系統部署到 Ubuntu 伺服器，並透過 Tailscale 讓所有裝置安全連線存取。

---

## 架構說明

```
┌─────────────────────────────────────────────────────┐
│  開發端（Windows / macOS）                            │
│  git push → github.com/MarkSuTW/asset_allocation     │
└──────────────────────┬──────────────────────────────┘
                       │ git pull（./deploy.sh）
                       ▼
┌─────────────────────────────────────────────────────┐
│  Ubuntu Server（VPS / QNAP / Raspberry Pi）          │
│  /opt/asset_allocation/                              │
│  ├── uvicorn :8001  (systemd 管理)                   │
│  ├── wealth.db      (SQLite WAL mode)                │
│  └── Tailscale                                       │
└──────────────────────┬──────────────────────────────┘
                       │ Tailscale VPN（100.x.x.x:8001）
          ┌────────────┼────────────┐
          ▼            ▼            ▼
        手機          平板         電腦
     (Tailscale)  (Tailscale)  (Tailscale)
```

### 為什麼用 Tailscale？

- **零暴露公網**：不需要設定防火牆規則或 DDNS
- **裝置間加密**：WireGuard P2P 加密，比 VPN 更快
- **免費方案**：個人 / 小型家庭使用完全免費（最多 3 個用戶 / 100 台裝置）
- **安裝簡單**：一個指令完成，比 SSH tunneling 容易得多

---

## 前置條件

| 項目          | 需求                                                |
| ------------- | --------------------------------------------------- |
| Ubuntu Server | 22.04 LTS 或 24.04 LTS（x86_64 或 ARM64）           |
| RAM           | 最低 512 MB，建議 1 GB+                             |
| 磁碟          | 最低 2 GB                                           |
| Python        | 3.11+（安裝腳本會自動處理）                         |
| 連線          | 能連網（Tailscale 需要）                            |
| 帳號          | Tailscale 帳號（免費，用 Google / GitHub 登入即可） |

---

## 第一次設定（伺服器端）

### 步驟 1：執行初始化腳本

```bash
# SSH 進入 Ubuntu Server
ssh ubuntu@<server-ip>

# 下載並執行初始化腳本
curl -O https://raw.githubusercontent.com/MarkSuTW/asset_allocation/main/setup-ubuntu.sh
chmod +x setup-ubuntu.sh
./setup-ubuntu.sh
```

腳本會自動完成：安裝 Python 3.11、git、Tailscale、clone 專案、建立虛擬環境、安裝 systemd 服務。

### 步驟 2：連接 Tailscale

```bash
sudo tailscale up
# 會顯示一個網址，用瀏覽器開啟並登入你的 Tailscale 帳號授權
```

查看本機的 Tailscale IP：

```bash
tailscale ip -4
# 輸出範例：100.96.43.21
```

### 步驟 3：設定環境變數

```bash
nano /opt/asset_allocation/.env
```

最低設定：

```env
ALLOWED_ORIGINS=*
DB_PATH=wealth.db
```

可選填寫：

```env
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=...
GDRIVE_CREDENTIALS_PATH=/opt/secrets/gdrive_key.json
GDRIVE_FOLDER_ID=1AbCdEfGhIjKlMnOpQrS
```

### 步驟 4：搬移資料庫

**選項 A：搬移現有 wealth.db**

```bash
# 在開發電腦（Windows）執行
scp C:\Projects\asset_allocation\wealth.db ubuntu@<server-ip>:/opt/asset_allocation/wealth.db
```

**選項 B：從 CSV 重新匯入**

```bash
# 在 Server 上執行
mkdir -p /opt/asset_allocation/data
# 把 CSV 檔上傳到 data/ 後
cd /opt/asset_allocation
source .venv/bin/activate
python init_db.py
```

### 步驟 5：啟動服務

```bash
sudo systemctl start wealth-app
sudo systemctl status wealth-app
```

看到 `Active: active (running)` 即表示成功。

---

## 從各裝置連線

1. 在每台裝置安裝 Tailscale（iOS / Android / Windows / macOS）：https://tailscale.com/download
2. 登入同一個 Tailscale 帳號
3. 開啟瀏覽器，輸入 `http://<tailscale-ip>:8001`

> **注意：** Tailscale IP 格式為 `100.x.x.x`，可在 Tailscale 管理後台（admin.tailscale.com）查看每台裝置的 IP。

---

## 日常更新部署

### 推送程式碼（開發端）

```bash
git add -A
git commit -m "描述你的修改"
git push
```

### 部署到 Server

```bash
# SSH 進入 Server
ssh ubuntu@<server-ip>
cd /opt/asset_allocation

# 部署 main 分支
./deploy.sh

# 或指定分支
./deploy.sh feat/new-feature
```

`deploy.sh` 自動執行：`git pull → pip install → systemctl restart`。

---

## 服務管理

```bash
# 查看狀態
sudo systemctl status wealth-app

# 查看即時 Log
journalctl -u wealth-app -f

# 手動重啟
sudo systemctl restart wealth-app

# 停止服務
sudo systemctl stop wealth-app

# 禁止開機自啟（如需暫停）
sudo systemctl disable wealth-app

# 卸載服務（保留資料）
./remove-ubuntu.sh

# 卸載服務並刪除程式（保留 wealth.db / backups / .env）
./remove-ubuntu.sh --purge-app

# 完整刪除（包含 wealth.db / backups / .env）
./remove-ubuntu.sh --purge-app --purge-data
```

---

## 自動備份設定（選填）

### Google Drive 備份

1. 在 Google Cloud 建立 Service Account，啟用 Drive API
2. 下載 JSON 金鑰，存放在 **專案目錄以外**（例：`/opt/secrets/gdrive_key.json`）
3. 建立 Google Drive 資料夾，將資料夾分享給 Service Account email（設為「編輯者」）
4. 設定 `.env`：

```env
GDRIVE_CREDENTIALS_PATH=/opt/secrets/gdrive_key.json
GDRIVE_FOLDER_ID=<從 Drive 資料夾 URL 複製 ID>
GDRIVE_KEEP_VERSIONS=7
```

5. 測試備份：

```bash
cd /opt/asset_allocation
source .venv/bin/activate
python backup_to_gdrive.py
```

### 設定 cron 排程（每天凌晨 2:00）

```bash
crontab -e
# 加入以下一行
0 2 * * * cd /opt/asset_allocation && .venv/bin/python backup_to_gdrive.py >> /var/log/wealth-backup.log 2>&1
```

---

## 疑難排解

### 服務無法啟動

```bash
journalctl -u wealth-app -n 50 --no-pager
```

常見原因：

- `wealth.db` 不存在 → 執行 `python init_db.py` 或複製 DB 檔
- Port 8001 被佔用 → `sudo lsof -i :8001` 找出並終止

### 從其他裝置連不到

1. 確認兩台裝置都已登入同一個 Tailscale 帳號
2. `ping <tailscale-ip>` 測試連通性
3. 確認服務在執行：`sudo systemctl status wealth-app`

### 報價更新失敗

系統在台股交易時間（週一至週五 09:00–13:30）自動更新報價。如需手動刷新：

```bash
curl -X POST http://localhost:8001/api/stock/refresh-prices
```

### 資料庫被鎖定（Database is locked）

啟動時已設定 WAL 模式與 15 秒鎖等待，正常使用下不應出現。如發生：

```bash
# 確認沒有其他進程佔用 DB
sudo lsof /opt/asset_allocation/wealth.db
```

---

## 多人使用說明

| 情境                           | 結果                                         |
| ------------------------------ | -------------------------------------------- |
| 多人同時**查看**儀表板         | 完全無衝突，WAL 支援無限並發讀取             |
| 多人同時**寫入**（新增交易等） | 自動排隊，每筆最多等 15 秒，日常使用感受不到 |
| 背景股價更新 + 使用者操作      | 安全，WAL 自動處理讀寫分離                   |

此系統設計為讀多寫少（家庭辦公室規模），SQLite WAL 模式完全足夠。
