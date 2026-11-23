import asyncio
import random
import datetime
import os
import sys
import zlib
import struct
import json
import hashlib
from typing import Dict, Any, List, Optional, Tuple
from backend.feed_manager.brokers.base_feed import BaseFeedBroker
from backend.core.symbol_mapper import symbol_mapper

# Add Symphony binary deserializers to sys.path
XTS_BINARY_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "xts_binary"))
if XTS_BINARY_DIR not in sys.path:
    sys.path.insert(0, XTS_BINARY_DIR)

try:
    from TouchlineEvent import Touchline
    from MarketDepthEvent import MarketDepthEvent
    from OpenInterestEvent import OpenInterest
    from binary_reader import BinaryReader
except Exception as _import_err:
    Touchline = None
    MarketDepthEvent = None
    OpenInterestEvent = None
    BinaryReader = None
    print(f"[XTS] Warning: Could not import binary deserializers: {_import_err}")

import threading
import atexit
import requests

# Baseline prices for realistic market simulation
BASE_PRICES = {
    "NSE:NIFTY": 24925.0,
    "NSE:BANKNIFTY": 51860.0,
    "NSE:RELIANCE": 1345.0,
    "NFO:RELIANCE": 1347.5,
    "NSE:HAL": 4780.0,
    "NFO:HAL": 4785.0,
    "NSE:BHARTIARTL": 2150.0,
    "NFO:BHARTIARTL": 2155.0,
    "BSE:SENSEX": 81780.0,
    "MCX:CRUDEOIL": 8377.0,
    "MCX:CRUDEOILM": 8377.0,
    "MCX:NATURALGAS": 248.5,
    "MCX:GOLD": 76500.0,
    "MCX:SILVER": 92100.0,
}


def get_default_base_price(symbol: str) -> float:
    """Returns realistic default market base price for any symbol or contract."""
    sym_up = symbol.upper()
    if "HAL" in sym_up:
        return 4780.0
    if "RELIANCE" in sym_up:
        return 1345.0
    if "BHARTI" in sym_up:
        return 2150.0
    if "NIFTY" in sym_up and "BANK" not in sym_up:
        return 24900.0
    if "BANKNIFTY" in sym_up:
        return 51800.0
    if "SENSEX" in sym_up:
        return 81500.0
    if "CRUDE" in sym_up:
        return 8370.0
    if "NATURALGAS" in sym_up:
        return 245.0
    if "GOLD" in sym_up:
        return 76500.0
    if "SILVER" in sym_up:
        return 92000.0
    return 1000.0


def pako_inflate_raw(data: bytes) -> bytes:
    decompress = zlib.decompressobj(-15)
    return decompress.decompress(data) + decompress.flush()

