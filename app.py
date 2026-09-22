import streamlit as st
import json
import os
from datetime import datetime
import yfinance as yf
import pandas as pd
import plotly.express as px

# ---- 資料庫與 API 函式 ----
DATA_FILE = 'data.json'

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if "daily_snapshots" not in data:
                data["daily_snapshots"] = {}
            return data
    return {"cash_account": {"balance": 0, "history": []}, "target_hardware": {"current_target": 0, "history": []}, "transactions": [], "dividends": [], "daily_snapshots": {}}

def save_data(data):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

@st.cache_data(ttl=300)
def get_live_price(ticker):
    try:
        stock = yf.Ticker(ticker)
        price = stock.history(period="1d")['Close'].iloc[-1]
        return float(price)
    except:
        return 0.0

# ---- 網頁介面開始 ----
st.set_page_config(page_title="PC Fund Tracker", page_icon="💻", layout="wide")

# 🎨 注入自訂 CSS 
st.markdown("""
<style>
    /* 🌟 放大輸入框上方的細項標題文字 */
    [data-testid="stWidgetLabel"] p {
        font-size: 18px !important;
        font-weight: 600 !important;
        color: #E0E0E0 !important; /* 讓文字稍微亮一點點 */
    }

    /* 針對所有數字與文字輸入框：放大字體、增加高度 */
    div[data-baseweb="input"] input {
        font-size: 24px !important;
        padding-top: 14px !important;
        padding-bottom: 14px !important;
    }
    div[data-testid="stButton"] button {
        height: 55px;
        font-size: 18px !important;
        border-radius: 8px;
        font-weight: bold;
    }
    div[data-testid="stVerticalBlock"] h3 {
        padding-bottom: 10px;
    }
    
    /* 💎 置頂浮動 (Sticky Header) */
    div[data-testid="stVerticalBlockBorderWrapper"]:has(#sticky-metrics) {
        position: -webkit-sticky !important;
        position: sticky !important;
        top: 3.5rem !important; 
        z-index: 99999 !important;
        align-self: flex-start !important; 
        background-color: #0E1117 !important; 
        box-shadow: 0px 10px 25px rgba(0,0,0,0.7) !important; 
        border-radius: 10px !important;
    }
</style>
""", unsafe_allow_html=True)

st.title("💻 PC Fund Tracker - 組電腦基金追蹤器")

data = load_data()

# ---- 1. 核心資產與進度計算 ----
portfolio_value = 0
holdings = {}

for tx in data['transactions']:
    ticker = tx['ticker']
    if ticker not in holdings:
        holdings[ticker] = {'shares': 0, 'total_cost': 0}
    
    if tx['type'] == 'buy':
        holdings[ticker]['shares'] += tx['shares']
        holdings[ticker]['total_cost'] += (tx['price'] * tx['shares'] + tx['fee'])

for ticker, info in holdings.items():
    if info['shares'] > 0:
        live_price = get_live_price(ticker)
        portfolio_value += (live_price * info['shares'])

total_assets = data['cash_account']['balance'] + portfolio_value
target_price = data['target_hardware']['current_target']

today_str = datetime.now().strftime("%Y-%m-%d")
data["daily_snapshots"][today_str] = {
    "total_assets": int(total_assets),
    "target_price": int(target_price)
}
save_data(data)

# 🌟 埋入錨點，並包裝數據區塊
with st.container(border=True):
    st.markdown('<span id="sticky-metrics"></span>', unsafe_allow_html=True)
    
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric(label="💰 現金餘額", value=f"NT$ {int(data['cash_account']['balance']):,}")
    with col2:
        st.metric(label="📈 股票總市值", value=f"NT$ {int(portfolio_value):,}")
    with col3:
        st.metric(label="💎 總資產", value=f"NT$ {int(total_assets):,}")
    with col4:
        st.metric(label="🎯 目標金額", value=f"NT$ {int(target_price):,}")

    if target_price > 0:
        progress = min(total_assets / target_price, 1.0)
        st.progress(progress, text=f"目標達成率：{round(progress * 100, 2)}%")

st.write("") 

# ---- 2. 歷史走勢圖表 (Plotly) ----
if data["daily_snapshots"]:
    chart_data = []
    for date, vals in data["daily_snapshots"].items():
        chart_data.append({"日期": date, "指標": "總資產", "金額 (NT$)": vals["total_assets"]})
        chart_data.append({"日期": date, "指標": "硬體目標價", "金額 (NT$)": vals["target_price"]})
    
    df_chart = pd.DataFrame(chart_data)
    fig = px.line(df_chart, x="日期", y="金額 (NT$)", color="指標", markers=True, color_discrete_map={"總資產": "#00CC96", "硬體目標價": "#EF553B"})
    fig.update_layout(yaxis_tickformat=",", hovermode="x unified", margin=dict(l=0, r=0, t=30, b=0), height=350)
    st.plotly_chart(fig, use_container_width=True)

st.divider()

# ---- 3. 執行操作 ----
st.header("📝 執行操作")

row1_col1, row1_col2 = st.columns(2)

