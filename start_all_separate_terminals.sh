#!/bin/bash
# ===================================================================
# Launch Fresh Trade Engine in Separate Terminals / Tmux Panes (Linux)
# ===================================================================

SESSION="trade_engine"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"

if ! command -v tmux &> /dev/null; then
    echo "[!] tmux is not installed. Run: sudo apt install -y tmux"
    exit 1
fi

# Kill old session if exists
tmux kill-session -t $SESSION 2>/dev/null

echo "==================================================================="
echo "  LAUNCHING FRESH TRADE ENGINE IN SEPARATE TMUX TERMINALS"
echo "==================================================================="

# 1. Window 1: Feed Manager (IPC Server)
tmux new-session -d -s $SESSION -n "FeedManager"
tmux send-keys -t $SESSION:FeedManager "cd '$BASE_DIR' && [ -f venv/bin/activate ] && source venv/bin/activate; python3 backend/run_feed.py" C-m

sleep 2

# 2. Window 2: Strategy Supervisor
tmux new-window -t $SESSION -n "Strategies"
tmux send-keys -t $SESSION:Strategies "cd '$BASE_DIR' && [ -f venv/bin/activate ] && source venv/bin/activate; python3 backend/run_strategies.py" C-m

# 3. Window 3: Order Manager
tmux new-window -t $SESSION -n "Orders"
tmux send-keys -t $SESSION:Orders "cd '$BASE_DIR' && [ -f venv/bin/activate ] && source venv/bin/activate; python3 backend/run_order_manager.py" C-m

# 4. Window 4: Web Dashboard & REST/WS API
tmux new-window -t $SESSION -n "WebAPI"
tmux send-keys -t $SESSION:WebAPI "cd '$BASE_DIR' && [ -f venv/bin/activate ] && source venv/bin/activate; python3 backend/run_web.py" C-m

echo "[+] All 4 microservices have been started in tmux session '$SESSION'!"
echo "[*] Open Dashboard at: http://127.0.0.1:8000"
echo "[*] Run 'tmux attach -t $SESSION' to inspect logs in real time."
echo "==================================================================="
