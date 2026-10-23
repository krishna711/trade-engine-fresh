/**
 * Client Cards & Drawer Management
 */

class ClientCardsManager {
    constructor() {
        this.gridEl = document.getElementById("client-cards-grid");
        this.drawerEl = document.getElementById("side-drawer");
        this.overlayEl = document.getElementById("drawer-overlay");
        this.currentClientId = null;
        this.activeDrawerTab = "positions";

        this.initEventListeners();
    }

    initEventListeners() {
        // Drawer close
        const closeBtn = document.getElementById("drawer-close-btn");
        if (closeBtn) closeBtn.onclick = () => this.closeDrawer();
        if (this.overlayEl) this.overlayEl.onclick = () => this.closeDrawer();

        // Drawer tab switches
        const tabBtns = document.querySelectorAll("[data-drawer-tab]");
        tabBtns.forEach(btn => {
            btn.onclick = () => {
                tabBtns.forEach(b => b.classList.remove("active"));
                btn.classList.add("active");
                this.activeDrawerTab = btn.dataset.drawerTab;
                if (this.currentClientId) this.loadDrawerDetails(this.currentClientId);
            };
        });
    }

    async loadAccounts() {
        try {
            const res = await fetch("/api/accounts");
            const accounts = await res.json();
            this.render(accounts);
        } catch (e) {
            console.error("Failed to load accounts:", e);
        }
    }

    render(accounts) {
        if (!this.gridEl) return;
        this.gridEl.innerHTML = accounts.map(acc => {
            const isPos = acc.total_pnl >= 0;
            const sign = isPos ? "+" : "";
            const pnlCls = isPos ? "pnl-positive" : "pnl-negative";
            const brokerCls = `broker-${acc.broker.toLowerCase()}`;

            return `
                <div class="client-card" id="card-${acc.client_id}">
                    <div class="client-card-header">
                        <div class="client-info">
                            <h3>${acc.client_name} <span class="broker-tag ${brokerCls}">${acc.broker}</span></h3>
                            <span class="client-id">${acc.client_id} &bull; ${acc.running_strategies_count} Strategies</span>
                        </div>
                        <button class="mode-toggle-btn ${acc.paper_mode ? 'mode-paper' : 'mode-live'}" 
                                onclick="clientCardsManager.toggleMode('${acc.client_id}')" 
                                title="Click to toggle Paper/Live mode">
                            ${acc.paper_mode ? '📄 PAPER' : '🟢 LIVE'}
                        </button>
                    </div>

                    <!-- PnL Grid (Short U: R: T:) -->
                    <div class="card-pnl-grid">
                        <div class="card-pnl-item">
                            <span class="card-pnl-title">U:</span>
                            <span class="card-pnl-num ${acc.unrealized_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}">
                                ${acc.unrealized_pnl >= 0 ? '+' : ''}₹${acc.unrealized_pnl.toFixed(2)}
                            </span>
                        </div>
                        <div class="card-pnl-item">
                            <span class="card-pnl-title">R:</span>
                            <span class="card-pnl-num ${acc.realized_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}">
                                ${acc.realized_pnl >= 0 ? '+' : ''}₹${acc.realized_pnl.toFixed(2)}
                            </span>
                        </div>
                        <div class="card-pnl-item">
                            <span class="card-pnl-title">T:</span>
                            <span class="card-pnl-num ${pnlCls}">
                                ${sign}₹${acc.total_pnl.toFixed(2)}
                            </span>
                        </div>
                    </div>

                    <!-- Prominent Actions -->
                    <div class="card-actions-row">
                        <button class="btn-square-off" onclick="clientCardsManager.squareOff('${acc.client_id}')" title="Emergency Square Off all positions for this account">
                            ⚡ SQUARE OFF
                        </button>
                        <button class="btn-card-menu" onclick="clientCardsManager.toggleLogin('${acc.client_id}', ${acc.is_logged_in})" title="Session status">
                            ${acc.is_logged_in ? '🟢 Active' : '⚪ Terminated'}
                        </button>
                        <button class="btn-card-menu" onclick="clientCardsManager.openDrawer('${acc.client_id}', '${acc.client_name}')" title="View OrderBook, Trades, Positions, Margins">
                            📋 Details
                        </button>
                    </div>
                </div>
            `;
        }).join("");
    }

