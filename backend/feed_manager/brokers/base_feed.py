from abc import ABC, abstractmethod
from typing import Dict, Any, List, Callable, Optional
import datetime

class BaseFeedBroker(ABC):
    """Abstract interface for all broker feed adapters"""

    def __init__(self, broker_name: str, config: Optional[Dict[str, Any]] = None):
        self.broker_name = broker_name
        self.config = config or {}
        self.is_connected = False
        self.latency_ms = 0.0
        self.last_heartbeat: Optional[datetime.datetime] = None
        self.subscribed_symbols: List[str] = []
        self._on_tick_callback: Optional[Callable[[Dict[str, Any]], None]] = None
        self._on_status_callback: Optional[Callable[[str, bool, str], None]] = None

    def set_callbacks(
        self,
        on_tick: Callable[[Dict[str, Any]], None],
        on_status: Callable[[str, bool, str], None]
    ):
        self._on_tick_callback = on_tick
        self._on_status_callback = on_status

    @abstractmethod
    async def connect(self) -> bool:
        """Connect to broker market data stream"""
        pass

    @abstractmethod
    async def disconnect(self):
        """Disconnect cleanly"""
        pass

    @abstractmethod
    async def subscribe(self, symbols: List[str]) -> bool:
        """Subscribe to given canonical symbols"""
        pass

    @abstractmethod
    async def unsubscribe(self, symbols: List[str]) -> bool:
        """Unsubscribe from given symbols"""
        pass

    @abstractmethod
    async def fetch_ohlc(
        self,
        canonical_symbol: str,
        resolution: str = "5m",
        count: int = 100
    ) -> List[Dict[str, Any]]:
        """Fetch historical or recent OHLC bars"""
        pass

    def emit_tick(self, tick: Dict[str, Any]):
        self.last_heartbeat = datetime.datetime.utcnow()
        if self._on_tick_callback:
            self._on_tick_callback(tick)

    def emit_status(self, is_connected: bool, message: str = ""):
        self.is_connected = is_connected
        if self._on_status_callback:
            self._on_status_callback(self.broker_name, is_connected, message)
