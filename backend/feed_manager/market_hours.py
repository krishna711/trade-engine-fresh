"""
Market Hours & Trading Session Management
Provides market session awareness, non-market hours and weekend session anchoring,
and session-constrained historical OHLC generation for NSE, NFO, BSE, BSEFO, and MCX.
"""

import datetime
import hashlib
import random
from typing import Dict, Any, List, Optional, Tuple

IST_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

NSE_MARKET_OPEN = datetime.time(9, 15, 0)
NSE_MARKET_CLOSE = datetime.time(15, 30, 0)

MCX_MARKET_OPEN = datetime.time(9, 0, 0)
MCX_MARKET_CLOSE = datetime.time(23, 30, 0)

DEFAULT_BASE_PRICES = {
    "NSE:NIFTY": 22485.0,
    "NSE:BANKNIFTY": 48250.0,
    "BSE:SENSEX": 73800.0,
    "MCX:CRUDEOIL": 8377.0,
    "MCX:CRUDEOILM": 8377.0,
    "MCX:NATURALGAS": 185.0,
    "MCX:GOLD": 71500.0,
    "MCX:SILVER": 83200.0,
    "NSE:HAL": 4250.0,
    "NSE:RELIANCE": 2980.0,
    "NSE:BHARTIARTL": 1610.0,
}


def is_mcx_symbol(symbol: str) -> bool:
    sym_u = (symbol or "").upper()
    return "MCX" in sym_u or any(c in sym_u for c in ["CRUDEOIL", "NATURALGAS", "GOLD", "SILVER"])


def get_market_hours_for_symbol(symbol: str) -> Tuple[datetime.time, datetime.time]:
    if is_mcx_symbol(symbol):
        return MCX_MARKET_OPEN, MCX_MARKET_CLOSE
    return NSE_MARKET_OPEN, NSE_MARKET_CLOSE


def get_current_ist(now_dt: Optional[datetime.datetime] = None) -> datetime.datetime:
    if now_dt is None:
        return datetime.datetime.now(IST_TZ)
    if now_dt.tzinfo is None:
        return now_dt.replace(tzinfo=datetime.timezone.utc).astimezone(IST_TZ)
    return now_dt.astimezone(IST_TZ)


def is_market_open(symbol: str, now_dt: Optional[datetime.datetime] = None) -> bool:
    """Returns True if the exchange for this symbol is currently in an active trading session."""
    dt_ist = get_current_ist(now_dt)
    if dt_ist.weekday() >= 5:  # Saturday (5) or Sunday (6)
        return False
    open_time, close_time = get_market_hours_for_symbol(symbol)
    curr_time = dt_ist.time()
    return open_time <= curr_time <= close_time


