import asyncio
import os
import sys
from pathlib import Path

# Add workspace root to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from backend.database.connection import init_db
from backend.strategy_manager.manager import strategy_supervisor
from backend.core.events import event_bus

async def main():
    init_db()
    print("=" * 65)
    print(">>> [TERMINAL 2] STRATEGY SUPERVISOR (PROCESS CONTAINERS) <<<")
    print("=" * 65)
    asyncio.create_task(event_bus.start_ipc_client())
    await strategy_supervisor.start()
    print("[*] Strategy Supervisor active. Monitoring containers. Press CTRL+C to stop.")
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[*] Stopping Strategy Supervisor...")
        await strategy_supervisor.stop()
        print("[+] Strategy Supervisor stopped cleanly.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
