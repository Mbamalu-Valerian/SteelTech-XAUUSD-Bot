# ====================== #
# 1. IMPORTS & SETTINGS  #
# ====================== #
import tkinter as tk
from tkinter import ttk
import pandas as pd
import datetime
import requests
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

# ======================= #
# 2. BOT CONFIGURATION    #
# ======================= #
symbols = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "XAU/USD"]
PAIR_CONFIG = {
    "EUR/USD": {"atr_mult": 1.5, "rsi_thresh": 50, "min_score": 5},
    "GBP/USD": {"atr_mult": 1.5, "rsi_thresh": 50, "min_score": 5},
    "USD/JPY": {"atr_mult": 1.5, "rsi_thresh": 50, "min_score": 5},
    "AUD/USD": {"atr_mult": 1.5, "rsi_thresh": 50, "min_score": 5},
    "XAU/USD": {"atr_mult": 1.5, "rsi_thresh": 50, "min_score": 5}
}

API_KEY = "25e62fb28d1040f1be47bf4f4c5d4138"
BASE_URL = "https://api.twelvedata.com/time_series"

# ======================= #
# 3. TECHNICAL FUNCTIONS  #
# ======================= #
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

def apply_indicators(df):
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

def apply_smc(df):
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

def detect_price_action(df):
    df['Candle_Body'] = abs(df['close'] - df['open'])
    df['Upper_Wick'] = df['high'] - df[['close', 'open']].max(axis=1)
    df['Lower_Wick'] = df[['close', 'open']].min(axis=1) - df['low']
    df['Pin_Bar'] = (
        ((df['Upper_Wick'] > 2 * df['Candle_Body']) | (df['Lower_Wick'] > 2 * df['Candle_Body'])) &
        (df['Candle_Body'] < (df['high'] - df['low']) * 0.4)
    )
    df['Bullish_Engulfing'] = (
        (df['close'].shift(1) < df['open'].shift(1)) &
        (df['close'] > df['open']) &
        (df['open'] < df['close'].shift(1)) &
        (df['close'] > df['open'].shift(1))
    )
    df['Bearish_Engulfing'] = (
        (df['close'].shift(1) > df['open'].shift(1)) &
        (df['close'] < df['open']) &
        (df['open'] > df['close'].shift(1)) &
        (df['close'] < df['open'].shift(1))
    )
    df['PA_Valid'] = df['Pin_Bar'] | df['Bullish_Engulfing'] | df['Bearish_Engulfing']
    return df

def generate_signals(df, atr_mult, rsi_thresh, min_score):
    df = apply_indicators(df)
    df = apply_smc(df)
    df = detect_price_action(df)
    last = df.iloc[-2]
    score = sum([
        last['BOS'], last['FVG'], last['Liquidity_Sweep'],
        last['OrderBlock'], last['StructureBias'],
        last['MACD_hist'] > 0, last['RSI'] > rsi_thresh,
        last['EMA_8'] > last['EMA_21']
    ])
    if score >= min_score and last['PA_Valid']:
        entry = last['close']
        sl = entry - last['ATR'] * atr_mult
        tp = entry + (entry - sl) * 1.0
        return True, entry, sl, tp, score
    return False, last['close'], None, None, score

# ======================= #
# 4. GUI + MAIN EXECUTION#
# ======================= #
root = tk.Tk()
root.title("STEEL TECH - Adaptive Signal Bot")
root.configure(bg="black", padx=20, pady=20)

style = ttk.Style()
style.theme_use("default")
style.configure("TLabel", background="black", foreground="#FF4444", font=("Segoe UI", 12, "bold"))
style.configure("TButton", background="#FF4444", foreground="black", font=("Segoe UI", 10, "bold"))
style.map("TButton", background=[('active', '#FF0000')], foreground=[('active', 'white')])

symbol_var = tk.StringVar(value=symbols[0])

signal_label = ttk.Label(root, text="")
signal_label.pack(pady=5)
levels_label = ttk.Label(root, text="")
levels_label.pack(pady=5)

chart_frame = ttk.Frame(root)
chart_frame.pack()

def update_analysis():
    symbol = symbol_var.get()
    df = fetch_data(symbol)
    conf = PAIR_CONFIG[symbol]
    signal, entry, sl, tp, score = generate_signals(df, **conf)
    if signal:
        signal_label.config(text=f"{symbol} SIGNAL ✅ | Score: {score}")
        levels_label.config(text=f"ENTRY: {entry}\nTP: {tp}\nSL: {sl}")
    else:
        signal_label.config(text=f"{symbol}: No clear signal ❌ | Score: {score}")
        levels_label.config(text="")
    # Chart
    fig, ax = plt.subplots(figsize=(6, 3))
    ax.plot(df.index[-100:], df['close'][-100:], label="Price", color="white")
    if signal:
        ax.axhline(entry, color='red', linestyle='--', label='Entry')
        ax.axhline(tp, color='green', linestyle='--', label='TP')
        ax.axhline(sl, color='white', linestyle='--', label='SL')
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

# GUI Layout
ttk.Label(root, text="MULTISIGNAL BOT", font=("Segoe UI", 20, "bold")).pack(pady=10)
ttk.OptionMenu(root, symbol_var, symbols[0], *symbols).pack()
ttk.Button(root, text="Analyze Market", command=update_analysis).pack(pady=10)
tk.Label(root, text="STEEL TECH", font=("Segoe UI", 10, "bold"), bg="black", fg="#444444").place(relx=1.0, rely=1.0, anchor='se', x=-10, y=-10)

root.mainloop()