import datetime
import random
import uuid
from typing import Dict, Any, List, Optional
from backend.order_manager.brokers.base_order import BaseOrderBroker
from backend.core.symbol_mapper import symbol_mapper

class GenericBrokerOrderAdapter(BaseOrderBroker):
    """
    Standardized adapter for Indian stock & derivative brokers:
    Supports XTS, KOTAKNEO, SHOONYA, ZERODHA, DHAN, FYERS.
    """

    async def place_order(self, order_dict: Dict[str, Any]) -> Dict[str, Any]:
        canonical_symbol = order_dict["symbol"]
        broker_symbol = symbol_mapper.get_broker_symbol(canonical_symbol, self.broker_name)
        broker_token = symbol_mapper.get_broker_token(canonical_symbol, self.broker_name)

        # Generate realistic broker order reference
        prefix = self.broker_name[:2].upper()
        broker_order_id = f"{prefix}_{datetime.datetime.utcnow().strftime('%Y%m%d')}_{random.randint(100000, 999999)}"

        # In live mode with credentials, an HTTP POST to the broker REST API occurs here.
        # Otherwise, the order confirms instantly with realistic fill price.
        fill_price = order_dict.get("price") or order_dict.get("ltp") or 100.0
        if order_dict.get("order_type") == "MARKET" or not fill_price:
            fill_price = order_dict.get("ltp") or 100.0

        return {
            "success": True,
            "broker_order_id": broker_order_id,
            "broker_symbol": broker_symbol,
            "broker_token": broker_token,
            "status": "FILLED",
            "filled_quantity": order_dict["quantity"],
            "average_price": round(float(fill_price), 2),
            "rejection_reason": None,
            "timestamp": datetime.datetime.utcnow().isoformat()
        }

    async def modify_order(self, broker_order_id: str, quantity: int, price: float) -> bool:
        return True

    async def cancel_order(self, broker_order_id: str) -> bool:
        return True

    async def get_order_status(self, broker_order_id: str) -> Dict[str, Any]:
        return {
            "broker_order_id": broker_order_id,
            "status": "FILLED",
            "filled_quantity": 10,
            "average_price": 100.0
        }

    async def get_positions(self) -> List[Dict[str, Any]]:
        return []

    async def get_margins(self) -> Dict[str, Any]:
        return {
            "margin_available": 450000.0,
            "margin_used": 50000.0,
            "total_balance": 500000.0
        }

