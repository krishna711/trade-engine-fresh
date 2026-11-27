import asyncio
import datetime
import re
import uuid
from typing import Dict, Any, List, Optional
from sqlalchemy.orm.attributes import flag_modified
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
        # Start background orderbook reconciler loop (every 5 seconds)
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())
        print("[OrderManager] Started successfully with background orderbook reconciler.")

    async def stop(self):
        self._is_running = False
        if hasattr(self, "_reconcile_task") and self._reconcile_task and not self._reconcile_task.done():
            self._reconcile_task.cancel()
        print("[OrderManager] Stopped.")

    async def _reconcile_loop(self):
        while self._is_running:
            try:
                await reconciler.reconcile_open_orders()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[OrderManager] Background reconciler error: {e}")
            await asyncio.sleep(5.0)

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

            # Strategy-level and Account-level paper mode rule:
            # 1. If account.paper_mode is True -> ALL orders for this account run in PAPER mode (even if strategy is Live).
            # 2. If account.paper_mode is False (Live account) -> Strategy-level paper_mode applies:
            #    - Strategy in Live mode -> order executes in LIVE mode with real broker.
            #    - Strategy in Paper mode -> order executes in PAPER mode simulation.
            strategy_paper_mode = True
            if strategy_instance_id:
                strat = db.query(StrategyInstance).filter(StrategyInstance.id == strategy_instance_id).first()
                if strat:
                    strategy_paper_mode = bool(getattr(strat, "paper_mode", True))

            effective_is_paper = bool(account.paper_mode) or bool(strategy_paper_mode)

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
                is_paper=effective_is_paper,
                leg_id=leg_id
            )
            db.add(order)
            db.commit()

            # Holiday & Market Session guard for live broker orders
            from backend.feed_manager.market_hours import is_market_open, is_trading_holiday
            if not effective_is_paper and not is_market_open(symbol):
                is_hol, hol_name = is_trading_holiday(symbol)
                block_reason = f"Exchange {segment} is closed today for {hol_name} (Trading Holiday)" if is_hol else f"Exchange {segment} is outside active trading hours."
                print(f"[OrderManager] Holiday/Session Guard: Blocked live order for {client_id} on {symbol}: {block_reason}")
                order.status = "REJECTED"
                order.rejection_reason = block_reason
                db.commit()
                return {
                    "success": False,
                    "client_id": client_id,
                    "order_id": engine_order_id,
                    "broker_order_id": None,
                    "status": "REJECTED",
                    "error": block_reason,
                    "rejection_reason": block_reason
                }

            # Resolve live market LTP when price is omitted or in paper mode (never fall back to tick_size 0.05!)
            live_ltp = float(price or 0.0)
            if live_ltp <= 0.0:
                try:
                    from backend.feed_manager.manager import feed_manager
                    q = await feed_manager.get_live_quote(symbol)
                    if q and float(q.get("ltp") or 0.0) > 0:
                        live_ltp = float(q["ltp"])
                except Exception:
                    pass
            if live_ltp <= 0.0:
                live_ltp = 100.0

            # Dispatch order
            adapter = create_broker_order_adapter(account.broker, client_id, account.credentials_json, static_ip=account.static_ip)
            order_dict = {
                "order_id": engine_order_id,
                "symbol": symbol,
                "segment": segment,
                "side": side.upper(),
                "quantity": quantity,
                "order_type": order_type.upper(),
                "price": float(price or 0.0) if float(price or 0.0) > 0 else live_ltp,
                "trigger_price": trigger_price,
                "product": prod_upper,
                "is_paper": effective_is_paper,
                "buffer_pct": float(buffer_pct),
                "auto_chase": bool(auto_chase),
                "ltp": live_ltp
            }

            resp = await adapter.place_order(order_dict)

            # Reconcile result
            is_success = bool(resp.get("success", False))
            order_status = resp.get("status", "FILLED" if is_success else "REJECTED")
            filled_qty = int(resp.get("filled_quantity", quantity if (is_success and order_status == "FILLED") else 0))
            if quantity > 0 and filled_qty > quantity:
                filled_qty = quantity
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

            order_results = {}
            for pos in positions:
                side = "SELL" if pos.net_quantity > 0 else "BUY"
                qty = abs(pos.net_quantity)
                clean_sym = pos.symbol.split(":")[-1]
                leg_id = f"EXIT_{clean_sym}"
                res = await self.place_order(
                    client_id=pos.client_id,
                    symbol=pos.symbol,
                    side=side,
                    quantity=qty,
                    order_type="MARKET",
                    price=pos.ltp,
                    strategy_instance_id=strategy_id,
                    leg_id=leg_id
                )
                order_results[pos.symbol] = res
                squared_count += 1

            # Update strategy state: Keep RUNNING so strategy can trade on next scheduled session/time!
            strat = db.query(StrategyInstance).filter(StrategyInstance.id == strategy_id).first()
            if strat:
                strat.last_trade_status = "CLOSED"
                strat.unrealized_pnl = 0.0

                if strat.legs_info:
                    closed_legs = []
                    tot_realized_from_legs = 0.0
                    for leg in strat.legs_info:
                        l_copy = dict(leg)
                        l_sym = str(l_copy.get("symbol") or l_copy.get("leg") or "")
                        match_res = None
                        for s_sym, r in order_results.items():
                            if s_sym in l_sym or l_sym in s_sym or s_sym.split(":")[-1] in l_sym:
                                match_res = r
                                break

                        if match_res and match_res.get("price") and match_res.get("price") > 0:
                            l_copy["exit"] = float(match_res["price"])
                            l_copy["status"] = "CLOSED"
                        elif not l_copy.get("exit"):
                            l_copy["exit"] = float(l_copy.get("ltp") or l_copy.get("entry") or 0.0)
                            l_copy["status"] = "CLOSED"

                        # Calculate leg realized PnL
                        entry_p = float(l_copy.get("entry") or 0.0)
                        exit_p = float(l_copy.get("exit") or entry_p)
                        leg_qty = abs(int(l_copy.get("qty") or 1))
                        is_mcx = "MCX" in l_sym.upper()
                        mult = 10 if is_mcx and "CRUDE" in l_sym.upper() else 1
                        leg_pnl = round((entry_p - exit_p) * leg_qty * mult, 2)
                        l_copy["pnl"] = leg_pnl
                        tot_realized_from_legs += leg_pnl
                        closed_legs.append(l_copy)

                    strat.legs_info = closed_legs
                    flag_modified(strat, "legs_info")
                    strat.realized_pnl = round((strat.realized_pnl or 0.0) + tot_realized_from_legs, 2)
                    strat.total_pnl = strat.realized_pnl
                db.commit()

            # Zero out any position records for this strategy instance
            db.query(Position).filter(Position.strategy_instance_id == strategy_id).update({
                Position.net_quantity: 0,
                Position.unrealized_pnl: 0.0
            })
            db.commit()

            # Reset worker process internal trade_state to IDLE so it can enter on future time / new session
            try:
                from backend.strategy_manager.manager import strategy_supervisor
                strategy_supervisor.reset_strategy_trade(strategy_id)
            except Exception as e_sup:
                print(f"[OrderManager] Note: Could not send reset_strategy_trade: {e_sup}")

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
                Position.symbol == symbol
            ).all()

            for pos in positions:
                pos.ltp = ltp

                # If position is associated with a strategy that is closed, force net_quantity=0 and unrealized_pnl=0.0
                if pos.strategy_instance_id:
                    strat = db.query(StrategyInstance).filter(StrategyInstance.id == pos.strategy_instance_id).first()
                    if not strat or strat.last_trade_status == "CLOSED":
                        pos.net_quantity = 0
                        pos.unrealized_pnl = 0.0
                        continue

                if pos.net_quantity == 0:
                    pos.unrealized_pnl = 0.0
                    continue

                is_mcx = "MCX" in (pos.symbol or "").upper() or (pos.segment and "MCX" in pos.segment.upper())
                lot_mult = symbol_mapper.get_lot_size(pos.symbol) if is_mcx else 1

                if pos.net_quantity > 0: # Long position
                    pos.unrealized_pnl = round(pos.net_quantity * (ltp - pos.buy_avg_price) * lot_mult, 2)
                elif pos.net_quantity < 0: # Short position
                    pos.unrealized_pnl = round(abs(pos.net_quantity) * (pos.sell_avg_price - ltp) * lot_mult, 2)

            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

# Global singleton order manager
order_manager = OrderManager()
