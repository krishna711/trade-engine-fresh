# Architecture Comparison: Original vs. Proposed Trading System Architecture
> **Comparison Reference**: `ARCHITECTURE.md` (Original Architecture) vs. `TRADING_SYSTEM_ARCHITECTURE.md` (Proposed Future-State Architecture)

---

## 1. Executive Summary & Design Philosophy

| Architectural Dimension | Original Architecture (`ARCHITECTURE.md`) | Proposed Future Architecture (`TRADING_SYSTEM_ARCHITECTURE.md`) |
| :--- | :--- | :--- |
| **System Topology** | **Component Microservices**: Independent processes (Feed, Orders, Strategies, API) communicating over local TCP socket IPC (`127.0.0.1:8765`) + SQLite WAL. | **Modular Monolith**: In-process runtime components owned by the API/Gateway, avoiding premature IPC overhead unless scaling demands it. |
| **Data Access Boundary** | **Direct SQLite WAL**: Each module independently opens `SessionLocal()` and queries/modifies DB tables directly. | **Storage Facade (`STORE`)**: Centralized data access abstraction managing transactions, state caching, and thread safety. |
| **Market Data Persistence** | **In-Memory Streaming OHLC**: Bars (`1m`, `5m`, `15m`, `1h`) live in memory (up to 300 bars); lost on engine restart. | **Persistent `MarketBar` & Warmup**: Completed `1m` bars stored in SQLite; higher timeframes derived; automatic indicator lookback warmup. |
| **Order Generation Flow** | **Direct Order Signals**: Strategy code directly instructs Order Manager to place specific broker orders. | **Intent-Based Routing**: Strategy emits an **Order Intent**; Risk/Order Manager validates account limits, margins, and mode before dispatching. |
| **Position Ownership** | **Strategy-Centric**: Positions and PnL are primarily tracked per strategy instance leg. | **Account/Portfolio-Centric**: The Broker Account owns net positions and risk; strategies receive **Attribution** without competing positions. |
| **Paper vs. Live Mode** | **Account-Level Toggle**: Switched via client card in UI (`paper_mode: true/false`). | **Hierarchical Inheritance**: System Default → Portfolio Default → Strategy Override, with strict balance and order segregation. |
| **Fault Isolation** | **OS Process Containers (`multiprocessing.Process`)**: Mandatory container per strategy instance. | **In-Process Modular**: Started in-process, with process isolation reserved as a future scaling extension point. |

---

## 2. Detailed Breakdown of Advised Enhancements

### A. Market Data: Persistent `MarketBar` & Automatic Warmup
- **Current Limitation in `ARCHITECTURE.md`**:  
  Market data is streamed in-memory via `OHLCBuilder`. If the feed or supervisor restarts, indicator history (like EMA 9/21, Supertrend, ATR) is empty until new bars form in real time, causing strategies to sit idle or miss early-morning entry triggers.
- **Enhancement in `TRADING_SYSTEM_ARCHITECTURE.md`**:  
  1. **Canonical `1m` Storage**: Stores completed 1-minute bars in SQLite. All higher resolutions (`5m`, `15m`, `1h`) are derived from `1m` bars on the fly.
  2. **Historical Warmup**: On strategy startup, the engine queries recent historical bars from broker REST endpoints (or local SQLite) to immediately warm up indicator buffers before trading begins.
  3. **Failover Deduplication**: During feed failover (e.g. XTS → Shoonya), any duplicate bars are ignored, and missing intervals are backfilled seamlessly.

---

### B. Order Execution: Intent-Based Architecture vs. Direct Signals
- **Current Limitation in `ARCHITECTURE.md`**:  
  Strategies directly dictate order parameters (price, product, qty). When broker-specific RMS rules change (e.g., MCX forbidding `MIS` or requiring raw lot counts instead of multiplied sizes), the strategy code itself had to be patched.
- **Enhancement in `TRADING_SYSTEM_ARCHITECTURE.md`**:  
  Strategies submit **Order Intents** (e.g., *"Intend to open short ATM straddle on CRUDEOIL with 1 lot"*).  
  The **Order Manager** is solely responsible for:
  - Applying broker-specific rules (e.g. mapping `MIS` to `NRML` on MCX, resolving raw lots vs contract multiplier).
  - Checking account margin reservations before sending.
  - Selecting live vs paper execution dynamically.

---

### C. Risk & Position Management: Portfolio-Level Netting & Strategy Attribution
- **Current Limitation in `ARCHITECTURE.md`**:  
  Positions are tracked on a per-strategy leg basis. If Strategy A buys 1 lot of `CRUDEOILM` and Strategy B sells 1 lot of `CRUDEOILM` under the same client account, both strategies track their own independent legs, even though the broker account is flat (0 net position).
- **Enhancement in `TRADING_SYSTEM_ARCHITECTURE.md`**:  
  - The **Account Portfolio** is the single source of truth for net position, margins, and broker equity.
  - The Strategy layer maintains **Attribution** (tagging which strategy initiated the fill and calculating virtual PnL), preventing competing strategy orders from creating conflicting broker positions.

---

