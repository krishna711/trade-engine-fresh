import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "trade_engine.db"
DB_URL = f"sqlite:///{DB_PATH}"

API_HOST = "127.0.0.1"
API_PORT = 8000
FRONTEND_PORT = 3000

# IIFL XTS Market Data Feed Credentials
XTS_FEED_URL = os.getenv("XTS_FEED_URL", "https://ttblaze.iifl.com/apibinarymarketdata")
XTS_FEED_API_KEY = os.getenv("XTS_FEED_API_KEY", "")
XTS_FEED_API_SECRET = os.getenv("XTS_FEED_API_SECRET", "")
XTS_FEED_STATIC_IP = os.getenv("XTS_FEED_STATIC_IP", "")

# Shoonya Credentials (Hot Failover)
SHOONYA_USER_ID = os.getenv("SHOONYA_USER_ID", "")
SHOONYA_PASSWORD = os.getenv("SHOONYA_PASSWORD", "")
SHOONYA_TWO_FA = os.getenv("SHOONYA_TWO_FA", "")
SHOONYA_VENDOR_CODE = os.getenv("SHOONYA_VENDOR_CODE", "")
SHOONYA_API_SECRET = os.getenv("SHOONYA_API_SECRET", "")

# Feed Manager Configuration
DEFAULT_FEED_PRIORITY = ["XTS", "SHOONYA", "FYERS"]
FEED_HEARTBEAT_TIMEOUT_SEC = 30.0
OHLC_BUFFER_SIZE = 500

# Default Subscriptions
DEFAULT_SUBSCRIPTIONS = [
    {"symbol": "NIFTY 50", "canonical": "NSE:NIFTY", "segment": "NSE", "exchange": "NSE"},
    {"symbol": "BANKNIFTY", "canonical": "NSE:BANKNIFTY", "segment": "NSE", "exchange": "NSE"},
    {"symbol": "SENSEX", "canonical": "BSE:SENSEX", "segment": "BSE", "exchange": "BSE"},
    {"symbol": "CRUDEOIL FUT", "canonical": "MCX:CRUDEOIL", "segment": "MCX", "exchange": "MCX", "is_commodity_future": True},
    {"symbol": "CRUDEOIL MINI FUT", "canonical": "MCX:CRUDEOILM", "segment": "MCX", "exchange": "MCX", "is_commodity_future": True},
    {"symbol": "NATURALGAS FUT", "canonical": "MCX:NATURALGAS", "segment": "MCX", "exchange": "MCX", "is_commodity_future": True},
    {"symbol": "GOLD FUT", "canonical": "MCX:GOLD", "segment": "MCX", "exchange": "MCX", "is_commodity_future": True},
    {"symbol": "SILVER FUT", "canonical": "MCX:SILVER", "segment": "MCX", "exchange": "MCX", "is_commodity_future": True},
    {"symbol": "RELIANCE", "canonical": "NSE:RELIANCE", "segment": "NSE", "exchange": "NSE"},
    {"symbol": "RELIANCE FUT", "canonical": "NFO:RELIANCE", "segment": "NFO", "exchange": "NSE"},
    {"symbol": "HAL", "canonical": "NSE:HAL", "segment": "NSE", "exchange": "NSE"},
    {"symbol": "HAL FUT", "canonical": "NFO:HAL", "segment": "NFO", "exchange": "NSE"},
    {"symbol": "BHARTIARTL", "canonical": "NSE:BHARTIARTL", "segment": "NSE", "exchange": "NSE"},
    {"symbol": "BHARTIARTL FUT", "canonical": "NFO:BHARTIARTL", "segment": "NFO", "exchange": "NSE"}
]

# Supported Segments
SUPPORTED_SEGMENTS = ["NSE", "NFO", "BSE", "BSEFO", "MCX", "MCXFO"]

# Supported Order Brokers (orders route strictly to accounts mapped in each strategy)
SUPPORTED_BROKERS = ["XTS", "KOTAKNEO", "SHOONYA", "ZERODHA", "DHAN", "FYERS"]

# Configurable Default Lot Sizes (Regulator / Exchange updates can be adjusted here or via environment variables)
DEFAULT_LOT_SIZES = {
    "NIFTY": int(os.getenv("LOT_SIZE_NIFTY", 65)),
    "BANKNIFTY": int(os.getenv("LOT_SIZE_BANKNIFTY", 30)),
    "SENSEX": int(os.getenv("LOT_SIZE_SENSEX", 20)),
    "CRUDEOILM": int(os.getenv("LOT_SIZE_CRUDEOILM", 10)),
    "CRUDEOIL": int(os.getenv("LOT_SIZE_CRUDEOIL", 100)),
    "NATURALGAS": int(os.getenv("LOT_SIZE_NATURALGAS", 1250)),
    "GOLD": int(os.getenv("LOT_SIZE_GOLD", 100)),
    "SILVER": int(os.getenv("LOT_SIZE_SILVER", 30)),
    "RELIANCE": int(os.getenv("LOT_SIZE_RELIANCE", 250)),
    "HAL": int(os.getenv("LOT_SIZE_HAL", 150)),
    "BHARTIARTL": int(os.getenv("LOT_SIZE_BHARTIARTL", 475)),
    "BHARTIAIRTEL": int(os.getenv("LOT_SIZE_BHARTIAIRTEL", 475)),
}

def get_default_lot_size(symbol: str) -> int:
    """Returns the configured lot size based on symbol keyword matching"""
    sym_upper = symbol.upper()
    for root, lot in DEFAULT_LOT_SIZES.items():
        if root in sym_upper:
            return lot
    return 1



