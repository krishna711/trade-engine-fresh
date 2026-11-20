"""
Strategy: PivotChandelier Pro v2.0.1 (PCP_Pro_v2_0_1)
Dual-Chandelier + JMA(RWI) + ADX + Intraday Orderflow Delta & MAE Hard-Stop Engine.

Combines:
1. Faithful translation of PCP_Pro_v2_0_1.afl indicators & ``generate_signals``:
   - Dual Chandelier bands: stable ``atr_multiplier`` band drives trend flips,
     independent ``exit_atr_mult`` band (clamped <= ``atr_multiplier``) trails protective stop.
   - Day-frozen ATR: ``baseAtr = ValueWhen(newday, ATR(atrPeriod))`` held per session.
   - Trend Filter: ADX > threshold & rising (``adx > Ref(adx, -3)``) + JMA-smoothed RWI bias.
   - Quality Gate: Intraday cumulative volume delta trend & momentum + optional volume SMA filter.
   - Risk-based lot sizing via ``equity_at_risk`` (max 8 lots) or configurable fixed lots.
   - Exits: MAE hard stop against bar color, trailing Chandelier stop, profit target, 15:15 EOD square-off,
     and max 2 losing trades per session guardrail.
2. Live Execution Wrapper ``PivotChandelierProV201Strategy(BaseStrategy)``:
   - Runs in an isolated OS worker container via StrategySupervisor.
   - Supports NSE/NFO Futures (with near-month contract auto-roll), ATM Options, Spot, or MCX.
   - Sub-second live Profit Target & MTM tracking via ``on_tick``, 1-second EOD timer via ``on_timer``,
     and seamless crash/restart recovery via ``rehydrate_state``.
"""

from __future__ import annotations

import datetime
import math
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


# ---------------------------------------------------------------------------
# Technical Indicators (AmiBroker AFL-Compatible)
# ---------------------------------------------------------------------------

def time_as_int(index: pd.Index) -> pd.Series:
    """Converts DatetimeIndex (or ISO timestamps) to HHMMSS integers in IST."""
    dt_idx = pd.to_datetime(index)
    if dt_idx.tz is not None:
        dt_idx = dt_idx.tz_convert(IST_TZ)
    vals = dt_idx.hour * 10000 + dt_idx.minute * 100 + dt_idx.second
    return pd.Series(vals, index=index, dtype=int)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Wilder True Range series matching AmiBroker TR."""
    prev_close = close.shift(1)
    hl = high - low
    hc = (high - prev_close).abs()
    lc = (low - prev_close).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    if len(tr) > 0 and pd.isna(tr.iloc[0]):
        tr.iloc[0] = hl.iloc[0]
    return tr.astype(float)


def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's exponential smoothing (alpha = 1 / period)."""
    p = max(1, int(period))
    return series.ewm(alpha=1.0 / p, adjust=False, min_periods=1).mean()


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 12,
    tr: Optional[pd.Series] = None,
) -> pd.Series:
    """Wilder's Average True Range matching AmiBroker ATR(period)."""
    tr_s = tr if tr is not None else true_range(high, low, close)
    return _wilder_smooth(tr_s, period)


def hhv(series: pd.Series, period: int) -> pd.Series:
    """Highest High Value over ``period`` bars matching AmiBroker HHV(x, n)."""
    p = max(1, int(period))
    return series.rolling(p, min_periods=1).max()


def llv(series: pd.Series, period: int) -> pd.Series:
    """Lowest Low Value over ``period`` bars matching AmiBroker LLV(x, n)."""
    p = max(1, int(period))
    return series.rolling(p, min_periods=1).min()


