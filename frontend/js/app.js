/**
 * Master Application Controller & WebSocket Connection
 */

class App {
    constructor() {
        this.currentView = "dashboard";
        this.currentRole = localStorage.getItem("fresh_trade_role") || "ADMIN";
        this.currentTheme = localStorage.getItem("fresh_trade_theme") || "dark";
        this.ws = null;

        this.init();
    }

    init() {
        // Apply theme
        if (this.currentTheme === "light") {
            document.body.classList.add("light-theme");
        }

        // Apply role UI state
        this.applyRole(this.currentRole);

        // Setup event listeners
        this.initEventListeners();

        const token = localStorage.getItem("fresh_trade_auth_token");
        if (token) {
            // Connect WebSocket
            this.connectWebSocket();

            // Initial data fetch
            this.refreshAll();
        }

        // Periodic summary refresh (only when authenticated)
        setInterval(() => {
            if (localStorage.getItem("fresh_trade_auth_token")) {
                this.fetchDashboardSummary();
            }
        }, 3000);
    }

    onAuthSuccess() {
        this.currentRole = localStorage.getItem("fresh_trade_role") || "USER";
        this.applyRole(this.currentRole);
        this.connectWebSocket();
        this.refreshAll();
    }

    initEventListeners() {
        // Navigation Buttons
        document.querySelectorAll("[data-view]").forEach(btn => {
            btn.onclick = () => {
                this.switchView(btn.dataset.view);
            };
        });

        // Role Switcher
        const btnAdmin = document.getElementById("btn-role-admin");
        const btnUser = document.getElementById("btn-role-user");

        if (btnAdmin) btnAdmin.onclick = () => this.applyRole("ADMIN");
        if (btnUser) btnUser.onclick = () => this.applyRole("USER");

        // Theme Toggle
        const btnTheme = document.getElementById("btn-theme-toggle");
        if (btnTheme) {
            btnTheme.onclick = () => {
                document.body.classList.toggle("light-theme");
                this.currentTheme = document.body.classList.contains("light-theme") ? "light" : "dark";
                localStorage.setItem("fresh_trade_theme", this.currentTheme);
            };
        }

        // Settings Modal
        const btnSettings = document.getElementById("btn-open-settings");
        if (btnSettings) {
            btnSettings.onclick = () => this.openSettingsModal();
        }

        const btnSaveCreds = document.getElementById("btn-save-credentials");
        if (btnSaveCreds) {
            btnSaveCreds.onclick = () => this.saveCredentials();
        }
    }

    switchView(viewName) {
        this.currentView = viewName;
        // Update nav buttons
        document.querySelectorAll("[data-view]").forEach(btn => {
            btn.classList.toggle("active", btn.dataset.view === viewName);
        });

        // Update view sections
        document.querySelectorAll(".view-section").forEach(sec => {
            sec.classList.toggle("active", sec.id === `view-${viewName}`);
        });

        // Trigger view-specific loads
        if (viewName === "dashboard") {
            if (window.clientCardsManager) window.clientCardsManager.loadAccounts();
            if (window.strategiesManager) window.strategiesManager.loadInstances();
        } else if (viewName === "strategies") {
            if (window.strategiesManager) window.strategiesManager.loadTemplates();
        } else if (viewName === "status") {
            if (window.statusManager) window.statusManager.loadFeedStatus();
        } else if (viewName === "logs") {
            if (window.logsManager) window.logsManager.loadLogs();
        } else if (viewName === "reports") {
            if (window.reportsManager) window.reportsManager.loadReports();
        }
    }

    applyRole(role) {
        this.currentRole = role;
        localStorage.setItem("fresh_trade_role", role);

        const btnAdmin = document.getElementById("btn-role-admin");
        const btnUser = document.getElementById("btn-role-user");
        if (btnAdmin) btnAdmin.classList.toggle("active", role === "ADMIN");
        if (btnUser) btnUser.classList.toggle("active", role === "USER");

        // Toggle admin-only controls
        const btnOpenUsers = document.getElementById("btn-open-users");
        const navUsers = document.getElementById("nav-users");
        if (btnOpenUsers) btnOpenUsers.style.display = role === "ADMIN" ? "inline-flex" : "none";
        if (navUsers) navUsers.style.display = role === "ADMIN" ? "inline-flex" : "none";
        document.querySelectorAll(".admin-only").forEach(el => {
            el.style.display = role === "ADMIN" ? "" : "none";
        });

        // Re-render strategy lists to toggle admin vs user names
        if (window.strategiesManager) {
            window.strategiesManager.loadInstances();
        }
    }

    connectWebSocket() {
        const token = localStorage.getItem("fresh_trade_auth_token");
        if (!token) return;

        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = `${protocol}//${window.location.host}/ws/live?token=${encodeURIComponent(token)}`;
        const badgeText = document.getElementById("ws-status-text");
        const badgePill = document.getElementById("ws-status-badge");

        try {
            if (this.ws) {
                try { this.ws.close(); } catch (e) {}
            }
            this.ws = new WebSocket(wsUrl);

            this.ws.onopen = () => {
                if (badgeText) badgeText.innerText = "LIVE";
                if (badgePill) {
                    badgePill.className = "status-pill status-running";
                }
            };

            this.ws.onmessage = (event) => {
                try {
                    const msg = JSON.parse(event.data);
                    this.handleWebSocketMessage(msg);
                } catch (e) {
                    console.error("WS Parse error:", e);
                }
            };

            this.ws.onclose = () => {
                if (badgeText) badgeText.innerText = "RECONNECTING";
                if (badgePill) {
                    badgePill.className = "status-pill status-error";
                }
                setTimeout(() => this.connectWebSocket(), 2000);
            };

            this.ws.onerror = () => {
                if (this.ws) this.ws.close();
            };
        } catch (e) {
            console.error("WebSocket init error:", e);
        }
    }

