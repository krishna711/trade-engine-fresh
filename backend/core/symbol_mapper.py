import datetime
import calendar
from typing import Dict, Optional, Any, List
from backend.database.connection import SessionLocal
from backend.database.models import SymbolMapping
from backend.config import get_default_lot_size, DEFAULT_LOT_SIZES


# Commodity expiry standard calendar logic:
# CRUDEOIL MCX: typically expires around 19th of contract month
# NATURALGAS MCX: typically expires 25th-26th of contract month
# GOLD MCX: typically expires around 5th of delivery month
# SILVER MCX: typically expires around 5th of delivery month

COMMODITY_EXPIRY_DAYS = {
    "CRUDEOIL": 19,
    "NATURALGAS": 25,
    "GOLD": 5,
    "SILVER": 5
}

MONTH_CODES = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC"
}

class UniversalSymbolMapper:
    """
    Translates universal canonical symbols across brokers (XTS, Shoonya, Fyers, Zerodha, Dhan, Kotak).
    Also performs automatic expiry roll for commodity futures on the next day of expiry.
    """

    def __init__(self):
        self._cache: Dict[str, SymbolMapping] = {}
        self.reload_cache()

    def reload_cache(self):
        db = SessionLocal()
        try:
            mappings = db.query(SymbolMapping).all()
            self._cache = {m.canonical_symbol: m for m in mappings}
        finally:
            db.close()

    def get_mapping(self, canonical_symbol: str) -> Optional[SymbolMapping]:
        if canonical_symbol not in self._cache:
            self.reload_cache()
        mapping = self._cache.get(canonical_symbol)
        if mapping:
            return mapping

        # Dynamically generate and cache mapping for options (e.g. MCXFO:CRUDEOILM5850CE, NFO:NIFTY24900PE)
        if ":" in canonical_symbol:
            segment, sym_part = canonical_symbol.split(":", 1)
            token_hash = str(abs(hash(canonical_symbol)) % 800000 + 100000)
            
            lot_size = get_default_lot_size(sym_part)
            tick_size = 0.1 if "MCX" in segment else 0.05
            exchange = segment.replace("FO", "")
            if not exchange:
                exchange = "NSE"

            synthetic = SymbolMapping(
                canonical_symbol=canonical_symbol,
                display_name=sym_part,
                segment=segment,
                exchange=exchange,
                xts_token=token_hash,
                shoonya_token=token_hash,
                fyers_token=f"{exchange}:{sym_part}",
                zerodha_token=token_hash,
                dhan_token=str(int(token_hash) // 10),
                kotak_token=sym_part,
                lot_size=lot_size,
                tick_size=tick_size,
                is_commodity_future=(segment == "MCX")
            )
            self._cache[canonical_symbol] = synthetic
            return synthetic

        return None

    def get_lot_size(self, canonical_symbol: str) -> int:
        """Returns lot size: checks database mapping first, falls back to configurable default"""
        mapping = self.get_mapping(canonical_symbol)
        if mapping and mapping.lot_size:
            return mapping.lot_size
        return get_default_lot_size(canonical_symbol)



    def get_broker_token(self, canonical_symbol: str, broker: str) -> Optional[str]:
        mapping = self.get_mapping(canonical_symbol)
        if not mapping:
            return None
        broker_lower = broker.lower()
        if "xts" in broker_lower:
            return mapping.xts_token
        elif "shoonya" in broker_lower:
            return mapping.shoonya_token
        elif "fyers" in broker_lower:
            return mapping.fyers_token
        elif "zerodha" in broker_lower:
            return mapping.zerodha_token
        elif "dhan" in broker_lower:
            return mapping.dhan_token
        elif "kotak" in broker_lower:
            return mapping.kotak_token
        return None

    def get_canonical_by_token(self, broker: str, token: str) -> Optional[str]:
        """Resolves broker token back to canonical symbol (e.g. 26000 -> NSE:NIFTY)"""
        token_str = str(token).strip()
        broker_lower = broker.lower()
        for sym, m in self._cache.items():
            if "xts" in broker_lower and str(m.xts_token).strip() == token_str:
                return sym
            elif "shoonya" in broker_lower and str(m.shoonya_token).strip() == token_str:
                return sym
            elif "fyers" in broker_lower and str(m.fyers_token).strip() == token_str:
                return sym
        return None

    def get_broker_symbol(self, canonical_symbol: str, broker: str) -> str:
        """Returns the broker-specific trading symbol string"""
        mapping = self.get_mapping(canonical_symbol)
        if not mapping:
            return canonical_symbol
        broker_upper = broker.upper()
        if broker_upper == "FYERS":
            return mapping.fyers_token or canonical_symbol
        elif broker_upper == "SHOONYA":
            return mapping.shoonya_token or canonical_symbol
        elif broker_upper == "XTS":
            return mapping.xts_token or canonical_symbol
        return mapping.canonical_symbol

    def check_and_roll_expiries(self) -> List[Dict[str, Any]]:
        """
        Scans all commodity futures in DB.
        If current local date > contract expiry date, automatically rolls to next month's contract!
        """
        rolled_contracts = []
        db = SessionLocal()
        try:
            today = datetime.datetime.now()
            commodity_mappings = db.query(SymbolMapping).filter(SymbolMapping.is_commodity_future == True).all()

            for mapping in commodity_mappings:
                if mapping.expiry_date and today.date() > mapping.expiry_date.date():
                    # Contract has expired! Calculate next month's contract
                    old_expiry = mapping.expiry_date
                    underlying = mapping.underlying or mapping.canonical_symbol.split(":")[-1]
                    expiry_day = COMMODITY_EXPIRY_DAYS.get(underlying, 20)

                    # Compute next month
                    curr_month = old_expiry.month
                    curr_year = old_expiry.year
                    next_month = curr_month + 1 if curr_month < 12 else 1
                    next_year = curr_year if curr_month < 12 else curr_year + 1

                    # Ensure day doesn't exceed month's days
                    _, max_days = calendar.monthrange(next_year, next_month)
                    valid_day = min(expiry_day, max_days)
                    new_expiry = datetime.datetime(next_year, next_month, valid_day, 23, 30, 0)

                    year_str = str(next_year)[2:]
                    month_code = MONTH_CODES[next_month]
                    
                    # Update tokens for new contract
                    mapping.expiry_date = new_expiry
                    mapping.display_name = f"{underlying} {month_code} FUT"
                    mapping.fyers_token = f"MCX:{underlying}{year_str}{month_code}FUT"
                    mapping.kotak_token = f"{underlying}{month_code}FUT"

                    db.commit()
                    rolled_contracts.append({
                        "symbol": mapping.canonical_symbol,
                        "underlying": underlying,
                        "previous_expiry": old_expiry.strftime("%Y-%m-%d"),
                        "new_expiry": new_expiry.strftime("%Y-%m-%d"),
                        "contract": f"{month_code} {next_year}"
                    })

            if rolled_contracts:
                self.reload_cache()

            return rolled_contracts
        except Exception as e:
            db.rollback()
            print(f"Error checking commodity expiries: {e}")
            return []
        finally:
            db.close()

# Global Singleton instance
symbol_mapper = UniversalSymbolMapper()
