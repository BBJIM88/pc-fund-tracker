"""PC Fund Tracker v2 — compatible with the original A1 + Events ledger.

Run: streamlit run app.py
The pure ledger functions below need only the Python standard library.
See README.md before replacing the old running application.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import logging
import math
import re
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

APP_VERSION = "2.0.0"
SHEET_NAME = "PC_Fund_Tracker"
EVENTS_NAME = "Events"
SNAPSHOTS_NAME = "Snapshots_v2"
TZ = ZoneInfo("Asia/Taipei")
CENT = Decimal("0.01")
TICKER_PATTERN = re.compile(r"^[0-9A-Z]{4,8}\.(TW|TWO)$")
BUSINESS_ACTIONS = {"deposit", "withdrawal", "buy", "sell", "dividend", "target", "split"}
LABELS = {"deposit": "存入", "withdrawal": "提款", "buy": "買入", "sell": "賣出",
          "dividend": "股息", "target": "目標", "split": "股數調整", "amend": "更正／撤銷",
          "delete": "舊版刪除", "baseline": "基準", "snapshot": "舊版快照"}
LOG = logging.getLogger("pc_fund_tracker")


class LedgerError(ValueError):
    """Invalid input or ledger; no silent fallback to an empty ledger."""


class WriteUncertain(RuntimeError):
    """Keep the exact pending event ID; do not generate a new operation."""


class Conflict(LedgerError):
    pass


def money(value):
    try:
        if isinstance(value, bool):
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or abs(result) > Decimal("1000000000000000"):
            raise ValueError()
        return result.quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LedgerError("金額必須是有效且有限的數字（絕對值上限 10^15）。") from exc


def integer(value, label="股數"):
    try:
        n = Decimal(str(value))
        if isinstance(value, bool) or not n.is_finite() or n != n.to_integral_value() or not 0 < n <= 10**12:
            raise ValueError()
        return int(n)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LedgerError(f"{label}必須是大於零的整數。") from exc


def ticker_text(value):
    ticker = str(value).strip().upper()
    if not TICKER_PATTERN.fullmatch(ticker):
        raise LedgerError("股票代號須為台灣市場代號，並包含 .TW 或 .TWO。")
    return ticker


def amount_text(value):
    return f"NT$ {money(value):,.2f}"


def now_text():
    return datetime.now(TZ).isoformat(timespec="microseconds")


def trade_day(value):
    try:
        text = str(value)
        # Old records may be a date or an ISO timestamp. Never compare raw strings.
        d = date.fromisoformat(text[:10])
        if len(text) > 10:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            d = dt.astimezone(TZ).date() if dt.tzinfo else dt.date()
        return d
    except (ValueError, TypeError) as exc:
        raise LedgerError("日期須為 YYYY-MM-DD 或有效 ISO 時間。") from exc


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def default_data():
    return {"cash_account": {"balance": 0, "history": []},
            "target_hardware": {"current_target": 0, "history": []},
            "transactions": [], "dividends": [], "daily_snapshots": {}}


def parse_baseline(raw):
    try:
        baseline = json.loads(raw) if raw else default_data()
        if not isinstance(baseline, dict):
            raise LedgerError("A1 必須是帳本物件。")
        if "cash_account" not in baseline or "target_hardware" not in baseline:
            raise LedgerError("A1 缺少 cash_account 或 target_hardware。")
        return baseline
    except (json.JSONDecodeError, TypeError) as exc:
        raise LedgerError("基準 A1 的 JSON 損壞；請保留原始資料並還原備份。") from exc


def parse_rows(rows, worksheet_name=EVENTS_NAME):
    result = []
    for row_number, row in enumerate(rows, 1):
        if not row or not row[0]:
            if any(cell for cell in row):
                raise LedgerError(f"{worksheet_name} 第 {row_number} 列：A 欄為空但其他欄有資料。")
            continue
        try:
            e = json.loads(row[0])
            if not isinstance(e, dict) or not isinstance(e.get("id"), str) or not e["id"] or not e.get("action"):
                raise ValueError()
            result.append(e)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise LedgerError(f"{worksheet_name} 第 {row_number} 列格式損壞，已停止寫入。") from exc
    return result


def baseline_records(baseline):
    records = {}
    for i, t in enumerate(baseline.get("transactions", [])):
        if t.get("type") != "buy":
            raise LedgerError("舊 A1 含非買入交易；此格式需另外遷移。")
        rid = f"legacy-buy-{i}"
        records[rid] = {**t, "id": rid, "action": "buy"}
    for key, items, action in (("deposit", baseline["cash_account"].get("history", []), "deposit"),
                               ("dividend", baseline.get("dividends", []), "dividend")):
        for i, item in enumerate(items):
            rid = f"legacy-{key}-{i}"
            records[rid] = {**item, "id": rid, "action": action}
    return records


def make_state(baseline, overrides=None):
    """A1's stored cash is authoritative; only correction deltas change it."""
    overrides = overrides or {}
    state = {"balance": money(baseline["cash_account"]["balance"]),
             "target": money(baseline["target_hardware"]["current_target"]),
             "buys": [], "sells": [], "deposits": [], "withdrawals": [], "dividends": [],
             "splits": [], "target_history": deepcopy(baseline["target_hardware"].get("history", [])),
             "snapshots": deepcopy(baseline.get("daily_snapshots", {}))}
    for rid, original in baseline_records(baseline).items():
        entry = overrides.get(rid, original)
        if original["action"] == "buy":
            old_cost = money(money(original["price"]) * integer(original["shares"]) + money(original["fee"]))
            new_cost = (money(money(entry["price"]) * integer(entry["shares"]) + money(entry["fee"]) +
                              money(entry.get("tax", 0)) + money(entry.get("other_fee", 0)))
                        if entry else Decimal("0.00"))
            state["balance"] += old_cost - new_cost
            if entry:
                shares = integer(entry["shares"])
                price, fee = money(entry["price"]), money(entry["fee"])
                if price <= 0 or min(fee, money(entry.get("tax", 0)), money(entry.get("other_fee", 0))) < 0:
                    raise LedgerError("舊買入的單價或費用無效。")
                state["buys"].append({**entry, "ticker": ticker_text(entry["ticker"]), "shares": shares,
                                      "remaining": shares, "price": price, "fee": fee,
                                      "remaining_cost": new_cost, "original_shares": shares})
        else:
            old_amount = money(original["amount"])
            new_amount = money(entry["amount"]) if entry else Decimal("0.00")
            state["balance"] += new_amount - old_amount
            if entry:
                if new_amount <= 0:
                    raise LedgerError("舊存款或股息金額必須大於零。")
                category = "deposits" if entry["action"] == "deposit" else "dividends"
                state[category].append({**entry, "amount": new_amount})
    if state["balance"] < 0 or state["target"] < 0:
        raise LedgerError("基準帳本或更正後的基準現金／目標為負數。")
    return state


def holdings(state):
    result = {}
    for lot in state["buys"]:
        if lot["remaining"]:
            result[lot["ticker"]] = result.get(lot["ticker"], 0) + lot["remaining"]
    return result


