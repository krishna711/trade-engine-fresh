import asyncio
import datetime
import json
import re
import requests
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, Query, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from backend.config import BASE_DIR, DEFAULT_SUBSCRIPTIONS, SUPPORTED_SEGMENTS
from backend.database.connection import get_db, SessionLocal
from backend.database.models import (
    Account, StrategyTemplate, StrategyInstance, Order, Trade, Position,
    SymbolMapping, SystemLog, FeedBrokerStatus, TradeReport, User, TelegramSetting
)
from backend.feed_manager.manager import feed_manager
from backend.order_manager.manager import order_manager
from backend.strategy_manager.manager import strategy_supervisor
from backend.core.events import event_bus
from backend.core.symbol_mapper import symbol_mapper
from backend.core.auth import (
    create_access_token, decode_access_token, get_current_user,
    get_optional_user, require_admin, hash_user_password, verify_user_password
)

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

# ----------------- IST TIMEZONE HELPERS -----------------
IST_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def to_ist(dt: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    if not dt:
        return None
    if dt.tzinfo is None:
        # SQLite naive timestamps are stored as UTC
        return dt.replace(tzinfo=datetime.timezone.utc).astimezone(IST_TZ)
    return dt.astimezone(IST_TZ)

def format_ist(dt: Optional[datetime.datetime], fmt: str = "%H:%M:%S") -> str:
    ist_dt = to_ist(dt)
    return ist_dt.strftime(fmt) if ist_dt else "--:--:--"

def format_ist_timeline(dt: Optional[datetime.datetime]) -> str:
    ist_dt = to_ist(dt)
    if not ist_dt:
        return "--:--:--"
    now_ist = datetime.datetime.now(IST_TZ)
    if ist_dt.date() == now_ist.date():
        return ist_dt.strftime("%H:%M:%S IST")
    return ist_dt.strftime("%d %b %H:%M:%S IST")

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


def check_strategy_owner_or_admin(instance: StrategyInstance, current_user: User, db: Session):
    """Ensures that a normal user can only control strategies mapped to their own accounts or created by them."""
    if current_user.role == "ADMIN":
        return True
    if instance.user_id == current_user.id:
        return True
    user_cids = {a.client_id for a in db.query(Account).filter(Account.user_id == current_user.id).all()}
    if any(cid in user_cids for cid in (instance.client_ids or [])):
        return True
    raise HTTPException(status_code=403, detail="You do not have permission to manage this strategy instance.")

@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket, token: Optional[str] = Query(None)):
    # Validate session token if provided; reject if completely invalid
    if token:
        user_info = decode_access_token(token)
        if not user_info:
            await websocket.close(code=4001, reason="Authentication required")
            return
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



# ==============================================================================
# AUTHENTICATION & SESSION ENDPOINTS
# ==============================================================================

