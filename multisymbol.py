import tkinter as tk
from tkinter import ttk
import pandas as pd
import requests
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import datetime

# === CONFIG ===
API_KEY = "25e62fb28d1040f1be47bf4f4c5d4138"
BASE_URL = "https://api.twelvedata.com/time_series"
symbols = ["EUR/USD", "USD/JPY", "AUD/USD", "GBP/USD", "XAU/USD"]
last_refresh_time = None

# === FETCH DATA ===
def fetch_data(symbol, interval="15min"):
    params = {
        "symbol": symbol,
        "interval": interval,
        "apikey": API_KEY,
        "outputsize": 50,
        "format": "JSON"
    }
    response = requests.get(BASE_URL, params=params)
    data = response.json()
    if "values" not in data:
        raise ValueError("Error fetching data")
    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    df.set_index("datetime", inplace=True)
    df = df.apply(pd.to_numeric, errors="coerce")
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
    df['Breakout'] = (df['high'] > df['high'].rolling(20).max().shift(1)) | (df['low'] < df['low'].rolling(20).min().shift(1))
    return df

# === MULTI-TIMEFRAME CONFIRM ===
def multi_timeframe_confirm(symbol):
    df_15 = calculate_indicators(fetch_data(symbol, "15min"))
    df_1h = calculate_indicators(fetch_data(symbol, "1h"))
    df_5m = calculate_indicators(fetch_data(symbol, "5min"))

    def assess(df):
        last = df.iloc[-1]
        ma_score = 1 if last['MA_short'] > last['MA_long'] else -1
        rsi_score = 1 if last['RSI'] > 50 else -1
        candle = 1 if last['close'] > last['open'] else -1
        breakout = 1 if last['Breakout'] else 0
        return ma_score + rsi_score + candle + breakout

    score = assess(df_5m) + assess(df_15) + assess(df_1h)
    return score, df_15

# === SIGNAL ENGINE ===
def signal_engine(symbol):
    score, df = multi_timeframe_confirm(symbol)
    last = df.iloc[-1]
    entry = last['close']
    atr = last['ATR']
    sl_distance = max(entry * 0.004, atr)

    if score >= 3:
        sl = round(entry - sl_distance, 4)
        return "🟢 Strong Buy", round(entry, 4), round(entry + sl_distance, 4), round(entry + sl_distance*1.5, 4), round(entry + sl_distance*2, 4), sl
    elif score <= -3:
        sl = round(entry + sl_distance, 4)
        return "🔴 Strong Sell", round(entry, 4), round(entry - sl_distance, 4), round(entry - sl_distance*1.5, 4), round(entry - sl_distance*2, 4), sl
    elif score == 2:
        return "🟡 Weak Buy", round(entry, 4), None, None, None, None
    elif score == -2:
        return "🟠 Weak Sell", round(entry, 4), None, None, None, None
    else:
        return "⚪ No Clear Signal", round(entry, 4), None, None, None, None

# === GUI ===
def update_analysis():
    global last_refresh_time
    now = datetime.datetime.now()
    if last_refresh_time and (now - last_refresh_time).total_seconds() < 60:
        signal_label.config(text="⏱️ Wait 60s between checks")
        return
    last_refresh_time = now
    symbol = symbol_var.get()
    try:
        signal, entry, tp1, tp2, tp3, sl = signal_engine(symbol)
        signal_label.config(text=f"{symbol}: {signal}")
        level_txt = f"Entry: {entry}"
        if tp1: level_txt += f"\nTP1: {tp1}\nTP2: {tp2}\nTP3: {tp3}\nSL: {sl}"
        levels_label.config(text=level_txt)

        df = fetch_data(symbol)
        fig, ax = plt.subplots(figsize=(6,3))
        ax.plot(df.index, df["close"], label="Close", color="#00FFFF", linewidth=2)
        ax.axhline(entry, color='#FFD700', linestyle='--', label='Entry')
        if tp1: ax.axhline(tp1, color='#00FF00', linestyle='--', label='TP1')
        if tp2: ax.axhline(tp2, color='#00AA00', linestyle='--', label='TP2')
        if tp3: ax.axhline(tp3, color='#007700', linestyle='--', label='TP3')
        if sl:  ax.axhline(sl,  color='#FF0000', linestyle='--', label='Stop')
        ax.set_title(f"{symbol} Chart", color="white", fontweight='bold')
        ax.set_facecolor("#000000")
        fig.patch.set_facecolor("#000000")
        ax.tick_params(colors='white')
        ax.legend()

        for widget in chart_frame.winfo_children():
            widget.destroy()
        canvas = FigureCanvasTkAgg(fig, master=chart_frame)
        canvas.draw()
        canvas.get_tk_widget().pack()
    except Exception as e:
        signal_label.config(text=f"Error: {e}")
        levels_label.config(text="")

# === GUI SETUP ===
root = tk.Tk()
root.title("🧨 Boss Signal Bot")
root.configure(bg="#000000", padx=20, pady=20)

style = ttk.Style()
style.theme_use("default")
style.configure("TLabel", background="#000000", foreground="#FF4444", font=("Segoe UI", 12, "bold"))
style.configure("TButton", background="#FF4444", foreground="#000000", font=("Segoe UI", 11, "bold"), padding=6)
style.map("TButton", background=[('active', '#FF0000')], foreground=[('active', '#FFFFFF')])

ttk.Label(root, text="Multi-Symbol Signal Bot", font=("Segoe UI", 22, "bold")).pack(pady=10)
symbol_var = tk.StringVar(value="EUR/USD")
ttk.OptionMenu(root, symbol_var, "EUR/USD", *symbols).pack()

signal_label = ttk.Label(root, text="", font=("Segoe UI", 16, "bold"))
signal_label.pack()
levels_label = ttk.Label(root, text="", font=("Segoe UI", 12, "bold"))
levels_label.pack(pady=5)
ttk.Button(root, text="📊 Analyze Market", command=update_analysis).pack(pady=10)

chart_frame = ttk.Frame(root)
chart_frame.pack()
watermark = tk.Label(root, text="STEEL TECH 3", font=("Segoe UI", 10, "bold"),
                     bg="#000000", fg="#444444")
watermark.place(relx=1.0, rely=1.0, anchor='se', x=-10, y=-10)

root.mainloop()
