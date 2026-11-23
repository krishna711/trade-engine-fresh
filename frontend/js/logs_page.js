/**
 * Logs & Events Module
 * Divided clean logs, dismissable error banner, and strategy-level filtering.
 */

class LogsManager {
    constructor() {
        this.tbodyEl = document.getElementById("system-logs-tbody");
        this.errorBannerContainer = document.getElementById("top-error-banner-container");
        this.strategyFilterSelect = document.getElementById("logs-strategy-filter");
        this.currentCategory = "";
        this.currentStrategyFilter = "";
        this.strategiesList = [];

        this.initEventListeners();
    }

    initEventListeners() {
        // Tab buttons for categories
        const tabBtns = document.querySelectorAll("[data-log-cat]");
        tabBtns.forEach(btn => {
            btn.onclick = () => {
                tabBtns.forEach(b => b.classList.remove("active"));
                btn.classList.add("active");
                this.currentCategory = btn.dataset.logCat;
                this.loadLogs();
            };
        });

        // Strategy filter dropdown
        if (this.strategyFilterSelect) {
            this.strategyFilterSelect.onchange = (e) => {
                this.currentStrategyFilter = e.target.value;
                this.loadLogs();
            };
        }

        // Refresh button
        const btnRefresh = document.getElementById("btn-refresh-logs");
        if (btnRefresh) {
            btnRefresh.onclick = () => this.loadLogs();
        }

        const btnDismissAll = document.getElementById("btn-dismiss-all-errors");
        if (btnDismissAll) btnDismissAll.onclick = () => this.dismissAllErrors();
    }

    async loadLogs() {
        try {
            let url = `/api/logs?`;
            if (this.currentCategory) url += `category=${encodeURIComponent(this.currentCategory)}&`;
            if (this.currentStrategyFilter) url += `strategy_id=${encodeURIComponent(this.currentStrategyFilter)}&`;
            url += `limit=300`;

            const res = await fetch(url);
            const data = await res.json();

            if (data.strategies && data.strategies.length) {
                this.strategiesList = data.strategies;
                this.populateStrategyFilter(data.strategies);
            }

            this.renderErrorBanners(data.active_errors);
            this.renderLogs(data.logs || []);
        } catch (e) {
            console.error("Failed to load logs:", e);
        }
    }

    renderErrorBanners(activeErrors) {
        if (!this.errorBannerContainer) return;
        if (!activeErrors || !activeErrors.length) {
            this.errorBannerContainer.innerHTML = "";
            return;
        }

        this.errorBannerContainer.innerHTML = activeErrors.map(err => {
            const stratLabel = err.strategy_name ? ` &bull; Strategy: ${err.strategy_name} (#${err.strategy_id})` : (err.strategy_id ? ` &bull; Strat #${err.strategy_id}` : '');
            return `
                <div class="prompt-error-banner" id="error-banner-${err.id}">
                    <div class="error-banner-content">
                        <span class="error-banner-icon">🚨</span>
                        <div class="error-banner-text">
                            <strong>CRITICAL ATTENTION REQUIRED: [${err.category}]</strong>
                            <p>${err.message}</p>
                            <span style="font-size: 0.7rem; color: var(--text-muted);">${err.timestamp} &bull; Target: ${err.client_id || err.symbol || 'SYSTEM'}${stratLabel}</span>
                        </div>
                    </div>
                    <button class="btn-dismiss-error" onclick="logsManager.dismissError(${err.id})">
                        ✕ DISMISS
                    </button>
                </div>
            `;
        }).join("");
    }

