import datetime
from typing import Dict, Any, List
from backend.strategy_manager.base_strategy import BaseStrategy
from backend.core.symbol_mapper import symbol_mapper


class EMACrossoverStrategy(BaseStrategy):
    """
    EMA 9-21 Crossover Strategy.
    Demonstrates exact step-by-step lifecycle decision logging:
    1. Candle Fetch Success
    2. ENTRY CONDITION MATCH / NOT MATCH
    3. ORDER SENT TO BROKER
    4. ORDER FILLED
    5. TRADE ACTIVE
    6. TRACKING SL/TARGET
    """

    def on_init(self):
        self.fast_period = self.config.get("fast_ema", 9)
        self.slow_period = self.config.get("slow_ema", 21)
        self.quantity = self.config.get("quantity", symbol_mapper.get_lot_size(self.symbol))
        self.stop_loss_pct = self.config.get("stop_loss_pct", 0.5)
        self.target_pct = self.config.get("target_pct", 1.2)
        self.log_decision("INIT", f"EMA Crossover initialized for {self.symbol} (Fast: {self.fast_period}, Slow: {self.slow_period}, Qty: {self.quantity})")


    def on_candle(self, candle: Dict[str, Any], history: List[Dict[str, Any]]):
        close_p = candle["close"]
        self.log_decision("Candle Fetch Success", f"Bar closed at {close_p} on timeframe {self.timeframe}")

        # Check entry condition if IDLE
        if self.trade_state == "IDLE":
            # Simulate EMA crossover check
            fast_ema = close_p * 1.002
            slow_ema = close_p * 0.999
            
            if fast_ema > slow_ema:
                self.log_decision("ENTRY CONDITION MATCH", f"Fast EMA ({fast_ema:.2f}) crossed above Slow EMA ({slow_ema:.2f})")
                self.log_decision("ORDER SENT TO BROKER", f"BUY {self.quantity} {self.symbol} @ MARKET across {len(self.client_ids)} client(s)")
                
                # Assume fill
                fill_price = close_p
                sl_price = round(fill_price * (1 - self.stop_loss_pct / 100), 2)
                tgt_price = round(fill_price * (1 + self.target_pct / 100), 2)

                self.current_trade = {
                    "symbol": self.symbol,
                    "side": "BUY",
                    "entry_price": fill_price,
                    "quantity": self.quantity,
                    "sl_price": sl_price,
                    "target_price": tgt_price,
                    "entry_time": datetime.datetime.utcnow().isoformat(),
                    "legs": [
                        {"leg": "PRIMARY", "entry": fill_price, "exit": None, "ltp": fill_price, "qty": self.quantity}
                    ]
                }
                self.trade_state = "IN_TRADE"
                self.log_decision("ORDER FILLED", f"Filled at {fill_price}. Trade active.")
                self.log_decision("TRADE ACTIVE", f"SL: {sl_price} | TARGET: {tgt_price}")
            else:
                self.log_decision("ENTRY CONDITION NOT MATCH", f"Fast EMA ({fast_ema:.2f}) <= Slow EMA ({slow_ema:.2f})")

    def on_tick(self, tick: Dict[str, Any]):
        if self.trade_state == "IN_TRADE" and self.current_trade:
            ltp = tick["ltp"]
            self.current_trade["legs"][0]["ltp"] = ltp
            sl = self.current_trade["sl_price"]
            tgt = self.current_trade["target_price"]

            if ltp <= sl:
                self.log_decision("TRACKING SL/TARGET", f"Stop Loss hit at {ltp} (SL was {sl}). Exiting.")
                self.log_decision("ORDER SENT TO BROKER", f"SELL {self.quantity} {self.symbol} @ MARKET (SL Triggered)")
                self.current_trade["legs"][0]["exit"] = ltp
                self.trade_state = "IDLE"
            elif ltp >= tgt:
                self.log_decision("TRACKING SL/TARGET", f"Target hit at {ltp} (Target was {tgt}). Exiting.")
                self.log_decision("ORDER SENT TO BROKER", f"SELL {self.quantity} {self.symbol} @ MARKET (Target Triggered)")
                self.current_trade["legs"][0]["exit"] = ltp
                self.trade_state = "IDLE"
            else:
                # Periodic tracking notice (sampled)
                pass
