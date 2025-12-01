# ====================== #
# 1. IMPORTS & SETTINGS  #
# ====================== #

import tkinter as tk
from tkinter import ttk
import pandas as pd
import requests
import datetime
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

API_KEY = "25e62fb28d1040f1be47bf4f4c5d4138"
BASE_URL = "https://api.twelvedata.com/time_series"
symbols = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "XAU/USD"]
last_refresh_time = None

# Config per pair
PAIR_CONFIG = {
    "EUR/USD": {"atr_mult": 2.0, "min_score": 60},
    "USD/JPY": {"atr_mult": 1.8, "min_score": 55},
    "AUD/USD": {"atr_mult": 2.0, "min_score": 60},
    "GBP/USD": {"atr_mult": 2.2, "min_score": 60},
    "XAU/USD": {"atr_mult": 3.0, "min_score": 65}
}


# ====================== #
# 2. FETCH DATA (1m)     #
# ====================== #

def fetch_data(symbol, interval="1min"):
    params = {
        "symbol": symbol,
        "interval": interval,
        "apikey": API_KEY,
        "outputsize": 100,
        "format": "JSON"
    }
    response = requests.get(BASE_URL, params=params)
    data = response.json()
    if "values" not in data:
        raise ValueError("Data fetch failed.")
    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    df.set_index("datetime", inplace=True)
    df = df.apply(pd.to_numeric, errors="coerce").dropna().sort_index()
    return df


# ============================ #
# 3. INDICATORS & SMC LOGIC    #
# ============================ #

def calculate_indicators(df):
    df['EMA_8'] = df['close'].ewm(span=8).mean()
    df['EMA_21'] = df['close'].ewm(span=21).mean()
    df['MACD'] = df['close'].ewm(span=12).mean() - df['close'].ewm(span=26).mean()
    df['MACD_signal'] = df['MACD'].ewm(span=9).mean()
    df['MACD_hist'] = df['MACD'] - df['MACD_signal']

    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss
    df['RSI'] = 100 - (100 / (1 + rs))

    tr1 = df['high'] - df['low']
    tr2 = abs(df['high'] - df['close'].shift())
    tr3 = abs(df['low'] - df['close'].shift())
    df['TR'] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df['ATR'] = df['TR'].rolling(14).mean()

    return df


def detect_smc(df):
    df['BOS'] = (df['high'] > df['high'].shift(1)) & (df['low'] > df['low'].shift(1))
    df['FVG'] = (df['low'].shift(1) > df['high'].shift(2)) | (df['high'].shift(1) < df['low'].shift(2))
    df['Liquidity_Sweep'] = (df['low'] < df['low'].rolling(20).min().shift(1)) | (df['high'] > df['high'].rolling(20).max().shift(1))
    df['OrderBlock'] = (
        (df['open'].shift(1) > df['close'].shift(1)) & 
        (df['close'] > df['open']) &
        (df['low'] > df['low'].shift(1))
    )
    df['StructureBias'] = df['EMA_8'] > df['EMA_21']
    return df


# ============================ #
# 4. SIGNAL ENGINE + CONF SCORE#
# ============================ #

def signal_engine(symbol):
    df = fetch_data(symbol)
    df = calculate_indicators(df)
    df = detect_smc(df)
    last = df.iloc[-1]
    confirmed_entry = df.iloc[-2]['close']  # last closed 1m candle
    atr = df['ATR'].iloc[-1]

    score = 0
    if last['BOS']: score += 1
    if last['FVG']: score += 1
    if last['Liquidity_Sweep']: score += 1
    if last['OrderBlock']: score += 1
    if last['StructureBias']: score += 1
    if last['MACD_hist'] > 0: score += 1
    if last['RSI'] > 55: score += 1
    if last['EMA_8'] > last['EMA_21']: score += 1

    config = PAIR_CONFIG.get(symbol, {"atr_mult": 2.0, "min_score": 60})
    atr_mult = config["atr_mult"]
    min_score = config["min_score"]

    now = datetime.datetime.now()
    expires = now + datetime.timedelta(minutes=15)

    if score >= min_score:
        sl = round(confirmed_entry - atr * atr_mult, 4)
        tp1 = round(confirmed_entry + (confirmed_entry - sl) * 1.5, 4)
        tp2 = round(confirmed_entry + (confirmed_entry - sl) * 2, 4)
        tp3 = round(confirmed_entry + (confirmed_entry - sl) * 3, 4)
        return {
            "type": "Strong Buy",
            "entry": round(confirmed_entry, 4),
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "sl": sl,
            "score": score,
            "timestamp": now,
            "expires": expires
        }
    else:
        return {
            "type": "No Clear Signal",
            "entry": round(confirmed_entry, 4),
            "tp1": None,
            "tp2": None,
            "tp3": None,
            "sl": None,
            "score": score,
            "timestamp": now,
            "expires": expires
        }