def apply_record(state, event, *, chronological_fifo=False):
    action, rid = event["action"], event["id"]
    day = event.get("date", "")
    note = event.get("note", "")
    if not isinstance(note, str) or len(note) > 500:
        raise LedgerError("備註最多 500 字。")
    if action in {"deposit", "withdrawal", "dividend", "target"}:
        value = money(event["amount"])
        if value <= 0:
            raise LedgerError("金額必須大於零。")
        if action in {"deposit", "dividend"}:
            money(state["balance"] + value)  # Validate the total before mutating.
        if action == "target":
            state["target"] = value
            state["target_history"].append({"id": rid, "date": day, "price": str(value), "note": note})
        else:
            if action == "withdrawal" and value > state["balance"]:
                raise LedgerError("提款超過該交易日期的現金餘額。")
            entry = {"id": rid, "action": action, "date": day, "amount": value, "note": note}
            if action == "dividend":
                entry["ticker"] = ticker_text(event["ticker"])
                # Cash receipt is independent of current holdings (e.g. after liquidation).
            state["balance"] += -value if action == "withdrawal" else value
            category = {"deposit": "deposits", "withdrawal": "withdrawals", "dividend": "dividends"}[action]
            state[category].append(entry)
    elif action in {"buy", "sell"}:
        shares, price, fee = integer(event["shares"]), money(event["price"]), money(event["fee"])
        tax, other = money(event.get("tax", 0)), money(event.get("other_fee", 0))
        ticker = ticker_text(event["ticker"])
        if price <= 0 or min(fee, tax, other) < 0:
            raise LedgerError("成交價必須大於零，費用不能為負數。")
        costs = fee + tax + other
        entry = {"id": rid, "action": action, "date": day, "ticker": ticker, "shares": shares,
                 "price": price, "fee": fee, "tax": tax, "other_fee": other, "note": note}
        if action == "buy":
            cost = money(price * shares + costs)
            if cost > state["balance"]:
                raise LedgerError("買入超過該交易日期的現金餘額。")
            state["balance"] -= cost
            state["buys"].append({**entry, "remaining": shares, "remaining_cost": cost,
                                  "original_shares": shares})
        else:
            proceeds = money(price * shares - costs)
            if proceeds < 0 or holdings(state).get(ticker, 0) < shares:
                raise LedgerError("賣出股數超過該日期庫存，或實收金額為負數。")
            money(state["balance"] + proceeds)
            lots = state["buys"]
            if chronological_fifo:
                lots = sorted(lots, key=lambda lot: trade_day(lot["date"]))
            remaining, basis, allocations = shares, Decimal("0.00"), []
            for lot in lots:
                if lot["ticker"] != ticker or not lot["remaining"]:
                    continue
                taken = min(remaining, lot["remaining"])
                cost = (lot["remaining_cost"] if taken == lot["remaining"] else
                        money(lot["remaining_cost"] * taken / lot["remaining"]))
                lot["remaining"] -= taken
                lot["remaining_cost"] -= cost
                basis += cost
                allocations.append({"buy_id": lot["id"], "shares": taken, "cost": str(cost)})
                remaining -= taken
                if not remaining:
                    break
            state["balance"] += proceeds
            state["sells"].append({**entry, "basis": basis, "proceeds": proceeds,
                                   "realized": proceeds - basis, "allocations": allocations})
    elif action == "split":
        ticker = ticker_text(event["ticker"])
        numerator, denominator = integer(event["numerator"], "調整後比例"), integer(event["denominator"], "調整前比例")
        if not holdings(state).get(ticker):
            raise LedgerError("該日期沒有可調整的股票庫存。")
        changes = []
        for lot in state["buys"]:
            if lot["ticker"] != ticker or not lot["remaining"]:
                continue
            new_shares, remainder = divmod(lot["remaining"] * numerator, denominator)
            if remainder or new_shares <= 0:
                raise LedgerError("本次調整會產生不足一股；請依券商明細另行處理，不能自動捨去。")
            integer(new_shares)
            changes.append((lot, new_shares))
        for lot, new_shares in changes:
            lot["remaining"] = new_shares  # Cost is conserved; original trade shares stay unchanged.
        state["splits"].append({**event})
    elif action == "delete":
        category, target = event["category"], event["target_id"]
        if category not in {"buys", "deposits", "dividends"}:
            raise LedgerError("舊版刪除類型無效。")
        entry = next((e for e in state[category] if e["id"] == target), None)
        if not entry:
            raise LedgerError("舊版刪除找不到目標紀錄。")
        if category == "buys":
            if entry["remaining"] != entry["shares"]:
                raise LedgerError("舊版刪除涉及已賣出的買入。")
            state["balance"] += entry["remaining_cost"]
        else:
            if entry["amount"] > state["balance"]:
                raise LedgerError("舊版刪除會造成現金不足。")
            state["balance"] -= entry["amount"]
        state[category].remove(entry)
    elif action == "snapshot":
        state["snapshots"][event["day"]] = {"total_assets": float(money(event["total_assets"])),
                                               "target_price": float(money(event["target_price"]))}
    else:
        raise LedgerError(f"無法辨識交易類型：{action}")


def validate_new_record(record):
    if record.get("action") not in BUSINESS_ACTIONS:
        raise LedgerError("不支援此操作。")
    d = trade_day(record.get("date", ""))
    if d > datetime.now(TZ).date():
        raise LedgerError("不能把尚未發生的未來交易記入實際帳本。")
    if not isinstance(record.get("note", ""), str) or len(record.get("note", "")) > 500:
        raise LedgerError("備註最多 500 字。")


def replay_effective(baseline, accepted):
    originals = baseline_records(baseline)
    overrides = {}
    legacy, modern = [], []
    for event in accepted:
        if event["action"] == "amend":
            target = event.get("target_id")
            if target not in originals:
                raise LedgerError("更正目標不存在或尚未入帳。")
            replacement = event.get("replacement")
            if replacement is not None:
                replacement = {**replacement, "id": target}
                if replacement["action"] != originals[target]["action"]:
                    raise LedgerError("更正不能改變交易類型。")
                validate_new_record(replacement)
            reason = event.get("reason", "")
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
                raise LedgerError("更正或撤銷必須填寫原因（最多 500 字）。")
            overrides[target] = replacement
        elif event["action"] != "baseline":
            if event["action"] in BUSINESS_ACTIONS:
                originals[event["id"]] = event
            (modern if event.get("schema_version") == 2 else legacy).append(event)
    cutoff_days = [trade_day(e["date"]) for e in baseline_records(baseline).values() if e.get("date")]
    cutoff_days.extend(trade_day(e["date"]) for e in legacy if e.get("date") and e["action"] != "snapshot")
    cutoff = max(cutoff_days, default=date.min)
    for rid, replacement in overrides.items():
        if replacement and originals[rid].get("schema_version") != 2 and trade_day(replacement["date"]) > cutoff:
            raise LedgerError("舊帳更正日期不得跨入新版期間；此情境需要專門的帳本遷移。")
    state = make_state(baseline, overrides)
    for original in legacy:
        if original["action"] == "snapshot":
            apply_record(state, original)
            continue
        effective = overrides.get(original["id"], original)
        if effective:
            apply_record(state, effective)
    active_modern = []
    for sequence, original in enumerate(modern):
        effective = overrides.get(original["id"], original)
        if effective:
            validate_new_record(effective)
            if trade_day(effective["date"]) < cutoff:
                raise LedgerError(f"新交易日期早於舊帳截止日 {cutoff}；請更正原紀錄，勿直接插入旧帳期間。")
            active_modern.append((trade_day(effective["date"]), sequence, effective))
    for _, _, effective in sorted(active_modern, key=lambda x: (x[0], x[1])):
        apply_record(state, effective, chronological_fifo=True)
    # Old delete events remain authoritative and cannot be casually undone by restoring an absent record.
    deleted = {e["target_id"] for e in legacy if e["action"] == "delete"}
    records = []
    for rid, original in originals.items():
        effective = overrides.get(rid, original)
        records.append({"id": rid, "original": original, "effective": effective,
                        "status": "舊版已刪除" if rid in deleted else "已撤銷" if effective is None else
                                  "已更正" if rid in overrides else "有效"})
    return state, records, cutoff


@dataclass
class Ledger:
    raw_baseline: str
    baseline: dict
    events: list
    accepted: list
    state: dict
    records: list
    statuses: dict
    revision: str
    cutoff: date
    warnings: list
    source_spreadsheet_id: str | None = None
    baseline_worksheet_id: int | None = None


