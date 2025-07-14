# ====================== #
# 1. IMPORTS & SETTINGS  #
# ====================== #
import tkinter as tk
from tkinter import ttk
import pandas as pd
import requests
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

# ======================= #
# 2. BOT CONFIGURATION    #
# ======================= #
symbols = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "XAU/USD"]
PAIR_CONFIG = {
    "EUR/USD": {"atr_mult": 1.0, "rsi_thresh": 50, "min_score": 3},
    "GBP/USD": {"atr_mult": 1.2, "rsi_thresh": 48, "min_score": 3},
    "USD/JPY": {"atr_mult": 1.0, "rsi_thresh": 47, "min_score": 3},
    "AUD/USD": {"atr_mult": 1.0, "rsi_thresh": 49, "min_score": 3},
    "XAU/USD": {"atr_mult": 1.5, "rsi_thresh": 52, "min_score": 4}
}

API_KEY = "25e62fb28d1040f1be47bf4f4c5d4138"
BASE_URL = "https://api.twelvedata.com/time_series"

# ============================ #
# 3. DATA & INDICATOR LOGIC    #
# ============================ #
def fetch_data(symbol, interval="15min"):
    resp = requests.get(BASE_URL, params={
        "symbol": symbol, "interval": interval,
        "apikey": API_KEY, "outputsize": 100, "format": "JSON"
    })
    data = resp.json()
    if "values" not in data:
        raise ValueError("Failed to fetch data for " + symbol)
    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    df.set_index("datetime", inplace=True)
    return df.astype(float).sort_index()

def apply_indicators(df):
    df['EMA_8'] = df['close'].ewm(span=8).mean()
    df['EMA_21'] = df['close'].ewm(span=21).mean()
    macd = df['close'].ewm(span=12).mean() - df['close'].ewm(span=26).mean()
    df['MACD_hist'] = macd - macd.ewm(span=9).mean()
    delta = df['close'].diff()
    gain = delta.clip(lower=0); loss = -delta.clip(upper=0)
    df['RSI'] = 100 - 100/(1 + gain.rolling(14).mean()/loss.rolling(14).mean())
    tr1 = df['high'] - df['low']
    tr2 = (df['high'] - df['close'].shift()).abs()
    tr3 = (df['low'] - df['close'].shift()).abs()
    df['ATR'] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1).rolling(14).mean()
    return df

def apply_smc(df):
    df['BOS'] = (df['high'] > df['high'].shift(1)) & (df['low'] > df['low'].shift(1))
    df['FVG'] = (df['low'].shift(1) > df['high'].shift(2)) | (df['high'].shift(1) < df['low'].shift(2))
    df['Liquidity_Sweep'] = (
        (df['low'] < df['low'].rolling(20).min().shift(1)) |
        (df['high'] > df['high'].rolling(20).max().shift(1))
    )
    df['OrderBlock'] = (
        (df['open'].shift(1) > df['close'].shift(1)) &
        (df['close'] > df['open']) &
        (df['low'] > df['low'].shift(1))
    )
    df['StructureBias'] = df['EMA_8'] > df['EMA_21']
    return df

def detect_price_action(df):
    body = (df['close'] - df['open']).abs()
    upper = df['high'] - df[['close','open']].max(axis=1)
    lower = df[['close','open']].min(axis=1) - df['low']
    df['Pin_Bar'] = ((upper>2*body)|(lower>2*body)) & (body<(df['high']-df['low'])*0.4)
    df['Bullish_Engulfing'] = (
        (df['close'].shift(1)<df['open'].shift(1)) &
        (df['close']>df['open']) &
        (df['open']<df['close'].shift(1)) &
        (df['close']>df['open'].shift(1))
    )
    df['Bearish_Engulfing'] = (
        (df['close'].shift(1)>df['open'].shift(1)) &
        (df['close']<df['open']) &
        (df['open']>df['close'].shift(1)) &
        (df['close']<df['open'].shift(1))
    )
    df['PA_Valid'] = df['Pin_Bar']|df['Bullish_Engulfing']|df['Bearish_Engulfing']
    return df

def generate_signals(df, atr_mult, rsi_thresh, min_score):
    df = apply_indicators(df)
    df = apply_smc(df)
    df = detect_price_action(df)
    last = df.iloc[-2]

    score = sum([
        last['BOS'], last['FVG'], last['Liquidity_Sweep'],
        last['OrderBlock'], last['StructureBias'],
        last['MACD_hist'] > 0, last['RSI'] > rsi_thresh
    ])
    if last['PA_Valid']:
        score += 1

    if last['Bullish_Engulfing'] or (last['Pin_Bar'] and last['close'] > last['open']):
        direction = "BUY"
    elif last['Bearish_Engulfing'] or (last['Pin_Bar'] and last['close'] < last['open']):
        direction = "SELL"
    elif last['StructureBias']:
        direction = "BUY"
    else:
        direction = "SELL"

    if score >= min_score:
        entry = last['close']
        if direction == "BUY":
            sl = entry - df['ATR'].iloc[-2] * atr_mult
            tp1 = entry + (entry - sl) * 1.0
            tp2 = entry + (entry - sl) * 2.0
            tp3 = entry + (entry - sl) * 3.0
        else:
            sl = entry + df['ATR'].iloc[-2] * atr_mult
            tp1 = entry - (sl - entry) * 1.0
            tp2 = entry - (sl - entry) * 2.0
            tp3 = entry - (sl - entry) * 3.0

        return True, entry, sl, tp1, tp2, tp3, score, direction

    return False, last['close'], None, None, None, None, score, None
    # ======================= #
# 4. GUI IMPLEMENTATION   #
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
signal_label = ttk.Label(root, text="", font=("Segoe UI", 14, "bold"))
signal_label.pack(pady=5)
levels_label = ttk.Label(root, text="", font=("Segoe UI", 12))
levels_label.pack(pady=5)
chart_frame = ttk.Frame(root)
chart_frame.pack()

def update_analysis():
    symbol = symbol_var.get()
    df = fetch_data(symbol)
    conf = PAIR_CONFIG[symbol]
    signal, entry, sl, tp1, tp2, tp3, score, direction = generate_signals(df, **conf)

    if signal:
        signal_label.config(text=f"{symbol} {direction} SIGNAL ✅ | Score: {score}")
        levels_label.config(text=f"Entry: {round(entry, 4)}\nTP1: {round(tp1, 4)}\nTP2: {round(tp2, 4)}\nTP3: {round(tp3, 4)}\nStop Loss: {round(sl, 4)}")
    else:
        signal_label.config(text=f"{symbol}: No clear signal ❌ | Score: {score}")
        levels_label.config(text="")

    fig, ax = plt.subplots(figsize=(6, 3))
    ax.plot(df.index[-100:], df['close'][-100:], label="Price", color="white")
    if signal:
        ax.axhline(entry, color='blue', linestyle='--', label='Entry')
        ax.axhline(tp1, color='green', linestyle='--', label='TP1')
        ax.axhline(tp2, color='lime', linestyle='--', label='TP2')
        ax.axhline(tp3, color='darkgreen', linestyle='--', label='TP3')
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

ttk.Label(root, text="MULTISIGNAL BOT", font=("Segoe UI", 20, "bold")).pack(pady=10)
ttk.OptionMenu(root, symbol_var, symbols[0], *symbols).pack()
ttk.Button(root, text="Analyze Market", command=update_analysis).pack(pady=10)
tk.Label(root, text="STEEL TECH", font=("Segoe UI", 10, "bold"), bg="black", fg="#444444").place(relx=1.0, rely=1.0, anchor='se', x=-10, y=-10)

root.mainloop()