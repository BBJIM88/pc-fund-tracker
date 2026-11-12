"""Completed U.S. equity closes and Taiwan central bank USD/TWD reference rates.

Dates requested by the ledger are Taipei valuation dates. Yahoo Finance's daily
Close is split-adjusted, even with dividend adjustment disabled; subsequent
split actions are needed to recover the per-share price for the old share count.
"""

from __future__ import annotations

import bisect
import logging
import re
import time
from datetime import date, datetime, time as wall_time, timedelta
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from functools import lru_cache
from html.parser import HTMLParser
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")
NEW_YORK = ZoneInfo("America/New_York")
DAY = timedelta(days=1)
FX_MAX_AGE = timedelta(days=10)
MAX_HISTORY_DAYS = 120
CBC_PAGE = "https://www.cbc.gov.tw/tw/lp-645-1-{}-60.html"
SOURCE = "Yahoo Finance 美股收盤價／中央銀行美元收盤匯率"
LOG = logging.getLogger(__name__)


def _now():
    return datetime.now(TAIPEI)


def _decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Invalid market value") from exc
    if not result.is_finite():
        raise ValueError("Nonfinite market value")
    return result


def _nth_weekday(year, month, weekday, n):
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _observed(day):
    return day - DAY if day.weekday() == 5 else day + DAY if day.weekday() == 6 else day


def _easter(year):
    # Gregorian computus, for the Good Friday exchange holiday.
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=40)
def _market_holidays(year):
    # Standard NYSE equity holidays, plus the 2025 national day of mourning.
    # https://www.nyse.com/trade/hours-calendars
    new_year = date(year, 1, 1)
    last_may = date(year, 5, 31)
    holidays = {
        new_year + DAY if new_year.weekday() == 6 else new_year,
        _nth_weekday(year, 2, 0, 3),
        _easter(year) - timedelta(days=2),
        last_may - timedelta(days=last_may.weekday()),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    if year >= 1998:
        holidays.add(_nth_weekday(year, 1, 0, 3))
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))
    if year == 2025:
        holidays.add(date(2025, 1, 9))
    return holidays


def _is_session(day):
    return day.weekday() < 5 and day not in _market_holidays(day.year)


def _stock_available(day):
    # A short delay after the regular 16:00 NY close keeps intraday daily bars
    # out of valuations. Waiting until this cutoff is conservative on half days.
    return datetime.combine(day, wall_time(16, 15), NEW_YORK).astimezone(TAIPEI)


def _last_session(cutoff):
    day = cutoff.astimezone(NEW_YORK).date()
    while not _is_session(day) or _stock_available(day) > cutoff:
        day -= DAY
    return day