def load_bundle(raw_baseline, events):
    baseline = parse_baseline(raw_baseline)
    baseline_hash = text_hash(raw_baseline)
    revision = digest({"baseline_hash": baseline_hash, "protocol": 2})
    seen, statuses, accepted, warnings = {}, {}, [], []
    state, records, cutoff = replay_effective(baseline, [])
    record_index = {r["id"]: r for r in records}
    last_modern_day = date.min
    modern_seen = False
    for row_number, event in enumerate(events, 1):
        rid = event.get("id")
        if not isinstance(rid, str) or not rid:
            raise LedgerError(f"Events 第 {row_number} 筆缺少有效 ID。")
        fingerprint = digest(event)
        if rid in seen:
            if seen[rid] != fingerprint:
                raise LedgerError(f"Events 第 {row_number} 筆：相同 ID 的內容不同，已停止寫入。")
            continue
        seen[rid] = fingerprint
        action = event.get("action")
        if action == "baseline":
            if event.get("baseline_hash") != baseline_hash:
                raise LedgerError("A1 已被更動，與 Events 的基準不符；請停止舊版並還原原始 A1。")
            statuses[rid] = {"status": "基準", "reason": ""}
            continue
        if rid.startswith("legacy-"):
            raise LedgerError("事件 ID 不得使用舊 A1 紀錄的保留前綴 legacy-。")
        version = event.get("schema_version")
        if version not in (None, 1, 2):
            raise LedgerError(f"Events 第 {row_number} 筆格式版本不受支援。")
        if version == 2:
            modern_seen = True
            check = {k: v for k, v in event.items() if k != "event_hash"}
            if event.get("event_hash") != digest(check):
                raise LedgerError(f"Events 第 {row_number} 筆內容校驗失敗，請檢查是否被手動改動。")
            if event.get("expected_revision") != revision:
                statuses[rid] = {"status": "衝突未入帳", "reason": "提交時的帳本版本已過期。"}
                continue
        elif modern_seen:
            raise LedgerError("新版啟用後仍出現舊版寫入；請立即停止所有舊版程式。")
        try:
            # Normal chronological appends can be applied in one pass. Only
            # amendments and backdated records need a complete historical replay.
            # apply_record validates before mutating each operation.
            fast = action != "amend" and (version != 2 or action in BUSINESS_ACTIONS)
            event_day = trade_day(event["date"]) if event.get("date") else None
            if version == 2 and action in BUSINESS_ACTIONS:
                validate_new_record(event)
                if event_day < cutoff:
                    raise LedgerError(f"新交易日期早於舊帳截止日 {cutoff}；請更正原紀錄。")
                fast = event_day >= last_modern_day
            if fast:
                apply_record(state, event, chronological_fifo=version == 2)
                if action in BUSINESS_ACTIONS:
                    item = {"id": rid, "original": event, "effective": event, "status": "有效"}
                    records.append(item)
                    record_index[rid] = item
                elif action == "delete" and event["target_id"] in record_index:
                    record_index[event["target_id"]]["status"] = "舊版已刪除"
                if version != 2 and event_day and action != "snapshot":
                    cutoff = max(cutoff, event_day)
                if version == 2 and event_day:
                    last_modern_day = max(last_modern_day, event_day)
            else:
                state, records, cutoff = replay_effective(baseline, accepted + [event])
                record_index = {r["id"]: r for r in records}
                last_modern_day = max((trade_day(r["effective"]["date"]) for r in records
                    if r["effective"] and r["original"].get("schema_version") == 2), default=date.min)
        except (LedgerError, KeyError, TypeError) as exc:
            if version != 2:
                raise LedgerError(f"Events 第 {row_number} 筆舊資料無法回放：{exc}") from exc
            statuses[rid] = {"status": "驗證失敗未入帳", "reason": str(exc)}
            continue
        accepted.append(event)
        statuses[rid] = {"status": "已入帳", "reason": ""}
        if action != "snapshot":
            revision = digest({"parent": revision, "event": event})
    dates = [trade_day(e["date"]) for e in baseline_records(baseline).values() if e["action"] == "buy"]
    if dates != sorted(dates):
        warnings.append("舊 A1 買入順序與日期不一致。舊賣出維持原算法；新賣出按交易日期分攤剩餘成本。")
    return Ledger(raw_baseline, baseline, events, accepted, state, records, statuses, revision, cutoff, warnings)


def create_event(ledger, action, *, operation_id=None, **fields):
    event = {**fields, "id": operation_id or str(uuid.uuid4()), "action": action,
             "date": fields.get("date", datetime.now(TZ).date().isoformat()),
             "recorded_at": now_text(), "schema_version": 2, "expected_revision": ledger.revision}
    if ledger.source_spreadsheet_id:
        event["source_spreadsheet_id"] = ledger.source_spreadsheet_id
    event["event_hash"] = digest(event)
    trial = load_bundle(ledger.raw_baseline, ledger.events + [event])
    result = trial.statuses[event["id"]]
    if result["status"] != "已入帳":
        raise LedgerError(result["reason"])
    return event, trial


def cumulative_principal(state):
    return money(sum((e["amount"] for e in state["deposits"]), Decimal("0.00")))


def net_principal(state):
    return cumulative_principal(state) - sum((e["amount"] for e in state["withdrawals"]), Decimal("0.00"))


def profit_parts(state, prices):
    realized = money(sum((e["realized"] for e in state["sells"]), Decimal("0.00")))
    dividends = money(sum((e["amount"] for e in state["dividends"]), Decimal("0.00")))
    complete = all(prices.get(t) is not None for t in holdings(state))
    unrealized = (money(sum((prices[lot["ticker"]]["price"] * lot["remaining"] - lot["remaining_cost"]
                            for lot in state["buys"] if lot["remaining"]), Decimal("0.00"))) if complete else None)
    return {"realized": realized, "dividends": dividends, "unrealized": unrealized,
            "total": money(realized + dividends + unrealized) if unrealized is not None else None}


def valuation(state, prices):
    if any(prices.get(t) is None for t in holdings(state)):
        return None, None
    market = money(sum((prices[t]["price"] * n for t, n in holdings(state).items()), Decimal("0.00")))
    return market, money(state["balance"] + market)


def backup_document(ledger, *, spreadsheet_id, baseline_worksheet_id, snapshot_rows=None):
    content = {"format": "pc-fund-tracker-backup", "version": 2, "app_version": APP_VERSION,
               "exported_at": now_text(), "source_spreadsheet_id": spreadsheet_id,
               "baseline_worksheet_id": baseline_worksheet_id,
               "raw_baseline": ledger.raw_baseline, "events": ledger.events,
               "snapshot_rows": snapshot_rows or [], "ledger_revision": ledger.revision}
    content["checksum"] = digest(content)
    return content


def validate_backup(content):
    if not isinstance(content, dict) or content.get("format") != "pc-fund-tracker-backup" or content.get("version") != 2:
        raise LedgerError("不是支援的 v2 完整備份。")
    if content.get("checksum") != digest({k: v for k, v in content.items() if k != "checksum"}):
        raise LedgerError("備份校驗失敗，檔案可能被更改或不完整。")
    ledger = load_bundle(content["raw_baseline"], content["events"])
    if ledger.revision != content.get("ledger_revision"):
        raise LedgerError("備份回放後的帳本版本不符。")
    return ledger


