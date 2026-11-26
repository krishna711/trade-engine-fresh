"""
Strategy: LR v0.6 (MCX 10-Minute Linear Regression & KAMA Trend Engine)
Default Instrument: MCX:CRUDEOILM (10m chart), universal for MCX commodities (CRUDEOIL, NATURALGAS, etc.).
Implements:
- KAMA (80) ValueWave & Dual-Period Linear Regression (40 / 20) Basis with box-size quantization
- 12-day Weighted Daily MA (High/Low) + Swing High/Low (21) + ADX(15) > 25 trend strength
- Intraday Cumulative Volume Delta & Up/Down Ratio position sizing (with risk-budget & capital limit)
- Trading window 11:00:00 - 17:00:00 IST, max_lots=2, trailing exit & trend-mode reversal
"""

from __future__ import annotations

import datetime
import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from backend.core.symbol_mapper import symbol_mapper
from backend.strategy_manager.base_strategy import BaseStrategy

IST_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

DEFAULT_PARAMS: Dict[str, Any] = {
    "symbol": "MCX:CRUDEOILM",
    "timeframe": "10m",
    "kama_period": 80,
    "linear_regression_period": 40,
    "equity_risk_per_trade": 25_000,
    "swing_low_lookback": 21,
    "swing_high_lookback": 21,
    "adx_period": 15,
    "adx_threshold": 25,
    "first_trade_time": 110000,
    "last_trade_time": 170000,
    "max_lots": 2,
    "capital_limit": 7_500_000,
    "trade_average_period": 12,
    "use_delta_sizing": True,
    "round_lot_size": None,
    "box_size": None,
}


# ---------------------------------------------------------------------------
# Self-Contained Technical Indicators & Helpers
# ---------------------------------------------------------------------------

def _time_as_int(index: pd.DatetimeIndex) -> np.ndarray:
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_convert(IST_TZ)
    return (idx.hour * 10000 + idx.minute * 100 + idx.second).to_numpy(dtype=np.int64)


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def _adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
    tr: Optional[pd.Series] = None,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    p = max(1, int(period))
    tr_s = tr if tr is not None else _true_range(high, low, close)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    atr_s = tr_s.ewm(alpha=1 / p, adjust=False, min_periods=p).mean()
    plus_di = 100.0 * (plus_dm.ewm(alpha=1 / p, adjust=False, min_periods=p).mean() / atr_s.replace(0, np.nan))
    minus_di = 100.0 * (minus_dm.ewm(alpha=1 / p, adjust=False, min_periods=p).mean() / atr_s.replace(0, np.nan))
    dx = 100.0 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx_s = dx.ewm(alpha=1 / p, adjust=False, min_periods=p).mean()
    return adx_s, plus_di, minus_di


def _linear_regression(values: pd.Series, period: int) -> pd.Series:
    p = max(2, int(period))
    x = np.arange(p, dtype=float)
    x -= x.mean()
    denominator = float(np.dot(x, x))
    data = values.to_numpy(dtype=float)
    result = np.full(len(data), np.nan)
    if len(data) >= p and denominator > 0:
        windows = np.lib.stride_tricks.sliding_window_view(data, p)
        means = windows.mean(axis=1)
        slopes = ((windows - means[:, None]) @ x) / denominator * x[-1]
        result[p - 1:] = means + slopes
    return pd.Series(result, index=values.index)


def _kama(values: pd.Series, period: int) -> pd.Series:
    p = max(1, int(period))
    change = values.diff(p).abs()
    volatility = values.diff().abs().rolling(p, min_periods=p).sum()
    efficiency = (change / volatility.replace(0, np.nan)).fillna(0.0)
    smoothing = (efficiency * (2 / 3 - 2 / 27) + 2 / 27) ** 2
    result = np.full(len(values), np.nan)
    data = values.to_numpy(dtype=float)
    factors = smoothing.to_numpy(dtype=float)
    for i in range(len(data)):
        if i == 0:
            result[i] = data[i]
        elif np.isfinite(factors[i]):
            prior = result[i - 1] if np.isfinite(result[i - 1]) else data[i - 1]
            result[i] = prior + factors[i] * (data[i] - prior)
        else:
            result[i] = result[i - 1]
    return pd.Series(result, index=values.index)


