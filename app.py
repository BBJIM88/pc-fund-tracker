"""PC fund tracker. Keep the old A1 ledger as a read-only baseline.

New changes are appended to an Events worksheet in the same spreadsheet.
Set APP_PASSWORD and GCP_KEY_JSON in Streamlit Secrets before running.
"""

import hashlib
import hmac
import json
import math
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import gspread
import pandas as pd
import plotly.express as px
import streamlit as st
import yfinance as yf


SHEET_NAME = "PC_Fund_Tracker"
EVENTS_NAME = "Events"
TZ = ZoneInfo("Asia/Taipei")
CENT = Decimal("0.01")
TICKER_PATTERN = re.compile(r"^[0-9A-Z]{4,8}\.(TW|TWO)$")


def money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            raise ValueError("金額必須是有限數字")
        return amount.quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"無效金額：{value}") from exc


def amount_text(value):
    return f"NT$ {money(value):,.2f}"


def now_text():
    return datetime.now(TZ).isoformat(timespec="seconds")


def default_data():
    return {
        "cash_account": {"balance": 0, "history": []},
        "target_hardware": {"current_target": 0, "history": []},
        "transactions": [],
        "dividends": [],
        "daily_snapshots": {},
    }


def connection():
    # Never fall back to an easily committed local key file.
    raw_key = st.secrets.get("GCP_KEY_JSON")
    if not raw_key:
        raise RuntimeError("尚未設定 GCP_KEY_JSON")
    credentials = json.loads(raw_key)
    return gspread.service_account_from_dict(credentials).open(SHEET_NAME)


def events_worksheet(spreadsheet):
    try:
        return spreadsheet.worksheet(EVENTS_NAME)
    except gspread.WorksheetNotFound:
        try:
            return spreadsheet.add_worksheet(title=EVENTS_NAME, rows=100, cols=1)
        except gspread.exceptions.APIError:
            # Another session may have created it in the meantime.
            return spreadsheet.worksheet(EVENTS_NAME)


def load_ledger():
    spreadsheet = connection()
    raw_baseline = spreadsheet.sheet1.acell("A1").value or ""
    baseline = json.loads(raw_baseline) if raw_baseline else default_data()
    baseline_hash = hashlib.sha256(raw_baseline.encode("utf-8")).hexdigest()
    event_sheet = events_worksheet(spreadsheet)
    rows = event_sheet.get_all_values()
    events = [json.loads(row[0]) for row in rows if row and row[0]]

    saved_hashes = {e["baseline_hash"] for e in events if e.get("action") == "baseline"}
    if saved_hashes and saved_hashes != {baseline_hash}:
        raise ValueError("舊版 A1 資料在啟用新版後被改動。請先備份並停止舊版程式。")
    if not saved_hashes:
        # A1 is not changed by this application, so it remains a recovery copy.
        event_sheet.append_row(
            [json.dumps({"id": str(uuid.uuid4()), "action": "baseline", "baseline_hash": baseline_hash})],
            value_input_option="RAW",
        )

    state = make_state(baseline)
    seen = set()
    for event in events:
        event_id = event["id"]
        if event_id in seen:
            continue  # A retry of a timed-out append must not count twice.
        seen.add(event_id)
        if event["action"] != "baseline":
            apply_event(state, event)
    return state, event_sheet


def make_state(baseline):
    cash = baseline["cash_account"]
    target = baseline["target_hardware"]
    state = {
        "balance": money(cash["balance"]),
        "target": money(target["current_target"]),
        "buys": [], "sells": [], "deposits": [], "withdrawals": [], "dividends": [],
        "target_history": list(target.get("history", [])),
        "snapshots": dict(baseline.get("daily_snapshots", {})),
    }
    for index, transaction in enumerate(baseline.get("transactions", [])):
        if transaction["type"] != "buy":
            raise ValueError("舊帳本含未知交易類型，無法安全載入")
        shares = int(transaction["shares"])
        if shares <= 0:
            raise ValueError("舊帳本含無效股數")
        price = money(transaction["price"])
        fee = money(transaction["fee"])
        state["buys"].append({
            "id": f"legacy-buy-{index}", "date": transaction["date"],
            "ticker": transaction["ticker"].strip().upper(),
            "shares": shares, "remaining": shares, "price": price, "fee": fee,
            "remaining_cost": money(price * shares + fee),
        })
    for index, deposit in enumerate(cash.get("history", [])):
        state["deposits"].append({**deposit, "id": f"legacy-deposit-{index}",
                                  "amount": money(deposit["amount"])})
    for index, dividend in enumerate(baseline.get("dividends", [])):
        state["dividends"].append({**dividend, "id": f"legacy-dividend-{index}",
                                   "amount": money(dividend["amount"])})
    return state


