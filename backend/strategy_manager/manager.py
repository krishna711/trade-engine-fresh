import asyncio
import multiprocessing
import datetime
from typing import Dict, Any, List, Optional
from backend.database.connection import SessionLocal
from backend.database.models import StrategyInstance, StrategyTemplate, SystemLog, Account
from backend.strategy_manager.worker import strategy_process_entrypoint
from backend.core.events import event_bus
from backend.order_manager.manager import order_manager

MODULE_CLASS_MAP = {
    "strat_time_atm_seller_v1": ("backend.strategy_manager.registered.time_atm_seller", "TimeBasedATMSellerStrategy"),
    "strat_ema_cross_v1": ("backend.strategy_manager.registered.ema_crossover", "EMACrossoverStrategy"),
    "strat_short_straddle_v2": ("backend.strategy_manager.registered.short_straddle", "ShortStraddleStrategy"),
    "strat_supertrend_mcx_v1": ("backend.strategy_manager.registered.supertrend_mcx", "SupertrendMCXStrategy")
}

class StrategySupervisor:
    """
    Supervisor for isolated strategy execution.
    Runs each strategy in its own process container so one strategy
    can never disturb another strategy or crash the main engine.
    """

    def __init__(self):
        # instance_id -> {process, in_queue, out_queue, config, client_ids}
        self.active_workers: Dict[int, Dict[str, Any]] = {}
        self._is_running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._candle_sync_task: Optional[asyncio.Task] = None

    async def start(self):
        if self._is_running:
            return
        self._is_running = True

        # Subscribe to feed ticks to route to relevant strategy workers
        event_bus.subscribe("feed.tick", self._on_feed_tick)

        # Start supervisor loop to poll queues from child worker processes
        self._poll_task = asyncio.create_task(self._poll_worker_queues())
        self._candle_sync_task = asyncio.create_task(self._real_candle_sync_loop())

        # Auto-start strategies that have status RUNNING in DB
        await self._auto_start_saved_strategies()
        print("[StrategyManager] Supervisor started successfully.")

    async def stop(self):
        self._is_running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        if self._candle_sync_task and not self._candle_sync_task.done():
            self._candle_sync_task.cancel()

        for inst_id in list(self.active_workers.keys()):
            await self.stop_strategy(inst_id)
        print("[StrategyManager] Supervisor stopped.")


    async def _auto_start_saved_strategies(self):
        strat_ids = []
        db = SessionLocal()
        try:
            running_strats = db.query(StrategyInstance).filter(StrategyInstance.status == "RUNNING").all()
            strat_ids = [st.id for st in running_strats]
        finally:
            db.close()

        for sid in strat_ids:
            await self.start_strategy(sid)

    async def start_strategy(self, instance_id: int) -> bool:
        if instance_id in self.active_workers:
            return True

        db = SessionLocal()
        try:
            inst = db.query(StrategyInstance).filter(StrategyInstance.id == instance_id).first()
            if not inst:
                return False

            tpl = db.query(StrategyTemplate).filter(StrategyTemplate.key == inst.template_key).first()
            if not tpl:
                return False

            mod_info = MODULE_CLASS_MAP.get(tpl.key, ("backend.strategy_manager.registered.ema_crossover", "EMACrossoverStrategy"))
            module_path, class_name = mod_info

            in_q = multiprocessing.Queue()
            out_q = multiprocessing.Queue()

            proc = multiprocessing.Process(
                target=strategy_process_entrypoint,
                args=(
                    inst.id,
                    module_path,
                    class_name,
                    inst.config or {},
                    inst.client_ids or [],
                    in_q,
                    out_q
                ),
                daemon=True
            )
            proc.start()

            self.active_workers[instance_id] = {
                "process": proc,
                "in_queue": in_q,
                "out_queue": out_q,
                "symbol": (inst.config or {}).get("symbol", "MCX:CRUDEOILM"),
                "extra_symbols": set(),
                "client_ids": inst.client_ids or [],
                "template_key": inst.template_key
            }


            inst.status = "RUNNING"
            db.commit()

            self._log_strategy_decision(
                instance_id,
                "INFO",
                f"Strategy process container spawned (PID: {proc.pid}) mapped to {len(inst.client_ids or [])} clients"
            )
            return True
        except Exception as e:
            print(f"[StrategyManager] Error starting strategy {instance_id}: {e}")
            return False
        finally:
            db.close()

    async def stop_strategy(self, instance_id: int) -> bool:
        worker = self.active_workers.get(instance_id)
        if not worker:
            return True

        try:
            in_q = worker["in_queue"]
            in_q.put({"type": "STOP"})
            proc = worker["process"]
            proc.join(timeout=1.5)
            if proc.is_alive():
                proc.terminate()
        except Exception as e:
            print(f"[StrategyManager] Error stopping worker {instance_id}: {e}")

        self.active_workers.pop(instance_id, None)

        db = SessionLocal()
        try:
            inst = db.query(StrategyInstance).filter(StrategyInstance.id == instance_id).first()
            if inst:
                inst.status = "STOPPED"
                db.commit()
        finally:
            db.close()

        self._log_strategy_decision(instance_id, "INFO", "Strategy process container stopped")
        return True

    async def update_strategy_config(self, instance_id: int, new_config: Dict[str, Any]) -> bool:
        """
        Sends dynamic parameter updates directly to the running worker container process.
        Takes effect immediately without dropping connection or stopping the strategy.
        """
        worker = self.active_workers.get(instance_id)
        if worker and "in_queue" in worker:
            try:
                worker["in_queue"].put({
                    "type": "UPDATE_CONFIG",
                    "config": new_config
                })
                if "symbol" in new_config:
                    worker["symbol"] = new_config["symbol"]
                self._log_strategy_decision(
                    instance_id,
                    "SUCCESS",
                    f"Live parameter update delivered to worker process: {list(new_config.keys())}"
                )
                return True
            except Exception as e:
                print(f"[StrategyManager] Error updating live config for {instance_id}: {e}")
                return False
        return False

    async def duplicate_strategy(
        self,
        source_instance_id: int,
        new_name_admin: str,
        new_name_user: str,
        custom_config: Optional[Dict[str, Any]] = None,
        custom_client_ids: Optional[List[str]] = None
    ) -> Optional[int]:
        """
        Duplicates an existing strategy with different parameters or symbols.
        Main tested logic remains identical while creating a separate runtime instance.
        """
        db = SessionLocal()
        try:
            src = db.query(StrategyInstance).filter(StrategyInstance.id == source_instance_id).first()
            if not src:
                return None

            merged_config = dict(src.config or {})
            if custom_config:
                merged_config.update(custom_config)

            client_ids = custom_client_ids if custom_client_ids is not None else list(src.client_ids or [])

            clone = StrategyInstance(
                template_key=src.template_key,
                instance_name_admin=new_name_admin,
                instance_name_user=new_name_user,
                client_ids=client_ids,
                status="STOPPED",
                config=merged_config,
                start_time=src.start_time,
                end_time=src.end_time,
                last_trade_status="CLOSED",
                unrealized_pnl=0.0,
                realized_pnl=0.0,
                total_pnl=0.0,
                legs_info=[]
            )
            db.add(clone)
            db.commit()
            db.refresh(clone)
            clone_id = clone.id
            db.close()

            self._log_strategy_decision(
                clone_id,
                "SUCCESS",
                f"Strategy duplicated from Instance #{source_instance_id}. Ready for independent execution."
            )
            return clone_id
        except Exception as e:
            try:
                db.rollback()
                db.close()
            except Exception:
                pass
            print(f"[StrategyManager] Error duplicating strategy: {e}")
            return None

    def _on_feed_tick(self, tick: Dict[str, Any]):
        """Dispatches incoming ticks to worker processes subscribed to this symbol or its option legs"""
        sym = tick.get("symbol")
        ltp = tick.get("ltp")
        if not sym:
            return

        for inst_id, worker in self.active_workers.items():
            worker_sym = worker.get("symbol")
            extra_syms = worker.get("extra_symbols", set())
            
            # Match underlying symbol or option strikes
            matched = (worker_sym == sym) or (sym in extra_syms)
            if not matched and ":" in sym:
                suffix = sym.split(":")[-1]
                matched = any(suffix in es for es in extra_syms)

            if matched:
                try:
                    worker["in_queue"].put({"type": "TICK", "data": tick})
                except Exception:
                    pass

    async def _poll_worker_queues(self):
        """Polls messages emitted by isolated strategy processes"""
        while self._is_running:
            try:
                for inst_id, worker in list(self.active_workers.items()):
                    out_q = worker["out_queue"]
                    while not out_q.empty():
                        msg = out_q.get_nowait()
                        msg_type = msg.get("type")

                        if msg_type == "DECISION_LOG":
                            log_data = msg["data"]
                            step = log_data.get("step")
                            details = log_data.get("details")
                            level = "SUCCESS" if ("MATCH" in step or "FILLED" in step or "ACTIVE" in step) else "INFO"
                            self._log_strategy_decision(inst_id, level, f"{step} - {details}")

                        elif msg_type == "ORDER":
                            ord_data = msg["data"]
                            client_ids = worker.get("client_ids", [])
                            # Dispatch concurrently across all mapped broker accounts
                            order_results = await order_manager.dispatch_parallel_multi_client(
                                client_ids=client_ids,
                                symbol=ord_data["symbol"],
                                side=ord_data["side"],
                                quantity=ord_data["quantity"],
                                order_type=ord_data.get("order_type", "MARKET"),
                                price=ord_data.get("price", 0.0),
                                trigger_price=ord_data.get("trigger_price", 0.0),
                                product=ord_data.get("product", "MIS"),
                                strategy_instance_id=inst_id,
                                leg_id=ord_data.get("leg_id")
                            )
                            # Feed execution report back into child worker process container
                            worker["in_queue"].put({
                                "type": "ORDER_RESULT",
                                "data": {
                                    "order_request": ord_data,
                                    "results": order_results
                                }
                            })
                            # Synchronize database state
                            self._update_instance_trade_state(inst_id, ord_data, order_results)

                        elif msg_type == "SUBSCRIBE_SYMBOLS":
                            symbols = msg.get("symbols", [])
                            if symbols:
                                from backend.feed_manager.manager import feed_manager
                                await feed_manager.subscribe(symbols)
                                worker.setdefault("extra_symbols", set()).update(symbols)

                        elif msg_type == "ERROR":
                            error_text = msg.get("error", "Unknown error")
                            self._log_strategy_decision(inst_id, "ERROR", f"CRASH: {error_text}", is_dismissed=False)
                            # Handle crash in child worker without killing supervisor
                            db = SessionLocal()
                            try:
                                inst = db.query(StrategyInstance).filter(StrategyInstance.id == inst_id).first()
                                if inst:
                                    inst.status = "ERROR"
                                    db.commit()
                            finally:
                                db.close()

                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[StrategySupervisor] Poller error: {e}")
                await asyncio.sleep(0.5)

    def _update_instance_trade_state(self, instance_id: int, ord_data: Dict[str, Any], results: List[Dict[str, Any]]):
        """Syncs filled order details with the StrategyInstance record in database"""
        db = SessionLocal()
        try:
            inst = db.query(StrategyInstance).filter(StrategyInstance.id == instance_id).first()
            if not inst:
                return

            side = ord_data.get("side", "SELL").upper()
            sym = ord_data.get("symbol")
            avg_p = 0.0
            filled_count = 0
            for r in results:
                if r.get("success"):
                    avg_p += float(r.get("price", 0.0))
                    filled_count += 1
            if filled_count > 0:
                avg_p = round(avg_p / filled_count, 2)

            current_legs = list(inst.legs_info or [])
            if side == "SELL": # Entry
                inst.last_trade_status = "OPEN"
                inst.last_trade_at = datetime.datetime.utcnow()
                found = False
                for leg in current_legs:
                    if leg.get("symbol") == sym:
                        leg["entry"] = avg_p
                        leg["ltp"] = avg_p
                        leg["qty"] = -ord_data.get("quantity", 10)
                        found = True
                        break
                if not found:
                    current_legs.append({
                        "leg": sym.split(":")[-1],
                        "symbol": sym,
                        "entry": avg_p,
                        "exit": None,
                        "ltp": avg_p,
                        "qty": -ord_data.get("quantity", 10),
                        "pnl": 0.0
                    })
                inst.legs_info = current_legs
            elif side == "BUY": # Exit
                for leg in current_legs:
                    if leg.get("symbol") == sym:
                        leg["exit"] = avg_p
                        leg["pnl"] = round(((leg.get("entry") or avg_p) - avg_p) * abs(leg.get("qty", 10)), 2)
                inst.legs_info = current_legs
                if all(leg.get("exit") is not None for leg in current_legs):
                    inst.last_trade_status = "CLOSED"
                    closed_pnl = sum(leg.get("pnl", 0.0) for leg in current_legs)
                    inst.realized_pnl += closed_pnl
                    inst.unrealized_pnl = 0.0
                    inst.total_pnl = inst.realized_pnl

            db.commit()
        except Exception as e:
            db.rollback()
            print(f"[StrategySupervisor] Error updating instance state: {e}")
        finally:
            db.close()

    async def _real_candle_sync_loop(self):
        """Syncs genuine in-memory OHLC bars built by FeedManager to strategy worker queues"""
        from backend.feed_manager.manager import feed_manager
        while self._is_running:
            try:
                await asyncio.sleep(10.0)
                for inst_id, worker in list(self.active_workers.items()):
                    sym = worker.get("symbol", "MCX:CRUDEOILM")
                    bars = feed_manager.ohlc_builder.get_bars(sym, resolution="5m", count=20)
                    if bars:
                        latest_bar = bars[-1]
                        history = bars[:-1]
                        try:
                            worker["in_queue"].put({"type": "CANDLE", "candle": latest_bar, "history": history})
                        except Exception:
                            pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[StrategySupervisor] Candle sync error: {e}")
                await asyncio.sleep(5.0)


    def _log_strategy_decision(self, strategy_id: int, level: str, message: str, is_dismissed: bool = True):
        db = SessionLocal()
        try:
            log = SystemLog(
                category="STRATEGY_DECISION",
                level=level,
                message=message,
                strategy_id=strategy_id,
                is_dismissed=is_dismissed,
                timestamp=datetime.datetime.utcnow()
            )
            db.add(log)
            db.commit()

            # Broadcast to event bus for real-time UI logging
            asyncio.create_task(event_bus.publish("strategy.event", {
                "id": log.id,
                "strategy_id": strategy_id,
                "level": level,
                "message": message,
                "timestamp": log.timestamp.isoformat(),
                "is_dismissed": is_dismissed
            }))
        except Exception:
            db.rollback()
        finally:
            db.close()

# Global Strategy Supervisor Singleton
strategy_supervisor = StrategySupervisor()