def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
    tr: Optional[pd.Series] = None,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Wilder ADX matching AmiBroker ADX(period). Returns (adx, +DI, -DI)."""
    p = max(1, int(period))
    tr_s = tr if tr is not None else true_range(high, low, close)
    atr_w = _wilder_smooth(tr_s, p).replace(0.0, np.nan)

    up_move = high - high.shift(1)
    down_move = low.shift(1) - low

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=high.index,
        dtype=float,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=high.index,
        dtype=float,
    )

    plus_di = 100.0 * _wilder_smooth(plus_dm, p) / atr_w
    minus_di = 100.0 * _wilder_smooth(minus_dm, p) / atr_w
    di_sum = (plus_di + minus_di).replace(0.0, np.nan)
    dx = (100.0 * (plus_di - minus_di).abs() / di_sum).fillna(0.0)
    adx_val = _wilder_smooth(dx, p).fillna(0.0)
    return adx_val, plus_di.fillna(0.0), minus_di.fillna(0.0)


def rwi_components(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    min_period: int = 12,
    max_period: int = 40,
    tr: Optional[pd.Series] = None,
) -> Tuple[pd.Series, pd.Series]:
    """
    Random Walk Index High and Low components matching AmiBroker RWIHi / RWILo.
    Returns (rwi_lo, rwi_hi).
    """
    tr_s = tr if tr is not None else true_range(high, low, close)
    min_p = max(2, int(min_period))
    max_p = max(min_p, int(max_period))

    hi_arr = high.to_numpy(dtype=float)
    lo_arr = low.to_numpy(dtype=float)
    tr_arr = tr_s.to_numpy(dtype=float)
    n = len(hi_arr)

    rwi_hi = np.zeros(n, dtype=float)
    rwi_lo = np.zeros(n, dtype=float)

    # Prefix sum for O(1) rolling TR sum over any lookback k
    csum = np.concatenate(([0.0], np.cumsum(np.nan_to_num(tr_arr, nan=0.0))))

    for k in range(min_p, max_p + 1):
        if k >= n:
            break
        # Average TR over k bars: (Sum_{j=i-k+1..i} TR[j]) / k; denom = avg_tr * sqrt(k) = sum_tr / sqrt(k)
        sum_tr = csum[k + 1 :] - csum[1 : n - k + 1]
        denom = sum_tr / math.sqrt(k)
        with np.errstate(divide="ignore", invalid="ignore"):
            cand_hi = np.where(denom > 1e-9, (hi_arr[k:] - lo_arr[: n - k]) / denom, 0.0)
            cand_lo = np.where(denom > 1e-9, (hi_arr[: n - k] - lo_arr[k:]) / denom, 0.0)
        rwi_hi[k:] = np.maximum(rwi_hi[k:], cand_hi)
        rwi_lo[k:] = np.maximum(rwi_lo[k:], cand_lo)

    return (
        pd.Series(rwi_lo, index=high.index, dtype=float),
        pd.Series(rwi_hi, index=high.index, dtype=float),
    )


def jma(series: pd.Series, period: int = 16, phase: float = 0.0) -> pd.Series:
    """
    Jurik Moving Average (adaptive low-lag smoother).
    Follows the standard Mark Jurik JMA recursive formulation.
    """
    vals = series.to_numpy(dtype=float)
    n = len(vals)
    if n == 0:
        return series.copy()

    length = max(1.0, float(period))
    phase_ratio = 0.5 if phase < -100 else (2.5 if phase > 100 else phase / 100.0 + 1.5)
    beta = 0.45 * (length - 1.0) / (0.45 * (length - 1.0) + 2.0)
    alpha = math.pow(beta, 2.0)

    out = np.zeros(n, dtype=float)
    e0 = 0.0
    e1 = 0.0
    e2 = 0.0
    jma_prev = 0.0
    initialized = False

    for i in range(n):
        src = vals[i]
        if not math.isfinite(src):
            out[i] = jma_prev if initialized else 0.0
            continue
        if not initialized:
            e0 = src
            e1 = 0.0
            e2 = 0.0
            jma_prev = src
            out[i] = src
            initialized = True
            continue

        e0 = (1.0 - alpha) * src + alpha * e0
        e1 = (src - e0) * (1.0 - beta) + beta * e1
        e2 = (e0 + phase_ratio * e1 - jma_prev) * math.pow(1.0 - alpha, 2.0) + math.pow(alpha, 2.0) * e2
        jma_val = e2 + jma_prev
        jma_prev = jma_val
        out[i] = jma_val

    return pd.Series(out, index=series.index, dtype=float)


def resolve_frame_lot_size(
    df: pd.DataFrame, override: Optional[int] = None
) -> Tuple[str, int]:
    """Resolves (symbol, lot_size) using explicit override, DataFrame attrs, or symbol_mapper."""
    sym = str(df.attrs.get("symbol") or "NSE:NIFTY")
    if override is not None and int(override) > 0:
        return sym, int(override)
    attr_lot = df.attrs.get("round_lot_size")
    if attr_lot is not None and int(attr_lot) > 0:
        return sym, int(attr_lot)
    mapped = symbol_mapper.get_lot_size(sym)
    return sym, int(mapped if mapped and mapped > 0 else DEFAULT_LOT_SIZE)


# ---------------------------------------------------------------------------
# Strategy Parameter Types & Defaults
# ---------------------------------------------------------------------------

class PCPv201ParamsOverrides(TypedDict, total=False):
    """Partial parameter overrides accepted by ``generate_signals``."""
    atr_multiplier: float
    atr_period: int
    exit_atr_mult: float
    use_close_for_extremums: bool
    equity_at_risk: int
    target_long_pct: float
    target_short_pct: float
    jma_period: int
    rwi_min_period: int
    rwi_max_period: int
    first_trade_time: int
    last_trade_time: int
    square_off_time: int
    shakeout_start: int
    shakeout_end: int
    adx_period: int
    adx_threshold: float
    use_delta_filter: bool
    use_volume_ok_filter: bool
    use_mae_stop: bool
    mae_stop_mult: float
    quality_gate_threshold: int
    llv_period: int
    hhv_period: int
    vol_sma_period: int
    vol_sma_lt_period: int
    delta_sma_period: int
    delta_momentum_period: int
    round_lot_size: int
    max_trades_per_day: int
    reentry_cooldown_bars: int


class PCPv201Params(PCPv201ParamsOverrides):
    """Fully resolved parameter set."""
    atr_multiplier: float
    atr_period: int
    exit_atr_mult: float
    use_close_for_extremums: bool
    equity_at_risk: int
    target_long_pct: float
    target_short_pct: float
    jma_period: int
    rwi_min_period: int
    rwi_max_period: int
    first_trade_time: int
    last_trade_time: int
    square_off_time: int
    shakeout_start: int
    shakeout_end: int
    adx_period: int
    adx_threshold: float
    use_delta_filter: bool
    use_volume_ok_filter: bool
    use_mae_stop: bool
    mae_stop_mult: float
    quality_gate_threshold: int
    llv_period: int
    hhv_period: int
    vol_sma_period: int
    vol_sma_lt_period: int
    delta_sma_period: int
    delta_momentum_period: int
    round_lot_size: int
    max_trades_per_day: int
    reentry_cooldown_bars: int


DEFAULT_PARAMS: PCPv201Params = PCPv201Params(
    atr_multiplier=5.2,
    atr_period=12,
    exit_atr_mult=5.2,
    use_close_for_extremums=True,
    equity_at_risk=20000,
    target_long_pct=27 / 1000,
    target_short_pct=23 / 1000,
    jma_period=16,
    rwi_min_period=12,
    rwi_max_period=40,
    first_trade_time=91800,
    last_trade_time=124200,
    square_off_time=151500,
    shakeout_start=94800,
    shakeout_end=100600,
    adx_period=14,
    adx_threshold=18,
    use_delta_filter=True,
    use_volume_ok_filter=False,
    use_mae_stop=True,
    mae_stop_mult=0.75,
    quality_gate_threshold=1,
    llv_period=9,
    hhv_period=15,
    vol_sma_period=60,
    vol_sma_lt_period=180,
    delta_sma_period=3,
    delta_momentum_period=15,
    round_lot_size=DEFAULT_LOT_SIZE,
    max_trades_per_day=0,
    reentry_cooldown_bars=0,
)


# ---------------------------------------------------------------------------
# Signal Generator (Pure Pandas / NumPy Engine)
# ---------------------------------------------------------------------------

def generate_signals(
    df: pd.DataFrame,
    params: PCPv201ParamsOverrides | None = None,
) -> pd.DataFrame:
    """
    Generate trading signals for the PivotChandelier Pro v2.0.1 strategy.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain columns Open, High, Low, Close, Volume with a
        datetime index (or integer time index).
    params : PCPv201ParamsOverrides, optional
        Overrides for DEFAULT_PARAMS.

    Returns
    -------
    pd.DataFrame with signal/state columns added.
    """
    p = DEFAULT_PARAMS.copy()
    if params:
        p.update(params)
    # AFL: exitAtrMult = Min(atrMultiplier, "Exit ATR Mult" param).
    p["exit_atr_mult"] = min(p["atr_multiplier"], p["exit_atr_mult"])

    out = df.copy()
    symbol, lot_size = resolve_frame_lot_size(
        df, params.get("round_lot_size") if params else None
    )
    p["round_lot_size"] = lot_size
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col not in out.columns:
            raise ValueError(f"Missing required column: {col}")

    if len(out) < 2:
        raise ValueError("At least 2 bars are required to generate signals")

    # ---------------------------------------------------------------
    # Base Series
    # ---------------------------------------------------------------
    TN = time_as_int(out.index)
    tr = true_range(out.High, out.Low, out.Close)
    day = pd.to_datetime(out.index).date
    new_day = np.concatenate(([True], day[1:] != day[:-1]))
    # AFL: baseAtr = ValueWhen(newday, ATR(atrPeriod)) — the ATR captured on
    # each day's first bar is held for the rest of the session.
    base_atr = (
        atr(out.High, out.Low, out.Close, p["atr_period"], tr=tr)
        .where(new_day)
        .ffill()
    )

    atr_value = p["atr_multiplier"] * base_atr
    exit_atr_value = p["exit_atr_mult"] * base_atr

    extreme_high = out.Close if p["use_close_for_extremums"] else out.High
    extreme_low = out.Close if p["use_close_for_extremums"] else out.Low
    highest_extreme = hhv(extreme_high, p["atr_period"])
    lowest_extreme = llv(extreme_low, p["atr_period"])

    is_trade_window = (p["first_trade_time"] <= TN) & (p["last_trade_time"] >= TN)
    is_square_off = p["square_off_time"] <= TN
    is_shakeout = (p["shakeout_start"] <= TN) & (p["shakeout_end"] >= TN)
    is_trade_window = is_trade_window & ~is_shakeout

    adx_val, _, _ = adx(out.High, out.Low, out.Close, p["adx_period"], tr=tr)
    # AFL Ref(x, -3) is the value from three bars ago.
    adx_rising = adx_val > adx_val.shift(3)
    adx_filter = (adx_val > p["adx_threshold"]) & adx_rising

    LLV_9 = llv(out.Low, p["llv_period"])
    HHV_15 = hhv(out.High, p["hhv_period"])

    # ---------------------------------------------------------------
    # JMA + RWI Trend Filter
    # ---------------------------------------------------------------
    rwi_lo_series, rwi_hi_series = rwi_components(
        out.High,
        out.Low,
        out.Close,
        p["rwi_min_period"],
        p["rwi_max_period"],
        tr=tr,
    )

    jma_rwi_low = jma(rwi_lo_series, p["jma_period"])
    jma_rwi_high = jma(rwi_hi_series, p["jma_period"])

    high_bias = jma_rwi_high > 0.6
    low_bias = jma_rwi_low > 0.6
    trend_mode = np.where(high_bias, 1, np.where(low_bias, -1, 0))

    # ---------------------------------------------------------------
    # Entry Setup Conditions (orderflow delta + volume)
    # ---------------------------------------------------------------
    cg = out.Close > out.Open
    grp = pd.Series(day, index=out.index)

    green_volume = out.Volume.where(cg, 0.0)
    red_volume = out.Volume.where(~cg, 0.0)
    sum_green = green_volume.groupby(grp).cumsum()
    sum_red = red_volume.groupby(grp).cumsum()
    cum_delta = sum_green - sum_red
    total_vol = sum_green + sum_red
    delta_ratio = np.where(
        total_vol > 0,
        sum_green / total_vol.clip(lower=1),
        0.5,
    )

    vol_sma = out.Volume.rolling(
        p["vol_sma_period"], min_periods=p["vol_sma_period"]
    ).mean()
    vol_sma_lt = out.Volume.rolling(
        p["vol_sma_lt_period"], min_periods=p["vol_sma_lt_period"]
    ).mean()
    volume_ok = (vol_sma >= vol_sma_lt).fillna(False)

    delta_sma = cum_delta.rolling(
        p["delta_sma_period"], min_periods=p["delta_sma_period"]
    ).mean()
    delta_trend = (cum_delta > delta_sma).fillna(False)
    delta_momentum = (cum_delta > cum_delta.shift(p["delta_momentum_period"])).fillna(False)

    long_delta_conditions = delta_trend & delta_momentum
    short_delta_conditions = (~delta_trend) & (~delta_momentum)

    session_bias_long = delta_ratio > 0.55
    session_bias_short = delta_ratio < 0.45

    c1 = out.Close.shift(1)

    buy_setup = is_trade_window & cg & (out.Close > c1) & adx_filter & (trend_mode != -1)
    short_setup = is_trade_window & (~cg) & (out.Close < c1) & adx_filter & (trend_mode != 1)

    udf = p["use_delta_filter"]
    uvf = p["use_volume_ok_filter"]
    delta_quality_score = (
        np.where(udf & long_delta_conditions, 1, 0)
        + np.where(udf & session_bias_long, 1, 0)
        + np.where(uvf & volume_ok, 1, 0)
    )
    short_delta_quality_score = (
        np.where(udf & short_delta_conditions, 1, 0)
        + np.where(udf & session_bias_short, 1, 0)
        + np.where(uvf & volume_ok, 1, 0)
    )
    any_filter_active = udf or uvf

    # ---------------------------------------------------------------
    # State Variables
    # ---------------------------------------------------------------
    n = len(out)
    Buy = np.zeros(n, dtype=bool)
    Sell = np.zeros(n, dtype=bool)
    Short = np.zeros(n, dtype=bool)
    Cover = np.zeros(n, dtype=bool)

    BuyPrice = np.full(n, np.nan)
    SellPrice = np.full(n, np.nan)
    ShortPrice = np.full(n, np.nan)
    CoverPrice = np.full(n, np.nan)

    trendLongStop = np.full(n, np.nan)
    trendShortStop = np.full(n, np.nan)
    longStop = np.full(n, np.nan)
    shortStop = np.full(n, np.nan)
    trendDirection = np.zeros(n, dtype=int)
    profitTarget = np.full(n, np.nan)
    exitLine = np.full(n, np.nan)

    isLongActive = np.zeros(n, dtype=bool)
    isShortActive = np.zeros(n, dtype=bool)

    blots = np.zeros(n, dtype=int)
    slots = np.zeros(n, dtype=int)
    buyRiskPerShare = np.zeros(n)
    shortRiskPerShare = np.zeros(n)

    trendDirection[0] = 1
    hasProfitTargetHit = False
    entryLongPrice = 0.0
    entryShortPrice = 0.0
    maeLong = 0.0
    maeShort = 0.0
    trades_today = 0
    last_long_exit_i = -10**9
    last_short_exit_i = -10**9
    daily_loss_count = 0

    # numpy views for the loop
    open_arr = out.Open.to_numpy()
    high_arr = out.High.to_numpy()
    low_arr = out.Low.to_numpy()
    close_arr = out.Close.to_numpy()
    he = highest_extreme.to_numpy()
    le = lowest_extreme.to_numpy()
    base_atr_arr = base_atr.to_numpy()
    trend_atr_arr = atr_value.to_numpy()
    exit_atr_arr = exit_atr_value.to_numpy()
    h15 = HHV_15.to_numpy()
    l9 = LLV_9.to_numpy()
    dqs = delta_quality_score
    sdqs = short_delta_quality_score
    afa = any_filter_active
    qgt = p["quality_gate_threshold"]
    rls = p["round_lot_size"]
    ear = p["equity_at_risk"]
    max_trades_per_day = p["max_trades_per_day"]
    cooldown_bars = p["reentry_cooldown_bars"]

    isSqOff = is_square_off.to_numpy()
    bsu = buy_setup.to_numpy()
    ssu = short_setup.to_numpy()
    cg_arr = cg.to_numpy()

    start = 1
    long_trigger_prev = False
    short_trigger_prev = False
    for i in range(start, n):
        trendLongStopBase = he[i] - trend_atr_arr[i]
        trendShortStopBase = le[i] + trend_atr_arr[i]
        longStopBase = he[i] - exit_atr_arr[i]
        shortStopBase = le[i] + exit_atr_arr[i]

        first_bar_of_day = day[i] != day[i - 1]
        if first_bar_of_day:
            trades_today = 0
            daily_loss_count = 0

        isLongActive[i] = isLongActive[i - 1]
        isShortActive[i] = isShortActive[i - 1]
        trendDirection[i] = trendDirection[i - 1]
        profitTarget[i] = profitTarget[i - 1]
        exitLine[i] = exitLine[i - 1]

        # Trend band (stable): trails to detect regime flips
        tls_prev = trendLongStop[i - 1]
        if not np.isnan(tls_prev) and close_arr[i - 1] > tls_prev:
            trendLongStop[i] = max(trendLongStopBase, tls_prev)
        else:
            trendLongStop[i] = trendLongStopBase

        tss_prev = trendShortStop[i - 1]
        if not np.isnan(tss_prev) and close_arr[i - 1] < tss_prev:
            trendShortStop[i] = min(trendShortStopBase, tss_prev)
        else:
            trendShortStop[i] = trendShortStopBase

        # Exit band (independent): trails the protective stop
        ls_prev = longStop[i - 1]
        if not np.isnan(ls_prev) and close_arr[i - 1] > ls_prev:
            longStop[i] = max(longStopBase, ls_prev)
        else:
            longStop[i] = longStopBase

        ss_prev = shortStop[i - 1]
        if not np.isnan(ss_prev) and close_arr[i - 1] < ss_prev:
            shortStop[i] = min(shortStopBase, ss_prev)
        else:
            shortStop[i] = shortStopBase

        # Determine trend direction from the trend band
        upper = max(
            trendShortStop[i - 1] if not np.isnan(trendShortStop[i - 1]) else -np.inf,
            trendLongStop[i - 1] if not np.isnan(trendLongStop[i - 1]) else -np.inf,
        )
        lower = min(
            trendLongStop[i - 1] if not np.isnan(trendLongStop[i - 1]) else np.inf,
            trendShortStop[i - 1] if not np.isnan(trendShortStop[i - 1]) else np.inf,
        )
        if close_arr[i] > upper and not first_bar_of_day:
            trendDirection[i] = 1
        elif close_arr[i] < lower and not first_bar_of_day:
            trendDirection[i] = -1

        longTrigger = bool(bsu[i]) and trendDirection[i - 1] == 1 and trendDirection[i] == 1
        shortTrigger = bool(ssu[i]) and trendDirection[i - 1] == -1 and trendDirection[i] == -1

        # ---------------- Long trade management ----------------
        if isLongActive[i]:
            maeLong = max(maeLong, entryLongPrice - low_arr[i])

            el_prev = exitLine[i - 1]
            exitLine[i] = max(el_prev if not np.isnan(el_prev) else -np.inf, longStop[i])

            # Exit priority: MAE hard stop, then trailing / square-off
            if (
                p["use_mae_stop"]
                and maeLong > p["mae_stop_mult"] * base_atr_arr[i]
                and not cg_arr[i]
            ):
                Sell[i] = True
                SellPrice[i] = open_arr[i]
                isLongActive[i] = False
                last_long_exit_i = i
            elif isSqOff[i] or close_arr[i - 1] < (
                exitLine[i - 1] if not np.isnan(exitLine[i - 1]) else -np.inf
            ):
                Sell[i] = True
                SellPrice[i] = open_arr[i]
                isLongActive[i] = False
                last_long_exit_i = i

            # Profit target exit
            if high_arr[i] >= profitTarget[i] and not hasProfitTargetHit:
                Sell[i] = True
                SellPrice[i] = profitTarget[i]
                isLongActive[i] = False
                last_long_exit_i = i
                hasProfitTargetHit = True

        # ---------------- Short trade management ----------------
        if isShortActive[i]:
            maeShort = max(maeShort, high_arr[i] - entryShortPrice)

            el_prev = exitLine[i - 1]
            exitLine[i] = min(el_prev if not np.isnan(el_prev) else np.inf, shortStop[i])

            # Exit priority: MAE hard stop, then trailing / square-off
            if (
                p["use_mae_stop"]
                and maeShort > p["mae_stop_mult"] * base_atr_arr[i]
                and cg_arr[i]
            ):
                Cover[i] = True
                CoverPrice[i] = open_arr[i]
                isShortActive[i] = False
                last_short_exit_i = i
            elif isSqOff[i] or close_arr[i - 1] > (
                exitLine[i - 1] if not np.isnan(exitLine[i - 1]) else np.inf
            ):
                Cover[i] = True
                CoverPrice[i] = open_arr[i]
                isShortActive[i] = False
                last_short_exit_i = i

            # Profit target exit
            if low_arr[i] <= profitTarget[i] and not hasProfitTargetHit:
                Cover[i] = True
                CoverPrice[i] = profitTarget[i]
                isShortActive[i] = False
                last_short_exit_i = i
                hasProfitTargetHit = True

        # Match AFL dailyLossCount: losing exits disable further entries after 2 losses
        if Sell[i] and SellPrice[i] < entryLongPrice:
            daily_loss_count += 1
        if Cover[i] and CoverPrice[i] > entryShortPrice:
            daily_loss_count += 1

        # ---------------- Long entry logic ----------------
        BuyPrice[i] = max(close_arr[i - 1], h15[i - 1])
        ls_prev = longStop[i - 1]
        risk = max(1.0, BuyPrice[i] - longStop[i])
        buyRiskPerShare[i] = risk
        blots[i] = int(min(np.floor(ear / (rls * risk)), 8))

        if (
            not isLongActive[i]
            and not Sell[i]
            and daily_loss_count < 2
            and long_trigger_prev
            and high_arr[i] > h15[i - 1]
            and close_arr[i - 1] > (ls_prev if not np.isnan(ls_prev) else -np.inf)
            and (dqs[i - 1] >= qgt or not afa)
            and blots[i - 1] >= 1
            and (max_trades_per_day <= 0 or trades_today < max_trades_per_day)
            and (cooldown_bars <= 0 or i - last_long_exit_i > cooldown_bars)
        ):
            Buy[i] = True
            isLongActive[i] = True
            exitLine[i] = longStop[i]
            profitTarget[i] = BuyPrice[i] * (1 + p["target_long_pct"])
            hasProfitTargetHit = False
            entryLongPrice = BuyPrice[i]
            maeLong = 0.0
            trades_today += 1

        # ---------------- Short entry logic ----------------
        ShortPrice[i] = min(close_arr[i - 1], l9[i - 1])
        ss_prev = shortStop[i - 1]
        risk = max(1.0, shortStop[i] - ShortPrice[i])
        shortRiskPerShare[i] = risk
        slots[i] = int(min(np.floor(ear / (rls * risk)), 8))

        if (
            not isShortActive[i]
            and not Cover[i]
            and daily_loss_count < 2
            and short_trigger_prev
            and low_arr[i] < l9[i - 1]
            and close_arr[i - 1] < (ss_prev if not np.isnan(ss_prev) else np.inf)
            and (sdqs[i - 1] >= qgt or not afa)
            and slots[i - 1] >= 1
            and (max_trades_per_day <= 0 or trades_today < max_trades_per_day)
            and (cooldown_bars <= 0 or i - last_short_exit_i > cooldown_bars)
        ):
            Short[i] = True
            isShortActive[i] = True
            exitLine[i] = shortStop[i]
            profitTarget[i] = ShortPrice[i] * (1 - p["target_short_pct"])
            hasProfitTargetHit = False
            entryShortPrice = ShortPrice[i]
            maeShort = 0.0
            trades_today += 1

        long_trigger_prev = longTrigger
        short_trigger_prev = shortTrigger

    # ---------------------------------------------------------------
    # Attach results
    # ---------------------------------------------------------------
    out["Buy"] = Buy
    out["Sell"] = Sell
    out["Short"] = Short
    out["Cover"] = Cover
    out["BuyPrice"] = BuyPrice
    out["SellPrice"] = SellPrice
    out["ShortPrice"] = ShortPrice
    out["CoverPrice"] = CoverPrice
    out["TrendLongStop"] = trendLongStop
    out["TrendShortStop"] = trendShortStop
    out["LongStop"] = longStop
    out["ShortStop"] = shortStop
    out["TrendDirection"] = trendDirection
    out["ProfitTarget"] = profitTarget
    out["ExitLine"] = exitLine
    out["IsLongActive"] = isLongActive
    out["IsShortActive"] = isShortActive
    out["Blots"] = blots
    out["Slots"] = slots
    out["BuyRiskPerShare"] = buyRiskPerShare
    out["ShortRiskPerShare"] = shortRiskPerShare
    out["TrendMode"] = trend_mode
    out["AdxFilter"] = adx_filter.to_numpy()
    out["Adx"] = adx_val.to_numpy()
    out["JmaRwiHigh"] = jma_rwi_high.to_numpy()
    out["JmaRwiLow"] = jma_rwi_low.to_numpy()
    out["BaseAtr"] = base_atr_arr
    out.attrs["round_lot_size"] = rls
    out.attrs["symbol"] = symbol
    return out


def compute_pcp_chart_payload(bars: List[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Computes OHLC candles, Chandelier/ADX/RWI overlays, and Buy/Sell/Short/Cover signals for charting."""
    valid_bars = [
        b for b in (bars or [])
        if b.get("open") is not None and b.get("high") is not None and b.get("low") is not None and b.get("close") is not None
    ]
    if not valid_bars:
        return {"candles": [], "overlays": {}, "oscillators": {}, "signals": []}

    rows = []
    idx = []
    for b in valid_bars:
        dt_ist = _parse_bar_ist(b.get("time"))
        idx.append(dt_ist)
        o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
        v = float(b.get("volume") or 0.0)
        rows.append({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v})

    if len(rows) < 2:
        return {"candles": [], "overlays": {}, "oscillators": {}, "signals": []}

    df = pd.DataFrame(rows, index=pd.DatetimeIndex(idx))
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.attrs["symbol"] = cfg.get("symbol", "NSE:NIFTY")

    # Generate candles_out strictly from deduplicated & sorted df so indices and lengths match 1:1
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

    params: PCPv201ParamsOverrides = {
        "atr_multiplier": float(cfg.get("atr_multiplier", 5.2)),
        "atr_period": int(cfg.get("atr_period", 12)),
        "exit_atr_mult": float(cfg.get("exit_atr_mult", 5.2)),
        "use_close_for_extremums": bool(cfg.get("use_close_for_extremums", True)),
        "equity_at_risk": int(cfg.get("equity_at_risk", 20000)),
        "target_long_pct": float(cfg.get("target_long_pct", 27 / 1000)),
        "target_short_pct": float(cfg.get("target_short_pct", 23 / 1000)),
        "jma_period": int(cfg.get("jma_period", 16)),
        "rwi_min_period": int(cfg.get("rwi_min_period", 12)),
        "rwi_max_period": int(cfg.get("rwi_max_period", 40)),
        "first_trade_time": _parse_hhmmss(cfg.get("first_trade_time"), 91800),
        "last_trade_time": _parse_hhmmss(cfg.get("last_trade_time"), 124200),
        "square_off_time": _parse_hhmmss(cfg.get("square_off_time"), 151500),
        "shakeout_start": _parse_hhmmss(cfg.get("shakeout_start"), 94800),
        "shakeout_end": _parse_hhmmss(cfg.get("shakeout_end"), 100600),
        "adx_period": int(cfg.get("adx_period", 14)),
        "adx_threshold": float(cfg.get("adx_threshold", 18)),
        "use_delta_filter": bool(cfg.get("use_delta_filter", True)),
        "use_volume_ok_filter": bool(cfg.get("use_volume_ok_filter", False)),
        "use_mae_stop": bool(cfg.get("use_mae_stop", True)),
        "mae_stop_mult": float(cfg.get("mae_stop_mult", 0.75)),
        "quality_gate_threshold": int(cfg.get("quality_gate_threshold", 1)),
        "llv_period": int(cfg.get("llv_period", 9)),
        "hhv_period": int(cfg.get("hhv_period", 15)),
        "vol_sma_period": int(cfg.get("vol_sma_period", 60)),
        "vol_sma_lt_period": int(cfg.get("vol_sma_lt_period", 180)),
        "delta_sma_period": int(cfg.get("delta_sma_period", 3)),
        "delta_momentum_period": int(cfg.get("delta_momentum_period", 15)),
        "round_lot_size": int(cfg.get("round_lot_size") or cfg.get("lot_size") or DEFAULT_LOT_SIZE),
        "max_trades_per_day": int(cfg.get("max_trades_per_day", 0)),
        "reentry_cooldown_bars": int(cfg.get("reentry_cooldown_bars", 0)),
    }

    sig_df = generate_signals(df, params)

    def _clean_series(col: str) -> List[Optional[float]]:
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
            signals.append({"index": pos_i, "time": ts_iso, "label_time": lbl_t, "type": "BUY", "price": round(p_val, 2), "label": f"BUY ({int(row['Blots'])}L)"})
        if bool(row["Sell"]):
            p_val = float(row["SellPrice"]) if math.isfinite(row["SellPrice"]) else float(row["Close"])
            signals.append({"index": pos_i, "time": ts_iso, "label_time": lbl_t, "type": "SELL", "price": round(p_val, 2), "label": "SELL EXIT"})
        if bool(row["Short"]):
            p_val = float(row["ShortPrice"]) if math.isfinite(row["ShortPrice"]) else float(row["Close"])
            signals.append({"index": pos_i, "time": ts_iso, "label_time": lbl_t, "type": "SHORT", "price": round(p_val, 2), "label": f"SHORT ({int(row['Slots'])}L)"})
        if bool(row["Cover"]):
            p_val = float(row["CoverPrice"]) if math.isfinite(row["CoverPrice"]) else float(row["Close"])
            signals.append({"index": pos_i, "time": ts_iso, "label_time": lbl_t, "type": "COVER", "price": round(p_val, 2), "label": "COVER EXIT"})

    return {
        "candles": candles_out,
        "overlays": {
            "long_stop": _clean_series("LongStop"),
            "short_stop": _clean_series("ShortStop"),
            "trend_long_stop": _clean_series("TrendLongStop"),
            "trend_short_stop": _clean_series("TrendShortStop"),
            "exit_line": _clean_series("ExitLine"),
            "profit_target": _clean_series("ProfitTarget"),
        },
        "oscillators": {
            "adx": _clean_series("Adx"),
            "adx_threshold": float(params["adx_threshold"]),
            "jma_rwi_high": _clean_series("JmaRwiHigh"),
            "jma_rwi_low": _clean_series("JmaRwiLow"),
            "rwi_threshold": 0.6,
            "trend_direction": [int(v) for v in sig_df["TrendDirection"].to_numpy()],
        },
        "signals": signals,
    }