def _box_size(data: pd.DataFrame, symbol: str, configured: Optional[float]) -> float | pd.Series:
    if configured is not None:
        return float(configured)
    name = (symbol or "").upper()
    for token, size in (
        ("NIFTY BANK", 10.0), ("BANKNIFTY", 10.0), ("NIFTY", 6.0),
        ("MARUTI", 3.0), ("TATASTEEL", 0.5), ("HDFCBANK", 1.0),
        ("RELIANCE", 0.5), ("TCS", 1.2),
    ):
        if token in name:
            return size
    rolling_mean = data["Close"].rolling(100, min_periods=100).mean()
    # Fallback to available rolling mean if < 100 bars so early bars don't stay NaN
    if rolling_mean.isna().all():
        rolling_mean = data["Close"].expanding(min_periods=1).mean()
    else:
        rolling_mean = rolling_mean.bfill()
    return (rolling_mean / 200.0).apply(np.ceil) / 10.0


def _parse_time_int(val: Any, fallback: int) -> int:
    if val is None:
        return fallback
    if isinstance(val, int):
        return val
    s = str(val).strip()
    if ":" in s:
        parts = [int(p) for p in s.split(":")]
        h = parts[0] if len(parts) > 0 else 0
        m = parts[1] if len(parts) > 1 else 0
        sec = parts[2] if len(parts) > 2 else 0
        return h * 10000 + m * 100 + sec
    try:
        return int(float(s))
    except Exception:
        return fallback


