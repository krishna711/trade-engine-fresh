/**
 * Strategies Management Module
 * Supports 3 flexible view modes:
 *  1. 'by-strategy' : Consolidated view per strategy with client badges
 *  2. 'individual'  : Individual row per (Client Account, Strategy) pair with account-specific PnL
 *  3. 'by-client'   : Grouped cards per client account listing their mapped strategies
 *
 * Also handles Re-mapping clients, Charting, Trades, Logs, Duplication, and Admin Templates.
 */

const STRAT_ICONS = {
    start: `<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>`,
    stop: `<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>`,
    squareoff: `<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>`,
    remap: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>`,
    edit: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>`,
    chart: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>`,
    trades: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><circle cx="4" cy="6" r="1.5" fill="currentColor"/><circle cx="4" cy="12" r="1.5" fill="currentColor"/><circle cx="4" cy="18" r="1.5" fill="currentColor"/></svg>`,
    log: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>`,
    dup: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`,
    del: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>`,
    unmap: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18.8 4L4 18.8M4 4l14.8 14.8"/></svg>`
};

class StrategiesManager {
    constructor() {
        this.instancesTableBody = document.getElementById("strategy-instances-tbody");
        this.tableCard = document.getElementById("strategy-table-card");
        this.byClientContainer = document.getElementById("strategy-by-client-container");
        this.templatesTableBody = document.getElementById("strategy-templates-tbody");
        this.chartInstance = null;
        this.currentDuplicateSourceId = null;
        this.currentRemapStrategyId = null;
        this.latestInstances = [];
        this.latestAccounts = [];

        this.deployModalIsPaper = true;
        this.editModalIsPaper = true;
        this.dupModalIsPaper = true;

        this.viewMode = localStorage.getItem("fresh_trade_strat_view_mode") || "by-strategy";

        this.initEventListeners();
    }

    initEventListeners() {
        // Modal close buttons
        document.querySelectorAll(".modal-close-btn").forEach(btn => {
            btn.onclick = (e) => {
                e.stopPropagation();
                document.querySelectorAll(".modal-overlay").forEach(m => {
                    m.classList.remove("active");
                    m.style.display = "";
                });
            };
        });

        // Click outside modal box to close
        document.querySelectorAll(".modal-overlay").forEach(overlay => {
            overlay.onclick = (e) => {
                if (e.target === overlay) {
                    overlay.classList.remove("active");
                    overlay.style.display = "";
                }
            };
        });

        // View mode selector buttons
        const modeButtons = document.querySelectorAll(".view-mode-btn");
        modeButtons.forEach(btn => {
            btn.onclick = () => {
                const mode = btn.dataset.mode;
                this.switchViewMode(mode);
            };
        });
        this.updateViewModeButtons();

        // Submit duplicate
        const btnSubDup = document.getElementById("btn-submit-duplicate");
        if (btnSubDup) btnSubDup.onclick = () => this.submitDuplicate();

        // Submit remap clients
        const btnSubRemap = document.getElementById("btn-submit-remap-clients");
        if (btnSubRemap) btnSubRemap.onclick = () => this.submitRemapClients();

        const btnDeploy = document.getElementById("btn-deploy-new-strategy");
        if (btnDeploy) btnDeploy.onclick = () => {
            this.openDeployModal();
        };
    }

    switchViewMode(mode) {
        this.viewMode = mode;
        localStorage.setItem("fresh_trade_strat_view_mode", mode);
        this.updateViewModeButtons();
        if (this.latestInstances) {
            this.renderInstances(this.latestInstances);
        }
    }

    updateViewModeButtons() {
        document.querySelectorAll(".view-mode-btn").forEach(btn => {
            if (btn.dataset.mode === this.viewMode) {
                btn.classList.add("active");
            } else {
                btn.classList.remove("active");
            }
        });
    }

    async loadInstances() {
        try {
            const [resInst, resAcc] = await Promise.all([
                fetch("/api/strategies/instances"),
                fetch("/api/accounts")
            ]);
            this.latestInstances = await resInst.json();
            this.latestAccounts = await resAcc.json();
            this.renderInstances(this.latestInstances);
        } catch (e) {
            console.error("Failed to load strategy instances:", e);
        }
    }

    renderInstances(instances) {
        this.latestInstances = instances;
        if (this.viewMode === "by-client") {
            this.renderByClient(instances);
        } else if (this.viewMode === "individual") {
            this.renderIndividual(instances);
        } else {
            this.renderByStrategy(instances);
        }
    }

