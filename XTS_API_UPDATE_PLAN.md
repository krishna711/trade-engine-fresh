# XTS / Symphony API Update Audit & Migration Plan

**Created:** 2026-11-26  
**Status:** Planned (No code changes implemented yet)  

---

## 1. Timeline & Schedule

| Milestone | Date | Scope | Work Window |
| :--- | :--- | :--- | :--- |
| **Phase 1: Interactive TOTP Login** | **30 Nov 2026** | 5-step TOTP-based Interactive API login flow | **This Weekend** (market hours not required) |
| **Phase 2: API v1.2 Build & Mock** | **5 Dec 2026** | Binary Marketdata (`1501`/`1502`/`1510`) + Interactive Order API v1.2 updates + Mock participation | **Next Week (Monday onward)** |
| **Go-Live** | **7 Dec 2026** | Production rollout of new API response formats | — |

---

## 2. Current Codebase Audit vs. XTS Requirements

| Area | Current Status | Files Affected | Risk / Impact |
| :--- | :--- | :--- | :--- |
| **Interactive TOTP Login** | ❌ Not implemented | `backend/order_manager/brokers/broker_adapters.py` (`XTSOrderBroker.get_valid_token`) + Account credentials UI/API | Currently calls legacy `POST /interactive/user/session` with `appKey` + `secretKey` (`WEBAPI`). Needs 5-step TOTP flow from 30 Nov. |
| **Interactive Order API (`AppOrderID` & Dates)** | ⚠️ Partially incompatible | `backend/order_manager/brokers/broker_adapters.py`, `backend/order_manager/reconciler.py` | Reading `AppOrderID` already wraps with `str()`, **but `modify_order()` (L702) and `cancel_order()` (L732)** cast `appOrderID` to `int()`, which will fail when `AppOrderID` becomes a string. |
| **Binary Market Data (`1501` / `1502`)** | ❌ Will break on 7 Dec | `backend/feed_manager/xts_binary/MarketDepthRowInfo.py`, `TouchlineEvent.py`, `MarketDepthEvent.py`, `backend/feed_manager/brokers/xts_feed.py` | Currently unpacks **8-byte `double`** for prices and reads the removed 8-byte `TotalValueTraded`. On 7 Dec this will cause byte-alignment corruption and wrong LTPs. |

---

## 3. Phase 1 Plan (Weekend — Before 30 Nov 2026)

### Objective
Implement the 5-step **XTS/Blaze TOTP-based Interactive API Login** (`version: interactive_1.0.3`, `source: TWSAPI`).

### Steps to Implement
1. **Account Credentials & UI Support**:
   - Support storing XTS trading login `password` and `totp_secret` (Base32 secret key obtained from `https://ttblaze.iifl.com/#/app` → Profile → *Generate TOTP*) in the client account `credentials_json`.
   - Use `pyotp` to auto-generate the 6-digit TOTP pin during `get_valid_token()`, with optional manual TOTP fallback in UI if desired.
2. **5-Step Login Flow in `XTSOrderBroker.get_valid_token()`**:
   - **Step 1 — Hostlookup**:  
     `POST https://ttblaze.iifl.com/hostlookup`  
     Payload: `{"accesspassword": "2021HostLookUpAccess", "version": "interactive_1.0.3"}`  
     → Extract `result.connectionString` & `result.uniqueKey`.
   - **Step 2 — Validate User**:  
     `POST {connectionString}/1interactive/auth/validateUser`  
     Payload: `{"userID": user_id, "appKey": api_key, "source": "TWSAPI"}`  
     → Extract `result.imageIndex`.
   - **Step 3 — Validate Password**:  
     `POST {connectionString}/1interactive/auth/validatepassword`  
     Payload: `{"deviceType": "WEB", "imageIndex": image_index, "password": password, "userID": user_id, "source": "TWSAPI"}`
   - **Step 4 — Validate TOTP**:  
     `POST {connectionString}/1interactive/auth/verifytotp`  
     Payload: `{"source": "TWSAPI", "userID": user_id, "appKey": api_key, "password": password, "pin": totp_pin}`  
     → Extract `result.accessToken`.
   - **Step 5 — Interactive Session Login**:  
     `POST {connectionString}/1interactive/user/session`  
     Payload: `{"secretKey": api_secret, "appKey": api_key, "accessToken": access_token, "uniqueKey": unique_key}`  
     → Extract `result.token` and store `token_date`.