def records_csv(ledger):
    buffer = io.StringIO()
    fields = ["id", "狀態", "日期", "類型", "代號", "股數", "單價", "金額", "手續費", "交易稅", "其他費用", "備註"]
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    for item in ledger.records:
        e = item["effective"] or item["original"]
        row = {"id": item["id"], "狀態": item["status"], "日期": e.get("date", ""),
               "類型": LABELS.get(e["action"], e["action"]), "代號": e.get("ticker", ""),
               "股數": e.get("shares", ""), "單價": e.get("price", ""), "金額": e.get("amount", ""),
               "手續費": e.get("fee", ""), "交易稅": e.get("tax", ""),
               "其他費用": e.get("other_fee", ""), "備註": e.get("note", "")}
        # Spreadsheet formula injection protection for CSV users.
        for key, value in row.items():
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
                row[key] = "'" + value
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8-sig")


class SheetStore:
    """Sheets remains storage; row order + revision chain defines accepted writes.

    No claim that a read + append is a database transaction. Stale branches are
    retained as rejected audit records, not applied to the account. Every writer
    must use this protocol; manual edits/sorts and old writers are unsupported.
    """

    def __init__(self, spreadsheet, *, baseline_id=None, baseline_title=None, client=None):
        self.spreadsheet = spreadsheet
        self.baseline_id = baseline_id
        self.baseline_title = baseline_title
        self.client = client

    def worksheet(self, title, *, create=False):
        import gspread
        try:
            return self.spreadsheet.worksheet(title)
        except gspread.WorksheetNotFound:
            if not create:
                return None
            try:
                return self.spreadsheet.add_worksheet(title=title, rows=100, cols=1)
            except gspread.exceptions.APIError:
                return self.spreadsheet.worksheet(title)

    def read(self):
        ws = self.worksheet(EVENTS_NAME)
        rows = ws.get_all_values() if ws else []
        events = parse_rows(rows)
        markers = [e for e in events if e.get("action") == "baseline"]
        stored_ids = {int(e["baseline_worksheet_id"]) for e in markers if "baseline_worksheet_id" in e}
        configured_id = int(self.baseline_id) if self.baseline_id is not None else None
        if len(stored_ids) > 1 or (stored_ids and configured_id is not None and configured_id not in stored_ids):
            raise LedgerError("基準工作表 ID 與已保存的設定不符。")
        selected_id = configured_id if configured_id is not None else next(iter(stored_ids), None)
        if selected_id is not None:
            baseline_ws = self.spreadsheet.get_worksheet_by_id(selected_id)
        elif self.baseline_title:
            baseline_ws = self.spreadsheet.worksheet(self.baseline_title)
        else:
            # Resolve by saved content, not by tab ordering. Never assume an empty A1 means data was lost safely.
            sheets = [s for s in self.spreadsheet.worksheets() if s.title not in {EVENTS_NAME, SNAPSHOTS_NAME}]
            expected = {e["baseline_hash"] for e in markers}
            candidates = []
            for s in sheets:
                raw = s.acell("A1").value or ""
                if expected:
                    if text_hash(raw) in expected:
                        candidates.append((s, raw))
                else:
                    try:
                        parse_baseline(raw)
                    except LedgerError:
                        continue
                    candidates.append((s, raw))
            if len(candidates) != 1:
                raise LedgerError("無法唯一辨識舊 A1 工作表；請在 Secrets 設定 BASELINE_WORKSHEET_ID。")
            baseline_ws = candidates[0][0]
        if baseline_ws is None or baseline_ws.title in {EVENTS_NAME, SNAPSHOTS_NAME}:
            raise LedgerError("基準工作表不存在或指向事件工作表。")
        raw = baseline_ws.acell("A1").value or ""
        ledger = load_bundle(raw, events)
        self.baseline_id = baseline_ws.id
        ledger.source_spreadsheet_id = self.spreadsheet.id
        ledger.baseline_worksheet_id = baseline_ws.id
        return ledger

    def append(self, event):
        self.worksheet(EVENTS_NAME, create=True).append_row([canonical(event)], value_input_option="RAW",
                                                          insert_data_option="INSERT_ROWS")

    def ensure_marker(self, ledger):
        if any(e.get("action") == "baseline" and e.get("baseline_worksheet_id") == self.baseline_id
               for e in ledger.events):
            return
        marker = {"id": "baseline-v2-" + text_hash(ledger.raw_baseline), "action": "baseline",
                  "baseline_hash": text_hash(ledger.raw_baseline), "baseline_worksheet_id": self.baseline_id}
        self.append(marker)

    def read_snapshots(self):
        ws = self.worksheet(SNAPSHOTS_NAME)
        return ws.get_all_values() if ws else []

    def append_snapshot(self, snapshot):
        self.worksheet(SNAPSHOTS_NAME, create=True).append_row([canonical(snapshot)], value_input_option="RAW",
                                                             insert_data_option="INSERT_ROWS")


def write_pending(store, event):
    """Always reconcile before retrying. Append is never blindly retried."""
    source_id = event.get("source_spreadsheet_id")
    if source_id and source_id != store.spreadsheet.id:
        raise LedgerError("這份待確認操作來自另一份試算表；請切回原帳本後查詢。")
    try:
        current = store.read()
    except Exception as exc:
        raise WriteUncertain("無法讀取目前寫入狀態；請保留此操作 ID，稍後查詢。") from exc
    existing = next((e for e in current.events if e["id"] == event["id"]), None)
    if existing:
        if digest(existing) != digest(event):
            raise LedgerError("同一操作 ID 已存在但內容不同。")
        return current, current.statuses[event["id"]]
    if current.revision != event["expected_revision"]:
        return current, {"status": "衝突未入帳", "reason": "帳本已更新。請檢查最新餘額後重新預覽。"}
    trial = load_bundle(current.raw_baseline, current.events + [event])
    if trial.statuses[event["id"]]["status"] != "已入帳":
        return current, trial.statuses[event["id"]]
    try:
        store.ensure_marker(current)
        store.append(event)
    except Exception:
        # Even on a timeout the server may have appended. Verify instead of retrying.
        try:
            checked = store.read()
            if event["id"] in checked.statuses:
                return checked, checked.statuses[event["id"]]
        except Exception:
            pass
        raise WriteUncertain("寫入回覆不明，尚不能判定成功或失敗。請用同一操作 ID 查詢／重試。")
    try:
        checked = store.read()
        if event["id"] not in checked.statuses:
            raise WriteUncertain("已送出但尚未讀到該操作；請稍後查詢同一 ID。")
        return checked, checked.statuses[event["id"]]
    except WriteUncertain:
        raise
    except Exception as exc:
        raise WriteUncertain("已送出，但無法確認結果；請稍後查詢同一 ID。") from exc


# Process-wide throttling improves on session-only counters. Multi-replica public
# hosting should additionally restrict access with the platform's identity gate.
_AUTH_LOCK = threading.Lock()
_AUTH_FAILURES = {}


def auth_storage():
    # Streamlit reruns the main module. This factory is used through
    # cache_resource in sign_in(), so counters actually survive those reruns.
    return {"lock": threading.Lock(), "failures": {}}


def compare_password(entered, expected):
    return hmac.compare_digest(entered.encode("utf-8"), expected.encode("utf-8"))


def auth_wait(key, at=None, storage=None):
    at = time.time() if at is None else at
    lock = storage["lock"] if storage else _AUTH_LOCK
    failures = storage["failures"] if storage else _AUTH_FAILURES
    with lock:
        _, until = failures.get(key, (0, 0))
    return max(0, until - at)


def record_login(key, success, at=None, storage=None):
    at = time.time() if at is None else at
    lock = storage["lock"] if storage else _AUTH_LOCK
    failures = storage["failures"] if storage else _AUTH_FAILURES
    with lock:
        count, until = failures.get(key, (0, 0))
        if success:
            failures.pop(key, None)
        else:
            if until and until <= at:
                count = 0
            count += 1
            failures[key] = (count, at + 15 * 60 if count >= 5 else 0)


