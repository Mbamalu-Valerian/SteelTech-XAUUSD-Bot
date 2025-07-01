import tkinter as tk
from tkinter import ttk
import pandas as pd
import requests
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import datetime

# === CONFIG ===
API_KEY = "25e62fb28d1040f1be47bf4f4c5d4138"
NEWS_API_KEY = "d5209d1d64e346c1ad6ce0890156d585"
BASE_URL = "https://api.twelvedata.com/time_series"
LOG_FILE = "signals_log.csv"
last_refresh_time = None

# === FETCH PRICE DATA ===
def fetch_data(interval="15min"):
    params = {
        "symbol": "XAU/USD",
        "interval": interval,
        "apikey": API_KEY,
        "outputsize": 50,
        "format": "JSON"
    }
    response = requests.get(BASE_URL, params=params)
    data = response.json()
    if "status" in data and data["status"] == "error":
        raise ValueError(data.get("message", "API Error"))
    if 'values' not in data:
        raise ValueError("No 'values' in API response")
    df = pd.DataFrame(data['values'])
    df['datetime'] = pd.to_datetime(df['datetime'])
    df.set_index('datetime', inplace=True)
    for col in ['open', 'high', 'low', 'close']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df.dropna(inplace=True)
    df.sort_index(inplace=True)
    return df

# === INDICATORS ===
def calculate_indicators(df):
    df['MA_short'] = df['close'].rolling(window=5).mean()
    df['MA_long'] = df['close'].rolling(window=20).mean()
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=14).mean()
    avg_loss = loss.rolling(window=14).mean()
    rs = avg_gain / avg_loss
    df['RSI'] = 100 - (100 / (1 + rs))
    tr1 = df['high'] - df['low']
    tr2 = (df['high'] - df['close'].shift()).abs()
    tr3 = (df['low'] - df['close'].shift()).abs()
    df['TR'] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df['ATR'] = df['TR'].rolling(window=14).mean()
    df['Breakout'] = (df['high'] > df['high'].rolling(window=20).max().shift(1)) | \
                     (df['low'] < df['low'].rolling(window=20).min().shift(1))
    return df

# === NEWS CHECK ===
def is_high_impact_news_near():
    url = (
        f"https://newsapi.org/v2/everything?"
        f"q=gold+OR+XAUUSD+OR+Federal+Reserve+OR+FOMC+OR+NFP+OR+CPI+OR+USD&"
        f"language=en&sortBy=publishedAt&pageSize=5&"
        f"apiKey={NEWS_API_KEY}"
    )
    try:
        response = requests.get(url)
        data = response.json()
        for article in data.get("articles", []):
            published_time = datetime.datetime.fromisoformat(article['publishedAt'].replace("Z", "+00:00"))
            if (datetime.datetime.now(datetime.timezone.utc) - published_time).total_seconds() < 1800:
                return True, article['title']
    except Exception as e:
        print(f"News check failed: {e}")
    return False, ""

# === MULTI-TIMEFRAME ===
def multi_timeframe_confirm():
    df_15 = calculate_indicators(fetch_data("15min"))
    df_1h = calculate_indicators(fetch_data("1h"))
    df_5m = calculate_indicators(fetch_data("5min"))

    def assess(df):
        last = df.iloc[-1]
        ma_score = 1 if last['MA_short'] > last['MA_long'] else -1
        rsi_score = 1 if last['RSI'] > 50 else -1
        candle = 1 if last['close'] > last['open'] else -1
        breakout = 1 if last['Breakout'] else 0
        return ma_score + rsi_score + candle + breakout

    scores = {
        "5m": assess(df_5m),
        "15m": assess(df_15),
        "1h": assess(df_1h)
    }
    total_score = sum(scores.values())
    return total_score, df_15

