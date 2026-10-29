# Fresh Trade Engine: CloudPanel Deployment Guide (Ubuntu)
> **Repository**: [trade-engine-fresh](https://github.com/krishna711/trade-engine-fresh.git)  
> **Target Environment**: Ubuntu 22.04 LTS / 24.04 LTS with **CloudPanel** (Control Panel)  
> **Purpose**: Complete guide to deploying Fresh Trade Engine on an existing CloudPanel server, utilizing CloudPanel's Reverse Proxy site engine, 1-Click Let's Encrypt SSL, Nginx WebSocket proxying (`/ws/live`), built-in Basic Auth security, and Systemd autostart daemons.

---

## Table of Contents
1. [Why CloudPanel for Fresh Trade Engine](#1-why-cloudpanel-for-fresh-trade-engine)
2. [Architecture with CloudPanel](#2-architecture-with-cloudpanel)
3. [Step 1: Add a Reverse Proxy Site in CloudPanel](#3-step-1-add-a-reverse-proxy-site-in-cloudpanel)
4. [Step 2: 1-Click Let's Encrypt SSL Certificate](#4-step-2-1-click-lets-encrypt-ssl-certificate)
5. [Step 3: Configure WebSocket Proxying in CloudPanel VHost (Critical)](#5-step-3-configure-websocket-proxying-in-cloudpanel-vhost-critical)
6. [Step 4: Enable CloudPanel Basic Auth Security](#6-step-4-enable-cloudpanel-basic-auth-security)
7. [Step 5: Server Provisioning & Repository Setup](#7-step-5-server-provisioning--repository-setup)
8. [Step 6: NTP Time Synchronization (Critical for Indian Markets)](#8-step-6-ntp-time-synchronization-critical-for-indian-markets)
9. [Step 7: Production Systemd Daemon Setup](#9-step-7-production-systemd-daemon-setup)
10. [Step 8: Interactive Tmux Multi-Window Workspace (Alternative)](#10-step-8-interactive-tmux-multi-window-workspace-alternative)
11. [Step 9: CloudPanel Firewall & Internal Port Safety](#11-step-9-cloudpanel-firewall--internal-port-safety)
12. [Step 10: Pre-Market Verification & Live Operation](#12-step-10-pre-market-verification--live-operation)

---

## 1. Why CloudPanel for Fresh Trade Engine

If you already manage your Ubuntu server using [CloudPanel](https://www.cloudpanel.io/), you do **not** need to manually build Nginx virtual host configurations, install Certbot CLI, or manage SSL cron renewals from the command line.

### Key Advantages:
- **1-Click Let's Encrypt SSL**: Automatic certificate issuance and auto-renewals managed directly by CloudPanel.
- **GUI Reverse Proxy**: Routes incoming traffic on `https://trade.yourdomain.com` directly to `http://127.0.0.1:8000` with zero overhead.
- **Multi-Site Coexistence**: Fresh Trade Engine runs seamlessly alongside your other websites or WordPress/Node/PHP apps without port collisions or Nginx file conflicts.
- **Built-in Security Controls**: Easy IP whitelisting, HTTP Basic Auth, and firewall management in CloudPanel's web interface.

---

## 2. Architecture with CloudPanel

```
[ Public Internet / Web Browser ]
              │
          (HTTPS 443 / WSS /ws/live)
              ▼
   [ CloudPanel Nginx (Port 80/443) ]
   (SSL Termination + Basic Auth + WebSocket Upgrade)
              │
         (HTTP 8000)
              ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Ubuntu Host (127.0.0.1)                                                │
│                                                                        │
│   [Process 4: Web Dashboard & REST/WS API] (Port 8000)                 │
│         │                                                              │
│         ├─── (Local TCP IPC: 8765) ───┐                                │
│   [Process 2: Strategy Supervisor] ───┼──┐                             │
│   [Process 3: Order Manager] ─────────┘  │                             │
│                                          ▼                             │
│                                [Process 1: Feed Manager] (IPC Server)  │
│                                          │                             │
│                                          ▼                             │
│                              [SQLite Database in WAL Mode]             │
│                              data/trade_engine.db                      │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Step 1: Add a Reverse Proxy Site in CloudPanel

1. Open your CloudPanel admin interface in your browser:
   ```
   https://YOUR_SERVER_IP:8443
   ```
2. In the left navigation menu, click **Sites**, then click **+ Add Site**.
3. Select **Reverse Proxy** as the site type.
4. Fill in the site details:
   - **Domain Name**: `trade.yourdomain.com` (point your domain's DNS A-record to your server IP).
   - **Site User**: Select an existing user or create a dedicated user (e.g. `trader`).
   - **Reverse Proxy URL**: `http://127.0.0.1:8000`
5. Click **Create** to create the site.

> [!TIP]
> Ensure your DNS `A` record for `trade.yourdomain.com` is pointed to your server IP before proceeding to the SSL step so Let's Encrypt verification succeeds immediately.

---

## 4. Step 2: 1-Click Let's Encrypt SSL Certificate

1. Inside CloudPanel, click on your newly created site: **`trade.yourdomain.com`**.
2. Click on the **SSL/TLS** tab.
3. Click **New Let's Encrypt Certificate**.
4. Enter your email address and click **Create and Install**.
5. Once issued, CloudPanel automatically configures HTTPS redirect and handles 90-day certificate renewals in the background.

---

## 5. Step 3: Configure WebSocket Proxying in CloudPanel VHost (Critical)

Fresh Trade Engine streams real-time market ticks, flashing quotes, and order reconciliation updates via WebSocket at `/ws/live`. By default, standard reverse proxies do not forward WebSocket connection upgrades.

1. In CloudPanel, open your site: **`trade.yourdomain.com`**.
2. Click on the **Vhost** tab.
3. You will see CloudPanel's Nginx configuration editor.
4. Add the dedicated `/ws/live` location block inside the `server { listen 443 ssl ... }` block:

```nginx
    # ===================================================================
    # WebSocket Proxy for Fresh Trade Engine Live Ticks & Order Updates
    # ===================================================================
    location /ws/live {
        proxy_pass http://127.0.0.1:8000/ws/live;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
        proxy_buffering off;
    }
```

5. Click **Save** in CloudPanel. CloudPanel validates the Nginx syntax and reloads Nginx instantly.

---

## 6. Step 4: Enable CloudPanel Basic Auth Security

Trading engines should never be exposed to open public access. CloudPanel provides built-in HTTP Basic Authentication directly from the dashboard:

1. In CloudPanel, open **`trade.yourdomain.com`**.
2. Click on the **Security** tab.
3. Locate **Basic Authentication** and toggle it **ON**.
4. Enter a secure **Username** (e.g. `admin` or `leadtrader`) and a strong **Password**.
5. Click **Save**.
6. When opening `https://trade.yourdomain.com`, the browser will prompt for this username and password before loading the trading terminal.

*(Optional)*: If you trade from a static IP or office VPN, you can also add your IP address to CloudPanel's **IP Restriction** list under the same tab for military-grade protection.

---

## 7. Step 5: Server Provisioning & Repository Setup

Now, connect to your Ubuntu server via SSH:

```bash
ssh root@YOUR_SERVER_IP
# Or as your site user:
ssh trader@YOUR_SERVER_IP
```

### 1. Install Required Build Tools & Python 3.10+:
```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git curl tmux sqlite3 chrony build-essential
```

### 2. Set Up the Engine Directory:
You can clone the repository either into the CloudPanel site directory (`/home/trader/htdocs/trade.yourdomain.com`) or your user home directory:

```bash
# Switch to the site user
su - trader

# Navigate to home or site directory
cd /home/trader
git clone https://github.com/krishna711/trade-engine-fresh.git
cd trade-engine-fresh
```

### 3. Create Virtual Environment & Install Dependencies:
```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
mkdir -p data
chmod 755 data
```

### 4. Configure Production Environment Variables:
```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Ensure `API_HOST` is set to `127.0.0.1` so the backend binds only to localhost:
```ini
API_HOST=127.0.0.1
API_PORT=8000

# IIFL XTS Live Feed Credentials (if primary)
XTS_FEED_URL=https://ttblaze.iifl.com/apibinarymarketdata
XTS_FEED_API_KEY=your_live_xts_app_key
XTS_FEED_API_SECRET=your_live_xts_secret_key

# Shoonya Credentials (hot standby failover)
SHOONYA_USER_ID=your_client_id
SHOONYA_PASSWORD=your_password
SHOONYA_TWO_FA=your_totp_secret_key
SHOONYA_VENDOR_CODE=your_vendor_code
SHOONYA_API_SECRET=your_api_secret
```

---

## 8. Step 6: NTP Time Synchronization (Critical for Indian Markets)

Indian stock exchanges (NSE, BSE, MCX) reject orders or TOTP logins if your server clock has more than a 2-second drift.

### 1. Configure Chrony for Indian NTP Pool:
```bash
sudo nano /etc/chrony/chrony.conf
```
Add Indian NTP pool servers at the top:
```conf
server time.google.com iburst
server 0.in.pool.ntp.org iburst
server 1.in.pool.ntp.org iburst
server 2.in.pool.ntp.org iburst
```

### 2. Restart & Enable Chrony:
```bash
sudo systemctl restart chrony
sudo systemctl enable --now chrony
chronyc tracking
```
Confirm `Leap status: Normal` and offset is sub-millisecond.

---

## 9. Step 7: Production Systemd Daemon Setup

Run Fresh Trade Engine as a persistent system daemon that automatically starts on server boot, auto-restarts on unexpected crashes, and logs directly to journalctl.

### 1. Identify Your CloudPanel Site User:
In CloudPanel, each site is assigned a dedicated system user (for example: `bentenbooks-v3` or the username chosen during site creation).

To check the exact user owning your directory:
```bash
ls -ld /home/*/trade-engine-fresh
# Example output: drwxr-xr-x 10 bentenbooks-v3 clp ... -> User is bentenbooks-v3
```

> [!WARNING]
> **Fixing `status=217/USER` Error**:  
> If `systemctl status` reports `code=exited, status=217/USER`, it means the `User=` or `Group=` specified in the service file **does not exist** on your Ubuntu system (e.g., leaving `User=trader`).  
> Set `User=` to your actual CloudPanel user (e.g., `bentenbooks-v3`), and **omit the `Group=` line** so systemd automatically resolves the user's primary group.

### 2. Create / Edit the Systemd Service File:
```bash
sudo nano /etc/systemd/system/trade-engine-fresh.service
```

### 3. Paste the Configuration:
*(Replace `bentenbooks-v3` with your actual site user if different)*:

```ini
[Unit]
Description=Fresh Trade Engine - Unified Production Microservices
After=network.target chrony.service

[Service]
Type=simple
# Your CloudPanel site user (e.g. bentenbooks-v3)
User=bentenbooks-v3
# (Omitting Group= lets systemd use the user's default group automatically)
WorkingDirectory=/home/bentenbooks-v3/trade-engine-fresh
Environment="PATH=/home/bentenbooks-v3/trade-engine-fresh/venv/bin:/usr/local/bin:/usr/bin"
EnvironmentFile=/home/bentenbooks-v3/trade-engine-fresh/.env
ExecStart=/home/bentenbooks-v3/trade-engine-fresh/venv/bin/python backend/run.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

### 4. Reload Systemd & Start the Service:
Whenever you modify a `.service` file, always reload systemd:
```bash
sudo systemctl daemon-reload
sudo systemctl enable trade-engine-fresh
sudo systemctl restart trade-engine-fresh
sudo systemctl status trade-engine-fresh
```

### 5. Monitor Live Engine Logs:
```bash
journalctl -u trade-engine-fresh -f -n 100
```

---

## 10. Step 8: Interactive Tmux Multi-Window Workspace (Alternative)

If you prefer running the 4 engine phases in separate interactive terminal panes to monitor them live (mirroring `start_all_separate_terminals.bat`), use `tmux`:

```bash
cd /home/trader/trade-engine-fresh
nano start_tmux.sh
```

Paste:
```bash
#!/bin/bash
SESSION="trade_engine"
DIR="/home/trader/trade-engine-fresh"
VENV="$DIR/venv/bin/activate"

tmux kill-session -t $SESSION 2>/dev/null

# 1. Market Data Feed Manager
tmux new-session -d -s $SESSION -n "FeedManager"
tmux send-keys -t $SESSION:FeedManager "cd $DIR && source $VENV && python backend/run_feed.py" C-m

sleep 2

# 2. Strategy Supervisor Container
tmux new-window -t $SESSION -n "Strategies"
tmux send-keys -t $SESSION:Strategies "cd $DIR && source $VENV && python backend/run_strategies.py" C-m

# 3. Order Manager & Execution Router
tmux new-window -t $SESSION -n "Orders"
tmux send-keys -t $SESSION:Orders "cd $DIR && source $VENV && python backend/run_order_manager.py" C-m

# 4. Web Terminal & WebSocket API
tmux new-window -t $SESSION -n "WebAPI"
tmux send-keys -t $SESSION:WebAPI "cd $DIR && source $VENV && python backend/run_web.py" C-m

echo "[+] Fresh Trade Engine microservices running inside tmux session '$SESSION'!"
echo "[*] Attach using: tmux attach -t $SESSION"
```

Make executable and run:
```bash
chmod +x start_tmux.sh
./start_tmux.sh
tmux attach -t trade_engine
```
*(Switch windows with `Ctrl+B` then `n`; detach safely with `Ctrl+B` then `d`).*

---

## 11. Step 9: CloudPanel Firewall & Internal Port Safety

1. Open CloudPanel &rarr; **Security** &rarr; **Firewall**.
2. Verify that **only** the following public ports are allowed:
   - `22` (SSH)
   - `80` (HTTP &rarr; redirected to 443)
   - `443` (HTTPS - CloudPanel Nginx)
   - `8443` (CloudPanel Web Management)
3. Ensure backend ports (`8000`, `8765`) are **NOT** added to the CloudPanel firewall.
   - Port `8000` is accessed strictly by CloudPanel's local Nginx reverse proxy.
   - Port `8765` is local loopback TCP IPC for high-frequency tick exchange between python processes.

---

## 12. Step 10: Pre-Market Verification & Live Operation

Once deployed, access your live terminal at `https://trade.yourdomain.com`:

1. **Verify Basic Auth**: Enter the username and password configured in CloudPanel.
2. **Verify WebSocket Live Ticks**:
   - Check the top ticker track. Numbers should flash green/red as ticks arrive.
   - Check the connection status pill on the status page (`/status`). It should display `WebSocket Connected`.
3. **Configure Real Broker Accounts**:
   - Click **+ Add Client Account**.
   - Select **IIFL Securities (XTS)**.
   - Enter your XTS API Key (App Key), API Secret, and click **🔑 Generate Token**.
   - Verify that the card displays `🔑 Token Active Today`.
4. **Run Diagnostics**:
   - Navigate to **Status & Diagnostics**.
   - Run the 6-Segment test to verify live quotes and order simulator connectivity across NSE, NFO, BSE, BSEFO, MCX, and MCXFO.
5. **Switch from Paper to Live (When Ready)**:
   - Toggle the mode badge on your client account card from `PAPER` to `LIVE`.
   - Your configured strategies will now dispatch live market orders to your broker gateway.

---

## 📚 Related Documentation
- 🚀 **[DEPLOYMENT.md](./DEPLOYMENT.md)**: Standard Bare-Metal Linux deployment guide (Ubuntu & Oracle Linux without CloudPanel).
- 🤖 **[AGENTS.md](./AGENTS.md)**: Master AI Agent & Developer Handbook (IPC protocols, schemas, strategy containers).
- 🏗️ **[ARCHITECTURE.md](./ARCHITECTURE.md)**: High-level architectural diagrams & component flows.

