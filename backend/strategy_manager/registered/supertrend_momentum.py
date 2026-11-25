"""
Strategy: Supertrend RSI Bollinger Momentum (NIFTY) - Updated Variant Engine
Long/short Supertrend, RSI, daily pivot, and Bollinger momentum strategy with
research variants (A, B, C, D, D1, D1R, D1B, D2, D2R, D2B), risk-budget position sizing,
entry ATR bounds, 13:45 entry cutoff, and optional lower-timeframe execution.

Default Variant: D1R (D1 + RSI Momentum + Volume Expansion + Consolidation Breakout)
"""

from __future__ import annotations

import argparse
import datetime
import math
import re
from typing import Any, Dict, List, Optional, Tuple, TypedDict, NotRequired

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
    variant: str
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
    minimum_entry_atr: float
    maximum_entry_atr: float
    risk_budget: float
    lot_size: NotRequired[int]
    round_lot_size: int
    max_trades_per_day: int
    square_off_time: int | None
    entry_cutoff_minute: int | None


class MomentumParams(TypedDict):
    variant: str
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
    minimum_entry_atr: float
    maximum_entry_atr: float
    risk_budget: float
    lot_size: NotRequired[int]
    round_lot_size: int
    max_trades_per_day: int
    square_off_time: int | None
    entry_cutoff_minute: int | None


DEFAULT_PARAMS: MomentumParams = {
    "variant": "D1R",
    "supertrend_period": 10,
    "supertrend_multiplier": 3.0,
    "rsi_period": 14,
    "rsi_threshold": 70.0,
    "rsi_short_threshold": 30.0,
    "adx_period": 14,
    "adx_threshold": 25.0,
    "adx_slope_bars": 1,
    "opening_range_minutes": None,
    "risk_reward": 2.0,
    "use_adx_filter": False,
    "bb_period": 20,
    "bb_std_dev": 2.0,
    "minimum_entry_atr": 15.0,
    "maximum_entry_atr": 35.0,
    "risk_budget": 10_000.0,
    "round_lot_size": DEFAULT_LOT_SIZE,
    "max_trades_per_day": 0,
    "square_off_time": 151000,
    "entry_cutoff_minute": 13 * 60 + 45,
}

ENTRY_CUTOFF_MINUTE = 13 * 60 + 45  # 13:45 IST


def resolve_futures_lot_size(symbol: str, default: Optional[int] = None) -> int:
    """Resolves contract lot size from symbol mapper or fallback."""
    try:
        from backend.core.symbol_mapper import symbol_mapper
        lot = symbol_mapper.get_lot_size(symbol)
        if lot and lot > 0:
            return int(lot)
    except Exception:
        pass
    return default or DEFAULT_LOT_SIZE


# ---------------------------------------------------------------------------
# Technical Indicator Calculations
# ---------------------------------------------------------------------------

def _supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
    multiplier: float,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Supertrend with Wilder's ATR and trailing band clamping in fast NumPy arrays."""
    p = max(1, period)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = true_range.ewm(alpha=1 / p, adjust=False, min_periods=p).mean()
    midpoint = (high + low) / 2
    upper_basic = midpoint + multiplier * atr
    lower_basic = midpoint - multiplier * atr
    # Keep the recursive portion in NumPy arrays; repeated Series.iloc access
    # dominates this loop on long intraday replays.
    upper = upper_basic.to_numpy(copy=True)
    lower = lower_basic.to_numpy(copy=True)
    close_values = close.to_numpy()
    atr_values = atr.to_numpy()
    direction = np.zeros(len(close), dtype=np.int64)
    line = np.full(len(close), np.nan, dtype=np.float64)
    for i in range(1, len(close_values)):
        if np.isnan(atr_values[i]):
            continue
        upper[i] = (
            upper[i]
            if np.isnan(upper[i - 1])
            or upper[i] < upper[i - 1]
            or close_values[i - 1] > upper[i - 1]
            else upper[i - 1]
        )
        lower[i] = (
            lower[i]
            if np.isnan(lower[i - 1])
            or lower[i] > lower[i - 1]
            or close_values[i - 1] < lower[i - 1]
            else lower[i - 1]
        )
        prior_direction = direction[i - 1]
        direction[i] = 1 if close_values[i] > upper[i - 1] else (
            -1 if close_values[i] < lower[i - 1] else (prior_direction or 1)
        )
        line[i] = lower[i] if direction[i] == 1 else upper[i]
    return (
        pd.Series(line, index=close.index),
        pd.Series(direction, index=close.index),
        atr,
    )


def _rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder RSI calculation."""
    p = max(1, period)
    change = close.diff()
    gain = change.clip(lower=0)
    loss = -change.clip(upper=0)
    average_gain = gain.ewm(alpha=1 / p, adjust=False, min_periods=p).mean()
    average_loss = loss.ewm(alpha=1 / p, adjust=False, min_periods=p).mean()
    relative_strength = average_gain / average_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + relative_strength))
    return result.where(average_loss != 0, 100.0).where(
        ~(average_gain.eq(0) & average_loss.eq(0)), 50.0
    ).fillna(50.0)


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Wilder ADX, using the same EWM convention as the RSI above."""
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
    """Resolves canonical active NSE Future contract symbol (e.g. NFO:NIFTY26DECFUT)."""
    root = str(underlying).replace("NSE:", "").replace("NFO:", "").strip().upper()
    if not root:
        root = "NIFTY"
    active_sym = symbol_mapper.get_active_future_contract(root, now_dt, roll_last_friday)
    if active_sym:
        return active_sym

    now = now_dt or datetime.datetime.now(IST_TZ)
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
# Setup Signal Generator (Pure Pandas / NumPy Engine)
# ---------------------------------------------------------------------------

