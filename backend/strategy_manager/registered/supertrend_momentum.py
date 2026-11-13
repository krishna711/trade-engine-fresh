"""
Strategy: Supertrend RSI Bollinger Momentum (NIFTY)
Long/short Supertrend, RSI, daily pivot, and Bollinger momentum strategy with 15-minute Opening Range gate.

Combines:
1. Supertrend (10, 3.0): Wilder ATR-based trend direction & trailing stop.
2. RSI (14): Overbought momentum confirmation (> 70 for Long, < 30 for Short).
3. Daily Pivots: Prior-day Floor Pivots (Long requires Close > R1; Short requires Close < S1).
4. Bollinger Bands (20, 2.0 std): Volatility expansion filter (Long requires Close > Upper Band; Short requires Close < Lower Band).
5. 15-minute Opening Range: High/Low breakout confirmation after opening window.
6. Optional ADX (14) trend strength filter (> 25 & rising).
7. Risk-Reward Exit Target (1:2 default R:R) & Supertrend Trailing Stop Loss.
8. Intraday Square-off at 15:10 IST.
"""

from __future__ import annotations

import datetime
import math
import re
from typing import Any, Dict, List, Optional, Tuple, TypedDict

import numpy as np
import pandas as pd

from backend.core.symbol_mapper import symbol_mapper
from backend.strategy_manager.base_strategy import BaseStrategy

IST_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

MONTH_CODES = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}

DEFAULT_LOT_SIZE = 65


class MomentumParamsOverrides(TypedDict, total=False):
    supertrend_period: int
    supertrend_multiplier: float
    rsi_period: int
    rsi_threshold: float
    rsi_short_threshold: float
    adx_period: int
    adx_threshold: float
    adx_slope_bars: int
    opening_range_minutes: int | None
    risk_reward: float
    use_adx_filter: bool
    bb_period: int
    bb_std_dev: float
    square_off_time: int | None
    round_lot_size: int
    max_trades_per_day: int


class MomentumParams(TypedDict):
    supertrend_period: int
    supertrend_multiplier: float
    rsi_period: int
    rsi_threshold: float
    rsi_short_threshold: float
    adx_period: int
    adx_threshold: float
    adx_slope_bars: int
    opening_range_minutes: int | None
    risk_reward: float
    use_adx_filter: bool
    bb_period: int
    bb_std_dev: float
    square_off_time: int | None
    round_lot_size: int
    max_trades_per_day: int


DEFAULT_PARAMS: MomentumParams = {
    "supertrend_period": 10,
    "supertrend_multiplier": 3.0,
    "rsi_period": 14,
    "rsi_threshold": 70.0,
    "rsi_short_threshold": 30.0,
    "adx_period": 14,
    "adx_threshold": 25.0,
    "adx_slope_bars": 1,
    "opening_range_minutes": 15,
    "risk_reward": 2.0,
    "use_adx_filter": False,
    "bb_period": 20,
    "bb_std_dev": 2.0,
    "square_off_time": 151000,
    "round_lot_size": DEFAULT_LOT_SIZE,
    "max_trades_per_day": 0,
}


# ---------------------------------------------------------------------------
# Technical Indicator Calculations
# ---------------------------------------------------------------------------

def _supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
    multiplier: float,
) -> tuple[pd.Series, pd.Series]:
    """Supertrend with Wilder's ATR and trailing band clamping."""
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = true_range.ewm(alpha=1 / max(1, period), adjust=False, min_periods=period).mean()
    midpoint = (high + low) / 2
    upper_basic = midpoint + multiplier * atr
    lower_basic = midpoint - multiplier * atr
    upper = upper_basic.copy()
    lower = lower_basic.copy()
    direction = pd.Series(0, index=close.index, dtype="int64")
    line = pd.Series(np.nan, index=close.index, dtype="float64")
    for i in range(1, len(close)):
        if pd.isna(atr.iloc[i]):
            continue
        upper.iloc[i] = (
            upper_basic.iloc[i]
            if pd.isna(upper.iloc[i - 1])
            or upper_basic.iloc[i] < upper.iloc[i - 1]
            or close.iloc[i - 1] > upper.iloc[i - 1]
            else upper.iloc[i - 1]
        )
        lower.iloc[i] = (
            lower_basic.iloc[i]
            if pd.isna(lower.iloc[i - 1])
            or lower_basic.iloc[i] > lower.iloc[i - 1]
            or close.iloc[i - 1] < lower.iloc[i - 1]
            else lower.iloc[i - 1]
        )
        prior_direction = direction.iloc[i - 1]
        direction.iloc[i] = 1 if close.iloc[i] > upper.iloc[i - 1] else (
            -1 if close.iloc[i] < lower.iloc[i - 1] else (prior_direction or 1)
        )
        line.iloc[i] = lower.iloc[i] if direction.iloc[i] == 1 else upper.iloc[i]
    return line, direction


def _rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder RSI calculation."""
    change = close.diff()
    gain = change.clip(lower=0)
    loss = -change.clip(upper=0)
    p = max(1, period)
    average_gain = gain.ewm(alpha=1 / p, adjust=False, min_periods=p).mean()
    average_loss = loss.ewm(alpha=1 / p, adjust=False, min_periods=p).mean()
    relative_strength = average_gain / average_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + relative_strength))
    return result.where(average_loss != 0, 100.0).where(
        ~(average_gain.eq(0) & average_loss.eq(0)), 50.0
    ).fillna(50.0)


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Wilder ADX calculation."""
    p = max(1, period)
    previous_high = high.shift(1)
    previous_low = low.shift(1)
    previous_close = close.shift(1)
    up_move = high - previous_high
    down_move = previous_low - low
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1)
    smoothed_range = true_range.ewm(alpha=1 / p, adjust=False, min_periods=p).mean().replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1 / p, adjust=False, min_periods=p).mean() / smoothed_range
    minus_di = 100 * minus_dm.ewm(alpha=1 / p, adjust=False, min_periods=p).mean() / smoothed_range
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di)).where(
        (plus_di + minus_di) != 0
    )
    return dx.ewm(alpha=1 / p, adjust=False, min_periods=p).mean().fillna(0.0)