def _stock_history(ticker, start, now):
    import yfinance as yf

    provider_day = now.astimezone(NEW_YORK).date()
    if (provider_day - start).days > 366 * 30:
        raise ValueError("Market history exceeds the supported split window")
    stock = yf.Ticker(ticker)
    frame = stock.history(start=start.isoformat(), end=(provider_day + DAY).isoformat(),
                          interval="1d", actions=True, auto_adjust=False, back_adjust=False,
                          repair=False, keepna=True, timeout=6, raise_errors=True)
    if frame.empty or not {"Close", "Stock Splits"} <= set(frame.columns):
        raise ValueError("Stock prices or split actions are unavailable")
    meta = stock.get_history_metadata()
    if (meta.get("currency") != "USD" or meta.get("instrumentType") not in {"EQUITY", "ETF"}
            or meta.get("exchangeTimezoneName") != "America/New_York"):
        raise ValueError("Not a supported USD equity or ETF")
    actions, closes = {}, {}
    for stamp, row in frame.iterrows():
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("Stock history lacks an exchange timezone")
        day = stamp.astimezone(NEW_YORK).date()
        if not start <= day <= provider_day or day in actions:
            raise ValueError("Unexpected date in stock history")
        ratio = _decimal(row["Stock Splits"])
        if ratio < 0:
            raise ValueError("Invalid split ratio")
        actions[day] = Fraction(ratio).limit_denominator(1_000_000) if ratio else Fraction(1)
        if not actions[day]:
            raise ValueError("Invalid split ratio")
        try:
            close = _decimal(row["Close"])
        except ValueError:
            continue
        if close > 0 and _is_session(day) and _stock_available(day) <= now:
            closes[day] = close

    last_session = _last_session(now)
    # Without the latest completed session, the provider response may be
    # truncated, and a later split may already be reflected in older prices.
    if last_session not in closes:
        raise ValueError("Stock history did not reach the latest completed session")

    ratio = Fraction(1)
    trustworthy = True
    result = {}
    for day in sorted(set(actions) | {start + i * DAY for i in range((last_session - start).days + 1)}, reverse=True):
        if _is_session(day) and day <= last_session and day not in actions:
            # A missing action row could conceal a split. Later quotes can be
            # used; earlier split reconstruction cannot be verified.
            trustworthy = False
        if trustworthy and day in closes:
            result[day] = closes[day] * Decimal(ratio.numerator) / Decimal(ratio.denominator)
        split = actions.get(day, Fraction(1))
        if split != 1:
            if day not in closes:
                # Around a split, wait until a completed post-split close so a
                # partially updated provider cannot corrupt a previous price.
                trustworthy = False
            else:
                previous = _last_session(_stock_available(day) - timedelta(minutes=1))
                if previous in closes and (split >= Fraction(3, 2) or split <= Fraction(2, 3)):
                    # Yahoo may fail to adjust on split day. A ratio nearer the
                    # announced split than one flags suspect pre-split data.
                    observed = Fraction(closes[previous]) / Fraction(closes[day])
                    if abs(observed - split) < abs(observed - 1):
                        trustworthy = False
            ratio *= split
    return result