def holdings(state):
    result = {}
    for lot in state["buys"]:
        if lot["remaining"] > 0:
            result[lot["ticker"]] = result.get(lot["ticker"], 0) + lot["remaining"]
    return result


def apply_event(state, event):
    action = event["action"]
    entry_id = event["id"]
    date = event.get("date", "")
    if action == "deposit":
        value = money(event["amount"])
        if value <= 0:
            raise ValueError("存款金額必須大於零")
        state["balance"] += value
        state["deposits"].append({"id": entry_id, "date": date, "amount": value, "note": event.get("note", "")})
    elif action == "withdrawal":
        value = money(event["amount"])
        if value <= 0 or value > state["balance"]:
            raise ValueError("提款金額無效或超過餘額")
        state["balance"] -= value
        state["withdrawals"].append({"id": entry_id, "date": date, "amount": value, "note": event.get("note", "")})
    elif action == "buy":
        shares, price, fee = int(event["shares"]), money(event["price"]), money(event["fee"])
        ticker = event["ticker"].strip().upper()
        cost = money(price * shares + fee)
        if shares <= 0 or price <= 0 or fee < 0 or not TICKER_PATTERN.fullmatch(ticker) or cost > state["balance"]:
            raise ValueError("買入資料無效或現金餘額不足")
        state["balance"] -= cost
        state["buys"].append({"id": entry_id, "date": date, "ticker": ticker,
                              "shares": shares, "remaining": shares, "price": price,
                              "fee": fee, "remaining_cost": cost})
    elif action == "sell":
        shares, price, fee = int(event["shares"]), money(event["price"]), money(event["fee"])
        ticker = event["ticker"].strip().upper()
        proceeds = money(price * shares - fee)
        if shares <= 0 or price <= 0 or fee < 0 or proceeds < 0 or holdings(state).get(ticker, 0) < shares:
            raise ValueError("賣出資料無效或庫存不足")
        unallocated, basis = shares, Decimal("0.00")
        # FIFO; the final sale of a lot takes its exact remaining cost.
        for lot in state["buys"]:
            if lot["ticker"] != ticker or lot["remaining"] == 0:
                continue
            taken = min(unallocated, lot["remaining"])
            allocated_cost = (lot["remaining_cost"] if taken == lot["remaining"] else
                              money(lot["remaining_cost"] * taken / lot["remaining"]))
            lot["remaining_cost"] -= allocated_cost
            lot["remaining"] -= taken
            basis += allocated_cost
            unallocated -= taken
            if unallocated == 0:
                break
        state["balance"] += proceeds
        state["sells"].append({"id": entry_id, "date": date, "ticker": ticker,
                               "shares": shares, "price": price, "fee": fee,
                               "realized": proceeds - basis})
    elif action == "dividend":
        value = money(event["amount"])
        ticker = event["ticker"].strip().upper()
        if value <= 0 or holdings(state).get(ticker, 0) <= 0:
            raise ValueError("股息金額無效或沒有該股票庫存")
        state["balance"] += value
        state["dividends"].append({"id": entry_id, "date": date, "ticker": ticker,
                                   "amount": value, "note": event.get("note", "")})
    elif action == "target":
        value = money(event["amount"])
        if value <= 0:
            raise ValueError("目標金額必須大於零")
        state["target"] = value
        state["target_history"].append({"date": date, "price": str(value), "note": event.get("note", "")})
    elif action == "delete":
        category, target_id = event["category"], event["target_id"]
        if category not in ("buys", "deposits", "dividends"):
            raise ValueError("未知刪除類型")
        entry = next((item for item in state[category] if item["id"] == target_id), None)
        if entry is None:
            raise ValueError("欲刪除的紀錄已不存在")
        if category == "buys":
            if entry["remaining"] != entry["shares"]:
                raise ValueError("該筆買入已部分賣出，無法刪除")
            state["balance"] += money(entry["price"] * entry["shares"] + entry["fee"])
        else:
            if entry["amount"] > state["balance"]:
                raise ValueError("刪除後現金會不足；請先處理後續支出")
            state["balance"] -= entry["amount"]
        state[category].remove(entry)
    elif action == "snapshot":
        state["snapshots"][event["day"]] = {
            "total_assets": float(money(event["total_assets"])),
            "target_price": float(money(event["target_price"])),
        }
    else:
        raise ValueError(f"未知事件類型：{action}")


