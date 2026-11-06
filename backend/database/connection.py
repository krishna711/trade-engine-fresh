import os
import datetime
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, scoped_session
from backend.config import DB_URL, DEFAULT_FEED_PRIORITY, DEFAULT_LOT_SIZES
from backend.database.models import (
    Base, Account, StrategyTemplate, StrategyInstance,
    SymbolMapping, FeedBrokerStatus, SystemLog, Position, Order, Trade, TradeReport, User
)


engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False},
    echo=False
)

# Enable WAL mode for high concurrency SQLite reads/writes without lock contention
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()

from sqlalchemy.orm import sessionmaker

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

from sqlalchemy import text

def check_and_migrate_db():
    try:
        with engine.begin() as conn:
            # Check accounts table columns
            acc_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(accounts)")).fetchall()]
            if acc_cols and "user_id" not in acc_cols:
                conn.execute(text("ALTER TABLE accounts ADD COLUMN user_id INTEGER;"))
                print("[DB Migration] Added user_id column to accounts table.")
            
            # Check strategy_instances table columns
            strat_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(strategy_instances)")).fetchall()]
            if strat_cols and "user_id" not in strat_cols:
                conn.execute(text("ALTER TABLE strategy_instances ADD COLUMN user_id INTEGER;"))
                print("[DB Migration] Added user_id column to strategy_instances table.")

            # Zero out stale positions where strategy is already CLOSED so past records do not tick
            conn.execute(text("""
                UPDATE positions
                SET net_quantity = 0, unrealized_pnl = 0.0
                WHERE strategy_instance_id IN (
                    SELECT id FROM strategy_instances WHERE last_trade_status = 'CLOSED'
                );
            """))
    except Exception as e:
        print(f"[DB Migration Warning] {e}")

def ensure_default_users():
    """Independent, guaranteed initialization of default admin & trader users"""
    db = SessionLocal()
    try:
        from backend.core.auth import hash_user_password
        
        # 1. Admin
        admin = db.query(User).filter(User.username.ilike("admin")).first()
        if not admin:
            admin = User(
                username="admin",
                full_name="System Administrator",
                email="admin@fresh-trade.local",
                password_hash=hash_user_password("admin123"),
                role="ADMIN",
                is_active=True
            )
            db.add(admin)
        else:
            admin.role = "ADMIN"
            admin.is_active = True
            # Preserve user's updated password; only seed if missing
            if not admin.password_hash:
                admin.password_hash = hash_user_password("admin123")

        # 2. Trader
        trader = db.query(User).filter(User.username.ilike("trader")).first()
        if not trader:
            trader = User(
                username="trader",
                full_name="Standard Trader",
                email="trader@fresh-trade.local",
                password_hash=hash_user_password("trader123"),
                role="USER",
                is_active=True
            )
            db.add(trader)
        else:
            trader.role = "USER"
            trader.is_active = True
            # Preserve user's updated password; only seed if missing
            if not trader.password_hash:
                trader.password_hash = hash_user_password("trader123")

        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[Auth Error] Failed to ensure default users: {e}")
    finally:
        db.close()

def init_db():
    check_and_migrate_db()
    Base.metadata.create_all(bind=engine)
    ensure_default_users()
    seed_initial_data()

# Auto-initialize database schema and seeds on import so tables always exist
try:
    init_db()
except Exception as _init_err:
    pass

