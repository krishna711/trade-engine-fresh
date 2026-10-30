import asyncio
import datetime
import uuid
from typing import Dict, Any, List, Optional
from backend.database.connection import SessionLocal
from backend.database.models import Account, Order, Position, Trade, StrategyInstance, SystemLog
from backend.order_manager.brokers.broker_adapters import create_broker_order_adapter
from backend.order_manager.reconciler import reconciler
from backend.core.symbol_mapper import symbol_mapper
from backend.core.events import event_bus

class OrderManager:
    """
    Broker Order Manager Microservice / Independent Module.
    Features:
    - Multi-Broker Support: XTS, KOTAKNEO, SHOONYA, ZERODHA, DHAN, FYERS
    - Parallel multi-client dispatch from single strategy signal
    - Paper Trading simulation engine + Live API routing
    - Real-time PnL calculation on live feed ticks
    - Account-level and Strategy-level Emergency Square-Off
    """

    def __init__(self):
        self._is_running = False
        self._tick_listener_task: Optional[asyncio.Task] = None

    async def start(self):
        if self._is_running:
            return
        self._is_running = True
        # Listen to ticks from event bus to update position mark-to-market and unrealized PnL
        event_bus.subscribe("feed.tick", self._on_tick_update)
        print("[OrderManager] Started successfully.")

    async def stop(self):
        self._is_running = False
        print("[OrderManager] Stopped.")

    async def place_order(
        self,
        client_id: str,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "LIMIT",
        price: float = 0.0,
        trigger_price: float = 0.0,
        product: str = "MIS",
        strategy_instance_id: Optional[int] = None,
        leg_id: Optional[str] = None,
        buffer_pct: float = 0.015,
        auto_chase: bool = True
    ) -> Dict[str, Any]:
        """
        Executes single client order with universal symbol mapping and reconciliation.
        """
        db = SessionLocal()
        try:
            account = db.query(Account).filter(Account.client_id == client_id).first()
            if not account:
                return {"success": False, "error": f"Client account '{client_id}' not found"}

            mapping = symbol_mapper.get_mapping(symbol)
            segment = mapping.segment if mapping else (symbol.split(":")[0] if ":" in symbol else "NSE")

            # For MCX / MCXFO, RMS blocks MIS orders across all brokers in India. Force NRML for MCX.
            prod_upper = product.upper()
            if "MCX" in symbol.upper() or segment.upper() in ["MCX", "MCXFO"]:
                if prod_upper == "MIS":
                    print(f"[OrderManager] Auto-switching MIS -> NRML for {symbol} ({segment}) to satisfy broker RMS rule.")
                    prod_upper = "NRML"

            engine_order_id = f"ORD_{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"

            order = Order(
                order_id=engine_order_id,
                client_id=client_id,
                strategy_instance_id=strategy_instance_id,
                symbol=symbol,
                segment=segment,
                broker=account.broker,
                side=side.upper(),
                order_type=order_type.upper(),
                product=prod_upper,
                quantity=quantity,
                price=price,
                trigger_price=trigger_price,
                status="NEW",
                is_paper=account.paper_mode,
                leg_id=leg_id
            )
            db.add(order)
            db.commit()

            # Dispatch order
            adapter = create_broker_order_adapter(account.broker, client_id, account.credentials_json)
            order_dict = {
                "order_id": engine_order_id,
                "symbol": symbol,
                "segment": segment,
                "side": side.upper(),
                "quantity": quantity,
                "order_type": order_type.upper(),
                "price": price,
                "trigger_price": trigger_price,
                "product": prod_upper,
                "is_paper": bool(account.paper_mode),
                "buffer_pct": float(buffer_pct),
                "auto_chase": bool(auto_chase),
                "ltp": price or (mapping.tick_size if mapping else 100.0)
            }

            resp = await adapter.place_order(order_dict)

            # Reconcile result
            is_success = bool(resp.get("success", False))
            order_status = resp.get("status", "FILLED" if is_success else "REJECTED")
            filled_qty = int(resp.get("filled_quantity", quantity if (is_success and order_status == "FILLED") else 0))
            fill_price = float(resp.get("average_price", price if is_success else 0.0))
            rej_reason = resp.get("rejection_reason")

            reconcile_report = {
                "order_id": engine_order_id,
                "broker_order_id": resp.get("broker_order_id"),
                "status": order_status,
                "filled_quantity": filled_qty,
                "average_price": fill_price,
                "client_id": client_id,
                "symbol": symbol,
                "rejection_reason": rej_reason
            }
            await reconciler.handle_execution_report(reconcile_report)

            return {
                "success": is_success and order_status not in ["REJECTED", "CANCELLED"],
                "client_id": client_id,
                "order_id": engine_order_id,
                "broker_order_id": resp.get("broker_order_id"),
                "status": order_status,
                "price": fill_price,
                "filled_quantity": filled_qty,
                "rejection_reason": rej_reason
            }

        except Exception as e:
            db.rollback()
            return {"success": False, "error": str(e)}
        finally:
            db.close()

    async def dispatch_parallel_multi_client(
        self,
        client_ids: List[str],
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "LIMIT",
        price: float = 0.0,
        trigger_price: float = 0.0,
        product: str = "MIS",
        strategy_instance_id: Optional[int] = None,
        leg_id: Optional[str] = None,
        buffer_pct: float = 0.015,
        auto_chase: bool = True
    ) -> List[Dict[str, Any]]:
        """
        One strategy maps to multiple broker/clients and fires orders in parallel.
        Executes concurrently with zero head-of-line blocking.
        """
        # For MCX / MCXFO, RMS blocks MIS orders across all brokers in India. Force NRML for MCX.
        if "MCX" in symbol.upper() and product.upper() == "MIS":
            product = "NRML"

        tasks = [
            self.place_order(
                client_id=cid,
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type=order_type,
                price=price,
                trigger_price=trigger_price,
                product=product,
                strategy_instance_id=strategy_instance_id,
                leg_id=leg_id,
                buffer_pct=buffer_pct,
                auto_chase=auto_chase
            )
            for cid in client_ids
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        formatted_results = []
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                formatted_results.append({"client_id": client_ids[i], "success": False, "error": str(res)})
            else:
                formatted_results.append(res)
        return formatted_results

    async def square_off_account(self, client_id: str) -> Dict[str, Any]:
        """Square off all open positions for an account prominently"""
        db = SessionLocal()
        squared_count = 0
        try:
            open_positions = db.query(Position).filter(
                Position.client_id == client_id,
                Position.net_quantity != 0
            ).all()

            for pos in open_positions:
                side = "SELL" if pos.net_quantity > 0 else "BUY"
                qty = abs(pos.net_quantity)
                await self.place_order(
                    client_id=client_id,
                    symbol=pos.symbol,
                    side=side,
                    quantity=qty,
                    order_type="MARKET",
                    price=pos.ltp,
                    strategy_instance_id=pos.strategy_instance_id
                )
                squared_count += 1

            return {"success": True, "squared_positions": squared_count, "client_id": client_id}
        finally:
            db.close()

    async def square_off_strategy(self, strategy_id: int) -> Dict[str, Any]:
        """Square off all open positions for a strategy across all mapped clients"""
        db = SessionLocal()
        squared_count = 0
        try:
            positions = db.query(Position).filter(
                Position.strategy_instance_id == strategy_id,
                Position.net_quantity != 0
            ).all()

            for pos in positions:
                side = "SELL" if pos.net_quantity > 0 else "BUY"
                qty = abs(pos.net_quantity)
                await self.place_order(
                    client_id=pos.client_id,
                    symbol=pos.symbol,
                    side=side,
                    quantity=qty,
                    order_type="MARKET",
                    price=pos.ltp,
                    strategy_instance_id=strategy_id
                )
                squared_count += 1

            # Update strategy state to STOPPED and trade status to CLOSED
            strat = db.query(StrategyInstance).filter(StrategyInstance.id == strategy_id).first()
            if strat:
                strat.status = "STOPPED"
                strat.last_trade_status = "CLOSED"
                db.commit()

            return {"success": True, "squared_positions": squared_count, "strategy_id": strategy_id}
        finally:
            db.close()

    async def square_off_account_strategy(self, client_id: str, strategy_id: int) -> Dict[str, Any]:
        """Square off open positions for a specific client under a specific strategy"""
        db = SessionLocal()
        squared_count = 0
        try:
            positions = db.query(Position).filter(
                Position.client_id == client_id,
                Position.strategy_instance_id == strategy_id,
                Position.net_quantity != 0
            ).all()

            for pos in positions:
                side = "SELL" if pos.net_quantity > 0 else "BUY"
                qty = abs(pos.net_quantity)
                await self.place_order(
                    client_id=pos.client_id,
                    symbol=pos.symbol,
                    side=side,
                    quantity=qty,
                    order_type="MARKET",
                    price=pos.ltp,
                    strategy_instance_id=strategy_id
                )
                squared_count += 1

            return {"success": True, "squared_positions": squared_count, "client_id": client_id, "strategy_id": strategy_id}
        finally:
            db.close()

    def _on_tick_update(self, tick: Dict[str, Any]):
        """Real-time MTM (Mark-To-Market) and Unrealized PnL recalculation on incoming ticks"""
        symbol = tick.get("symbol")
        ltp = tick.get("ltp")
        if not symbol or not ltp:
            return

        db = SessionLocal()
        try:
            positions = db.query(Position).filter(
                Position.symbol == symbol,
                Position.net_quantity != 0
            ).all()

            for pos in positions:
                pos.ltp = ltp
                if pos.net_quantity > 0: # Long position
                    pos.unrealized_pnl = round(pos.net_quantity * (ltp - pos.buy_avg_price), 2)
                elif pos.net_quantity < 0: # Short position
                    pos.unrealized_pnl = round(abs(pos.net_quantity) * (pos.sell_avg_price - ltp), 2)

            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

# Global singleton order manager
order_manager = OrderManager()
