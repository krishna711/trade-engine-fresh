import datetime
from typing import Dict, Any, Optional
from backend.database.connection import SessionLocal
from backend.database.models import Order, Trade, Position, SystemLog
from backend.core.events import event_bus
from backend.core.symbol_mapper import symbol_mapper

class OrderReconciler:
    """
    Solid Order Reconciliation Engine.
    Handles socket execution reports, webhook callbacks, and polling reconciliations.
    Ensures orders, trades, and positions in the engine are in exact sync with broker states.
    """

    async def handle_execution_report(self, report: Dict[str, Any]) -> bool:
        """
        Processes fill, rejection, or cancellation reports from WebSocket/Webhook.
        Expected format:
        {
            "broker_order_id": "...",
            "order_id": "...", # engine order id
            "status": "FILLED" / "REJECTED" / "CANCELLED",
            "filled_quantity": 50,
            "average_price": 24850.5,
            "client_id": "SH10023",
            "symbol": "NSE:NIFTY",
            "rejection_reason": ""
        }
        """
        db = SessionLocal()
        try:
            order_id = report.get("order_id")
            broker_order_id = report.get("broker_order_id")

            order = None
            if order_id:
                order = db.query(Order).filter(Order.order_id == order_id).first()
            elif broker_order_id:
                order = db.query(Order).filter(Order.broker_order_id == broker_order_id).first()

            if not order:
                print(f"[Reconciler] Order not found for report: {report}")
                return False

            new_status = report.get("status", order.status)
            order.status = new_status
            order.filled_quantity = report.get("filled_quantity", order.filled_quantity)
            order.average_price = report.get("average_price", order.average_price)
            if report.get("rejection_reason"):
                order.rejection_reason = report["rejection_reason"]
            order.updated_at = datetime.datetime.utcnow()

            # If filled, register trade record and update position
            if new_status == "FILLED" and order.filled_quantity > 0:
                trade_id = f"TRD_{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{order.id}"
                existing_trade = db.query(Trade).filter(Trade.order_id == order.order_id).first()
                if not existing_trade:
                    trade = Trade(
                        trade_id=trade_id,
                        order_id=order.order_id,
                        client_id=order.client_id,
                        strategy_instance_id=order.strategy_instance_id,
                        symbol=order.symbol,
                        segment=order.segment,
                        side=order.side,
                        quantity=order.filled_quantity,
                        price=order.average_price,
                        leg_id=order.leg_id,
                        timestamp=datetime.datetime.utcnow()
                    )
                    db.add(trade)

                # Update Position
                self._update_position(db, order)

            # Log execution event
            log_level = "SUCCESS" if new_status == "FILLED" else ("ERROR" if new_status == "REJECTED" else "INFO")
            msg = f"Order {order.order_id} ({order.symbol}) reconciled: {new_status} (Filled: {order.filled_quantity} @ {order.average_price})"
            if order.rejection_reason:
                msg += f" Reason: {order.rejection_reason}"

            log = SystemLog(
                category="BROKER_API",
                level=log_level,
                message=msg,
                client_id=order.client_id,
                strategy_id=order.strategy_instance_id,
                symbol=order.symbol,
                is_dismissed=False if log_level == "ERROR" else True
            )
            db.add(log)
            db.commit()

            # Publish order update on event bus for UI and Strategy Manager
            await event_bus.publish("order.update", {
                "order_id": order.order_id,
                "broker_order_id": order.broker_order_id,
                "client_id": order.client_id,
                "strategy_id": order.strategy_instance_id,
                "status": order.status,
                "symbol": order.symbol,
                "side": order.side,
                "filled_qty": order.filled_quantity,
                "avg_price": order.average_price
            })

            return True
        except Exception as e:
            db.rollback()
            print(f"[Reconciler] Error handling execution report: {e}")
            return False
        finally:
            db.close()

    def recalculate_position_for_symbol(self, db, client_id: str, symbol: str, strategy_instance_id: Optional[int] = None):
        """
        Recalculates a Position record cleanly from all FILLED orders for that symbol.
        Prevents double-counting and ensures exact buy/sell average prices and MTM/realized PnL.
        """
        orders = db.query(Order).filter(
            Order.client_id == client_id,
            Order.symbol == symbol,
            Order.strategy_instance_id == strategy_instance_id,
            Order.status == "FILLED"
        ).all()

        if not orders:
            return

        pos = db.query(Position).filter(
            Position.client_id == client_id,
            Position.symbol == symbol,
            Position.strategy_instance_id == strategy_instance_id
        ).first()

        if not pos:
            first_ord = orders[0]
            pos = Position(
                client_id=client_id,
                strategy_instance_id=strategy_instance_id,
                symbol=symbol,
                segment=first_ord.segment,
                product=first_ord.product,
                net_quantity=0,
                buy_quantity=0,
                buy_amount=0.0,
                sell_quantity=0,
                sell_amount=0.0,
                ltp=first_ord.average_price,
                unrealized_pnl=0.0,
                realized_pnl=0.0
            )
            db.add(pos)

        buy_qty = 0
        buy_amt = 0.0
        sell_qty = 0
        sell_amt = 0.0
        for o in orders:
            if o.side.upper() == "BUY":
                buy_qty += o.filled_quantity
                buy_amt += (o.filled_quantity * o.average_price)
            else:
                sell_qty += o.filled_quantity
                sell_amt += (o.filled_quantity * o.average_price)

        pos.buy_quantity = buy_qty
        pos.buy_amount = round(buy_amt, 2)
        pos.buy_avg_price = round(buy_amt / buy_qty, 2) if buy_qty > 0 else 0.0

        pos.sell_quantity = sell_qty
        pos.sell_amount = round(sell_amt, 2)
        pos.sell_avg_price = round(sell_amt / sell_qty, 2) if sell_qty > 0 else 0.0

        pos.net_quantity = buy_qty - sell_qty
        if pos.net_quantity == 0:
            pos.unrealized_pnl = 0.0

        # Realized PnL on closed contracts
        closed_qty = min(buy_qty, sell_qty)
        if closed_qty > 0 and pos.buy_avg_price > 0 and pos.sell_avg_price > 0:
            is_mcx = "MCX" in symbol.upper() or (pos.segment and "MCX" in pos.segment.upper())
            lot_mult = symbol_mapper.get_lot_size(symbol) if is_mcx else 1
            pos.realized_pnl = round(closed_qty * (pos.sell_avg_price - pos.buy_avg_price) * lot_mult, 2)

    def _update_position(self, db, order: Order):
        self.recalculate_position_for_symbol(db, order.client_id, order.symbol, order.strategy_instance_id)

    async def reconcile_open_orders(self):
        """
        Polls broker orderbook, tradebook, and positions for open and filled orders.
        Syncs fills and updates trade / position records automatically, ensuring genuine fill prices.
        """
        db = SessionLocal()
        try:
            cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=14)
            # 1. Open/Pending orders to reconcile
            open_orders = db.query(Order).filter(
                Order.status.in_(["NEW", "PENDING", "OPEN"]),
                Order.broker_order_id.isnot(None),
                Order.created_at >= cutoff
            ).all()

            # 2. Filled orders to verify fill prices against broker tradebook
            filled_orders = db.query(Order).filter(
                Order.status == "FILLED",
                Order.broker_order_id.isnot(None),
                Order.created_at >= cutoff
            ).all()

            all_to_check = open_orders + filled_orders
            if not all_to_check:
                return

            client_orders = {}
            for ord_rec in all_to_check:
                client_orders.setdefault(ord_rec.client_id, []).append(ord_rec)

            from backend.database.models import Account
            from backend.order_manager.brokers.broker_adapters import create_broker_order_adapter

            for cid, ord_list in client_orders.items():
                account = db.query(Account).filter(Account.client_id == cid).first()
                if not account or account.paper_mode:
                    continue

                adapter = create_broker_order_adapter(account.broker, account.client_id, account.credentials_json)
                
                # Fetch trades and positions once per client
                trades = []
                positions = []
                try:
                    trades = await adapter.get_trades()
                except Exception:
                    pass
                try:
                    positions = await adapter.get_positions()
                except Exception:
                    pass

                for ord_rec in ord_list:
                    if getattr(ord_rec, "is_paper", False):
                        continue
                    if ord_rec.status in ["NEW", "PENDING", "OPEN"]:
                        st_info = await adapter.get_order_status(ord_rec.broker_order_id)
                        current_st = str(st_info.get("status") or "").upper()

                        if current_st in ["FILLED", "REJECTED", "CANCELLED"] and current_st != ord_rec.status:
                            fill_p = float(st_info.get("average_price") or ord_rec.price)
                            fill_q = int(st_info.get("filled_quantity") or ord_rec.quantity)
                            print(f"[OrderReconciler] Auto-reconciling open order {ord_rec.order_id} ({ord_rec.symbol}): {ord_rec.status} -> {current_st} ({fill_q} @ {fill_p})")
                            await self.handle_execution_report({
                                "order_id": ord_rec.order_id,
                                "broker_order_id": ord_rec.broker_order_id,
                                "status": current_st,
                                "filled_quantity": fill_q,
                                "average_price": fill_p,
                                "client_id": ord_rec.client_id,
                                "symbol": ord_rec.symbol,
                                "side": ord_rec.side,
                                "rejection_reason": st_info.get("rejection_reason")
                            })
                    elif ord_rec.status == "FILLED":
                        # Verify filled price against tradebook and position book
                        real_p = 0.0
                        b_id = str(ord_rec.broker_order_id or "").strip()
                        # Check trades
                        for t in trades:
                            t_oids = [
                                str(t.get("order_id") or "").strip(),
                                str(t.get("broker_order_id") or "").strip(),
                                str(t.get("trade_id") or "").strip(),
                                str(t.get("app_order_id") or "").strip(),
                                str(t.get("exchange_order_id") or "").strip()
                            ]
                            raw_t = t.get("raw") or {}
                            t_oids.extend([
                                str(raw_t.get("AppOrderID") or "").strip(),
                                str(raw_t.get("OrderID") or "").strip(),
                                str(raw_t.get("ExchangeOrderID") or "").strip()
                            ])
                            if b_id in t_oids and float(t.get("price") or 0.0) > 0:
                                real_p = float(t["price"])
                                break
                        
                        # Fallback to positions if not in tradebook
                        if real_p <= 0.0:
                            ord_clean = ord_rec.symbol.split(":")[-1].replace(" ", "").upper()
                            for p in positions:
                                p_clean = str(p.get("symbol") or "").replace(" ", "").upper()
                                if ord_clean and (ord_clean in p_clean or p_clean in ord_clean):
                                    side_u = str(ord_rec.side or "SELL").upper()
                                    pos_p = float(p.get("sell_avg", 0.0) if side_u == "SELL" else p.get("buy_avg", 0.0) or 0.0)
                                    if pos_p > 0:
                                        real_p = pos_p
                                        break

                        # If real traded price found and differs from recorded price, update it!
                        if real_p > 0 and abs(ord_rec.average_price - real_p) > 0.01:
                            print(f"[OrderReconciler] Price correction for order {ord_rec.order_id} ({ord_rec.symbol}): {ord_rec.average_price} -> {real_p}")
                            ord_rec.average_price = real_p
                            trd = db.query(Trade).filter(Trade.order_id == ord_rec.order_id).first()
                            if trd:
                                trd.price = real_p
                            self.recalculate_position_for_symbol(db, ord_rec.client_id, ord_rec.symbol, ord_rec.strategy_instance_id)
                            db.commit()

                            # Dispatch event so StrategySupervisor and UI update immediately
                            await event_bus.publish("order.update", {
                                "order_id": ord_rec.order_id,
                                "broker_order_id": ord_rec.broker_order_id,
                                "client_id": ord_rec.client_id,
                                "strategy_id": ord_rec.strategy_instance_id,
                                "status": "FILLED",
                                "symbol": ord_rec.symbol,
                                "side": ord_rec.side,
                                "filled_qty": ord_rec.filled_quantity,
                                "avg_price": real_p
                            })
        except Exception as e:
            print(f"[OrderReconciler] Background reconciliation error: {e}")
        finally:
            db.close()

reconciler = OrderReconciler()
