import multiprocessing
import time
import importlib
import traceback
from typing import Dict, Any, List, Optional

def strategy_process_entrypoint(
    strategy_id: int,
    module_path: str,
    class_name: str,
    config: Dict[str, Any],
    client_ids: List[str],
    in_queue: multiprocessing.Queue,
    out_queue: multiprocessing.Queue
):
    """
    Isolated OS Process container for a strategy instance.
    Runs in its own process address space.
    Completely isolated from the main engine and any other strategy.
    """
    try:
        # Dynamically import the strategy module
        mod = importlib.import_module(module_path)
        strat_cls = getattr(mod, class_name)
        instance = strat_cls(strategy_id, config, client_ids)
        instance.on_init()

        out_queue.put({
            "type": "STATUS",
            "strategy_id": strategy_id,
            "status": "RUNNING",
            "message": "Strategy process worker initialized successfully"
        })

        last_candle_time = time.time()
        last_timer_call = time.time()

        while True:
            # Check for commands or ticks from supervisor
            try:
                msg = in_queue.get(timeout=0.2)
                msg_type = msg.get("type")

                if msg_type == "STOP":
                    out_queue.put({
                        "type": "STATUS",
                        "strategy_id": strategy_id,
                        "status": "STOPPED",
                        "message": "Strategy stopped cleanly"
                    })
                    break

                elif msg_type == "TICK":
                    tick = msg.get("data")
                    if tick:
                        instance.on_tick(tick)
                        # Flush any new decision logs
                        while instance.decision_logs:
                            log_entry = instance.decision_logs.pop(0)
                            out_queue.put({
                                "type": "DECISION_LOG",
                                "strategy_id": strategy_id,
                                "data": log_entry
                            })

                elif msg_type == "CANDLE":
                    candle = msg.get("candle")
                    history = msg.get("history", [])
                    instance.on_candle(candle, history)

                elif msg_type == "ORDER_RESULT":
                    instance.on_order_update(msg.get("data", {}))

                elif msg_type == "UPDATE_CONFIG":
                    new_cfg = msg.get("config", {})
                    if hasattr(instance, "on_config_updated"):
                        instance.on_config_updated(new_cfg)
                    elif hasattr(instance, "config"):
                        instance.config.update(new_cfg)

            except multiprocessing.queues.Empty:
                pass

            # Flush any new decision logs
            while instance.decision_logs:
                log_entry = instance.decision_logs.pop(0)
                out_queue.put({
                    "type": "DECISION_LOG",
                    "strategy_id": strategy_id,
                    "data": log_entry
                })

            # Flush any pending orders
            while instance.pending_orders:
                ord_req = instance.pending_orders.pop(0)
                out_queue.put({
                    "type": "ORDER",
                    "strategy_id": strategy_id,
                    "data": ord_req
                })

            # Flush any dynamic symbol subscriptions
            if instance.new_subscriptions:
                subs = list(instance.new_subscriptions)
                instance.new_subscriptions.clear()
                out_queue.put({
                    "type": "SUBSCRIBE_SYMBOLS",
                    "strategy_id": strategy_id,
                    "symbols": subs
                })

            # Periodic timer check (every 1 second) for time-based entries & exits and state sync
            now_ts = time.time()
            if now_ts - last_timer_call >= 1.0:
                last_timer_call = now_ts
                try:
                    if hasattr(instance, "on_timer"):
                        instance.on_timer()
                except Exception:
                    pass

                # Emit real-time trade state, leg LTPs & PnL sync
                try:
                    trade_obj = getattr(instance, "current_trade", None)
                    state_obj = getattr(instance, "trade_state", "IDLE")
                    if trade_obj or state_obj != "IDLE":
                        out_queue.put({
                            "type": "STATE_UPDATE",
                            "strategy_id": strategy_id,
                            "data": {
                                "trade_state": state_obj,
                                "current_trade": trade_obj
                            }
                        })
                except Exception:
                    pass

            # Heartbeat check
            time.sleep(0.01)


    except Exception as e:
        error_tb = traceback.format_exc()
        out_queue.put({
            "type": "ERROR",
            "strategy_id": strategy_id,
            "status": "ERROR",
            "error": str(e),
            "traceback": error_tb
        })
