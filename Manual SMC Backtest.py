import certifi
import csv
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
import time
import sys
import requests

# ---------------- USER CONFIG ----------------
API_KEY = "6cc04abb42d24b848dfe4cfdc8e3ca86"

SYMBOLS = ["USD/JPY", "AUD/USD", "EUR/USD", "GBP/USD", "GBP/JPY"]
FNAME_MAP = {
    "AUD/USD": "AU$_trades.csv",
    "USD/JPY": "UJ$_trades.csv",
    "EUR/USD": "EU$_trades.csv",
    "GBP/USD": "GU$_trades.csv",
    "GBP/JPY": "GJ$_trades.csv"
}

HTF = "1h"
LTF = "15min"
MONTHS_BACK = 6
MAX_CALLS_PER_MINUTE = 8

# ---------------- Per-symbol configs ----------------
SYMBOL_CONFIG = {
    "AUD/USD": {
        "ENTRY_TOLERANCE_PIPS": 5,
        "RETEST_CANDLES": 3,
        "MIN_BOS_RANGE_PIPS": 6,
        "REQUIRE_REJECTION": True,
        "RR_RATIO": 1.5,
        "MAX_TRADES_PER_DAY": 3,
        "MAX_BARS_TO_RESOLVE": 96,
        "SESSION_START_HOUR": 7,
        "SESSION_END_HOUR": 21,
        "RECENT_RANGE_CANDLES": 20,
        "CHOPPY_THRESHOLD": 1.0
    },
    "DEFAULT": {
        "ENTRY_TOLERANCE_PIPS": 4,
        "RETEST_CANDLES": 3,
        "MIN_BOS_RANGE_PIPS": 8,
        "REQUIRE_REJECTION": True,
        "RR_RATIO": 1.5,
        "MAX_TRADES_PER_DAY": 3,
        "MAX_BARS_TO_RESOLVE": 96,
        "SESSION_START_HOUR": 7,
        "SESSION_END_HOUR": 21,
        "RECENT_RANGE_CANDLES": 20,
        "CHOPPY_THRESHOLD": 1.2
    }
}

# ---------------- pacing state ----------------
_calls_this_minute = 0
_minute_start = time.time()
def pace_request():
    global _calls_this_minute, _minute_start
    _calls_this_minute += 1
    elapsed = time.time() - _minute_start
    if _calls_this_minute >= MAX_CALLS_PER_MINUTE:
        wait = 60 - elapsed
        if wait > 0:
            print(f"[PACING] Sleeping {wait:.1f}s...")
            time.sleep(wait)
        _calls_this_minute = 0
        _minute_start = time.time()

# ---------------- utilities ----------------
def pip_size(symbol):
    if "JPY" in symbol:
        return 0.01
    if "XAU" in symbol or "GOLD" in symbol:
        return 0.1
    return 0.0001

def fetch_ohlcv(symbol, interval, months, max_retries=3):
    for attempt in range(max_retries):
        try:
            pace_request()
            end_date = datetime.now(timezone.utc)
            start_date = end_date - timedelta(days=30*months)
            url = "https://api.twelvedata.com/time_series"
            params = {
                "symbol": symbol,
                "interval": interval,
                "start_date": start_date.strftime("%Y-%m-%d"),
                "end_date": end_date.strftime("%Y-%m-%d"),
                "apikey": API_KEY,
                "outputsize": 5000
            }
            r = requests.get(url, params=params, timeout=30, verify=certifi.where())
            r.raise_for_status()
            data = r.json()
            if "values" not in data:
                raise RuntimeError(f"No values for {symbol} {interval}: {data}")
            df = pd.DataFrame(data["values"])
            df["time"] = pd.to_datetime(df["datetime"])
            for c in ["open","high","low","close","volume"]:
                if c not in df.columns:
                    df[c] = np.nan
            df[["open","high","low","close","volume"]] = df[["open","high","low","close","volume"]].astype(float)
            df = df.sort_values("time").reset_index(drop=True)
            return df
        except Exception as e:
            print(f"[API ERROR] {symbol} {interval} attempt {attempt+1}: {e}")
            time.sleep(2)
    raise RuntimeError(f"Failed to fetch {symbol} {interval} after {max_retries} attempts.")

