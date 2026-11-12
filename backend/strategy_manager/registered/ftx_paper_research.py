"""
Strategy: FTX Paper Research Strategy (Nifty Structural Trend & Cell Alpha)
Ported from D:\\projects\\ftx-paper (v0.4.0-pipeline-research-aligned)
Authoritative Specifications: D:\\projects\\ftx-paper\\docs\\STRATEGY_RULES.md

Core Structural Alpha Concept:
- Evaluates price causally against 7 structural intraday levels:
  1. VWAP_ZONE (within 0.25% of VWAP)
  2. OR_HIGH (first 15 bars opening range high, proximity within 0.25 ATR)
  3. OR_LOW (first 15 bars opening range low, proximity within 0.25 ATR)
  4. SESSION_HIGH (top 10% percentile of running intraday session range)
  5. SESSION_LOW (bottom 10% percentile of running intraday session range)
  6. PRIOR_DAY_HIGH (previous session high, proximity within 0.25 ATR)
  7. PRIOR_DAY_LOW (previous session low, proximity within 0.25 ATR)

- Composite Cells & Normalization:
  Simultaneous location intersections with canonical normalization:
  SESSION_HIGH implies NEW_HIGH; NEW_HIGH implies OR_HIGH.
  SESSION_LOW implies NEW_LOW; NEW_LOW implies OR_LOW.

- Dual Intraday Session Regimes (IST):
  - Morning Session: Minutes 0 to 120 from open (09:15 AM to before 11:15 AM IST)
  - Afternoon Session: Minutes 240 to 300 from open (01:15 PM to before 02:15 PM IST)
  - EOD Square-Off: 15:10 IST (Force close all open positions)

- Canonical Cell Policies:
  * Morning:
    1. VWAP_ZONE + OR_LOW + PRIOR_DAY_HIGH -> SELL (Trend Continuation, TRAIL)
    2. VWAP_ZONE + OR_HIGH + PRIOR_DAY_LOW -> BUY (Trend Continuation, TRAIL)
    3. SESSION_HIGH + OR_HIGH + PRIOR_DAY_LOW -> SELL (Mean Reversion, SIGNAL)
    4. VWAP_ZONE + SESSION_LOW + OR_LOW + PRIOR_DAY_LOW -> SELL (Trend Continuation, TRAIL)
    5. VWAP_ZONE + SESSION_HIGH + PRIOR_DAY_LOW -> BUY (Trend Continuation, SIGNAL)
  * Afternoon:
    1. VWAP_ZONE + OR_LOW + PRIOR_DAY_HIGH -> SELL (Trend Continuation, TRAIL)
    2. VWAP_ZONE + PRIOR_DAY_HIGH -> SELL (Mean Reversion, TRAIL)
    3. OR_LOW + PRIOR_DAY_LOW -> BUY (Mean Reversion, TRAIL)

- Causal Admission Predicates:
  - Mean Reversion: panic_descent==False, pcr_extreme==False, climactic_selling==False.
  - Trend Continuation: panic_descent==False, pcr_extreme==False, climactic_selling==False,
                        vix_spike==False, and at least 2 confirmations from:
                        (volume_climax_prior, prior_midpoint_reclaim, volume_drying).
  - Mandatory 2-bar post-exit cooldown per cell.

- Protective Exits & Adaptive Stops:
  - Trailing Stop: Activates at +20.0 bps (0.20%), trailing distance 20.0 bps from peak price.
  - Signal Exit: Armed after +5.0 bps MFE; triggers on volume climax > 2.5x 10-bar average
                 or 2-bar consecutive adverse closes.
  - Adaptive Defensive Stop: 2.0 * ATR_bp * max(1.0, VIX / 16.0), clamped [15.0, 50.0] bps,
                            widened 1.5x on expiry days.
  - EOD Exit: 15:10 IST hard square-off.

- Sizing & Risk Management:
  - 3% risk budget of available capital per trade.
  - Max 8 lots per order; 12 lots max net directional exposure.
  - 80% margin utilization cap (175,000 INR per lot reference).
  - 5% daily loss limit & 15% peak drawdown guardrail.
"""

from __future__ import annotations

import math
import datetime
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Dict, Any, List, Optional, Tuple, Sequence, Set

from backend.strategy_manager.base_strategy import BaseStrategy
from backend.core.symbol_mapper import symbol_mapper

IST_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
SESSION_OPEN_MINUTES = 9 * 60 + 15      # 09:15 IST (555 minutes)
SESSION_EXIT_MINUTES = 15 * 60 + 10     # 15:10 IST (910 minutes)
MORNING_WINDOW = (0, 120)               # Minutes from open (09:15 to 11:15)
AFTERNOON_WINDOW = (240, 300)           # Minutes from open (13:15 to 14:15)

STOP_ATR_SCALE = 2.0
STOP_MIN_BP = 15.0
STOP_MAX_BP = 50.0
VIX_BASELINE = 16.0
EXPIRY_STOP_MULTIPLIER = 1.5
TRAIL_ACTIVATE_BP = 20.0
TRAIL_DISTANCE_BP = 20.0


# ---------------------------------------------------------------------------
# Structural Location & Cell Definitions
# ---------------------------------------------------------------------------

class Location(StrEnum):
    VWAP_ZONE = "vwap_zone"
    SESSION_HIGH = "session_high"
    SESSION_LOW = "session_low"
    NEW_HIGH = "new_high"
    NEW_LOW = "new_low"
    OR_HIGH = "or_high"
    OR_LOW = "or_low"
    PRIOR_DAY_HIGH = "prior_day_high"
    PRIOR_DAY_LOW = "prior_day_low"


class Session(StrEnum):
    MORNING = "morning"
    AFTERNOON = "afternoon"
    OUTSIDE = "outside"


class ExitMode(StrEnum):
    SIGNAL = "signal"
    TRAIL = "trail"


class TradeHypothesis(StrEnum):
    MEAN_REVERSION = "mean_reversion"
    TREND_CONTINUATION = "trend_continuation"


_ORDER = tuple(Location)
# Implied locations: session extremes imply new extremes; new extremes imply opening range extremes
_IMPLIED = {
    Location.SESSION_HIGH: Location.NEW_HIGH,
    Location.SESSION_LOW: Location.NEW_LOW,
    Location.NEW_HIGH: Location.OR_HIGH,
    Location.NEW_LOW: Location.OR_LOW,
}


