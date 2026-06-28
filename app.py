from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import shutil
import smtplib
import sqlite3
import subprocess
import threading
import time
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("ATLAS_DATA_DIR", str(APP_DIR / "data")))
UPLOAD_DIR = DATA_DIR / "uploads"
LOCAL_TESSDATA_DIR = DATA_DIR / "tessdata"
DB_PATH = DATA_DIR / "atlas_mailer.sqlite3"
PREVIEW_PATH = DATA_DIR / "email_preview.html"
HOST = os.getenv("HOST", "0.0.0.0" if os.getenv("PORT") else "127.0.0.1")
PORT = int(os.getenv("PORT", "8080"))
MARKET_CACHE_HOURS = int(os.getenv("MARKET_CACHE_HOURS", "20"))
CLEAR_USER_DATA_AFTER_SUCCESS = os.getenv("CLEAR_USER_DATA_AFTER_SUCCESS", "1") != "0"


def configure_data_paths(base_dir: Path) -> None:
    global DATA_DIR, UPLOAD_DIR, LOCAL_TESSDATA_DIR, DB_PATH, PREVIEW_PATH
    DATA_DIR = base_dir
    UPLOAD_DIR = DATA_DIR / "uploads"
    LOCAL_TESSDATA_DIR = DATA_DIR / "tessdata"
    DB_PATH = DATA_DIR / "atlas_mailer.sqlite3"
    PREVIEW_PATH = DATA_DIR / "email_preview.html"


def ensure_storage() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fallback_dir = APP_DIR / "data"
        print(f"Could not create ATLAS_DATA_DIR={DATA_DIR}: {exc}. Falling back to {fallback_dir}.")
        configure_data_paths(fallback_dir)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_TESSDATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS holdings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                name TEXT,
                quantity REAL,
                avg_price REAL,
                current_price REAL,
                currency TEXT,
                memo TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        existing_holding_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(holdings)").fetchall()
        }
        if "current_price" not in existing_holding_columns:
            conn.execute("ALTER TABLE holdings ADD COLUMN current_price REAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                ocr_text TEXT,
                note TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        existing_upload_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(uploads)").fetchall()
        }
        if "ocr_text" not in existing_upload_columns:
            conn.execute("ALTER TABLE uploads ADD COLUMN ocr_text TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS send_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_for_date TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS market_cache (
                cache_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS investment_diary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                diary_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                name TEXT NOT NULL,
                suggestion TEXT NOT NULL,
                confidence INTEGER,
                suggested_price REAL,
                currency TEXT,
                user_action TEXT,
                action_note TEXT,
                action_date TEXT,
                eval_1w TEXT,
                eval_1m TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(diary_date, ticker)
            )
            """
        )


def now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_text() -> str:
    return dt.date.today().isoformat()


def db_rows(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchall()


def db_execute(query: str, params: tuple = ()) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(query, params)


def get_setting(key: str, default: str = "") -> str:
    rows = db_rows("SELECT value FROM settings WHERE key = ?", (key,))
    return rows[0]["value"] if rows else default


def set_setting(key: str, value: str) -> None:
    db_execute(
        """
        INSERT INTO settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value.strip()),
    )


def get_settings() -> dict[str, str]:
    return {
        "recipient_email": get_setting("recipient_email"),
        "send_time": get_setting("send_time", "07:00"),
        "user_name": get_setting("user_name", "사용자"),
        "cash_krw": get_setting("cash_krw", "0"),
        "cash_usd": get_setting("cash_usd", "0"),
        "smtp_host": os.getenv("SMTP_HOST") or get_setting("smtp_host", "smtp.gmail.com"),
        "smtp_port": os.getenv("SMTP_PORT") or get_setting("smtp_port", "587"),
        "smtp_user": os.getenv("SMTP_USER") or get_setting("smtp_user", ""),
        "smtp_password": os.getenv("SMTP_PASSWORD") or get_setting("smtp_password", ""),
        "smtp_from": os.getenv("SMTP_FROM") or get_setting("smtp_from", ""),
        "resend_api_key": os.getenv("RESEND_API_KEY") or get_setting("resend_api_key", ""),
        "resend_from": os.getenv("RESEND_FROM") or get_setting("resend_from", ""),
        "alpha_vantage_key": os.getenv("ALPHA_VANTAGE_KEY") or get_setting("alpha_vantage_key", ""),
    }