def _generate_setup_signals(
    df: pd.DataFrame,
    params: MomentumParamsOverrides | None = None,
) -> pd.DataFrame:
    """Generate completed-candle signals filled at the following candle open."""
    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"Missing required column(s): {sorted(missing)}")
    p: MomentumParams = {**DEFAULT_PARAMS, **(params or {})}
    allowed_variants = {"A", "B", "C", "D", "D1", "D1R", "D1B", "D2", "D2R", "D2B"}
    if p["variant"] not in allowed_variants:
        raise ValueError("unknown research variant")
    out = df.copy()
    close = out["Close"]
    line, direction, atr = _supertrend(
        out["High"], out["Low"], close,
        int(p["supertrend_period"]), float(p["supertrend_multiplier"]),
    )
    rsi = _rsi(close, int(p["rsi_period"]))
    adx = _adx(out["High"], out["Low"], close, int(p["adx_period"]))
    bb_p = int(p["bb_period"])
    middle = close.rolling(bb_p).mean()
    deviation = close.rolling(bb_p).std(ddof=0)
    upper_band = middle + float(p["bb_std_dev"]) * deviation
    lower_band = middle - float(p["bb_std_dev"]) * deviation
    timestamps = pd.DatetimeIndex(out.index)
    if timestamps.tz is not None:
        timestamps = timestamps.tz_convert(IST_TZ)
    dates = pd.Series(timestamps.date, index=out.index)
    daily = out.assign(_date=dates).groupby("_date")[
        ["High", "Low", "Close"]
    ].agg({"High": "max", "Low": "min", "Close": "last"})
    pivot = (daily["High"] + daily["Low"] + daily["Close"]) / 3
    daily["R1"] = 2 * pivot - daily["Low"]
    daily["S1"] = 2 * pivot - daily["High"]
    r1 = dates.map(daily["R1"].shift(1))
    s1 = dates.map(daily["S1"].shift(1))
    previous_daily_close = dates.map(daily["Close"].shift(1))
    daily_sma20 = dates.map(daily["Close"].rolling(20).mean().shift(1))
    daily_high60 = dates.map(daily["High"].rolling(60).max().shift(1))
    daily_low60 = dates.map(daily["Low"].rolling(60).min().shift(1))
    minutes = pd.Series(timestamps.hour * 60 + timestamps.minute, index=out.index)
    day_start = minutes.groupby(dates).transform("min")
    opening_range_duration = p["opening_range_minutes"] or 0
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
    # Do not suppress the strategy before ADX has enough history to be known.
    adx_rising = adx.isna() | (adx > adx.shift(int(p["adx_slope_bars"])))
    adx_strong = adx.isna() | (adx > float(p["adx_threshold"]))
    if not p["use_adx_filter"]:
        adx_strong = pd.Series(True, index=out.index)
        adx_rising = pd.Series(True, index=out.index)
    above_r1 = close > r1.to_numpy()
    below_s1 = close < s1.to_numpy()
    long_setup = (direction == 1) & (out["Low"] > line) & (rsi > float(p["rsi_threshold"]))
    long_setup &= adx_strong & adx_rising & after_opening_range
    long_setup &= above_r1
    opening_range_long = (
        close > opening_range_high.to_numpy()
        if opening_range_enabled
        else pd.Series(True, index=out.index)
    )
    long_setup &= (close > upper_band) & opening_range_long
    short_setup = (direction == -1) & (out["High"] < line) & (rsi < float(p["rsi_short_threshold"]))
    short_setup &= adx_strong & adx_rising & after_opening_range
    short_setup &= below_s1
    opening_range_short = (
        close < opening_range_low.to_numpy()
        if opening_range_enabled
        else pd.Series(True, index=out.index)
    )
    short_setup &= (close < lower_band) & opening_range_short
    if p["variant"] == "B":
        strong_daily_long = (previous_daily_close > daily_sma20) & (
            previous_daily_close >= 0.9 * daily_high60
        )
        strong_daily_short = (previous_daily_close < daily_sma20) & (
            previous_daily_close <= 1.1 * daily_low60
        )
        long_setup &= strong_daily_long
        short_setup &= strong_daily_short
    if p["variant"] == "D":
        long_setup = (direction == 1) & (out["Low"] > line) & above_r1 & after_opening_range
        short_setup = (direction == -1) & (out["High"] < line) & below_s1 & after_opening_range
    prior_high = out["High"].groupby(dates).transform(
        lambda values: values.shift(1).rolling(6, min_periods=6).max()
    )
    prior_low = out["Low"].groupby(dates).transform(
        lambda values: values.shift(1).rolling(6, min_periods=6).min()
    )
    prior_volume_median = out["Volume"].groupby(dates).transform(
        lambda values: values.shift(1).rolling(6, min_periods=6).median()
    )
    prior_volume_median20 = out["Volume"].groupby(dates).transform(
        lambda values: values.shift(1).rolling(20, min_periods=10).median()
    )
    prior_bar_range_median = (out["High"] - out["Low"]).groupby(dates).transform(
        lambda values: values.shift(1).rolling(6, min_periods=6).median()
    )
    prior_range = prior_high - prior_low
    consolidation = prior_range <= 2.5 * deviation.shift(1)
    volume_expansion = (out["Volume"] > prior_volume_median).where(
        prior_volume_median > 0,
        (out["High"] - out["Low"]) > prior_bar_range_median,
    )
    volume_expansion20 = (out["Volume"] > prior_volume_median20).where(
        prior_volume_median20 > 0,
        (out["High"] - out["Low"]) > (out["High"] - out["Low"]).groupby(dates).transform(
            lambda values: values.shift(1).rolling(20, min_periods=10).median()
        ),
    )
    if p["variant"] == "C":
        long_setup &= consolidation & volume_expansion & (close > prior_high)
        short_setup &= consolidation & volume_expansion & (close < prior_low)
    elif p["variant"] == "D":
        long_setup &= consolidation & volume_expansion & (close > prior_high)
        short_setup &= consolidation & volume_expansion & (close < prior_low)
    elif p["variant"] in {"D1", "D1R", "D1B", "D2", "D2R", "D2B"}:
        long_setup = (direction == 1) & (out["Low"] > line) & above_r1 & after_opening_range
        short_setup = (direction == -1) & (out["High"] < line) & below_s1 & after_opening_range
        if p["variant"] in {"D1R", "D2R"}:
            long_setup &= rsi > float(p["rsi_threshold"])
            short_setup &= rsi < float(p["rsi_short_threshold"])
        if p["variant"] in {"D1B", "D2B"}:
            long_setup &= (close > upper_band) & opening_range_long
            short_setup &= (close < lower_band) & opening_range_short
        long_setup &= consolidation & (close > prior_high)
        short_setup &= consolidation & (close < prior_low)
        if p["variant"] in {"D1", "D1R", "D1B"}:
            long_setup &= volume_expansion20
            short_setup &= volume_expansion20
    # Do not carry late-session setups into the following session.
    cutoff_min = int(p.get("entry_cutoff_minute") or ENTRY_CUTOFF_MINUTE)
    entry_cutoff = minutes <= cutoff_min
    long_setup &= entry_cutoff
    short_setup &= entry_cutoff
    bullish_reversal = (direction == 1) & (direction.shift(1) == -1)
    bearish_reversal = (direction == -1) & (direction.shift(1) == 1)

    out["Supertrend"] = line
    out["SupertrendDirection"] = direction
    out["ATR"] = atr
    out["RSI"] = rsi
    out["ADX"] = adx
    out["BBUpper"] = upper_band
    out["BBLower"] = lower_band
    out["PivotR1"] = r1.to_numpy()
    out["PivotS1"] = s1.to_numpy()
    out["OpeningRangeHigh"] = opening_range_high.to_numpy()
    out["OpeningRangeLow"] = opening_range_low.to_numpy()
    out["ConsolidationHigh"] = prior_high.to_numpy()
    out["ConsolidationLow"] = prior_low.to_numpy()
    out["ConsolidationValid"] = consolidation.to_numpy()
    out["VolumeExpansion"] = volume_expansion.to_numpy()
    out["ADXIncreasing"] = adx_rising
    out["LongEntrySetup"] = long_setup
    out["ShortEntrySetup"] = short_setup
    out["EntrySetup"] = long_setup | short_setup
    out["BullishReversal"] = bullish_reversal
    out["BearishReversal"] = bearish_reversal
    buy_values = np.zeros(len(out), dtype=bool)
    sell_values = np.zeros(len(out), dtype=bool)
    short_values = np.zeros(len(out), dtype=bool)
    cover_values = np.zeros(len(out), dtype=bool)
    buy_prices = np.full(len(out), np.nan)
    sell_prices = np.full(len(out), np.nan)
    short_prices = np.full(len(out), np.nan)
    cover_prices = np.full(len(out), np.nan)
    out["LongStop"] = line
    out["ShortStop"] = line

    opens = out["Open"].to_numpy()
    highs = out["High"].to_numpy()
    lows = out["Low"].to_numpy()
    line_values = line.to_numpy()
    bullish_reversal_values = bullish_reversal.to_numpy()
    bearish_reversal_values = bearish_reversal.to_numpy()
    long_setup_values = long_setup.to_numpy()
    short_setup_values = short_setup.to_numpy()

    position = 0
    for i in range(len(out) - 1):
        long_stop_hit = position == 1 and lows[i] <= line_values[i]
        short_stop_hit = position == -1 and highs[i] >= line_values[i]
        if position == 1 and (bearish_reversal_values[i] or long_stop_hit):
            sell_values[i + 1] = True
            sell_prices[i + 1] = opens[i + 1]
            position = 0
        elif position == -1 and (bullish_reversal_values[i] or short_stop_hit):
            cover_values[i + 1] = True
            cover_prices[i + 1] = opens[i + 1]
            position = 0
        elif position == 0 and long_setup_values[i]:
            buy_values[i + 1] = True
            buy_prices[i + 1] = opens[i + 1]
            position = 1
        elif position == 0 and short_setup_values[i]:
            short_values[i + 1] = True
            short_prices[i + 1] = opens[i + 1]
            position = -1
    out["Buy"] = buy_values
    out["Sell"] = sell_values
    out["Short"] = short_values
    out["Cover"] = cover_values
    out["BuyPrice"] = buy_prices
    out["SellPrice"] = sell_prices
    out["ShortPrice"] = short_prices
    out["CoverPrice"] = cover_prices
    out.attrs.update(df.attrs)
    return out


