import asyncio
import datetime
import json
import requests
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, Query, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from backend.config import BASE_DIR, DEFAULT_SUBSCRIPTIONS, SUPPORTED_SEGMENTS
from backend.database.connection import get_db, SessionLocal
from backend.database.models import (
    Account, StrategyTemplate, StrategyInstance, Order, Trade, Position,
    SymbolMapping, SystemLog, FeedBrokerStatus
)
from backend.feed_manager.manager import feed_manager
from backend.order_manager.manager import order_manager
from backend.strategy_manager.manager import strategy_supervisor
from backend.core.events import event_bus
from backend.core.symbol_mapper import symbol_mapper

app = FastAPI(title="Fresh Trade Engine API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# ----------------- WEBSOCKET CLIENT MANAGER -----------------
class WebSocketManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: Dict[str, Any]):
        for connection in list(self.active_connections):
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                self.disconnect(connection)

ws_manager = WebSocketManager()

@app.on_event("startup")
async def startup_event():
    # Hook event bus forwarders to WebSocket broadcast
    event_bus.subscribe("feed.tick", lambda tick: asyncio.create_task(ws_manager.broadcast({"type": "TICK", "data": tick})))
    event_bus.subscribe("feed.status", lambda st: asyncio.create_task(ws_manager.broadcast({"type": "FEED_STATUS", "data": st})))
    event_bus.subscribe("order.update", lambda o: asyncio.create_task(ws_manager.broadcast({"type": "ORDER_UPDATE", "data": o})))
    event_bus.subscribe("strategy.event", lambda ev: asyncio.create_task(ws_manager.broadcast({"type": "STRATEGY_EVENT", "data": ev})))

def get_effective_feed_status(db: Optional[Session] = None) -> Dict[str, Any]:
    """
    Returns live feed status. If feed_manager is actively running in this process,
    returns its in-memory status. If running in multi-terminal microservice mode,
    reads live broker statuses directly from SQLite database table (updated by Terminal 1 in WAL mode).
    """
    if feed_manager._is_running and feed_manager.active_broker_name:
        return feed_manager.get_status()

    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        statuses = db.query(FeedBrokerStatus).order_by(FeedBrokerStatus.priority).all()
        if statuses:
            broker_states = {}
            priority_list = []
            active_broker = None
            total_sub = 0
            for st in statuses:
                priority_list.append(st.broker)
                if st.is_active:
                    active_broker = st.broker
                    total_sub = st.subscribed_count
                broker_states[st.broker] = {
                    "name": st.broker,
                    "is_connected": st.is_connected,
                    "is_active": st.is_active,
                    "latency_ms": st.latency_ms,
                    "last_heartbeat": st.last_heartbeat.isoformat() if st.last_heartbeat else None,
                    "subscribed_count": st.subscribed_count
                }

            return {
                "active_broker": active_broker or "XTS",
                "priority": priority_list,
                "total_subscribed": total_sub,
                "subscribed_symbols": list(feed_manager.ticks_cache.keys()),
                "brokers": broker_states,
                "uptime_seconds": 100,
                "ticks_in_cache": len(feed_manager.ticks_cache)
            }
        return feed_manager.get_status()
    finally:
        if close_db:
            db.close()

@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        # Send initial snapshot
        initial_ticks = feed_manager.get_all_ticks()
        feed_st = get_effective_feed_status()
        await websocket.send_text(json.dumps({"type": "INITIAL_DATA", "ticks": initial_ticks, "feed_status": feed_st}))

        while True:
            data = await websocket.receive_text()
            # Handle incoming ping or requests
            msg = json.loads(data)
            if msg.get("action") == "ping":
                await websocket.send_text(json.dumps({"type": "PONG", "timestamp": datetime.datetime.utcnow().isoformat()}))
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


# ----------------- DASHBOARD & SUMMARY -----------------
@app.get("/api/dashboard")
def get_dashboard_summary(db: Session = Depends(get_db)):
    accounts = db.query(Account).all()
    instances = db.query(StrategyInstance).all()
    all_positions = db.query(Position).all()

    # Calculate strategy PnL
    inst_unrealized = sum(inst.unrealized_pnl for inst in instances)
    inst_realized = sum(inst.realized_pnl for inst in instances)

    # Calculate position PnL (from broker executed positions)
    pos_unrealized = sum(p.unrealized_pnl for p in all_positions)
    pos_realized = sum(p.realized_pnl for p in all_positions)

    # Use whichever has active non-zero data (or sum if separate)
    total_unrealized = inst_unrealized if abs(inst_unrealized) > 0.01 else pos_unrealized
    total_realized = inst_realized if abs(inst_realized) > 0.01 else pos_realized
    total_pnl = total_unrealized + total_realized

    ticks = feed_manager.get_all_ticks()

    return {
        "pnl_summary": {
            "unrealized_pnl": round(total_unrealized, 2),
            "realized_pnl": round(total_realized, 2),
            "total_pnl": round(total_pnl, 2)
        },
        "active_clients_count": len([a for a in accounts if a.is_active]),
        "running_strategies_count": len([i for i in instances if i.status == "RUNNING"]),
        "ticks": ticks
    }


