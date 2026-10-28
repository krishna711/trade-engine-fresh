@echo off
title Fresh Trade Engine - Multi-Terminal Microservices
echo ===================================================================
echo   LAUNCHING FRESH TRADE ENGINE IN SEPARATE TERMINALS
echo ===================================================================
echo.

start "Terminal 1: Feed Manager (Live IIFL XTS)" cmd /k "python backend/run_feed.py"
start "Terminal 2: Strategy Supervisor (Isolated Containers)" cmd /k "python backend/run_strategies.py"
start "Terminal 3: Order Manager (Multi-Broker Execution)" cmd /k "python backend/run_order_manager.py"
start "Terminal 4: Web Dashboard & REST/WS API" cmd /k "python backend/run_web.py"

echo [+] All 4 microservice terminals have been started!
echo [*] Open Dashboard at: http://127.0.0.1:8000
echo ===================================================================
timeout /t 5