def twse_quote(ticker):
    """Official unadjusted closing price, preserving the actual trading date."""
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen

    if not TICKER_PATTERN.fullmatch(ticker) or not ticker.endswith(".TW"):
        return None
    today = datetime.now(TZ).date()
    month = today.replace(day=1)
    previous_month = (month - timedelta(days=1)).replace(day=1)
    for query_month in (month, previous_month):
        try:
            query = urlencode({"response": "json", "stockNo": ticker[:-3],
                               "date": query_month.strftime("%Y%m%d")})
            request = Request("https://www.twse.com.tw/exchangeReport/STOCK_DAY?" + query,
                              headers={"User-Agent": "PCFundTracker/2.0", "Accept": "application/json"})
            with urlopen(request, timeout=10) as response:
                payload = json.load(response)
            if payload.get("stat") != "OK":
                continue
            fields = payload.get("fields", [])
            day_index, close_index = fields.index("日期"), fields.index("收盤價")
            quotes = []
            for row in payload.get("data", []):
                try:
                    year, mo, day = map(int, str(row[day_index]).split("/"))
                    trading_day = date(year + 1911 if year < 1911 else year, mo, day)
                    price = money(str(row[close_index]).replace(",", "").strip())
                    if price > 0 and timedelta(0) <= today - trading_day <= timedelta(days=45):
                        quotes.append((trading_day, price))
                except (ValueError, TypeError, IndexError, InvalidOperation):
                    continue
            if quotes:
                trading_day, price = max(quotes, key=lambda q: q[0])
                return {"price": price, "at": trading_day.isoformat(), "fetched_at": now_text(),
                        "source": "臺灣證券交易所 日線最近一筆收盤價"}
        except Exception as exc:
            logging.getLogger(__name__).warning("TWSE quote unavailable for %s (%s)",
                                                ticker, type(exc).__name__)
    return None


def quote_uncached(ticker):
    try:
        import yfinance as yf
        history = yf.Ticker(ticker).history(period="5d", auto_adjust=False, timeout=10)
        if not history.empty:
            p = float(history["Close"].iloc[-1])
            if math.isfinite(p) and p > 0:
                return {"price": money(p), "at": history.index[-1].isoformat(), "fetched_at": now_text(),
                        "source": "Yahoo Finance 日線最近一筆收盤價"}
        logging.getLogger(__name__).warning("Yahoo quote empty or invalid for %s", ticker)
    except Exception as exc:
        logging.getLogger(__name__).warning("Yahoo quote unavailable for %s (%s)",
                                            ticker, type(exc).__name__)
    return twse_quote(ticker)


def sign_in(st):
    password = st.secrets.get("APP_PASSWORD")
    if not isinstance(password, str) or len(password) < 12:
        st.error("請在 Secrets 設定至少 12 字元的 APP_PASSWORD。")
        st.stop()
    signature = text_hash(password)
    now = time.time()
    idle_minutes = max(1, int(st.secrets.get("AUTH_IDLE_MINUTES", 30)))
    if st.session_state.get("auth_signature") == signature:
        if now - st.session_state.get("last_active", now) < idle_minutes * 60:
            st.session_state["last_active"] = now
            return
        st.warning("閒置時間已超過設定，請重新登入。")
    st.session_state.pop("auth_signature", None)
    st.title("🔒 PC Fund Tracker")
    storage = st.cache_resource(show_spinner=False)(auth_storage)()
    wait = auth_wait(signature, storage=storage)
    if wait:
        st.error(f"登入嘗試過多，請約 {math.ceil(wait / 60)} 分鐘後再試。")
        st.stop()
    with st.form("login", clear_on_submit=True):
        entered = st.text_input("密碼", type="password")
        submitted = st.form_submit_button("登入")
    if submitted:
        ok = compare_password(entered, password)
        record_login(signature, ok, storage=storage)
        if ok:
            st.session_state["auth_signature"] = signature
            st.session_state["last_active"] = now
            st.rerun()
        st.error("密碼錯誤。")
    st.stop()


def connect_store(st):
    import gspread
    raw = st.secrets.get("GCP_KEY_JSON")
    if not raw:
        raise LedgerError("尚未設定 GCP_KEY_JSON。")
    credentials = json.loads(raw) if isinstance(raw, str) else dict(raw)
    client = gspread.service_account_from_dict(credentials,
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.readonly"])
    spreadsheet_id = st.secrets.get("SPREADSHEET_ID")
    spreadsheet = client.open_by_key(spreadsheet_id) if spreadsheet_id else client.open(SHEET_NAME)
    return SheetStore(spreadsheet, baseline_id=st.secrets.get("BASELINE_WORKSHEET_ID"),
                      baseline_title=st.secrets.get("BASELINE_WORKSHEET_NAME"), client=client)


def show_failure(st, exc, label="操作失敗"):
    if isinstance(exc, LedgerError):
        st.error(f"{label}：{exc}")
    else:
        reference = uuid.uuid4().hex[:8]
        LOG.error("%s reference=%s exception=%s", label, reference, type(exc).__name__)
        st.error(f"{label}（參考碼 {reference}）。請檢查連線與 Secrets／試算表權限；原資料不會被清空。")


def stage(st, ledger, action, **fields):
    if st.session_state.get("pending_event"):
        st.error("有尚未確認的操作，請先處理頁面上方的待確認紀錄。")
        return
    try:
        event, _ = create_event(ledger, action, **fields)
        st.session_state["pending_event"] = event
        st.session_state["pending_sent"] = False
        st.session_state.pop("pending_rejected", None)
        st.query_params["pending"] = event["id"]
        st.rerun()
    except LedgerError as exc:
        st.error(str(exc))


def render_pending(st, store, ledger):
    event = st.session_state.get("pending_event")
    if not event:
        pending_id = st.query_params.get("pending")
        if pending_id:
            result = ledger.statuses.get(pending_id)
            if result:
                st.info(f"上次操作 {pending_id}：{result['status']}。{result['reason']}")
                if st.button("已確認此操作結果", key="ack_recovered"):
                    del st.query_params["pending"]
                    st.rerun()
            else:
                st.warning(f"上次操作 {pending_id} 尚未在帳本中找到。請在資料管理匯入已保存的待確認操作檔案，或稍後重新查詢。")
                if st.button("重新查詢上次操作", key="query_recovered"):
                    st.rerun()
                st.caption("此狀態會暫停新增帳務。若伺服器已確認沒有未完成請求，可移除網址的 pending 參數後重新操作。")
            return True
        return False
    st.info(f"待確認：{LABELS.get(event['action'], event['action'])}，操作 ID：{event['id']}")
    try:
        trial = load_bundle(ledger.raw_baseline, ledger.events + [event])
        status = trial.statuses[event["id"]]
        if status["status"] == "已入帳" and event["id"] not in ledger.statuses:
            c1, c2 = st.columns(2)
            c1.metric("確認後現金", amount_text(trial.state["balance"]),
                      amount_text(trial.state["balance"] - ledger.state["balance"]))
            c2.write("確認後庫存")
            c2.json(holdings(trial.state))
        elif event["id"] in ledger.statuses:
            st.write("目前狀態：" + ledger.statuses[event["id"]]["status"])
        else:
            st.warning(status["reason"])
    except LedgerError as exc:
        st.error(str(exc))
    with st.expander("操作明細"):
        st.json({k: v for k, v in event.items() if k not in {"event_hash", "expected_revision"}})
    # Export the exact ID and payload, so a refresh or logout does not require guessing.
    st.download_button("保存待確認操作", canonical(event).encode("utf-8"),
                       file_name=f"pending_{event['id']}.json", mime="application/json")
    left, right = st.columns(2)
    if left.button("查詢／以同一 ID 確認寫入" if st.session_state.get("pending_sent") else "確認寫入",
                   key="write_pending", type="primary"):
        st.session_state["pending_sent"] = True
        try:
            _, result = write_pending(store, event)
            if result["status"] == "已入帳":
                st.session_state.pop("pending_event", None)
                st.session_state.pop("pending_sent", None)
                st.session_state.pop("pending_rejected", None)
                if "pending" in st.query_params:
                    del st.query_params["pending"]
                st.session_state["flash"] = f"已確認入帳，操作 ID：{event['id']}"
                st.rerun()
            st.error(result["status"] + "：" + result["reason"])
            # Confirmed rejection is safe to discard, unlike an unknown timeout.
            st.session_state["pending_rejected"] = True
        except WriteUncertain as exc:
            st.warning(str(exc))
        except Exception as exc:
            show_failure(st, exc, "寫入未能確認")
    can_cancel = not st.session_state.get("pending_sent") or st.session_state.get("pending_rejected")
    if can_cancel and right.button("取消此操作", key="cancel_pending"):
        for key in ("pending_event", "pending_sent", "pending_rejected"):
            st.session_state.pop(key, None)
        if "pending" in st.query_params:
            del st.query_params["pending"]
        st.rerun()
    elif not can_cancel:
        right.caption("結果尚未確認；請先查詢，避免重新登記。")
    return True


def dataframe(st, rows, *, money_columns=()):
    import pandas as pd
    if not rows:
        st.caption("目前沒有紀錄。")
        return
    configs = {c: st.column_config.NumberColumn(c, format="NT$ %.2f") for c in money_columns}
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config=configs)


