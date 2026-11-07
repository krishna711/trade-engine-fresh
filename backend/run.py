import asyncio
import os
import sys
from pathlib import Path
import uvicorn
from fastapi.staticfiles import StaticFiles

# Add root project directory to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from backend.config import API_HOST, API_PORT
from backend.database.connection import init_db

# Initialize database schema & seeds immediately before importing microservices
init_db()

from backend.feed_manager.manager import feed_manager
from backend.order_manager.manager import order_manager
from backend.strategy_manager.manager import strategy_supervisor
from backend.api.app import app
from backend.core.events import event_bus

# Mount frontend directory for easy serving
FRONTEND_DIR = ROOT_DIR / "frontend"
FRONTEND_DIR.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

async def startup_all_services():
    print("=" * 60)
    print(">>> INITIALIZING FRESH TRADE ENGINE <<<")
    print("=" * 60)
    # 1. Initialize SQLite Database with WAL mode & seeds
    init_db()
    print("[1/3] Database initialized (WAL mode active).")

    # 2. Start IPC Server & Feed Manager
    await event_bus.start_ipc_server()
    await feed_manager.start()
    print("[2/3] Feed Manager microservice started with multi-broker auto-failover.")

    # 3. Start Order Manager
    await order_manager.start()
    print("[3/3] Order Manager initialized with parallel multi-client routing.")

    # 4. Start Strategy Supervisor
    await strategy_supervisor.start()
    print("[+] Strategy Supervisor active (Process-isolated containers).")

    # 5. Start Telegram Notification Scheduler
    from backend.core.telegram_notifier import telegram_notifier
    telegram_notifier.start_scheduler()
    print("[+] Telegram Notifier active (Critical, Warning, and Info multi-tier dispatcher).")

    print(f"[*] Trade Engine Live Web Dashboard: http://{API_HOST}:{API_PORT}")
    print("=" * 60)

async def shutdown_all_services():
    print("[*] Shutting down Fresh Trade Engine...")
    from backend.core.telegram_notifier import telegram_notifier
    telegram_notifier.stop_scheduler()
    await strategy_supervisor.stop()
    await order_manager.stop()
    await feed_manager.stop()
    print("[*] All engine microservices stopped cleanly.")

@app.on_event("startup")
async def on_startup():
    await startup_all_services()

@app.on_event("shutdown")
async def on_shutdown():
    await shutdown_all_services()

if __name__ == "__main__":
    uvicorn.run("backend.run:app", host=API_HOST, port=API_PORT, reload=False, log_level="info")