@dataclass(frozen=True, slots=True)
class Cell:
    """Canonical simultaneous-location combination with implied reduction."""
    locations: frozenset[Location]

    def __init__(self, *locations: Location | str) -> None:
        values = frozenset(Location(v) if not isinstance(v, Location) else v for v in locations)
        normalized = frozenset(x for x in values if _IMPLIED.get(x) not in values)
        if not normalized:
            raise ValueError("Cell requires at least one location")
        object.__setattr__(self, "locations", normalized)

    @property
    def ordered_locations(self) -> tuple[Location, ...]:
        return tuple(loc for loc in _ORDER if loc in self.locations)

    @property
    def name(self) -> str:
        return "+".join(loc.value for loc in self.ordered_locations)

    @classmethod
    def parse(cls, value: str | Cell) -> Cell:
        return value if isinstance(value, Cell) else cls(*value.split("+"))

    def __str__(self) -> str:
        return self.name

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Cell):
            return self.locations == other.locations
        return False

    def __hash__(self) -> int:
        return hash(self.locations)


@dataclass(frozen=True, slots=True)
class CellPolicyConfig:
    cell: Cell
    direction: str          # "BUY" or "SELL"
    exit_mode: ExitMode = ExitMode.SIGNAL
    hypothesis: TradeHypothesis = TradeHypothesis.TREND_CONTINUATION


MORNING_CELL_POLICIES: tuple[CellPolicyConfig, ...] = (
    CellPolicyConfig(Cell(Location.VWAP_ZONE, Location.OR_LOW, Location.PRIOR_DAY_HIGH), "SELL", ExitMode.TRAIL, TradeHypothesis.TREND_CONTINUATION),
    CellPolicyConfig(Cell(Location.VWAP_ZONE, Location.OR_HIGH, Location.PRIOR_DAY_LOW), "BUY", ExitMode.TRAIL, TradeHypothesis.TREND_CONTINUATION),
    CellPolicyConfig(Cell(Location.SESSION_HIGH, Location.OR_HIGH, Location.PRIOR_DAY_LOW), "SELL", ExitMode.SIGNAL, TradeHypothesis.MEAN_REVERSION),
    CellPolicyConfig(Cell(Location.VWAP_ZONE, Location.SESSION_LOW, Location.OR_LOW, Location.PRIOR_DAY_LOW), "SELL", ExitMode.TRAIL, TradeHypothesis.TREND_CONTINUATION),
    CellPolicyConfig(Cell(Location.VWAP_ZONE, Location.SESSION_HIGH, Location.PRIOR_DAY_LOW), "BUY", ExitMode.SIGNAL, TradeHypothesis.TREND_CONTINUATION),
)

AFTERNOON_CELL_POLICIES: tuple[CellPolicyConfig, ...] = (
    CellPolicyConfig(Cell(Location.VWAP_ZONE, Location.OR_LOW, Location.PRIOR_DAY_HIGH), "SELL", ExitMode.TRAIL, TradeHypothesis.TREND_CONTINUATION),
    CellPolicyConfig(Cell(Location.VWAP_ZONE, Location.PRIOR_DAY_HIGH), "SELL", ExitMode.TRAIL, TradeHypothesis.MEAN_REVERSION),
    CellPolicyConfig(Cell(Location.OR_LOW, Location.PRIOR_DAY_LOW), "BUY", ExitMode.TRAIL, TradeHypothesis.MEAN_REVERSION),
)


# ---------------------------------------------------------------------------
# Market Bar & Location Detection
# ---------------------------------------------------------------------------

@dataclass
class Bar:
    timestamp: datetime.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def ist_minute(self) -> int:
        return self.timestamp.hour * 60 + self.timestamp.minute


@dataclass(frozen=True, slots=True)
class LocationFeatures:
    vwap: float | None
    session_high: float
    session_low: float
    atr: float
    opening_range_high: float | None
    opening_range_low: float | None
    opening_range_complete: bool
    prior_day_high: float | None
    prior_day_low: float | None


def compute_features(
    session_bars: Sequence[Bar],
    opening_range_bars: int = 15,
    atr_window: int = 20,
    prior_day_high: float | None = None,
    prior_day_low: float | None = None
) -> LocationFeatures:
    if len(session_bars) < 2:
        raise ValueError("At least two session bars required")

    volume = 0.0
    weighted_typical = 0.0
    for bar in session_bars:
        vol = max(0.0, float(bar.volume))
        typ = (bar.high + bar.low + bar.close) / 3.0
        volume += vol
        weighted_typical += typ * vol
    vwap = weighted_typical / volume if volume > 0 else None

    ranges = [bar.high - bar.low for bar in session_bars]
    recent_ranges = ranges[-atr_window:] if len(ranges) >= atr_window else ranges
    atr = max(sum(recent_ranges) / len(recent_ranges), 1e-4)

    opening = session_bars[:opening_range_bars]
    or_complete = len(session_bars) >= opening_range_bars
    or_high = max(b.high for b in opening) if or_complete else None
    or_low = min(b.low for b in opening) if or_complete else None

    return LocationFeatures(
        vwap=vwap,
        session_high=max(b.high for b in session_bars),
        session_low=min(b.low for b in session_bars),
        atr=atr,
        opening_range_high=or_high,
        opening_range_low=or_low,
        opening_range_complete=or_complete,
        prior_day_high=prior_day_high,
        prior_day_low=prior_day_low
    )