def append_event(worksheet, action, **fields):
    event = {"id": str(uuid.uuid4()), "action": action, "date": now_text(), **fields}
    worksheet.append_row([json.dumps(event, ensure_ascii=False)], value_input_option="RAW")
    return event


@st.cache_data(ttl=300)
def quote(ticker):
    try:
        history = yf.Ticker(ticker).history(period="5d", auto_adjust=False)
        if history.empty:
            return None
        price = float(history["Close"].iloc[-1])
        quoted_at = history.index[-1].isoformat()
        if not math.isfinite(price) or price <= 0:
            return None
        return {"price": money(price), "at": quoted_at}
    except Exception:
        return None


def price_all(state):
    prices = {ticker: quote(ticker) for ticker in holdings(state)}
    if any(value is None for value in prices.values()):
        return prices, None, None
    portfolio = sum((prices[ticker]["price"] * shares for ticker, shares in holdings(state).items()), Decimal("0.00"))
    return prices, money(portfolio), money(state["balance"] + portfolio)


def commit(worksheet, state, action, **fields):
    """Validate locally, then append. Network uncertainty is reported, never shown as success."""
    draft = clone_state(state)
    candidate = {"id": str(uuid.uuid4()), "action": action, "date": now_text(), **fields}
    apply_event(draft, candidate)
    worksheet.append_row([json.dumps(candidate, ensure_ascii=False)], value_input_option="RAW")
    st.rerun()


def clone_state(state):
    from copy import deepcopy
    return deepcopy(state)


def safe_commit(worksheet, state, action, **fields):
    try:
        commit(worksheet, state, action, **fields)
    except ValueError as exc:
        st.error(str(exc))
    except Exception:
        st.error("寫入狀態無法確認，請重新整理並檢查紀錄後再操作，避免重複登記。")
        st.stop()


def sign_in():
    password = st.secrets.get("APP_PASSWORD")
    if not isinstance(password, str) or len(password) < 12:
        st.error("請在 Streamlit Secrets 設定至少 12 字元的 APP_PASSWORD。")
        st.stop()
    signature = hashlib.sha256(password.encode("utf-8")).hexdigest()
    if st.session_state.get("auth_signature") == signature:
        return
    st.session_state.pop("auth_signature", None)
    st.title("🔒 登入 PC Fund Tracker")
    locked_until = st.session_state.get("locked_until")
    if locked_until and datetime.now(TZ) < locked_until:
        st.error("嘗試次數過多，請稍後再試。")
        st.stop()
    entered = st.text_input("密碼", type="password")
    if st.button("登入"):
        if hmac.compare_digest(entered, password):
            st.session_state["auth_signature"] = signature
            st.session_state["failed_logins"] = 0
            st.rerun()
        attempts = st.session_state.get("failed_logins", 0) + 1
        st.session_state["failed_logins"] = attempts
        if attempts >= 5:
            st.session_state["locked_until"] = datetime.now(TZ) + timedelta(minutes=15)
        st.error("密碼錯誤")
    st.stop()


