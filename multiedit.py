import tkinter as tk
from tkinter import ttk
import pandas as pd
import requests
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import datetime

API_KEY = "25e62fb28d1040f1be47bf4f4c5d4138"
BASE_URL = "https://api.twelvedata.com/time_series"
symbols = ["EUR/USD", "USD/JPY", "AUD/USD", "GBP/USD", "XAU/USD"]
last_refresh_time = None

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
        raise ValueError("Data fetch failed")
    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    df.set_index("datetime", inplace=True)
    df = df.apply(pd.to_numeric, errors="coerce")
    df.dropna(inplace=True)
    df.sort_index(inplace=True)
    return df

def calculate_indicators(df):
    df['EMA_short'] = df['close'].ewm(span=8).mean()
    df['EMA_long'] = df['close'].ewm(span=21).mean()

    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=13).mean()
    avg_loss = loss.rolling(window=13).mean()
    rs = avg_gain / avg_loss
    df['RSI'] = 100 - (100 / (1 + rs))

    df['MACD'] = df['close'].ewm(span=12).mean() - df['close'].ewm(span=26).mean()
    df['MACD_signal'] = df['MACD'].ewm(span=9).mean()
    df['MACD_hist'] = df['MACD'] - df['MACD_signal']

    tr1 = df['high'] - df['low']
    tr2 = (df['high'] - df['close'].shift()).abs()
    tr3 = (df['low'] - df['close'].shift()).abs()
    df['TR'] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df['ATR'] = df['TR'].rolling(window=14).mean()

    df['Breakout'] = (df['high'] > df['high'].rolling(20).max().shift(1)) | \
                     (df['low'] < df['low'].rolling(20).min().shift(1))

    ma20 = df['close'].rolling(20).mean()
    std20 = df['close'].rolling(20).std()
    df['BB_upper'] = ma20 + 2 * std20
    df['BB_lower'] = ma20 - 2 * std20

    low14 = df['low'].rolling(14).min()
    high14 = df['high'].rolling(14).max()
    df['%K'] = 100 * ((df['close'] - low14) / (high14 - low14))
    df['%D'] = df['%K'].rolling(3).mean()

    return df

def multi_timeframe_confirm(symbol):
    df_5 = calculate_indicators(fetch_data(symbol, "5min"))
    df_15 = calculate_indicators(fetch_data(symbol, "15min"))
    df_1h = calculate_indicators(fetch_data(symbol, "1h"))

    def assess(df):
        last = df.iloc[-1]
        real_body = abs(last['close'] - last['open'])
        wick_range = last['high'] - last['low']
        if wick_range == 0 or real_body / wick_range < 0.3:
            return 0
        if last['ATR'] > last['close'] * 0.04:
            return 0
        ma_score = 1 if last['EMA_short'] > last['EMA_long'] else -1
        rsi_score = 1 if last['RSI'] > 55 else -1 if last['RSI'] < 45 else 0
        bb_score = 1 if last['close'] < last['BB_lower'] else -1 if last['close'] > last['BB_upper'] else 0
        stoch_score = 1 if last['%K'] < 20 else -1 if last['%K'] > 80 else 0
        candle = 1 if last['close'] > last['open'] else -1
        breakout = 1 if last['Breakout'] else 0
        macd_score = 1 if last['MACD_hist'] > 0 else -1
        return ma_score + rsi_score + candle + breakout + macd_score + bb_score + stoch_score

    score = assess(df_5) + assess(df_15) + assess(df_1h)
    return score, df_15

def signal_engine(symbol):
    now = datetime.datetime.now()
    expires = now + datetime.timedelta(minutes=15)

    score, df = multi_timeframe_confirm(symbol)
    last = df.iloc[-1]
    entry = last['close']

    recent_high = df['high'].rolling(20).max().shift(1).iloc[-1]
    recent_low = df['low'].rolling(20).min().shift(1).iloc[-1]

    if score >= 3 and entry > recent_high and entry <= recent_high * 1.01:
        sl = round(recent_low, 4)
        tp1 = round(entry + (entry - sl) * 2, 4)
        tp2 = round(entry + (entry - sl) * 3, 4)
        tp3 = round(entry + (entry - sl) * 4, 4)
        rrr = round((tp1 - entry) / (entry - sl), 2)
        return {
            "type": "Strong Buy",
            "entry": round(entry, 4),
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "sl": sl,
            "rrr": rrr,
            "timestamp": now,
            "expires": expires
        }

    elif score <= -3 and entry < recent_low and entry >= recent_low * 0.99:
        sl = round(recent_high, 4)
        tp1 = round(entry - (sl - entry) * 2, 4)
        tp2 = round(entry - (sl - entry) * 3, 4)
        tp3 = round(entry - (sl - entry) * 4, 4)
        rrr = round((entry - tp1) / (sl - entry), 2)
        return {
            "type": "Strong Sell",
            "entry": round(entry, 4),
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "sl": sl,
            "rrr": rrr,
            "timestamp": now,
            "expires": expires
        }

    elif score == 2:
        return {"type": "Weak Buy", "entry": round(entry, 4), "tp1": None, "tp2": None, "tp3": None, "sl": None, "timestamp": now, "expires": expires}
    elif score == -2:
        return {"type": "Weak Sell", "entry": round(entry, 4), "tp1": None, "tp2": None, "tp3": None, "sl": None, "timestamp": now, "expires": expires}
    else:
        return {"type": "No Clear Signal", "entry": round(entry, 4), "tp1": None, "tp2": None, "tp3": None, "sl": None, "timestamp": now, "expires": expires}