def render_overview(st, ledger, prices, parts, market, assets):
    state = ledger.state
    c1, c2, c3 = st.columns(3)
    c1.metric("目前總資產", amount_text(assets) if assets is not None else "報價不完整")
    c2.metric("投資總損益", amount_text(parts["total"]) if parts["total"] is not None else "報價不完整")
    c3.metric("帳面現金", amount_text(state["balance"]))
    st.caption("帳面現金依已登記交易即時增減；未區分待交割款。市值使用最近一筆日線收盤價，非即時報價。")
    with st.expander("本金與損益組成", expanded=True):
        for columns, values in ((st.columns(3), [("已實現損益", parts["realized"]),
                               ("未實現損益", parts["unrealized"]), ("實收股息", parts["dividends"])]),
                               (st.columns(3), [("累計存入", cumulative_principal(state)),
                               ("累計提款", cumulative_principal(state) - net_principal(state)),
                               ("淨投入本金", net_principal(state))])):
            for col, (label, value) in zip(columns, values):
                col.metric(label, amount_text(value) if value is not None else "報價不完整")
    if state["target"] > 0 and assets is not None:
        gap = state["target"] - assets
        st.metric("距離硬體目標" if gap > 0 else "已超過硬體目標", amount_text(abs(gap)))
        st.progress(max(0.0, min(float(assets / state["target"]), 1.0)),
                    text=f"資產達成率 {assets / state['target']:.2%} · 目標 {amount_text(state['target'])}")
    rows = []
    for ticker, shares in holdings(state).items():
        basis = sum((lot["remaining_cost"] for lot in state["buys"] if lot["ticker"] == ticker), Decimal("0.00"))
        q = prices.get(ticker)
        value = money(q["price"] * shares) if q else None
        rows.append({"代號": ticker, "股數": shares, "剩餘成本": float(basis), "平均成本": float(basis / shares),
                     "參考收盤價": float(q["price"]) if q else None, "市值": float(value) if value is not None else None,
                     "未實現損益": float(value - basis) if value is not None else None,
                     "報價日期": trade_day(q["at"]).isoformat() if q else "無法取得",
                     "行情來源": q.get("source", "未標示") if q else "無法取得"})
    st.subheader("庫存彙總")
    dataframe(st, rows, money_columns=("剩餘成本", "平均成本", "參考收盤價", "市值", "未實現損益"))
    with st.expander("買入批次與 FIFO 成本"):
        dataframe(st, [{"ID": b["id"], "日期": b["date"], "代號": b["ticker"],
                        "原始成交股數": b["original_shares"], "目前剩餘股數": b["remaining"],
                        "成交價": float(b["price"]), "剩餘成本": float(b["remaining_cost"])} for b in state["buys"]],
                  money_columns=("成交價", "剩餘成本"))
    with st.expander("已實現損益與賣出成本分攤"):
        dataframe(st, [{"ID": s["id"], "日期": s["date"], "代號": s["ticker"], "股數": s["shares"],
                        "實收": float(s["proceeds"]), "分攤成本": float(s["basis"]),
                        "已實現損益": float(s["realized"])} for s in state["sells"]],
                  money_columns=("實收", "分攤成本", "已實現損益"))
        if state["sells"]:
            sale = st.selectbox("查看賣出成本分攤 ID", state["sells"], format_func=lambda s: s["id"])
            dataframe(st, [{"買入 ID": a["buy_id"], "股數": a["shares"], "成本": float(money(a["cost"]))}
                           for a in sale["allocations"]], money_columns=("成本",))


def record_fields(st, action, state, *, prefix, initial=None):
    initial = initial or {}
    today = datetime.now(TZ).date()
    default_day = trade_day(initial["date"]) if initial.get("date") else today
    fields = {"date": st.date_input("交易／實收日期", value=default_day, max_value=today,
                                    key=prefix + "_day").isoformat()}
    if action in {"buy", "sell", "dividend", "split"}:
        historical = sorted({b["ticker"] for b in state["buys"]} | {d.get("ticker", "") for d in state["dividends"]} - {""})
        st.caption("代號含 .TW 或 .TWO。可補登已清倉股票的股息；行情故障不會阻止記帳。")
        if historical:
            st.caption("曾持有：" + "、".join(historical))
        fields["ticker"] = st.text_input("股票代號", value=initial.get("ticker", "006208.TW"),
                                          key=prefix + "_ticker").strip().upper()
    if action in {"buy", "sell"}:
        fields["shares"] = st.number_input("成交股數", min_value=1, value=integer(initial.get("shares", 1)),
                                            step=1, key=prefix + "_shares")
        fields["price"] = str(money(st.number_input("成交單價 NT$", min_value=0.01,
                                  value=float(initial.get("price", 1)), step=0.01, format="%.2f", key=prefix + "_price")))
        fields["fee"] = str(money(st.number_input("手續費 NT$", min_value=0.0,
                                value=float(initial.get("fee", 0)), step=1.0, format="%.2f", key=prefix + "_fee")))
        fields["tax"] = str(money(st.number_input("交易稅 NT$（依券商明細）", min_value=0.0,
                                value=float(initial.get("tax", 0)), step=1.0, format="%.2f", key=prefix + "_tax")))
        fields["other_fee"] = str(money(st.number_input("其他費用 NT$", min_value=0.0,
                                      value=float(initial.get("other_fee", 0)), step=1.0, format="%.2f", key=prefix + "_other")))
        net = money(money(fields["price"]) * fields["shares"] +
                    (1 if action == "buy" else -1) * (money(fields["fee"]) + money(fields["tax"]) + money(fields["other_fee"])))
        st.caption(("應付" if action == "buy" else "實收") + "：" + amount_text(net) + "（提交預覽後再確認）")
    elif action == "split":
        fields["numerator"] = st.number_input("調整後股數比例", min_value=1,
                                              value=int(initial.get("numerator", 4)), key=prefix + "_num")
        fields["denominator"] = st.number_input("調整前股數比例", min_value=1,
                                                value=int(initial.get("denominator", 1)), key=prefix + "_den")
        st.caption("例如 1 股變 4 股：後=4、前=1。僅調整股數並保留成本；含現金補償／減資不能套用此操作。")
    else:
        default = initial.get("amount", state["target"] if action == "target" and state["target"] else 1000)
        fields["amount"] = str(money(st.number_input("實收／支出／目標金額 NT$", min_value=0.01,
                                    value=float(default), step=100.0, format="%.2f", key=prefix + "_amount")))
    fields["note"] = st.text_input("備註", value=initial.get("note", ""), max_chars=500, key=prefix + "_note")
    return fields