# ---------------------------------------------------------------------------
# Helpers for Live Worker Container Execution
# ---------------------------------------------------------------------------

def _parse_hhmmss(val: Any, fallback: int) -> int:
    if isinstance(val, (int, float)) and math.isfinite(val):
        return int(val)
    s = str(val or "").strip()
    if not s:
        return fallback
    if len(s) == 6 and s.isdigit():
        return int(s)
    parts = s.split(":")
    if len(parts) >= 2:
        try:
            h = int(parts[0])
            m = int(parts[1])
            sec = int(parts[2]) if len(parts) > 2 else 0
            return h * 10000 + m * 100 + sec
        except Exception:
            pass
    return fallback


def _parse_bar_ist(t_val: Any) -> datetime.datetime:
    if isinstance(t_val, datetime.datetime):
        dt = t_val
    elif isinstance(t_val, (int, float)):
        sec = t_val / 1000.0 if t_val > 1e11 else t_val
        dt = datetime.datetime.fromtimestamp(sec, tz=datetime.timezone.utc)
    else:
        s = str(t_val or "").replace("Z", "+00:00")
        try:
            dt = datetime.datetime.fromisoformat(s)
        except Exception:
            dt = datetime.datetime.now(datetime.timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST_TZ)
    else:
        dt = dt.astimezone(IST_TZ)
    return dt


