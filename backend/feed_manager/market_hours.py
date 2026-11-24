"""
Market Hours & Trading Session Management
Provides market session awareness, dynamic exchange holiday calendar enforcement,
non-market hours and weekend/holiday session anchoring, and session-constrained
historical OHLC generation for NSE, NFO, BSE, BSEFO, and MCX.

Holidays are stored dynamically in the SQLite database (trading_holidays table)
and managed by Administrators via the UI, avoiding hardcoded dates.
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
MCX_EVENING_SESSION_OPEN = datetime.time(17, 0, 0)

DEFAULT_BASE_PRICES = {
    "NSE:NIFTY": 22485.0,
    "NSE:BANKNIFTY": 48250.0,
    "BSE:SENSEX": 73800.0,
    "MCX:CRUDEOIL": 8033.0,
    "MCX:CRUDEOILM": 8033.0,
    "MCX:NATURALGAS": 185.0,
    "MCX:GOLD": 71500.0,
    "MCX:SILVER": 83200.0,
    "NSE:HAL": 4250.0,
    "NSE:RELIANCE": 2980.0,
    "NSE:BHARTIARTL": 1610.0,
}

# ==============================================================================
# DYNAMIC EXCHANGE TRADING HOLIDAYS (LOADED FROM SQLITE DB)
# ==============================================================================

_HOLIDAYS_CACHE: Optional[List[Dict[str, Any]]] = None
_HOLIDAYS_CACHE_TIME: Optional[datetime.datetime] = None

# Fallback default seeds if DB is uninitialized or in test runner
DEFAULT_HOLIDAYS_FALLBACK: List[Dict[str, Any]] = [
    {"date": datetime.date(2026, 11, 24), "description": "Prakash Gurpurb Sri Guru Nanak Dev Jayanti", "exchange": "ALL", "session": "FULL_DAY"},
    {"date": datetime.date(2026, 12, 25), "description": "Christmas", "exchange": "ALL", "session": "FULL_DAY"},
    {"date": datetime.date(2027, 1, 26), "description": "Republic Day", "exchange": "ALL", "session": "FULL_DAY"},
    {"date": datetime.date(2027, 3, 10), "description": "Id-ul-Fitr (Ramzan Eid)", "exchange": "ALL", "session": "FULL_DAY"},
    {"date": datetime.date(2027, 3, 22), "description": "Holi", "exchange": "ALL", "session": "FULL_DAY"},
    {"date": datetime.date(2027, 3, 26), "description": "Good Friday", "exchange": "ALL", "session": "FULL_DAY"},
    {"date": datetime.date(2027, 4, 14), "description": "Dr. Baba Saheb Ambedkar Jayanti", "exchange": "ALL", "session": "FULL_DAY"},
    {"date": datetime.date(2027, 4, 15), "description": "Shri Ram Navami", "exchange": "ALL", "session": "FULL_DAY"},
    {"date": datetime.date(2027, 4, 19), "description": "Shri Mahavir Jayanti", "exchange": "ALL", "session": "FULL_DAY"},
    {"date": datetime.date(2027, 5, 17), "description": "Bakri Id (Eid-ul-Zuha)", "exchange": "ALL", "session": "FULL_DAY"},
    {"date": datetime.date(2027, 6, 15), "description": "Muharram", "exchange": "ALL", "session": "FULL_DAY"},
    {"date": datetime.date(2027, 10, 29), "description": "Diwali (Laxmi Pujan)", "exchange": "ALL", "session": "FULL_DAY"},
    {"date": datetime.date(2027, 11, 1), "description": "Diwali Balipratipada", "exchange": "ALL", "session": "FULL_DAY"},
]

def get_holidays_from_db(force_reload: bool = False) -> List[Dict[str, Any]]:
    """
    Retrieves active trading holidays from SQLite database table (trading_holidays).
    Maintains an in-memory cache to guarantee sub-microsecond latency during market execution.
    """
    global _HOLIDAYS_CACHE, _HOLIDAYS_CACHE_TIME
    now = datetime.datetime.utcnow()
    if not force_reload and _HOLIDAYS_CACHE is not None and _HOLIDAYS_CACHE_TIME is not None:
        if (now - _HOLIDAYS_CACHE_TIME).total_seconds() < 300:  # Cache for 5 minutes
            return _HOLIDAYS_CACHE

    try:
        from backend.database.connection import SessionLocal
        from backend.database.models import TradingHoliday
        db = SessionLocal()
        try:
            records = db.query(TradingHoliday).filter(TradingHoliday.is_active == True).all()
            if records:
                _HOLIDAYS_CACHE = [
                    {
                        "id": r.id,
                        "date": r.holiday_date if isinstance(r.holiday_date, datetime.date) else datetime.date.fromisoformat(str(r.holiday_date)),
                        "description": r.description,
                        "exchange": (r.exchange or "ALL").upper(),
                        "session": (r.session or "FULL_DAY").upper()
                    }
                    for r in records
                ]
                _HOLIDAYS_CACHE_TIME = now
                return _HOLIDAYS_CACHE
        finally:
            db.close()
    except Exception as e:
        pass

    if _HOLIDAYS_CACHE is not None:
        return _HOLIDAYS_CACHE

    return DEFAULT_HOLIDAYS_FALLBACK


def reload_holidays_cache() -> List[Dict[str, Any]]:
    """Forces an immediate reload of the in-memory trading holidays cache from SQLite."""
    return get_holidays_from_db(force_reload=True)


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
        return now_dt.replace(tzinfo=IST_TZ)
    return now_dt.astimezone(IST_TZ)


def is_trading_holiday(symbol: str, now_dt: Optional[datetime.datetime] = None) -> Tuple[bool, Optional[str]]:
    """
    Returns (is_holiday, holiday_name) for a given symbol and timestamp in IST.
    Dynamically cross-checks active trading holidays from SQLite database.
    """
    dt_ist = get_current_ist(now_dt)
    target_date = dt_ist.date()
    target_time = dt_ist.time()
    is_mcx = is_mcx_symbol(symbol)

    holidays = get_holidays_from_db()
    for h in holidays:
        h_date = h["date"]
        if isinstance(h_date, str):
            try:
                h_date = datetime.date.fromisoformat(h_date)
            except Exception:
                continue

        if h_date == target_date:
            exch = h.get("exchange", "ALL").upper()
            session = h.get("session", "FULL_DAY").upper()
            desc = h.get("description", "Exchange Trading Holiday")

            if is_mcx:
                if exch in ["ALL", "MCX", "MCXFO"]:
                    if session == "MORNING_CLOSED":
                        if target_time < MCX_EVENING_SESSION_OPEN:
                            return True, f"{desc} (MCX Morning Session Closed until 17:00 IST)"
                        return False, None
                    else:  # FULL_DAY
                        return True, desc
            else:  # NSE / BSE / NFO / BSEFO
                if exch in ["ALL", "NSE", "BSE", "NFO", "BSEFO"]:
                    return True, desc

    return False, None


def get_previous_trading_date(symbol: str, from_date: datetime.date) -> datetime.date:
    """Finds the most recent prior date on which the exchange for this symbol had an active trading session."""
    cand = from_date - datetime.timedelta(days=1)
    is_mcx = is_mcx_symbol(symbol)
    holidays = get_holidays_from_db()

    hol_dates = set()
    for h in holidays:
        h_date = h["date"]
        if isinstance(h_date, str):
            try:
                h_date = datetime.date.fromisoformat(h_date)
            except Exception:
                continue
        exch = h.get("exchange", "ALL").upper()
        sess = h.get("session", "FULL_DAY").upper()
        if is_mcx:
            # If MCX had an evening session, trades took place; only full day closure is skipped
            if exch in ["ALL", "MCX", "MCXFO"] and sess == "FULL_DAY":
                hol_dates.add(h_date)
        else:
            if exch in ["ALL", "NSE", "BSE", "NFO", "BSEFO"]:
                hol_dates.add(h_date)

    while True:
        # Skip weekends
        if cand.weekday() >= 5:
            cand -= datetime.timedelta(days=1)
            continue
        # Skip full day holidays
        if cand in hol_dates:
            cand -= datetime.timedelta(days=1)
            continue
        return cand


def is_market_open(symbol: str, now_dt: Optional[datetime.datetime] = None) -> bool:
    """
    Returns True if the exchange for this symbol is currently in an active trading session.
    Returns False on weekends, official exchange holidays, and outside trading hours.
    """
    dt_ist = get_current_ist(now_dt)
    if dt_ist.weekday() >= 5:  # Saturday (5) or Sunday (6)
        return False

    is_hol, _ = is_trading_holiday(symbol, dt_ist)
    if is_hol:
        return False

    open_time, close_time = get_market_hours_for_symbol(symbol)
    curr_time = dt_ist.time()

    # For MCX on morning-only holiday: open_time starts at 17:00 IST
    if is_mcx_symbol(symbol):
        holidays = get_holidays_from_db()
        for h in holidays:
            h_date = h["date"]
            if isinstance(h_date, str):
                try:
                    h_date = datetime.date.fromisoformat(h_date)
                except Exception:
                    continue
            if h_date == dt_ist.date() and h.get("session", "").upper() == "MORNING_CLOSED":
                open_time = MCX_EVENING_SESSION_OPEN
                break

    return open_time <= curr_time <= close_time


def is_any_market_open(now_dt: Optional[datetime.datetime] = None) -> bool:
    """
    Returns True if ANY Indian financial exchange (NSE, BSE, MCX) is currently in an active trading session.
    Returns False when all exchanges are closed (weekends, shared national holidays, overnight hours).
    """
    dt_ist = get_current_ist(now_dt)
    if dt_ist.weekday() >= 5:
        return False
    return is_market_open("NSE:NIFTY", dt_ist) or is_market_open("MCX:CRUDEOIL", dt_ist)


def get_last_completed_trading_session_anchor(
    symbol: str,
    resolution: str = "5m",
    now_dt: Optional[datetime.datetime] = None
) -> datetime.datetime:
    """
    Returns the latest valid market session timestamp in IST.
    During active market hours: returns the current minute rounded down to resolution bucket.
    Outside market hours (evening, early morning, weekend, or holiday): returns the exact close
    timestamp of the most recent completed market trading session.
    """
    dt_ist = get_current_ist(now_dt)
    open_time, close_time = get_market_hours_for_symbol(symbol)
    step_min = parse_resolution_minutes(resolution)

    # If MCX on morning-only holiday, open_time shifts to 17:00
    if is_mcx_symbol(symbol):
        holidays = get_holidays_from_db()
        for h in holidays:
            h_date = h["date"]
            if isinstance(h_date, str):
                try:
                    h_date = datetime.date.fromisoformat(h_date)
                except Exception:
                    continue
            if h_date == dt_ist.date() and h.get("session", "").upper() == "MORNING_CLOSED":
                open_time = MCX_EVENING_SESSION_OPEN
                break

    curr_date = dt_ist.date()
    curr_time = dt_ist.time()

    # 1. Weekend or Holiday closure -> Anchor to the previous trading day's last candle
    is_hol, _ = is_trading_holiday(symbol, dt_ist)
    if dt_ist.weekday() >= 5 or is_hol:
        prev_date = get_previous_trading_date(symbol, curr_date)
        prev_close = datetime.datetime.combine(prev_date, close_time, tzinfo=IST_TZ)
        return prev_close - datetime.timedelta(minutes=step_min)

    # 2. Weekday before market open -> Anchor to previous trading day's last candle
    if curr_time < open_time:
        prev_date = get_previous_trading_date(symbol, curr_date)
        prev_close = datetime.datetime.combine(prev_date, close_time, tzinfo=IST_TZ)
        return prev_close - datetime.timedelta(minutes=step_min)

    # 3. Weekday after market close -> Anchor to today's last candle start
    if curr_time >= close_time:
        today_close = datetime.datetime.combine(curr_date, close_time, tzinfo=IST_TZ)
        return today_close - datetime.timedelta(minutes=step_min)

    # 4. Live market session -> Round down to current bucket aligned to session open
    total_mins_since_open = (dt_ist.hour - open_time.hour) * 60 + (dt_ist.minute - open_time.minute)
    if total_mins_since_open < 0:
        minute_bucket = (dt_ist.minute // step_min) * step_min
        return dt_ist.replace(minute=minute_bucket, second=0, microsecond=0)
    bucket_offset = (total_mins_since_open // step_min) * step_min
    return datetime.datetime.combine(curr_date, open_time, tzinfo=IST_TZ) + datetime.timedelta(minutes=bucket_offset)


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
    hours, weekends, and official exchange holidays. For multi-day lookbacks, timestamps
    cleanly step from session open back to previous trading day's session close.
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

        # Determine effective open time for that particular date
        effective_open = open_time
        if is_mcx_symbol(symbol):
            holidays = get_holidays_from_db()
            for h in holidays:
                h_date = h["date"]
                if isinstance(h_date, str):
                    try:
                        h_date = datetime.date.fromisoformat(h_date)
                    except Exception:
                        continue
                if h_date == curr.date() and h.get("session", "").upper() == "MORNING_CLOSED":
                    effective_open = MCX_EVENING_SESSION_OPEN
                    break

        # Check if stepped before market open of that day
        if prev.time() < effective_open:
            # Jump back to previous valid trading day's last candle start time
            prev_date = get_previous_trading_date(symbol, curr.date())
            prev_close_dt = datetime.datetime.combine(prev_date, close_time, tzinfo=IST_TZ)
            curr = prev_close_dt - datetime.timedelta(minutes=step_min)
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
    Guarantees valid OHLC geometry, consistent holiday/weekend curve persistence,
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
            p_ref = 8033.0
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
