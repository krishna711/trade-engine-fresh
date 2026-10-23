import asyncio
import os
import sys
from pathlib import Path

# Add workspace root to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from backend.database.connection import init_db
from backend.feed_manager.manager import feed_manager
from backend.core.events import event_bus

async def main():
    init_db()
    print("=" * 65)
    print(">>> [TERMINAL 1] FEED MANAGER MICROSERVICE (LIVE XTS FEED) <<<")
    print("=" * 65)
    await event_bus.start_ipc_server()
    await feed_manager.start()
    print("[*] Feed Manager active. Press CTRL+C to stop.")
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        print("\n[*] Stopping Feed Manager and unsubscribing XTS instruments...")
        await feed_manager.stop()
        print("[+] Feed Manager stopped cleanly.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