    async toggleMode(clientId) {
        try {
            const res = await fetch(`/api/accounts/${clientId}/toggle-mode`, { method: "POST" });
            const data = await res.json();
            if (data.success) {
                this.loadAccounts();
            }
        } catch (e) {
            alert("Failed to toggle mode: " + e);
        }
    }

    async toggleLogin(clientId, isCurrentlyLoggedIn) {
        try {
            const res = await fetch(`/api/accounts/${clientId}/login`, { method: "POST" });
            const data = await res.json();
            if (data.success) {
                this.loadAccounts();
            }
        } catch (e) {
            alert("Failed to update login status: " + e);
        }
    }

    async squareOff(clientId) {
        if (!confirm(`Are you sure you want to SQUARE OFF all open positions for account ${clientId}?`)) return;
        try {
            const res = await fetch(`/api/accounts/${clientId}/square-off`, { method: "POST" });
            const data = await res.json();
            alert(`Square off executed: ${data.squared_positions} positions closed.`);
            this.loadAccounts();
            if (window.strategiesManager) window.strategiesManager.loadInstances();
        } catch (e) {
            alert("Square off failed: " + e);
        }
    }

    async openDrawer(clientId, clientName) {
        this.currentClientId = clientId;
        document.getElementById("drawer-client-name").innerText = clientName;
        document.getElementById("drawer-client-id").innerText = `Client ID: ${clientId}`;
        this.drawerEl.classList.add("active");
        this.overlayEl.classList.add("active");
        this.loadDrawerDetails(clientId);
    }

    closeDrawer() {
        this.drawerEl.classList.remove("active");
        this.overlayEl.classList.remove("active");
        this.currentClientId = null;
    }

