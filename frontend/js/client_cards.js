/**
 * Client Cards & Drawer Management
 */

class ClientCardsManager {
    constructor() {
        window.clientCardsManager = this;
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

        // Add Client button & submit
        const addAccBtn = document.getElementById("btn-add-client-account");
        if (addAccBtn) addAccBtn.onclick = () => this.openAddClientModal();

        const submitAddBtn = document.getElementById("btn-submit-add-client");
        if (submitAddBtn) submitAddBtn.onclick = () => this.submitAddClient();
    }

    openAddClientModal() {
        const idInput = document.getElementById("new-account-client-id");
        const nameInput = document.getElementById("new-account-name");
        const marginInput = document.getElementById("new-account-margin");
        const xtsKey = document.getElementById("xts-api-key");
        const xtsSecret = document.getElementById("xts-api-secret");
        const xtsToken = document.getElementById("xts-today-token");
        const otherKey = document.getElementById("other-broker-api-key");
        const feedbackEl = document.getElementById("xts-token-feedback");

        if (idInput) idInput.value = "";
        if (nameInput) nameInput.value = "";
        if (marginInput) marginInput.value = "500000";
        if (xtsKey) xtsKey.value = "";
        if (xtsSecret) xtsSecret.value = "";
        if (xtsToken) xtsToken.value = "";
        if (otherKey) otherKey.value = "";
        if (feedbackEl) {
            feedbackEl.innerHTML = `ℹ️ Optional: Leave empty or paste existing token. Click <strong>Generate Token</strong> to login and generate today's session token. It will be used for all further broker API calls.`;
        }

        const brokerSelect = document.getElementById("new-account-broker");
        if (brokerSelect) {
            brokerSelect.value = "XTS";
            this.onBrokerSelectChange("XTS");
        }

        const modal = document.getElementById("modal-add-client");
        if (modal) {
            modal.classList.add("active");
            modal.style.display = "";
        }
    }

    closeAddClientModal() {
        const modal = document.getElementById("modal-add-client");
        if (modal) {
            modal.classList.remove("active");
            modal.style.display = "";
        }
    }

    onBrokerSelectChange(broker) {
        const xtsBlock = document.getElementById("fields-broker-xts");
        const otherBlock = document.getElementById("fields-broker-other");
        const otherTitle = document.getElementById("other-broker-title");
        if (broker === "XTS") {
            if (xtsBlock) xtsBlock.style.display = "block";
            if (otherBlock) otherBlock.style.display = "none";
        } else {
            if (xtsBlock) xtsBlock.style.display = "none";
            if (otherBlock) {
                otherBlock.style.display = "block";
                if (otherTitle) otherTitle.innerText = broker;
            }
        }
    }

    toggleSecretVisibility(inputId) {
        const inp = document.getElementById(inputId);
        if (inp) {
            inp.type = inp.type === "password" ? "text" : "password";
        }
    }

