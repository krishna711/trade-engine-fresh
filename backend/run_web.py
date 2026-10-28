import os
import sys
from pathlib import Path
import uvicorn
from fastapi.staticfiles import StaticFiles

# Add workspace root to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import asyncio
from backend.config import API_HOST, API_PORT
from backend.database.connection import init_db
from backend.api.app import app
from backend.core.events import event_bus
from backend.feed_manager.manager import feed_manager

# Mount frontend directory for serving
FRONTEND_DIR = ROOT_DIR / "frontend"
FRONTEND_DIR.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

@app.on_event("startup")
async def on_startup():
    init_db()
    # Connect to live IPC event broadcast from Feed Manager
    asyncio.create_task(event_bus.start_ipc_client())
    # Cache live ticks received via IPC into feed_manager.ticks_cache for dashboard REST queries
    event_bus.subscribe("feed.tick", lambda tick: feed_manager.ticks_cache.update({tick["symbol"]: tick}))
    print("=" * 65)
    print(">>> [WEB DASHBOARD & REST API] SERVER ACTIVE <<<")
    print(f"[*] Dashboard URL: http://{API_HOST}:{API_PORT}")
    print("=" * 65)

if __name__ == "__main__":
    uvicorn.run("backend.run_web:app", host=API_HOST, port=API_PORT, reload=True, log_level="info")
