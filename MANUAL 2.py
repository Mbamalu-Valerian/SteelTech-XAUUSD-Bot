# steel_tech_live.py

import certifi
import pandas as pd
import numpy as np
import requests
import threading
import time
from datetime import datetime, timedelta, timezone
import tkinter as tk
from tkinter import ttk, messagebox

# ---------------- USER CONFIG ----------------
API_KEY = "6cc04abb42d24b848dfe4cfdc8e3ca86"

SYMBOLS = ["USD/JPY", "AUD/USD", "EUR/USD", "GBP/USD", "GBP/JPY"]
HTF = "1h"
LTF = "15min"

# Rate limit helper (same spirit as your backtest)
MAX_CALLS_PER_MINUTE = 8
_calls_this_minute = 0
_minute_start = time.time()

def pace_request():
    global _calls_this_minute, _minute_start
    _calls_this_minute += 1
    elapsed = time.time() - _minute_start
    if _calls_this_minute >= MAX_CALLS_PER_MINUTE:
        wait = 60 - elapsed
        if wait > 0:
            time.sleep(wait)
        _calls_this_minute = 0
        _minute_start = time.time()

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

# ---------------- Utilities ----------------
def pip_size(symbol: str) -> float:
    if "JPY" in symbol:
        return 0.01
    if "XAU" in symbol or "GOLD" in symbol:
        return 0.1
    return 0.0001

def fetch_ohlcv(symbol: str, interval: str, outputsize: int = 500, max_retries: int = 3) -> pd.DataFrame:
    """
    Fetch OHLCV from TwelveData. Uses smaller outputsize for speed.
    """
    for attempt in range(max_retries):
        try:
            pace_request()
            url = "https://api.twelvedata.com/time_series"
            params = {
                "symbol": symbol,
                "interval": interval,
                "apikey": API_KEY,
                "outputsize": outputsize
            }
            r = requests.get(url, params=params, timeout=30, verify=certifi.where())
            r.raise_for_status()
            data = r.json()
            if "values" not in data:
                raise RuntimeError(f"No values for {symbol} {interval}: {data}")
            df = pd.DataFrame(data["values"])
            df["time"] = pd.to_datetime(df["datetime"], utc=True)
            for c in ["open", "high", "low", "close", "volume"]:
                if c not in df.columns:
                    df[c] = np.nan
            df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype(float)
            df = df.sort_values("time").reset_index(drop=True)
            return df
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            time.sleep(1 + attempt)
    raise RuntimeError(f"Failed to fetch {symbol} {interval} after {max_retries} attempts.")

def in_session(dt_utc: datetime, start_hr: int, end_hr: int) -> bool:
    t = dt_utc.time()
    start = datetime.min.replace(hour=start_hr).time()
    end = datetime.min.replace(hour=end_hr).time()
    return start <= t <= end

