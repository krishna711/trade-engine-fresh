# Fresh Trade Engine: Deep Technical & Operational System Handbook
> **Project**: `trande-engine-fresh`  
> **Target Audience**: AI Agents, Systems Architects, and Algorithmic Trading Developers  
> **Mirror / Entry Point**: [AGENTS.md](./AGENTS.md)

---

## 1. System Philosophy & Non-Negotiable Operational Constraints

1. **Local IPC Over Network APIs**:
   - The engine is designed to execute on a single physical host or dedicated high-frequency trading VPS.
   - Microservices MUST NOT communicate via public HTTP endpoints. They MUST use `LocalEventBus` ([`backend/core/events.py`](./backend/core/events.py)) either in-process or via local loopback TCP `127.0.0.1:8765`.
   - SQLite with WAL mode (`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`) is the single source of truth for accounts, orders, positions, and strategy configurations.
2. **Crash Resilience Through Process Isolation**:
   - Strategies must **never** be executed inside threads of the main engine process.
   - All strategy instances must run in distinct OS process containers via `multiprocessing.Process` ([`backend/strategy_manager/worker.py`](./backend/strategy_manager/worker.py)).
   - A segmentation fault, unhandled python exception, or memory spike inside a strategy container will only terminate that specific container. The supervisor will catch it, log an `ERROR` event in `system_logs`, and trigger the sticky prompt error banner on the frontend.
3. **Failover Transparency**:
   - The Market Data Feed Manager operates on a strict priority list: `["XTS", "SHOONYA", "FYERS"]`.
   - Failover MUST be zero-touch: when the primary broker's WebSocket drops, the standby broker receives the subscription list immediately.
4. **Universal Symbol Canonicalization**:
   - No strategy code may reference raw broker tokens (e.g. `26000` or `NSE_EQ|INE002A01018`).
   - Strategies must only specify canonical names: `NSE:NIFTY`, `NSE:BANKNIFTY`, `BSE:SENSEX`, `MCX:CRUDEOIL`, `NFO:BANKNIFTY52000CE`.
   - The `UniversalSymbolMapper` maps these canonical names to the appropriate exchange tokens for order placement and market data feeds.

---

## 2. Microservice Process Lifecycle

### Unified Mode (`python backend/run.py`):
- Single entry point launching `uvicorn` on `127.0.0.1:8000`.
- On startup event:
  1. `init_db()`: Initializes SQLite database, applies WAL pragmas, and seeds accounts, templates, and symbol mappings.
  2. `event_bus.start_ipc_server()`: Starts TCP server on port `8765`.
  3. `feed_manager.start()`: Connects XTS, Shoonya, and Fyers, activates primary broker, subscribes default instruments, and launches heartbeat monitor.
  4. `order_manager.start()`: Subscribes to `feed.tick` for real-time mark-to-market position updates.
  5. `strategy_supervisor.start()`: Subscribes to `feed.tick`, starts worker polling queues, and auto-spawns workers for strategies saved as `RUNNING` in the database.
- On shutdown event:
  - Cleanly stops Strategy Supervisor, Order Manager, and Feed Manager, closing all open broker sockets.

### Multi-Terminal Microservices Mode (`start_all_separate_terminals.bat`):
- **Terminal 1 (`run_feed.py`)**:
  - Initializes database and runs the TCP IPC server (`127.0.0.1:8765`).
  - Broadcasts all market data ticks and feed status messages.
- **Terminal 2 (`run_strategies.py`)**:
  - Connects as an IPC client to Terminal 1.
  - Receives ticks and routes them into strategy worker process queues.
- **Terminal 3 (`run_order_manager.py`)**:
  - Connects as an IPC client to Terminal 1.
  - Updates position mark-to-market values and handles reconciliation.
- **Terminal 4 (`run_web.py`)**:
  - Connects as an IPC client to Terminal 1.
  - Serves static frontend files and exposes FastAPI REST endpoints and WebSocket (`/ws/live`).

---

## 3. Detailed Data Models & Invariants

All tables are defined in [`backend/database/models.py`](./backend/database/models.py):

### 1. `accounts`
| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | Integer | PK, Autoincrement | Internal account ID |
| `broker` | String(50) | Not Null | `XTS`, `KOTAKNEO`, `SHOONYA`, `ZERODHA`, `DHAN`, `FYERS` |
| `client_id` | String(100) | Unique, Not Null | Broker user / client code (e.g. `SH10023`) |
| `client_name` | String(150) | Not Null | Descriptive display name |
| `paper_mode` | Boolean | Default True | `True` for simulated paper orders; `False` for live broker API |
| `is_active` | Boolean | Default True | Account activation toggle |
| `is_logged_in` | Boolean | Default True | Session status |
| `credentials_json` | JSON | Default `{}` | Encrypted / masked API keys, secret, TOTP |
| `margin_available`| Float | Default 500,000.0 | Available trading margin |
| `margin_used` | Float | Default 0.0 | Utilized margin |
| `total_balance` | Float | Default 500,000.0 | Total account equity |