def detect_all_locations(
    features: LocationFeatures,
    current_close: float,
    vwap_zone_pct: float = 0.25
) -> tuple[Location, ...]:
    """Detects all active structural locations for current close in canonical order."""
    close, atr = current_close, features.atr
    found: list[Location] = []

    # 1. VWAP Zone (within 0.25% of VWAP)
    if features.vwap is not None and features.vwap > 0 and close > 0:
        if abs(close - features.vwap) / close * 100.0 <= vwap_zone_pct:
            found.append(Location.VWAP_ZONE)

    # 2. Session High / Low extremes
    if close > features.session_high:
        found.append(Location.NEW_HIGH)
    if close < features.session_low:
        found.append(Location.NEW_LOW)

    session_range = features.session_high - features.session_low
    percentile = (close - features.session_low) / session_range if session_range > 0 else 0.5
    if percentile <= 0.10:
        found.append(Location.SESSION_LOW)
    if percentile >= 0.90:
        found.append(Location.SESSION_HIGH)

    # 3. Opening Range High / Low (within 0.25 ATR)
    if features.opening_range_complete and features.opening_range_high is not None:
        if close >= features.opening_range_high - 0.25 * atr:
            found.append(Location.OR_HIGH)
    if features.opening_range_complete and features.opening_range_low is not None:
        if close <= features.opening_range_low + 0.25 * atr:
            found.append(Location.OR_LOW)

    # 4. Prior Day High / Low (within 0.25 ATR)
    if features.prior_day_high is not None and abs(close - features.prior_day_high) <= 0.25 * atr:
        found.append(Location.PRIOR_DAY_HIGH)
    if features.prior_day_low is not None and abs(close - features.prior_day_low) <= 0.25 * atr:
        found.append(Location.PRIOR_DAY_LOW)

    return tuple(found)


# ---------------------------------------------------------------------------
# Admission Predicates & Gates
# ---------------------------------------------------------------------------

def build_admission_features(
    prior_bars: Sequence[Bar],
    current: Bar,
    pcr: float | None = None,
    vix_open: float = 16.0,
    vix_at_event: float = 16.0
) -> dict[str, bool | None]:
    """Evaluates the 7 causal admission features from completed 1-minute bars."""
    all_bars = list(prior_bars)
    bars = all_bars[-10:] if len(all_bars) >= 10 else all_bars
    closes = [float(b.close) for b in bars]
    volumes = [float(b.volume or 0.0) for b in bars]

    # Consecutive down closes in recent window
    consecutive_down = 0
    for idx in range(len(closes) - 1, 0, -1):
        if closes[idx] < closes[idx - 1]:
            consecutive_down += 1
        else:
            break
    climactic_selling = consecutive_down >= 3 if len(closes) >= 2 else None

    # Panic descent speed (basis points per bar)
    descent_speed = None
    if len(bars) >= 2 and current.close > 0:
        recent_high = max(float(b.close) for b in bars)
        descent_speed = (recent_high - float(current.close)) / float(current.close) * 10000.0 / len(bars)
    panic_descent = descent_speed > 8.0 if descent_speed is not None else None

    # Volume drying: sum(recent 3) < sum(prior 3)
    volume_drying = None
    if len(volumes) >= 6:
        volume_drying = sum(volumes[-3:]) < sum(volumes[-6:-3])

    # Volume climax prior: any bar volume >= 2.0x 5-bar prior moving average
    ratios: list[float] = []
    first_idx = len(all_bars) - len(bars)
    for idx, vol in enumerate(volumes, start=first_idx):
        baseline_vals = [float(item.volume or 0.0) for item in all_bars[max(0, idx - 5):idx]]
        baseline = sum(baseline_vals) / len(baseline_vals) if baseline_vals else 0.0
        if baseline > 0:
            ratios.append(vol / baseline)
    volume_climax_prior = max(ratios, default=0.0) >= 2.0 if ratios else None

    # Prior midpoint reclaim: current close > (prior.high + prior.low) / 2
    prior_midpoint_reclaim = None
    if bars:
        prior = bars[-1]
        prior_midpoint_reclaim = float(current.close) > (float(prior.high) + float(prior.low)) / 2.0

    # PCR extreme: pcr > 1.2
    pcr_extreme = pcr > 1.2 if pcr is not None else False

    # VIX spike: intraday VIX rise > 5.0%
    vix_spike = (vix_at_event - vix_open) / vix_open * 100.0 > 5.0 if vix_open > 0 else False

    return {
        "climactic_selling": climactic_selling,
        "panic_descent": panic_descent,
        "volume_drying": volume_drying,
        "pcr_extreme": pcr_extreme,
        "vix_spike": vix_spike,
        "volume_climax_prior": volume_climax_prior,
        "prior_midpoint_reclaim": prior_midpoint_reclaim
    }


def admission_result(features: dict[str, bool | None], hypothesis: TradeHypothesis) -> tuple[bool, str | None]:
    """Validates trade hypothesis admission gates."""
    if hypothesis == TradeHypothesis.MEAN_REVERSION:
        requirements = {
            "panic_descent": False,
            "pcr_extreme": False,
            "climactic_selling": False
        }
    elif hypothesis == TradeHypothesis.TREND_CONTINUATION:
        requirements = {
            "panic_descent": False,
            "pcr_extreme": False,
            "climactic_selling": False,
            "vix_spike": False
        }
        confirmations = ("volume_climax_prior", "prior_midpoint_reclaim", "volume_drying")
        if sum(features.get(name) is True for name in confirmations) < 2:
            return False, "tr_confirmation_count (need >= 2 of volume_climax, midpoint_reclaim, volume_drying)"
    else:
        return True, None

    for name, expected in requirements.items():
        val = features.get(name)
        if val is None:
            return False, f"admission_rejected (missing {name})"
        if val != expected:
            return False, f"{hypothesis.value}_{name}_required_{expected}"

    return True, None


# ---------------------------------------------------------------------------
# Adaptive Stop & Trailing Exit State Machine
# ---------------------------------------------------------------------------

def adaptive_stop_bp(
    bars_before: Sequence[Bar],
    vix_at_event: float = 16.0,
    is_expiry_day: bool = False,
    current_close: float | None = None
) -> float:
    """Calculates bounded ATR/VIX stop distance in basis points."""
    if len(bars_before) < 2:
        stop = STOP_MIN_BP
    else:
        recent = tuple(bars_before[-10:])
        last_close = float(current_close if current_close is not None else bars_before[-1].close)
        if last_close <= 0:
            stop = STOP_MIN_BP
        else:
            atr_pts = sum(float(b.high - b.low) for b in recent) / len(recent)
            atr_bp = atr_pts / last_close * 10000.0
            vix_mult = max(1.0, float(vix_at_event) / VIX_BASELINE)
            stop = max(STOP_MIN_BP, min(STOP_MAX_BP, STOP_ATR_SCALE * atr_bp * vix_mult))
    return stop * EXPIRY_STOP_MULTIPLIER if is_expiry_day else stop