def render_entry(st, ledger, *, disabled=False):
    st.subheader("登記操作")
    action = st.selectbox("操作類型", ["buy", "sell", "deposit", "withdrawal", "dividend", "target", "split"],
                           format_func=lambda a: LABELS[a], key="entry_action")
    st.caption(f"舊帳截止日：{ledger.cutoff if ledger.cutoff != date.min else '尚無舊帳'}。新帳可按交易日期補登；同日依登記順序。")
    with st.form("entry_" + action):
        fields = record_fields(st, action, ledger.state, prefix="entry_" + action)
        if st.form_submit_button("預覽此筆操作", type="primary", disabled=disabled):
            stage(st, ledger, action, **fields)


def render_records(st, ledger):
    st.subheader("全部帳務紀錄")
    search = st.text_input("搜尋代號、備註或 ID", key="record_search").strip().lower()
    include_inactive = st.checkbox("包含已撤銷／舊版刪除", value=True)
    rows = []
    for item in ledger.records:
        e = item["effective"] or item["original"]
        if not include_inactive and item["status"] in {"已撤銷", "舊版已刪除"}:
            continue
        if search and search not in canonical(e).lower():
            continue
        net = None
        if e["action"] in {"buy", "sell"}:
            costs = money(e["fee"]) + money(e.get("tax", 0)) + money(e.get("other_fee", 0))
            net = money(money(e["price"]) * integer(e["shares"]) + (costs if e["action"] == "buy" else -costs))
        elif "amount" in e:
            net = money(e["amount"])
        rows.append({"ID": item["id"], "狀態": item["status"], "交易日期": trade_day(e["date"]).isoformat(),
                     "登記時間": e.get("recorded_at", "舊版未保存"), "類型": LABELS[e["action"]],
                     "代號": e.get("ticker", ""), "股數": e.get("shares"),
                     "應付／實收／金額": float(net) if net is not None else None, "備註": e.get("note", "")})
    rows.sort(key=lambda row: row["交易日期"], reverse=True)
    dataframe(st, rows, money_columns=("應付／實收／金額",))
    st.download_button("匯出全部紀錄 CSV", records_csv(ledger), file_name="tracker_records.csv", mime="text/csv")
    st.caption("CSV 供對帳；完整還原請使用資料管理頁的 JSON 備份。舊版賣出費用欄已包含當時合併輸入的交易費用。")


def render_amend(st, ledger, *, disabled=False):
    st.subheader("更正、撤銷與復原")
    options = [r for r in ledger.records if r["status"] != "舊版已刪除"]
    if not options:
        st.caption("沒有可更正的紀錄。")
        return
    selected = st.selectbox("選擇紀錄", options, key="amend_selection", format_func=lambda r:
                            f"{(r['effective'] or r['original']).get('date', '')[:10]} · "
                            f"{LABELS[r['original']['action']]} · {(r['effective'] or r['original']).get('ticker', '')} · "
                            f"{r['status']} · {r['id'][-8:]}")
    mode = st.radio("處理方式", ["更正內容", "撤銷紀錄", "復原原始內容"], horizontal=True, key="amend_mode")
    st.caption("更正會重算後續現金、庫存與賣出成本；若出現不足，會拒絕執行。歷史行情快照不會自動改寫。")
    with st.form("amend_" + selected["id"] + mode):
        initial = selected["effective"] or selected["original"]
        fields = record_fields(st, initial["action"], ledger.state, prefix="amend_" + selected["id"], initial=initial) if mode == "更正內容" else None
        reason = st.text_input("更正／撤銷／復原原因", max_chars=500, key="amend_reason_" + selected["id"])
        if st.form_submit_button("重算並預覽影響", disabled=disabled):
            replacement = ({**fields, "action": initial["action"], "recorded_at": initial.get("recorded_at", "")}
                           if fields else None if mode == "撤銷紀錄" else selected["original"])
            stage(st, ledger, "amend", target_id=selected["id"], replacement=replacement, reason=reason)


def merged_snapshots(ledger, rows):
    snapshots = {day: {**values, "day": day, "legacy": True} for day, values in ledger.state["snapshots"].items()}
    errors = []
    for i, row in enumerate(rows, 1):
        if not row or not row[0]:
            continue
        try:
            s = json.loads(row[0])
            if s.get("checksum") != digest({k: v for k, v in s.items() if k != "checksum"}):
                raise LedgerError("校驗失敗")
            day = trade_day(s["day"]).isoformat()
            money(s["total_assets"])
            money(s["target_price"])
            if "net_principal" in s:
                money(s["net_principal"])
            snapshots[day] = s
        except (ValueError, KeyError, TypeError):
            errors.append(i)
    return snapshots, errors


def render_history(st, store, ledger, prices, assets, parts):
    import pandas as pd
    st.subheader("資產走勢")
    st.caption("快照由你按下按鈕時保存，同日以最後一筆顯示；沒有開啟 App 的日期不會自動補值。")
    try:
        snapshots, errors = merged_snapshots(ledger, store.read_snapshots())
    except Exception as exc:
        show_failure(st, exc, "快照讀取失敗")
        snapshots, errors = {}, []
    if errors:
        st.warning("部分快照損壞，未納入圖表。列號：" + ", ".join(map(str, errors)))
    if snapshots:
        rows = []
        for day, s in sorted(snapshots.items()):
            rows.append({"日期": date.fromisoformat(day), "總資產": float(money(s["total_assets"])),
                         "淨投入": float(money(s["net_principal"])) if "net_principal" in s else None,
                         "目標": float(money(s["target_price"]))})
        st.line_chart(pd.DataFrame(rows).set_index("日期"))
        mismatch = [d for d, s in snapshots.items() if not s.get("legacy") and s.get("ledger_revision") != ledger.revision]
        if mismatch:
            st.caption("過去快照保留當時數值；之後存提款、交易或更正都可能讓帳本版本不同。")
    if st.button("保存今日快照", disabled=assets is None):
        try:
            latest = store.read()
            if latest.revision != ledger.revision:
                raise Conflict("帳本已更新；請重新整理後再保存快照。")
            s = {"id": str(uuid.uuid4()), "day": datetime.now(TZ).date().isoformat(), "recorded_at": now_text(),
                 "total_assets": str(assets), "target_price": str(ledger.state["target"]),
                 "net_principal": str(net_principal(ledger.state)), "profit": str(parts["total"]),
                 "ledger_revision": ledger.revision, "holdings": holdings(ledger.state),
                 "quotes": {t: {**q, "price": str(q["price"])} for t, q in prices.items()}}
            s["checksum"] = digest(s)
            store.append_snapshot(s)
            st.success("今日快照已保存。")
        except Exception as exc:
            show_failure(st, exc, "快照未能確認")


def restore_to_empty_spreadsheet(source_store, target_spreadsheet, content):
    """Restore only to a different completely empty workbook; never clear existing data."""
    validate_backup(content)
    if target_spreadsheet.id == source_store.spreadsheet.id:
        raise LedgerError("還原目的地必須是另一份空白試算表。")
    sheets = target_spreadsheet.worksheets()
    if not sheets or any(any(cell for row in s.get_all_values() for cell in row) for s in sheets):
        raise LedgerError("目的地試算表必須完全空白；含任何資料都不會覆蓋。")
    baseline_ws = sheets[0]
    baseline_ws.update([[content["raw_baseline"]]], range_name="A1", value_input_option="RAW")
    target = SheetStore(target_spreadsheet, baseline_id=baseline_ws.id, client=source_store.client)
    # Sheet IDs change on restoration. Rebind markers; financial events and revisions stay byte-for-byte equivalent.
    events = deepcopy(content["events"])
    for e in events:
        if e["action"] == "baseline" and "baseline_worksheet_id" in e:
            e["baseline_worksheet_id"] = baseline_ws.id
    if events:
        target.worksheet(EVENTS_NAME, create=True).append_rows([[canonical(e)] for e in events], value_input_option="RAW")
    if content.get("snapshot_rows"):
        target.worksheet(SNAPSHOTS_NAME, create=True).append_rows(content["snapshot_rows"], value_input_option="RAW")
    checked = target.read()
    if checked.revision != content["ledger_revision"]:
        raise LedgerError("還原後版本不符；請保留目的地供檢查，勿切換設定。")
    return target


