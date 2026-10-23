import datetime
from typing import Dict, Any, Optional
from backend.database.connection import SessionLocal
from backend.database.models import Order, Trade, Position, SystemLog
from backend.core.events import event_bus

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

    def _update_position(self, db, order: Order):
        pos = db.query(Position).filter(
            Position.client_id == order.client_id,
            Position.symbol == order.symbol,
            Position.strategy_instance_id == order.strategy_instance_id
        ).first()

        qty = order.filled_quantity
        price = order.average_price

        if not pos:
            pos = Position(
                client_id=order.client_id,
                strategy_instance_id=order.strategy_instance_id,
                symbol=order.symbol,
                segment=order.segment,
                product=order.product,
                net_quantity=0,
                buy_quantity=0,
                buy_amount=0.0,
                sell_quantity=0,
                sell_amount=0.0,
                ltp=price,
                unrealized_pnl=0.0,
                realized_pnl=0.0
            )
            db.add(pos)

        if order.side.upper() == "BUY":
            pos.buy_quantity += qty
            pos.buy_amount += (qty * price)
            pos.buy_avg_price = pos.buy_amount / pos.buy_quantity if pos.buy_quantity > 0 else 0.0
            pos.net_quantity += qty
        else: # SELL
            pos.sell_quantity += qty
            pos.sell_amount += (qty * price)
            pos.sell_avg_price = pos.sell_amount / pos.sell_quantity if pos.sell_quantity > 0 else 0.0
            pos.net_quantity -= qty

        # Compute realized PnL on closed quantity
        closed_qty = min(pos.buy_quantity, pos.sell_quantity)
        if closed_qty > 0 and pos.buy_avg_price > 0 and pos.sell_avg_price > 0:
            pos.realized_pnl = round(closed_qty * (pos.sell_avg_price - pos.buy_avg_price), 2)

reconciler = OrderReconciler()
