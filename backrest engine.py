import tkinter as tk
from tkinter import ttk, messagebox
import pandas as pd
import requests
import warnings
from datetime import datetime, timedelta

# === CONFIGURATION ===
API_KEY = "25e62fb28d1040f1be47bf4f4c5d4138"
BASE_URL = "https://api.twelvedata.com/time_series"
symbols = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "XAU/USD"]

PAIR_CONFIG = {
    "EUR/USD": {"atr_mult": 1.5, "rsi_thresh": 50, "min_score": 3},
    "GBP/USD": {"atr_mult": 1.5, "rsi_thresh": 50, "min_score": 3},
    "USD/JPY": {"atr_mult": 1.5, "rsi_thresh": 50, "min_score": 3},
    "AUD/USD": {"atr_mult": 1.5, "rsi_thresh": 50, "min_score": 3},
    "XAU/USD": {"atr_mult": 1.5, "rsi_thresh": 50, "min_score": 3}
}

# === STRATEGY FUNCTIONS ===

def apply_indicators(df):
    df['EMA_8'] = df['close'].ewm(span=8).mean()
    df['EMA_21'] = df['close'].ewm(span=21).mean()
    macd = df['close'].ewm(span=12).mean() - df['close'].ewm(span=26).mean()
    df['MACD_hist'] = macd - macd.ewm(span=9).mean()
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    df['RSI'] = 100 - 100 / (1 + gain.rolling(14).mean() / loss.rolling(14).mean())
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
    upper = df['high'] - df[['close', 'open']].max(axis=1)
    lower = df[['close', 'open']].min(axis=1) - df['low']
    df['Pin_Bar'] = ((upper > 2 * body) | (lower > 2 * body)) & (body < (df['high'] - df['low']) * 0.4)
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
    df.dropna(inplace=True)

    for offset in range(3, 0, -1):
        last = df.iloc[-offset]
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
            atr = df['ATR'].iloc[-offset]
            if direction == "BUY":
                sl = entry - atr * atr_mult
                tp1 = entry + (entry - sl)
                tp2 = entry + 2 * (entry - sl)
                tp3 = entry + 3 * (entry - sl)
            else:
                sl = entry + atr * atr_mult
                tp1 = entry - (sl - entry)
                tp2 = entry - 2 * (sl - entry)
                tp3 = entry - 3 * (sl - entry)
            return True, entry, sl, tp1, tp2, tp3, score, direction

    return False, df.iloc[-1]['close'], None, None, None, None, 0, None

# === Fetch Data ONLY From This Week ===
def fetch_data(symbol, interval="15min"):
    params = {
        "symbol": symbol,
        "interval": interval,
        "apikey": API_KEY,
        "outputsize": 500,
        "format": "JSON"
    }
    r = requests.get(BASE_URL, params=params)
    data = r.json()
    if "values" not in data:
        raise Exception(f"API error for {symbol}")
    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    df.set_index("datetime", inplace=True)
    df = df.apply(pd.to_numeric, errors="coerce").dropna().sort_index()

    # ✅ Filter only Monday to Now
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    df = df[df.index >= monday.replace(hour=0, minute=0, second=0, microsecond=0)]

    return df

# === Backtest ===
def simulate_backtest(symbol, df, config):
    wins, losses, total = 0, 0, 0
    tp_hits = {1: 0, 2: 0, 3: 0}
    df = apply_indicators(df)
    df = apply_smc(df)
    df = detect_price_action(df)
    df.dropna(inplace=True)
    start_idx = max(50, len(df) - 250)

    for i in range(start_idx, len(df) - 6):
        sub = df.iloc[:i+1]
        signal, entry, sl, tp1, tp2, tp3, score, direction = generate_signals(sub, **config)
        if not signal:
            continue
        total += 1
        future = df.iloc[i+1:i+6]
        for _, row in future.iterrows():
            if direction == "BUY":
                if row["low"] <= sl: losses += 1; break
                elif row["high"] >= tp3: wins += 1; tp_hits[3] += 1; break
                elif row["high"] >= tp2: wins += 1; tp_hits[2] += 1; break
                elif row["high"] >= tp1: wins += 1; tp_hits[1] += 1; break
            else:
                if row["high"] >= sl: losses += 1; break
                elif row["low"] <= tp3: wins += 1; tp_hits[3] += 1; break
                elif row["low"] <= tp2: wins += 1; tp_hits[2] += 1; break
                elif row["low"] <= tp1: wins += 1; tp_hits[1] += 1; break
    return {
        "symbol": symbol,
        "total": total,
        "wins": wins,
        "losses": losses,
        "winrate": (wins / total * 100) if total else 0,
        "tp_hits": tp_hits
    }

# === GUI ===
def run_backtest():
    results_text.delete(1.0, tk.END)
    symbol = symbol_var.get()
    try:
        results_text.insert(tk.END, f"Fetching {symbol} data...\\n")
        df = fetch_data(symbol)
        result = simulate_backtest(symbol, df, PAIR_CONFIG[symbol])
        results_text.insert(tk.END, f"{symbol} Backtest Result:\\n")
        results_text.insert(tk.END, f"  Total Trades: {result['total']}\\n")
        results_text.insert(tk.END, f"  Wins: {result['wins']} | Losses: {result['losses']}\\n")
        results_text.insert(tk.END, f"  Win Rate: {result['winrate']:.2f}%\\n")
        results_text.insert(tk.END, f"  TP1: {result['tp_hits'][1]}, TP2: {result['tp_hits'][2]}, TP3: {result['tp_hits'][3]}\\n")
    except Exception as e:
        messagebox.showerror("Error", str(e))

# === GUI Setup ===
root = tk.Tk()
root.title("Backtest This Week")
root.geometry("600x500")
root.configure(bg="black")

ttk.Label(root, text="📊 Weekly Strategy Backtester", font=("Segoe UI", 16, "bold")).pack(pady=10)

symbol_var = tk.StringVar(value=symbols[0])
ttk.OptionMenu(root, symbol_var, symbols[0], *symbols).pack(pady=5)

ttk.Button(root, text="Fetch & Backtest", command=run_backtest).pack(pady=10)

results_text = tk.Text(root, wrap="word", bg="black", fg="white", font=("Consolas", 10))
results_text.pack(fill="both", expand=True, padx=10, pady=10)

root.mainloop()