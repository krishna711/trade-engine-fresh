import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Text, JSON, ForeignKey, Enum
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    broker = Column(String(50), nullable=False) # XTS, KOTAKNEO, SHOONYA, ZERODHA, DHAN, FYERS
    client_id = Column(String(100), unique=True, nullable=False)
    client_name = Column(String(150), nullable=False)
    paper_mode = Column(Boolean, default=True) # Paper vs Live mode
    is_active = Column(Boolean, default=True)
    is_logged_in = Column(Boolean, default=True)
    static_ip = Column(String(50), nullable=True) # Dedicated outbound/static IP for broker requests
    credentials_json = Column(JSON, default=dict) # encrypted/masked creds
    margin_available = Column(Float, default=500000.0)
    margin_used = Column(Float, default=0.0)
    total_balance = Column(Float, default=500000.0)
    last_login_at = Column(DateTime, default=datetime.datetime.utcnow)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    user = relationship("User", back_populates="accounts")
    orders = relationship("Order", back_populates="account", cascade="all, delete-orphan")
    positions = relationship("Position", back_populates="account", cascade="all, delete-orphan")


class StrategyTemplate(Base):
    """Registered strategy definition in the system (available for Admin to map to clients)"""
    __tablename__ = "strategy_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(100), unique=True, nullable=False) # e.g. "strat_ema_cross_v1" (Admin only)
    name_admin = Column(String(150), nullable=False) # Admin visible name
    name_user = Column(String(150), nullable=False) # Non-admin user visible name (clean, no slug)
    description = Column(Text, default="")
    default_config = Column(JSON, default=dict)
    python_module = Column(String(150), nullable=False) # Path or class name
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    instances = relationship("StrategyInstance", back_populates="template")


class StrategyInstance(Base):
    """Configured instance of a strategy mapped to one or multiple accounts"""
    __tablename__ = "strategy_instances"

    id = Column(Integer, primary_key=True, autoincrement=True)
    template_key = Column(String(100), ForeignKey("strategy_templates.key"), nullable=False)
    instance_name_admin = Column(String(150), nullable=False) # e.g. "EMA 9-21 Alpha Client1"
    instance_name_user = Column(String(150), nullable=False) # e.g. "Trend Momentum Alpha"
    client_ids = Column(JSON, default=list) # List of client_ids mapped to this strategy (parallel execution)
    status = Column(String(20), default="STOPPED") # RUNNING, STOPPED, ERROR
    paper_mode = Column(Boolean, default=True) # Strategy-level Paper vs Live mode
    config = Column(JSON, default=dict) # Custom parameters for this instance
    
    # Timing
    start_time = Column(String(20), default="09:15:00")
    end_time = Column(String(20), default="15:20:00")
    
    # State tracking
    last_trade_status = Column(String(20), default="CLOSED") # OPEN, CLOSED
    last_trade_at = Column(DateTime, nullable=True)
    
    # Consolidated PnL
    unrealized_pnl = Column(Float, default=0.0)
    realized_pnl = Column(Float, default=0.0)
    total_pnl = Column(Float, default=0.0)
    
    # Multi-leg price details of last trade (list of dicts: [{leg: "CE", entry: 120.5, exit: 145.0, ltp: 135.0}])
    legs_info = Column(JSON, default=list)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    user = relationship("User", back_populates="strategy_instances")
    template = relationship("StrategyTemplate", back_populates="instances")
    orders = relationship("Order", back_populates="strategy_instance")


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(100), unique=True, nullable=False) # Engine universal order id
    broker_order_id = Column(String(100), nullable=True)
    client_id = Column(String(100), ForeignKey("accounts.client_id"), nullable=False)
    strategy_instance_id = Column(Integer, ForeignKey("strategy_instances.id"), nullable=True)
    
    symbol = Column(String(100), nullable=False) # Canonical symbol e.g. "NSE:NIFTY"
    segment = Column(String(20), nullable=False) # NSE, NFO, BSE, BSEFO, MCX, MCXFO
    broker = Column(String(50), nullable=False)
    
    side = Column(String(10), nullable=False) # BUY / SELL
    order_type = Column(String(20), default="MARKET") # MARKET, LIMIT, SL, SL-M
    product = Column(String(20), default="MIS") # MIS, CNC, NRML
    quantity = Column(Integer, nullable=False)
    price = Column(Float, default=0.0)
    trigger_price = Column(Float, default=0.0)
    
    status = Column(String(20), default="NEW") # NEW, PENDING, FILLED, REJECTED, CANCELLED
    filled_quantity = Column(Integer, default=0)
    average_price = Column(Float, default=0.0)
    rejection_reason = Column(Text, nullable=True)
    
    is_paper = Column(Boolean, default=True)
    leg_id = Column(String(50), nullable=True) # for multi-leg orders: "LEG_1_CE", "LEG_2_PE"
    
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    account = relationship("Account", back_populates="orders")
    strategy_instance = relationship("StrategyInstance", back_populates="orders")
    trades = relationship("Trade", back_populates="order")


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(String(100), unique=True, nullable=False)
    order_id = Column(String(100), ForeignKey("orders.order_id"), nullable=False)
    client_id = Column(String(100), nullable=False)
    strategy_instance_id = Column(Integer, nullable=True)
    
    symbol = Column(String(100), nullable=False)
    segment = Column(String(20), nullable=False)
    side = Column(String(10), nullable=False) # BUY / SELL
    quantity = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)
    leg_id = Column(String(50), nullable=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)

    order = relationship("Order", back_populates="trades")


