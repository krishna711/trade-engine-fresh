import asyncio
import datetime
from typing import Dict, Any, List, Optional
from backend.config import (
    DEFAULT_FEED_PRIORITY, DEFAULT_SUBSCRIPTIONS, FEED_HEARTBEAT_TIMEOUT_SEC,
    XTS_FEED_URL, XTS_FEED_API_KEY, XTS_FEED_API_SECRET,
    SHOONYA_USER_ID, SHOONYA_PASSWORD, SHOONYA_TWO_FA, SHOONYA_VENDOR_CODE, SHOONYA_API_SECRET
)
from backend.feed_manager.brokers.base_feed import BaseFeedBroker
from backend.feed_manager.brokers.xts_feed import XTSFeedBroker
from backend.feed_manager.brokers.shoonya_feed import ShoonyaFeedBroker
from backend.feed_manager.brokers.fyers_feed import FyersFeedBroker
from backend.core.events import event_bus
from backend.core.symbol_mapper import symbol_mapper
from backend.database.connection import SessionLocal
from backend.database.models import FeedBrokerStatus, SystemLog, HistoricalCandle

class OHLCBuilder:
    """In-memory streaming OHLC candle aggregator for 1m, 5m, 15m, 1h intervals"""

    def __init__(self):
        # symbol -> resolution -> list of bars
        self.candles: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        # symbol -> current building candle
        self.current_bars: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def on_tick(self, tick: Dict[str, Any]):
        symbol = tick["symbol"]
        ltp = float(tick.get("ltp", 0.0))
        from backend.feed_manager.market_hours import IST_TZ
        now = datetime.datetime.now(IST_TZ)

        if symbol not in self.candles:
            self.candles[symbol] = {"1m": [], "3m": [], "5m": [], "15m": [], "1h": []}
            self.current_bars[symbol] = {}

        for res, step_minutes in [("1m", 1), ("3m", 3), ("5m", 5), ("15m", 15), ("1h", 60)]:
            curr = self.current_bars[symbol].get(res)
            # Candle bucket time in IST
            minute_bucket = (now.minute // step_minutes) * step_minutes
            bucket_dt = now.replace(minute=minute_bucket, second=0, microsecond=0)
            bucket_time_str = bucket_dt.isoformat()

            curr_t_str = str(curr.get("time") if curr else "")
            # Check if bucket shifted
            if not curr or not curr_t_str.startswith(bucket_time_str[:16]):
                # Save previous bar
                if curr:
                    self.candles[symbol][res].append(dict(curr))
                    if len(self.candles[symbol][res]) > 3500:
                        self.candles[symbol][res].pop(0)

                # Start new bar
                self.current_bars[symbol][res] = {
                    "symbol": symbol,
                    "time": bucket_time_str,
                    "label": bucket_dt.strftime("%H:%M"),
                    "open": ltp,
                    "high": ltp,
                    "low": ltp,
                    "close": ltp,
                    "volume": float(tick.get("volume", 0))
                }
            else:
                curr["high"] = max(curr["high"], ltp)
                curr["low"] = min(curr["low"], ltp)
                curr["close"] = ltp
                curr["volume"] = float(curr.get("volume", 0)) + float(tick.get("volume", 0))

    def get_bars(self, symbol: str, resolution: str = "5m", count: int = 50) -> List[Dict[str, Any]]:
        sym_candles = self.candles.get(symbol, {}).get(resolution, [])
        bars = [dict(b) for b in sym_candles[-count:]]

        # If RAM cache has fewer than count bars, attempt to load persisted historical bars from DB
        if len(bars) < count:
            try:
                db = SessionLocal()
                db_rows = db.query(HistoricalCandle).filter(
                    HistoricalCandle.symbol == symbol,
                    HistoricalCandle.resolution == resolution
                ).order_by(HistoricalCandle.timestamp.desc()).limit(count).all()
                if db_rows:
                    db_rows.reverse()
                    from backend.feed_manager.market_hours import get_last_completed_trading_session_anchor, IST_TZ
                    anchor = get_last_completed_trading_session_anchor(symbol, resolution)
                    last_row_t = db_rows[-1].timestamp
                    if last_row_t.tzinfo is None:
                        last_row_t = last_row_t.replace(tzinfo=datetime.timezone.utc).astimezone(IST_TZ)
                    # Accept cached DB rows if the latest row is from today or within the last 3 days
                    if (anchor.date() - last_row_t.date()).days <= 3:
                        loaded = [
                            {
                                "symbol": r.symbol,
                                "time": r.timestamp.isoformat(),
                                "open": float(r.open),
                                "high": float(r.high),
                                "low": float(r.low),
                                "close": float(r.close),
                                "volume": int(r.volume or 0)
                            }
                            for r in db_rows
                        ]
                        self.candles.setdefault(symbol, {})[resolution] = loaded
                        bars = [dict(b) for b in loaded[-count:]]
                db.close()
            except Exception:
                pass

        # Include currently building bar if present
        curr = self.current_bars.get(symbol, {}).get(resolution)
        if curr:
            bars.append(dict(curr))
        # Format time to ISO string
        for b in bars:
            if isinstance(b.get("time"), datetime.datetime):
                b["time"] = b["time"].isoformat()
        return bars

class FeedManager:
    """
    Microservice supervisor for Market Data Feeds.
    Features:
    - Broker priority list with automatic failover (XTS -> SHOONYA -> FYERS)
    - Default instrument auto-subscriptions across NSE, NFO, BSE, BSEFO, MCX, MCXFO
    - Expiry auto-roll detection for MCX commodity contracts
    - Real-time in-memory tick cache & OHLC candle aggregator
    - Low-latency local broadcast to Frontend and Strategy Workers
    """

    def __init__(self):
        self.priority_list: List[str] = list(DEFAULT_FEED_PRIORITY)
        self.active_broker_name: Optional[str] = None
        self.active_broker: Optional[BaseFeedBroker] = None
        
        self.brokers: Dict[str, BaseFeedBroker] = {
            "XTS": XTSFeedBroker({
                "api_url": XTS_FEED_URL,
                "api_key": XTS_FEED_API_KEY,
                "api_secret": XTS_FEED_API_SECRET
            }),
            "SHOONYA": ShoonyaFeedBroker({
                "user_id": SHOONYA_USER_ID,
                "password": SHOONYA_PASSWORD,
                "two_fa": SHOONYA_TWO_FA,
                "vendor_code": SHOONYA_VENDOR_CODE,
                "api_secret": SHOONYA_API_SECRET
            }),
            "FYERS": FyersFeedBroker()
        }
        
        self.subscribed_symbols: List[str] = []
        self.ticks_cache: Dict[str, Dict[str, Any]] = {}
        self.ohlc_builder = OHLCBuilder()
        self._monitor_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._is_running = False

    async def start(self):
        if self._is_running:
            return
        self._is_running = True

        # Check and perform any auto-roll for expired commodity contracts
        rolled = symbol_mapper.check_and_roll_expiries()
        if rolled:
            print(f"[FeedManager] Auto-rolled expired commodity contracts: {rolled}")

        # Pre-assign default active broker based on priority so initial snapshot quotes during connect are accepted
        if not self.active_broker_name and self.priority_list:
            self.active_broker_name = self.priority_list[0]
            self.active_broker = self.brokers.get(self.active_broker_name)

        # Connect all brokers in standby or active mode
        for name, broker in self.brokers.items():
            broker.set_callbacks(
                on_tick=self._handle_tick,
                on_status=self._handle_broker_status
            )
            await broker.connect()

        # Set active broker based on priority
        await self._select_best_broker()

        # Auto-subscribe default instruments
        default_syms = [item["canonical"] for item in DEFAULT_SUBSCRIPTIONS]
        await self.subscribe(default_syms)

        # Capture running asyncio loop for thread-safe cross-thread event scheduling
        self._loop = asyncio.get_running_loop()

        # Start supervisor health monitor
        self._monitor_task = asyncio.create_task(self._health_monitor_loop())
        print(f"[FeedManager] Started successfully. Active broker: {self.active_broker_name}")

    async def stop(self):
        self._is_running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        for broker in self.brokers.values():
            await broker.disconnect()
        print("[FeedManager] Stopped.")

    async def _select_best_broker(self) -> bool:
        """Picks the highest priority connected broker"""
        for broker_name in self.priority_list:
            broker = self.brokers.get(broker_name)
            if broker and broker.is_connected:
                old_broker = self.active_broker_name
                self.active_broker_name = broker_name
                self.active_broker = broker
                broker.last_heartbeat = datetime.datetime.utcnow()
                self._update_db_broker_status()

                if old_broker != broker_name:
                    log_msg = f"Feed Manager active source switched to {broker_name}"
                    if old_broker:
                        log_msg += f" (Failover from {old_broker})"
                    self._log_event("SUCCESS" if not old_broker else "WARNING", log_msg)

                    # Re-subscribe all active symbols to the newly activated broker
                    if self.subscribed_symbols:
                        await self.active_broker.subscribe(self.subscribed_symbols)

                return True
        return False

    async def failover_to_next(self, reason: str = "Manual failover test"):
        """Triggers immediate failover to the next available broker in priority list"""
        old_broker_name = self.active_broker_name
        curr_idx = self.priority_list.index(self.active_broker_name) if self.active_broker_name in self.priority_list else -1
        next_idx = (curr_idx + 1) % len(self.priority_list)
        next_broker_name = self.priority_list[next_idx]

        self._log_event("WARNING", f"Initiating failover from {self.active_broker_name} to {next_broker_name}. Reason: {reason}")
        
        self.active_broker_name = next_broker_name
        self.active_broker = self.brokers[next_broker_name]
        if not self.active_broker.is_connected:
            await self.active_broker.connect()

        await self.active_broker.subscribe(self.subscribed_symbols)
        self._update_db_broker_status()
        
        await event_bus.publish("feed.status", self.get_status())

        # Telegram notification: Feed failover event (Critical) - Only during active market hours
        try:
            from backend.feed_manager.market_hours import is_any_market_open
            if is_any_market_open():
                from backend.core.telegram_notifier import telegram_notifier
                asyncio.create_task(telegram_notifier.notify_critical(
                    title=f"Market Feed Failover: {old_broker_name} ➔ {next_broker_name}",
                    details=f"Reason: {reason}. All {len(self.subscribed_symbols)} subscriptions migrated to {next_broker_name}.",
                    component="Market Data Feed Manager",
                    entity=f"Primary Feed: {next_broker_name}"
                ))
        except Exception as tg_err:
            pass

        return self.active_broker_name

    async def switch_broker(self, target_broker: str, reason: str = "Manual user selection") -> str:
        """Explicitly switches active feed source to the designated broker (e.g. XTS, SHOONYA, FYERS)"""
        target = target_broker.upper().strip()
        if target not in self.brokers:
            print(f"[FeedManager] Invalid target broker: {target}")
            return self.active_broker_name or "XTS"

        old_broker_name = self.active_broker_name
        self.active_broker_name = target
        self.active_broker = self.brokers[target]

        # Connect target broker if not connected
        if not self.active_broker.is_connected:
            await self.active_broker.connect()

        self.active_broker.last_heartbeat = datetime.datetime.utcnow()

        if self.subscribed_symbols:
            await self.active_broker.subscribe(self.subscribed_symbols)

        # Place target at index 0 of priority_list so it remains primary priority
        if target in self.priority_list:
            self.priority_list.remove(target)
            self.priority_list.insert(0, target)

        self._update_db_broker_status()
        self._log_event("SUCCESS", f"Feed source switched to {target} (Previous: {old_broker_name}). {reason}")

        await event_bus.publish("feed.status", self.get_status())
        return self.active_broker_name

    async def set_priority(self, new_priority: List[str]):
        """Dynamically update priority order"""
        self.priority_list = [p.upper() for p in new_priority if p.upper() in self.brokers]
        await self._select_best_broker()

    async def subscribe(self, symbols: List[str]) -> bool:
        for s in symbols:
            if s not in self.subscribed_symbols:
                self.subscribed_symbols.append(s)

        if self.active_broker:
            success = await self.active_broker.subscribe(symbols)
            self._log_event("SUCCESS", f"Subscribed {len(symbols)} symbols on {self.active_broker_name}: {', '.join(symbols[:4])}...")
            return success
        return False

    async def unsubscribe(self, symbols: List[str]) -> bool:
        for s in symbols:
            if s in self.subscribed_symbols:
                self.subscribed_symbols.remove(s)
        if self.active_broker:
            return await self.active_broker.unsubscribe(symbols)
        return True

    def _handle_tick(self, tick: Dict[str, Any]):
        """Invoked when a tick is received from the active feed broker"""
        # Only process ticks from active broker to avoid duplicate feeds
        if tick.get("broker") != self.active_broker_name:
            return

        symbol = tick["symbol"]
        self.ticks_cache[symbol] = tick
        self.ohlc_builder.on_tick(tick)

        # Broadcast via ultra low-latency local event bus to Frontend & Strategy workers (thread-safe)
        if self._loop and self._loop.is_running():
            try:
                curr_loop = asyncio.get_running_loop()
                if curr_loop == self._loop:
                    self._loop.create_task(event_bus.publish("feed.tick", tick))
                else:
                    asyncio.run_coroutine_threadsafe(event_bus.publish("feed.tick", tick), self._loop)
            except RuntimeError:
                asyncio.run_coroutine_threadsafe(event_bus.publish("feed.tick", tick), self._loop)

    async def get_live_quote(self, symbol: str) -> Dict[str, Any]:
        """
        Returns live market quote {symbol, ltp, bid, ask, close} for any symbol.
        Checks real-time in-memory tick cache first.
        If missing or stale, fetches immediate quote snapshot from active broker.
        """
        cached = self.ticks_cache.get(symbol)
        if cached and float(cached.get("ltp") or 0.0) > 0:
            ltp = float(cached["ltp"])
            return {
                "symbol": symbol,
                "ltp": ltp,
                "bid": float(cached.get("bid") or ltp),
                "ask": float(cached.get("ask") or ltp),
                "close": float(cached.get("close") or ltp)
            }

        # Query XTS feed broker for on-demand live quote snapshot
        xts_feed = self.brokers.get("XTS")
        if xts_feed and hasattr(xts_feed, "fetch_single_quote"):
            mapping = symbol_mapper.get_mapping(symbol)
            token = symbol_mapper.get_broker_token(symbol, "XTS")
            seg = mapping.segment if mapping else ("MCXFO" if "MCX" in symbol else "NSE")
            if token and str(token).isdigit():
                q = await xts_feed.fetch_single_quote(symbol, int(token), seg)
                if q:
                    self.ticks_cache[symbol] = q
                    return q

        return {
            "symbol": symbol,
            "ltp": 100.0,
            "bid": 99.5,
            "ask": 100.5,
            "close": 100.0
        }

    def _handle_broker_status(self, broker_name: str, is_connected: bool, message: str):
        self._update_db_broker_status()
        if not is_connected and broker_name == self.active_broker_name:
            # Automatic failover triggered!
            if self._loop and self._loop.is_running():
                try:
                    curr_loop = asyncio.get_running_loop()
                    if curr_loop == self._loop:
                        self._loop.create_task(self._select_best_broker())
                    else:
                        asyncio.run_coroutine_threadsafe(self._select_best_broker(), self._loop)
                except RuntimeError:
                    asyncio.run_coroutine_threadsafe(self._select_best_broker(), self._loop)

    async def _health_monitor_loop(self):
        """Continuously checks broker heartbeats and performs automatic failover if hung during active market hours"""
        while self._is_running:
            try:
                await asyncio.sleep(2.0)
                # Check expiry auto-roll once every 5 minutes
                symbol_mapper.check_and_roll_expiries()

                from backend.feed_manager.market_hours import is_any_market_open
                market_active = is_any_market_open()

                if market_active and self.active_broker:
                    hb = self.active_broker.last_heartbeat
                    if hb:
                        diff = (datetime.datetime.utcnow() - hb).total_seconds()
                        if diff > FEED_HEARTBEAT_TIMEOUT_SEC:
                            print(f"[FeedManager] Active broker {self.active_broker_name} heartbeat timed out ({diff:.1f}s)! Triggering failover...")
                            await self.failover_to_next(f"Heartbeat timeout ({diff:.1f}s)")
                elif not market_active and self.active_broker:
                    # During non-market hours and weekends, keep heartbeat fresh so broker doesn't failover due to zero market ticks
                    self.active_broker.last_heartbeat = datetime.datetime.utcnow()

                self._update_db_broker_status()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[FeedManager] Monitor error: {e}")

    def get_latest_tick(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self.ticks_cache.get(symbol)

    def get_all_ticks(self) -> Dict[str, Dict[str, Any]]:
        return dict(self.ticks_cache)

    async def get_ohlc(self, canonical_symbol: str, resolution: str = "5m", count: int = 50) -> List[Dict[str, Any]]:
        bars = self.ohlc_builder.get_bars(canonical_symbol, resolution, count)

        # Check if returned bars are stale (older than current session anchor)
        from backend.feed_manager.market_hours import (
            get_last_completed_trading_session_anchor,
            parse_resolution_minutes,
            build_market_session_bars,
            IST_TZ
        )
        anchor = get_last_completed_trading_session_anchor(canonical_symbol, resolution)
        step_min = parse_resolution_minutes(resolution)

        is_stale = False
        if bars:
            try:
                last_t_str = str(bars[-1].get("time") or "")
                if "T" in last_t_str:
                    last_dt = datetime.datetime.fromisoformat(last_t_str)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=datetime.timezone.utc).astimezone(IST_TZ)
                    else:
                        last_dt = last_dt.astimezone(IST_TZ)
                    # If last candle is from an older day or more than 3 candle intervals behind current anchor
                    if last_dt.date() < anchor.date() or (anchor - last_dt) > datetime.timedelta(minutes=step_min * 3):
                        is_stale = True
            except Exception:
                is_stale = False

        if len(bars) < count or is_stale:
            # Prioritize XTS broker for genuine multi-day historical candles via /instruments/ohlc
            broker = None
            if "XTS" in self.brokers:
                broker = self.brokers["XTS"]
            elif self.active_broker:
                broker = self.active_broker
            else:
                broker = list(self.brokers.values())[0] if self.brokers else None

            broker_bars = None
            if broker:
                try:
                    broker_bars = await broker.fetch_ohlc(canonical_symbol, resolution, count)
                except Exception as e_br:
                    print(f"[FeedManager] Error fetching OHLC from broker {broker.name}: {e_br}")

            # If primary broker returned empty, try active_broker if different
            if not broker_bars and self.active_broker and self.active_broker != broker:
                try:
                    broker_bars = await self.active_broker.fetch_ohlc(canonical_symbol, resolution, count)
                except Exception:
                    pass

            # If no broker connected or broker returned empty, fallback to deterministic market session bars
            if not broker_bars:
                curr_price = float(self.ticks_cache.get(canonical_symbol, {}).get("ltp", 0.0))
                broker_bars = build_market_session_bars(
                    symbol=canonical_symbol,
                    resolution=resolution,
                    count=max(count, 60),
                    base_price=curr_price
                )

            if broker_bars:
                if canonical_symbol not in self.ohlc_builder.candles:
                    self.ohlc_builder.candles[canonical_symbol] = {"1m": [], "3m": [], "5m": [], "15m": [], "1h": []}
                self.ohlc_builder.candles[canonical_symbol][resolution] = [dict(b) for b in broker_bars[:-1]]
                if broker_bars:
                    self.ohlc_builder.current_bars.setdefault(canonical_symbol, {})[resolution] = dict(broker_bars[-1])

                # Asynchronously / safely persist into SQLite historical_candles in bulk
                try:
                    db = SessionLocal()
                    existing_ts = {
                        r[0] for r in db.query(HistoricalCandle.timestamp).filter(
                            HistoricalCandle.symbol == canonical_symbol,
                            HistoricalCandle.resolution == resolution
                        ).all()
                    }
                    new_candles = []
                    for b in broker_bars:
                        raw_t = b.get("time")
                        if isinstance(raw_t, str):
                            try:
                                dt_val = datetime.datetime.fromisoformat(raw_t.replace("Z", "+00:00"))
                            except Exception:
                                continue
                        else:
                            dt_val = raw_t
                        if dt_val not in existing_ts:
                            new_candles.append(HistoricalCandle(
                                symbol=canonical_symbol,
                                resolution=resolution,
                                timestamp=dt_val,
                                open=float(b.get("open", 0.0)),
                                high=float(b.get("high", 0.0)),
                                low=float(b.get("low", 0.0)),
                                close=float(b.get("close", 0.0)),
                                volume=int(b.get("volume", 0))
                            ))
                            existing_ts.add(dt_val)
                    if new_candles:
                        db.bulk_save_objects(new_candles)
                        db.commit()
                    db.close()
                except Exception:
                    pass

                return broker_bars[-count:]
        return bars

    def get_status(self) -> Dict[str, Any]:
        broker_states = {}
        for name, b in self.brokers.items():
            broker_states[name] = {
                "name": name,
                "is_connected": b.is_connected,
                "is_active": (name == self.active_broker_name),
                "latency_ms": b.latency_ms,
                "last_heartbeat": b.last_heartbeat.isoformat() if b.last_heartbeat else None,
                "subscribed_count": len(b.subscribed_symbols)
            }

        return {
            "active_broker": self.active_broker_name,
            "priority": self.priority_list,
            "total_subscribed": len(self.subscribed_symbols),
            "subscribed_symbols": self.subscribed_symbols,
            "brokers": broker_states,
            "uptime_seconds": 100, # runtime counter
            "ticks_in_cache": len(self.ticks_cache)
        }

    def _update_db_broker_status(self):
        db = SessionLocal()
        try:
            for name, b in self.brokers.items():
                p_idx = self.priority_list.index(name) + 1 if name in self.priority_list else 99
                st = db.query(FeedBrokerStatus).filter(FeedBrokerStatus.broker == name).first()
                if not st:
                    st = FeedBrokerStatus(broker=name)
                    db.add(st)
                st.priority = p_idx
                st.is_connected = b.is_connected
                st.is_active = (name == self.active_broker_name)
                st.latency_ms = b.latency_ms
                st.last_heartbeat = b.last_heartbeat
                st.subscribed_count = len(b.subscribed_symbols)
            db.commit()

            if self._loop and self._loop.is_running():
                try:
                    curr_loop = asyncio.get_running_loop()
                    if curr_loop == self._loop:
                        self._loop.create_task(event_bus.publish("feed.status", self.get_status()))
                    else:
                        asyncio.run_coroutine_threadsafe(event_bus.publish("feed.status", self.get_status()), self._loop)
                except Exception:
                    pass
        except Exception as e:
            db.rollback()
        finally:
            db.close()

    def _log_event(self, level: str, message: str):
        db = SessionLocal()
        try:
            log = SystemLog(
                category="FEED_SUBSCRIPTION",
                level=level,
                message=message,
                symbol="FEED_MGR",
                timestamp=datetime.datetime.utcnow()
            )
            db.add(log)
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

# Global Feed Manager Singleton
feed_manager = FeedManager()