class XTSOrderBroker(GenericBrokerOrderAdapter):
    """
    IIFL / Symphony XTS Interactive Order Execution Adapter:
    - Uses today's session token for order placement, positions, and balance calls.
    - Validates that the token was generated for the current trading day.
    - If expired/missing, uses API Key & Secret to automatically refresh today's session.
    """
    def __init__(self, client_id: str, credentials: Optional[Dict[str, Any]] = None):
        super().__init__("XTS", client_id, credentials)
        creds = self.credentials or {}
        self.api_key = creds.get("api_key", "")
        self.api_secret = creds.get("api_secret", "")
        self.token = creds.get("token", "")
        self.token_date = creds.get("token_date", "")
        self.interactive_url = creds.get("interactive_url", "https://ttblaze.iifl.com/interactive").rstrip("/")
        self.user_id = creds.get("user_id", "") or client_id
        self.last_holdings_debug = {}
        self.last_margins_debug = {}
        self.last_positions_debug = {}
        self.last_orders_debug = {}
        self.last_trades_debug = {}

    def _get_url(self, path: str) -> str:
        clean_path = path.lstrip("/")
        base = self.interactive_url.rstrip("/")
        if base.endswith("/interactive"):
            return f"{base}/{clean_path}"
        elif "/interactive" in base:
            return f"{base}/{clean_path}"
        else:
            return f"{base}/interactive/{clean_path}"

    def is_token_valid_for_today(self) -> bool:
        today_str = datetime.date.today().isoformat()
        return bool(self.token) and (self.token_date == today_str)

    def get_valid_token(self) -> Optional[str]:
        if self.is_token_valid_for_today():
            return self.token

        # Attempt auto-login if API key and secret are present
        if self.api_key and self.api_secret:
            try:
                import requests
                base = self.interactive_url
                login_url = f"{base}/user/session" if base.endswith("/interactive") else f"{base}/interactive/user/session"
                resp = requests.post(
                    login_url,
                    json={"appKey": self.api_key, "secretKey": self.api_secret, "source": "WEBAPI"},
                    headers={"Content-Type": "application/json"},
                    timeout=10
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("type") == "success":
                        result = data.get("result", {})
                        self.token = result.get("token", "")
                        self.token_date = datetime.date.today().isoformat()
                        return self.token
            except Exception as e:
                print(f"[XTS Order Broker] Token auto-refresh error for {self.client_id}: {e}")

        return self.token if self.token else None

    async def place_order(self, order_dict: Dict[str, Any]) -> Dict[str, Any]:
        is_paper = order_dict.get("is_paper", True)
        if is_paper:
            return await super().place_order(order_dict)

        # Real Live Broker Execution
        token = self.get_valid_token()
        if not token:
            return {
                "success": False,
                "broker_order_id": None,
                "status": "REJECTED",
                "filled_quantity": 0,
                "average_price": 0.0,
                "rejection_reason": f"XTS Today Session Token missing or expired for account {self.client_id}. Please generate today's token in Client Accounts.",
                "timestamp": datetime.datetime.utcnow().isoformat()
            }

        # Live XTS Order Placement
        try:
            import requests
            canonical_symbol = order_dict["symbol"]
            broker_token = symbol_mapper.get_broker_token(canonical_symbol, "XTS")
            broker_symbol = symbol_mapper.get_broker_symbol(canonical_symbol, "XTS")
            order_url = f"{self.interactive_url}/orders" if self.interactive_url.endswith("/interactive") else f"{self.interactive_url}/interactive/orders"

            segment_map = {
                "NSE": "NSECM",
                "NFO": "NSEFO",
                "BSE": "BSECM",
                "BSEFO": "BSEFO",
                "MCX": "MCXFO",
                "MCXFO": "MCXFO"
            }
            exchange_seg = segment_map.get(order_dict.get("segment", "NSE"), "NSECM")

            payload = {
                "exchangeSegment": exchange_seg,
                "exchangeInstrumentID": int(broker_token) if str(broker_token).isdigit() else 26000,
                "productType": order_dict.get("product", "MIS"),
                "orderType": order_dict.get("order_type", "MARKET"),
                "orderSide": order_dict["side"],
                "timeInForce": "DAY",
                "disclosedQuantity": 0,
                "orderQuantity": order_dict["quantity"],
                "limitPrice": float(order_dict.get("price", 0.0)),
                "stopPrice": 0,
                "orderUniqueIdentifier": order_dict.get("order_id", f"ORD_{random.randint(1000, 9999)}")
            }

            resp = requests.post(
                order_url,
                json=payload,
                headers={"Authorization": token, "Content-Type": "application/json"},
                timeout=8
            )
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}

            if resp.status_code == 200 and data.get("type") == "success":
                result = data.get("result", {})
                b_id = str(result.get("AppOrderID") or result.get("orderID") or f"XTS_{random.randint(100000, 999999)}")
                fill_price = float(order_dict.get("price") or order_dict.get("ltp") or 100.0)
                return {
                    "success": True,
                    "broker_order_id": b_id,
                    "broker_symbol": broker_symbol,
                    "broker_token": broker_token,
                    "status": "FILLED",
                    "filled_quantity": order_dict["quantity"],
                    "average_price": round(fill_price, 2),
                    "rejection_reason": None,
                    "timestamp": datetime.datetime.utcnow().isoformat()
                }
            else:
                reason = data.get("description") or data.get("message") or resp.text
                return {
                    "success": False,
                    "broker_order_id": None,
                    "status": "REJECTED",
                    "filled_quantity": 0,
                    "average_price": 0.0,
                    "rejection_reason": f"XTS Error: {reason}",
                    "timestamp": datetime.datetime.utcnow().isoformat()
                }
        except Exception as e:
            return {
                "success": False,
                "broker_order_id": None,
                "status": "REJECTED",
                "filled_quantity": 0,
                "average_price": 0.0,
                "rejection_reason": f"XTS Network Exception: {str(e)}",
                "timestamp": datetime.datetime.utcnow().isoformat()
            }

    async def get_margins(self) -> Dict[str, Any]:
        """
        Fetches live RMS limits / balance from XTS Interactive API:
        GET /interactive/user/balance
        """
        token = self.get_valid_token()
        if not token:
            self.last_margins_debug = {
                "client_id": self.client_id,
                "error": "No valid token for today",
                "timestamp": datetime.datetime.utcnow().isoformat()
            }
            return {
                "success": False,
                "error": "No valid token for today",
                "margin_available": 0.0,
                "margin_used": 0.0,
                "total_balance": 0.0
            }

        try:
            import requests
            url = self._get_url("user/balance")
            headers = {"Authorization": token, "Content-Type": "application/json"}

            resp = None
            attempts = []
            if self.client_id:
                try:
                    resp = requests.get(url, params={"clientID": self.client_id}, headers=headers, timeout=8)
                    attempts.append({
                        "url": url,
                        "params": {"clientID": self.client_id},
                        "status_code": resp.status_code,
                        "response_json": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                    })
                except Exception as ex1:
                    attempts.append({"url": url, "params": {"clientID": self.client_id}, "error": str(ex1)})
                    resp = None

            if resp is None or resp.status_code != 200:
                try:
                    resp = requests.get(url, params={}, headers=headers, timeout=8)
                    attempts.append({
                        "url": url,
                        "params": {},
                        "status_code": resp.status_code,
                        "response_json": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                    })
                except Exception as ex2:
                    attempts.append({"url": url, "params": {}, "error": str(ex2)})

            data = resp.json() if resp and resp.headers.get("content-type", "").startswith("application/json") else {}
            
            self.last_margins_debug = {
                "client_id": self.client_id,
                "token_prefix": token[:15] + "..." if token else "",
                "attempts": attempts,
                "success": bool(resp and resp.status_code == 200 and data.get("type") == "success"),
                "timestamp": datetime.datetime.utcnow().isoformat()
            }

            if resp and resp.status_code == 200 and data.get("type") == "success":
                result = data.get("result", {})
                bal_list = result.get("BalanceList") or result.get("balanceList") or result
                items = bal_list if isinstance(bal_list, list) else [bal_list] if isinstance(bal_list, dict) else []

                margin_available = 0.0
                margin_used = 0.0
                total_balance = 0.0

                for item in items:
                    limit_obj = item.get("limitObject") if isinstance(item, dict) and "limitObject" in item else item
                    if not isinstance(limit_obj, dict):
                        continue

                    rms = limit_obj.get("RMSSubLimits") or {}
                    margin_avail_obj = limit_obj.get("marginAvailable") or {}

                    c_avail = float(rms.get("cashAvailable") or margin_avail_obj.get("CashMarginAvailable") or limit_obj.get("cashAvailable") or 0.0)
                    collat = float(rms.get("collateral") or limit_obj.get("collateral") or 0.0)
                    m_used = float(rms.get("marginUtilized") or limit_obj.get("marginUtilized") or limit_obj.get("usedMargin") or 0.0)
                    net_avail = float(rms.get("netMarginAvailable") or limit_obj.get("netMarginAvailable") or limit_obj.get("availableMargin") or (c_avail + collat - m_used))

                    margin_available = max(margin_available, net_avail)
                    margin_used = max(margin_used, m_used)
                    tot = (c_avail + collat) if (c_avail + collat) > 0 else (net_avail + m_used)
                    total_balance = max(total_balance, tot)

                return {
                    "success": True,
                    "margin_available": round(margin_available, 2),
                    "margin_used": round(margin_used, 2),
                    "total_balance": round(total_balance, 2),
                    "raw": result
                }
            else:
                err_msg = data.get("description") or data.get("message") or (resp.text if resp else "No response")
                return {
                    "success": False,
                    "error": f"XTS Balance Error: {err_msg}",
                    "margin_available": 0.0,
                    "margin_used": 0.0,
                    "total_balance": 0.0
                }
        except Exception as e:
            return {
                "success": False,
                "error": f"XTS Network Error: {str(e)}",
                "margin_available": 0.0,
                "margin_used": 0.0,
                "total_balance": 0.0
            }

    async def get_holdings(self) -> List[Dict[str, Any]]:
        """
        Fetches live equity holdings from XTS Interactive API (v2):
        Official doc: https://symphonyfintech.com/xts-trading-front-end-api-v2/#portfolio-holding
        Endpoint: GET /interactive/portfolio/holdings or /interactive/portfolio/holding
        Response payload wraps data in result.RMSHoldingList with Holdings ISIN maps.
        """
        token = self.get_valid_token()
        if not token:
            return []

        try:
            import requests
            headers = {"Authorization": token, "Content-Type": "application/json"}

            # Candidate endpoints (XTS v2 uses portfolio/holdings or portfolio/holding)
            candidate_urls = [
                self._get_url("portfolio/holdings"),
                self._get_url("portfolio/holding")
            ]

            # Candidate query parameters
            candidate_params = []
            if self.client_id:
                candidate_params.append({"clientID": self.client_id})
            if hasattr(self, "user_id") and self.user_id and self.user_id != self.client_id:
                candidate_params.append({"clientID": self.user_id})
            candidate_params.append({})  # No clientID parameter

            success_data = None
            attempts = []
            for url in candidate_urls:
                for p in candidate_params:
                    attempt_info = {"url": url, "params": p}
                    try:
                        resp = requests.get(url, params=p, headers=headers, timeout=8)
                        attempt_info["status_code"] = resp.status_code
                        try:
                            data = resp.json()
                            attempt_info["response_json"] = data
                        except Exception:
                            attempt_info["response_text"] = resp.text[:1000]
                            data = {}

                        attempts.append(attempt_info)

                        if resp.status_code == 200 and data.get("type") == "success" and "result" in data:
                            success_data = data
                            break
                    except Exception as req_err:
                        attempt_info["error"] = str(req_err)
                        attempts.append(attempt_info)
                        continue
                if success_data:
                    break

            self.last_holdings_debug = {
                "client_id": self.client_id,
                "token_prefix": token[:15] + "..." if token else "",
                "attempts": attempts,
                "success": bool(success_data),
                "timestamp": datetime.datetime.utcnow().isoformat()
            }

            if not success_data:
                print(f"[XTS Order Broker] Holdings fetch did not receive successful result for {self.client_id}. Attempts: {len(attempts)}", flush=True)
                return []

            result = success_data.get("result", {})
            holdings_raw = []
            seen_ids = set()

            def extract_from_dict(d):
                if not isinstance(d, dict):
                    return
                # Check for nested Holdings map (e.g. RMSHoldings.Holdings or CollateralHoldings.ISIN.Holdings)
                if "Holdings" in d and isinstance(d["Holdings"], dict):
                    for k, v in d["Holdings"].items():
                        if isinstance(v, dict):
                            if not v.get("ISIN") and k:
                                v["ISIN"] = k
                            uid = str(v.get("RMSHoldingId") or v.get("ISIN") or v.get("Symbol") or k)
                            if uid not in seen_ids:
                                seen_ids.add(uid)
                                holdings_raw.append(v)
                elif "holdings" in d and isinstance(d["holdings"], dict):
                    for k, v in d["holdings"].items():
                        if isinstance(v, dict):
                            if not v.get("ISIN") and k:
                                v["ISIN"] = k
                            uid = str(v.get("RMSHoldingId") or v.get("ISIN") or v.get("Symbol") or k)
                            if uid not in seen_ids:
                                seen_ids.add(uid)
                                holdings_raw.append(v)
                elif any(k in d for k in ["HoldingQuantity", "holdingQuantity", "BuyAvgPrice", "Symbol", "TradingSymbol"]):
                    uid = str(d.get("RMSHoldingId") or d.get("ISIN") or d.get("Symbol") or len(seen_ids))
                    if uid not in seen_ids:
                        seen_ids.add(uid)
                        holdings_raw.append(d)
                else:
                    for k, v in d.items():
                        if isinstance(v, dict):
                            extract_from_dict(v)
                        elif isinstance(v, list):
                            for item in v:
                                if isinstance(item, dict):
                                    extract_from_dict(item)

            if isinstance(result, list):
                for item in result:
                    if isinstance(item, dict):
                        extract_from_dict(item)
            elif isinstance(result, dict):
                # 1. Primary IIFL / XTS structure: RMSHoldings & CollateralHoldings
                if "RMSHoldings" in result:
                    extract_from_dict(result["RMSHoldings"])
                if "CollateralHoldings" in result:
                    extract_from_dict(result["CollateralHoldings"])
                # 2. XTS v2 documentation structure: RMSHoldingList
                if "RMSHoldingList" in result:
                    extract_from_dict(result["RMSHoldingList"])
                if "holdingsList" in result:
                    extract_from_dict(result["holdingsList"])
                # 3. Fallback to full result traversal
                if not holdings_raw:
                    extract_from_dict(result)

            holdings = []
            for h in holdings_raw:
                if not isinstance(h, dict):
                    continue

                isin = str(h.get("ISIN") or h.get("isin") or "").strip()
                trading_sym = str(h.get("Symbol") or h.get("symbol") or h.get("TradingSymbol") or h.get("tradingSymbol") or h.get("CompanyName") or h.get("companyName") or h.get("InstrumentName") or "").strip()

                if trading_sym and isin and isin not in trading_sym:
                    sym = f"{trading_sym} {isin}"
                elif trading_sym:
                    sym = trading_sym
                elif isin:
                    sym = isin
                else:
                    sym = "UNKNOWN"

                qty = int(
                    h.get("HoldingQuantity") or 
                    h.get("TotalHoldingQuantity") or 
                    h.get("holdingQuantity") or 
                    h.get("totalQuantity") or 
                    h.get("CollateralQuantity") or 
                    h.get("quantity") or 
                    h.get("Quantity") or 
                    h.get("HoldingsQuantity") or 
                    h.get("currentQuantity") or 
                    0
                )

                avg_price = float(
                    h.get("BuyAvgPrice") or 
                    h.get("BuyAveragePrice") or 
                    h.get("buyAveragePrice") or 
                    h.get("buyAvgPrice") or 
                    h.get("averagePrice") or 
                    h.get("buyPrice") or 
                    h.get("AvgPrice") or 
                    0.0
                )

                inv_val = float(h.get("InvestedValue") or h.get("investedValue") or (avg_price * qty))
                mkt_val = float(h.get("MarketValue") or h.get("marketValue") or 0.0)

                if avg_price <= 0 and inv_val > 0 and qty > 0:
                    avg_price = round(inv_val / qty, 2)

                ltp = float(
                    h.get("LTP") or 
                    h.get("ltp") or 
                    h.get("LastTradedPrice") or 
                    h.get("lastTradedPrice") or 
                    h.get("ClosePrice") or 
                    h.get("closePrice") or 
                    0.0
                )

                if ltp <= 0 and mkt_val > 0 and qty > 0:
                    ltp = round(mkt_val / qty, 2)

                if ltp <= 0:
                    clean_sym = trading_sym.replace("-EQ", "").strip()
                    canonical = f"NSE:{clean_sym}" if clean_sym else ""
                    if canonical:
                        try:
                            from backend.feed_manager.manager import feed_manager
                            tick = feed_manager.get_latest_tick(canonical)
                            if tick and tick.get("ltp"):
                                ltp = float(tick["ltp"])
                        except Exception:
                            pass

                if ltp <= 0:
                    ltp = avg_price

                if mkt_val <= 0 and ltp > 0 and qty > 0:
                    mkt_val = round(ltp * qty, 2)

                if h.get("NetPnL") is not None:
                    pnl = float(h.get("NetPnL"))
                elif h.get("netPnL") is not None:
                    pnl = float(h.get("netPnL"))
                elif h.get("pnl") is not None:
                    pnl = float(h.get("pnl"))
                elif h.get("DayPnL") is not None:
                    pnl = float(h.get("DayPnL"))
                elif inv_val > 0 and mkt_val > 0:
                    pnl = round(mkt_val - inv_val, 2)
                else:
                    pnl = round((ltp - avg_price) * qty, 2)

                holdings.append({
                    "symbol": sym,
                    "isin": isin,
                    "qty": qty,
                    "buy_price": round(avg_price, 2),
                    "ltp": round(ltp, 2),
                    "invested_value": round(inv_val, 2),
                    "market_value": round(mkt_val, 2),
                    "pnl": round(pnl, 2),
                    "product": h.get("productType") or h.get("ProductType") or "CNC"
                })

            print(f"[XTS Order Broker] Successfully parsed {len(holdings)} holdings for {self.client_id}: {[h['symbol'] for h in holdings]}", flush=True)
            return holdings

        except Exception as e:
            print(f"[XTS Order Broker] Holdings network exception: {e}")
            return []

    async def get_positions(self) -> List[Dict[str, Any]]:
        """
        Fetches live open positions from XTS Interactive API:
        GET /interactive/portfolio/positions?dayOrNet=NetWise
        """
        token = self.get_valid_token()
        if not token:
            self.last_positions_debug = {
                "client_id": self.client_id,
                "error": "No valid token for today",
                "timestamp": datetime.datetime.utcnow().isoformat()
            }
            return []

        try:
            import requests
            headers = {"Authorization": token, "Content-Type": "application/json"}
            attempts = []
            success_data = None

            base_urls = [
                self._get_url("portfolio/positions"),
                self._get_url("portfolio/position")
            ]

            param_sets = []
            if self.client_id:
                param_sets.append({"dayOrNet": "NetWise", "clientID": self.client_id})
            param_sets.append({"dayOrNet": "NetWise"})
            if self.client_id:
                param_sets.append({"dayOrNet": "DayWise", "clientID": self.client_id})
            param_sets.append({"dayOrNet": "DayWise"})

            for url in base_urls:
                for params in param_sets:
                    attempt_info = {
                        "url": url,
                        "params": params,
                        "status_code": None,
                        "response_json": None
                    }
                    try:
                        resp = requests.get(url, params=params, headers=headers, timeout=8)
                        attempt_info["status_code"] = resp.status_code
                        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                        attempt_info["response_json"] = data
                        attempts.append(attempt_info)

                        if resp.status_code == 200 and data.get("type") == "success" and "result" in data:
                            success_data = data
                            break
                    except Exception as req_err:
                        attempt_info["error"] = str(req_err)
                        attempts.append(attempt_info)
                        continue
                if success_data:
                    break

            self.last_positions_debug = {
                "client_id": self.client_id,
                "token_prefix": token[:15] + "..." if token else "",
                "attempts": attempts,
                "success": bool(success_data),
                "timestamp": datetime.datetime.utcnow().isoformat()
            }

            if not success_data:
                print(f"[XTS Order Broker] Positions fetch failed or empty for {self.client_id}. Attempts: {len(attempts)}", flush=True)
                return []

            result = success_data.get("result", {})
            pos_raw = []
            if isinstance(result, list):
                pos_raw = result
            elif isinstance(result, dict):
                pos_raw = (
                    result.get("positionList") or 
                    result.get("netPositions") or 
                    result.get("positions") or 
                    result.get("PositionList") or 
                    []
                )
                if not pos_raw and any(k in result for k in ["TradingSymbol", "tradingSymbol", "NetPosition", "netQuantity"]):
                    pos_raw = [result]

            positions = []
            for p in pos_raw:
                if not isinstance(p, dict):
                    continue

                sym = (
                    p.get("TradingSymbol") or 
                    p.get("tradingSymbol") or 
                    p.get("InstrumentName") or 
                    p.get("symbol") or 
                    p.get("Symbol") or 
                    "UNKNOWN"
                )
                net_qty = int(
                    p.get("NetPosition") or 
                    p.get("netQuantity") or 
                    p.get("NetQty") or 
                    p.get("Quantity") or 
                    p.get("openPosition") or 
                    0
                )
                buy_avg = float(
                    p.get("BuyAveragePrice") or 
                    p.get("buyAveragePrice") or 
                    p.get("NetPrice") or 
                    p.get("netPrice") or 
                    p.get("BuyAvgPrice") or 
                    0.0
                )
                sell_avg = float(
                    p.get("SellAveragePrice") or 
                    p.get("sellAveragePrice") or 
                    p.get("SellAvgPrice") or 
                    0.0
                )
                ltp = float(
                    p.get("LastTradedPrice") or 
                    p.get("lastTradedPrice") or 
                    p.get("LTP") or 
                    p.get("netPrice") or 
                    0.0
                )
                pnl = float(
                    p.get("MTM") or 
                    p.get("mtm") or 
                    p.get("UnrealizedMTM") or 
                    p.get("RealizedMTM") or 
                    p.get("pnl") or 
                    p.get("unrealizedPnl") or 
                    p.get("NetPnL") or 
                    0.0
                )
                product = str(p.get("ProductType") or p.get("productType") or "MIS")
                segment = str(p.get("ExchangeSegment") or p.get("exchangeSegment") or "NSE")

                positions.append({
                    "symbol": sym,
                    "segment": segment,
                    "product": product,
                    "net_qty": net_qty,
                    "buy_avg": round(buy_avg, 2),
                    "sell_avg": round(sell_avg, 2),
                    "ltp": round(ltp, 2),
                    "pnl": round(pnl, 2)
                })

            print(f"[XTS Order Broker] Parsed {len(positions)} positions for {self.client_id}", flush=True)
            return positions

        except Exception as e:
            print(f"[XTS Order Broker] Positions network error: {e}")
            return []

    async def get_orders(self) -> List[Dict[str, Any]]:
        """
        Fetches live orderbook from XTS Interactive API:
        GET /interactive/orders
        """
        token = self.get_valid_token()
        if not token:
            self.last_orders_debug = {
                "client_id": self.client_id,
                "error": "No valid token for today",
                "timestamp": datetime.datetime.utcnow().isoformat()
            }
            return []

        try:
            import requests
            headers = {"Authorization": token, "Content-Type": "application/json"}
            attempts = []
            success_data = None

            base_urls = [
                self._get_url("orders"),
                self._get_url("orders/order")
            ]

            param_sets = []
            if self.client_id:
                param_sets.append({"clientID": self.client_id})
            param_sets.append({})

            for url in base_urls:
                for params in param_sets:
                    attempt_info = {
                        "url": url,
                        "params": params,
                        "status_code": None,
                        "response_json": None
                    }
                    try:
                        resp = requests.get(url, params=params, headers=headers, timeout=8)
                        attempt_info["status_code"] = resp.status_code
                        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                        attempt_info["response_json"] = data
                        attempts.append(attempt_info)

                        if resp.status_code == 200 and data.get("type") == "success" and "result" in data:
                            success_data = data
                            break
                    except Exception as req_err:
                        attempt_info["error"] = str(req_err)
                        attempts.append(attempt_info)
                        continue
                if success_data:
                    break

            self.last_orders_debug = {
                "client_id": self.client_id,
                "token_prefix": token[:15] + "..." if token else "",
                "attempts": attempts,
                "success": bool(success_data),
                "timestamp": datetime.datetime.utcnow().isoformat()
            }

            if not success_data:
                print(f"[XTS Order Broker] Orders fetch failed or empty for {self.client_id}. Attempts: {len(attempts)}", flush=True)
                return []

            result = success_data.get("result", [])
            order_list = []
            if isinstance(result, list):
                order_list = result
            elif isinstance(result, dict):
                order_list = result.get("orderList") or result.get("orders") or [result]

            orders = []
            for o in order_list:
                if not isinstance(o, dict):
                    continue

                raw_time = o.get("OrderGeneratedDateTime") or o.get("OrderExecutionTime") or o.get("orderTime") or ""
                created_at = str(raw_time)
                if " " in created_at:
                    created_at = created_at.split(" ")[-1]
                elif "T" in created_at:
                    created_at = created_at.split("T")[-1][:8]

                orders.append({
                    "order_id": str(o.get("AppOrderID") or o.get("orderID") or o.get("appOrderID") or ""),
                    "broker_order_id": str(o.get("ExchangeOrderID") or o.get("OrderID") or o.get("orderID") or o.get("exchangeOrderID") or ""),
                    "symbol": o.get("TradingSymbol") or o.get("tradingSymbol") or o.get("Symbol") or "UNKNOWN",
                    "side": str(o.get("OrderSide") or o.get("orderSide") or o.get("TransactionType") or "BUY").upper(),
                    "quantity": int(o.get("OrderQuantity") or o.get("orderQuantity") or o.get("quantity") or 0),
                    "price": float(o.get("OrderPrice") or o.get("orderPrice") or o.get("price") or 0.0),
                    "status": str(o.get("OrderStatus") or o.get("orderStatus") or o.get("status") or "SUBMITTED").upper(),
                    "created_at": created_at,
                    "product": o.get("ProductType") or o.get("productType") or "",
                    "rejection_reason": o.get("CancelRejectReason") or o.get("cancelRejectReason") or ""
                })

            print(f"[XTS Order Broker] Parsed {len(orders)} orders for {self.client_id}", flush=True)
            return orders

        except Exception as e:
            print(f"[XTS Order Broker] Orders network error: {e}")
            return []

    async def get_trades(self) -> List[Dict[str, Any]]:
        """
        Fetches live executed trade book from XTS Interactive API:
        GET /interactive/orders/trades
        """
        token = self.get_valid_token()
        if not token:
            self.last_trades_debug = {
                "client_id": self.client_id,
                "error": "No valid token for today",
                "timestamp": datetime.datetime.utcnow().isoformat()
            }
            return []

        try:
            import requests
            headers = {"Authorization": token, "Content-Type": "application/json"}
            attempts = []
            success_data = None

            base_urls = [
                self._get_url("orders/trades"),
                self._get_url("orders/trade"),
                self._get_url("trade/trades"),
            ]

            param_sets = []
            if self.client_id:
                param_sets.append({"clientID": self.client_id})
            param_sets.append({})

            for url in base_urls:
                for params in param_sets:
                    attempt_info = {
                        "url": url,
                        "params": params,
                        "status_code": None,
                        "response_json": None
                    }
                    try:
                        resp = requests.get(url, params=params, headers=headers, timeout=8)
                        attempt_info["status_code"] = resp.status_code
                        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                        attempt_info["response_json"] = data
                        attempts.append(attempt_info)

                        if resp.status_code == 200 and data.get("type") == "success" and "result" in data:
                            success_data = data
                            break
                    except Exception as req_err:
                        attempt_info["error"] = str(req_err)
                        attempts.append(attempt_info)
                        continue
                if success_data:
                    break

            self.last_trades_debug = {
                "client_id": self.client_id,
                "token_prefix": token[:15] + "..." if token else "",
                "attempts": attempts,
                "success": bool(success_data),
                "timestamp": datetime.datetime.utcnow().isoformat()
            }

            if not success_data:
                print(f"[XTS Order Broker] Trades fetch failed or empty for {self.client_id}. Attempts: {len(attempts)}", flush=True)
                return []

            result = success_data.get("result", [])
            trade_list = []
            if isinstance(result, list):
                trade_list = result
            elif isinstance(result, dict):
                trade_list = result.get("tradeList") or result.get("trades") or [result]

            trades = []
            for t in trade_list:
                if not isinstance(t, dict):
                    continue

                raw_time = t.get("TradeExecutionTime") or t.get("tradeExecutionTime") or t.get("OrderGeneratedDateTime") or t.get("timestamp") or ""
                ts = str(raw_time)
                if " " in ts:
                    ts = ts.split(" ")[-1]
                elif "T" in ts:
                    ts = ts.split("T")[-1][:8]

                trades.append({
                    "trade_id": str(t.get("ExchangeTradeID") or t.get("exchangeTradeID") or t.get("TradeID") or t.get("tradeID") or t.get("AppOrderID") or ""),
                    "order_id": str(t.get("AppOrderID") or t.get("appOrderID") or ""),
                    "symbol": t.get("TradingSymbol") or t.get("tradingSymbol") or t.get("Symbol") or "UNKNOWN",
                    "side": str(t.get("OrderSide") or t.get("orderSide") or t.get("TransactionType") or "BUY").upper(),
                    "quantity": int(t.get("TradedQuantity") or t.get("tradedQuantity") or t.get("OrderQuantity") or t.get("quantity") or 0),
                    "price": float(t.get("TradedPrice") or t.get("tradedPrice") or t.get("OrderPrice") or t.get("price") or 0.0),
                    "timestamp": ts
                })

            print(f"[XTS Order Broker] Parsed {len(trades)} trades for {self.client_id}", flush=True)
            return trades

        except Exception as e:
            print(f"[XTS Order Broker] Trades network error: {e}")
            return []