def _parse_time_int(val: Any, fallback: int) -> int:
    if isinstance(val, (int, float)) and math.isfinite(val):
        v = int(val)
        return v * 100 if v < 2400 else v
    s = str(val or "").strip().replace(":", "")
    if len(s) == 4 and s.isdigit():
        return int(s) * 100
    if len(s) == 6 and s.isdigit():
        return int(s)
    return fallback


def _get_last_thursday(year: int, month: int) -> datetime.date:
    next_first = (
        datetime.date(year + 1, 1, 1)
        if month == 12
        else datetime.date(year, month + 1, 1)
    )
    last_day = next_first - datetime.timedelta(days=1)
    offset = (last_day.weekday() - 3) % 7
    return last_day - datetime.timedelta(days=offset)


def resolve_future_contract(
    underlying: str = "NIFTY",
    now_dt: Optional[datetime.datetime] = None,
    roll_last_friday: bool = True
) -> str:
    """Resolves canonical current-month NSE Future contract symbol (e.g. NFO:NIFTY26NOVFUT)."""
    now = now_dt or datetime.datetime.now(IST_TZ)
    root = str(underlying).replace("NSE:", "").replace("NFO:", "").strip().upper()
    if not root:
        root = "NIFTY"
    year, month = now.year, now.month
    last_thu = _get_last_thursday(year, month)
    roll_threshold = (last_thu - datetime.timedelta(days=6)) if roll_last_friday else last_thu
    if now.date() >= roll_threshold:
        month = month + 1 if month < 12 else 1
        year = year if month > 1 else year + 1
    year_str = str(year)[-2:]
    month_code = MONTH_CODES[month]
    return f"NFO:{root}{year_str}{month_code}FUT"


resolve_nifty_future_contract = resolve_future_contract


# ---------------------------------------------------------------------------
# Signal Generator (Pure Pandas / NumPy Engine)
# ---------------------------------------------------------------------------