class Position(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_id = Column(String(100), ForeignKey("accounts.client_id"), nullable=False)
    strategy_instance_id = Column(Integer, nullable=True)
    
    symbol = Column(String(100), nullable=False)
    segment = Column(String(20), nullable=False)
    product = Column(String(20), default="MIS")
    
    net_quantity = Column(Integer, default=0)
    buy_quantity = Column(Integer, default=0)
    buy_amount = Column(Float, default=0.0)
    buy_avg_price = Column(Float, default=0.0)
    
    sell_quantity = Column(Integer, default=0)
    sell_amount = Column(Float, default=0.0)
    sell_avg_price = Column(Float, default=0.0)
    
    ltp = Column(Float, default=0.0)
    unrealized_pnl = Column(Float, default=0.0)
    realized_pnl = Column(Float, default=0.0)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    account = relationship("Account", back_populates="positions")


class SymbolMapping(Base):
    """Universal symbol mapping across all brokers"""
    __tablename__ = "symbol_mappings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    canonical_symbol = Column(String(100), unique=True, nullable=False) # e.g. "NSE:NIFTY"
    display_name = Column(String(100), nullable=False)
    segment = Column(String(20), nullable=False) # NSE, NFO, BSE, BSEFO, MCX, MCXFO
    exchange = Column(String(20), nullable=False)
    
    # Broker specific tokens/symbols
    xts_token = Column(String(50), nullable=True)
    shoonya_token = Column(String(50), nullable=True)
    fyers_token = Column(String(50), nullable=True)
    zerodha_token = Column(String(50), nullable=True)
    dhan_token = Column(String(50), nullable=True)
    kotak_token = Column(String(50), nullable=True)
    
    lot_size = Column(Integer, default=1)
    tick_size = Column(Float, default=0.05)
    is_commodity_future = Column(Boolean, default=False)
    expiry_date = Column(DateTime, nullable=True)
    underlying = Column(String(50), nullable=True)


class SystemLog(Base):
    """Structured events/logs divided into 3 categories with prompt error dismissal"""
    __tablename__ = "system_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    category = Column(String(50), nullable=False) # BROKER_API, FEED_SUBSCRIPTION, STRATEGY_DECISION
    level = Column(String(20), default="INFO") # INFO, SUCCESS, WARNING, ERROR, DATA_RECEIVING, EMPTY_RESULT
    message = Column(String(500), nullable=False)
    details = Column(JSON, default=dict)
    strategy_id = Column(Integer, nullable=True)
    client_id = Column(String(100), nullable=True)
    symbol = Column(String(100), nullable=True)
    
    is_dismissed = Column(Boolean, default=False) # For highlighting prompt errors until user dismisses
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)


class FeedBrokerStatus(Base):
    __tablename__ = "feed_broker_statuses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    broker = Column(String(50), unique=True, nullable=False) # XTS, SHOONYA, FYERS
    priority = Column(Integer, default=1)
    is_connected = Column(Boolean, default=False)
    is_active = Column(Boolean, default=False) # Current active source
    latency_ms = Column(Float, default=0.0)
    last_heartbeat = Column(DateTime, nullable=True)
    error_count = Column(Integer, default=0)
    subscribed_count = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class TradeReport(Base):
    """Historical and live trade cycle records for comprehensive multi-client & strategy reporting"""
    __tablename__ = "trade_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cycle_id = Column(String(100), unique=True, nullable=False)
    client_id = Column(String(100), nullable=False)
    strategy_instance_id = Column(Integer, nullable=True)
    strategy_name = Column(String(150), nullable=False)
    
    symbol = Column(String(100), nullable=False)
    segment = Column(String(20), default="MCX")
    side = Column(String(10), default="SELL")
    quantity = Column(Integer, default=1)
    
    is_paper = Column(Boolean, default=False)
    mode = Column(String(10), default="LIVE") # "LIVE" or "PAPER"
    
    opened_at = Column(DateTime, nullable=False)
    closed_at = Column(DateTime, nullable=True)
    
    entry_prices = Column(JSON, default=list) # e.g. [459.00, 440.70]
    exit_prices = Column(JSON, default=list)  # e.g. [404.35, 582.00]
    legs_info = Column(JSON, default=list)
    
    reason = Column(String(50), default="-")  # e.g. "EXIT_TIME", "STOP_LOSS", "MANUAL", "-"
    mtm = Column(Float, default=0.0)
    status = Column(String(20), default="CLOSED") # "CLOSED" or "OPEN"
    
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)



class User(Base):
    """System users with role-based access control (ADMIN vs USER)"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(80), unique=True, nullable=False, index=True)
    full_name = Column(String(150), default="")
    email = Column(String(150), nullable=True)
    password_hash = Column(String(200), nullable=False)
    role = Column(String(20), default="USER", nullable=False)  # "ADMIN" or "USER"
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    accounts = relationship("Account", back_populates="user")
    strategy_instances = relationship("StrategyInstance", back_populates="user")
    telegram_setting = relationship("TelegramSetting", back_populates="user", uselist=False, cascade="all, delete-orphan")


class TelegramSetting(Base):
    """User-specific Telegram notification credentials and preferences"""
    __tablename__ = "telegram_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    enabled = Column(Boolean, default=False)
    bot_token = Column(String(200), default="")
    chat_id = Column(String(100), default="")
    
    # 3-level notification category filters
    notify_critical = Column(Boolean, default=True) # System critical alerts, feed drops, crashes
    notify_warning = Column(Boolean, default=True)  # Token expiry, 1-hr pre-start reminders
    notify_info = Column(Boolean, default=True)     # Orders placed/filled, trade closed with PnL

    # Admin only: List of user IDs that Admin mutes from their own Telegram feed
    # e.g. [2, 5] -> Admin will NOT receive notifications generated for user 2 and 5
    admin_muted_user_ids = Column(JSON, default=list)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    user = relationship("User", back_populates="telegram_setting")

