from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from flask import Flask, jsonify, request, send_from_directory
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
CACHE_FILE = DATA_DIR / "events_cache.json"
STOCK_CONFIG_FILE = BASE_DIR / "stocks.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0 Safari/537.36"
        )
    }
)
retry = Retry(
    total=3,
    connect=3,
    backoff_factor=0.6,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=frozenset(["GET"]),
)
adapter = HTTPAdapter(max_retries=retry)
SESSION.mount("https://", adapter)
SESSION.mount("http://", adapter)

NOTICE_ENDPOINT = "https://np-anotice-stock.eastmoney.com/api/security/ann"
A_SHARE_CALENDAR_ENDPOINT = "https://datacenter-web.eastmoney.com/api/data/v1/get"
MACRO_FASTNEWS_ENDPOINT = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
SEARCH_SUGGEST_ENDPOINT = "https://searchapi.eastmoney.com/api/suggest/get"
MACRO_FAST_COLUMNS = "125,126,127,128,129,130,131"
MACRO_MAX_PAGES = 12

# Public repo keeps stock config empty by default.
# Users should create local stocks.json (or save from UI) for their own watchlist.
DEFAULT_STOCKS: list[dict[str, str]] = []

A_REPORT_COLUMNS = {
    "年度报告全文",
    "年度报告摘要",
    "年度报告全文(英文)",
    "半年度报告全文",
    "半年度报告摘要",
    "一季度报告全文",
    "三季度报告全文",
    "季度报告全文",
}

HK_EARNINGS_PATTERN = re.compile(r"(业绩公告|年度业绩|中期业绩|季度业绩|董事会会议召开日期)", re.IGNORECASE)
US_TITLE_PATTERN = re.compile(r"(earnings|financial results|10-q|10-k|业绩)", re.IGNORECASE)
US_REPORT_COLUMNS = {"10-Q", "10-K", "8-K 2.02", "PRESENTATION"}

MACRO_MARKET_PATTERNS = [
    ("港股", re.compile(r"(香港|港元|香港特区|金管局)", re.IGNORECASE)),
    ("美股", re.compile(r"(美国|美联储|华尔街|非农|初请失业金|ADP)", re.IGNORECASE)),
    ("A股", re.compile(r"(中国|国家统计局|中国人民银行|全国城镇|内地|国务院)", re.IGNORECASE)),
]

MACRO_PATTERNS = [
    ("就业率/就业数据", re.compile(r"(失业|就业|非农|初请失业金|adp|unemployment|employment|jobless)", re.IGNORECASE)),
    ("PMI", re.compile(r"(PMI|采购经理)", re.IGNORECASE)),
    ("CPI", re.compile(r"(CPI|消费者物价|通胀|inflation)", re.IGNORECASE)),
    ("GDP", re.compile(r"(GDP|国内生产总值|gross domestic product)", re.IGNORECASE)),
    (
        "住宅价格",
        re.compile(
            r"(房价|住宅|新屋销售|成屋销售|case-shiller|s&p/cs|fhfa|home\s*price|house\s*price)",
            re.IGNORECASE,
        ),
    ),
]

MACRO_RELEASE_HINT_PATTERN = re.compile(
    r"(指数|数据|同比|环比|录得|公布|预期|前值|上涨|下降|增加|减少|百分点|万人|%|pct)",
    re.IGNORECASE,
)

CACHE_LOCK = threading.Lock()


def normalize_stock(item: dict[str, Any]) -> dict[str, str] | None:
    name = str(item.get("name", "")).strip()
    raw_code = str(item.get("code", "")).strip()
    market_code = str(item.get("market_code", "")).strip()
    market = str(item.get("market", "")).strip()
    if not name or not raw_code:
        return None

    code = raw_code
    upper_code = raw_code.upper()

    if "." in upper_code:
        base, suffix = upper_code.rsplit(".", 1)
        code = base
        if not market_code or not market:
            if suffix == "XSHE":
                market_code, market = "0", "A股"
            elif suffix == "XSHG":
                market_code, market = "1", "A股"
            elif suffix == "XHKG":
                market_code, market = "116", "港股"
            elif suffix in {"XNAS", "XNYS", "US"}:
                market_code, market = "105", "美股"

    if not market_code or not market:
        # fallback: infer by code pattern
        if re.fullmatch(r"\d{5}", code):
            market_code, market = "116", "港股"
        elif re.fullmatch(r"\d{6}", code):
            if code.startswith(("5", "6", "9")):
                market_code, market = "1", "A股"
            else:
                market_code, market = "0", "A股"
        elif re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,11}", code):
            market_code, market = "105", "美股"
            code = code.upper()
        else:
            return None

    return {
        "name": name,
        "code": code,
        "market_code": market_code,
        "market": market,
    }