def generate_signals(
    df: pd.DataFrame,
    params: MomentumParamsOverrides | None = None,
) -> pd.DataFrame:
    """
    Generate completed-candle setups and signals for Supertrend RSI Bollinger Momentum.
    """
    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"Missing required column(s): {sorted(missing)}")

    p: MomentumParams = {**DEFAULT_PARAMS, **(params or {})}
    out = df.copy()
    close = out["Close"]

    line, direction = _supertrend(
        out["High"], out["Low"], close,
        int(p["supertrend_period"]), float(p["supertrend_multiplier"]),
    )
    rsi = _rsi(close, int(p["rsi_period"]))
    adx = _adx(out["High"], out["Low"], close, int(p["adx_period"]))

    bb_p = int(p["bb_period"])
    middle = close.rolling(bb_p, min_periods=bb_p).mean()
    deviation = close.rolling(bb_p, min_periods=bb_p).std(ddof=0)
    upper_band = middle + float(p["bb_std_dev"]) * deviation
    lower_band = middle - float(p["bb_std_dev"]) * deviation

    timestamps = pd.DatetimeIndex(out.index)
    if timestamps.tz is not None:
        timestamps = timestamps.tz_convert(IST_TZ)
    dates = pd.Series(timestamps.date, index=out.index)

    # Daily Floor Pivots: aggregated by session and shifted by 1 day
    daily = out.assign(_date=dates).groupby("_date")[
        ["High", "Low", "Close"]
    ].agg({"High": "max", "Low": "min", "Close": "last"})
    pivot = (daily["High"] + daily["Low"] + daily["Close"]) / 3.0
    daily["R1"] = 2.0 * pivot - daily["Low"]
    daily["S1"] = 2.0 * pivot - daily["High"]
    r1 = dates.map(daily["R1"].shift(1))
    s1 = dates.map(daily["S1"].shift(1))

    minutes = pd.Series(timestamps.hour * 60 + timestamps.minute, index=out.index)
    time_int = pd.Series(timestamps.hour * 10000 + timestamps.minute * 100 + timestamps.second, index=out.index)
    day_start = minutes.groupby(dates).transform("min")

    opening_range_duration = p.get("opening_range_minutes") or 0
    opening_range_enabled = opening_range_duration > 0
    if opening_range_enabled:
        opening_range_end = day_start + opening_range_duration
        opening_range_high = out["High"].where(minutes < opening_range_end).groupby(dates).transform("max")
        opening_range_low = out["Low"].where(minutes < opening_range_end).groupby(dates).transform("min")
        after_opening_range = minutes >= opening_range_end
    else:
        opening_range_high = pd.Series(np.inf, index=out.index)
        opening_range_low = pd.Series(-np.inf, index=out.index)
        after_opening_range = pd.Series(True, index=out.index)

    adx_slope_bars = int(p.get("adx_slope_bars", 1))
    adx_rising = adx.isna() | (adx > adx.shift(adx_slope_bars))
    adx_strong = adx.isna() | (adx > float(p["adx_threshold"]))
    if not p.get("use_adx_filter", False):
        adx_strong = pd.Series(True, index=out.index)
        adx_rising = pd.Series(True, index=out.index)

    # Long Setup
    above_r1 = r1.isna() | (close > r1.to_numpy())
    opening_range_long = (
        close > opening_range_high.to_numpy()
        if opening_range_enabled
        else pd.Series(True, index=out.index)
    )
    long_setup = (direction == 1) & (out["Low"] > line) & (rsi > float(p["rsi_threshold"]))
    long_setup &= adx_strong & adx_rising & after_opening_range
    long_setup &= above_r1
    long_setup &= upper_band.isna() | (close > upper_band)
    long_setup &= opening_range_long

    # Short Setup
    below_s1 = s1.isna() | (close < s1.to_numpy())
    opening_range_short = (
        close < opening_range_low.to_numpy()
        if opening_range_enabled
        else pd.Series(True, index=out.index)
    )
    short_setup = (direction == -1) & (out["High"] < line) & (rsi < float(p["rsi_short_threshold"]))
    short_setup &= adx_strong & adx_rising & after_opening_range
    short_setup &= below_s1
    short_setup &= lower_band.isna() | (close < lower_band)
    short_setup &= opening_range_short

    # Cutoff: no new entries after 15:00 IST
    entry_cutoff = minutes <= 15 * 60
    long_setup &= entry_cutoff
    short_setup &= entry_cutoff

    bullish_reversal = (direction == 1) & (direction.shift(1) == -1)
    bearish_reversal = (direction == -1) & (direction.shift(1) == 1)

    out["Supertrend"] = line
    out["SupertrendDirection"] = direction
    out["RSI"] = rsi
    out["ADX"] = adx
    out["BBUpper"] = upper_band
    out["BBLower"] = lower_band
    out["PivotR1"] = r1.to_numpy()
    out["PivotS1"] = s1.to_numpy()
    out["OpeningRangeHigh"] = opening_range_high.to_numpy()
    out["OpeningRangeLow"] = opening_range_low.to_numpy()
    out["ADXIncreasing"] = adx_rising
    out["LongEntrySetup"] = long_setup
    out["ShortEntrySetup"] = short_setup
    out["EntrySetup"] = long_setup | short_setup
    out["BullishReversal"] = bullish_reversal
    out["BearishReversal"] = bearish_reversal

    n = len(out)
    Buy = np.zeros(n, dtype=bool)
    Sell = np.zeros(n, dtype=bool)
    Short = np.zeros(n, dtype=bool)
    Cover = np.zeros(n, dtype=bool)

    BuyPrice = np.full(n, np.nan)
    SellPrice = np.full(n, np.nan)
    ShortPrice = np.full(n, np.nan)
    CoverPrice = np.full(n, np.nan)

    LongStop = np.full(n, np.nan)
    ShortStop = np.full(n, np.nan)
    LongRiskTarget = np.full(n, np.nan)
    ShortRiskTarget = np.full(n, np.nan)

    sq_time = _parse_time_int(p.get("square_off_time"), 151000)
    rr = float(p.get("risk_reward", 2.0))

    open_arr = out["Open"].to_numpy()
    high_arr = out["High"].to_numpy()
    low_arr = out["Low"].to_numpy()
    close_arr = out["Close"].to_numpy()
    line_arr = line.to_numpy()
    t_int_arr = time_int.to_numpy()
    l_set_arr = long_setup.to_numpy()
    s_set_arr = short_setup.to_numpy()
    b_rev_arr = bullish_reversal.to_numpy()
    s_rev_arr = bearish_reversal.to_numpy()

    position = 0
    entry_price = np.nan
    active_stop = np.nan
    active_target = np.nan

    for i in range(n - 1):
        t_now = t_int_arr[i]
        is_sq = t_now >= sq_time

        long_stop_hit = position == 1 and low_arr[i] <= line_arr[i]
        short_stop_hit = position == -1 and high_arr[i] >= line_arr[i]

        long_target_hit = position == 1 and not np.isnan(active_target) and high_arr[i] >= active_target
        short_target_hit = position == -1 and not np.isnan(active_target) and low_arr[i] <= active_target

        if position == 1 and is_sq:
            Sell[i] = True
            SellPrice[i] = close_arr[i]
            position = 0
            active_stop = np.nan
            active_target = np.nan
        elif position == -1 and is_sq:
            Cover[i] = True
            CoverPrice[i] = close_arr[i]
            position = 0
            active_stop = np.nan
            active_target = np.nan
        elif position == 1 and (s_rev_arr[i] or long_stop_hit or long_target_hit):
            Sell[i + 1] = True
            SellPrice[i + 1] = open_arr[i + 1]
            position = 0
            active_stop = np.nan
            active_target = np.nan
        elif position == -1 and (b_rev_arr[i] or short_stop_hit or short_target_hit):
            Cover[i + 1] = True
            CoverPrice[i + 1] = open_arr[i + 1]
            position = 0
            active_stop = np.nan
            active_target = np.nan
        elif position == 0 and l_set_arr[i] and not is_sq:
            Buy[i + 1] = True
            BuyPrice[i + 1] = open_arr[i + 1]
            entry_price = open_arr[i + 1]
            active_stop = line_arr[i]
            risk = max(1.0, entry_price - active_stop)
            active_target = entry_price + risk * rr
            LongStop[i + 1] = active_stop
            LongRiskTarget[i + 1] = active_target
            position = 1
        elif position == 0 and s_set_arr[i] and not is_sq:
            Short[i + 1] = True
            ShortPrice[i + 1] = open_arr[i + 1]
            entry_price = open_arr[i + 1]
            active_stop = line_arr[i]
            risk = max(1.0, active_stop - entry_price)
            active_target = entry_price - risk * rr
            ShortStop[i + 1] = active_stop
            ShortRiskTarget[i + 1] = active_target
            position = -1

        if position == 1:
            LongStop[i] = line_arr[i]
            LongRiskTarget[i] = active_target
        elif position == -1:
            ShortStop[i] = line_arr[i]
            ShortRiskTarget[i] = active_target

    out["Buy"] = Buy
    out["Sell"] = Sell
    out["Short"] = Short
    out["Cover"] = Cover
    out["BuyPrice"] = BuyPrice
    out["SellPrice"] = SellPrice
    out["ShortPrice"] = ShortPrice
    out["CoverPrice"] = CoverPrice
    out["LongStop"] = LongStop
    out["ShortStop"] = ShortStop
    out["LongRiskTarget"] = LongRiskTarget
    out["ShortRiskTarget"] = ShortRiskTarget

    out.attrs.update(df.attrs)
    return out