def in_session(dt_utc, start_hr, end_hr):
    t = dt_utc.time()
    return (t >= datetime.min.replace(hour=start_hr).time() and
            t <= datetime.min.replace(hour=end_hr).time())

# ---------------- structure & SMC helpers ----------------
def detect_swings_and_bos(df):
    df = df.copy()
    df["swing_high"] = (df["high"] > df["high"].shift(1)) & (df["high"] > df["high"].shift(-1))
    df["swing_low"] = (df["low"] < df["low"].shift(1)) & (df["low"] < df["low"].shift(-1))
    df["BOS"] = 0
    prev_swing_high, prev_swing_low = None, None
    count_bull, count_bear = 0, 0
    bull_counts, bear_counts = [], []
    for idx, row in df.iterrows():
        if row["swing_high"]:
            if prev_swing_high is not None and row["high"] > prev_swing_high:
                count_bull += 1
                df.at[idx, "BOS"] = 1
            else:
                count_bull = 0
            prev_swing_high = row["high"]
        bull_counts.append(count_bull)
        if row["swing_low"]:
            if prev_swing_low is not None and row["low"] < prev_swing_low:
                count_bear += 1
                df.at[idx, "BOS"] = -1
            else:
                count_bear = 0
            prev_swing_low = row["low"]
        bear_counts.append(count_bear)
    df["bos_bull_count"] = bull_counts
    df["bos_bear_count"] = bear_counts
    return df

def detect_order_blocks(df):
    df = df.copy()
    df["order_block"] = np.nan
    for idx, row in df.iterrows():
        if row.get("BOS",0) != 0 and idx > 0:
            df.at[idx-1, "order_block"] = row["BOS"]
    return df

def price_within_pips(p1, p2, pips, pip):
    return abs(p1 - p2) <= pips * pip

def is_retest(df, idx, entry_tolerance_pips, pip, retest_candles):
    if idx < retest_candles:
        return False
    ob_idx = idx - 1
    if pd.isna(df.at[ob_idx, "order_block"]):
        return False
    ob_close = df.at[ob_idx, "close"]
    for i in range(idx - retest_candles, idx):
        if price_within_pips(df.at[i, "close"], ob_close, entry_tolerance_pips, pip):
            return True
    return False

def is_liquidity_sweep(df, idx, trade_type, window=6):
    if idx < window:
        return False
    recent = df.iloc[idx-window:idx]
    if trade_type == "buy":
        prev_high = recent["high"].max()
        return df.at[idx, "high"] > prev_high and df.at[idx, "close"] < prev_high
    else:
        prev_low = recent["low"].min()
        return df.at[idx, "low"] < prev_low and df.at[idx, "close"] > prev_low

def is_rejection(df, idx, trade_type, require_rejection):
    if not require_rejection:
        return True
    row = df.iloc[idx]
    body = abs(row["close"] - row["open"])
    rng = row["high"] - row["low"]
    if rng == 0:
        return False
    if trade_type == "buy":
        upper_wick = row["high"] - max(row["close"], row["open"])
        return (row["close"] > row["open"]) and (upper_wick / rng) < 0.3 and (body > 0.4 * rng)
    else:
        lower_wick = min(row["close"], row["open"]) - row["low"]
        return (row["close"] < row["open"]) and (lower_wick / rng) < 0.3 and (body > 0.4 * rng)

def is_choppy(df_recent, recent_range_candles, choppy_threshold):
    if len(df_recent) < recent_range_candles:
        return False
    recent = df_recent.iloc[-recent_range_candles:]
    price_range = recent["high"].max() - recent["low"].min()
    avg_body = np.mean(np.abs(recent["close"] - recent["open"]).replace(0, np.nan))
    if np.isnan(avg_body) or avg_body == 0:
        return True
    return (price_range / avg_body) < choppy_threshold