3. **Verification**:
   - Verify live XTS Interactive session login without market hours needed (Saturday/Sunday test against XTS auth endpoints).

---

## 4. Phase 2 Plan (Next Week Monday Onward — Before 5 Dec Mock / 7 Dec Go-Live)

### Objective
Upgrade **Binary Marketdata Deserializers (v1.2)** and **Interactive Order API / WebSocket Handlers (v1.2)** with a toggleable flag (e.g., `XTS_BINARY_V2`) so we can test cleanly during the **5 Dec mock** and switch on for **7 Dec live**.

### 4.1 Binary Marketdata Changes (`backend/feed_manager/xts_binary/` & `xts_feed.py`)
1. **PriceDivisor Mapping by `exchangeSegment`**:
   - `100`: `1` (NSECM), `11` (BSECM), `2` (NSEFO), `12` (BSEFO), `51` (MCXFO)
   - `10000000`: `3` (NSECD)
   - `10000`: `13` (BSECD), `4` (NSECO)
2. **`MarketDepthRowInfo.py`**:
   - Change `rowprice` from 8-byte `double` (`struct.unpack('d', ...)`) → **4-byte `Int32`** (`reader.read_int32()`), then divide by `price_divisors.get(exchangeSegment, 1)`.
3. **`TouchlineEvent.py` (`1501`) & `MarketDepthEvent.py` (`1502`)**:
   - Change the following fields from 8-byte `double` → **4-byte `Int32`** (`reader.read_int32()`) divided by `PriceDivisor`:
     - `LTP` (`LastTradedPrice`)
     - `averageTradedPrice`
     - `percentChange`
     - `open`
     - `high`
     - `low`
     - `close`
   - **Remove** reading `TotalValueTraded` (8-byte `double`) from both `1501` and `1502` (removed in v1.2; reading it will misalign all trailing fields and subsequent packets).
4. **`xts_feed.py`**:
   - Add `isGzipCompressed == 0` uncompressed packet handling alongside `isGzipCompressed == 1`.
   - Ensure `1502` Bid/Ask lookup reads `md_data.get("Bid", [])` / `md_data.get("Ask", [])` cleanly as well as `Touchline`.

### 4.2 Interactive Order API & WebSocket Changes (`broker_adapters.py` & `reconciler.py`)
1. **`AppOrderID` (`int` → `string`)**:
   - Update `modify_order()` (`broker_adapters.py:702`) and `cancel_order()` (`broker_adapters.py:732`) to send `str(broker_order_id)` instead of `int(broker_order_id)`.
   - Verify `EntryAppOrderID` / `ExitAppOrderID` / `AppOrderID` handling across `OrderBook`, `OrderHistory`, `TradeBook`.
2. **DateTime Format (`"%d-%m-%Y %H:%M:%S.%f"`)**:
   - Update any timestamp parsing for `ExchangeTransactTime`, `LastUpdateDateTime`, `OrderExpiryDate`, `OrderGeneratedDateTime`, `LastExecutionTransactTime` to accept `"%d-%m-%Y %H:%M:%S.%f"`.
3. **New & Removed Fields**:
   - Ignore removed `*API` suffixed fields (`OrderAverageTradedPriceAPI`, `OrderSideAPI`, `OrderGeneratedDateTimeAPI`, `ExchangeTransactTimeAPI`, `LastUpdateDateTimeAPI`, `OrderExpiryDateAPI`, `LastExecutionTransactTimeAPI`, `MessageSynchronizeUniqueKey`) and removed `TradingSymbol` in `trade` / `tradeConversion` socket events (resolve via `ExchangeInstrumentID` + `ExchangeSegment` if needed).
   - Optionally log/consume new fields (`ApiVendorCode`, `DashboardAppKey`, `SequenceId`, `OpenBuyQuantity`, `OpenSellQuantity`, `Quantity`, `BuyAmount`, `SellAmount`, `NetAmount`, `ExecutionReportIndex`).
4. **Mock Verification (5 Dec)**:
   - Enable `XTS_BINARY_V2=true` during the 5 Dec mock session to verify `1501`/`1502` live ticks, `AppOrderID` string placement/modify/cancel, and `OrderBook`/`TradeBook` reconciliation before 7 Dec market open.
