"""
Strategy: AC SRv2 Future (Alpha Algorithmic Trend-Following Engine)
Ported from node-system-strategies-reference/acSrv2Future.ts (v1.0, v1.1, and v1.2)
Specifically tuned for Reliance Industries (RELIANCE) Stock Futures / ATM Options.

Key Algorithmic Components:
1. Wilder's ATR with configurable length (default 28) and multiplier (default 4.4)
2. Volatility Ratio filter (vCheck = BaseATR / SMA(BaseATR, Length))
3. Dynamic Trailing Stop Lines (longStop / shortStop) & Trend Direction (+1/-1)
4. Version-gated Entry Conditions (v1.0, v1.1, v1.2):
   - Long: buyCandidate (Green bar, C > C[i-2], vCheck > 1.6, H < DayHigh[i-1], not newDay) + Low[i-1] > ShortStop[i-1]
   - Short: shortCandidate (Red bar, C < C[i-2], vCheck > 1.3, L > DayLow[i-1], not newDay) + High[i-1] < LongStop[i-1]
5. Target PT (TargetL = 48/1000 = 4.8%, TargetS = 18/1000 = 1.8%)
6. Trailing Stop exit tracking on candle and live ticks
7. Scheduled Square-off at 14:27 IST
8. Near-month Futures Auto-roll starting from last Friday before expiry
9. Multi-client parallel execution support via BaseStrategy
"""

import math
import datetime
from typing import Dict, Any, List, Optional
from backend.strategy_manager.base_strategy import BaseStrategy
from backend.core.symbol_mapper import symbol_mapper

IST_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

MONTH_CODES = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC"
}


def atr_wilder(highs: List[float], lows: List[float], closes: List[float], period: int = 28) -> List[float]:
    """Calculates Wilder's Average True Range (matching TS atrWilder)."""
    p = max(1, int(period))
    n = len(closes)
    if n == 0:
        return []
    tr = [0.0] * n
    for i in range(n):
        h = highs[i]
        l = lows[i]
        prev_c = closes[i - 1] if i > 0 else closes[i]
        a = h - l if (math.isfinite(h) and math.isfinite(l)) else 0.0
        b = abs(h - prev_c) if (math.isfinite(h) and math.isfinite(prev_c)) else 0.0
        c = abs(l - prev_c) if (math.isfinite(l) and math.isfinite(prev_c)) else 0.0
        tr[i] = max(a, b, c)

    out = [0.0] * n
    out[0] = tr[0]
    for i in range(1, n):
        out[i] = (out[i - 1] * (p - 1) + tr[i]) / p
    return out


def sma(series: List[float], period: int = 28) -> List[Optional[float]]:
    """Calculates Simple Moving Average (matching TS sma)."""
    p = max(1, int(period))
    n = len(series)
    out: List[Optional[float]] = [None] * n
    if p <= 1:
        return [float(v) if (v is not None and math.isfinite(v)) else None for v in series]

    sum_val = 0.0
    valid = 0
    for i in range(n):
        v = series[i]
        if v is not None and math.isfinite(v):
            sum_val += v
            valid += 1
        if i >= p:
            old = series[i - p]
            if old is not None and math.isfinite(old):
                sum_val -= old
                valid -= 1
        if i >= p - 1 and valid == p:
            out[i] = sum_val / p
    return out


def rolling_max(series: List[float], period: int = 28) -> List[float]:
    """Calculates rolling maximum over period (matching TS rollingMax)."""
    p = max(1, int(period))
    n = len(series)
    out = [float("nan")] * n
    for i in range(n):
        m = float("-inf")
        start = max(0, i - p + 1)
        for j in range(start, i + 1):
            v = series[j]
            if v is not None and math.isfinite(v) and v > m:
                m = v
        out[i] = m if math.isfinite(m) else float("nan")
    return out


