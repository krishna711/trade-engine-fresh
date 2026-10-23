import asyncio
import random
import datetime
from typing import Dict, Any, List, Optional
from backend.feed_manager.brokers.base_feed import BaseFeedBroker
from backend.core.symbol_mapper import symbol_mapper
from backend.feed_manager.brokers.xts_feed import BASE_PRICES

class FyersFeedBroker(BaseFeedBroker):
    """
    Fyers Data Socket adapter.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__("FYERS", config)
        self.app_id = self.config.get("app_id", "")
        self.access_token = self.config.get("access_token", "")
        self._streaming_task: Optional[asyncio.Task] = None
        self._current_prices: Dict[str, float] = dict(BASE_PRICES)
        self.latency_ms = 22.0

    async def connect(self) -> bool:
        self.is_connected = True
        self.last_heartbeat = datetime.datetime.utcnow()
        self.emit_status(True, "Fyers Feed ready as standby failover")
        return True

    async def disconnect(self):
        self.is_connected = False
        if self._streaming_task and not self._streaming_task.done():
            self._streaming_task.cancel()
        self.emit_status(False, "Fyers Feed disconnected")

    async def subscribe(self, symbols: List[str]) -> bool:
        for s in symbols:
            if s not in self.subscribed_symbols:
                self.subscribed_symbols.append(s)
                if s not in self._current_prices:
                    self._current_prices[s] = 100.0

        if not self._streaming_task or self._streaming_task.done():
            self._streaming_task = asyncio.create_task(self._run_ticker_loop())
        return True

    async def unsubscribe(self, symbols: List[str]) -> bool:
        for s in symbols:
            if s in self.subscribed_symbols:
                self.subscribed_symbols.remove(s)
        return True

    async def _run_ticker_loop(self):
        while self.is_connected:
            try:
                for symbol in list(self.subscribed_symbols):
                    base = self._current_prices.get(symbol, 100.0)
                    delta_pct = (random.random() - 0.498) * 0.003
                    new_ltp = round(base * (1 + delta_pct), 2)
                    self._current_prices[symbol] = new_ltp

                    mapping = symbol_mapper.get_mapping(symbol)
                    display = mapping.display_name if mapping else symbol
                    segment = mapping.segment if mapping else symbol.split(":")[0]

                    tick = {
                        "symbol": symbol,
                        "display_name": display,
                        "segment": segment,
                        "broker": "FYERS",
                        "ltp": new_ltp,
                        "change": round(new_ltp - base, 2),
                        "change_pct": round((delta_pct * 100), 2),
                        "high": round(new_ltp * 1.015, 2),
                        "low": round(new_ltp * 0.985, 2),
                        "volume": random.randint(500, 25000),
                        "bid": round(new_ltp - 0.05, 2),
                        "ask": round(new_ltp + 0.05, 2),
                        "timestamp": datetime.datetime.utcnow().isoformat(),
                        "latency_ms": round(random.uniform(16.0, 28.0), 1)
                    }
                    self.emit_tick(tick)
                    self.latency_ms = tick["latency_ms"]

                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Fyers] Ticker loop error: {e}")
                await asyncio.sleep(1.0)

    async def fetch_ohlc(
        self,
        canonical_symbol: str,
        resolution: str = "5m",
        count: int = 50
    ) -> List[Dict[str, Any]]:
        base_price = self._current_prices.get(canonical_symbol, BASE_PRICES.get(canonical_symbol, 1000.0))
        now = datetime.datetime.utcnow()
        bars = []
        step_min = 5 if "5" in resolution else (60 if "1h" in resolution or "60" in resolution else 1)

        p = base_price * 0.98
        for i in range(count, 0, -1):
            bar_time = now - datetime.timedelta(minutes=i * step_min)
            delta = (random.random() - 0.48) * (base_price * 0.005)
            open_p = p
            close_p = round(open_p + delta, 2)
            high_p = round(max(open_p, close_p) + abs(random.random() * (base_price * 0.002)), 2)
            low_p = round(min(open_p, close_p) - abs(random.random() * (base_price * 0.002)), 2)
            vol = random.randint(1000, 50000)

            bars.append({
                "symbol": canonical_symbol,
                "time": bar_time.isoformat(),
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p,
                "volume": vol
            })
            p = close_p

        return bars