def stop_price(entry: float, direction: str, stop_bp: float) -> float:
    """Translates basis point stop distance into a directional stop price."""
    distance = float(entry) * float(stop_bp) / 10000.0
    if direction.upper() in {"BUY", "LONG"}:
        return float(entry) - distance
    if direction.upper() in {"SELL", "SHORT"}:
        return float(entry) + distance
    raise ValueError(f"Unsupported direction: {direction}")


@dataclass
class PositionState:
    symbol: str
    entry_price: float
    stop_price: float
    quantity: int
    direction: str          # "BUY" or "SELL"
    cell: str
    exit_mode: ExitMode
    entry_time: datetime.datetime
    stop_basis_bp: float


class ExitStateMachine:
    """
    Evaluates defensive exits:
    1. Hard Stop
    2. Trailing Stop (+20 bps activation, 20 bps distance)
    3. Signal Exit (+5 bps MFE arming; vol climax > 2.5x or 2-bar adverse close)
    4. Session EOD Square-Off (15:10 IST)
    """

    def __init__(
        self,
        trail_activate_bp: float = TRAIL_ACTIVATE_BP,
        trail_distance_bp: float = TRAIL_DISTANCE_BP,
        eod_exit_minute: int = SESSION_EXIT_MINUTES
    ) -> None:
        self.trail_activate_bp = trail_activate_bp
        self.trail_distance_bp = trail_distance_bp
        self.eod_exit_minute = eod_exit_minute
        self._closes: deque[float] = deque(maxlen=3)
        self._volumes: deque[float] = deque(maxlen=10)
        self._max_favorable_price: float | None = None
        self._trail_active = False
        self._bars_held = 0
        self._mae_bp = 0.0
        self._mfe_bp = 0.0

    def reset(self) -> None:
        self._closes.clear()
        self._volumes.clear()
        self._max_favorable_price = None
        self._trail_active = False
        self._bars_held = 0
        self._mae_bp = 0.0
        self._mfe_bp = 0.0

    def evaluate(
        self,
        pos: PositionState,
        timestamp: datetime.datetime,
        high: float,
        low: float,
        close: float,
        volume: float | None = None,
        is_candle: bool = True
    ) -> tuple[Optional[str], float]:
        """
        Returns (exit_reason, exit_price) if an exit triggers, else (None, 0.0).
        """
        ref_entry = pos.entry_price
        is_buy = pos.direction.upper() == "BUY"

        # Calculate excursion
        if is_buy:
            adverse = (ref_entry - low) / ref_entry * 10000.0
            favorable = (high - ref_entry) / ref_entry * 10000.0
        else:
            adverse = (high - ref_entry) / ref_entry * 10000.0
            favorable = (ref_entry - low) / ref_entry * 10000.0

        self._mae_bp = max(self._mae_bp, adverse)
        self._mfe_bp = max(self._mfe_bp, favorable)

        # 1. Hard Defensive Stop check
        stop_hit = (low <= pos.stop_price) if is_buy else (high >= pos.stop_price)
        if stop_hit:
            return "hard_stop", pos.stop_price

        # Update favorable extreme
        fav_price = high if is_buy else low
        if self._max_favorable_price is None:
            self._max_favorable_price = fav_price
        elif is_buy:
            self._max_favorable_price = max(self._max_favorable_price, fav_price)
        else:
            self._max_favorable_price = min(self._max_favorable_price, fav_price)

        # 2. Trailing Stop evaluation
        fav_bp = ((self._max_favorable_price - ref_entry) / ref_entry * 10000.0
                  if is_buy else (ref_entry - self._max_favorable_price) / ref_entry * 10000.0)

        if fav_bp >= self.trail_activate_bp:
            self._trail_active = True

        if self._trail_active:
            trail_dist = self._max_favorable_price * (self.trail_distance_bp / 10000.0)
            trail_level = (self._max_favorable_price - trail_dist if is_buy
                           else self._max_favorable_price + trail_dist)
            if (is_buy and low <= trail_level) or (not is_buy and high >= trail_level):
                return "trail_stop", trail_level

        # Candle-specific evaluations (Signal exit and history updates)
        if is_candle:
            prior_vols = tuple(self._volumes)
            self._closes.append(close)
            self._bars_held += 1
            if volume is not None:
                self._volumes.append(float(volume))

            # 3. Signal Exit: Armed only after +5.0 bps MFE
            if pos.exit_mode == ExitMode.SIGNAL and self._mfe_bp > 5.0:
                # Volume climax > 2.5x prior 10-bar average
                if prior_vols and volume is not None:
                    avg_vol = sum(prior_vols) / len(prior_vols)
                    if avg_vol > 0 and volume > 2.5 * avg_vol:
                        return "vol_climax", close

                # 2-bar consecutive adverse close
                if len(self._closes) >= 3:
                    c_recent = tuple(self._closes)
                    if is_buy and c_recent[-1] < c_recent[-2] < c_recent[-3]:
                        return "counter_move", close
                    elif not is_buy and c_recent[-1] > c_recent[-2] > c_recent[-3]:
                        return "counter_move", close

            # 4. EOD Exit check
            cur_min = timestamp.hour * 60 + timestamp.minute
            if cur_min >= self.eod_exit_minute:
                return "eod", close

        return None, 0.0


# ---------------------------------------------------------------------------
# Sizing & Risk Management Engine
# ---------------------------------------------------------------------------