def _get_last_thursday(year: int, month: int) -> datetime.date:
    next_first = (
        datetime.date(year + 1, 1, 1)
        if month == 12
        else datetime.date(year, month + 1, 1)
    )
    last_day = next_first - datetime.timedelta(days=1)
    offset = (last_day.weekday() - 3) % 7
    return last_day - datetime.timedelta(days=offset)


def _snap_strike(spot: float, step: float) -> int:
    if not math.isfinite(spot) or spot <= 0:
        return 0
    s = max(1.0, float(step))
    return int(round(spot / s) * s)


SUPPORTED_STOCK_PROFILES: Dict[str, Dict[str, Any]] = {
    "HAL": {
        "underlying": "HAL",
        "lot_size": 150,
        "strike_step": 50.0,
        "base_price": 4250.0,
    },
    "RELIANCE": {
        "underlying": "RELIANCE",
        "lot_size": 250,
        "strike_step": 20.0,
        "base_price": 2980.0,
    },
    "BHARTIARTL": {
        "underlying": "BHARTIARTL",
        "lot_size": 475,
        "strike_step": 20.0,
        "base_price": 1610.0,
    },
    "BHARTIAIRTEL": {
        "underlying": "BHARTIARTL",
        "lot_size": 475,
        "strike_step": 20.0,
        "base_price": 1610.0,
    },
    "NIFTY": {
        "underlying": "NIFTY",
        "lot_size": 65,
        "strike_step": 50.0,
        "base_price": 24925.0,
    },
    "BANKNIFTY": {
        "underlying": "BANKNIFTY",
        "lot_size": 30,
        "strike_step": 100.0,
        "base_price": 51860.0,
    },
}