# ----------------- CLIENT / BROKER ACCOUNTS -----------------
@app.get("/api/accounts")
def list_accounts(db: Session = Depends(get_db)):
    accounts = db.query(Account).all()
    result = []
    for acc in accounts:
        # Calculate account PnL across active strategies running in this account
        acc_positions = db.query(Position).filter(Position.client_id == acc.client_id).all()
        u_pnl = sum(p.unrealized_pnl for p in acc_positions)
        r_pnl = sum(p.realized_pnl for p in acc_positions)
        t_pnl = u_pnl + r_pnl

        # Count strategies running on this account
        running_strats = 0
        all_strats = db.query(StrategyInstance).all()
        for s in all_strats:
            if acc.client_id in (s.client_ids or []) and s.status == "RUNNING":
                running_strats += 1

        # Check if today's session token is active
        creds = acc.credentials_json or {}
        today_str = datetime.date.today().isoformat()
        token = creds.get("token", "")
        token_date = creds.get("token_date", "")
        has_token_today = bool(token) and (token_date == today_str)

        result.append({
            "id": acc.id,
            "broker": acc.broker,
            "client_id": acc.client_id,
            "client_name": acc.client_name,
            "paper_mode": acc.paper_mode,
            "is_active": acc.is_active,
            "is_logged_in": acc.is_logged_in if has_token_today else (acc.is_logged_in and not bool(token)),
            "margin_available": acc.margin_available,
            "margin_used": acc.margin_used,
            "total_balance": acc.total_balance,
            "unrealized_pnl": round(u_pnl, 2),
            "realized_pnl": round(r_pnl, 2),
            "total_pnl": round(t_pnl, 2),
            "running_strategies_count": running_strats,
            "has_token_today": has_token_today,
            "token_date": token_date,
            "credentials": {
                "api_key": creds.get("api_key", ""),
                "token": token,
                "token_date": token_date,
                "interactive_url": creds.get("interactive_url", "https://ttblaze.iifl.com/interactive"),
                "is_valid_today": has_token_today
            },
            "last_login_at": acc.last_login_at.isoformat() if acc.last_login_at else None
        })
    return result

