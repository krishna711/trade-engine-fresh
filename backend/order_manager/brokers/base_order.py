from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional

class BaseOrderBroker(ABC):
    """Abstract interface for all broker order execution adapters"""

    def __init__(self, broker_name: str, client_id: str, credentials: Optional[Dict[str, Any]] = None, static_ip: Optional[str] = None):
        self.broker_name = broker_name
        self.client_id = client_id
        self.credentials = credentials or {}
        self.static_ip = static_ip or (self.credentials.get("static_ip") if isinstance(self.credentials, dict) else None)

    @abstractmethod
    async def place_order(self, order_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Places an order with the broker API. Returns broker_order_id, status, avg_price"""
        pass

    @abstractmethod
    async def modify_order(self, broker_order_id: str, quantity: int, price: float) -> bool:
        """Modifies price/quantity of an open order"""
        pass

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> bool:
        """Cancels an open order"""
        pass

    @abstractmethod
    async def get_order_status(self, broker_order_id: str) -> Dict[str, Any]:
        """Polls single order status"""
        pass

    @abstractmethod
    async def get_positions(self) -> List[Dict[str, Any]]:
        """Fetches net and day positions from broker"""
        pass

    @abstractmethod
    async def get_margins(self) -> Dict[str, Any]:
        """Fetches available and used margin"""
        pass

    async def get_holdings(self) -> List[Dict[str, Any]]:
        """Fetches long-term equity holdings from broker"""
        return []

    async def get_orders(self) -> List[Dict[str, Any]]:
        """Fetches live orderbook from broker"""
        return []

    async def get_trades(self) -> List[Dict[str, Any]]:
        """Fetches executed trades from broker"""
        return []