def normalize_underlying_root(raw_sym: str) -> str:
    """Normalizes underlying ticker: strips segment prefixes, handles BHARTIAIRTEL -> BHARTIARTL."""
    import re
    s = str(raw_sym or "HAL").strip().upper()
    if ":" in s:
        s = s.split(":")[-1]
    m = re.match(r"^([A-Z]+?)(\d{2}[A-Z]{3})?FUT$", s)
    if m:
        s = m.group(1)
    if s in ["BHARTIAIRTEL", "AIRTEL"]:
        s = "BHARTIARTL"
    return s


def resolve_future_contract(
    underlying: str,
    now_dt: Optional[datetime.datetime] = None,
    roll_last_friday: bool = True
) -> str:
    """Resolves canonical current-month NSE Future contract symbol (e.g. NFO:HAL26NOVFUT)."""
    now = now_dt or datetime.datetime.now(IST_TZ)
    root = normalize_underlying_root(underlying)
    if "MCX" in str(underlying).upper():
        return f"MCX:{root}"
    year, month = now.year, now.month
    last_thu = _get_last_thursday(year, month)
    roll_threshold = (last_thu - datetime.timedelta(days=6)) if roll_last_friday else last_thu
    if now.date() >= roll_threshold:
        month = month + 1 if month < 12 else 1
        year = year if month > 1 else year + 1
    year_str = str(year)[-2:]
    month_code = MONTH_CODES[month]
    return f"NFO:{root}{year_str}{month_code}FUT"


def resolve_option_contract(
    underlying: str,
    strike: int,
    opt_type: str,
    now_dt: Optional[datetime.datetime] = None,
    roll_last_friday: bool = False
) -> str:
    """Resolves canonical monthly NSE Option contract symbol (e.g. NFO:HAL26NOV4150CE)."""
    now = now_dt or datetime.datetime.now(IST_TZ)
    root = normalize_underlying_root(underlying)
    year, month = now.year, now.month
    last_thu = _get_last_thursday(year, month)
    if now.date() > last_thu:
        month = month + 1 if month < 12 else 1
        year = year if month > 1 else year + 1
    year_str = str(year)[-2:]
    month_code = MONTH_CODES[month]
    seg = "MCXFO" if "MCX" in str(underlying).upper() else "NFO"
    return f"{seg}:{root}{year_str}{month_code}{int(strike)}{opt_type.upper()}"