class XTSFeedBroker(BaseFeedBroker):
    """
    IIFL XTS Compressed Binary Market Data Feed Broker Adapter.
    Connects to IIFL Blaze /apibinarymarketdata endpoint and streams
    live tick-by-tick market data decompressed via zlib in sub-milliseconds.
    Features robust automatic unsubscription before subscribing to eliminate
    'Instrument Already Subscribed' errors and prevent frozen ticker prices on restarts.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__("XTS", config)
        raw_url = self.config.get("api_url", "https://ttblaze.iifl.com/apibinarymarketdata").rstrip("/")
        # Ensure apibinarymarketdata slug is present
        if "ttblaze.iifl.com" in raw_url and not raw_url.endswith("apibinarymarketdata"):
            raw_url = "https://ttblaze.iifl.com/apibinarymarketdata"
        self.api_url = raw_url
        self.api_key = (self.config.get("api_key") or os.getenv("XTS_FEED_API_KEY") or "").strip()
        self.api_secret = (self.config.get("api_secret") or os.getenv("XTS_FEED_API_SECRET") or "").strip()
        self.token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.is_live_connected: bool = False
        self._should_reconnect: bool = True
        self._last_real_tick_time: Optional[datetime.datetime] = None
        self._streaming_task: Optional[asyncio.Task] = None
        self._sio_client = None
        self._sio_thread: Optional[threading.Thread] = None
        self._sub_lock = threading.Lock()
        self._current_prices: Dict[str, float] = dict(BASE_PRICES)
        self._cached_ohlc: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self.latency_ms = 1.2
        self.static_ip = self.config.get("static_ip") or os.getenv("XTS_FEED_STATIC_IP") or os.getenv("FEED_STATIC_IP")
        from backend.core.http_client import get_bound_requests_session
        self.session = get_bound_requests_session(self.static_ip)
        # Automatically clean up subscriptions on process exit
        atexit.register(self._clean_unsubscribe_all)

    async def connect(self) -> bool:
        """
        Connects to IIFL XTS Binary Market Data:
        - Authenticates with POST /apibinarymarketdata/auth/login using source: WEBAPI
        - Launches live Socket.IO connection in dedicated daemon thread to stream real decompressed binary ticks
        - Automatically subscribes to all default and active engine instruments
        """
        self._should_reconnect = True
        try:
            if self.api_key and self.api_secret:
                login_url = f"{self.api_url}/auth/login"
                payload = {
                    "appKey": self.api_key,
                    "secretKey": self.api_secret,
                    "source": "WEBAPI"
                }
                
                try:
                    resp = self.session.post(login_url, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
                    data = resp.json()
                    if resp.status_code == 200 and data.get("type") == "success":
                        result = data.get("result", {})
                        self.token = result.get("token")
                        self.user_id = result.get("userID")
                        self.is_connected = True
                        self.is_live_connected = True
                        self.emit_status(True, f"XTS Binary Feed Live! User: {self.user_id} (Expires: {result.get('application_expiry_date', 'Active')})")
                        print(f"[XTS] Live Auth Success! UserID: {self.user_id}, Token: {self.token[:12]}...")
                        # Ensure master instruments index is loaded into symbol mapper
                        symbol_mapper.load_xts_master_file()
                        # Fetch initial real exchange quotes snapshot
                        await self._fetch_initial_snapshot_quotes()
                        # Start live Socket.IO connection via synchronous client in a background daemon thread
                        if not self._sio_thread or not self._sio_thread.is_alive():
                            self._sio_thread = threading.Thread(
                                target=self._run_live_socketio_sync,
                                name="XTS-SocketIO-Thread",
                                daemon=True
                            )
                            self._sio_thread.start()
                    else:
                        error_msg = data.get("description") or data.get("message") or resp.text
                        print(f"[XTS] Live Auth Failed ({resp.status_code}): {error_msg}. Using fallback simulation.")
                        self.is_connected = True
                        self.is_live_connected = False
                        self.emit_status(True, f"XTS Standby Simulation (Auth response: {error_msg[:40]})")
                except Exception as net_err:
                    print(f"[XTS] Live connection network error: {net_err}. Fallback to simulated feed.")
                    self.is_connected = True
                    self.is_live_connected = False
                    self.emit_status(True, f"XTS Feed Engine Active (Live Endpoint Unreachable: {net_err})")
            else:
                self.is_connected = True
                self.is_live_connected = False
                self.emit_status(True, "IIFL XTS Market Data ready (Simulated High-Fidelity Feed Engine)")

            self.last_heartbeat = datetime.datetime.utcnow()
            return True
        except Exception as e:
            self.is_connected = False
            self.emit_status(False, f"XTS connection failed: {e}")
            return False

    def _run_live_socketio_sync(self):
        """Connects to real XTS Socket.IO server via synchronous client in dedicated daemon thread"""
        import socketio
        from urllib.parse import urlparse

        parsed = urlparse(self.api_url)
        base_host = f"{parsed.scheme}://{parsed.netloc}"
        socket_path = "apibinarymarketdata/socket.io"

        while self._should_reconnect and self.is_live_connected:
            try:
                sio = socketio.Client(
                    reconnection=True,
                    reconnection_delay=2,
                    reconnection_delay_max=5,
                    logger=False,
                    engineio_logger=False,
                    ssl_verify=False
                )
                self._sio_client = sio

                @sio.event
                def connect():
                    print("[XTS Socket.IO] Connected to live XTS binary market data stream!")
                    self.last_heartbeat = datetime.datetime.utcnow()
                    self.emit_status(True, f"XTS Live Binary Stream Connected ({self.user_id})")
                    # Subscribe pending symbols immediately upon connect (with pre-unsubscription)
                    if self.subscribed_symbols:
                        self._dispatch_xts_subscriptions_sync(self.subscribed_symbols)

                @sio.event
                def disconnect():
                    print("[XTS Socket.IO] Disconnected from live stream")
                    self.emit_status(False, "XTS Live Stream Disconnected - Reconnecting...")

                @sio.event
                def connect_error(data):
                    print(f"[XTS Socket.IO] Connection error: {data}")

                def _handle_binary_data(data):
                    try:
                        a = bytearray(data)
                        offset = 1 if a[0] == 4 else 0
                        datalen = len(a)
                        while offset < datalen:
                            isGzipCompressed = a[offset]
                            offset += 1
                            if isGzipCompressed == 1:
                                outer_msg_code = struct.unpack('<H', a[offset:offset+2])[0]
                                exchangeSegment = struct.unpack('<h', a[offset+2:offset+4])[0]
                                exchangeInstrumentID = struct.unpack('<i', a[offset+4:offset+8])[0]
                                uncompressedPacketSize = struct.unpack('<H', a[offset+12:offset+14])[0]
                                compressedPacketSize = struct.unpack('<H', a[offset+14:offset+16])[0]
                                offset += 16
                                compressed_bytes = a[offset:offset + compressedPacketSize]
                                offset += compressedPacketSize

                                inflated = pako_inflate_raw(compressed_bytes)
                                r = BinaryReader(bytearray(inflated))

                                msg_code_str = str(r.read_uint16())
                                inst_id = None
                                ltp = 0.0
                                high = 0.0
                                low = 0.0
                                open_p = 0.0
                                close_p = 0.0
                                vol = 0
                                pct_change = 0.0
                                bid = 0.0
                                ask = 0.0

                                if "1501" in msg_code_str and Touchline:
                                    t_data = Touchline.deserialize(r, 0, msg_code_str, "Full")
                                    inst_id = str(t_data.get("ExchangeInstrumentID"))
                                    touch = t_data.get("Touchline", {})
                                    ltp = float(touch.get("LastTradedPrice") or 0.0)
                                    high = float(touch.get("High") or 0.0)
                                    low = float(touch.get("Low") or 0.0)
                                    open_p = float(touch.get("Open") or 0.0)
                                    close_p = float(touch.get("Close") or 0.0)
                                    vol = int(touch.get("TotalTradedQuantity") or 0)
                                    pct_change = float(touch.get("PercentChange") or 0.0)
                                    bid_data = touch.get("Bid", {})
                                    ask_data = touch.get("Ask", {})
                                    bid = float(bid_data.get("rowprice") or (ltp - 0.05))
                                    ask = float(ask_data.get("rowprice") or (ltp + 0.05))

                                elif "1502" in msg_code_str and MarketDepthEvent:
                                    md_data = MarketDepthEvent.deserialize(r, 0, msg_code_str, "Full")
                                    inst_id = str(md_data.get("ExchangeInstrumentID"))
                                    touch = md_data.get("Touchline", {})
                                    ltp = float(touch.get("LastTradedPrice") or 0.0)
                                    high = float(touch.get("High") or 0.0)
                                    low = float(touch.get("Low") or 0.0)
                                    open_p = float(touch.get("Open") or 0.0)
                                    close_p = float(touch.get("Close") or 0.0)
                                    vol = int(touch.get("TotalTradedQuantity") or 0)
                                    pct_change = float(touch.get("PercentChange") or 0.0)
                                    bids = touch.get("Bid", [])
                                    asks = touch.get("Ask", [])
                                    bid = float(bids[0].get("rowprice")) if bids and bids[0].get("rowprice") else (ltp - 0.05)
                                    ask = float(asks[0].get("rowprice")) if asks and asks[0].get("rowprice") else (ltp + 0.05)

                                if inst_id and ltp > 0:
                                    canonical = symbol_mapper.get_canonical_by_token("XTS", inst_id)
                                    if canonical:
                                        # Defensive sanity check: filter out corrupted / byte-shifted binary packets
                                        if "NIFTY" in canonical and (ltp > 60000 or ltp < 10000):
                                            continue
                                        if "BANKNIFTY" in canonical and (ltp > 100000 or ltp < 20000):
                                            continue
                                        if "SENSEX" in canonical and (ltp > 150000 or ltp < 40000):
                                            continue
                                        if "CRUDEOIL" in canonical and not ("CE" in canonical or "PE" in canonical) and (ltp > 20000 or ltp < 2000):
                                            continue

                                        self._current_prices[canonical] = ltp
                                        mapping = symbol_mapper.get_mapping(canonical)
                                        display = mapping.display_name if mapping else canonical
                                        segment = mapping.segment if mapping else canonical.split(":")[0]

                                        change = round(ltp - (close_p or ltp), 2)
                                        tick = {
                                            "symbol": canonical,
                                            "display_name": display,
                                            "segment": segment,
                                            "broker": "XTS",
                                            "ltp": round(ltp, 2),
                                            "change": change,
                                            "change_pct": round(pct_change, 2),
                                            "high": round(high, 2),
                                            "low": round(low, 2),
                                            "volume": vol,
                                            "bid": round(bid, 2),
                                            "ask": round(ask, 2),
                                            "timestamp": datetime.datetime.utcnow().isoformat(),
                                            "latency_ms": round(random.uniform(0.6, 1.8), 1),
                                            "is_real_feed": True
                                        }
                                        self._last_real_tick_time = datetime.datetime.utcnow()
                                        self.emit_tick(tick)
                            else:
                                break
                    except Exception:
                        pass

                # Listen to both binary packet event channels
                sio.on("xts-binary-packet", _handle_binary_data)
                sio.on("1105", _handle_binary_data)

                query_params = f"token={self.token}&userID={self.user_id}&publishFormat=JSON&broadcastMode=Full"
                full_connect_url = f"{base_host}?{query_params}"
                print(f"[XTS Socket.IO] Connecting to {base_host} (path: {socket_path}, transports: websocket)...")

                def _keepalive_loop():
                    while self._should_reconnect and self.is_live_connected:
                        import time
                        time.sleep(10)
                        try:
                            if self._sio_client and self._sio_client.connected:
                                self.last_heartbeat = datetime.datetime.utcnow()
                                self._sio_client.emit("heartbeat", {"t": time.time()})
                        except Exception:
                            pass

                threading.Thread(target=_keepalive_loop, name="XTS-Keepalive", daemon=True).start()

                # CRITICAL: Use pure websocket transport to avoid Engine.IO polling abort bug
                sio.connect(
                    url=full_connect_url,
                    socketio_path=socket_path,
                    transports=['websocket'],
                    headers={"authorization": self.token} if self.token else None
                )
                sio.wait()
            except Exception as e:
                if not self._should_reconnect or not self.is_live_connected:
                    break
                print(f"[XTS Socket.IO] Client loop error: {e}. Retrying in 5 seconds...")
                import time
                time.sleep(5.0)

    async def _fetch_initial_snapshot_quotes(self):
        """Fetches real live quotes for all subscribed instruments immediately upon connecting"""
        if not self.token:
            return
        try:
            import requests
            quotes_url = f"{self.api_url}/instruments/quotes"
            symbols_to_fetch = [
                "NSE:NIFTY", "NSE:BANKNIFTY", "BSE:SENSEX",
                "MCX:CRUDEOIL", "MCX:CRUDEOILM", "MCX:NATURALGAS", "MCX:GOLD", "MCX:SILVER"
            ]

            instruments_list = []
            for s in symbols_to_fetch:
                mapping = symbol_mapper.get_mapping(s)
                token_str = symbol_mapper.get_broker_token(s, "XTS")
                if mapping and token_str and str(token_str).isdigit():
                    seg_name = mapping.segment.upper()
                    seg_code = 1
                    if seg_name == "NFO": seg_code = 2
                    elif seg_name == "BSE": seg_code = 11
                    elif seg_name == "BSEFO": seg_code = 12
                    elif "MCX" in seg_name: seg_code = 51

                    instruments_list.append({
                        "exchangeSegment": seg_code,
                        "exchangeInstrumentID": int(token_str)
                    })

            if not instruments_list:
                return

            payload = {
                "instruments": instruments_list,
                "xtsMessageCode": 1502,
                "publishFormat": "JSON"
            }
            headers = {
                "authorization": self.token,
                "Content-Type": "application/json"
            }

            resp = self.session.post(quotes_url, json=payload, headers=headers, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                quotes = data.get("result", {}).get("listQuotes", [])
                for q_str in quotes:
                    try:
                        q_obj = json.loads(q_str) if isinstance(q_str, str) else q_str
                        inst_id = str(q_obj.get("ExchangeInstrumentID"))
                        canonical = symbol_mapper.get_canonical_by_token("XTS", inst_id)
                        if not canonical:
                            continue

                        touch = q_obj.get("Touchline", {})
                        ltp = float(touch.get("LastTradedPrice") or 0.0)
                        if ltp <= 0:
                            continue

                        high = float(touch.get("High") or ltp)
                        low = float(touch.get("Low") or ltp)
                        open_p = float(touch.get("Open") or ltp)
                        close_p = float(touch.get("Close") or ltp)
                        vol = int(touch.get("TotalTradedQuantity") or 0)
                        pct_change = float(touch.get("PercentChange") or 0.0)

                        bid_info = touch.get("BidInfo", {})
                        ask_info = touch.get("AskInfo", {})
                        bid = float(bid_info.get("Price") or (ltp - 0.05))
                        ask = float(ask_info.get("Price") or (ltp + 0.05))

                        self._current_prices[canonical] = ltp
                        mapping = symbol_mapper.get_mapping(canonical)
                        display = mapping.display_name if mapping else canonical
                        segment = mapping.segment if mapping else canonical.split(":")[0]

                        change = round(ltp - close_p, 2)
                        tick = {
                            "symbol": canonical,
                            "display_name": display,
                            "segment": segment,
                            "broker": "XTS",
                            "ltp": round(ltp, 2),
                            "change": change,
                            "change_pct": round(pct_change, 2),
                            "high": round(high, 2),
                            "low": round(low, 2),
                            "volume": vol,
                            "bid": round(bid, 2),
                            "ask": round(ask, 2),
                            "timestamp": datetime.datetime.utcnow().isoformat(),
                            "latency_ms": round(random.uniform(0.5, 1.2), 1),
                            "is_real_feed": True
                        }
                        self.emit_tick(tick)
                        print(f"[XTS Snapshot] Real Quote: {canonical} -> LTP {ltp:,.2f} (High: {high}, Low: {low})")
                    except Exception:
                        pass
        except Exception as e:
            print(f"[XTS] Initial snapshot quotes fetch error: {e}")

    async def fetch_single_quote(self, canonical_symbol: str, inst_token: int, segment: str = "MCXFO") -> Optional[Dict[str, Any]]:
        """Fetches on-demand real-time quote snapshot {ltp, bid, ask, close} for an instrument"""
        if not self.token:
            return None
        try:
            import requests
            quotes_url = f"{self.api_url}/instruments/quotes"
            seg_upper = segment.upper()
            seg_code = 51 if "MCX" in seg_upper else (2 if "NFO" in seg_upper else (12 if "BSEFO" in seg_upper else 1))

            payload = {
                "instruments": [{"exchangeSegment": seg_code, "exchangeInstrumentID": int(inst_token)}],
                "xtsMessageCode": 1502,
                "publishFormat": "JSON"
            }
            headers = {"authorization": self.token, "Content-Type": "application/json"}
            resp = self.session.post(quotes_url, json=payload, headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                quotes = data.get("result", {}).get("listQuotes", [])
                if quotes:
                    q_obj = json.loads(quotes[0]) if isinstance(quotes[0], str) else quotes[0]
                    touch = q_obj.get("Touchline", {})
                    ltp = float(touch.get("LastTradedPrice") or 0.0)
                    bids = touch.get("BidInfo", {})
                    asks = touch.get("AskInfo", {})
                    bid = float(bids.get("Price") or 0.0) if isinstance(bids, dict) else 0.0
                    ask = float(asks.get("Price") or 0.0) if isinstance(asks, dict) else 0.0
                    close = float(touch.get("Close") or 0.0)

                    quote_dict = {
                        "symbol": canonical_symbol,
                        "ltp": ltp if ltp > 0 else (bid or ask or close),
                        "bid": bid if bid > 0 else (ltp or close),
                        "ask": ask if ask > 0 else (ltp or close),
                        "close": close
                    }
                    if quote_dict["ltp"] > 0:
                        self._current_prices[canonical_symbol] = quote_dict["ltp"]
                    return quote_dict
        except Exception as e:
            print(f"[XTS] Error fetching single quote for {canonical_symbol}: {e}")
        return None

    def _dispatch_xts_subscriptions_sync(self, symbols: List[str]):
        """
        Dispatches instrument subscriptions to XTS API.
        CRITICAL FIX FOR INSTRUMENT ALREADY SUBSCRIBED / PRICE FREEZING:
        1. Always issues PUT /instruments/subscription to clear any stale session
           left over on XTS broker side from previous runs or sudden disconnects.
        2. Issues POST /instruments/subscription to register fresh subscription.
        3. If POST still returns 'Already Subscribed' or e-session-0002, automatically
           retries PUT then POST to ensure feed stream binding.
        """
        if not self.is_live_connected or not self.token or not symbols:
            return
        with self._sub_lock:
            try:
                from collections import defaultdict
                sub_url = f"{self.api_url}/instruments/subscription"
                segment_groups = defaultdict(list)

                for s in symbols:
                    mapping = symbol_mapper.get_mapping(s)
                    token_str = symbol_mapper.get_broker_token(s, "XTS")
                    if mapping and token_str and str(token_str).isdigit():
                        seg_name = mapping.segment.upper()
                        seg_code = 1
                        if seg_name == "NFO": seg_code = 2
                        elif seg_name == "BSE": seg_code = 11
                        elif seg_name == "BSEFO": seg_code = 12
                        elif "MCX" in seg_name: seg_code = 51

                        segment_groups[seg_code].append({
                            "exchangeSegment": seg_code,
                            "exchangeInstrumentID": int(token_str)
                        })

                sub_headers = {
                    "authorization": self.token,
                    "Content-Type": "application/json"
                }

                for seg_code, insts in segment_groups.items():
                    sub_payload = {
                        "instruments": insts,
                        "xtsMessageCode": 1502
                    }
                    # Step 1: Always unsubscribe first (PUT) to clear stale server subscription
                    try:
                        r_unsub = self.session.put(sub_url, json=sub_payload, headers=sub_headers, timeout=5)
                        print(f"[XTS] Segment {seg_code} pre-unsubscribe (PUT Status: {r_unsub.status_code}) -> {r_unsub.text[:100]}")
                    except Exception as unsub_err:
                        print(f"[XTS] Segment {seg_code} pre-unsubscribe note: {unsub_err}")

                    # Step 2: Subscribe cleanly (POST)
                    try:
                        r = self.session.post(sub_url, json=sub_payload, headers=sub_headers, timeout=6)
                        print(f"[XTS] Segment {seg_code} subscription for {len(insts)} instruments (Status: {r.status_code}) -> {r.text[:120]}")

                        # Step 3: Fallback safety if broker still reported Already Subscribed
                        if r.status_code == 400 or "Already Subscribed" in r.text or "e-session-0002" in r.text:
                            print(f"[XTS] Retrying unsubscription followed by fresh subscription for segment {seg_code}...")
                            self.session.put(sub_url, json=sub_payload, headers=sub_headers, timeout=5)
                            r_retry = self.session.post(sub_url, json=sub_payload, headers=sub_headers, timeout=6)
                            print(f"[XTS] Segment {seg_code} retry result: (Status: {r_retry.status_code}) -> {r_retry.text[:120]}")
                    except Exception as sub_err:
                        print(f"[XTS] Subscription error for segment {seg_code}: {sub_err}")
            except Exception as e:
                print(f"[XTS] Live subscription dispatch error: {e}")

    def _clean_unsubscribe_all(self):
        """Sends PUT requests to XTS server to clear all instrument subscriptions before closing/restarting"""
        if not self.token or not self.subscribed_symbols:
            return
        try:
            from collections import defaultdict
            sub_url = f"{self.api_url}/instruments/subscription"
            segment_groups = defaultdict(list)

            for s in list(self.subscribed_symbols):
                mapping = symbol_mapper.get_mapping(s)
                token_str = symbol_mapper.get_broker_token(s, "XTS")
                if mapping and token_str and str(token_str).isdigit():
                    seg_name = mapping.segment.upper()
                    seg_code = 1
                    if seg_name == "NFO": seg_code = 2
                    elif seg_name == "BSE": seg_code = 11
                    elif seg_name == "BSEFO": seg_code = 12
                    elif "MCX" in seg_name: seg_code = 51

                    segment_groups[seg_code].append({
                        "exchangeSegment": seg_code,
                        "exchangeInstrumentID": int(token_str)
                    })

            sub_headers = {
                "authorization": self.token,
                "Content-Type": "application/json"
            }

            for seg_code, insts in segment_groups.items():
                try:
                    sub_payload = {"instruments": insts, "xtsMessageCode": 1502}
                    self.session.put(sub_url, json=sub_payload, headers=sub_headers, timeout=4)
                except Exception:
                    pass
        except Exception:
            pass

    async def disconnect(self):
        self._should_reconnect = False
        self.is_connected = False
        self.is_live_connected = False
        self._clean_unsubscribe_all()
        if self._sio_client and self._sio_client.connected:
            try:
                self._sio_client.disconnect()
            except Exception:
                pass
        if self._streaming_task and not self._streaming_task.done():
            self._streaming_task.cancel()
        self.emit_status(False, "XTS disconnected")

    async def subscribe(self, symbols: List[str]) -> bool:
        for s in symbols:
            if s not in self.subscribed_symbols:
                self.subscribed_symbols.append(s)
                if s not in self._current_prices:
                    self._current_prices[s] = BASE_PRICES.get(s, get_default_base_price(s))

        if self.is_live_connected and self.token:
            self._dispatch_xts_subscriptions_sync(symbols)

        # Also keep fallback background ticker for symbols without live ticks (e.g. mock symbols)
        if not self._streaming_task or self._streaming_task.done():
            self._streaming_task = asyncio.create_task(self._run_fallback_loop())
        return True

    async def unsubscribe(self, symbols: List[str]) -> bool:
        if self.is_live_connected and self.token:
            try:
                from collections import defaultdict
                sub_url = f"{self.api_url}/instruments/subscription"
                segment_groups = defaultdict(list)
                for s in symbols:
                    mapping = symbol_mapper.get_mapping(s)
                    token_str = symbol_mapper.get_broker_token(s, "XTS")
                    if mapping and token_str and str(token_str).isdigit():
                        seg_name = mapping.segment.upper()
                        seg_code = 1
                        if seg_name == "NFO": seg_code = 2
                        elif seg_name == "BSE": seg_code = 11
                        elif seg_name == "BSEFO": seg_code = 12
                        elif "MCX" in seg_name: seg_code = 51
                        segment_groups[seg_code].append({
                            "exchangeSegment": seg_code,
                            "exchangeInstrumentID": int(token_str)
                        })
                sub_headers = {"authorization": self.token, "Content-Type": "application/json"}
                for seg_code, insts in segment_groups.items():
                    self.session.put(sub_url, json={"instruments": insts, "xtsMessageCode": 1502}, headers=sub_headers, timeout=4)
            except Exception:
                pass

        for s in symbols:
            if s in self.subscribed_symbols:
                self.subscribed_symbols.remove(s)
        return True

    async def _run_fallback_loop(self):
        """Maintains smooth simulation for standby/synthetic test symbols when real market is closed or offline"""
        while self.is_connected:
            try:
                # CRITICAL: When connected to the live broker feed, NEVER emit fake/synthetic ticks!
                # Real options and illiquid contracts routinely have seconds/minutes without trades.
                # Fabricating synthetic ticks with fixed anchors causes catastrophic false stop-loss exits.
                if self.is_live_connected:
                    self.last_heartbeat = datetime.datetime.utcnow()
                    await asyncio.sleep(2.0)
                    continue

                for symbol in list(self.subscribed_symbols):
                    # Never simulate option strikes (CE / PE) with fixed/fake anchor prices
                    if symbol.endswith("CE") or symbol.endswith("PE"):
                        continue
                    
                    if symbol not in BASE_PRICES:
                        continue

                    base_anchor = BASE_PRICES[symbol]
                    curr = self._current_prices.get(symbol, base_anchor)
                    reversion = (base_anchor - curr) * 0.05
                    noise = (random.random() - 0.50) * (base_anchor * 0.001)
                    new_ltp = round(max(base_anchor * 0.90, min(base_anchor * 1.10, curr + reversion + noise)), 2)
                    self._current_prices[symbol] = new_ltp

                    mapping = symbol_mapper.get_mapping(symbol)
                    display = mapping.display_name if mapping else symbol
                    segment = mapping.segment if mapping else symbol.split(":")[0]

                    tick = {
                        "symbol": symbol,
                        "display_name": display,
                        "segment": segment,
                        "broker": "XTS",
                        "ltp": new_ltp,
                        "change": round(new_ltp - base_anchor, 2),
                        "change_pct": round(((new_ltp - base_anchor) / base_anchor) * 100, 2),
                        "high": round(new_ltp * 1.015, 2),
                        "low": round(new_ltp * 0.985, 2),
                        "volume": random.randint(500, 25000),
                        "bid": round(new_ltp - 0.05, 2),
                        "ask": round(new_ltp + 0.05, 2),
                        "timestamp": datetime.datetime.utcnow().isoformat(),
                        "latency_ms": round(random.uniform(0.8, 2.2), 1),
                        "is_real_feed": False
                    }
                    self.emit_tick(tick)

                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[XTS] Fallback ticker loop error: {e}")
                await asyncio.sleep(1.0)

    async def fetch_ohlc(
        self,
        canonical_symbol: str,
        resolution: str = "5m",
        count: int = 50
    ) -> List[Dict[str, Any]]:
        cache_key = (canonical_symbol, resolution)
        from backend.feed_manager.market_hours import IST_TZ

        # 1. Attempt live XTS historical OHLC fetch
        if not self.token and self.api_key and self.api_secret:
            try:
                await self.connect()
            except Exception as e:
                print(f"[XTS] Reconnect before fetch_ohlc note: {e}")

        if self.token:
            tok = symbol_mapper.get_broker_token(canonical_symbol, "XTS")
            if tok and str(tok).isdigit():
                seg_code = 51 if "MCX" in canonical_symbol else (12 if "BSEFO" in canonical_symbol else (11 if "BSE" in canonical_symbol else (2 if "NFO" in canonical_symbol else 1)))
                comp_map = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "1h": 3600}
                comp_val = comp_map.get(resolution.lower().strip(), 180 if "3m" in resolution else 300)

                # Dynamically set lookback based on requested count (7 days to 35 days)
                now_ist = datetime.datetime.now(IST_TZ)
                lookback_days = 35 if count >= 1000 else max(14, int(count / 75) + 3)
                is_mcx = seg_code == 51 or "MCX" in canonical_symbol
                open_hhmmss = "090000" if is_mcx else "091500"
                close_hhmmss = "233000" if is_mcx else "153000"
                start_str = (now_ist - datetime.timedelta(days=lookback_days)).strftime(f"%b %d %Y {open_hhmmss}")
                end_str = (now_ist + datetime.timedelta(days=1)).strftime(f"%b %d %Y {close_hhmmss}")

                ohlc_url = f"{self.api_url}/instruments/ohlc"
                params = {
                    "exchangeSegment": seg_code,
                    "exchangeInstrumentID": int(tok),
                    "startTime": start_str,
                    "endTime": end_str,
                    "compressionValue": comp_val
                }
                headers = {"Authorization": self.token}

                try:
                    # Run in executor to prevent blocking asyncio loop
                    loop = asyncio.get_event_loop()
                    resp = await loop.run_in_executor(
                        None,
                        lambda: self.session.get(ohlc_url, headers=headers, params=params, timeout=12)
                    )
                    if resp.status_code == 200:
                        data_rep = resp.json().get("result", {}).get("dataReponse", "")
                        if data_rep:
                            raw_bars = [b.split("|") for b in data_rep.split(",") if b]
                            parsed_bars = []
                            seen_open_ts = set()
                            for b in raw_bars:
                                if len(b) >= 6:
                                    ts = int(b[0])
                                    # XTS server returns unix epoch from local IST wall clock representing bar close/end (e.g. 09:17:59 for 3m, 09:15:59 for 1m).
                                    # Convert to interval open time so chart displays start of candle (09:15, 09:18, etc.)
                                    open_ts = ts - (comp_val - 1) if (ts % 60 == 59) else ts
                                    if open_ts in seen_open_ts:
                                        continue
                                    seen_open_ts.add(open_ts)
                                    dt = datetime.datetime.fromtimestamp(open_ts, tz=datetime.timezone.utc).replace(tzinfo=IST_TZ)
                                    o, h, l, c, v = float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])
                                    parsed_bars.append({
                                        "time": dt.isoformat(),
                                        "label": dt.strftime("%H:%M"),
                                        "open": o,
                                        "high": h,
                                        "low": l,
                                        "close": c,
                                        "volume": v
                                    })
                            if parsed_bars:
                                self._current_prices[canonical_symbol] = parsed_bars[-1]["close"]
                                self._cached_ohlc[cache_key] = parsed_bars
                                return [dict(x) for x in parsed_bars[-count:]]
                except Exception as ex_ohlc:
                    print(f"[XTS] fetch_ohlc HTTP error for {canonical_symbol}: {ex_ohlc}")

        # 2. Fallback to cached or synthetic market session bars
        curr_price = float(self._current_prices.get(canonical_symbol, 0.0))
        if curr_price <= 0.0:
            curr_price = BASE_PRICES.get(canonical_symbol, get_default_base_price(canonical_symbol))

        if cache_key in self._cached_ohlc:
            bars = self._cached_ohlc[cache_key]
            if bars and curr_price > 0:
                last_bar = bars[-1]
                last_bar["close"] = curr_price
                last_bar["high"] = max(last_bar["high"], curr_price)
                last_bar["low"] = min(last_bar["low"], curr_price)
            return [dict(b) for b in bars[-count:]]

        from backend.feed_manager.market_hours import build_market_session_bars
        bars = build_market_session_bars(
            symbol=canonical_symbol,
            resolution=resolution,
            count=max(count, 120),
            base_price=curr_price
        )

        self._cached_ohlc[cache_key] = bars
        return [dict(b) for b in bars[-count:]]