### D. Centralized Runtime & Storage Facade (`STORE`)
- **Current Limitation in `ARCHITECTURE.md`**:  
  FastAPI endpoints, background feed tasks, reconcilers, and strategy workers each create raw SQLite sessions (`SessionLocal()`). This requires manual session cleanup (`db.close()`, `db.rollback()`) and careful handling of SQLite `busy_timeout` during high-frequency writes.
- **Enhancement in `TRADING_SYSTEM_ARCHITECTURE.md`**:  
  Introduces a `StorageFacade` layer. Background workers and API endpoints interact with high-level domain methods (e.g., `store.get_account_balance()`, `store.record_trade_report()`, `store.save_bar()`), isolating raw SQL and WAL pragmas behind a single thread-safe interface.

---

## 3. Comparison of Core Components

| Dimension | `ARCHITECTURE.md` (Original) | `TRADING_SYSTEM_ARCHITECTURE.md` (Proposed) |
| :--- | :--- | :--- |
| **1. Strategy Container Model** | `multiprocessing.Process` OS containers | In-process async (initial phase) |
| **2. Candle Persistence** | In-memory ring buffer (300 bars) | SQLite DB-backed 1m bars |
| **3. Indicator Initialization** | Waits for live incoming ticks | Pre-market REST warmup fetch |
| **4. Strategy → Order Flow** | Direct order signal dispatch | Intent → Validation → Order |
| **5. Mode Selection** | Per-Account toggle in UI | System → Portfolio → Strategy hierarchy |
| **6. DB Access** | Distributed raw SQLAlchemy sessions | Centralized Storage Facade |
| **7. Multi-Strategy Conflicts** | Independent strategy legs | Net portfolio & attribution tags |

---

## 4. Antigravity Architectural Recommendations

While `TRADING_SYSTEM_ARCHITECTURE.md` introduces valuable design paradigms, adopting all of them blindly would undo critical strengths already achieved in your codebase:

### What to ADOPT from `TRADING_SYSTEM_ARCHITECTURE.md` (High Priority):
1. **Pre-Market Historical Warmup for Indicators**:
   - Integrate a REST warmup call (fetching 30-50 historical bars from broker API) when a strategy starts so EMAs, ATRs, and historical pivots are populated instantly.
2. **Persistence for Completed 1-Minute Bars (`MarketBar`)**:
   - Persisting completed `1m` bars into SQLite provides continuous data continuity across system restarts, strategy restarts, and offline backtesting.
3. **Storage Facade Refactoring**:
   - Centralize database operations behind a unified facade (`backend/database/store.py`). This eliminates scattered `SessionLocal()` / `db.commit()` calls across the codebase and avoids SQLite lock contention.
4. **Order Intent Pattern**:
   - Keep strategies focused purely on trading logic (e.g., indicator calculations, strike selection, target/SL definitions) while leaving order translation, slippage buffers, limit chase loops, and broker RMS rules (`MIS` vs `NRML`, lot count vs contract size) inside the Order Manager.

### What to KEEP from `ARCHITECTURE.md` (Do NOT Regress):
1. **Keep Process Container Isolation (`multiprocessing.Process`)**:
   - In live trading with Indian brokers, python strategy containers can hang on network sockets, suffer memory leaks, or encounter unhandled mathematical exceptions. Dedicated OS process containers (`worker.py`) guarantee that **a bug in one strategy will never crash the supervisor, the feed, or other client accounts**.
2. **Keep Sub-Second Ticks for Real-Time Stop Loss**:
   - For fast intraday straddles (like MCX CrudeOil Options and BankNifty 0DTE), stop-losses must be triggered on real-time streaming ticks (`feed.tick`), not delayed until a 1-minute candle closes. Keep direct tick routing to active strategy workers.
3. **Keep Dual Admin vs User Role Visibility**:
   - The UI distinction (Admin viewing raw broker tokens/slugs, User viewing clean cards/PnL) is clean and already loved by end-users. Maintain this presentation boundary.

---

## 5. Suggested Roadmap to Modernize to the New Architecture

```mermaid
flowchart LR
    A["Phase 1: DB & Facade"] --> B["Phase 2: MarketBar & Warmup"]
    B --> C["Phase 3: Order Intent Engine"]
    C --> D["Phase 4: Net Risk & Attribution"]

    style A fill:#1e293b,stroke:#3b82f6,color:#fff
    style B fill:#1e293b,stroke:#10b981,color:#fff
    style C fill:#1e293b,stroke:#f59e0b,color:#fff
    style D fill:#1e293b,stroke:#8b5cf6,color:#fff
```

1. **Step 1: Build the Storage Facade (`backend/database/store.py`)**  
   Encapsulate SQLite queries for accounts, orders, positions, and trade reports.
2. **Step 2: Add 1-Minute Candle Persistence & Warmup**  
   Save completed bars in a `market_bars` table. On strategy deployment, auto-fetch the previous 50 bars from the primary broker feed.
3. **Step 3: Formalize Order Intent Protocol**  
   Transition strategy workers from emitting `{"type": "ORDER_REQUEST"}` to `{"type": "ORDER_INTENT"}`, letting the Order Manager validate account risk and RMS translation centrally.
4. **Step 4: Portfolio Net Position Layer**  
   Maintain account-level net exposure while attaching strategy attribution tags to each execution report.
