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
            const uPos = acc.unrealized_pnl >= 0;
            const rPos = acc.realized_pnl >= 0;
            const tPos = acc.total_pnl >= 0;

            const uCls = uPos ? "pos" : "neg";
            const rCls = rPos ? "pos" : "neg";
            const tCls = tPos ? "pos" : "neg";

            const formatPnl = (val) => {
                const isNeg = val < 0;
                const sign = isNeg ? "-₹" : "+₹";
                return `${sign}${Math.abs(val).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
            };

            const brokerDisplay = acc.broker ? acc.broker.toUpperCase() : "XTS";
            const isActive = Boolean(acc.is_active);

            // Owner badge if admin and owned by another user
            const ownerBadge = (window.app && window.app.currentRole === "ADMIN" && acc.owner_username)
                ? `<span class="card-owner-tag" title="Account owner: ${acc.owner_username}">👤 ${acc.owner_username}</span>`
                : '';

            // Token status indicator
            const isXts = (acc.broker || '').toUpperCase() === 'XTS';
            const tokenActive = isXts ? acc.has_token_today : true;
            const tokenTooltip = isXts
                ? (acc.has_token_today ? `XTS Session Active for Today (${acc.token_date}). Click to view.` : '⚠️ XTS Session Token required today. Click to generate.')
                : 'API Credentials & Session';

            return `
                <div class="client-card" id="card-${acc.client_id}">
                    <div class="card-top-row">
                        <div class="card-name-wrap">
                            <span class="card-title" title="${acc.client_id}">${acc.client_name || acc.client_id}</span>
                            ${ownerBadge}
                        </div>
                        <span class="card-status-badge ${isActive ? 'status-active' : 'status-inactive'}" 
                              onclick="clientCardsManager.toggleActive('${acc.client_id}')" 
                              title="Click to toggle Active/Inactive">
                            ${isActive ? 'ACTIVE' : 'INACTIVE'}
                        </span>
                    </div>

                    <div class="card-broker-text">${brokerDisplay}</div>

                    <div class="card-pnl-line">
                        <span class="pnl-chip"><span class="pnl-lbl">U:</span> <span class="pnl-val ${uCls}">${formatPnl(acc.unrealized_pnl)}</span></span>
                        <span class="pnl-chip"><span class="pnl-lbl">R:</span> <span class="pnl-val ${rCls}">${formatPnl(acc.realized_pnl)}</span></span>
                        <span class="pnl-chip"><span class="pnl-lbl">T:</span> <span class="pnl-val ${tCls}">${formatPnl(acc.total_pnl)}</span></span>
                    </div>

                    <div class="card-toolbar">
                        <div class="mode-pill-group">
                            <button class="mode-pill ${acc.paper_mode ? 'active' : ''}" 
                                    onclick="clientCardsManager.setMode('${acc.client_id}', true)" 
                                    title="Switch to Paper Trading">PAPER</button>
                            <button class="mode-pill ${!acc.paper_mode ? 'active live' : ''}" 
                                    onclick="clientCardsManager.setMode('${acc.client_id}', false)" 
                                    title="Switch to Live Broker Orders">LIVE</button>
                        </div>

                        <div class="card-action-icons">
                            <button class="btn-tool-icon ${tokenActive ? 'tool-token-active' : 'tool-token-warn'}" 
                                    onclick="clientCardsManager.openDrawer('${acc.client_id}', '${acc.client_name.replace(/'/g, "\\'")}', 'credentials')" 
                                    title="${tokenTooltip}">
                                ➔
                            </button>
                            <button class="btn-tool-icon ${isActive ? 'tool-power-active' : 'tool-power-off'}" 
                                    onclick="clientCardsManager.toggleActive('${acc.client_id}')" 
                                    title="${isActive ? 'Account Active (Click to Pause)' : 'Account Paused (Click to Activate)'}">
                                ⏻
                            </button>
                            <button class="btn-tool-icon tool-squareoff" 
                                    onclick="clientCardsManager.squareOff('${acc.client_id}')" 
                                    title="Emergency Square Off all positions">
                                ✕
                            </button>
                            <button class="btn-tool-icon tool-more" 
                                    onclick="clientCardsManager.openDrawer('${acc.client_id}', '${acc.client_name.replace(/'/g, "\\'")}', 'positions')" 
                                    title="Account Settings & Details (Positions, Orders, Margins)">
                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
                            </button>
                        </div>
                    </div>
                </div>
            `;
        }).join("");
    }

    async setMode(clientId, isPaper) {
        try {
            const res = await fetch(`/api/accounts/${clientId}/set-mode`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ paper_mode: isPaper })
            });
            const data = await res.json();
            if (data.success) {
                this.loadAccounts();
            }
        } catch (e) {
            alert("Failed to update mode: " + e);
        }
    }

    async toggleActive(clientId) {
        try {
            const res = await fetch(`/api/accounts/${clientId}/toggle-active`, { method: "POST" });
            const data = await res.json();
            if (data.success) {
                this.loadAccounts();
            }
        } catch (e) {
            alert("Failed to toggle active status: " + e);
        }
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
                await this.syncBrokerDataNow(clientId);
            } else {
                alert("Failed to update token: " + (data.detail || data.error));
            }
        } catch (e) {
            alert("Error updating token: " + e);
        }
    }

    async syncBrokerDataNow(clientId) {
        const syncBtn = document.getElementById("btn-drawer-sync");
        if (syncBtn) {
            syncBtn.disabled = true;
            syncBtn.innerText = "⏳ Syncing...";
        }
        try {
            await fetch(`/api/accounts/${clientId}/sync`, { method: "POST" });
            await this.loadDrawerDetails(clientId);
            await this.loadAccounts();
            if (syncBtn) {
                syncBtn.innerText = "✓ Synced";
                setTimeout(() => {
                    syncBtn.disabled = false;
                    syncBtn.innerText = "🔄 Sync Broker";
                }, 1500);
            }
        } catch (e) {
            console.error("Error syncing broker data:", e);
            await this.loadDrawerDetails(clientId);
            if (syncBtn) {
                syncBtn.disabled = false;
                syncBtn.innerText = "🔄 Sync Broker";
            }
        }
    }

    async loadDrawerDetails(clientId) {
        const bodyEl = document.getElementById("drawer-body-content");
        bodyEl.innerHTML = `<div style="text-align: center; color: var(--text-muted); padding: 2rem;">Loading account details...</div>`;

        try {
            const res = await fetch(`/api/accounts/${clientId}/details`);
            const data = await res.json();

            if (this.activeDrawerTab === "positions") {
                const isLive = data.credentials && data.credentials.is_valid_today && !data.paper_mode;
                bodyEl.innerHTML = `
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem; padding: 0 0.25rem;">
                        <span style="font-size: 0.75rem; color: var(--text-muted); font-weight: 600;">OPEN POSITIONS (${data.positions.length})</span>
                        ${isLive 
                            ? `<span style="font-size: 0.7rem; padding: 2px 8px; border-radius: 12px; background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3); font-weight: 700;">🟢 Live from ${data.broker}</span>`
                            : `<span style="font-size: 0.7rem; color: var(--text-muted);">${data.paper_mode ? '📄 Paper Positions' : 'Engine Positions'}</span>`}
                    </div>
                    <table class="data-table">
                        <thead>
                            <tr><th>Symbol</th><th>Seg</th><th>Prod</th><th>Net Qty</th><th>Buy Avg</th><th>Sell Avg</th><th>LTP</th><th>PnL</th></tr>
                        </thead>
                        <tbody>
                            ${data.positions.length ? data.positions.map(p => {
                                const isPos = (p.pnl || 0) >= 0;
                                const sign = isPos ? "+" : "";
                                const cls = isPos ? "pnl-positive" : "pnl-negative";
                                return `
                                    <tr>
                                        <td><strong>${p.symbol}</strong></td>
                                        <td><span style="font-size: 0.7rem; opacity: 0.8;">${p.segment || 'NSE'}</span></td>
                                        <td><span style="font-size: 0.68rem; padding: 1px 5px; background: var(--bg-tertiary); border-radius: 4px;">${p.product || 'MIS'}</span></td>
                                        <td style="font-family: var(--font-mono); font-weight: 600;">${p.net_qty}</td>
                                        <td style="font-family: var(--font-mono);">₹${Number(p.buy_avg || 0).toFixed(2)}</td>
                                        <td style="font-family: var(--font-mono);">₹${Number(p.sell_avg || 0).toFixed(2)}</td>
                                        <td style="font-family: var(--font-mono);">₹${Number(p.ltp || 0).toFixed(2)}</td>
                                        <td class="${cls}" style="font-family: var(--font-mono); font-weight: 700;">
                                            ${sign}₹${Number(p.pnl || 0).toFixed(2)}
                                        </td>
                                    </tr>
                                `;
                            }).join("") : `
                                <tr>
                                    <td colspan="8" style="text-align: center; color: var(--text-muted); padding: 2.5rem 1rem;">
                                        <div>No open positions</div>
                                        <button class="action-btn-sm" onclick="clientCardsManager.syncBrokerDataNow('${data.client_id}')" style="margin-top: 0.75rem; background: var(--bg-tertiary); color: var(--accent-blue); border: 1px solid var(--accent-blue); padding: 0.4rem 0.8rem; cursor: pointer; border-radius: 4px;">
                                            🔄 Retry Sync with Broker
                                        </button>
                                    </td>
                                </tr>
                            `}
                        </tbody>
                    </table>

                    <!-- Live Positions Diagnostics Inspector -->
                    <div style="margin-top: 1rem; border-top: 1px dashed var(--border-color); padding-top: 0.75rem;">
                        <details style="font-size: 0.75rem; color: var(--text-muted);" ${(data.positions && data.positions.length) ? '' : 'open'}>
                            <summary style="cursor: pointer; font-weight: 600; color: var(--accent-blue); padding: 4px 0; user-select: none;">
                                🔍 Broker API Response Log & Diagnostics (${data.debug_info?.positions?.attempts?.length || 0} endpoint calls attempted)
                            </summary>
                            <div style="margin-top: 6px; display: flex; flex-direction: column; gap: 6px;">
                                <div style="font-size: 0.7rem; color: var(--text-muted);">
                                    Target: <code style="color: var(--accent-cyan);">${data.credentials?.interactive_url || 'https://ttblaze.iifl.com/interactive'}</code> &bull; ClientID: <code>${data.client_id}</code> &bull; Token Active: <code>${data.credentials?.is_valid_today ? 'YES' : 'NO'}</code>
                                </div>
                                <pre style="background: var(--bg-secondary); padding: 0.75rem; border-radius: 6px; border: 1px solid var(--border-color); font-family: var(--font-mono); font-size: 0.72rem; overflow-x: auto; max-height: 250px; color: var(--text-primary); white-space: pre-wrap;">${JSON.stringify(data.debug_info?.positions || 'No debug data captured yet. Click Sync Broker above.', null, 2)}</pre>
                            </div>
                        </details>
                    </div>
                `;
            } else if (this.activeDrawerTab === "orders") {
                const isLive = data.credentials && data.credentials.is_valid_today && !data.paper_mode;
                bodyEl.innerHTML = `
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem; padding: 0 0.25rem;">
                        <span style="font-size: 0.75rem; color: var(--text-muted); font-weight: 600;">ORDER BOOK (${data.orders.length})</span>
                        ${isLive 
                            ? `<span style="font-size: 0.7rem; padding: 2px 8px; border-radius: 12px; background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3); font-weight: 700;">🟢 Live from ${data.broker}</span>`
                            : `<span style="font-size: 0.7rem; color: var(--text-muted);">${data.paper_mode ? '📄 Paper Orders' : 'Engine Orders'}</span>`}
                    </div>
                    <table class="data-table">
                        <thead>
                            <tr><th>Time</th><th>Order ID</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Status</th></tr>
                        </thead>
                        <tbody>
                            ${data.orders.length ? data.orders.map(o => {
                                const isBuy = (o.side || '').toUpperCase() === 'BUY';
                                const sideColor = isBuy ? 'var(--accent-green)' : 'var(--accent-red)';
                                const statusUpper = (o.status || '').toUpperCase();
                                const statusCls = statusUpper === 'FILLED' ? 'status-running' : statusUpper === 'REJECTED' ? 'status-error' : 'status-stopped';
                                return `
                                    <tr>
                                        <td>${o.created_at || '-'}</td>
                                        <td style="font-family: var(--font-mono); font-size: 0.72rem; opacity: 0.85;">${o.broker_order_id || o.order_id || '-'}</td>
                                        <td><strong>${o.symbol}</strong></td>
                                        <td style="font-weight: 700; color: ${sideColor};">${o.side}</td>
                                        <td style="font-family: var(--font-mono);">${o.quantity}</td>
                                        <td style="font-family: var(--font-mono);">₹${Number(o.price || 0).toFixed(2)}</td>
                                        <td><span class="status-pill ${statusCls}">${o.status}</span></td>
                                    </tr>
                                `;
                            }).join("") : `
                                <tr>
                                    <td colspan="7" style="text-align: center; color: var(--text-muted); padding: 2.5rem 1rem;">
                                        <div>No orders found</div>
                                        <button class="action-btn-sm" onclick="clientCardsManager.syncBrokerDataNow('${data.client_id}')" style="margin-top: 0.75rem; background: var(--bg-tertiary); color: var(--accent-blue); border: 1px solid var(--accent-blue); padding: 0.4rem 0.8rem; cursor: pointer; border-radius: 4px;">
                                            🔄 Retry Sync with Broker
                                        </button>
                                    </td>
                                </tr>
                            `}
                        </tbody>
                    </table>

                    <!-- Live Orders Diagnostics Inspector -->
                    <div style="margin-top: 1rem; border-top: 1px dashed var(--border-color); padding-top: 0.75rem;">
                        <details style="font-size: 0.75rem; color: var(--text-muted);" ${(data.orders && data.orders.length) ? '' : 'open'}>
                            <summary style="cursor: pointer; font-weight: 600; color: var(--accent-blue); padding: 4px 0; user-select: none;">
                                🔍 Broker API Response Log & Diagnostics (${data.debug_info?.orders?.attempts?.length || 0} endpoint calls attempted)
                            </summary>
                            <div style="margin-top: 6px; display: flex; flex-direction: column; gap: 6px;">
                                <div style="font-size: 0.7rem; color: var(--text-muted);">
                                    Target: <code style="color: var(--accent-cyan);">${data.credentials?.interactive_url || 'https://ttblaze.iifl.com/interactive'}</code> &bull; ClientID: <code>${data.client_id}</code> &bull; Token Active: <code>${data.credentials?.is_valid_today ? 'YES' : 'NO'}</code>
                                </div>
                                <pre style="background: var(--bg-secondary); padding: 0.75rem; border-radius: 6px; border: 1px solid var(--border-color); font-family: var(--font-mono); font-size: 0.72rem; overflow-x: auto; max-height: 250px; color: var(--text-primary); white-space: pre-wrap;">${JSON.stringify(data.debug_info?.orders || 'No debug data captured yet. Click Sync Broker above.', null, 2)}</pre>
                            </div>
                        </details>
                    </div>
                `;
            } else if (this.activeDrawerTab === "trades") {
                const isLive = data.credentials && data.credentials.is_valid_today && !data.paper_mode;
                bodyEl.innerHTML = `
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem; padding: 0 0.25rem;">
                        <span style="font-size: 0.75rem; color: var(--text-muted); font-weight: 600;">TRADE BOOK (${data.trades.length})</span>
                        ${isLive 
                            ? `<span style="font-size: 0.7rem; padding: 2px 8px; border-radius: 12px; background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3); font-weight: 700;">🟢 Live from ${data.broker}</span>`
                            : `<span style="font-size: 0.7rem; color: var(--text-muted);">${data.paper_mode ? '📄 Paper Trades' : 'Engine Trades'}</span>`}
                    </div>
                    <table class="data-table">
                        <thead>
                            <tr><th>Time</th><th>Trade ID</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th></tr>
                        </thead>
                        <tbody>
                            ${data.trades.length ? data.trades.map(t => {
                                const isBuy = (t.side || '').toUpperCase() === 'BUY';
                                const sideColor = isBuy ? 'var(--accent-green)' : 'var(--accent-red)';
                                return `
                                    <tr>
                                        <td>${t.timestamp || '-'}</td>
                                        <td style="font-family: var(--font-mono); font-size: 0.72rem; opacity: 0.85;">${t.trade_id || '-'}</td>
                                        <td><strong>${t.symbol}</strong></td>
                                        <td style="font-weight: 700; color: ${sideColor};">${t.side}</td>
                                        <td style="font-family: var(--font-mono);">${t.quantity}</td>
                                        <td style="font-family: var(--font-mono);">₹${Number(t.price || 0).toFixed(2)}</td>
                                    </tr>
                                `;
                            }).join("") : `
                                <tr>
                                    <td colspan="6" style="text-align: center; color: var(--text-muted); padding: 2.5rem 1rem;">
                                        <div>No executed trades</div>
                                        <button class="action-btn-sm" onclick="clientCardsManager.syncBrokerDataNow('${data.client_id}')" style="margin-top: 0.75rem; background: var(--bg-tertiary); color: var(--accent-blue); border: 1px solid var(--accent-blue); padding: 0.4rem 0.8rem; cursor: pointer; border-radius: 4px;">
                                            🔄 Retry Sync with Broker
                                        </button>
                                    </td>
                                </tr>
                            `}
                        </tbody>
                    </table>

                    <!-- Live Trades Diagnostics Inspector -->
                    <div style="margin-top: 1rem; border-top: 1px dashed var(--border-color); padding-top: 0.75rem;">
                        <details style="font-size: 0.75rem; color: var(--text-muted);" ${(data.trades && data.trades.length) ? '' : 'open'}>
                            <summary style="cursor: pointer; font-weight: 600; color: var(--accent-blue); padding: 4px 0; user-select: none;">
                                🔍 Broker API Response Log & Diagnostics (${data.debug_info?.trades?.attempts?.length || 0} endpoint calls attempted)
                            </summary>
                            <div style="margin-top: 6px; display: flex; flex-direction: column; gap: 6px;">
                                <div style="font-size: 0.7rem; color: var(--text-muted);">
                                    Target: <code style="color: var(--accent-cyan);">${data.credentials?.interactive_url || 'https://ttblaze.iifl.com/interactive'}</code> &bull; ClientID: <code>${data.client_id}</code> &bull; Token Active: <code>${data.credentials?.is_valid_today ? 'YES' : 'NO'}</code>
                                </div>
                                <pre style="background: var(--bg-secondary); padding: 0.75rem; border-radius: 6px; border: 1px solid var(--border-color); font-family: var(--font-mono); font-size: 0.72rem; overflow-x: auto; max-height: 250px; color: var(--text-primary); white-space: pre-wrap;">${JSON.stringify(data.debug_info?.trades || 'No debug data captured yet. Click Sync Broker above.', null, 2)}</pre>
                            </div>
                        </details>
                    </div>
                `;
            } else if (this.activeDrawerTab === "holdings") {
                const isLive = data.credentials && data.credentials.is_valid_today;
                const totalInvested = (data.holdings || []).reduce((sum, h) => sum + (h.invested_value || (h.buy_price * h.qty) || 0), 0);
                const totalCurrent = (data.holdings || []).reduce((sum, h) => sum + (h.market_value || (h.ltp * h.qty) || 0), 0);
                const totalPnl = (data.holdings || []).reduce((sum, h) => sum + (h.pnl || 0), 0);
                const isPos = totalPnl >= 0;
                const pnlSign = isPos ? "+" : "";
                const pnlCls = isPos ? "pnl-positive" : "pnl-negative";

                const summaryHtml = (data.holdings && data.holdings.length) ? `
                    <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.5rem; background: var(--bg-tertiary); padding: 0.65rem 0.85rem; border-radius: 6px; border: 1px solid var(--border-color); margin-bottom: 0.85rem;">
                        <div>
                            <span style="font-size: 0.68rem; color: var(--text-muted); text-transform: uppercase;">Invested</span>
                            <div style="font-family: var(--font-mono); font-weight: 700; font-size: 0.95rem; color: var(--text-primary);">₹${totalInvested.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}</div>
                        </div>
                        <div>
                            <span style="font-size: 0.68rem; color: var(--text-muted); text-transform: uppercase;">Current Value</span>
                            <div style="font-family: var(--font-mono); font-weight: 700; font-size: 0.95rem; color: var(--text-primary);">₹${totalCurrent.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}</div>
                        </div>
                        <div>
                            <span style="font-size: 0.68rem; color: var(--text-muted); text-transform: uppercase;">Net P&L</span>
                            <div class="${pnlCls}" style="font-family: var(--font-mono); font-weight: 700; font-size: 0.95rem;">${pnlSign}₹${totalPnl.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}</div>
                        </div>
                    </div>
                ` : '';

                bodyEl.innerHTML = `
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem; padding: 0 0.25rem;">
                        <span style="font-size: 0.75rem; color: var(--text-muted); font-weight: 600;">PORTFOLIO HOLDINGS (${data.holdings.length})</span>
                        ${isLive 
                            ? `<span style="font-size: 0.7rem; padding: 2px 8px; border-radius: 12px; background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3); font-weight: 700;">🟢 Live from ${data.broker}</span>`
                            : `<span style="font-size: 0.7rem; color: var(--accent-amber);">⚠️ Active token required to fetch live holdings</span>`}
                    </div>
                    ${summaryHtml}
                    <table class="data-table">
                        <thead>
                            <tr><th>Instrument</th><th>Qty</th><th>Avg Price</th><th>LTP</th><th>PnL</th></tr>
                        </thead>
                        <tbody>
                            ${data.holdings.length ? data.holdings.map(h => {
                                const isPos = (h.pnl || 0) >= 0;
                                const sign = isPos ? "+" : "";
                                const cls = isPos ? "pnl-positive" : "pnl-negative";
                                return `
                                    <tr>
                                        <td>
                                            <strong>${h.symbol}</strong>
                                            ${h.product ? `<span style="font-size: 0.65rem; opacity: 0.7; margin-left: 4px; padding: 1px 4px; background: var(--bg-secondary); border-radius: 3px;">${h.product}</span>` : ''}
                                        </td>
                                        <td style="font-family: var(--font-mono);">${h.qty}</td>
                                        <td style="font-family: var(--font-mono);">₹${Number(h.buy_price || 0).toFixed(2)}</td>
                                        <td style="font-family: var(--font-mono);">₹${Number(h.ltp || 0).toFixed(2)}</td>
                                        <td class="${cls}" style="font-family: var(--font-mono); font-weight: 700;">
                                            ${sign}₹${Number(h.pnl || 0).toFixed(2)}
                                        </td>
                                    </tr>
                                `;
                            }).join("") : `
                                <tr>
                                    <td colspan="5" style="text-align: center; color: var(--text-muted); padding: 2.5rem 1rem;">
                                        <div>No equity holdings returned by broker.</div>
                                        <button class="action-btn-sm" onclick="clientCardsManager.syncBrokerDataNow('${data.client_id}')" style="margin-top: 0.75rem; background: var(--bg-tertiary); color: var(--accent-blue); border: 1px solid var(--accent-blue); padding: 0.4rem 0.8rem; cursor: pointer; border-radius: 4px;">
                                            🔄 Retry Sync with Broker
                                        </button>
                                    </td>
                                </tr>
                            `}
                        </tbody>
                    </table>

                    <!-- Live Debug Diagnostics Inspector -->
                    <div style="margin-top: 1rem; border-top: 1px dashed var(--border-color); padding-top: 0.75rem;">
                        <details style="font-size: 0.75rem; color: var(--text-muted);" ${(data.holdings && data.holdings.length) ? '' : 'open'}>
                            <summary style="cursor: pointer; font-weight: 600; color: var(--accent-blue); padding: 4px 0; user-select: none;">
                                🔍 Broker API Response Log & Diagnostics (${data.debug_info?.holdings?.attempts?.length || 0} endpoint calls attempted)
                            </summary>
                            <div style="margin-top: 6px; display: flex; flex-direction: column; gap: 6px;">
                                <div style="font-size: 0.7rem; color: var(--text-muted);">
                                    Target: <code style="color: var(--accent-cyan);">${data.credentials?.interactive_url || 'https://ttblaze.iifl.com/interactive'}</code> &bull; ClientID: <code>${data.client_id}</code> &bull; Token Active: <code>${data.credentials?.is_valid_today ? 'YES' : 'NO'}</code>
                                </div>
                                <pre style="background: var(--bg-secondary); padding: 0.75rem; border-radius: 6px; border: 1px solid var(--border-color); font-family: var(--font-mono); font-size: 0.72rem; overflow-x: auto; max-height: 250px; color: var(--text-primary); white-space: pre-wrap;">${JSON.stringify(data.debug_info?.holdings || 'No debug data captured yet. Click Sync Broker above.', null, 2)}</pre>
                            </div>
                        </details>
                    </div>
                `;
            } else if (this.activeDrawerTab === "margins") {
                const isLive = data.credentials && data.credentials.is_valid_today;
                bodyEl.innerHTML = `
                    <div style="display: flex; flex-direction: column; gap: 1rem; padding: 0.5rem;">
                        <div style="display: flex; justify-content: space-between; align-items: center;">
                            <span style="font-size: 0.75rem; color: var(--text-muted); font-weight: 600;">MARGINS & LIMITS</span>
                            ${isLive 
                                ? `<span style="font-size: 0.7rem; padding: 2px 8px; border-radius: 12px; background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3); font-weight: 700;">🟢 Live from ${data.broker}</span>`
                                : `<span style="font-size: 0.7rem; color: var(--accent-amber);">⚪ Stored Margin (Generate Token to Sync Live)</span>`}
                        </div>
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
                        <div>
                            <button class="action-btn-sm" onclick="clientCardsManager.syncBrokerDataNow('${data.client_id}')" style="background: var(--bg-tertiary); color: var(--accent-blue); border: 1px solid var(--accent-blue); width: 100%; padding: 0.6rem; font-weight: 600; cursor: pointer; border-radius: 6px;">
                                🔄 Refresh Real-Time Margins & Holdings from ${data.broker}
                            </button>
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
