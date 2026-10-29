import os
import datetime
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, scoped_session
from backend.config import DB_URL, DEFAULT_FEED_PRIORITY, DEFAULT_LOT_SIZES
from backend.database.models import (
    Base, Account, StrategyTemplate, StrategyInstance,
    SymbolMapping, FeedBrokerStatus, SystemLog, Position
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

def init_db():
    Base.metadata.create_all(bind=engine)
    seed_initial_data()

# Auto-initialize database schema and seeds on import so tables always exist
try:
    init_db()
except Exception as _init_err:
    pass

def seed_initial_data():
    db = SessionLocal()
    try:
        # 1. Seed Accounts in User's Priority Order (XTS > Kotak Neo > Shoonya > Zerodha > Dhan)
        accounts_marker = db.query(SystemLog).filter(
            SystemLog.category == "SYSTEM_INIT",
            SystemLog.message == "ACCOUNTS_INITIALIZED"
        ).first()

        if not accounts_marker:
            if db.query(Account).count() == 0:
                accounts = [
                    Account(
                        broker="XTS",
                        client_id="XT88219",
                        client_name="IIFL Prop Desk (XTS)",
                        paper_mode=True,
                        is_active=True,
                        is_logged_in=True,
                        margin_available=500000.0,
                        margin_used=0.0,
                        total_balance=500000.0
                    ),
                    Account(
                        broker="KOTAKNEO",
                        client_id="KT99231",
                        client_name="Kotak Neo Hedger",
                        paper_mode=True,
                        is_active=True,
                        is_logged_in=True,
                        margin_available=500000.0,
                        margin_used=0.0,
                        total_balance=500000.0
                    ),
                    Account(
                        broker="SHOONYA",
                        client_id="SH10023",
                        client_name="Alpha Prime (Shoonya)",
                        paper_mode=True,
                        is_active=True,
                        is_logged_in=True,
                        margin_available=500000.0,
                        margin_used=0.0,
                        total_balance=500000.0
                    ),
                    Account(
                        broker="ZERODHA",
                        client_id="ZR44102",
                        client_name="Kite Momentum (Zerodha)",
                        paper_mode=True,
                        is_active=True,
                        is_logged_in=True,
                        margin_available=500000.0,
                        margin_used=0.0,
                        total_balance=500000.0
                    ),
                    Account(
                        broker="DHAN",
                        client_id="DH55102",
                        client_name="Dhan Fast Scalper",
                        paper_mode=True,
                        is_active=True,
                        is_logged_in=True,
                        margin_available=500000.0,
                        margin_used=0.0,
                        total_balance=500000.0
                    ),
                    Account(
                        broker="FYERS",
                        client_id="FY11048",
                        client_name="Fyers Commodity Desk",
                        paper_mode=True,
                        is_active=True,
                        is_logged_in=True,
                        margin_available=500000.0,
                        margin_used=0.0,
                        total_balance=500000.0
                    )
                ]
                db.add_all(accounts)
            db.add(SystemLog(
                category="SYSTEM_INIT",
                level="INFO",
                message="ACCOUNTS_INITIALIZED",
                is_dismissed=True
            ))
            db.commit()

        # 2. Seed Registered Strategy Templates (Admin catalog)
        if db.query(StrategyTemplate).count() == 0 or not db.query(StrategyTemplate).filter(StrategyTemplate.key == "strat_time_atm_seller_v1").first():
            templates = [
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
                        "product": "MIS"
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
                if not db.query(StrategyTemplate).filter(StrategyTemplate.key == tpl.key).first():
                    db.add(tpl)
            db.commit()

        # 3. Seed Strategy Instances (Clean State: 0 PnL, no dummy legs)
        if db.query(StrategyInstance).count() == 0 or not db.query(StrategyInstance).filter(StrategyInstance.template_key == "strat_time_atm_seller_v1").first():
            instances = [
                StrategyInstance(
                    template_key="strat_time_atm_seller_v1",
                    instance_name_admin="MCX CrudeOilM ATM Seller [XTS + Kotak + Shoonya + Zerodha + Dhan]",
                    instance_name_user="MCX CrudeOilM ATM Seller",
                    client_ids=["XT88219", "KT99231", "SH10023", "ZR44102", "DH55102"],
                    status="RUNNING",
                    config={
                        "symbol": "MCX:CRUDEOILM",
                        "strike_step": 50,
                        "entry_time": "15:45:00",
                        "exit_time": "23:18:00",
                        "legs": ["CE", "PE"],
                        "lots": 1,
                        "lot_size": 10,
                        "stop_loss_per_lot": 750.0,
                        "allow_entry_in_window": True,
                        "product": "MIS"
                    },
                    start_time="15:45:00",
                    end_time="23:18:00",
                    last_trade_status="IDLE",
                    last_trade_at=None,
                    unrealized_pnl=0.0,
                    realized_pnl=0.0,
                    total_pnl=0.0,
                    legs_info=[]
                )
            ]
            for inst in instances:
                if not db.query(StrategyInstance).filter(StrategyInstance.instance_name_admin == inst.instance_name_admin).first():
                    db.add(inst)
            db.commit()


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
                    display_name="CRUDE OIL 19OCT FUT",
                    segment="MCX",
                    exchange="MCX",
                    xts_token="569900",
                    shoonya_token="569900",
                    fyers_token="MCX:CRUDEOIL26OCTFUT",
                    zerodha_token="23145",
                    dhan_token="3200",
                    kotak_token="CRUDEOILFUT",
                    lot_size=100,
                    tick_size=1.0,
                    is_commodity_future=True,
                    expiry_date=mcx_current_expiry,
                    underlying="CRUDEOIL"
                ),
                SymbolMapping(
                    canonical_symbol="MCX:CRUDEOILM",
                    display_name="CRUDE OIL MINI 19OCT FUT",
                    segment="MCX",
                    exchange="MCX",
                    xts_token="571200",
                    shoonya_token="571200",
                    fyers_token="MCX:CRUDEOILM26OCTFUT",
                    zerodha_token="23200",
                    dhan_token="3210",
                    kotak_token="CRUDEOILMFUT",
                    lot_size=10,
                    tick_size=1.0,
                    is_commodity_future=True,
                    expiry_date=mcx_current_expiry,
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

        # Ensure MCX:CRUDEOILM mapping exists
        if not db.query(SymbolMapping).filter(SymbolMapping.canonical_symbol == "MCX:CRUDEOILM").first():
            today = datetime.datetime.utcnow()
            mcx_current_expiry = datetime.datetime(today.year, today.month, 19, 23, 30)
            db.add(SymbolMapping(
                canonical_symbol="MCX:CRUDEOILM",
                display_name="CRUDE OIL MINI 19OCT FUT",
                segment="MCX",
                exchange="MCX",
                xts_token="571200",
                shoonya_token="571200",
                fyers_token="MCX:CRUDEOILM26OCTFUT",
                zerodha_token="23200",
                dhan_token="3210",
                kotak_token="CRUDEOILMFUT",
                lot_size=10,
                tick_size=1.0,
                is_commodity_future=True,
                expiry_date=mcx_current_expiry,
                underlying="CRUDEOILM"
            ))

        # Sync configured lot sizes into SymbolMapping table for automatic regulatory updates
        for sym_key, lot in DEFAULT_LOT_SIZES.items():
            db.query(SymbolMapping).filter(SymbolMapping.canonical_symbol.like(f"%{sym_key}%")).update({"lot_size": lot}, synchronize_session=False)

        db.commit()



    except Exception as e:
        db.rollback()
        print(f"Error seeding database: {e}")
    finally:
        db.close()