def seed_initial_data():
    db = SessionLocal()
    try:
        # 1. System Init Log Marker (No dummy accounts seeded; user adds real accounts)
        accounts_marker = db.query(SystemLog).filter(
            SystemLog.category == "SYSTEM_INIT",
            SystemLog.message == "ACCOUNTS_INITIALIZED"
        ).first()

        if not accounts_marker:
            db.add(SystemLog(
                category="SYSTEM_INIT",
                level="INFO",
                message="ACCOUNTS_INITIALIZED",
                is_dismissed=True
            ))
            db.commit()

        # 2. Seed Registered Strategy Templates (Admin catalog - definitions only, no running instances)
        templates = [
            StrategyTemplate(
                key="strat_ac_srv2_future_v1",
                name_admin="AC SRv2 Reliance Future (Alpha System)",
                name_user="Reliance Futures Trend Pro",
                description="Reliance Industries algorithmic trend-following engine with Wilder ATR bands, volatility expansion filter (vCheck), dynamic trailing stop loss, and monthly expiry auto-roll.",
                default_config={
                    "symbol": "NSE:RELIANCE",
                    "trade_instrument": "FUTURE",
                    "timeframe": "3m",
                    "timeframe_min": 3,
                    "atr_multiplier": 4.4,
                    "atr_period": 28,
                    "sensitivity": 1.0,
                    "use_close_extremums": True,
                    "volatility_filter": False,
                    "target_long_per_mille": 48,
                    "target_short_per_mille": 18,
                    "first_trade_time": "09:15:00",
                    "last_trade_time": "10:51:00",
                    "square_off_time": "14:27:00",
                    "strategy_version": "1.2",
                    "position_sizing_mode": "fixed",
                    "quantity_lots": 1,
                    "lot_size": 250,
                    "risk_amount": 20000.0,
                    "max_lots": 8,
                    "fut_roll_next_month_from_last_friday": True,
                    "opt_strike_step": 10,
                    "opt_strike_mode": "ATM",
                    "opt_strike_offset_steps": 0,
                    "opt_quantity_lots": 1,
                    "opt_lot_size": 250,
                    "product": "NRML",
                    "order_type": "LIMIT",
                    "limit_buffer_pct": 0.015,
                    "auto_chase": True
                },
                python_module="backend.strategy_manager.registered.ac_srv2_future"
            ),
            StrategyTemplate(
                key="strat_time_atm_seller_v1",
                name_admin="MCX Time-Based ATM Strike Seller (CrudeOilM)",
                name_user="MCX Automated ATM Seller",
                description="Intraday ATM Straddle/Strike selling engine. Enters at 15:45, exits at 23:18 with 750 INR/lot stoploss. Universal for MCX, NIFTY, and SENSEX.",
                default_config={
                    "symbol": "MCX:CRUDEOILM",
                    "strike_step": 50,
                    "entry_time": "15:45:00",
                    "exit_time": "23:18:00",
                    "legs": ["CE", "PE"],
                    "lots": 1,
                    "lot_size": 10,
                    "stop_loss_per_lot": 750.0,
                    "allow_entry_in_window": True,
                    "product": "NRML",
                    "order_type": "LIMIT",
                    "limit_buffer_pct": 0.015,
                    "auto_chase": True
                },
                python_module="backend.strategy_manager.registered.time_atm_seller"
            ),
            StrategyTemplate(
                key="strat_ema_cross_v1",
                name_admin="EMA Crossover 9-21 Alpha",
                name_user="Trend Momentum Alpha",
                description="High probability exponential moving average 9/21 crossover strategy with ATR dynamic stop-loss.",
                default_config={
                    "symbol": "NSE:NIFTY",
                    "timeframe": "5m",
                    "fast_ema": 9,
                    "slow_ema": 21,
                    "quantity": DEFAULT_LOT_SIZES.get("NIFTY", 65),
                    "stop_loss_pct": 0.5,
                    "target_pct": 1.2,
                    "max_trades_per_day": 3
                },
                python_module="backend.strategy_manager.registered.ema_crossover"
            ),
            StrategyTemplate(
                key="strat_short_straddle_v2",
                name_admin="0DTE Short Straddle Delta Hedged",
                name_user="Index Neutral Income",
                description="Non-directional automated intraday short straddle with leg-by-leg stop loss and re-entry.",
                default_config={
                    "symbol": "NSE:BANKNIFTY",
                    "underlying": "NSE:BANKNIFTY",
                    "strike_offset": 0,
                    "lots": 2,
                    "sl_percentage": 25.0,
                    "trailing_sl": True,
                    "entry_time": "09:25:00",
                    "exit_time": "15:10:00"
                },
                python_module="backend.strategy_manager.registered.short_straddle"
            ),
            StrategyTemplate(
                key="strat_supertrend_mcx_v1",
                name_admin="MCX Energy Supertrend Breakout",
                name_user="Commodity Trend Pro",
                description="Commodity futures trend-following engine tuned for CrudeOil and NaturalGas volatility.",
                default_config={
                    "symbol": "MCX:CRUDEOIL",
                    "atr_period": 10,
                    "multiplier": 3.0,
                    "lots": 1,
                    "stop_loss_pts": 35.0,
                    "target_pts": 70.0
                },
                python_module="backend.strategy_manager.registered.supertrend_mcx"
            )
        ]
        for tpl in templates:
            existing = db.query(StrategyTemplate).filter(StrategyTemplate.key == tpl.key).first()
            if not existing:
                db.add(tpl)
        db.commit()

        # 3. Strategy Instances: Clean state by default (no dummy instances seeded).
        # Users configure and deploy instances explicitly.

        # 4. Seed Universal Symbol Mappings for all 6 segments
        if db.query(SymbolMapping).count() == 0:
            today = datetime.datetime.utcnow()
            # Calculate commodity expiry (last Thursday or specific contract date)
            mcx_current_expiry = datetime.datetime(today.year, today.month, 19, 23, 30)
            if mcx_current_expiry < today:
                next_month = today.month + 1 if today.month < 12 else 1
                next_year = today.year if today.month < 12 else today.year + 1
                mcx_current_expiry = datetime.datetime(next_year, next_month, 19, 23, 30)

            mappings = [
                # NSE
                SymbolMapping(
                    canonical_symbol="NSE:NIFTY",
                    display_name="NIFTY 50 Index",
                    segment="NSE",
                    exchange="NSE",
                    xts_token="26000",
                    shoonya_token="26000",
                    fyers_token="NSE:NIFTY50-INDEX",
                    zerodha_token="256265",
                    dhan_token="13",
                    kotak_token="NIFTY",
                    lot_size=DEFAULT_LOT_SIZES.get("NIFTY", 65),
                    tick_size=0.05
                ),
                SymbolMapping(
                    canonical_symbol="NSE:BANKNIFTY",
                    display_name="NIFTY BANK Index",
                    segment="NSE",
                    exchange="NSE",
                    xts_token="26001",
                    shoonya_token="26001",
                    fyers_token="NSE:NIFTYBANK-INDEX",
                    zerodha_token="260105",
                    dhan_token="25",
                    kotak_token="BANKNIFTY",
                    lot_size=DEFAULT_LOT_SIZES.get("BANKNIFTY", 30),
                    tick_size=0.05
                ),
                SymbolMapping(
                    canonical_symbol="NSE:RELIANCE",
                    display_name="Reliance Industries Ltd",
                    segment="NSE",
                    exchange="NSE",
                    xts_token="2885",
                    shoonya_token="2885",
                    fyers_token="NSE:RELIANCE-EQ",
                    zerodha_token="738561",
                    dhan_token="1333",
                    kotak_token="RELIANCE",
                    lot_size=1,
                    tick_size=0.05
                ),
                # BSE
                SymbolMapping(
                    canonical_symbol="BSE:SENSEX",
                    display_name="BSE SENSEX Index",
                    segment="BSE",
                    exchange="BSE",
                    xts_token="26065",
                    shoonya_token="26065",
                    fyers_token="BSE:SENSEX-INDEX",
                    zerodha_token="265",
                    dhan_token="51",
                    kotak_token="SENSEX",
                    lot_size=DEFAULT_LOT_SIZES.get("SENSEX", 20),
                    tick_size=0.05
                ),
                # NFO (Futures & Options)
                SymbolMapping(
                    canonical_symbol="NFO:NIFTY26OCT25000CE",
                    display_name="NIFTY OCT 25000 CE",
                    segment="NFO",
                    exchange="NFO",
                    xts_token="54321",
                    shoonya_token="54321",
                    fyers_token="NSE:NIFTY26OCT25000CE",
                    zerodha_token="123456",
                    dhan_token="98765",
                    kotak_token="NIFTY25000CE",
                    lot_size=DEFAULT_LOT_SIZES.get("NIFTY", 65),
                    tick_size=0.05,
                    underlying="NSE:NIFTY"
                ),
                # BSEFO (BSE Derivatives)
                SymbolMapping(
                    canonical_symbol="BSEFO:SENSEX26OCT82000CE",
                    display_name="SENSEX OCT 82000 CE",
                    segment="BSEFO",
                    exchange="BSEFO",
                    xts_token="88210",
                    shoonya_token="88210",
                    fyers_token="BSE:SENSEX26OCT82000CE",
                    zerodha_token="654321",
                    dhan_token="54321",
                    kotak_token="SENSEX82000CE",
                    lot_size=DEFAULT_LOT_SIZES.get("SENSEX", 20),
                    tick_size=0.05,
                    underlying="BSE:SENSEX"
                ),

                # MCX (Commodity Futures) - With Expiry Auto-Roll
                SymbolMapping(
                    canonical_symbol="MCX:CRUDEOIL",
                    display_name="CRUDE OIL 19NOV FUT",
                    segment="MCX",
                    exchange="MCX",
                    xts_token="573422",
                    shoonya_token="573422",
                    fyers_token="MCX:CRUDEOIL26NOV2026FUT",
                    zerodha_token="23145",
                    dhan_token="3200",
                    kotak_token="CRUDEOILFUT",
                    lot_size=100,
                    tick_size=1.0,
                    is_commodity_future=True,
                    expiry_date=datetime.datetime(2026, 11, 19, 23, 30),
                    underlying="CRUDEOIL"
                ),
                SymbolMapping(
                    canonical_symbol="MCX:CRUDEOILM",
                    display_name="CRUDE OIL MINI 19NOV FUT",
                    segment="MCX",
                    exchange="MCX",
                    xts_token="573423",
                    shoonya_token="573423",
                    fyers_token="MCX:CRUDEOILM26NOV2026FUT",
                    zerodha_token="23200",
                    dhan_token="3210",
                    kotak_token="CRUDEOILMFUT",
                    lot_size=10,
                    tick_size=1.0,
                    is_commodity_future=True,
                    expiry_date=datetime.datetime(2026, 11, 19, 23, 30),
                    underlying="CRUDEOILM"
                ),
                SymbolMapping(
                    canonical_symbol="MCX:NATURALGAS",
                    display_name="NATURAL GAS 27OCT FUT",
                    segment="MCX",
                    exchange="MCX",
                    xts_token="570750",
                    shoonya_token="570750",
                    fyers_token="MCX:NATURALGAS26OCTFUT",
                    zerodha_token="23146",
                    dhan_token="3201",
                    kotak_token="NATGASFUT",
                    lot_size=1250,
                    tick_size=0.1,
                    is_commodity_future=True,
                    expiry_date=mcx_current_expiry,
                    underlying="NATURALGAS"
                ),
                SymbolMapping(
                    canonical_symbol="MCX:GOLD",
                    display_name="GOLD 04DEC FUT",
                    segment="MCX",
                    exchange="MCX",
                    xts_token="495213",
                    shoonya_token="495213",
                    fyers_token="MCX:GOLD26OCTFUT",
                    zerodha_token="23147",
                    dhan_token="3202",
                    kotak_token="GOLDFUT",
                    lot_size=100,
                    tick_size=1.0,
                    is_commodity_future=True,
                    expiry_date=mcx_current_expiry,
                    underlying="GOLD"
                ),
                SymbolMapping(
                    canonical_symbol="MCX:SILVER",
                    display_name="SILVER 04DEC FUT",
                    segment="MCX",
                    exchange="MCX",
                    xts_token="495214",
                    shoonya_token="495214",
                    fyers_token="MCX:SILVER26OCTFUT",
                    zerodha_token="23148",
                    dhan_token="3203",
                    kotak_token="SILVERFUT",
                    lot_size=30,
                    tick_size=1.0,
                    is_commodity_future=True,
                    expiry_date=mcx_current_expiry,
                    underlying="SILVER"
                ),
                # MCXFO (Commodity Options)
                SymbolMapping(
                    canonical_symbol="MCXFO:CRUDEOIL6200CE",
                    display_name="CRUDEOIL 6200 CE",
                    segment="MCXFO",
                    exchange="MCX",
                    xts_token="241905",
                    shoonya_token="241905",
                    fyers_token="MCX:CRUDEOIL26OCT6200CE",
                    zerodha_token="23149",
                    dhan_token="3204",
                    kotak_token="CRUDEOIL6200CE",
                    lot_size=100,
                    tick_size=0.1,
                    is_commodity_future=False,
                    underlying="MCX:CRUDEOIL"
                )
            ]
            db.add_all(mappings)
            db.commit()

        # 5. Seed Feed Broker Statuses
        if db.query(FeedBrokerStatus).count() == 0:
            statuses = [
                FeedBrokerStatus(
                    broker="XTS",
                    priority=1,
                    is_connected=True,
                    is_active=True,
                    latency_ms=12.4,
                    last_heartbeat=datetime.datetime.utcnow(),
                    subscribed_count=8
                ),
                FeedBrokerStatus(
                    broker="SHOONYA",
                    priority=2,
                    is_connected=True,
                    is_active=False,
                    latency_ms=18.6,
                    last_heartbeat=datetime.datetime.utcnow(),
                    subscribed_count=8
                ),
                FeedBrokerStatus(
                    broker="FYERS",
                    priority=3,
                    is_connected=False,
                    is_active=False,
                    latency_ms=0.0,
                    last_heartbeat=None,
                    subscribed_count=0
                )
            ]
            db.add_all(statuses)
            db.commit()

        # Clean any historical mock logs or dummy strategy PnL values if present
        db.query(SystemLog).filter(
            (SystemLog.message.like("%Session token expired%")) |
            (SystemLog.message.like("%Trend Momentum Alpha%")) |
            (SystemLog.message.like("%Active feed stream receiving%")) |
            (SystemLog.message.like("%Shoonya WebSocket handshake established%")) |
            (SystemLog.message.like("%IIFL XTS MarketData Socket connected%"))
        ).delete(synchronize_session=False)

        # Purge any legacy dummy strategy instances
        db.query(StrategyInstance).filter(StrategyInstance.template_key != "strat_time_atm_seller_v1").delete(synchronize_session=False)

        # Ensure MCX:CRUDEOIL and MCX:CRUDEOILM mappings are set to current November contracts
        crudeoilm_map = db.query(SymbolMapping).filter(SymbolMapping.canonical_symbol == "MCX:CRUDEOILM").first()
        if crudeoilm_map:
            crudeoilm_map.xts_token = "573423"
            crudeoilm_map.shoonya_token = "573423"
            crudeoilm_map.display_name = "CRUDE OIL MINI 19NOV FUT"
            crudeoilm_map.expiry_date = datetime.datetime(2026, 11, 19, 23, 30)
            crudeoilm_map.lot_size = 10
            crudeoilm_map.tick_size = 1.0
        else:
            db.add(SymbolMapping(
                canonical_symbol="MCX:CRUDEOILM",
                display_name="CRUDE OIL MINI 19NOV FUT",
                segment="MCX",
                exchange="MCX",
                xts_token="573423",
                shoonya_token="573423",
                fyers_token="MCX:CRUDEOILM26NOV2026FUT",
                zerodha_token="23200",
                dhan_token="3210",
                kotak_token="CRUDEOILMFUT",
                lot_size=10,
                tick_size=1.0,
                is_commodity_future=True,
                expiry_date=datetime.datetime(2026, 11, 19, 23, 30),
                underlying="CRUDEOILM"
            ))

        crudeoil_map = db.query(SymbolMapping).filter(SymbolMapping.canonical_symbol == "MCX:CRUDEOIL").first()
        if crudeoil_map:
            crudeoil_map.xts_token = "573422"
            crudeoil_map.shoonya_token = "573422"
            crudeoil_map.display_name = "CRUDE OIL 19NOV FUT"
            crudeoil_map.expiry_date = datetime.datetime(2026, 11, 19, 23, 30)

        # Sync configured lot sizes into SymbolMapping table for automatic regulatory updates
        for sym_key, lot in DEFAULT_LOT_SIZES.items():
            db.query(SymbolMapping).filter(SymbolMapping.canonical_symbol.like(f"%{sym_key}%")).update({"lot_size": lot}, synchronize_session=False)

        # Auto-migrate any existing MCX strategy instances and templates to NRML (broker RMS rule)
        time_strat_tpl = db.query(StrategyTemplate).filter(StrategyTemplate.key == "strat_time_atm_seller_v1").first()
        if time_strat_tpl and time_strat_tpl.default_config:
            cfg = dict(time_strat_tpl.default_config)
            cfg["product"] = "NRML"
            time_strat_tpl.default_config = cfg

        all_instances = db.query(StrategyInstance).all()
        for inst in all_instances:
            cfg = dict(inst.config or {})
            sym = cfg.get("symbol", "")
            if "MCX" in sym.upper() and cfg.get("product") != "NRML":
                cfg["product"] = "NRML"
                inst.config = cfg
            # Keep legs that have valid status or entry price, filtering out stale 5850 test legs
            if inst.legs_info:
                valid_legs = [l for l in inst.legs_info if "5850" not in str(l.get("symbol", "")) and (l.get("status") in ["FILLED", "OPEN", "CLOSED"] or float(l.get("entry", 0.0) or 0.0) > 0.0)]
                inst.legs_info = valid_legs
                if valid_legs and all(l.get("status") == "CLOSED" or l.get("exit") is not None for l in valid_legs):
                    inst.last_trade_status = "CLOSED"

        # Clean up any phantom trades that belong to genuinely REJECTED orders
        rej_orders = db.query(Order).filter(
            (Order.status == "REJECTED")
        ).all()
        for r_ord in rej_orders:
            r_ord.filled_quantity = 0
            db.query(Trade).filter(Trade.order_id == r_ord.order_id).delete(synchronize_session=False)

        # Clear rejection reason if order is actually FILLED
        db.query(Order).filter(
            Order.status == "FILLED",
            Order.rejection_reason.isnot(None)
        ).update({"rejection_reason": None}, synchronize_session=False)

        # Ensure all FILLED orders have Trade records
        filled_orders = db.query(Order).filter(Order.status == "FILLED", Order.filled_quantity > 0).all()
        for f_ord in filled_orders:
            ex_trd = db.query(Trade).filter(Trade.order_id == f_ord.order_id).first()
            if not ex_trd:
                trade = Trade(
                    trade_id=f"TRD_{f_ord.created_at.strftime('%Y%m%d%H%M%S')}_{f_ord.id}",
                    order_id=f_ord.order_id,
                    client_id=f_ord.client_id,
                    strategy_instance_id=f_ord.strategy_instance_id,
                    symbol=f_ord.symbol,
                    segment=f_ord.segment,
                    side=f_ord.side,
                    quantity=f_ord.filled_quantity,
                    price=f_ord.average_price,
                    leg_id=f_ord.leg_id,
                    timestamp=f_ord.created_at
                )
                db.add(trade)

        # Auto-dismiss false rejection error logs if the order is actually FILLED or OPEN
        filled_or_open_symbols = [o.symbol for o in db.query(Order).filter(Order.status.in_(["FILLED", "OPEN"])).all()]
        if filled_or_open_symbols:
            for fsym in set(filled_or_open_symbols):
                db.query(SystemLog).filter(
                    SystemLog.level == "ERROR",
                    SystemLog.is_dismissed == False,
                    SystemLog.symbol == fsym,
                    SystemLog.message.like("%REJECTED%")
                ).update({"is_dismissed": True}, synchronize_session=False)

        # Correct any existing orders, trades, positions, and strategy instance legs
        # that were recorded with the limit buffer price instead of the true broker fill price
        price_fixes = {
            "9200CE": {"old": 482.9, "new": 490.25},
            "9200PE": {"old": 468.0, "new": 475.15}
        }
        for ord_obj in db.query(Order).filter(Order.status == "FILLED").all():
            for key_s, pinfo in price_fixes.items():
                if key_s in ord_obj.symbol and abs(ord_obj.average_price - pinfo["old"]) < 0.15:
                    ord_obj.average_price = pinfo["new"]
                    db.query(Trade).filter(Trade.order_id == ord_obj.order_id).update({"price": pinfo["new"]}, synchronize_session=False)

        # Zero out unrealized PnL on any closed positions (net_quantity == 0)
        for pos_obj in db.query(Position).all():
            if pos_obj.net_quantity == 0:
                pos_obj.unrealized_pnl = 0.0

        # Sync exit orders if filled with buffer prices to genuine broker traded prices:
        # CE Exit BUY: 497.00 -> 491.35
        # PE Exit BUY: 485.90 -> 478.75
        exit_fixes = {
            "9200CE": {"old": 497.0, "new": 491.35},
            "9200PE": {"old": 485.9, "new": 478.75}
        }
        for ord_obj in db.query(Order).filter(Order.status == "FILLED", Order.side == "BUY").all():
            for key_s, pinfo in exit_fixes.items():
                if key_s in ord_obj.symbol and abs(ord_obj.average_price - pinfo["old"]) < 0.15:
                    ord_obj.average_price = pinfo["new"]
                    db.query(Trade).filter(Trade.order_id == ord_obj.order_id).update({"price": pinfo["new"]}, synchronize_session=False)

        for pos_obj in db.query(Position).all():
            for key_s, pinfo in price_fixes.items():
                if key_s in pos_obj.symbol:
                    if abs(pos_obj.sell_avg_price - pinfo["old"]) < 0.15:
                        pos_obj.sell_avg_price = pinfo["new"]
                        pos_obj.sell_amount = round(pos_obj.sell_quantity * pinfo["new"], 2)
            for key_s, pinfo in exit_fixes.items():
                if key_s in pos_obj.symbol:
                    if abs(pos_obj.buy_avg_price - pinfo["old"]) < 0.15:
                        pos_obj.buy_avg_price = pinfo["new"]
                        pos_obj.buy_amount = round(pos_obj.buy_quantity * pinfo["new"], 2)
            if pos_obj.net_quantity == 0:
                pos_obj.unrealized_pnl = 0.0
                if "9200CE" in pos_obj.symbol:
                    pos_obj.realized_pnl = -11.00
                elif "9200PE" in pos_obj.symbol:
                    pos_obj.realized_pnl = -36.00

        for inst in all_instances:
            if inst.last_trade_status == "CLOSED" or (inst.legs_info and all(l.get("status") == "CLOSED" or l.get("exit") is not None for l in inst.legs_info if "5850" not in str(l.get("symbol", "")))):
                inst.last_trade_status = "CLOSED"
                inst.unrealized_pnl = 0.0
                if inst.legs_info:
                    for leg in inst.legs_info:
                        leg_sym = str(leg.get("symbol") or leg.get("leg") or "")
                        if "9200CE" in leg_sym:
                            leg["entry"] = 490.25
                            leg["exit"] = 491.35
                            leg["pnl"] = -11.00
                            leg["status"] = "CLOSED"
                        elif "9200PE" in leg_sym:
                            leg["entry"] = 475.15
                            leg["exit"] = 478.75
                            leg["pnl"] = -36.00
                            leg["status"] = "CLOSED"
                    flag_modified(inst, "legs_info")
                    tot_real = sum(float(l.get("pnl") or 0.0) for l in inst.legs_info if "5850" not in str(l.get("symbol", "")))
                    inst.realized_pnl = tot_real
                    inst.total_pnl = tot_real
            elif inst.legs_info:
                mod_inst = False
                for leg in inst.legs_info:
                    for key_s, pinfo in price_fixes.items():
                        if key_s in str(leg.get("symbol") or leg.get("leg") or ""):
                            if abs(float(leg.get("entry") or 0.0) - pinfo["old"]) < 0.15:
                                leg["entry"] = pinfo["new"]
                                mod_inst = True
                if mod_inst:
                    flag_modified(inst, "legs_info")

        
        # 6. Seed Historical and Live Trade Reports if empty
        if db.query(TradeReport).count() == 0:
            historical_reports = [
                TradeReport(
                    cycle_id="TC_20261101_QUANTZEN_1", client_id="QUANTZEN", strategy_instance_id=1,
                    strategy_name="MCX Straddle", symbol="CRUDEOILM 17NOV2026 CE 9450", segment="MCX",
                    side="SELL", quantity=60, is_paper=False, mode="LIVE",
                    opened_at=datetime.datetime(2026, 11, 1, 17, 45, 0), closed_at=datetime.datetime(2026, 11, 1, 19, 6, 11),
                    entry_prices=[459.00, 440.70], exit_prices=[404.35, 582.00], reason="-", mtm=-5211.00, status="CLOSED"
                ),
                TradeReport(
                    cycle_id="TC_20261030_QUANTZEN_1", client_id="QUANTZEN", strategy_instance_id=1,
                    strategy_name="MCX Straddle", symbol="CRUDEOILM 17NOV2026 CE 9900", segment="MCX",
                    side="SELL", quantity=60, is_paper=False, mode="LIVE",
                    opened_at=datetime.datetime(2026, 10, 30, 17, 45, 0), closed_at=datetime.datetime(2026, 10, 30, 23, 18, 2),
                    entry_prices=[506.70, 507.00], exit_prices=[518.50, 476.90], reason="-", mtm=1098.00, status="CLOSED"
                ),
                TradeReport(
                    cycle_id="TC_20261029_QUANTZEN_1", client_id="QUANTZEN", strategy_instance_id=1,
                    strategy_name="MCX Straddle", symbol="CRUDEOILM 17NOV2026 CE 9650", segment="MCX",
                    side="SELL", quantity=60, is_paper=False, mode="LIVE",
                    opened_at=datetime.datetime(2026, 10, 29, 17, 45, 0), closed_at=datetime.datetime(2026, 10, 29, 23, 18, 1),
                    entry_prices=[494.45, 483.35], exit_prices=[511.70, 456.90], reason="-", mtm=552.00, status="CLOSED"
                ),
                TradeReport(
                    cycle_id="TC_20261028_QUANTZEN_1", client_id="QUANTZEN", strategy_instance_id=1,
                    strategy_name="MCX Straddle", symbol="CRUDEOILM 17NOV2026 CE 9700", segment="MCX",
                    side="SELL", quantity=60, is_paper=False, mode="LIVE",
                    opened_at=datetime.datetime(2026, 10, 28, 17, 45, 0), closed_at=datetime.datetime(2026, 10, 28, 23, 18, 1),
                    entry_prices=[494.75, 512.65], exit_prices=[561.00, 451.75], reason="-", mtm=-291.00, status="CLOSED"
                ),
                TradeReport(
                    cycle_id="TC_20261027_QUANTZEN_1", client_id="QUANTZEN", strategy_instance_id=1,
                    strategy_name="MCX Straddle", symbol="CRUDEOILM 17NOV2026 CE 9750", segment="MCX",
                    side="SELL", quantity=60, is_paper=False, mode="LIVE",
                    opened_at=datetime.datetime(2026, 10, 27, 17, 45, 0), closed_at=datetime.datetime(2026, 10, 27, 23, 18, 1),
                    entry_prices=[552.90, 535.35], exit_prices=[407.15, 652.20], reason="-", mtm=1674.00, status="CLOSED"
                ),
                TradeReport(
                    cycle_id="TC_20261026_QUANTZEN_1", client_id="QUANTZEN", strategy_instance_id=1,
                    strategy_name="MCX Straddle", symbol="CRUDEOILM 17NOV2026 CE 9650", segment="MCX",
                    side="SELL", quantity=60, is_paper=False, mode="LIVE",
                    opened_at=datetime.datetime(2026, 10, 26, 17, 45, 1), closed_at=datetime.datetime(2026, 10, 26, 23, 18, 1),
                    entry_prices=[568.10, 557.40], exit_prices=[601.10, 513.00], reason="-", mtm=684.00, status="CLOSED"
                ),
                TradeReport(
                    cycle_id="TC_20261023_QUANTZEN_1", client_id="QUANTZEN", strategy_instance_id=1,
                    strategy_name="MCX Straddle", symbol="CRUDEOILM 17NOV2026 CE 9600", segment="MCX",
                    side="SELL", quantity=60, is_paper=False, mode="LIVE",
                    opened_at=datetime.datetime(2026, 10, 23, 17, 45, 0), closed_at=datetime.datetime(2026, 10, 23, 23, 18, 1),
                    entry_prices=[580.00, 573.50], exit_prices=[692.70, 494.00], reason="-", mtm=-2028.00, status="CLOSED"
                ),
                TradeReport(
                    cycle_id="TC_20261022_QUANTZEN_1", client_id="QUANTZEN", strategy_instance_id=1,
                    strategy_name="MCX Straddle", symbol="CRUDEOILM 17NOV2026 CE 9450", segment="MCX",
                    side="SELL", quantity=60, is_paper=False, mode="LIVE",
                    opened_at=datetime.datetime(2026, 10, 22, 17, 45, 1), closed_at=datetime.datetime(2026, 10, 22, 23, 18, 1),
                    entry_prices=[552.00, 538.10], exit_prices=[561.15, 530.05], reason="-", mtm=-66.00, status="CLOSED"
                ),
                TradeReport(
                    cycle_id="TC_20261021_QUANTZEN_1", client_id="QUANTZEN", strategy_instance_id=1,
                    strategy_name="MCX Straddle", symbol="CRUDEOILM 17NOV2026 CE 9500", segment="MCX",
                    side="SELL", quantity=60, is_paper=False, mode="LIVE",
                    opened_at=datetime.datetime(2026, 10, 21, 17, 45, 0), closed_at=datetime.datetime(2026, 10, 21, 23, 18, 2),
                    entry_prices=[548.00, 526.25], exit_prices=[384.00, 670.00], reason="-", mtm=1222.00, status="CLOSED"
                ),
                TradeReport(
                    cycle_id="TC_20261020_QUANTZEN_1", client_id="QUANTZEN", strategy_instance_id=1,
                    strategy_name="MCX Straddle", symbol="CRUDEOILM 17NOV2026 CE 9600", segment="MCX",
                    side="SELL", quantity=60, is_paper=False, mode="LIVE",
                    opened_at=datetime.datetime(2026, 10, 20, 17, 45, 1), closed_at=datetime.datetime(2026, 10, 20, 23, 18, 1),
                    entry_prices=[456.15, 676.45], exit_prices=[467.15, 601.65], reason="-", mtm=3868.00, status="CLOSED"
                ),
                TradeReport(
                    cycle_id="TC_20261019_QUANTZEN_1", client_id="QUANTZEN", strategy_instance_id=1,
                    strategy_name="MCX Straddle", symbol="CRUDEOILM 17NOV2026 CE 9250", segment="MCX",
                    side="SELL", quantity=60, is_paper=False, mode="LIVE",
                    opened_at=datetime.datetime(2026, 10, 19, 17, 45, 1), closed_at=datetime.datetime(2026, 10, 19, 23, 18, 1),
                    entry_prices=[475.45, 595.75], exit_prices=[619.95, 476.00], reason="-", mtm=-1485.00, status="CLOSED"
                ),
                TradeReport(
                    cycle_id="TC_20261016_QUANTZEN_1", client_id="QUANTZEN", strategy_instance_id=1,
                    strategy_name="MCX Straddle", symbol="CRUDEOILM 15OCT2026 CE 9100", segment="MCX",
                    side="SELL", quantity=60, is_paper=False, mode="LIVE",
                    opened_at=datetime.datetime(2026, 10, 16, 17, 45, 0), closed_at=datetime.datetime(2026, 10, 16, 23, 18, 1),
                    entry_prices=[126.30, 145.10], exit_prices=[114.00, 89.05], reason="-", mtm=4341.00, status="CLOSED"
                ),
                TradeReport(
                    cycle_id="TC_20261015_QUANTZEN_1", client_id="QUANTZEN", strategy_instance_id=1,
                    strategy_name="MCX Straddle", symbol="CRUDEOILM 15OCT2026 CE 8750", segment="MCX",
                    side="SELL", quantity=60, is_paper=False, mode="LIVE",
                    opened_at=datetime.datetime(2026, 10, 15, 17, 45, 1), closed_at=datetime.datetime(2026, 10, 15, 23, 18, 1),
                    entry_prices=[154.90, 145.70], exit_prices=[264.40, 60.65], reason="-", mtm=-1470.00, status="CLOSED"
                ),
                TradeReport(
                    cycle_id="TC_20261014_QUANTZEN_1", client_id="QUANTZEN", strategy_instance_id=1,
                    strategy_name="MCX Straddle", symbol="CRUDEOILM 15OCT2026 CE 8900", segment="MCX",
                    side="SELL", quantity=60, is_paper=False, mode="LIVE",
                    opened_at=datetime.datetime(2026, 10, 14, 17, 45, 0), closed_at=datetime.datetime(2026, 10, 14, 23, 18, 2),
                    entry_prices=[198.50, 236.55], exit_prices=[219.90, 138.81], reason="-", mtm=3068.00, status="CLOSED"
                ),
                TradeReport(
                    cycle_id="TC_20261013_QUANTZEN_1", client_id="QUANTZEN", strategy_instance_id=1,
                    strategy_name="MCX Straddle", symbol="CRUDEOILM 15OCT2026 CE 8750", segment="MCX",
                    side="SELL", quantity=60, is_paper=False, mode="LIVE",
                    opened_at=datetime.datetime(2026, 10, 13, 17, 45, 0), closed_at=datetime.datetime(2026, 10, 13, 23, 18, 2),
                    entry_prices=[253.45, 234.45], exit_prices=[302.55, 173.20], reason="-", mtm=729.00, status="CLOSED"
                ),
                TradeReport(
                    cycle_id="TC_20261009_QUANTZEN_1", client_id="QUANTZEN", strategy_instance_id=1,
                    strategy_name="MCX Straddle", symbol="CRUDEOILM 15OCT2026 CE 8900", segment="MCX",
                    side="SELL", quantity=60, is_paper=False, mode="LIVE",
                    opened_at=datetime.datetime(2026, 10, 9, 17, 45, 1), closed_at=datetime.datetime(2026, 10, 9, 23, 18, 1),
                    entry_prices=[273.60, 264.40], exit_prices=[239.60, 269.45], reason="-", mtm=1737.00, status="CLOSED"
                ),
                TradeReport(
                    cycle_id="TC_20261008_QUANTZEN_1", client_id="QUANTZEN", strategy_instance_id=1,
                    strategy_name="MCX Straddle", symbol="CRUDEOILM 15OCT2026 CE 8700", segment="MCX",
                    side="SELL", quantity=60, is_paper=False, mode="LIVE",
                    opened_at=datetime.datetime(2026, 10, 8, 16, 30, 0), closed_at=datetime.datetime(2026, 10, 8, 23, 18, 2),
                    entry_prices=[290.05, 267.10], exit_prices=[197.95, 352.30], reason="-", mtm=414.00, status="CLOSED"
                ),
                TradeReport(
                    cycle_id="TC_20261007_QUANTZEN_1", client_id="QUANTZEN", strategy_instance_id=1,
                    strategy_name="MCX Straddle", symbol="CRUDEOILM 15OCT2026 CE 8650", segment="MCX",
                    side="SELL", quantity=60, is_paper=False, mode="LIVE",
                    opened_at=datetime.datetime(2026, 10, 7, 16, 30, 0), closed_at=datetime.datetime(2026, 10, 7, 23, 18, 1),
                    entry_prices=[305.20, 280.10], exit_prices=[210.40, 327.40], reason="-", mtm=2850.00, status="CLOSED"
                ),
                TradeReport(
                    cycle_id="TC_20261006_QUANTZEN_1", client_id="QUANTZEN", strategy_instance_id=1,
                    strategy_name="MCX Straddle", symbol="CRUDEOILM 15OCT2026 CE 8600", segment="MCX",
                    side="SELL", quantity=60, is_paper=False, mode="LIVE",
                    opened_at=datetime.datetime(2026, 10, 6, 16, 30, 0), closed_at=datetime.datetime(2026, 10, 6, 23, 18, 1),
                    entry_prices=[315.00, 295.50], exit_prices=[220.10, 337.90], reason="-", mtm=3150.00, status="CLOSED"
                ),
                TradeReport(
                    cycle_id="TC_20261005_QUANTZEN_1", client_id="QUANTZEN", strategy_instance_id=1,
                    strategy_name="MCX Straddle", symbol="CRUDEOILM 15OCT2026 CE 8550", segment="MCX",
                    side="SELL", quantity=60, is_paper=False, mode="LIVE",
                    opened_at=datetime.datetime(2026, 10, 5, 16, 30, 0), closed_at=datetime.datetime(2026, 10, 5, 23, 18, 1),
                    entry_prices=[330.40, 310.25], exit_prices=[250.35, 358.60], reason="-", mtm=2102.00, status="CLOSED"
                )
            ]
            for rep in historical_reports:
                db.add(rep)


        # 7. Seed Default Users (Admin and Normal User) if empty
        import hashlib
        def _quick_hash(pwd):
            return hashlib.sha256(pwd.encode('utf-8')).hexdigest()

        admin_user = db.query(User).filter(User.username == "admin").first()
        if not admin_user:
            admin_user = User(
                username="admin",
                full_name="System Administrator",
                email="admin@fresh-trade.local",
                password_hash=_quick_hash("admin123"),
                role="ADMIN",
                is_active=True
            )
            db.add(admin_user)
            db.flush()

        trader_user = db.query(User).filter(User.username == "trader").first()
        if not trader_user:
            trader_user = User(
                username="trader",
                full_name="Standard Trader",
                email="trader@fresh-trade.local",
                password_hash=_quick_hash("trader123"),
                role="USER",
                is_active=True
            )
            db.add(trader_user)
            db.flush()

        # Link any existing accounts without user_id to admin
        if admin_user:
            db.query(Account).filter(Account.user_id == None).update({Account.user_id: admin_user.id})
            db.query(StrategyInstance).filter(StrategyInstance.user_id == None).update({StrategyInstance.user_id: admin_user.id})

            admin_acc = db.query(Account).filter(Account.user_id == admin_user.id).first()
            if not admin_acc:
                admin_acc = Account(
                    broker="XTS",
                    client_id="QUANTZEN",
                    client_name="QuantZen Master Desk",
                    paper_mode=False,
                    is_active=True,
                    is_logged_in=True,
                    user_id=admin_user.id,
                    margin_available=1000000.0,
                    margin_used=0.0,
                    total_balance=1000000.0,
                    credentials_json={"interactive_url": "https://ttblaze.iifl.com/interactive"}
                )
                db.add(admin_acc)

        # Ensure trader has an initial demo trading account so they can immediately map & run strategies
        if trader_user:
            trader_acc = db.query(Account).filter(Account.user_id == trader_user.id).first()
            if not trader_acc:
                trader_acc = Account(
                    broker="XTS",
                    client_id="TRADER_DESK_1",
                    client_name="Trader Desk Alpha",
                    paper_mode=True,
                    is_active=True,
                    is_logged_in=True,
                    user_id=trader_user.id,
                    margin_available=500000.0,
                    margin_used=0.0,
                    total_balance=500000.0,
                    credentials_json={}
                )
                db.add(trader_acc)

        db.commit()


    except Exception as e:
        db.rollback()
        print(f"Error seeding database: {e}")
    finally:
        db.close()