class _CbcTable(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = self.cell = None
        self.last_page = 1

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            if match := re.search(r"(?:^|/)lp-645-1-([0-9]{1,3})-60\.html(?:$|[?#])", href):
                self.last_page = max(self.last_page, int(match[1]))

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.row is not None and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = self.cell = None


def _parse_fx_page(text):
    table = _CbcTable()
    table.feed(text)
    if not any(len(row) == 2 and "日期" in row[0] and row[1].replace(" ", "") == "NTD/USD"
               for row in table.rows):
        raise ValueError("Central bank rate table not found")
    rates = []
    for row in table.rows:
        if len(row) != 2 or not re.fullmatch(r"[0-9]{4}/[0-9]{2}/[0-9]{2}", row[0]):
            continue
        day = date.fromisoformat(row[0].replace("/", "-"))
        rate = _decimal(row[1])
        if not Decimal("1") < rate < Decimal("1000") or (rates and day >= rates[-1][0]):
            raise ValueError("Invalid central bank rate row")
        rates.append((day, rate))
    if not rates or len(rates) > 60:
        raise ValueError("Unexpected central bank rate page size")
    return tuple(rates), table.last_page


@lru_cache(maxsize=24)
def _fx_page(page, bucket):
    import requests

    response = requests.get(CBC_PAGE.format(page), timeout=(3, 5))
    response.raise_for_status()
    return _parse_fx_page(response.content.decode("utf-8-sig"))


def _fx_history(start, end):
    # The central bank's open-data archive is updated annually. Its public
    # latest-rate list is updated each business day and also has older pages.
    bucket = int(time.monotonic() // 300)
    first, last_page = _fx_page(1, bucket)
    page, rows = 1, first
    if end < first[-1][0]:
        low, high = 2, last_page
        while low <= high:
            mid = (low + high) // 2
            candidate, _ = _fx_page(mid, bucket)
            if end < candidate[-1][0]:
                low = mid + 1
            else:
                page, rows = mid, candidate
                high = mid - 1
        if page == 1:
            return {}
    result = dict(rows)
    for _ in range(5):
        if rows[-1][0] <= start or page >= last_page:
            break
        page += 1
        older, _ = _fx_page(page, bucket)
        gap = rows[-1][0] - older[0][0]
        if not DAY <= gap <= FX_MAX_AGE:
            raise ValueError("Central bank rate pages are inconsistent")
        rows = older
        result.update(rows)
    return result


def _fx_available(day):
    # CBC publishes the closing USD reference by 17:00 Taipei on business days.
    # https://www.cbc.gov.tw/tw/cp-2139-100102-5d889-1.html
    return datetime.combine(day, wall_time(17), TAIPEI)


def _combine(closes, rates, rate_days, cutoff):
    market_day = _last_session(cutoff)
    if market_day not in closes:
        return None
    ref_day = cutoff.date() - (DAY if cutoff.time() < wall_time(17) else timedelta(0))
    index = bisect.bisect_right(rate_days, ref_day) - 1
    if index < 0 or cutoff.date() - rate_days[index] > FX_MAX_AGE:
        return None
    fx_day = rate_days[index]
    usd, fx = closes[market_day], rates[fx_day]
    return {"price": usd * fx, "usd_price": usd, "fx_rate": fx,
            "at": _stock_available(market_day).isoformat(), "market_date": market_day.isoformat(),
            "fx_at": _fx_available(fx_day).isoformat(), "source": SOURCE}


def _symbol(ticker):
    ticker = str(ticker).strip().upper().replace(".", "-")
    if not re.fullmatch(r"[A-Z][A-Z0-9]{0,5}(?:-[A-Z]{1,2})?", ticker):
        raise ValueError("Invalid U.S. equity symbol")
    return ticker


def latest_quote(ticker, *, at=None):
    """Latest safely completed USD close multiplied by the available USD/TWD rate."""
    try:
        now = _now()
        if at is not None and (at.tzinfo is None or at.utcoffset() is None):
            raise ValueError("Valuation time must have a timezone")
        cutoff = now if at is None else at.astimezone(TAIPEI)
        if cutoff > now:
            return None
        day = cutoff.date()
        closes = _stock_history(_symbol(ticker), day - timedelta(days=10), now)
        rates = _fx_history(day - FX_MAX_AGE, day)
        quote = _combine(closes, rates, sorted(rates), cutoff)
        if quote is not None:
            quote["fetched_at"] = now.isoformat()
        return quote
    except Exception as exc:
        LOG.warning("U.S. price or USD/TWD rate unavailable (%s)", type(exc).__name__)
        return None


def load_history(ticker, start, end):
    """Map inclusive Taipei dates to completed, USD/TWD-converted stock closes."""
    empty = {"quotes": {}, "coverage": []}
    try:
        if type(start) is not date or type(end) is not date or not 0 <= (end - start).days < MAX_HISTORY_DAYS:
            raise ValueError("Invalid historical valuation range")
        now = _now()
        if end > now.date():
            raise ValueError("Cannot value a future Taipei date")
        closes = _stock_history(_symbol(ticker), start - timedelta(days=10), now)
        rates = _fx_history(start - FX_MAX_AGE, end)
        rate_days = sorted(rates)
        quotes, coverage = {}, []
        for offset in range((end - start).days + 1):
            day = start + offset * DAY
            cutoff = min(now, datetime.combine(day, wall_time.max, TAIPEI))
            quote = _combine(closes, rates, rate_days, cutoff)
            if quote is None:
                continue
            quotes[day.isoformat()] = quote
            if coverage and day == coverage[-1][1] + DAY:
                coverage[-1] = (coverage[-1][0], day)
            else:
                coverage.append((day, day))
        return {"quotes": quotes, "coverage": coverage}
    except Exception as exc:
        LOG.warning("U.S. price history or USD/TWD rate unavailable (%s)", type(exc).__name__)
        return empty