def render_data(st, store, ledger):
    st.subheader("備份與資料健康")
    rejected = [(rid, s) for rid, s in ledger.statuses.items() if "未入帳" in s["status"]]
    st.write(f"有效事件 {len(ledger.accepted)} 筆 · 未入帳事件 {len(rejected)} 筆")
    st.caption(f"試算表 ID：{store.spreadsheet.id} · 基準工作表 ID：{store.baseline_id} · 帳本版本：{ledger.revision[:16]}")
    for warning in ledger.warnings:
        st.warning(warning)
    if rejected:
        dataframe(st, [{"操作 ID": rid, "狀態": s["status"], "原因": s["reason"]} for rid, s in rejected])
    st.caption("未入帳事件保留供追查；如仍需執行，先確認最新紀錄，再建立新操作。請勿在 Sheets 排序或手改 Events。")
    if st.button("產生完整備份"):
        try:
            current = store.read()
            doc = backup_document(current, spreadsheet_id=store.spreadsheet.id,
                                  baseline_worksheet_id=store.baseline_id, snapshot_rows=store.read_snapshots())
            st.session_state["backup_bytes"] = json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
            st.session_state["backup_revision"] = current.revision
        except Exception as exc:
            show_failure(st, exc, "備份產生失敗")
    if st.session_state.get("backup_bytes"):
        if st.session_state.get("backup_revision") != ledger.revision:
            st.warning("這份已產生備份早於目前帳本，請重新產生最新版。")
        st.download_button("下載完整 JSON 備份", st.session_state["backup_bytes"],
                           file_name=f"tracker_backup_{datetime.now(TZ):%Y%m%d_%H%M%S}.json", mime="application/json")
    with st.expander("驗證備份與還原至另一份空白試算表"):
        upload = st.file_uploader("選擇完整備份 JSON", type=["json"], key="backup_upload")
        if upload:
            try:
                if upload.size > 20 * 1024 * 1024:
                    raise LedgerError("備份超過 20 MiB，請以離線方式處理。")
                content = json.loads(upload.getvalue().decode("utf-8"))
                preview = validate_backup(content)
                st.success(f"備份回放通過：帳面現金 {amount_text(preview.state['balance'])}，有效事件 {len(preview.accepted)} 筆。")
                st.json(holdings(preview.state))
                target_id = st.text_input("另一份空白試算表 ID", key="restore_target")
                confirmed = st.checkbox("我已保存目前帳本備份，目的地是專供還原的空白試算表。")
                if st.button("還原至該空白試算表", disabled=not confirmed or not target_id.strip()):
                    target_spreadsheet = store.client.open_by_key(target_id.strip())
                    result = restore_to_empty_spreadsheet(store, target_spreadsheet, content)
                    st.success("還原完成並通過回放檢查。確認後將 Secrets 的試算表與工作表 ID 改為下方設定。")
                    st.code(f'SPREADSHEET_ID = "{target_id.strip()}"\nBASELINE_WORKSHEET_ID = {result.baseline_id}', language="toml")
            except Exception as exc:
                show_failure(st, exc, "備份驗證／還原失敗")
    with st.expander("匯入待確認操作（刷新後保留同一 ID）"):
        pending_upload = st.file_uploader("待確認操作 JSON", type=["json"], key="pending_upload")
        if pending_upload and st.button("載入並查詢此操作"):
            try:
                event = json.loads(pending_upload.getvalue().decode("utf-8"))
                if event.get("schema_version") != 2 or event.get("event_hash") != digest({k: v for k, v in event.items() if k != "event_hash"}):
                    raise LedgerError("待確認檔案校驗失敗。")
                if st.session_state.get("pending_event"):
                    raise LedgerError("已有待確認操作，請先處理。")
                st.session_state["pending_event"] = event
                st.session_state["pending_sent"] = True
                st.session_state.pop("pending_rejected", None)
                st.query_params["pending"] = event["id"]
                st.rerun()
            except Exception as exc:
                show_failure(st, exc, "操作檔案無法載入")
    with st.expander("原始事件稽核"):
        dataframe(st, [{"ID": e["id"], "操作": LABELS.get(e["action"], e["action"]),
                        "登記時間": e.get("recorded_at", e.get("date", "")),
                        "狀態": ledger.statuses[e["id"]]["status"],
                        "原因": e.get("reason", ledger.statuses[e["id"]]["reason"])} for e in ledger.events])


def render():
    import streamlit as st
    st.set_page_config(page_title="PC Fund Tracker", page_icon="💻", layout="wide")
    sign_in(st)
    st.title("💻 PC Fund Tracker")
    with st.sidebar:
        st.caption("個人股票帳務 · v" + APP_VERSION)
        if st.button("重新整理帳本與行情"):
            st.cache_data.clear()
            st.rerun()
        if st.button("登出"):
            if st.session_state.get("pending_sent") and not st.session_state.get("pending_rejected"):
                st.warning("請先確認待處理操作並保存操作檔案，再登出。")
            else:
                for key in list(st.session_state):
                    del st.session_state[key]
                st.rerun()
    try:
        store = connect_store(st)
        ledger = store.read()
    except Exception as exc:
        show_failure(st, exc, "帳本無法載入，已停止操作")
        st.caption("若有損壞，先從 Google Sheets 下載原始工作表備份，再依錯誤列號檢查。勿清空 A1 或 Events。")
        st.stop()
    if st.session_state.get("flash"):
        st.success(st.session_state.pop("flash"))
    pending = render_pending(st, store, ledger)
    quote = st.cache_data(ttl=300, show_spinner=False)(quote_uncached)
    with st.spinner("讀取參考收盤價…"):
        prices = {t: quote(t) for t in holdings(ledger.state)}
    missing = [t for t, q in prices.items() if q is None]
    if missing:
        st.warning("報價不完整：" + "、".join(missing) + "。總市值與總損益暫不顯示；帳務仍可登記。")
    stale = [t for t, q in prices.items() if q and (datetime.now(TZ).date() - trade_day(q["at"])).days > 4]
    if stale:
        st.warning("以下參考收盤價超過 4 個日曆日（可能為休市或停牌）：" + "、".join(stale))
    rejected = [s for s in ledger.statuses.values() if "未入帳" in s["status"]]
    if rejected:
        st.warning(f"有 {len(rejected)} 筆事件未入帳，請到資料管理查看衝突與原因。")
    parts = profit_parts(ledger.state, prices)
    market, assets = valuation(ledger.state, prices)
    tabs = st.tabs(["總覽與庫存", "登記操作", "全部紀錄", "更正／撤銷", "資產走勢", "資料管理"])
    with tabs[0]:
        render_overview(st, ledger, prices, parts, market, assets)
    with tabs[1]:
        if pending:
            st.caption("請先處理上方待確認操作。")
        render_entry(st, ledger, disabled=pending)
    with tabs[2]:
        render_records(st, ledger)
    with tabs[3]:
        if pending:
            st.caption("請先處理上方待確認操作。")
        render_amend(st, ledger, disabled=pending)
    with tabs[4]:
        render_history(st, store, ledger, prices, assets, parts)
    with tabs[5]:
        render_data(st, store, ledger)


if __name__ == "__main__":
    render()
