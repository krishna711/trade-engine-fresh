/**
 * Reports and Real Trade Audit Manager
 * Handles multi-client & strategy filtering, date selection, trade listing with stacked prices, and MTM aggregation
 */
class ReportsManager {
    constructor() {
        this.clients = [];
        this.strategies = [];
        this.selectedClientIds = new Set();
        this.selectedStrategyNames = new Set();
        this.searchTerm = "";
        this.stratSearchTerm = "";
        this.debounceTimer = null;
        this.isInitialized = false;

        this.init();
    }

    init() {
        this.initDates();
        this.bindEvents();
    }

    initDates() {
        const fromDateEl = document.getElementById("rep-from-date");
        const toDateEl = document.getElementById("rep-to-date");

        const today = new Date();
        const yyyy = today.getFullYear();
        const mm = String(today.getMonth() + 1).padStart(2, '0');
        const dd = String(today.getDate()).padStart(2, '0');
        const todayStr = `${yyyy}-${mm}-${dd}`;

        if (fromDateEl && !fromDateEl.value) {
            fromDateEl.value = "2026-10-01";
        }
        if (toDateEl && !toDateEl.value) {
            toDateEl.value = todayStr;
        }
    }

    bindEvents() {
        // Date Pickers & Refresh
        const btnRefresh = document.getElementById("rep-btn-refresh");
        if (btnRefresh) {
            btnRefresh.onclick = () => this.loadTrades();
        }

        const fromDate = document.getElementById("rep-from-date");
        const toDate = document.getElementById("rep-to-date");
        if (fromDate) fromDate.onchange = () => this.loadTrades();
        if (toDate) toDate.onchange = () => this.loadTrades();

        // Filters: Mode, Status, Order
        const selMode = document.getElementById("rep-filter-mode");
        const selStatus = document.getElementById("rep-filter-status");
        const selOrder = document.getElementById("rep-filter-order");
        if (selMode) selMode.onchange = () => this.loadTrades();
        if (selStatus) selStatus.onchange = () => this.loadTrades();
        if (selOrder) selOrder.onchange = () => this.loadTrades();

        // Trade Search Input
        const inpSearch = document.getElementById("rep-trades-search");
        if (inpSearch) {
            inpSearch.oninput = (e) => {
                clearTimeout(this.debounceTimer);
                this.debounceTimer = setTimeout(() => {
                    this.searchTerm = e.target.value.trim();
                    this.loadTrades();
                }, 300);
            };
        }

        // Strategy Search Box
        const inpStratSearch = document.getElementById("rep-strats-search");
        if (inpStratSearch) {
            inpStratSearch.oninput = (e) => {
                this.stratSearchTerm = e.target.value.toLowerCase().trim();
                this.renderStrategyFilters();
            };
        }

        // "Select all" toggle links
        const linkClientsAll = document.getElementById("rep-clients-select-all");
        if (linkClientsAll) {
            linkClientsAll.onclick = () => this.toggleAllClients();
        }

        const linkStratsAll = document.getElementById("rep-strats-select-all");
        if (linkStratsAll) {
            linkStratsAll.onclick = () => this.toggleAllStrategies();
        }

        // "All clients" checkbox
        const chkClientsAll = document.getElementById("rep-clients-all-chk");
        if (chkClientsAll) {
            chkClientsAll.onchange = (e) => {
                if (e.target.checked) {
                    this.clients.forEach(c => this.selectedClientIds.add(c.id));
                } else {
                    this.selectedClientIds.clear();
                }
                this.renderClientFilters();
                this.loadTrades();
            };
        }

        // "All strategies" checkbox
        const chkStratsAll = document.getElementById("rep-strats-all-chk");
        if (chkStratsAll) {
            chkStratsAll.onchange = (e) => {
                if (e.target.checked) {
                    this.strategies.forEach(s => this.selectedStrategyNames.add(s.name));
                } else {
                    this.selectedStrategyNames.clear();
                }
                this.renderStrategyFilters();
                this.loadTrades();
            };
        }
    }

    async loadReports() {
        this.initDates();
        if (!this.isInitialized) {
            await this.loadFilters();
            this.isInitialized = true;
        }
        await this.loadTrades();
    }

    async loadFilters() {
        try {
            const resp = await fetch("/api/reports/filters");
            const data = await resp.json();
            if (data.success) {
                this.clients = data.clients || [];
                this.strategies = data.strategies || [];

                // Select ALL real clients by default
                this.selectedClientIds.clear();
                this.clients.forEach(c => this.selectedClientIds.add(c.id));

                // Select ALL real strategies by default
                this.selectedStrategyNames.clear();
                this.strategies.forEach(s => this.selectedStrategyNames.add(s.name));

                this.renderClientFilters();
                this.renderStrategyFilters();
            }
        } catch (err) {
            console.error("Error loading report filters:", err);
        }
    }

