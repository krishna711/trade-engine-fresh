import datetime
import time
import re
from typing import Dict, Any, List, Optional
from backend.strategy_manager.base_strategy import BaseStrategy
from backend.core.symbol_mapper import symbol_mapper

class TimeBasedATMSellerStrategy(BaseStrategy):
    """
    Universal Time-Based ATM Strike Seller Strategy.
    
    Default MCX Setup Rules:
    1. Entry Time: 03:45 PM (15:45:00 IST).
    2. Action: Sells ATM Strike CE & PE (Short Straddle) for configured symbol (e.g. MCX:CRUDEOILM).
    3. Works universally for any symbol (MCX:CRUDEOILM, MCX:CRUDEOIL, NSE:NIFTY, BSE:SENSEX, etc.) without code changes.
    4. Stoploss: INR 750 per lot for CrudeoilM (configurable via stop_loss_per_lot).
    5. Exit Time: 23:18 (11:18 PM IST).
    6. Parallel execution across all mapped broker accounts (XTS, Kotak Neo, Shoonya, Zerodha, Dhan).
    """

    def on_init(self):
        self.symbol = self.config.get("symbol", "MCX:CRUDEOILM")
        self.entry_time = str(self.config.get("entry_time") or self.config.get("start_time") or "15:45:00")
        self.exit_time = str(self.config.get("exit_time") or self.config.get("end_time") or "23:18:00")
        self.legs = self.config.get("legs", ["CE", "PE"])
        self.lots = int(self.config.get("lots") or self.config.get("quantity") or self.config.get("qty") or 1)
        self.stop_loss_per_lot = float(self.config.get("stop_loss_per_lot", 750.0))
        
        # Product type: MCX Options / Futures require NRML in Indian brokers due to RMS intraday blocking
        is_mcx = "MCX" in self.symbol.upper()
        default_prod = "NRML" if is_mcx else "MIS"
        self.product = str(self.config.get("product", default_prod)).upper()
        if is_mcx and self.product == "MIS":
            print(f"[TimeATM] MCX detected for {self.symbol}. Auto-switching product MIS -> NRML to satisfy broker RMS rule.")
            self.product = "NRML"
        
        # Derive strike step automatically if not provided
        self.strike_step = float(self.config.get("strike_step", self._detect_strike_step(self.symbol)))
        
        # Derive lot size automatically if not provided
        self.lot_size = int(self.config.get("lot_size", self._detect_lot_size(self.symbol)))

        # Allow immediate entry if market time is currently inside entry-exit window
        self.allow_entry_in_window = self.config.get("allow_entry_in_window", True)
        
        # Max loss threshold
        self.max_loss = self.stop_loss_per_lot * self.lots

        # Subscribed symbols set
        self.subscribed_option_symbols: List[str] = []

        # Keep track of last known underlying LTP for timer-based entries
        self.last_underlying_ltp = 0.0

        # Debounce timer for stop-loss confirmation to prevent exit on momentary bad ticks
        self._sl_breach_first_seen: Optional[float] = None

        self.log_decision(
            "INIT",
            f"TimeBased ATM Seller Initialized for {self.symbol} | "
            f"Entry: {self.entry_time} | Exit: {self.exit_time} | "
            f"Lots: {self.lots} (Size: {self.lot_size}) | SL: INR {self.max_loss:.2f} ({self.stop_loss_per_lot}/lot) | "
            f"Strike Step: {self.strike_step}"
        )

    def rehydrate_state(self, initial_state: Dict[str, Any]):
        """Recovers active open straddle trade state upon process spawn / engine restart"""
        legs = initial_state.get("legs_info", [])
        has_active = any(l.get("status") in ["FILLED", "OPEN", "PENDING"] and l.get("exit") is None for l in legs)

        if initial_state.get("trade_state") == "IN_TRADE" or has_active:
            self.trade_state = "IN_TRADE"
            atm_strike = 0
            opt_symbols = []
            clean_legs = []

            for l in legs:
                l_copy = dict(l)
                sym = str(l_copy.get("symbol") or "")
                if sym:
                    opt_symbols.append(sym)
                m = re.search(r"(\d+)", sym) or re.search(r"(\d+)", str(l_copy.get("leg") or ""))
                if m:
                    atm_strike = int(m.group(1))
                clean_legs.append(l_copy)

            self.current_trade = {
                "underlying": self.symbol,
                "atm_strike": atm_strike,
                "lots": self.lots,
                "lot_size": self.lot_size,
                "max_loss": self.max_loss,
                "entry_time": initial_state.get("last_trade_at") or datetime.datetime.utcnow().isoformat(),
                "legs": clean_legs,
                "total_pnl": float(initial_state.get("unrealized_pnl", 0.0) or 0.0)
            }

            if opt_symbols:
                self.subscribed_option_symbols = list(set(opt_symbols))
                self.request_subscribe(self.subscribed_option_symbols)

            self._sl_breach_first_seen = None
            self.log_decision(
                "STATE REHYDRATED",
                f"Resumed active straddle trade ({len(clean_legs)} legs). State: IN_TRADE. Strike: {atm_strike}. "
                f"Active tracking SL (Max Loss: -₹{self.max_loss:.2f}) and Exit Time ({self.exit_time}).",
                level="SUCCESS"
            )

    def on_config_updated(self, new_config: Dict[str, Any]):
        """Dynamically applies parameter changes (timings, lots, stop loss) while running"""
        self.config.update(new_config)
        if "symbol" in new_config:
            self.symbol = new_config["symbol"]
        if "entry_time" in new_config:
            self.entry_time = str(new_config["entry_time"])
        elif "start_time" in new_config:
            self.entry_time = str(new_config["start_time"])

        if "exit_time" in new_config:
            self.exit_time = str(new_config["exit_time"])
        elif "end_time" in new_config:
            self.exit_time = str(new_config["end_time"])

        if "lots" in new_config:
            self.lots = int(new_config["lots"])
        elif "quantity" in new_config:
            self.lots = int(new_config["quantity"])
        elif "qty" in new_config:
            self.lots = int(new_config["qty"])

        if "stop_loss_per_lot" in new_config:
            self.stop_loss_per_lot = float(new_config["stop_loss_per_lot"])
        self.max_loss = self.stop_loss_per_lot * self.lots
        if "product" in new_config:
            self.product = str(new_config["product"]).upper()
        if "MCX" in self.symbol.upper() and self.product == "MIS":
            self.product = "NRML"
        if "strike_step" in new_config:
            self.strike_step = float(new_config["strike_step"])
        if "lot_size" in new_config:
            self.lot_size = int(new_config["lot_size"])
        if "allow_entry_in_window" in new_config:
            self.allow_entry_in_window = bool(new_config["allow_entry_in_window"])

        # Re-arm strategy to IDLE if user configured entry time for a future time
        if "entry_time" in new_config or "start_time" in new_config:
            if self.trade_state in ["CLOSED", "STOPPED"]:
                self.trade_state = "IDLE"
                self.current_trade = None
                self.log_decision(
                    "CYCLE RE-ARMED",
                    f"Entry time updated to {self.entry_time}. Strategy re-armed to IDLE for next trade trigger.",
                    level="SUCCESS"
                )

        self.log_decision(
            "CONFIG_UPDATE",
            f"Live Parameters Updated | Lots: {self.lots} | SL/Lot: {self.stop_loss_per_lot} (Max Loss: ₹{self.max_loss:.2f}) | "
            f"Entry: {self.entry_time} | Exit: {self.exit_time}",
            level="SUCCESS"
        )

    def _detect_strike_step(self, symbol: str) -> float:
        sym_upper = symbol.upper()
        if "CRUDEOIL" in sym_upper:
            return 50.0
        elif "BANKNIFTY" in sym_upper or "SENSEX" in sym_upper:
            return 100.0
        elif "NIFTY" in sym_upper:
            return 50.0
        elif "NATURALGAS" in sym_upper:
            return 5.0
        elif "GOLD" in sym_upper or "SILVER" in sym_upper:
            return 100.0
        return 50.0

    def _detect_lot_size(self, symbol: str) -> int:
        return symbol_mapper.get_lot_size(symbol)


    def _get_current_ist_time(self) -> datetime.time:
        """Computes current Indian Standard Time (UTC + 05:30)"""
        now_utc = datetime.datetime.utcnow()
        now_ist = now_utc + datetime.timedelta(hours=5, minutes=30)
        return now_ist.time()

    def _parse_time_str(self, time_str: str) -> datetime.time:
        parts = [int(p) for p in time_str.split(":")]
        if len(parts) == 2:
            return datetime.time(parts[0], parts[1], 0)
        return datetime.time(parts[0], parts[1], parts[2])

    def _build_option_symbols(self, underlying_symbol: str, strike: int) -> Dict[str, str]:
        """Constructs canonical option symbols across exchanges (MCX, NSE, BSE)"""
        if ":" in underlying_symbol:
            exch, root = underlying_symbol.split(":", 1)
        else:
            exch, root = "MCX", underlying_symbol

        prefix = "MCXFO" if exch.upper() == "MCX" else ("NFO" if exch.upper() == "NSE" else "BSEFO")
        return {leg: f"{prefix}:{root}{strike}{leg}" for leg in self.legs}

    def _check_and_reset_for_next_cycle(self):
        """
        Allows strategy to automatically re-arm for future time entries or next trading day:
        If all legs of current trade are closed, and current time has passed the exit window,
        or when a new trading day begins, or if current time is before the entry_time.
        """
        if self.trade_state not in ["CLOSED", "IDLE"]:
            return

        now_utc = datetime.datetime.utcnow()
        now_ist = now_utc + datetime.timedelta(hours=5, minutes=30)
        curr_time = now_ist.time()
        curr_date = now_ist.date()
        entry_t = self._parse_time_str(self.entry_time)

        # 1. Reset if entering a new trading date
        if getattr(self, "_last_trade_date", None) and curr_date > self._last_trade_date:
            self.trade_state = "IDLE"
            self.current_trade = None
            self._last_trade_date = None
            self._sl_breach_first_seen = None
            self.log_decision("NEW SESSION ARMED", f"New trading day {curr_date}. Armed for scheduled entry at {self.entry_time}.", level="INFO")
            return

        # 2. Reset if previous trade was CLOSED and current time is before the scheduled entry_time
        # (e.g. after manual square-off or when scheduled for a later entry time today)
        if self.trade_state == "CLOSED" and curr_time < entry_t:
            self.trade_state = "IDLE"
            self.current_trade = None
            self._sl_breach_first_seen = None
            self.log_decision("CYCLE ARMED", f"Previous trade closed. Re-armed to IDLE for next scheduled entry at {self.entry_time}.", level="INFO")

    def on_candle(self, candle: Dict[str, Any], history: List[Dict[str, Any]]):
        close_p = float(candle.get("close") or self.last_underlying_ltp or 0.0)
        if close_p > 0:
            self.last_underlying_ltp = close_p
        bar_time = str(candle.get("time") or "")
        if getattr(self, "_last_candle_logged_time", None) != bar_time:
            self._last_candle_logged_time = bar_time
            self.log_decision("Candle Fetch Success", f"Underlying {self.symbol} bar close: {close_p}")

        self._check_and_reset_for_next_cycle()
        if self.trade_state == "IDLE":
            self._check_entry_and_exit(close_p)
        elif self.trade_state in ["IN_TRADE", "ENTRY_SENT"] and self.current_trade:
            self._check_exit_conditions(self.current_trade.get("total_pnl", 0.0))

    def on_tick(self, tick: Dict[str, Any]):
        sym = tick.get("symbol")
        ltp = tick.get("ltp")
        if not ltp:
            return

        # Discard synthetic / mock ticks when evaluating live positions
        if tick.get("is_real_feed") is False and self.trade_state in ["IN_TRADE", "ENTRY_SENT"]:
            return

        # Track underlying price strictly (never let option ticks pollute underlying future price)
        is_opt = sym.endswith("CE") or sym.endswith("PE")
        root = self.symbol.split(":")[-1]
        is_underlying = (
            sym == self.symbol or 
            sym == root or 
            sym.startswith(f"{self.symbol}") or 
            sym.startswith(f"MCX:{root}") or 
            sym.startswith(root)
        ) and not is_opt

        if is_underlying:
            flt_ltp = float(ltp)
            # Defensive check: Crude oil underlying must be within realistic bounds (2000 - 20000)
            if "CRUDEOIL" in self.symbol.upper() and (flt_ltp < 2000.0 or flt_ltp > 20000.0):
                return
            self.last_underlying_ltp = flt_ltp

        # If tick is for underlying index/future and we are IDLE, evaluate entry
        self._check_and_reset_for_next_cycle()
        if self.trade_state == "IDLE" and is_underlying:
            self._check_entry_and_exit(self.last_underlying_ltp)
            return

        # If we are in trade, monitor leg LTPs and evaluate SL & Exit Time
        if self.trade_state in ["IN_TRADE", "ENTRY_SENT"] and self.current_trade:
            # Check if this tick is for one of the option legs
            for leg in self.current_trade["legs"]:
                if leg["symbol"] == sym or sym.endswith(leg["symbol"].split(":")[-1]):
                    flt_ltp = float(ltp)
                    if flt_ltp <= 0.0:
                        continue

                    # Transient spike filter: If leg was already populated with a valid LTP > 0,
                    # detect abnormal single-tick price jumps (e.g. > 2.5x jump and > 30 points)
                    prev_ltp = float(leg.get("ltp") or 0.0)
                    if prev_ltp > 0.0:
                        jump_ratio = flt_ltp / prev_ltp
                        diff = abs(flt_ltp - prev_ltp)
                        if (jump_ratio > 2.5 or jump_ratio < 0.35) and diff > 30.0:
                            spike_count = leg.get("_spike_count", 0) + 1
                            leg["_spike_count"] = spike_count
                            if spike_count < 2:
                                self.log_decision(
                                    "ANOMALY TICK FILTERED",
                                    f"Ignored transient price jump on {sym}: LTP ₹{flt_ltp:.2f} vs Prev ₹{prev_ltp:.2f} (jump: {jump_ratio:.1f}x, diff: {diff:.1f} pts). Awaiting confirmation.",
                                    level="WARNING"
                                )
                                return
                    leg["_spike_count"] = 0
                    leg["ltp"] = flt_ltp

                    # Rupee PnL strictly calculated ONLY when leg has actually been filled at broker!
                    if leg.get("status") == "FILLED" and leg.get("entry") and leg["entry"] > 0:
                        # Selling: profit when LTP drops below entry price
                        is_mcx = "MCX" in self.symbol.upper()
                        # For MCX, order quantity is in lots, so rupee PnL = (entry - ltp) * lots * lot_size
                        # For NSE/BSE, order quantity is already in total units (lots * lot_size)
                        pnl_mult = self.lot_size if is_mcx else 1
                        leg["pnl"] = round((leg["entry"] - flt_ltp) * abs(leg["qty"]) * pnl_mult, 2)

            # Total PnL only sums legs that are actually filled
            total_pnl = sum(leg.get("pnl", 0.0) for leg in self.current_trade["legs"] if leg.get("status") == "FILLED")
            self.current_trade["total_pnl"] = round(total_pnl, 2)

            # Check SL and Exit Time only if at least one leg is actively filled
            if self.trade_state == "IN_TRADE":
                self._check_exit_conditions(total_pnl)

    def on_timer(self):
        """Called every second by worker container process for heartbeat and second-precision exit timing"""
        self._check_and_reset_for_next_cycle()
        if self.trade_state == "IDLE":
            self._check_entry_and_exit(self.last_underlying_ltp)
        elif self.trade_state == "IN_TRADE" and self.current_trade:
            total_pnl = self.current_trade.get("total_pnl", 0.0)
            self._check_exit_conditions(total_pnl)

    def _check_entry_and_exit(self, underlying_ltp: float):
        if self.trade_state != "IDLE":
            return
        if not underlying_ltp or underlying_ltp <= 0.0:
            return

        curr_time = self._get_current_ist_time()
        entry_t = self._parse_time_str(self.entry_time)
        exit_t = self._parse_time_str(self.exit_time)

        # Check if current time qualifies for entry
        is_entry_time = False
        if entry_t <= exit_t:
            is_entry_time = (entry_t <= curr_time < exit_t) if self.allow_entry_in_window else (curr_time.hour == entry_t.hour and curr_time.minute == entry_t.minute)
        else: # Overnight/late session spanning midnight
            is_entry_time = (curr_time >= entry_t or curr_time < exit_t) if self.allow_entry_in_window else (curr_time.hour == entry_t.hour and curr_time.minute == entry_t.minute)

        if is_entry_time:
            self._trigger_entry(underlying_ltp, curr_time)

    def _trigger_entry(self, underlying_ltp: float, curr_time: datetime.time):
        if not underlying_ltp or underlying_ltp <= 0.0:
            return
        # Sanity check for underlying price
        if "CRUDEOIL" in self.symbol.upper() and (underlying_ltp > 20000.0 or underlying_ltp < 2000.0):
            self.log_decision(
                "PRICE ANOMALY",
                f"Underlying LTP {underlying_ltp} out of bounds for {self.symbol}. Skipping entry.",
                level="WARNING"
            )
            return

        raw_strike = int(round(underlying_ltp / self.strike_step) * self.strike_step)
        root = self.symbol.split(":")[-1]
        
        # Snap strike to nearest real strike available in broker master list
        snapped = symbol_mapper.snap_to_nearest_option_strike(root, raw_strike, "CE")
        atm_strike = snapped if snapped else raw_strike

        option_symbols = self._build_option_symbols(self.symbol, atm_strike)

        # MCX order quantity rule: For MCX orders, send lot without multiplying lot size.
        # For NSE and BSE orders, total quantity is lots multiplied by lot size.
        is_mcx = "MCX" in self.symbol.upper()
        order_qty = self.lots if is_mcx else (self.lots * self.lot_size)

        self.log_decision(
            "ENTRY CONDITION MATCH",
            f"Time {curr_time.strftime('%H:%M:%S')} in window. "
            f"Underlying {self.symbol} LTP: {underlying_ltp:.2f} -> ATM Strike: {atm_strike}. "
            f"Preparing to SELL {', '.join(option_symbols.values())} ({order_qty} {'lots' if is_mcx else 'qty'}) across {len(self.client_ids)} broker account(s)."
        )

        # Request feed subscriptions for the new option strikes immediately so market ticks stream in
        self.subscribed_option_symbols = list(option_symbols.values())
        self.request_subscribe(self.subscribed_option_symbols)

        legs_data = []
        buffer_pct = float(self.config.get("limit_buffer_pct", 0.015))
        auto_chase = bool(self.config.get("auto_chase", True))

        for leg, opt_sym in option_symbols.items():
            # Emit SELL order for this ATM leg (Marketable LIMIT with buffer)
            self.emit_order(
                symbol=opt_sym,
                side="SELL",
                quantity=order_qty,
                order_type="LIMIT",
                product=self.product,
                leg_id=f"LEG_{leg}",
                buffer_pct=buffer_pct,
                auto_chase=auto_chase
            )
            self.log_decision(
                "ORDER SENT TO BROKER",
                f"Dispatched SELL {opt_sym} (Qty: {order_qty} {'lots' if is_mcx else 'shares'}) [Marketable LIMIT + buffer] to Order Manager"
            )

            # Initial state before fill: entry price is 0.0, status is SENT
            legs_data.append({
                "leg": f"{self.symbol.split(':')[-1]} {atm_strike} {leg}",
                "symbol": opt_sym,
                "entry": 0.0,
                "exit": None,
                "ltp": 0.0,
                "qty": -order_qty,
                "pnl": 0.0,
                "status": "SENT"
            })

        self.current_trade = {
            "underlying": self.symbol,
            "atm_strike": atm_strike,
            "lots": self.lots,
            "lot_size": self.lot_size,
            "max_loss": self.max_loss,
            "entry_time": datetime.datetime.utcnow().isoformat(),
            "legs": legs_data,
            "total_pnl": 0.0
        }
        self.trade_state = "ENTRY_SENT"

    def on_order_update(self, update_data: Dict[str, Any]):
        """Callback when broker fills or rejects orders"""
        ord_req = update_data.get("order_request", {})
        results = update_data.get("results", [])
        sym = ord_req.get("symbol")
        side = ord_req.get("side")

        if side == "SELL" and self.current_trade:
            # Entry fills
            tot_filled_price = 0.0
            filled_count = 0
            open_count = 0
            rejected_count = 0
            rejection_reasons = []

            for r in results:
                st = str(r.get("status") or "").upper()
                if r.get("success") and st == "FILLED":
                    tot_filled_price += float(r.get("price", 0.0))
                    filled_count += 1
                elif r.get("success") and st in ["OPEN", "PENDING", "NEW"]:
                    open_count += 1
                else:
                    rejected_count += 1
                    if r.get("rejection_reason"):
                        rejection_reasons.append(r["rejection_reason"])

            if filled_count > 0:
                avg_price = round(tot_filled_price / filled_count, 2)
                for leg in self.current_trade["legs"]:
                    if leg["symbol"] == sym:
                        leg["entry"] = avg_price
                        leg["ltp"] = avg_price
                        leg["status"] = "FILLED"

                self.trade_state = "IN_TRADE"
                self.log_decision(
                    "ORDER FILLED",
                    f"Leg {sym} SELL filled @ {avg_price} across {filled_count} broker account(s)."
                )
                self.log_decision(
                    "TRADE ACTIVE",
                    f"ATM legs active. Active monitoring SL (Max Loss: -INR {self.max_loss:.2f}) and Exit Time ({self.exit_time})."
                )
            elif open_count > 0:
                for leg in self.current_trade["legs"]:
                    if leg["symbol"] == sym:
                        leg["entry"] = 0.0
                        leg["status"] = "PENDING"
                self.log_decision(
                    "ORDER PENDING",
                    f"Leg {sym} SELL order is PENDING in broker order book. Awaiting execution."
                )
                # Keep trade_state = "ENTRY_SENT" so fake PnL is NOT calculated
            else:
                rej_msg = "; ".join(set(rejection_reasons)) if rejection_reasons else "Broker/RMS rejected order"
                for leg in self.current_trade["legs"]:
                    if leg["symbol"] == sym:
                        leg["status"] = "REJECTED"
                self.trade_state = "REJECTED"
                self.log_decision(
                    "ORDER REJECTED",
                    f"CRITICAL: Order {sym} rejected by broker: {rej_msg}",
                    level="ERROR"
                )

        elif side == "BUY" and self.current_trade:
            # Exit fills
            filled_count = sum(1 for r in results if r.get("success") and str(r.get("status") or "").upper() == "FILLED")
            open_count = sum(1 for r in results if r.get("success") and str(r.get("status") or "").upper() in ["OPEN", "PENDING", "NEW"])
            if filled_count > 0:
                avg_exit_p = round(sum(float(r.get("price", 0.0)) for r in results if r.get("success") and str(r.get("status") or "").upper() == "FILLED") / filled_count, 2)
                self.log_decision("ORDER FILLED", f"Square-off BUY filled for {sym} @ ₹{avg_exit_p}.")
                for leg in self.current_trade["legs"]:
                    if leg["symbol"] == sym or sym.endswith(str(leg.get("symbol", "")).split(":")[-1]):
                        leg["exit"] = avg_exit_p
                        leg["status"] = "CLOSED"
                        is_mcx = "MCX" in self.symbol.upper()
                        pnl_mult = self.lot_size if is_mcx else 1
                        entry_p = float(leg.get("entry") or avg_exit_p)
                        leg["pnl"] = round((entry_p - avg_exit_p) * abs(leg.get("qty", 1)) * pnl_mult, 2)

                if all(leg.get("status") == "CLOSED" for leg in self.current_trade["legs"]):
                    self.trade_state = "CLOSED"
                    now_utc = datetime.datetime.utcnow()
                    now_ist = now_utc + datetime.timedelta(hours=5, minutes=30)
                    self._last_trade_date = now_ist.date()
                    tot_realized = sum(l.get("pnl", 0.0) for l in self.current_trade["legs"])
                    self.current_trade["total_pnl"] = round(tot_realized, 2)
                    self.log_decision("TRADE COMPLETED", f"Strategy cycle completed. Realized PnL: INR {tot_realized:.2f}")
            elif open_count > 0:
                self.log_decision("ORDER OPEN", f"Square-off BUY order for {sym} is OPEN on exchange. Monitoring exit fill.")
            else:
                rej_msg = "; ".join(set(r.get("rejection_reason") for r in results if r.get("rejection_reason"))) or "Broker/RMS rejected exit"
                self.log_decision(
                    "EXIT REJECTED",
                    f"CRITICAL: Square-off BUY rejected for {sym}: {rej_msg}",
                    level="ERROR"
                )

    def _check_exit_conditions(self, total_pnl: float):
        if self.trade_state not in ["IN_TRADE", "ENTRY_SENT"] or not self.current_trade:
            return

        curr_time = self._get_current_ist_time()
        exit_t = self._parse_time_str(self.exit_time)

        # 1. Check Stoploss: -INR 750 per lot with debounce verification against momentary price spikes
        if total_pnl <= -self.max_loss:
            now = time.time()
            if not self._sl_breach_first_seen:
                self._sl_breach_first_seen = now
                self.log_decision(
                    "SL WATCH",
                    f"Stop loss threshold breached (Total PnL: INR {total_pnl:.2f} <= -INR {self.max_loss:.2f}). "
                    f"Debounce verification active (requiring persistent breach >= 1.5s) to reject transient tick spikes.",
                    level="WARNING"
                )
                return
            elif (now - self._sl_breach_first_seen) < 1.5:
                # Still within verification debounce window
                return

            self.log_decision(
                "TRACKING SL/TARGET",
                f"STOP LOSS HIT! Total PnL INR {total_pnl:.2f} <= -INR {self.max_loss:.2f} (SL limit {self.stop_loss_per_lot}/lot) confirmed for >= 1.5s. "
                f"Triggering immediate square-off across all brokers!",
                level="WARNING"
            )
            self._sl_breach_first_seen = None
            self._square_off_all_legs(reason="STOP_LOSS")
            return
        else:
            # If PnL recovered above stop loss threshold, reset breach timer
            if self._sl_breach_first_seen is not None:
                self.log_decision(
                    "SL WATCH RECOVERED",
                    f"PnL recovered to INR {total_pnl:.2f} (above -INR {self.max_loss:.2f} threshold). SL watch reset.",
                    level="INFO"
                )
                self._sl_breach_first_seen = None

        # 2. Check Exit Time
        if curr_time >= exit_t:
            self.log_decision(
                "TRACKING SL/TARGET",
                f"EXIT TIME {self.exit_time} REACHED! Current PnL: INR {total_pnl:.2f}. "
                f"Triggering scheduled square-off across all brokers!",
                level="SUCCESS"
            )
            self._square_off_all_legs(reason="EXIT_TIME")
            return

    def _square_off_all_legs(self, reason: str):
        if not self.current_trade:
            return
        self.trade_state = "EXITING"
        buffer_pct = float(self.config.get("limit_buffer_pct", 0.015))
        auto_chase = bool(self.config.get("auto_chase", True))

        for leg in self.current_trade.get("legs", []):
            qty_to_buy = abs(leg.get("qty", 0))
            if qty_to_buy > 0 and leg.get("status") not in ["CLOSED", "EXIT_SENT"]:
                self.emit_order(
                    symbol=leg["symbol"],
                    side="BUY",
                    quantity=qty_to_buy,
                    order_type="LIMIT",
                    product=self.product,
                    leg_id=f"EXIT_{leg.get('leg')}",
                    buffer_pct=buffer_pct,
                    auto_chase=auto_chase
                )
                leg["status"] = "EXIT_SENT"
                self.log_decision(
                    "ORDER SENT TO BROKER",
                    f"Dispatched BUY {leg['symbol']} ({qty_to_buy} qty) [Marketable LIMIT + buffer] for {reason} square-off.",
                    level="SUCCESS"
                )
