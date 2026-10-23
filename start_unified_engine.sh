#!/bin/bash
# ===================================================================
# Launch Fresh Trade Engine in Unified Mode (Linux)
# ===================================================================

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"

if [ -f "$BASE_DIR/venv/bin/activate" ]; then
    source "$BASE_DIR/venv/bin/activate"
fi

echo "==================================================================="
echo "  LAUNCHING FRESH TRADE ENGINE (UNIFIED MODE)"
echo "==================================================================="
echo ""
python3 backend/run.py