# ======================= #
# 5. GUI FUNCTION HANDLER #
# ======================= #

def update_analysis():
    global last_refresh_time
    now = datetime.datetime.now()
    if last_refresh_time and (now - last_refresh_time).total_seconds() < 30:
        signal_label.config(text="Wait 30s before refreshing.")
        return
    last_refresh_time = now

    symbol = symbol_var.get()
    try:
        result = signal_engine(symbol)
        signal = result["type"]
        entry = result["entry"]
        score = result["score"]
        timestamp = result["timestamp"].strftime("%H:%M:%S")
        expiry = result["expires"].strftime("%H:%M:%S")

        signal_label.config(text=f"{symbol}: {signal} | Score: {score} | {timestamp} → {expiry}")
        txt = f"Entry: {entry}"
        if result["tp1"]:
            txt += f"\nTP1: {result['tp1']}\nTP2: {result['tp2']}\nTP3: {result['tp3']}\nSL: {result['sl']}"
        levels_label.config(text=txt)

        df = fetch_data(symbol)
        fig, ax = plt.subplots(figsize=(6, 3))
        ax.plot(df.index, df["close"], label="Price", color="white")
        ax.axhline(entry, color='red', linestyle='--', label='Entry')
        if result["tp1"]: ax.axhline(result["tp1"], color='lime', linestyle='--', label='TP1')
        if result["tp2"]: ax.axhline(result["tp2"], color='green', linestyle='--', label='TP2')
        if result["tp3"]: ax.axhline(result["tp3"], color='darkgreen', linestyle='--', label='TP3')
        if result["sl"]: ax.axhline(result["sl"], color='white', linestyle='--', label='SL')
        ax.legend()
        ax.set_facecolor("black")
        fig.patch.set_facecolor("black")
        ax.tick_params(colors="white")
        ax.set_title(f"{symbol} Chart", color="white")

        for widget in chart_frame.winfo_children():
            widget.destroy()
        chart = FigureCanvasTkAgg(fig, master=chart_frame)
        chart.draw()
        chart.get_tk_widget().pack()

    except Exception as e:
        signal_label.config(text=f"Error: {e}")
        levels_label.config(text="")


# ============ #
# 6. GUI SETUP #
# ============ #

root = tk.Tk()
root.title("STEEL TECH - Adaptive Signal Bot")
root.configure(bg="black", padx=20, pady=20)

style = ttk.Style()
style.theme_use("default")
style.configure("TLabel", background="black", foreground="#FF4444", font=("Segoe UI", 12, "bold"))
style.configure("TButton", background="#FF4444", foreground="black", font=("Segoe UI", 10, "bold"))
style.map("TButton", background=[('active', '#FF0000')], foreground=[('active', 'white')])

ttk.Label(root, text="STEEL TECH MULTISIGNAL BOT", font=("Segoe UI", 20, "bold")).pack(pady=10)
symbol_var = tk.StringVar(value=symbols[0])
ttk.OptionMenu(root, symbol_var, symbols[0], *symbols).pack()

signal_label = ttk.Label(root, text="", font=("Segoe UI", 14, "bold"))
signal_label.pack(pady=5)
levels_label = ttk.Label(root, text="", font=("Segoe UI", 12))
levels_label.pack(pady=5)

ttk.Button(root, text="Analyze Market", command=update_analysis).pack(pady=10)
chart_frame = ttk.Frame(root)
chart_frame.pack()

tk.Label(root, text="STEEL TECH", font=("Segoe UI", 10, "bold"),
         bg="black", fg="#444444").place(relx=1.0, rely=1.0, anchor='se', x=-10, y=-10)

root.mainloop()