def rolling_min(series: List[float], period: int = 28) -> List[float]:
    """Calculates rolling minimum over period (matching TS rollingMin)."""
    p = max(1, int(period))
    n = len(series)
    out = [float("nan")] * n
    for i in range(n):
        m = float("inf")
        start = max(0, i - p + 1)
        for j in range(start, i + 1):
            v = series[j]
            if v is not None and math.isfinite(v) and v < m:
                m = v
        out[i] = m if math.isfinite(m) else float("nan")
    return out


def parse_time_num(val: Any, fallback: str = "09:15") -> int:
    """Parses time representation into HHMMSS integer, e.g. '09:15' -> 91500."""
    if isinstance(val, (int, float)) and math.isfinite(val):
        return int(val)
    s = str(val or "").strip()
    if not s:
        s = fallback
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
    f_parts = fallback.split(":")
    return int(f_parts[0]) * 10000 + int(f_parts[1]) * 100


def time_num_from_dt(dt: datetime.datetime) -> int:
    return dt.hour * 10000 + dt.minute * 100 + dt.second


def parse_bar_dt(t_val: Any) -> datetime.datetime:
    """Parses candle time into timezone-aware IST datetime."""
    if isinstance(t_val, datetime.datetime):
        dt = t_val
    elif isinstance(t_val, (int, float)):
        # Epoch seconds or milliseconds
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
    return dt.astimezone(IST_TZ)


def get_last_thursday(year: int, month: int) -> datetime.date:
    """Returns the last Thursday of a given month and year (NSE standard monthly expiry)."""
    if month == 12:
        next_month_first = datetime.date(year + 1, 1, 1)
    else:
        next_month_first = datetime.date(year, month + 1, 1)
    last_day = next_month_first - datetime.timedelta(days=1)
    offset = (last_day.weekday() - 3) % 7
    return last_day - datetime.timedelta(days=offset)


def atm_strike(underlying_price: float, step: float = 10.0) -> int:
    """Calculates ATM strike based on step rounding (matching TS atmStrike)."""
    if not math.isfinite(underlying_price) or underlying_price <= 0:
        return 0
    step = max(1.0, float(step))
    base = math.trunc(underlying_price / step) * step
    rem = underlying_price - base
    if rem >= step / 2.0:
        base += step
    return int(base)


def compute_entry_qty(
    mode: str,
    quantity_lots: int,
    lot_size: int,
    risk_amount: float,
    max_lots: int,
    entry_price: float,
    sl_line: Optional[float],
    forced_lots: Optional[int] = None
) -> int:
    """Computes fixed or risk-based entry quantity."""
    if forced_lots is not None and forced_lots > 0:
        return int(forced_lots) * lot_size

    fixed_qty = max(0, int(quantity_lots)) * max(1, int(lot_size))
    if str(mode).lower() != "risk":
        return fixed_qty

    if not math.isfinite(entry_price) or entry_price <= 0 or sl_line is None or not math.isfinite(sl_line):
        return fixed_qty

    risk_per_unit = max(1.0, abs(entry_price - sl_line))
    denom = lot_size * risk_per_unit
    if denom <= 0:
        return fixed_qty

    lots = math.floor(risk_amount / denom)
    lots = max(0, min(int(max_lots), lots))
    return int(lots) * int(lot_size)


