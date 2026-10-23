/**
 * Status & Diagnostics Module
 * Features Feed Manager Health Monitoring, Failover triggers,
 * and runnable diagnostic test cases across all 6 segments (NSE, NFO, BSE, BSEFO, MCX, MCXFO).
 */

class StatusManager {
    constructor() {
        this.brokerCardsEl = document.getElementById("feed-broker-cards");
        this.feedTestTbody = document.getElementById("feed-test-results-tbody");
        this.orderTestTbody = document.getElementById("order-test-results-tbody");

        this.initEventListeners();
    }

    initEventListeners() {
        const btnFailover = document.getElementById("btn-trigger-failover");
        if (btnFailover) btnFailover.onclick = () => this.triggerFailover();

        const btnFeedTest = document.getElementById("btn-run-feed-test");
        if (btnFeedTest) btnFeedTest.onclick = () => this.runFeedTest();

        const btnOrderTest = document.getElementById("btn-run-order-test");
        if (btnOrderTest) btnOrderTest.onclick = () => this.runOrderTest();

        const btnRunAll = document.getElementById("btn-run-all-tests");
        if (btnRunAll) btnRunAll.onclick = async () => {
            await this.runFeedTest();
            await this.runOrderTest();
        };
    }

    async loadFeedStatus() {
        try {
            const res = await fetch("/api/status/feed");
            const status = await res.json();
            this.renderFeedBrokers(status);
        } catch (e) {
            console.error("Failed to load feed status:", e);
        }
    }

    renderFeedBrokers(status) {
        if (!this.brokerCardsEl) return;
        const brokers = Object.values(status.brokers || {});

        this.brokerCardsEl.innerHTML = brokers.map(b => {
            const isAct = b.is_active;
            const isConn = b.is_connected;
            const priorityIdx = (status.priority || []).indexOf(b.name) + 1;

            return `
                <div class="client-card" style="border: ${isAct ? '2px solid var(--accent-green)' : '1px solid var(--border-color)'};">
                    <div class="client-card-header">
                        <div>
                            <h3>
                                ${b.name}
                                ${isAct ? '<span class="status-pill status-running" style="font-size:0.65rem;">ACTIVE SOURCE</span>' : '<span style="font-size:0.65rem; color:var(--text-muted);">(Standby)</span>'}
                            </h3>
                            <span style="font-size:0.75rem; color:var(--text-muted);">Priority #${priorityIdx}</span>
                        </div>
                        <span class="status-pill status-${isConn ? 'running' : 'stopped'}">
                            <span class="status-dot"></span> ${isConn ? 'Connected' : 'Offline'}
                        </span>
                    </div>

                    <div class="card-pnl-grid" style="grid-template-columns: 1fr 1fr;">
                        <div class="card-pnl-item">
                            <span class="card-pnl-title">Latency</span>
                            <span class="card-pnl-num pnl-positive">${b.latency_ms.toFixed(1)} ms</span>
                        </div>
                        <div class="card-pnl-item">
                            <span class="card-pnl-title">Subscriptions</span>
                            <span class="card-pnl-num">${b.subscribed_count} Symbols</span>
                        </div>
                    </div>

                    <div style="font-size:0.7rem; color:var(--text-muted);">
                        Last Heartbeat: ${b.last_heartbeat ? new Date(b.last_heartbeat).toLocaleTimeString() : 'N/A'}
                    </div>
                </div>
            `;
        }).join("");
    }

    async triggerFailover() {
        if (!confirm("Trigger failover to next configured priority broker?")) return;
        try {
            const res = await fetch("/api/status/feed/failover", { method: "POST" });
            const data = await res.json();
            alert(`Failover successful! New active feed source: ${data.active_broker}`);
            this.loadFeedStatus();
        } catch (e) {
            alert("Failover failed: " + e);
        }
    }

    async runFeedTest() {
        this.feedTestTbody.innerHTML = `<tr><td colspan="8" style="text-align: center; color: var(--accent-blue); padding: 1.5rem;">Running Symbol Subscription & OHLC test across all 6 segments (NSE, NFO, BSE, BSEFO, MCX, MCXFO)...</td></tr>`;

        try {
            const res = await fetch("/api/status/diagnostics/run-feed-test", { method: "POST" });
            const data = await res.json();

            this.feedTestTbody.innerHTML = data.results.map(r => `
                <tr>
                    <td><strong style="color: var(--accent-cyan);">${r.segment}</strong></td>
                    <td><code>${r.symbol}</code></td>
                    <td><span class="status-pill status-${r.subscription_status === 'SUCCESS' ? 'running' : 'stopped'}">${r.subscription_status}</span></td>
                    <td><span class="status-pill status-${r.ohlc_fetch_status === 'SUCCESS' ? 'running' : 'stopped'}">${r.ohlc_fetch_status}</span></td>
                    <td>${r.bars_count} Bars</td>
                    <td style="font-family: var(--font-mono); font-weight: 600;">₹${r.latest_ltp ? r.latest_ltp.toFixed(2) : '-'}</td>
                    <td style="font-family: var(--font-mono);">${r.latency_ms} ms</td>
                    <td><strong style="color: ${r.overall_status === 'PASSED' ? 'var(--accent-green)' : 'var(--accent-red)'};">${r.overall_status}</strong></td>
                </tr>
            `).join("");
        } catch (e) {
            this.feedTestTbody.innerHTML = `<tr><td colspan="8" style="color: var(--accent-red); text-align: center;">Test execution failed: ${e}</td></tr>`;
        }
    }

    async runOrderTest() {
        this.orderTestTbody.innerHTML = `<tr><td colspan="8" style="text-align: center; color: var(--accent-blue); padding: 1.5rem;">Executing 1 order test across all 6 segments with real-time status update...</td></tr>`;

        try {
            const res = await fetch("/api/status/diagnostics/run-order-test", { method: "POST" });
            const data = await res.json();

            this.orderTestTbody.innerHTML = data.results.map(r => `
                <tr>
                    <td><strong style="color: var(--accent-cyan);">${r.segment}</strong></td>
                    <td><code>${r.symbol}</code></td>
                    <td>${r.client_id}</td>
                    <td style="font-family: var(--font-mono); font-size: 0.75rem;">${r.order_id}</td>
                    <td style="font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-muted);">${r.broker_order_id}</td>
                    <td><span class="status-pill status-${r.status === 'FILLED' ? 'running' : 'stopped'}">${r.status}</span></td>
                    <td style="font-family: var(--font-mono);">₹${r.filled_price.toFixed(2)}</td>
                    <td style="font-family: var(--font-mono);">${r.latency_ms} ms</td>
                </tr>
            `).join("");
        } catch (e) {
            this.orderTestTbody.innerHTML = `<tr><td colspan="8" style="color: var(--accent-red); text-align: center;">Order test execution failed: ${e}</td></tr>`;
        }
    }
}

window.statusManager = new StatusManager();