def load_stocks() -> list[dict[str, str]]:
    if not STOCK_CONFIG_FILE.exists():
        return DEFAULT_STOCKS

    try:
        payload = json.loads(STOCK_CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            valid = []
            for item in payload:
                if not isinstance(item, dict):
                    continue
                normalized = normalize_stock(item)
                if normalized:
                    valid.append(normalized)
            if valid:
                return valid
    except Exception:
        logging.exception("Failed to parse %s, fallback to defaults", STOCK_CONFIG_FILE)

    return DEFAULT_STOCKS


def save_stocks_config(stocks_input: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for item in stocks_input:
        if not isinstance(item, dict):
            continue
        stock = normalize_stock(item)
        if not stock:
            continue
        dedupe_key = (stock["market_code"], stock["code"])
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append(stock)

    if not normalized:
        raise ValueError("股票列表为空或格式无效")

    STOCK_CONFIG_FILE.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return normalized


def request_json(url: str, params: dict[str, Any], timeout: int = 30) -> dict[str, Any] | None:
    try:
        response = SESSION.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except Exception:
        logging.exception("Request failed: %s", url)
        return None


def market_from_mkt_num(mkt_num: str) -> str | None:
    if mkt_num in {"0", "1"}:
        return "A股"
    if mkt_num == "116":
        return "港股"
    if mkt_num in {"105", "106", "107"}:
        return "美股"
    return None


def group_from_mkt_num(mkt_num: str) -> str | None:
    if mkt_num in {"0", "1"}:
        return "A"
    if mkt_num == "116":
        return "HK"
    if mkt_num in {"105", "106", "107"}:
        return "US"
    return None


def mkt_num_set_from_group(group: str | None) -> set[str]:
    if group == "A":
        return {"0", "1"}
    if group == "HK":
        return {"116"}
    if group == "US":
        return {"105", "106", "107"}
    return {"0", "1", "116", "105", "106", "107"}


def display_code_for_mkt_num(code: str, mkt_num: str) -> str:
    raw = str(code or "").strip().upper()
    if not raw:
        return raw

    if "." in raw:
        return raw

    if mkt_num == "0":
        digits = re.sub(r"\D", "", raw)
        return f"{digits.zfill(6)}.XSHE" if digits else raw
    if mkt_num == "1":
        digits = re.sub(r"\D", "", raw)
        return f"{digits.zfill(6)}.XSHG" if digits else raw
    if mkt_num == "116":
        digits = re.sub(r"\D", "", raw)
        return f"{digits.zfill(5)}.XHKG" if digits else raw
    if mkt_num in {"105", "106", "107"}:
        return f"{raw}.US"
    return raw


def strip_known_suffix(text: str) -> tuple[str, str]:
    raw = str(text or "").strip().upper()
    if "." not in raw:
        return raw, ""
    base, suffix = raw.rsplit(".", 1)
    if suffix in {"XSHE", "XSHG", "XHKG", "US", "XNAS", "XNYS"}:
        return base, suffix
    return raw, ""


def resolve_stock_by_query(query: str, group: str | None = None) -> dict[str, str] | None:
    query_raw = str(query or "").strip()
    if not query_raw:
        return None

    query_code, query_suffix = strip_known_suffix(query_raw)
    allowed_mkt_nums = mkt_num_set_from_group(group)
    suffix_to_mkt = {
        "XSHE": {"0"},
        "XSHG": {"1"},
        "XHKG": {"116"},
        "US": {"105"},
        "XNAS": {"105"},
        "XNYS": {"105"},
    }
    if query_suffix in suffix_to_mkt:
        allowed_mkt_nums = suffix_to_mkt[query_suffix]

    payload = request_json(
        SEARCH_SUGGEST_ENDPOINT,
        {
            "input": query_raw,
            "type": "14",
        },
        timeout=15,
    )

    rows = ((payload or {}).get("QuotationCodeTable") or {}).get("Data") or []
    if not isinstance(rows, list):
        return None

    query_upper = query_code.upper()
    query_text = query_raw.lower()
    best: dict[str, Any] | None = None
    best_score = -1
    seen: set[tuple[str, str]] = set()

    for row in rows:
        if not isinstance(row, dict):
            continue
        mkt_num = str(row.get("MktNum") or "").strip()
        if mkt_num not in {"0", "1", "116", "105", "106", "107"}:
            continue
        if mkt_num not in allowed_mkt_nums:
            continue

        code = str(row.get("Code") or "").strip().upper()
        name = str(row.get("Name") or "").strip()
        if not code or not name:
            continue

        unique_key = (mkt_num, code)
        if unique_key in seen:
            continue
        seen.add(unique_key)

        score = 0
        if code == query_upper:
            score += 120
        elif query_upper and code.startswith(query_upper):
            score += 85

        name_lower = name.lower()
        if name_lower == query_text:
            score += 110
        elif query_text and name_lower.startswith(query_text):
            score += 70
        elif query_text and query_text in name_lower:
            score += 45

        pinyin = str(row.get("PinYin") or "").strip().upper()
        if query_upper and pinyin == query_upper:
            score += 65
        elif query_upper and pinyin.startswith(query_upper):
            score += 35

        if group and mkt_num in mkt_num_set_from_group(group):
            score += 5

        if score > best_score:
            best_score = score
            best = {
                "mkt_num": mkt_num,
                "code": code,
                "name": name,
            }

    if not best:
        return None

    market = market_from_mkt_num(best["mkt_num"])
    market_group = group_from_mkt_num(best["mkt_num"])
    if not market or not market_group:
        return None

    return {
        "name": best["name"],
        "code": display_code_for_mkt_num(best["code"], best["mkt_num"]),
        "market": market,
        "market_code": best["mkt_num"],
        "group": market_group,
    }


def to_date_str(value: Any) -> str | None:
    if value is None:
        return None

    raw = str(value)
    match = re.search(r"\d{4}-\d{2}-\d{2}", raw)
    return match.group(0) if match else None


def parse_date(value: Any) -> date | None:
    text = to_date_str(value)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def get_columns(item: dict[str, Any]) -> set[str]:
    cols = set()
    for column in item.get("columns", []) or []:
        name = str(column.get("column_name", "")).strip()
        if name:
            cols.add(name)
    return cols


def notice_detail_url(stock_code: str, art_code: str) -> str:
    return f"https://data.eastmoney.com/notices/detail/{stock_code}/{art_code}.html"


def build_notice_event(
    stock: dict[str, str],
    item: dict[str, Any],
    event_type: str,
) -> dict[str, Any] | None:
    event_date = to_date_str(item.get("notice_date"))
    if not event_date:
        return None

    art_code = str(item.get("art_code", "")).strip()
    title = str(item.get("title_ch") or item.get("title") or "").strip()
    if not title:
        return None

    clean_title = title.split("|", 1)[-1].strip() if "|" in title else title
    event_id = f"stock:{stock['code']}:{art_code}:{event_type}"
    source_url = notice_detail_url(stock["code"], art_code) if art_code else ""

    return {
        "id": event_id,
        "category": "stock",
        "title": f"{stock['name']} · {clean_title}",
        "start": event_date,
        "allDay": True,
        "market": stock["market"],
        "stockCode": stock["code"],
        "eventType": event_type,
        "description": title,
        "sourceUrl": source_url,
        "sourceLabel": "东方财富公告",
    }


def fetch_announcements(stock: dict[str, str], max_pages: int = 8) -> list[dict[str, Any]]:
    page_index = 1
    records: list[dict[str, Any]] = []

    while page_index <= max_pages:
        payload = request_json(
            NOTICE_ENDPOINT,
            {
                "page_size": 100,
                "page_index": page_index,
                "market_code": stock["market_code"],
                "stock_list": stock["code"],
                "client_source": "web",
            },
        )

        data = (payload or {}).get("data") or {}
        entries = data.get("list") or []
        if not entries:
            break

        records.extend(entries)
        page_count = int(data.get("page_count") or 1)
        if page_index >= page_count:
            break

        page_index += 1

    return records


def filter_stock_report_events(stock: dict[str, str], entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    market_code = stock["market_code"]

    for item in entries:
        title = str(item.get("title_ch") or item.get("title") or "")
        columns = get_columns(item)

        include = False
        event_type = "财报公告"

        if market_code in {"0", "1"}:
            if columns & A_REPORT_COLUMNS:
                include = True
                event_type = "财报公告"

        elif market_code == "116":
            if HK_EARNINGS_PATTERN.search(title):
                include = True
                if "董事会会议召开日期" in title:
                    event_type = "董事会会议（财报相关）"
                else:
                    event_type = "业绩公告"

        elif market_code == "105":
            if columns & US_REPORT_COLUMNS or US_TITLE_PATTERN.search(title):
                include = True
                if "10-K" in columns:
                    event_type = "10-K 年度报告"
                elif "10-Q" in columns:
                    event_type = "10-Q 季度报告"
                elif "8-K 2.02" in columns:
                    event_type = "8-K 业绩披露"
                elif "PRESENTATION" in columns:
                    event_type = "业绩演示文稿"
                else:
                    event_type = "财报相关公告"

        if not include:
            continue

        event = build_notice_event(stock, item, event_type)
        if event:
            events.append(event)

    return events


def fetch_a_share_appointments(stock: dict[str, str]) -> list[dict[str, Any]]:
    if stock["market_code"] not in {"0", "1"}:
        return []

    rows: list[dict[str, Any]] = []
    page_number = 1

    while page_number <= 5:
        payload = request_json(
            A_SHARE_CALENDAR_ENDPOINT,
            {
                "reportName": "RPT_STOCKCALENDAR",
                "columns": (
                    "SECUCODE,SECURITY_CODE,SECURITY_INNER_CODE,ORG_CODE,NOTICE_DATE,"
                    "INFO_CODE,EVENT_TYPE,EVENT_TYPE_CODE,LEVEL1_CONTENT"
                ),
                "quoteColumns": "",
                "filter": f'(SECURITY_CODE="{stock["code"]}")(EVENT_TYPE_CODE in ("006"))',
                "pageNumber": page_number,
                "pageSize": 100,
                "sortTypes": 1,
                "sortColumns": "NOTICE_DATE",
                "source": "QuoteWeb",
                "client": "WEB",
            },
        )

        result = (payload or {}).get("result") or {}
        data = result.get("data") or []
        if not data:
            break

        rows.extend(data)
        pages = int(result.get("pages") or 1)
        if page_number >= pages:
            break

        page_number += 1

    today = date.today()
    horizon = today + timedelta(days=365)
    events: list[dict[str, Any]] = []

    for row in rows:
        notice_date = parse_date(row.get("NOTICE_DATE"))
        if not notice_date:
            continue
        if notice_date < today - timedelta(days=7) or notice_date > horizon:
            continue

        level1 = str(row.get("LEVEL1_CONTENT") or "财报预约披露日").strip()
        hash_part = hashlib.md5(level1.encode("utf-8")).hexdigest()[:8]
        event_id = f"stock:{stock['code']}:appointment:{notice_date.isoformat()}:{hash_part}"

        events.append(
            {
                "id": event_id,
                "category": "stock",
                "title": f"{stock['name']} · {level1}",
                "start": notice_date.isoformat(),
                "allDay": True,
                "market": stock["market"],
                "stockCode": stock["code"],
                "eventType": "财报预约披露日",
                "description": level1,
                "sourceUrl": f"https://data.eastmoney.com/bbsj/{stock['code']}.html",
                "sourceLabel": "东方财富业绩日历",
            }
        )

    return events


def classify_macro_event(title: str) -> str | None:
    for name, pattern in MACRO_PATTERNS:
        if pattern.search(title):
            return name
    return None


def classify_macro_market(text: str) -> str | None:
    for market, pattern in MACRO_MARKET_PATTERNS:
        if pattern.search(text):
            return market
    return None


def parse_macro_start(show_time: str) -> str | None:
    show_time = show_time.strip()
    if not show_time:
        return None
    try:
        return datetime.strptime(show_time, "%Y-%m-%d %H:%M:%S").isoformat(timespec="minutes")
    except ValueError:
        day = parse_date(show_time)
        return day.isoformat() if day else None


def is_macro_release_text(text: str) -> bool:
    # Filter out commentary-style headlines and keep release-like messages.
    if re.search(r"[？?]", text):
        return False
    if re.search(r"(前瞻|解读|点评|缘何|驳斥|观察|速览|展望|专访|怎么看|重磅来袭|心跳时刻)", text):
        return False
    if re.search(r"\d", text) and re.search(r"(%|万人|万|亿|点|同比|环比|指数|初值|终值|前值|预期)", text):
        return True
    if re.search(r"(国家统计局|美国劳工部|ADP|中国人民银行|香港特区政府统计处|FHFA|Case-Shiller)\s*[：:]", text):
        return True
    return bool(MACRO_RELEASE_HINT_PATTERN.search(text))


def add_months(year: int, month: int, offset: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + offset
    return total // 12, total % 12 + 1


def iter_months(start_day: date, months: int) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for offset in range(months):
        result.append(add_months(start_day.year, start_day.month, offset))
    return result


def adjust_business_day(day: date, forward: bool = True) -> date:
    step = 1 if forward else -1
    result = day
    while result.weekday() >= 5:
        result += timedelta(days=step)
    return result


def first_business_day(year: int, month: int) -> date:
    return adjust_business_day(date(year, month, 1), forward=True)


def last_business_day(year: int, month: int) -> date:
    next_year, next_month = add_months(year, month, 1)
    last = date(next_year, next_month, 1) - timedelta(days=1)
    return adjust_business_day(last, forward=False)


def nth_weekday_of_month(year: int, month: int, weekday: int, nth: int) -> date:
    first = date(year, month, 1)
    shift = (weekday - first.weekday()) % 7
    return first + timedelta(days=shift + 7 * (nth - 1))


def first_weekday_of_month(year: int, month: int, weekday: int) -> date:
    return nth_weekday_of_month(year, month, weekday, 1)


def in_horizon(day: date, start_day: date, horizon_day: date) -> bool:
    return start_day <= day <= horizon_day


def make_macro_forecast_event(
    *,
    market: str,
    event_type: str,
    title: str,
    start_day: date,
    description: str,
    source_url: str,
    source_label: str,
) -> dict[str, Any]:
    hash_part = hashlib.md5(f"{market}|{event_type}|{title}|{start_day.isoformat()}".encode("utf-8")).hexdigest()[:10]
    return {
        "id": f"macrof:{market}:{event_type}:{start_day.isoformat()}:{hash_part}",
        "category": "macro",
        "title": title,
        "start": start_day.isoformat(),
        "allDay": True,
        "market": market,
        "stockCode": "",
        "eventType": event_type,
        "description": description,
        "sourceUrl": source_url,
        "sourceLabel": source_label,
        "isForecast": True,
    }


def build_macro_forecast_events(months_ahead: int = 12) -> list[dict[str, Any]]:
    today = date.today()
    horizon = today + timedelta(days=months_ahead * 32)
    events: list[dict[str, Any]] = []

    def add_event(
        *,
        day: date,
        market: str,
        event_type: str,
        title: str,
        description: str,
        source_url: str,
        source_label: str,
    ) -> None:
        if not in_horizon(day, today, horizon):
            return
        events.append(
            make_macro_forecast_event(
                market=market,
                event_type=event_type,
                title=title,
                start_day=day,
                description=description,
                source_url=source_url,
                source_label=source_label,
            )
        )

    months = iter_months(today, months_ahead + 2)

    # A股：按历史节奏给出预计窗口
    for year, month in months:
        add_event(
            day=adjust_business_day(date(year, month, 10), forward=True),
            market="A股",
            event_type="CPI",
            title="A股 · 中国CPI月度数据（预计）",
            description="预计发布窗口，具体发布时间以国家统计局公告为准。",
            source_url="https://www.stats.gov.cn/sj/zxfb/",
            source_label="国家统计局发布日历",
        )
        add_event(
            day=last_business_day(year, month),
            market="A股",
            event_type="PMI",
            title="A股 · 中国官方制造业PMI（预计）",
            description="通常在月末发布，遇节假日可能顺延，具体以国家统计局公告为准。",
            source_url="https://www.stats.gov.cn/sj/zxfb/",
            source_label="国家统计局发布日历",
        )
        add_event(
            day=adjust_business_day(date(year, month, 15), forward=True),
            market="A股",
            event_type="就业率/就业数据",
            title="A股 · 中国城镇调查失业率（月度）（预计）",
            description="通常随国民经济月度数据发布，具体以国家统计局公告为准。",
            source_url="https://www.stats.gov.cn/sj/zxfb/",
            source_label="国家统计局发布日历",
        )
        add_event(
            day=adjust_business_day(date(year, month, 16), forward=True),
            market="A股",
            event_type="住宅价格",
            title="A股 · 70城住宅销售价格月度报告（预计）",
            description="预计发布窗口，具体发布时间以国家统计局公告为准。",
            source_url="https://www.stats.gov.cn/sj/zxfb/",
            source_label="国家统计局发布日历",
        )

        if month in {1, 4, 7, 10}:
            add_event(
                day=adjust_business_day(date(year, month, 15), forward=True),
                market="A股",
                event_type="GDP",
                title="A股 · 中国季度GDP数据（预计）",
                description="通常在季度后次月中旬发布，具体以国家统计局公告为准。",
                source_url="https://www.stats.gov.cn/sj/zxfb/",
                source_label="国家统计局发布日历",
            )

    # 港股：香港统计口径相关的预计发布时间窗口
    for year, month in months:
        add_event(
            day=first_business_day(year, month),
            market="港股",
            event_type="PMI",
            title="港股 · 香港PMI（月度，预计）",
            description="预计发布窗口，具体时间以发布机构公告为准。",
            source_url="https://www.spglobal.com/marketintelligence/en/mi/research-analysis/pmi.html",
            source_label="S&P Global PMI 日历",
        )
        add_event(
            day=adjust_business_day(date(year, month, 22), forward=True),
            market="港股",
            event_type="CPI",
            title="港股 · 香港CPI月度数据（预计）",
            description="预计发布窗口，具体发布时间以香港政府统计处公告为准。",
            source_url="https://www.censtatd.gov.hk/en/press_release_list.html",
            source_label="香港政府统计处发布日历",
        )
        add_event(
            day=adjust_business_day(date(year, month, 18), forward=True),
            market="港股",
            event_type="就业率/就业数据",
            title="港股 · 香港失业率（月度）（预计）",
            description="预计发布窗口，具体发布时间以香港政府统计处公告为准。",
            source_url="https://www.censtatd.gov.hk/en/press_release_list.html",
            source_label="香港政府统计处发布日历",
        )
        add_event(
            day=adjust_business_day(date(year, month, 28), forward=True),
            market="港股",
            event_type="住宅价格",
            title="港股 · 香港私人住宅售价指数（月度）（预计）",
            description="预计发布窗口，具体发布时间以差饷物业估价署公告为准。",
            source_url="https://www.rvd.gov.hk/en/property_market_statistics/index.html",
            source_label="香港差饷物业估价署",
        )

        if month in {2, 5, 8, 11}:
            add_event(
                day=first_business_day(year, month),
                market="港股",
                event_type="GDP",
                title="港股 · 香港季度GDP数据（预计）",
                description="通常在季度结束后一个月左右发布，具体以香港政府统计处公告为准。",
                source_url="https://www.censtatd.gov.hk/en/press_release_list.html",
                source_label="香港政府统计处发布日历",
            )

    # 美股：按公开发布节奏生成未来窗口
    for year, month in months:
        add_event(
            day=adjust_business_day(date(year, month, 12), forward=True),
            market="美股",
            event_type="CPI",
            title="美股 · 美国CPI月度数据（预计）",
            description="预计发布时间窗口，具体以美国劳工统计局（BLS）日历为准。",
            source_url="https://www.bls.gov/schedule/news_release/cpi.htm",
            source_label="BLS 发布日历",
        )
        add_event(
            day=first_business_day(year, month),
            market="美股",
            event_type="PMI",
            title="美股 · ISM制造业PMI（月度，预计）",
            description="通常在每月首个工作日发布，具体以ISM公告为准。",
            source_url="https://www.ismworld.org/supply-management-news-and-reports/reports/ism-report-on-business/",
            source_label="ISM 发布日历",
        )
        add_event(
            day=first_weekday_of_month(year, month, 4),
            market="美股",
            event_type="就业率/就业数据",
            title="美股 · 美国非农就业/失业率（月度，预计）",
            description="通常在每月首个周五发布，具体以BLS公告为准。",
            source_url="https://www.bls.gov/schedule/news_release/empsit.htm",
            source_label="BLS 发布日历",
        )
        add_event(
            day=nth_weekday_of_month(year, month, 1, 4),
            market="美股",
            event_type="住宅价格",
            title="美股 · 美国住宅价格指数（月度，预计）",
            description="预计发布窗口，具体时间以FHFA/S&P相关机构公告为准。",
            source_url="https://www.fhfa.gov/DataTools/Downloads/Pages/House-Price-Index-Datasets.aspx",
            source_label="FHFA 指数发布页",
        )

        if month in {1, 4, 7, 10}:
            add_event(
                day=last_business_day(year, month),
                market="美股",
                event_type="GDP",
                title="美股 · 美国季度GDP（预估值，预计）",
                description="通常在季度结束后下月末发布，具体以BEA发布日历为准。",
                source_url="https://www.bea.gov/news/schedule",
                source_label="BEA 发布日历",
            )

    return dedupe_and_sort(events)


def fetch_macro_fastnews_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    sort_end = ""
    oldest_allowed = date.today() - timedelta(days=120)

    for page in range(MACRO_MAX_PAGES):
        payload = request_json(
            MACRO_FASTNEWS_ENDPOINT,
            {
                "client": "web",
                "biz": "web_724",
                "fastColumn": MACRO_FAST_COLUMNS,
                "sortEnd": sort_end,
                "pageSize": 100,
                "req_trace": f"macro_{page}_{int(datetime.utcnow().timestamp())}",
            },
        )

        data = (payload or {}).get("data") or {}
        items = data.get("fastNewsList") or []
        if not items:
            break

        for item in items:
            title = str(item.get("title") or "").strip()
            summary = str(item.get("summary") or "").strip()
            text = f"{title} {summary}".strip()
            if not text:
                continue

            market = classify_macro_market(text)
            if not market:
                continue

            event_type = classify_macro_event(text)
            if not event_type:
                continue
            if not is_macro_release_text(text):
                continue

            start = parse_macro_start(str(item.get("showTime") or ""))
            if not start:
                continue
            event_day = parse_date(start)
            if event_day and event_day < oldest_allowed:
                continue

            code = str(item.get("code") or "").strip()
            if not code:
                continue

            events.append(
                {
                    "id": f"macro:{code}",
                    "category": "macro",
                    "title": f"{market} · {title}",
                    "start": start,
                    "allDay": "T" not in start,
                    "market": market,
                    "stockCode": "",
                    "eventType": event_type,
                    "description": summary or title,
                    "sourceUrl": f"https://finance.eastmoney.com/a/{code}.html",
                    "sourceLabel": "东方财富快讯",
                }
            )

        sort_end = str(data.get("sortEnd") or "").strip()
        if not sort_end:
            break
        last_day = parse_date(items[-1].get("showTime"))
        if last_day and last_day < oldest_allowed:
            break

    return events[:80]


def fetch_macro_events() -> list[dict[str, Any]]:
    recent = fetch_macro_fastnews_events()
    forecast = build_macro_forecast_events(months_ahead=12)
    return dedupe_and_sort(recent + forecast)


def dedupe_and_sort(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for event in events:
        deduped[event["id"]] = event

    def sort_key(item: dict[str, Any]) -> tuple[str, str]:
        return str(item.get("start", "")), str(item.get("title", ""))

    return sorted(deduped.values(), key=sort_key)


def load_cache() -> dict[str, Any]:
    if not CACHE_FILE.exists():
        return {"updatedAt": None, "events": []}

    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("Failed to read cache file")
        return {"updatedAt": None, "events": []}


def save_cache(payload: dict[str, Any]) -> None:
    temp_file = CACHE_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_file.replace(CACHE_FILE)


def collect_stock_events() -> list[dict[str, Any]]:
    stocks = load_stocks()
    collected: list[dict[str, Any]] = []

    for stock in stocks:
        announcements = fetch_announcements(stock)
        collected.extend(filter_stock_report_events(stock, announcements))
        collected.extend(fetch_a_share_appointments(stock))

    return collected


def refresh_cache() -> dict[str, Any]:
    with CACHE_LOCK:
        stock_events = collect_stock_events()
        macro_events = fetch_macro_events()
        events = dedupe_and_sort(stock_events + macro_events)

        payload = {
            "updatedAt": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "events": events,
            "stats": {
                "stockEventCount": len(stock_events),
                "macroEventCount": len(macro_events),
                "total": len(events),
            },
        }

        save_cache(payload)
        logging.info(
            "Cache refreshed: total=%s stock=%s macro=%s",
            payload["stats"]["total"],
            payload["stats"]["stockEventCount"],
            payload["stats"]["macroEventCount"],
        )
        return payload


def ensure_cache(max_age_hours: int = 6) -> None:
    cache = load_cache()
    updated_at = cache.get("updatedAt")

    if not updated_at:
        refresh_cache()
        return

    try:
        last = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        refresh_cache()
        return

    age = datetime.utcnow() - last.replace(tzinfo=None)
    if age > timedelta(hours=max_age_hours):
        refresh_cache()


def filter_events_by_range(
    events: list[dict[str, Any]],
    start_text: str | None,
    end_text: str | None,
) -> list[dict[str, Any]]:
    start_date = parse_date(start_text) if start_text else None
    end_date = parse_date(end_text) if end_text else None

    if not start_date and not end_date:
        return events

    filtered = []
    for event in events:
        event_day = parse_date(event.get("start"))
        if not event_day:
            continue
        if start_date and event_day < start_date:
            continue
        if end_date and event_day > end_date:
            continue
        filtered.append(event)

    return filtered


app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")


@app.route("/")
def index() -> Any:
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/events")
def api_events() -> Any:
    cache = load_cache()
    events = cache.get("events") or []

    start = request.args.get("start")
    end = request.args.get("end")
    events = filter_events_by_range(events, start, end)

    return jsonify(
        {
            "updatedAt": cache.get("updatedAt"),
            "count": len(events),
            "events": events,
        }
    )


@app.route("/api/status")
def api_status() -> Any:
    cache = load_cache()
    events = cache.get("events") or []
    stats = cache.get("stats") or {}

    return jsonify(
        {
            "updatedAt": cache.get("updatedAt"),
            "eventCount": len(events),
            "stats": stats,
            "stocks": load_stocks(),
        }
    )


@app.route("/api/stocks/resolve")
def api_stocks_resolve() -> Any:
    query = str(request.args.get("q") or "").strip()
    group = str(request.args.get("group") or "").strip().upper()

    if not query:
        return jsonify({"ok": False, "error": "q 不能为空"}), 400
    if group and group not in {"A", "HK", "US"}:
        return jsonify({"ok": False, "error": "group 仅支持 A/HK/US"}), 400

    stock = resolve_stock_by_query(query, group or None)
    if not stock:
        return jsonify({"ok": False, "error": "未找到可匹配股票"}), 404

    return jsonify({"ok": True, "stock": stock})


@app.route("/api/stocks", methods=["GET", "POST"])
def api_stocks() -> Any:
    if request.method == "GET":
        return jsonify({"stocks": load_stocks()})

    body = request.get_json(silent=True) or {}
    stocks_input = body.get("stocks")
    if not isinstance(stocks_input, list):
        return jsonify({"ok": False, "error": "stocks 必须是数组"}), 400

    try:
        stocks = save_stocks_config(stocks_input)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception:
        logging.exception("Failed to save stocks config")
        return jsonify({"ok": False, "error": "保存股票配置失败"}), 500

    payload = refresh_cache()
    return jsonify(
        {
            "ok": True,
            "stocks": stocks,
            "updatedAt": payload.get("updatedAt"),
            "stats": payload.get("stats"),
        }
    )


@app.route("/api/refresh", methods=["POST"])
def api_refresh() -> Any:
    payload = refresh_cache()
    return jsonify(
        {
            "ok": True,
            "updatedAt": payload.get("updatedAt"),
            "stats": payload.get("stats"),
        }
    )


scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(
    refresh_cache,
    trigger=IntervalTrigger(hours=6),
    id="refresh_events_cache",
    replace_existing=True,
)
scheduler.start()
atexit.register(lambda: scheduler.shutdown(wait=False))


if __name__ == "__main__":
    ensure_cache()
    host = os.getenv("HOST", "localhost")
    port = int(os.getenv("PORT", "8000"))
    app.run(host=host, port=port, debug=False)