@app.post("/api/auth/login")
def login_endpoint(payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    """Authenticate user credentials and return signed access token"""
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", "")).strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password are required.")

    user = db.query(User).filter(User.username.ilike(username)).first()

    # Self-healing fallback: If default admin or trader is missing from database, auto-create immediately
    if not user:
        if username.lower() == "admin" and password == "admin123":
            user = User(
                username="admin",
                full_name="System Administrator",
                email="admin@fresh-trade.local",
                password_hash=hash_user_password("admin123"),
                role="ADMIN",
                is_active=True
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        elif username.lower() == "trader" and password == "trader123":
            user = User(
                username="trader",
                full_name="Standard Trader",
                email="trader@fresh-trade.local",
                password_hash=hash_user_password("trader123"),
                role="USER",
                is_active=True
            )
            db.add(user)
            db.commit()
            db.refresh(user)

    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    # Strict password verification: never overwrite user-configured passwords
    if not verify_user_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is deactivated. Contact Administrator.")

    token = create_access_token(user)
    return {
        "success": True,
        "token": token,
        "user": {
            "id": user.id,
            "username": user.username,
            "full_name": user.full_name or user.username,
            "email": user.email or "",
            "role": user.role
        }
    }

@app.get("/api/auth/me")
def me_endpoint(current_user: User = Depends(get_current_user)):
    """Return profile of currently authenticated session"""
    return {
        "success": True,
        "user": {
            "id": current_user.id,
            "username": current_user.username,
            "full_name": current_user.full_name or current_user.username,
            "email": current_user.email or "",
            "role": current_user.role
        }
    }

@app.post("/api/auth/logout")
def logout_endpoint():
    """Sign out user session"""
    return {"success": True, "message": "Signed out successfully."}

@app.post("/api/auth/change-password")
def change_password_endpoint(
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Allows any authenticated user to change their own password"""
    current_password = str(payload.get("current_password", "")).strip()
    new_password = str(payload.get("new_password", "")).strip()

    if not current_password:
        raise HTTPException(status_code=400, detail="Current password is required.")

    # Query active user record directly in this session to guarantee transaction persistence
    db_user = db.query(User).filter(User.id == current_user.id).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found.")

    if not verify_user_password(current_password, db_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    if not new_password or len(new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters long.")
    if current_password == new_password:
        raise HTTPException(status_code=400, detail="New password must be different from current password.")

    db_user.password_hash = hash_user_password(new_password)
    db_user.updated_at = datetime.datetime.utcnow()
    db.commit()
    db.refresh(db_user)

    # Issue updated session token for the user
    new_token = create_access_token(db_user)

    return {
        "success": True,
        "message": "Password updated successfully.",
        "token": new_token
    }

# ----------------- TODAY PNL & DATE HELPERS -----------------
def get_ist_today_range():
    """Returns (today_start_utc, today_end_utc) based on Indian Standard Time (IST, UTC+5:30)"""
    ist_now = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
    today_start_ist = ist_now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start_ist - datetime.timedelta(hours=5, minutes=30)
    today_end_utc = today_start_utc + datetime.timedelta(days=1)
    return today_start_utc, today_end_utc

def is_strategy_traded_today(inst: StrategyInstance, today_start_utc: datetime.datetime, db: Session) -> bool:
    """Returns True if the strategy is currently open or has executed trades today (IST)"""
    # 1. Any currently active trade is active today
    if inst.last_trade_status in ["OPEN", "IN_TRADE", "ENTRY_SENT"]:
        return True

    # 2. Check last_trade_at timestamp
    if inst.last_trade_at and inst.last_trade_at >= today_start_utc:
        return True

    # 3. Check if any trade occurred in Trade table today for this instance
    has_trade = db.query(Trade).filter(
        Trade.strategy_instance_id == inst.id,
        Trade.timestamp >= today_start_utc
    ).first()
    if has_trade:
        return True

    return False


# ----------------- DASHBOARD & SUMMARY -----------------
@app.get("/api/dashboard")
def get_dashboard_summary(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """
    Computes TODAY's consolidated PnL strictly.
    - If trade is CLOSED, unrealized PnL is 0.00 (no running with LTP).
    - R and T reflect today's trading only (all-time & historical belong in Reports).
    """
    today_start_utc, _ = get_ist_today_range()

    if current_user.role == "ADMIN":
        accounts = db.query(Account).all()
        instances = db.query(StrategyInstance).all()
    else:
        accounts = db.query(Account).filter(Account.user_id == current_user.id).all()
        user_cids = {a.client_id for a in accounts}
        all_instances = db.query(StrategyInstance).all()
        instances = [i for i in all_instances if i.user_id == current_user.id or any(c in user_cids for c in (i.client_ids or []))]

    total_unrealized = 0.0
    total_realized = 0.0

    for inst in instances:
        if is_strategy_traded_today(inst, today_start_utc, db):
            if inst.last_trade_status in ["OPEN", "IN_TRADE", "ENTRY_SENT"]:
                total_unrealized += float(inst.unrealized_pnl or 0.0)
                total_realized += float(inst.realized_pnl or 0.0)
            else: # CLOSED
                total_realized += float(inst.realized_pnl or 0.0)

    # If no strategy currently has an open trade, unrealized PnL is STRICTLY 0.0!
    has_any_open = any(
        inst.last_trade_status in ["OPEN", "IN_TRADE", "ENTRY_SENT"]
        for inst in instances
    )
    if not has_any_open:
        total_unrealized = 0.0

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
def list_accounts(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """
    Returns client accounts with TODAY-only PnL.
    - U is 0.00 if no active trade is open in the mapped strategy today.
    - R & T reflect today's realized profit/loss only.
    """
    today_start_utc, _ = get_ist_today_range()

    if current_user.role == "ADMIN":
        accounts = db.query(Account).all()
    else:
        accounts = db.query(Account).filter(Account.user_id == current_user.id).all()

    users_map = {u.id: u.username for u in db.query(User).all()}
    all_strats = db.query(StrategyInstance).all()
    result = []

    for acc in accounts:
        # Calculate TODAY's account PnL across strategies mapped to this account
        acc_strats = [s for s in all_strats if acc.client_id in (s.client_ids or [])]
        u_pnl = 0.0
        r_pnl = 0.0
        running_strats = 0

        for inst in acc_strats:
            if inst.status == "RUNNING":
                running_strats += 1

            if is_strategy_traded_today(inst, today_start_utc, db):
                if inst.last_trade_status in ["OPEN", "IN_TRADE", "ENTRY_SENT"]:
                    u_pnl += float(inst.unrealized_pnl or 0.0)
                    r_pnl += float(inst.realized_pnl or 0.0)
                else: # CLOSED
                    r_pnl += float(inst.realized_pnl or 0.0)

        # If no open trade in any strategy mapped to this account, unrealized PnL is STRICTLY 0.0!
        has_acc_open = any(
            inst.last_trade_status in ["OPEN", "IN_TRADE", "ENTRY_SENT"]
            for inst in acc_strats
        )
        if not has_acc_open:
            u_pnl = 0.0

        t_pnl = u_pnl + r_pnl

        # Check if today's session token is active
        creds = acc.credentials_json or {}
        today_str = datetime.date.today().isoformat()
        today_compact = datetime.date.today().strftime("%Y%m%d")
        token = str(creds.get("token") or "").strip()
        token_date = str(creds.get("token_date") or "").strip()
        has_token_today = bool(token) and (token_date == today_str or token_date == today_compact)
        if acc.broker != "XTS" and (has_token_today or bool(acc.is_logged_in)):
            has_token_today = True

        if has_token_today and not acc.is_logged_in:
            acc.is_logged_in = True
            try:
                db.commit()
            except Exception:
                pass

        is_logged_in = has_token_today or bool(acc.is_logged_in)

        result.append({
            "id": acc.id,
            "broker": acc.broker,
            "client_id": acc.client_id,
            "client_name": acc.client_name,
            "static_ip": acc.static_ip or creds.get("static_ip", ""),
            "paper_mode": acc.paper_mode,
            "is_active": acc.is_active,
            "is_logged_in": is_logged_in,
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
                "api_secret": creds.get("api_secret", ""),
                "token": token,
                "token_date": token_date,
                "interactive_url": creds.get("interactive_url", "https://ttblaze.iifl.com/interactive"),
                "static_ip": acc.static_ip or creds.get("static_ip", ""),
                "is_valid_today": has_token_today
            },
            "last_login_at": acc.last_login_at.isoformat() if acc.last_login_at else None,
            "user_id": acc.user_id,
            "owner_username": users_map.get(acc.user_id, "admin")
        })
    return result

@app.post("/api/accounts/{client_id}/toggle-mode")
def toggle_account_paper_mode(client_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    acc = db.query(Account).filter(Account.client_id == client_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    if current_user.role != "ADMIN" and acc.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You do not have permission to manage this account.")
    acc.paper_mode = not acc.paper_mode
    db.commit()
    return {"success": True, "client_id": client_id, "paper_mode": acc.paper_mode}

@app.post("/api/accounts/{client_id}/set-mode")
def set_account_paper_mode(client_id: str, payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    acc = db.query(Account).filter(Account.client_id == client_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    if current_user.role != "ADMIN" and acc.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You do not have permission to manage this account.")
    acc.paper_mode = bool(payload.get("paper_mode", True))
    db.commit()
    return {"success": True, "client_id": client_id, "paper_mode": acc.paper_mode}

@app.post("/api/accounts/{client_id}/toggle-active")
def toggle_account_active(client_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    acc = db.query(Account).filter(Account.client_id == client_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    if current_user.role != "ADMIN" and acc.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You do not have permission to manage this account.")
    acc.is_active = not acc.is_active
    db.commit()
    return {"success": True, "client_id": client_id, "is_active": acc.is_active}

@app.post("/api/accounts/{client_id}/login")
def toggle_account_login(client_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    acc = db.query(Account).filter(Account.client_id == client_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    if current_user.role != "ADMIN" and acc.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You do not have permission to manage this account.")
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
    static_ip = payload.get("static_ip", "").strip() or None

    acc = None
    if client_id:
        acc = db.query(Account).filter(Account.client_id == client_id).first()
        if acc:
            static_ip = static_ip or acc.static_ip or (acc.credentials_json.get("static_ip") if acc.credentials_json else None)
            if acc.credentials_json:
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
        from backend.core.http_client import get_bound_requests_session
        http_session = get_bound_requests_session(static_ip)
        resp = http_session.post(
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
                if static_ip:
                    acc.static_ip = static_ip
                    creds["static_ip"] = static_ip
                acc.credentials_json = creds
                acc.is_logged_in = True
                acc.last_login_at = datetime.datetime.utcnow()

                # Immediately sync real live margin from XTS
                try:
                    from backend.order_manager.brokers.broker_adapters import create_broker_order_adapter
                    adapter = create_broker_order_adapter("XTS", acc.client_id, creds, static_ip=acc.static_ip)
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
async def square_off_account_positions(client_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    acc = db.query(Account).filter(Account.client_id == client_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    if current_user.role != "ADMIN" and acc.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You do not have permission to square off this account.")
    res = await order_manager.square_off_account(client_id)
    return res

@app.post("/api/accounts")
def create_account(payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    client_id = payload.get("client_id", "").strip()
    client_name = payload.get("client_name", "").strip()
    broker = payload.get("broker", "XTS").strip().upper()
    paper_mode = payload.get("paper_mode", True)
    margin_available = float(payload.get("margin_available", 500000.0))
    credentials = dict(payload.get("credentials_json", {}))
    static_ip = payload.get("static_ip", "").strip() or None
    if not static_ip and credentials.get("static_ip"):
        static_ip = credentials.get("static_ip", "").strip() or None
    if static_ip:
        credentials["static_ip"] = static_ip

    if not client_id or not client_name:
        raise HTTPException(status_code=400, detail="Client ID and Client Name are required")

    existing = db.query(Account).filter(Account.client_id == client_id).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Account with Client ID {client_id} already exists")

    # If token was supplied (e.g. today's token entered manually or generated), tag with today's date
    if credentials.get("token"):
        if not credentials.get("token_date"):
            credentials["token_date"] = datetime.date.today().isoformat()

    target_user_id = current_user.id
    if current_user.role == "ADMIN" and payload.get("user_id"):
        try:
            target_user_id = int(payload.get("user_id"))
        except Exception:
            pass

    acc = Account(
        broker=broker,
        user_id=target_user_id,
        client_id=client_id,
        client_name=client_name,
        static_ip=static_ip,
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
    return {"success": True, "account": {"client_id": acc.client_id, "client_name": acc.client_name, "broker": acc.broker, "static_ip": acc.static_ip, "user_id": acc.user_id}}

@app.put("/api/accounts/{client_id}")
def update_account(client_id: str, payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    acc = db.query(Account).filter(Account.client_id == client_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    if current_user.role != "ADMIN" and acc.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You do not have permission to modify this account.")
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
    if "static_ip" in payload:
        acc.static_ip = payload["static_ip"].strip() if payload["static_ip"] else None
        curr_creds = dict(acc.credentials_json or {})
        curr_creds["static_ip"] = acc.static_ip
        acc.credentials_json = curr_creds
    elif "credentials_json" in payload and isinstance(payload["credentials_json"], dict) and "static_ip" in payload["credentials_json"]:
        acc.static_ip = payload["credentials_json"]["static_ip"].strip() if payload["credentials_json"]["static_ip"] else None
    if "credentials_json" in payload:
        new_creds = payload["credentials_json"]
        if isinstance(new_creds, dict):
            curr_creds = dict(acc.credentials_json or {})
            curr_creds.update(new_creds)
            if acc.static_ip:
                curr_creds["static_ip"] = acc.static_ip
            if curr_creds.get("token") and not curr_creds.get("token_date"):
                curr_creds["token_date"] = datetime.date.today().isoformat()
            acc.credentials_json = curr_creds
            if curr_creds.get("token"):
                acc.is_logged_in = True
                acc.last_login_at = datetime.datetime.utcnow()
    db.commit()
    return {"success": True, "client_id": client_id, "static_ip": acc.static_ip}

@app.delete("/api/accounts/{client_id}")
def delete_account(client_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    acc = db.query(Account).filter(Account.client_id == client_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    if current_user.role != "ADMIN" and acc.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You do not have permission to delete this account.")

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
            adapter = create_broker_order_adapter("XTS", acc.client_id, creds, static_ip=acc.static_ip)
            
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
                "created_at": format_ist(o.created_at, "%H:%M:%S") if hasattr(o.created_at, "strftime") else str(o.created_at)
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
                "timestamp": format_ist(t.timestamp, "%H:%M:%S") if hasattr(t.timestamp, "strftime") else str(t.timestamp)
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
        "static_ip": acc.static_ip or (creds.get("static_ip", "") if creds else ""),
        "paper_mode": acc.paper_mode if acc else True,
        "credentials": {
            "api_key": creds.get("api_key", ""),
            "api_secret": creds.get("api_secret", ""),
            "token": token,
            "token_date": token_date,
            "interactive_url": creds.get("interactive_url", "https://ttblaze.iifl.com/interactive"),
            "static_ip": acc.static_ip or creds.get("static_ip", ""),
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
async def deploy_strategy(payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Deploys a new strategy instance from a registered template and maps client accounts"""
    template_key = payload.get("template_key")
    if not template_key:
        raise HTTPException(status_code=400, detail="template_key is required")

    template = db.query(StrategyTemplate).filter(StrategyTemplate.key == template_key).first()
    if not template:
        raise HTTPException(status_code=404, detail=f"Strategy template '{template_key}' not found")

    client_ids = payload.get("client_ids", [])
    if current_user.role != "ADMIN":
        user_cids = {a.client_id for a in db.query(Account).filter(Account.user_id == current_user.id).all()}
        for cid in client_ids:
            if cid not in user_cids:
                raise HTTPException(status_code=403, detail=f"Trading account '{cid}' does not belong to your user profile.")

    config = payload.get("config") or template.default_config or {}
    name_admin = payload.get("name_admin") or f"{template.name_admin} [{', '.join(client_ids) if client_ids else 'UNMAPPED'}]"
    name_user = payload.get("name_user") or template.name_user
    start_time = str(config.get("entry_time") or config.get("start_time") or "09:15:00")
    end_time = str(config.get("exit_time") or config.get("end_time") or "15:20:00")
    auto_start = bool(payload.get("auto_start", False))
    paper_mode = bool(payload.get("paper_mode", True))

    instance = StrategyInstance(
        template_key=template_key,
        user_id=current_user.id,
        instance_name_admin=name_admin,
        instance_name_user=name_user,
        client_ids=list(client_ids),
        status="STOPPED",
        paper_mode=paper_mode,
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
            "created": format_ist(t.created_at, "%Y-%m-%d %H:%M")
        } for t in templates
    ]

@app.get("/api/strategies/instances")
def list_strategy_instances(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Strategy lists for dashboard table with Admin & Non-Admin representation"""
    if current_user.role == "ADMIN":
        instances = db.query(StrategyInstance).all()
        accounts = {a.client_id: a for a in db.query(Account).all()}
    else:
        user_accs = db.query(Account).filter(Account.user_id == current_user.id).all()
        user_cids = {a.client_id for a in user_accs}
        accounts = {a.client_id: a for a in user_accs}
        all_insts = db.query(StrategyInstance).all()
        instances = [
            inst for inst in all_insts
            if inst.user_id == current_user.id or any(cid in user_cids for cid in (inst.client_ids or []))
        ]

    result = []
    for inst in instances:
        mapped_clients = []
        is_inst_open = inst.last_trade_status in ["OPEN", "IN_TRADE", "ENTRY_SENT"]
        inst_u = float(inst.unrealized_pnl or 0.0) if is_inst_open else 0.0
        inst_r = float(inst.realized_pnl or 0.0)
        inst_t = round(inst_u + inst_r, 2)

        for cid in (inst.client_ids or []):
            acc = accounts.get(cid)
            mapped_clients.append({
                "client_id": cid,
                "name": acc.client_name if acc else cid,
                "is_active": acc.is_active if acc else False,
                "paper_mode": acc.paper_mode if acc else True,
                "broker": acc.broker if acc else "UNKNOWN",
                "pnl": {
                    "unrealized": round(inst_u, 2),
                    "realized": round(inst_r, 2),
                    "total": round(inst_t, 2)
                }
            })

        # Multi-leg prices of last trade
        legs = inst.legs_info or []

        # Convert last_signal_time to IST string
        last_sig_dt = getattr(inst, "last_signal_time", None)
        if not last_sig_dt:
            from backend.strategy_manager.manager import strategy_supervisor
            w = strategy_supervisor.active_workers.get(inst.id)
            if w and w.get("last_signal_time"):
                last_sig_dt = w["last_signal_time"]

        last_sig_str = format_ist(last_sig_dt, "%H:%M:%S") if last_sig_dt else None

        result.append({
            "id": inst.id,
            "template_key": inst.template_key, # Admin sees this
            "name_admin": inst.instance_name_admin,
            "name_user": inst.instance_name_user, # Non-admin sees this only without slug
            "status": inst.status,
            "paper_mode": bool(getattr(inst, "paper_mode", True)),
            "client_ids": inst.client_ids,
            "mapped_clients": mapped_clients,
            "last_trade_status": inst.last_trade_status,
            "last_trade_at": format_ist(inst.last_trade_at, "%Y-%m-%d %H:%M:%S") if inst.last_trade_at else None,
            "last_signal_time": last_sig_str,
            "start_time": inst.start_time,
            "end_time": inst.end_time,
            "pnl": {
                "unrealized": round(inst.unrealized_pnl if inst.last_trade_status != "CLOSED" else 0.0, 2),
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
    if "paper_mode" in payload:
        inst.paper_mode = bool(payload["paper_mode"])
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

@app.post("/api/strategies/start-all")
async def start_all_strategy_instances():
    started = await strategy_supervisor.start_all_strategies()
    return {"success": True, "started_count": len(started), "strategy_ids": started}

@app.post("/api/strategies/stop-all")
async def stop_all_strategy_instances():
    stopped = await strategy_supervisor.stop_all_strategies()
    return {"success": True, "stopped_count": len(stopped), "strategy_ids": stopped}

@app.post("/api/strategies/{id}/toggle-mode")
def toggle_strategy_paper_mode(id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Toggles strategy paper mode vs live mode"""
    inst = db.query(StrategyInstance).filter(StrategyInstance.id == id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Strategy instance not found")
    check_strategy_owner_or_admin(inst, current_user, db)
    inst.paper_mode = not bool(getattr(inst, "paper_mode", True))
    db.commit()
    return {"success": True, "strategy_id": id, "paper_mode": inst.paper_mode}

@app.post("/api/strategies/{id}/set-mode")
def set_strategy_paper_mode(id: int, payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Sets strategy paper mode or live mode explicitly"""
    inst = db.query(StrategyInstance).filter(StrategyInstance.id == id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Strategy instance not found")
    check_strategy_owner_or_admin(inst, current_user, db)
    inst.paper_mode = bool(payload.get("paper_mode", True))
    db.commit()
    return {"success": True, "strategy_id": id, "paper_mode": inst.paper_mode}

@app.post("/api/strategies/{id}/duplicate")
async def duplicate_strategy_instance(
    id: int,
    payload: Dict[str, Any] = Body(...)
):
    """Duplicates an existing strategy with custom parameters or symbols"""
    new_admin_name = payload.get("name_admin", f"Cloned Strategy #{id}")
    new_user_name = payload.get("name_user", "Alpha Strategy (Copy)")
    custom_config = dict(payload.get("config") or {})
    if "paper_mode" in payload:
        custom_config["paper_mode"] = bool(payload["paper_mode"])
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
async def delete_strategy_instance(id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    inst = db.query(StrategyInstance).filter(StrategyInstance.id == id).first()
    if inst: check_strategy_owner_or_admin(inst, current_user, db)
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

@app.post("/api/strategies/{id}/reset-trade")
async def reset_strategy_trade_state(id: int, db: Session = Depends(get_db)):
    """
    Manually resets trade status to CLOSED, zeroes out unrealized PnL,
    and marks all open legs as CLOSED. Works whether strategy is STOPPED or RUNNING.
    """
    inst = db.query(StrategyInstance).filter(StrategyInstance.id == id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Strategy not found")

    inst.last_trade_status = "CLOSED"
    inst.unrealized_pnl = 0.0
    inst.total_pnl = inst.realized_pnl

    if inst.legs_info:
        closed_legs = []
        for leg in inst.legs_info:
            leg_str = str(leg.get("symbol") or leg.get("leg") or "")
            m = re.search(r"(\d+)", leg_str)
            if m and ("CRUDE" in leg_str.upper() or "MCX" in leg_str.upper()):
                strike_val = int(m.group(1))
                if strike_val > 15000 or strike_val < 3000:
                    continue  # Purge phantom strike from legs
            l_copy = dict(leg)
            l_copy["status"] = "CLOSED"
            if not l_copy.get("exit"):
                l_copy["exit"] = l_copy.get("ltp") or l_copy.get("entry") or 0.0
            l_copy["pnl"] = 0.0
            closed_legs.append(l_copy)
        inst.legs_info = closed_legs
        flag_modified(inst, "legs_info")

    db.query(Position).filter(Position.strategy_instance_id == id).update({
        Position.net_quantity: 0,
        Position.unrealized_pnl: 0.0
    })
    db.commit()
    db.refresh(inst)

    # If worker is active, also notify the worker process to reset state
    worker = strategy_supervisor.active_workers.get(id)
    if worker and "in_queue" in worker:
        try:
            worker["in_queue"].put({"type": "RESET_TRADE"})
        except Exception:
            pass

    return {
        "success": True,
        "strategy_id": id,
        "last_trade_status": inst.last_trade_status,
        "legs_info": inst.legs_info
    }

@app.get("/api/strategies/{id}/chart")
async def get_strategy_chart(id: int, db: Session = Depends(get_db)):
    inst = db.query(StrategyInstance).filter(StrategyInstance.id == id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Strategy not found")
    
    cfg = inst.config or {}
    if inst.template_key == "strat_pcp_pro_v201":
        from backend.strategy_manager.registered.pcp_pro_v201 import resolve_future_contract, normalize_underlying_root
        root = normalize_underlying_root(_extract_pcp_stock(inst or cfg, fallback="HAL"))
        symbol = resolve_future_contract(root)
    else:
        symbol = cfg.get("symbol") or cfg.get("underlying")
        if not symbol and inst.template and inst.template.default_config:
            symbol = inst.template.default_config.get("symbol")
        if not symbol:
            # Check active legs if available
            legs = inst.legs_info or []
            if legs and legs[0].get("symbol"):
                leg_sym = str(legs[0]["symbol"])
                if "CRUDE" in leg_sym:
                    symbol = "MCX:CRUDEOILM"
                elif "RELIANCE" in leg_sym:
                    symbol = "NSE:RELIANCE"
                elif "HAL" in leg_sym:
                    symbol = "NSE:HAL"
                elif "BHARTI" in leg_sym:
                    symbol = "NSE:BHARTIARTL"
                elif "NIFTY" in leg_sym:
                    symbol = "NSE:NIFTY"
                else:
                    symbol = leg_sym
            else:
                symbol = "NSE:NIFTY"

        if symbol and ":" not in symbol:
            if "CRUDE" in symbol.upper():
                symbol = f"MCX:{symbol}"
            else:
                symbol = f"NSE:{symbol}"

    tf = str(cfg.get("timeframe") or cfg.get("timeframe_min") or "5m").strip().lower()
    if tf.isdigit():
        tf = f"{tf}m"
    if tf not in ["1m", "3m", "5m", "15m", "1h"]:
        tf = "3m" if "pcp_pro" in str(inst.template_key) else ("1m" if "ftx" in str(inst.template_key) else "5m")

    ohlc = await feed_manager.get_ohlc(symbol, resolution=tf, count=40)
    
    # Calculate strategy intraday / session PnL series
    ist_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    now_ist = datetime.datetime.now(ist_tz)
    today_start = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    # Look back 7 days on weekends or pre-market so completed trading sessions show analysis
    lookback = datetime.timedelta(days=7) if (now_ist.weekday() >= 5 or now_ist.hour < 9) else datetime.timedelta(days=1)
    
    # Check trade reports for this strategy
    reports = db.query(TradeReport).filter(
        TradeReport.strategy_instance_id == id,
        TradeReport.created_at >= today_start - lookback
    ).order_by(TradeReport.created_at.asc()).all()
    
    # Also check trades table
    trades = db.query(Trade).filter(
        Trade.strategy_instance_id == id,
        Trade.timestamp >= today_start - lookback
    ).order_by(Trade.timestamp.asc()).all()
    
    has_active_legs = any(
        float(l.get("entry", 0.0) or 0.0) > 0 and "5850" not in str(l.get("symbol", ""))
        for l in (inst.legs_info or [])
    )
    has_trades = bool(reports or trades or has_active_legs)
    
    total_pnl = float(inst.total_pnl or 0.0)
    realized_pnl = float(inst.realized_pnl or 0.0)
    unrealized_pnl = float(inst.unrealized_pnl or 0.0)
    
    # Build PnL progression
    
    if not has_trades or (total_pnl == 0.0 and not reports):
        # Flat 0.00 curve for today when no trade has occurred
        pnl_series = [
            {"time": "09:15", "pnl": 0.0},
            {"time": "11:00", "pnl": 0.0},
            {"time": "13:00", "pnl": 0.0},
            {"time": now_ist.strftime("%H:%M"), "pnl": 0.0}
        ]
        has_trades = False
    else:
        cum_pnl = 0.0
        pnl_series = [{"time": "09:15", "pnl": 0.0}]
        for rep in reports:
            cum_pnl += float(rep.mtm or 0.0)
            rep_time = rep.closed_at or rep.opened_at or rep.created_at
            if rep_time:
                rep_ist = rep_time.replace(tzinfo=datetime.timezone.utc).astimezone(ist_tz)
                pnl_series.append({"time": rep_ist.strftime("%H:%M"), "pnl": round(cum_pnl, 2)})
        
        # Current point with live total PnL
        pnl_series.append({"time": now_ist.strftime("%H:%M"), "pnl": round(total_pnl, 2)})

    return {
        "strategy_id": id,
        "strategy_name": inst.instance_name_user or inst.instance_name_admin,
        "symbol": symbol,
        "resolution": tf,
        "status": inst.status,
        "last_trade_status": inst.last_trade_status,
        "has_trades": has_trades,
        "total_pnl": total_pnl,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "pnl_series": pnl_series,
        "ohlc": ohlc
    }


def _extract_pcp_stock(inst_or_cfg: Any, fallback: str = "HAL") -> str:
    """Safely extracts PCP stock symbol (HAL, RELIANCE, BHARTIARTL) from instance or config."""
    cand = ""
    if isinstance(inst_or_cfg, dict):
        cand = inst_or_cfg.get("underlying") or inst_or_cfg.get("symbol") or ""
    elif hasattr(inst_or_cfg, "config") and inst_or_cfg.config:
        cand = inst_or_cfg.config.get("underlying") or inst_or_cfg.config.get("symbol") or ""

    if cand and str(cand).strip().upper() not in ["NSE:NIFTY", "NIFTY", ""]:
        try:
            from backend.strategy_manager.registered.pcp_pro_v201 import normalize_underlying_root
            return normalize_underlying_root(str(cand))
        except Exception:
            pass

    # Check instance names if available
    name = ""
    if hasattr(inst_or_cfg, "instance_name_admin"):
        name += f" {inst_or_cfg.instance_name_admin or ''}"
    if hasattr(inst_or_cfg, "instance_name_user"):
        name += f" {inst_or_cfg.instance_name_user or ''}"
    name_up = name.upper()
    if "RELIANCE" in name_up:
        return "RELIANCE"
    if "BHARTI" in name_up:
        return "BHARTIARTL"
    if "HAL" in name_up:
        return "HAL"

    return fallback


@app.get("/api/charts/catalog")
def get_charts_catalog(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Returns deployed strategy instances and registered strategy templates selectable in the Charts page."""
    inst_q = db.query(StrategyInstance)
    if current_user.role != "ADMIN":
        user_cids = {a.client_id for a in db.query(Account).filter(Account.user_id == current_user.id).all()}
        instances = [
            inst for inst in inst_q.all()
            if inst.user_id == current_user.id or any(c in user_cids for c in (inst.client_ids or []))
        ]
    else:
        instances = inst_q.all()

    templates = db.query(StrategyTemplate).all()

    items = []
    for inst in instances:
        cfg = inst.config or (inst.template.default_config if inst.template else {}) or {}
        tf = str(cfg.get("timeframe") or cfg.get("timeframe_min") or ("3m" if "pcp_pro" in str(inst.template_key) else ("1m" if "ftx" in str(inst.template_key) else "5m"))).lower()
        if tf.isdigit():
            tf = f"{tf}m"

        underlying = cfg.get("symbol", "NSE:NIFTY")
        if "pcp_pro" in str(inst.template_key):
            underlying = _extract_pcp_stock(inst, fallback="HAL")

        items.append({
            "target": f"instance:{inst.id}",
            "kind": "instance",
            "id": inst.id,
            "template_key": inst.template_key,
            "label": f"{inst.instance_name_user or inst.instance_name_admin} (#{inst.id} - {inst.status}) ({underlying})",
            "label_admin": f"{inst.instance_name_admin} (#{inst.id} - {inst.status}) ({underlying})",
            "symbol": underlying,
            "underlying": underlying,
            "timeframe": tf,
            "status": inst.status,
            "chart_mode": "COMBINED_PREMIUM" if inst.template_key == "strat_time_atm_seller_v1" else "OHLC_CANDLES",
        })

    for tpl in templates:
        cfg = tpl.default_config or {}
        tf = str(cfg.get("timeframe") or cfg.get("timeframe_min") or ("3m" if "pcp_pro" in tpl.key else ("1m" if "ftx" in tpl.key else "5m"))).lower()
        if tf.isdigit():
            tf = f"{tf}m"

        if tpl.key == "strat_pcp_pro_v201":
            for stk in ["HAL", "RELIANCE", "BHARTIARTL"]:
                items.append({
                    "target": f"template:{tpl.key}:{stk}",
                    "kind": "template",
                    "id": None,
                    "template_key": tpl.key,
                    "label": f"[Template] {tpl.name_user} ({stk})",
                    "label_admin": f"[Template] {tpl.name_admin} ({tpl.key}) ({stk})",
                    "symbol": stk,
                    "underlying": stk,
                    "timeframe": tf,
                    "status": "TEMPLATE",
                    "chart_mode": "OHLC_CANDLES",
                })
        else:
            items.append({
                "target": f"template:{tpl.key}",
                "kind": "template",
                "id": None,
                "template_key": tpl.key,
                "label": f"[Template] {tpl.name_user}",
                "label_admin": f"[Template] {tpl.name_admin} ({tpl.key})",
                "symbol": cfg.get("symbol", "NSE:NIFTY"),
                "underlying": cfg.get("symbol", "NSE:NIFTY"),
                "timeframe": tf,
                "status": "TEMPLATE",
                "chart_mode": "COMBINED_PREMIUM" if tpl.key == "strat_time_atm_seller_v1" else "OHLC_CANDLES",
            })

    return {"items": items}


@app.get("/api/charts/data")
async def get_strategy_chart_data(
    target: str = "template:strat_time_atm_seller_v1",
    resolution: Optional[str] = None,
    stock: Optional[str] = None,
    lookback: Optional[str] = "1w",
    count: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Returns full charting payload for any of the 3 registered strategies:
    - strat_time_atm_seller_v1: Combined Premium Line Chart (CE + PE) + signals
    - strat_pcp_pro_v201: OHLC Candlestick Chart + Dual Chandelier, ADX, JMA(RWI) + signals (HAL, RELIANCE, BHARTIARTL)
    - strat_ftx_paper_v1: OHLC Candlestick Chart + VWAP, OR H/L, Session H/L, Prior Day H/L + cell signals
    - strat_supertrend_momentum_v1: OHLC Candlestick Chart + Supertrend, BB, Pivots, RSI, ATR + signals
    """
    inst: Optional[StrategyInstance] = None
    tpl: Optional[StrategyTemplate] = None

    if target.startswith("instance:"):
        try:
            inst_id = int(target.split(":", 1)[1])
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid instance target")
        inst = db.query(StrategyInstance).filter(StrategyInstance.id == inst_id).first()
        if not inst:
            raise HTTPException(status_code=404, detail="Strategy instance not found")
        can_view_trades = True
        if current_user.role != "ADMIN":
            user_cids = {a.client_id for a in db.query(Account).filter(Account.user_id == current_user.id).all()}
            if inst.user_id is not None and inst.user_id != current_user.id and not any(c in user_cids for c in (inst.client_ids or [])):
                can_view_trades = False
        tpl = inst.template or db.query(StrategyTemplate).filter(StrategyTemplate.key == inst.template_key).first()
    elif target.startswith("template:"):
        tpl_parts = target.split(":")
        tpl_key = tpl_parts[1]
        if len(tpl_parts) >= 3 and not stock:
            stock = tpl_parts[2]
        tpl = db.query(StrategyTemplate).filter(StrategyTemplate.key == tpl_key).first()
        if not tpl:
            raise HTTPException(status_code=404, detail=f"Strategy template '{tpl_key}' not found")
    else:
        raise HTTPException(status_code=400, detail="target must start with 'instance:' or 'template:'")

    template_key = inst.template_key if inst else tpl.key
    cfg = dict(tpl.default_config or {}) if tpl else {}
    if inst and inst.config:
        cfg.update(inst.config)

    symbol = str(cfg.get("symbol") or ("MCX:CRUDEOILM" if template_key == "strat_time_atm_seller_v1" else ("HAL" if template_key == "strat_pcp_pro_v201" else "NSE:NIFTY")))
    if template_key == "strat_pcp_pro_v201":
        from backend.strategy_manager.registered.pcp_pro_v201 import resolve_future_contract, normalize_underlying_root
        target_stock = stock or _extract_pcp_stock(inst or cfg, fallback="HAL")
        root = normalize_underlying_root(target_stock)
        fut_roll = bool(cfg.get("fut_roll_last_friday", True))
        symbol = resolve_future_contract(root, roll_last_friday=fut_roll)
        cfg["symbol"] = root
        cfg["underlying"] = root
    elif template_key == "strat_supertrend_momentum_v1":
        trade_inst = str(cfg.get("trade_instrument") or "FUTURE").upper()
        if trade_inst == "FUTURE":
            from backend.strategy_manager.registered.supertrend_momentum import resolve_future_contract
            symbol = resolve_future_contract("NIFTY")
        else:
            symbol = "NSE:NIFTY"
    default_tf = str(cfg.get("timeframe") or cfg.get("timeframe_min") or ("3m" if "pcp_pro" in template_key else ("1m" if "ftx" in template_key else "5m"))).lower()
    if default_tf.isdigit():
        default_tf = f"{default_tf}m"
    tf = (resolution or default_tf).strip().lower()
    if tf.isdigit():
        tf = f"{tf}m"
    if tf not in ["1m", "3m", "5m", "15m", "1h"]:
        tf = "3m" if "pcp_pro" in template_key else ("1m" if "ftx" in template_key else "5m")

    # Determine target bar count based on requested lookback & resolution
    lb = (lookback or "1w").lower().strip()
    if count is not None and count > 0:
        target_count = min(3500, count)
    else:
        step_min = 3 if "3m" in tf else (1 if "1m" in tf else (5 if "5m" in tf else (15 if "15m" in tf else 60)))
        trading_hours_per_day = 14.5 if "MCX" in symbol.upper() else 6.25
        bars_per_day = int((trading_hours_per_day * 60) / step_min) + 5
        if lb == "1d":
            target_count = max(130, bars_per_day)
        elif lb == "3d":
            target_count = max(400, bars_per_day * 3)
        elif lb == "1m":
            target_count = min(3500, max(2800, bars_per_day * 22))
        else:  # "1w" default
            target_count = min(3500, max(1200, bars_per_day * 8))

    ist_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

    # Executed trade records for this instance (overlay onto chart signals)
    executed_trades: List[Dict[str, Any]] = []
    if inst and can_view_trades:
        today_start = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        db_trades = db.query(Trade).filter(
            Trade.strategy_instance_id == inst.id,
            Trade.timestamp >= today_start - datetime.timedelta(days=7),
        ).order_by(Trade.timestamp.asc()).all()
        for t in db_trades:
            dt_t = t.timestamp.replace(tzinfo=datetime.timezone.utc).astimezone(ist_tz) if t.timestamp else datetime.datetime.now(ist_tz)
            executed_trades.append({
                "time": dt_t.isoformat(),
                "label_time": dt_t.strftime("%H:%M"),
                "symbol": t.symbol,
                "side": t.side,
                "price": round(float(t.price or 0.0), 2),
                "quantity": int(t.quantity or 0),
                "leg_id": t.leg_id or "",
            })

    # ------------------------------------------------------------------
    # 1. Combined Premium Line Chart (strat_time_atm_seller_v1)
    # ------------------------------------------------------------------
    if template_key == "strat_time_atm_seller_v1":
        ce_sym = None
        pe_sym = None
        ce_entry = 0.0
        pe_entry = 0.0
        ce_ltp_hint = 0.0
        pe_ltp_hint = 0.0

        if inst and inst.legs_info:
            for l in inst.legs_info:
                ls = str(l.get("symbol") or "")
                if ls.endswith("CE") and not ce_sym:
                    ce_sym = ls
                    ce_entry = float(l.get("entry") or 0.0)
                    ce_ltp_hint = float(l.get("ltp") or ce_entry or 0.0)
                elif ls.endswith("PE") and not pe_sym:
                    pe_sym = ls
                    pe_entry = float(l.get("entry") or 0.0)
                    pe_ltp_hint = float(l.get("ltp") or pe_entry or 0.0)

        underlying_tick = feed_manager.get_latest_tick(symbol)
        underlying_ltp = float(underlying_tick.get("ltp") or 0.0) if underlying_tick else 0.0
        if underlying_ltp <= 0.0:
            und_bars = await feed_manager.get_ohlc(symbol, resolution=tf, count=20)
            if und_bars:
                underlying_ltp = float(und_bars[-1].get("close") or 0.0)

        if not ce_sym or not pe_sym:
            step = float(cfg.get("strike_step") or 50.0)
            ref_spot = underlying_ltp if underlying_ltp > 0 else 6500.0
            strike = int(round(ref_spot / max(1.0, step)) * step)
            root = symbol.split(":")[-1]
            seg = "MCXFO" if "MCX" in symbol.upper() else "NFO"
            snapped_ce = symbol_mapper.snap_to_nearest_option_strike(root, strike, "CE")
            use_strike = snapped_ce if snapped_ce else strike
            ce_sym = ce_sym or f"{seg}:{root}{use_strike}CE"
            pe_sym = pe_sym or f"{seg}:{root}{use_strike}PE"

        # Seed active broker price hints so cold-start fallback anchors near real option LTPs
        broker_inst = feed_manager.active_broker or feed_manager.brokers.get("XTS")
        if broker_inst and hasattr(broker_inst, "_current_prices"):
            ce_tick = feed_manager.ticks_cache.get(ce_sym)
            pe_tick = feed_manager.ticks_cache.get(pe_sym)
            if ce_tick and float(ce_tick.get("ltp") or 0) > 0:
                broker_inst._current_prices[ce_sym] = float(ce_tick["ltp"])
            elif ce_ltp_hint > 0:
                broker_inst._current_prices[ce_sym] = ce_ltp_hint
            if pe_tick and float(pe_tick.get("ltp") or 0) > 0:
                broker_inst._current_prices[pe_sym] = float(pe_tick["ltp"])
            elif pe_ltp_hint > 0:
                broker_inst._current_prices[pe_sym] = pe_ltp_hint

        ce_bars = await feed_manager.get_ohlc(ce_sym, resolution=tf, count=target_count)
        pe_bars = await feed_manager.get_ohlc(pe_sym, resolution=tf, count=target_count)

        # Align CE & PE bars cleanly
        pe_by_label: Dict[str, float] = {}
        pe_list = []
        for pb in pe_bars:
            dt_p = pb.get("time")
            try:
                pdt = datetime.datetime.fromisoformat(str(dt_p).replace("Z", "+00:00"))
                if pdt.tzinfo is None:
                    pdt = pdt.replace(tzinfo=datetime.timezone.utc)
                lbl = pdt.astimezone(ist_tz).strftime("%H:%M")
            except Exception:
                lbl = str(dt_p)[-8:-3]
            val = float(pb.get("close") or 0.0)
            pe_by_label[lbl] = val
            pe_list.append(val)

        series = []
        for idx_c, cb in enumerate(ce_bars):
            dt_c = cb.get("time")
            try:
                cdt = datetime.datetime.fromisoformat(str(dt_c).replace("Z", "+00:00"))
                if cdt.tzinfo is None:
                    cdt = cdt.replace(tzinfo=datetime.timezone.utc)
                cdt_ist = cdt.astimezone(ist_tz)
                lbl = cdt_ist.strftime("%H:%M")
                ts_iso = cdt_ist.isoformat()
            except Exception:
                lbl = str(dt_c)[-8:-3]
                ts_iso = str(dt_c)
            ce_val = float(cb.get("close") or 0.0)
            pe_val = pe_by_label.get(lbl)
            if pe_val is None:
                pe_val = pe_list[idx_c] if idx_c < len(pe_list) else (pe_list[-1] if pe_list else ce_val)
            series.append({
                "time": ts_iso,
                "label": lbl,
                "ce_ltp": round(ce_val, 2),
                "pe_ltp": round(pe_val, 2),
                "combined_premium": round(ce_val + pe_val, 2),
            })

        # Override latest point with live tick cache if available
        live_ce = feed_manager.ticks_cache.get(ce_sym)
        live_pe = feed_manager.ticks_cache.get(pe_sym)
        if series and (live_ce or live_pe):
            last_pt = series[-1]
            if live_ce and float(live_ce.get("ltp") or 0) > 0:
                last_pt["ce_ltp"] = round(float(live_ce["ltp"]), 2)
            if live_pe and float(live_pe.get("ltp") or 0) > 0:
                last_pt["pe_ltp"] = round(float(live_pe["ltp"]), 2)
            last_pt["combined_premium"] = round(last_pt["ce_ltp"] + last_pt["pe_ltp"], 2)

        entry_combined = round(ce_entry + pe_entry, 2) if (ce_entry > 0 and pe_entry > 0) else None
        sl_per_lot = float(cfg.get("stop_loss_per_lot", 750.0))
        lot_sz = float(cfg.get("lot_size") or symbol_mapper.get_lot_size(symbol) or 10.0)
        sl_combined = round(entry_combined + (sl_per_lot / max(1.0, lot_sz)), 2) if entry_combined else None

        # Map signals (entry/exit cycles from DB + scheduled entry/exit markers)
        signals = []
        if inst and inst.legs_info:
            for l in inst.legs_info:
                if float(l.get("entry") or 0) > 0:
                    signals.append({
                        "label_time": str(cfg.get("entry_time", "15:45:00"))[:5],
                        "type": "SELL",
                        "price": entry_combined or round(float(l["entry"]) * 2, 2),
                        "label": f"STRADDLE ENTRY @ ₹{(entry_combined or float(l['entry'])):.2f}",
                    })
                    break
            for l in inst.legs_info:
                if l.get("exit") is not None and float(l.get("exit") or 0) > 0:
                    ce_x = sum(float(x.get("exit") or 0.0) for x in inst.legs_info if x.get("exit"))
                    signals.append({
                        "label_time": str(cfg.get("exit_time", "23:18:00"))[:5],
                        "type": "COVER",
                        "price": round(ce_x, 2),
                        "label": f"STRADDLE EXIT @ ₹{ce_x:.2f}",
                    })
                    break

        return {
            "target": target,
            "template_key": template_key,
            "strategy_name": (inst.instance_name_user or inst.instance_name_admin) if inst else tpl.name_user,
            "chart_mode": "COMBINED_PREMIUM",
            "symbol": symbol,
            "ce_symbol": ce_sym,
            "pe_symbol": pe_sym,
            "resolution": tf,
            "lookback": lb,
            "requested_count": target_count,
            "underlying_ltp": round(underlying_ltp, 2),
            "combined_series": series,
            "reference_lines": {
                "entry_combined": entry_combined,
                "stop_loss_combined": sl_combined,
                "entry_time": str(cfg.get("entry_time", "15:45:00"))[:5],
                "exit_time": str(cfg.get("exit_time", "23:18:00"))[:5],
            },
            "signals": signals,
            "executed_trades": executed_trades,
        }

    # ------------------------------------------------------------------
    # 2. PivotChandelier Pro / Supertrend Momentum / FTX (OHLC + Indicators)
    # ------------------------------------------------------------------
    bars = await feed_manager.get_ohlc(symbol, resolution=tf, count=target_count)
    if template_key == "strat_pcp_pro_v201":
        from backend.strategy_manager.registered.pcp_pro_v201 import compute_pcp_chart_payload
        payload = compute_pcp_chart_payload(bars, cfg)
    elif template_key == "strat_supertrend_momentum_v1":
        from backend.strategy_manager.registered.supertrend_momentum import compute_supertrend_chart_payload
        payload = compute_supertrend_chart_payload(bars, cfg)
    else:
        from backend.strategy_manager.registered.ftx_paper_research import compute_ftx_chart_payload
        payload = compute_ftx_chart_payload(bars, cfg)

    return {
        "target": target,
        "template_key": template_key,
        "strategy_name": (
            (inst.instance_name_user or inst.instance_name_admin)
            if (inst and (not stock or root in str(inst.instance_name_user or inst.instance_name_admin or "").upper()))
            else (f"{tpl.name_user} ({root})" if (template_key == "strat_pcp_pro_v201" and tpl) else (tpl.name_user if tpl else symbol))
        ),
        "chart_mode": "OHLC_CANDLES",
        "symbol": symbol,
        "underlying": root if template_key == "strat_pcp_pro_v201" else symbol,
        "resolution": tf,
        "lookback": lb,
        "requested_count": target_count,
        "candles": payload.get("candles", []),
        "overlays": payload.get("overlays", {}),
        "oscillators": payload.get("oscillators", {}),
        "signals": payload.get("signals", []),
        "executed_trades": executed_trades,
    }

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
                "timestamp": format_ist(t.timestamp, "%Y-%m-%d %H:%M:%S")
            } for t in trades
        ]
    }

@app.get("/api/strategies/{id}/logs")
def get_strategy_chronological_logs(id: int, include_candle_fetch: bool = True, db: Session = Depends(get_db)):
    """Consolidated strategy-level audit timeline with step-by-step progress, including recent signal checks"""
    trade_logs = db.query(SystemLog).filter(
        SystemLog.strategy_id == id,
        SystemLog.category != "CANDLE_FETCH"
    ).order_by(SystemLog.id.desc()).limit(60).all()

    candle_logs = []
    if include_candle_fetch:
        candle_logs = db.query(SystemLog).filter(
            SystemLog.strategy_id == id,
            SystemLog.category == "CANDLE_FETCH"
        ).order_by(SystemLog.id.desc()).limit(15).all()

    combined = sorted(trade_logs + candle_logs, key=lambda l: l.id, reverse=True)

    return [
        {
            "id": l.id,
            "category": l.category,
            "level": l.level,
            "message": l.message,
            "timestamp": format_ist_timeline(l.timestamp)
        } for l in combined
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

@app.post("/api/status/feed/switch")
async def switch_active_feed_broker(payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    """
    Directly switches the active feed source to the specified broker (XTS, SHOONYA, FYERS).
    Updates priority list so the chosen broker becomes primary (#1).
    Works in both unified in-process mode and multi-terminal IPC mode.
    """
    target = str(payload.get("broker") or payload.get("target") or "XTS").strip().upper()
    valid_brokers = ["XTS", "SHOONYA", "FYERS"]
    if target not in valid_brokers:
        raise HTTPException(status_code=400, detail=f"Invalid broker '{target}'. Must be one of: {valid_brokers}")

    # 1. Update database priority and statuses immediately
    statuses = db.query(FeedBrokerStatus).all()
    if statuses:
        for st in statuses:
            if st.broker.upper() == target:
                st.is_active = True
                st.priority = 1
            else:
                st.is_active = False
                if st.priority == 1:
                    st.priority = 2
        db.commit()

    # 2. In-process FeedManager or Multi-Terminal EventBus IPC
    if feed_manager._is_running and feed_manager.brokers:
        active = await feed_manager.switch_broker(target, "Triggered via Status Card Selection")
    else:
        await event_bus.publish("feed.switch", target)
        await asyncio.sleep(0.5)
        st = get_effective_feed_status(db)
        active = st.get("active_broker", target)

    return {"success": True, "active_broker": active, "message": f"Feed source switched to {active}"}

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
    category: Optional[str] = None, # BROKER_API, FEED_SUBSCRIPTION, STRATEGY_DECISION, CANDLE_FETCH
    level: Optional[str] = None,
    strategy_id: Optional[int] = None,
    limit: int = 150,
    db: Session = Depends(get_db)
):
    """
    Clear Log - No Noise.
    Divided into four categories:
    1. Broker API Log - ERROR / SUCCESS
    2. Feed Subscription - ERROR / SUCCESS / DATA RECEIVING (Includes OHLC fetch)
    3. Strategy Decision Log (Actionable trade signals, entries, exits, order updates)
    4. Candle Fetch Log (Isolated periodic polling bars)
    """
    q = db.query(SystemLog)
    if category:
        q = q.filter(SystemLog.category == category.upper())
    else:
        # Default 'All Logs' view excludes high-frequency CANDLE_FETCH logs to eliminate clutter
        q = q.filter(SystemLog.category != "CANDLE_FETCH")
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
                "timestamp": format_ist(e.timestamp, "%Y-%m-%d %H:%M:%S")
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
                "timestamp": format_ist(l.timestamp, "%Y-%m-%d %H:%M:%S")
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


# ==============================================================================
# REPORTS & AUDIT TRAIL API
# ==============================================================================

def parse_report_date(d_str: Optional[str], is_end_of_day: bool = False) -> Optional[datetime.datetime]:
    if not d_str or not isinstance(d_str, str):
        return None
    d_str = d_str.strip()
    dt = None
    for fmt in ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"]:
        try:
            dt = datetime.datetime.strptime(d_str, fmt)
            break
        except ValueError:
            pass
    if dt:
        if is_end_of_day:
            return dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        else:
            return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return None

def sync_real_engine_data_to_reports(db: Session):
    """
    Scans real StrategyInstance, Order, and Trade tables to ensure any real engine
    trades and active positions are represented in TradeReport.
    """
    try:
        # 1. Sync from real StrategyInstance states (both OPEN and CLOSED)
        instances = db.query(StrategyInstance).all()
        for inst in instances:
            legs = inst.legs_info or []
            if not legs:
                continue
            
            client_id = inst.client_ids[0] if (inst.client_ids and len(inst.client_ids) > 0) else "QUANTZEN"
            strat_name = inst.instance_name_user or inst.instance_name_admin or "MCX Straddle"
            
            active_legs = [l for l in legs if l.get("entry") or l.get("status") in ["FILLED", "OPEN", "CLOSED"]]
            if not active_legs:
                continue

            today_str = (inst.last_trade_at or datetime.datetime.utcnow()).strftime("%Y%m%d")
            cycle_id = f"TC_{today_str}_{client_id}_{inst.id}"

            rep = db.query(TradeReport).filter(TradeReport.cycle_id == cycle_id).first()
            if not rep:
                rep = TradeReport(
                    cycle_id=cycle_id,
                    client_id=client_id,
                    strategy_instance_id=inst.id,
                    strategy_name=strat_name,
                    opened_at=inst.last_trade_at or datetime.datetime.utcnow()
                )
                db.add(rep)

            sym = active_legs[0].get("symbol", "CRUDEOILM")
            rep.symbol = sym.replace("MCXFO:", "").replace("MCX:", "")
            rep.segment = "MCX" if "MCX" in sym.upper() else "NSE"
            rep.side = "SELL"
            rep.quantity = sum(abs(l.get("qty", 10)) for l in active_legs)
            rep.is_paper = False
            rep.mode = "LIVE"
            rep.entry_prices = [round(float(l.get("entry") or 0.0), 2) for l in active_legs if l.get("entry")]
            rep.exit_prices = [round(float(l.get("exit") or 0.0), 2) for l in active_legs if l.get("exit")]
            rep.legs_info = active_legs

            if inst.last_trade_status == "CLOSED":
                rep.status = "CLOSED"
                rep.closed_at = rep.closed_at or datetime.datetime.utcnow()
                rep.reason = "EXIT_TIME"
                rep.mtm = round(float(inst.realized_pnl or 0.0), 2)
            else:
                rep.status = "OPEN"
                rep.closed_at = None
                rep.reason = "-"
                rep.mtm = round(float(inst.unrealized_pnl or 0.0), 2)

        # 2. Check standalone real trades in Trade table
        trades = db.query(Trade).all()
        for trd in trades:
            trd_cycle_id = f"TRD_{trd.trade_id}"
            rep = db.query(TradeReport).filter(TradeReport.cycle_id == trd_cycle_id).first()
            if not rep:
                db.add(TradeReport(
                    cycle_id=trd_cycle_id,
                    client_id=trd.client_id or "QUANTZEN",
                    strategy_instance_id=trd.strategy_instance_id,
                    strategy_name="MCX Straddle" if "CRUDE" in trd.symbol else "Trading Alpha",
                    symbol=trd.symbol.replace("MCXFO:", "").replace("MCX:", ""),
                    segment=trd.segment or "MCX",
                    side=trd.side,
                    quantity=trd.quantity,
                    is_paper=False,
                    mode="LIVE",
                    opened_at=trd.timestamp or datetime.datetime.utcnow(),
                    closed_at=trd.timestamp,
                    entry_prices=[round(float(trd.price), 2)],
                    exit_prices=[round(float(trd.price), 2)],
                    reason="-",
                    mtm=0.0,
                    status="CLOSED"
                ))

        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[Reports] Error syncing real data to reports: {e}")

@app.get("/api/reports/filters")
async def get_reports_filters(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Returns REAL connected clients and strategies for reports filter panes"""
    sync_real_engine_data_to_reports(db)

    # 1. Real Client Accounts strictly from database
    if current_user.role == "ADMIN":
        accounts = db.query(Account).all()
    else:
        accounts = db.query(Account).filter(Account.user_id == current_user.id).all()
    clients = []
    seen_clients = set()
    for acc in accounts:
        if acc.client_id and acc.client_id not in seen_clients:
            seen_clients.add(acc.client_id)
            clients.append({
                "id": acc.client_id,
                "name": acc.client_name or acc.client_id,
                "status": "ACTIVE" if acc.is_active else "INACTIVE",
                "broker": acc.broker
            })
    
    # Also extract distinct clients from real TradeReport and Trade records
    try:
        rep_clients = db.query(TradeReport.client_id).distinct().all()
        for (cid,) in rep_clients:
            if cid and cid not in seen_clients:
                seen_clients.add(cid)
                clients.append({
                    "id": cid,
                    "name": cid,
                    "status": "ACTIVE",
                    "broker": "XTS"
                })
    except Exception:
        pass

    try:
        trd_clients = db.query(Trade.client_id).distinct().all()
        for (cid,) in trd_clients:
            if cid and cid not in seen_clients:
                seen_clients.add(cid)
                clients.append({
                    "id": cid,
                    "name": cid,
                    "status": "ACTIVE",
                    "broker": "XTS"
                })
    except Exception:
        pass

    # 2. Real Strategies from instances and reports
    instances = db.query(StrategyInstance).all()
    strategies = []
    seen_strat_names = set()
    for inst in instances:
        mapped_client = inst.client_ids[0] if (inst.client_ids and len(inst.client_ids) > 0) else (clients[0]["id"] if clients else "QUANTZEN")
        slug = inst.template_key.lower().replace("_", "-") if inst.template_key else "strat-time-atm-seller-v1"
        name = inst.instance_name_user or inst.instance_name_admin or "MCX Straddle"
        if name not in seen_strat_names:
            seen_strat_names.add(name)
            strategies.append({
                "id": inst.id,
                "name": name,
                "client_name": mapped_client,
                "slug": slug
            })

    # Include any distinct historical strategy names from TradeReport
    try:
        rep_strats = db.query(TradeReport.strategy_name).distinct().all()
        for (sname,) in rep_strats:
            if sname and sname not in seen_strat_names:
                seen_strat_names.add(sname)
                strategies.append({
                    "id": sname,
                    "name": sname,
                    "client_name": clients[0]["id"] if clients else "QUANTZEN",
                    "slug": "strat-time-atm-seller-v1" if "straddle" in sname.lower() or "mcx" in sname.lower() else sname.lower().replace(" ", "-")
                })
    except Exception:
        pass

    return {
        "success": True,
        "clients": clients,
        "strategies": strategies
    }

@app.get("/api/reports/trades")
async def get_reports_trades(
    client_ids: Optional[str] = Query(None),
    strategy_names: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
    mode: Optional[str] = Query("ALL"),
    status: Optional[str] = Query("ALL"),
    order: Optional[str] = Query("newest"),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Fetches real filtered trades with aggregated metrics (total trades, total MTM)"""
    sync_real_engine_data_to_reports(db)

    query = db.query(TradeReport)
    if current_user.role != "ADMIN":
        user_cids = [a.client_id for a in db.query(Account).filter(Account.user_id == current_user.id).all()]
        query = query.filter(TradeReport.client_id.in_(user_cids))

    if client_ids and isinstance(client_ids, str):
        c_list = [c.strip() for c in client_ids.split(",") if c.strip() and c.strip().lower() != "all"]
        if c_list:
            query = query.filter(TradeReport.client_id.in_(c_list))

    if strategy_names and isinstance(strategy_names, str):
        s_list = [s.strip() for s in strategy_names.split(",") if s.strip() and s.strip().lower() != "all"]
        if s_list:
            # Expand common aliases so MCX Straddle and Crude Short Straddle match seamlessly
            expanded_s_list = set(s_list)
            for s in s_list:
                s_lower = s.lower()
                if "straddle" in s_lower or "mcx" in s_lower or "crude" in s_lower:
                    expanded_s_list.add("MCX Straddle")
                    expanded_s_list.add("Crude Short Straddle")
                    expanded_s_list.add("MCX Time-Based ATM Strike Seller (CrudeOilM)")
                    expanded_s_list.add("MCX Automated ATM Seller")
            query = query.filter(TradeReport.strategy_name.in_(list(expanded_s_list)))

    if from_date and isinstance(from_date, str):
        f_dt = parse_report_date(from_date, is_end_of_day=False)
        if f_dt:
            query = query.filter(TradeReport.opened_at >= f_dt)

    if to_date and isinstance(to_date, str):
        t_dt = parse_report_date(to_date, is_end_of_day=True)
        if t_dt:
            query = query.filter(TradeReport.opened_at <= t_dt)

    if mode and isinstance(mode, str) and mode.upper() in ["LIVE", "PAPER"]:
        query = query.filter(TradeReport.mode == mode.upper())

    if status and isinstance(status, str) and status.upper() in ["CLOSED", "OPEN"]:
        query = query.filter(TradeReport.status == status.upper())

    if search and isinstance(search, str):
        pat = f"%{search.strip()}%"
        query = query.filter(
            (TradeReport.symbol.ilike(pat)) |
            (TradeReport.client_id.ilike(pat)) |
            (TradeReport.strategy_name.ilike(pat)) |
            (TradeReport.reason.ilike(pat))
        )

    if order and isinstance(order, str) and order.lower() == "oldest":
        query = query.order_by(TradeReport.opened_at.asc())
    else:
        query = query.order_by(TradeReport.opened_at.desc())

    reports = query.all()
    total_mtm = sum(r.mtm for r in reports)

    trade_list = []
    for r in reports:
        trade_list.append({
            "id": r.id,
            "cycle_id": r.cycle_id,
            "opened": format_ist(r.opened_at, "%m/%d/%Y, %I:%M:%S %p") if r.opened_at else "-",
            "closed": format_ist(r.closed_at, "%m/%d/%Y, %I:%M:%S %p") if r.closed_at else "-",
            "client": r.client_id,
            "strategy": r.strategy_name,
            "mode": r.mode,
            "symbol": r.symbol,
            "side": r.side,
            "qty": r.quantity,
            "entry_prices": r.entry_prices or [],
            "exit_prices": r.exit_prices or [],
            "reason": r.reason or "-",
            "mtm": round(r.mtm, 2),
            "status": r.status
        })

    return {
        "success": True,
        "total_trades": len(trade_list),
        "total_mtm": round(total_mtm, 2),
        "trades": trade_list
    }


# ==============================================================================
# USER & ROLE MANAGEMENT API
# ==============================================================================

@app.get("/api/users")
async def list_users(db: Session = Depends(get_db), current_user: User = Depends(require_admin)):
    """List all registered users with their roles and status"""
    users = db.query(User).order_by(User.id.asc()).all()
    user_list = []
    for u in users:
        user_list.append({
            "id": u.id,
            "username": u.username,
            "full_name": u.full_name or u.username,
            "email": u.email or "",
            "role": u.role,
            "is_active": u.is_active,
            "created_at": format_ist(u.created_at, "%Y-%m-%d %H:%M:%S") if u.created_at else "-"
        })
    return {
        "success": True,
        "total": len(user_list),
        "users": user_list
    }

@app.post("/api/users")
async def create_user(payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db), current_user: User = Depends(require_admin)):
    """Create a new user (ADMIN or USER)"""
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", "")).strip()
    role = str(payload.get("role", "USER")).strip().upper()
    full_name = str(payload.get("full_name", "")).strip()
    email = str(payload.get("email", "")).strip()
    is_active = bool(payload.get("is_active", True))

    if not username:
        raise HTTPException(status_code=400, detail="Username is required.")
    
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters.")

    if not password or len(password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters.")

    if role not in ["ADMIN", "USER"]:
        raise HTTPException(status_code=400, detail="Role must be either 'ADMIN' or 'USER'.")

    # Check for existing user (case-insensitive)
    existing = db.query(User).filter(User.username.ilike(username)).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Username '{username}' already exists.")

    new_user = User(
        username=username,
        full_name=full_name or username,
        email=email,
        password_hash=hash_user_password(password),
        role=role,
        is_active=is_active
    )
    db.add(new_user)

    # Log system event
    db.add(SystemLog(
        category="BROKER_API",
        level="SUCCESS",
        symbol="USER_MGMT",
        message=f"New {role} user created: '{username}' ({full_name or username})"
    ))

    db.commit()
    db.refresh(new_user)

    return {
        "success": True,
        "message": f"{role} user '{username}' created successfully.",
        "user": {
            "id": new_user.id,
            "username": new_user.username,
            "full_name": new_user.full_name,
            "email": new_user.email,
            "role": new_user.role,
            "is_active": new_user.is_active,
            "created_at": format_ist(new_user.created_at, "%Y-%m-%d %H:%M:%S") if new_user.created_at else "-"
        }
    }

@app.put("/api/users/{user_id}")
async def update_user(user_id: int, payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db), current_user: User = Depends(require_admin)):
    """Update user details, role, status, or password"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    new_role = payload.get("role")
    new_is_active = payload.get("is_active")

    # Guard: prevent removing or deactivating the last active ADMIN
    if (new_role and new_role.upper() != "ADMIN" and user.role == "ADMIN") or (new_is_active is False and user.role == "ADMIN"):
        active_admins = db.query(User).filter(User.role == "ADMIN", User.is_active == True, User.id != user_id).count()
        if active_admins == 0:
            raise HTTPException(status_code=400, detail="Cannot demote or deactivate the only remaining active Administrator.")

    if new_role and new_role.upper() in ["ADMIN", "USER"]:
        user.role = new_role.upper()

    if new_is_active is not None:
        user.is_active = bool(new_is_active)

    if "full_name" in payload:
        user.full_name = str(payload.get("full_name", "")).strip()

    if "email" in payload:
        user.email = str(payload.get("email", "")).strip()

    password = payload.get("password")
    if password and str(password).strip():
        pwd_str = str(password).strip()
        if len(pwd_str) < 4:
            raise HTTPException(status_code=400, detail="Password must be at least 4 characters.")
        user.password_hash = hash_user_password(pwd_str)

    user.updated_at = datetime.datetime.utcnow()
    db.commit()
    db.refresh(user)

    return {
        "success": True,
        "message": f"User '{user.username}' updated successfully.",
        "user": {
            "id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "email": user.email,
            "role": user.role,
            "is_active": user.is_active,
            "created_at": format_ist(user.created_at, "%Y-%m-%d %H:%M:%S") if user.created_at else "-"
        }
    }

@app.delete("/api/users/{user_id}")
async def delete_user(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(require_admin)):
    """Delete a user account"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    # Guard: prevent deleting the last active ADMIN
    if user.role == "ADMIN":
        active_admins = db.query(User).filter(User.role == "ADMIN", User.is_active == True, User.id != user_id).count()
        if active_admins == 0:
            raise HTTPException(status_code=400, detail="Cannot delete the sole remaining active Administrator.")

    username = user.username
    db.delete(user)
    db.add(SystemLog(
        category="BROKER_API",
        level="WARNING",
        symbol="USER_MGMT",
        message=f"User deleted: '{username}' (ID: {user_id})"
    ))
    db.commit()

    return {
        "success": True,
        "message": f"User '{username}' deleted successfully."
    }


# =============================================================================
# TELEGRAM NOTIFICATION SETTINGS & TEST ENDPOINTS
# =============================================================================

@app.get("/api/telegram/settings")
def get_telegram_settings(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Fetches telegram configuration for the authenticated user and admin multi-user controls"""
    setting = db.query(TelegramSetting).filter(TelegramSetting.user_id == current_user.id).first()
    
    from backend.core.telegram_notifier import telegram_notifier
    system_bot_token = telegram_notifier.get_system_default_bot_token(db)

    resp = {
        "success": True,
        "enabled": bool(setting.enabled) if setting else False,
        "bot_token": setting.bot_token if setting else "",
        "chat_id": setting.chat_id if setting else "",
        "notify_critical": bool(setting.notify_critical) if setting else True,
        "notify_warning": bool(setting.notify_warning) if setting else True,
        "notify_info": bool(setting.notify_info) if setting else True,
        "admin_muted_user_ids": (setting.admin_muted_user_ids or []) if setting else [],
        "system_bot_token_configured": bool(system_bot_token),
        "is_admin": current_user.role == "ADMIN"
    }

    # If Admin, attach list of all active users so Admin can selectively mute/unmute users
    if current_user.role == "ADMIN":
        all_users = db.query(User).filter(User.is_active == True).order_by(User.id.asc()).all()
        user_list = []
        for u in all_users:
            u_setting = db.query(TelegramSetting).filter(TelegramSetting.user_id == u.id).first()
            user_list.append({
                "id": u.id,
                "username": u.username,
                "full_name": u.full_name or u.username,
                "role": u.role,
                "has_telegram": bool(u_setting and u_setting.enabled and u_setting.chat_id),
                "is_muted_for_admin": (u.id in (setting.admin_muted_user_ids or [])) if setting else False
            })
        resp["users_for_admin"] = user_list

    return resp


@app.post("/api/telegram/settings")
def update_telegram_settings(
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Updates Telegram notification settings and admin user filters"""
    setting = db.query(TelegramSetting).filter(TelegramSetting.user_id == current_user.id).first()
    if not setting:
        setting = TelegramSetting(user_id=current_user.id)
        db.add(setting)

    if "enabled" in payload:
        setting.enabled = bool(payload["enabled"])
    if "bot_token" in payload:
        setting.bot_token = str(payload["bot_token"]).strip()
    if "chat_id" in payload:
        setting.chat_id = str(payload["chat_id"]).strip()
    if "notify_critical" in payload:
        setting.notify_critical = bool(payload["notify_critical"])
    if "notify_warning" in payload:
        setting.notify_warning = bool(payload["notify_warning"])
    if "notify_info" in payload:
        setting.notify_info = bool(payload["notify_info"])

    # Admin only: can specify list of user_ids to mute from admin's personal telegram
    if current_user.role == "ADMIN" and "admin_muted_user_ids" in payload:
        muted_ids = payload.get("admin_muted_user_ids")
        if isinstance(muted_ids, list):
            setting.admin_muted_user_ids = [int(uid) for uid in muted_ids if isinstance(uid, (int, str)) and str(uid).isdigit()]

    setting.updated_at = datetime.datetime.utcnow()
    db.commit()
    db.refresh(setting)

    return {
        "success": True,
        "message": "Telegram notification settings saved successfully.",
        "settings": {
            "enabled": setting.enabled,
            "bot_token": setting.bot_token,
            "chat_id": setting.chat_id,
            "notify_critical": setting.notify_critical,
            "notify_warning": setting.notify_warning,
            "notify_info": setting.notify_info,
            "admin_muted_user_ids": setting.admin_muted_user_ids or []
        }
    }


@app.post("/api/telegram/test")
async def test_telegram_notification(
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Sends a sample multi-tier test notification directly to the provided or saved Telegram chat
    """
    import html
    from backend.core.telegram_notifier import telegram_notifier

    # Check payload or fall back to saved user settings
    bot_token = payload.get("bot_token")
    chat_id = payload.get("chat_id")

    setting = db.query(TelegramSetting).filter(TelegramSetting.user_id == current_user.id).first()

    if not chat_id and setting:
        chat_id = setting.chat_id

    if not bot_token and setting and setting.bot_token:
        bot_token = setting.bot_token

    if not bot_token:
        bot_token = telegram_notifier.get_system_default_bot_token(db)

    if not bot_token:
        raise HTTPException(
            status_code=400,
            detail="No Telegram Bot Token provided. Please enter your Bot Token or ask Admin to configure System Bot Token."
        )

    if not chat_id:
        raise HTTPException(
            status_code=400,
            detail="No Telegram Chat ID provided. Please enter your numeric Chat ID (obtainable from @userinfobot)."
        )

    # Formatted sample test message demonstrating the 3 tiers
    now_str = datetime.datetime.now(IST_TZ).strftime("%Y-%m-%d %H:%M:%S IST")
    test_msg = (
        f"⚡ <b>FRESH TRADE ENGINE - TELEGRAM ALERT TEST</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ <b>Connection Status:</b> <b>CONNECTED & VERIFIED</b>\n"
        f"👤 <b>Recipient:</b> {html.escape(current_user.full_name or current_user.username)} (<code>{current_user.role}</code>)\n"
        f"⏰ <b>Timestamp:</b> {now_str} IST\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b><u>Sample 3-Level Notification Previews:</u></b>\n\n"
        f"🚨 <b>CRITICAL:</b> System failover, engine crashes, unhandled RMS halts\n"
        f"⚠️ <b>WARNING:</b> 1-hour pre-strategy token reminders & pre-market login alerts\n"
        f"ℹ️ <b>INFO:</b> Real-time order fills, position tracking & closed trade cycle PnL (🟢 +₹ / 🔴 -₹)\n\n"
        f"<i>Your Telegram notification channel is ready and active!</i>"
    )

    res = await telegram_notifier.send_raw_message(str(bot_token), str(chat_id), test_msg)
    if res.get("success"):
        return {
            "success": True,
            "message": "Test notification delivered to Telegram successfully! Check your Telegram app."
        }
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Telegram API delivery failed: {res.get('error', 'Unknown Telegram API error')}"
        )

