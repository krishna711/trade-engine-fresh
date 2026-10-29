import asyncio
import os
import sys
from pathlib import Path

# Add workspace root to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from backend.database.connection import init_db
init_db()

from backend.order_manager.manager import order_manager
from backend.core.events import event_bus

async def main():
    init_db()
    print("=" * 65)
    print(">>> [TERMINAL 3] BROKER ORDER MANAGER & RECONCILER <<<")
    print("=" * 65)
    asyncio.create_task(event_bus.start_ipc_client())
    await order_manager.start()
    print("[*] Order Manager active. Multi-client execution ready. Press CTRL+C to stop.")
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[*] Stopping Order Manager...")
        await order_manager.stop()
        print("[+] Order Manager stopped cleanly.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