def escape(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def parse_float(value: str) -> float | None:
    cleaned = value.strip().replace(",", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def as_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fmt_money(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.2f}"


def fmt_input_money(value: object) -> str:
    parsed = as_float(value)
    if parsed is None:
        return ""
    if parsed == int(parsed):
        return f"{int(parsed):,}"
    return f"{parsed:,.2f}".rstrip("0").rstrip(".")


def fmt_quantity(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{int(round(value)):,}"


def fmt_krw(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.0f}원"


def fmt_usd(value: float | None) -> str:
    if value is None:
        return "-"
    return f"${value:,.2f}"


def fmt_price_by_currency(value: float | None, currency: str) -> str:
    if value is None:
        return "-"
    if currency == "KRW":
        return fmt_krw(value)
    if currency == "USD":
        return fmt_usd(value)
    if not currency:
        return fmt_money(value)
    return f"{value:,.2f} {currency}"


def fmt_percent(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:+.2f}%"


def parse_percent_text(value: object) -> float | None:
    if value is None:
        return None
    cleaned = str(value).replace("%", "").replace("+", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def fmt_change_percent(value: object) -> str:
    parsed = parse_percent_text(value)
    if parsed is None:
        return escape(value)
    return fmt_percent(parsed)


def recommendation_for(return_pct: float | None) -> str:
    if return_pct is None:
        return "HOLD"
    if return_pct <= -10:
        return "추가 매수 검토"
    if return_pct >= 20:
        return "매도 검토"
    return "HOLD"


def price_from_snapshot(snapshot: dict[str, str]) -> float | None:
    if snapshot.get("status") != "ok":
        return None
    return as_float(snapshot.get("price"))


def holdings() -> list[sqlite3.Row]:
    return db_rows("SELECT * FROM holdings ORDER BY id DESC")


TICKER_LOOKUP = {
    "AAPL": ("Apple", "USD"),
    "MSFT": ("Microsoft", "USD"),
    "NVDA": ("NVIDIA", "USD"),
    "AMD": ("AMD", "USD"),
    "TSLA": ("Tesla", "USD"),
    "GOOGL": ("Alphabet Class A", "USD"),
    "GOOG": ("Alphabet Class C", "USD"),
    "AMZN": ("Amazon", "USD"),
    "META": ("Meta Platforms", "USD"),
    "QQQ": ("Invesco QQQ Trust", "USD"),
    "SPY": ("SPDR S&P 500 ETF", "USD"),
    "SOXL": ("Direxion Daily Semiconductor Bull 3X Shares", "USD"),
    "SOXX": ("iShares Semiconductor ETF", "USD"),
    "TQQQ": ("ProShares UltraPro QQQ", "USD"),
    "SOXS": ("Direxion Daily Semiconductor Bear 3X Shares", "USD"),
    "TSLL": ("Direxion Daily TSLA Bull 2X Shares", "USD"),
    "PLTU": ("Direxion Daily PLTR Bull 2X Shares", "USD"),
    "NVDU": ("Direxion Daily NVDA Bull 2X Shares", "USD"),
    "005930.KS": ("삼성전자", "KRW"),
    "005935.KS": ("삼성전자우", "KRW"),
    "000660.KS": ("SK하이닉스", "KRW"),
    "005380.KS": ("현대차", "KRW"),
    "000270.KS": ("기아", "KRW"),
    "035420.KS": ("NAVER", "KRW"),
    "035720.KS": ("카카오", "KRW"),
    "373220.KS": ("LG에너지솔루션", "KRW"),
    "207940.KS": ("삼성바이오로직스", "KRW"),
    "068270.KS": ("셀트리온", "KRW"),
    "458730.KS": ("TIGER 미국배당다우존스 ETF", "KRW"),
    "368590.KS": ("KBSTAR 미국나스닥100 ETF", "KRW"),
    "360750.KS": ("TIGER 미국S&P500 ETF", "KRW"),
}


def normalize_ticker(value: str) -> str:
    ticker = value.strip().upper()
    if re.fullmatch(r"\d{6}", ticker):
        return f"{ticker}.KS"
    return ticker


def lookup_ticker(value: str) -> dict[str, str]:
    ticker = normalize_ticker(value)
    name, currency = TICKER_LOOKUP.get(ticker, (ticker, "KRW" if ticker.endswith(".KS") else "USD"))
    return {"ticker": ticker, "name": name, "currency": currency}


def display_name_for_row(row: sqlite3.Row) -> str:
    ticker = row["ticker"]
    name = (row["name"] or "").strip()
    if name and name != ticker:
        return name
    return TICKER_LOOKUP.get(ticker, (ticker, ""))[0]


def uploads() -> list[sqlite3.Row]:
    return db_rows("SELECT * FROM uploads ORDER BY id DESC")


COMMON_STOCKS = {
    "삼성전자": "005930.KS",
    "삼성전자우": "005935.KS",
    "SK하이닉스": "000660.KS",
    "하이닉스": "000660.KS",
    "현대차": "005380.KS",
    "기아": "000270.KS",
    "NAVER": "035420.KS",
    "네이버": "035420.KS",
    "카카오": "035720.KS",
    "LG에너지솔루션": "373220.KS",
    "삼성바이오로직스": "207940.KS",
    "셀트리온": "068270.KS",
    "NVIDIA": "NVDA",
    "엔비디아": "NVDA",
    "APPLE": "AAPL",
    "애플": "AAPL",
    "MICROSOFT": "MSFT",
    "마이크로소프트": "MSFT",
    "TESLA": "TSLA",
    "테슬라": "TSLA",
    "AMD": "AMD",
    "QQQ": "QQQ",
    "SOXL": "SOXL",
    "SOXX": "SOXX",
    "TQQQ": "TQQQ",
    "SPY": "SPY",
}


def holding_exists(ticker: str) -> bool:
    rows = db_rows("SELECT id FROM holdings WHERE UPPER(ticker) = UPPER(?) LIMIT 1", (ticker,))
    return bool(rows)


def parse_holdings_from_text(text: str) -> list[dict[str, str]]:
    candidates: dict[str, dict[str, str]] = {}
    normalized = text.replace("\r", "\n")

    for name, ticker in COMMON_STOCKS.items():
        if name.lower() in normalized.lower():
            candidates[ticker] = {"ticker": ticker, "name": name, "memo": "OCR 자동 인식 후보"}

    for match in re.finditer(r"\b([A-Z]{1,5})(?:\s|\n|$)", normalized):
        ticker = match.group(1).upper()
        if ticker in {"USD", "KRW", "ETF", "PER", "PBR", "EPS", "ROE", "CEO"}:
            continue
        candidates.setdefault(ticker, {"ticker": ticker, "name": ticker, "memo": "OCR 티커 후보"})

    for match in re.finditer(r"\b(\d{6})(?:\s|\n|$)", normalized):
        code = match.group(1)
        ticker = f"{code}.KS"
        candidates.setdefault(ticker, {"ticker": ticker, "name": code, "memo": "OCR 한국 종목코드 후보"})

    return list(candidates.values())


def run_ocr_for_file(image_path: Path) -> tuple[bool, str]:
    default_tesseract = Path("C:/Program Files/Tesseract-OCR/tesseract.exe")
    tesseract = shutil.which("tesseract") or (
        str(default_tesseract) if default_tesseract.exists() else ""
    )
    if not tesseract:
        return (
            False,
            "OCR 엔진(Tesseract)이 설치되어 있지 않습니다. Tesseract 설치 후 다시 실행하면 캡처 자동 인식이 가능합니다.",
        )

    tessdata_args = []
    if (LOCAL_TESSDATA_DIR / "kor.traineddata").exists():
        tessdata_args = ["--tessdata-dir", str(LOCAL_TESSDATA_DIR)]
    attempts = [
        [tesseract, str(image_path), "stdout", *tessdata_args, "-l", "kor+eng", "--psm", "6"],
        [tesseract, str(image_path), "stdout", "-l", "eng", "--psm", "6"],
    ]
    last_error = ""
    for command in attempts:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except Exception as exc:
            last_error = str(exc)
            continue
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        if result.returncode == 0 and stdout.strip():
            return True, stdout.strip()
        last_error = stderr.strip() or "OCR 결과가 비어 있습니다."
    return False, f"OCR 실행에 실패했습니다. {last_error}"


def save_ocr_text(upload_id: int, text: str) -> None:
    db_execute("UPDATE uploads SET ocr_text = ? WHERE id = ?", (text, upload_id))


def auto_register_from_ocr(text: str) -> int:
    inserted = 0
    for candidate in parse_holdings_from_text(text):
        if holding_exists(candidate["ticker"]):
            continue
        db_execute(
            """
            INSERT INTO holdings (ticker, name, quantity, avg_price, currency, memo, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate["ticker"],
                candidate["name"],
                None,
                None,
                "KRW" if candidate["ticker"].endswith(".KS") else "USD",
                candidate["memo"],
                now_text(),
            ),
        )
        inserted += 1
    return inserted


def fetch_json(url: str) -> dict:
    with urlopen(url, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


def cache_get(cache_key: str, max_age_hours: int = MARKET_CACHE_HOURS) -> object | None:
    rows = db_rows("SELECT payload, fetched_at FROM market_cache WHERE cache_key = ?", (cache_key,))
    if not rows:
        return None
    try:
        fetched_at = dt.datetime.strptime(rows[0]["fetched_at"], "%Y-%m-%d %H:%M:%S")
        if dt.datetime.now() - fetched_at > dt.timedelta(hours=max_age_hours):
            return None
        return json.loads(rows[0]["payload"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def cache_set(cache_key: str, payload: object) -> None:
    db_execute(
        """
        INSERT INTO market_cache (cache_key, payload, fetched_at)
        VALUES (?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE
        SET payload = excluded.payload, fetched_at = excluded.fetched_at
        """,
        (cache_key, json.dumps(payload, ensure_ascii=False), now_text()),
    )


def yfinance_available() -> bool:
    try:
        import yfinance  # noqa: F401

        return True
    except Exception:
        return False


def fetch_yfinance_quote_live(ticker: str) -> dict[str, str]:
    try:
        import yfinance as yf

        history = yf.Ticker(ticker).history(period="5d", interval="1d", auto_adjust=False)
    except Exception as exc:
        return {"status": "error", "message": f"비공식 시세 조회 오류: {exc}"}

    if history is None or history.empty:
        return {"status": "empty", "message": "비공식 시세 데이터가 비어 있습니다."}

    last = history.iloc[-1]
    close = as_float(last.get("Close"))
    if close is None:
        return {"status": "empty", "message": "비공식 시세의 종가 데이터가 비어 있습니다."}

    prev_close = None
    if len(history.index) >= 2:
        prev_close = as_float(history.iloc[-2].get("Close"))
    change = close - prev_close if prev_close else None
    change_percent = change / prev_close * 100 if change is not None and prev_close else None
    latest = history.index[-1]
    latest_day = latest.strftime("%Y-%m-%d") if hasattr(latest, "strftime") else str(latest)[:10]
    return {
        "status": "ok",
        "source": "Yahoo Finance 비공식 데이터",
        "symbol": ticker,
        "price": f"{close:.4f}",
        "change": f"{change:.4f}" if change is not None else "",
        "change_percent": fmt_percent(change_percent) if change_percent is not None else "",
        "latest_day": latest_day,
    }


def fetch_market_snapshot_live(ticker: str, api_key: str) -> dict[str, str]:
    if not api_key:
        return {"status": "no_key", "message": "시세 데이터 연동이 아직 설정되지 않았습니다."}
    symbol = ticker.replace(".KS", ".KS")
    url = (
        "https://www.alphavantage.co/query"
        f"?function=GLOBAL_QUOTE&symbol={quote(symbol)}&apikey={quote(api_key)}"
    )
    try:
        data = fetch_json(url)
    except Exception as exc:
        return {"status": "error", "message": f"시세 API 오류: {exc}"}
    quote_data = data.get("Global Quote") or {}
    if not quote_data:
        message = data.get("Information") or data.get("Note") or "시세 데이터가 비어 있습니다."
        return {"status": "empty", "message": str(message)}
    return {
        "status": "ok",
        "symbol": quote_data.get("01. symbol", ticker),
        "price": quote_data.get("05. price", ""),
        "change": quote_data.get("09. change", ""),
        "change_percent": quote_data.get("10. change percent", ""),
        "latest_day": quote_data.get("07. latest trading day", ""),
    }


def fetch_market_snapshot(ticker: str, api_key: str) -> dict[str, str]:
    cache_key = f"quote:{ticker}"
    cached = cache_get(cache_key)
    if isinstance(cached, dict):
        cached["cached"] = "true"
        return cached
    if yfinance_available():
        snapshot = fetch_yfinance_quote_live(ticker)
        if snapshot.get("status") == "ok":
            cache_set(cache_key, snapshot)
            return snapshot
        if not api_key:
            return snapshot
    if not api_key:
        return fetch_market_snapshot_live(ticker, api_key)
    snapshot = fetch_market_snapshot_live(ticker, api_key)
    if snapshot.get("status") == "ok":
        cache_set(cache_key, snapshot)
    return snapshot


def fetch_exchange_rate() -> dict[str, str]:
    cache_key = "fx:USDKRW"
    cached = cache_get(cache_key)
    if isinstance(cached, dict):
        cached["cached"] = "true"
        return cached
    if not yfinance_available():
        return {"status": "no_source", "message": "환율 데이터 소스가 설정되지 않았습니다."}
    snapshot = fetch_yfinance_quote_live("USDKRW=X")
    if snapshot.get("status") == "ok":
        snapshot["pair"] = "USD/KRW"
        cache_set(cache_key, snapshot)
    return snapshot


def fetch_news_snapshot_live(ticker: str, api_key: str) -> list[dict[str, str]]:
    if not api_key:
        return []
    url = (
        "https://www.alphavantage.co/query"
        f"?function=NEWS_SENTIMENT&tickers={quote(ticker)}&limit=3&apikey={quote(api_key)}"
    )
    try:
        data = fetch_json(url)
    except Exception:
        return []
    feed = data.get("feed") or []
    items = []
    for item in feed[:3]:
        items.append(
            {
                "title": item.get("title", ""),
                "source": item.get("source", ""),
                "url": item.get("url", ""),
                "summary": item.get("summary", "")[:220],
            }
        )
    return items


def normalize_yfinance_news_item(item: dict) -> dict[str, str]:
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    title = item.get("title") or content.get("title") or ""
    summary = item.get("summary") or content.get("summary") or content.get("description") or ""
    source = item.get("publisher") or ""
    provider = content.get("provider") if isinstance(content.get("provider"), dict) else {}
    source = source or provider.get("displayName") or provider.get("name") or ""
    url = item.get("link") or item.get("url") or ""
    canonical = content.get("canonicalUrl") if isinstance(content.get("canonicalUrl"), dict) else {}
    url = url or canonical.get("url") or ""
    return {
        "title": str(title),
        "source": str(source),
        "url": str(url),
        "summary": str(summary)[:360],
    }


def fetch_yfinance_news_live(ticker: str) -> list[dict[str, str]]:
    try:
        import yfinance as yf

        raw_items = yf.Ticker(ticker).news or []
    except Exception:
        return []
    items = []
    for item in raw_items[:5]:
        normalized = normalize_yfinance_news_item(item)
        if normalized["title"]:
            items.append(normalized)
    return items


def fetch_news_snapshot(ticker: str, api_key: str) -> list[dict[str, str]]:
    cache_key = f"news:{ticker}"
    cached = cache_get(cache_key)
    if isinstance(cached, list):
        return cached
    if yfinance_available():
        items = fetch_yfinance_news_live(ticker)
        if items:
            cache_set(cache_key, items)
            return items
    if not api_key:
        return []
    items = fetch_news_snapshot_live(ticker, api_key)
    if items:
        cache_set(cache_key, items)
    return items


NEWS_SIGNALS = [
    (("distribution", "dividend", "yield", "fund flow", "flows", "options", "open interest"), "ETF 수급, 배당, 옵션 거래 관련 뉴스가 관찰됩니다."),
    (("earnings", "revenue", "profit", "margin", "eps", "guidance", "forecast"), "실적과 가이던스 변화가 주가 변동의 핵심 재료입니다."),
    (("rate", "fed", "inflation", "yield", "dollar"), "금리, 물가, 달러 흐름이 성장주와 레버리지 상품의 변동성을 키울 수 있습니다."),
    (("chip", "semiconductor", "ai", "gpu", "data center", "nvidia"), "AI·반도체 수요와 밸류에이션 기대가 함께 반영되는 구간입니다."),
    (("tesla", "ev", "delivery", "vehicle", "musk"), "전기차 수요, 인도량, 가격 정책 관련 이슈가 민감하게 작용할 수 있습니다."),
    (("regulation", "lawsuit", "probe", "ban", "tariff"), "규제·소송·관세 이슈는 단기 리스크 프리미엄을 높일 수 있습니다."),
    (("upgrade", "downgrade", "analyst", "target"), "애널리스트 의견 변화가 단기 수급에 영향을 줄 수 있습니다."),
    (("market", "selloff", "rally", "volatility"), "시장 전체의 위험 선호 변화가 포지션 성과에 크게 반영될 수 있습니다."),
]


RESEARCH_SIGNAL_RULES = [
    {
        "key": "rates",
        "label": "금리·달러 신호",
        "keywords": ("rate", "fed", "inflation", "yield", "dollar", "treasury"),
        "thesis": "금리와 달러 움직임은 성장주, 나스닥, 레버리지 ETF의 밸류에이션 부담을 키울 수 있습니다.",
        "action": "신규 추격매수보다 현금 여력과 손실 제한선을 먼저 확인합니다.",
    },
    {
        "key": "ai_semis",
        "label": "AI·반도체 신호",
        "keywords": ("chip", "semiconductor", "ai", "gpu", "data center", "nvidia", "memory"),
        "thesis": "AI·반도체 수요 뉴스는 엔비디아, 반도체 ETF, 국내 반도체 밸류체인에 함께 영향을 줍니다.",
        "action": "방향성은 유지하되 레버리지 상품은 목표 비중을 넘기지 않습니다.",
    },
    {
        "key": "korea",
        "label": "한국 시장 신호",
        "keywords": ("korea", "kospi", "won", "samsung", "sk hynix", "export"),
        "thesis": "한국 시장과 원화 흐름은 국내 상장 해외 ETF와 반도체 관련 자산의 체감 수익률에 영향을 줍니다.",
        "action": "환율과 국내 ETF 괴리율을 함께 확인합니다.",
    },
    {
        "key": "etf_flow",
        "label": "ETF·수급 신호",
        "keywords": ("etf", "flow", "flows", "options", "open interest", "distribution", "dividend"),
        "thesis": "ETF 자금 흐름과 옵션 거래 증가는 단기 변동성 확대 신호로 해석할 수 있습니다.",
        "action": "레버리지·인버스 ETF는 당일 변동성 확대에 대비해 비중을 보수적으로 둡니다.",
    },
    {
        "key": "market",
        "label": "시장 심리 신호",
        "keywords": ("market", "selloff", "rally", "volatility", "risk", "stocks"),
        "thesis": "시장 전체 위험 선호 변화는 개별 종목보다 포트폴리오 전체 변동성을 먼저 흔들 수 있습니다.",
        "action": "오늘은 종목별 매수보다 전체 노출과 현금 비중을 우선 점검합니다.",
    },
]


def research_signal_for_news(news: dict[str, str], ticker: str) -> dict[str, object]:
    text = f"{news.get('title', '')} {news.get('summary', '')} {ticker}".lower()
    for rule in RESEARCH_SIGNAL_RULES:
        if any(keyword in text for keyword in rule["keywords"]):
            return rule
    return {
        "key": "general",
        "label": "일반 시장 신호",
        "thesis": "단일 뉴스보다 시장 흐름과 보유 비중을 함께 확인할 필요가 있습니다.",
        "action": "큰 포지션 변경보다 관찰과 리스크 점검을 우선합니다.",
    }


def related_portfolio_names(signal_key: str, items: list[dict[str, object]]) -> str:
    if not items:
        return "보유 종목 입력 후 영향도를 계산합니다."
    if signal_key == "ai_semis":
        related = [item for item in items if item["bucket"] == "반도체/AI"]
    elif signal_key == "korea":
        related = [item for item in items if item["currency"] == "KRW"]
    elif signal_key == "etf_flow":
        related = [item for item in items if item["risk_flags"]]
    elif signal_key == "rates":
        related = [item for item in items if item["bucket"] in {"미국 성장주", "반도체/AI", "AI 소프트웨어"} or item["risk_flags"]]
    else:
        related = items[:3]
    if not related:
        related = items[:2]
    return ", ".join(str(item["name"]) for item in related[:3])


def build_research_strategy_html(
    news_records: list[dict[str, str]],
    analysis_items: list[dict[str, object]],
    action_summary: str,
) -> str:
    grouped: dict[str, dict[str, object]] = {}
    for record in news_records:
        signal = research_signal_for_news(record, record["ticker"])
        key = str(signal["key"])
        if key not in grouped:
            grouped[key] = {"signal": signal, "records": []}
        grouped[key]["records"].append(record)

    if not grouped:
        return (
            "<div class='insight-board'>"
            "<article class='insight-card'>"
            "<div class='insight-head'><span>Strategy Agent</span><strong>뉴스 데이터 대기</strong></div>"
            f"<p>{escape(action_summary)}</p>"
            "<div class='insight-action'>오늘의 대응: 보유 비중과 현금 여력을 우선 점검합니다.</div>"
            "</article></div>"
        )

    cards = []
    priority = ["rates", "ai_semis", "korea", "etf_flow", "market", "general"]
    ordered_groups = sorted(grouped.values(), key=lambda group: priority.index(str(group["signal"]["key"])) if str(group["signal"]["key"]) in priority else 99)
    for group in ordered_groups[:4]:
        signal = group["signal"]
        records = group["records"]
        sources = []
        for record in records[:3]:
            source = record.get("source") or "출처"
            url = record.get("url") or "#"
            sources.append(f"<a href='{escape(url)}'>{escape(source)}</a>")
        cards.append(
            "<article class='insight-card'>"
            "<div class='insight-head'>"
            f"<span>Research Signal</span><strong>{escape(signal['label'])}</strong>"
            "</div>"
            f"<p>{escape(signal['thesis'])}</p>"
            "<div class='insight-impact'>"
            "<span>내 포트폴리오 영향</span>"
            f"<b>{escape(related_portfolio_names(str(signal['key']), analysis_items))}</b>"
            "</div>"
            f"<div class='insight-action'>오늘의 대응: {escape(signal['action'])}</div>"
            f"<div class='insight-sources'>{' · '.join(sources)}</div>"
            "</article>"
        )
    return "<div class='insight-board'>" + "\n".join(cards) + "</div>"


def news_brief_ko(ticker: str, news: dict[str, str], subject: str | None = None) -> str:
    text = f"{news.get('title', '')} {news.get('summary', '')}".lower()
    signals = [message for keywords, message in NEWS_SIGNALS if any(keyword in text for keyword in keywords)]
    if not signals:
        signals = ["원문 뉴스의 세부 내용을 확인해 단기 수급 변화가 있는지 점검할 필요가 있습니다."]
    source = news.get("source", "").strip() or "출처 미상"
    context = {
        "SOXS": "반도체 섹터가 반등하면 인버스 포지션 손실이 빠르게 커질 수 있으므로 짧은 점검 주기가 필요합니다.",
        "TSLL": "테슬라 관련 뉴스는 단일 종목 레버리지 상품의 변동성을 크게 키울 수 있습니다.",
        "NVDU": "엔비디아와 AI 반도체 기대가 약해질 때 레버리지 손실이 확대될 수 있습니다.",
        "PLTU": "성장주 이벤트와 시장 위험 선호 변화에 민감한 포지션으로 보는 편이 좋습니다.",
        "SPY": "미국 대형주 흐름은 전체 위험 선호와 포트폴리오 방향성 판단에 중요합니다.",
        "QQQ": "나스닥·성장주 흐름은 AI, 반도체, 레버리지 ETF 포지션에 직접적인 영향을 줄 수 있습니다.",
        "DIA": "다우 흐름은 경기민감주와 전통 산업 투자심리를 확인하는 보조 지표입니다.",
        "EWY": "한국 시장 관련 뉴스는 원화 자산과 국내 상장 해외 ETF 심리에 영향을 줄 수 있습니다.",
    }.get(ticker, "해당 종목의 가격 변동과 보유 비중을 함께 점검하는 것이 좋습니다.")
    label = subject or ticker
    return f"{label}: {signals[0]} {context} 출처: {source}"


def risk_flags_for_ticker(ticker: str) -> list[str]:
    flags = []
    leveraged_notes = {
        "SOXS": "반도체 하락에 베팅하는 인버스 레버리지 ETF 성격이 강해 장기 보유보다 짧은 리스크 관리가 중요합니다.",
        "TSLL": "테슬라 방향성에 크게 노출된 레버리지 상품으로 변동성 손실과 단일 종목 이벤트 리스크가 큽니다.",
        "NVDU": "엔비디아 방향성에 크게 노출된 레버리지 상품으로 AI 기대가 꺾일 때 낙폭이 커질 수 있습니다.",
        "PLTU": "단일 성장주 방향성에 크게 노출된 레버리지 상품일 수 있어 손절·익절 기준을 분명히 두는 편이 좋습니다.",
    }
    if ticker in leveraged_notes:
        flags.append(leveraged_notes[ticker])
    if ticker.endswith(("U", "L")) or ticker in {"SOXL", "SOXS", "TQQQ", "SQQQ"}:
        flags.append("레버리지/인버스 상품은 횡보장에서도 시간 가치와 변동성 손실이 누적될 수 있습니다.")
    return flags


def position_advice_for(ticker: str, return_pct: float | None, risk_flags: list[str]) -> str:
    if risk_flags and return_pct is not None and return_pct < -8:
        return "비중 축소 또는 손실 제한 기준 우선"
    if risk_flags and return_pct is not None and return_pct > 15:
        return "일부 이익실현과 잔여 HOLD"
    if risk_flags:
        return "단기 HOLD, 비중 관리"
    if return_pct is not None and return_pct <= -10:
        return "추가 매수 전 원인 점검"
    if return_pct is not None and return_pct >= 20:
        return "일부 이익실현 검토"
    return "HOLD"


def total_cost_by_currency(rows: list[sqlite3.Row]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for row in rows:
        qty = row["quantity"] or 0
        price = row["avg_price"] or 0
        currency = row["currency"] or "KRW"
        totals[currency] = totals.get(currency, 0) + qty * price
    return totals


def concentration_note_from_items(items: list[dict[str, object]], total_value_krw: float | None = None) -> str:
    if not items:
        return "아직 보유 종목이 없어 리스크를 계산할 수 없습니다."
    values = [(str(item["name"]), value_for_weight(item)) for item in items]
    total = total_value_krw if total_value_krw is not None else sum(value for _, value in values)
    if total <= 0:
        return "수량, 평균단가 또는 현재가를 입력하면 종목 집중도를 계산할 수 있습니다."
    top_name, top_value = max(values, key=lambda item: item[1])
    ratio = top_value / total * 100
    if ratio >= 50:
        level = "높음"
    elif ratio >= 30:
        level = "보통"
    else:
        level = "낮음"
    return f"가장 큰 종목 비중은 {top_name} 약 {ratio:.1f}%이며, 종목 집중도는 {level} 수준입니다."


def clamp(value: float, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, int(round(value))))


def value_for_weight(item: dict[str, object]) -> float:
    weight_basis = as_float(item.get("weight_basis_krw"))
    if weight_basis is not None:
        return weight_basis
    krw_value = as_float(item.get("valuation_krw"))
    usd_value = as_float(item.get("valuation_usd"))
    return krw_value if krw_value is not None else (usd_value or 0)


def score_bar(label: str, score: int) -> str:
    return (
        "<div class='score-row'>"
        f"<span>{escape(label)}</span>"
        "<div class='score-track'>"
        f"<div class='score-fill' style='width:{score}%'></div>"
        "</div>"
        f"<strong>{score}</strong>"
        "</div>"
    )


def impact_for(return_pct: float | None, risk_flags: list[str]) -> tuple[str, str]:
    if risk_flags and return_pct is not None and return_pct < -8:
        return "▼ 부정", "레버리지 손실과 변동성 관리가 우선입니다."
    if return_pct is not None and return_pct >= 20:
        return "▲ 긍정", "수익 구간이나 일부 이익실현 기준을 점검할 만합니다."
    if risk_flags:
        return "→ 중립", "방향성은 열려 있지만 포지션 크기 관리가 필요합니다."
    return "→ 중립", "현재는 큰 경고 신호보다 보유 점검 성격이 강합니다."


def confidence_for(return_pct: float | None, risk_flags: list[str]) -> int:
    if risk_flags and return_pct is not None and abs(return_pct) >= 20:
        return 86
    if risk_flags:
        return 78
    if return_pct is not None and abs(return_pct) >= 20:
        return 74
    return 66


def exposure_bucket(ticker: str) -> str:
    if ticker in {"SOXS", "SOXL", "SOXX", "NVDU", "NVDA"} or ticker.endswith(".KS") and ticker in {"005930.KS", "000660.KS"}:
        return "반도체/AI"
    if ticker in {"TSLA", "TSLL"}:
        return "전기차"
    if ticker in {"PLTR", "PLTU"}:
        return "AI 소프트웨어"
    if ticker in {"QQQ", "TQQQ", "368590.KS"}:
        return "미국 성장주"
    if ticker in {"SPY", "360750.KS"}:
        return "미국 대형주"
    if ticker == "458730.KS":
        return "배당/퀄리티"
    return "기타"


def diary_rows(limit: int = 12) -> list[sqlite3.Row]:
    return db_rows(
        """
        SELECT * FROM investment_diary
        ORDER BY diary_date DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    )


def prior_diary_rows(limit: int = 5) -> list[sqlite3.Row]:
    return db_rows(
        """
        SELECT * FROM investment_diary
        WHERE diary_date < ?
        ORDER BY diary_date DESC, id DESC
        LIMIT ?
        """,
        (today_text(), limit),
    )


def record_diary_entries(items: list[dict[str, object]]) -> None:
    for item in items:
        current_price = as_float(item.get("current_price"))
        db_execute(
            """
            INSERT INTO investment_diary (
                diary_date, ticker, name, suggestion, confidence, suggested_price,
                currency, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(diary_date, ticker) DO UPDATE SET
                name = excluded.name,
                suggestion = excluded.suggestion,
                confidence = excluded.confidence,
                suggested_price = excluded.suggested_price,
                currency = excluded.currency,
                updated_at = excluded.updated_at
            """,
            (
                today_text(),
                str(item["ticker"]),
                str(item["name"]),
                str(item["recommendation"]),
                int(item["confidence"]),
                current_price,
                str(item["currency"]),
                now_text(),
                now_text(),
            ),
        )


def evaluate_diary_entries(market_snapshots: dict[str, dict[str, str]]) -> None:
    rows = db_rows(
        """
        SELECT * FROM investment_diary
        WHERE suggested_price IS NOT NULL
          AND (eval_1w IS NULL OR eval_1m IS NULL)
        """
    )
    today = dt.date.today()
    for row in rows:
        try:
            diary_day = dt.date.fromisoformat(row["diary_date"])
        except ValueError:
            continue
        current_price = price_from_snapshot(market_snapshots.get(row["ticker"], {}))
        if current_price is None:
            continue
        suggested_price = as_float(row["suggested_price"])
        if not suggested_price:
            continue
        result = (current_price - suggested_price) / suggested_price * 100
        updates = []
        params: list[object] = []
        if row["eval_1w"] is None and (today - diary_day).days >= 7:
            updates.append("eval_1w = ?")
            params.append(f"1주 후 {fmt_percent(result)}")
        if row["eval_1m"] is None and (today - diary_day).days >= 30:
            updates.append("eval_1m = ?")
            params.append(f"1개월 후 {fmt_percent(result)}")
        if updates:
            updates.append("updated_at = ?")
            params.append(now_text())
            params.append(row["id"])
            db_execute(f"UPDATE investment_diary SET {', '.join(updates)} WHERE id = ?", tuple(params))


def diary_result_text(row: sqlite3.Row) -> str:
    eval_1w = row["eval_1w"] or "1주 평가 대기"
    eval_1m = row["eval_1m"] or "1개월 평가 대기"
    action = row["user_action"] or "아직 행동 미기록"
    note = f" · {row['action_note']}" if row["action_note"] else ""
    return (
        f"{escape(row['diary_date'])} {escape(row['name'])}: "
        f"AI 제안은 {escape(row['suggestion'])}, 실제 행동은 {escape(action)}{escape(note)}. "
        f"{escape(eval_1w)} / {escape(eval_1m)}"
    )


def diary_report_html() -> str:
    rows = prior_diary_rows()
    if not rows:
        rows = diary_rows(5)
    if not rows:
        return "<p class='muted'>아직 기록된 AI 제안이 없습니다. 오늘 리포트부터 자동 기록이 시작됩니다.</p>"
    cards = []
    for row in rows:
        action = row["user_action"] or "미기록"
        note = row["action_note"] or "사용자 행동이 아직 기록되지 않았습니다."
        price = fmt_price_by_currency(as_float(row["suggested_price"]), row["currency"] or "")
        cards.append(
            "<article class='diary-card'>"
            "<div class='diary-head'>"
            f"<strong>{escape(row['name'])}</strong>"
            f"<span>{escape(row['diary_date'])}</span>"
            "</div>"
            "<div class='diary-grid'>"
            "<div><span>AI 제안</span>"
            f"<b>{escape(row['suggestion'])}</b>"
            f"<small>신뢰도 {escape(row['confidence'])}% · 제안가 {escape(price)}</small></div>"
            "<div><span>실제 행동</span>"
            f"<b>{escape(action)}</b>"
            f"<small>{escape(note)}</small></div>"
            "<div><span>결과 평가</span>"
            f"<b>{escape(row['eval_1w'] or '1주 평가 대기')}</b>"
            f"<small>{escape(row['eval_1m'] or '1개월 평가 대기')}</small></div>"
            "</div>"
            "</article>"
        )
    return "<div class='diary-list'>" + "\n".join(cards) + "</div>"


def build_report_html() -> str:
    settings = get_settings()
    stock_rows = holdings()
    api_key = settings["alpha_vantage_key"]
    unique_tickers = []
    for row in stock_rows:
        ticker = row["ticker"]
        if ticker not in unique_tickers:
            unique_tickers.append(ticker)
    market_snapshots = {
        ticker: fetch_market_snapshot(ticker, api_key)
        for ticker in unique_tickers[:8]
    }
    fx_snapshot = fetch_exchange_rate()
    usd_krw = price_from_snapshot(fx_snapshot)
    cash_krw = parse_float(settings.get("cash_krw", "0")) or 0
    cash_usd = parse_float(settings.get("cash_usd", "0")) or 0
    cash_value_krw = cash_krw + (cash_usd * usd_krw if usd_krw else 0)
    totals = total_cost_by_currency(stock_rows)
    totals_html_parts = []
    ordered_total_currencies = [currency for currency in ("USD", "KRW") if currency in totals]
    ordered_total_currencies.extend(currency for currency in totals if currency not in {"USD", "KRW"})
    for currency in ordered_total_currencies:
        amount = totals[currency]
        if currency == "USD" and usd_krw:
            totals_html_parts.append(f"<li>USD 기준 추정 원금: {fmt_usd(amount)} / {fmt_krw(amount * usd_krw)}</li>")
        elif currency == "KRW" and usd_krw:
            totals_html_parts.append(f"<li>KRW 기준 추정 원금: {fmt_krw(amount)} / {fmt_usd(amount / usd_krw)}</li>")
        else:
            totals_html_parts.append(f"<li>{escape(currency)} 기준 추정 원금: {amount:,.2f}</li>")
    totals_html = "".join(totals_html_parts)
    if not totals_html:
        totals_html = "<li>아직 입력된 보유 종목이 없습니다.</li>"
    cash_html_parts = []
    if cash_krw:
        cash_html_parts.append(f"<li>KRW 현금: {fmt_krw(cash_krw)}</li>")
    if cash_usd:
        converted = f" / {fmt_krw(cash_usd * usd_krw)} 환산" if usd_krw else ""
        cash_html_parts.append(f"<li>USD 현금: {fmt_usd(cash_usd)}{converted}</li>")
    if not cash_html_parts:
        cash_html_parts.append("<li>현금: 입력 없음</li>")
    cash_html = "".join(cash_html_parts)

    risk_items = []
    analysis_items = []
    for row in stock_rows:
        display_name = display_name_for_row(row)
        snapshot = market_snapshots.get(row["ticker"], {"status": "skipped", "message": "시세 조회 대상에서 제외되었습니다."})
        current_price = price_from_snapshot(snapshot) or as_float(row["current_price"])
        quantity = as_float(row["quantity"])
        avg_price = as_float(row["avg_price"])
        currency = row["currency"] or "KRW"
        return_pct = None
        if avg_price and current_price:
            return_pct = (current_price - avg_price) / avg_price * 100
        valuation = quantity * current_price if quantity is not None and current_price is not None else None
        valuation_usd = None
        valuation_krw = None
        if valuation is not None:
            if currency == "USD":
                valuation_usd = valuation
                valuation_krw = valuation * usd_krw if usd_krw else None
            elif currency == "KRW":
                valuation_krw = valuation
                valuation_usd = valuation / usd_krw if usd_krw else None
        cost_value = quantity * avg_price if quantity is not None and avg_price is not None else None
        cost_value_krw = None
        if cost_value is not None:
            if currency == "USD" and usd_krw:
                cost_value_krw = cost_value * usd_krw
            elif currency == "KRW":
                cost_value_krw = cost_value
        weight_basis_krw = valuation_krw if valuation_krw is not None else cost_value_krw
        risk_flags = risk_flags_for_ticker(row["ticker"])
        recommendation = position_advice_for(row["ticker"], return_pct, risk_flags)
        impact_label, impact_reason = impact_for(return_pct, risk_flags)
        confidence = confidence_for(return_pct, risk_flags)
        analysis_items.append(
            {
                "ticker": row["ticker"],
                "name": display_name,
                "memo": row["memo"],
                "currency": currency,
                "quantity": quantity,
                "avg_price": avg_price,
                "current_price": current_price,
                "return_pct": return_pct,
                "valuation_usd": valuation_usd,
                "valuation_krw": valuation_krw,
                "weight_basis_krw": weight_basis_krw,
                "recommendation": recommendation,
                "risk_flags": risk_flags,
                "impact_label": impact_label,
                "impact_reason": impact_reason,
                "confidence": confidence,
                "bucket": exposure_bucket(row["ticker"]),
            }
        )
        if risk_flags:
            risk_items.append(f"<li><strong>{escape(display_name)}</strong>: {escape(risk_flags[0])}</li>")
    risks_html = "\n".join(risk_items[:8]) or "<li>현재 입력값 기준으로 별도 고위험 상품 신호는 제한적입니다.</li>"
    if usd_krw:
        fx_html = f"적용 환율: 1 USD = {usd_krw:,.2f} KRW"
        if fx_snapshot.get("latest_day"):
            fx_html += f" ({escape(fx_snapshot.get('latest_day'))} 기준)"
    else:
        fx_html = "환율 데이터가 없어 평가금액의 달러/원화 동시 환산이 제한됩니다."

    total_invested_value = sum(value_for_weight(item) for item in analysis_items)
    total_asset_value = total_invested_value + cash_value_krw
    cash_ratio = cash_value_krw / total_asset_value * 100 if total_asset_value else 0
    holding_items = []
    for item in analysis_items:
        item_weight = value_for_weight(item) / total_asset_value * 100 if total_asset_value else None
        holding_items.append(
            "<tr>"
            f"<td><span class='ticker'>{escape(item['ticker'])}</span></td>"
            f"<td class='name-cell'>{escape(item['name'])}</td>"
            f"<td class='num'>{fmt_quantity(as_float(item['quantity']))}</td>"
            f"<td class='num'>{fmt_price_by_currency(as_float(item['avg_price']), str(item['currency']))}</td>"
            f"<td class='num'>{fmt_price_by_currency(as_float(item['current_price']), str(item['currency']))}</td>"
            f"<td class='num'>{fmt_percent(as_float(item['return_pct']))}</td>"
            f"<td class='num'>{fmt_percent(item_weight)}</td>"
            f"<td class='num'>{fmt_usd(as_float(item['valuation_usd']))}</td>"
            f"<td class='num'>{fmt_krw(as_float(item['valuation_krw']))}</td>"
            f"<td><strong>{escape(item['recommendation'])}</strong></td>"
            f"<td><span class='currency'>{escape(item['currency'])}</span></td>"
            f"<td>{escape(item['memo'])}</td>"
            "</tr>"
        )
    holdings_html = "\n".join(holding_items) or (
        "<tr><td colspan='12'>보유 종목을 입력하면 이곳에 표시됩니다.</td></tr>"
    )
    top_item = max(analysis_items, key=value_for_weight, default=None)
    top_ratio = value_for_weight(top_item) / total_asset_value * 100 if top_item and total_asset_value else 0
    leveraged_value = sum(value_for_weight(item) for item in analysis_items if item["risk_flags"])
    leveraged_ratio = leveraged_value / total_asset_value * 100 if total_asset_value else 0
    bucket_values: dict[str, float] = {}
    for item in analysis_items:
        bucket = str(item["bucket"])
        bucket_values[bucket] = bucket_values.get(bucket, 0) + value_for_weight(item)
    top_bucket, top_bucket_value = max(bucket_values.items(), key=lambda item: item[1], default=("없음", 0))
    top_bucket_ratio = top_bucket_value / total_asset_value * 100 if total_asset_value else 0

    diversification_score = clamp(100 - max(top_ratio - 20, 0) * 1.2)
    if total_asset_value <= 0:
        cash_score = 55
    elif cash_ratio < 5:
        cash_score = 45
    elif cash_ratio <= 20:
        cash_score = 90
    elif cash_ratio <= 35:
        cash_score = 75
    elif cash_ratio <= 50:
        cash_score = 60
    else:
        cash_score = 45
    concentration_score = clamp(100 - max(top_bucket_ratio - 35, 0) * 1.1)
    growth_score = clamp(70 + min(top_bucket_ratio, 30) * 0.5)
    value_score = clamp(72 - max(leveraged_ratio - 30, 0) * 0.4)
    risk_score = clamp(100 - leveraged_ratio * 0.75 - max(top_ratio - 30, 0) * 0.4)
    health_score = clamp(
        diversification_score * 0.2
        + cash_score * 0.1
        + concentration_score * 0.2
        + growth_score * 0.15
        + value_score * 0.15
        + risk_score * 0.2
    )
    health_delta = "+2" if health_score >= 75 else ("0" if health_score >= 65 else "-3")
    health_level = "양호" if health_score >= 80 else ("점검" if health_score >= 65 else "주의")
    methodology_html = (
        "<div class='method-grid'>"
        "<div><strong>AI Health Score</strong><span>0~100점 중 높을수록 양호합니다. 80점 이상은 양호, 65~79점은 점검, 64점 이하는 리스크 관리가 필요한 구간으로 봅니다.</span></div>"
        "<div><strong>현금비중</strong><span>KRW 현금과 USD 현금을 원화로 환산해 총자산에 포함합니다. 5~20% 구간을 가장 안정적인 완충 구간으로 봅니다.</span></div>"
        "<div><strong>핵심 편중</strong><span>종목 평가액과 현금을 합친 총자산을 분모로 사용합니다. 같은 테마로 묶은 투자금 중 가장 큰 그룹의 비중입니다.</span></div>"
        "<div><strong>신뢰도</strong><span>상승 확률이 아니라 현재 입력값으로 해당 의견을 뒷받침하는 근거의 강도입니다. 레버리지/인버스 여부, 손익률 크기, 리스크 플래그를 반영합니다.</span></div>"
        "</div>"
    )
    if leveraged_ratio >= 35:
        today_action = "리스크 축소 / 관망"
        action_summary = "레버리지·인버스 비중이 높아 신규 추격매수보다 손실 제한선과 비중 관리를 우선합니다."
    elif health_score >= 80:
        today_action = "보유 / 선별 매수"
        action_summary = "포트폴리오 건강도는 양호하며, 급등 종목은 일부 이익실현 기준을 점검합니다."
    else:
        today_action = "보유 / 리밸런싱 점검"
        action_summary = "상위 자산 편중을 줄이고 현금 여력을 확보하는 방향이 적합합니다."

    score_cards_html = (
        score_bar("분산", diversification_score)
        + score_bar("현금비중", cash_score)
        + score_bar("섹터집중", concentration_score)
        + score_bar("성장성", growth_score)
        + score_bar("밸류", value_score)
        + score_bar("리스크", risk_score)
    )

    impact_rows_html = "".join(
        "<tr>"
        f"<td class='name-cell'>{escape(item['name'])}</td>"
        f"<td><span class='impact'>{escape(item['impact_label'])}</span></td>"
        f"<td>{escape(item['impact_reason'])}</td>"
        f"<td class='num'>{escape(str(item['confidence']))}%</td>"
        "</tr>"
        for item in analysis_items[:8]
    ) or "<tr><td colspan='4'>보유 종목을 입력하면 영향도를 계산합니다.</td></tr>"

    reduce_candidates = [item for item in analysis_items if "축소" in str(item["recommendation"]) or "손실 제한" in str(item["recommendation"])]
    profit_candidates = [item for item in analysis_items if item["return_pct"] is not None and item["return_pct"] >= 20]
    hold_candidates = [item for item in analysis_items if item not in reduce_candidates and item not in profit_candidates]
    action_items = []
    if reduce_candidates:
        item = reduce_candidates[0]
        action_items.append(f"<li><strong>{escape(item['name'])}</strong>: 비중 확대 금지, 손실 제한선 재확인 <span class='confidence'>신뢰도 {item['confidence']}%</span></li>")
    if profit_candidates:
        item = profit_candidates[0]
        action_items.append(f"<li><strong>{escape(item['name'])}</strong>: 일부 이익실현 기준 검토 <span class='confidence'>신뢰도 {item['confidence']}%</span></li>")
    if hold_candidates:
        item = hold_candidates[0]
        action_items.append(f"<li><strong>{escape(item['name'])}</strong>: 신규 매수보다 관찰 유지 <span class='confidence'>신뢰도 {item['confidence']}%</span></li>")
    action_items.append("<li><strong>현금</strong>: 변동성 확대에 대비해 최소 10% 현금 여력 유지</li>")
    action_plan_html = "\n".join(action_items[:4])

    agent_cards_html = f"""
      <div class="agent-card"><strong>Market Agent</strong><span>환율 {escape(fx_html)}. 미국 성장주와 반도체 변동성에 주의합니다.</span></div>
      <div class="agent-card"><strong>Portfolio Agent</strong><span>최대 종목 비중은 {escape(top_item['name'] if top_item else '없음')} 약 {top_ratio:.1f}%, 현금 비중은 약 {cash_ratio:.1f}%입니다.</span></div>
      <div class="agent-card"><strong>Risk Agent</strong><span>{escape(top_bucket)} 노출 약 {top_bucket_ratio:.1f}%, 레버리지/고위험 노출 약 {leveraged_ratio:.1f}%입니다.</span></div>
      <div class="agent-card"><strong>Research Agent</strong><span>보유 종목 뉴스뿐 아니라 미국·한국 시장 뉴스까지 선별해 포트폴리오 영향으로 해석합니다.</span></div>
      <div class="agent-card"><strong>Strategy Agent</strong><span>{escape(action_summary)}</span></div>
    """
    record_diary_entries(analysis_items)
    evaluate_diary_entries(market_snapshots)
    diary_html = diary_report_html()

    market_rows = []
    news_records = []
    seen_news_urls = set()
    macro_news_sources = [
        ("SPY", "미국 경제·S&P500"),
        ("QQQ", "나스닥·성장주"),
        ("DIA", "다우·경기민감주"),
        ("EWY", "한국 시장"),
    ]
    for ticker, subject in macro_news_sources:
        for news in fetch_news_snapshot(ticker, api_key)[:2]:
            news_url = news.get("url", "")
            if news_url and news_url in seen_news_urls:
                continue
            if news_url:
                seen_news_urls.add(news_url)
            news_records.append(
                {
                    "ticker": ticker,
                    "subject": subject,
                    "title": news.get("title", ""),
                    "summary": news.get("summary", ""),
                    "source": news.get("source", ""),
                    "url": news_url,
                }
            )
    news_tickers = set()
    for row in stock_rows[:8]:
        display_name = display_name_for_row(row)
        snapshot = market_snapshots.get(row["ticker"], {"status": "skipped", "message": "시세 조회 대상에서 제외되었습니다."})
        if snapshot["status"] == "ok":
            market_rows.append(
                "<tr>"
                f"<td><span class='ticker'>{escape(row['ticker'])}</span></td>"
                f"<td class='num'>{fmt_price_by_currency(price_from_snapshot(snapshot), row['currency'] or 'KRW')}</td>"
                f"<td class='num'>{fmt_change_percent(snapshot.get('change_percent'))}</td>"
                f"<td>{escape(snapshot.get('latest_day'))}</td>"
                "</tr>"
            )
            if row["ticker"] not in news_tickers and len(news_tickers) < 4:
                news_tickers.add(row["ticker"])
                for news in fetch_news_snapshot(row["ticker"], api_key)[:1]:
                    news_url = news.get("url", "")
                    if news_url and news_url in seen_news_urls:
                        continue
                    if news_url:
                        seen_news_urls.add(news_url)
                    news_records.append(
                        {
                            "ticker": row["ticker"],
                            "subject": display_name,
                            "title": news.get("title", ""),
                            "summary": news.get("summary", ""),
                            "source": news.get("source", ""),
                            "url": news_url,
                        }
                    )
        else:
            market_rows.append(
                "<tr>"
                f"<td>{escape(row['ticker'])}</td>"
                f"<td colspan='3'>{escape(snapshot.get('message'))}</td>"
                "</tr>"
            )
    market_html = "\n".join(market_rows) or (
        "<tr><td colspan='4'>보유 종목이 없거나 시세 데이터 연동이 아직 설정되지 않았습니다.</td></tr>"
    )
    research_strategy_html = build_research_strategy_html(news_records, analysis_items, action_summary)

    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: Arial, sans-serif; color: #1f2937; line-height: 1.55; background: #f8fafc; }}
    .wrap {{ max-width: 980px; margin: 0 auto; padding: 24px; background: #ffffff; }}
    h1 {{ font-size: 24px; margin-bottom: 8px; }}
    h2 {{ font-size: 18px; margin-top: 28px; border-bottom: 1px solid #e5e7eb; padding-bottom: 6px; }}
    table {{ width: 100%; border-collapse: separate; border-spacing: 0; margin-top: 12px; font-size: 12px; border: 1px solid #dbe3ef; border-radius: 8px; overflow: hidden; }}
    th, td {{ border-bottom: 1px solid #e5e7eb; padding: 9px 10px; text-align: left; vertical-align: middle; }}
    th {{ background: #eef2f7; color: #374151; font-size: 11px; text-transform: uppercase; letter-spacing: 0; }}
    tbody tr:nth-child(even) {{ background: #f9fbfd; }}
    tbody tr:last-child td {{ border-bottom: 0; }}
    .num {{ text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }}
    .name-cell {{ min-width: 150px; font-weight: 700; color: #111827; }}
    .ticker, .currency {{ display: inline-block; padding: 2px 6px; border-radius: 5px; background: #eef2ff; color: #3730a3; font-size: 11px; font-weight: 700; }}
    .currency {{ background: #ecfdf5; color: #047857; }}
    .pill {{ display: inline-block; padding: 4px 8px; border-radius: 999px; background: #eef2ff; color: #3730a3; }}
    .muted {{ color: #6b7280; font-size: 12px; }}
    .warning {{ background: #fff7ed; border: 1px solid #fed7aa; padding: 12px; border-radius: 8px; }}
    .hero {{ background: #111827; color: #ffffff; border-radius: 8px; padding: 18px; margin-bottom: 18px; }}
    .hero h1 {{ margin: 4px 0 6px; color: #ffffff; }}
    .hero .muted {{ color: #cbd5e1; }}
    .summary-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin: 14px 0; }}
    .metric-card {{ border: 1px solid #dbe3ef; border-radius: 8px; padding: 12px; background: #f8fafc; }}
    .metric-card strong {{ display: block; font-size: 22px; color: #111827; }}
    .metric-card span {{ color: #64748b; font-size: 12px; }}
    .method-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin: 12px 0 18px; }}
    .method-grid div {{ border: 1px solid #dbe3ef; border-radius: 8px; padding: 11px; background: #ffffff; }}
    .method-grid strong {{ display: block; margin-bottom: 5px; color: #111827; }}
    .method-grid span {{ display: block; color: #475569; font-size: 12px; }}
    .agent-grid {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; }}
    .agent-card {{ border: 1px solid #dbe3ef; border-radius: 8px; padding: 10px; background: #ffffff; }}
    .agent-card strong {{ display: block; margin-bottom: 5px; color: #0f172a; }}
    .agent-card span {{ color: #475569; font-size: 12px; }}
    .score-row {{ display: grid; grid-template-columns: 76px 1fr 36px; gap: 8px; align-items: center; margin: 7px 0; font-size: 12px; }}
    .score-track {{ height: 8px; border-radius: 999px; background: #e5e7eb; overflow: hidden; }}
    .score-fill {{ height: 100%; border-radius: 999px; background: #2563eb; }}
    .two-col {{ display: grid; grid-template-columns: 0.8fr 1.2fr; gap: 14px; align-items: start; }}
    .action-box {{ border: 1px solid #bfdbfe; background: #eff6ff; border-radius: 8px; padding: 14px; }}
    .action-box h2 {{ border: 0; margin: 0 0 8px; padding: 0; }}
    .confidence {{ color: #2563eb; font-weight: 700; font-size: 12px; }}
    .impact {{ font-weight: 700; }}
    .insight-board {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin-top: 12px; }}
    .insight-card {{ border: 1px solid #c7d2fe; border-radius: 8px; background: #f8fafc; overflow: hidden; }}
    .insight-head {{ padding: 10px 12px; background: #eef2ff; border-bottom: 1px solid #c7d2fe; }}
    .insight-head span {{ display: block; color: #4f46e5; font-size: 11px; font-weight: 700; margin-bottom: 3px; }}
    .insight-head strong {{ color: #111827; font-size: 15px; }}
    .insight-card p {{ margin: 10px 12px; }}
    .insight-impact {{ margin: 10px 12px; padding: 9px; border-radius: 7px; background: #ffffff; border: 1px solid #e5e7eb; }}
    .insight-impact span {{ display: block; color: #64748b; font-size: 11px; margin-bottom: 4px; }}
    .insight-impact b {{ color: #0f172a; }}
    .insight-action {{ margin: 10px 12px; padding: 9px; border-radius: 7px; background: #ecfdf5; color: #065f46; font-weight: 700; }}
    .insight-sources {{ margin: 10px 12px 12px; font-size: 12px; color: #64748b; }}
    .insight-sources a {{ color: #2563eb; text-decoration: none; }}
    .diary-list {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-top: 12px; }}
    .diary-card {{ border: 1px solid #dbe3ef; border-radius: 8px; background: #ffffff; overflow: hidden; }}
    .diary-head {{ display: flex; justify-content: space-between; gap: 8px; padding: 10px 12px; background: #f1f5f9; border-bottom: 1px solid #e5e7eb; }}
    .diary-head strong {{ color: #0f172a; }}
    .diary-head span {{ color: #64748b; font-size: 12px; }}
    .diary-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 0; }}
    .diary-grid div {{ padding: 10px 12px; border-right: 1px solid #e5e7eb; }}
    .diary-grid div:last-child {{ border-right: 0; }}
    .diary-grid span {{ display: block; color: #64748b; font-size: 11px; margin-bottom: 4px; }}
    .diary-grid b {{ display: block; color: #111827; margin-bottom: 5px; }}
    .diary-grid small {{ display: block; color: #475569; line-height: 1.4; }}
    @media (max-width: 820px) {{
      .summary-grid, .agent-grid, .two-col, .insight-board, .diary-list, .diary-grid, .method-grid {{ grid-template-columns: 1fr; }}
      .diary-grid div {{ border-right: 0; border-bottom: 1px solid #e5e7eb; }}
      .diary-grid div:last-child {{ border-bottom: 0; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <p class="pill">Atlas AI Morning Brief</p>
      <h1>매일 아침 7시, 당신의 자산을 가장 먼저 확인하는 AI</h1>
      <p class="muted">{escape(settings['user_name'])}님의 포트폴리오 점검 리포트 · 생성 시각: {escape(now_text())}</p>
    </section>

    <h2>Executive Summary</h2>
    <div class="summary-grid">
      <div class="metric-card"><span>Portfolio Health</span><strong>{health_score}/100</strong><span>{escape(health_level)} · 전일 대비 {escape(health_delta)}</span></div>
      <div class="metric-card"><span>오늘의 행동</span><strong>{escape(today_action)}</strong><span>{escape(action_summary)}</span></div>
      <div class="metric-card"><span>핵심 편중</span><strong>{top_bucket_ratio:.1f}%</strong><span>{escape(top_bucket)} 실질 노출</span></div>
      <div class="metric-card"><span>현금 비중</span><strong>{cash_ratio:.1f}%</strong><span>{fmt_krw(cash_value_krw)} 기준</span></div>
    </div>
    {methodology_html}
    <p>{escape(concentration_note_from_items(analysis_items, total_asset_value))}</p>
    <ul>{totals_html}{cash_html}</ul>

    <div class="two-col">
      <section class="metric-card">
        <h2>AI Health Score</h2>
        {score_cards_html}
      </section>
      <section class="action-box">
        <h2>오늘의 Action Plan</h2>
        <ol>{action_plan_html}</ol>
      </section>
    </div>

    <h2>5-Agent Analysis</h2>
    <div class="agent-grid">{agent_cards_html}</div>

    <h2>My Portfolio Impact</h2>
    <table>
      <thead><tr><th>종목명</th><th>영향</th><th>근거</th><th>신뢰도</th></tr></thead>
      <tbody>{impact_rows_html}</tbody>
    </table>

    <h2>보유 종목</h2>
    <table>
      <thead>
        <tr>
          <th>티커</th>
          <th>종목명</th>
          <th>수량</th>
          <th>평균단가</th>
          <th>현재가</th>
          <th>수익률</th>
          <th>비중</th>
          <th>평가금액 USD</th>
          <th>평가금액 KRW</th>
          <th>포지션 의견</th>
          <th>통화</th>
          <th>메모</th>
        </tr>
      </thead>
      <tbody>{holdings_html}</tbody>
    </table>

    <h2>Overnight Market</h2>
    <p class="muted">API 호출 제한을 줄이기 위해 시세/뉴스는 최대 {MARKET_CACHE_HOURS}시간 동안 저장된 데이터를 재사용합니다.</p>
    <p class="muted">{fx_html}</p>
    <table>
      <thead>
        <tr>
          <th>티커</th>
          <th>가격</th>
          <th>등락률</th>
          <th>기준일</th>
        </tr>
      </thead>
      <tbody>{market_html}</tbody>
    </table>

    <h2>Research & Strategy Agent</h2>
    <p class="muted">중복 뉴스는 신호 단위로 묶고, 각 신호가 내 포트폴리오에 주는 영향과 오늘의 대응만 남겼습니다.</p>
    {research_strategy_html}

    <h2>Watch List</h2>
    <ul>
      <li>미국 장 마감 이후 기술주·반도체 ETF 흐름과 VIX 변화를 확인합니다.</li>
      <li>원/달러 환율이 상승하면 해외자산 원화 평가액은 올라가지만 신규 환전 부담도 커집니다.</li>
      <li>레버리지 상품은 장중 변동성이 커질 때 손실 제한선과 목표 비중을 먼저 확인합니다.</li>
    </ul>

    <h2>Digital Investment Diary</h2>
    <p class="muted">AI 제안, 사용자의 실제 행동, 1주·1개월 후 결과를 자동으로 누적합니다.</p>
    {diary_html}

    <h2>다음 단계</h2>
    <p>현재 MVP는 사용자가 직접 입력한 보유 종목과 비공식 시세/뉴스 데이터를 함께 사용해 포트폴리오 점검 리포트를 만듭니다.</p>

    <div class="warning">
      본 리포트는 투자 판단을 돕기 위한 정보 정리 자료이며, 특정 종목의 매수 또는 매도를 지시하지 않습니다.
      최종 투자 결정은 사용자 본인의 판단에 따라야 합니다.
    </div>
  </div>
</body>
</html>"""


def send_resend_email(
    subject: str,
    html_body: str,
    recipient: str,
    sender: str,
    api_key: str,
) -> tuple[bool, str]:
    payload = json.dumps(
        {
            "from": sender,
            "to": [recipient],
            "subject": subject,
            "html": html_body,
        }
    ).encode("utf-8")
    request = Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "AtlasAI-Mailer/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            if 200 <= response.status < 300:
                return True, f"{recipient} 주소로 이메일을 발송했습니다. (Resend API)"
            return False, f"Resend 발송 실패: HTTP {response.status}"
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed_error = json.loads(error_body)
            error_message = parsed_error.get("message") or parsed_error.get("error") or error_body
        except json.JSONDecodeError:
            error_message = error_body or str(exc)
        if "1010" in error_message:
            error_message = (
                f"{error_message} / 요청이 Resend 보안 정책에서 차단됐을 가능성이 큽니다. "
                "Resend API Key가 활성 상태인지, From 주소가 onboarding@resend.dev 또는 verified 도메인인지, "
                "테스트 수신자가 Resend 계정 이메일인지 확인해 주세요."
            )
        return False, f"Resend 발송 오류: HTTP {exc.code} - {error_message}"
    except Exception as exc:
        return False, f"Resend 발송 오류: {exc}"


def send_email(subject: str, html_body: str) -> tuple[bool, str]:
    settings = get_settings()
    recipient = settings["recipient_email"]
    resend_api_key = settings["resend_api_key"]
    resend_from = settings["resend_from"]
    smtp_host = settings["smtp_host"]
    smtp_port = int(settings["smtp_port"] or "587")
    smtp_user = settings["smtp_user"]
    smtp_password = settings["smtp_password"]
    smtp_from = settings["smtp_from"] or smtp_user

    PREVIEW_PATH.write_text(html_body, encoding="utf-8")

    if not recipient:
        return False, "수신 이메일이 등록되지 않았습니다. 미리보기 파일만 저장했습니다."
    if resend_api_key and resend_from:
        return send_resend_email(subject, html_body, recipient, resend_from, resend_api_key)
    if not smtp_host or not smtp_user or not smtp_password or not smtp_from:
        return False, "메일 발송 설정이 없어 실제 발송하지 않고 미리보기 파일만 저장했습니다. Resend API 키 또는 SMTP 설정을 추가해 주세요."

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_from
    message["To"] = recipient
    message.set_content("HTML 리포트를 볼 수 있는 메일 클라이언트에서 확인해 주세요.")
    message.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(message)
    return True, f"{recipient} 주소로 이메일을 발송했습니다."


def log_send(status: str, detail: str, sent_for_date: str | None = None) -> None:
    db_execute(
        "INSERT INTO send_log (sent_for_date, sent_at, status, detail) VALUES (?, ?, ?, ?)",
        (sent_for_date or today_text(), now_text(), status, detail),
    )


def clean_money_input(value: str) -> str:
    parsed = parse_float(value)
    return "" if parsed is None else str(parsed)


def clear_user_entered_data() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM holdings")
        conn.execute("DELETE FROM uploads")
        conn.execute("DELETE FROM investment_diary")
        conn.execute("DELETE FROM send_log")
        conn.execute(
            "DELETE FROM settings WHERE key IN ('recipient_email', 'send_time', 'user_name', 'cash_krw', 'cash_usd')"
        )


def send_report(reason: str) -> tuple[bool, str]:
    html_body = build_report_html()
    subject = f"[Atlas AI] {today_text()} 포트폴리오 점검 리포트"
    ok, detail = send_email(subject, html_body)
    log_send("sent" if ok else "preview", f"{reason}: {detail}")
    return ok, detail


def last_scheduled_send_date() -> str:
    rows = db_rows(
        """
        SELECT sent_for_date FROM send_log
        WHERE detail LIKE 'scheduled:%'
        ORDER BY id DESC
        LIMIT 1
        """
    )
    return rows[0]["sent_for_date"] if rows else ""


def scheduler_loop() -> None:
    while True:
        try:
            send_time = get_setting("send_time", "07:00")
            current_time = dt.datetime.now().strftime("%H:%M")
            already_sent = last_scheduled_send_date() == today_text()
            if current_time == send_time and not already_sent:
                send_report("scheduled")
        except Exception as exc:
            log_send("error", f"scheduled: {exc}")
        time.sleep(30)


def page(title: str, body: str) -> bytes:
    settings = get_settings()
    html_doc = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{ color-scheme: light; }}
    body {{ margin: 0; font-family: Arial, sans-serif; background: #f6f7f9; color: #111827; }}
    header {{ background: #111827; color: white; padding: 18px 24px; }}
    main {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
    section {{ background: white; border: 1px solid #e5e7eb; border-radius: 8px; padding: 18px; margin-bottom: 16px; }}
    h1 {{ margin: 0; font-size: 22px; }}
    h2 {{ margin-top: 0; font-size: 18px; }}
    label {{ display: block; font-weight: 700; margin-top: 12px; }}
    input, textarea, select {{ width: 100%; box-sizing: border-box; padding: 10px; margin-top: 6px; border: 1px solid #d1d5db; border-radius: 6px; }}
    textarea {{ min-height: 72px; }}
    button, .button {{ display: inline-block; border: 0; background: #2563eb; color: white; padding: 10px 14px; border-radius: 6px; margin-top: 14px; text-decoration: none; cursor: pointer; }}
    .secondary {{ background: #4b5563; }}
    .danger {{ background: #dc2626; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid #e5e7eb; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f9fafb; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }}
    .muted {{ color: #6b7280; font-size: 13px; }}
    .notice {{ background: #ecfeff; border-color: #a5f3fc; }}
    .warning {{ background: #fff7ed; border-color: #fed7aa; }}
    .steps {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; margin: 12px 0; }}
    .steps div {{ display: flex; gap: 10px; align-items: center; background: white; border: 1px solid #dbeafe; border-radius: 8px; padding: 12px; }}
    .steps strong {{ display: inline-flex; width: 26px; height: 26px; align-items: center; justify-content: center; border-radius: 999px; background: #2563eb; color: white; flex: 0 0 auto; }}
    table input, table select {{ min-width: 90px; padding: 7px; margin: 0; }}
    td form {{ display: inline-block; margin: 0 4px 0 0; }}
  </style>
</head>
<body>
  <header>
    <h1>Atlas Stock Mailer MVP</h1>
    <div class="muted">수신 이메일: {escape(settings['recipient_email'] or '미등록')} · 매일 발송 시간: {escape(settings['send_time'])}</div>
  </header>
  <main>{body}</main>
</body>
</html>"""
    return html_doc.encode("utf-8")


def redirect(location: str = "/") -> bytes:
    return f"HTTP/1.1 303 See Other\r\nLocation: {location}\r\n\r\n".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self.respond_json({"status": "ok"})
            return
        if parsed.path == "/ticker-lookup":
            params = parse_qs(parsed.query)
            self.respond_json(lookup_ticker(params.get("ticker", [""])[0]))
            return
        if parsed.path == "/preview":
            self.respond_html(build_report_html().encode("utf-8"))
            return
        if parsed.path == "/email_preview.html":
            if PREVIEW_PATH.exists():
                self.respond_html(PREVIEW_PATH.read_bytes())
            else:
                self.send_error(404)
            return
        if parsed.path == "/api-settings":
            if not self.is_operator_request():
                self.send_error(404)
                return
            self.respond_html(page("운영자 시세/뉴스 설정", self.api_settings_body()))
            return
        self.respond_html(page("Atlas Stock Mailer", self.home_body()))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/settings":
            form = self.read_form()
            set_setting("user_name", form.get("user_name", [""])[0])
            set_setting("recipient_email", form.get("recipient_email", [""])[0])
            set_setting("send_time", form.get("send_time", ["07:00"])[0])
            set_setting("cash_krw", clean_money_input(form.get("cash_krw", [""])[0]))
            set_setting("cash_usd", clean_money_input(form.get("cash_usd", [""])[0]))
            self.respond_redirect("/")
            return
        if parsed.path == "/smtp":
            if not self.is_operator_request():
                self.send_error(404)
                return
            form = self.read_form()
            set_setting("resend_from", form.get("resend_from", [""])[0])
            resend_api_key = form.get("resend_api_key", [""])[0]
            if resend_api_key:
                set_setting("resend_api_key", resend_api_key)
            set_setting("smtp_host", form.get("smtp_host", ["smtp.gmail.com"])[0])
            set_setting("smtp_port", form.get("smtp_port", ["587"])[0])
            set_setting("smtp_user", form.get("smtp_user", [""])[0])
            set_setting("smtp_from", form.get("smtp_from", [""])[0])
            password = form.get("smtp_password", [""])[0]
            if password:
                set_setting("smtp_password", password)
            self.respond_redirect("/")
            return
        if parsed.path == "/api-settings":
            if not self.is_operator_request():
                self.send_error(404)
                return
            form = self.read_form()
            api_key = form.get("alpha_vantage_key", [""])[0].strip()
            if api_key:
                set_setting("alpha_vantage_key", api_key)
            self.respond_redirect("/api-settings")
            return
        if parsed.path == "/holding":
            form = self.read_form()
            ticker_info = lookup_ticker(form.get("ticker", [""])[0])
            name = form.get("name", [""])[0].strip() or ticker_info["name"]
            currency = form.get("currency", [ticker_info["currency"]])[0].strip() or ticker_info["currency"]
            db_execute(
                """
                INSERT INTO holdings (ticker, name, quantity, avg_price, current_price, currency, memo, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker_info["ticker"],
                    name,
                    parse_float(form.get("quantity", [""])[0]),
                    parse_float(form.get("avg_price", [""])[0]),
                    parse_float(form.get("current_price", [""])[0]),
                    currency,
                    form.get("memo", [""])[0].strip(),
                    now_text(),
                ),
            )
            self.respond_redirect("/")
            return
        if parsed.path == "/update-holding":
            form = self.read_form()
            holding_id = int(form.get("holding_id", ["0"])[0] or "0")
            ticker_info = lookup_ticker(form.get("ticker", [""])[0])
            name = form.get("name", [""])[0].strip() or ticker_info["name"]
            currency = form.get("currency", [ticker_info["currency"]])[0].strip() or ticker_info["currency"]
            db_execute(
                """
                UPDATE holdings
                SET ticker = ?, name = ?, quantity = ?, avg_price = ?, current_price = ?, currency = ?, memo = ?
                WHERE id = ?
                """,
                (
                    ticker_info["ticker"],
                    name,
                    parse_float(form.get("quantity", [""])[0]),
                    parse_float(form.get("avg_price", [""])[0]),
                    parse_float(form.get("current_price", [""])[0]),
                    currency,
                    form.get("memo", [""])[0].strip(),
                    holding_id,
                ),
            )
            self.respond_redirect("/")
            return
        if parsed.path == "/delete-holding":
            form = self.read_form()
            holding_id = int(form.get("holding_id", ["0"])[0] or "0")
            db_execute("DELETE FROM holdings WHERE id = ?", (holding_id,))
            self.respond_redirect("/")
            return
        if parsed.path == "/clear-user-data":
            clear_user_entered_data()
            if PREVIEW_PATH.exists():
                PREVIEW_PATH.unlink()
            self.respond_redirect("/")
            return
        if parsed.path == "/diary-action":
            form = self.read_form()
            diary_id = int(form.get("diary_id", ["0"])[0] or "0")
            db_execute(
                """
                UPDATE investment_diary
                SET user_action = ?, action_note = ?, action_date = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    form.get("user_action", [""])[0],
                    form.get("action_note", [""])[0].strip(),
                    today_text(),
                    now_text(),
                    diary_id,
                ),
            )
            self.respond_redirect("/")
            return
        if parsed.path == "/upload":
            self.respond_html(page("Upload disabled", "<section><h2>업로드 기능 제거됨</h2><p>이 버전은 보유 종목을 수기로 입력하는 방식으로 단순화했습니다.</p><a class='button' href='/'>돌아가기</a></section>"))
            return
        if parsed.path == "/ocr-upload":
            self.respond_html(page("OCR disabled", "<section><h2>OCR 기능 제거됨</h2><p>오인식 문제가 있어 OCR 자동등록을 제거하고 수기 입력 방식으로 바꿨습니다.</p><a class='button' href='/'>돌아가기</a></section>"))
            return
        if parsed.path == "/send-test":
            ok, detail = send_report("manual")
            status = "발송 완료" if ok else "미리보기 저장"
            cleanup_message = ""
            preview_link = "<a class='button secondary' href='/email_preview.html'>미리보기 열기</a>"
            if ok and CLEAR_USER_DATA_AFTER_SUCCESS:
                clear_user_entered_data()
                if PREVIEW_PATH.exists():
                    PREVIEW_PATH.unlink()
                cleanup_message = "<p class='muted'>개인정보 보호를 위해 입력한 이메일과 보유 종목은 발송 후 초기화했습니다.</p>"
                preview_link = ""
            self.respond_html(page(status, f"<section><h2>{escape(status)}</h2><p>{escape(detail)}</p>{cleanup_message}<a class='button' href='/'>돌아가기</a>{preview_link}</section>"))
            return
        self.send_error(404)

    def read_form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        return parse_qs(body)

    def is_operator_request(self) -> bool:
        token = os.getenv("ADMIN_TOKEN", "")
        if not token:
            return False
        params = parse_qs(urlparse(self.path).query)
        return (
            params.get("token", [""])[0] == token
            or self.headers.get("X-Admin-Token", "") == token
        )

    def handle_upload(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        content_type = self.headers.get("Content-Type", "")
        raw_body = self.rfile.read(length)
        message_bytes = (
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
            + raw_body
        )
        form = BytesParser(policy=policy.default).parsebytes(message_bytes)
        note = ""

        screenshot_name = ""
        screenshot_bytes = b""
        for part in form.iter_parts():
            field_name = part.get_param("name", header="content-disposition")
            if field_name == "note":
                note = part.get_content().strip()
            if field_name == "screenshot":
                screenshot_name = part.get_filename() or ""
                screenshot_bytes = part.get_payload(decode=True) or b""

        if not screenshot_name or not screenshot_bytes:
            return
        safe_name = Path(screenshot_name).name
        suffix = Path(safe_name).suffix[:12]
        stored_name = f"{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}_{len(uploads()) + 1}{suffix}"
        target = UPLOAD_DIR / stored_name
        target.write_bytes(screenshot_bytes)
        db_execute(
            "INSERT INTO uploads (original_name, stored_name, note, created_at) VALUES (?, ?, ?, ?)",
            (safe_name, stored_name, note, now_text()),
        )

    def home_body(self) -> str:
        settings = get_settings()
        stock_rows = holdings()
        holdings_html = "".join(
            "<tr>"
            f"<td><input form='edit-holding-{escape(row['id'])}' name='ticker' value='{escape(row['ticker'])}'></td>"
            f"<td><input form='edit-holding-{escape(row['id'])}' name='name' value='{escape(row['name'])}'></td>"
            f"<td><input form='edit-holding-{escape(row['id'])}' name='quantity' type='number' min='0' step='1' value='{escape(fmt_quantity(as_float(row['quantity'])).replace(',', ''))}'></td>"
            f"<td><input class='money-input' form='edit-holding-{escape(row['id'])}' name='avg_price' inputmode='decimal' value='{escape(fmt_input_money(row['avg_price']))}'></td>"
            f"<td><input class='money-input' form='edit-holding-{escape(row['id'])}' name='current_price' inputmode='decimal' value='{escape(fmt_input_money(row['current_price']))}'></td>"
            f"<td><select form='edit-holding-{escape(row['id'])}' name='currency'>"
            f"<option {'selected' if row['currency'] == 'KRW' else ''}>KRW</option>"
            f"<option {'selected' if row['currency'] == 'USD' else ''}>USD</option>"
            "</select></td>"
            f"<td><input form='edit-holding-{escape(row['id'])}' name='memo' value='{escape(row['memo'])}'></td>"
            "<td>"
            f"<form id='edit-holding-{escape(row['id'])}' method='post' action='/update-holding' style='display:inline'>"
            f"<input type='hidden' name='holding_id' value='{escape(row['id'])}'>"
            "<button type='submit'>저장</button>"
            "</form>"
            "<form method='post' action='/delete-holding' style='margin:0'>"
            f"<input type='hidden' name='holding_id' value='{escape(row['id'])}'>"
            "<button class='danger' type='submit'>삭제</button>"
            "</form>"
            "</td>"
            "</tr>"
            for row in stock_rows
        ) or "<tr><td colspan='8'>아직 입력된 보유 종목이 없습니다.</td></tr>"

        return f"""
<section class="notice">
  <h2>Atlas AI 사용 방법</h2>
  <div class="steps">
    <div><strong>1</strong><span>받을 이메일과 발송 시간을 입력합니다.</span></div>
    <div><strong>2</strong><span>보유 종목을 티커, 수량, 평균단가 기준으로 추가합니다.</span></div>
    <div><strong>3</strong><span>리포트를 미리 확인한 뒤 이메일로 발송합니다.</span></div>
  </div>
  <p class="muted">운영자용 메일 발송, 시세, 뉴스 설정은 배포 서버 환경변수로 관리됩니다. 사용자가 별도 API 키를 입력할 필요는 없습니다.</p>
</section>

<div class="grid">
  <section>
    <h2>1. 이메일과 발송 시간 등록</h2>
    <form method="post" action="/settings">
      <label>사용자 이름</label>
      <input name="user_name" value="{escape(settings['user_name'])}" placeholder="예: 김OO">
      <label>수신 이메일</label>
      <input name="recipient_email" type="email" value="{escape(settings['recipient_email'])}" placeholder="example@email.com">
      <label>매일 발송 시간</label>
      <input name="send_time" type="time" value="{escape(settings['send_time'])}">
      <label>보유 현금 (KRW)</label>
      <input class="money-input" name="cash_krw" inputmode="decimal" value="{escape(fmt_input_money(settings['cash_krw']))}" placeholder="예: 1,000,000">
      <label>보유 현금 (USD)</label>
      <input class="money-input" name="cash_usd" inputmode="decimal" value="{escape(fmt_input_money(settings['cash_usd']))}" placeholder="예: 500">
      <button type="submit">저장</button>
    </form>
    <p class="muted">현금은 전체자산 비중, 핵심 편중, AI Health Score에 함께 반영됩니다. 공개 배포 데모에서는 발송 성공 후 입력 정보가 초기화됩니다.</p>
  </section>

  <section>
    <h2>2. 보유 종목 입력</h2>
    <form method="post" action="/holding">
      <label>티커</label>
      <input id="ticker-input" name="ticker" placeholder="예: NVDA, 005930">
      <label>종목명</label>
      <input id="name-input" name="name" placeholder="티커를 입력하면 자동으로 채워집니다">
      <label>수량</label>
      <input name="quantity" type="number" min="0" step="1" placeholder="예: 10">
      <label>평균단가</label>
      <input class="money-input" name="avg_price" inputmode="decimal" placeholder="예: 120.5">
      <label>현재가</label>
      <input class="money-input" name="current_price" inputmode="decimal" placeholder="선택 입력. 시세 연동 전에는 직접 입력">
      <label>통화</label>
      <select id="currency-input" name="currency">
        <option>KRW</option>
        <option>USD</option>
      </select>
      <label>메모</label>
      <textarea name="memo" placeholder="예: 장기 보유, 반도체 핵심 종목"></textarea>
      <button type="submit">종목 추가</button>
    </form>
  </section>
</div>

<section>
  <h2>3. 이번 리포트에 포함될 종목</h2>
  <table>
    <thead><tr><th>티커</th><th>종목명</th><th>수량</th><th>평균단가</th><th>현재가</th><th>통화</th><th>메모</th><th>관리</th></tr></thead>
    <tbody>{holdings_html}</tbody>
  </table>
  <form method="post" action="/clear-user-data">
    <button class="danger" type="submit">입력 내용 전체 삭제</button>
  </form>
</section>

<section>
  <h2>4. 리포트 확인 및 발송</h2>
  <p class="muted">리포트는 입력한 보유 종목을 기준으로 생성됩니다. 이메일 발송이 성공하면 개인정보 보호를 위해 입력 내용은 자동 초기화됩니다.</p>
  <form method="post" action="/send-test">
    <button type="submit">리포트 이메일 발송</button>
    <a class="button secondary" href="/preview">리포트 미리보기</a>
  </form>
</section>

<section class="warning">
  <h2>안내</h2>
  <p>Atlas AI는 투자 판단을 돕는 정보 정리 도구입니다. 최종 매수·매도 결정은 사용자 본인의 판단에 따라야 합니다.</p>
</section>

<script>
async function fillTickerInfo() {{
  const tickerInput = document.getElementById('ticker-input');
  const nameInput = document.getElementById('name-input');
  const currencyInput = document.getElementById('currency-input');
  const ticker = tickerInput.value.trim();
  if (!ticker) return;
  const response = await fetch('/ticker-lookup?ticker=' + encodeURIComponent(ticker));
  if (!response.ok) return;
  const data = await response.json();
  tickerInput.value = data.ticker || ticker;
  if (!nameInput.value.trim()) nameInput.value = data.name || '';
  if (data.currency) currencyInput.value = data.currency;
}}
function formatMoneyInput(input) {{
  const original = input.value;
  const cleaned = original.replace(/,/g, '').replace(/[^\\d.]/g, '');
  if (!cleaned) {{
    input.value = '';
    return;
  }}
  const parts = cleaned.split('.');
  const integerPart = parts[0] || '0';
  const decimalPart = parts.length > 1 ? '.' + parts.slice(1).join('').slice(0, 4) : '';
  input.value = Number(integerPart).toLocaleString('en-US') + decimalPart;
}}
document.addEventListener('DOMContentLoaded', () => {{
  const tickerInput = document.getElementById('ticker-input');
  if (tickerInput) {{
    tickerInput.addEventListener('blur', fillTickerInfo);
    tickerInput.addEventListener('change', fillTickerInfo);
  }}
  document.querySelectorAll('.money-input').forEach((input) => {{
    formatMoneyInput(input);
    input.addEventListener('input', () => formatMoneyInput(input));
    input.addEventListener('blur', () => formatMoneyInput(input));
  }});
}});
</script>
"""

    def api_settings_body(self) -> str:
        settings = get_settings()
        alpha_status = "설정됨" if settings["alpha_vantage_key"] else "미설정"
        unofficial_status = "사용 가능" if yfinance_available() else "설치 필요"
        return f"""
<section>
  <h2>운영자 시세/뉴스 보조 설정</h2>
  <p class="muted">기본값은 비공식 Yahoo Finance 기반 데이터입니다. Alpha Vantage 키는 비공식 데이터가 실패할 때 사용할 보조 옵션입니다.</p>
  <p><strong>비공식 데이터 라이브러리: {escape(unofficial_status)}</strong></p>
  <p><strong>Alpha Vantage API Key: {escape(alpha_status)}</strong></p>
  <form method="post" action="/api-settings">
    <label>Alpha Vantage API Key</label>
    <input name="alpha_vantage_key" type="password" placeholder="발급받은 키를 붙여넣기">
    <p class="muted">배포 서버에서는 이 값 대신 환경변수 ALPHA_VANTAGE_KEY로 설정하는 것을 권장합니다.</p>
    <button type="submit">시세/뉴스 연동 저장</button>
    <a class="button secondary" href="/">돌아가기</a>
  </form>
</section>
"""

    def respond_html(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_json(self, payload: dict[str, str]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{now_text()}] {fmt % args}")


def main() -> None:
    ensure_storage()
    scheduler = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Atlas Stock Mailer MVP running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


if __name__ == "__main__":
    main()