with row1_col1:
    with st.container(border=True):
        st.subheader("🛒 新增買入紀錄")
        buy_col1, buy_col2 = st.columns(2)
        with buy_col1:
            buy_ticker = st.text_input("股票代號 (如 2330.TW)", value="006208.TW")
            buy_shares = st.number_input("買入股數 (1張 = 1000)", value=1, step=1)
        with buy_col2:
            buy_price = st.number_input("成交單價", value=0.0, step=1.0)
            buy_fee = st.number_input("手續費 (NT$)", value=0, step=1)
        
        total_cost = (buy_price * buy_shares) + buy_fee
        st.info(f"💡 總成本：NT$ {int(total_cost):,}")
        
        if st.button("確認買入", use_container_width=True):
            if buy_price <= 0 or buy_shares <= 0 or buy_fee < 0:
                st.error("操作失敗：單價與股數必須大於 0，手續費不得為負！")
            else:
                ticker_upper = buy_ticker.upper()
                check_live_price = get_live_price(ticker_upper)
                if check_live_price == 0.0:
                    st.error(f"買入失敗：找不到 {ticker_upper}，請檢查代號！")
                elif total_cost > data['cash_account']['balance']:
                    st.error(f"餘額不足！扣款失敗。")
                else:
                    data['cash_account']['balance'] -= total_cost
                    data['transactions'].append({
                        "tx_id": datetime.now().strftime("%Y%m%d%H%M%S"), "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "type": "buy", "ticker": ticker_upper, "price": buy_price, "shares": buy_shares, "fee": buy_fee
                    })
                    save_data(data)
                    st.rerun()

with row1_col2:
    with st.container(border=True):
        st.subheader("💰 存入打工薪水")
        deposit_amount = st.number_input("輸入存入金額 (NT$)", value=0, step=1000)
        deposit_note = st.text_input("款項備註")
        
        st.write("") 
        st.write("")
        
        if st.button("存入打工金", use_container_width=True):
            if deposit_amount <= 0:
                st.error("操作失敗：存入金額必須大於 0！")
            else:
                data['cash_account']['balance'] += deposit_amount
                data['cash_account']['history'].append({
                    "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "action": "deposit", "amount": deposit_amount, "note": deposit_note
                })
                save_data(data)
                st.rerun()

st.write("") 
row2_col1, row2_col2 = st.columns(2)

with row2_col1:
    with st.container(border=True):
        st.subheader("🎯 更新硬體目標價")
        new_target = st.number_input("輸入新的硬體總價 (NT$)", value=0, step=1000)
        target_note = st.text_input("硬體備註")
        
        if st.button("更新目標價", use_container_width=True):
            if new_target <= 0:
                st.error("操作失敗：目標金額必須大於 0！")
            else:
                data['target_hardware']['current_target'] = new_target
                data['target_hardware']['history'].append({
                    "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "price": new_target, "note": target_note
                })
                save_data(data)
                st.rerun()

with row2_col2:
    with st.container(border=True):
        st.subheader("💸 新增股息收入")
        div_col1, div_col2 = st.columns(2)
        with div_col1:
            div_ticker = st.text_input("股票代號", value="006208.TW")
        with div_col2:
            div_amount = st.number_input("配息總額 (NT$)", value=0, step=100)
        div_note = st.text_input("配息備註")
        
        if st.button("確認領取配息", use_container_width=True):
            if div_amount <= 0:
                st.error("操作失敗：配息金額必須大於 0！")
            else:
                div_ticker_upper = div_ticker.upper()
                if div_ticker_upper in holdings and holdings[div_ticker_upper]['shares'] > 0:
                    data['cash_account']['balance'] += div_amount
                    data['dividends'].append({
                        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "ticker": div_ticker_upper, "amount": div_amount, "note": div_note
                    })
                    save_data(data)
                    st.rerun()
                else:
                    st.error(f"失敗：庫存中沒有 {div_ticker_upper}！")

st.divider()

# ---- 4. 目前投資組合損益表 ----
st.header("📊 目前投資組合 (即時損益)")

portfolio_display = []
for ticker, info in holdings.items():
    if info['shares'] > 0:
        live_price = get_live_price(ticker)
        avg_cost = info['total_cost'] / info['shares']
        current_value = live_price * info['shares']
        unrealized_pl = current_value - info['total_cost']
        roi_percent = (unrealized_pl / info['total_cost']) * 100 if info['total_cost'] > 0 else 0
        
        portfolio_display.append({
            "股票代號": ticker, "持有股數": info['shares'], "平均成本 (含手續費)": avg_cost,
            "目前現價": live_price, "目前市值": int(current_value), "未實現損益": int(unrealized_pl), "報酬率 (%)": round(roi_percent, 2)
        })

if portfolio_display:
    df = pd.DataFrame(portfolio_display)
    styled_df = df.style.format({
        "持有股數": "{:,}", "平均成本 (含手續費)": "{:,.2f}", "目前現價": "{:,.2f}", "目前市值": "{:,}", "未實現損益": "{:,}"
    })
    st.dataframe(styled_df, use_container_width=True)
else:
    st.info("目前沒有庫存股票，快去買進第一檔股票吧！")