def _execute_on_lower_timeframe(
    setup: pd.DataFrame,
    execution: pd.DataFrame,
    params: MomentumParamsOverrides | None = None,
) -> pd.DataFrame:
    """Execute setup-candle levels using risk-budget position sizing."""
    p: MomentumParams = {**DEFAULT_PARAMS, **(params or {})}
    if p["risk_budget"] <= 0:
        raise ValueError("risk_budget must be positive")
    lot_size = p.get("lot_size") or p.get("round_lot_size") or DEFAULT_LOT_SIZE
    if lot_size <= 0:
        raise ValueError("lot_size must be positive")
    if execution.empty:
        return execution.copy()
    setup_diffs = pd.DatetimeIndex(setup.index).to_series().diff().dropna()
    setup_delta = (
        pd.Timedelta(setup_diffs.median())
        if not setup_diffs.empty
        else pd.Timedelta(minutes=5)
    )
    effective = setup.copy()
    effective.index = effective.index + setup_delta
    active = effective.reindex(execution.index, method="ffill")
    out = execution.copy()
    for column in (
        "Supertrend", "SupertrendDirection", "RSI", "ADX", "BBUpper", "BBLower",
        "ATR",
        "PivotR1", "PivotS1", "OpeningRangeHigh", "OpeningRangeLow", "ADXIncreasing",
        "LongEntrySetup", "ShortEntrySetup", "EntrySetup", "BullishReversal",
        "BearishReversal",
        "ConsolidationHigh", "ConsolidationLow", "ConsolidationValid",
        "VolumeExpansion",
    ):
        if column in active.columns:
            out[column] = active[column].to_numpy()
    for column in ("Buy", "Sell", "Short", "Cover"):
        out[column] = False
    for column in ("BuyPrice", "SellPrice", "ShortPrice", "CoverPrice"):
        out[column] = np.nan
    out["LongStop"] = active["Supertrend"].to_numpy()
    out["ShortStop"] = active["Supertrend"].to_numpy()
    execution_rsi = _rsi(out["Close"], int(p["rsi_period"]))
    opening_range_enabled = bool(p["opening_range_minutes"])

    position = 0
    timestamps = pd.DatetimeIndex(out.index)
    if timestamps.tz is not None:
        timestamps = timestamps.tz_convert(IST_TZ)
    opens = out["Open"].to_numpy()
    highs = out["High"].to_numpy()
    lows = out["Low"].to_numpy()
    closes = out["Close"].to_numpy()
    rsi_values = execution_rsi.to_numpy()
    supertrend_values = active["Supertrend"].to_numpy()
    atr_values = active["ATR"].to_numpy()
    direction_values = active["SupertrendDirection"].to_numpy()
    pivot_r1_values = active["PivotR1"].to_numpy()
    pivot_s1_values = active["PivotS1"].to_numpy()
    bb_upper_values = active["BBUpper"].to_numpy()
    bb_lower_values = active["BBLower"].to_numpy()
    opening_high_values = active["OpeningRangeHigh"].to_numpy()
    opening_low_values = active["OpeningRangeLow"].to_numpy()
    long_entry_values = active["LongEntrySetup"].to_numpy().copy()
    short_entry_values = active["ShortEntrySetup"].to_numpy().copy()
    entry_values = active["EntrySetup"].to_numpy().copy()
    bullish_reversal_values = active["BullishReversal"].to_numpy()
    bearish_reversal_values = active["BearishReversal"].to_numpy()
    buy_values = np.zeros(len(out), dtype=bool)
    sell_values = np.zeros(len(out), dtype=bool)
    short_values = np.zeros(len(out), dtype=bool)
    cover_values = np.zeros(len(out), dtype=bool)
    buy_prices = np.full(len(out), np.nan)
    sell_prices = np.full(len(out), np.nan)
    short_prices = np.full(len(out), np.nan)
    cover_prices = np.full(len(out), np.nan)
    buy_qty = np.zeros(len(out))
    short_qty = np.zeros(len(out))
    buy_lots = np.zeros(len(out))
    short_lots = np.zeros(len(out))
    cutoff_min = int(p.get("entry_cutoff_minute") or ENTRY_CUTOFF_MINUTE)

    for row_number in range(len(out)):
        timestamp = timestamps[row_number]
        if pd.isna(supertrend_values[row_number]):
            continue
        stop = float(supertrend_values[row_number])
        if any(pd.isna(values[row_number]) for values in (
            pivot_r1_values, pivot_s1_values, bb_upper_values, bb_lower_values,
        )):
            continue
        long_setup = (
            bool(long_entry_values[row_number])
            and
            int(direction_values[row_number]) == 1
            and lows[row_number] > stop
            and rsi_values[row_number] > float(p["rsi_threshold"])
            and closes[row_number] > float(pivot_r1_values[row_number])
            and closes[row_number] > float(bb_upper_values[row_number])
            and float(p["minimum_entry_atr"]) <= atr_values[row_number] <= float(p["maximum_entry_atr"])
            and (
                not opening_range_enabled
                or closes[row_number] > float(opening_high_values[row_number])
            )
            and timestamp.hour * 60 + timestamp.minute <= cutoff_min
        )
        short_setup = (
            bool(short_entry_values[row_number])
            and
            int(direction_values[row_number]) == -1
            and highs[row_number] < stop
            and rsi_values[row_number] < float(p["rsi_short_threshold"])
            and closes[row_number] < float(pivot_s1_values[row_number])
            and closes[row_number] < float(bb_lower_values[row_number])
            and float(p["minimum_entry_atr"]) <= atr_values[row_number] <= float(p["maximum_entry_atr"])
            and (
                not opening_range_enabled
                or closes[row_number] < float(opening_low_values[row_number])
            )
            and timestamp.hour * 60 + timestamp.minute <= cutoff_min
        )
        long_entry_values[row_number] = long_setup
        short_entry_values[row_number] = short_setup
        entry_values[row_number] = long_setup or short_setup
        if position == 1 and (lows[row_number] <= stop or bool(bearish_reversal_values[row_number])):
            price = min(opens[row_number], stop) if lows[row_number] <= stop else opens[row_number]
            sell_values[row_number] = True
            sell_prices[row_number] = price
            position = 0
        elif position == -1 and (highs[row_number] >= stop or bool(bullish_reversal_values[row_number])):
            price = max(opens[row_number], stop) if highs[row_number] >= stop else opens[row_number]
            cover_values[row_number] = True
            cover_prices[row_number] = price
            position = 0
        elif position == 0:
            long_entry_level = max(float(pivot_r1_values[row_number]), float(bb_upper_values[row_number]))
            short_entry_level = min(float(pivot_s1_values[row_number]), float(bb_lower_values[row_number]))
            if long_setup and highs[row_number] >= long_entry_level:
                entry_price = max(opens[row_number], long_entry_level)
                risk_points = abs(entry_price - stop)
                buy_values[row_number] = True
                buy_prices[row_number] = entry_price
                buy_lots[row_number] = (
                    p["risk_budget"] / (risk_points * lot_size)
                    if risk_points > 0
                    else 0.0
                )
                buy_qty[row_number] = buy_lots[row_number] * lot_size
                position = 1
            elif short_setup and lows[row_number] <= short_entry_level:
                entry_price = min(opens[row_number], short_entry_level)
                risk_points = abs(stop - entry_price)
                short_values[row_number] = True
                short_prices[row_number] = entry_price
                short_lots[row_number] = (
                    p["risk_budget"] / (risk_points * lot_size)
                    if risk_points > 0
                    else 0.0
                )
                short_qty[row_number] = short_lots[row_number] * lot_size
                position = -1
    out["LongEntrySetup"] = long_entry_values
    out["ShortEntrySetup"] = short_entry_values
    out["EntrySetup"] = entry_values
    out["Buy"] = buy_values
    out["Sell"] = sell_values
    out["Short"] = short_values
    out["Cover"] = cover_values
    out["BuyPrice"] = buy_prices
    out["SellPrice"] = sell_prices
    out["ShortPrice"] = short_prices
    out["CoverPrice"] = cover_prices
    out["BuyQty"] = buy_qty
    out["ShortQty"] = short_qty
    out["BuyLots"] = buy_lots
    out["ShortLots"] = short_lots
    out.attrs.update(setup.attrs)
    out.attrs["lot_size"] = lot_size
    out.attrs["risk_budget"] = p["risk_budget"]
    return out


