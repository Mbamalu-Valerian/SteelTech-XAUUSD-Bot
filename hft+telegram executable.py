import asyncio
import csv
import logging
import os
import threading
import time
from datetime import datetime, date, timezone  # Added timezone
from typing import Optional, List, Dict, Any

import MetaTrader5 as MT5
import certifi
import numpy as np
import pandas as pd
import requests
import telegram
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler

# ==============================================================================
# ------------------------------ CONFIGURATION ---------------------------------
# ==============================================================================

API_KEY = "ae70d5b2ac1e48d3ae414ec3a73a6267"
SECONDARY_API_KEY = "609214e82d4b4fe3a3c747f0160b38b5"
BOT_TOKEN = '8326724921:AAHpESyMA1LwzdWZd5SsSgwbfrccg4yzxEA'
CHAT_ID = '-1002974299440'

SYMBOLS = ["USD/JPY", "AUD/USD", "EUR/USD", "GBP/USD", "GBP/JPY"]
HTF = "1h"
LTF = "15min"

TRADE_LOG_FILE = 'trade_log.csv'
MAX_SIGNALS_PER_DAY = 6
EMA_PERIOD = 50
ATR_PERIOD = 14
ATR_MA_PERIOD = 20
ENTRY_TOLERANCE_PIPS_FOR_MARKET_EXEC = 5

REQUIRE_HTF_BIAS = False
MAX_CALLS_PER_MINUTE = 8
_calls_this_minute = 0
_minute_start = time.time()

SYMBOL_CONFIG = {
    "AUD/USD": { "ENTRY_TOLERANCE_PIPS": 5, "RETEST_CANDLES": 3, "MIN_BOS_RANGE_PIPS": 6, "REQUIRE_REJECTION": True, "SESSION_START_HOUR": 7, "SESSION_END_HOUR": 21, "RECENT_RANGE_CANDLES": 20, "CHOPPY_THRESHOLD": 1.0 },
    "DEFAULT": { "ENTRY_TOLERANCE_PIPS": 4, "RETEST_CANDLES": 3, "MIN_BOS_RANGE_PIPS": 8, "REQUIRE_REJECTION": True, "SESSION_START_HOUR": 7, "SESSION_END_HOUR": 21, "RECENT_RANGE_CANDLES": 20, "CHOPPY_THRESHOLD": 1.2 }
}

SYMBOL_NOTIFICATIONS = {
    "USD/JPY": { "buy": "🟢 BUY USDJPY\nEntry: `{entry:.5f}`\nTP: `{tp:.5f}`\nSL: `{sl:.5f}`", "sell": "🔴 SELL USDJPY\nEntry: `{entry:.5f}`\nTP: `{tp:.5f}`\nSL: `{sl:.5f}`", "pending": "⏳ PENDING {type} USDJPY\nEntry: `{entry:.5f}`\nTP: `{tp:.5f}`\nSL: `{sl:.5f}`" },
    "AUD/USD": { "buy": "🟢 BUY AUDUSD\nEntry: `{entry:.5f}`\nTP: `{tp:.5f}`\nSL: `{sl:.5f}`", "sell": "🔴 SELL AUDUSD\nEntry: `{entry:.5f}`\nTP: `{tp:.5f}`\nSL: `{sl:.5f}`", "pending": "⏳ PENDING {type} AUDUSD\nEntry: `{entry:.5f}`\nTP: `{tp:.5f}`\nSL: `{sl:.5f}`" },
    "EUR/USD": { "buy": "🟢 BUY EURUSD\nEntry: `{entry:.5f}`\nTP: `{tp:.5f}`\nSL: `{sl:.5f}`", "sell": "🔴 SELL EURUSD\nEntry: `{entry:.5f}`\nTP: `{tp:.5f}`\nSL: `{sl:.5f}`", "pending": "⏳ PENDING {type} EURUSD\nEntry: `{entry:.5f}`\nTP: `{tp:.5f}`\nSL: `{sl:.5f}`" },
    "GBP/USD": { "buy": "🟢 BUY GBPUSD\nEntry: `{entry:.5f}`\nTP: `{tp:.5f}`\nSL: `{sl:.5f}`", "sell": "🔴 SELL GBPUSD\nEntry: `{entry:.5f}`\nTP: `{tp:.5f}`\nSL: `{sl:.5f}`", "pending": "⏳ PENDING {type} GBPUSD\nEntry: `{entry:.5f}`\nTP: `{tp:.5f}`\nSL: `{sl:.5f}`" },
    "GBP/JPY": { "buy": "🟢 BUY GBPJPY\nEntry: `{entry:.5f}`\nTP: `{tp:.5f}`\nSL: `{sl:.5f}`", "sell": "🔴 SELL GBPJPY\nEntry: `{entry:.5f}`\nTP: `{tp:.5f}`\nSL: `{sl:.5f}`", "pending": "⏳ PENDING {type} GBPJPY\nEntry: `{entry:.5f}`\nTP: `{tp:.5f}`\nSL: `{sl:.5f}`" }
}