# === SIGNAL ENGINE ===
def signal_engine():
    news_mode, news_title = is_high_impact_news_near()
    if news_mode:
        df = fetch_data("5min")
        last = df.iloc[-1]
        entry = last['close']
        sl = round(entry * 0.001, 2)  # tighter SL
        tp1 = round(entry + sl * 1.2, 2)
        tp2 = round(entry + sl * 1.8, 2)
        tp3 = round(entry + sl * 2.5, 2)
        return f"⚠️ News Breakout Signal - {news_title[:40]}", entry, tp1, tp2, tp3, round(entry - sl, 2)

    score, df = multi_timeframe_confirm()
    last = df.iloc[-1]
    entry = last['close']
    atr = last['ATR']
    sl_distance = min(max(atr, entry * 0.0025), entry * 0.01)

    if score >= 3:
        sl = round(entry - sl_distance, 2)
        return "🟢 Strong Buy", round(entry, 2), round(entry + sl_distance, 2), round(entry + sl_distance*1.5, 2), round(entry + sl_distance*2, 2), sl
    elif score <= -3:
        sl = round(entry + sl_distance, 2)
        return "🔴 Strong Sell", round(entry, 2), round(entry - sl_distance, 2), round(entry - sl_distance*1.5, 2), round(entry - sl_distance*2, 2), sl
    elif score == 2:
        return "🟡 Weak Buy", round(entry, 2), None, None, None, None
    elif score == -2:
        return "🟠 Weak Sell", round(entry, 2), None, None, None, None
    else:
        return "⚪ No Clear Signal", round(entry, 2), None, None, None, None

# === LOG TO CSV ===
def log_signal(signal, entry, tp1, tp2, tp3, sl):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    df_log = pd.DataFrame([{
        "datetime": now,
        "signal": signal,
        "entry": entry,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "sl": sl
    }])
    try:
        with open(LOG_FILE, 'a', encoding="utf-8") as f:
            df_log.to_csv(f, header=f.tell() == 0, index=False)
    except Exception as e:
        print(f"Logging failed: {e}")

# === GUI UPDATE ===
def update_analysis():
    global last_refresh_time
    now = datetime.datetime.now()
    if last_refresh_time and (now - last_refresh_time).total_seconds() < 40:
        signal_label.config(text="⏱️ Please wait 40s between refreshes.")
        return
    last_refresh_time = now
    try:
        signal, entry, tp1, tp2, tp3, sl = signal_engine()
        signal_label.config(text=signal)
        level_txt = f"Entry: {entry}"
        if tp1:
            level_txt += f"\nTP1: {tp1}\nTP2: {tp2}\nTP3: {tp3}\nStop Loss: {sl}"
        levels_label.config(text=level_txt)

        if "Buy" in signal or "Sell" in signal or "⚠️" in signal:
            log_signal(signal, entry, tp1, tp2, tp3, sl)

        df = fetch_data()
        fig, ax = plt.subplots(figsize=(6, 3))
        ax.plot(df.index, df['close'], label='Close', color='white', linewidth=2)
        ax.axhline(entry, color='dodgerblue', linestyle='--', label='Entry')
        if tp1: ax.axhline(tp1, color='lime', linestyle='--', label='TP1')
        if tp2: ax.axhline(tp2, color='springgreen', linestyle='--', label='TP2')
        if tp3: ax.axhline(tp3, color='darkgreen', linestyle='--', label='TP3')
        if sl: ax.axhline(sl, color='red', linestyle='--', label='Stop')
        ax.legend()
        ax.set_title("XAUUSD Chart", color='white')
        ax.set_facecolor("#121212")
        fig.patch.set_facecolor("#121212")
        ax.tick_params(colors='white')

        for widget in chart_frame.winfo_children():
            widget.destroy()
        chart = FigureCanvasTkAgg(fig, master=chart_frame)
        chart.draw()
        chart.get_tk_widget().pack()
    except Exception as e:
        signal_label.config(text=f"Error: {e}")
        levels_label.config(text="")

# === GUI SETUP ===
root = tk.Tk()
root.title("XAUUSD Signal Bot")
root.configure(bg="#121212", padx=20, pady=20)

style = ttk.Style()
style.theme_use("default")
style.configure("TLabel", background="#121212", foreground="#FFD700", font=("Segoe UI", 12))
style.configure("TButton", background="#FFD700", foreground="#121212", font=("Segoe UI", 10), padding=6)
style.map("TButton", background=[('active', '#e6c200')], foreground=[('active', '#000000')])

ttk.Label(root, text="💠 XAUUSD Signal Bot", font=("Segoe UI", 22, "bold")).pack(pady=10)
signal_label = ttk.Label(root, text="", font=("Segoe UI", 16))
signal_label.pack()

levels_label = ttk.Label(root, text="", font=("Segoe UI", 12))
levels_label.pack(pady=5)

ttk.Button(root, text="📊 Analyze Market", command=update_analysis).pack(pady=10)

chart_frame = ttk.Frame(root)
chart_frame.pack()

# === WATERMARK ===
watermark = tk.Label(root, text="STEEL TECH 1", font=("Segoe UI", 10, "bold"),
                     bg="#121212", fg="#444444")
watermark.place(relx=1.0, rely=1.0, anchor='se', x=-10, y=-10)

root.mainloop()
