import asyncio
import datetime
import json
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, Query, Body
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

@websocket_route := app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        # Send initial snapshot
        initial_ticks = feed_manager.get_all_ticks()
        feed_st = feed_manager.get_status()
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

    total_unrealized = sum(inst.unrealized_pnl for inst in instances)
    total_realized = sum(inst.realized_pnl for inst in instances)
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

        result.append({
            "id": acc.id,
            "broker": acc.broker,
            "client_id": acc.client_id,
            "client_name": acc.client_name,
            "paper_mode": acc.paper_mode,
            "is_active": acc.is_active,
            "is_logged_in": acc.is_logged_in,
            "margin_available": acc.margin_available,
            "margin_used": acc.margin_used,
            "total_balance": acc.total_balance,
            "unrealized_pnl": round(u_pnl, 2),
            "realized_pnl": round(r_pnl, 2),
            "total_pnl": round(t_pnl, 2),
            "running_strategies_count": running_strats,
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

@app.post("/api/accounts/{client_id}/square-off")
async def square_off_account_positions(client_id: str):
    res = await order_manager.square_off_account(client_id)
    return res

@app.get("/api/accounts/{client_id}/details")
def get_account_drawer_details(client_id: str, db: Session = Depends(get_db)):
    orders = db.query(Order).filter(Order.client_id == client_id).order_by(Order.id.desc()).limit(50).all()
    trades = db.query(Trade).filter(Trade.client_id == client_id).order_by(Trade.id.desc()).limit(50).all()
    positions = db.query(Position).filter(Position.client_id == client_id).all()
    acc = db.query(Account).filter(Account.client_id == client_id).first()

    return {
        "client_id": client_id,
        "margins": {
            "available": acc.margin_available if acc else 0,
            "used": acc.margin_used if acc else 0,
            "total": acc.total_balance if acc else 0
        },
        "orders": [
            {
                "order_id": o.order_id,
                "broker_order_id": o.broker_order_id,
                "symbol": o.symbol,
                "side": o.side,
                "quantity": o.quantity,
                "price": o.price,
                "status": o.status,
                "created_at": o.created_at.strftime("%H:%M:%S")
            } for o in orders
        ],
        "trades": [
            {
                "trade_id": t.trade_id,
                "symbol": t.symbol,
                "side": t.side,
                "quantity": t.quantity,
                "price": t.price,
                "timestamp": t.timestamp.strftime("%H:%M:%S")
            } for t in trades
        ],
        "positions": [
            {
                "symbol": p.symbol,
                "segment": p.segment,
                "net_qty": p.net_quantity,
                "buy_avg": p.buy_avg_price,
                "sell_avg": p.sell_avg_price,
                "ltp": p.ltp,
                "pnl": round(p.unrealized_pnl + p.realized_pnl, 2)
            } for p in positions
        ],
        "holdings": [
            {
                "symbol": p.symbol,
                "qty": p.net_quantity,
                "buy_price": p.buy_avg_price,
                "ltp": p.ltp,
                "pnl": round(p.unrealized_pnl + p.realized_pnl, 2)
            } for p in positions if getattr(p, "product", "") == "CNC" and p.net_quantity > 0
        ]
    }



# ----------------- STRATEGIES (DASHBOARD & ADMIN CATALOG) -----------------
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
            mapped_clients.append({
                "client_id": cid,
                "name": acc.client_name if acc else cid,
                "is_active": acc.is_active if acc else False,
                "broker": acc.broker if acc else "UNKNOWN"
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
def get_feed_status():
    return feed_manager.get_status()

@app.post("/api/status/feed/failover")
async def trigger_feed_failover():
    next_broker = await feed_manager.failover_to_next("Triggered via Status Diagnostic Panel")
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
async def run_order_segment_test():
    """
    Status Page Test Case:
    1 Order Test with real-time order status update for each of the 6 segments.
    """
    segment_symbols = {
        "NSE": "NSE:RELIANCE",
        "NFO": "NFO:NIFTY26OCT25000CE",
        "BSE": "BSE:SENSEX",
        "BSEFO": "BSEFO:SENSEX26OCT82000CE",
        "MCX": "MCX:CRUDEOIL",
        "MCXFO": "MCXFO:CRUDEOIL6200CE"
    }

    results = []
    # Test on primary client
    client_id = "SH10023"

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
async def run_multi_broker_order_test():
    """
    Tests parallel order dispatch across mapped brokers:
    IIFL XTS, Kotak Neo, Shoonya, Zerodha, and Dhan.
    """
    mapped_clients = ["XT88219", "KT99231", "SH10023", "ZR44102", "DH55102"]
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