shared_state = { 'is_auto_analyzing': False, 'lock': threading.Lock(), 'active_trades': {}, 'pending_orders': {}, 'signals_today_count': 0, 'last_reset_date': date.today() }
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')

def initialize_trade_log():
    if not os.path.exists(TRADE_LOG_FILE):
        with open(TRADE_LOG_FILE, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Timestamp', 'Symbol', 'Type', 'Entry', 'SL', 'TP', 'Outcome', 'Outcome_Price', 'Outcome_Time'])
        logging.info(f"Created trade log file: {TRADE_LOG_FILE}")

def log_trade_outcome(symbol, trade_info, outcome, outcome_price):
    with open(TRADE_LOG_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([datetime.now().strftime('%Y-%m-%d %H:%M:%S'), symbol, trade_info['type'], trade_info['entry'], trade_info['sl'], trade_info['tp'], outcome, outcome_price, datetime.now().strftime('%Y-%m-%d %H:%M:%S')])

def reset_daily_counter_if_needed():
    with shared_state['lock']:
        today = date.today()
        if shared_state['last_reset_date'] < today:
            logging.info(f"New day. Resetting daily signal counter from {shared_state['signals_today_count']} to 0.")
            shared_state['signals_today_count'] = 0
            shared_state['last_reset_date'] = today

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df[f'ema_{EMA_PERIOD}'] = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df[f'atr_{ATR_PERIOD}'] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()
    df[f'atr_ma_{ATR_MA_PERIOD}'] = df[f'atr_{ATR_PERIOD}'].rolling(window=ATR_MA_PERIOD).mean()
    return df

def get_dynamic_rr(df: pd.DataFrame) -> float:
    last_atr = df[f'atr_{ATR_PERIOD}'].iloc[-1]
    last_atr_ma = df[f'atr_ma_{ATR_MA_PERIOD}'].iloc[-1]
    if pd.isna(last_atr) or pd.isna(last_atr_ma): return 2.0
    if last_atr > last_atr_ma:
        logging.info("High volatility detected. Using 1:2 RR.")
        return 2.0
    else:
        logging.info("Low volatility detected. Using 1:3 RR.")
        return 3.0

def pace_request():
    global _calls_this_minute, _minute_start
    _calls_this_minute += 1
    elapsed = time.time() - _minute_start
    if _calls_this_minute >= MAX_CALLS_PER_MINUTE:
        wait = 60 - elapsed
        if wait > 0:
            logging.info(f"Rate limit hit, waiting for {wait:.2f} seconds.")
            time.sleep(wait)
        _calls_this_minute, _minute_start = 0, time.time()

def pip_size(symbol: str) -> float: return 0.01 if "JPY" in symbol else 0.0001

## MODIFICATION ## - This function is now robust against missing 'volume' data.
def fetch_ohlcv(symbol: str, interval: str, outputsize: int = 500, max_retries: int = 3, api_key: str = API_KEY) -> pd.DataFrame | None:
    for attempt in range(max_retries):
        try:
            pace_request()
            url = "https://api.twelvedata.com/time_series"
            params = {"symbol": symbol, "interval": interval, "apikey": api_key, "outputsize": outputsize}
            r = requests.get(url, params=params, timeout=30, verify=certifi.where())
            r.raise_for_status()
            data = r.json()
            if "values" not in data or not data["values"]:
                logging.warning(f"No values in API response for {symbol} {interval}.")
                return None
            df = pd.DataFrame(data["values"])
            df["time"] = pd.to_datetime(df["datetime"], utc=True)
            for c in ["open", "high", "low", "close"]:
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors='coerce')
                else:
                    logging.error(f"Essential column '{c}' is missing for {symbol}. Cannot proceed.")
                    return None
            if "volume" in df.columns:
                df["volume"] = pd.to_numeric(df["volume"], errors='coerce')
            else:
                df["volume"] = 0
            return df.sort_values("time").reset_index(drop=True)
        except requests.exceptions.RequestException as e:
            logging.error(f"Attempt {attempt + 1}/{max_retries} failed for {symbol} {interval}: {e}")
            if attempt < max_retries - 1: time.sleep(1 + attempt)
        except Exception as e:
            logging.error(f"An unexpected error occurred in fetch_ohlcv for {symbol} {interval}: {e}")
            return None
    return None

def fetch_mt5_ohlcv(symbol: str, timeframe=MT5.TIMEFRAME_M15, count: int = 500) -> Optional[pd.DataFrame]:
    rates = MT5.copy_rates_from_pos(symbol.replace("/", ""), timeframe, 0, count)
    if rates is None or len(rates) == 0:
        logging.error(f"MT5 OHLCV fetch failed for {symbol}")
        return None
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
    # Ensure volume is present
    if 'tick_volume' in df.columns:
        df['volume'] = df['tick_volume']
    elif 'real_volume' in df.columns:
        df['volume'] = df['real_volume']
    else:
        df['volume'] = 0
    return df

def fetch_mt5_orderbook(symbol: str) -> List[Dict[str, Any]]:
    symbol_mt5 = symbol.replace("/", "")
    if not MT5.market_book_add(symbol_mt5):
        logging.warning(f"No orderbook for {symbol}")
        return []
    book = MT5.market_book_get(symbol_mt5)
    if book is None:
        return []
    return [dict(b) for b in book]

def get_mt5_symbol_info(symbol: str) -> Optional[Any]:
    symbol_mt5 = symbol.replace("/", "")
    info = MT5.symbol_info(symbol_mt5)
    if info is None:
        logging.error(f"MT5 symbol info not found for {symbol}")
    return info

def get_min_stop_distance(symbol: str) -> float:
    info = get_mt5_symbol_info(symbol)
    if info is None:
        return 0
    return info.trade_stops_level * info.point

def recalculate_trade_params(entry: float, stop: float, target: float, trade_type: str, pip: float, min_distance_pips: int = 10, symbol: Optional[str] = None) -> (float, float):
    min_distance = min_distance_pips * pip
    if symbol:
        mt5_min = get_min_stop_distance(symbol)
        if mt5_min > min_distance:
            min_distance = mt5_min
    if trade_type == "buy":
        if abs(entry - stop) < min_distance or stop >= entry:
            stop = entry - min_distance
        if abs(target - entry) < min_distance or target <= entry:
            target = entry + min_distance
    else:
        if abs(stop - entry) < min_distance or stop <= entry:
            stop = entry + min_distance
        if abs(entry - target) < min_distance or target >= entry:
            target = entry - min_distance
    return stop, target

# --- Advanced signal filtering and order book/volume integration ---
def generate_signals(df_ltf: pd.DataFrame, symbol: str) -> list[dict]:
    cfg = SYMBOL_CONFIG.get(symbol, SYMBOL_CONFIG["DEFAULT"])
    pip = pip_size(symbol)
    df = df_ltf.copy()
    df = calculate_indicators(df)
    df = detect_swings_and_bos(df)
    df = detect_order_blocks(df)
    # --- Integrate volume and order book data ---
    mt5_df = fetch_mt5_ohlcv(symbol, count=100)
    orderbook = fetch_mt5_orderbook(symbol)
    signals = []
    last_idx = len(df) - 2
    if last_idx < 1: return []
    row = df.iloc[last_idx]
    dt, entry_price = row["time"], row["close"]
    rr_ratio = get_dynamic_rr(df)
    ema = df[f'ema_{EMA_PERIOD}'].iloc[last_idx]
    # --- Filter out signals with low volume or weak orderbook support ---
    if mt5_df is not None and len(mt5_df) > 10:
        recent_vol = mt5_df['volume'].iloc[-10:].mean()
        if recent_vol < mt5_df['volume'].mean() * 0.7:
            logging.info(f"Skipping {symbol} signal: recent volume too low.")
            return []
    if orderbook:
        # Example: require at least 2x more bids than asks for buy, or vice versa for sell
        bids = sum([b['volume'] for b in orderbook if b['type'] == MT5.BOOK_TYPE_BUY])
        asks = sum([b['volume'] for b in orderbook if b['type'] == MT5.BOOK_TYPE_SELL])
    else:
        bids = asks = 0
    # --- Main signal logic ---
    if dt.weekday() != 5 and in_session(dt, cfg["SESSION_START_HOUR"], cfg["SESSION_END_HOUR"]):
        recent = df.iloc[max(0, last_idx - cfg["RECENT_RANGE_CANDLES"]): last_idx + 1]
        if not is_choppy(recent, cfg["RECENT_RANGE_CANDLES"], cfg["CHOPPY_THRESHOLD"]):
            # --- BUY ---
            if (
                row["BOS"] == 1 and entry_price > ema and
                is_retest(df, last_idx, cfg["ENTRY_TOLERANCE_PIPS"], pip, cfg["RETEST_CANDLES"]) and
                (is_liquidity_sweep(df, last_idx, "buy") or is_rejection(df, last_idx, "buy", cfg["REQUIRE_REJECTION"])) and
                (bids > 2 * asks if bids and asks else True)
            ):
                if (row["high"] - df.iloc[last_idx - 1]["low"]) >= cfg["MIN_BOS_RANGE_PIPS"] * pip:
                    stop = float(df.iloc[max(0, last_idx - 5):last_idx]["low"].min())
                    perfect_entry = predict_perfect_entry(df, last_idx, "buy")
                    entry_price = perfect_entry if perfect_entry is not None else entry_price
                    target = entry_price + (entry_price - stop) * rr_ratio
                    stop, target = recalculate_trade_params(entry_price, stop, target, "buy", pip, symbol=symbol)
                    if (
                        abs(entry_price - stop) < pip or
                        abs(entry_price - target) < pip or
                        abs(stop - target) < pip
                    ):
                        logging.warning(f"Skipped invalid BUY signal for {symbol}: entry={entry_price}, stop={stop}, target={target}")
                    else:
                        signals.append({"type": "buy", "entry": float(entry_price), "stop": float(stop), "target": float(target), "time": dt})
            # --- SELL ---
            elif (
                row["BOS"] == -1 and entry_price < ema and
                is_retest(df, last_idx, cfg["ENTRY_TOLERANCE_PIPS"], pip, cfg["RETEST_CANDLES"]) and
                (is_liquidity_sweep(df, last_idx, "sell") or is_rejection(df, last_idx, "sell", cfg["REQUIRE_REJECTION"])) and
                (asks > 2 * bids if bids and asks else True)
            ):
                if (df.iloc[last_idx - 1]["high"] - row["low"]) >= cfg["MIN_BOS_RANGE_PIPS"] * pip:
                    stop = float(df.iloc[max(0, last_idx - 5):last_idx]["high"].max())
                    perfect_entry = predict_perfect_entry(df, last_idx, "sell")
                    entry_price = perfect_entry if perfect_entry is not None else entry_price
                    target = entry_price - (stop - entry_price) * rr_ratio
                    stop, target = recalculate_trade_params(entry_price, stop, target, "sell", pip, symbol=symbol)
                    if (
                        abs(entry_price - stop) < pip or
                        abs(entry_price - target) < pip or
                        abs(stop - target) < pip
                    ):
                        logging.warning(f"Skipped invalid SELL signal for {symbol}: entry={entry_price}, stop={stop}, target={target}")
                    else:
                        signals.append({"type": "sell", "entry": float(entry_price), "stop": float(stop), "target": float(target), "time": dt})
    return signals

# --- Robust MT5 execution (single definition only) ---
def run_hft_signal_execution(symbol: str, signal_id: str, entry: float, stop: float, target: float, trade_type: str,
                             lot_size=0.05):
    symbol_mt5 = symbol.replace("/", "")
    info = get_mt5_symbol_info(symbol)
    if info is None:
        logging.error(f"Cannot execute trade: symbol info not found for {symbol}")
        return
    lot = lot_size
    deviation = 10
    order_type = MT5.ORDER_TYPE_BUY if trade_type == "buy" else MT5.ORDER_TYPE_SELL
    price = entry
    sl = stop
    tp = target
    pip = pip_size(symbol)
    sl, tp = recalculate_trade_params(price, sl, tp, trade_type, pip, symbol=symbol)
    if (
        abs(price - sl) < pip or
        abs(price - tp) < pip or
        abs(sl - tp) < pip
    ):
        logging.error(f"Order not sent: Invalid stops for {symbol} (entry={price}, sl={sl}, tp={tp})")
        return
    request = {
        "action": MT5.TRADE_ACTION_DEAL,
        "symbol": symbol_mt5,
        "volume": lot,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": deviation,
        "magic": 123456,
        "comment": f"HFT-{signal_id}",
        "type_time": MT5.ORDER_TIME_GTC,
        "type_filling": MT5.ORDER_FILLING_IOC,
    }
    result = MT5.order_send(request)
    if result.retcode != MT5.TRADE_RETCODE_DONE:
        logging.error(f"MT5 order_send failed: {result.retcode} {result.comment}")
    else:
        logging.info(f"MT5 order placed: {symbol} {trade_type} at {price} SL:{sl} TP:{tp}")

def run_single_analysis(symbol: str):
    try:
        with shared_state['lock']:
            if symbol in shared_state['active_trades'] or symbol in shared_state['pending_orders']: return
            if shared_state['signals_today_count'] >= MAX_SIGNALS_PER_DAY: return
        logging.info(f"Analyzing {symbol}...")
        df_ltf = fetch_ohlcv(symbol, LTF, outputsize=500)
        if df_ltf is None or len(df_ltf) < 50:
            logging.warning(f"Skipping {symbol}: Not enough LTF data.")
            return
        signals = generate_signals(df_ltf, symbol)
        if not signals:
            logging.info(f"No valid signal found for {symbol}.")
            return
        sig = signals[-1]
        current_price = df_ltf.iloc[-1]['close']
        pip = pip_size(symbol)
        is_market_execution = price_within_pips(current_price, sig['entry'], ENTRY_TOLERANCE_PIPS_FOR_MARKET_EXEC, pip)
        notify = SYMBOL_NOTIFICATIONS.get(symbol)
        signal_id = f"{symbol}_{sig['time']}"
        if is_market_execution:
            logging.warning(f">>> MARKET SIGNAL for {symbol}! Sending notification. <<<")
            msg_template = notify[sig['type']]
            message = msg_template.format(entry=sig['entry'], tp=sig['target'], sl=sig['stop'])
            sent_message = asyncio.run(send_telegram_message(message))
            if sent_message:
                with shared_state['lock']:
                    shared_state['active_trades'][symbol] = {"type": sig['type'], "entry": sig['entry'], "tp": sig['target'], "sl": sig['stop'], "message_id": sent_message.message_id}
                    shared_state['signals_today_count'] += 1
                    logging.info(f"Active trade for {symbol}. Total signals today: {shared_state['signals_today_count']}")
                # HFT execution
                run_hft_signal_execution(symbol, signal_id, sig['entry'], sig['stop'], sig['target'], sig['type'])
        else:
            logging.warning(f">>> PENDING ORDER for {symbol}! Sending notification. <<<")
            msg_template = notify['pending']
            message = msg_template.format(type=sig['type'].upper(), entry=sig['entry'], tp=sig['target'], sl=sig['stop'])
            sent_message = asyncio.run(send_telegram_message(message))
            if sent_message:
                with shared_state['lock']:
                    shared_state['pending_orders'][symbol] = {"type": sig['type'], "entry": sig['entry'], "tp": sig['target'], "sl": sig['stop'], "message_id": sent_message.message_id}
                    logging.info(f"Pending order for {symbol} is now active.")
    except Exception as e: logging.error(f"Error in run_single_analysis for {symbol}: {e}", exc_info=True)

def run_high_vol_analysis(symbol: str):
    try:
        with shared_state['lock']:
            if symbol in shared_state['active_trades'] or symbol in shared_state['pending_orders']: return
            if shared_state['signals_today_count'] >= MAX_SIGNALS_PER_DAY: return
        logging.info(f"[VOLATILE] Analyzing {symbol}...")
        df_ltf = fetch_ohlcv(symbol, LTF, outputsize=500)
        if df_ltf is None or len(df_ltf) < 50: return
        signals = generate_signals(df_ltf, symbol)
        if not signals: return
        sig = signals[-1]
        logging.warning(f">>> [VOLATILE] MARKET SIGNAL for {symbol}! <<<")
        notify = SYMBOL_NOTIFICATIONS.get(symbol)
        msg_template = notify[sig['type']]
        message = msg_template.format(entry=sig['entry'], tp=sig['target'], sl=sig['stop'])
        sent_message = asyncio.run(send_telegram_message(message))
        if sent_message:
            with shared_state['lock']:
                shared_state['active_trades'][symbol] = {"type": sig['type'], "entry": sig['entry'], "tp": sig['target'], "sl": sig['stop'], "message_id": sent_message.message_id}
                shared_state['signals_today_count'] += 1
                logging.info(f"[VOLATILE] Active trade for {symbol}. Total signals: {shared_state['signals_today_count']}")
    except Exception as e: logging.error(f"Error in high_vol_analysis for {symbol}: {e}", exc_info=True)

def check_pending_orders():
    with shared_state['lock']:
        pending_symbols = list(shared_state['pending_orders'].keys())
    if not pending_symbols: return
    logging.info(f"Checking pending orders for: {', '.join(pending_symbols)}")
    for symbol in pending_symbols:
        try:
            df = fetch_ohlcv(symbol, "1min", outputsize=2, api_key=SECONDARY_API_KEY)
            if df is None or df.empty: continue
            current_high, current_low = df.iloc[-1]['high'], df.iloc[-1]['low']
            with shared_state['lock']:
                pending_info = shared_state['pending_orders'].get(symbol)
            if not pending_info: continue
            entry_price, trade_type = pending_info['entry'], pending_info['type']
            is_triggered = (trade_type == 'buy' and current_low <= entry_price <= current_high) or (trade_type == 'sell' and current_low <= entry_price <= current_high)
            if is_triggered:
                logging.warning(f"PENDING ORDER TRIGGERED for {symbol} at {entry_price}")
                with shared_state['lock']:
                    if shared_state['signals_today_count'] >= MAX_SIGNALS_PER_DAY:
                        logging.warning(f"Daily limit reached. Cancelling pending order for {symbol}.")
                        asyncio.run(delete_telegram_message(pending_info['message_id']))
                        del shared_state['pending_orders'][symbol]
                        continue
                asyncio.run(delete_telegram_message(pending_info['message_id']))
                notify = SYMBOL_NOTIFICATIONS.get(symbol)
                msg_template = notify[trade_type]
                message = msg_template.format(entry=pending_info['entry'], tp=pending_info['tp'], sl=pending_info['sl'])
                new_sent_message = asyncio.run(send_telegram_message(message))
                if new_sent_message:
                    with shared_state['lock']:
                        shared_state['active_trades'][symbol] = {"type": trade_type, "entry": entry_price, "tp": pending_info['tp'], "sl": pending_info['sl'], "message_id": new_sent_message.message_id}
                        del shared_state['pending_orders'][symbol]
                        shared_state['signals_today_count'] += 1
                        logging.info(f"Pending order for {symbol} is now active. Total signals: {shared_state['signals_today_count']}")
        except Exception as e: logging.error(f"Error checking pending order for {symbol}: {e}", exc_info=True)
        time.sleep(2)

def check_active_trades():
    with shared_state['lock']:
        active_symbols = list(shared_state['active_trades'].keys())
    if not active_symbols: return
    logging.info(f"Checking active trades for: {', '.join(active_symbols)}")
    for symbol in active_symbols:
        try:
            df = fetch_ohlcv(symbol, "1min", outputsize=2, api_key=SECONDARY_API_KEY)
            if df is None or df.empty: continue
            current_high, current_low = df.iloc[-1]['high'], df.iloc[-1]['low']
            with shared_state['lock']:
                trade_info = shared_state['active_trades'].get(symbol)
            if not trade_info: continue
            tp, sl, trade_type, msg_id = trade_info['tp'], trade_info['sl'], trade_info['type'], trade_info['message_id']
            outcome, outcome_price = None, None
            if trade_type == 'buy':
                if current_high >= tp: outcome, outcome_price = 'TP_HIT', tp
                elif current_low <= sl: outcome, outcome_price = 'SL_HIT', sl
            elif trade_type == 'sell':
                if current_low <= tp: outcome, outcome_price = 'TP_HIT', tp
                elif current_high >= sl: outcome, outcome_price = 'SL_HIT', sl
            if outcome:
                logging.warning(f"OUTCOME for {symbol}: {outcome} at {outcome_price}")
                log_trade_outcome(symbol, trade_info, outcome, outcome_price)
                result_prefix = "✅ TP HIT" if "TP" in outcome else "❌ SL HIT"
                result_msg = f"{result_prefix}: {symbol} {trade_type.upper()} from {trade_info['entry']:.5f} has hit its target."
                asyncio.run(send_telegram_message(result_msg))
                asyncio.run(delete_telegram_message(msg_id))
                with shared_state['lock']: del shared_state['active_trades'][symbol]
                logging.info(f"Trade for {symbol} closed and removed.")
        except Exception as e: logging.error(f"Error checking active trade for {symbol}: {e}", exc_info=True)
        time.sleep(2)

def auto_analysis_loop():
    logging.info("Analysis thread started. Send /start to begin.")
    while True:
        with shared_state['lock']:
            is_running = shared_state['is_auto_analyzing']
        if is_running:
            reset_daily_counter_if_needed()
            check_pending_orders()
            check_active_trades()
            logging.info("\n--- Starting analysis cycle ---")
            now_utc = datetime.now(timezone.utc)
            for symbol in SYMBOLS:
                if is_high_volatility_period(now_utc):
                    run_high_vol_analysis(symbol)
                else:
                    run_single_analysis(symbol)
                time.sleep(5)
            logging.info("--- Cycle complete. Waiting 15 minutes. ---")
            time.sleep(15 * 60)
        else:
            time.sleep(10)

def in_session(dt_utc: datetime, start_hr: int, end_hr: int) -> bool:
    t = dt_utc.time()
    return datetime.min.replace(hour=start_hr).time() <= t <= datetime.min.replace(hour=end_hr).time()

def detect_swings_and_bos(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy().reset_index(drop=True)
    df["swing_high"] = (df["high"] > df["high"].shift(1)) & (df["high"] > df["high"].shift(-1))
    df["swing_low"] = (df["low"] < df["low"].shift(1)) & (df["low"] < df["low"].shift(-1))
    df["BOS"] = 0
    prev_swing_high, prev_swing_low = None, None
    for idx, row in df.iterrows():
        if row["swing_high"]:
            if prev_swing_high is not None and row["high"] > prev_swing_high: df.at[idx, "BOS"] = 1
            prev_swing_high = row["high"]
        if row["swing_low"]:
            if prev_swing_low is not None and row["low"] < prev_swing_low: df.at[idx, "BOS"] = -1
            prev_swing_low = row["low"]
    return df

def detect_order_blocks(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["order_block"] = np.nan
    for idx, row in df.iterrows():
        if row["BOS"] != 0 and idx > 0: df.at[idx - 1, "order_block"] = row["BOS"]
    return df

def price_within_pips(p1: float, p2: float, pips: float, pip: float) -> bool: return abs(p1 - p2) <= pips * pip

def is_retest(df: pd.DataFrame, idx: int, entry_tolerance_pips: int, pip: float, retest_candles: int) -> bool:
    if idx < retest_candles: return False
    ob_idx = idx - 1
    if pd.isna(df.at[ob_idx, "order_block"]): return False
    ob_close = df.at[ob_idx, "close"]
    for i in range(idx - retest_candles, idx):
        if price_within_pips(df.at[i, "low"], ob_close, entry_tolerance_pips, pip) or price_within_pips(df.at[i, "high"], ob_close, entry_tolerance_pips, pip):
            return True
    return False

def is_liquidity_sweep(df: pd.DataFrame, idx: int, trade_type: str, window: int = 6) -> bool:
    if idx < window: return False
    recent = df.iloc[idx - window:idx]
    if trade_type == "buy":
        prev_high = recent["high"].max()
        return df.at[idx, "high"] > prev_high > df.at[idx, "close"]
    else:
        prev_low = recent["low"].min()
        return df.at[idx, "low"] < prev_low < df.at[idx, "close"]


def is_rejection(df: pd.DataFrame, idx: int, trade_type: str, require_rejection: bool) -> bool:
    if not require_rejection: return True
    row = df.iloc[idx]
    body, rng = abs(row["close"] - row["open"]), row["high"] - row["low"]
    if rng == 0: return False
    if trade_type == "buy": return (body / rng < 0.3) and ((min(row["close"], row["open"]) - row["low"]) / rng > 0.6)
    else: return (body / rng < 0.3) and ((row["high"] - max(row["close"], row["open"])) / rng > 0.6)

def is_choppy(df_recent: pd.DataFrame, candles: int, threshold: float) -> bool:
    if len(df_recent) < candles: return False
    recent = df_recent.iloc[-candles:]
    price_range = recent["high"].max() - recent["low"].min()
    avg_body = np.mean(np.abs(recent["close"] - recent["open"]).replace(0, np.nan))
    if np.isnan(avg_body) or avg_body == 0: return True
    return (price_range / avg_body) < threshold

def compute_htf_bias(df_htf: pd.DataFrame) -> str | None:
    if len(df_htf) < 20: return None
    sub = detect_swings_and_bos(df_htf.tail(20).copy())
    swing_highs, swing_lows = sub[sub["swing_high"]], sub[sub["swing_low"]]
    if len(swing_highs) < 2 or len(swing_lows) < 2: return None
    last_high, second_last_high = swing_highs.iloc[-1]["high"], swing_highs.iloc[-2]["high"]
    last_low, second_last_low = swing_lows.iloc[-1]["low"], swing_lows.iloc[-2]["low"]
    if last_high > second_last_high and last_low > second_last_low: return "bull"
    elif last_high < second_last_high and last_low < second_last_low: return "bear"
    else: return None

def predict_perfect_entry(df: pd.DataFrame, idx: int, trade_type: str) -> float | None:
    window = df.iloc[max(0, idx - 3):idx]
    if trade_type == "buy": return window["low"].min()
    elif trade_type == "sell": return window["high"].max()
    return None

async def send_telegram_message(message: str) -> telegram.Message | None:
    bot = telegram.Bot(token=BOT_TOKEN)
    try: return await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")
        return None

async def delete_telegram_message(message_id: int):
    bot = telegram.Bot(token=BOT_TOKEN)
    try:
        await bot.delete_message(chat_id=CHAT_ID, message_id=message_id)
        logging.info(f"Deleted message ID: {message_id}")
    except BadRequest as e:
        if "message to delete not found" in e.message: logging.warning(f"Message {message_id} already deleted.")
        else: logging.error(f"Failed to delete message ID {message_id}: {e}")
    except Exception as e: logging.error(f"Unexpected error deleting message ID {message_id}: {e}")

def mt5_initialize() -> bool:
    """Initializes MetaTrader 5 and returns True if successful, else False."""
    if MT5.initialize():
        logging.info("MetaTrader 5 initialized successfully.")
        return True
    else:
        logging.error(f"MetaTrader 5 initialization failed: {MT5.last_error()}")
        return False

async def start_command(update: telegram.Update):
    with shared_state['lock']:
        if not shared_state['is_auto_analyzing']:
            shared_state['is_auto_analyzing'] = True
            await update.message.reply_text("✅ I have started Auto Analysis.")
        else:
            await update.message.reply_text("ℹ️ Auto-analysis is already running.")

async def stop_command(update: telegram.Update):
    with shared_state['lock']:
        if shared_state['is_auto_analyzing']:
            shared_state['is_auto_analyzing'] = False
            await update.message.reply_text("❌ Ok, i've Stopped Auto Analysis.")
        else:
            await update.message.reply_text("ℹ️ Auto-analysis is already stopped.")

async def status_command(update: telegram.Update):
    with shared_state['lock']:
        is_running, active_trades, pending_orders, signals_count = shared_state['is_auto_analyzing'], shared_state['active_trades'], shared_state['pending_orders'], shared_state['signals_today_count']
    status_message = "✅ Auto-analysis is RUNNING." if is_running else "❌ Auto-analysis is STOPPED."
    status_message += f"\nSignals today: {signals_count}/{MAX_SIGNALS_PER_DAY}"
    if active_trades:
        status_message += "\n\nActive Trades:"
        for s, i in active_trades.items(): status_message += f"\n- {s} {i['type'].upper()} (TP: {i['tp']:.5f}, SL: {i['sl']:.5f})"
    if pending_orders:
        status_message += "\n\nPending Orders:"
        for s, i in pending_orders.items(): status_message += f"\n- {s} {i['type'].upper()} (Entry: {i['entry']:.5f})"
    if not active_trades and not pending_orders:
        status_message += "\nNo active or pending trades."
    await update.message.reply_text(status_message)

def is_high_volatility_period(now_utc: datetime) -> bool:
    """
    Returns True if the current UTC time is during a typical high-volatility period (e.g., London/NY overlap).
    Adjust the hours as needed for your strategy.
    """
    # London/NY overlap: 12:00 - 16:00 UTC (example)
    return 12 <= now_utc.hour < 16

def main():
    if not all([BOT_TOKEN, API_KEY, SECONDARY_API_KEY]):
        logging.error("API keys or Bot Token are not configured. Please edit the script.")
        return
    if not mt5_initialize():
        logging.error("MT5 initialization failed. Exiting.")
        return
    initialize_trade_log()
    analysis_thread = threading.Thread(target=auto_analysis_loop, daemon=True)
    analysis_thread.start()
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("command1", start_command))
    application.add_handler(CommandHandler("command2", stop_command))
    application.add_handler(CommandHandler("command3", status_command))
    logging.info("=" * 40 + "\n  Upgraded Bot is RUNNING\n  Send /start to begin.\n" + "=" * 40)
    application.run_polling()

if __name__ == "__main__":
    main()