def resolve_parameters(symbol: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    resolved = {**DEFAULT_PARAMS, **(params or {})}
    if not resolved.get("round_lot_size"):
        lot = symbol_mapper.get_lot_size(symbol)
        if not lot or lot <= 0:
            lot = 10 if "CRUDEOILM" in symbol.upper() else (100 if "CRUDEOIL" in symbol.upper() else 1250)
        resolved["round_lot_size"] = int(lot)
    resolved["first_trade_time"] = _parse_time_int(resolved.get("first_trade_time"), 110000)
    resolved["last_trade_time"] = _parse_time_int(resolved.get("last_trade_time"), 170000)
    return resolved


# ---------------------------------------------------------------------------
# LR v0.6 Signal Generator
# ---------------------------------------------------------------------------

def generate_signals(
    df: pd.DataFrame,
    params: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Generate LR v0.6 signals and carried state columns on 10m OHLCV bars."""
    symbol = str((params or {}).get("symbol") or df.attrs.get("symbol") or "MCX:CRUDEOILM")
    p = resolve_parameters(symbol, params)
    out = df.copy()
    n = len(out)
    if n < 2:
        return out

    idx = pd.DatetimeIndex(out.index)
    if idx.tz is not None:
        idx = idx.tz_convert(IST_TZ)
        out.index = idx

    open_values = out["Open"].to_numpy(dtype=float)
    high_values = out["High"].to_numpy(dtype=float)
    low_values = out["Low"].to_numpy(dtype=float)
    close_values = out["Close"].to_numpy(dtype=float)

    dates_series = pd.Series(idx.date, index=idx)
    dates = dates_series.to_numpy()
    new_day = np.r_[True, dates[1:] != dates[:-1]].astype(bool)
    first_bar = new_day
    times = _time_as_int(idx)
    zone = (times >= p["first_trade_time"]) & (times <= p["last_trade_time"])

    tr = _true_range(out["High"], out["Low"], out["Close"])
    adx_value = _adx(out["High"], out["Low"], out["Close"], int(p["adx_period"]), tr=tr)[0]
    trend_strength = (adx_value > float(p["adx_threshold"])).fillna(False).to_numpy(dtype=bool)

    kama_raw = _kama(out["Close"], int(p["kama_period"]))
    kama_value = pd.Series(np.floor(kama_raw / 0.05 + 0.5) * 0.05, index=idx)

    reg_period = int(p["linear_regression_period"])
    half_period = int(np.ceil(reg_period / 2))
    high_reg = _linear_regression(out["High"], reg_period)
    high_half_reg = _linear_regression(out["High"], half_period)
    low_reg = _linear_regression(out["Low"], reg_period)
    low_half_reg = _linear_regression(out["Low"], half_period)

    top = pd.concat((high_reg, high_half_reg), axis=1).max(axis=1)
    bottom = pd.concat((low_reg, low_half_reg), axis=1).min(axis=1)
    box = _box_size(out, symbol, p.get("box_size"))
    basis = pd.Series((np.floor((top + bottom) / box + 0.5) * box) / 2.0, index=idx)

    basis_values = basis.to_numpy(dtype=float)
    kama_values = kama_value.to_numpy(dtype=float)
    basis_prev, kama_prev = basis.shift(1), kama_value.shift(1)

    trend_mode = np.zeros(n, dtype=bool)
    mode = False
    for i in range(n):
        if np.isfinite(basis_values[i]) and np.isfinite(kama_values[i]):
            if basis_values[i] > kama_values[i]:
                mode = True
            elif basis_values[i] < kama_values[i]:
                mode = False
        trend_mode[i] = mode

    raw_swing_high = out["High"].rolling(
        int(p["swing_high_lookback"]), min_periods=int(p["swing_high_lookback"])
    ).max()
    raw_swing_low = out["Low"].rolling(
        int(p["swing_low_lookback"]), min_periods=int(p["swing_low_lookback"])
    ).min()
    swing_high = pd.Series(np.ceil(raw_swing_high / box) * box, index=idx)
    swing_low = pd.Series(np.floor(raw_swing_low / box) * box, index=idx)
    swing_high_prev, swing_low_prev = swing_high.shift(1), swing_low.shift(1)

    can_buy = (
        pd.Series(np.r_[False, trend_mode[:-1]], index=idx)
        & (out["Low"] > basis_prev)
        & (out["Close"] > swing_high_prev)
        & (out["Open"] > 1)
        & ~pd.Series(new_day, index=idx)
    )
    can_short = (
        ~pd.Series(np.r_[False, trend_mode[:-1]], index=idx)
        & (out["High"] < kama_value)
        & (out["Close"] < swing_low_prev)
        & (out["Open"] > 1)
    )

    first_mid = (basis_prev + kama_prev) / 2.0
    long_initial_exit = pd.Series(np.floor(first_mid / box) * box, index=idx)

    trade_period = int(p["trade_average_period"])
    daily_weights = np.arange(1, trade_period + 1, dtype=float)
    half_weights = np.arange(1, int(np.ceil(trade_period / 2)) + 1, dtype=float)
    daily_highs: List[float] = []
    daily_lows: List[float] = []
    daily_values = np.full(n, np.nan)
    for i in range(n):
        if new_day[i]:
            daily_highs.append(high_values[i])
            daily_lows.append(low_values[i])
        else:
            daily_highs[-1] = max(daily_highs[-1], high_values[i])
            daily_lows[-1] = min(daily_lows[-1], low_values[i])
        if len(daily_highs) >= len(daily_weights):
            h = np.dot(daily_highs[-len(daily_weights):], daily_weights) / daily_weights.sum()
            h_half = np.dot(daily_highs[-len(half_weights):], half_weights) / half_weights.sum()
            low_wma = np.dot(daily_lows[-len(daily_weights):], daily_weights) / daily_weights.sum()
            low_half = np.dot(daily_lows[-len(half_weights):], half_weights) / half_weights.sum()
            daily_values[i] = (max(h, h_half) + min(low_wma, low_half)) / 2.0
    daily_ma = pd.Series(daily_values, index=idx)
    long_base_exit = pd.Series(
        np.where(daily_ma < first_mid, (2.0 * first_mid + daily_ma) / 3.0, first_mid),
        index=idx,
    )

    green = out["Close"] > out["Open"]
    volume = out["Volume"].fillna(0)
    delta = (
        volume.where(green, 0).groupby(dates_series).cumsum()
        - volume.where(~green, 0).groupby(dates_series).cumsum()
    )
    delta_trend = (delta > delta.shift(1)).fillna(False)
    up_volume = volume.where(green, 0).groupby(dates_series).cumsum()
    down_volume = volume.where(~green, 0).groupby(dates_series).cumsum()
    ratio = (up_volume / (up_volume + down_volume).replace(0, np.nan)).fillna(0.5)

    use_delta = bool(p["use_delta_sizing"])
    long_score = (delta_trend.astype(int) + (ratio > 0.5).astype(int)) if use_delta else 0
    short_score = ((~delta_trend).astype(int) + (ratio < 0.5).astype(int)) if use_delta else 0
    delta_mult = (0.5 + 0.25 * long_score).fillna(1.0).to_numpy() if use_delta else np.ones(n)
    short_delta_mult = (0.5 + 0.25 * short_score).fillna(1.0).to_numpy() if use_delta else np.ones(n)

    lot_sz = max(1, int(p["round_lot_size"]))
    max_lots_series = np.minimum(
        np.floor(float(p["capital_limit"]) / (lot_sz * np.maximum(1.0, close_values))),
        int(p["max_lots"]),
    )
    max_lots_values = max_lots_series
    long_base_exit_values = long_base_exit.to_numpy(dtype=float)
    long_initial_exit_values = long_initial_exit.to_numpy(dtype=float)
    can_buy_values = can_buy.fillna(False).to_numpy(dtype=bool)
    can_short_values = can_short.fillna(False).to_numpy(dtype=bool)
    swing_high_prev_values = swing_high_prev.to_numpy(dtype=float)

    names = ("Buy", "Sell", "Short", "Cover")
    events = {name: np.zeros(n, dtype=bool) for name in names}
    prices = {f"{name}Price": np.full(n, np.nan) for name in names}
    blots, slots = np.zeros(n, dtype=int), np.zeros(n, dtype=int)
    active_long = active_short = False
    long_flags, short_flags = np.zeros(n, dtype=bool), np.zeros(n, dtype=bool)
    trail = np.full(n, np.nan)
    short_state = np.zeros(n, dtype=int)
    short_hh = 0.0

    for i in range(1, n):
        short_state[i] = short_state[i - 1] if not trend_mode[i] else 0
        trail[i] = close_values[i]
        previous_close = close_values[i - 1]

        if active_long:
            if np.isfinite(long_base_exit_values[i]) and np.isfinite(trail[i - 1]):
                trail[i] = max(long_base_exit_values[i], trail[i - 1]) if open_values[i] > long_base_exit_values[i] else trail[i - 1]
            elif np.isfinite(trail[i - 1]):
                trail[i] = trail[i - 1]
            if (first_bar[i - 1] or not first_bar[i]) and np.isfinite(trail[i - 1]) and previous_close < trail[i - 1]:
                events["Sell"][i], prices["SellPrice"][i], active_long = True, open_values[i], False
            if active_long and not trend_mode[i - 1]:
                events["Sell"][i], prices["SellPrice"][i], active_long = True, open_values[i], False

        if active_short:
            if np.isfinite(kama_values[i]) and np.isfinite(trail[i - 1]):
                trail[i] = min(kama_values[i], trail[i - 1]) if open_values[i] < kama_values[i] else trail[i - 1]
            elif np.isfinite(trail[i - 1]):
                trail[i] = trail[i - 1]
            if (first_bar[i - 1] or not first_bar[i]) and np.isfinite(trail[i - 1]) and previous_close > trail[i - 1]:
                events["Cover"][i], prices["CoverPrice"][i], active_short, short_state[i] = True, open_values[i], False, 0
            if active_short and trend_mode[i - 1]:
                events["Cover"][i], prices["CoverPrice"][i], active_short, short_state[i] = True, open_values[i], False, 0

        long_entry = open_values[i]
        init_exit = long_initial_exit_values[i] if np.isfinite(long_initial_exit_values[i]) else long_entry
        long_risk = max(1.0, long_entry - init_exit)
        blots[i] = max(0, int(np.floor(float(p["equity_risk_per_trade"]) / (lot_sz * long_risk * (2 if trend_strength[i] else 1)))))
        blots[i] = min(int(max_lots_values[i]), int(np.floor(blots[i] * delta_mult[i])))
        if not active_long and zone[i] and can_buy_values[i - 1] and blots[i] > 0:
            events["Buy"][i], prices["BuyPrice"][i] = True, long_entry
            trail[i], active_long, active_short = init_exit, True, False

        short_entry = open_values[i]
        short_risk = max(1.0, short_hh - short_entry)
        slots[i] = max(0, int(np.floor(float(p["equity_risk_per_trade"]) / (lot_sz * short_risk * (2 if trend_strength[i] else 1)))))
        slots[i] = min(int(max_lots_values[i]), int(np.floor(slots[i] * short_delta_mult[i])))
        if (
            not active_short
            and zone[i]
            and short_state[i - 1] == 2
            and np.isfinite(basis_values[i - 1])
            and previous_close < basis_values[i - 1]
            and not trend_mode[i - 1]
        ):
            events["Short"][i], prices["ShortPrice"][i] = True, short_entry
            trail[i], active_long, active_short, short_state[i] = short_hh, False, True, 3
        elif (
            short_state[i - 1] == 1
            and np.isfinite(swing_high_prev_values[i - 1])
            and previous_close > swing_high_prev_values[i - 1]
            and not trend_mode[i - 1]
        ):
            short_state[i], short_hh = 2, float(swing_high_prev_values[i])
        elif short_state[i - 1] == 0 and can_short_values[i]:
            short_state[i] = 1
        if short_state[i] == 2 and np.isfinite(swing_high_prev_values[i]):
            short_hh = max(short_hh, float(swing_high_prev_values[i]))

        long_flags[i], short_flags[i] = active_long, active_short

    for key, values in events.items():
        out[key] = values
    for key, values in prices.items():
        out[key] = values
    out["Blots"], out["Slots"] = blots, slots
    out["IsLongActive"], out["IsShortActive"] = long_flags, short_flags
    out["TrailExit"], out["TrendMode"], out["ShortState"] = trail, trend_mode, short_state
    out["LinearRegBasis"], out["ValueWave"] = basis, kama_value
    out["ADX"] = adx_value
    out["TrendStrength"], out["CumulativeDelta"] = trend_strength, delta
    out.attrs["round_lot_size"], out.attrs["symbol"] = lot_sz, symbol
    return out


# ---------------------------------------------------------------------------
# Chart Payload Generator
# ---------------------------------------------------------------------------

def _parse_bar_ist(t_val: Any) -> datetime.datetime:
    if isinstance(t_val, datetime.datetime):
        return t_val if t_val.tzinfo else t_val.replace(tzinfo=IST_TZ)
    if isinstance(t_val, (int, float)):
        sec = t_val / 1000.0 if t_val > 1e11 else t_val
        return datetime.datetime.fromtimestamp(sec, tz=datetime.timezone.utc).astimezone(IST_TZ)
    s = str(t_val or "").replace("Z", "+00:00")
    try:
        dt_raw = datetime.datetime.fromisoformat(s)
        return dt_raw if dt_raw.tzinfo else dt_raw.replace(tzinfo=IST_TZ)
    except Exception:
        return datetime.datetime.now(IST_TZ)


def compute_lr_chart_payload(bars: List[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Computes OHLC candles, LinearRegBasis, KAMA ValueWave, TrailExit, ADX, CumulativeDelta & signals."""
    valid_bars = [
        b for b in (bars or [])
        if b.get("open") is not None and b.get("high") is not None and b.get("low") is not None and b.get("close") is not None
    ]
    if len(valid_bars) < 2:
        return {"candles": [], "overlays": {}, "oscillators": {}, "signals": []}

    rows, idx = [], []
    for b in valid_bars:
        idx.append(_parse_bar_ist(b.get("time")))
        rows.append({
            "Open": float(b["open"]),
            "High": float(b["high"]),
            "Low": float(b["low"]),
            "Close": float(b["close"]),
            "Volume": float(b.get("volume") or 0.0),
        })

    df = pd.DataFrame(rows, index=pd.DatetimeIndex(idx))
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.attrs["symbol"] = cfg.get("symbol", "MCX:CRUDEOILM")

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

    sig_df = generate_signals(df, cfg)

    def _clean(col: str) -> List[Optional[float]]:
        if col not in sig_df.columns:
            return []
        return [round(float(v), 2) if (v is not None and np.isfinite(v)) else None for v in sig_df[col].to_numpy(dtype=float)]

    signals = []
    for pos_i, (ts_idx, row) in enumerate(sig_df.iterrows()):
        ts_dt = ts_idx.to_pydatetime()
        ts_iso = ts_dt.isoformat()
        lbl_t = ts_dt.strftime("%d %b %Y %H:%M")
        if bool(row.get("Buy", False)):
            p_val = float(row["BuyPrice"]) if math.isfinite(row["BuyPrice"]) else float(row["Close"])
            signals.append({"index": pos_i, "time": ts_iso, "label_time": lbl_t, "type": "BUY", "price": round(p_val, 2), "label": f"BUY ({int(row['Blots'])}L)"})
        if bool(row.get("Sell", False)):
            p_val = float(row["SellPrice"]) if math.isfinite(row["SellPrice"]) else float(row["Close"])
            signals.append({"index": pos_i, "time": ts_iso, "label_time": lbl_t, "type": "SELL", "price": round(p_val, 2), "label": "SELL EXIT"})
        if bool(row.get("Short", False)):
            p_val = float(row["ShortPrice"]) if math.isfinite(row["ShortPrice"]) else float(row["Close"])
            signals.append({"index": pos_i, "time": ts_iso, "label_time": lbl_t, "type": "SHORT", "price": round(p_val, 2), "label": f"SHORT ({int(row['Slots'])}L)"})
        if bool(row.get("Cover", False)):
            p_val = float(row["CoverPrice"]) if math.isfinite(row["CoverPrice"]) else float(row["Close"])
            signals.append({"index": pos_i, "time": ts_iso, "label_time": lbl_t, "type": "COVER", "price": round(p_val, 2), "label": "COVER EXIT"})

    return {
        "candles": candles_out,
        "overlays": {
            "linear_reg_basis": _clean("LinearRegBasis"),
            "value_wave": _clean("ValueWave"),
            "trail_exit": _clean("TrailExit"),
            "trend_mode": [bool(v) for v in sig_df["TrendMode"].to_numpy()],
        },
        "oscillators": {
            "adx": _clean("ADX"),
            "adx_threshold": float(cfg.get("adx_threshold", DEFAULT_PARAMS["adx_threshold"])),
            "cumulative_delta": _clean("CumulativeDelta"),
        },
        "signals": signals,
    }


# ---------------------------------------------------------------------------
# Live / Paper Worker Container Strategy
# ---------------------------------------------------------------------------

class LR10MCXStrategy(BaseStrategy):
    """
    Live / Paper Algorithmic Execution Container for LR v0.6 on MCX (10m CRUDEOILM).
    Executes completed-bar open signals with risk-budget & delta sizing and trailing stop exits.
    """

    def on_init(self):
        raw_sym = str(self.config.get("symbol") or "MCX:CRUDEOILM").strip().upper()
        self.symbol = raw_sym if ":" in raw_sym else f"MCX:{raw_sym}"
        self.timeframe = str(self.config.get("timeframe") or "10m").strip().lower()
        if self.timeframe.isdigit():
            self.timeframe = f"{self.timeframe}m"

        self.product = str(self.config.get("product", "NRML")).upper()
        self.order_type = str(self.config.get("order_type", "LIMIT")).upper()
        self.limit_buffer_pct = float(self.config.get("limit_buffer_pct", 0.015))
        self.auto_chase = bool(self.config.get("auto_chase", True))

        self.params = resolve_parameters(self.symbol, self.config)
        self.round_lot_size = int(self.params["round_lot_size"])

        self.request_subscribe([self.symbol])

        if not hasattr(self, "last_ltp"):
            self.last_ltp = 0.0
        if not hasattr(self, "_last_bucket_seen"):
            self._last_bucket_seen: Optional[datetime.datetime] = None
            self._last_entry_bucket: Optional[datetime.datetime] = None

        self.log_decision(
            "INIT",
            f"LR v0.6 Initialized for {self.symbol} ({self.timeframe}) | "
            f"KAMA({self.params['kama_period']}) | LinReg({self.params['linear_regression_period']}) | "
            f"Swing({self.params['swing_high_lookback']}) | ADX({self.params['adx_period']})>{self.params['adx_threshold']} | "
            f"Window: {self.params['first_trade_time']}-{self.params['last_trade_time']} IST | "
            f"Risk/Trade: ₹{self.params['equity_risk_per_trade']:,} | MaxLots: {self.params['max_lots']} | LotSize: {self.round_lot_size}",
        )

    def on_config_updated(self, new_config: Dict[str, Any]):
        super().on_config_updated(new_config)
        self.on_init()

    def rehydrate_state(self, initial_state: Dict[str, Any]):
        """Recovers active open position cleanly on worker restart."""
        legs = initial_state.get("legs_info", [])
        active = [
            dict(l) for l in legs
            if l.get("status") in ["FILLED", "OPEN", "PENDING"] and l.get("exit") is None
        ]
        if initial_state.get("trade_state") == "IN_TRADE" or active:
            self.trade_state = "IN_TRADE"
            leg0 = active[0] if active else {}
            sym = str(leg0.get("symbol") or self.symbol)
            raw_qty = int(leg0.get("qty") or 1)
            side = "BUY" if raw_qty > 0 else "SELL"
            entry_p = float(leg0.get("entry") or 0.0)
            self.current_trade = {
                "symbol": sym,
                "signal_side": side,
                "entry_price": entry_p,
                "quantity": abs(raw_qty),
                "trail_exit": float(leg0.get("stop_price") or entry_p),
                "entry_time": initial_state.get("last_trade_at") or datetime.datetime.utcnow().isoformat(),
                "legs": [dict(l) for l in legs],
                "total_pnl": float(initial_state.get("unrealized_pnl", 0.0) or 0.0),
            }
            self.log_decision(
                "STATE REHYDRATED",
                f"Resumed active {side} position on {sym} ({abs(raw_qty)} lot(s) @ ₹{entry_p:.2f}).",
                level="SUCCESS",
            )

    def on_candle(self, candle: Dict[str, Any], history: List[Dict[str, Any]]):
        """Evaluates LR v0.6 rules strictly when a new 10m bar opens (completed previous bar)."""
        all_bars = (history or []) + ([candle] if candle else [])
        completed = [
            b for b in all_bars
            if b.get("open") is not None and b.get("high") is not None
            and b.get("low") is not None and b.get("close") is not None
            and math.isfinite(float(b.get("close", 0.0)))
        ]

        min_req = max(int(self.params["kama_period"]), int(self.params["linear_regression_period"])) + 5
        if len(completed) < min_req:
            self.log_decision(
                "Candle Fetch",
                f"Warming up LR v0.6 indicators: {len(completed)}/{min_req} bars for {self.symbol}.",
            )
            return

        rows, idx = [], []
        for b in completed:
            idx.append(_parse_bar_ist(b.get("time")))
            rows.append({
                "Open": float(b["open"]),
                "High": float(b["high"]),
                "Low": float(b["low"]),
                "Close": float(b["close"]),
                "Volume": float(b.get("volume") or 0.0),
            })

        df = pd.DataFrame(rows, index=pd.DatetimeIndex(idx))
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df.attrs["symbol"] = self.symbol

        latest_dt = df.index[-1].to_pydatetime()
        was_uninitialized = self._last_bucket_seen is None
        is_new_bar = was_uninitialized or latest_dt > self._last_bucket_seen
        if not is_new_bar:
            return
        self._last_bucket_seen = latest_dt

        sig_df = generate_signals(df, self.params)
        last = sig_df.iloc[-1]
        self.last_ltp = float(last["Close"])

        basis_v = float(last["LinearRegBasis"]) if math.isfinite(last["LinearRegBasis"]) else 0.0
        kama_v = float(last["ValueWave"]) if math.isfinite(last["ValueWave"]) else 0.0
        adx_v = float(last["ADX"]) if math.isfinite(last["ADX"]) else 0.0
        trail_v = float(last["TrailExit"]) if math.isfinite(last["TrailExit"]) else 0.0
        t_mode = bool(last["TrendMode"])

        self.log_decision(
            "Candle Fetch Success",
            f"{self.symbol} ({latest_dt.strftime('%H:%M')}) O={last['Open']:.2f} C={last['Close']:.2f} | "
            f"Basis={basis_v:.2f} KAMA={kama_v:.2f} ({'BULL' if t_mode else 'BEAR'}) | "
            f"ADX={adx_v:.1f} | Trail={trail_v:.2f} | Blots={int(last['Blots'])} Slots={int(last['Slots'])}",
        )

        if was_uninitialized:
            return

        # 1. Exit Check if IN_TRADE
        if self.trade_state == "IN_TRADE" and self.current_trade:
            sig_side = self.current_trade["signal_side"]
            if sig_side == "BUY" and bool(last["Sell"]):
                exit_p = float(last["SellPrice"]) if math.isfinite(last["SellPrice"]) else float(last["Open"])
                self._execute_exit("LR_LONG_EXIT", exit_price=exit_p)
                return
            if sig_side == "SELL" and bool(last["Cover"]):
                exit_p = float(last["CoverPrice"]) if math.isfinite(last["CoverPrice"]) else float(last["Open"])
                self._execute_exit("LR_SHORT_EXIT", exit_price=exit_p)
                return
            if math.isfinite(trail_v):
                self.current_trade["trail_exit"] = round(trail_v, 2)
            return

        # 2. Entry Check if IDLE
        if self.trade_state != "IDLE" or self._last_entry_bucket == latest_dt:
            return

        if bool(last["Buy"]) and int(last["Blots"]) > 0:
            lots = int(last["Blots"])
            entry_p = float(last["BuyPrice"]) if math.isfinite(last["BuyPrice"]) else float(last["Open"])
            self._enter_trade("BUY", lots, entry_p, trail_v, latest_dt)
        elif bool(last["Short"]) and int(last["Slots"]) > 0:
            lots = int(last["Slots"])
            entry_p = float(last["ShortPrice"]) if math.isfinite(last["ShortPrice"]) else float(last["Open"])
            self._enter_trade("SELL", lots, entry_p, trail_v, latest_dt)

    def _enter_trade(self, side: str, lots: int, entry_p: float, trail_v: float, bar_dt: datetime.datetime):
        ref_price = self.last_ltp if self.last_ltp > 0 else entry_p
        self.log_decision(
            "ENTRY CONDITION MATCH",
            f"LR v0.6 {side} Signal | {self.symbol} | Lots={lots} (LotSize={self.round_lot_size}) | "
            f"RefPrice=₹{ref_price:.2f} | TrailExit=₹{trail_v:.2f}",
            level="SUCCESS",
        )
        self.emit_order(
            symbol=self.symbol,
            side=side,
            quantity=lots,
            order_type=self.order_type,
            price=ref_price,
            product=self.product,
            leg_id="LEG_PRIMARY",
            buffer_pct=self.limit_buffer_pct,
            auto_chase=self.auto_chase,
        )
        self.log_decision("ORDER SENT TO BROKER", f"{side} {lots} lot(s) {self.symbol} @ ~₹{ref_price:.2f}")

        leg_qty = lots if side == "BUY" else -lots
        self.current_trade = {
            "symbol": self.symbol,
            "signal_side": side,
            "entry_price": ref_price,
            "quantity": lots,
            "trail_exit": round(trail_v, 2),
            "entry_time": bar_dt.isoformat(),
            "total_pnl": 0.0,
            "legs": [{
                "leg": self.symbol.split(":")[-1],
                "symbol": self.symbol,
                "entry": ref_price,
                "exit": None,
                "ltp": ref_price,
                "qty": leg_qty,
                "pnl": 0.0,
                "status": "PENDING",
            }],
        }
        self.trade_state = "IN_TRADE"
        self._last_entry_bucket = bar_dt

    def _execute_exit(self, reason: str, exit_price: float):
        if not self.current_trade or self.trade_state != "IN_TRADE":
            return
        sig_side = self.current_trade["signal_side"]
        exit_side = "SELL" if sig_side == "BUY" else "BUY"
        lots = int(self.current_trade["quantity"])
        leg0 = self.current_trade["legs"][0]
        ep = float(leg0.get("entry") or self.current_trade.get("entry_price") or 0.0)
        cur_ltp = self.last_ltp if self.last_ltp > 0 else exit_price

        self.log_decision(
            "EXIT CONDITION MATCH",
            f"Reason={reason} | {exit_side} {lots} lot(s) {self.symbol} @ ~₹{cur_ltp:.2f} (Entry: ₹{ep:.2f})",
            level="INFO",
        )
        self.emit_order(
            symbol=self.symbol,
            side=exit_side,
            quantity=lots,
            order_type=self.order_type,
            price=cur_ltp,
            product=self.product,
            leg_id="EXIT_PRIMARY",
            buffer_pct=self.limit_buffer_pct,
            auto_chase=self.auto_chase,
        )

        is_long = sig_side == "BUY"
        diff = (cur_ltp - ep) if is_long else (ep - cur_ltp)
        pnl = round(diff * lots * self.round_lot_size, 2) if ep > 0 else 0.0
        leg0["exit"], leg0["ltp"], leg0["pnl"], leg0["status"] = cur_ltp, cur_ltp, pnl, "CLOSED"
        self.current_trade["total_pnl"] = pnl
        self.trade_state = "IDLE"
        self.log_decision("TRADE CLOSED", f"Cycle completed ({reason}). Est. PnL: ₹{pnl:+,.2f}", level="SUCCESS" if pnl >= 0 else "WARNING")

    def on_tick(self, tick: Dict[str, Any]):
        """Real-time tick tracking for MTM PnL."""
        sym = tick.get("symbol")
        ltp = tick.get("ltp")
        if not sym or not ltp or float(ltp) <= 0:
            return
        if sym != self.symbol and (":" not in sym or sym.split(":")[-1] != self.symbol.split(":")[-1]):
            return

        ltp_f = float(ltp)
        self.last_ltp = ltp_f
        if self.trade_state != "IN_TRADE" or not self.current_trade or not self.current_trade.get("legs"):
            return

        leg = self.current_trade["legs"][0]
        leg["ltp"] = ltp_f
        ep = float(leg.get("entry") or 0.0)
        if ep > 0 and leg.get("status") in ["FILLED", "OPEN"]:
            is_long = int(leg.get("qty", 1)) > 0
            diff = (ltp_f - ep) if is_long else (ep - ltp_f)
            pnl = round(diff * abs(int(leg.get("qty", 1))) * self.round_lot_size, 2)
            leg["pnl"] = pnl
            self.current_trade["total_pnl"] = pnl
