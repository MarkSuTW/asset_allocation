import argparse
import csv
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

DB_PATH = Path("wealth.db")
DATA_DIR = Path("data")

EXCEL_ERROR_CODES = {"#N/A", "#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#NULL!"}

SECTOR_RULES = [
    ("半導體", ["台積", "聯發", "南亞科", "旺宏", "天鈺", "光聖", "南茂", "聯電"]),
    ("金融業", ["富邦", "國泰", "玉山", "兆豐", "中信", "合庫", "上海商", "金", "銀"]),
    ("航運", ["長榮", "陽明", "萬海", "華航", "虎航"]),
    ("通訊網路", ["5G", "網路", "資安", "合勤"]),
    ("電子製造", ["廣達", "仁寶", "和碩", "緯創", "佳世達", "藍天", "彩晶", "群創"]),
    ("原物料", ["中鋼", "台泥", "亞泥", "台塑", "台化", "台達化", "台玻", "華紙", "台肥"]),
    ("觀光餐旅", ["晶華", "雄獅", "八方"]),
]


# ---------- Schema ----------
def create_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_info (
            stock_id TEXT PRIMARY KEY,
            chinese_name TEXT,
            asset_type TEXT NOT NULL,
            sector TEXT NOT NULL,
            current_price REAL NOT NULL DEFAULT 0
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS holdings (
            stock_id TEXT PRIMARY KEY,
            shares REAL NOT NULL DEFAULT 0,
            total_cost REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (stock_id) REFERENCES stock_info(stock_id)
        )
        """
    )
    cur.execute(
        """
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
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS dividend_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_id TEXT NOT NULL,
            date TEXT NOT NULL,
            amount REAL NOT NULL,
            FOREIGN KEY (stock_id) REFERENCES stock_info(stock_id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS loans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lender TEXT NOT NULL,
            collateral TEXT NOT NULL,
            principal REAL NOT NULL,
            interest_rate REAL NOT NULL,
            start_date TEXT NOT NULL
        )
        """
    )
    conn.commit()


def reset_data(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM transactions")
    cur.execute("DELETE FROM dividend_records")
    cur.execute("DELETE FROM holdings")
    cur.execute("DELETE FROM loans")
    cur.execute("DELETE FROM stock_info")
    conn.commit()


# ---------- Normalization ----------
def normalize_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def clean_numeric(raw: object) -> float:
    s = normalize_text(raw)
    if not s:
        return 0.0

    if s.upper() in EXCEL_ERROR_CODES:
        return 0.0

    s = s.replace(",", "")
    s = s.replace("$", "")
    s = s.replace("NT$", "")
    s = s.replace("%", "")
    s = re.sub(r"[^0-9.\-]", "", s)

    if s in {"", "-", ".", "-."}:
        return 0.0

    try:
        return float(s)
    except ValueError:
        return 0.0


def normalize_date(raw: object) -> str:
    s = normalize_text(raw)
    if not s:
        return datetime.today().date().isoformat()

    # JS Date 字串: Wed Apr 29 2026 00:00:00 GMT+0800 (...)
    if "GMT" in s and ":" in s:
        m = re.match(r"^[A-Za-z]{3} ([A-Za-z]{3}) (\d{1,2}) (\d{4})", s)
        if m:
            mon = m.group(1)
            day = int(m.group(2))
            year = int(m.group(3))
            month_map = {
                "Jan": 1,
                "Feb": 2,
                "Mar": 3,
                "Apr": 4,
                "May": 5,
                "Jun": 6,
                "Jul": 7,
                "Aug": 8,
                "Sep": 9,
                "Oct": 10,
                "Nov": 11,
                "Dec": 12,
            }
            if mon in month_map:
                return datetime(year, month_map[mon], day).date().isoformat()

    s = s.replace("年", "-").replace("月", "-").replace("日", "")
    s = s.replace("/", "-").replace(".", "-")
    s = re.sub(r"\s+", "", s)

    parts = s.split("-")
    if len(parts) >= 3:
        try:
            y = int(parts[0])
            if y < 1911:
                y += 1911
            m = int(parts[1])
            d = int(parts[2])
            return datetime(y, m, d).date().isoformat()
        except ValueError:
            pass

    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue

    return datetime.today().date().isoformat()


# ---------- Read helpers ----------
def read_rows_with_fallback(path: Path) -> Tuple[List[List[str]], str]:
    for enc in ("utf-8-sig", "big5", "cp950"):
        try:
            with path.open("r", encoding=enc, errors="strict", newline="") as f:
                rows = list(csv.reader(f))
            return rows, enc
        except Exception:
            continue

    # 最後保底：忽略錯字元，避免整檔失敗
    with path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
        rows = list(csv.reader(f))
    return rows, "utf-8-sig"


def load_df(path: Path, encoding: str, skiprows: int) -> pd.DataFrame:
    return pd.read_csv(path, encoding=encoding, skiprows=skiprows, dtype=str)


def find_col(columns: Iterable[str], candidates: Iterable[str]) -> Optional[str]:
    cols = [normalize_text(c) for c in columns]
    for cand in candidates:
        cand_l = cand.lower()
        for c in cols:
            if cand_l == c.lower():
                return c
        for c in cols:
            if cand_l in c.lower():
                return c
    return None


def parse_stock_id_from_filename(path: Path) -> Optional[str]:
    m = re.match(r"^([0-9]{4,5}[A-Z]?)", path.stem)
    return m.group(1) if m else None


def extract_chinese_name(path: Path, stock_id: str) -> Optional[str]:
    """Extract Chinese name from filename. E.g., '2330台積電.csv' -> '台積電'"""
    stem = path.stem
    if stock_id and stem.startswith(stock_id):
        return stem[len(stock_id):].strip() or None
    return None


def infer_asset_type(stock_id: str, name: str) -> str:
    if stock_id.endswith("B"):
        return "債券"
    if stock_id.startswith("00") or "ETF" in name.upper() or "高股息" in name:
        return "ETF"
    return "個股"


def infer_sector(name: str) -> str:
    for sector, keywords in SECTOR_RULES:
        if any(k in name for k in keywords):
            return sector
    return "其他"


def upsert_stock_info(conn: sqlite3.Connection, stock_id: str, asset_type: str, sector: str, chinese_name: Optional[str] = None, current_price: float = 0.0) -> None:
    conn.execute(
        """
        INSERT INTO stock_info (stock_id, chinese_name, asset_type, sector, current_price)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(stock_id) DO UPDATE SET
            chinese_name=COALESCE(excluded.chinese_name, chinese_name),
            asset_type=excluded.asset_type,
            sector=excluded.sector,
            current_price=excluded.current_price
        """,
        (stock_id, chinese_name, asset_type, sector, current_price),
    )


# ---------- Holding math ----------
def apply_transaction_to_holding(
    conn: sqlite3.Connection,
    stock_id: str,
    action: str,
    shares: float,
    price: float,
    fees: float,
) -> float:
    row = conn.execute(
        "SELECT shares, total_cost FROM holdings WHERE stock_id = ?",
        (stock_id,),
    ).fetchone()

    cur_shares = float(row[0]) if row else 0.0
    cur_cost = float(row[1]) if row else 0.0
    realized = 0.0

    if action == "buy":
        new_shares = cur_shares + shares
        new_cost = cur_cost + shares * price + fees
    else:
        sell_shares = min(shares, cur_shares)
        avg_cost = (cur_cost / cur_shares) if cur_shares > 0 else 0.0
        cost_basis = avg_cost * sell_shares
        proceeds = sell_shares * price
        realized = proceeds - cost_basis - fees

        new_shares = cur_shares - sell_shares
        new_cost = cur_cost - cost_basis
        if new_shares <= 1e-9:
            new_shares = 0.0
            new_cost = 0.0

    conn.execute(
        """
        INSERT INTO holdings (stock_id, shares, total_cost)
        VALUES (?, ?, ?)
        ON CONFLICT(stock_id) DO UPDATE SET
            shares=excluded.shares,
            total_cost=excluded.total_cost
        """,
        (stock_id, new_shares, max(new_cost, 0.0)),
    )
    return realized


# ---------- Parsers ----------
def find_header_row(rows: List[List[str]], required_tokens: List[str], max_scan: int = 40) -> Optional[int]:
    for i, row in enumerate(rows[:max_scan]):
        joined = ",".join(normalize_text(x) for x in row)
        if all(tok in joined for tok in required_tokens):
            return i
    return None


def parse_stock_file(conn: sqlite3.Connection, path: Path, rows: List[List[str]], encoding: str) -> Tuple[int, int]:
    stock_id = parse_stock_id_from_filename(path)
    if not stock_id:
        return 0, 0

    name = path.stem
    chinese_name = extract_chinese_name(path, stock_id)
    upsert_stock_info(conn, stock_id, infer_asset_type(stock_id, name), infer_sector(name), chinese_name, 0.0)

    tx_count = 0
    div_count = 0
    latest_price = 0.0

    # 交易區：常見欄位「序號,交易日期,股數,類別,交易價格...」
    tx_header = find_header_row(rows, ["交易日期", "股數"], max_scan=15)
    if tx_header is not None:
        tx_df = load_df(path, encoding, tx_header)
        tx_df.columns = [normalize_text(c) for c in tx_df.columns]

        date_col = find_col(tx_df.columns, ["交易日期", "日期", "date"])
        action_col = find_col(tx_df.columns, ["類別", "買賣", "交易別", "action"])
        shares_col = find_col(tx_df.columns, ["股數", "數量", "shares"])
        price_col = find_col(tx_df.columns, ["交易價格", "成交價", "單價", "price"])
        fees_col = find_col(tx_df.columns, ["手續費", "交易費用", "fees"])

        if date_col and action_col and shares_col and price_col:
            for _, row in tx_df.iterrows():
                raw_action = normalize_text(row.get(action_col, "")).lower()
                raw_shares = clean_numeric(row.get(shares_col, 0))
                raw_price = clean_numeric(row.get(price_col, 0))
                raw_amount = clean_numeric(row.get(find_col(tx_df.columns, ["成交金額", "成交額", "amount"]), 0)) if find_col(tx_df.columns, ["成交金額", "成交額", "amount"]) else 0.0

                if any(k in raw_action for k in ["買", "buy"]):
                    action = "buy"
                elif any(k in raw_action for k in ["賣", "sell"]):
                    action = "sell"
                else:
                    if raw_shares < 0 or raw_amount < 0:
                        action = "sell"
                    elif raw_shares > 0 or raw_amount > 0:
                        action = "buy"
                    else:
                        continue

                shares = abs(raw_shares)
                price = abs(raw_price)
                fees = clean_numeric(row.get(fees_col, 0)) if fees_col else 0.0
                tx_date = normalize_date(row.get(date_col))

                if shares <= 0 or price <= 0:
                    continue

                # 部分 CSV 會把賣出列的股數或成交金額寫成負值；匯入時統一存成正數並以 action 表示方向。
                if raw_shares < 0 or raw_amount < 0:
                    action = "sell"

                realized = apply_transaction_to_holding(conn, stock_id, action, shares, price, fees)
                tax_rate = 0.001 if stock_id.startswith("00") else 0.003
                transaction_tax = round(shares * price * tax_rate, 2) if action == "sell" else 0.0
                conn.execute(
                    """
                    INSERT INTO transactions (date, stock_id, action, shares, price, fees, transaction_tax, realized_profit)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (tx_date, stock_id, action, shares, price, fees, transaction_tax, realized),
                )
                latest_price = price
                tx_count += 1

    # 除權息區：常見欄位「年度,除權息日,...,總計股息,補充健保費,實領...」
    div_header = find_header_row(rows, ["除權"], max_scan=30)
    if div_header is not None:
        div_df = load_df(path, encoding, div_header)
        div_df.columns = [normalize_text(c) for c in div_df.columns]

        date_col = find_col(div_df.columns, ["股利發放日", "除權息日", "日期", "date"])
        amount_col = find_col(div_df.columns, ["實領", "總計股息", "現金股利", "股利", "股息"])

        if date_col and amount_col:
            for _, row in div_df.iterrows():
                amount = clean_numeric(row.get(amount_col, 0))
                if amount <= 0:
                    continue
                d = normalize_date(row.get(date_col))
                conn.execute(
                    """
                    INSERT INTO dividend_records (stock_id, date, amount)
                    VALUES (?, ?, ?)
                    """,
                    (stock_id, d, amount),
                )
                div_count += 1

    if latest_price > 0:
        conn.execute("UPDATE stock_info SET current_price = ? WHERE stock_id = ?", (latest_price, stock_id))

    return tx_count, div_count