class ACSRv2FutureStrategy(BaseStrategy):
    """
    AC SRv2 Future Strategy (v1.0, v1.1, v1.2) for Reliance Stock Futures & Options.
    """

    def on_init(self):
        # Symbol & Underlying
        raw_sym = str(self.config.get("symbol", "RELIANCE")).strip()
        self.symbol = f"NSE:{raw_sym}" if ":" not in raw_sym else raw_sym
        self.underlying_root = self.symbol.split(":")[-1]

        # Versioning ('1.0', '1.1', '1.2')
        ver_raw = str(self.config.get("strategy_version") or self.config.get("version") or "1.2").strip()
        self.strategy_version = ver_raw if ver_raw in ["1.0", "1.1", "1.2"] else "1.2"
        self.use_v11 = self.strategy_version in ["1.1", "1.2"]
        self.use_v12 = self.strategy_version == "1.2"

        # Timeframe
        self.timeframe_min = int(self.config.get("timeframe_min") or self.config.get("timeframe") or 3)
        self.timeframe = f"{self.timeframe_min}m"

        # Indicators Parameters
        self.atr_multiplier = float(self.config.get("atr_multiplier", self.config.get("mult", 4.4)))
        self.atr_period = int(self.config.get("atr_period", self.config.get("length", 28)))
        self.use_close_extremums = bool(self.config.get("use_close_extremums", self.config.get("use_close", True)))
        self.sensitivity = float(self.config.get("sensitivity_factor", self.config.get("sensitivity", 1.0)))
        self.use_volatility_filter = bool(self.config.get("volatility_filter", self.config.get("use_volatility_filter", False)))

        # Targets (per-mille: 48 = 0.048, 18 = 0.018)
        self.target_long = float(self.config.get("target_long_per_mille", self.config.get("target_long", 48))) / 1000.0
        self.target_short = float(self.config.get("target_short_per_mille", self.config.get("target_short", 18))) / 1000.0

        # Schedule Timings (IST)
        self.first_trade_time_num = parse_time_num(self.config.get("first_trade_time"), "09:15")
        self.last_trade_time_num = parse_time_num(self.config.get("last_trade_time"), "10:51")
        self.square_off_time_num = parse_time_num(self.config.get("square_off_time"), "14:27")

        # Trading Instrument: 'FUTURE' or 'ATM_OPTION'
        inst_raw = str(self.config.get("trade_instrument", "FUTURE")).upper()
        self.trade_instrument = "ATM_OPTION" if "OPT" in inst_raw else "FUTURE"

        # Position Sizing
        self.position_sizing_mode = str(self.config.get("position_sizing_mode", "fixed")).lower()
        self.quantity_lots = int(self.config.get("quantity_lots", self.config.get("quantity", 1)))
        self.lot_size = int(self.config.get("lot_size", 250))
        self.risk_amount = float(self.config.get("risk_amount", 20000.0))
        self.max_lots = int(self.config.get("max_lots", 8))
        self.forced_lots = self.config.get("forced_lots") or self.config.get("force_lots")
        if self.forced_lots is not None:
            self.forced_lots = int(self.forced_lots)

        # Options-specific parameters
        self.opt_strike_step = float(self.config.get("opt_strike_step", 10.0))
        self.opt_strike_mode = str(self.config.get("opt_strike_mode", "ATM")).upper()
        self.opt_strike_offset_steps = int(self.config.get("opt_strike_offset_steps", 0))
        self.opt_quantity_lots = int(self.config.get("opt_quantity_lots", self.quantity_lots))
        self.opt_lot_size = int(self.config.get("opt_lot_size", self.lot_size))

        # Futures Expiry Auto-roll
        self.fut_roll_next_month_from_last_friday = bool(
            self.config.get("fut_roll_next_month_from_last_friday", True)
        )
        self.fut_expiry_date = self.config.get("fut_expiry_date")

        # Runtime State
        self.last_underlying_ltp = 2885.0
        self.pt_hit = False
        self.last_processed_candle_time: Optional[str] = None

        self.log_decision(
            "INIT",
            f"AC SRv2 Future Initialized for {self.symbol} (v{self.strategy_version}) | "
            f"ATR: Mult {self.atr_multiplier}, Len {self.atr_period} | "
            f"Target: L {self.target_long*1000:.0f}‰ / S {self.target_short*1000:.0f}‰ | "
            f"Trading Window: {self.config.get('first_trade_time', '09:15')} - {self.config.get('last_trade_time', '10:51')} | "
            f"Square Off: {self.config.get('square_off_time', '14:27')} | Instrument: {self.trade_instrument}"
        )

    def on_config_updated(self, new_config: Dict[str, Any]):
        """Dynamically updates strategy parameters without resetting trade state."""
        super().on_config_updated(new_config)
        self.on_init()
        self.log_decision("CONFIG_UPDATE", f"AC SRv2 Future configuration reloaded: {list(new_config.keys())}", level="SUCCESS")

    def get_future_contract_symbol(self, now_dt: Optional[datetime.datetime] = None) -> str:
        """
        Determines current or next month future contract symbol with auto-roll from last Friday before expiry.
        """
        if self.fut_expiry_date:
            return f"NFO:{self.underlying_root}{self.fut_expiry_date}FUT"

        now = now_dt or datetime.datetime.now(IST_TZ)
        year = now.year
        month = now.month

        last_thu = get_last_thursday(year, month)
        # Friday preceding expiry week
        roll_friday = last_thu - datetime.timedelta(days=6)

        # If today is >= Friday before expiry week, roll to next month contract
        if self.fut_roll_next_month_from_last_friday and now.date() >= roll_friday:
            next_month = month + 1 if month < 12 else 1
            next_year = year if month < 12 else year + 1
            m_code = MONTH_CODES[next_month]
            yr_str = str(next_year)[-2:]
            return f"NFO:{self.underlying_root}{yr_str}{m_code}FUT"
        else:
            m_code = MONTH_CODES[month]
            yr_str = str(year)[-2:]
            return f"NFO:{self.underlying_root}{yr_str}{m_code}FUT"

    def get_option_contract_symbol(self, strike: int, option_type: str, now_dt: Optional[datetime.datetime] = None) -> str:
        """Determines option contract symbol for given strike and CE/PE."""
        return f"NFO:{self.underlying_root}{strike}{option_type}"

    def on_candle(self, candle: Dict[str, Any], history: List[Dict[str, Any]]):
        """Processes incoming completed OHLC bars and evaluates entry, stop loss, or target conditions."""
        all_bars = history + [candle]
        # Filter valid bars
        completed = [
            b for b in all_bars
            if b.get("open") is not None and b.get("close") is not None
            and math.isfinite(float(b.get("open", 0))) and math.isfinite(float(b.get("close", 0)))
        ]

        if len(completed) < 3:
            self.log_decision("Candle Fetch", "Waiting for at least 3 completed bars", level="INFO")
            return

        candle_time_str = str(candle.get("time", ""))
        self.last_processed_candle_time = candle_time_str

        n = len(completed)
        open_arr = [float(b["open"]) for b in completed]
        high_arr = [float(b["high"]) for b in completed]
        low_arr = [float(b["low"]) for b in completed]
        close_arr = [float(b["close"]) for b in completed]

        self.last_underlying_ltp = close_arr[-1]

        # Day tracking
        day_low = [float("nan")] * n
        day_high = [float("nan")] * n
        new_day = [False] * n
        cur_day = ""
        lo = float("inf")
        hi = float("-inf")
        for k in range(n):
            b_dt = parse_bar_dt(completed[k].get("time"))
            d_str = b_dt.strftime("%Y-%m-%d")
            is_first = (d_str != cur_day)
            if is_first:
                cur_day = d_str
                lo = float("inf")
                hi = float("-inf")
            new_day[k] = is_first
            l_val = low_arr[k]
            h_val = high_arr[k]
            if math.isfinite(l_val):
                lo = min(lo, l_val)
            if math.isfinite(h_val):
                hi = max(hi, h_val)
            day_low[k] = lo
            day_high[k] = hi

        # Wilder ATR & MA
        base_atr = atr_wilder(high_arr, low_arr, close_arr, self.atr_period)
        atr_ma = sma(base_atr, self.atr_period)

        # vCheck & atrVal
        v_check = [0.0] * n
        atr_val = [0.0] * n
        for k in range(n):
            a = base_atr[k]
            ma = atr_ma[k]
            if ma is not None and math.isfinite(ma) and ma > 0:
                v_check[k] = a / ma
                vol = (ma / a) if (self.use_volatility_filter and a > 0) else 1.0
            else:
                v_check[k] = 0.0
                vol = 1.0
            val = self.atr_multiplier * a * self.sensitivity * vol
            atr_val[k] = val if math.isfinite(val) else 0.0

        # Rolling extremes
        src_hh = close_arr if self.use_close_extremums else high_arr
        src_ll = close_arr if self.use_close_extremums else low_arr
        hh = rolling_max(src_hh, self.atr_period)
        ll = rolling_min(src_ll, self.atr_period)

        # Trailing stop lines & direction
        long_stop = [float("nan")] * n
        short_stop = [float("nan")] * n
        dir_arr = [1] * n

        for k in range(n):
            ls = hh[k] - atr_val[k]
            ss = ll[k] + atr_val[k]
            if k == 0:
                long_stop[0] = ls
                short_stop[0] = ss
                dir_arr[0] = 1
                continue
            prev_c = close_arr[k - 1]
            prev_long = long_stop[k - 1]
            prev_short = short_stop[k - 1]
            long_stop[k] = max(ls, prev_long) if prev_c > prev_long else ls
            short_stop[k] = min(ss, prev_short) if prev_c < prev_short else ss

            d = dir_arr[k - 1]
            if not new_day[k]:
                c = close_arr[k]
                max_stop = max(prev_short, prev_long)
                min_stop = min(prev_long, prev_short)
                if c > max_stop:
                    d = 1
                elif c < min_stop:
                    d = -1
            dir_arr[k] = d

        # Timings
        timeframe_delta = datetime.timedelta(minutes=self.timeframe_min)
        trading_zone = [False] * n
        square_off = [False] * n
        for k in range(n):
            b_dt = parse_bar_dt(completed[k].get("time")) + timeframe_delta
            tn = time_num_from_dt(b_dt)
            trading_zone[k] = (self.first_trade_time_num <= tn <= self.last_trade_time_num)
            square_off[k] = (tn >= self.square_off_time_num)

        # Candidates
        c2 = [close_arr[k - 2] if k >= 2 else None for k in range(n)]
        cg = [open_arr[k] is not None and close_arr[k] > open_arr[k] for k in range(n)]

        buy_candidate = [False] * n
        short_candidate = [False] * n

        for k in range(n):
            c2v = c2[k]
            if not trading_zone[k]:
                continue
            if c2v is None or not math.isfinite(c2v):
                continue

            # Buy Candidate
            if cg[k] and close_arr[k] > c2v:
                is_cand = True
                if self.use_v12:
                    if k > 0 and high_arr[k] >= day_high[k - 1]:
                        is_cand = False
                    if new_day[k]:
                        is_cand = False
                    if v_check[k] <= 1.6:
                        is_cand = False
                buy_candidate[k] = is_cand

            # Short Candidate
            if (not cg[k]) and close_arr[k] < c2v:
                is_cand = True
                if self.use_v12:
                    if k > 0 and low_arr[k] <= day_low[k - 1]:
                        is_cand = False
                    if new_day[k]:
                        is_cand = False
                    if v_check[k] <= 1.3:
                        is_cand = False
                short_candidate[k] = is_cand

        latest_i = n - 1
        self.log_decision(
            "Candle Fetch Success",
            f"Bar {latest_i} close={close_arr[-1]} | vCheck={v_check[-1]:.2f} | "
            f"Dir={'LONG' if dir_arr[-1] == 1 else 'SHORT'} | "
            f"SL={long_stop[-1]:.2f}/{short_stop[-1]:.2f} | Zone={trading_zone[-1]}"
        )

        # -------------------------------------------------------------
        # IF ALREADY IN TRADE: Check Trailing Stop, Target, Square Off
        # -------------------------------------------------------------
        if self.trade_state == "IN_TRADE" and self.current_trade:
            entry_idx = int(self.current_trade.get("entry_idx", 0))
            signal_side = self.current_trade.get("signal_side", "BUY")
            in_long = (signal_side == "BUY")

            start_check_idx = max(entry_idx + 1, 1)
            for k in range(start_check_idx, n):
                if square_off[k]:
                    self._execute_exit("TIME", exit_price=close_arr[k])
                    return

                # Reset pt_hit if not in trading zone (matching AFL logic)
                if not trading_zone[k]:
                    self.current_trade["pt_hit"] = False

                if in_long:
                    prev_exit = self.current_trade["exit_line"]
                    next_exit = max(prev_exit, long_stop[k])
                    if close_arr[k - 1] < prev_exit:
                        self._execute_exit("STOP", exit_price=close_arr[k - 1])
                        return
                    if high_arr[k] >= self.current_trade["pt_price"] and not self.current_trade.get("pt_hit", False):
                        self.current_trade["pt_hit"] = True
                        self._execute_exit("TARGET", exit_price=self.current_trade["pt_price"])
                        return
                    self.current_trade["exit_line"] = next_exit
                else:
                    prev_exit = self.current_trade["exit_line"]
                    next_exit = min(prev_exit, short_stop[k])
                    if close_arr[k - 1] > prev_exit:
                        self._execute_exit("STOP", exit_price=close_arr[k - 1])
                        return
                    if low_arr[k] <= self.current_trade["pt_price"] and not self.current_trade.get("pt_hit", False):
                        self.current_trade["pt_hit"] = True
                        self._execute_exit("TARGET", exit_price=self.current_trade["pt_price"])
                        return
                    self.current_trade["exit_line"] = next_exit
            return

        # -------------------------------------------------------------
        # IF IDLE: Evaluate Entry Trigger Conditions
        # -------------------------------------------------------------
        i = latest_i
        can_enter = trading_zone[i] and not square_off[i] and i >= 2
        if not can_enter:
            return

        # Entry logic gated by strategy version
        if self.use_v11:
            enter_long = (
                buy_candidate[i - 1]
                and dir_arr[i - 2] == 1
                and dir_arr[i - 1] == 1
                and low_arr[i - 1] > short_stop[i - 1]
            )
            enter_short = (
                short_candidate[i - 1]
                and dir_arr[i - 2] == -1
                and dir_arr[i - 1] == -1
                and high_arr[i - 1] < long_stop[i - 1]
            )
        else:
            enter_long = (
                buy_candidate[i - 1]
                and dir_arr[i - 2] == 1
                and dir_arr[i - 1] == 1
                and high_arr[i] >= high_arr[i - 1]
            )
            is_new_day_low = abs(low_arr[i] - day_low[i]) < 1e-6
            enter_short = (
                short_candidate[i - 1]
                and dir_arr[i - 2] == -1
                and dir_arr[i - 1] == -1
                and low_arr[i] <= day_low[i]
                and is_new_day_low
            )

        if not enter_long and not enter_short:
            return

        signal_side = "BUY" if enter_long else "SELL"
        assumed_entry = close_arr[i - 1] if self.use_v11 else close_arr[i]
        sl_line = long_stop[i] if enter_long else short_stop[i]

        risk_entry_price = (day_high[i - 1] if enter_long else day_low[i - 1]) if self.use_v11 else assumed_entry

        # Sizing
        active_lots = self.opt_quantity_lots if self.trade_instrument == "ATM_OPTION" else self.quantity_lots
        active_lot_size = self.opt_lot_size if self.trade_instrument == "ATM_OPTION" else self.lot_size
        qty = compute_entry_qty(
            mode=self.position_sizing_mode,
            quantity_lots=active_lots,
            lot_size=active_lot_size,
            risk_amount=self.risk_amount,
            max_lots=self.max_lots,
            entry_price=risk_entry_price,
            sl_line=sl_line,
            forced_lots=self.forced_lots
        )

        if qty <= 0:
            self.log_decision("NO_ENTRY", "Computed quantity is zero", level="INFO")
            return

        # Build trade instrument order details
        b_dt = parse_bar_dt(completed[i].get("time"))
        if self.trade_instrument == "ATM_OPTION":
            option_type = "CE" if enter_long else "PE"
            strike = atm_strike(assumed_entry, self.opt_strike_step)
            trade_symbol = self.get_option_contract_symbol(strike, option_type, b_dt)
            order_side = "BUY"
            self.request_subscribe([trade_symbol])
        else:
            trade_symbol = self.get_future_contract_symbol(b_dt)
            order_side = signal_side

        # Target PT Calculation
        if enter_long:
            pt_price = assumed_entry * (1.0 + self.target_long)
        else:
            pt_price = assumed_entry * (1.0 - self.target_short)

        self.log_decision(
            "ENTRY CONDITION MATCH",
            f"Signal: {signal_side} | Symbol: {trade_symbol} | Sizing: {qty} qty | "
            f"Assumed Entry: {assumed_entry:.2f} | SL: {sl_line:.2f} | Target PT: {pt_price:.2f}"
        )

        # Dispatch order via BaseStrategy
        self.emit_order(
            symbol=trade_symbol,
            side=order_side,
            quantity=qty,
            order_type="LIMIT",
            price=assumed_entry,
            buffer_pct=0.015,
            auto_chase=True
        )

        self.log_decision(
            "ORDER SENT TO BROKER",
            f"{order_side} {qty} {trade_symbol} @ ~{assumed_entry:.2f} across {len(self.client_ids)} account(s)"
        )

        # Initialize current trade record
        leg_qty = qty if order_side == "BUY" else -qty
        self.current_trade = {
            "symbol": trade_symbol,
            "underlying": self.symbol,
            "trade_instrument": self.trade_instrument,
            "signal_side": signal_side,
            "entry_price": assumed_entry,
            "entry_spot": assumed_entry,
            "entry_idx": i,
            "entry_time": candle.get("time"),
            "quantity": qty,
            "pt_price": round(pt_price, 2),
            "sl_price": round(sl_line, 2),
            "exit_line": sl_line,
            "pt_hit": False,
            "total_pnl": 0.0,
            "legs": [
                {
                    "leg": trade_symbol.split(":")[-1],
                    "symbol": trade_symbol,
                    "entry": assumed_entry,
                    "exit": None,
                    "ltp": assumed_entry,
                    "qty": leg_qty,
                    "pnl": 0.0,
                    "status": "OPEN"
                }
            ]
        }
        self.trade_state = "IN_TRADE"

    def _execute_exit(self, reason: str, exit_price: Optional[float] = None):
        """Squares off open position and records decision audit step."""
        if not self.current_trade or self.trade_state not in ["IN_TRADE", "ENTRY_SENT"]:
            return

        trade_symbol = self.current_trade["symbol"]
        signal_side = self.current_trade["signal_side"]
        qty = self.current_trade["quantity"]
        ep = float(self.current_trade["entry_price"])

        # Determine exit order side
        if self.trade_instrument == "ATM_OPTION":
            exit_side = "SELL"
        else:
            exit_side = "SELL" if signal_side == "BUY" else "BUY"

        p = exit_price if (exit_price is not None and exit_price > 0) else self.current_trade["legs"][0].get("ltp", ep)

        self.log_decision(
            "EXIT CONDITION MATCH",
            f"Exit Triggered: Reason={reason} | Symbol={trade_symbol} | Trigger Price={p:.2f}"
        )

        self.emit_order(
            symbol=trade_symbol,
            side=exit_side,
            quantity=qty,
            order_type="LIMIT",
            price=p,
            buffer_pct=0.015,
            auto_chase=True
        )

        self.log_decision(
            "ORDER SENT TO BROKER",
            f"Exit {exit_side} {qty} {trade_symbol} @ {p:.2f} across {len(self.client_ids)} account(s)"
        )

        # Compute finalized PnL
        mult = 1 if signal_side == "BUY" else -1
        if self.trade_instrument == "ATM_OPTION":
            mult = 1
        pnl = round((p - ep) * qty * mult, 2)

        self.current_trade["legs"][0]["exit"] = p
        self.current_trade["legs"][0]["ltp"] = p
        self.current_trade["legs"][0]["pnl"] = pnl
        self.current_trade["legs"][0]["status"] = "CLOSED"
        self.current_trade["total_pnl"] = pnl
        self.trade_state = "CLOSED"

        self.log_decision(
            "TRADE CLOSED",
            f"Strategy cycle finished. Realized PnL: ₹{pnl:.2f} (Reason: {reason})",
            level="SUCCESS" if pnl >= 0 else "WARNING"
        )

    def on_tick(self, tick: Dict[str, Any]):
        """Evaluates real-time price changes for sub-millisecond stop loss, target, and PnL updates."""
        sym = tick.get("symbol")
        ltp = tick.get("ltp")
        if not sym or not ltp or float(ltp) <= 0:
            return

        ltp_f = float(ltp)
        if sym == self.symbol or "RELIANCE" in sym:
            self.last_underlying_ltp = ltp_f

        if self.trade_state == "IN_TRADE" and self.current_trade:
            trade_sym = self.current_trade.get("symbol")
            is_trade_sym = (sym == trade_sym or (":" in sym and sym.split(":")[-1] == str(trade_sym).split(":")[-1]))

            # Check underlying spot or contract price
            check_p = ltp_f if is_trade_sym else self.last_underlying_ltp

            # Update live leg LTP and unrealized PnL
            if is_trade_sym:
                leg = self.current_trade["legs"][0]
                leg["ltp"] = ltp_f
                ep = float(leg.get("entry") or ltp_f)
                sig = self.current_trade["signal_side"]
                mult = 1 if sig == "BUY" else -1
                if self.trade_instrument == "ATM_OPTION":
                    mult = 1
                leg_pnl = round((ltp_f - ep) * self.current_trade["quantity"] * mult, 2)
                leg["pnl"] = leg_pnl
                self.current_trade["total_pnl"] = leg_pnl

            # Live Trailing Stop Loss & Target Check
            signal_side = self.current_trade["signal_side"]
            exit_line = self.current_trade.get("exit_line")
            pt_price = self.current_trade.get("pt_price")
            pt_hit = self.current_trade.get("pt_hit", False)

            if signal_side == "BUY":
                if exit_line is not None and check_p <= exit_line:
                    self._execute_exit("STOP", exit_price=ltp_f)
                elif pt_price is not None and check_p >= pt_price and not pt_hit:
                    self.current_trade["pt_hit"] = True
                    self._execute_exit("TARGET", exit_price=ltp_f)
            elif signal_side == "SELL":
                if exit_line is not None and check_p >= exit_line:
                    self._execute_exit("STOP", exit_price=ltp_f)
                elif pt_price is not None and check_p <= pt_price and not pt_hit:
                    self.current_trade["pt_hit"] = True
                    self._execute_exit("TARGET", exit_price=ltp_f)

    def on_timer(self):
        """Called every second by worker container to enforce time-based scheduled square-off."""
        if self.trade_state == "IN_TRADE" and self.current_trade:
            now_ist = datetime.datetime.now(IST_TZ)
            tn = time_num_from_dt(now_ist)
            if tn >= self.square_off_time_num:
                self._execute_exit("TIME")

    def on_order_update(self, update_data: Dict[str, Any]):
        """Synchronizes broker fill reports back into internal trade tracking."""
        results = update_data.get("results", [])
        for res in results:
            if res.get("success") and res.get("status") == "FILLED":
                fill_p = float(res.get("price") or 0.0)
                if fill_p > 0 and self.current_trade and self.current_trade.get("legs"):
                    self.current_trade["legs"][0]["entry"] = fill_p
                    self.current_trade["entry_price"] = fill_p
                    self.log_decision(
                        "ORDER FILLED",
                        f"Broker execution filled @ ₹{fill_p:.2f}. Trailing SL & Target active.",
                        level="SUCCESS"
                    )