def render():
    st.set_page_config(page_title="PC Fund Tracker", page_icon="💻", layout="wide")
    sign_in()
    st.title("💻 PC Fund Tracker")
    if st.button("登出"):
        st.session_state.pop("auth_signature", None)
        st.rerun()

    try:
        state, worksheet = load_ledger()
    except Exception as exc:
        st.error(f"無法讀取帳本，已停止操作。原因：{exc}")
        st.stop()

    prices, portfolio, assets = price_all(state)
    missing = [ticker for ticker, result in prices.items() if result is None]
    if missing:
        st.error("無法取得報價：" + "、".join(missing) + "。總市值、總資產與損益暫停計算。")
    day = datetime.now(TZ).date().isoformat()
    if assets is not None:
        desired = {"total_assets": float(assets), "target_price": float(state["target"])}
        if state["snapshots"].get(day) != desired:
            try:
                append_event(worksheet, "snapshot", day=day, **{k: str(v) for k, v in desired.items()})
                state["snapshots"][day] = desired
            except Exception:
                st.warning("今日走勢快照未儲存；交易紀錄不受影響。")

    cols = st.columns(4)
    for column, (label, value) in zip(cols, [
        ("現金餘額", amount_text(state["balance"])),
        ("股票總市值", amount_text(portfolio) if portfolio is not None else "無法計算"),
        ("總資產", amount_text(assets) if assets is not None else "無法計算"),
        ("硬體目標價", amount_text(state["target"])),
    ]):
        column.metric(label, value)
    if state["target"] > 0 and assets is not None:
        st.metric("含現金距離目標", amount_text(state["target"] - assets))
        st.metric("股票市值距離目標", amount_text(state["target"] - portfolio))
        st.progress(min(float(assets / state["target"]), 1.0), text=f"可用資金達成率：{assets / state['target']:.2%}")

    if assets is not None:
        realized = sum((sale["realized"] for sale in state["sells"]), Decimal("0.00"))
        unrealized = sum((prices[lot["ticker"]]["price"] * lot["remaining"] - lot["remaining_cost"]
                          for lot in state["buys"] if lot["remaining"]), Decimal("0.00"))
        dividends = sum((d["amount"] for d in state["dividends"]), Decimal("0.00"))
        investment_profit = money(realized + unrealized + dividends)
        st.metric("投資損益（含股息、交易手續費）", amount_text(investment_profit))
        history = state["target_history"]
        if len(history) >= 2:
            increase = state["target"] - money(history[0]["price"])
            st.metric("相對首次記錄的硬體漲幅：投資損益減漲幅", amount_text(investment_profit - increase))
        st.caption("投資損益以買入成本、賣出收入與已登記股息計算；不把新存入的薪水當成獲利。")

    if state["snapshots"]:
        chart_rows = []
        for date, values in sorted(state["snapshots"].items()):
            chart_rows.extend([
                {"日期": date, "指標": "總資產", "金額（NT$）": values["total_assets"]},
                {"日期": date, "指標": "硬體目標價", "金額（NT$）": values["target_price"]},
            ])
        st.plotly_chart(px.line(pd.DataFrame(chart_rows), x="日期", y="金額（NT$）",
                                color="指標", markers=True), use_container_width=True)
        st.caption("每日快照是當天最後一次開啟程式或操作時的數值，並非自動收盤價。")

    st.header("登記操作")
    left, right = st.columns(2)
    with left:
        with st.form("buy"):
            st.subheader("買入（台灣市場、台幣）")
            ticker = st.text_input("股票代號", value="006208.TW").strip().upper()
            shares = st.number_input("股數", min_value=1, value=1, step=1)
            price = st.number_input("成交單價 NT$", min_value=0.01, value=1.0, step=0.01, format="%.2f")
            fee = st.number_input("買入手續費 NT$", min_value=0.0, value=0.0, step=1.0, format="%.2f")
            if st.form_submit_button("確認買入"):
                if not TICKER_PATTERN.fullmatch(ticker):
                    st.error("目前僅支援 .TW 或 .TWO 的台幣股票代號。")
                elif quote(ticker) is None:
                    st.error("目前無法驗證代號與行情，請稍後重試。")
                else:
                    safe_commit(worksheet, state, "buy", ticker=ticker, shares=int(shares),
                                price=str(money(price)), fee=str(money(fee)))
        with st.form("sell"):
            st.subheader("賣出")
            available = holdings(state)
            sell_ticker = st.selectbox("庫存股票", list(available), key="sell_ticker") if available else None
            sell_shares = st.number_input("賣出股數", min_value=1, value=1, step=1)
            sell_price = st.number_input("賣出成交單價 NT$", min_value=0.01, value=1.0, step=0.01, format="%.2f")
            sell_fee = st.number_input("賣出手續費及相關交易費用 NT$", min_value=0.0, value=0.0, step=1.0, format="%.2f")
            if st.form_submit_button("確認賣出"):
                if sell_ticker:
                    safe_commit(worksheet, state, "sell", ticker=sell_ticker,
                                shares=int(sell_shares), price=str(money(sell_price)), fee=str(money(sell_fee)))
                else:
                    st.error("目前沒有可賣出的股票。")
    with right:
        with st.form("deposit"):
            st.subheader("存入現金")
            value = st.number_input("存入金額 NT$", min_value=1, value=1000, step=100)
            note = st.text_input("存款備註", max_chars=500)
            if st.form_submit_button("確認存入"):
                safe_commit(worksheet, state, "deposit", amount=str(value), note=note)
        with st.form("withdrawal"):
            st.subheader("提款或支付電腦費用")
            value = st.number_input("提款金額 NT$", min_value=1, value=1000, step=100)
            note = st.text_input("提款備註", max_chars=500)
            if st.form_submit_button("確認提款"):
                safe_commit(worksheet, state, "withdrawal", amount=str(value), note=note)
        with st.form("target"):
            st.subheader("更新硬體目標")
            value = st.number_input("目前硬體總價 NT$", min_value=1, value=1000, step=100)
            note = st.text_input("硬體備註", max_chars=500)
            if st.form_submit_button("更新目標"):
                safe_commit(worksheet, state, "target", amount=str(value), note=note)
        with st.form("dividend"):
            st.subheader("登記股息")
            dividend_ticker = st.selectbox("配息股票", list(holdings(state)), key="dividend_ticker") if holdings(state) else None
            value = st.number_input("實收股息 NT$", min_value=1, value=100, step=100)
            note = st.text_input("股息備註", max_chars=500)
            if st.form_submit_button("確認股息"):
                if dividend_ticker:
                    safe_commit(worksheet, state, "dividend", ticker=dividend_ticker, amount=str(value), note=note)
                else:
                    st.error("目前沒有可登記股息的庫存。")

    st.header("買入明細與未實現損益")
    if state["buys"]:
        rows = []
        for lot in state["buys"]:
            current = prices.get(lot["ticker"])
            remaining_value = (money(current["price"] * lot["remaining"])
                               if current and lot["remaining"] else None)
            rows.append({"日期": lot["date"], "代號": lot["ticker"], "成交單價": str(lot["price"]),
                         "原買入股數": lot["shares"], "剩餘股數": lot["remaining"],
                         "買入手續費": str(lot["fee"]),
                         "報價日期": current["at"] if current else "無法取得",
                         "剩餘市值": str(remaining_value) if remaining_value is not None else "—",
                         "未實現損益": str(money(remaining_value - lot["remaining_cost"]))
                         if remaining_value is not None else "—"})
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    if state["sells"]:
        st.subheader("賣出與已實現損益（先買先賣）")
        st.dataframe(pd.DataFrame([{"日期": s["date"], "代號": s["ticker"], "股數": s["shares"],
                                   "成交單價": str(s["price"]), "費用": str(s["fee"]),
                                   "已實現損益": str(s["realized"])} for s in state["sells"]]),
                     hide_index=True, use_container_width=True)

    with st.expander("刪除輸入錯誤的買入、存款或股息"):
        st.caption("已賣出部分股數的買入不能刪除；刪除不會改動原本的 A1 資料。")
        for category, title in (("buys", "買入"), ("deposits", "存款"), ("dividends", "股息")):
            entries = state[category]
            if not entries:
                continue
            # Options are IDs, so two identical-looking records remain independently selectable.
            by_id = {entry["id"]: entry for entry in entries}
            choice = st.selectbox(
                f"選擇{title}紀錄", list(by_id),
                format_func=lambda entry_id, mapping=by_id: (
                    f"{mapping[entry_id]['date']}　{mapping[entry_id].get('ticker', '')}　"
                    f"{mapping[entry_id].get('amount', mapping[entry_id].get('shares', ''))}　"
                    f"ID: {entry_id[-8:]}"),
                key=f"delete_{category}",
            )
            if st.button(f"刪除此筆{title}", key=f"confirm_{category}"):
                safe_commit(worksheet, state, "delete", category=category, target_id=choice)


if __name__ == "__main__":
    render()