def log_signal(symbol, signal_data):
    row = {
        "datetime": signal_data["timestamp"].strftime("%Y-%m-%d %H:%M:%S"),
        "expires": signal_data["expires"].strftime("%H:%M:%S"),
        "symbol": symbol,
        "signal": signal_data["type"],
        "entry": signal_data["entry"],
        "tp1": signal_data["tp1"],
        "tp2": signal_data["tp2"],
        "tp3": signal_data["tp3"],
        "sl": signal_data["sl"],
        "rrr": signal_data.get("rrr")
    }
    df = pd.DataFrame([row])
    try:
        with open("signal_log.csv", "a", encoding="utf-8") as f:
            df.to_csv(f, header=f.tell() == 0, index=False)
    except Exception as e:
        print(f"Log error: {e}")

def update_analysis():
    global last_refresh_time
    now = datetime.datetime.now()
    if last_refresh_time and (now - last_refresh_time).total_seconds() < 40:
        signal_label.config(text="Please wait 40s between refreshes.")
        return
    last_refresh_time = now
    symbol = symbol_var.get()
    try:
        signal_data = signal_engine(symbol)
        signal = signal_data["type"]
        entry = signal_data["entry"]
        tp1 = signal_data["tp1"]
        tp2 = signal_data["tp2"]
        tp3 = signal_data["tp3"]
        sl = signal_data["sl"]
        timestamp = signal_data["timestamp"].strftime("%H:%M:%S")
        expiry = signal_data["expires"].strftime("%H:%M:%S")
        rrr = signal_data.get("rrr")

        signal_label.config(text=f"{symbol}: {signal} | {timestamp} → {expiry}")
        txt = f"Entry: {entry}"
        if tp1:
            txt += f"\nTP1: {tp1}\nTP2: {tp2}\nTP3: {tp3}\nSL: {sl}"
        if rrr:
            txt += f"\nRRR: {rrr} : 1"
        levels_label.config(text=txt)

        if "Buy" in signal or "Sell" in signal:
            log_signal(symbol, signal_data)

        df = fetch_data(symbol)
        fig, ax = plt.subplots(figsize=(6, 3))
        ax.plot(df.index, df["close"], label="Close", color="white", linewidth=2)
        ax.axhline(entry, color='red', linestyle='--', label='Entry')
        if tp1: ax.axhline(tp1, color='lime', linestyle='--', label='TP1')
        if tp2: ax.axhline(tp2, color='green', linestyle='--', label='TP2')
        if tp3: ax.axhline(tp3, color='darkgreen', linestyle='--', label='TP3')
        if sl: ax.axhline(sl, color='white', linestyle='--', label='SL')
        ax.legend()
        ax.set_title(f"{symbol} Chart", color="white")
        ax.set_facecolor("#000000")
        fig.patch.set_facecolor("#000000")
        ax.tick_params(colors='white')

        for widget in chart_frame.winfo_children():
            widget.destroy()
        chart = FigureCanvasTkAgg(fig, master=chart_frame)
        chart.draw()
        chart.get_tk_widget().pack()
    except Exception as e:
        signal_label.config(text=f"Error: {e}")
        levels_label.config(text="")

# GUI Setup
root = tk.Tk()
root.title("Multi-Symbol Signal Bot")
root.configure(bg="#000000", padx=20, pady=20)

style = ttk.Style()
style.theme_use("default")
style.configure("TLabel", background="#000000", foreground="#FF4444", font=("Segoe UI", 12, "bold"))
style.configure("TButton", background="#FF4444", foreground="#000000", font=("Segoe UI", 10, "bold"), padding=6)
style.map("TButton", background=[('active', '#FF0000')], foreground=[('active', '#FFFFFF')])

ttk.Label(root, text="Multi-Symbol Signal Bot", font=("Segoe UI", 22, "bold")).pack(pady=10)
symbol_var = tk.StringVar(value="EUR/USD")
ttk.OptionMenu(root, symbol_var, "EUR/USD", *symbols).pack()

signal_label = ttk.Label(root, text="", font=("Segoe UI", 14, "bold"))
signal_label.pack(pady=5)
levels_label = ttk.Label(root, text="", font=("Segoe UI", 12))
levels_label.pack(pady=5)

ttk.Button(root, text="Analyze Market", command=update_analysis).pack(pady=10)
chart_frame = ttk.Frame(root)
chart_frame.pack()

tk.Label(root, text="STEEL TECH 3", font=("Segoe UI", 10, "bold"),
         bg="#000000", fg="#444444").place(relx=1.0, rely=1.0, anchor='se', x=-10, y=-10)

root.mainloop()