# ---------------- SMC Helpers (from your backtest) ----------------
def detect_swings_and_bos(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["swing_high"] = (df["high"] > df["high"].shift(1)) & (df["high"] > df["high"].shift(-1))
    df["swing_low"]  = (df["low"]  < df["low"].shift(1))  & (df["low"]  < df["low"].shift(-1))
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

def detect_order_blocks(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["order_block"] = np.nan
    for idx, row in df.iterrows():
        if row.get("BOS", 0) != 0 and idx > 0:
            df.at[idx - 1, "order_block"] = row["BOS"]
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
    recent = df.iloc[idx - window:idx]
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

def compute_htf_bias_from_last10(df_htf: pd.DataFrame) -> str | None:
    """
    Apply your swing/BOS logic on just the last 10 closed HTF candles
    and derive 'bull'/'bear'/None like your backtest.
    """
    if len(df_htf) < 10:
        return None
    sub = df_htf.tail(10).copy()
    sub = detect_swings_and_bos(sub)
    sub["bias"] = None
    for i in range(len(sub)):
        highs = sub.loc[:i, "high"][sub["swing_high"]]
        lows  = sub.loc[:i, "low"][sub["swing_low"]]
        if len(highs) >= 2 and len(lows) >= 2:
            if highs.values[-1] > highs.values[-2] and lows.values[-1] > lows.values[-2]:
                sub.at[sub.index[i], "bias"] = "bull"
            elif highs.values[-1] < highs.values[-2] and lows.values[-1] < lows.values[-2]:
                sub.at[sub.index[i], "bias"] = "bear"
    return sub["bias"].dropna().iloc[-1] if sub["bias"].dropna().size > 0 else None

def generate_signals_ltf_filtered(df_ltf: pd.DataFrame, htf_bias: str | None, symbol: str) -> list[dict]:
    """
    Uses your original generate_signals logic but restricts output to the latest closed LTF bar only.
    """
    cfg = SYMBOL_CONFIG.get(symbol, SYMBOL_CONFIG["DEFAULT"])
    pip = pip_size(symbol)

    # Prepare HTF bias column across LTF time just as in your backtest (but constant bias here)
    df = df_ltf.copy()
    df["htf_bias"] = htf_bias

    # Your swing/OB annotations
    df = detect_swings_and_bos(df)
    df = detect_order_blocks(df)

    signals = []
    last_idx = len(df) - 1  # last closed bar
    row = df.iloc[last_idx]
    dt = row["time"]

    # Basic filters
    if dt.weekday() != 0 and in_session(dt, cfg["SESSION_START_HOUR"], cfg["SESSION_END_HOUR"]):
        recent = df.iloc[max(0, last_idx - cfg["RECENT_RANGE_CANDLES"]): last_idx + 1]
        if not is_choppy(recent, cfg["RECENT_RANGE_CANDLES"], cfg["CHOPPY_THRESHOLD"]):
            # Long
            if row["BOS"] == 1:
                if htf_bias and htf_bias != "bull":
                    pass
                else:
                    if last_idx > 0 and (row["high"] - df.iloc[last_idx - 1]["high"]) >= cfg["MIN_BOS_RANGE_PIPS"] * pip:
                        if is_retest(df, last_idx, cfg["ENTRY_TOLERANCE_PIPS"], pip, cfg["RETEST_CANDLES"]):
                            if is_liquidity_sweep(df, last_idx, "buy") or is_rejection(df, last_idx, "buy", cfg["REQUIRE_REJECTION"]):
                                ob_idx = last_idx - 1
                                ob_mid = (df.at[ob_idx, "high"] + df.at[ob_idx, "low"]) / 2.0
                                stop = float(df.iloc[max(0, last_idx - 5):last_idx]["low"].min())
                                target = ob_mid + (ob_mid - stop) * cfg["RR_RATIO"]
                                signals.append({
                                    "type": "buy",
                                    "entry": float(ob_mid),
                                    "stop": float(stop),
                                    "target": float(target),
                                    "time": dt
                                })
            # Short
            elif row["BOS"] == -1:
                if htf_bias and htf_bias != "bear":
                    pass
                else:
                    if last_idx > 0 and (df.iloc[last_idx - 1]["low"] - row["low"]) >= cfg["MIN_BOS_RANGE_PIPS"] * pip:
                        if is_retest(df, last_idx, cfg["ENTRY_TOLERANCE_PIPS"], pip, cfg["RETEST_CANDLES"]):
                            if is_liquidity_sweep(df, last_idx, "sell") or is_rejection(df, last_idx, "sell", cfg["REQUIRE_REJECTION"]):
                                ob_idx = last_idx - 1
                                ob_mid = (df.at[ob_idx, "high"] + df.at[ob_idx, "low"]) / 2.0
                                stop = float(df.iloc[max(0, last_idx - 5):last_idx]["high"].max())
                                target = ob_mid - (stop - ob_mid) * cfg["RR_RATIO"]
                                signals.append({
                                    "type": "sell",
                                    "entry": float(ob_mid),
                                    "stop": float(stop),
                                    "target": float(target),
                                    "time": dt
                                })
    return signals

# ---------------- GUI ----------------
class SteelTechGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("STEEL TECH 1 — Live Signals")
        self.configure(bg="#0a0a0a")
        self.geometry("760x620")
        self.minsize(720, 560)

        # Global style
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#0a0a0a")
        style.configure("TLabel", background="#0a0a0a", foreground="#e5e5e5")
        style.configure("TButton", background="#1a1a1a", foreground="#ff2a2a", font=("Segoe UI", 10, "bold"))
        style.map("TButton", background=[("active", "#2a2a2a")])
        style.configure("TLabelframe", background="#0a0a0a", foreground="#ff2a2a")
        style.configure("TLabelframe.Label", background="#0a0a0a", foreground="#ff2a2a", font=("Segoe UI", 10, "bold"))

        # Header
        header = ttk.Frame(self)
        header.pack(fill="x", padx=16, pady=(14, 6))
        ttk.Label(header, text="Steel Tech Live SMC Signals", font=("Segoe UI", 16, "bold"), foreground="#ff2a2a").pack(side="left")

        # Controls
        controls = ttk.Frame(self)
        controls.pack(fill="x", padx=16, pady=8)

        ttk.Label(controls, text="Symbol:", font=("Segoe UI", 10, "bold")).pack(side="left", padx=(0, 8))
        self.symbol_var = tk.StringVar(value=SYMBOLS[0])
        self.symbol_combo = ttk.Combobox(controls, values=SYMBOLS, textvariable=self.symbol_var, state="readonly")
        self.symbol_combo.pack(side="left")

        self.analyze_btn = ttk.Button(controls, text="Analyze", command=self.on_analyze_click)
        self.analyze_btn.pack(side="left", padx=12)

        self.status_var = tk.StringVar(value="Idle")
        self.status_label = ttk.Label(controls, textvariable=self.status_var, foreground="#9e9e9e")
        self.status_label.pack(side="right")

        # Signal list (scrollable)
        list_frame = ttk.Frame(self)
        list_frame.pack(fill="both", expand=True, padx=16, pady=(6, 56))  # leave space for logo

        self.canvas = tk.Canvas(list_frame, bg="#0a0a0a", highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)

        self.inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        # Storage for live cards/timers
        self.cards = []  # list of dicts {frame, expire_ts, countdown_label}

        # Logo bottom-right
        self.logo_frame = ttk.Frame(self)
        self.logo_frame.place(relx=1.0, rely=1.0, anchor="se", x=-12, y=-10)
        ttk.Label(self.logo_frame, text="STEEL TECH 1™", font=("Segoe UI", 10, "bold"), foreground="#ff2a2a").pack()

        # Start countdown updater
        self.after(1000, self._tick_countdowns)

    # --------- UI helpers ----------
    def clear_expired_cards(self):
        now = datetime.now(timezone.utc).timestamp()
        keep = []
        for c in self.cards:
            if now >= c["expire_ts"]:
                c["frame"].destroy()
            else:
                keep.append(c)
        self.cards = keep

    def add_signal_card(self, symbol: str, sig: dict, valid_minutes: int = 15):
        """
        sig fields: type, entry, stop, target, time (UTC pd.Timestamp)
        """
        # Expiry handling
        expire_ts = (datetime.now(timezone.utc) + timedelta(minutes=valid_minutes)).timestamp()

        # Card UI
        f = ttk.Labelframe(self.inner, text=f"{symbol}  —  {sig['type'].upper()}", padding=10)
        f.pack(fill="x", pady=8)

        # Colour band (thin) to emphasize buy/sell within red/black theme
        band = tk.Frame(f, height=3, bg=("#13c113" if sig["type"] == "buy" else "#ff2a2a"))
        band.pack(fill="x", pady=(0, 10))

        def row(label, value):
            r = ttk.Frame(f)
            r.pack(fill="x", pady=2)
            ttk.Label(r, text=f"{label}: ", width=12, anchor="w", foreground="#e0e0e0").pack(side="left")
            ttk.Label(r, text=value, anchor="w", foreground="#ffffff").pack(side="left")

        row("ENTRY", f"{sig['entry']:.5f}")
        row("TP",    f"{sig['target']:.5f}")
        row("SL",    f"{sig['stop']:.5f}")

        # Validity countdown
        countdown_row = ttk.Frame(f)
        countdown_row.pack(fill="x", pady=(6, 0))
        ttk.Label(countdown_row, text="Valid:", width=12, anchor="w", foreground="#e0e0e0").pack(side="left")
        countdown_var = tk.StringVar(value="15:00")
        countdown_lbl = ttk.Label(countdown_row, textvariable=countdown_var, foreground="#ffb0b0")
        countdown_lbl.pack(side="left")

        # Save card for tick updates
        self.cards.append({
            "frame": f,
            "expire_ts": expire_ts,
            "countdown_var": countdown_var
        })

    def add_info_card(self, text: str):
        f = ttk.Labelframe(self.inner, text="INFO", padding=10)
        f.pack(fill="x", pady=8)
        ttk.Label(f, text=text, foreground="#b0b0b0").pack(anchor="w")
        # Auto-remove after 10s to keep list clean
        expire_ts = (datetime.now(timezone.utc) + timedelta(seconds=10)).timestamp()
        self.cards.append({"frame": f, "expire_ts": expire_ts, "countdown_var": None})

    def _tick_countdowns(self):
        now = datetime.now(timezone.utc).timestamp()
        for c in list(self.cards):
            if c["countdown_var"] is None:
                continue
            remaining = int(c["expire_ts"] - now)
            if remaining <= 0:
                c["countdown_var"].set("00:00")
            else:
                m = remaining // 60
                s = remaining % 60
                c["countdown_var"].set(f"{m:02d}:{s:02d}")
        # Remove expired cards
        self.clear_expired_cards()
        self.after(1000, self._tick_countdowns)

    # --------- Analyze flow ----------
    def on_analyze_click(self):
        symbol = self.symbol_var.get()
        self.status_var.set(f"Analyzing {symbol} ...")
        self.analyze_btn.config(state="disabled")
        threading.Thread(target=self._analyze_worker, args=(symbol,), daemon=True).start()

    def _analyze_worker(self, symbol: str):
        try:
            # 1) Fetch HTF (small output, we only need last 10 closed)
            df_htf = fetch_ohlcv(symbol, HTF, outputsize=60)
            if df_htf is None or len(df_htf) < 10:
                self._post_ui(lambda: self.add_info_card("Not enough HTF candles."))
                self._post_ui(lambda: self._analysis_done("Idle"))
                return

            htf_bias = compute_htf_bias_from_last10(df_htf)

            if htf_bias is None:
                self._post_ui(lambda: self.add_info_card("No trade bias on HTF — stand by"))
                self._post_ui(lambda: self._analysis_done("Idle"))
                return

            # 2) Fetch LTF recent data
            df_ltf = fetch_ohlcv(symbol, LTF, outputsize=400)
            if df_ltf is None or len(df_ltf) < 50:
                self._post_ui(lambda: self.add_info_card("Not enough LTF candles."))
                self._post_ui(lambda: self._analysis_done("Idle"))
                return

            # 3) Run LTF confirmation but only for the latest closed bar
            signals = generate_signals_ltf_filtered(df_ltf, htf_bias, symbol)

            if not signals:
                self._post_ui(lambda: self.add_info_card("No valid signal at this time"))
                self._post_ui(lambda: self._analysis_done("Idle"))
                return

            # 4) Display signals (each with 15-min countdown)
            for sig in signals:
                self._post_ui(lambda s=sig: self.add_signal_card(symbol, s, valid_minutes=15))

            self._post_ui(lambda: self._analysis_done("Idle"))
        except Exception as e:
            self._post_ui(lambda: self.add_info_card(f"Error: {e}"))
            self._post_ui(lambda: self._analysis_done("Idle"))

    def _post_ui(self, fn):
        self.after(0, fn)

    def _analysis_done(self, status="Idle"):
        self.status_var.set(status)
        self.analyze_btn.config(state="normal")


if __name__ == "__main__":
    app = SteelTechGUI()
    app.mainloop()
