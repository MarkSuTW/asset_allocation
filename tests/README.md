# 測試說明

## 執行測試

```bash
# 安裝測試相依套件（已含於 requirements.txt）
pip install -r requirements.txt

# 執行所有測試
pytest tests/ -v

# 執行特定檔案
pytest tests/test_unit.py -v
pytest tests/test_api.py -v
pytest tests/test_integration.py -v

# 顯示測試涵蓋率
pytest tests/ --cov=app --cov-report=term-missing
```

## 測試架構

| 檔案 | 說明 |
|---|---|
| `conftest.py` | 共用 fixture（測試用 DB、FastAPI TestClient）|
| `test_unit.py` | 單元測試：FIFO 計算、稅率、補充健保、日期工具 |
| `test_api.py` | API 端點測試：交易 CRUD、借貸、股息、設定 |
| `test_integration.py` | 整合測試：完整交易流程、持倉重建、損益驗證 |

## 關鍵 Fixture

```python
# conftest.py
@pytest.fixture
def db_path(tmp_path):
    # 建立獨立的測試用 SQLite DB，與 wealth.db 完全隔離
    ...

@pytest.fixture
def client(db_path, monkeypatch):
    # 將 main.DB_PATH patch 到測試 DB，取得 FastAPI TestClient
    ...
```

每個測試使用獨立的 `tmp_path` 資料庫，互不干擾，也不會影響 `wealth.db`。

## 目前覆蓋範圍

- FIFO 成本計算（買入、賣出、多批次）
- 交易稅計算（股票 0.3%、ETF 0.1%、債券 0.1%）
- 補充健保費計算（單次 / 年度累計）
- 持倉重建（含股票事件：除股、減資）
- 借貸利息與維持率計算
- API 端點 CRUD：交易、借貸、現金股息、股票事件
- 股息自動同步邏輯
- 損益彙整（已實現 / 未實現 / 含股息）
