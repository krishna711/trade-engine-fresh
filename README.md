# Fresh Trade Engine ⚡

A high-performance, fault-proof algorithmic trading engine with a component-based microservice architecture running locally on a shared SQLite WAL database.

[![System Status](https://img.shields.io/badge/System_Status-Active-brightgreen.svg)](#)
[![Architecture](https://img.shields.io/badge/Architecture-Component_Microservices-blue.svg)](#)
[![Python Version](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](#)
[![Database](https://img.shields.io/badge/Database-SQLite_WAL-orange.svg)](#)
[![Brokers Supported](https://img.shields.io/badge/Brokers-XTS_%7C_Shoonya_%7C_Fyers_%7C_Kotak_%7C_Zerodha_%7C_Dhan-purple.svg)](#)

---

## 📖 Essential Documentation
- 🤖 **[AGENTS.md](./AGENTS.md)**: **Master AI Agent & Developer Handbook** — Comprehensive architectural reference, database schemas, local IPC specifications, multi-broker routing, isolated strategy process container mechanics, and implementation status roadmap.
- 🏗️ **[ARCHITECTURE.md](./ARCHITECTURE.md)**: Architectural diagrams, component breakdowns, and operational workflows.

---

## 🚀 Key Highlights

1. **Zero Network Latency**: Feed Manager, Order Manager, and Strategy Supervisor run on the same server, communicating locally via an in-memory/TCP socket event bus (`127.0.0.1:8765`) and shared SQLite database in WAL mode (`PRAGMA journal_mode=WAL`).
2. **Process Container Isolation**: Each strategy instance runs inside an isolated OS process (`multiprocessing.Process`). A bug, exception, or update in one strategy can **never** disturb another strategy or crash the engine.
3. **Hot Market Feed Failover**: Priority switchover (`XTS` > `SHOONYA` > `FYERS`). If the primary feed disconnects, the standby feed activates automatically and restores all subscriptions.
4. **Universal Symbol Canonicalization**: Clean symbols (`NSE:NIFTY`, `BSE:SENSEX`, `MCX:CRUDEOIL`, `NFO:...`) automatically mapped across all 6 brokers.
5. **MCX Commodity Expiry Auto-Roll**: Detects when commodity futures reach expiration and automatically rolls to the next active contract.
6. **Parallel Multi-Client Routing**: Single strategy signals are dispatched concurrently across all mapped accounts with zero head-of-line blocking.
7. **Paper vs. Live Execution**: Instant per-account mode switch with realistic fill and slippage simulation.
8. **Web Trading Terminal**: Modern dark/light interface with real-time flashing ticker, Admin vs. User role view (slug masking), client cards with side drawer, 6-segment diagnostic test suites, and 3-category logs with a sticky prompt error banner.

---

## 🏁 Quick Start

### 1. Unified Engine Mode (All-in-One)
To run the entire engine in a single terminal:
```cmd
start_unified_engine.bat
```
*Or manually:*
```bash
python backend/run.py
```
Open your web browser at **`http://127.0.0.1:8000`**.

### 2. Multi-Terminal Microservices Mode
To run each microservice in an independent terminal window:
```cmd
start_all_separate_terminals.bat
```
This spawns:
- **Terminal 1**: Market Data Feed Manager (`backend/run_feed.py`)
- **Terminal 2**: Strategy Process Supervisor (`backend/run_strategies.py`)
- **Terminal 3**: Broker Order Manager & Reconciler (`backend/run_order_manager.py`)
- **Terminal 4**: Web Terminal Server & REST/WS API (`backend/run_web.py`)

### 3. Run Automated System Test Suite
To verify end-to-end functionality, run:
```bash
python test_engine.py
```

---

## 🛠️ Supported Segments & Default Instruments
- **Segments**: `NSE`, `NFO`, `BSE`, `BSEFO`, `MCX`, `MCXFO`
- **Default Subscriptions**:
  - `NSE:NIFTY` (NIFTY 50)
  - `NSE:BANKNIFTY` (BANK NIFTY)
  - `BSE:SENSEX` (SENSEX)
  - `MCX:CRUDEOIL` (Current Month Future - Auto Rolled)
  - `MCX:NATURALGAS` (Current Month Future - Auto Rolled)
  - `MCX:GOLD` (Current Month Future - Auto Rolled)
  - `MCX:SILVER` (Current Month Future - Auto Rolled)

---

## 📂 Project Structure
```text
trande-engine-fresh/
├── backend/
│   ├── api/                  # FastAPI REST & WebSocket endpoints (/ws/live)
│   ├── core/                 # LocalEventBus (IPC) & UniversalSymbolMapper
│   ├── database/             # SQLite connection (WAL mode) & SQLAlchemy models
│   ├── feed_manager/         # XTS, Shoonya, Fyers feed adapters & failover
│   ├── order_manager/        # Multi-client order router & reconciler
│   └── strategy_manager/     # Process container supervisor & strategy algorithms
├── data/                     # SQLite database files (trade_engine.db)
├── frontend/                 # Web Terminal UI (HTML, CSS, JavaScript)
├── AGENTS.md                 # Master handbook for AI agents
├── ARCHITECTURE.md           # Architecture specifications
├── test_engine.py            # Integration test runner
├── start_unified_engine.bat  # Unified launcher
└── start_all_separate_terminals.bat # Multi-terminal launcher
```
