# Fresh Trade Engine: Production Deployment & Live Trading Guide
> **Repository**: [trade-engine-fresh](https://github.com/krishna711/trade-engine-fresh.git)  
> **Target Environment**: Linux VPS / Cloud VM (Ubuntu 22.04 LTS / 24.04 LTS) or Dedicated Trading Server  
> **Purpose**: Complete step-by-step instructions for deploying Fresh Trade Engine to a live server, configuring production service daemons, enabling secure public access with SSL/Nginx, and safely executing live broker orders.

---

## Table of Contents
1. [Architecture & Server Topology](#1-architecture--server-topology)
2. [Hardware & Network Recommendations](#2-hardware--network-recommendations)
3. [Step 1: Server Provisioning & Dependencies](#3-step-1-server-provisioning--dependencies)
4. [Step 2: NTP Time Synchronization (Critical for Indian Markets)](#4-step-2-ntp-time-synchronization-critical-for-indian-markets)
5. [Step 3: Repository Setup & Virtual Environment](#5-step-3-repository-setup--virtual-environment)
6. [Step 4: Live Broker Credentials & `.env` Setup](#6-step-4-live-broker-credentials--env-setup)
7. [Step 5: Production Daemon Setup (Systemd vs Tmux)](#7-step-5-production-daemon-setup-systemd-vs-tmux)
   - [Option A: Systemd Services (Recommended for 24/7 Autostart)](#option-a-systemd-services-recommended)
   - [Option B: Tmux Multi-Window Workspace (Interactive CLI)](#option-b-tmux-multi-window-workspace)
8. [Step 6: Nginx Reverse Proxy with HTTPS & WebSocket Proxying](#8-step-6-nginx-reverse-proxy-with-https--websocket-proxying)
9. [Step 7: Security Hardening & Firewall (UFW)](#9-step-7-security-hardening--firewall-ufw)
10. [Step 8: Transitioning from Paper Trading to Live Orders](#10-step-8-transitioning-from-paper-trading-to-live-orders)
11. [Step 9: First Live Order Verification Checklist](#11-step-9-first-live-order-verification-checklist)
12. [Step 10: Daily Operational Schedule & Pre-Market Routine (IST)](#12-step-10-daily-operational-schedule--pre-market-routine-ist)
13. [Troubleshooting & Maintenance](#13-troubleshooting--maintenance)

---

## 1. Architecture & Server Topology

Fresh Trade Engine operates locally on a single machine with zero network latency between components. The architecture consists of 4 distinct processes that communicate over an in-memory loopback TCP socket (`127.0.0.1:8765`) and SQLite in WAL mode:

```
[ Public Internet / Web Browser ]
             │
         (HTTPS 443 / WSS /ws/live)
             ▼
      [ Nginx Reverse Proxy ]
             │
        (HTTP 8000)
             ▼
┌──────────────────────────────────────────────────────────────────┐
│ Live Trading Host (127.0.0.1)                                    │
│                                                                  │
│  [Process 4: Web / API] ─── (Local TCP IPC: 8765) ──┐            │
│  [Process 2: Strategy Supervisor] ───────────────────┼──┐         │
│  [Process 3: Order Manager & Reconciler] ───────────┘  │         │
│                                                        ▼         │
│                                              [Process 1: Feed]   │
│                                                 (IPC Server)     │
│                                                        │         │
│                                                        ▼         │
│                                            [SQLite Database: WAL]│
│                                            data/trade_engine.db  │
└──────────────────────────────────────────────────────────────────┘
```

---

## 2. Hardware & Network Recommendations

| Specification | Minimum | Recommended Production |
| :--- | :--- | :--- |
| **OS** | Ubuntu 22.04 / 24.04 LTS or Oracle Linux 8 / 9 | Ubuntu 24.04 LTS / Oracle Linux 9 (x86_64) |
| **vCPU** | 2 Cores | 4 Cores (Dedicated CPU) |
| **RAM** | 4 GB | 8 GB |
| **Storage** | 30 GB SSD | 50 GB NVMe SSD |
| **Server Location** | India (Mumbai / Bangalore) | Oracle Cloud (OCI Mumbai `ap-mumbai-1` / Hyderabad `ap-hyderabad-1`), AWS `ap-south-1` (Mumbai), or DigitalOcean (BLR1) |
| **Network Latency** | < 30ms to NSE/BSE colocation | < 10ms to broker trading gateways |

---

## 3. Step 1: Server Provisioning & Dependencies

Connect to your server via SSH:
```bash
# Standard VPS / Dedicated Server (Ubuntu/Debian):
ssh root@YOUR_SERVER_IP

# Oracle Cloud Infrastructure (OCI) default admin user:
ssh opc@YOUR_SERVER_IP
# (or if using SSH key on OCI: ssh -i ~/.ssh/id_rsa opc@YOUR_SERVER_IP)
```

---

### Option A: Ubuntu (22.04 LTS / 24.04 LTS)

#### 1. Update Packages & Install System Tools:
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git curl ufw nginx certbot python3-certbot-nginx tmux sqlite3 chrony build-essential
```

#### 2. Create a Dedicated Non-Root User:
```bash
sudo adduser --gecos "" trader
sudo usermod -aG sudo trader
su - trader
```

---

### Option B: Oracle Linux (8 / 9) / Oracle Cloud Infrastructure (OCI)

#### 1. Update Packages & Install System Tools:
```bash
sudo dnf update -y

# Enable EPEL (Extra Packages for Enterprise Linux) for certbot and nginx tools
sudo dnf install -y oracle-epel-release-el$(rpm -E %{rhel}) || sudo dnf install -y epel-release

# Install Python 3, build tools, Nginx, SQLite, Chrony, Tmux, and Certbot
sudo dnf install -y python3 python3-pip git curl nginx certbot python3-certbot-nginx tmux sqlite chrony gcc make python3-devel tar
```

#### 2. Create a Dedicated Non-Root User:
```bash
sudo useradd -m -s /bin/bash trader
sudo passwd trader
# (Enter a secure password for the trader user)

# Add trader to wheel group (sudo equivalent on Oracle Linux / RHEL)
sudo usermod -aG wheel trader

# Switch to the trader user
su - trader
```

---

## 4. Step 2: NTP Time Synchronization (Critical for Indian Markets)

Stock and derivative exchanges enforce strict timestamp matching on order requests and TOTP generation. Even a 2-second clock drift can cause TOTP failures or order rejections.

### 1. Configure Chrony for Indian NTP Pool:
```bash
# Ubuntu:
sudo nano /etc/chrony/chrony.conf

# Oracle Linux:
sudo nano /etc/chrony.conf
```
Add Indian pool servers at the top:
```conf
server time.google.com iburst
server 0.in.pool.ntp.org iburst
server 1.in.pool.ntp.org iburst
server 2.in.pool.ntp.org iburst
```

### 2. Restart and Verify Time Sync:
```bash
# Ubuntu:
sudo systemctl restart chrony
sudo systemctl enable --now chrony

# Oracle Linux:
sudo systemctl restart chronyd
sudo systemctl enable --now chronyd

# Check sync status (both OS):
chronyc tracking
```
Ensure `Leap status` is **Normal** and `System time` offset is sub-millisecond.

---

## 5. Step 3: Repository Setup & Virtual Environment

Log in as user `trader` in the home directory (`/home/trader`):

```bash
cd /home/trader
git clone https://github.com/krishna711/trade-engine-fresh.git
cd trade-engine-fresh
```

### 1. Create and Activate Python Virtual Environment:
```bash
python3 -m venv venv
source venv/bin/activate
```

### 2. Install Project Dependencies:
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Verify Database Storage Directory:
```bash
mkdir -p data
chmod 755 data
```

---

## 6. Step 4: Live Broker Credentials & `.env` Setup

Copy the example configuration to `.env`:
```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Populate `.env` with your active live broker credentials:

```ini
# ===================================================================
# FRESH TRADE ENGINE - PRODUCTION CONFIGURATION
# ===================================================================

# 1. IIFL XTS Market Data Feed (Primary Feed)
XTS_FEED_URL=https://ttblaze.iifl.com/apibinarymarketdata
XTS_FEED_API_KEY=your_live_xts_app_key
XTS_FEED_API_SECRET=your_live_xts_secret_key

# 2. Shoonya (Finvasia) Hot Failover Feed & Order Execution
SHOONYA_USER_ID=your_client_id
SHOONYA_PASSWORD=your_password
SHOONYA_TWO_FA=your_totp_secret_key
SHOONYA_VENDOR_CODE=your_vendor_code
SHOONYA_API_SECRET=your_api_secret

# 3. Server Bind Settings (Localhost only - Nginx handles public access)
API_HOST=127.0.0.1
API_PORT=8000
```

> [!IMPORTANT]
> Never commit `.env` to Git. Keep file permissions restricted to `600` (`chmod 600 .env`) so only the `trader` user can read credentials.

---

## 7. Step 5: Production Daemon Setup (Systemd vs Tmux)

### Option A: Systemd Services (Recommended)
Systemd ensures that your trading engine:
- Automatically starts when the server boots.
- Automatically restarts within 5 seconds if a process crashes.
- Logs all output directly to `journalctl`.

Create the unified systemd unit:
```bash
sudo nano /etc/systemd/system/trade-engine.service
```

Paste the following configuration:
```ini
[Unit]
Description=Fresh Trade Engine - Unified Production Engine
After=network.target chrony.service

[Service]
Type=simple
User=trader
WorkingDirectory=/home/trader/trade-engine-fresh
Environment="PATH=/home/trader/trade-engine-fresh/venv/bin:/usr/local/bin:/usr/bin"
EnvironmentFile=/home/trader/trade-engine-fresh/.env
ExecStart=/home/trader/trade-engine-fresh/venv/bin/python backend/run.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable trade-engine
sudo systemctl start trade-engine
sudo systemctl status trade-engine
```

To tail live service logs:
```bash
journalctl -u trade-engine -f -n 100
```

---

### Option B: Tmux Multi-Window Workspace
If you prefer running the 4 microservices in separate interactive terminal panes (mirroring `start_all_separate_terminals.bat`), use `tmux`:

Create a start script:
```bash
nano start_tmux.sh
```

Paste:
```bash
#!/bin/bash
SESSION="trade_engine"
DIR="/home/trader/trade-engine-fresh"
VENV="$DIR/venv/bin/activate"

tmux kill-session -t $SESSION 2>/dev/null

# 1. Feed Manager
tmux new-session -d -s $SESSION -n "FeedManager"
tmux send-keys -t $SESSION:FeedManager "cd $DIR && source $VENV && python backend/run_feed.py" C-m

sleep 2

# 2. Strategy Supervisor
tmux new-window -t $SESSION -n "Strategies"
tmux send-keys -t $SESSION:Strategies "cd $DIR && source $VENV && python backend/run_strategies.py" C-m

# 3. Order Manager
tmux new-window -t $SESSION -n "Orders"
tmux send-keys -t $SESSION:Orders "cd $DIR && source $VENV && python backend/run_order_manager.py" C-m

# 4. Web & API
tmux new-window -t $SESSION -n "WebAPI"
tmux send-keys -t $SESSION:WebAPI "cd $DIR && source $VENV && python backend/run_web.py" C-m

echo "[+] Trade Engine started inside tmux session '$SESSION'!"
echo "[*] Run 'tmux attach -t $SESSION' to view."
```

Make it executable and run:
```bash
chmod +x start_tmux.sh
./start_tmux.sh
tmux attach -t trade_engine
```
*(Use `Ctrl+B`, then `n` to switch windows, or `Ctrl+B`, then `d` to detach).*

---

## 8. Step 6: Nginx Reverse Proxy with HTTPS & WebSocket Proxying

Never expose port 8000 directly to the internet. Use Nginx with SSL and HTTP Basic Authentication.

### 1. Configure Nginx Virtual Host:
```bash
# Ubuntu:
sudo nano /etc/nginx/sites-available/trade-engine

# Oracle Linux:
sudo nano /etc/nginx/conf.d/trade-engine.conf
```

Paste the configuration (replace `trade.yourdomain.com` with your domain or server IP):
```nginx
server {
    listen 80;
    server_name trade.yourdomain.com;

    # Protect trading terminal with HTTP Basic Auth
    auth_basic "Fresh Trade Engine Terminal";
    auth_basic_user_file /etc/nginx/.htpasswd;

    # Proxy REST API and Web Dashboard
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Proxy WebSocket live market ticks and order updates
    location /ws/live {
        proxy_pass http://127.0.0.1:8000/ws/live;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }
}
```

### 2. Create Password for Terminal Access:
```bash
# Ubuntu:
sudo apt install -y apache2-utils

# Oracle Linux:
sudo dnf install -y httpd-tools

# Create htpasswd admin file (both OS):
sudo htpasswd -c /etc/nginx/.htpasswd admin
# (Enter a strong password when prompted)
```

### 3. Enable Nginx Site & Test:
```bash
# Ubuntu:
sudo ln -s /etc/nginx/sites-available/trade-engine /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx

# Oracle Linux:
# (SELinux permission to allow Nginx to reverse-proxy to localhost 8000):
sudo setsebool -P httpd_can_network_connect 1 2>/dev/null || true
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
```

### 4. Enable Free HTTPS via Let's Encrypt Certbot:
```bash
sudo certbot --nginx -d trade.yourdomain.com
```
Follow prompts to enable automatic HTTPS redirect.

---

## 9. Step 7: Security Hardening & Firewall

Lock down all ports except SSH (22), HTTP (80), and HTTPS (443):

### Option A: Ubuntu (UFW Firewall)
```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status verbose
```

### Option B: Oracle Linux (Firewalld & Oracle Cloud OCI)
```bash
# 1. Configure OS-level firewalld:
sudo systemctl enable --now firewalld
sudo firewall-cmd --permanent --add-service=http
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --permanent --add-port=22/tcp
sudo firewall-cmd --reload
sudo firewall-cmd --list-all
```

> [!IMPORTANT]
> **Oracle Cloud Infrastructure (OCI) VCN Security List Setup**:  
> In the Oracle Cloud Web Console, ensure your VM's Virtual Cloud Network (VCN) allows ingress traffic:  
> 1. Go to **Networking** &rarr; **Virtual Cloud Networks** &rarr; Click your VCN &rarr; **Security Lists** &rarr; **Default Security List**.  
> 2. Click **Add Ingress Rules**:  
>    - **Source CIDR**: `0.0.0.0/0`  
>    - **IP Protocol**: `TCP`  
>    - **Destination Port Range**: `80, 443`  
> 3. Save the rule. Without this OCI console rule, external web traffic will be blocked at the OCI hypervisor gateway.

Verify that internal engine ports (`8000`, `8765`) are **NOT** open to the public. They are only accessible internally via `127.0.0.1`.

---

## 10. Step 8: Transitioning from Paper Trading to Live Orders

Fresh Trade Engine ships with accounts in **Paper Trading Mode** by default (`paper_mode = True`). In this mode, orders fill virtually against live market prices.

To switch an account to **LIVE Order Execution**:

### Method 1: Via the Web Terminal Dashboard
1. Open your browser: `https://trade.yourdomain.com`.
2. Locate the broker account card in the **Connected Broker Accounts** section.
3. Click the **Paper / Live** toggle switch on the card.
4. When prompted, confirm switching to **LIVE**. The badge will change from yellow `PAPER` to green `LIVE`.

### Method 2: Via Direct Database Query
```bash
sqlite3 data/trade_engine.db "UPDATE accounts SET paper_mode = 0 WHERE client_id = 'YOUR_CLIENT_ID';"
```
Verify the change:
```bash
sqlite3 data/trade_engine.db "SELECT client_id, broker, paper_mode, is_active FROM accounts;"
```

---

## 11. Step 9: First Live Order Verification Checklist

> [!CAUTION]
> Always execute this verification checklist during live market hours (09:15 AM - 03:30 PM IST) before allocating real capital to automated strategies.

1. **Check Live Feed Health**:
   - Open `/status` on the web dashboard.
   - Verify that **XTS** or **Shoonya** is connected and `Packets Received` is continuously counting up.
   - Run the **6-Segment Diagnostic Test** to confirm NSE, NFO, BSE, BSEFO, MCX, and MCXFO tick delivery.

2. **Run a 1-Quantity Test Order**:
   - Select a low-value, highly liquid instrument (e.g. 1 share of `NSE:IDEA` or `NSE:TATASTEEL`).
   - Place a `BUY MARKET` order for 1 qty.
   - Open your broker's official mobile app or trading terminal (e.g., IIFL Trader, Shoonya Web, Zerodha Kite).
   - Verify that the order appears in your broker's official orderbook as `COMPLETE`.
   - Verify that the fill price in the Fresh Trade Engine matches your broker's contract note.

3. **Verify Position Mark-to-Market**:
   - Check the **Client Card** side drawer on the dashboard.
   - Confirm the open position shows correct LTP and fluctuating unrealized PnL.

4. **Verify Exit / Square-Off**:
   - Click the **Square-Off** button for that position on the dashboard.
   - Verify that the exit order executes immediately, the position closes, and realized PnL is locked in.

---

## 12. Step 10: Daily Operational Schedule & Pre-Market Routine (IST)

| Time (IST) | Action | Instructions |
| :--- | :--- | :--- |
| **08:45 AM** | **Pre-Market Health Check** | Verify system is running: `systemctl status trade-engine`. Check disk space: `df -h`. |
| **09:00 AM** | **Feed & Token Check** | Check web dashboard. Confirm primary feed connects to IIFL XTS. Confirm auto-rolled commodity futures expiries. |
| **09:15 AM** | **Market Open** | Monitor initial ticks. Start required strategy instances (`RUNNING`). |
| **09:15 - 15:30** | **Live Monitoring** | Keep the dashboard open. Monitor sticky error banner for any broker API rejections. |
| **15:15 PM** | **Intraday Square-Off Alert** | Review intraday MIS positions. Engine auto-squares positions according to strategy rules. |
| **15:30 PM** | **Equity/F&O Market Close** | All NSE/BSE intraday positions should be closed. Realized PnL is finalized. |
| **23:30 / 23:55** | **MCX Commodity Close** | Crude Oil, Natural Gas, Gold, Silver trading concludes. Daily session complete. |

---

## 13. Troubleshooting & Maintenance

### 1. View Service Logs
```bash
# View last 100 lines and follow live logs:
journalctl -u trade-engine -f -n 100

# View only errors:
journalctl -u trade-engine -p err -n 50
```

### 2. Restarting Services
```bash
# Graceful restart:
sudo systemctl restart trade-engine

# Hard stop:
sudo systemctl stop trade-engine
```

### 3. Backing Up the SQLite Database
SQLite in WAL mode should be backed up using the SQLite online backup API to prevent copying torn pages:
```bash
sqlite3 /home/trader/trade-engine-fresh/data/trade_engine.db ".backup '/home/trader/trade_engine_backup_$(date +%Y%m%d_%H%M%S).db'"
```

### 4. Updating Code from GitHub
```bash
cd /home/trader/trade-engine-fresh
git pull origin main
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart trade-engine
```

---

## 🏁 Summary
With **Systemd**, **Chrony time sync**, **Nginx with SSL**, and **UFW firewall**, Fresh Trade Engine runs as a self-healing, low-latency, fault-proof algorithmic trading station capable of 24/7 autonomous operation.
