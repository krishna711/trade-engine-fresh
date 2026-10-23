/**
 * Strategies Management Module
 * Covers Dashboard Strategy List, Multi-Leg prices, Charting, Duplicate modal,
 * and the Admin-only Strategy Catalog page.
 */

class StrategiesManager {
    constructor() {
        this.instancesTableBody = document.getElementById("strategy-instances-tbody");
        this.templatesTableBody = document.getElementById("strategy-templates-tbody");
        this.chartInstance = null;
        this.currentDuplicateSourceId = null;

        this.initEventListeners();
    }

    initEventListeners() {
        // Modal close buttons
        document.querySelectorAll(".modal-close-btn").forEach(btn => {
            btn.onclick = () => {
                document.querySelectorAll(".modal-overlay").forEach(m => m.classList.remove("active"));
            };
        });

        // Submit duplicate
        const btnSubDup = document.getElementById("btn-submit-duplicate");
        if (btnSubDup) btnSubDup.onclick = () => this.submitDuplicate();

        const btnDeploy = document.getElementById("btn-deploy-new-strategy");
        if (btnDeploy) btnDeploy.onclick = () => {
            if (window.app) window.app.switchView("strategies");
        };
    }

    async loadInstances() {
        try {
            const res = await fetch("/api/strategies/instances");
            const instances = await res.json();
            this.renderInstances(instances);
        } catch (e) {
            console.error("Failed to load strategy instances:", e);
        }
    }

    renderInstances(instances) {
        if (!this.instancesTableBody) return;
        const isAdmin = window.app ? window.app.currentRole === "ADMIN" : true;

        this.instancesTableBody.innerHTML = instances.map(inst => {
            const isPos = inst.pnl.total >= 0;
            const sign = isPos ? "+" : "";
            const pnlCls = isPos ? "pnl-positive" : "pnl-negative";
            const isRunning = inst.status === "RUNNING";

            // Multi-client display
            const clientsHtml = inst.mapped_clients.map(c => `
                <span class="status-pill status-${c.is_active ? 'running' : 'stopped'}" style="margin-right: 4px; margin-bottom: 2px;">
                    <span class="status-dot"></span> ${c.name}
                </span>
            `).join("");

            // Multi-leg prices of last trade
            const legsHtml = (inst.legs_info && inst.legs_info.length) ? `
                <div class="legs-display">
                    ${inst.legs_info.map(leg => `
                        <span class="leg-pill">
                            <span class="leg-name">${leg.leg}</span>
                            <span>Entry: ₹${leg.entry.toFixed(2)}</span>
                            <span>Exit: ${leg.exit ? '₹' + leg.exit.toFixed(2) : '<em style="color:var(--accent-amber)">OPEN</em>'}</span>
                            <span>LTP: ₹${leg.ltp.toFixed(2)}</span>
                        </span>
                    `).join("")}
                </div>
            ` : '<div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 2px;">No active trade legs</div>';

            return `
                <tr id="strat-row-${inst.id}">
                    <td>
                        <div style="display: flex; flex-direction: column; gap: 4px;">
                            ${clientsHtml}
                        </div>
                    </td>
                    <td>
                        <strong>${isAdmin ? inst.name_admin : inst.name_user}</strong>
                        ${isAdmin ? `<div style="font-size: 0.7rem; color: var(--text-muted); font-family: var(--font-mono);">${inst.template_key}</div>` : ''}
                    </td>
                    <td>
                        <span class="status-pill status-${isRunning ? 'running' : 'stopped'}">
                            <span class="status-dot"></span> ${inst.status}
                        </span>
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
                                ${isRunning ? '⏹' : '▶'}
                            </button>
                            <button class="action-btn-icon btn-action-squareoff" onclick="strategiesManager.squareOff(${inst.id})" title="Emergency Square Off Strategy">
                                ⚡
                            </button>
                            <button class="action-btn-icon btn-action-chart" onclick="strategiesManager.openChart(${inst.id}, '${inst.name_user}')" title="Performance Chart">
                                📈
                            </button>
                            <button class="action-btn-icon btn-action-trades" onclick="strategiesManager.openTrades(${inst.id}, '${inst.name_user}')" title="Recent Trades List (All Legs)">
                                📋
                            </button>
                            <button class="action-btn-icon btn-action-log" onclick="strategiesManager.openLogs(${inst.id}, '${inst.name_user}')" title="Chronological Decision Audit Log">
                                📜
                            </button>
                            <button class="action-btn-icon btn-action-dup" onclick="strategiesManager.openDuplicateModal(${inst.id}, '${inst.name_admin}', '${inst.name_user}', '${JSON.stringify(inst.config).replace(/"/g, '&quot;')}')" title="Duplicate Strategy">
                                📑
                            </button>
                        </div>
                    </td>
                </tr>
            `;
        }).join("");
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
        document.getElementById("chart-strategy-title").innerText = `${name} - Intraday Performance`;
        document.getElementById("modal-chart").classList.add("active");

        try {
            const res = await fetch(`/api/strategies/${id}/chart`);
            const data = await res.json();
            document.getElementById("chart-symbol-subtitle").innerText = `Symbol: ${data.symbol} | Resolution: 5m`;

            const ctx = document.getElementById("strategy-chart-canvas").getContext("2d");
            if (this.chartInstance) {
                this.chartInstance.destroy();
            }

            const labels = data.ohlc.map(b => b.time ? b.time.split("T")[1]?.slice(0, 5) || b.time : "");
            const prices = data.ohlc.map(b => b.close);

            this.chartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [{
                        label: `${data.symbol} Close Price`,
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
                        y: { grid: { color: 'rgba(255,255,255,0.05)' } }
                    },
                    plugins: {
                        legend: { display: false }
                    }
                }
            });
        } catch (e) {
            console.error("Failed to load chart:", e);
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
                                    <span>Entry: ₹${l.entry}</span>
                                    <span>Exit: ${l.exit ? '₹' + l.exit : 'OPEN'}</span>
                                    <span>LTP: ₹${l.ltp}</span>
                                    <span class="${l.pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}" style="font-weight: 700;">₹${l.pnl}</span>
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
                                <td>₹${t.price.toFixed(2)}</td>
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
        try {
            const parsed = JSON.parse(configStr);
            document.getElementById("dup-config-json").value = JSON.stringify(parsed, null, 2);
            if (parsed.symbol) document.getElementById("dup-symbol").value = parsed.symbol;
        } catch (e) {
            document.getElementById("dup-config-json").value = "{}";
        }
        document.getElementById("modal-duplicate").classList.add("active");
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
                    config: config
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
        alert(`Deploy Wizard: Strategy '${key}' is ready to map to clients from Dashboard.`);
        if (window.app) window.app.switchView("dashboard");
    }
}

window.strategiesManager = new StrategiesManager();