@app.post("/api/accounts/{client_id}/toggle-mode")
def toggle_account_paper_mode(client_id: str, db: Session = Depends(get_db)):
    acc = db.query(Account).filter(Account.client_id == client_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    acc.paper_mode = not acc.paper_mode
    db.commit()
    return {"success": True, "client_id": client_id, "paper_mode": acc.paper_mode}

@app.post("/api/accounts/{client_id}/login")
def toggle_account_login(client_id: str, db: Session = Depends(get_db)):
    acc = db.query(Account).filter(Account.client_id == client_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    acc.is_logged_in = not acc.is_logged_in
    acc.last_login_at = datetime.datetime.utcnow()
    db.commit()
    return {"success": True, "client_id": client_id, "is_logged_in": acc.is_logged_in}

@app.post("/api/accounts/xts/generate-token")
async def generate_xts_token_endpoint(payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    """
    Generate today's XTS session token via Symphony XTS API:
    - Accepts api_key, api_secret, and optional client_id or interactive_url.
    - If client_id provided, saves token and today's date into the client's credentials_json.
    - Token once generated is valid for the whole trading day.
    """
    client_id = payload.get("client_id", "").strip()
    api_key = payload.get("api_key", "").strip()
    api_secret = payload.get("api_secret", "").strip()
    base_url = payload.get("interactive_url", "https://ttblaze.iifl.com/interactive").strip().rstrip("/")
    source = payload.get("source", "WEBAPI")

    acc = None
    if client_id:
        acc = db.query(Account).filter(Account.client_id == client_id).first()
        if acc and acc.credentials_json:
            api_key = api_key or acc.credentials_json.get("api_key", "")
            api_secret = api_secret or acc.credentials_json.get("api_secret", "")
            base_url = acc.credentials_json.get("interactive_url", base_url)

    if not api_key or not api_secret:
        raise HTTPException(status_code=400, detail="XTS API Key (App Key) and API Secret are required to generate token")

    # Format login URL for Symphony XTS Interactive session
    if base_url.endswith("/interactive"):
        login_url = f"{base_url}/user/session"
    elif "/interactive" in base_url:
        login_url = f"{base_url}/user/session"
    else:
        login_url = f"{base_url}/interactive/user/session"

    req_body = {
        "appKey": api_key,
        "secretKey": api_secret,
        "source": source
    }

    try:
        resp = requests.post(
            login_url,
            json=req_body,
            headers={"Content-Type": "application/json"},
            timeout=12
        )
        try:
            data = resp.json()
        except Exception:
            data = {}

        today_date = datetime.date.today().isoformat()

        if resp.status_code == 200 and data.get("type") == "success":
            result = data.get("result", {})
            token = result.get("token")
            user_id = result.get("userID", client_id)

            if acc:
                creds = dict(acc.credentials_json or {})
                creds["api_key"] = api_key
                creds["api_secret"] = api_secret
                creds["token"] = token
                creds["token_date"] = today_date
                creds["user_id"] = user_id
                creds["interactive_url"] = base_url
                acc.credentials_json = creds
                acc.is_logged_in = True
                acc.last_login_at = datetime.datetime.utcnow()

                # Immediately sync real live margin from XTS
                try:
                    from backend.order_manager.brokers.broker_adapters import create_broker_order_adapter
                    adapter = create_broker_order_adapter("XTS", acc.client_id, creds)
                    m_res = await adapter.get_margins()
                    if m_res.get("success"):
                        acc.margin_available = m_res["margin_available"]
                        acc.margin_used = m_res["margin_used"]
                        acc.total_balance = m_res["total_balance"]
                except Exception as m_err:
                    print(f"[Generate Token] Balance query notice: {m_err}")

                db.commit()

            # Record system log
            log_entry = SystemLog(
                category="BROKER_API",
                level="SUCCESS",
                message=f"XTS session token generated for {client_id or 'account'} (User: {user_id}). Valid for {today_date}.",
                details={"user_id": user_id, "token_prefix": token[:10] + "..." if token else "", "date": today_date}
            )
            db.add(log_entry)
            db.commit()

            return {
                "success": True,
                "token": token,
                "user_id": user_id,
                "token_date": today_date,
                "message": f"XTS token successfully generated for today ({today_date})! Valid for all broker API calls."
            }
        else:
            error_msg = data.get("description") or data.get("message") or resp.text or f"HTTP status {resp.status_code}"
            log_entry = SystemLog(
                category="BROKER_API",
                level="ERROR",
                message=f"XTS session generation failed: {error_msg}",
                details={"status_code": resp.status_code, "response": error_msg}
            )
            db.add(log_entry)
            db.commit()

            return {
                "success": False,
                "error": error_msg,
                "status_code": resp.status_code
            }
    except Exception as e:
        err_msg = f"Network error connecting to XTS server ({login_url}): {str(e)}"
        return {
            "success": False,
            "error": err_msg
        }

@app.post("/api/accounts/{client_id}/square-off")
async def square_off_account_positions(client_id: str):
    res = await order_manager.square_off_account(client_id)
    return res

@app.post("/api/accounts")
def create_account(payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    client_id = payload.get("client_id", "").strip()
    client_name = payload.get("client_name", "").strip()
    broker = payload.get("broker", "XTS").strip().upper()
    paper_mode = payload.get("paper_mode", True)
    margin_available = float(payload.get("margin_available", 500000.0))
    credentials = dict(payload.get("credentials_json", {}))

    if not client_id or not client_name:
        raise HTTPException(status_code=400, detail="Client ID and Client Name are required")

    existing = db.query(Account).filter(Account.client_id == client_id).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Account with Client ID {client_id} already exists")

    # If token was supplied (e.g. today's token entered manually or generated), tag with today's date
    if credentials.get("token"):
        if not credentials.get("token_date"):
            credentials["token_date"] = datetime.date.today().isoformat()

    acc = Account(
        broker=broker,
        client_id=client_id,
        client_name=client_name,
        paper_mode=bool(paper_mode),
        is_active=True,
        is_logged_in=bool(credentials.get("token")) or True,
        margin_available=margin_available,
        margin_used=0.0,
        total_balance=margin_available,
        credentials_json=credentials
    )
    db.add(acc)
    db.commit()
    return {"success": True, "account": {"client_id": acc.client_id, "client_name": acc.client_name, "broker": acc.broker}}

@app.put("/api/accounts/{client_id}")
def update_account(client_id: str, payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    acc = db.query(Account).filter(Account.client_id == client_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    if "client_name" in payload:
        acc.client_name = payload["client_name"].strip()
    if "broker" in payload:
        acc.broker = payload["broker"].strip().upper()
    if "paper_mode" in payload:
        acc.paper_mode = bool(payload["paper_mode"])
    if "margin_available" in payload:
        acc.margin_available = float(payload["margin_available"])
        acc.total_balance = acc.margin_available + acc.margin_used
    if "is_active" in payload:
        acc.is_active = bool(payload["is_active"])
    if "credentials_json" in payload:
        new_creds = payload["credentials_json"]
        if isinstance(new_creds, dict):
            curr_creds = dict(acc.credentials_json or {})
            curr_creds.update(new_creds)
            if curr_creds.get("token") and not curr_creds.get("token_date"):
                curr_creds["token_date"] = datetime.date.today().isoformat()
            acc.credentials_json = curr_creds
            if curr_creds.get("token"):
                acc.is_logged_in = True
                acc.last_login_at = datetime.datetime.utcnow()
    db.commit()
    return {"success": True, "client_id": client_id}

@app.delete("/api/accounts/{client_id}")
def delete_account(client_id: str, db: Session = Depends(get_db)):
    acc = db.query(Account).filter(Account.client_id == client_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    # Remove client_id from all mapped strategy instances
    instances = db.query(StrategyInstance).all()
    for inst in instances:
        if inst.client_ids and client_id in inst.client_ids:
            new_ids = [cid for cid in inst.client_ids if cid != client_id]
            inst.client_ids = new_ids
            db.add(inst)

    # Delete trades for this client
    db.query(Trade).filter(Trade.client_id == client_id).delete()

    # Delete positions for this client
    db.query(Position).filter(Position.client_id == client_id).delete()

    # Delete the account (cascades to orders)
    db.delete(acc)
    db.commit()
    return {"success": True, "deleted_client_id": client_id}

@app.post("/api/accounts/{client_id}/strategies/{strategy_id}/square-off")
async def square_off_account_strategy_positions(client_id: str, strategy_id: int):
    res = await order_manager.square_off_account_strategy(client_id, strategy_id)
    return res

@app.get("/api/accounts/{client_id}/details")
async def get_account_drawer_details(client_id: str, db: Session = Depends(get_db)):
    acc = db.query(Account).filter(Account.client_id == client_id).first()
    orders = db.query(Order).filter(Order.client_id == client_id).order_by(Order.id.desc()).limit(50).all()
    trades = db.query(Trade).filter(Trade.client_id == client_id).order_by(Trade.id.desc()).limit(50).all()
    positions = db.query(Position).filter(Position.client_id == client_id).all()

    creds = acc.credentials_json or {} if acc else {}
    today_str = datetime.date.today().isoformat()
    token = creds.get("token", "")
    token_date = creds.get("token_date", "")
    has_token_today = bool(token) and (token_date == today_str)

    live_margin = None
    live_holdings = None
    live_positions = None
    live_orders = None
    live_trades = None

    if acc and acc.broker == "XTS" and has_token_today:
        try:
            from backend.order_manager.brokers.broker_adapters import create_broker_order_adapter
            adapter = create_broker_order_adapter("XTS", acc.client_id, creds)
            
            # 1. Fetch live margins from broker
            m_res = await adapter.get_margins()
            if m_res.get("success"):
                live_margin = {
                    "available": m_res["margin_available"],
                    "used": m_res["margin_used"],
                    "total": m_res["total_balance"]
                }
                # Sync into database so dashboard cards also show real margin
                acc.margin_available = m_res["margin_available"]
                acc.margin_used = m_res["margin_used"]
                acc.total_balance = m_res["total_balance"]
                db.commit()

            # 2. Fetch live equity holdings from broker
            h_res = await adapter.get_holdings()
            if isinstance(h_res, list):
                live_holdings = h_res

            # 3. Fetch live positions from broker
            p_res = await adapter.get_positions()
            if isinstance(p_res, list):
                live_positions = p_res

            # 4. Fetch live orders from broker
            o_res = await adapter.get_orders()
            if isinstance(o_res, list):
                live_orders = o_res

            # 5. Fetch live trades from broker
            t_res = await adapter.get_trades()
            if isinstance(t_res, list):
                live_trades = t_res
        except Exception as e:
            print(f"[Drawer Details] Error querying live broker data for {client_id}: {e}")

    # Margin response
    margins_data = live_margin if live_margin else {
        "available": acc.margin_available if acc else 0,
        "used": acc.margin_used if acc else 0,
        "total": acc.total_balance if acc else 0
    }

    # Holdings response
    if live_holdings is not None:
        holdings_data = live_holdings
    else:
        holdings_data = [
            {
                "symbol": p.symbol,
                "qty": p.net_quantity,
                "buy_price": p.buy_avg_price,
                "ltp": p.ltp,
                "pnl": round(p.unrealized_pnl + p.realized_pnl, 2)
            } for p in positions if getattr(p, "product", "") == "CNC" and p.net_quantity > 0
        ]

    # Positions response
    if live_positions is not None:
        positions_data = live_positions
    else:
        positions_data = [
            {
                "symbol": p.symbol,
                "segment": p.segment,
                "net_qty": p.net_quantity,
                "buy_avg": p.buy_avg_price,
                "sell_avg": p.sell_avg_price,
                "ltp": p.ltp,
                "pnl": round(p.unrealized_pnl + p.realized_pnl, 2)
            } for p in positions
        ]

    # Orders response
    if live_orders is not None:
        orders_data = live_orders
    else:
        orders_data = [
            {
                "order_id": o.order_id,
                "broker_order_id": o.broker_order_id,
                "symbol": o.symbol,
                "side": o.side,
                "quantity": o.quantity,
                "price": o.price,
                "status": o.status,
                "created_at": o.created_at.strftime("%H:%M:%S") if hasattr(o.created_at, "strftime") else str(o.created_at)
            } for o in orders
        ]

    # Trades response
    if live_trades is not None:
        trades_data = live_trades
    else:
        trades_data = [
            {
                "trade_id": t.trade_id,
                "symbol": t.symbol,
                "side": t.side,
                "quantity": t.quantity,
                "price": t.price,
                "timestamp": t.timestamp.strftime("%H:%M:%S") if hasattr(t.timestamp, "strftime") else str(t.timestamp)
            } for t in trades
        ]

    debug_info = {}
    if acc and acc.broker == "XTS" and 'adapter' in locals():
        debug_info = {
            "holdings": getattr(adapter, "last_holdings_debug", {}),
            "margins": getattr(adapter, "last_margins_debug", {}),
            "positions": getattr(adapter, "last_positions_debug", {}),
            "orders": getattr(adapter, "last_orders_debug", {}),
            "trades": getattr(adapter, "last_trades_debug", {})
        }
        try:
            for cat_name, cnt, d_info in [
                ("Holdings", len(live_holdings or []), debug_info.get("holdings", {})),
                ("Positions", len(live_positions or []), debug_info.get("positions", {})),
                ("Orders", len(live_orders or []), debug_info.get("orders", {})),
                ("Trades", len(live_trades or []), debug_info.get("trades", {}))
            ]:
                att = d_info.get("attempts", [])
                last_att = att[-1] if att else {}
                code = last_att.get("status_code", "N/A")
                is_ok = d_info.get("success", False)
                db.add(SystemLog(
                    category="BROKER_API",
                    level="SUCCESS" if is_ok else "INFO",
                    message=f"XTS {cat_name} Query for {client_id}: Status={code} Found={cnt}",
                    details=d_info
                ))
            db.commit()
        except Exception as log_err:
            pass

    return {
        "client_id": client_id,
        "broker": acc.broker if acc else "XTS",
        "client_name": acc.client_name if acc else "",
        "paper_mode": acc.paper_mode if acc else True,
        "credentials": {
            "api_key": creds.get("api_key", ""),
            "token": token,
            "token_date": token_date,
            "interactive_url": creds.get("interactive_url", "https://ttblaze.iifl.com/interactive"),
            "is_valid_today": has_token_today
        },
        "margins": margins_data,
        "orders": orders_data,
        "trades": trades_data,
        "positions": positions_data,
        "holdings": holdings_data,
        "debug_info": debug_info
    }

@app.post("/api/accounts/{client_id}/sync")
async def sync_account_with_broker(client_id: str, db: Session = Depends(get_db)):
    """Explicitly triggers live sync of balance & holdings from broker API"""
    return await get_account_drawer_details(client_id, db)



# ----------------- STRATEGIES (DASHBOARD & ADMIN CATALOG) -----------------
@app.post("/api/strategies/deploy")
async def deploy_strategy(payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    """Deploys a new strategy instance from a registered template and maps client accounts"""
    template_key = payload.get("template_key")
    if not template_key:
        raise HTTPException(status_code=400, detail="template_key is required")

    template = db.query(StrategyTemplate).filter(StrategyTemplate.key == template_key).first()
    if not template:
        raise HTTPException(status_code=404, detail=f"Strategy template '{template_key}' not found")

    client_ids = payload.get("client_ids", [])
    config = payload.get("config") or template.default_config or {}
    name_admin = payload.get("name_admin") or f"{template.name_admin} [{', '.join(client_ids) if client_ids else 'UNMAPPED'}]"
    name_user = payload.get("name_user") or template.name_user
    start_time = str(config.get("entry_time") or config.get("start_time") or "09:15:00")
    end_time = str(config.get("exit_time") or config.get("end_time") or "15:20:00")
    auto_start = bool(payload.get("auto_start", False))

    instance = StrategyInstance(
        template_key=template_key,
        instance_name_admin=name_admin,
        instance_name_user=name_user,
        client_ids=list(client_ids),
        status="STOPPED",
        config=config,
        start_time=start_time,
        end_time=end_time,
        legs_info=[]
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)

    if auto_start and client_ids:
        try:
            await strategy_supervisor.start_strategy(instance.id)
            db.refresh(instance)
        except Exception as start_err:
            print(f"[Deploy Strategy] Error auto-starting strategy {instance.id}: {start_err}")

    return {
        "success": True,
        "strategy_id": instance.id,
        "name_admin": instance.instance_name_admin,
        "name_user": instance.instance_name_user,
        "client_ids": instance.client_ids,
        "status": instance.status
    }

@app.get("/api/strategies/templates")
def list_strategy_templates(db: Session = Depends(get_db)):
    """Admin Only page: Registered strategies ready to map to clients"""
    templates = db.query(StrategyTemplate).all()
    return [
        {
            "id": t.id,
            "key": t.key, # Admin only
            "name_admin": t.name_admin,
            "name_user": t.name_user,
            "description": t.description,
            "default_config": t.default_config,
            "created": t.created_at.strftime("%Y-%m-%d %H:%M")
        } for t in templates
    ]

@app.get("/api/strategies/instances")
def list_strategy_instances(db: Session = Depends(get_db)):
    """Strategy lists for dashboard table with Admin & Non-Admin representation"""
    instances = db.query(StrategyInstance).all()
    accounts = {a.client_id: a for a in db.query(Account).all()}

    result = []
    for inst in instances:
        mapped_clients = []
        for cid in (inst.client_ids or []):
            acc = accounts.get(cid)
            acc_positions = db.query(Position).filter(
                Position.client_id == cid,
                Position.strategy_instance_id == inst.id
            ).all()
            u_pnl = sum(p.unrealized_pnl for p in acc_positions)
            r_pnl = sum(p.realized_pnl for p in acc_positions)
            t_pnl = u_pnl + r_pnl

            mapped_clients.append({
                "client_id": cid,
                "name": acc.client_name if acc else cid,
                "is_active": acc.is_active if acc else False,
                "paper_mode": acc.paper_mode if acc else True,
                "broker": acc.broker if acc else "UNKNOWN",
                "pnl": {
                    "unrealized": round(u_pnl, 2),
                    "realized": round(r_pnl, 2),
                    "total": round(t_pnl, 2)
                }
            })

        # Multi-leg prices of last trade
        legs = inst.legs_info or []

        result.append({
            "id": inst.id,
            "template_key": inst.template_key, # Admin sees this
            "name_admin": inst.instance_name_admin,
            "name_user": inst.instance_name_user, # Non-admin sees this only without slug
            "status": inst.status,
            "client_ids": inst.client_ids,
            "mapped_clients": mapped_clients,
            "last_trade_status": inst.last_trade_status,
            "last_trade_at": inst.last_trade_at.strftime("%Y-%m-%d %H:%M:%S") if inst.last_trade_at else None,
            "start_time": inst.start_time,
            "end_time": inst.end_time,
            "pnl": {
                "unrealized": round(inst.unrealized_pnl, 2),
                "realized": round(inst.realized_pnl, 2),
                "total": round(inst.total_pnl, 2)
            },
            "legs_info": legs,
            "config": inst.config
        })
    return result

@app.put("/api/strategies/{id}")
async def update_strategy_instance(
    id: int,
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db)
):
    """
    Updates strategy instance parameters, names, timings, or configuration.
    If the strategy is currently RUNNING, changes take effect immediately in the
    running OS process container without requiring a restart.
    """
    inst = db.query(StrategyInstance).filter(StrategyInstance.id == id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Strategy instance not found")

    if "name_admin" in payload and payload["name_admin"]:
        inst.instance_name_admin = payload["name_admin"]
    if "name_user" in payload and payload["name_user"]:
        inst.instance_name_user = payload["name_user"]
    new_config = payload.get("config") or {}
    if "start_time" in payload and payload["start_time"]:
        inst.start_time = payload["start_time"]
        new_config["start_time"] = payload["start_time"]
        new_config["entry_time"] = payload["start_time"]
    if "end_time" in payload and payload["end_time"]:
        inst.end_time = payload["end_time"]
        new_config["end_time"] = payload["end_time"]
        new_config["exit_time"] = payload["end_time"]

    if new_config and isinstance(new_config, dict):
        merged_cfg = dict(inst.config or {})
        merged_cfg.update(new_config)
        inst.config = merged_cfg

        if "entry_time" in new_config:
            inst.start_time = str(new_config["entry_time"])
        if "exit_time" in new_config:
            inst.end_time = str(new_config["exit_time"])

    db.commit()
    db.refresh(inst)

    # If currently running, hot-push updates to the live worker process container
    hot_updated = False
    if inst.status == "RUNNING":
        hot_updated = await strategy_supervisor.update_strategy_config(id, inst.config or {})

    return {
        "success": True,
        "strategy_id": id,
        "status": inst.status,
        "hot_reloaded": hot_updated,
        "config": inst.config,
        "start_time": inst.start_time,
        "end_time": inst.end_time
    }

@app.put("/api/strategies/{id}/clients")
def update_strategy_clients(id: int, payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    inst = db.query(StrategyInstance).filter(StrategyInstance.id == id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Strategy not found")
    new_client_ids = payload.get("client_ids", [])
    inst.client_ids = list(new_client_ids)
    db.commit()
    return {"success": True, "strategy_id": id, "client_ids": inst.client_ids}

@app.post("/api/strategies/{id}/clients/{client_id}/unmap")
def unmap_strategy_client(id: int, client_id: str, db: Session = Depends(get_db)):
    inst = db.query(StrategyInstance).filter(StrategyInstance.id == id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Strategy not found")
    if inst.client_ids and client_id in inst.client_ids:
        inst.client_ids = [cid for cid in inst.client_ids if cid != client_id]
        db.commit()
    return {"success": True, "strategy_id": id, "client_ids": inst.client_ids}

@app.post("/api/strategies/{id}/start")
async def start_strategy_instance(id: int):
    success = await strategy_supervisor.start_strategy(id)
    return {"success": success, "strategy_id": id}

@app.post("/api/strategies/{id}/stop")
async def stop_strategy_instance(id: int):
    success = await strategy_supervisor.stop_strategy(id)
    return {"success": success, "strategy_id": id}

@app.post("/api/strategies/{id}/duplicate")
async def duplicate_strategy_instance(
    id: int,
    payload: Dict[str, Any] = Body(...)
):
    """Duplicates an existing strategy with custom parameters or symbols"""
    new_admin_name = payload.get("name_admin", f"Cloned Strategy #{id}")
    new_user_name = payload.get("name_user", "Alpha Strategy (Copy)")
    custom_config = payload.get("config")
    custom_client_ids = payload.get("client_ids")

    clone_id = await strategy_supervisor.duplicate_strategy(
        source_instance_id=id,
        new_name_admin=new_admin_name,
        new_name_user=new_user_name,
        custom_config=custom_config,
        custom_client_ids=custom_client_ids
    )
    if not clone_id:
        raise HTTPException(status_code=400, detail="Failed to duplicate strategy")
    return {"success": True, "cloned_strategy_id": clone_id}

@app.delete("/api/strategies/{id}")
async def delete_strategy_instance(id: int, db: Session = Depends(get_db)):
    await strategy_supervisor.stop_strategy(id)
    inst = db.query(StrategyInstance).filter(StrategyInstance.id == id).first()
    if inst:
        db.delete(inst)
        db.commit()
        return {"success": True, "deleted_id": id}
    return {"success": False, "error": "Strategy not found"}

@app.post("/api/strategies/{id}/square-off")
async def square_off_strategy_positions(id: int):
    return await order_manager.square_off_strategy(id)

@app.get("/api/strategies/{id}/chart")
async def get_strategy_chart(id: int, db: Session = Depends(get_db)):
    inst = db.query(StrategyInstance).filter(StrategyInstance.id == id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Strategy not found")
    symbol = (inst.config or {}).get("symbol", "NSE:NIFTY")
    ohlc = await feed_manager.get_ohlc(symbol, resolution="5m", count=40)
    return {"strategy_id": id, "symbol": symbol, "ohlc": ohlc}

@app.get("/api/strategies/{id}/trades")
def get_strategy_recent_trades(id: int, db: Session = Depends(get_db)):
    trades = db.query(Trade).filter(Trade.strategy_instance_id == id).order_by(Trade.id.desc()).limit(30).all()
    inst = db.query(StrategyInstance).filter(StrategyInstance.id == id).first()
    if not trades and inst:
        # Fallback: check trades for mapped clients with matching symbol
        cfg = inst.config or {}
        sym = cfg.get("symbol", "")
        base_sym = sym.split(":")[-1] if ":" in sym else sym
        client_ids = inst.client_ids or []
        if client_ids:
            trades = db.query(Trade).filter(
                Trade.client_id.in_(client_ids),
                Trade.symbol.like(f"%{base_sym}%")
            ).order_by(Trade.id.desc()).limit(30).all()
    return {
        "strategy_id": id,
        "legs_info": inst.legs_info if inst else [],
        "trades": [
            {
                "trade_id": t.trade_id,
                "order_id": t.order_id,
                "symbol": t.symbol,
                "side": t.side,
                "quantity": t.quantity,
                "price": t.price,
                "leg_id": t.leg_id,
                "timestamp": t.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            } for t in trades
        ]
    }

@app.get("/api/strategies/{id}/logs")
def get_strategy_chronological_logs(id: int, db: Session = Depends(get_db)):
    """Consolidated strategy-level audit timeline with step-by-step progress"""
    logs = db.query(SystemLog).filter(
        SystemLog.strategy_id == id
    ).order_by(SystemLog.id.desc()).limit(100).all()

    return [
        {
            "id": l.id,
            "level": l.level,
            "message": l.message,
            "timestamp": l.timestamp.strftime("%H:%M:%S")
        } for l in logs
    ]


# ----------------- STATUS & DIAGNOSTICS PAGE -----------------
@app.get("/api/status/feed")
def get_feed_status(db: Session = Depends(get_db)):
    return get_effective_feed_status(db)

@app.post("/api/status/feed/failover")
async def trigger_feed_failover(db: Session = Depends(get_db)):
    if feed_manager._is_running and feed_manager.active_broker_name:
        next_broker = await feed_manager.failover_to_next("Triggered via Status Diagnostic Panel")
    else:
        await event_bus.publish("feed.failover", "Triggered via Status Diagnostic Panel")
        await asyncio.sleep(0.5)
        st = get_effective_feed_status(db)
        next_broker = st.get("active_broker", "SHOONYA")
    return {"success": True, "active_broker": next_broker}

@app.post("/api/status/feed/priority")
async def set_feed_priority(payload: Dict[str, Any] = Body(...)):
    new_priority = payload.get("priority", [])
    if new_priority:
        await feed_manager.set_priority(new_priority)
    return {"success": True, "priority": feed_manager.priority_list}

@app.post("/api/status/diagnostics/run-feed-test")
async def run_feed_segment_test():
    """
    Status Page Test Case:
    1 Symbol Subscription from each of the 6 segments: NSE, NFO, BSE, BSEFO, MCX, MCXFO
    And OHLC fetch for 1 symbol from each segment.
    """
    segment_symbols = {
        "NSE": "NSE:NIFTY",
        "NFO": "NFO:NIFTY26OCT25000CE",
        "BSE": "BSE:SENSEX",
        "BSEFO": "BSEFO:SENSEX26OCT82000CE",
        "MCX": "MCX:CRUDEOIL",
        "MCXFO": "MCXFO:CRUDEOIL6200CE"
    }

    results = []
    for segment, symbol in segment_symbols.items():
        t0 = datetime.datetime.utcnow()
        # 1. Test Subscription
        sub_ok = await feed_manager.subscribe([symbol])
        tick = feed_manager.get_latest_tick(symbol)
        
        # 2. Test OHLC Fetch
        ohlc = await feed_manager.get_ohlc(symbol, resolution="5m", count=10)
        t1 = datetime.datetime.utcnow()
        latency = round((t1 - t0).total_seconds() * 1000, 1)

        status = "PASSED" if (sub_ok and len(ohlc) > 0) else "FAILED"

        # Log event in Feed Subscription category
        db = SessionLocal()
        try:
            log = SystemLog(
                category="FEED_SUBSCRIPTION",
                level="SUCCESS" if status == "PASSED" else "ERROR",
                message=f"Diagnostic Feed Test [{segment}] {symbol}: Subscribed OK, OHLC Bars: {len(ohlc)} ({latency}ms)",
                symbol=symbol
            )
            db.add(log)
            db.commit()
        finally:
            db.close()

        results.append({
            "segment": segment,
            "symbol": symbol,
            "subscription_status": "SUCCESS" if sub_ok else "FAILED",
            "ohlc_fetch_status": "SUCCESS" if len(ohlc) > 0 else "EMPTY_RESULT",
            "bars_count": len(ohlc),
            "latest_ltp": tick.get("ltp") if tick else None,
            "latency_ms": latency,
            "overall_status": status
        })

    return {"status": "COMPLETED", "results": results}

@app.post("/api/status/diagnostics/run-order-test")
async def run_order_segment_test(db: Session = Depends(get_db)):
    """
    Status Page Test Case:
    1 Order Test with real-time order status update for each of the 6 segments.
    """
    first_acc = db.query(Account).filter(Account.is_active == True).first()
    if not first_acc:
        return {
            "status": "SKIPPED",
            "message": "No active client accounts found. Please configure a client account before running order tests.",
            "results": []
        }

    client_id = first_acc.client_id
    segment_symbols = {
        "NSE": "NSE:RELIANCE",
        "NFO": "NFO:NIFTY26OCT25000CE",
        "BSE": "BSE:SENSEX",
        "BSEFO": "BSEFO:SENSEX26OCT82000CE",
        "MCX": "MCX:CRUDEOIL",
        "MCXFO": "MCXFO:CRUDEOIL6200CE"
    }

    results = []
    for segment, symbol in segment_symbols.items():
        t0 = datetime.datetime.utcnow()
        res = await order_manager.place_order(
            client_id=client_id,
            symbol=symbol,
            side="BUY",
            quantity=1,
            order_type="MARKET",
            price=150.0
        )
        t1 = datetime.datetime.utcnow()
        latency = round((t1 - t0).total_seconds() * 1000, 1)

        results.append({
            "segment": segment,
            "symbol": symbol,
            "client_id": client_id,
            "order_id": res.get("order_id"),
            "broker_order_id": res.get("broker_order_id"),
            "status": res.get("status", "FAILED"),
            "filled_price": res.get("price"),
            "latency_ms": latency
        })

    return {"status": "COMPLETED", "results": results}

@app.post("/api/status/diagnostics/run-multi-broker-test")
async def run_multi_broker_order_test(db: Session = Depends(get_db)):
    """
    Tests parallel order dispatch across active mapped brokers
    """
    active_accs = db.query(Account).filter(Account.is_active == True).all()
    mapped_clients = [a.client_id for a in active_accs]

    if not mapped_clients:
        return {
            "status": "SKIPPED",
            "message": "No active client accounts found to test. Please add at least one client account.",
            "brokers_tested": 0,
            "results": []
        }

    symbol = "MCX:CRUDEOILM"
    qty = 10
    
    results = await order_manager.dispatch_parallel_multi_client(
        client_ids=mapped_clients,
        symbol=symbol,
        side="SELL",
        quantity=qty,
        order_type="MARKET",
        price=5850.0
    )
    return {
        "status": "COMPLETED",
        "symbol": symbol,
        "quantity_per_broker": qty,
        "brokers_tested": len(mapped_clients),
        "results": results
    }

# ----------------- REGULATORY LOT SIZE CONFIGURATION -----------------
@app.get("/api/config/lot-sizes")
def get_lot_sizes_config(db: Session = Depends(get_db)):
    """Returns currently active regulatory lot sizes across all instruments"""
    from backend.config import DEFAULT_LOT_SIZES
    mappings = db.query(SymbolMapping).all()
    symbol_lots = {m.canonical_symbol: m.lot_size for m in mappings}
    return {
        "default_lot_sizes": DEFAULT_LOT_SIZES,
        "symbol_lots": symbol_lots
    }

@app.post("/api/config/lot-sizes")
def update_lot_sizes_config(payload: Dict[str, int] = Body(...), db: Session = Depends(get_db)):
    """
    Updates lot sizes when exchange/SEBI regulators change lot sizes.
    Accepts dict like {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20}.
    Immediately updates in-memory config and database symbol mappings.
    """
    from backend.config import DEFAULT_LOT_SIZES
    updated = {}
    for key, new_lot in payload.items():
        key_upper = key.upper()
        if new_lot > 0:
            DEFAULT_LOT_SIZES[key_upper] = int(new_lot)
            # Update matching symbol mappings in DB
            count = db.query(SymbolMapping).filter(SymbolMapping.canonical_symbol.like(f"%{key_upper}%")).update({"lot_size": new_lot}, synchronize_session=False)
            updated[key_upper] = {"new_lot_size": int(new_lot), "mappings_updated": count}
    
    db.commit()
    return {"success": True, "updated": updated, "current_defaults": DEFAULT_LOT_SIZES}





# ----------------- LOGS & EVENTS PAGE -----------------
@app.get("/api/logs")
def get_system_logs(
    category: Optional[str] = None, # BROKER_API, FEED_SUBSCRIPTION, STRATEGY_DECISION
    level: Optional[str] = None,
    strategy_id: Optional[int] = None,
    limit: int = 150,
    db: Session = Depends(get_db)
):
    """
    Clear Log - No Noise.
    Divided into three parts:
    1. Broker API Log - ERROR / SUCCESS
    2. Feed Subscription - ERROR / SUCCESS / DATA RECEIVING (Includes OHLC fetch)
    3. Strategy Decision Log
    """
    q = db.query(SystemLog)
    if category:
        q = q.filter(SystemLog.category == category.upper())
    if level:
        q = q.filter(SystemLog.level == level.upper())
    if strategy_id:
        q = q.filter(SystemLog.strategy_id == strategy_id)

    logs = q.order_by(SystemLog.id.desc()).limit(limit).all()

    # Get active undismissed errors to highlight promptly at the top!
    undismissed_errors = db.query(SystemLog).filter(
        SystemLog.level == "ERROR",
        SystemLog.is_dismissed == False
    ).order_by(SystemLog.id.desc()).limit(5).all()

    return {
        "active_errors": [
            {
                "id": e.id,
                "category": e.category,
                "message": e.message,
                "client_id": e.client_id,
                "symbol": e.symbol,
                "strategy_id": e.strategy_id,
                "timestamp": e.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            } for e in undismissed_errors
        ],
        "logs": [
            {
                "id": l.id,
                "category": l.category,
                "level": l.level,
                "message": l.message,
                "client_id": l.client_id,
                "strategy_id": l.strategy_id,
                "symbol": l.symbol,
                "is_dismissed": l.is_dismissed,
                "timestamp": l.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            } for l in logs
        ]
    }

@app.post("/api/logs/{log_id}/dismiss")
def dismiss_error_log(log_id: int, db: Session = Depends(get_db)):
    """User dismisses highlighted prompt error"""
    log = db.query(SystemLog).filter(SystemLog.id == log_id).first()
    if log:
        log.is_dismissed = True
        db.commit()
    return {"success": True, "dismissed_id": log_id}

@app.post("/api/logs/dismiss-all")
def dismiss_all_errors(db: Session = Depends(get_db)):
    db.query(SystemLog).filter(SystemLog.level == "ERROR", SystemLog.is_dismissed == False).update({"is_dismissed": True})
    db.commit()
    return {"success": True}


# ----------------- CREDENTIALS / SETTINGS -----------------
@app.get("/api/settings/credentials")
def get_credentials_settings():
    from backend import config
    xts_broker = feed_manager.brokers.get("XTS")
    return {
        "xts": {
            "api_url": getattr(xts_broker, "api_url", config.XTS_FEED_URL),
            "api_key": getattr(xts_broker, "api_key", config.XTS_FEED_API_KEY),
            "api_secret": getattr(xts_broker, "api_secret", config.XTS_FEED_API_SECRET),
            "is_live_connected": getattr(xts_broker, "is_live_connected", False),
            "user_id": getattr(xts_broker, "user_id", None)
        }
    }

@app.post("/api/settings/credentials")
async def save_credentials_settings(payload: Dict[str, Any] = Body(...)):
    xts_data = payload.get("xts", {})
    api_url = xts_data.get("api_url", "https://xts.iifl.com/marketdata").strip()
    api_key = xts_data.get("api_key", "").strip()
    api_secret = xts_data.get("api_secret", "").strip()

    # Update .env file
    env_path = BASE_DIR / ".env"
    env_content = f"""# FRESH TRADE ENGINE CONFIGURATION
XTS_FEED_URL={api_url}
XTS_FEED_API_KEY={api_key}
XTS_FEED_API_SECRET={api_secret}
"""
    with open(env_path, "w", encoding="utf-8") as f:
        f.write(env_content)

    # Hot reload broker configuration
    xts_broker = feed_manager.brokers.get("XTS")
    if xts_broker:
        xts_broker.api_url = api_url
        xts_broker.api_key = api_key
        xts_broker.api_secret = api_secret
        await xts_broker.connect()
        if feed_manager.subscribed_symbols:
            await xts_broker.subscribe(feed_manager.subscribed_symbols)

    return {
        "success": True,
        "message": "Credentials saved and XTS feed connection refreshed",
        "is_live_connected": getattr(xts_broker, "is_live_connected", False),
        "user_id": getattr(xts_broker, "user_id", None),
        "status": feed_manager.get_status()
    }