    handleWebSocketMessage(msg) {
        if (msg.type === "TICK") {
            const tick = msg.data;
            if (window.tickerManager) window.tickerManager.updateTick(tick);
        } else if (msg.type === "INITIAL_DATA") {
            if (msg.ticks && window.tickerManager) {
                Object.values(msg.ticks).forEach(t => window.tickerManager.updateTick(t));
            }
            if (msg.feed_status && window.statusManager) {
                window.statusManager.renderFeedBrokers(msg.feed_status);
            }
        } else if (msg.type === "FEED_STATUS") {
            if (window.statusManager) window.statusManager.renderFeedBrokers(msg.data);
        } else if (msg.type === "ORDER_UPDATE") {
            if (window.clientCardsManager) window.clientCardsManager.loadAccounts();
            if (window.strategiesManager) window.strategiesManager.loadInstances();
            if (window.logsManager) window.logsManager.loadLogs();
        } else if (msg.type === "STRATEGY_EVENT") {
            if (window.logsManager) window.logsManager.loadLogs();
        }
    }

    async refreshAll() {
        await this.fetchDashboardSummary();
        if (window.clientCardsManager) await window.clientCardsManager.loadAccounts();
        if (window.strategiesManager) {
            await window.strategiesManager.loadInstances();
            await window.strategiesManager.loadTemplates();
        }
        if (window.statusManager) await window.statusManager.loadFeedStatus();
        if (window.logsManager) await window.logsManager.loadLogs();
    }

    async fetchDashboardSummary() {
        try {
            const res = await fetch("/api/dashboard");
            const data = await res.json();

            // Update ticker prices from dashboard payload
            if (data.ticks && window.tickerManager) {
                Object.values(data.ticks).forEach(t => window.tickerManager.updateTick(t));
            }

            const pnl = data.pnl_summary;
            const uEl = document.getElementById("header-unrealized-pnl");
            const rEl = document.getElementById("header-realized-pnl");
            const tEl = document.getElementById("header-total-pnl");

            const formatHeaderPnl = (val) => {
                const isNeg = val < 0;
                const sign = isNeg ? "-₹" : "+₹";
                return `${sign}${Math.abs(val).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
            };

            if (uEl) {
                uEl.className = `pnl-val ${pnl.unrealized_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}`;
                uEl.innerText = formatHeaderPnl(pnl.unrealized_pnl);
            }
            if (rEl) {
                rEl.className = `pnl-val ${pnl.realized_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}`;
                rEl.innerText = formatHeaderPnl(pnl.realized_pnl);
            }
            if (tEl) {
                tEl.className = `pnl-val ${pnl.total_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}`;
                tEl.innerText = formatHeaderPnl(pnl.total_pnl);
            }

            // Keep dashboard tables and client cards refreshed with latest PnL & trade states
            if (this.currentView === "dashboard") {
                if (window.strategiesManager) window.strategiesManager.loadInstances();
                if (window.clientCardsManager) window.clientCardsManager.loadAccounts();
            }
        } catch (e) {
            // silent retry
        }
    }

    async openSettingsModal() {
        document.getElementById("modal-settings").classList.add("active");
        try {
            const res = await fetch("/api/settings/credentials");
            const data = await res.json();
            const xts = data.xts || {};
            document.getElementById("setting-xts-url").value = xts.api_url || "https://xts.iifl.com/marketdata";
            document.getElementById("setting-xts-key").value = xts.api_key || "";
            document.getElementById("setting-xts-secret").value = xts.api_secret || "";

            const statusPill = document.getElementById("xts-live-status-pill");
            const statusText = document.getElementById("xts-live-status-text");
            if (xts.is_live_connected) {
                statusPill.className = "status-pill status-running";
                statusPill.innerHTML = `<span class="status-dot"></span> LIVE CONNECTED`;
                statusText.innerText = `Connected with User ID: ${xts.user_id || 'Active'}`;
            } else {
                statusPill.className = "status-pill status-stopped";
                statusPill.innerHTML = `<span class="status-dot"></span> Standby / Sim`;
                statusText.innerText = `Standby (High-Fidelity Simulation Active)`;
            }
        } catch (e) {
            console.error("Error loading settings:", e);
        }
    }

    async saveCredentials() {
        const btn = document.getElementById("btn-save-credentials");
        const originalText = btn.innerText;
        btn.innerText = "Connecting...";
        btn.disabled = true;

        const apiUrl = document.getElementById("setting-xts-url").value;
        const apiKey = document.getElementById("setting-xts-key").value;
        const apiSecret = document.getElementById("setting-xts-secret").value;

        try {
            const res = await fetch("/api/settings/credentials", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    xts: {
                        api_url: apiUrl,
                        api_key: apiKey,
                        api_secret: apiSecret
                    }
                })
            });
            const data = await res.json();
            btn.innerText = originalText;
            btn.disabled = false;

            if (data.success) {
                alert(data.is_live_connected 
                    ? `Successfully connected to Live IIFL XTS Market Data Feed! User ID: ${data.user_id}`
                    : `Credentials saved to .env. Feed engine is active.`);
                document.getElementById("modal-settings").classList.remove("active");
                if (window.statusManager) window.statusManager.loadFeedStatus();
            } else {
                alert("Failed: " + (data.message || "Unknown error"));
            }
        } catch (e) {
            btn.innerText = originalText;
            btn.disabled = false;
            alert("Error saving credentials: " + e);
        }
    }
}

window.app = new App();