class PivotChandelierProV201Strategy(BaseStrategy):
    """
    Live / Paper Algorithmic Worker Container for PivotChandelier Pro v2.0.1.
    - Signal Data Source: Current Month Future (e.g. NFO:HAL26NOVFUT).
    - Tradable Instrument: ITM-2 Strike Option Buy (CE for Long, PE for Short).
    - Multi-Stock Ready: Supports HAL (default), RELIANCE, and BHARTIARTL.
    """

    def on_init(self):
        raw_sym = str(self.config.get("symbol") or self.config.get("underlying") or "HAL").strip()
        self.underlying_root = normalize_underlying_root(raw_sym)
        self.symbol = f"NSE:{self.underlying_root}"

        profile = SUPPORTED_STOCK_PROFILES.get(self.underlying_root, {})
        default_step = float(profile.get("strike_step", 50.0))
        default_lot = int(profile.get("lot_size", symbol_mapper.get_lot_size(self.symbol) or 150))

        tf_raw = str(self.config.get("timeframe") or self.config.get("timeframe_min") or "3m").strip().lower()
        if tf_raw.isdigit():
            tf_raw = f"{tf_raw}m"
        self.timeframe = tf_raw

        # Trade Instrument: Default is OPTION per user requirement
        inst_raw = str(self.config.get("trade_instrument", "OPTION")).upper()
        if "FUT" in inst_raw:
            self.trade_instrument = "FUTURE"
        elif "SPOT" in inst_raw or "EQ" in inst_raw:
            self.trade_instrument = "SPOT"
        else:
            self.trade_instrument = "OPTION"

        # Signal Data Source: Default is FUTURE per user requirement
        self.data_source = str(self.config.get("data_source", "FUTURE")).upper()

        self.product = str(self.config.get("product", "NRML")).upper()
        self.order_type = str(self.config.get("order_type", "LIMIT")).upper()
        self.limit_buffer_pct = float(self.config.get("limit_buffer_pct", 0.015))
        self.auto_chase = bool(self.config.get("auto_chase", True))

        # ITM Offset / Depth (Default 2 for ITM-2 strike buy, configurable)
        self.itm_depth = int(self.config.get("itm_depth", 2))

        # Option strike settings & lot sizing
        self.opt_strike_step = float(self.config.get("opt_strike_step", default_step))
        self.round_lot_size = int(
            self.config.get("round_lot_size")
            or self.config.get("lot_size")
            or default_lot
        )
        self.position_sizing_mode = str(self.config.get("position_sizing_mode", "fixed")).lower()
        self.quantity_lots = int(self.config.get("quantity_lots", self.config.get("lots", 1)))
        self.max_lots = int(self.config.get("max_lots", 8))

        # Expiry auto-roll setting
        self.fut_roll_last_friday = bool(self.config.get("fut_roll_last_friday", True))

        # Resolve Signal Future Contract (Current Month)
        now_ist = datetime.datetime.now(IST_TZ)
        self.signal_symbol = resolve_future_contract(self.underlying_root, now_ist, self.fut_roll_last_friday)

        # Resolve target pct (accepts float fraction like 0.027 or per-mille/pct override)
        tl_raw = float(self.config.get("target_long_pct", 27 / 1000))
        ts_raw = float(self.config.get("target_short_pct", 23 / 1000))

        self.pcp_params: PCPv201ParamsOverrides = {
            "atr_multiplier": float(self.config.get("atr_multiplier", 5.2)),
            "atr_period": int(self.config.get("atr_period", 12)),
            "exit_atr_mult": float(self.config.get("exit_atr_mult", 5.2)),
            "use_close_for_extremums": bool(self.config.get("use_close_for_extremums", True)),
            "equity_at_risk": int(self.config.get("equity_at_risk", 20000)),
            "target_long_pct": tl_raw,
            "target_short_pct": ts_raw,
            "jma_period": int(self.config.get("jma_period", 16)),
            "rwi_min_period": int(self.config.get("rwi_min_period", 12)),
            "rwi_max_period": int(self.config.get("rwi_max_period", 40)),
            "first_trade_time": _parse_hhmmss(self.config.get("first_trade_time"), 91800),
            "last_trade_time": _parse_hhmmss(self.config.get("last_trade_time"), 124200),
            "square_off_time": _parse_hhmmss(self.config.get("square_off_time"), 151500),
            "shakeout_start": _parse_hhmmss(self.config.get("shakeout_start"), 94800),
            "shakeout_end": _parse_hhmmss(self.config.get("shakeout_end"), 100600),
            "adx_period": int(self.config.get("adx_period", 14)),
            "adx_threshold": float(self.config.get("adx_threshold", 18)),
            "use_delta_filter": bool(self.config.get("use_delta_filter", True)),
            "use_volume_ok_filter": bool(self.config.get("use_volume_ok_filter", False)),
            "use_mae_stop": bool(self.config.get("use_mae_stop", True)),
            "mae_stop_mult": float(self.config.get("mae_stop_mult", 0.75)),
            "quality_gate_threshold": int(self.config.get("quality_gate_threshold", 1)),
            "llv_period": int(self.config.get("llv_period", 9)),
            "hhv_period": int(self.config.get("hhv_period", 15)),
            "vol_sma_period": int(self.config.get("vol_sma_period", 60)),
            "vol_sma_lt_period": int(self.config.get("vol_sma_lt_period", 180)),
            "delta_sma_period": int(self.config.get("delta_sma_period", 3)),
            "delta_momentum_period": int(self.config.get("delta_momentum_period", 15)),
            "round_lot_size": self.round_lot_size,
            "max_trades_per_day": int(self.config.get("max_trades_per_day", 0)),
            "reentry_cooldown_bars": int(self.config.get("reentry_cooldown_bars", 0)),
        }

        self.square_off_time_num = int(self.pcp_params["square_off_time"])

        # Auto-subscribe Signal Future, Spot, and NFO base
        self.request_subscribe([
            self.signal_symbol,
            self.symbol,
            f"NFO:{self.underlying_root}",
            self.underlying_root
        ])

        # Live runtime state (preserved across config updates if already initialized)
        if not hasattr(self, "last_underlying_ltp"):
            self.last_underlying_ltp = 0.0
        if not hasattr(self, "_session_date"):
            self._session_date: Optional[datetime.date] = None
            self._trades_today = 0
            self._daily_loss_count = 0
            self._last_bucket_seen: Optional[datetime.datetime] = None
            self._last_entry_bucket: Optional[datetime.datetime] = None
            self._bars_since_long_exit = 10**6
            self._bars_since_short_exit = 10**6

        self.log_decision(
            "INIT",
            f"PivotChandelier Pro v2.0.1 initialized for {self.underlying_root} | "
            f"Signal Future: {self.signal_symbol} ({self.timeframe}) | "
            f"Instrument: {self.trade_instrument} (ITM-{self.itm_depth}) | "
            f"Lot: {self.round_lot_size} | Strike Step: {self.opt_strike_step} | "
            f"ATR {self.pcp_params['atr_period']}x{self.pcp_params['atr_multiplier']} | "
            f"ADX>{self.pcp_params['adx_threshold']} | JMA/RWI Bias | Window {self.square_off_time_num} EOD",
        )

    def on_config_updated(self, new_config: Dict[str, Any]):
        super().on_config_updated(new_config)
        self.on_init()

    def rehydrate_state(self, initial_state: Dict[str, Any]):
        """Recovers open position state cleanly on worker restart without duplicate entries."""
        legs = initial_state.get("legs_info", [])
        active = [
            dict(l) for l in legs
            if l.get("status") in ["FILLED", "OPEN", "PENDING"] and l.get("exit") is None
        ]
        if initial_state.get("trade_state") == "IN_TRADE" or active:
            self.trade_state = "IN_TRADE"
            leg0 = active[0] if active else {}
            sym = str(leg0.get("symbol") or self.symbol)
            raw_qty = int(leg0.get("qty") or self.round_lot_size)
            side = "BUY" if raw_qty > 0 else "SELL"
            entry_p = float(leg0.get("entry") or 0.0)
            ltp_p = float(leg0.get("ltp") or entry_p)

            tl = float(self.pcp_params.get("target_long_pct", 0.027))
            ts = float(self.pcp_params.get("target_short_pct", 0.023))
            pt_spot = round(entry_p * (1 + tl if side == "BUY" else 1 - ts), 2) if entry_p > 0 else None

            self.current_trade = {
                "symbol": sym,
                "underlying": self.symbol,
                "signal_symbol": getattr(self, "signal_symbol", self.symbol),
                "trade_instrument": self.trade_instrument,
                "signal_side": side,
                "order_side": "BUY" if self.trade_instrument == "OPTION" else side,
                "entry_price": entry_p,
                "entry_spot": entry_p,
                "quantity": abs(raw_qty),
                "pt_spot": pt_spot,
                "exit_line": None,
                "mae_val": 0.0,
                "base_atr": 0.0,
                "entry_time": initial_state.get("last_trade_at") or datetime.datetime.utcnow().isoformat(),
                "legs": [dict(l) for l in legs],
                "total_pnl": float(initial_state.get("unrealized_pnl", 0.0) or 0.0),
            }
            if sym and sym != self.symbol:
                self.request_subscribe([sym])

            self.log_decision(
                "STATE REHYDRATED",
                f"Resumed active {side} trade on {sym} (Qty: {abs(raw_qty)} @ ₹{entry_p:.2f}). "
                f"Tracking Chandelier trail, MAE stop & {self.square_off_time_num} EOD.",
                level="SUCCESS",
            )

    def _resolve_execution_symbol(self, side: str, spot: float, now_dt: datetime.datetime) -> Tuple[str, str, float]:
        """
        Resolves (canonical_trade_symbol, order_side, estimated_price) for entry.
        - Long Signal: BUY ITM-2 Call Option (CE)
        - Short Signal: BUY ITM-2 Put Option (PE)
        """
        if self.trade_instrument == "OPTION":
            atm_strike = _snap_strike(spot, self.opt_strike_step)
            if side == "BUY":  # Long Bullish signal -> BUY ITM Call
                opt_type = "CE"
                raw_strike = int(round(atm_strike - (self.itm_depth * self.opt_strike_step)))
            else:  # Short Bearish signal -> BUY ITM Put
                opt_type = "PE"
                raw_strike = int(round(atm_strike + (self.itm_depth * self.opt_strike_step)))

            snapped = symbol_mapper.snap_to_nearest_option_strike(self.underlying_root, raw_strike, opt_type)
            use_strike = snapped if snapped else raw_strike

            opt_sym = resolve_option_contract(self.underlying_root, use_strike, opt_type, now_dt)
            self.request_subscribe([opt_sym])

            # Realistic estimated option price for paper fills: intrinsic + time value
            intrinsic = float(self.itm_depth * self.opt_strike_step)
            time_val = max(10.0, float(self.opt_strike_step * 0.4))
            est_opt_price = round(intrinsic + time_val, 2)

            return opt_sym, "BUY", est_opt_price

        if self.trade_instrument == "SPOT":
            return self.symbol, side, spot

        # FUTURE contract execution
        fut_sym = resolve_future_contract(self.underlying_root, now_dt, self.fut_roll_last_friday)
        self.request_subscribe([fut_sym])
        return fut_sym, side, spot

    def on_candle(self, candle: Dict[str, Any], history: List[Dict[str, Any]]):
        """Evaluates PivotChandelier Pro v2.0.1 signals on incoming OHLC bars."""
        all_bars = (history or []) + ([candle] if candle else [])
        completed = [
            b for b in all_bars
            if b.get("open") is not None
            and b.get("high") is not None
            and b.get("low") is not None
            and b.get("close") is not None
            and math.isfinite(float(b.get("close", 0.0)))
        ]

        min_req = max(int(self.pcp_params["atr_period"]) + 4, 18)
        if len(completed) < min_req:
            self.log_decision(
                "Candle Fetch",
                f"Warming up indicators: {len(completed)}/{min_req} bars collected for {self.symbol}.",
            )
            return

        # Build IST-indexed OHLCV DataFrame
        rows = []
        idx = []
        for b in completed:
            dt_ist = _parse_bar_ist(b.get("time"))
            idx.append(dt_ist)
            rows.append({
                "Open": float(b["open"]),
                "High": float(b["high"]),
                "Low": float(b["low"]),
                "Close": float(b["close"]),
                "Volume": float(b.get("volume") or 0.0),
            })

        df = pd.DataFrame(rows, index=pd.DatetimeIndex(idx))
        # Ensure monotonically non-decreasing index for groupby/time checks
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df.attrs["symbol"] = self.symbol
        df.attrs["round_lot_size"] = self.round_lot_size

        if len(df) < min_req:
            return

        latest_dt = df.index[-1].to_pydatetime()
        is_new_bar = self._last_bucket_seen is None or latest_dt > self._last_bucket_seen
        if is_new_bar:
            if self._session_date != latest_dt.date():
                self._session_date = latest_dt.date()
                self._trades_today = 0
                self._daily_loss_count = 0
            self._last_bucket_seen = latest_dt
            self._bars_since_long_exit += 1
            self._bars_since_short_exit += 1

        sig_df = generate_signals(df, self.pcp_params)
        last = sig_df.iloc[-1]
        prev = sig_df.iloc[-2]
        self.last_underlying_ltp = float(last["Close"])

        base_atr_val = float(last["BaseAtr"]) if math.isfinite(last["BaseAtr"]) else 0.0
        if base_atr_val <= 0.0:
            return

        if is_new_bar:
            self.log_decision(
                "Candle Fetch Success",
                f"{self.symbol} C={last['Close']:.2f} | Dir={'LONG' if int(last['TrendDirection']) == 1 else 'SHORT'} | "
                f"Mode={int(last['TrendMode'])} | ADX_OK={bool(last['AdxFilter'])} | "
                f"LStop={last['LongStop']:.2f} SStop={last['ShortStop']:.2f} | "
                f"Blots={int(last['Blots'])} Slots={int(last['Slots'])}",
            )

        tn_now = int(latest_dt.hour * 10000 + latest_dt.minute * 100 + latest_dt.second)
        is_sq_off_now = tn_now >= self.square_off_time_num

        # -------------------------------------------------------------
        # ACTIVE TRADE MANAGEMENT
        # -------------------------------------------------------------
        if self.trade_state == "IN_TRADE" and self.current_trade:
            sig_side = self.current_trade["signal_side"]
            in_long = sig_side == "BUY"
            entry_spot = float(self.current_trade.get("entry_spot") or last["Close"])
            self.current_trade["base_atr"] = base_atr_val

            cur_open = float(last["Open"])
            cur_high = float(last["High"])
            cur_low = float(last["Low"])
            cur_close = float(last["Close"])
            is_green = cur_close > cur_open

            if in_long:
                self.current_trade["mae_val"] = max(
                    float(self.current_trade.get("mae_val", 0.0)),
                    entry_spot - cur_low,
                )
            else:
                self.current_trade["mae_val"] = max(
                    float(self.current_trade.get("mae_val", 0.0)),
                    cur_high - entry_spot,
                )

            prev_exit_line = self.current_trade.get("exit_line")
            if prev_exit_line is None or not math.isfinite(float(prev_exit_line)):
                prev_exit_line = float(last["LongStop"] if in_long else last["ShortStop"])

            entered_earlier_bar = (
                self._last_entry_bucket is None or latest_dt > self._last_entry_bucket
            )

            # 1. Square-off Time
            if is_sq_off_now:
                self._execute_exit("TIME", exit_spot=cur_close)
                return

            # 2. Profit Target
            pt_spot = self.current_trade.get("pt_spot")
            if pt_spot is not None:
                if (in_long and cur_high >= float(pt_spot)) or (not in_long and cur_low <= float(pt_spot)):
                    self._execute_exit("TARGET", exit_spot=float(pt_spot))
                    return

            if entered_earlier_bar:
                # 3. MAE Hard Stop against candle color
                mae_mult = float(self.pcp_params.get("mae_stop_mult", 0.75))
                if self.pcp_params.get("use_mae_stop", True):
                    mae_hit = float(self.current_trade["mae_val"]) > mae_mult * base_atr_val
                    if mae_hit and ((in_long and not is_green) or (not in_long and is_green)):
                        self._execute_exit("MAE_STOP", exit_spot=cur_open)
                        return

                # 4. Chandelier Trailing Stop on prior bar close
                prev_c = float(prev["Close"])
                if (in_long and prev_c < float(prev_exit_line)) or (not in_long and prev_c > float(prev_exit_line)):
                    self._execute_exit("CHANDELIER_STOP", exit_spot=cur_open)
                    return

            # Update trailing exit line monotonically
            band_val = float(last["LongStop"] if in_long else last["ShortStop"])
            if in_long:
                self.current_trade["exit_line"] = max(float(prev_exit_line), band_val)
                self.current_trade["sl_price"] = round(self.current_trade["exit_line"], 2)
            else:
                self.current_trade["exit_line"] = min(float(prev_exit_line), band_val)
                self.current_trade["sl_price"] = round(self.current_trade["exit_line"], 2)
            return

        # -------------------------------------------------------------
        # IDLE: EVALUATE ENTRY TRIGGER
        # -------------------------------------------------------------
        if self.trade_state != "IDLE" or is_sq_off_now:
            return

        # Guard: do not evaluate multiple entries within same bar
        if self._last_entry_bucket == latest_dt:
            return

        if self._daily_loss_count >= 2:
            if is_new_bar:
                self.log_decision("DAILY_LOSS_HALT", "2 losing exits reached today; pausing further entries.", level="WARNING")
            return

        max_t = int(self.pcp_params.get("max_trades_per_day", 0))
        if max_t > 0 and self._trades_today >= max_t:
            return

        enter_long = bool(last["Buy"])
        enter_short = bool(last["Short"])

        cd_bars = int(self.pcp_params.get("reentry_cooldown_bars", 0))
        if enter_long and cd_bars > 0 and self._bars_since_long_exit <= cd_bars:
            enter_long = False
        if enter_short and cd_bars > 0 and self._bars_since_short_exit <= cd_bars:
            enter_short = False

        if not enter_long and not enter_short:
            return

        sig_side = "BUY" if enter_long else "SELL"
        signal_spot = float(last["BuyPrice"] if enter_long else last["ShortPrice"])
        if not math.isfinite(signal_spot) or signal_spot <= 0:
            signal_spot = float(last["Close"])

        exit_line = float(last["ExitLine"])
        if not math.isfinite(exit_line):
            exit_line = float(last["LongStop"] if enter_long else last["ShortStop"])

        # Determine Lots & Quantity
        if self.position_sizing_mode == "fixed":
            lots = max(1, min(self.max_lots, self.quantity_lots))
        else:
            raw_lots = int(last["Blots"] if enter_long else last["Slots"])
            lots = max(0, min(self.max_lots, raw_lots))

        if lots <= 0:
            self.log_decision("SIZING_SKIP", "Calculated risk-based lots = 0. Trade skipped.", level="INFO")
            return

        is_mcx = "MCX" in self.symbol.upper()
        order_qty = lots if is_mcx else (lots * self.round_lot_size)

        trade_symbol, order_side, est_opt_price = self._resolve_execution_symbol(sig_side, signal_spot, latest_dt)

        tl_pct = float(self.pcp_params.get("target_long_pct", 0.027))
        ts_pct = float(self.pcp_params.get("target_short_pct", 0.023))
        pt_spot = signal_spot * (1.0 + tl_pct) if enter_long else signal_spot * (1.0 - ts_pct)

        self.log_decision(
            "ENTRY CONDITION MATCH",
            f"PCP v2.0.1 {sig_side} Signal on Future {self.signal_symbol} ({signal_spot:.2f}) | "
            f"Contract={trade_symbol} ({order_side} ITM-{self.itm_depth}) | Lots={lots} (Qty={order_qty}) | "
            f"Trail SL={exit_line:.2f} | Target Spot={pt_spot:.2f}",
            level="SUCCESS",
        )

        entry_order_price = est_opt_price if self.trade_instrument == "OPTION" else signal_spot

        # Dispatch Entry Order
        self.emit_order(
            symbol=trade_symbol,
            side=order_side,
            quantity=order_qty,
            order_type=self.order_type,
            price=entry_order_price,
            product=self.product,
            leg_id="LEG_PRIMARY",
            buffer_pct=self.limit_buffer_pct,
            auto_chase=self.auto_chase,
        )

        self.log_decision(
            "ORDER SENT TO BROKER",
            f"{order_side} {order_qty} {trade_symbol} ({lots} lot(s)) across {len(self.client_ids)} account(s).",
        )

        leg_qty = order_qty if order_side == "BUY" else -order_qty
        self.current_trade = {
            "symbol": trade_symbol,
            "underlying": self.symbol,
            "signal_symbol": self.signal_symbol,
            "trade_instrument": self.trade_instrument,
            "signal_side": sig_side,
            "order_side": order_side,
            "entry_price": entry_order_price,
            "entry_spot": signal_spot,
            "quantity": order_qty,
            "lots": lots,
            "pt_spot": round(pt_spot, 2),
            "sl_price": round(exit_line, 2),
            "exit_line": exit_line,
            "mae_val": 0.0,
            "base_atr": base_atr_val,
            "entry_time": candle.get("time") or latest_dt.isoformat(),
            "total_pnl": 0.0,
            "legs": [
                {
                    "leg": trade_symbol.split(":")[-1],
                    "symbol": trade_symbol,
                    "entry": entry_order_price,
                    "exit": None,
                    "ltp": entry_order_price,
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
        """Squares off active position and resets strategy state to IDLE for next valid setup."""
        if not self.current_trade or self.trade_state != "IN_TRADE":
            return

        trade_symbol = self.current_trade["symbol"]
        sig_side = self.current_trade["signal_side"]
        order_side = self.current_trade.get("order_side", "BUY")
        exit_side = "SELL" if order_side == "BUY" else "BUY"
        qty = int(self.current_trade["quantity"])

        leg0 = self.current_trade["legs"][0]
        ep = float(leg0.get("entry") or self.current_trade.get("entry_price") or 0.0)
        cur_ltp = float(leg0.get("ltp") or (exit_spot if exit_spot else ep))
        if self.trade_instrument != "OPTION" and exit_spot and exit_spot > 0:
            cur_ltp = float(exit_spot)

        self.log_decision(
            "EXIT CONDITION MATCH",
            f"Exit Reason={reason} | {exit_side} {qty} {trade_symbol} @ ~{cur_ltp:.2f} (Signal Future={self.last_underlying_ltp:.2f})",
            level="WARNING" if "STOP" in reason else "INFO",
        )

        self.emit_order(
            symbol=trade_symbol,
            side=exit_side,
            quantity=qty,
            order_type=self.order_type,
            price=cur_ltp if self.trade_instrument != "OPTION" else 0.0,
            product=self.product,
            leg_id="EXIT_PRIMARY",
            buffer_pct=self.limit_buffer_pct,
            auto_chase=self.auto_chase,
        )

        is_mcx = "MCX" in trade_symbol.upper()
        lot_mult = symbol_mapper.get_lot_size(trade_symbol) if is_mcx else 1
        is_long_order = order_side == "BUY"
        diff = (cur_ltp - ep) if is_long_order else (ep - cur_ltp)
        pnl = round(diff * qty * lot_mult, 2) if ep > 0 else 0.0

        # Track daily loss count & exit cooldown
        if pnl < 0:
            self._daily_loss_count += 1
        if sig_side == "BUY":
            self._bars_since_long_exit = 0
        else:
            self._bars_since_short_exit = 0

        leg0["exit"] = cur_ltp
        leg0["ltp"] = cur_ltp
        leg0["pnl"] = pnl
        leg0["status"] = "CLOSED"
        self.current_trade["total_pnl"] = pnl
        self.trade_state = "IDLE"

        self.log_decision(
            "TRADE CLOSED",
            f"Cycle completed ({reason}). Est. PnL: ₹{pnl:+,.2f} | Losses today: {self._daily_loss_count}/2",
            level="SUCCESS" if pnl >= 0 else "WARNING",
        )

    def on_tick(self, tick: Dict[str, Any]):
        """Real-time tick tracking for MTM PnL and instant Profit Target execution."""
        sym = tick.get("symbol")
        ltp = tick.get("ltp")
        if not sym or not ltp or float(ltp) <= 0:
            return

        ltp_f = float(ltp)
        sig_sym = getattr(self, "signal_symbol", "")
        target_signal = sig_sym if self.trade_instrument != "SPOT" else self.symbol
        is_signal_sym = (
            (sym == target_signal)
            or (":" in sym and sym.split(":")[-1] == target_signal.split(":")[-1])
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
                is_long_order = int(leg.get("qty", 1)) > 0
                is_mcx = "MCX" in trade_sym.upper()
                lot_mult = symbol_mapper.get_lot_size(trade_sym) if is_mcx else 1
                diff = (ltp_f - ep) if is_long_order else (ep - ltp_f)
                pnl = round(diff * abs(int(leg.get("qty", 1))) * lot_mult, 2)
                leg["pnl"] = pnl
                self.current_trade["total_pnl"] = pnl

        # Sub-second Profit Target check on Signal Future LTP
        check_spot = self.last_underlying_ltp
        pt_spot = self.current_trade.get("pt_spot")
        sig_side = self.current_trade.get("signal_side")
        if pt_spot is not None and check_spot > 0:
            if (sig_side == "BUY" and check_spot >= float(pt_spot)) or (
                sig_side == "SELL" and check_spot <= float(pt_spot)
            ):
                self._execute_exit("TARGET", exit_spot=check_spot)

    def on_timer(self):
        """Second-precision EOD square-off check (15:15:00 IST)."""
        if self.trade_state == "IN_TRADE" and self.current_trade:
            now_ist = datetime.datetime.now(IST_TZ)
            tn = now_ist.hour * 10000 + now_ist.minute * 100 + now_ist.second
            if tn >= self.square_off_time_num:
                self._execute_exit("TIME", exit_spot=self.last_underlying_ltp or None)

    def on_order_update(self, update_data: Dict[str, Any]):
        """Synchronizes genuine broker fill prices or handles entry rejection cleanly."""
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
            req_side = str(ord_req.get("side") or "").upper()
            entry_side = self.current_trade.get("order_side", "BUY")

            if req_side == entry_side or not req_side:
                if avg_fill > 0:
                    leg0["entry"] = avg_fill
                    leg0["ltp"] = avg_fill
                    self.current_trade["entry_price"] = avg_fill
                    if self.trade_instrument != "ATM_OPTION":
                        self.current_trade["entry_spot"] = avg_fill
                        tl = float(self.pcp_params.get("target_long_pct", 0.027))
                        ts = float(self.pcp_params.get("target_short_pct", 0.023))
                        sig_side = self.current_trade["signal_side"]
                        self.current_trade["pt_spot"] = round(
                            avg_fill * (1.0 + tl if sig_side == "BUY" else 1.0 - ts), 2
                        )
                leg0["status"] = "FILLED"
                self.log_decision(
                    "ORDER FILLED",
                    f"Broker execution confirmed {self.current_trade['symbol']} @ ₹{avg_fill:.2f}",
                    level="SUCCESS",
                )
        elif not open_pending and self.trade_state == "IN_TRADE" and leg0.get("status") in ["PENDING", "SENT"]:
            # Entry order rejected: revert cleanly to IDLE so next valid setup isn't blocked
            rej = "; ".join(set(r.get("rejection_reason") for r in results if r.get("rejection_reason"))) or "Order rejected"
            leg0["status"] = "REJECTED"
            self.trade_state = "IDLE"
            self.log_decision("ORDER REJECTED", f"Entry order rejected: {rej}", level="ERROR")

