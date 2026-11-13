import asyncio
import datetime
import html
import json
import logging
from typing import Any, Dict, List, Optional, Set
import requests

from backend.database.connection import SessionLocal
from backend.database.models import User, TelegramSetting, Account, StrategyInstance

logger = logging.getLogger("TelegramNotifier")


class TelegramNotifier:
    """
    Central Telegram Notification Engine for Fresh Trade Engine.
    Handles 3 distinct tiers: CRITICAL (🚨), WARNING (⚠️), and INFO (ℹ️ / 🟢 / 🔴).
    Provides user-isolated routing and admin filtering capabilities.
    """

    def __init__(self):
        self._warned_strategies_today: Set[str] = set() # keys: "YYYYMMDD_strategyId"
        self._premarket_warned_today: Optional[str] = None # "YYYYMMDD"
        self._scheduler_task: Optional[asyncio.Task] = None
        self._is_running = False

    def get_system_default_bot_token(self, db) -> str:
        """Finds any configured admin bot token to serve as system default fallback"""
        admin_setting = (
            db.query(TelegramSetting)
            .join(User, TelegramSetting.user_id == User.id)
            .filter(User.role == "ADMIN", TelegramSetting.bot_token != "")
            .first()
        )
        if admin_setting and admin_setting.bot_token:
            return admin_setting.bot_token.strip()
        return ""

    def _send_http_request(self, bot_token: str, chat_id: str, message_html: str) -> Dict[str, Any]:
        """Synchronous HTTP request to Telegram Bot API (executed in thread pool)"""
        if not bot_token or not chat_id:
            return {"success": False, "error": "Bot token or Chat ID is missing"}

        url = f"https://api.telegram.org/bot{bot_token.strip()}/sendMessage"
        payload = {
            "chat_id": chat_id.strip(),
            "text": message_html,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }

        try:
            resp = requests.post(url, json=payload, timeout=8)
            data = resp.json()
            if resp.status_code == 200 and data.get("ok"):
                return {"success": True, "data": data}
            else:
                err_desc = data.get("description", f"HTTP {resp.status_code}")
                logger.warning(f"[Telegram] Send failed for chat_id {chat_id}: {err_desc}")
                return {"success": False, "error": err_desc}
        except requests.exceptions.RequestException as e:
            logger.error(f"[Telegram] Network exception: {e}")
            return {"success": False, "error": str(e)}

    async def send_raw_message(self, bot_token: str, chat_id: str, message_html: str) -> Dict[str, Any]:
        """Asynchronously dispatches message to Telegram via thread pool without blocking"""
        return await asyncio.to_thread(self._send_http_request, bot_token, chat_id, message_html)

    # =========================================================================
    # 3-LEVEL MESSAGE FORMATTERS
    # =========================================================================

    def format_critical(self, title: str, details: str, component: str = "System Engine", entity: str = "") -> str:
        """
        🚨 CRITICAL Format:
        System-related failures, feed dropouts, strategy crashes, unhandled engine exceptions.
        """
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        safe_title = html.escape(str(title))
        safe_details = html.escape(str(details))
        safe_comp = html.escape(str(component))
        safe_ent = html.escape(str(entity))

        ent_row = f"🎯 <b>Target:</b> <code>{safe_ent}</code>\n" if safe_ent else ""

        return (
            f"🚨 <b>[CRITICAL SYSTEM ALERT]</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏢 <b>Component:</b> {safe_comp}\n"
            f"{ent_row}"
            f"🔥 <b>Alert:</b> <b>{safe_title}</b>\n"
            f"📝 <b>Details:</b> <code>{safe_details}</code>\n"
            f"⏰ <b>Time:</b> {now_str} IST\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ <i>Immediate attention may be required in terminal.</i>"
        )

    def format_warning(
        self,
        title: str,
        account_info: str = "",
        strategy_name: str = "",
        scheduled_time: str = "",
        action_required: str = ""
    ) -> str:
        """
        ⚠️ WARNING Format:
        Pre-market checks, token expiry, 1-hour pre-strategy start login reminders.
        """
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        safe_title = html.escape(str(title))
        safe_acc = html.escape(str(account_info))
        safe_strat = html.escape(str(strategy_name))
        safe_sched = html.escape(str(scheduled_time))
        safe_action = html.escape(str(action_required or "Please login to broker portal and refresh token."))

        lines = [
            "⚠️ <b>[ACTION REQUIRED: BROKER TOKEN & LOGIN]</b>",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🔔 <b>Notice:</b> <b>{safe_title}</b>"
        ]
        if safe_acc:
            lines.append(f"👤 <b>Account/Broker:</b> <code>{safe_acc}</code>")
        if safe_strat:
            lines.append(f"📌 <b>Strategy:</b> <b>{safe_strat}</b>")
        if safe_sched:
            lines.append(f"⏰ <b>Scheduled Run:</b> {safe_sched} IST")
        lines.append(f"👉 <b>Required Action:</b> {safe_action}")
        lines.append(f"⏰ <b>Time:</b> {now_str} IST")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        return "\n".join(lines)

    def format_info_order(
        self,
        status: str,
        side: str,
        quantity: int,
        symbol: str,
        price: float,
        client_id: str,
        strategy_name: str = "",
        mode: str = "PAPER",
        rejection_reason: str = ""
    ) -> str:
        """
        ℹ️ INFO Format for Orders:
        Order placed, filled, or updated.
        """
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        side_upper = str(side).upper()
        status_upper = str(status).upper()
        mode_upper = str(mode).upper()
        safe_sym = html.escape(str(symbol))
        safe_client = html.escape(str(client_id))
        safe_strat = html.escape(str(strategy_name or "Manual / System"))

        icon = "🟢" if side_upper == "BUY" else "🔴"
        mode_badge = "🧪 PAPER" if mode_upper == "PAPER" else "⚡ LIVE REAL"

        lines = [
            f"ℹ️ <b>[ORDER EXECUTION UPDATE]</b>",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"⚡ <b>Status:</b> <b>{status_upper}</b> ({icon} {side_upper} {quantity} qty)",
            f"🎯 <b>Symbol:</b> <code>{safe_sym}</code>",
            f"💰 <b>Price:</b> ₹{price:,.2f}",
            f"👤 <b>Client:</b> <code>{safe_client}</code>",
            f"📊 <b>Strategy:</b> {safe_strat}",
            f"🏷️ <b>Mode:</b> {mode_badge}",
            f"⏰ <b>Time:</b> {now_str} IST",
        ]
        if rejection_reason and status_upper in ["REJECTED", "CANCELLED"]:
            lines.append(f"❌ <b>Reason:</b> <code>{html.escape(rejection_reason)}</code>")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        return "\n".join(lines)

    def format_info_trade_cycle(
        self,
        strategy_name: str,
        client_id: str,
        symbol: str,
        realized_pnl: float,
        reason: str = "EXIT_TIME",
        mode: str = "PAPER"
    ) -> str:
        """
        ℹ️ / 🟢 / 🔴 INFO Format for Closed Trade Cycles with PnL.
        """
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        safe_strat = html.escape(str(strategy_name))
        safe_client = html.escape(str(client_id))
        safe_sym = html.escape(str(symbol))
        safe_reason = html.escape(str(reason or "EXIT"))
        mode_upper = str(mode).upper()
        mode_badge = "🧪 PAPER" if mode_upper == "PAPER" else "⚡ LIVE REAL"

        is_profit = realized_pnl >= 0.0
        pnl_icon = "🟢" if is_profit else "🔴"
        pnl_formatted = f"+₹{realized_pnl:,.2f}" if is_profit else f"-₹{abs(realized_pnl):,.2f}"

        return (
            f"{pnl_icon} <b>[TRADE CYCLE CLOSED]</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Strategy:</b> <b>{safe_strat}</b>\n"
            f"👤 <b>Client:</b> <code>{safe_client}</code>\n"
            f"🎯 <b>Symbol:</b> <code>{safe_sym}</code>\n"
            f"💵 <b>Realized PnL:</b> <b>{pnl_formatted}</b>\n"
            f"🏁 <b>Exit Reason:</b> {safe_reason}\n"
            f"🏷️ <b>Mode:</b> {mode_badge}\n"
            f"⏰ <b>Time:</b> {now_str} IST\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )

    # =========================================================================
    # RECIPIENT RESOLUTION & DISPATCH LOGIC
    # =========================================================================

    async def notify(
        self,
        level: str, # "CRITICAL", "WARNING", "INFO"
        message_html: str,
        user_id: Optional[int] = None,
        strategy_id: Optional[int] = None,
        client_id: Optional[str] = None
    ):
        """
        Dispatches notification adhering to user preferences and admin multi-user filtering.
        1. Target user (if identified) receives the notification if enabled & subscribed to level.
        2. Admins receive all notifications across users/strategies by default, UNLESS
           the target user is in the admin's `admin_muted_user_ids` list.
        """
        level_upper = level.upper()

        db = SessionLocal()
        try:
            # 1. Resolve user_id if not explicitly provided
            if not user_id:
                if client_id:
                    acc = db.query(Account).filter(Account.client_id == client_id).first()
                    if acc and acc.user_id:
                        user_id = acc.user_id
                if not user_id and strategy_id:
                    strat = db.query(StrategyInstance).filter(StrategyInstance.id == strategy_id).first()
                    if strat and strat.user_id:
                        user_id = strat.user_id

            system_bot_token = self.get_system_default_bot_token(db)

            # We will collect destinations: { (bot_token, chat_id): user_obj } to prevent duplicate messages
            destinations: Dict[str, Dict[str, Any]] = {}

            # A. Check Target User
            if user_id:
                user_setting = db.query(TelegramSetting).filter(TelegramSetting.user_id == user_id).first()
                if user_setting and user_setting.enabled and user_setting.chat_id:
                    # Check level preference
                    should_send = False
                    if level_upper == "CRITICAL" and user_setting.notify_critical:
                        should_send = True
                    elif level_upper == "WARNING" and user_setting.notify_warning:
                        should_send = True
                    elif level_upper == "INFO" and user_setting.notify_info:
                        should_send = True

                    if should_send:
                        token = user_setting.bot_token.strip() if user_setting.bot_token else system_bot_token
                        if token:
                            destinations[user_setting.chat_id.strip()] = {
                                "bot_token": token,
                                "chat_id": user_setting.chat_id.strip(),
                                "user_id": user_id
                            }

            # B. Check Admins
            admin_users = db.query(User).filter(User.role == "ADMIN", User.is_active == True).all()
            for admin in admin_users:
                admin_setting = db.query(TelegramSetting).filter(TelegramSetting.user_id == admin.id).first()
                if not admin_setting or not admin_setting.enabled or not admin_setting.chat_id:
                    continue

                # Check level preference for admin
                should_send_admin = False
                if level_upper == "CRITICAL" and admin_setting.notify_critical:
                    should_send_admin = True
                elif level_upper == "WARNING" and admin_setting.notify_warning:
                    should_send_admin = True
                elif level_upper == "INFO" and admin_setting.notify_info:
                    should_send_admin = True

                if not should_send_admin:
                    continue

                # Admin multi-user filtering:
                # If target user is muted by this admin, skip this admin!
                if user_id:
                    muted_list = admin_setting.admin_muted_user_ids or []
                    if user_id in muted_list:
                        # Admin explicitly muted notifications from this user
                        continue

                token = admin_setting.bot_token.strip() if admin_setting.bot_token else system_bot_token
                if token:
                    destinations[admin_setting.chat_id.strip()] = {
                        "bot_token": token,
                        "chat_id": admin_setting.chat_id.strip(),
                        "user_id": admin.id
                    }

            # Dispatch concurrently
            tasks = []
            for dest in destinations.values():
                tasks.append(self.send_raw_message(dest["bot_token"], dest["chat_id"], message_html))

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            logger.error(f"[TelegramNotifier] Error in notify: {e}")
        finally:
            db.close()

    # =========================================================================
    # HIGH-LEVEL DISPATCH HELPERS
    # =========================================================================

    async def notify_critical(
        self,
        title: str,
        details: str,
        component: str = "System Engine",
        entity: str = "",
        user_id: Optional[int] = None,
        strategy_id: Optional[int] = None,
        client_id: Optional[str] = None
    ):
        msg = self.format_critical(title, details, component=component, entity=entity)
        await self.notify("CRITICAL", msg, user_id=user_id, strategy_id=strategy_id, client_id=client_id)

    async def notify_warning(
        self,
        title: str,
        account_info: str = "",
        strategy_name: str = "",
        scheduled_time: str = "",
        action_required: str = "",
        user_id: Optional[int] = None,
        strategy_id: Optional[int] = None,
        client_id: Optional[str] = None
    ):
        msg = self.format_warning(title, account_info, strategy_name, scheduled_time, action_required)
        await self.notify("WARNING", msg, user_id=user_id, strategy_id=strategy_id, client_id=client_id)

    async def notify_order(
        self,
        status: str,
        side: str,
        quantity: int,
        symbol: str,
        price: float,
        client_id: str,
        strategy_name: str = "",
        mode: str = "PAPER",
        rejection_reason: str = "",
        user_id: Optional[int] = None,
        strategy_id: Optional[int] = None
    ):
        # Rejection is treated as Critical if rejected, otherwise Info
        if status.upper() in ["REJECTED", "CANCELLED"]:
            msg = self.format_critical(
                title=f"Order {status.upper()} for {symbol}",
                details=rejection_reason or "Broker rejected order execution.",
                component="Order Router",
                entity=f"{symbol} ({client_id})"
            )
            await self.notify("CRITICAL", msg, user_id=user_id, strategy_id=strategy_id, client_id=client_id)
        else:
            msg = self.format_info_order(status, side, quantity, symbol, price, client_id, strategy_name, mode, rejection_reason)
            await self.notify("INFO", msg, user_id=user_id, strategy_id=strategy_id, client_id=client_id)

    async def notify_trade_cycle(
        self,
        strategy_name: str,
        client_id: str,
        symbol: str,
        realized_pnl: float,
        reason: str = "EXIT_TIME",
        mode: str = "PAPER",
        user_id: Optional[int] = None,
        strategy_id: Optional[int] = None
    ):
        msg = self.format_info_trade_cycle(strategy_name, client_id, symbol, realized_pnl, reason, mode)
        await self.notify("INFO", msg, user_id=user_id, strategy_id=strategy_id, client_id=client_id)

    # =========================================================================
    # SCHEDULED WARNING CHECKS (1 Hour Pre-Run & Pre-Market)
    # =========================================================================

    def _is_account_token_valid_today(self, acc: Account, db=None) -> bool:
        """
        Determines whether the account has a valid active session token for today.
        - Checks today's ISO date ('YYYY-MM-DD') and compact date ('YYYYMMDD') against token_date.
        - Checks if last_login_at was today as a secondary fallback.
        - For non-XTS brokers that don't enforce daily tokens, respects is_logged_in.
        - Automatically updates acc.is_logged_in = True in DB if token is valid today.
        """
        if not acc:
            return False

        creds = acc.credentials_json or {}
        token = str(creds.get("token") or "").strip()
        token_date = str(creds.get("token_date") or "").strip()

        today_iso = datetime.date.today().isoformat()
        today_compact = datetime.date.today().strftime("%Y%m%d")

        # 1. Primary check: token exists and token_date matches today
        has_token_today = bool(token) and (token_date == today_iso or token_date == today_compact)

        # 2. Secondary check: token exists and last_login_at is today
        if not has_token_today and token and acc.last_login_at:
            try:
                if acc.last_login_at.date() == datetime.date.today():
                    has_token_today = True
            except Exception:
                pass

        # 3. Non-XTS brokers that may use static API credentials
        broker_upper = str(acc.broker or "").upper()
        if broker_upper != "XTS":
            if has_token_today or (bool(acc.is_logged_in) and bool(creds)):
                has_token_today = True

        # Sync database boolean flag if token is active today
        if has_token_today and not acc.is_logged_in and db:
            try:
                acc.is_logged_in = True
                db.commit()
            except Exception:
                pass

        return has_token_today

    def _check_upcoming_strategies_sync(self):
        """
        Synchronous check for strategies starting within 1 hour.
        Warns if mapped broker accounts are not logged in or have missing tokens.
        Skips:
        - Paper trading strategies (inst.paper_mode == True)
        - Stopped strategies (inst.status == 'STOPPED')
        - Paper trading accounts (acc.paper_mode == True)
        - Inactive accounts (acc.is_active == False)
        - Accounts that already have a valid session token generated for today.
        """
        now = datetime.datetime.now()
        today_date = now.strftime("%Y%m%d")

        db = SessionLocal()
        warnings_to_dispatch = []
        try:
            instances = db.query(StrategyInstance).all()
            for inst in instances:
                if not inst.start_time:
                    continue

                # Skip paper trading strategies
                if bool(getattr(inst, "paper_mode", False)):
                    continue

                # Skip stopped strategies
                strat_status = str(inst.status or "").upper()
                if strat_status == "STOPPED":
                    continue

                # Parse scheduled start time for today
                try:
                    parts = [int(p) for p in inst.start_time.split(":")]
                    strat_start_dt = now.replace(hour=parts[0], minute=parts[1], second=parts[2] if len(parts) > 2 else 0, microsecond=0)
                except Exception:
                    continue

                diff_seconds = (strat_start_dt - now).total_seconds()
                
                # Check if within 60 minutes before start time (between 0 and 3600 seconds)
                if 0 <= diff_seconds <= 3600:
                    # Check mapped accounts
                    client_ids = inst.client_ids or []
                    for cid in client_ids:
                        warn_key = f"{today_date}_{inst.id}_{cid}"
                        if warn_key in self._warned_strategies_today:
                            continue # Already warned today for this strategy and account

                        acc = db.query(Account).filter(Account.client_id == cid).first()
                        if not acc:
                            continue

                        # Skip paper trading accounts
                        if bool(acc.paper_mode):
                            continue

                        # Skip paused/inactive accounts
                        if not bool(acc.is_active):
                            continue

                        # Check if today's broker token is already generated / active
                        has_token = self._is_account_token_valid_today(acc, db=db)
                        if has_token:
                            continue # Token already generated today! No alert needed.

                        # Live account missing today's token
                        mins_left = max(1, int(diff_seconds // 60))
                        warnings_to_dispatch.append({
                            "title": f"Broker Token Required in ~{mins_left} mins",
                            "account_info": f"{acc.client_id} ({acc.broker})",
                            "strategy_name": inst.instance_name_user or inst.instance_name_admin,
                            "scheduled_time": inst.start_time,
                            "action_required": f"Live strategy starts in {mins_left} min(s). Please login to {acc.broker} to generate valid token.",
                            "user_id": inst.user_id or acc.user_id,
                            "strategy_id": inst.id,
                            "client_id": acc.client_id
                        })
                        self._warned_strategies_today.add(warn_key)

        except Exception as e:
            logger.error(f"[TelegramNotifier] Error in strategy pre-run check: {e}")
        finally:
            db.close()

        return warnings_to_dispatch

    def _check_premarket_sync(self):
        """
        Pre-market morning check (runs between 08:30 and 09:15 AM).
        Warns for any active LIVE broker account missing today's token.
        """
        now = datetime.datetime.now()
        today_date = now.strftime("%Y%m%d")

        # Only check between 08:30:00 and 09:14:59
        if not (datetime.time(8, 30) <= now.time() < datetime.time(9, 15)):
            return []

        if self._premarket_warned_today == today_date:
            return [] # Already warned for today

        db = SessionLocal()
        warnings = []
        try:
            accounts = db.query(Account).filter(Account.is_active == True).all()
            for acc in accounts:
                # Skip paper trading accounts
                if bool(acc.paper_mode):
                    continue

                # Check if today's broker token is already generated / active
                has_token = self._is_account_token_valid_today(acc, db=db)
                if has_token:
                    continue # Token already active! No alert needed.

                warnings.append({
                    "title": "Pre-Market Broker Login Required",
                    "account_info": f"{acc.client_id} ({acc.broker})",
                    "strategy_name": "Pre-Market Session Readiness",
                    "scheduled_time": "09:15:00",
                    "action_required": f"Market opens at 09:15. Please login to {acc.broker} and ensure token is active.",
                    "user_id": acc.user_id,
                    "client_id": acc.client_id
                })

            # Mark checked for today so we don't repeat queries every 60s
            self._premarket_warned_today = today_date
        except Exception as e:
            logger.error(f"[TelegramNotifier] Error in premarket check: {e}")
        finally:
            db.close()

        return warnings

    async def _scheduler_loop(self):
        """Background loop that periodically executes warning checks every 60 seconds"""
        logger.info("[TelegramNotifier] Background 1-hour pre-run & pre-market warning scheduler started.")
        while self._is_running:
            try:
                # 1. Check upcoming strategies (1 hour before run)
                strat_warnings = await asyncio.to_thread(self._check_upcoming_strategies_sync)
                for w in strat_warnings:
                    await self.notify_warning(
                        title=w["title"],
                        account_info=w.get("account_info", ""),
                        strategy_name=w.get("strategy_name", ""),
                        scheduled_time=w.get("scheduled_time", ""),
                        action_required=w.get("action_required", ""),
                        user_id=w.get("user_id"),
                        strategy_id=w.get("strategy_id"),
                        client_id=w.get("client_id")
                    )

                # 2. Check premarket morning accounts
                premarket_warnings = await asyncio.to_thread(self._check_premarket_sync)
                for w in premarket_warnings:
                    await self.notify_warning(
                        title=w["title"],
                        account_info=w.get("account_info", ""),
                        strategy_name=w.get("strategy_name", ""),
                        scheduled_time=w.get("scheduled_time", ""),
                        action_required=w.get("action_required", ""),
                        user_id=w.get("user_id"),
                        client_id=w.get("client_id")
                    )

            except Exception as e:
                logger.error(f"[TelegramNotifier] Scheduler error: {e}")

            await asyncio.sleep(60)

    def start_scheduler(self):
        """Starts the background scheduler loop"""
        if not self._is_running:
            self._is_running = True
            loop = asyncio.get_event_loop()
            self._scheduler_task = loop.create_task(self._scheduler_loop())

    def stop_scheduler(self):
        """Stops the background scheduler loop"""
        self._is_running = False
        if self._scheduler_task:
            self._scheduler_task.cancel()
            self._scheduler_task = None


# Singleton instance
telegram_notifier = TelegramNotifier()