# ---------------- candidate generation & capping ----------------
def generate_signals(df_ltf, df_htf, pip, cfg):
    """
    Generate trade signals (long and short) based on SMC logic and filters.
    Returns a list of dicts with keys: type, entry, stop, time.
    """
    df = df_ltf.copy()
    df["htf_bias"] = None
    htf_times = list(df_htf["time"])
    htf_bias = list(df_htf.get("bias", [None]*len(df_htf)))
    for i, t in enumerate(df["time"]):
        idxs = [j for j, ht in enumerate(htf_times) if ht <= t]
        df.at[i, "htf_bias"] = htf_bias[idxs[-1]] if idxs else None

    signals = []
    for idx, row in df.iterrows():
        dt = row["time"]
        if dt.weekday() == 0:
            continue
        if not in_session(dt, cfg["SESSION_START_HOUR"], cfg["SESSION_END_HOUR"]):
            continue
        recent = df.iloc[max(0, idx-cfg["RECENT_RANGE_CANDLES"]): idx+1]
        if is_choppy(recent, cfg["RECENT_RANGE_CANDLES"], cfg["CHOPPY_THRESHOLD"]):
            continue
        # Long signal
        if row["BOS"] == 1:
            if df.at[idx, "htf_bias"] and df.at[idx, "htf_bias"] != "bull":
                continue
            if idx > 0 and (row["high"] - df.iloc[idx-1]["high"]) < cfg["MIN_BOS_RANGE_PIPS"] * pip:
                continue
            if not is_retest(df, idx, cfg["ENTRY_TOLERANCE_PIPS"], pip, cfg["RETEST_CANDLES"]):
                continue
            if not (is_liquidity_sweep(df, idx, "buy") or is_rejection(df, idx, "buy", cfg["REQUIRE_REJECTION"])):
                continue
            ob_idx = idx - 1
            ob_mid = (df.at[ob_idx, "high"] + df.at[ob_idx, "low"]) / 2.0
            stop = float(df.iloc[max(0, idx-5):idx]["low"].min())
            signals.append({"type":"buy","entry":float(ob_mid),"stop":stop,"time":row["time"]})
        # Short signal
        elif row["BOS"] == -1:
            if df.at[idx, "htf_bias"] and df.at[idx, "htf_bias"] != "bear":
                continue
            if idx > 0 and (df.iloc[idx-1]["low"] - row["low"]) < cfg["MIN_BOS_RANGE_PIPS"] * pip:
                continue
            if not is_retest(df, idx, cfg["ENTRY_TOLERANCE_PIPS"], pip, cfg["RETEST_CANDLES"]):
                continue
            if not (is_liquidity_sweep(df, idx, "sell") or is_rejection(df, idx, "sell", cfg["REQUIRE_REJECTION"])):
                continue
            ob_idx = idx - 1
            ob_mid = (df.at[ob_idx, "high"] + df.at[ob_idx, "low"]) / 2.0
            stop = float(df.iloc[max(0, idx-5):idx]["high"].max())
            signals.append({"type":"sell","entry":float(ob_mid),"stop":stop,"time":row["time"]})
    return signals

def cap_trades_daily(candidates, max_per_day, rr_ratio, max_longs=4, max_shorts=4):
    if not candidates:
        return []
    df = pd.DataFrame(candidates)
    df["date"] = pd.to_datetime(df["time"]).dt.date
    final = []
    for date, group in df.groupby("date"):
        longs = group[group["type"] == "buy"].sort_values("time").head(max_longs)
        shorts = group[group["type"] == "sell"].sort_values("time").head(max_shorts)
        sel = pd.concat([longs, shorts])
        for _, r in sel.iterrows():
            entry, stop = float(r["entry"]), float(r["stop"])
            target = entry + (entry - stop) * rr_ratio if r["type"] == "buy" else entry - (stop - entry) * rr_ratio
            final.append({"type": r["type"], "entry":entry, "stop":stop, "target":float(target), "time":r["time"], "date":r["date"]})
    return final