def parse_loan_file(conn: sqlite3.Connection, path: Path, rows: List[List[str]], encoding: str) -> int:
    header = find_header_row(rows, ["股票代號", "已借金額"], max_scan=10)
    if header is None:
        header = 0

    df = load_df(path, encoding, header)
    df.columns = [normalize_text(c) for c in df.columns]

    stock_col = find_col(df.columns, ["股票代號", "stock_id"])
    name_col = find_col(df.columns, ["股票名稱", "name"])
    principal_col = find_col(df.columns, ["已借金額", "借款金額", "本金", "principal"])
    rate_col = find_col(df.columns, ["利率", "年利率", "interest_rate"])
    date_col = find_col(df.columns, ["起息日", "開始日", "date"])
    collateral_col = find_col(df.columns, ["總市值", "擔保品", "collateral"])

    inserted = 0
    for _, row in df.iterrows():
        stock_id = normalize_text(row.get(stock_col, "")) if stock_col else ""
        principal = clean_numeric(row.get(principal_col, 0)) if principal_col else 0.0
        if principal <= 0:
            continue

        rate = clean_numeric(row.get(rate_col, 0)) if rate_col else 0.0
        if rate > 1:
            rate = rate / 100.0

        start_date = normalize_date(row.get(date_col)) if date_col else datetime.today().date().isoformat()
        collateral_value = clean_numeric(row.get(collateral_col, 0)) if collateral_col else 0.0
        collateral = str(collateral_value) if collateral_value > 0 else stock_id

        conn.execute(
            """
            INSERT INTO loans (lender, collateral, principal, interest_rate, start_date)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("元大證金", collateral, principal, rate, start_date),
        )
        inserted += 1

        if stock_id:
            display_name = normalize_text(row.get(name_col, "")) if name_col else stock_id
            upsert_stock_info(conn, stock_id, infer_asset_type(stock_id, display_name), infer_sector(display_name))

    return inserted


def run_etl(data_dir: Path, db_path: Path) -> Dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        create_schema(conn)
        reset_data(conn)

        stats = {
            "files": 0,
            "stock_info": 0,
            "transactions": 0,
            "dividends": 0,
            "loans": 0,
        }

        for path in sorted(data_dir.glob("*.csv")):
            stats["files"] += 1
            filename = path.stem

            rows, encoding = read_rows_with_fallback(path)

            if "借款" in filename or "維持率" in filename:
                stats["loans"] += parse_loan_file(conn, path, rows, encoding)
                continue

            stock_id = parse_stock_id_from_filename(path)
            if not stock_id:
                continue

            tx, div = parse_stock_file(conn, path, rows, encoding)
            if tx > 0 or div > 0:
                stats["stock_info"] += 1
            stats["transactions"] += tx
            stats["dividends"] += div

        conn.commit()
        return stats
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize wealth.db from data/*.csv")
    parser.add_argument("--data-dir", default=str(DATA_DIR), help="CSV data directory")
    parser.add_argument("--db-path", default=str(DB_PATH), help="SQLite DB path")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    db_path = Path(args.db_path)

    if not data_dir.exists():
        raise FileNotFoundError(f"Data directory not found: {data_dir}")

    stats = run_etl(data_dir, db_path)
    print("ETL finished:")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