def generate_signals(
    df: pd.DataFrame,
    params: MomentumParamsOverrides | None = None,
    execution_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Generate 5-minute setups, optionally executed on lower-timeframe bars."""
    setup = _generate_setup_signals(df, params)
    return (
        _execute_on_lower_timeframe(setup, execution_df, params)
        if execution_df is not None
        else setup
    )


def _is_missing(value: object) -> bool:
    return value is None or value is pd.NA or (
        isinstance(value, (float, np.floating)) and bool(np.isnan(value))
    )


def _format_money(value: object) -> str:
    return "-" if _is_missing(value) else f"{float(str(value)):,.2f}"


def _extract_trades(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate risk-sized P&L from generated entry and exit prices."""
    columns = [
        "side", "entry_time", "exit_time", "entry_price", "exit_price",
        "initial_sl_bps", "entry_atr", "lots", "qty", "gross_pnl", "cost", "pnl",
    ]
    rows: list[dict[str, object]] = []
    position = 0
    entry_time: pd.Timestamp | None = None
    entry_price = 0.0
    initial_sl_bps = np.nan
    entry_atr = np.nan
    entry_lots = 0.0
    entry_qty = 0.0
    timestamps = pd.DatetimeIndex(df.index)
    buy_values = df["Buy"].to_numpy()
    short_values = df["Short"].to_numpy()
    sell_values = df["Sell"].to_numpy()
    cover_values = df["Cover"].to_numpy()
    buy_prices = df["BuyPrice"].to_numpy()
    short_prices = df["ShortPrice"].to_numpy()
    sell_prices = df["SellPrice"].to_numpy()
    cover_prices = df["CoverPrice"].to_numpy()
    buy_lots = df.get("BuyLots", pd.Series(1.0, index=df.index)).to_numpy()
    short_lots = df.get("ShortLots", pd.Series(1.0, index=df.index)).to_numpy()
    buy_qty = df.get("BuyQty", pd.Series(DEFAULT_LOT_SIZE, index=df.index)).to_numpy()
    short_qty = df.get("ShortQty", pd.Series(DEFAULT_LOT_SIZE, index=df.index)).to_numpy()
    closes = df["Close"].to_numpy()
    for row_number, bar_time in enumerate(timestamps):
        if position == 0 and bool(buy_values[row_number]):
            position, entry_time, entry_price = 1, bar_time, float(buy_prices[row_number])
            sl_val = float(df["LongStop"].iloc[row_number]) if "LongStop" in df.columns else entry_price * 0.99
            initial_sl_bps = (
                abs(entry_price - sl_val)
                / entry_price
                * 10_000
            ) if entry_price > 0 else np.nan
            entry_atr = float(df["ATR"].iloc[row_number]) if "ATR" in df.columns else np.nan
            entry_lots, entry_qty = float(buy_lots[row_number]), float(buy_qty[row_number])
        elif position == 0 and bool(short_values[row_number]):
            position, entry_time, entry_price = -1, bar_time, float(short_prices[row_number])
            sl_val = float(df["ShortStop"].iloc[row_number]) if "ShortStop" in df.columns else entry_price * 1.01
            initial_sl_bps = (
                abs(entry_price - sl_val)
                / entry_price
                * 10_000
            ) if entry_price > 0 else np.nan
            entry_atr = float(df["ATR"].iloc[row_number]) if "ATR" in df.columns else np.nan
            entry_lots, entry_qty = float(short_lots[row_number]), float(short_qty[row_number])
        elif position == 1 and bool(sell_values[row_number]):
            exit_price = float(sell_prices[row_number])
            qty_mult = entry_qty if entry_qty > 0 else DEFAULT_LOT_SIZE
            pnl = (exit_price - entry_price) * qty_mult
            rows.append({"side": "L", "entry_time": entry_time, "exit_time": bar_time,
                         "entry_price": entry_price, "exit_price": exit_price,
                         "initial_sl_bps": initial_sl_bps,
                         "entry_atr": entry_atr,
                         "lots": entry_lots, "qty": entry_qty, "gross_pnl": pnl,
                         "cost": 0.0, "pnl": pnl})
            position = 0
        elif position == -1 and bool(cover_values[row_number]):
            exit_price = float(cover_prices[row_number])
            qty_mult = entry_qty if entry_qty > 0 else DEFAULT_LOT_SIZE
            pnl = (entry_price - exit_price) * qty_mult
            rows.append({"side": "S", "entry_time": entry_time, "exit_time": bar_time,
                         "entry_price": entry_price, "exit_price": exit_price,
                         "initial_sl_bps": initial_sl_bps,
                         "entry_atr": entry_atr,
                         "lots": entry_lots, "qty": entry_qty, "gross_pnl": pnl,
                         "cost": 0.0, "pnl": pnl})
            position = 0
    if position and entry_time is not None and len(df):
        exit_price = float(closes[-1])
        qty_mult = entry_qty if entry_qty > 0 else DEFAULT_LOT_SIZE
        pnl = ((exit_price - entry_price) if position == 1 else (entry_price - exit_price)) * qty_mult
        rows.append({"side": "L" if position == 1 else "S", "entry_time": entry_time,
                     "exit_time": timestamps[-1], "entry_price": entry_price,
                     "exit_price": exit_price, "lots": entry_lots, "qty": entry_qty,
                     "initial_sl_bps": initial_sl_bps,
                     "entry_atr": entry_atr,
                     "gross_pnl": pnl,
                     "cost": 0.0, "pnl": pnl})
    return pd.DataFrame(rows, columns=columns)


# ---------------------------------------------------------------------------
# Chart Payload Generator
# ---------------------------------------------------------------------------

def compute_supertrend_chart_payload(bars: List[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Computes OHLC candles, Supertrend, BB, Pivots, RSI, ATR, and Signals for charting."""
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
            dt_ist = t_val if t_val.tzinfo else t_val.replace(tzinfo=IST_TZ)
        elif isinstance(t_val, (int, float)):
            sec = t_val / 1000.0 if t_val > 1e11 else t_val
            dt_ist = datetime.datetime.fromtimestamp(sec, tz=datetime.timezone.utc).astimezone(IST_TZ)
        else:
            s = str(t_val or "").replace("Z", "+00:00")
            try:
                dt_raw = datetime.datetime.fromisoformat(s)
                dt_ist = dt_raw if dt_raw.tzinfo else dt_raw.replace(tzinfo=IST_TZ)
            except Exception:
                dt_ist = datetime.datetime.now(IST_TZ)

        o = float(b["open"])
        h = float(b["high"])
        l = float(b["low"])
        c = float(b["close"])
        v = float(b.get("volume") or 0.0)

        idx.append(dt_ist)
        rows.append({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v})

    if len(rows) < 2:
        return {"candles": [], "overlays": {}, "oscillators": {}, "signals": []}

    df = pd.DataFrame(rows, index=pd.DatetimeIndex(idx))
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.attrs["symbol"] = cfg.get("symbol", "NSE:NIFTY")

    candles_out = []
    for dt_ts, row in df.iterrows():
        dt_ist = dt_ts.to_pydatetime()
        candles_out.append({
            "time": dt_ist.isoformat(),
            "label": dt_ist.strftime("%H:%M"),
            "open": round(float(row["Open"]), 2),
            "high": round(float(row["High"]), 2),
            "low": round(float(row["Low"]), 2),
            "close": round(float(row["Close"]), 2),
            "volume": round(float(row["Volume"]), 2),
        })

    params: MomentumParamsOverrides = {
        "variant": str(cfg.get("variant", "D1R")),
        "supertrend_period": int(cfg.get("supertrend_period", 10)),
        "supertrend_multiplier": float(cfg.get("supertrend_multiplier", 3.0)),
        "rsi_period": int(cfg.get("rsi_period", 14)),
        "rsi_threshold": float(cfg.get("rsi_threshold", 70.0)),
        "rsi_short_threshold": float(cfg.get("rsi_short_threshold", 30.0)),
        "adx_period": int(cfg.get("adx_period", 14)),
        "adx_threshold": float(cfg.get("adx_threshold", 25.0)),
        "adx_slope_bars": int(cfg.get("adx_slope_bars", 1)),
        "opening_range_minutes": int(cfg.get("opening_range_minutes")) if cfg.get("opening_range_minutes") is not None else None,
        "risk_reward": float(cfg.get("risk_reward", 2.0)),
        "use_adx_filter": bool(cfg.get("use_adx_filter", False)),
        "bb_period": int(cfg.get("bb_period", 20)),
        "bb_std_dev": float(cfg.get("bb_std_dev", 2.0)),
        "minimum_entry_atr": float(cfg.get("minimum_entry_atr", 15.0)),
        "maximum_entry_atr": float(cfg.get("maximum_entry_atr", 35.0)),
        "risk_budget": float(cfg.get("risk_budget", 10000.0)),
        "square_off_time": _parse_time_int(cfg.get("square_off_time"), 151000),
        "entry_cutoff_minute": int(cfg.get("entry_cutoff_minute") or ENTRY_CUTOFF_MINUTE),
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
        lbl_t = ts_dt.strftime("%d %b %Y %H:%M")
        if bool(row.get("Buy", False)):
            p_val = float(row["BuyPrice"]) if math.isfinite(row.get("BuyPrice", np.nan)) else float(row["Close"])
            signals.append({"index": pos_i, "time": ts_iso, "label_time": lbl_t, "type": "BUY", "price": round(p_val, 2), "label": f"BUY ({params['variant']})"})
        if bool(row.get("Sell", False)):
            p_val = float(row["SellPrice"]) if math.isfinite(row.get("SellPrice", np.nan)) else float(row["Close"])
            signals.append({"index": pos_i, "time": ts_iso, "label_time": lbl_t, "type": "SELL", "price": round(p_val, 2), "label": "SELL (Exit)"})
        if bool(row.get("Short", False)):
            p_val = float(row["ShortPrice"]) if math.isfinite(row.get("ShortPrice", np.nan)) else float(row["Close"])
            signals.append({"index": pos_i, "time": ts_iso, "label_time": lbl_t, "type": "SHORT", "price": round(p_val, 2), "label": f"SHORT ({params['variant']})"})
        if bool(row.get("Cover", False)):
            p_val = float(row["CoverPrice"]) if math.isfinite(row.get("CoverPrice", np.nan)) else float(row["Close"])
            signals.append({"index": pos_i, "time": ts_iso, "label_time": lbl_t, "type": "COVER", "price": round(p_val, 2), "label": "COVER (Exit)"})

    return {
        "candles": candles_out,
        "overlays": {
            "supertrend": _clean_series("Supertrend"),
            "supertrend_direction": [int(v) for v in sig_df["SupertrendDirection"].to_numpy()] if "SupertrendDirection" in sig_df.columns else [],
            "bb_upper": _clean_series("BBUpper"),
            "bb_lower": _clean_series("BBLower"),
            "pivot_r1": _clean_series("PivotR1"),
            "pivot_s1": _clean_series("PivotS1"),
            "opening_range_high": _clean_series("OpeningRangeHigh"),
            "opening_range_low": _clean_series("OpeningRangeLow"),
            "consolidation_high": _clean_series("ConsolidationHigh"),
            "consolidation_low": _clean_series("ConsolidationLow"),
            "long_stop": _clean_series("LongStop"),
            "short_stop": _clean_series("ShortStop"),
            "atr": _clean_series("ATR"),
        },
        "oscillators": {
            "rsi": _clean_series("RSI"),
            "rsi_overbought": float(params["rsi_threshold"]),
            "rsi_oversold": float(params["rsi_short_threshold"]),
            "adx": _clean_series("ADX"),
            "adx_threshold": float(params["adx_threshold"]),
            "atr": _clean_series("ATR"),
            "minimum_entry_atr": float(params["minimum_entry_atr"]),
            "maximum_entry_atr": float(params["maximum_entry_atr"]),
        },
        "signals": signals,
    }


# ---------------------------------------------------------------------------
# Live Worker Container Strategy
# ---------------------------------------------------------------------------

class SupertrendMomentumStrategy(BaseStrategy):
    """
    Live / Paper Algorithmic Execution Container for Supertrend RSI Bollinger Momentum Strategy on NIFTY.
    Supports variant D1R with risk-budget lot sizing, entry ATR bounds, and Supertrend trailing stop.
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
        self.variant = str(self.config.get("variant", "D1R")).strip().upper()
        self.supertrend_period = int(self.config.get("supertrend_period", 10))
        self.supertrend_multiplier = float(self.config.get("supertrend_multiplier", 3.0))
        self.rsi_period = int(self.config.get("rsi_period", 14))
        self.rsi_threshold = float(self.config.get("rsi_threshold", 70.0))
        self.rsi_short_threshold = float(self.config.get("rsi_short_threshold", 30.0))
        self.adx_period = int(self.config.get("adx_period", 14))
        self.adx_threshold = float(self.config.get("adx_threshold", 25.0))
        self.adx_slope_bars = int(self.config.get("adx_slope_bars", 1))
        self.opening_range_minutes = int(self.config.get("opening_range_minutes")) if self.config.get("opening_range_minutes") is not None else None
        self.risk_reward = float(self.config.get("risk_reward", 2.0))
        self.use_adx_filter = bool(self.config.get("use_adx_filter", False))
        self.bb_period = int(self.config.get("bb_period", 20))
        self.bb_std_dev = float(self.config.get("bb_std_dev", 2.0))
        self.minimum_entry_atr = float(self.config.get("minimum_entry_atr", 15.0))
        self.maximum_entry_atr = float(self.config.get("maximum_entry_atr", 35.0))
        self.risk_budget = float(self.config.get("risk_budget", 10000.0))
        self.entry_cutoff_minute = int(self.config.get("entry_cutoff_minute") or ENTRY_CUTOFF_MINUTE)
        self.square_off_time_num = _parse_time_int(self.config.get("square_off_time"), 151000)
        self.fut_roll_last_friday = bool(self.config.get("fut_roll_last_friday", False))

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
        if not hasattr(self, "last_trade_ltp"):
            self.last_trade_ltp = 0.0
        if not hasattr(self, "_session_date"):
            self._session_date: Optional[datetime.date] = None
            self._trades_today = 0
            self._last_bucket_seen: Optional[datetime.datetime] = None
            self._last_entry_bucket: Optional[datetime.datetime] = None

        self.log_decision(
            "INIT",
            f"Supertrend Momentum Strategy Initialized for {self.underlying_root} | "
            f"Variant: {self.variant} | ST({self.supertrend_period}, {self.supertrend_multiplier}) | "
            f"RSI({self.rsi_period}) > {self.rsi_threshold}/<{self.rsi_short_threshold} | "
            f"BB({self.bb_period}, {self.bb_std_dev}) | ATR Bounds: [{self.minimum_entry_atr}, {self.maximum_entry_atr}] | "
            f"Risk Budget: ₹{self.risk_budget:,.2f} | Cutoff: {self.entry_cutoff_minute // 60:02d}:{self.entry_cutoff_minute % 60:02d} IST | "
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
        exec_price = float(getattr(self, "last_trade_ltp", 0.0) or 0.0)
        if exec_price <= 0.0:
            exec_price = spot
        return fut_sym, side, exec_price

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
                dt_ist = t_val if t_val.tzinfo else t_val.replace(tzinfo=IST_TZ)
            elif isinstance(t_val, (int, float)):
                sec = t_val / 1000.0 if t_val > 1e11 else t_val
                dt_ist = datetime.datetime.fromtimestamp(sec, tz=datetime.timezone.utc).astimezone(IST_TZ)
            else:
                s = str(t_val or "").replace("Z", "+00:00")
                try:
                    dt_raw = datetime.datetime.fromisoformat(s)
                    dt_ist = dt_raw if dt_raw.tzinfo else dt_raw.replace(tzinfo=IST_TZ)
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

        # Evaluate setups strictly on COMPLETED bars (df.iloc[:-1] when a new bar bucket opens).
        # The last bar in df (df.index[-1]) is the currently-forming incomplete candle.
        latest_bucket_dt = df.index[-1].to_pydatetime()
        was_uninitialized = self._last_bucket_seen is None
        is_new_bar = was_uninitialized or latest_bucket_dt > self._last_bucket_seen
        if not is_new_bar:
            return

        if self._session_date != latest_bucket_dt.date():
            self._session_date = latest_bucket_dt.date()
            self._trades_today = 0
        self._last_bucket_seen = latest_bucket_dt

        completed_df = df.iloc[:-1] if len(df) > min_req else df
        latest_dt = completed_df.index[-1].to_pydatetime()

        params: MomentumParamsOverrides = {
            "variant": self.variant,
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
            "minimum_entry_atr": self.minimum_entry_atr,
            "maximum_entry_atr": self.maximum_entry_atr,
            "risk_budget": self.risk_budget,
            "entry_cutoff_minute": self.entry_cutoff_minute,
            "square_off_time": self.square_off_time_num,
        }

        sig_df = generate_signals(completed_df, params)
        last = sig_df.iloc[-1]
        self.last_underlying_ltp = float(last["Close"])

        st_val = float(last["Supertrend"]) if math.isfinite(last["Supertrend"]) else 0.0
        st_dir = int(last["SupertrendDirection"])
        rsi_val = float(last["RSI"]) if math.isfinite(last["RSI"]) else 50.0
        atr_val = float(last["ATR"]) if math.isfinite(last.get("ATR", np.nan)) else 0.0

        self.log_decision(
            "Candle Fetch Success",
            f"{self.symbol} ({latest_dt.strftime('%H:%M')}) C={last['Close']:.2f} | ST={st_val:.2f} ({'BULL' if st_dir == 1 else 'BEAR'}) | "
            f"ATR={atr_val:.1f} (Bounds: [{self.minimum_entry_atr:.1f}, {self.maximum_entry_atr:.1f}]) | "
            f"RSI={rsi_val:.1f} | BB=[{last['BBLower']:.1f} - {last['BBUpper']:.1f}] | "
            f"Pivot=[S1:{last['PivotS1']:.1f} R1:{last['PivotR1']:.1f}] | Variant={self.variant}",
        )

        # On first startup poll, sync indicators & trailing stop without firing a retroactive entry on an old bar
        if was_uninitialized:
            return

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

            # Prevent entry candle's prior extremum from triggering immediate exit.
            # Only evaluate bar-level targets/stops/reversals on subsequent candles.
            is_subsequent_bar = self._last_entry_bucket is None or latest_dt > self._last_entry_bucket

            # 2. Risk Target Hit (if risk_reward configured)
            if is_subsequent_bar and self.risk_reward > 0 and target_p > 0:
                if in_long and cur_high >= target_p:
                    self._execute_exit("TARGET_HIT", exit_spot=target_p)
                    return
                elif not in_long and cur_low <= target_p:
                    self._execute_exit("TARGET_HIT", exit_spot=target_p)
                    return

            # 3. Supertrend Stop Loss Hit
            if is_subsequent_bar:
                if in_long and cur_low <= stop_p:
                    self._execute_exit("STOP_LOSS", exit_spot=stop_p)
                    return
                elif not in_long and cur_high >= stop_p:
                    self._execute_exit("STOP_LOSS", exit_spot=stop_p)
                    return

            # 4. Supertrend Direction Reversal
            if is_subsequent_bar:
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

        # Cutoff check: no new entries after cutoff minute (default 13:45 IST)
        minute_now = latest_dt.hour * 60 + latest_dt.minute
        if minute_now > self.entry_cutoff_minute:
            return

        enter_long = bool(last.get("LongEntrySetup", False))
        enter_short = bool(last.get("ShortEntrySetup", False))

        if not enter_long and not enter_short:
            return

        # Check ATR bounds (minimum_entry_atr <= atr <= maximum_entry_atr)
        if atr_val > 0 and (atr_val < self.minimum_entry_atr or atr_val > self.maximum_entry_atr):
            self.log_decision(
                "ENTRY FILTER REJECTED",
                f"ATR {atr_val:.2f} is outside entry bounds [{self.minimum_entry_atr}, {self.maximum_entry_atr}] | Setup skipped.",
                level="WARNING",
            )
            return

        sig_side = "BUY" if enter_long else "SELL"
        signal_spot = float(last["Close"])

        stop_p = st_val
        risk_points = max(1.0, abs(signal_spot - stop_p))

        # Dynamic risk-budget position sizing
        if self.risk_budget > 0 and risk_points > 0:
            calc_lots = self.risk_budget / (risk_points * self.round_lot_size)
            lots = max(1, min(self.max_lots, int(round(calc_lots))))
        else:
            lots = max(1, min(self.max_lots, self.quantity_lots))

        order_qty = lots * self.round_lot_size
        trade_symbol, order_side, ref_price = self._resolve_execution_symbol(sig_side, signal_spot, latest_dt)

        target_p = (
            signal_spot + (risk_points * self.risk_reward)
            if enter_long
            else signal_spot - (risk_points * self.risk_reward)
        )

        self.log_decision(
            "ENTRY CONDITION MATCH",
            f"Supertrend Momentum [{self.variant}] {sig_side} Signal | Spot={signal_spot:.2f} | "
            f"ATR={atr_val:.1f} | Stop={stop_p:.2f} (Risk={risk_points:.1f} pts) | Target={target_p:.2f} | "
            f"Budget=₹{self.risk_budget:,.0f} -> Lots={lots} (Qty={order_qty}) | Contract={trade_symbol}",
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
            "entry_price": ref_price,
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
                    "entry": ref_price,
                    "exit": None,
                    "ltp": ref_price,
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
        target_signal = self.symbol
        is_signal_sym = (
            (sym == target_signal)
            or (":" in sym and sym.split(":")[-1] == target_signal.split(":")[-1])
        )
        if is_signal_sym:
            self.last_underlying_ltp = ltp_f

        is_future_sig = (
            hasattr(self, "signal_symbol")
            and (sym == self.signal_symbol or (":" in sym and sym.split(":")[-1] == self.signal_symbol.split(":")[-1]))
        )
        if is_future_sig:
            self.last_trade_ltp = ltp_f

        if self.trade_state != "IN_TRADE" or not self.current_trade:
            return

        trade_sym = self.current_trade.get("symbol", "")
        is_trade_sym = (sym == trade_sym) or (":" in sym and sym.split(":")[-1] == trade_sym.split(":")[-1])

        if is_trade_sym:
            self.last_trade_ltp = ltp_f
            if self.current_trade.get("legs"):
                leg = self.current_trade["legs"][0]
                leg["ltp"] = ltp_f
                ep = float(leg.get("entry") or 0.0)
                if ep > 0 and leg.get("status") in ["FILLED", "OPEN"]:
                    is_long = int(leg.get("qty", 1)) > 0
                    diff = (ltp_f - ep) if is_long else (ep - ltp_f)
                    pnl = round(diff * abs(int(leg.get("qty", 1))), 2)
                    leg["pnl"] = pnl
                    self.current_trade["total_pnl"] = pnl

        # Sub-second Stop Loss & Target checks against the Spot signal instrument
        # (stop_price & target_price are computed in Spot points)
        if not is_signal_sym:
            return

        stop_p = float(self.current_trade.get("stop_price") or 0.0)
        target_p = float(self.current_trade.get("target_price") or 0.0)
        sig_side = self.current_trade.get("signal_side")

        if ltp_f > 0:
            if sig_side == "BUY":
                if self.risk_reward > 0 and target_p > 0 and ltp_f >= target_p:
                    self._execute_exit("TARGET_HIT", exit_spot=ltp_f)
                elif stop_p > 0 and ltp_f <= stop_p:
                    self._execute_exit("STOP_LOSS", exit_spot=ltp_f)
            elif sig_side == "SELL":
                if self.risk_reward > 0 and target_p > 0 and ltp_f <= target_p:
                    self._execute_exit("TARGET_HIT", exit_spot=ltp_f)
                elif stop_p > 0 and ltp_f >= stop_p:
                    self._execute_exit("STOP_LOSS", exit_spot=ltp_f)

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
            req_side = str(ord_req.get("side") or "").upper()
            entry_side = self.current_trade.get("order_side", "BUY")
            if req_side == entry_side or not req_side:
                avg_fill = round(sum(float(r.get("price", 0.0)) for r in filled) / len(filled), 2)
                if avg_fill > 0:
                    leg0["entry"] = avg_fill
                    leg0["ltp"] = avg_fill
                    self.current_trade["entry_price"] = avg_fill
                    # Scale stop and target into contract price terms preserving point risk
                    sig_spot = float(self.current_trade.get("entry_spot") or avg_fill)
                    orig_stop = float(self.current_trade.get("stop_price") or avg_fill * 0.99)
                    risk_pts = max(1.0, abs(sig_spot - orig_stop))
                    is_long = self.current_trade.get("signal_side") == "BUY"
                    contract_stop = (avg_fill - risk_pts) if is_long else (avg_fill + risk_pts)
                    self.current_trade["stop_price"] = round(contract_stop, 2)
                    self.current_trade["sl_price"] = round(contract_stop, 2)
                    if self.risk_reward > 0:
                        target_p = (avg_fill + (risk_pts * self.risk_reward)) if is_long else (avg_fill - (risk_pts * self.risk_reward))
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


__all__ = [
    "DEFAULT_PARAMS",
    "MomentumParamsOverrides",
    "MomentumParams",
    "ENTRY_CUTOFF_MINUTE",
    "_supertrend",
    "_rsi",
    "_adx",
    "_generate_setup_signals",
    "_execute_on_lower_timeframe",
    "generate_signals",
    "_extract_trades",
    "resolve_futures_lot_size",
    "resolve_future_contract",
    "resolve_nifty_future_contract",
    "compute_supertrend_chart_payload",
    "SupertrendMomentumStrategy",
]