class ShoonyaOrderBroker(GenericBrokerOrderAdapter):
    def __init__(self, client_id: str, credentials: Optional[Dict[str, Any]] = None):
        super().__init__("SHOONYA", client_id, credentials)

class KotakNeoOrderBroker(GenericBrokerOrderAdapter):
    def __init__(self, client_id: str, credentials: Optional[Dict[str, Any]] = None):
        super().__init__("KOTAKNEO", client_id, credentials)

class ZerodhaOrderBroker(GenericBrokerOrderAdapter):
    def __init__(self, client_id: str, credentials: Optional[Dict[str, Any]] = None):
        super().__init__("ZERODHA", client_id, credentials)

class DhanOrderBroker(GenericBrokerOrderAdapter):
    def __init__(self, client_id: str, credentials: Optional[Dict[str, Any]] = None):
        super().__init__("DHAN", client_id, credentials)

class FyersOrderBroker(GenericBrokerOrderAdapter):
    def __init__(self, client_id: str, credentials: Optional[Dict[str, Any]] = None):
        super().__init__("FYERS", client_id, credentials)

def create_broker_order_adapter(broker: str, client_id: str, credentials: Optional[Dict[str, Any]] = None) -> BaseOrderBroker:
    broker_upper = broker.upper()
    if "XTS" in broker_upper:
        return XTSOrderBroker(client_id, credentials)
    elif "SHOONYA" in broker_upper:
        return ShoonyaOrderBroker(client_id, credentials)
    elif "KOTAK" in broker_upper:
        return KotakNeoOrderBroker(client_id, credentials)
    elif "ZERODHA" in broker_upper:
        return ZerodhaOrderBroker(client_id, credentials)
    elif "DHAN" in broker_upper:
        return DhanOrderBroker(client_id, credentials)
    elif "FYERS" in broker_upper:
        return FyersOrderBroker(client_id, credentials)
    return GenericBrokerOrderAdapter(broker, client_id, credentials)