    renderClientFilters() {
        const listEl = document.getElementById("rep-clients-list");
        const countEl = document.getElementById("rep-clients-count");
        const chkAll = document.getElementById("rep-clients-all-chk");
        if (!listEl) return;

        if (countEl) countEl.innerText = this.clients.length;
        if (chkAll) {
            chkAll.checked = this.clients.length > 0 && this.selectedClientIds.size === this.clients.length;
        }

        listEl.innerHTML = "";
        this.clients.forEach(c => {
            const item = document.createElement("div");
            item.className = "reports-checkbox-item";
            const isChecked = this.selectedClientIds.has(c.id);

            item.innerHTML = `
                <label>
                    <input type="checkbox" value="${c.id}" ${isChecked ? "checked" : ""}>
                    <span>${c.name || c.id}</span>
                </label>
                <span class="status-pill status-active-pill">${c.status || "ACTIVE"}</span>
            `;

            const chk = item.querySelector("input");
            chk.onchange = (e) => {
                if (e.target.checked) {
                    this.selectedClientIds.add(c.id);
                } else {
                    this.selectedClientIds.delete(c.id);
                }
                if (chkAll) chkAll.checked = this.selectedClientIds.size === this.clients.length;
                this.loadTrades();
            };

            listEl.appendChild(item);
        });
    }

    renderStrategyFilters() {
        const listEl = document.getElementById("rep-strats-list");
        const countEl = document.getElementById("rep-strats-count");
        const chkAll = document.getElementById("rep-strats-all-chk");
        if (!listEl) return;

        const filtered = this.strategies.filter(s => {
            if (!this.stratSearchTerm) return true;
            return s.name.toLowerCase().includes(this.stratSearchTerm) ||
                   (s.slug && s.slug.toLowerCase().includes(this.stratSearchTerm));
        });

        if (countEl) countEl.innerText = filtered.length;
        if (chkAll) {
            chkAll.checked = this.strategies.length > 0 && this.selectedStrategyNames.size === this.strategies.length;
        }

        listEl.innerHTML = "";
        filtered.forEach(s => {
            const item = document.createElement("div");
            item.className = "reports-checkbox-item strategy-item";
            const isChecked = this.selectedStrategyNames.has(s.name);

            item.innerHTML = `
                <label>
                    <input type="checkbox" value="${s.name}" ${isChecked ? "checked" : ""}>
                    <div class="strat-label-content">
                        <span class="strat-name">${s.name}</span>
                        <span class="strat-sub">${s.client_name || "QUANTZEN"} • ${s.slug || "strategy"}</span>
                    </div>
                </label>
            `;

            const chk = item.querySelector("input");
            chk.onchange = (e) => {
                if (e.target.checked) {
                    this.selectedStrategyNames.add(s.name);
                } else {
                    this.selectedStrategyNames.delete(s.name);
                }
                if (chkAll) chkAll.checked = this.selectedStrategyNames.size === this.strategies.length;
                this.loadTrades();
            };

            listEl.appendChild(item);
        });
    }

    toggleAllClients() {
        const selectAll = this.selectedClientIds.size !== this.clients.length;
        if (selectAll) {
            this.clients.forEach(c => this.selectedClientIds.add(c.id));
        } else {
            this.selectedClientIds.clear();
        }
        this.renderClientFilters();
        this.loadTrades();
    }

    toggleAllStrategies() {
        const selectAll = this.selectedStrategyNames.size !== this.strategies.length;
        if (selectAll) {
            this.strategies.forEach(s => this.selectedStrategyNames.add(s.name));
        } else {
            this.selectedStrategyNames.clear();
        }
        this.renderStrategyFilters();
        this.loadTrades();
    }

