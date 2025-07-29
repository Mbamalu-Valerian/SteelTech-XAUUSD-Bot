import tkinter as tk
from tkinter import ttk
import pandas as pd
import requests
import datetime
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

# === CONFIGURATION ===
API_KEY = "25e62fb28d1040f1be47bf4f4c5d4138"
BASE_URL = "https://api.twelvedata.com/time_series"
symbols = ["EUR/USD", "USD/JPY", "AUD/USD", "GBP/USD", "XAU/USD"]

PAIR_CONFIG = {
    "EUR/USD": {"atr_mult": 1.4, "rsi_thresh": 48, "min_score": 6},
    "USD/JPY": {"atr_mult": 1.7, "rsi_thresh": 48, "min_score": 6},
    "AUD/USD": {"atr_mult": 1.7, "rsi_thresh": 48, "min_score": 6},
    "GBP/USD": {"atr_mult": 1.7, "rsi_thresh": 48, "min_score": 6},
    "XAU/USD": {"atr_mult": 1.4, "rsi_thresh": 52, "min_score": 6}
}

# === DATA FETCHING ===
def fetch_data(symbol, interval="15min"):
    params = {
        "symbol": symbol,
        "interval": interval,
        "apikey": API_KEY,
        "outputsize": 100,
        "format": "JSON"
    }
    r = requests.get(BASE_URL, params=params)
    data = r.json()
    if "values" not in data:
        raise Exception(f"Fetch failed for {symbol}")
    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    df.set_index("datetime", inplace=True)
    df = df.apply(pd.to_numeric, errors="coerce").dropna().sort_index()
    return df

# === INDICATORS ===
def apply_indicators(df):
    df = df.copy()
    df['EMA_21'] = df['close'].ewm(span=21).mean()
    df['EMA_50'] = df['close'].ewm(span=50).mean()
    df['EMA_200'] = df['close'].ewm(span=200).mean()
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss
    df['RSI'] = 100 - 100 / (1 + rs)
    df['MACD'] = df['close'].ewm(span=12).mean() - df['close'].ewm(span=26).mean()
    df['MACD_signal'] = df['MACD'].ewm(span=9).mean()
    df['MACD_hist'] = df['MACD'] - df['MACD_signal']
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['close'].shift()).abs(),
        (df['low'] - df['close'].shift()).abs()
    ], axis=1)
    df['ATR'] = tr.max(axis=1).rolling(14).mean()
    return df.dropna()

# === SIGNAL LOGIC ===
def generate_signal(symbol, df):
    config = PAIR_CONFIG[symbol]
    df = apply_indicators(df)
    last = df.iloc[-1]
    score = 0

    now = datetime.datetime.now()
    expires = now + datetime.timedelta(minutes=15)

    if symbol == "EUR/USD":
        if df['EMA_50'].iloc[-1] > df['EMA_200'].iloc[-1]: score += 1
        if df['MACD_hist'].iloc[-1] > 0: score += 1
        if last['close'] > df['EMA_21'].iloc[-1]: score += 1
        if last['RSI'] > config['rsi_thresh']: score += 1

    elif symbol == "XAU/USD":
        recent_high = df['high'][-10:].max()
        recent_low = df['low'][-10:].min()
        if last['high'] > recent_high or last['low'] < recent_low: score += 1
        if last['close'] > df['EMA_21'].iloc[-1]: score += 1
        if last['MACD_hist'] > 0: score += 1

    elif symbol == "USD/JPY":
        recent_range = df['high'][-10:].max()
        if last['close'] > recent_range: score += 1
        if last['close'] > df['EMA_21'].iloc[-1]: score += 1
        if last['MACD_hist'] > 0: score += 1

    elif symbol in ["GBP/USD", "AUD/USD"]:
        if last['close'] > df['EMA_21'].iloc[-1]: score += 1
        if last['MACD_hist'] > 0: score += 1
        if last['RSI'] > config['rsi_thresh']: score += 1

    if score >= config['min_score'] - 3:
        entry = round(last['close'], 4)
        atr = last['ATR']
        sl = round(entry - atr * config['atr_mult'], 4)
        tp1 = round(entry + (entry - sl), 4)
        tp2 = round(entry + 2 * (entry - sl), 4)
        tp3 = round(entry + 3 * (entry - sl), 4)
        return {
            "type": "High Probability Signal",
            "entry": entry,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "sl": sl,
            "score": score,
            "timestamp": now.strftime("%H:%M:%S"),
            "expires": expires.strftime("%H:%M:%S")
        }

    return {
        "type": "No Clear Signal",
        "entry": last['close'],
        "score": score,
        "timestamp": now.strftime("%H:%M:%S"),
        "expires": expires.strftime("%H:%M:%S")
    }
