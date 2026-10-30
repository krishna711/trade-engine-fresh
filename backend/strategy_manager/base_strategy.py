import datetime
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional

class BaseStrategy(ABC):
    """
    Abstract base strategy class for running algorithmic trading strategies.
    Runs inside an isolated worker process.
    Provides standard lifecycle events, decision logging, multi-client routing,
    and multi-leg order execution.
    """

    def __init__(self, instance_id: int, config: Dict[str, Any], client_ids: List[str]):
        self.instance_id = instance_id
        self.config = config
        self.client_ids = client_ids
        self.is_running = False
        self.symbol = config.get("symbol", "NSE:NIFTY")
        self.timeframe = config.get("timeframe", "5m")
        self.trade_state = "IDLE" # IDLE, IN_TRADE, EXITING, CLOSED
        self.current_trade: Optional[Dict[str, Any]] = None
        self.decision_logs: List[Dict[str, Any]] = []
        self.pending_orders: List[Dict[str, Any]] = []
        self.new_subscriptions: List[str] = []

    @abstractmethod
    def on_init(self):
        """Called once when strategy worker starts"""
        pass

    @abstractmethod
    def on_candle(self, candle: Dict[str, Any], history: List[Dict[str, Any]]):
        """Called when a new completed candle is fetched/generated"""
        pass

    @abstractmethod
    def on_tick(self, tick: Dict[str, Any]):
        """Called on every real-time market tick"""
        pass

    def on_order_update(self, update_data: Dict[str, Any]):
        """Called when an order execution or fill report is received from supervisor"""
        pass

    def emit_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "MARKET",
        price: float = 0.0,
        trigger_price: float = 0.0,
        product: str = "MIS",
        leg_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Dispatches an order request to the supervisor's out_queue for parallel routing"""
        order_req = {
            "symbol": symbol,
            "side": side.upper(),
            "quantity": quantity,
            "order_type": order_type.upper(),
            "price": price,
            "trigger_price": trigger_price,
            "product": product.upper(),
            "leg_id": leg_id
        }
        self.pending_orders.append(order_req)
        return order_req

    def request_subscribe(self, symbols: List[str]):
        """Requests market data feed subscriptions for newly calculated option strikes"""
        for s in symbols:
            if s not in self.new_subscriptions:
                self.new_subscriptions.append(s)

    def log_decision(self, step: str, details: str = "", level: str = "INFO"):
        """
        Records structured decision trail:
        - Candle Fetch Success
        - ENTRY CONDITION MATCH / NOT MATCH
        - ORDER SENT TO BROKER
        - ORDER FILLED / FAILED
        - TRADE ACTIVE
        - TRACKING SL/TARGET
        """
        entry = {
            "strategy_id": self.instance_id,
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "step": step,
            "details": details,
            "level": level,
            "trade_state": self.trade_state
        }
        self.decision_logs.append(entry)
        if len(self.decision_logs) > 200:
            self.decision_logs.pop(0)
        return entry

    def on_timer(self):
        """
        Called every 1 second by the worker process container.
        Allows time-based strategies to evaluate scheduled entries, exits,
        and trailing stops even if market ticks are temporarily paused.
        """
        pass

    def on_config_updated(self, new_config: Dict[str, Any]):
        """
        Called when strategy parameters are dynamically modified at runtime.
        Subclasses should override to update algorithm parameters without losing position tracking.
        """
        self.config.update(new_config)
        if "symbol" in new_config:
            self.symbol = new_config["symbol"]
        if "timeframe" in new_config:
            self.timeframe = new_config["timeframe"]
        self.log_decision("CONFIG_UPDATE", f"Live parameters updated: {list(new_config.keys())}", level="SUCCESS")

