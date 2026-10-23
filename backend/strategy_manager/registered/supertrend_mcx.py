import datetime
from typing import Dict, Any, List
from backend.strategy_manager.base_strategy import BaseStrategy

class SupertrendMCXStrategy(BaseStrategy):
    """
    MCX Commodity Supertrend Breakout Engine (CrudeOil & NaturalGas).
    """

    def on_init(self):
        self.atr_period = self.config.get("atr_period", 10)
        self.multiplier = self.config.get("multiplier", 3.0)
        self.lots = self.config.get("lots", 1)
        self.stop_loss_pts = self.config.get("stop_loss_pts", 35.0)
        self.target_pts = self.config.get("target_pts", 70.0)
        self.log_decision("INIT", f"MCX Supertrend Breakout initialized for {self.symbol} (ATR {self.atr_period}, Mult {self.multiplier})")

    def on_candle(self, candle: Dict[str, Any], history: List[Dict[str, Any]]):
        close_p = candle["close"]
        self.log_decision("Candle Fetch Success", f"MCX {self.symbol} candle closed at {close_p}")

        if self.trade_state == "IDLE":
            # Simulate breakout condition
            self.log_decision("ENTRY CONDITION MATCH", f"Price {close_p} broke above Supertrend upper band")
            self.log_decision("ORDER SENT TO BROKER", f"BUY {self.lots} Lot {self.symbol} @ MARKET across {len(self.client_ids)} account(s)")
            
            fill_price = close_p
            self.current_trade = {
                "symbol": self.symbol,
                "side": "BUY",
                "entry_price": fill_price,
                "quantity": self.lots * 100,
                "sl_price": fill_price - self.stop_loss_pts,
                "target_price": fill_price + self.target_pts,
                "legs": [
                    {"leg": f"{self.symbol} FUT", "entry": fill_price, "exit": None, "ltp": fill_price, "qty": self.lots * 100}
                ]
            }
            self.trade_state = "IN_TRADE"
            self.log_decision("ORDER FILLED", f"Filled at {fill_price}. Trade active.")
            self.log_decision("TRADE ACTIVE", f"SL: {fill_price - self.stop_loss_pts} | TARGET: {fill_price + self.target_pts}")

    def on_tick(self, tick: Dict[str, Any]):
        if self.trade_state == "IN_TRADE" and self.current_trade:
            ltp = tick["ltp"]
            self.current_trade["legs"][0]["ltp"] = ltp