# === GUI HANDLER ===
def update_analysis():
    symbol = symbol_var.get()
    try:
        df = fetch_data(symbol)
        signal = generate_signal(symbol, df)
        signal_label.config(
            text=f"{symbol}: {signal['type']} (Score: {signal['score']}) | {signal['timestamp']} → {signal['expires']}"
        )
        levels_label.config(
            text=f"Entry: {signal['entry']}\nTP1: {signal.get('tp1', '-')}\nTP2: {signal.get('tp2', '-')}\nTP3: {signal.get('tp3', '-')}\nSL: {signal.get('sl', '-')}")

        fig, ax = plt.subplots(figsize=(6, 3))
        ax.plot(df.index, df["close"], label="Close", color="white")
        if "entry" in signal:
            ax.axhline(signal["entry"], color='red', linestyle='--', label='Entry')
        if "tp1" in signal:
            ax.axhline(signal["tp1"], color='lime', linestyle='--', label='TP1')
        if "tp2" in signal:
            ax.axhline(signal["tp2"], color='green', linestyle='--', label='TP2')
        if "tp3" in signal:
            ax.axhline(signal["tp3"], color='blue', linestyle='--', label='TP3')
        if "sl" in signal:
            ax.axhline(signal["sl"], color='white', linestyle='--', label='SL')
        ax.legend()
        ax.set_facecolor("black")
        fig.patch.set_facecolor("black")
        ax.tick_params(colors='white')
        ax.set_title(f"{symbol} Chart", color="white")
        for widget in chart_frame.winfo_children():
            widget.destroy()
        chart = FigureCanvasTkAgg(fig, chart_frame)
        chart.draw()
        chart.get_tk_widget().pack()
    except Exception as e:
        signal_label.config(text=f"Error: {e}")
        levels_label.config(text="")
# === GUI SETUP ===
root = tk.Tk()
root.title("MULTISIGNAL BOT")
root.configure(bg="black")
style = ttk.Style()
style.theme_use("default")
style.configure("TLabel", background="black", foreground="#FF4444", font=("Segoe UI", 12, "bold"))
style.configure("TButton", background="#FF4444", foreground="black", font=("Segoe UI", 10, "bold"))
style.map("TButton", background=[('active', '#FF0000')], foreground=[('active', '#FFFFFF')])

ttk.Label(root, text="Forex Multisymbol Bot", font=("Segoe UI", 22, "bold")).pack(pady=10)
symbol_var = tk.StringVar(value="EUR/USD")
ttk.OptionMenu(root, symbol_var, "EUR/USD", *symbols).pack()

signal_label = ttk.Label(root, text="", font=("Segoe UI", 14))
signal_label.pack(pady=5)
levels_label = ttk.Label(root, text="", font=("Segoe UI", 11))
levels_label.pack()

ttk.Button(root, text="Analyze Market", command=update_analysis).pack(pady=10)
chart_frame = ttk.Frame(root)
chart_frame.pack()

tk.Label(root, text="STEEL TECH 3", bg="black", fg="gray").place(relx=1.0, rely=1.0, anchor='se', x=-10, y=-10)

root.mainloop()