# ---------------- backtest execution ----------------
def backtest_symbol(symbol):
    print(f"\n[BACKTEST] {symbol} -> fetching data...")
    pip = pip_size(symbol)
    cfg = SYMBOL_CONFIG.get(symbol, SYMBOL_CONFIG["DEFAULT"])

    try:
        df_htf = fetch_ohlcv(symbol, HTF, MONTHS_BACK)
        df_ltf = fetch_ohlcv(symbol, LTF, MONTHS_BACK)
    except Exception as e:
        print(f"[ERROR] fetching {symbol}: {e}")
        return

    df_htf = detect_swings_and_bos(df_htf)
    df_htf["bias"] = None
    for i in range(len(df_htf)):
        highs = df_htf.loc[:i, "high"][df_htf["swing_high"]]
        lows  = df_htf.loc[:i, "low"][df_htf["swing_low"]]
        if len(highs) >= 2 and len(lows) >= 2:
            if highs.values[-1] > highs.values[-2] and lows.values[-1] > lows.values[-2]:
                df_htf.at[i, "bias"] = "bull"
            elif highs.values[-1] < highs.values[-2] and lows.values[-1] < lows.values[-2]:
                df_htf.at[i, "bias"] = "bear"

    df_ltf = detect_swings_and_bos(df_ltf)
    df_ltf = detect_order_blocks(df_ltf)

    candidates = generate_signals(df_ltf, df_htf, pip, cfg)
    trades = cap_trades_daily(candidates, cfg["MAX_TRADES_PER_DAY"], cfg["RR_RATIO"], max_longs=4, max_shorts=4)

    results = []
    for tr in trades:
        idxs = df_ltf.index[df_ltf["time"] == tr["time"]].tolist()
        if not idxs:
            results.append({"trade": tr, "result": "open", "exit_time": None})
            continue
        start_idx = idxs[0]
        future = df_ltf.iloc[start_idx: start_idx + cfg["MAX_BARS_TO_RESOLVE"]]
        res, exit_time = "open", None
        for _, r in future.iterrows():
            if tr["type"] == "buy":
                if r["low"] <= tr["stop"]:
                    res = "LOSS"; exit_time = r["time"]; break
                if r["high"] >= tr["target"]:
                    res = "WIN"; exit_time = r["time"]; break
            else:
                if r["high"] >= tr["stop"]:
                    res = "LOSS"; exit_time = r["time"]; break
                if r["low"] <= tr["target"]:
                    res = "WIN"; exit_time = r["time"]; break
        results.append({"trade": tr, "result": res, "exit_time": exit_time})

    rows = []
    for r in results:
        tr = r["trade"]
        entry, stop, target = tr["entry"], tr["stop"], tr["target"]
        pips = (target - entry) / pip if (tr["type"] == "buy" and r["result"] == "WIN") else \
               (stop - entry) / pip if (tr["type"] == "buy" and r["result"] == "LOSS") else \
               (entry - target) / pip if (tr["type"] == "sell" and r["result"] == "WIN") else \
               (entry - stop) / pip if (tr["type"] == "sell" and r["result"] == "LOSS") else 0
        rr = abs((target - entry) / (entry - stop)) if (entry - stop) != 0 else None
        rows.append({
            "entry_time": tr["time"],
            "type": tr["type"],
            "entry": entry,
            "stop": stop,
            "target": target,
            "result": r["result"],
            "exit_time": r["exit_time"],
            "pips_PL": round(pips, 2),
            "RR": round(rr, 2) if rr else ""
        })

    fname = FNAME_MAP.get(symbol, f"{symbol.replace('/','_')}_trades.csv")
    with open(fname, "w", newline="") as f:
        fieldnames = ["entry_time","type","entry","stop","target","result","exit_time","pips_PL","RR"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    total = len(rows)
    wins = sum(1 for r in rows if r["result"] == "WIN")
    losses = sum(1 for r in rows if r["result"] == "LOSS")
    open_ = sum(1 for r in rows if r["result"] == "open")
    winrate = (wins / total * 100) if total > 0 else 0
    print(f"[RESULT] {symbol}: total {total}, WIN {wins}, LOSS {losses}, OPEN {open_}, winrate {winrate:.2f}%")
    print(f"[SAVED] {fname}")

# ---------------- main ----------------
if __name__ == "__main__":
    if API_KEY == "your_twelvedata_api_key_here":
        print("ERROR: set your API_KEY.")
        sys.exit(1)
    summary = []
    failed = []
    for s in SYMBOLS:
        try:
            backtest_symbol(s)
        except Exception as e:
            print(f"[ERROR] {s}: {e}")
            failed.append(s)
    # Retry failed symbols up to 2 more times
    for attempt in range(2):
        if not failed:
            break
        print(f"\n[RETRY] Attempt {attempt+1} for failed symbols: {failed}")
        still_failed = []
        for s in failed:
            try:
                backtest_symbol(s)
            except Exception as e:
                print(f"[ERROR] {s} (retry {attempt+1}): {e}")
                still_failed.append(s)
        failed = still_failed
    print("\nBacktest complete. See CSV files for details.")
