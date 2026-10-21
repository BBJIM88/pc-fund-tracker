"""Historical as-traded Taiwan daily closes from the exchanges' monthly reports.

Yahoo's historical Close can be split-adjusted even with auto_adjust=False. These
official reports keep the price traded on each date, matching historical shares.
No credentials, Streamlit, or live ledger data are needed by this module.
"""
from __future__ import annotations

import calendar
import json
import logging
import re
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache

import requests


LOG = logging.getLogger("pc_fund_tracker")
REQUEST_TIMEOUT = (3, 5)
MAX_MONTHS = 12
MAX_RESPONSE_BYTES = 4_000_000
TAIPEI = timezone(timedelta(hours=8))
TICKER = re.compile(r"^([0-9A-Z]{4,8})\.(TW|TWO)$")


def _read_json(url):
    headers = {
        "User-Agent": "Mozilla/5.0 PC-Fund-Tracker/2.3",
        "Accept": "application/json",
    }
    # Requests uses standard certificate and hostname verification. In particular,
    # never disable verification to work around an exchange's certificate chain.
    with requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, stream=True) as response:
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ValueError("Historical response exceeded the size limit.")
            chunks.append(chunk)
    content = b"".join(chunks)
    payload = json.loads(content.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("Historical response must be an object.")
    return payload


def _report_date(value):
    parts = str(value).strip().split("/")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError("Invalid daily report date.")
    year, month, day = map(int, parts)
    return date(year + 1911 if year < 1911 else year, month, day)


def _close_price(value):
    if isinstance(value, bool):
        raise ValueError("Invalid daily close.")
    price = Decimal(str(value).replace(",", "").strip())
    if not price.is_finite() or price <= 0 or price > Decimal("1000000000000000"):
        raise ValueError("Invalid daily close.")
    price = price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if price <= 0:
        raise ValueError("Invalid rounded daily close.")
    return price


def _month_report(code, market, month):
    month_text = month.strftime("%Y%m%d")
    if market == "TW":
        query = urllib.parse.urlencode({"response": "json", "date": month_text, "stockNo": code})
        payload = _read_json("https://www.twse.com.tw/exchangeReport/STOCK_DAY?" + query)
        if (str(payload.get("stat", "")).lower() != "ok"
                or payload.get("date") != month_text
                or not re.search(r"(?:^|\s)" + re.escape(code) + r"(?:\s|$)", str(payload.get("title", "")))):
            raise ValueError("Historical month or symbol was not confirmed.")
        source = "臺灣證券交易所 原始日收盤價"
        report = payload
    else:
        query = urllib.parse.urlencode({"code": code, "date": month.strftime("%Y/%m/%d"), "response": "json"})
        payload = _read_json("https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?" + query)
        tables = payload.get("tables")
        if (str(payload.get("stat", "")).lower() != "ok"
                or payload.get("date") != month_text or str(payload.get("code")) != code
                or not isinstance(tables, list) or len(tables) != 1 or not isinstance(tables[0], dict)):
            raise ValueError("Historical month or symbol was not confirmed.")
        report = tables[0]
        if report.get("date") != month_text:
            raise ValueError("Historical table month was not confirmed.")
        source = "證券櫃檯買賣中心 原始日收盤價"
    fields, rows = report.get("fields"), report.get("data")
    if not isinstance(fields, list) or not isinstance(rows, list) or not rows:
        raise ValueError("Historical daily rows are missing.")
    fields = [re.sub(r"\s+", "", str(field)) for field in fields]
    day_column = fields.index("日期")
    close_column = next(i for i, field in enumerate(fields) if field in {"收盤價", "收盤"})
    if "total" in report and report["total"] != len(rows):
        raise ValueError("Historical rows are incomplete.")
    if "totalCount" in report and report["totalCount"] != len(rows):
        raise ValueError("Historical rows are incomplete.")
    quotes = {}
    for row in rows:
        if not isinstance(row, list):
            raise ValueError("Invalid historical daily row.")
        trading_day = _report_date(row[day_column])
        if trading_day.year != month.year or trading_day.month != month.month:
            raise ValueError("Historical row belongs to another month.")
        key = trading_day.isoformat()
        if key in quotes:
            raise ValueError("Duplicate historical daily row.")
        quotes[key] = {"price": _close_price(row[close_column]), "at": key, "source": source}
    return quotes


@lru_cache(maxsize=256)
def _cached_month_report(code, market, month, hour_bucket, calendar_day):
    """Cache only successful public reports; failures propagate and are not cached.

    The calendar-day key prevents a cached current-month report from treating a
    newly completed day as a holiday when the next request crosses midnight.
    """
    return _month_report(code, market, month)


def load_history(ticker, start, end):
    """Return closes and confirmed calendar coverage for an inclusive date range.

    Missing rows inside a successfully parsed monthly report may be non-trading
    dates. Failed/empty/malformed months have NO coverage, so callers must never
    carry an older quote across an unavailable month and call it a holiday.
    This function does not forward-fill prices or request any dates before start.
    Callers should include lookback days for the preceding valid close.
    """
    match = TICKER.fullmatch(str(ticker).strip().upper())
    if not match or type(start) is not date or type(end) is not date or start > end:
        raise ValueError("Invalid historical symbol or inclusive date range.")
    today = datetime.now(TAIPEI).date()
    if end > today:
        raise ValueError("Historical dates cannot be in the future.")
    months = (end.year - start.year) * 12 + end.month - start.month + 1
    if months > MAX_MONTHS:
        raise ValueError("Historical request exceeds the month limit.")
    code, market = match.groups()
    quotes, coverage = {}, []
    month = start.replace(day=1)
    for _ in range(months):
        last = month.replace(day=calendar.monthrange(month.year, month.month)[1])
        lower, upper = max(start, month), min(end, last)
        try:
            monthly = _cached_month_report(code, market, month, int(time.monotonic() // 3600), today)
            quotes.update({day: dict(quote) for day, quote in monthly.items()
                           if lower <= date.fromisoformat(day) <= upper})
            coverage.append((lower, upper))
        except Exception as exc:
            # Third-party payloads and exception messages may include raw details.
            LOG.warning("Historical quotes unavailable (%s)", type(exc).__name__)
        month = last + timedelta(days=1)
    return {"quotes": quotes, "coverage": coverage}
