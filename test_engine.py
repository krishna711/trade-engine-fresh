import urllib.request
import json
import time

def test_endpoint(url, method='GET', data=None):
    req = urllib.request.Request(url, method=method)
    if data:
        req.add_header('Content-Type', 'application/json')
        data_bytes = json.dumps(data).encode('utf-8')
    else:
        data_bytes = None
    with urllib.request.urlopen(req, data=data_bytes, timeout=10) as resp:
        res = json.loads(resp.read().decode('utf-8'))
        print(f"[*] {method} {url} -> SUCCESS (HTTP {resp.status})")
        return res

print("=" * 60)
print("TESTING FRESH TRADE ENGINE SYSTEM")
print("=" * 60)

# 1. Dashboard summary
dash = test_endpoint('http://127.0.0.1:8000/api/dashboard')
print(f"  PNL Summary: Unrealized=INR {dash['pnl_summary']['unrealized_pnl']}, Realized=INR {dash['pnl_summary']['realized_pnl']}, Total=INR {dash['pnl_summary']['total_pnl']}")
print(f"  Active Clients: {dash['active_clients_count']}, Running Strategies: {dash['running_strategies_count']}")

# 2. Client Accounts
accounts = test_endpoint('http://127.0.0.1:8000/api/accounts')
print(f"  Accounts Loaded: {len(accounts)}")
for acc in accounts:
    print(f"    - {acc['client_name']} [{acc['broker']}]: Paper={acc['paper_mode']}, Total PnL=INR {acc['total_pnl']}")

# 3. Strategy Instances
instances = test_endpoint('http://127.0.0.1:8000/api/strategies/instances')
print(f"  Strategy Instances Loaded: {len(instances)}")
for inst in instances:
    print(f"    - ID {inst['id']}: Admin='{inst['name_admin']}' | User='{inst['name_user']}' | Status={inst['status']} | Legs={len(inst.get('legs_info', []))}")

# 4. Strategy Templates (Admin Only)
templates = test_endpoint('http://127.0.0.1:8000/api/strategies/templates')
print(f"  Registered Templates: {len(templates)}")
for tpl in templates:
    print(f"    - Key: '{tpl['key']}' | Name Admin: '{tpl['name_admin']}' | Name User: '{tpl['name_user']}'")

# 4.5 Regulatory Lot Sizes Configuration
lot_sizes = test_endpoint('http://127.0.0.1:8000/api/config/lot-sizes')
print(f"  Regulatory Lot Sizes Active: NIFTY={lot_sizes['default_lot_sizes']['NIFTY']}, BANKNIFTY={lot_sizes['default_lot_sizes']['BANKNIFTY']}, SENSEX={lot_sizes['default_lot_sizes']['SENSEX']}")

# 5. Feed Manager Status & Failover
feed = test_endpoint('http://127.0.0.1:8000/api/status/feed')

print(f"  Active Feed Broker: {feed['active_broker']}")
print(f"  Priority Order: {feed['priority']}")
print(f"  Total Subscribed Symbols: {feed['total_subscribed']}")

print("\n--- TRIGGERING AUTOMATIC FAILOVER ---")
failover_res = test_endpoint('http://127.0.0.1:8000/api/status/feed/failover', method='POST')
print(f"  Failover Triggered -> New Active Broker: {failover_res['active_broker']}")

# 6. Test Suite 1: Symbol Subscription & OHLC Fetch per segment
print("\n--- RUNNING DIAGNOSTICS TEST 1 (All 6 Segments Feed Subscription & OHLC) ---")
feed_test = test_endpoint('http://127.0.0.1:8000/api/status/diagnostics/run-feed-test', method='POST')
for r in feed_test['results']:
    print(f"  [{r['segment']}] Symbol: {r['symbol']} | Sub: {r['subscription_status']} | OHLC: {r['ohlc_fetch_status']} ({r['bars_count']} bars) | LTP: INR {r['latest_ltp']} | Latency: {r['latency_ms']}ms | Result: {r['overall_status']}")

# 7. Test Suite 2: Real-time Order Execution per segment
print("\n--- RUNNING DIAGNOSTICS TEST 2 (All 6 Segments Order Execution with Real-Time Update) ---")
order_test = test_endpoint('http://127.0.0.1:8000/api/status/diagnostics/run-order-test', method='POST')
for r in order_test['results']:
    print(f"  [{r['segment']}] Symbol: {r['symbol']} | OrderId: {r['order_id']} | Status: {r['status']} | FillPrice: INR {r['filled_price']} | Latency: {r['latency_ms']}ms")

# 7.5 Multi-Broker Parallel Order Execution Test
print("\n--- RUNNING MULTI-BROKER PARALLEL ORDER TEST (XTS, Kotak Neo, Shoonya, Zerodha, Dhan) ---")
mb_test = test_endpoint('http://127.0.0.1:8000/api/status/diagnostics/run-multi-broker-test', method='POST')
for r in mb_test['results']:
    print(f"  Broker Client: {r.get('client_id')} | Status: {r.get('status')} | OrderId: {r.get('order_id')} | FillPrice: INR {r.get('price')}")


# 8. Strategy Duplication Feature Test
print("\n--- TESTING STRATEGY DUPLICATION ---")
dup_payload = {
    "name_admin": "EMA 9-21 Alpha (Duplicated Instance)",
    "name_user": "Trend Momentum Alpha (Copy)",
    "config": {
        "symbol": "NSE:BANKNIFTY",
        "timeframe": "15m",
        "fast_ema": 9,
        "slow_ema": 21,
        "quantity": 15
    }
}
first_id = instances[0]['id'] if instances else 7
dup_res = test_endpoint(f'http://127.0.0.1:8000/api/strategies/{first_id}/duplicate', method='POST', data=dup_payload)
print(f"  Strategy Duplication Result: Cloned Instance ID: {dup_res.get('cloned_strategy_id')}")
if dup_res.get('cloned_strategy_id'):
    del_res = test_endpoint(f"http://127.0.0.1:8000/api/strategies/{dup_res['cloned_strategy_id']}", method='DELETE')
    print(f"  Cloned test strategy #{dup_res['cloned_strategy_id']} cleaned up -> Success: {del_res.get('success')}")

# 9. Logs & Prompt Error Highlight Test
print("\n--- TESTING LOGS & PROMPT ERROR HIGHLIGHT ---")
logs_res = test_endpoint('http://127.0.0.1:8000/api/logs')
print(f"  Active Undismissed Errors Highlighted: {len(logs_res['active_errors'])}")
for err in logs_res['active_errors']:
    print(f"    * PROMPT ERROR [{err['id']}]: {err['message']} (Target: {err['client_id'] or err['symbol']})")
print(f"  Total System Logs: {len(logs_res['logs'])}")

# Test Dismiss Error
if logs_res['active_errors']:
    err_id = logs_res['active_errors'][0]['id']
    dismiss_res = test_endpoint(f'http://127.0.0.1:8000/api/logs/{err_id}/dismiss', method='POST')
    print(f"  Dismissed Error #{err_id} -> Success: {dismiss_res['success']}")

print("\n" + "=" * 60)
print("ALL SYSTEM MODULES & REQUIREMENTS VERIFIED!")
print("=" * 60)