# ---------------------------------------------------------------------------
# Chart Payload Generator
# ---------------------------------------------------------------------------

def compute_supertrend_chart_payload(bars: List[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Computes OHLC candles, Supertrend, BB, Pivots, RSI, and Signals for charting."""
    valid_bars = [
        b for b in (bars or [])
        if b.get("open") is not None and b.get("high") is not None and b.get("low") is not None and b.get("close") is not None
    ]
    if not valid_bars:
        return {"candles": [], "overlays": {}, "oscillators": {}, "signals": []}

    rows = []
    idx = []
    candles_out = []
    for b in valid_bars:
        t_val = b.get("time")
        if isinstance(t_val, datetime.datetime):
            dt_ist = t_val if t_val.tzinfo else t_val.replace(tzinfo=datetime.timezone.utc).astimezone(IST_TZ)
        elif isinstance(t_val, (int, float)):
            sec = t_val / 1000.0 if t_val > 1e11 else t_val
            dt_ist = datetime.datetime.fromtimestamp(sec, tz=datetime.timezone.utc).astimezone(IST_TZ)
        else:
            s = str(t_val or "").replace("Z", "+00:00")
            try:
                dt_ist = datetime.datetime.fromisoformat(s).astimezone(IST_TZ)
            except Exception:
                dt_ist = datetime.datetime.now(IST_TZ)

        idx.append(dt_ist)
        o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
        v = float(b.get("volume") or 0.0)
        rows.append({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v})
        candles_out.append({
            "time": dt_ist.isoformat(),
            "label": dt_ist.strftime("%H:%M"),
            "open": round(o, 2),
            "high": round(h, 2),
            "low": round(l, 2),
            "close": round(c, 2),
            "volume": round(v, 2),
        })

    if len(rows) < 2:
        return {"candles": candles_out, "overlays": {}, "oscillators": {}, "signals": []}

    df = pd.DataFrame(rows, index=pd.DatetimeIndex(idx))
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.attrs["symbol"] = cfg.get("symbol", "NSE:NIFTY")

    params: MomentumParamsOverrides = {
        "supertrend_period": int(cfg.get("supertrend_period", 10)),
        "supertrend_multiplier": float(cfg.get("supertrend_multiplier", 3.0)),
        "rsi_period": int(cfg.get("rsi_period", 14)),
        "rsi_threshold": float(cfg.get("rsi_threshold", 70.0)),
        "rsi_short_threshold": float(cfg.get("rsi_short_threshold", 30.0)),
        "adx_period": int(cfg.get("adx_period", 14)),
        "adx_threshold": float(cfg.get("adx_threshold", 25.0)),
        "adx_slope_bars": int(cfg.get("adx_slope_bars", 1)),
        "opening_range_minutes": int(cfg.get("opening_range_minutes", 15)) if cfg.get("opening_range_minutes") is not None else 15,
        "risk_reward": float(cfg.get("risk_reward", 2.0)),
        "use_adx_filter": bool(cfg.get("use_adx_filter", False)),
        "bb_period": int(cfg.get("bb_period", 20)),
        "bb_std_dev": float(cfg.get("bb_std_dev", 2.0)),
        "square_off_time": _parse_time_int(cfg.get("square_off_time"), 151000),
    }

    sig_df = generate_signals(df, params)

    def _clean_series(col: str) -> List[Optional[float]]:
        if col not in sig_df.columns:
            return []
        vals = sig_df[col].to_numpy(dtype=float)
        return [round(float(v), 2) if math.isfinite(v) else None for v in vals]

    signals = []
    for pos_i in range(len(sig_df)):
        row = sig_df.iloc[pos_i]
        ts_dt = sig_df.index[pos_i].to_pydatetime()
        ts_iso = ts_dt.isoformat()
        lbl_t = ts_dt.strftime("%H:%M")
        if bool(row["Buy"]):
            p_val = float(row["BuyPrice"]) if math.isfinite(row["BuyPrice"]) else float(row["Close"])
            signals.append({"index": pos_i, "time": ts_iso, "label_time": lbl_t, "type": "BUY", "price": round(p_val, 2), "label": "BUY (Long)"})
        if bool(row["Sell"]):
            p_val = float(row["SellPrice"]) if math.isfinite(row["SellPrice"]) else float(row["Close"])
            signals.append({"index": pos_i, "time": ts_iso, "label_time": lbl_t, "type": "SELL", "price": round(p_val, 2), "label": "SELL (Exit)"})
        if bool(row["Short"]):
            p_val = float(row["ShortPrice"]) if math.isfinite(row["ShortPrice"]) else float(row["Close"])
            signals.append({"index": pos_i, "time": ts_iso, "label_time": lbl_t, "type": "SHORT", "price": round(p_val, 2), "label": "SHORT"})
        if bool(row["Cover"]):
            p_val = float(row["CoverPrice"]) if math.isfinite(row["CoverPrice"]) else float(row["Close"])
            signals.append({"index": pos_i, "time": ts_iso, "label_time": lbl_t, "type": "COVER", "price": round(p_val, 2), "label": "COVER (Exit)"})

    return {
        "candles": candles_out,
        "overlays": {
            "supertrend": _clean_series("Supertrend"),
            "supertrend_direction": [int(v) for v in sig_df["SupertrendDirection"].to_numpy()],
            "bb_upper": _clean_series("BBUpper"),
            "bb_lower": _clean_series("BBLower"),
            "pivot_r1": _clean_series("PivotR1"),
            "pivot_s1": _clean_series("PivotS1"),
            "opening_range_high": _clean_series("OpeningRangeHigh"),
            "opening_range_low": _clean_series("OpeningRangeLow"),
            "long_stop": _clean_series("LongStop"),
            "short_stop": _clean_series("ShortStop"),
            "long_target": _clean_series("LongRiskTarget"),
            "short_target": _clean_series("ShortRiskTarget"),
        },
        "oscillators": {
            "rsi": _clean_series("RSI"),
            "rsi_overbought": float(params["rsi_threshold"]),
            "rsi_oversold": float(params["rsi_short_threshold"]),
            "adx": _clean_series("ADX"),
            "adx_threshold": float(params["adx_threshold"]),
        },
        "signals": signals,
    }


# ---------------------------------------------------------------------------
# Live Worker Container Strategy
# ---------------------------------------------------------------------------

class SupertrendMomentumStrategy(BaseStrategy):
    """
    Live / Paper Algorithmic Execution Container for Supertrend RSI Bollinger Momentum Strategy on NIFTY.
    """

    def on_init(self):
        raw_sym = str(self.config.get("symbol", "NSE:NIFTY")).strip()
        self.symbol = f"NSE:{raw_sym}" if ":" not in raw_sym else raw_sym
        self.underlying_root = self.symbol.split(":")[-1]

        tf_raw = str(self.config.get("timeframe") or self.config.get("timeframe_min") or "5m").strip().lower()
        if tf_raw.isdigit():
            tf_raw = f"{tf_raw}m"
        self.timeframe = tf_raw

        inst_raw = str(self.config.get("trade_instrument", "FUTURE")).upper()
        if "OPT" in inst_raw:
            self.trade_instrument = "OPTION"
        elif "SPOT" in inst_raw or "EQ" in inst_raw:
            self.trade_instrument = "SPOT"
        else:
            self.trade_instrument = "FUTURE"

        self.product = str(self.config.get("product", "NRML")).upper()
        self.order_type = str(self.config.get("order_type", "LIMIT")).upper()
        self.limit_buffer_pct = float(self.config.get("limit_buffer_pct", 0.015))
        self.auto_chase = bool(self.config.get("auto_chase", True))

        default_lot = symbol_mapper.get_lot_size(self.symbol) or DEFAULT_LOT_SIZE
        self.round_lot_size = int(
            self.config.get("round_lot_size")
            or self.config.get("lot_size")
            or default_lot
        )
        self.quantity_lots = int(self.config.get("quantity_lots", self.config.get("lots", 1)))
        self.max_lots = int(self.config.get("max_lots", 8))

        # Parameters
        self.supertrend_period = int(self.config.get("supertrend_period", 10))
        self.supertrend_multiplier = float(self.config.get("supertrend_multiplier", 3.0))
        self.rsi_period = int(self.config.get("rsi_period", 14))
        self.rsi_threshold = float(self.config.get("rsi_threshold", 70.0))
        self.rsi_short_threshold = float(self.config.get("rsi_short_threshold", 30.0))
        self.adx_period = int(self.config.get("adx_period", 14))
        self.adx_threshold = float(self.config.get("adx_threshold", 25.0))
        self.adx_slope_bars = int(self.config.get("adx_slope_bars", 1))
        self.opening_range_minutes = int(self.config.get("opening_range_minutes", 15)) if self.config.get("opening_range_minutes") is not None else 15
        self.risk_reward = float(self.config.get("risk_reward", 2.0))
        self.use_adx_filter = bool(self.config.get("use_adx_filter", False))
        self.bb_period = int(self.config.get("bb_period", 20))
        self.bb_std_dev = float(self.config.get("bb_std_dev", 2.0))
        self.square_off_time_num = _parse_time_int(self.config.get("square_off_time"), 151000)
        self.fut_roll_last_friday = bool(self.config.get("fut_roll_last_friday", True))

        now_ist = datetime.datetime.now(IST_TZ)
        self.signal_symbol = resolve_future_contract(self.underlying_root, now_ist, self.fut_roll_last_friday)

        # Subscriptions
        self.request_subscribe([
            self.signal_symbol,
            self.symbol,
            f"NFO:{self.underlying_root}",
            self.underlying_root
        ])

        # State tracking
        if not hasattr(self, "last_underlying_ltp"):
            self.last_underlying_ltp = 0.0
        if not hasattr(self, "_session_date"):
            self._session_date: Optional[datetime.date] = None
            self._trades_today = 0
            self._last_bucket_seen: Optional[datetime.datetime] = None
            self._last_entry_bucket: Optional[datetime.datetime] = None

        self.log_decision(
            "INIT",
            f"Supertrend Momentum Strategy Initialized for {self.underlying_root} | "
            f"ST({self.supertrend_period}, {self.supertrend_multiplier}) | RSI({self.rsi_period}) > {self.rsi_threshold}/<{self.rsi_short_threshold} | "
            f"BB({self.bb_period}, {self.bb_std_dev}) | OR: {self.opening_range_minutes}m | R:R {self.risk_reward} | "
            f"SquareOff: {self.square_off_time_num} IST | Signal: {self.signal_symbol}",
        )

    def on_config_updated(self, new_config: Dict[str, Any]):
        super().on_config_updated(new_config)
        self.on_init()

    def rehydrate_state(self, initial_state: Dict[str, Any]):
        """Recovers open position state cleanly on worker restart."""
        legs = initial_state.get("legs_info", [])
        active = [
            dict(l) for l in legs
            if l.get("status") in ["FILLED", "OPEN", "PENDING"] and l.get("exit") is None
        ]
        if initial_state.get("trade_state") == "IN_TRADE" or active:
            self.trade_state = "IN_TRADE"
            leg0 = active[0] if active else {}
            sym = str(leg0.get("symbol") or self.signal_symbol)
            raw_qty = int(leg0.get("qty") or self.round_lot_size)
            side = "BUY" if raw_qty > 0 else "SELL"
            entry_p = float(leg0.get("entry") or 0.0)

            self.current_trade = {
                "symbol": sym,
                "underlying": self.symbol,
                "signal_symbol": self.signal_symbol,
                "trade_instrument": self.trade_instrument,
                "signal_side": side,
                "order_side": side,
                "entry_price": entry_p,
                "entry_spot": entry_p,
                "quantity": abs(raw_qty),
                "stop_price": float(leg0.get("stop_price") or entry_p * 0.99),
                "target_price": float(leg0.get("target_price") or entry_p * 1.02),
                "entry_time": initial_state.get("last_trade_at") or datetime.datetime.utcnow().isoformat(),
                "legs": [dict(l) for l in legs],
                "total_pnl": float(initial_state.get("unrealized_pnl", 0.0) or 0.0),
            }
            if sym and sym != self.symbol:
                self.request_subscribe([sym])

            self.log_decision(
                "STATE REHYDRATED",
                f"Resumed active {side} position on {sym} (Qty: {abs(raw_qty)} @ ₹{entry_p:.2f}). "
                f"Tracking Supertrend Stop & Risk Target ({self.risk_reward} R:R).",
                level="SUCCESS",
            )

    def _resolve_execution_symbol(self, side: str, spot: float, now_dt: datetime.datetime) -> Tuple[str, str, float]:
        """Resolves (contract_symbol, order_side, reference_price) for order placement."""
        if self.trade_instrument == "SPOT":
            return self.symbol, side, spot

        fut_sym = resolve_future_contract(self.underlying_root, now_dt, self.fut_roll_last_friday)
        self.request_subscribe([fut_sym])
        return fut_sym, side, spot

    def on_candle(self, candle: Dict[str, Any], history: List[Dict[str, Any]]):
        """Evaluates completed 5m candle setups and triggers entries / exits."""
        all_bars = (history or []) + ([candle] if candle else [])
        completed = [
            b for b in all_bars
            if b.get("open") is not None
            and b.get("high") is not None
            and b.get("low") is not None
            and b.get("close") is not None
            and math.isfinite(float(b.get("close", 0.0)))
        ]

        min_req = max(self.bb_period, self.supertrend_period, self.rsi_period) + 5
        if len(completed) < min_req:
            self.log_decision(
                "Candle Fetch",
                f"Warming up indicators: {len(completed)}/{min_req} bars collected for {self.symbol}.",
            )
            return

        rows = []
        idx = []
        for b in completed:
            t_val = b.get("time")
            if isinstance(t_val, datetime.datetime):
                dt_ist = t_val if t_val.tzinfo else t_val.replace(tzinfo=datetime.timezone.utc).astimezone(IST_TZ)
            elif isinstance(t_val, (int, float)):
                sec = t_val / 1000.0 if t_val > 1e11 else t_val
                dt_ist = datetime.datetime.fromtimestamp(sec, tz=datetime.timezone.utc).astimezone(IST_TZ)
            else:
                s = str(t_val or "").replace("Z", "+00:00")
                try:
                    dt_ist = datetime.datetime.fromisoformat(s).astimezone(IST_TZ)
                except Exception:
                    dt_ist = datetime.datetime.now(IST_TZ)

            idx.append(dt_ist)
            rows.append({
                "Open": float(b["open"]),
                "High": float(b["high"]),
                "Low": float(b["low"]),
                "Close": float(b["close"]),
                "Volume": float(b.get("volume") or 0.0),
            })

        df = pd.DataFrame(rows, index=pd.DatetimeIndex(idx))
        df = df[~df.index.duplicated(keep="last")].sort_index()

        latest_dt = df.index[-1].to_pydatetime()
        is_new_bar = self._last_bucket_seen is None or latest_dt > self._last_bucket_seen
        if is_new_bar:
            if self._session_date != latest_dt.date():
                self._session_date = latest_dt.date()
                self._trades_today = 0
            self._last_bucket_seen = latest_dt

        params: MomentumParamsOverrides = {
            "supertrend_period": self.supertrend_period,
            "supertrend_multiplier": self.supertrend_multiplier,
            "rsi_period": self.rsi_period,
            "rsi_threshold": self.rsi_threshold,
            "rsi_short_threshold": self.rsi_short_threshold,
            "adx_period": self.adx_period,
            "adx_threshold": self.adx_threshold,
            "adx_slope_bars": self.adx_slope_bars,
            "opening_range_minutes": self.opening_range_minutes,
            "risk_reward": self.risk_reward,
            "use_adx_filter": self.use_adx_filter,
            "bb_period": self.bb_period,
            "bb_std_dev": self.bb_std_dev,
            "square_off_time": self.square_off_time_num,
        }

        sig_df = generate_signals(df, params)
        last = sig_df.iloc[-1]
        self.last_underlying_ltp = float(last["Close"])

        st_val = float(last["Supertrend"]) if math.isfinite(last["Supertrend"]) else 0.0
        st_dir = int(last["SupertrendDirection"])
        rsi_val = float(last["RSI"]) if math.isfinite(last["RSI"]) else 50.0

        if is_new_bar:
            self.log_decision(
                "Candle Fetch Success",
                f"{self.symbol} C={last['Close']:.2f} | Supertrend={st_val:.2f} ({'BULL' if st_dir == 1 else 'BEAR'}) | "
                f"RSI={rsi_val:.1f} | BB=[{last['BBLower']:.1f} - {last['BBUpper']:.1f}] | "
                f"Pivot=[S1:{last['PivotS1']:.1f} R1:{last['PivotR1']:.1f}]",
            )

        tn_now = int(latest_dt.hour * 10000 + latest_dt.minute * 100 + latest_dt.second)
        is_sq_off_now = tn_now >= self.square_off_time_num

        # -------------------------------------------------------------
        # ACTIVE TRADE MANAGEMENT
        # -------------------------------------------------------------
        if self.trade_state == "IN_TRADE" and self.current_trade:
            sig_side = self.current_trade["signal_side"]
            in_long = sig_side == "BUY"
            cur_high = float(last["High"])
            cur_low = float(last["Low"])
            cur_close = float(last["Close"])

            stop_p = float(self.current_trade.get("stop_price") or st_val)
            target_p = float(self.current_trade.get("target_price") or 0.0)

            # 1. Square-off Time (15:10 IST)
            if is_sq_off_now:
                self._execute_exit("TIME_SQUARE_OFF", exit_spot=cur_close)
                return

            # 2. Risk Target Hit
            if in_long and target_p > 0 and cur_high >= target_p:
                self._execute_exit("TARGET_HIT", exit_spot=target_p)
                return
            elif not in_long and target_p > 0 and cur_low <= target_p:
                self._execute_exit("TARGET_HIT", exit_spot=target_p)
                return

            # 3. Supertrend Stop Loss Hit
            if in_long and cur_low <= stop_p:
                self._execute_exit("STOP_LOSS", exit_spot=stop_p)
                return
            elif not in_long and cur_high >= stop_p:
                self._execute_exit("STOP_LOSS", exit_spot=stop_p)
                return

            # 4. Supertrend Direction Reversal
            if in_long and st_dir == -1:
                self._execute_exit("SUPERTREND_REVERSAL", exit_spot=cur_close)
                return
            elif not in_long and st_dir == 1:
                self._execute_exit("SUPERTREND_REVERSAL", exit_spot=cur_close)
                return

            # Update trailing stop along Supertrend line
            if in_long:
                self.current_trade["stop_price"] = max(stop_p, st_val)
            else:
                self.current_trade["stop_price"] = min(stop_p, st_val)
            self.current_trade["sl_price"] = round(self.current_trade["stop_price"], 2)
            return

        # -------------------------------------------------------------
        # IDLE: EVALUATE ENTRY TRIGGER
        # -------------------------------------------------------------
        if self.trade_state != "IDLE" or is_sq_off_now:
            return

        if self._last_entry_bucket == latest_dt:
            return

        max_t = int(self.config.get("max_trades_per_day", 0))
        if max_t > 0 and self._trades_today >= max_t:
            return

        enter_long = bool(last["LongEntrySetup"])
        enter_short = bool(last["ShortEntrySetup"])

        if not enter_long and not enter_short:
            return

        sig_side = "BUY" if enter_long else "SELL"
        signal_spot = float(last["Close"])

        lots = max(1, min(self.max_lots, self.quantity_lots))
        order_qty = lots * self.round_lot_size

        trade_symbol, order_side, ref_price = self._resolve_execution_symbol(sig_side, signal_spot, latest_dt)

        stop_p = st_val
        risk = max(1.0, abs(signal_spot - stop_p))
        target_p = signal_spot + (risk * self.risk_reward) if enter_long else signal_spot - (risk * self.risk_reward)

        self.log_decision(
            "ENTRY CONDITION MATCH",
            f"Supertrend Momentum {sig_side} Signal | Spot={signal_spot:.2f} | "
            f"Supertrend Stop={stop_p:.2f} | Target ({self.risk_reward} R:R)={target_p:.2f} | "
            f"Contract={trade_symbol} ({order_side}) | Qty={order_qty} ({lots}L)",
            level="SUCCESS",
        )

        self.emit_order(
            symbol=trade_symbol,
            side=order_side,
            quantity=order_qty,
            order_type=self.order_type,
            price=ref_price,
            product=self.product,
            leg_id="LEG_PRIMARY",
            buffer_pct=self.limit_buffer_pct,
            auto_chase=self.auto_chase,
        )

        self.log_decision(
            "ORDER SENT TO BROKER",
            f"{order_side} {order_qty} {trade_symbol} across {len(self.client_ids)} account(s).",
        )

        leg_qty = order_qty if order_side == "BUY" else -order_qty
        self.current_trade = {
            "symbol": trade_symbol,
            "underlying": self.symbol,
            "signal_symbol": self.signal_symbol,
            "trade_instrument": self.trade_instrument,
            "signal_side": sig_side,
            "order_side": order_side,
            "entry_price": signal_spot,
            "entry_spot": signal_spot,
            "quantity": order_qty,
            "lots": lots,
            "stop_price": round(stop_p, 2),
            "target_price": round(target_p, 2),
            "pt_spot": round(target_p, 2),
            "sl_price": round(stop_p, 2),
            "entry_time": candle.get("time") or latest_dt.isoformat(),
            "total_pnl": 0.0,
            "legs": [
                {
                    "leg": trade_symbol.split(":")[-1],
                    "symbol": trade_symbol,
                    "entry": signal_spot,
                    "exit": None,
                    "ltp": signal_spot,
                    "qty": leg_qty,
                    "pnl": 0.0,
                    "status": "PENDING",
                }
            ],
        }
        self.trade_state = "IN_TRADE"
        self._last_entry_bucket = latest_dt
        self._trades_today += 1

    def _execute_exit(self, reason: str, exit_spot: Optional[float] = None):
        """Squares off active position cleanly."""
        if not self.current_trade or self.trade_state != "IN_TRADE":
            return

        trade_symbol = self.current_trade["symbol"]
        sig_side = self.current_trade["signal_side"]
        order_side = self.current_trade.get("order_side", sig_side)
        exit_side = "SELL" if order_side == "BUY" else "BUY"
        qty = int(self.current_trade["quantity"])

        leg0 = self.current_trade["legs"][0]
        ep = float(leg0.get("entry") or self.current_trade.get("entry_price") or 0.0)
        cur_ltp = float(leg0.get("ltp") or (exit_spot if exit_spot else ep))

        self.log_decision(
            "EXIT CONDITION MATCH",
            f"Exit Reason={reason} | {exit_side} {qty} {trade_symbol} @ ~{cur_ltp:.2f} (Entry: {ep:.2f})",
            level="WARNING" if "STOP" in reason else "INFO",
        )

        self.emit_order(
            symbol=trade_symbol,
            side=exit_side,
            quantity=qty,
            order_type=self.order_type,
            price=cur_ltp,
            product=self.product,
            leg_id="EXIT_PRIMARY",
            buffer_pct=self.limit_buffer_pct,
            auto_chase=self.auto_chase,
        )

        is_long = order_side == "BUY"
        diff = (cur_ltp - ep) if is_long else (ep - cur_ltp)
        pnl = round(diff * qty, 2) if ep > 0 else 0.0

        leg0["exit"] = cur_ltp
        leg0["ltp"] = cur_ltp
        leg0["pnl"] = pnl
        leg0["status"] = "CLOSED"
        self.current_trade["total_pnl"] = pnl
        self.trade_state = "IDLE"

        self.log_decision(
            "TRADE CLOSED",
            f"Cycle completed ({reason}). Est. PnL: ₹{pnl:+,.2f}",
            level="SUCCESS" if pnl >= 0 else "WARNING",
        )

    def on_tick(self, tick: Dict[str, Any]):
        """Real-time tick tracking for MTM PnL and sub-second Stop Loss & Target execution."""
        sym = tick.get("symbol")
        ltp = tick.get("ltp")
        if not sym or not ltp or float(ltp) <= 0:
            return

        ltp_f = float(ltp)
        is_signal_sym = (
            (sym == self.signal_symbol)
            or (sym == self.symbol)
            or (":" in sym and sym.split(":")[-1] == self.underlying_root)
        )
        if is_signal_sym:
            self.last_underlying_ltp = ltp_f

        if self.trade_state != "IN_TRADE" or not self.current_trade:
            return

        trade_sym = self.current_trade.get("symbol", "")
        is_trade_sym = (sym == trade_sym) or (":" in sym and sym.split(":")[-1] == trade_sym.split(":")[-1])

        if is_trade_sym and self.current_trade.get("legs"):
            leg = self.current_trade["legs"][0]
            leg["ltp"] = ltp_f
            ep = float(leg.get("entry") or 0.0)
            if ep > 0 and leg.get("status") in ["FILLED", "OPEN"]:
                is_long = int(leg.get("qty", 1)) > 0
                diff = (ltp_f - ep) if is_long else (ep - ltp_f)
                pnl = round(diff * abs(int(leg.get("qty", 1))), 2)
                leg["pnl"] = pnl
                self.current_trade["total_pnl"] = pnl

        # Sub-second Stop Loss & Target checks
        check_p = ltp_f if is_signal_sym else self.last_underlying_ltp
        stop_p = float(self.current_trade.get("stop_price") or 0.0)
        target_p = float(self.current_trade.get("target_price") or 0.0)
        sig_side = self.current_trade.get("signal_side")

        if check_p > 0:
            if sig_side == "BUY":
                if target_p > 0 and check_p >= target_p:
                    self._execute_exit("TARGET_HIT", exit_spot=check_p)
                elif stop_p > 0 and check_p <= stop_p:
                    self._execute_exit("STOP_LOSS", exit_spot=check_p)
            elif sig_side == "SELL":
                if target_p > 0 and check_p <= target_p:
                    self._execute_exit("TARGET_HIT", exit_spot=check_p)
                elif stop_p > 0 and check_p >= stop_p:
                    self._execute_exit("STOP_LOSS", exit_spot=check_p)

    def on_timer(self):
        """Second-precision EOD square-off check (15:10:00 IST)."""
        if self.trade_state == "IN_TRADE" and self.current_trade:
            now_ist = datetime.datetime.now(IST_TZ)
            tn = now_ist.hour * 10000 + now_ist.minute * 100 + now_ist.second
            if tn >= self.square_off_time_num:
                self._execute_exit("TIME_SQUARE_OFF", exit_spot=self.last_underlying_ltp or None)

    def on_order_update(self, update_data: Dict[str, Any]):
        """Synchronizes broker execution reports back to running strategy state."""
        ord_req = update_data.get("order_request", {})
        results = update_data.get("results", [])
        if not results:
            return

        filled = [r for r in results if r.get("success") and str(r.get("status") or "").upper() == "FILLED"]
        open_pending = [r for r in results if r.get("success") and str(r.get("status") or "").upper() in ["OPEN", "PENDING", "NEW"]]

        if not self.current_trade or not self.current_trade.get("legs"):
            return

        leg0 = self.current_trade["legs"][0]
        if filled:
            avg_fill = round(sum(float(r.get("price", 0.0)) for r in filled) / len(filled), 2)
            if avg_fill > 0:
                leg0["entry"] = avg_fill
                leg0["ltp"] = avg_fill
                self.current_trade["entry_price"] = avg_fill
                self.current_trade["entry_spot"] = avg_fill
                # Recalculate target with actual fill price
                stop_p = float(self.current_trade.get("stop_price") or avg_fill * 0.99)
                risk = max(1.0, abs(avg_fill - stop_p))
                is_long = self.current_trade.get("signal_side") == "BUY"
                target_p = avg_fill + (risk * self.risk_reward) if is_long else avg_fill - (risk * self.risk_reward)
                self.current_trade["target_price"] = round(target_p, 2)
                self.current_trade["pt_spot"] = round(target_p, 2)
            leg0["status"] = "FILLED"
            self.log_decision(
                "ORDER FILLED",
                f"Broker execution confirmed {self.current_trade['symbol']} @ ₹{avg_fill:.2f}",
                level="SUCCESS",
            )
        elif not open_pending and self.trade_state == "IN_TRADE" and leg0.get("status") in ["PENDING", "SENT"]:
            rej = "; ".join(set(r.get("rejection_reason") for r in results if r.get("rejection_reason"))) or "Order rejected"
            leg0["status"] = "REJECTED"
            self.trade_state = "IDLE"
            self.log_decision("ORDER REJECTED", f"Entry order rejected: {rej}", level="ERROR")
