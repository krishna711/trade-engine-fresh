import os
import re
import datetime
import calendar
from typing import Dict, Optional, Any, List, Tuple
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
        self._xts_options_map: Dict[Tuple[str, int, str], List[Tuple[datetime.datetime, str, str, str]]] = {}
        self._xts_futures_map: Dict[str, List[Tuple[datetime.datetime, str, str, str]]] = {}
        self._xts_token_to_canonical: Dict[str, str] = {}
        self._xts_canonical_to_token: Dict[str, str] = {}
        self._xts_canonical_to_symbol: Dict[str, str] = {}
        self.reload_cache()
        self.load_xts_master_file()

    def load_xts_master_file(self):
        """Loads Symphony XTS instrument master cache from data directory if present"""
        for candidate_path in [
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "xts_master_mcxfo.txt")),
            os.path.abspath(os.path.join(os.getcwd(), "data", "xts_master_mcxfo.txt")),
            r"d:\projects\trande-engine-fresh\data\xts_master_mcxfo.txt"
        ]:
            if os.path.exists(candidate_path):
                try:
                    with open(candidate_path, "r", encoding="utf-8") as f:
                        self.load_xts_master_content(f.read())
                    break
                except Exception as err:
                    print(f"[SymbolMapper] Warning reading master file {candidate_path}: {err}")

    def load_xts_master_content(self, text: str):
        """Parses pipe-delimited instruments from Symphony XTS into fast lookup structures"""
        if not text:
            return
        lines = text.splitlines()
        now = datetime.datetime.now()

        opt_map: Dict[Tuple[str, int, str], List[Tuple[datetime.datetime, str, str, str]]] = {}
        fut_map: Dict[str, List[Tuple[datetime.datetime, str, str, str]]] = {}

        for l in lines:
            parts = l.split("|")
            if len(parts) > 18:
                seg = parts[0]
                token = parts[1]
                inst_type = parts[2]
                root = parts[3].upper()
                trading_sym = parts[4]
                expiry_str = parts[16]
                strike_str = parts[17]
                opt_type_code = parts[18]

                try:
                    exp_dt = datetime.datetime.fromisoformat(expiry_str)
                    if exp_dt < now:
                        continue
                except Exception:
                    continue

                if inst_type == "2":  # Option
                    opt_type = "CE" if opt_type_code == "3" else ("PE" if opt_type_code == "4" else "")
                    if opt_type:
                        try:
                            strike = int(float(strike_str))
                            key = (root, strike, opt_type)
                            opt_map.setdefault(key, []).append((exp_dt, token, trading_sym, seg))
                        except Exception:
                            pass
                elif inst_type == "1":  # Future
                    fut_map.setdefault(root, []).append((exp_dt, token, trading_sym, seg))

        # Sort all lists by earliest expiry date (current active contract first)
        for k in opt_map:
            opt_map[k].sort(key=lambda x: x[0])
        for k in fut_map:
            fut_map[k].sort(key=lambda x: x[0])

        self._xts_options_map = opt_map
        self._xts_futures_map = fut_map

        # Map current futures tokens
        for root, f_list in fut_map.items():
            if f_list:
                exp_dt, token, trading_sym, seg = f_list[0]
                canonical = f"MCX:{root}"
                self._xts_token_to_canonical[token] = canonical
                self._xts_canonical_to_token[canonical] = token
                self._xts_canonical_to_symbol[canonical] = trading_sym

        print(f"[SymbolMapper] Loaded XTS master index: {len(opt_map)} option keys, {len(fut_map)} future keys")

    def reload_cache(self):
        db = SessionLocal()
        try:
            mappings = db.query(SymbolMapping).all()
            self._cache = {m.canonical_symbol: m for m in mappings}
        except Exception:
            # Self-heal: table might not exist yet if database is being initialized
            try:
                from backend.database.connection import init_db
                init_db()
                mappings = db.query(SymbolMapping).all()
                self._cache = {m.canonical_symbol: m for m in mappings}
            except Exception:
                self._cache = {}
        finally:
            db.close()

    def get_mapping(self, canonical_symbol: str) -> Optional[SymbolMapping]:
        if canonical_symbol not in self._cache:
            self.reload_cache()
        mapping = self._cache.get(canonical_symbol)
        if mapping:
            return mapping

        # Dynamically generate and cache mapping for options (e.g. MCXFO:CRUDEOILM9850CE, NFO:NIFTY24900PE)
        if ":" in canonical_symbol:
            segment, sym_part = canonical_symbol.split(":", 1)
            token_hash = str(abs(hash(canonical_symbol)) % 800000 + 100000)
            
            lot_size = get_default_lot_size(sym_part)
            tick_size = 0.1 if "MCX" in segment else 0.05
            exchange = segment.replace("FO", "")
            if not exchange:
                exchange = "NSE"

            xts_token_val = token_hash
            display_name_val = sym_part

            # Check if this matches an option strike (e.g. MCXFO:CRUDEOILM9850CE)
            opt_match = re.match(r"^([A-Z]+):([A-Z]+?)(\d+)(CE|PE)$", canonical_symbol)
            if opt_match:
                seg_grp, root_grp, strike_grp, type_grp = opt_match.groups()
                opt_key = (root_grp.upper(), int(strike_grp), type_grp.upper())
                candidates = self._xts_options_map.get(opt_key)
                if candidates:
                    exp_dt, real_token, real_trading_sym, seg_name = candidates[0]
                    xts_token_val = real_token
                    display_name_val = real_trading_sym
                    segment = seg_name or segment
                    self._xts_token_to_canonical[real_token] = canonical_symbol
                    self._xts_canonical_to_token[canonical_symbol] = real_token
                    self._xts_canonical_to_symbol[canonical_symbol] = real_trading_sym

            synthetic = SymbolMapping(
                canonical_symbol=canonical_symbol,
                display_name=display_name_val,
                segment=segment,
                exchange=exchange,
                xts_token=xts_token_val,
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
        if "xts" in broker_lower and token_str in self._xts_token_to_canonical:
            return self._xts_token_to_canonical[token_str]

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
        broker_upper = broker.upper()
        if broker_upper == "XTS" and canonical_symbol in self._xts_canonical_to_symbol:
            return self._xts_canonical_to_symbol[canonical_symbol]

        mapping = self.get_mapping(canonical_symbol)
        if not mapping:
            return canonical_symbol
        if broker_upper == "FYERS":
            return mapping.fyers_token or canonical_symbol
        elif broker_upper == "SHOONYA":
            return mapping.shoonya_token or canonical_symbol
        elif broker_upper == "XTS":
            return mapping.display_name or mapping.xts_token or canonical_symbol
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
