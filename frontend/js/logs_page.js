/**
 * Logs & Events Module
 * Three-part clean logs, dismissable error banner, and strategy-level filtering.
 */

class LogsManager {
    constructor() {
        this.tbodyEl = document.getElementById("system-logs-tbody");
        this.errorBannerContainer = document.getElementById("top-error-banner-container");
        this.strategyFilterSelect = document.getElementById("logs-strategy-filter");
        this.currentCategory = "";
        this.currentStrategyFilter = "";

        this.initEventListeners();
    }

    initEventListeners() {
        // Tab buttons for the 3 categories
        const tabBtns = document.querySelectorAll("[data-log-cat]");
        tabBtns.forEach(btn => {
            btn.onclick = () => {
                tabBtns.forEach(b => b.classList.remove("active"));
                btn.classList.add("active");
                this.currentCategory = btn.dataset.logCat;
                this.loadLogs();
            };
        });

        // Strategy filter
        if (this.strategyFilterSelect) {
            this.strategyFilterSelect.onchange = (e) => {
                this.currentStrategyFilter = e.target.value;
                this.loadLogs();
            };
        }

        const btnDismissAll = document.getElementById("btn-dismiss-all-errors");
        if (btnDismissAll) btnDismissAll.onclick = () => this.dismissAllErrors();
    }

    async loadLogs() {
        try {
            let url = `/api/logs?`;
            if (this.currentCategory) url += `category=${this.currentCategory}&`;
            if (this.currentStrategyFilter) url += `strategy_id=${this.currentStrategyFilter}&`;

            const res = await fetch(url);
            const data = await res.json();

            this.renderErrorBanners(data.active_errors);
            this.renderLogs(data.logs);
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

        this.errorBannerContainer.innerHTML = activeErrors.map(err => `
            <div class="prompt-error-banner" id="error-banner-${err.id}">
                <div class="error-banner-content">
                    <span class="error-banner-icon">🚨</span>
                    <div class="error-banner-text">
                        <strong>CRITICAL ATTENTION REQUIRED: [${err.category}]</strong>
                        <p>${err.message}</p>
                        <span style="font-size: 0.7rem; color: var(--text-muted);">${err.timestamp} &bull; Target: ${err.client_id || err.symbol || 'SYSTEM'}</span>
                    </div>
                </div>
                <button class="btn-dismiss-error" onclick="logsManager.dismissError(${err.id})">
                    ✕ DISMISS
                </button>
            </div>
        `).join("");
    }

    renderLogs(logs) {
        if (!this.tbodyEl) return;
        this.tbodyEl.innerHTML = logs.map(l => {
            const isErr = l.level === "ERROR";
            const isSucc = l.level === "SUCCESS";
            const isData = l.level === "DATA_RECEIVING";

            const badgeCls = isErr ? 'status-error' : (isSucc ? 'status-running' : (isData ? 'status-pill' : 'status-stopped'));
            const rowHighlight = (isErr && !l.is_dismissed) ? 'style="background: rgba(239,68,68,0.08);"' : '';

            return `
                <tr ${rowHighlight}>
                    <td style="font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-muted);">${l.timestamp}</td>
                    <td><span style="font-size: 0.75rem; font-weight: 600; color: var(--text-secondary);">${l.category}</span></td>
                    <td><span class="status-pill ${badgeCls}">${l.level}</span></td>
                    <td>
                        <span style="font-family: var(--font-mono); font-size: 0.8rem;">
                            ${l.strategy_id ? `Strat #${l.strategy_id} ` : ''}
                            ${l.symbol ? `[${l.symbol}] ` : ''}
                            ${l.client_id ? `(${l.client_id})` : ''}
                        </span>
                    </td>
                    <td>${l.message}</td>
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
        if (!this.strategyFilterSelect) return;
        const currentVal = this.strategyFilterSelect.value;
        this.strategyFilterSelect.innerHTML = `<option value="">All Strategies</option>` + instances.map(i => `
            <option value="${i.id}">${i.name_admin} (ID: ${i.id})</option>
        `).join("");
        this.strategyFilterSelect.value = currentVal;
    }
}

window.logsManager = new LogsManager();