def get_last_completed_trading_session_anchor(
    symbol: str,
    resolution: str = "5m",
    now_dt: Optional[datetime.datetime] = None
) -> datetime.datetime:
    """
    Returns the latest valid market session timestamp in IST.
    During market hours: returns the current minute rounded down to resolution bucket.
    Outside market hours (evening, early morning, or weekend): returns the exact close
    timestamp of the most recent completed market trading session (e.g. Friday 15:30 IST).
    """
    dt_ist = get_current_ist(now_dt)
    open_time, close_time = get_market_hours_for_symbol(symbol)
    step_min = parse_resolution_minutes(resolution)

    # 1. Weekend (Saturday or Sunday) -> Anchor to most recent Friday's market close
    if dt_ist.weekday() >= 5:
        days_back = 1 if dt_ist.weekday() == 5 else 2
        friday_date = dt_ist.date() - datetime.timedelta(days=days_back)
        return datetime.datetime.combine(friday_date, close_time, tzinfo=IST_TZ)

    curr_time = dt_ist.time()

    # 2. Weekday before market open -> Anchor to previous trading day's market close
    if curr_time < open_time:
        days_back = 3 if dt_ist.weekday() == 0 else 1
        prev_date = dt_ist.date() - datetime.timedelta(days=days_back)
        return datetime.datetime.combine(prev_date, close_time, tzinfo=IST_TZ)

    # 3. Weekday after market close -> Anchor to today's market close
    if curr_time >= close_time:
        return datetime.datetime.combine(dt_ist.date(), close_time, tzinfo=IST_TZ)

    # 4. Live market session -> Round down to current bucket
    minute_bucket = (dt_ist.minute // step_min) * step_min
    return dt_ist.replace(minute=minute_bucket, second=0, microsecond=0)


def parse_resolution_minutes(resolution: str) -> int:
    res = (resolution or "5m").strip().lower()
    if res in ["1m", "1"]:
        return 1
    if res in ["3m", "3"]:
        return 3
    if res in ["5m", "5"]:
        return 5
    if res in ["15m", "15"]:
        return 15
    if res in ["1h", "60m", "60"]:
        return 60
    # Extract any digits
    digits = "".join([c for c in res if c.isdigit()])
    return int(digits) if digits else 5


def generate_market_session_timestamps(
    symbol: str,
    resolution: str = "5m",
    count: int = 50,
    now_dt: Optional[datetime.datetime] = None
) -> List[datetime.datetime]:
    """
    Generates 'count' timestamps backward from the session anchor, skipping non-trading
    hours and weekends. For multi-day lookbacks, timestamps cleanly step from 09:15 back
    to previous trading day's 15:30 (or MCX 23:30), creating two or more continuous
    trading sessions. This ensures Floor Pivots, Opening Ranges, and session indicators
    calculate accurately even on weekends and during off-market hours.
    """
    anchor = get_last_completed_trading_session_anchor(symbol, resolution, now_dt)
    open_time, close_time = get_market_hours_for_symbol(symbol)
    step_min = parse_resolution_minutes(resolution)

    timestamps: List[datetime.datetime] = []
    curr = anchor

    for _ in range(count):
        timestamps.append(curr)

        # Step back by resolution
        prev = curr - datetime.timedelta(minutes=step_min)

        # Check if stepped before market open of that day
        if prev.time() < open_time:
            # Jump back to previous trading day's close
            days_back = 3 if curr.weekday() == 0 else 1  # Monday -> Friday, else yesterday
            prev_date = curr.date() - datetime.timedelta(days=days_back)
            curr = datetime.datetime.combine(prev_date, close_time, tzinfo=IST_TZ)
        else:
            curr = prev

    # Return sorted chronologically (oldest to newest)
    timestamps.reverse()
    return timestamps


def build_market_session_bars(
    symbol: str,
    resolution: str = "5m",
    count: int = 50,
    base_price: float = 0.0,
    seed_str: Optional[str] = None,
    now_dt: Optional[datetime.datetime] = None
) -> List[Dict[str, Any]]:
    """
    Builds deterministic, market-hours aligned OHLC bars across trading sessions.
    Guarantees valid OHLC geometry, consistent weekend curve persistence,
    and valid multi-day shift compatibility for Floor Pivots and Opening Ranges.
    """
    timestamps = generate_market_session_timestamps(symbol, resolution, count, now_dt)
    if not timestamps:
        return []

    # Determine reference base price
    p_ref = float(base_price)
    if p_ref <= 0.0:
        if symbol in DEFAULT_BASE_PRICES:
            p_ref = DEFAULT_BASE_PRICES[symbol]
        elif "HAL" in symbol.upper():
            p_ref = 4250.0
        elif "RELIANCE" in symbol.upper():
            p_ref = 2980.0
        elif "BHARTI" in symbol.upper():
            p_ref = 1610.0
        elif "CRUDE" in symbol.upper():
            p_ref = 8377.0
        elif "NIFTY" in symbol.upper():
            p_ref = 22485.0
        elif "BANKNIFTY" in symbol.upper():
            p_ref = 48250.0
        elif "SENSEX" in symbol.upper():
            p_ref = 73800.0
        else:
            p_ref = 1000.0

    is_option = symbol.endswith("CE") or symbol.endswith("PE")
    if is_option and p_ref > 5000:
        # Default option strike premium estimation if underlying price was accidentally passed
        p_ref = 350.0

    # Deterministic seed anchored to symbol, resolution, and the anchor date
    anchor_date_str = timestamps[-1].strftime("%Y%m%d")
    seed_key = seed_str or f"{symbol}_{resolution}_{anchor_date_str}"
    seed_int = int(hashlib.md5(seed_key.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed_int)

    # Volatility per step
    step_min = parse_resolution_minutes(resolution)
    vol_pct = (0.0012 if not is_option else 0.015) * (step_min ** 0.5)

    bars: List[Dict[str, Any]] = []
    # Start slightly displaced so the final bar lands exactly at p_ref
    drift_factor = 1.0 - (rng.random() - 0.5) * 0.02
    current_p = p_ref * drift_factor

    for i, ts in enumerate(timestamps):
        is_last = (i == len(timestamps) - 1)
        if is_last and p_ref > 0:
            close_p = round(p_ref, 2)
            open_p = round(current_p, 2)
        else:
            delta = (rng.random() - 0.49) * (current_p * vol_pct)
            open_p = round(current_p, 2)
            close_p = round(open_p + delta, 2)
            if is_option and close_p < 2.0:
                close_p = round(2.0 + rng.random() * 5.0, 2)

        # High / Low wicks
        wick_high = abs(rng.random() * (open_p * vol_pct * 0.75))
        wick_low = abs(rng.random() * (open_p * vol_pct * 0.75))
        high_p = round(max(open_p, close_p) + wick_high, 2)
        low_p = round(min(open_p, close_p) - wick_low, 2)
        if is_option and low_p < 0.5:
            low_p = 0.5

        vol = rng.randint(2000, 75000) if not is_option else rng.randint(500, 15000)

        bars.append({
            "symbol": symbol,
            "time": ts.isoformat(),
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "volume": vol
        })
        current_p = close_p

    return bars