    renderEmptyTrades(message) {
        const tbody = document.getElementById("reports-trades-tbody");
        const totalTradesEl = document.getElementById("rep-total-trades");
        const totalMtmEl = document.getElementById("rep-total-mtm-val");
        const badgeMtm = document.getElementById("rep-badge-mtm");

        if (totalTradesEl) totalTradesEl.innerText = "0";
        if (totalMtmEl) totalMtmEl.innerText = "₹0.00";
        if (badgeMtm) {
            badgeMtm.style.color = "var(--text-secondary)";
            badgeMtm.style.borderColor = "var(--border-color)";
        }
        if (tbody) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="13" style="text-align: center; color: var(--text-muted); padding: 2.5rem;">
                        ${message}
                    </td>
                </tr>
            `;
        }
    }

    async loadTrades() {
        if (this.clients.length > 0 && this.selectedClientIds.size === 0) {
            this.renderEmptyTrades("No client accounts selected. Please check at least one client.");
            return;
        }

        if (this.strategies.length > 0 && this.selectedStrategyNames.size === 0) {
            this.renderEmptyTrades("No strategies selected. Please check at least one strategy.");
            return;
        }

        const tbody = document.getElementById("reports-trades-tbody");
        const totalTradesEl = document.getElementById("rep-total-trades");
        const totalMtmEl = document.getElementById("rep-total-mtm-val");
        const badgeMtm = document.getElementById("rep-badge-mtm");

        const fromDate = document.getElementById("rep-from-date") ? document.getElementById("rep-from-date").value.trim() : "";
        const toDate = document.getElementById("rep-to-date") ? document.getElementById("rep-to-date").value.trim() : "";
        const mode = document.getElementById("rep-filter-mode") ? document.getElementById("rep-filter-mode").value : "ALL";
        const status = document.getElementById("rep-filter-status") ? document.getElementById("rep-filter-status").value : "ALL";
        const order = document.getElementById("rep-filter-order") ? document.getElementById("rep-filter-order").value : "newest";

        const params = new URLSearchParams();
        if (this.selectedClientIds.size > 0 && this.selectedClientIds.size < this.clients.length) {
            params.append("client_ids", Array.from(this.selectedClientIds).join(","));
        }
        if (this.selectedStrategyNames.size > 0 && this.selectedStrategyNames.size < this.strategies.length) {
            params.append("strategy_names", Array.from(this.selectedStrategyNames).join(","));
        }
        if (fromDate) params.append("from_date", fromDate);
        if (toDate) params.append("to_date", toDate);
        if (mode && mode !== "ALL") params.append("mode", mode);
        if (status && status !== "ALL") params.append("status", status);
        if (order) params.append("order", order);
        if (this.searchTerm) params.append("search", this.searchTerm);

        try {
            const resp = await fetch(`/api/reports/trades?${params.toString()}`);
            const data = await resp.json();
            if (!data.success) return;

            const count = data.total_trades || 0;
            const mtm = data.total_mtm || 0.0;

            if (totalTradesEl) totalTradesEl.innerText = count;
            if (totalMtmEl) {
                const sign = mtm >= 0 ? "+" : "";
                const formattedMtm = `${sign}${Math.abs(mtm).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
                totalMtmEl.innerText = (mtm < 0 ? "-" : "+") + formattedMtm.replace("+", "");
            }
            if (badgeMtm) {
                if (mtm >= 0) {
                    badgeMtm.style.color = "#10b981";
                    badgeMtm.style.borderColor = "rgba(16, 185, 129, 0.4)";
                } else {
                    badgeMtm.style.color = "#ef4444";
                    badgeMtm.style.borderColor = "rgba(239, 68, 68, 0.4)";
                }
            }

            if (!tbody) return;
            tbody.innerHTML = "";

            if (!data.trades || data.trades.length === 0) {
                this.renderEmptyTrades("No trade reports found matching selected criteria.");
                return;
            }

            data.trades.forEach(t => {
                const tr = document.createElement("tr");

                // Stacked entry prices
                let entryHtml = "-";
                if (t.entry_prices && t.entry_prices.length > 0) {
                    entryHtml = `<div class="stacked-prices">${t.entry_prices.map(p => `<span>${Number(p).toFixed(2)}</span>`).join("")}</div>`;
                }

                // Stacked exit prices
                let exitHtml = "-";
                if (t.exit_prices && t.exit_prices.length > 0) {
                    exitHtml = `<div class="stacked-prices">${t.exit_prices.map(p => `<span>${Number(p).toFixed(2)}</span>`).join("")}</div>`;
                }

                // MTM formatting
                const mtmVal = Number(t.mtm || 0.0);
                const mtmSign = mtmVal > 0 ? "+" : (mtmVal < 0 ? "-" : "");
                const mtmClass = mtmVal >= 0 ? "mtm-cell-pos" : "mtm-cell-neg";
                const mtmStr = `${mtmSign}${Math.abs(mtmVal).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

                const modeBadge = t.mode === "LIVE" ? 
                    `<span class="mode-badge-live">LIVE</span>` : 
                    `<span class="mode-badge-paper">PAPER</span>`;

                const sideClass = (t.side || "").toUpperCase() === "BUY" ? "side-buy" : "side-sell";
                const statusClass = t.status === "OPEN" ? "status-open-pill" : "status-closed-pill";

                tr.innerHTML = `
                    <td style="white-space: nowrap; font-family: var(--font-mono); font-size: 0.78rem;">${t.opened}</td>
                    <td style="white-space: nowrap; font-family: var(--font-mono); font-size: 0.78rem;">${t.closed}</td>
                    <td style="font-weight: 600;">${t.client}</td>
                    <td>${t.strategy}</td>
                    <td>${modeBadge}</td>
                    <td style="font-weight: 500; font-family: var(--font-mono); font-size: 0.78rem;">${t.symbol}</td>
                    <td><span class="${sideClass}">${t.side || "SELL"}</span></td>
                    <td style="font-family: var(--font-mono);">${t.qty}</td>
                    <td>${entryHtml}</td>
                    <td>${exitHtml}</td>
                    <td style="color: var(--text-muted);">${t.reason || "-"}</td>
                    <td class="${mtmClass}">${mtmStr}</td>
                    <td><span class="${statusClass}">${t.status}</span></td>
                `;

                tbody.appendChild(tr);
            });
        } catch (err) {
            console.error("Error loading trade reports:", err);
        }
    }
}

// Global instance
window.reportsManager = new ReportsManager();
