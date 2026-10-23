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