    async loadDrawerDetails(clientId) {
        const bodyEl = document.getElementById("drawer-body-content");
        bodyEl.innerHTML = `<div style="text-align: center; color: var(--text-muted); padding: 2rem;">Loading account details...</div>`;

        try {
            const res = await fetch(`/api/accounts/${clientId}/details`);
            const data = await res.json();

            if (this.activeDrawerTab === "positions") {
                bodyEl.innerHTML = `
                    <table class="data-table">
                        <thead>
                            <tr><th>Symbol</th><th>Net Qty</th><th>Buy Avg</th><th>LTP</th><th>PnL</th></tr>
                        </thead>
                        <tbody>
                            ${data.positions.length ? data.positions.map(p => `
                                <tr>
                                    <td><strong>${p.symbol}</strong></td>
                                    <td style="font-family: var(--font-mono);">${p.net_qty}</td>
                                    <td style="font-family: var(--font-mono);">${p.buy_avg.toFixed(2)}</td>
                                    <td style="font-family: var(--font-mono);">${p.ltp.toFixed(2)}</td>
                                    <td class="${p.pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}" style="font-family: var(--font-mono); font-weight: 700;">
                                        ${p.pnl >= 0 ? '+' : ''}₹${p.pnl.toFixed(2)}
                                    </td>
                                </tr>
                            `).join("") : '<tr><td colspan="5" style="text-align: center; color: var(--text-muted);">No open positions</td></tr>'}
                        </tbody>
                    </table>
                `;
            } else if (this.activeDrawerTab === "orders") {
                bodyEl.innerHTML = `
                    <table class="data-table">
                        <thead>
                            <tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Status</th></tr>
                        </thead>
                        <tbody>
                            ${data.orders.length ? data.orders.map(o => `
                                <tr>
                                    <td>${o.created_at}</td>
                                    <td>${o.symbol}</td>
                                    <td style="font-weight: 700; color: ${o.side === 'BUY' ? 'var(--accent-green)' : 'var(--accent-red)'}">${o.side}</td>
                                    <td>${o.quantity}</td>
                                    <td>${o.price}</td>
                                    <td><span class="status-pill status-${o.status === 'FILLED' ? 'running' : 'stopped'}">${o.status}</span></td>
                                </tr>
                            `).join("") : '<tr><td colspan="6" style="text-align: center; color: var(--text-muted);">No orders found</td></tr>'}
                        </tbody>
                    </table>
                `;
            } else if (this.activeDrawerTab === "trades") {
                bodyEl.innerHTML = `
                    <table class="data-table">
                        <thead>
                            <tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th></tr>
                        </thead>
                        <tbody>
                            ${data.trades.length ? data.trades.map(t => `
                                <tr>
                                    <td>${t.timestamp}</td>
                                    <td>${t.symbol}</td>
                                    <td style="font-weight: 700; color: ${t.side === 'BUY' ? 'var(--accent-green)' : 'var(--accent-red)'}">${t.side}</td>
                                    <td>${t.quantity}</td>
                                    <td>₹${t.price.toFixed(2)}</td>
                                </tr>
                            `).join("") : '<tr><td colspan="5" style="text-align: center; color: var(--text-muted);">No executed trades</td></tr>'}
                        </tbody>
                    </table>
                `;
            } else if (this.activeDrawerTab === "holdings") {
                bodyEl.innerHTML = `
                    <table class="data-table">
                        <thead>
                            <tr><th>Instrument</th><th>Qty</th><th>Avg Price</th><th>LTP</th><th>PnL</th></tr>
                        </thead>
                        <tbody>
                            ${data.holdings.map(h => `
                                <tr>
                                    <td><strong>${h.symbol}</strong></td>
                                    <td>${h.qty}</td>
                                    <td>₹${h.buy_price.toFixed(2)}</td>
                                    <td>₹${h.ltp.toFixed(2)}</td>
                                    <td class="pnl-positive" style="font-weight: 700;">+₹${h.pnl.toFixed(2)}</td>
                                </tr>
                            `).join("")}
                        </tbody>
                    </table>
                `;
            } else if (this.activeDrawerTab === "margins") {
                bodyEl.innerHTML = `
                    <div style="display: flex; flex-direction: column; gap: 1rem; padding: 0.5rem;">
                        <div class="card-pnl-grid" style="grid-template-columns: 1fr 1fr;">
                            <div class="card-pnl-item">
                                <span class="card-pnl-title">Margin Available</span>
                                <span class="card-pnl-num pnl-positive">₹${data.margins.available.toLocaleString('en-IN', {minimumFractionDigits: 2})}</span>
                            </div>
                            <div class="card-pnl-item">
                                <span class="card-pnl-title">Margin Utilized</span>
                                <span class="card-pnl-num pnl-negative">₹${data.margins.used.toLocaleString('en-IN', {minimumFractionDigits: 2})}</span>
                            </div>
                        </div>
                        <div style="background: var(--bg-tertiary); padding: 1rem; border-radius: 6px; border: 1px solid var(--border-color);">
                            <span style="font-size: 0.8rem; color: var(--text-muted);">Total Account Equity Balance</span>
                            <div style="font-family: var(--font-mono); font-size: 1.25rem; font-weight: 800; color: var(--text-primary); margin-top: 4px;">
                                ₹${data.margins.total.toLocaleString('en-IN', {minimumFractionDigits: 2})}
                            </div>
                        </div>
                    </div>
                `;
            }
        } catch (e) {
            bodyEl.innerHTML = `<div style="color: var(--accent-red); padding: 1rem;">Failed to load details: ${e}</div>`;
        }
    }
}

window.clientCardsManager = new ClientCardsManager();