### 2. `strategy_instances`
| Column | Type | Description |
| :--- | :--- | :--- |
| `id` | Integer (PK) | Strategy instance unique ID |
| `template_key` | String(100) | Foreign key to `strategy_templates.key` |
| `instance_name_admin`| String(150) | Technical name seen by Admin (e.g., `EMA 9-21 Alpha Client1`) |
| `instance_name_user` | String(150) | Clean name seen by Non-Admin user (e.g., `Trend Momentum Alpha`) |
| `client_ids` | JSON List | Array of mapped client accounts for parallel execution |
| `status` | String(20) | `RUNNING`, `STOPPED`, `ERROR` |
| `config` | JSON | Parameter overrides (timeframe, quantities, stop loss percentages) |
| `unrealized_pnl` | Float | Real-time mark-to-market PnL on active positions |
| `realized_pnl` | Float | Closed trades PnL |
| `total_pnl` | Float | Sum of unrealized and realized PnL |
| `legs_info` | JSON List | Price details per leg (`entry`, `exit`, `ltp`, `pnl`) |

### 3. `orders` & `trades`
- Every order generated by a strategy has a unique engine order ID: `ORD_YYYYMMDDHHMMSS_xxxxxx`.
- If the account is in paper mode (`is_paper = True`), orders fill immediately at market price without touching external broker APIs.
- When an order reaches `status = "FILLED"`, a record is automatically inserted into `trades`, and `positions` are updated mark-to-market.

---

## 4. Feed Manager In-Depth Mechanics

### Symphony XTS Compressed Binary Streaming ([`xts_feed.py`](./backend/feed_manager/brokers/xts_feed.py))
- Connects to IIFL Blaze market data endpoint: `https://ttblaze.iifl.com/apibinarymarketdata`.
- Uses raw zlib decompressor (`zlib.decompressobj(-15)`) to inflate binary frames sent from the socket.
- Handles Touchline events (event code `1501`), Market Depth (event code `1502`), and Open Interest (event code `1510`).
- To prevent Symphony binary feed deadlock, the adapter un-subscribes prior instrument lists before issuing new subscriptions.

### In-Memory OHLC Aggregator ([`manager.py:OHLCBuilder`](./backend/feed_manager/manager.py))
- Ingests streaming ticks and computes real-time candle buckets for `1m`, `5m`, `15m`, and `1h` intervals.
- The currently building bar is always accessible via `get_bars(symbol, resolution, count)` alongside completed historical bars.

---

## 5. Strategy Process Container Contract

Each strategy worker process is governed by [`strategy_process_entrypoint()`](./backend/strategy_manager/worker.py):

```python
def strategy_process_entrypoint(
    strategy_id: int,
    module_path: str,
    class_name: str,
    config: Dict[str, Any],
    client_ids: List[str],
    in_queue: multiprocessing.Queue,
    out_queue: multiprocessing.Queue
):
    ...
```

### Protocol Over `in_queue`:
- `{"type": "TICK", "data": {...}}`: Forwards real-time tick to `instance.on_tick(tick)`.
- `{"type": "CANDLE", "candle": {...}, "history": [...]}`: Forwards completed bar to `instance.on_candle(candle, history)`.
- `{"type": "STOP"}`: Instructs the worker to cleanly terminate its execution loop.

### Protocol Over `out_queue`:
- `{"type": "STATUS", "strategy_id": id, "status": "RUNNING" | "STOPPED", "message": "..."}`
- `{"type": "DECISION_LOG", "strategy_id": id, "data": {...}}`: Chronological decision step.
- `{"type": "ERROR", "strategy_id": id, "error": str(e), "traceback": str(tb)}`: Captures worker crashes without impacting supervisor.

---

## 6. Frontend Visual Architecture & Interaction Flow

1. **Role Switcher Logic**:
   - Stored in `localStorage.fresh_trade_role` (`ADMIN` or `USER`).
   - When set to `USER`:
     - Elements with class `.admin-only` are hidden.
     - Strategy table displays `name_user` rather than technical slugs.
     - Strategies Catalog menu item is hidden.
   - When set to `ADMIN`:
     - Full technical views, raw keys, slugs, and system diagnostics are displayed.
2. **Dynamic Price Flashes**:
   - In `ticker.js`, every incoming tick compares against previous LTP.
   - Prices flash green (`color: #00e676`) for upticks and red (`color: #ff3d00`) for downticks before smoothly transitioning back.
3. **Prompt Error Banner**:
   - Rendered at top of main workspace.
   - Displays unhandled system errors prominently.
   - Errors cannot be hidden or ignored until explicitly dismissed via user click.

---

## 7. Troubleshooting & Common Operational Errors

| Issue | Root Cause | Resolution |
| :--- | :--- | :--- |
| **`sqlite3.OperationalError: database is locked`** | WAL mode is inactive or connection pool has exhausted timeout. | Ensure connection pragma has `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`. |
| **XTS Ticker Prices Frozen on Restart** | Symphony session retained existing subscriptions without clearing. | XTS Feed Adapter's unsubscription loop automatically clears previous instruments before resubscribing. Verify XTS API credentials in `.env`. |
| **Multi-Terminal Mode: No Ticks on Dashboard** | Terminal 1 (Feed Manager) is not running or TCP IPC port `8765` is blocked. | Verify Terminal 1 is active. Ensure `run_web.py` connected to IPC client with `[EventBus IPC] Connected to IPC broadcast server`. |
| **Strategy Shows Status `ERROR`** | Syntax or runtime exception inside strategy container. | Check `system_logs` table under category `STRATEGY_DECISION` or click the strategy's "Audit Trail" modal in the UI to inspect the exception traceback. |
| **WebSocket Status Badge Shows `DISCONNECTED`** | FastAPI server down or port `8000` inaccessible. | Confirm `run.py` or `run_web.py` is running on `127.0.0.1:8000`. |