    renderLogs(logs) {
        if (!this.tbodyEl) return;
        const isAdmin = window.appState && window.appState.isAdmin;

        if (!logs.length) {
            this.tbodyEl.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 2rem;">No logs found matching current filter</td></tr>`;
            return;
        }

        this.tbodyEl.innerHTML = logs.map(l => {
            const isErr = l.level === "ERROR";
            const isSucc = l.level === "SUCCESS";
            const isData = l.level === "DATA_RECEIVING";
            const isCandle = l.category === "CANDLE_FETCH";

            const badgeCls = isErr ? 'status-error' : (isSucc ? 'status-running' : (isData ? 'status-pill' : (isCandle ? 'status-stopped' : 'status-stopped')));
            const rowHighlight = (isErr && !l.is_dismissed) ? 'style="background: rgba(239,68,68,0.08);"' : '';
            const catColor = isCandle ? 'color: #818cf8;' : (l.category === 'STRATEGY_DECISION' ? 'color: #38bdf8;' : 'color: var(--text-secondary);');

            // Strategy human-friendly display
            const rawStratName = isAdmin ? (l.strategy_name_admin || l.strategy_name) : (l.strategy_name || l.strategy_name_admin);
            let stratDisplay = '<span style="color: var(--text-muted);">-</span>';
            if (rawStratName) {
                stratDisplay = `<strong style="color: var(--accent-cyan); font-weight: 700;">${rawStratName}</strong> <span style="color: var(--text-muted); font-size: 0.72rem;">(#${l.strategy_id})</span>`;
            } else if (l.strategy_id) {
                stratDisplay = `<strong style="font-weight: 600;">Strat #${l.strategy_id}</strong>`;
            }

            return `
                <tr ${rowHighlight}>
                    <td style="font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-muted); white-space: nowrap;">${l.timestamp}</td>
                    <td><span style="font-size: 0.75rem; font-weight: 600; ${catColor}">${l.category}</span></td>
                    <td><span class="status-pill ${badgeCls}">${l.level}</span></td>
                    <td>
                        <div style="font-size: 0.8rem; display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
                            ${stratDisplay}
                            ${l.symbol ? `<span class="leg-pill" style="font-size: 0.72rem; padding: 1px 6px;">${l.symbol}</span>` : ''}
                            ${l.client_id ? `<span style="color: var(--text-muted); font-size: 0.72rem; font-family: var(--font-mono);">(${l.client_id})</span>` : ''}
                        </div>
                    </td>
                    <td style="font-size: 0.82rem; line-height: 1.4;">${l.message}</td>
                    <td style="text-align: right;">
                        ${isErr && !l.is_dismissed ? `
                            <button class="action-btn-sm" onclick="logsManager.dismissError(${l.id})">Dismiss</button>
                        ` : ''}
                    </td>
                </tr>
            `;
        }).join("");
    }

    async dismissError(logId) {
        try {
            await fetch(`/api/logs/${logId}/dismiss`, { method: "POST" });
            const banner = document.getElementById(`error-banner-${logId}`);
            if (banner) banner.remove();
            this.loadLogs();
        } catch (e) {
            console.error("Failed to dismiss error:", e);
        }
    }

    async dismissAllErrors() {
        try {
            await fetch("/api/logs/dismiss-all", { method: "POST" });
            if (this.errorBannerContainer) this.errorBannerContainer.innerHTML = "";
            this.loadLogs();
        } catch (e) {
            console.error("Failed to dismiss all:", e);
        }
    }

    populateStrategyFilter(instances) {
        if (!this.strategyFilterSelect) {
            this.strategyFilterSelect = document.getElementById("logs-strategy-filter");
        }
        if (!this.strategyFilterSelect) return;

        const currentVal = this.strategyFilterSelect.value;
        const isAdmin = window.appState && window.appState.isAdmin;

        let optionsHtml = `<option value="">All Strategies</option>`;
        (instances || []).forEach(i => {
            const displayName = (isAdmin ? (i.name_admin || i.name_user) : (i.name_user || i.name_admin)) || `Strat #${i.id}`;
            optionsHtml += `<option value="${i.id}">${displayName} (#${i.id})</option>`;
        });

        this.strategyFilterSelect.innerHTML = optionsHtml;
        if (currentVal) {
            this.strategyFilterSelect.value = currentVal;
        }
    }
}

window.logsManager = new LogsManager();
