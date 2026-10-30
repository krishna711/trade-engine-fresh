import datetime
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
        self.entry_time = self.config.get("entry_time", "15:45:00")
        self.exit_time = self.config.get("exit_time", "23:18:00")
        self.legs = self.config.get("legs", ["CE", "PE"])
        self.lots = int(self.config.get("lots", 1))
        self.stop_loss_per_lot = float(self.config.get("stop_loss_per_lot", 750.0))
        self.product = self.config.get("product", "MIS")
        
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

        self.log_decision(
            "INIT",
            f"TimeBased ATM Seller Initialized for {self.symbol} | "
            f"Entry: {self.entry_time} | Exit: {self.exit_time} | "
            f"Lots: {self.lots} (Size: {self.lot_size}) | SL: INR {self.max_loss:.2f} ({self.stop_loss_per_lot}/lot) | "
            f"Strike Step: {self.strike_step}"
        )

    def on_config_updated(self, new_config: Dict[str, Any]):
        """Dynamically applies parameter changes (timings, lots, stop loss) while running"""
        self.config.update(new_config)
        if "symbol" in new_config:
            self.symbol = new_config["symbol"]
        if "entry_time" in new_config:
            self.entry_time = new_config["entry_time"]
        if "exit_time" in new_config:
            self.exit_time = new_config["exit_time"]
        if "lots" in new_config:
            self.lots = int(new_config["lots"])
        if "stop_loss_per_lot" in new_config:
            self.stop_loss_per_lot = float(new_config["stop_loss_per_lot"])
        self.max_loss = self.stop_loss_per_lot * self.lots
        if "product" in new_config:
            self.product = new_config["product"]
        if "strike_step" in new_config:
            self.strike_step = float(new_config["strike_step"])
        if "lot_size" in new_config:
            self.lot_size = int(new_config["lot_size"])
        if "allow_entry_in_window" in new_config:
            self.allow_entry_in_window = bool(new_config["allow_entry_in_window"])

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

    def on_candle(self, candle: Dict[str, Any], history: List[Dict[str, Any]]):
        self.log_decision("Candle Fetch Success", f"Underlying {self.symbol} bar close: {candle.get('close')}")
        self._check_entry_and_exit(candle.get("close", 100.0))

    def on_tick(self, tick: Dict[str, Any]):
        sym = tick.get("symbol")
        ltp = tick.get("ltp")
        if not ltp:
            return

        # If tick is for underlying index/future and we are IDLE, evaluate entry
        if self.trade_state == "IDLE" and (sym == self.symbol or self.symbol.split(":")[-1] in sym):
            self._check_entry_and_exit(ltp)
            return

        # If we are in trade, monitor leg LTPs and evaluate SL & Exit Time
        if self.trade_state in ["IN_TRADE", "ENTRY_SENT"] and self.current_trade:
            # Check if this tick is for one of the option legs
            for leg in self.current_trade["legs"]:
                if leg["symbol"] == sym or sym.endswith(leg["symbol"].split(":")[-1]):
                    leg["ltp"] = ltp
                    if leg.get("entry") and leg["entry"] > 0:
                        # Selling: profit when LTP drops below entry price
                        leg["pnl"] = round((leg["entry"] - ltp) * abs(leg["qty"]), 2)

            total_pnl = sum(leg.get("pnl", 0.0) for leg in self.current_trade["legs"])
            self.current_trade["total_pnl"] = round(total_pnl, 2)

            # Check SL and Exit Time
            self._check_exit_conditions(total_pnl)

    def _check_entry_and_exit(self, underlying_ltp: float):
        if self.trade_state != "IDLE":
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
        atm_strike = int(round(underlying_ltp / self.strike_step) * self.strike_step)
        option_symbols = self._build_option_symbols(self.symbol, atm_strike)
        total_qty = self.lots * self.lot_size

        self.log_decision(
            "ENTRY CONDITION MATCH",
            f"Time {curr_time.strftime('%H:%M:%S')} in window. "
            f"Underlying {self.symbol} LTP: {underlying_ltp:.2f} -> ATM Strike: {atm_strike}. "
            f"Preparing to SELL {', '.join(option_symbols.values())} ({total_qty} qty) across {len(self.client_ids)} broker account(s)."
        )

        legs_data = []
        for leg, opt_sym in option_symbols.items():
            # Emit SELL order for this ATM leg
            self.emit_order(
                symbol=opt_sym,
                side="SELL",
                quantity=total_qty,
                order_type="MARKET",
                product=self.product,
                leg_id=f"LEG_{leg}"
            )
            self.log_decision(
                "ORDER SENT TO BROKER",
                f"Dispatched SELL {opt_sym} (Qty: {total_qty}) to Order Manager for parallel routing"
            )

            # Estimated initial LTP for tracking until first fill/tick
            est_ltp = round(underlying_ltp * 0.02, 1) or 120.0
            legs_data.append({
                "leg": f"{self.symbol.split(':')[-1]} {atm_strike} {leg}",
                "symbol": opt_sym,
                "entry": est_ltp,
                "exit": None,
                "ltp": est_ltp,
                "qty": -total_qty,
                "pnl": 0.0,
                "status": "SENT"
            })

        # Request feed subscriptions for the new option strikes so market ticks stream in
        self.subscribed_option_symbols = list(option_symbols.values())
        self.request_subscribe(self.subscribed_option_symbols)

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
            avg_price = 0.0
            filled_count = 0
            for r in results:
                if r.get("success"):
                    avg_price += float(r.get("price", 0.0))
                    filled_count += 1
            if filled_count > 0:
                avg_price = round(avg_price / filled_count, 2)

            for leg in self.current_trade["legs"]:
                if leg["symbol"] == sym:
                    if avg_price > 0:
                        leg["entry"] = avg_price
                        leg["ltp"] = avg_price
                    leg["status"] = "FILLED"

            self.log_decision(
                "ORDER FILLED",
                f"Leg {sym} SELL filled @ {avg_price} across {filled_count} broker account(s)."
            )

            # If all legs are filled, transition to IN_TRADE
            if all(leg.get("status") == "FILLED" for leg in self.current_trade["legs"]):
                self.trade_state = "IN_TRADE"
                self.log_decision(
                    "TRADE ACTIVE",
                    f"All ATM legs filled. Active monitoring SL (Max Loss: -INR {self.max_loss:.2f}) and Exit Time ({self.exit_time})."
                )

        elif side == "BUY" and self.current_trade:
            # Exit fills
            self.log_decision("ORDER FILLED", f"Square-off BUY filled for {sym}.")
            for leg in self.current_trade["legs"]:
                if leg["symbol"] == sym:
                    leg["status"] = "CLOSED"
            if all(leg.get("status") == "CLOSED" for leg in self.current_trade["legs"]):
                self.trade_state = "CLOSED"
                self.log_decision("TRADE COMPLETED", f"Strategy cycle completed. Realized PnL: INR {self.current_trade.get('total_pnl', 0.0):.2f}")

    def _check_exit_conditions(self, total_pnl: float):
        if self.trade_state != "IN_TRADE" or not self.current_trade:
            return

        curr_time = self._get_current_ist_time()
        exit_t = self._parse_time_str(self.exit_time)

        # 1. Check Stoploss: -INR 750 per lot
        if total_pnl <= -self.max_loss:
            self.log_decision(
                "TRACKING SL/TARGET",
                f"STOP LOSS HIT! Total PnL INR {total_pnl:.2f} <= -INR {self.max_loss:.2f} (SL limit {self.stop_loss_per_lot}/lot). "
                f"Triggering immediate square-off across all brokers!"
            )
            self._square_off_all_legs(reason="STOP_LOSS")
            return

        # 2. Check Exit Time: 23:18
        if curr_time >= exit_t:
            self.log_decision(
                "TRACKING SL/TARGET",
                f"EXIT TIME {self.exit_time} REACHED! Current PnL: INR {total_pnl:.2f}. "
                f"Triggering scheduled square-off across all brokers!"
            )
            self._square_off_all_legs(reason="EXIT_TIME")
            return

    def _square_off_all_legs(self, reason: str):
        self.trade_state = "EXITING"
        for leg in self.current_trade["legs"]:
            qty_to_buy = abs(leg["qty"])
            self.emit_order(
                symbol=leg["symbol"],
                side="BUY",
                quantity=qty_to_buy,
                order_type="MARKET",
                product=self.product,
                leg_id=f"EXIT_{leg['leg']}"
            )
            self.log_decision(
                "ORDER SENT TO BROKER",
                f"Dispatched BUY {leg['symbol']} ({qty_to_buy} qty) for {reason} square-off."
            )