    // =========================================================================
    // MODE 1: GROUP BY STRATEGY (Consolidated View)
    // =========================================================================
    renderByStrategy(instances) {
        if (this.tableCard) this.tableCard.style.display = "block";
        if (this.byClientContainer) this.byClientContainer.style.display = "none";
        if (!this.instancesTableBody) return;

        // Update table headers
        const thCol1 = document.getElementById("th-col-1");
        const thPnlCol = document.getElementById("th-pnl-col");
        if (thCol1) thCol1.innerText = "Mapped Accounts";
        if (thPnlCol) thPnlCol.innerText = "Consolidated PNL & Legs";

        const isAdmin = window.app ? window.app.currentRole === "ADMIN" : true;

        if (!instances || instances.length === 0) {
            this.instancesTableBody.innerHTML = `
                <tr><td colspan="8" style="text-align: center; color: var(--text-muted); padding: 2rem;">No active strategy instances configured.</td></tr>
            `;
            return;
        }

        this.instancesTableBody.innerHTML = instances.map(inst => {
            const isPos = inst.pnl.total >= 0;
            const sign = isPos ? "+" : "";
            const pnlCls = isPos ? "pnl-positive" : "pnl-negative";
            const isRunning = inst.status === "RUNNING";

            // Multi-client display with interactive pills
            const clientsHtml = (inst.mapped_clients && inst.mapped_clients.length) ? inst.mapped_clients.map(c => `
                <span class="status-pill status-${c.is_active ? 'running' : 'stopped'}" style="margin-right: 4px; margin-bottom: 2px;" title="${c.broker} &bull; ${c.client_id} &bull; ${c.paper_mode ? 'Paper' : 'Live'}">
                    <span class="status-dot"></span> <strong>${c.name}</strong>
                    <span style="font-size: 0.68rem; opacity: 0.8; margin-left: 3px;">(${c.broker})</span>
                </span>
            `).join("") : `<span style="font-size: 0.75rem; color: var(--accent-amber);">⚠️ No accounts mapped</span>`;

            // Multi-leg prices of last trade
            const legsHtml = (inst.legs_info && inst.legs_info.length) ? `
                <div class="legs-display">
                    ${inst.legs_info.map(leg => `
                        <span class="leg-pill">
                            <span class="leg-name">${leg.leg}</span>
                            <span>Entry: ₹${Number(leg.entry || 0).toFixed(2)}</span>
                            <span>Exit: ${leg.exit ? '₹' + Number(leg.exit).toFixed(2) : (leg.status === 'CLOSED' || inst.last_trade_status === 'CLOSED' ? '₹' + Number(leg.ltp || 0).toFixed(2) : '<em style="color:var(--accent-amber)">OPEN</em>')}</span>
                            <span>LTP: ₹${Number(leg.ltp || 0).toFixed(2)}</span>
                        </span>
                    `).join("")}
                </div>
            ` : '<div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 2px;">No active trade legs</div>';

            const adminNameEsc = inst.name_admin.replace(/'/g, "\\'");
            const userNameEsc = inst.name_user.replace(/'/g, "\\'");

            return `
                <tr id="strat-row-${inst.id}">
                    <td>
                        <div style="display: flex; flex-direction: column; gap: 4px; max-width: 280px;">
                            <div style="display: flex; flex-wrap: wrap; gap: 3px;">
                                ${clientsHtml}
                            </div>
                            <div>
                                <button class="action-btn-sm" onclick="strategiesManager.openRemapClientsModal(${inst.id}, '${userNameEsc}')" 
                                        style="font-size: 0.7rem; padding: 2px 7px; background: rgba(59, 130, 246, 0.12); color: var(--accent-blue); border: 1px solid rgba(59, 130, 246, 0.25); border-radius: 4px; cursor: pointer; margin-top: 3px;">
                                    👥 Re-map Accounts
                                </button>
                            </div>
                        </div>
                    </td>
                    <td>
                        <strong>${isAdmin ? inst.name_admin : inst.name_user}</strong>
                        ${isAdmin ? `<div style="font-size: 0.7rem; color: var(--text-muted); font-family: var(--font-mono);">${inst.template_key}</div>` : ''}
                    </td>
                    <td>
                        <div style="display: flex; flex-direction: column; gap: 4px; align-items: flex-start;">
                            <span class="status-pill status-${isRunning ? 'running' : 'stopped'}">
                                <span class="status-dot"></span> ${inst.status}
                            </span>
                            <div class="mode-pill-group" style="margin-top: 2px;" title="${inst.paper_mode ? 'Strategy is set to PAPER mode' : 'Strategy is set to LIVE mode (requires live account)'}">
                                <button class="mode-pill ${inst.paper_mode ? 'active' : ''}" 
                                        onclick="strategiesManager.setMode(${inst.id}, true); event.stopPropagation();">PAPER</button>
                                <button class="mode-pill ${!inst.paper_mode ? 'active live' : ''}" 
                                        onclick="strategiesManager.setMode(${inst.id}, false); event.stopPropagation();">LIVE</button>
                            </div>
                        </div>
                    </td>
                    <td>
                        <span class="status-pill status-${inst.last_trade_status === 'OPEN' ? 'running' : 'stopped'}">
                            ${inst.last_trade_status}
                        </span>
                    </td>
                    <td style="font-family: var(--font-mono); font-size: 0.8rem;">${inst.start_time}</td>
                    <td style="font-family: var(--font-mono); font-size: 0.8rem;">${inst.end_time}</td>
                    <td>
                        <div>
                            <span class="card-pnl-title">U: </span><span class="${inst.pnl.unrealized >= 0 ? 'pnl-positive' : 'pnl-negative'}" style="font-family: var(--font-mono); font-weight: 600;">${inst.pnl.unrealized >= 0 ? '+' : ''}₹${inst.pnl.unrealized.toFixed(2)}</span> |
                            <span class="card-pnl-title">R: </span><span class="${inst.pnl.realized >= 0 ? 'pnl-positive' : 'pnl-negative'}" style="font-family: var(--font-mono); font-weight: 600;">${inst.pnl.realized >= 0 ? '+' : ''}₹${inst.pnl.realized.toFixed(2)}</span> |
                            <strong class="${pnlCls}" style="font-family: var(--font-mono);">${sign}₹${inst.pnl.total.toFixed(2)}</strong>
                        </div>
                        ${legsHtml}
                    </td>
                    <td style="text-align: right;">
                        <div class="table-actions" style="justify-content: flex-end; gap: 0.35rem;">
                            <button class="action-btn-icon ${isRunning ? 'btn-action-stop' : 'btn-action-start'}" onclick="strategiesManager.toggleStartStop(${inst.id}, ${isRunning})" title="${isRunning ? 'Stop Strategy' : 'Start Strategy'}">
                                ${isRunning ? STRAT_ICONS.stop : STRAT_ICONS.start}
                            </button>
                            <button class="action-btn-icon btn-action-squareoff" onclick="strategiesManager.squareOff(${inst.id})" title="Emergency Square Off Strategy">
                                ${STRAT_ICONS.squareoff}
                            </button>
                            <button class="action-btn-icon btn-action-remap" onclick="strategiesManager.openRemapClientsModal(${inst.id}, '${userNameEsc}')" title="Re-map Accounts">
                                ${STRAT_ICONS.remap}
                            </button>
                            <button class="action-btn-icon btn-action-edit" onclick="strategiesManager.openEditModal(${inst.id})" title="Edit Strategy Parameters">
                                ${STRAT_ICONS.edit}
                            </button>
                            <button class="action-btn-icon btn-action-chart" onclick="strategiesManager.openChart(${inst.id}, '${userNameEsc}')" title="Performance Chart">
                                ${STRAT_ICONS.chart}
                            </button>
                            <button class="action-btn-icon btn-action-trades" onclick="strategiesManager.openTrades(${inst.id}, '${userNameEsc}')" title="Recent Trades List (All Legs)">
                                ${STRAT_ICONS.trades}
                            </button>
                            <button class="action-btn-icon btn-action-log" onclick="strategiesManager.openLogs(${inst.id}, '${userNameEsc}')" title="Chronological Decision Audit Log">
                                ${STRAT_ICONS.log}
                            </button>
                            <button class="action-btn-icon btn-action-dup" onclick="strategiesManager.openDuplicateModal(${inst.id}, '${adminNameEsc}', '${userNameEsc}', '${JSON.stringify(inst.config).replace(/"/g, '&quot;')}')" title="Duplicate Strategy">
                                ${STRAT_ICONS.dup}
                            </button>
                            <button class="action-btn-icon btn-action-delete" onclick="strategiesManager.deleteStrategy(${inst.id}, '${userNameEsc}')" title="Delete Strategy">
                                ${STRAT_ICONS.del}
                            </button>
                        </div>
                    </td>
                </tr>
            `;
        }).join("");
    }

    // =========================================================================
    // MODE 2: INDIVIDUAL ROWS (Account + Strategy with Account PnL)
    // =========================================================================
    renderIndividual(instances) {
        if (this.tableCard) this.tableCard.style.display = "block";
        if (this.byClientContainer) this.byClientContainer.style.display = "none";
        if (!this.instancesTableBody) return;

        // Update table headers
        const thCol1 = document.getElementById("th-col-1");
        const thPnlCol = document.getElementById("th-pnl-col");
        if (thCol1) thCol1.innerText = "Client Account";
        if (thPnlCol) thPnlCol.innerText = "Account PNL & Details";

        const isAdmin = window.app ? window.app.currentRole === "ADMIN" : true;

        if (!instances || instances.length === 0) {
            this.instancesTableBody.innerHTML = `
                <tr><td colspan="8" style="text-align: center; color: var(--text-muted); padding: 2rem;">No active strategy instances configured.</td></tr>
            `;
            return;
        }

        // Explode instances into individual rows per mapped client
        const rows = [];
        for (const inst of instances) {
            const isRunning = inst.status === "RUNNING";
            const adminNameEsc = inst.name_admin.replace(/'/g, "\\'");
            const userNameEsc = inst.name_user.replace(/'/g, "\\'");

            if (!inst.mapped_clients || inst.mapped_clients.length === 0) {
                // Strategy has no client mapped
                rows.push(`
                    <tr id="strat-indiv-${inst.id}-unmapped">
                        <td>
                            <div style="display: flex; align-items: center; gap: 0.5rem;">
                                <span class="status-pill status-stopped" style="color: var(--accent-amber);">⚠️ No Account</span>
                                <button class="action-btn-sm" onclick="strategiesManager.openRemapClientsModal(${inst.id}, '${userNameEsc}')" 
                                        style="font-size: 0.7rem; padding: 2px 6px; background: var(--accent-blue); color: white; border-radius: 4px;">
                                    + Map Account
                                </button>
                            </div>
                        </td>
                        <td>
                            <strong>${isAdmin ? inst.name_admin : inst.name_user}</strong>
                            ${isAdmin ? `<div style="font-size: 0.7rem; color: var(--text-muted); font-family: var(--font-mono);">${inst.template_key}</div>` : ''}
                        </td>
                        <td>
                            <div style="display: flex; flex-direction: column; gap: 4px; align-items: flex-start;">
                                <span class="status-pill status-${isRunning ? 'running' : 'stopped'}">
                                    <span class="status-dot"></span> ${inst.status}
                                </span>
                                <div class="mode-pill-group" style="margin-top: 2px;" title="${inst.paper_mode ? 'Strategy is set to PAPER mode' : 'Strategy is set to LIVE mode (requires live account)'}">
                                    <button class="mode-pill ${inst.paper_mode ? 'active' : ''}" 
                                            onclick="strategiesManager.setMode(${inst.id}, true); event.stopPropagation();">PAPER</button>
                                    <button class="mode-pill ${!inst.paper_mode ? 'active live' : ''}" 
                                            onclick="strategiesManager.setMode(${inst.id}, false); event.stopPropagation();">LIVE</button>
                                </div>
                            </div>
                        </td>
                        <td>
                            <span class="status-pill status-${inst.last_trade_status === 'OPEN' ? 'running' : 'stopped'}">
                                ${inst.last_trade_status}
                            </span>
                        </td>
                        <td style="font-family: var(--font-mono); font-size: 0.8rem;">${inst.start_time}</td>
                        <td style="font-family: var(--font-mono); font-size: 0.8rem;">${inst.end_time}</td>
                        <td>
                            <span style="font-family: var(--font-mono); color: var(--text-muted);">₹0.00 (Unmapped)</span>
                        </td>
                        <td style="text-align: right;">
                            <div class="table-actions" style="justify-content: flex-end; gap: 0.35rem;">
                                <button class="action-btn-icon btn-action-remap" onclick="strategiesManager.openRemapClientsModal(${inst.id}, '${userNameEsc}')" title="Map Accounts">${STRAT_ICONS.remap}</button>
                                <button class="action-btn-icon btn-action-delete" onclick="strategiesManager.deleteStrategy(${inst.id}, '${userNameEsc}')" title="Delete Strategy">${STRAT_ICONS.del}</button>
                            </div>
                        </td>
                    </tr>
                `);
                continue;
            }

            for (const client of inst.mapped_clients) {
                const brokerCls = `broker-${(client.broker || "xts").toLowerCase()}`;
                const clientPnl = client.pnl || { unrealized: 0.0, realized: 0.0, total: 0.0 };
                const isPos = clientPnl.total >= 0;
                const sign = isPos ? "+" : "";
                const pnlCls = isPos ? "pnl-positive" : "pnl-negative";
                const clientNameEsc = client.name.replace(/'/g, "\\'");

                rows.push(`
                    <tr id="strat-indiv-${inst.id}-${client.client_id}">
                        <td>
                            <div style="display: flex; flex-direction: column; gap: 2px;">
                                <div style="display: flex; align-items: center; gap: 6px;">
                                    <span class="broker-tag ${brokerCls}">${client.broker}</span>
                                    <strong style="font-size: 0.85rem;">${client.name}</strong>
                                    <span class="mode-tag ${client.paper_mode ? 'mode-tag-paper' : 'mode-tag-live'}" style="font-size: 0.65rem; padding: 1px 4px; border-radius: 4px;">
                                        ${client.paper_mode ? 'PAPER' : 'LIVE'}
                                    </span>
                                </div>
                                <span style="font-family: var(--font-mono); font-size: 0.72rem; color: var(--text-muted);">${client.client_id}</span>
                            </div>
                        </td>
                        <td>
                            <strong>${isAdmin ? inst.name_admin : inst.name_user}</strong>
                            ${isAdmin ? `<div style="font-size: 0.7rem; color: var(--text-muted); font-family: var(--font-mono);">${inst.template_key}</div>` : ''}
                        </td>
                        <td>
                            <div style="display: flex; flex-direction: column; gap: 4px; align-items: flex-start;">
                                <span class="status-pill status-${isRunning ? 'running' : 'stopped'}">
                                    <span class="status-dot"></span> ${inst.status}
                                </span>
                                <div class="mode-pill-group" style="margin-top: 2px;" title="${inst.paper_mode ? 'Strategy is set to PAPER mode' : 'Strategy is set to LIVE mode (requires live account)'}">
                                    <button class="mode-pill ${inst.paper_mode ? 'active' : ''}" 
                                            onclick="strategiesManager.setMode(${inst.id}, true); event.stopPropagation();">PAPER</button>
                                    <button class="mode-pill ${!inst.paper_mode ? 'active live' : ''}" 
                                            onclick="strategiesManager.setMode(${inst.id}, false); event.stopPropagation();">LIVE</button>
                                </div>
                            </div>
                        </td>
                        <td>
                            <span class="status-pill status-${inst.last_trade_status === 'OPEN' ? 'running' : 'stopped'}">
                                ${inst.last_trade_status}
                            </span>
                        </td>
                        <td style="font-family: var(--font-mono); font-size: 0.8rem;">${inst.start_time}</td>
                        <td style="font-family: var(--font-mono); font-size: 0.8rem;">${inst.end_time}</td>
                        <td>
                            <div>
                                <span class="card-pnl-title">U: </span><span class="${clientPnl.unrealized >= 0 ? 'pnl-positive' : 'pnl-negative'}" style="font-family: var(--font-mono); font-weight: 600;">${clientPnl.unrealized >= 0 ? '+' : ''}₹${clientPnl.unrealized.toFixed(2)}</span> |
                                <span class="card-pnl-title">R: </span><span class="${clientPnl.realized >= 0 ? 'pnl-positive' : 'pnl-negative'}" style="font-family: var(--font-mono); font-weight: 600;">${clientPnl.realized >= 0 ? '+' : ''}₹${clientPnl.realized.toFixed(2)}</span> |
                                <strong class="${pnlCls}" style="font-family: var(--font-mono);">${sign}₹${clientPnl.total.toFixed(2)}</strong>
                            </div>
                            <div style="font-size: 0.72rem; color: var(--text-muted); margin-top: 2px;">
                                Individual Account PnL
                            </div>
                        </td>
                        <td style="text-align: right;">
                            <div class="table-actions" style="justify-content: flex-end; gap: 0.35rem;">
                                <button class="action-btn-icon ${isRunning ? 'btn-action-stop' : 'btn-action-start'}" onclick="strategiesManager.toggleStartStop(${inst.id}, ${isRunning})" title="${isRunning ? 'Stop Strategy' : 'Start Strategy'}">
                                    ${isRunning ? STRAT_ICONS.stop : STRAT_ICONS.start}
                                </button>
                                <button class="action-btn-icon btn-action-squareoff" onclick="strategiesManager.squareOffAccountStrategy('${client.client_id}', ${inst.id})" title="Emergency Square Off positions for ${clientNameEsc} on this strategy">
                                    ${STRAT_ICONS.squareoff}
                                </button>
                                <button class="action-btn-icon btn-action-unmap" onclick="strategiesManager.unmapClient(${inst.id}, '${client.client_id}', '${clientNameEsc}')" title="Unmap ${clientNameEsc} from this strategy">
                                    ${STRAT_ICONS.unmap}
                                </button>
                                <button class="action-btn-icon btn-action-edit" onclick="strategiesManager.openEditModal(${inst.id})" title="Edit Strategy Parameters">
                                    ${STRAT_ICONS.edit}
                                </button>
                                <button class="action-btn-icon btn-action-chart" onclick="strategiesManager.openChart(${inst.id}, '${userNameEsc}')" title="Performance Chart">
                                    ${STRAT_ICONS.chart}
                                </button>
                                <button class="action-btn-icon btn-action-log" onclick="strategiesManager.openLogs(${inst.id}, '${userNameEsc}')" title="Chronological Decision Audit Log">
                                    ${STRAT_ICONS.log}
                                </button>
                            </div>
                        </td>
                    </tr>
                `);
            }
        }

        this.instancesTableBody.innerHTML = rows.join("");
    }

    // =========================================================================
    // MODE 3: GROUP BY CLIENT (Client Header Cards with Strategies Underneath)
    // =========================================================================
    renderByClient(instances) {
        if (this.tableCard) this.tableCard.style.display = "none";
        if (this.byClientContainer) this.byClientContainer.style.display = "flex";
        if (!this.byClientContainer) return;

        const isAdmin = window.app ? window.app.currentRole === "ADMIN" : true;
        const accounts = this.latestAccounts || [];

        if (!accounts || accounts.length === 0) {
            this.byClientContainer.innerHTML = `
                <div style="padding: 2.5rem; text-align: center; background: var(--bg-card); border-radius: 8px; border: 1px dashed var(--border-color); color: var(--text-muted);">
                    <div style="font-size: 1.5rem; margin-bottom: 0.5rem;">👥</div>
                    <div>No client accounts found.</div>
                    <div style="font-size: 0.8rem; margin-top: 0.25rem;">Click <strong>+ Add Client Account</strong> above to map real trading accounts.</div>
                </div>
            `;
            return;
        }

        this.byClientContainer.innerHTML = accounts.map(acc => {
            const brokerCls = `broker-${(acc.broker || "xts").toLowerCase()}`;
            const clientNameEsc = acc.client_name.replace(/'/g, "\\'");

            // Find all strategies mapped to this account
            const mappedStrats = instances.filter(inst => (inst.client_ids || []).includes(acc.client_id));

            let clientTotalU = 0;
            let clientTotalR = 0;

            const stratsRows = mappedStrats.map(inst => {
                const clientEntry = (inst.mapped_clients || []).find(c => c.client_id === acc.client_id);
                const pnl = clientEntry?.pnl || { unrealized: 0, realized: 0, total: 0 };
                clientTotalU += pnl.unrealized;
                clientTotalR += pnl.realized;

                const isPos = pnl.total >= 0;
                const sign = isPos ? "+" : "";
                const pnlCls = isPos ? "pnl-positive" : "pnl-negative";
                const isRunning = inst.status === "RUNNING";
                const userNameEsc = inst.name_user.replace(/'/g, "\\'");

                return `
                    <tr style="border-bottom: 1px solid rgba(255, 255, 255, 0.05);">
                        <td style="padding: 0.6rem 0.75rem;">
                            <strong>${isAdmin ? inst.name_admin : inst.name_user}</strong>
                            ${isAdmin ? `<div style="font-size: 0.68rem; color: var(--text-muted); font-family: var(--font-mono);">${inst.template_key}</div>` : ''}
                        </td>
                        <td style="padding: 0.6rem 0.75rem;">
                            <div style="display: flex; flex-direction: column; gap: 4px; align-items: flex-start;">
                                <span class="status-pill status-${isRunning ? 'running' : 'stopped'}">
                                    <span class="status-dot"></span> ${inst.status}
                                </span>
                                <div class="mode-pill-group" style="margin-top: 2px;" title="${inst.paper_mode ? 'Strategy is set to PAPER mode' : 'Strategy is set to LIVE mode (requires live account)'}">
                                    <button class="mode-pill ${inst.paper_mode ? 'active' : ''}" 
                                            onclick="strategiesManager.setMode(${inst.id}, true); event.stopPropagation();">PAPER</button>
                                    <button class="mode-pill ${!inst.paper_mode ? 'active live' : ''}" 
                                            onclick="strategiesManager.setMode(${inst.id}, false); event.stopPropagation();">LIVE</button>
                                </div>
                            </div>
                        </td>
                        <td style="padding: 0.6rem 0.75rem; font-family: var(--font-mono); font-size: 0.75rem;">
                            ${inst.start_time} - ${inst.end_time}
                        </td>
                        <td style="padding: 0.6rem 0.75rem;">
                            <div>
                                <span class="card-pnl-title">U: </span><span class="${pnl.unrealized >= 0 ? 'pnl-positive' : 'pnl-negative'}" style="font-family: var(--font-mono); font-size: 0.8rem;">${pnl.unrealized >= 0 ? '+' : ''}₹${pnl.unrealized.toFixed(2)}</span> |
                                <span class="card-pnl-title">R: </span><span class="${pnl.realized >= 0 ? 'pnl-positive' : 'pnl-negative'}" style="font-family: var(--font-mono); font-size: 0.8rem;">${pnl.realized >= 0 ? '+' : ''}₹${pnl.realized.toFixed(2)}</span> |
                                <strong class="${pnlCls}" style="font-family: var(--font-mono); font-size: 0.85rem;">${sign}₹${pnl.total.toFixed(2)}</strong>
                            </div>
                        </td>
                        <td style="padding: 0.6rem 0.75rem; text-align: right;">
                            <div class="table-actions" style="justify-content: flex-end; gap: 0.35rem;">
                                <button class="action-btn-icon ${isRunning ? 'btn-action-stop' : 'btn-action-start'}" onclick="strategiesManager.toggleStartStop(${inst.id}, ${isRunning})" title="${isRunning ? 'Stop Strategy' : 'Start Strategy'}">
                                    ${isRunning ? STRAT_ICONS.stop : STRAT_ICONS.start}
                                </button>
                                <button class="action-btn-icon btn-action-squareoff" onclick="strategiesManager.squareOffAccountStrategy('${acc.client_id}', ${inst.id})" title="Emergency Square Off positions for this strategy">
                                    ${STRAT_ICONS.squareoff}
                                </button>
                                <button class="action-btn-icon btn-action-unmap" onclick="strategiesManager.unmapClient(${inst.id}, '${acc.client_id}', '${clientNameEsc}')" title="Unmap strategy from this account">
                                    ${STRAT_ICONS.unmap}
                                </button>
                                <button class="action-btn-icon btn-action-edit" onclick="strategiesManager.openEditModal(${inst.id})" title="Edit Strategy Parameters">
                                    ${STRAT_ICONS.edit}
                                </button>
                                <button class="action-btn-icon btn-action-chart" onclick="strategiesManager.openChart(${inst.id}, '${userNameEsc}')" title="Performance Chart">
                                    ${STRAT_ICONS.chart}
                                </button>
                                <button class="action-btn-icon btn-action-log" onclick="strategiesManager.openLogs(${inst.id}, '${userNameEsc}')" title="Chronological Decision Audit Log">
                                    ${STRAT_ICONS.log}
                                </button>
                                </button>
                            </div>
                        </td>
                    </tr>
                `;
            }).join("");

            const clientTotalT = clientTotalU + clientTotalR;
            const clientTotalPos = clientTotalT >= 0;
            const clientTotalSign = clientTotalPos ? "+" : "";
            const clientTotalCls = clientTotalPos ? "pnl-positive" : "pnl-negative";

            return `
                <div class="client-group-card" style="background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 8px; overflow: hidden; box-shadow: var(--shadow-sm);">
                    <!-- Client Header Bar -->
                    <div style="background: var(--bg-secondary); padding: 0.75rem 1rem; border-bottom: 1px solid var(--border-color); display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 0.5rem;">
                        <div style="display: flex; align-items: center; gap: 0.65rem;">
                            <span class="broker-tag ${brokerCls}">${acc.broker}</span>
                            <h3 style="font-size: 0.95rem; margin: 0;">${acc.client_name}</h3>
                            <span style="font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-muted);">${acc.client_id}</span>
                            <span class="mode-tag ${acc.paper_mode ? 'mode-tag-paper' : 'mode-tag-live'}" style="font-size: 0.68rem; padding: 2px 6px; border-radius: 4px;">
                                ${acc.paper_mode ? '📄 PAPER' : '🟢 LIVE'}
                            </span>
                        </div>

                        <div style="display: flex; align-items: center; gap: 1rem;">
                            <!-- Client Total PnL across active strategies -->
                            <div style="font-size: 0.8rem; background: var(--bg-tertiary); padding: 4px 10px; border-radius: 6px; border: 1px solid var(--border-color);">
                                <span class="card-pnl-title">Account Total: </span>
                                <span class="${clientTotalU >= 0 ? 'pnl-positive' : 'pnl-negative'}" style="font-family: var(--font-mono); font-weight: 600;">U: ${clientTotalU >= 0 ? '+' : ''}₹${clientTotalU.toFixed(2)}</span> |
                                <span class="${clientTotalR >= 0 ? 'pnl-positive' : 'pnl-negative'}" style="font-family: var(--font-mono); font-weight: 600;">R: ${clientTotalR >= 0 ? '+' : ''}₹${clientTotalR.toFixed(2)}</span> |
                                <strong class="${clientTotalCls}" style="font-family: var(--font-mono); font-weight: 700;">T: ${clientTotalSign}₹${clientTotalT.toFixed(2)}</strong>
                            </div>

                            <button class="action-btn-sm" onclick="clientCardsManager.squareOff('${acc.client_id}')" 
                                    style="background: rgba(239, 68, 68, 0.15); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 6px; padding: 3px 8px; font-size: 0.75rem;">
                                ⚡ Square Off Account
                            </button>
                        </div>
                    </div>

                    <!-- Client Strategies Sub-Table -->
                    <div class="table-responsive">
                        <table class="data-table" style="margin: 0;">
                            <thead>
                                <tr style="background: rgba(0,0,0,0.15); font-size: 0.75rem;">
                                    <th>Strategy Name</th>
                                    <th>Status</th>
                                    <th>Timings</th>
                                    <th>Account PnL on this Strategy</th>
                                    <th style="text-align: right;">Action Menu</th>
                                </tr>
                            </thead>
                            <tbody>
                                ${mappedStrats.length ? stratsRows : `
                                    <tr>
                                        <td colspan="5" style="text-align: center; color: var(--text-muted); padding: 1.25rem;">
                                            No active strategies mapped to this account.
                                        </td>
                                    </tr>
                                `}
                            </tbody>
                        </table>
                    </div>
                </div>
            `;
        }).join("");
    }

    // =========================================================================
    // CLIENT RE-MAPPING & UNMAPPING HANDLERS
    // =========================================================================
    openRemapClientsModal(strategyId, strategyName) {
        this.currentRemapStrategyId = strategyId;
        const subTitle = document.getElementById("remap-strategy-subtitle");
        if (subTitle) subTitle.innerText = `Strategy: ${strategyName}`;

        const inst = this.latestInstances.find(i => i.id === strategyId);
        const currentCids = inst ? (inst.client_ids || []) : [];
        const accounts = this.latestAccounts || [];

        const listContainer = document.getElementById("remap-clients-list");
        if (!listContainer) return;

        if (!accounts || accounts.length === 0) {
            listContainer.innerHTML = `
                <div style="color: var(--accent-amber); padding: 1rem; text-align: center;">
                    No client accounts registered. Add client accounts first.
                </div>
            `;
        } else {
            listContainer.innerHTML = accounts.map(acc => {
                const isChecked = currentCids.includes(acc.client_id);
                const brokerCls = `broker-${(acc.broker || "xts").toLowerCase()}`;
                return `
                    <label style="display: flex; align-items: center; justify-content: space-between; padding: 0.65rem 0.85rem; background: var(--bg-tertiary); border: 1px solid var(--border-color); border-radius: 6px; cursor: pointer;">
                        <div style="display: flex; align-items: center; gap: 0.65rem;">
                            <input type="checkbox" class="remap-checkbox" value="${acc.client_id}" ${isChecked ? 'checked' : ''} style="width: 16px; height: 16px; cursor: pointer;">
                            <span class="broker-tag ${brokerCls}">${acc.broker}</span>
                            <strong>${acc.client_name}</strong>
                            <span style="font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-muted);">(${acc.client_id})</span>
                        </div>
                        <span class="mode-tag ${acc.paper_mode ? 'mode-tag-paper' : 'mode-tag-live'}" style="font-size: 0.68rem; padding: 2px 6px; border-radius: 4px;">
                            ${acc.paper_mode ? 'PAPER' : 'LIVE'}
                        </span>
                    </label>
                `;
            }).join("");
        }

        const modal = document.getElementById("modal-remap-clients");
        if (modal) modal.classList.add("active");
    }

    async submitRemapClients() {
        if (!this.currentRemapStrategyId) return;
        const checkboxes = document.querySelectorAll(".remap-checkbox:checked");
        const selectedCids = Array.from(checkboxes).map(cb => cb.value);

        try {
            const res = await fetch(`/api/strategies/${this.currentRemapStrategyId}/clients`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ client_ids: selectedCids })
            });
            const data = await res.json();
            if (data.success) {
                document.getElementById("modal-remap-clients")?.classList.remove("active");
                await this.loadInstances();
                if (window.clientCardsManager) {
                    window.clientCardsManager.loadAccounts();
                }
            } else {
                alert("Failed to update mapped clients: " + (data.detail || data.error || "Unknown error"));
            }
        } catch (e) {
            alert("Error updating client mapping: " + e);
        }
    }

    async unmapClient(strategyId, clientId, clientName) {
        if (!confirm(`Unmap account "${clientName}" (${clientId}) from Strategy #${strategyId}?`)) return;

        try {
            const res = await fetch(`/api/strategies/${strategyId}/clients/${clientId}/unmap`, {
                method: "POST"
            });
            const data = await res.json();
            if (data.success) {
                await this.loadInstances();
                if (window.clientCardsManager) {
                    window.clientCardsManager.loadAccounts();
                }
            } else {
                alert("Failed to unmap client: " + (data.detail || data.error || "Unknown error"));
            }
        } catch (e) {
            alert("Error unmapping client: " + e);
        }
    }

    async squareOffAccountStrategy(clientId, strategyId) {
        if (!confirm(`Emergency square off positions for client "${clientId}" on Strategy #${strategyId}?`)) return;

        try {
            const res = await fetch(`/api/accounts/${clientId}/strategies/${strategyId}/square-off`, {
                method: "POST"
            });
            const data = await res.json();
            alert(`Square off executed: ${data.squared_positions || 0} positions closed.`);
            await this.loadInstances();
            if (window.clientCardsManager) {
                window.clientCardsManager.loadAccounts();
            }
        } catch (e) {
            alert("Square off failed: " + e);
        }
    }

    async deleteStrategy(id, name) {
        if (!confirm(`Are you sure you want to delete strategy "${name}"?`)) return;
        try {
            const res = await fetch(`/api/strategies/${id}`, { method: "DELETE" });
            const data = await res.json();
            if (data.success) {
                await this.loadInstances();
                if (window.clientCardsManager) {
                    window.clientCardsManager.loadAccounts();
                }
            } else {
                alert("Failed to delete strategy: " + (data.error || "Unknown error"));
            }
        } catch (e) {
            alert("Delete failed: " + e);
        }
    }

    async toggleStartStop(id, isCurrentlyRunning) {
        const endpoint = isCurrentlyRunning ? `/api/strategies/${id}/stop` : `/api/strategies/${id}/start`;
        try {
            const res = await fetch(endpoint, { method: "POST" });
            const data = await res.json();
            if (data.success) {
                this.loadInstances();
            }
        } catch (e) {
            alert("Action failed: " + e);
        }
    }

    async squareOff(id) {
        if (!confirm(`Are you sure you want to SQUARE OFF all open positions for Strategy #${id}?`)) return;
        try {
            const res = await fetch(`/api/strategies/${id}/square-off`, { method: "POST" });
            const data = await res.json();
            alert(`Square off completed: ${data.squared_positions} positions closed.`);
            this.loadInstances();
            if (window.clientCardsManager) window.clientCardsManager.loadAccounts();
        } catch (e) {
            alert("Square off failed: " + e);
        }
    }

    // Modal: Interactive Strategy Chart
    async openChart(id, name) {
        document.getElementById("chart-strategy-title").innerText = name || "Strategy Chart";
        document.getElementById("modal-chart").classList.add("active");
        this.currentChartTab = 'pnl';

        try {
            const res = await fetch(`/api/strategies/${id}/chart`);
            const data = await res.json();
            this.currentChartData = data;

            // Update Header Information
            const sym = data.symbol || "NSE:NIFTY";
            const symLabel = document.getElementById("chart-tab-symbol-label");
            if (symLabel) symLabel.innerText = sym;
            document.getElementById("chart-symbol-subtitle").innerText = `Symbol: ${sym} | Timeframe: ${data.resolution || '5m'} | Status: ${data.status || 'IDLE'}`;

            const badge = document.getElementById("chart-status-badge");
            if (badge) {
                const isRun = data.status === "RUNNING";
                badge.innerText = data.status || "IDLE";
                badge.style.background = isRun ? "rgba(16, 185, 129, 0.15)" : "rgba(148, 163, 184, 0.15)";
                badge.style.color = isRun ? "#10b981" : "#94a3b8";
                badge.style.border = isRun ? "1px solid rgba(16, 185, 129, 0.3)" : "1px solid rgba(148, 163, 184, 0.3)";
            }

            const pnlVal = document.getElementById("chart-pnl-values");
            if (pnlVal) {
                const uPnl = Number(data.unrealized_pnl || 0);
                const rPnl = Number(data.realized_pnl || 0);
                const tPnl = Number(data.total_pnl || 0);

                const fmt = (v) => `${v >= 0 ? '+' : '-'}₹${Math.abs(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
                const color = (v) => v > 0 ? '#10b981' : (v < 0 ? '#ef4444' : 'var(--text-muted)');

                pnlVal.innerHTML = `U: <span style="color: ${color(uPnl)};">${fmt(uPnl)}</span> | R: <span style="color: ${color(rPnl)};">${fmt(rPnl)}</span> | T: <span style="color: ${color(tPnl)}; font-weight: 700;">${fmt(tPnl)}</span>`;
            }

            this.switchChartTab('pnl');
        } catch (e) {
            console.error("Failed to load chart:", e);
        }
    }

    switchChartTab(tab) {
        this.currentChartTab = tab;
        const btnPnl = document.getElementById("chart-tab-pnl");
        const btnMarket = document.getElementById("chart-tab-market");
        if (btnPnl && btnMarket) {
            if (tab === 'pnl') {
                btnPnl.classList.add("active");
                btnMarket.classList.remove("active");
            } else {
                btnMarket.classList.add("active");
                btnPnl.classList.remove("active");
            }
        }
        this.renderChart();
    }

    renderChart() {
        if (!this.currentChartData) return;
        const data = this.currentChartData;
        const ctx = document.getElementById("strategy-chart-canvas").getContext("2d");
        if (this.chartInstance) {
            this.chartInstance.destroy();
        }

        const noTradesOverlay = document.getElementById("chart-no-trades-overlay");
        const modeDesc = document.getElementById("chart-tab-mode-desc");

        if (this.currentChartTab === 'pnl') {
            if (modeDesc) modeDesc.innerText = "Cumulative Intraday PnL (₹)";
            const pnlSeries = data.pnl_series || [];
            const hasTrades = Boolean(data.has_trades);

            if (noTradesOverlay) {
                noTradesOverlay.style.display = hasTrades ? "none" : "block";
                noTradesOverlay.innerHTML = `
                    <div style="font-size: 0.88rem; font-weight: 700; color: #60a5fa; display: flex; align-items: center; justify-content: center; gap: 6px;">
                        <span>ℹ️</span> No Trades Opened Yet Today
                    </div>
                    <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">
                        Strategy is actively monitoring market signals. Cumulative PnL is flat ₹0.00.
                    </div>
                `;
            }

            const labels = pnlSeries.map(p => p.time || "");
            const values = pnlSeries.map(p => p.pnl || 0);
            const lastVal = values.length > 0 ? values[values.length - 1] : 0;
            const lineColor = lastVal > 0 ? '#10b981' : (lastVal < 0 ? '#ef4444' : '#60a5fa');
            const bgGradient = lastVal > 0 ? 'rgba(16, 185, 129, 0.12)' : (lastVal < 0 ? 'rgba(239, 68, 68, 0.12)' : 'rgba(96, 165, 250, 0.08)');

            this.chartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [{
                        label: 'Cumulative PnL (₹)',
                        data: values,
                        borderColor: lineColor,
                        backgroundColor: bgGradient,
                        fill: true,
                        tension: 0.15,
                        pointRadius: hasTrades ? 3 : 2,
                        pointHoverRadius: 5
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {
                        x: { grid: { color: 'rgba(255,255,255,0.05)' } },
                        y: {
                            grid: { color: 'rgba(255,255,255,0.05)' },
                            ticks: {
                                callback: function(value) {
                                    return (value >= 0 ? '+₹' : '-₹') + Math.abs(value);
                                }
                            }
                        }
                    },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                label: function(context) {
                                    const val = context.parsed.y;
                                    return `PnL: ${val >= 0 ? '+' : '-'}₹${Math.abs(val).toFixed(2)}`;
                                }
                            }
                        }
                    }
                }
            });
        } else {
            // Market Price View
            const sym = data.symbol || "NSE:NIFTY";
            if (modeDesc) modeDesc.innerText = `${sym} (${data.resolution || '5m'} Close Price)`;

            const ohlc = data.ohlc || [];
            if (!ohlc || ohlc.length === 0) {
                if (noTradesOverlay) {
                    noTradesOverlay.style.display = "block";
                    noTradesOverlay.innerHTML = `
                        <div style="font-size: 0.88rem; font-weight: 700; color: #f59e0b; display: flex; align-items: center; justify-content: center; gap: 6px;">
                            <span>⚠️</span> Market Closed / Waiting for Live Feed
                        </div>
                        <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">
                            No live candle buffer recorded yet for <strong>${sym}</strong>. Candlestick bars build dynamically as ticks stream in when market is open.
                        </div>
                    `;
                }
                return;
            } else {
                if (noTradesOverlay) noTradesOverlay.style.display = "none";
            }

            const labels = ohlc.map(b => b.time ? (b.time.includes("T") ? (b.time.split("T")[1]?.slice(0, 5) || b.time) : b.time.slice(0, 5)) : "");
            const prices = ohlc.map(b => b.close);

            this.chartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [{
                        label: `${sym} Close Price`,
                        data: prices,
                        borderColor: '#3b82f6',
                        backgroundColor: 'rgba(59, 130, 246, 0.1)',
                        fill: true,
                        tension: 0.2,
                        pointRadius: 2
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {
                        x: { grid: { color: 'rgba(255,255,255,0.05)' } },
                        y: {
                            grid: { color: 'rgba(255,255,255,0.05)' },
                            ticks: {
                                callback: function(value) {
                                    return '₹' + Number(value).toLocaleString('en-IN');
                                }
                            }
                        }
                    },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                label: function(context) {
                                    return `Price: ₹${Number(context.parsed.y).toFixed(2)}`;
                                }
                            }
                        }
                    }
                }
            });
        }
    }

    // Modal: Recent Trades
    async openTrades(id, name) {
        document.getElementById("trades-strategy-title").innerText = `${name} - Recent Trades`;
        const contentEl = document.getElementById("trades-modal-content");
        contentEl.innerHTML = `<div style="text-align: center; color: var(--text-muted);">Loading trades...</div>`;
        document.getElementById("modal-trades").classList.add("active");

        try {
            const res = await fetch(`/api/strategies/${id}/trades`);
            const data = await res.json();

            let legsBanner = "";
            if (data.legs_info && data.legs_info.length) {
                legsBanner = `
                    <div style="background: var(--bg-tertiary); padding: 0.75rem; border-radius: 6px; margin-bottom: 1rem; border: 1px solid var(--border-color);">
                        <strong style="font-size: 0.8rem; color: var(--text-secondary);">Active Multi-Leg State:</strong>
                        <div class="legs-display" style="margin-top: 6px;">
                            ${data.legs_info.map(l => `
                                <span class="leg-pill">
                                    <span class="leg-name">${l.leg}</span>
                                    <span>Entry: ₹${Number(l.entry || 0).toFixed(2)}</span>
                                    <span>Exit: ${l.exit ? '₹' + Number(l.exit).toFixed(2) : (l.status === 'CLOSED' ? '₹' + Number(l.ltp || 0).toFixed(2) : 'OPEN')}</span>
                                    <span>LTP: ₹${Number(l.ltp || 0).toFixed(2)}</span>
                                    <span class="${l.pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}" style="font-weight: 700;">₹${Number(l.pnl || 0).toFixed(2)}</span>
                                </span>
                            `).join("")}
                        </div>
                    </div>
                `;
            }

            contentEl.innerHTML = `
                ${legsBanner}
                <table class="data-table">
                    <thead>
                        <tr><th>Timestamp</th><th>Symbol</th><th>Leg ID</th><th>Side</th><th>Qty</th><th>Price</th></tr>
                    </thead>
                    <tbody>
                        ${data.trades.length ? data.trades.map(t => `
                            <tr>
                                <td>${t.timestamp}</td>
                                <td>${t.symbol}</td>
                                <td><span style="font-family: var(--font-mono); font-size: 0.75rem;">${t.leg_id || 'MAIN'}</span></td>
                                <td style="font-weight: 700; color: ${t.side === 'BUY' ? 'var(--accent-green)' : 'var(--accent-red)'}">${t.side}</td>
                                <td>${t.quantity}</td>
                                <td>₹${Number(t.price || 0).toFixed(2)}</td>
                            </tr>
                        `).join("") : '<tr><td colspan="6" style="text-align: center; color: var(--text-muted);">No executed trades yet</td></tr>'}
                    </tbody>
                </table>
            `;
        } catch (e) {
            contentEl.innerHTML = `<div style="color: var(--accent-red);">Failed to load trades: ${e}</div>`;
        }
    }

    // Modal: Decision Logs Timeline
    async openLogs(id, name) {
        document.getElementById("strat-logs-title").innerText = `${name} - Chronological Decision Timeline`;
        const container = document.getElementById("strat-timeline-container");
        container.innerHTML = `<div style="color: var(--text-muted);">Loading audit timeline...</div>`;
        document.getElementById("modal-strategy-logs").classList.add("active");

        try {
            const res = await fetch(`/api/strategies/${id}/logs`);
            const logs = await res.json();

            container.innerHTML = logs.length ? logs.map(l => {
                const isSuccess = l.level === "SUCCESS";
                const isErr = l.level === "ERROR";
                const cls = isSuccess ? "success" : (isErr ? "error" : "");

                return `
                    <div class="timeline-step ${cls}">
                        <div class="timeline-dot"></div>
                        <div class="timeline-time">${l.timestamp}</div>
                        <div class="timeline-title">${l.message}</div>
                    </div>
                `;
            }).join("") : '<div style="color: var(--text-muted);">No decision logs recorded yet</div>';
        } catch (e) {
            container.innerHTML = `<div style="color: var(--accent-red);">Failed to load logs: ${e}</div>`;
        }
    }

    // Modal: Duplicate Strategy
    openDuplicateModal(id, adminName, userName, configStr) {
        this.currentDuplicateSourceId = id;
        document.getElementById("dup-name-admin").value = `${adminName} (Clone)`;
        document.getElementById("dup-name-user").value = `${userName} (Copy)`;
        const srcInst = (this.latestInstances || []).find(i => i.id === id);
        this.setDupModalMode(srcInst ? srcInst.paper_mode !== false : true);
        try {
            const parsed = JSON.parse(configStr);
            document.getElementById("dup-config-json").value = JSON.stringify(parsed, null, 2);
            if (parsed.symbol) document.getElementById("dup-symbol").value = parsed.symbol;
        } catch (e) {
            document.getElementById("dup-config-json").value = "{}";
        }
        document.getElementById("modal-duplicate").classList.add("active");
    }

    setDupModalMode(isPaper) {
        this.dupModalIsPaper = !!isPaper;
        const btnPaper = document.getElementById("dup-mode-paper");
        const btnLive = document.getElementById("dup-mode-live");
        if (btnPaper && btnLive) {
            if (this.dupModalIsPaper) {
                btnPaper.className = "mode-pill active";
                btnLive.className = "mode-pill";
            } else {
                btnPaper.className = "mode-pill";
                btnLive.className = "mode-pill active live";
            }
        }
    }

    async submitDuplicate() {
        if (!this.currentDuplicateSourceId) return;
        const nameAdmin = document.getElementById("dup-name-admin").value;
        const nameUser = document.getElementById("dup-name-user").value;
        const symbol = document.getElementById("dup-symbol").value;
        let config = {};
        try {
            config = JSON.parse(document.getElementById("dup-config-json").value);
        } catch (e) {
            alert("Invalid JSON configuration");
            return;
        }
        config.symbol = symbol;

        try {
            const res = await fetch(`/api/strategies/${this.currentDuplicateSourceId}/duplicate`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    name_admin: nameAdmin,
                    name_user: nameUser,
                    config: config,
                    paper_mode: this.dupModalIsPaper
                })
            });
            const data = await res.json();
            if (data.success) {
                alert(`Strategy successfully duplicated! New Instance ID: ${data.cloned_strategy_id}`);
                document.getElementById("modal-duplicate").classList.remove("active");
                this.loadInstances();
            }
        } catch (e) {
            alert("Failed to duplicate strategy: " + e);
        }
    }

    // Admin Strategy Templates Catalog
    async loadTemplates() {
        try {
            const res = await fetch("/api/strategies/templates");
            const templates = await res.json();
            this.renderTemplates(templates);
        } catch (e) {
            console.error("Failed to load templates:", e);
        }
    }

    renderTemplates(templates) {
        if (!this.templatesTableBody) return;
        this.templatesTableBody.innerHTML = templates.map(tpl => `
            <tr>
                <td><code style="color: var(--accent-cyan); font-weight: 700;">${tpl.key}</code></td>
                <td><strong>${tpl.name_admin}</strong></td>
                <td>${tpl.name_user}</td>
                <td><pre style="font-size: 0.7rem; max-width: 300px; overflow-x: auto; background: var(--bg-tertiary); padding: 4px; border-radius: 4px;">${JSON.stringify(tpl.default_config, null, 1)}</pre></td>
                <td>${tpl.created}</td>
                <td style="text-align: right;">
                    <button class="action-btn-sm" style="background: var(--accent-blue); color: white;" onclick="strategiesManager.deployTemplate('${tpl.key}', '${tpl.name_admin}', '${tpl.name_user}')">
                        Deploy / Map Client
                    </button>
                </td>
            </tr>
        `).join("");
    }

    deployTemplate(key, adminName, userName) {
        this.openDeployModal(key);
    }

    async openDeployModal(preselectKey = null) {
        try {
            const [resTpl, resAcc] = await Promise.all([
                fetch("/api/strategies/templates"),
                fetch("/api/accounts")
            ]);
            this.templatesList = await resTpl.json();
            const accounts = await resAcc.json();

            // Populate template select dropdown
            const selectEl = document.getElementById("deploy-template-select");
            if (selectEl) {
                selectEl.innerHTML = this.templatesList.map(t => `
                    <option value="${t.key}" ${t.key === preselectKey ? 'selected' : ''}>
                        ${t.name_admin} (${t.name_user}) [${t.key}]
                    </option>
                `).join("");
            }

            const targetKey = preselectKey || (this.templatesList[0] ? this.templatesList[0].key : "");
            if (selectEl && targetKey) {
                selectEl.value = targetKey;
            }
            this.onDeployTemplateChange(targetKey);

            // Populate clients list
            const clientsListEl = document.getElementById("deploy-clients-list");
            if (clientsListEl) {
                if (accounts.length === 0) {
                    clientsListEl.innerHTML = `
                        <div style="font-size: 0.75rem; color: var(--accent-amber); padding: 0.5rem;">
                            ⚠️ No client accounts found. Please add a client account first.
                        </div>
                    `;
                } else {
                    clientsListEl.innerHTML = accounts.map(a => {
                        const isChecked = true; // Pre-check accounts so user can deploy in 1 click
                        const brokerCls = `broker-${(a.broker || 'xts').toLowerCase()}`;
                        return `
                            <label style="display: flex; align-items: center; justify-content: space-between; padding: 6px 8px; background: var(--bg-secondary); border-radius: 6px; cursor: pointer; user-select: none; border: 1px solid var(--border-color);">
                                <div style="display: flex; align-items: center; gap: 8px;">
                                    <input type="checkbox" name="deploy_clients" value="${a.client_id}" ${isChecked ? 'checked' : ''} style="cursor: pointer; width: 15px; height: 15px;">
                                    <span class="broker-tag ${brokerCls}" style="font-size: 0.65rem;">${a.broker}</span>
                                    <strong style="font-size: 0.82rem;">${a.client_name}</strong>
                                    <code style="font-size: 0.72rem; color: var(--text-muted);">${a.client_id}</code>
                                </div>
                                <span style="font-size: 0.7rem; color: ${a.paper_mode ? 'var(--text-muted)' : 'var(--accent-green)'}; font-weight: 600;">
                                    ${a.paper_mode ? '📄 PAPER' : '🟢 LIVE'}
                                </span>
                            </label>
                        `;
                    }).join("");
                }
            }

            const modal = document.getElementById("modal-deploy-strategy");
            if (modal) {
                this.setDeployModalMode(true);
                modal.classList.add("active");
                modal.style.display = "";
            }
        } catch (e) {
            alert("Error loading deployment wizard: " + e);
        }
    }

    setDeployModalMode(isPaper) {
        this.deployModalIsPaper = !!isPaper;
        const btnPaper = document.getElementById("deploy-mode-paper");
        const btnLive = document.getElementById("deploy-mode-live");
        if (btnPaper && btnLive) {
            if (this.deployModalIsPaper) {
                btnPaper.className = "mode-pill active";
                btnLive.className = "mode-pill";
            } else {
                btnPaper.className = "mode-pill";
                btnLive.className = "mode-pill active live";
            }
        }
    }

    onDeployTemplateChange(key) {
        if (!this.templatesList) return;
        const tpl = this.templatesList.find(t => t.key === key);
        if (!tpl) return;

        const nameAdminEl = document.getElementById("deploy-name-admin");
        const nameUserEl = document.getElementById("deploy-name-user");
        const configJsonEl = document.getElementById("deploy-config-json");

        if (nameAdminEl) nameAdminEl.value = tpl.name_admin;
        if (nameUserEl) nameUserEl.value = tpl.name_user;
        if (configJsonEl) configJsonEl.value = JSON.stringify(tpl.default_config || {}, null, 2);
    }

    async submitDeployStrategy() {
        const selectEl = document.getElementById("deploy-template-select");
        const templateKey = selectEl ? selectEl.value : "";
        if (!templateKey) {
            alert("Please select a strategy template.");
            return;
        }

        const nameAdmin = document.getElementById("deploy-name-admin")?.value?.trim() || "";
        const nameUser = document.getElementById("deploy-name-user")?.value?.trim() || "";
        const configJsonStr = document.getElementById("deploy-config-json")?.value?.trim() || "{}";
        const autoStart = document.getElementById("deploy-auto-start")?.checked || false;

        let parsedConfig = {};
        try {
            parsedConfig = JSON.parse(configJsonStr);
        } catch (e) {
            alert("Invalid Strategy Configuration JSON. Please check syntax:\n" + e);
            return;
        }

        const checkedBoxes = document.querySelectorAll("input[name='deploy_clients']:checked");
        const clientIds = Array.from(checkedBoxes).map(cb => cb.value);

        if (clientIds.length === 0) {
            if (!confirm("No client accounts selected. Deploy strategy without any mapped account?")) {
                return;
            }
        }

        const submitBtn = document.getElementById("btn-submit-deploy-strategy");
        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.innerText = "⏳ Deploying...";
        }

        try {
            const res = await fetch("/api/strategies/deploy", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    template_key: templateKey,
                    name_admin: nameAdmin,
                    name_user: nameUser,
                    client_ids: clientIds,
                    config: parsedConfig,
                    auto_start: autoStart,
                    paper_mode: this.deployModalIsPaper
                })
            });

            const data = await res.json();
            if (data.success) {
                const modal = document.getElementById("modal-deploy-strategy");
                if (modal) modal.classList.remove("active");

                if (window.app) window.app.switchView("dashboard");
                await this.loadInstances();
                if (window.clientCardsManager) await window.clientCardsManager.loadAccounts();

                alert(`🎉 Strategy successfully deployed!\n\nStrategy: ${nameUser}\nMapped to Account: ${clientIds.join(", ") || "None"}\nStatus: ${data.status}`);
            } else {
                alert("Failed to deploy strategy: " + (data.detail || data.error));
            }
        } catch (e) {
            alert("Network error deploying strategy: " + e);
        } finally {
            if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.innerText = "🚀 Deploy Strategy Instance";
            }
        }
    }

    async openEditModal(id) {
        let inst = (this.latestInstances || []).find(i => i.id === id);
        if (!inst) {
            try {
                const res = await fetch("/api/strategies/instances");
                const all = await res.json();
                this.latestInstances = all;
                inst = all.find(i => i.id === id);
            } catch (e) {
                alert("Error loading strategy details: " + e);
                return;
            }
        }
        if (!inst) {
            alert("Strategy instance not found.");
            return;
        }

        const idEl = document.getElementById("edit-strat-id");
        const titleEl = document.getElementById("edit-strat-title");
        const subtitleEl = document.getElementById("edit-strat-subtitle");
        const adminNameEl = document.getElementById("edit-strat-name-admin");
        const userNameEl = document.getElementById("edit-strat-name-user");
        const startTimeEl = document.getElementById("edit-strat-start-time");
        const endTimeEl = document.getElementById("edit-strat-end-time");
        const configJsonEl = document.getElementById("edit-strat-config-json");
        const pillEl = document.getElementById("edit-strat-status-pill");

        if (idEl) idEl.value = inst.id;
        if (titleEl) titleEl.innerText = `Edit: ${inst.name_user || inst.name_admin}`;
        if (subtitleEl) subtitleEl.innerText = `Instance #${inst.id} • Template: ${inst.template_key}`;
        if (adminNameEl) adminNameEl.value = inst.name_admin || "";
        if (userNameEl) userNameEl.value = inst.name_user || "";
        if (startTimeEl) startTimeEl.value = inst.start_time || "";
        if (endTimeEl) endTimeEl.value = inst.end_time || "";
        if (configJsonEl) configJsonEl.value = JSON.stringify(inst.config || {}, null, 2);

        if (pillEl) {
            const isRunning = inst.status === "RUNNING";
            pillEl.className = `status-pill status-${isRunning ? 'running' : 'stopped'}`;
            pillEl.innerHTML = `<span class="status-dot"></span> ${inst.status}`;
        }

        const modal = document.getElementById("modal-edit-strategy");
        if (modal) {
            this.setEditModalMode(inst.paper_mode !== false);
            modal.classList.add("active");
            modal.style.display = "";
        }
    }

    setEditModalMode(isPaper) {
        this.editModalIsPaper = !!isPaper;
        const btnPaper = document.getElementById("edit-mode-paper");
        const btnLive = document.getElementById("edit-mode-live");
        if (btnPaper && btnLive) {
            if (this.editModalIsPaper) {
                btnPaper.className = "mode-pill active";
                btnLive.className = "mode-pill";
            } else {
                btnPaper.className = "mode-pill";
                btnLive.className = "mode-pill active live";
            }
        }
    }

    async submitEditStrategy() {
        const id = document.getElementById("edit-strat-id")?.value;
        if (!id) return;

        const nameAdmin = document.getElementById("edit-strat-name-admin")?.value?.trim() || "";
        const nameUser = document.getElementById("edit-strat-name-user")?.value?.trim() || "";
        const startTime = document.getElementById("edit-strat-start-time")?.value?.trim() || "";
        const endTime = document.getElementById("edit-strat-end-time")?.value?.trim() || "";
        const configJsonStr = document.getElementById("edit-strat-config-json")?.value?.trim() || "{}";

        let parsedConfig = {};
        try {
            parsedConfig = JSON.parse(configJsonStr);
        } catch (e) {
            alert("Invalid Configuration JSON. Please check syntax:\n" + e);
            return;
        }

        if (startTime) {
            parsedConfig.start_time = startTime;
            parsedConfig.entry_time = startTime;
        }
        if (endTime) {
            parsedConfig.end_time = endTime;
            parsedConfig.exit_time = endTime;
        }

        const submitBtn = document.getElementById("btn-submit-edit-strategy");
        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.innerText = "⏳ Saving...";
        }

        try {
            const res = await fetch(`/api/strategies/${id}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    name_admin: nameAdmin,
                    name_user: nameUser,
                    start_time: startTime,
                    end_time: endTime,
                    config: parsedConfig,
                    paper_mode: this.editModalIsPaper
                })
            });

            const data = await res.json();
            if (data.success) {
                const modal = document.getElementById("modal-edit-strategy");
                if (modal) modal.classList.remove("active");

                await this.loadInstances();
                if (window.clientCardsManager) await window.clientCardsManager.loadAccounts();

                const hotMsg = data.hot_reloaded 
                    ? "\n⚡ Parameters updated live in the running container process on the fly!" 
                    : "\nParameters saved in database.";
                alert(`✓ Strategy #${id} updated successfully!${hotMsg}`);
            } else {
                alert("Failed to update strategy: " + (data.detail || data.error));
            }
        } catch (e) {
            alert("Network error updating strategy: " + e);
        } finally {
            if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.innerText = "💾 Save & Apply Parameters";
            }
        }
    }

    async setMode(id, isPaper) {
        try {
            const res = await fetch(`/api/strategies/${id}/set-mode`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ paper_mode: isPaper })
            });
            if (res.ok) {
                await this.loadInstances();
            } else {
                const err = await res.json();
                alert("Failed to update strategy mode: " + (err.detail || "Error"));
            }
        } catch (e) {
            console.error("Error setting strategy mode:", e);
        }
    }

    async toggleMode(id) {
        try {
            const res = await fetch(`/api/strategies/${id}/toggle-mode`, {
                method: "POST"
            });
            if (res.ok) {
                await this.loadInstances();
            } else {
                const err = await res.json();
                alert("Failed to toggle strategy mode: " + (err.detail || "Error"));
            }
        } catch (e) {
            console.error("Error toggling strategy mode:", e);
        }
    }
}

window.strategiesManager = new StrategiesManager();
