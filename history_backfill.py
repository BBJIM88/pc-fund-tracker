"""Reconstruct missing daily valuations without overwriting observed snapshots."""
from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import date, datetime, timedelta

import app
from historical_ledger import history_start, reconstruct_states
from historical_prices import load_history

VERSION = 1
MAX_WINDOW_DAYS = 90
LOG = logging.getLogger(__name__)


def state_fingerprint(state):
    """Ignore journal revision changes which do not change this day's finances."""
    return app.historical_state_fingerprint(state)


def plan_backfill(ledger, rows, *, at=None, window_days=MAX_WINDOW_DAYS):
    if not 1 <= window_days <= MAX_WINDOW_DAYS:
        raise ValueError("Invalid historical window.")
    end = app.snapshot_datetime(at).date() - timedelta(days=1)
    start = history_start(ledger)
    result = {"states": {}, "remaining": 0, "start": start}
    if start is None or start > end:
        return result
    snapshots, _ = app.merged_snapshots(ledger, rows)
    existing = {date.fromisoformat(day): snapshot for day, snapshot in snapshots.items()
                if start <= date.fromisoformat(day) <= end}
    missing = [start + timedelta(days=offset) for offset in range((end - start).days + 1)
               if start + timedelta(days=offset) not in existing]
    # Only materialize missing days in one recent calendar window. Previously
    # reconstructed days are checked for corrections without requesting prices.
    recent_missing = ([day for day in missing if day >= missing[-1] - timedelta(days=window_days - 1)]
                      if missing else [])
    reconstructed = {day for day, snapshot in existing.items()
                     if snapshot.get("source") == "historical_backfill"}
    days = set(recent_missing) | reconstructed
    if not days:
        return result
    states = reconstruct_states(ledger, min(days), max(days), days=days)
    changed = {day for day in reconstructed if day in states and
               (existing[day].get("reconstruction_version") != VERSION or
                existing[day].get("state_fingerprint") != state_fingerprint(states[day]))}
    needed = set(missing) | changed
    if not needed:
        return result
    last = max(needed)
    first = last - timedelta(days=window_days - 1)
    selected = {day for day in needed if day >= first and day in states}
    result["states"] = {day: states[day] for day in sorted(selected)}
    result["remaining"] = len(needed - selected)
    return result


def covered_between(start, end, coverage):
    """A failed intervening month must not look like a market holiday."""
    cursor = start
    for first, last in sorted(coverage):
        if last < cursor:
            continue
        if first > cursor:
            return False
        cursor = max(cursor, last + timedelta(days=1))
        if cursor > end:
            return True
    return False


def quote_for_day(history, ticker, day, state):
    eligible = []
    for text, quote in history.get("quotes", {}).items():
        try:
            quoted_day = app.trade_day(quote["at"])
            if app.trade_day(text) != quoted_day or quoted_day > day or app.money(quote["price"]) <= 0:
                continue
            eligible.append((quoted_day, quote))
        except (ValueError, KeyError, TypeError):
            continue
    if not eligible:
        return None
    quoted_day, quote = max(eligible, key=lambda item: item[0])
    if not covered_between(quoted_day, day, history.get("coverage", [])):
        return None
    if any(item["ticker"] == ticker and quoted_day < app.trade_day(item["date"]) <= day
           for item in state["splits"]):
        return None
    return quote


def build_historical_snapshot(ledger, state, day, prices, *, recorded_at=None):
    if day >= app.snapshot_datetime(recorded_at).date():
        raise app.LedgerError("歷史補算只處理已結束的日期。")
    proxy = replace(ledger, state={**state, "target": state["target"] or app.money(0)})
    snapshot = app.build_daily_snapshot(proxy, prices, source="historical_backfill",
        recorded_at=datetime.combine(day, datetime.max.time(), tzinfo=app.TZ))
    snapshot["recorded_at"] = app.snapshot_datetime(recorded_at).isoformat(timespec="microseconds")
    snapshot["reconstruction_version"] = VERSION
    snapshot["state_fingerprint"] = state_fingerprint(state)
    if state["target"] is None:
        snapshot.pop("target_price", None)
    snapshot["checksum"] = app.digest({key: value for key, value in snapshot.items() if key != "checksum"})
    return snapshot


def backfill_missing_days(store, ledger, *, history_loader=None, at=None,
                          window_days=MAX_WINDOW_DAYS, budget_seconds=15, ticker_offset=0):
    """Re-read before writing, then reconcile all IDs even after a timeout."""
    history_loader = load_history if history_loader is None else history_loader
    captured = app.snapshot_datetime(at)
    rows = store.read_snapshots()
    plan = plan_backfill(ledger, rows, at=captured, window_days=window_days)
    states = plan["states"]
    result = {"rows": rows, "written": 0, "unavailable": 0, "remaining": plan["remaining"],
              "start": plan["start"], "next_offset": 0}
    if not states:
        return result
    required = {}
    for day, state in states.items():
        for ticker in app.holdings(state):
            required.setdefault(ticker, []).append(day)
    histories = {}
    started = time.monotonic()
    ordered = sorted(required.items())
    offset = int(ticker_offset) % len(ordered) if ordered else 0
    ordered = ordered[offset:] + ordered[:offset]
    attempted = 0
    for ticker, days in ordered:
        if time.monotonic() - started >= budget_seconds:
            break
        attempted += 1
        try:
            histories[ticker] = history_loader(ticker, min(days) - timedelta(days=14), max(days))
        except Exception as exc:
            LOG.warning("Historical prices unavailable (%s)", type(exc).__name__)
    if ordered:
        result["next_offset"] = (offset + attempted) % len(ordered)
    pending = []
    for day, state in states.items():
        prices = {ticker: quote_for_day(histories.get(ticker, {}), ticker, day, state)
                  for ticker in app.holdings(state)}
        if any(quote is None for quote in prices.values()):
            result["unavailable"] += 1
            continue
        pending.append(build_historical_snapshot(ledger, state, day, prices, recorded_at=captured))
    if not pending:
        return result
    if store.read().revision != ledger.revision:
        raise app.Conflict("帳本已更新，歷史補算暫停；請重新整理後再試。")
    rows = store.read_snapshots()
    effective, _ = app.merged_snapshots(ledger, rows)
    pending = [snapshot for snapshot in pending if snapshot["day"] not in effective or
               (effective[snapshot["day"]].get("source") == "historical_backfill" and
                (effective[snapshot["day"]].get("reconstruction_version") != VERSION or
                 effective[snapshot["day"]].get("state_fingerprint") != snapshot["state_fingerprint"]))]
    result["rows"] = rows
    if not pending:
        return result
    expected = {(snapshot["id"], snapshot["checksum"]) for snapshot in pending}
    try:
        store.append_snapshots(pending)
    except Exception:
        # The server may have committed some or all rows before the timeout.
        pass
    try:
        checked_rows = store.read_snapshots()
        confirmed = {(item.get("id"), item.get("checksum")) for item in app.parse_snapshots(checked_rows)[0]}
        if not expected <= confirmed:
            raise app.WriteUncertain("歷史補算保存結果尚未完整確認；請稍後重新整理查詢。")
    except app.WriteUncertain:
        raise
    except Exception as exc:
        raise app.WriteUncertain("歷史補算已送出，尚未確認保存結果；請稍後重新整理查詢。") from exc
    result["rows"] = checked_rows
    result["written"] = len(pending)
    return result