def calculate_order_lots(
    available_capital: float,
    equity: float,
    peak_equity: float,
    entry_price: float,
    stop_distance_points: float,
    contract_lot_size: int = 65,
    risk_fraction: float = 0.03,
    max_lots: int = 8,
    margin_per_lot: float = 175000.0,
    margin_utilization_cap: float = 0.80,
    max_daily_loss: float = 0.05,
    max_drawdown_fraction: float = 0.15,
    day_baseline_capital: float = 2500000.0
) -> tuple[int, str]:
    """Calculates approved order lot size conforming to FTX research capital profile."""
    if available_capital <= 0 or equity <= 0:
        return 0, "non_positive_capital"

    # Peak drawdown limit (15%)
    if equity < peak_equity * (1.0 - max_drawdown_fraction):
        return 0, "drawdown_limit_breached"

    # Daily loss limit (5% below baseline)
    if equity < day_baseline_capital * (1.0 - max_daily_loss):
        return 0, "daily_loss_limit_breached"

    if stop_distance_points <= 0 or entry_price <= 0:
        return 0, "invalid_stop_distance"

    # Risk budget (3% of available capital)
    risk_budget = available_capital * risk_fraction
    risk_per_lot = stop_distance_points * contract_lot_size
    if risk_per_lot <= 0:
        return 0, "invalid_risk_per_lot"

    raw_lots = int(risk_budget // risk_per_lot)

    # Margin utilization cap (80%)
    margin_lots = int((available_capital * margin_utilization_cap) // margin_per_lot) if margin_per_lot > 0 else max_lots

    # Lot ceiling
    approved_lots = min(raw_lots, max_lots, margin_lots)
    if approved_lots < 1:
        return 0, "insufficient_risk_budget_for_1_lot"

    return approved_lots, "approved"


# ---------------------------------------------------------------------------
# Main Strategy Implementation
# ---------------------------------------------------------------------------

class FTXPaperResearchStrategy(BaseStrategy):
    """
    Fresh Trade Engine implementation of the FTX Paper Research Strategy.
    Runs in isolated worker process; operates on 1-minute Nifty bars.
    """

    def on_init(self):
        # Configuration parameters
        self.symbol = str(self.config.get("symbol", "NSE:NIFTY")).strip()
        self.trade_instrument = str(self.config.get("trade_instrument", "FUTURE")).upper()
        self.timeframe = str(self.config.get("timeframe", "1m")).lower()

        # Capital & Risk Profile
        self.initial_capital = float(self.config.get("initial_capital", 2500000.0))
        self.available_capital = self.initial_capital
        self.equity = self.initial_capital
        self.peak_equity = self.initial_capital
        self.day_baseline = self.initial_capital

        self.risk_per_trade = float(self.config.get("risk_per_trade", 0.03))
        self.max_lots = int(self.config.get("max_lots", 8))
        self.max_net_directional_lots = int(self.config.get("max_net_directional_lots", 12))
        self.margin_per_lot = float(self.config.get("margin_per_lot", 175000.0))
        self.contract_lot_size = int(self.config.get("lot_size", 65))
        self.max_daily_loss = float(self.config.get("max_daily_loss", 0.05))
        self.max_drawdown_fraction = float(self.config.get("max_drawdown_fraction", 0.15))

        # Level references
        self.prior_day_high = self.config.get("prior_day_high")
        self.prior_day_low = self.config.get("prior_day_low")

        # Market inputs & state
        self.vix_open = float(self.config.get("vix_open", 15.0))
        self.vix_at_event = float(self.config.get("vix_at_event", 15.0))
        self.pcr = float(self.config.get("pcr", 1.0))
        self.is_expiry_day = bool(self.config.get("is_expiry_day", False))

        # Runtime structures
        self.session_bars: list[Bar] = []
        self.current_trading_date: Optional[datetime.date] = None
        self.exit_machine = ExitStateMachine()
        self.position_state: Optional[PositionState] = None

        # Cooldown: cell -> exit bar index
        self._cell_exit_bars: dict[str, int] = {}
        self._bar_index = 0

        # Policies setup
        self._morning_policies = {p.cell: p for p in MORNING_CELL_POLICIES}
        self._afternoon_policies = {p.cell: p for p in AFTERNOON_CELL_POLICIES}

        self.log_decision(
            "INIT",
            f"FTX Paper Research Engine initialized. Symbol: {self.symbol} | "
            f"Capital: ₹{self.initial_capital:,.2f} | Risk/Trade: {self.risk_per_trade*100:.1f}% | "
            f"Max Lots: {self.max_lots} | Lot Size: {self.contract_lot_size}",
            level="SUCCESS"
        )

    def _parse_candle(self, c_dict: Dict[str, Any]) -> Bar:
        t_val = c_dict.get("time")
        if isinstance(t_val, datetime.datetime):
            dt = t_val
        elif isinstance(t_val, (int, float)):
            val_sec = t_val / 1000.0 if t_val > 1e11 else t_val
            dt = datetime.datetime.fromtimestamp(val_sec, tz=datetime.timezone.utc)
        else:
            s = str(t_val or "").replace("Z", "+00:00")
            try:
                dt = datetime.datetime.fromisoformat(s)
            except Exception:
                dt = datetime.datetime.now(datetime.timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        ist_dt = dt.astimezone(IST_TZ)

        return Bar(
            timestamp=ist_dt,
            open=float(c_dict.get("open", 0.0)),
            high=float(c_dict.get("high", 0.0)),
            low=float(c_dict.get("low", 0.0)),
            close=float(c_dict.get("close", 0.0)),
            volume=float(c_dict.get("volume", 0.0))
        )

    def on_candle(self, candle: Dict[str, Any], history: List[Dict[str, Any]]):
        """Called upon new completed 1-minute bar."""
        bar = self._parse_candle(candle)
        self._bar_index += 1

        # Check for new trading date to reset intraday state
        bar_date = bar.timestamp.date()
        if self.current_trading_date != bar_date:
            # Set prior day extremes from yesterday if not configured
            if self.session_bars:
                if self.prior_day_high is None:
                    self.prior_day_high = max(b.high for b in self.session_bars)
                if self.prior_day_low is None:
                    self.prior_day_low = min(b.low for b in self.session_bars)
            self.current_trading_date = bar_date
            self.session_bars.clear()
            self._cell_exit_bars.clear()
            self.day_baseline = self.equity
            self.log_decision(
                "NEW_DAY_SESSION",
                f"Date: {bar_date} | Prior Day High: {self.prior_day_high} | Prior Day Low: {self.prior_day_low}",
                level="INFO"
            )

        # Only accumulate bars starting from market open 09:15 IST (minute 555)
        bar_minute = bar.ist_minute
        if bar_minute < SESSION_OPEN_MINUTES:
            return

        self.session_bars.append(bar)
        if len(self.session_bars) < 2:
            return

        prior_bars = self.session_bars[:-1]

        # -------------------------------------------------------------------
        # 1. Evaluate Open Trade Exits on Completed Bar
        # -------------------------------------------------------------------
        if self.trade_state == "IN_TRADE" and self.position_state is not None:
            exit_reason, exit_price = self.exit_machine.evaluate(
                pos=self.position_state,
                timestamp=bar.timestamp,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                is_candle=True
            )
            if exit_reason:
                self._execute_exit(exit_reason, exit_price, bar.timestamp)
                return

        # -------------------------------------------------------------------
        # 2. Evaluate Session Window & Intraday Location Features
        # -------------------------------------------------------------------
        minute_from_open = bar_minute - SESSION_OPEN_MINUTES

        session_type = Session.OUTSIDE
        if MORNING_WINDOW[0] <= minute_from_open < MORNING_WINDOW[1]:
            session_type = Session.MORNING
        elif AFTERNOON_WINDOW[0] <= minute_from_open < AFTERNOON_WINDOW[1]:
            session_type = Session.AFTERNOON

        # If outside session windows or already in trade, do not look for new entries
        if session_type == Session.OUTSIDE or self.trade_state != "IDLE":
            return

        # Compute causal features from session prefix
        features = compute_features(
            session_bars=self.session_bars,
            opening_range_bars=15,
            atr_window=20,
            prior_day_high=self.prior_day_high,
            prior_day_low=self.prior_day_low
        )

        # Detect active locations on current close
        active_locs = detect_all_locations(features, bar.close)
        if not active_locs:
            return

        try:
            detected_cell = Cell(*active_locs)
        except ValueError:
            return

        # Check post-exit cooldown (2 bars per cell)
        last_exit_bar = self._cell_exit_bars.get(detected_cell.name)
        if last_exit_bar is not None and (self._bar_index - last_exit_bar) < 2:
            return

        # Find matching policy for current session
        policy_map = self._morning_policies if session_type == Session.MORNING else self._afternoon_policies
        policy = policy_map.get(detected_cell)
        if not policy:
            return

        self.log_decision(
            "CELL_DETECTED",
            f"Session: {session_type.value.upper()} | Cell: {detected_cell.name} | "
            f"Policy: {policy.direction} ({policy.hypothesis.value}, {policy.exit_mode.value}) | "
            f"Close: {bar.close:.2f} | VWAP: {features.vwap:.2f} | ATR: {features.atr:.2f}",
            level="INFO"
        )

        # -------------------------------------------------------------------
        # 3. Admission Filters Check
        # -------------------------------------------------------------------
        admission_feats = build_admission_features(
            prior_bars=prior_bars,
            current=bar,
            pcr=self.pcr,
            vix_open=self.vix_open,
            vix_at_event=self.vix_at_event
        )

        admitted, reject_reason = admission_result(admission_feats, policy.hypothesis)
        if not admitted:
            self.log_decision(
                "ADMISSION_REJECTED",
                f"Cell {detected_cell.name} failed admission gate: {reject_reason}",
                level="WARNING"
            )
            return

        self.log_decision(
            "ADMISSION_PASSED",
            f"Admission confirmed for {policy.hypothesis.value}. Running capital sizing...",
            level="SUCCESS"
        )

        # -------------------------------------------------------------------
        # 4. Sizing & Adaptive Stop Calculation
        # -------------------------------------------------------------------
        stop_basis = adaptive_stop_bp(
            bars_before=prior_bars,
            vix_at_event=self.vix_at_event,
            is_expiry_day=self.is_expiry_day,
            current_close=bar.close
        )
        calculated_stop_price = stop_price(bar.close, policy.direction, stop_basis)
        stop_dist_pts = abs(bar.close - calculated_stop_price)

        lots, sizing_reason = calculate_order_lots(
            available_capital=self.available_capital,
            equity=self.equity,
            peak_equity=self.peak_equity,
            entry_price=bar.close,
            stop_distance_points=stop_dist_pts,
            contract_lot_size=self.contract_lot_size,
            risk_fraction=self.risk_per_trade,
            max_lots=self.max_lots,
            margin_per_lot=self.margin_per_lot,
            margin_utilization_cap=0.80,
            max_daily_loss=self.max_daily_loss,
            max_drawdown_fraction=self.max_drawdown_fraction,
            day_baseline_capital=self.day_baseline
        )

        if lots < 1:
            self.log_decision(
                "SIZING_REJECTED",
                f"Capital sizing declined entry: {sizing_reason} | Stop Basis: {stop_basis:.1f} bps",
                level="WARNING"
            )
            return

        # -------------------------------------------------------------------
        # 5. Order Dispatch & Position State Initialization
        # -------------------------------------------------------------------
        qty = lots * self.contract_lot_size
        trade_sym = self._resolve_execution_symbol(bar.timestamp)

        self.position_state = PositionState(
            symbol=trade_sym,
            entry_price=bar.close,
            stop_price=calculated_stop_price,
            quantity=qty,
            direction=policy.direction,
            cell=detected_cell.name,
            exit_mode=policy.exit_mode,
            entry_time=bar.timestamp,
            stop_basis_bp=stop_basis
        )
        self.exit_machine.reset()
        self.trade_state = "IN_TRADE"

        # Emit order to supervisor for parallel routing across mapped accounts
        self.emit_order(
            symbol=trade_sym,
            side=policy.direction,
            quantity=qty,
            order_type="LIMIT",
            price=bar.close,
            buffer_pct=0.015,
            auto_chase=True
        )

        # Set current trade representation for UI
        leg_qty = qty if policy.direction == "BUY" else -qty
        self.current_trade = {
            "symbol": trade_sym,
            "underlying": self.symbol,
            "direction": policy.direction,
            "entry_price": bar.close,
            "entry_time": bar.timestamp.isoformat(),
            "quantity": qty,
            "stop_price": round(calculated_stop_price, 2),
            "stop_basis_bp": round(stop_basis, 1),
            "cell": detected_cell.name,
            "exit_mode": policy.exit_mode.value,
            "total_pnl": 0.0,
            "legs": [
                {
                    "leg": trade_sym.split(":")[-1],
                    "symbol": trade_sym,
                    "entry": bar.close,
                    "exit": None,
                    "ltp": bar.close,
                    "qty": leg_qty,
                    "pnl": 0.0,
                    "status": "OPEN"
                }
            ]
        }

        self.log_decision(
            "ENTRY_ORDER_SENT",
            f"DISPATCHED {policy.direction} {qty} {trade_sym} @ ~{bar.close:.2f} | "
            f"Stop: {calculated_stop_price:.2f} ({stop_basis:.1f} bps) | Cell: {detected_cell.name}",
            level="SUCCESS"
        )

    def on_tick(self, tick: Dict[str, Any]):
        """Evaluates ticks in real time against defensive hard stops and trailing stops."""
        ltp = float(tick.get("ltp") or 0.0)
        if ltp <= 0 or self.trade_state != "IN_TRADE" or self.position_state is None:
            return

        tick_sym = str(tick.get("symbol") or "")
        pos_sym = self.position_state.symbol
        if tick_sym and pos_sym and not (tick_sym == pos_sym or tick_sym.endswith(pos_sym.split(":")[-1])):
            # If tick is for underlying index, we can still evaluate if trading spot or futures tracking
            if tick_sym != self.symbol and not tick_sym.endswith(self.symbol.split(":")[-1]):
                return

        now_dt = datetime.datetime.now(IST_TZ)

        # Immediate tick evaluation
        exit_reason, exit_price = self.exit_machine.evaluate(
            pos=self.position_state,
            timestamp=now_dt,
            high=ltp,
            low=ltp,
            close=ltp,
            volume=None,
            is_candle=False
        )

        if exit_reason:
            self._execute_exit(exit_reason, exit_price, now_dt)
            return

        # Update current trade LTP and unrealized PnL
        if self.current_trade and self.current_trade.get("legs"):
            leg = self.current_trade["legs"][0]
            leg["ltp"] = ltp
            entry = float(leg.get("entry") or ltp)
            qty = abs(int(leg.get("qty", 1)))
            is_buy = self.position_state.direction == "BUY"
            pnl = round((ltp - entry) * qty if is_buy else (entry - ltp) * qty, 2)
            leg["pnl"] = pnl
            self.current_trade["total_pnl"] = pnl

    def on_timer(self):
        """Called every second; evaluates 15:10 EOD square-off."""
        if self.trade_state == "IN_TRADE" and self.position_state is not None:
            now_dt = datetime.datetime.now(IST_TZ)
            cur_minute = now_dt.hour * 60 + now_dt.minute
            if cur_minute >= SESSION_EXIT_MINUTES:
                self._execute_exit("eod_squareoff", self.position_state.entry_price, now_dt)

    def _execute_exit(self, reason: str, price: float, exit_time: datetime.datetime):
        """Dispatches closing order and updates position/trade states."""
        if self.position_state is None:
            return

        pos = self.position_state
        exit_side = "SELL" if pos.direction == "BUY" else "BUY"
        qty = pos.quantity

        self.log_decision(
            "EXIT_TRIGGERED",
            f"Exit Reason: {reason.upper()} @ ₹{price:.2f} | Closing {pos.direction} {qty} {pos.symbol}",
            level="WARNING" if "stop" in reason else "INFO"
        )

        # Emit exit order
        self.emit_order(
            symbol=pos.symbol,
            side=exit_side,
            quantity=qty,
            order_type="LIMIT",
            price=price,
            buffer_pct=0.015,
            auto_chase=True
        )

        # Record cooldown
        self._cell_exit_bars[pos.cell] = self._bar_index

        # Update trade record
        entry_p = pos.entry_price
        is_buy = pos.direction == "BUY"
        realized_pnl = round((price - entry_p) * qty if is_buy else (entry_p - price) * qty, 2)

        self.equity += realized_pnl
        self.peak_equity = max(self.peak_equity, self.equity)
        self.available_capital += realized_pnl

        if self.current_trade and self.current_trade.get("legs"):
            leg = self.current_trade["legs"][0]
            leg["exit"] = price
            leg["ltp"] = price
            leg["pnl"] = realized_pnl
            leg["status"] = "CLOSED"
            self.current_trade["total_pnl"] = realized_pnl
            self.current_trade["exit_time"] = exit_time.isoformat()
            self.current_trade["exit_reason"] = reason

        self.trade_state = "IDLE"
        self.position_state = None

        self.log_decision(
            "TRADE_CLOSED",
            f"Trade completed. Realized PnL: ₹{realized_pnl:+,.2f} | Strategy Equity: ₹{self.equity:,.2f}",
            level="SUCCESS" if realized_pnl >= 0 else "WARNING"
        )

    def on_order_update(self, update_data: Dict[str, Any]):
        """Syncs fill prices and updates entry reference."""
        results = update_data.get("results", [])
        for res in results:
            if res.get("status") == "FILLED":
                fill_p = float(res.get("price") or 0.0)
                if fill_p > 0 and self.position_state is not None:
                    # Update entry price with real broker fill
                    old_entry = self.position_state.entry_price
                    self.position_state = PositionState(
                        symbol=self.position_state.symbol,
                        entry_price=fill_p,
                        stop_price=self.position_state.stop_price,
                        quantity=self.position_state.quantity,
                        direction=self.position_state.direction,
                        cell=self.position_state.cell,
                        exit_mode=self.position_state.exit_mode,
                        entry_time=self.position_state.entry_time,
                        stop_basis_bp=self.position_state.stop_basis_bp
                    )
                    if self.current_trade and self.current_trade.get("legs"):
                        self.current_trade["legs"][0]["entry"] = fill_p
                    self.log_decision(
                        "BROKER_FILL_SYNC",
                        f"Execution confirmed @ ₹{fill_p:.2f} (order est. was ₹{old_entry:.2f})",
                        level="SUCCESS"
                    )

    def _resolve_execution_symbol(self, dt: datetime.datetime) -> str:
        """Resolves target execution contract (Futures or Spot)."""
        if self.trade_instrument != "FUTURE":
            return self.symbol

        # Check if symbol is already an explicit NFO contract
        if "NFO:" in self.symbol and "FUT" in self.symbol:
            return self.symbol

        # Generate near-month future symbol for NIFTY
        # Example: NFO:NIFTY24NOVFUT
        year_short = dt.strftime("%y")
        month_str = dt.strftime("%b").upper()
        near_contract = f"NFO:NIFTY{year_short}{month_str}FUT"

        # Check if mapped or fallback
        mapped = symbol_mapper.get_broker_token(near_contract, "XTS")
        if mapped:
            return near_contract
        # Fallback to canonical NFO:NIFTY or config symbol
        return f"NFO:{self.symbol.split(':')[-1]}" if ":" in self.symbol else f"NFO:{self.symbol}"

    def on_config_updated(self, new_config: Dict[str, Any]):
        super().on_config_updated(new_config)
        if "risk_per_trade" in new_config:
            self.risk_per_trade = float(new_config["risk_per_trade"])
        if "max_lots" in new_config:
            self.max_lots = int(new_config["max_lots"])
        if "lot_size" in new_config:
            self.contract_lot_size = int(new_config["lot_size"])
        if "vix_at_event" in new_config:
            self.vix_at_event = float(new_config["vix_at_event"])
        if "pcr" in new_config:
            self.pcr = float(new_config["pcr"])


def compute_ftx_chart_payload(raw_bars: List[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Computes OHLC candles, FTX structural overlays (VWAP, OR H/L, Session H/L, Prior Day H/L), and cell signals."""
    valid_bars = [
        b for b in (raw_bars or [])
        if b.get("open") is not None and b.get("high") is not None and b.get("low") is not None and b.get("close") is not None
    ]
    if not valid_bars:
        return {"candles": [], "overlays": {}, "oscillators": {}, "signals": []}

    parsed_bars: List[Bar] = []
    candles_out: List[Dict[str, Any]] = []
    for b in valid_bars:
        t_val = b.get("time")
        if isinstance(t_val, datetime.datetime):
            dt = t_val
        elif isinstance(t_val, (int, float)):
            dt = datetime.datetime.fromtimestamp(t_val / 1000.0 if t_val > 1e11 else t_val, tz=datetime.timezone.utc)
        else:
            s = str(t_val or "").replace("Z", "+00:00")
            try:
                dt = datetime.datetime.fromisoformat(s)
            except Exception:
                dt = datetime.datetime.now(datetime.timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        dt_ist = dt.astimezone(IST_TZ)

        o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
        v = float(b.get("volume") or 0.0)
        parsed_bars.append(Bar(timestamp=dt_ist, open=o, high=h, low=l, close=c, volume=v))
        candles_out.append({
            "time": dt_ist.isoformat(),
            "label": dt_ist.strftime("%H:%M"),
            "open": round(o, 2),
            "high": round(h, 2),
            "low": round(l, 2),
            "close": round(c, 2),
            "volume": round(v, 2),
        })

    n = len(parsed_bars)
    vwap_arr: List[Optional[float]] = [None] * n
    or_high_arr: List[Optional[float]] = [None] * n
    or_low_arr: List[Optional[float]] = [None] * n
    sess_high_arr: List[Optional[float]] = [None] * n
    sess_low_arr: List[Optional[float]] = [None] * n
    prior_high_arr: List[Optional[float]] = [None] * n
    prior_low_arr: List[Optional[float]] = [None] * n
    atr_arr: List[Optional[float]] = [None] * n
    signals: List[Dict[str, Any]] = []

    morning_policies = {p.cell: p for p in MORNING_CELL_POLICIES}
    afternoon_policies = {p.cell: p for p in AFTERNOON_CELL_POLICIES}
    pcr = float(cfg.get("pcr", 1.0))
    vix_open = float(cfg.get("vix_open", 15.0))
    vix_at_event = float(cfg.get("vix_at_event", 15.0))

    session_bars: List[Bar] = []
    cur_date = None
    prior_day_high: Optional[float] = None
    prior_day_low: Optional[float] = None

    for idx, bar in enumerate(parsed_bars):
        b_date = bar.timestamp.date()
        if cur_date is not None and b_date != cur_date and session_bars:
            prior_day_high = max(x.high for x in session_bars)
            prior_day_low = min(x.low for x in session_bars)
            session_bars = []
        cur_date = b_date
        prior_bars = list(session_bars)
        session_bars.append(bar)

        prior_high_arr[idx] = round(prior_day_high, 2) if prior_day_high is not None else None
        prior_low_arr[idx] = round(prior_day_low, 2) if prior_day_low is not None else None
        sess_high_arr[idx] = round(max(x.high for x in session_bars), 2)
        sess_low_arr[idx] = round(min(x.low for x in session_bars), 2)

        if len(session_bars) < 2:
            continue

        feats = compute_features(
            session_bars=session_bars,
            opening_range_bars=15,
            atr_window=20,
            prior_day_high=prior_day_high,
            prior_day_low=prior_day_low,
        )
        vwap_arr[idx] = round(feats.vwap, 2) if feats.vwap is not None else None
        or_high_arr[idx] = round(feats.opening_range_high, 2) if feats.opening_range_high is not None else None
        or_low_arr[idx] = round(feats.opening_range_low, 2) if feats.opening_range_low is not None else None
        atr_arr[idx] = round(feats.atr, 2)

        cur_min = bar.ist_minute
        session_type = Session.OUTSIDE
        if SESSION_OPEN_MINUTES + MORNING_WINDOW[0] <= cur_min < SESSION_OPEN_MINUTES + MORNING_WINDOW[1]:
            session_type = Session.MORNING
        elif SESSION_OPEN_MINUTES + AFTERNOON_WINDOW[0] <= cur_min < SESSION_OPEN_MINUTES + AFTERNOON_WINDOW[1]:
            session_type = Session.AFTERNOON

        if session_type != Session.OUTSIDE:
            locs = detect_all_locations(feats, bar.close)
            if locs:
                try:
                    cell = Cell(*locs)
                    policy_map = morning_policies if session_type == Session.MORNING else afternoon_policies
                    policy = policy_map.get(cell)
                    if policy:
                        adm_feats = build_admission_features(prior_bars, bar, pcr=pcr, vix_open=vix_open, vix_at_event=vix_at_event)
                        admitted, _ = admission_result(adm_feats, policy.hypothesis)
                        if admitted:
                            signals.append({
                                "index": idx,
                                "time": bar.timestamp.isoformat(),
                                "label_time": bar.timestamp.strftime("%H:%M"),
                                "type": policy.direction,
                                "price": round(bar.close, 2),
                                "label": f"{policy.direction} ({cell.name})",
                            })
                except ValueError:
                    pass

    return {
        "candles": candles_out,
        "overlays": {
            "vwap": vwap_arr,
            "or_high": or_high_arr,
            "or_low": or_low_arr,
            "session_high": sess_high_arr,
            "session_low": sess_low_arr,
            "prior_day_high": prior_high_arr,
            "prior_day_low": prior_low_arr,
        },
        "oscillators": {
            "atr": atr_arr,
        },
        "signals": signals,
    }