    async generateXtsTokenNow(clientId = null) {
        const apiKey = (clientId ? "" : document.getElementById("xts-api-key")?.value?.trim()) || "";
        const apiSecret = (clientId ? "" : document.getElementById("xts-api-secret")?.value?.trim()) || "";
        const interactiveUrl = document.getElementById("xts-interactive-url")?.value?.trim() || "https://ttblaze.iifl.com/interactive";
        const feedbackEl = document.getElementById(clientId ? "drawer-xts-token-feedback" : "xts-token-feedback");
        const btn = document.getElementById(clientId ? "drawer-btn-generate-token" : "btn-generate-xts-token");

        if (!clientId && (!apiKey || !apiSecret)) {
            alert("Please enter XTS API Key (App Key) and API Secret first.");
            return;
        }

        if (btn) {
            btn.disabled = true;
            btn.innerText = "⏳ Generating...";
        }
        if (feedbackEl) {
            feedbackEl.innerHTML = `<span style="color: var(--accent-blue);">Contacting XTS server to authenticate and generate session token...</span>`;
        }

        try {
            const res = await fetch("/api/accounts/xts/generate-token", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    client_id: clientId,
                    api_key: apiKey,
                    api_secret: apiSecret,
                    interactive_url: interactiveUrl
                })
            });
            const data = await res.json();
            if (data.success && data.token) {
                if (!clientId) {
                    const tokenInput = document.getElementById("xts-today-token");
                    if (tokenInput) tokenInput.value = data.token;
                }
                if (feedbackEl) {
                    feedbackEl.innerHTML = `<span style="color: var(--accent-green); font-weight: 600;">✓ Token generated successfully for today (${data.token_date})! Valid for all broker API calls.</span>`;
                }
                if (clientId) {
                    await this.loadDrawerDetails(clientId);
                    await this.loadAccounts();
                }
            } else {
                const errMsg = data.error || data.detail || "Authentication failed";
                if (feedbackEl) {
                    feedbackEl.innerHTML = `<span style="color: var(--accent-red); font-weight: 600;">✗ Generation failed: ${errMsg}</span>`;
                } else {
                    alert("Failed to generate XTS token: " + errMsg);
                }
            }
        } catch (e) {
            if (feedbackEl) {
                feedbackEl.innerHTML = `<span style="color: var(--accent-red);">✗ Error: ${e}</span>`;
            } else {
                alert("Error connecting to server: " + e);
            }
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.innerText = "🔑 Generate Token";
            }
        }
    }

    async submitAddClient() {
        const broker = document.getElementById("new-account-broker")?.value || "XTS";
        const clientId = document.getElementById("new-account-client-id")?.value?.trim();
        const clientName = document.getElementById("new-account-name")?.value?.trim();
        const mode = document.getElementById("new-account-mode")?.value || "paper";
        const margin = parseFloat(document.getElementById("new-account-margin")?.value) || 500000;

        let credentials_json = {};
        if (broker === "XTS") {
            const apiKey = document.getElementById("xts-api-key")?.value?.trim() || "";
            const apiSecret = document.getElementById("xts-api-secret")?.value?.trim() || "";
            const token = document.getElementById("xts-today-token")?.value?.trim() || "";
            const interactiveUrl = document.getElementById("xts-interactive-url")?.value?.trim() || "https://ttblaze.iifl.com/interactive";
            const todayStr = new Date().toISOString().slice(0, 10);

            credentials_json = {
                api_key: apiKey,
                api_secret: apiSecret,
                token: token,
                token_date: token ? todayStr : "",
                interactive_url: interactiveUrl
            };
        } else {
            const apiKey = document.getElementById("other-broker-api-key")?.value?.trim() || "";
            credentials_json = { api_key: apiKey };
        }

        if (!clientId || !clientName) {
            alert("Client ID and Client Name are required.");
            return;
        }

        try {
            const res = await fetch("/api/accounts", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    broker: broker,
                    client_id: clientId,
                    client_name: clientName,
                    paper_mode: mode === "paper",
                    margin_available: margin,
                    credentials_json: credentials_json
                })
            });
            const data = await res.json();
            if (data.success) {
                this.closeAddClientModal();
                await this.loadAccounts();
                if (window.strategiesManager) {
                    window.strategiesManager.loadInstances();
                }
            } else {
                alert("Failed to add client account: " + (data.detail || data.error || "Unknown error"));
            }
        } catch (e) {
            alert("Error adding client account: " + e);
        }
    }

    async deleteAccount(clientId, clientName) {
        if (!confirm(`Are you sure you want to delete client account "${clientName}" (${clientId})?\n\nThis will permanently remove the account and unmap it from all active strategies.`)) {
            return;
        }

        try {
            const res = await fetch(`/api/accounts/${clientId}`, { method: "DELETE" });
            const data = await res.json();
            if (data.success) {
                await this.loadAccounts();
                if (window.strategiesManager) {
                    window.strategiesManager.loadInstances();
                }
            } else {
                alert("Failed to delete account: " + (data.detail || data.error || "Unknown error"));
            }
        } catch (e) {
            alert("Error deleting client account: " + e);
        }
    }

    async loadAccounts() {
        try {
            const res = await fetch("/api/accounts");
            const accounts = await res.json();
            this.accounts = accounts;
            this.render(accounts);
        } catch (e) {
            console.error("Failed to load accounts:", e);
        }
    }

    render(accounts) {
        if (!this.gridEl) return;
        if (!accounts || accounts.length === 0) {
            this.gridEl.innerHTML = `
                <div style="grid-column: 1 / -1; padding: 2rem; text-align: center; background: var(--bg-card); border-radius: 8px; border: 1px dashed var(--border-color); color: var(--text-muted);">
                    <div style="font-size: 1.5rem; margin-bottom: 0.5rem;">👥</div>
                    <div>No client accounts configured.</div>
                    <div style="font-size: 0.8rem; margin-top: 0.25rem;">Click <strong>+ Add Client Account</strong> above to add real or paper accounts.</div>
                </div>
            `;
            return;
        }

        this.gridEl.innerHTML = accounts.map(acc => {
            const isPos = acc.total_pnl >= 0;
            const sign = isPos ? "+" : "";
            const pnlCls = isPos ? "pnl-positive" : "pnl-negative";
            const brokerCls = `broker-${acc.broker.toLowerCase()}`;

            const tokenBadge = acc.broker === 'XTS' ? (
                acc.has_token_today 
                ? `<span style="font-size: 0.68rem; padding: 1px 6px; border-radius: 4px; background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3); font-weight: 600; cursor: pointer;" onclick="clientCardsManager.openDrawer('${acc.client_id}', '${acc.client_name.replace(/'/g, "\\'")}', 'credentials')" title="Token active for today (${acc.token_date}). Click to view or regenerate.">🔑 Token Active</span>`
                : `<span style="font-size: 0.68rem; padding: 1px 6px; border-radius: 4px; background: rgba(245, 158, 11, 0.15); color: #f59e0b; border: 1px solid rgba(245, 158, 11, 0.3); font-weight: 600; cursor: pointer;" onclick="clientCardsManager.openDrawer('${acc.client_id}', '${acc.client_name.replace(/'/g, "\\'")}', 'credentials')" title="No active token for today. Click to generate or enter token.">⚠️ Set Today Token</span>`
            ) : '';

            return `
                <div class="client-card" id="card-${acc.client_id}">
                    <div class="client-card-header">
                        <div class="client-info">
                            <div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
                                <h3>${acc.client_name}</h3>
                                <span class="broker-tag ${brokerCls}">${acc.broker}</span>
                                ${tokenBadge}
                            </div>
                            <span class="client-id">${acc.client_id} &bull; ${acc.running_strategies_count} Strategies</span>
                        </div>
                        <div style="display: flex; gap: 0.35rem; align-items: center;">
                            <button class="mode-toggle-btn ${acc.paper_mode ? 'mode-paper' : 'mode-live'}" 
                                    onclick="clientCardsManager.toggleMode('${acc.client_id}')" 
                                    title="Click to toggle Paper/Live mode">
                                ${acc.paper_mode ? '📄 PAPER' : '🟢 LIVE'}
                            </button>
                            <button class="icon-btn-delete-acc" 
                                    onclick="clientCardsManager.deleteAccount('${acc.client_id}', '${acc.client_name.replace(/'/g, "\\'")}')" 
                                    title="Delete Account" 
                                    style="background: rgba(239, 68, 68, 0.12); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 6px; padding: 4px 7px; cursor: pointer; font-size: 0.8rem; transition: all 0.2s;">
                                🗑️
                            </button>
                        </div>
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
                        <button class="btn-card-menu" onclick="clientCardsManager.openDrawer('${acc.client_id}', '${acc.client_name.replace(/'/g, "\\'")}', 'credentials')" title="View / Update API credentials and Today's Token">
                            🔑 Token
                        </button>
                        <button class="btn-card-menu" onclick="clientCardsManager.openDrawer('${acc.client_id}', '${acc.client_name.replace(/'/g, "\\'")}', 'positions')" title="View OrderBook, Trades, Positions, Margins">
                            📋 Details
                        </button>
                        <button class="btn-card-menu btn-card-delete" onclick="clientCardsManager.deleteAccount('${acc.client_id}', '${acc.client_name.replace(/'/g, "\\'")}')" title="Delete Client Account" style="color: #ef4444; border-color: rgba(239, 68, 68, 0.5); background: rgba(239, 68, 68, 0.12); font-weight: 700; cursor: pointer;">
                            🗑️ Delete
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

    async openDrawer(clientId, clientName, defaultTab = null) {
        this.currentClientId = clientId;
        const nameEl = document.getElementById("drawer-client-name");
        const idEl = document.getElementById("drawer-client-id");
        if (nameEl) nameEl.innerText = clientName;
        if (idEl) idEl.innerText = `Client ID: ${clientId}`;

        if (defaultTab) {
            this.activeDrawerTab = defaultTab;
            const tabBtns = document.querySelectorAll("[data-drawer-tab]");
            tabBtns.forEach(b => {
                if (b.dataset.drawerTab === defaultTab) b.classList.add("active");
                else b.classList.remove("active");
            });
        }

        if (this.drawerEl) this.drawerEl.classList.add("active");
        if (this.overlayEl) this.overlayEl.classList.add("active");
        this.loadDrawerDetails(clientId);
    }

    closeDrawer() {
        if (this.drawerEl) this.drawerEl.classList.remove("active");
        if (this.overlayEl) this.overlayEl.classList.remove("active");
        this.currentClientId = null;
    }

    async saveDrawerToken(clientId) {
        const tokenVal = document.getElementById("drawer-xts-token-val")?.value?.trim() || "";
        const feedbackEl = document.getElementById("drawer-xts-token-feedback");

        try {
            const todayStr = new Date().toISOString().slice(0, 10);
            const res = await fetch(`/api/accounts/${clientId}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    credentials_json: {
                        token: tokenVal,
                        token_date: tokenVal ? todayStr : ""
                    }
                })
            });
            const data = await res.json();
            if (data.success) {
                if (feedbackEl) {
                    feedbackEl.innerHTML = `<span style="color: var(--accent-green); font-weight: 600;">✓ Token saved successfully for today (${todayStr})! Valid for broker API calls.</span>`;
                }
                await this.loadDrawerDetails(clientId);
                await this.loadAccounts();
            } else {
                alert("Failed to update token: " + (data.detail || data.error));
            }
        } catch (e) {
            alert("Error updating token: " + e);
        }
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
            } else if (this.activeDrawerTab === "credentials") {
                const creds = data.credentials || {};
                const isValid = creds.is_valid_today;

                bodyEl.innerHTML = `
                    <div style="display: flex; flex-direction: column; gap: 1rem; padding: 0.5rem;">
                        <!-- Broker Overview & Status Card -->
                        <div style="background: var(--bg-tertiary); padding: 1rem; border-radius: 8px; border: 1px solid var(--border-color);">
                            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem;">
                                <div>
                                    <span style="font-size: 0.72rem; color: var(--text-muted);">Broker Platform</span>
                                    <div style="font-weight: 700; color: var(--text-primary); font-size: 1rem;">
                                        ${data.broker} ${data.broker === 'XTS' ? '(IIFL Securities)' : ''}
                                    </div>
                                </div>
                                <div>
                                    ${isValid 
                                        ? `<span style="padding: 4px 10px; border-radius: 20px; background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3); font-weight: 700; font-size: 0.75rem;">🟢 Token Active Today</span>`
                                        : `<span style="padding: 4px 10px; border-radius: 20px; background: rgba(239, 68, 68, 0.15); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.3); font-weight: 700; font-size: 0.75rem;">🔴 Token Expired / Missing</span>`}
                                </div>
                            </div>

                            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; font-size: 0.8rem;">
                                <div>
                                    <span style="color: var(--text-muted); font-size: 0.72rem;">API Key (App Key):</span>
                                    <div style="font-family: var(--font-mono); font-weight: 600; color: var(--text-primary); word-break: break-all;">
                                        ${creds.api_key || '<span style="color: var(--text-muted);">Not Configured</span>'}
                                    </div>
                                </div>
                                <div>
                                    <span style="color: var(--text-muted); font-size: 0.72rem;">Token Date:</span>
                                    <div style="font-family: var(--font-mono); font-weight: 600; color: var(--text-primary);">
                                        ${creds.token_date || '<span style="color: var(--text-muted);">None</span>'}
                                    </div>
                                </div>
                            </div>
                        </div>

                        <!-- Token Management & Generation -->
                        <div style="background: var(--bg-tertiary); padding: 1rem; border-radius: 8px; border: 1px solid var(--border-color); display: flex; flex-direction: column; gap: 0.75rem;">
                            <div style="display: flex; justify-content: space-between; align-items: center;">
                                <label style="font-size: 0.8rem; font-weight: 700; color: var(--text-primary);">Today's Session Token</label>
                                <span style="font-size: 0.7rem; color: var(--text-muted);">Used for all broker API calls</span>
                            </div>

                            <div>
                                <textarea id="drawer-xts-token-val" rows="3" style="width: 100%; padding: 0.5rem; background: var(--bg-secondary); border: 1px solid var(--border-color); color: var(--text-primary); border-radius: 6px; font-family: var(--font-mono); font-size: 0.78rem; resize: vertical;" placeholder="Paste today's session token here...">${creds.token || ''}</textarea>
                            </div>

                            <div style="display: flex; gap: 0.5rem; justify-content: flex-end; align-items: center; flex-wrap: wrap;">
                                <button type="button" class="action-btn-sm" onclick="clientCardsManager.saveDrawerToken('${data.client_id}')" style="background: var(--accent-green, #10b981); color: white; font-weight: 600; cursor: pointer; padding: 0.5rem 0.85rem; border-radius: 6px; border: none;">
                                    💾 Save / Update Token
                                </button>
                                <button type="button" id="drawer-btn-generate-token" class="action-btn-sm" onclick="clientCardsManager.generateXtsTokenNow('${data.client_id}')" style="background: var(--accent-blue, #2563eb); color: white; font-weight: 600; cursor: pointer; padding: 0.5rem 0.85rem; border-radius: 6px; border: none; display: flex; align-items: center; gap: 4px;">
                                    🔑 Generate Token Now
                                </button>
                            </div>
                            <div id="drawer-xts-token-feedback" style="font-size: 0.75rem; color: var(--text-muted); line-height: 1.3;"></div>
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

window.openAddClientModal = function() {
    if (window.clientCardsManager) {
        window.clientCardsManager.openAddClientModal();
    } else {
        const modal = document.getElementById("modal-add-client");
        if (modal) {
            modal.classList.add("active");
            modal.style.display = "";
        }
    }
};

window.closeAddClientModal = function() {
    if (window.clientCardsManager) {
        window.clientCardsManager.closeAddClientModal();
    } else {
        const modal = document.getElementById("modal-add-client");
        if (modal) {
            modal.classList.remove("active");
            modal.style.display = "";
        }
    }
};
