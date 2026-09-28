# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "yfinance",
#     "pandas",
#     "mplfinance",
#     "requests",
# ]
# ///

import io
import time
from datetime import datetime, timezone
import requests
import pandas as pd
import yfinance as yf
import mplfinance as mpf

# ================= Configuration =================
TELEGRAM_BOT_TOKEN = "8217551657:AAFn6ouMoK9FnfT3iOdVw9Ew6mBeFyRXwhg"
TELEGRAM_CHAT_ID = "8353483980"

# Yahoo ticker -> display name & price decimals
SYMBOLS = {
    "GC=F":  {"name": "Gold (XAU/USD)", "decimals": 2},
    "JPY=X": {"name": "USD/JPY",        "decimals": 3},
}
TIMEFRAME = "15m"     # 15-minute execution
LOOKBACK = "5d"
CHECK_INTERVAL = 180  # Check every 3 minutes

# Session Times in UTC (HH, MM)
LONDON_START = (7, 0)
LONDON_END   = (11, 30)

NY_START     = (12, 30)
NY_END       = (16, 30)

# Set True to send one forced test alert immediately and exit (ignores sessions & setup rules)
TEST_MODE = False

# ================= Session & Quota Helper =================
def get_active_session():
    """Returns 'London', 'New York', or None based on current UTC time."""
    now_utc = datetime.now(timezone.utc)
    current_time = (now_utc.hour, now_utc.minute)

    if LONDON_START <= current_time <= LONDON_END:
        return "London"
    if NY_START <= current_time <= NY_END:
        return "New York"
    return None

# ================= Indicators =================
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # Trigger EMAs
    df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
    df["EMA21"] = df["Close"].ewm(span=21, adjust=False).mean()
    
    # Macro Trend EMAs (Filters out counter-trend whipsaws to keep signals low & clean)
    df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()
    df["EMA200"] = df["Close"].ewm(span=200, adjust=False).mean()

    # RSI (14)
    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=14).mean()
    avg_loss = loss.rolling(window=14).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    df["RSI"] = 100 - (100 / (1 + rs))

    # ATR (14)
    hl = df["High"] - df["Low"]
    hc = (df["High"] - df["Close"].shift()).abs()
    lc = (df["Low"] - df["Close"].shift()).abs()
    ranges = pd.concat([hl, hc, lc], axis=1)
    df["ATR"] = ranges.max(axis=1).rolling(14).mean()

    return df

# ================= High-Conviction Strategy Filter =================
def check_signal(df: pd.DataFrame, session_name: str, symbol: str):
    """
    Evaluates setups and calculates a confidence score (0-100).
    Only high-confidence setups (score >= 85) are triggered.
    """
    curr = df.iloc[-2]  # Most recent closed candle
    prev = df.iloc[-3]  # Prior closed candle

    bullish_cross = (prev["EMA9"] <= prev["EMA21"]) and (curr["EMA9"] > curr["EMA21"])
    bearish_cross = (prev["EMA9"] >= prev["EMA21"]) and (curr["EMA9"] < curr["EMA21"])

    if not bullish_cross and not bearish_cross:
        return None

    score = 40  # Base score for valid EMA cross

    if bullish_cross:
        # 1. Macro Trend (15 pts)
        if curr["Close"] > curr["EMA200"]:
            score += 15
        
        # 2. Medium Trend Confirmation (15 pts)
        if curr["EMA50"] > curr["EMA200"]:
            score += 15
            
        # 3. Momentum Sweet Spot (15 pts)
        if 50 <= curr["RSI"] <= 65:
            score += 15
            
        # 4. Pullback/Extension Check - Not overextended (15 pts)
        if (curr["Close"] - curr["EMA21"]) <= curr["ATR"]:
            score += 15

        if score >= 85:
            return build_signal(curr, "BUY", session_name, symbol, score)

    elif bearish_cross:
        # 1. Macro Trend (15 pts)
        if curr["Close"] < curr["EMA200"]:
            score += 15
            
        # 2. Medium Trend Confirmation (15 pts)
        if curr["EMA50"] < curr["EMA200"]:
            score += 15
            
        # 3. Momentum Sweet Spot (15 pts)
        if 35 <= curr["RSI"] <= 50:
            score += 15
            
        # 4. Pullback/Extension Check - Not overextended (15 pts)
        if (curr["EMA21"] - curr["Close"]) <= curr["ATR"]:
            score += 15

        if score >= 85:
            return build_signal(curr, "SELL", session_name, symbol, score)

    return None

def build_signal(curr: pd.Series, action: str, session_name: str, symbol: str, score: int) -> dict:
    """Entry at candle close, SL = 1.5 ATR, TP = 2.5 ATR."""
    decimals = SYMBOLS[symbol]["decimals"]
    entry_price = round(curr["Close"], decimals)
    atr = curr["ATR"]
    direction = 1 if action == "BUY" else -1
    return {
        "action": action,
        "symbol": symbol,
        "session": session_name,
        "entry": entry_price,
        "sl": round(entry_price - direction * 1.5 * atr, decimals),
        "tp": round(entry_price + direction * 2.5 * atr, decimals),
        "rsi": round(curr["RSI"], 2),
        "score": score,
        "timestamp": curr.name
    }

# ================= Chart Plotter =================
def generate_chart_bytes(df: pd.DataFrame, signal_info: dict) -> io.BytesIO:
    plot_df = df.iloc[-70:].copy()

    ema_plots = [
        mpf.make_addplot(plot_df["EMA9"], color="#00e5ff", width=1.1),
        mpf.make_addplot(plot_df["EMA21"], color="#ff4081", width=1.1),
        mpf.make_addplot(plot_df["EMA200"], color="#f59e0b", width=1.5),
        mpf.make_addplot(plot_df["RSI"], panel=1, color="#f1c40f", width=1.0, ylabel="RSI")
    ]

    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        rc={"font.size": 8, "grid.color": "#2c3e50"}
    )

    buf = io.BytesIO()
    mpf.plot(
        plot_df,
        type="candle",
        style=style,
        addplot=ema_plots,
        volume=False,
        panel_ratios=(3, 1),
        title=f"\n{SYMBOLS[signal_info['symbol']]['name']} [{TIMEFRAME}] ({signal_info['session']} Session) - {signal_info['action']} @ {signal_info['entry']} (Score: {signal_info.get('score', 100)}/100)",
        savefig=dict(fname=buf, dpi=120, bbox_inches="tight", format="png")
    )
    buf.seek(0)
    return buf

# ================= Telegram Sender =================
def send_telegram_alert(signal_info: dict, chart_buffer: io.BytesIO):
    emoji = "🟢" if signal_info["action"] == "BUY" else "🔴"
    symbol = signal_info["symbol"]

    caption = (
        f"{emoji} <b>{signal_info['session'].upper()} SESSION SIGNAL</b>\n\n"
        f"<b>Asset:</b> <code>{SYMBOLS[symbol]['name']}</code>\n"
        f"<b>Action:</b> <b>{signal_info['action']}</b>\n"
        f"🏅 <b>Confidence Score:</b> {signal_info.get('score', 100)}/100\n"
        f"📍 <b>Entry:</b> <code>{signal_info['entry']}</code>\n"
        f"🎯 <b>Take Profit (TP):</b> <code>{signal_info['tp']}</code>\n"
        f"🛑 <b>Stop Loss (SL):</b> <code>{signal_info['sl']}</code>\n"
        f"📊 <b>RSI:</b> {signal_info['rsi']}\n"
        f"⏱ <b>Candle:</b> {signal_info['timestamp']}\n\n"
        f"<i>High Conviction: Trend alignment & healthy extension check passed.</i>"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", chart_buffer, "image/png")}
    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}

    resp = requests.post(url, data=data, files=files)
    if resp.status_code == 200:
        print(f"[{time.strftime('%H:%M:%S')}] Signal sent to Telegram successfully!")
    else:
        print(f"Telegram API Error: {resp.text}")

# ================= Scanner Engine =================
def fetch_data(symbol: str) -> pd.DataFrame:
    raw_data = yf.download(tickers=symbol, period=LOOKBACK, interval=TIMEFRAME, progress=False)
    if isinstance(raw_data.columns, pd.MultiIndex):
        raw_data.columns = raw_data.columns.get_level_values(0)
    return raw_data

def run_bot():
    names = ", ".join(info["name"] for info in SYMBOLS.values())
    print(f"[*] Waking up! Starting Precision Signal Bot check for {names}...")

    # 1. Gate: Check active session
    active_session = get_active_session()

    if active_session:
        for symbol, info in SYMBOLS.items():
            try:
                # 2. Fetch market data
                raw_data = fetch_data(symbol)
                if len(raw_data) < 205:
                    print(f"[x] Not enough data for {info['name']} ({len(raw_data)} candles).")
                    continue

                # 3. Calculate indicators
                df = compute_indicators(raw_data)
                current_closed_time = df.iloc[-2].name

                # 4. Check setup on freshly closed candle
                print(f"[{time.strftime('%H:%M:%S')}] Evaluating {info['name']} candle in {active_session} Session...")
                signal = check_signal(df, active_session, symbol)

                if signal:
                    print(f"[!] {info['name']} {signal['action']} signal triggered during {active_session} session!")
                    chart_buf = generate_chart_bytes(df, signal)
                    send_telegram_alert(signal, chart_buf)
                else:
                    print(f"[-] No A+ setup on {info['name']} candle {current_closed_time}.")

            except Exception as e:
                print(f"[x] Error scanning {info['name']}: {e}")
    else:
        print(f"[-] Market is not in London or New York session. Sleeping.")

    print("[*] Check complete. Exiting script.")

# ================= Test Mode =================
def run_test():
    for symbol, info in SYMBOLS.items():
        print(f"[TEST] Sending forced test alert for {info['name']}...")
        df = compute_indicators(fetch_data(symbol))
        curr = df.iloc[-2]
        action = "BUY" if curr["EMA9"] > curr["EMA21"] else "SELL"
        signal = build_signal(curr, action, "TEST", symbol, 100)
        chart_buf = generate_chart_bytes(df, signal)
        send_telegram_alert(signal, chart_buf)

if __name__ == "__main__":
    if TEST_MODE:
        run_test()
    else:
        run_bot()