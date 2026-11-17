/**
 * Strategy Charts, Indicators & Live Signals Page
 * - strat_time_atm_seller_v1: Combined Premium Line Chart (CE + PE) + individual legs sub-chart
 * - strat_pcp_pro_v201: OHLC Candlestick + Dual Chandelier + ExitLine/PT + ADX & JMA(RWI) + Signals
 * - strat_ftx_paper_v1: OHLC Candlestick + VWAP, OR High/Low, Prior Day High/Low + ATR + Cell Signals
 */

class ChartsPageManager {
    constructor() {
        this.catalog = [];
        this.currentTarget = "template:strat_time_atm_seller_v1";
        this.currentResolution = "";
        this.currentLookback = "1w";
        this.latestData = null;
        this.lwChart = null;
        this.subChart = null;
        this.fallbackMainChart = null;
        this.refreshTimer = null;
        this.resizeObserver = null;
        this.currentPcpStock = "HAL";

        this.initListeners();
    }

    initListeners() {
        const selEl = document.getElementById("charts-page-selector");
        if (selEl) {
            selEl.onchange = () => {
                this.currentTarget = selEl.value;
                this.syncQuickTabsFromSelection();
                this.loadChartData();
            };
        }

        const resEl = document.getElementById("charts-page-resolution");
        if (resEl) {
            resEl.onchange = () => {
                this.currentResolution = resEl.value;
                this.loadChartData();
            };
        }

        const lbEl = document.getElementById("charts-page-lookback");
        if (lbEl) {
            lbEl.value = this.currentLookback || "1w";
            lbEl.onchange = () => {
                this.currentLookback = lbEl.value;
                this.loadChartData();
            };
        }

        const btnRef = document.getElementById("btn-charts-refresh");
        if (btnRef) {
            btnRef.onclick = () => this.loadChartData();
        }

        const autoChk = document.getElementById("charts-page-autorefresh");
        if (autoChk) {
            autoChk.onchange = () => {
                if (autoChk.checked) this.startAutoRefresh();
                else this.stopAutoRefresh();
            };
        }

        document.querySelectorAll("#charts-quick-tabs [data-chart-tpl]").forEach(btn => {
            btn.onclick = () => {
                const tplKey = btn.dataset.chartTpl;
                this.selectByTemplateKey(tplKey);
            };
        });
    }

    async onViewActivated() {
        await this.loadCatalog();
        await this.loadChartData();
        this.startAutoRefresh();
    }

    onViewDeactivated() {
        this.stopAutoRefresh();
    }

    async openTemplateChart(templateKey) {
        if (window.app) window.app.switchView("charts");
        await this.loadCatalog();
        this.selectByTemplateKey(templateKey);
    }

    async openInstanceChart(instanceId) {
        if (window.app) window.app.switchView("charts");
        await this.loadCatalog();
        this.currentTarget = `instance:${instanceId}`;
        const selEl = document.getElementById("charts-page-selector");
        if (selEl) selEl.value = this.currentTarget;
        this.syncQuickTabsFromSelection();
        await this.loadChartData();
    }

    selectByTemplateKey(tplKey) {
        if (tplKey === "strat_pcp_pro_v201") {
            const currentStock = this.currentPcpStock || "HAL";
            this.selectPcpStock(currentStock);
            return;
        }

        // Prefer a deployed instance of this template if one exists; otherwise select template preview
        const matchingInst = this.catalog.find(c => c.kind === "instance" && c.template_key === tplKey);
        const matchingTpl = this.catalog.find(c => c.kind === "template" && c.template_key === tplKey);
        const chosen = matchingInst || matchingTpl;
        this.currentTarget = chosen ? chosen.target : `template:${tplKey}`;

        const selEl = document.getElementById("charts-page-selector");
        if (selEl && chosen) selEl.value = this.currentTarget;
        this.syncQuickTabsFromSelection();
        this.loadChartData();
    }

    selectPcpStock(stockName) {
        const stk = (stockName || "HAL").toUpperCase();
        this.currentPcpStock = stk;

        // 1. Look for deployed instance matching this stock
        const matchingInst = this.catalog.find(c => 
            c.kind === "instance" && 
            c.template_key === "strat_pcp_pro_v201" && 
            ((c.underlying && c.underlying.toUpperCase() === stk) || 
             (c.symbol && c.symbol.toUpperCase() === stk) ||
             (c.label && c.label.toUpperCase().includes(stk)) ||
             (c.label_admin && c.label_admin.toUpperCase().includes(stk)))
        );

        if (matchingInst) {
            this.currentTarget = matchingInst.target;
        } else {
            // 2. Look for matching template entry (e.g. template:strat_pcp_pro_v201:RELIANCE)
            const matchingTpl = this.catalog.find(c => 
                c.kind === "template" && 
                c.template_key === "strat_pcp_pro_v201" && 
                ((c.underlying && c.underlying.toUpperCase() === stk) || c.target.endsWith(`:${stk}`))
            );
            this.currentTarget = matchingTpl ? matchingTpl.target : `template:strat_pcp_pro_v201:${stk}`;
        }

        const selEl = document.getElementById("charts-page-selector");
        if (selEl) selEl.value = this.currentTarget;
        this.syncQuickTabsFromSelection();
        this.loadChartData();
    }

    renderPcpStockSelector() {
        const container = document.getElementById("pcp-stock-pills");
        if (!container) return;

        // Supported stock list: HAL, RELIANCE, BHARTIARTL + any custom instances
        const stockSet = new Set(["HAL", "RELIANCE", "BHARTIARTL"]);
        const pcpInstances = this.catalog.filter(c => c.kind === "instance" && c.template_key === "strat_pcp_pro_v201");
        pcpInstances.forEach(inst => {
            const stk = (inst.underlying || inst.symbol || "").toUpperCase();
            if (stk && stk !== "NSE:NIFTY") stockSet.add(stk);
        });

        // Determine currently active stock
        let currentStock = (this.currentPcpStock || "HAL").toUpperCase();
        const selected = this.catalog.find(c => c.target === this.currentTarget);
        if (selected) {
            const selStk = (selected.underlying || selected.symbol || "").toUpperCase();
            if (selStk && selStk !== "NSE:NIFTY") {
                currentStock = selStk;
                this.currentPcpStock = selStk;
            }
        } else if (this.currentTarget.startsWith("template:strat_pcp_pro_v201:")) {
            const parts = this.currentTarget.split(":");
            if (parts.length >= 3) {
                currentStock = parts[2].toUpperCase();
                this.currentPcpStock = currentStock;
            }
        }

        let html = "";
        stockSet.forEach(stock => {
            const matchingInst = pcpInstances.find(i => 
                (i.underlying && i.underlying.toUpperCase() === stock) ||
                (i.symbol && i.symbol.toUpperCase() === stock) ||
                (i.label && i.label.toUpperCase().includes(stock)) ||
                (i.label_admin && i.label_admin.toUpperCase().includes(stock))
            );
            const isSelected = stock === currentStock;
            const badgeText = matchingInst ? (matchingInst.status === "RUNNING" ? "🟢 Live" : `#${matchingInst.id}`) : "Preview";
            const btnClass = `drawer-tab-btn pcp-stock-btn ${isSelected ? 'active' : ''}`;
            const btnStyle = isSelected
                ? "background: var(--accent-blue); color: white; border-color: var(--accent-blue); font-weight: 700; box-shadow: 0 0 10px rgba(59, 130, 246, 0.4); padding: 5px 12px; font-size: 0.8rem; border-radius: 6px; cursor: pointer;"
                : "background: var(--bg-tertiary); color: var(--text-primary); border: 1px solid var(--border-color); padding: 5px 12px; font-size: 0.8rem; border-radius: 6px; cursor: pointer;";

            html += `
                <button type="button" class="${btnClass}" 
                    data-stock="${stock}" 
                    style="${btnStyle}">
                    <strong>📈 ${stock}</strong>
                    <span style="font-size: 0.68rem; opacity: 0.9; margin-left: 5px; padding: 1px 5px; border-radius: 3px; background: rgba(0,0,0,0.25);">${badgeText}</span>
                </button>
            `;
        });

        container.innerHTML = html;

        // Bind clicks
        container.querySelectorAll(".pcp-stock-btn").forEach(btn => {
            btn.onclick = () => {
                const stk = btn.dataset.stock;
                this.selectPcpStock(stk);
            };
        });

        // Update info badge
        const infoBadge = document.getElementById("pcp-stock-info-badge");
        if (infoBadge && this.latestData && this.latestData.template_key === "strat_pcp_pro_v201") {
            infoBadge.innerHTML = `Active Contract: <strong style="color: var(--accent-cyan);">${this.latestData.symbol}</strong> (${this.latestData.resolution || '3m'})`;
        }
    }

    syncQuickTabsFromSelection() {
        const selected = this.catalog.find(c => c.target === this.currentTarget);
        const activeTpl = selected ? selected.template_key : (this.currentTarget.startsWith("template:") ? this.currentTarget.split(":")[1] : "");
        document.querySelectorAll("#charts-quick-tabs [data-chart-tpl]").forEach(btn => {
            btn.classList.toggle("active", btn.dataset.chartTpl === activeTpl);
        });

        const pcpBar = document.getElementById("pcp-stock-selector-container");
        if (pcpBar) {
            const isPcp = activeTpl === "strat_pcp_pro_v201";
            pcpBar.style.display = isPcp ? "flex" : "none";
            if (isPcp) {
                this.renderPcpStockSelector();
            }
        }
    }

    startAutoRefresh() {
        this.stopAutoRefresh();
        const autoChk = document.getElementById("charts-page-autorefresh");
        if (autoChk && !autoChk.checked) return;
        this.refreshTimer = setInterval(() => {
            if (window.app && window.app.currentView === "charts") {
                this.loadChartData(true);
            }
        }, 5000);
    }

    stopAutoRefresh() {
        if (this.refreshTimer) {
            clearInterval(this.refreshTimer);
            this.refreshTimer = null;
        }
    }

    async loadCatalog() {
        try {
            const res = await fetch("/api/charts/catalog");
            if (!res.ok) return;
            const data = await res.json();
            this.catalog = data.items || [];

            const selEl = document.getElementById("charts-page-selector");
            if (!selEl) return;

            const isAdmin = window.app ? window.app.currentRole === "ADMIN" : true;
            const instItems = this.catalog.filter(i => i.kind === "instance");
            const tplItems = this.catalog.filter(i => i.kind === "template");

            let html = "";
            if (instItems.length) {
                html += `<optgroup label="Deployed Strategy Instances">`;
                html += instItems.map(i => `<option value="${i.target}">${isAdmin ? i.label_admin : i.label} (${i.symbol})</option>`).join("");
                html += `</optgroup>`;
            }
            if (tplItems.length) {
                html += `<optgroup label="Registered Strategy Templates">`;
                html += tplItems.map(i => `<option value="${i.target}">${isAdmin ? i.label_admin : i.label} (${i.symbol})</option>`).join("");
                html += `</optgroup>`;
            }
            selEl.innerHTML = html || `<option value="template:strat_time_atm_seller_v1">MCX Automated ATM Seller</option>`;

            const exists = this.catalog.some(i => i.target === this.currentTarget);
            if (!exists && this.catalog.length > 0) {
                this.currentTarget = this.catalog[0].target;
            }
            selEl.value = this.currentTarget;
            this.syncQuickTabsFromSelection();
        } catch (e) {
            console.error("[ChartsPage] Failed to load catalog:", e);
        }
    }

    async loadChartData(silent = false) {
        try {
            const params = new URLSearchParams({ target: this.currentTarget });
            if (this.currentResolution) params.set("resolution", this.currentResolution);
            if (this.currentLookback) params.set("lookback", this.currentLookback);

            const res = await fetch(`/api/charts/data?${params.toString()}`);
            if (!res.ok) {
                if (!silent) console.error("[ChartsPage] Failed fetching chart data:", res.status);
                return;
            }
            this.latestData = await res.json();
            this.renderCurrent();

            if (this.latestData && this.latestData.template_key === "strat_pcp_pro_v201") {
                const infoBadge = document.getElementById("pcp-stock-info-badge");
                if (infoBadge) {
                    infoBadge.innerHTML = `Active Contract: <strong style="color: var(--accent-cyan);">${this.latestData.symbol}</strong> (${this.latestData.resolution || '3m'}) [${this.latestData.candles ? this.latestData.candles.length : 0} bars]`;
                }
            }
        } catch (e) {
            if (!silent) console.error("[ChartsPage] Error fetching chart data:", e);
        }
    }

    _destroyCharts() {
        if (this.resizeObserver) {
            this.resizeObserver.disconnect();
            this.resizeObserver = null;
        }
        if (this.lwChart) {
            this.lwChart.remove();
            this.lwChart = null;
        }
        if (this.fallbackMainChart) {
            this.fallbackMainChart.destroy();
            this.fallbackMainChart = null;
        }
        if (this.subChart) {
            this.subChart.destroy();
            this.subChart = null;
        }
        const host = document.getElementById("charts-main-host");
        if (host) host.innerHTML = "";
    }

    _themeColors() {
        const isLight = document.body.classList.contains("light-theme");
        return {
            isLight,
            bg: isLight ? "#ffffff" : "#0b1120",
            text: isLight ? "#334155" : "#94a3b8",
            grid: isLight ? "rgba(148,163,184,0.18)" : "rgba(148,163,184,0.08)",
            border: isLight ? "#e2e8f0" : "#1e293b",
        };
    }

    _toIstEpochSec(isoStr) {
        if (!isoStr) return null;
        let s = String(isoStr);
        if (!s.includes("+") && !s.includes("Z") && !s.includes("-", 10)) {
            s = s + "+05:30";
        }
        const ms = Date.parse(s);
        if (!Number.isFinite(ms)) return null;
        // Shift by +5:30 IST so UTC-based chart X-axis displays exact IST clock times
        return Math.floor(ms / 1000) + 19800;
    }

    _createLwChart(host, theme) {
        const chart = LightweightCharts.createChart(host, {
            width: host.clientWidth || 800,
            height: host.clientHeight || 430,
            layout: {
                background: { type: "solid", color: theme.bg },
                textColor: theme.text,
            },
            grid: {
                vertLines: { color: theme.grid },
                horzLines: { color: theme.grid },
            },
            rightPriceScale: { borderColor: theme.border },
            timeScale: {
                borderColor: theme.border,
                timeVisible: true,
                secondsVisible: false,
            },
            crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        });

        if (typeof ResizeObserver !== "undefined") {
            this.resizeObserver = new ResizeObserver(entries => {
                for (const entry of entries) {
                    if (entry.contentRect.width > 0 && chart) {
                        chart.applyOptions({ width: entry.contentRect.width });
                    }
                }
            });
            this.resizeObserver.observe(host);
        }
        return chart;
    }

    renderCurrent() {
        if (!this.latestData) return;
        this._destroyCharts();

        const data = this.latestData;
        const emptyOverlay = document.getElementById("charts-empty-overlay");
        const hasSeries =
            data.chart_mode === "COMBINED_PREMIUM"
                ? (data.combined_series && data.combined_series.length > 0)
                : (data.candles && data.candles.length > 0);

        if (emptyOverlay) {
            emptyOverlay.style.display = hasSeries ? "none" : "flex";
        }

        if (data.chart_mode === "COMBINED_PREMIUM") {
            this.renderCombinedPremiumView(data);
        } else {
            this.renderOhlcStrategyView(data);
        }

        this.renderSignalsTable(data);
    }

    renderCombinedPremiumView(data) {
        const series = data.combined_series || [];
        const refs = data.reference_lines || {};
        const lastPt = series.length ? series[series.length - 1] : null;

        // Metrics Strip
        const metricsEl = document.getElementById("charts-metrics-strip");
        if (metricsEl) {
            metricsEl.innerHTML = `
                <span><strong>${data.strategy_name}</strong> <code style="color: var(--accent-cyan);">${data.resolution}</code></span>
                <span>CE (<code>${data.ce_symbol || '-'}</code>): <strong>₹${lastPt ? lastPt.ce_ltp.toFixed(2) : '0.00'}</strong></span>
                <span>PE (<code>${data.pe_symbol || '-'}</code>): <strong>₹${lastPt ? lastPt.pe_ltp.toFixed(2) : '0.00'}</strong></span>
                <span style="color: #38bdf8; font-weight: 700;">Combined: ₹${lastPt ? lastPt.combined_premium.toFixed(2) : '0.00'}</span>
                ${refs.entry_combined ? `<span style="color: #10b981;">Entry: ₹${refs.entry_combined.toFixed(2)}</span>` : ""}
                ${refs.stop_loss_combined ? `<span style="color: #ef4444;">SL Cap: ₹${refs.stop_loss_combined.toFixed(2)}</span>` : ""}
                <span>Spot (${data.symbol}): <strong>₹${Number(data.underlying_ltp || 0).toFixed(2)}</strong></span>
            `;
        }

        const legendEl = document.getElementById("charts-legend-strip");
        if (legendEl) {
            legendEl.innerHTML = `
                <span style="color: #38bdf8;">● Combined Premium (CE+PE)</span>
                ${refs.entry_combined ? `<span style="color: #10b981;">┄ Entry Level</span>` : ""}
                ${refs.stop_loss_combined ? `<span style="color: #ef4444;">┄ Max Loss SL</span>` : ""}
            `;
        }

        const host = document.getElementById("charts-main-host");
        const theme = this._themeColors();
        if (!host || !series.length) return;

        if (typeof LightweightCharts !== "undefined") {
            const chart = this._createLwChart(host, theme);
            this.lwChart = chart;

            const areaSeries = chart.addAreaSeries({
                lineColor: "#38bdf8",
                topColor: "rgba(56, 189, 248, 0.28)",
                bottomColor: "rgba(56, 189, 248, 0.02)",
                lineWidth: 2,
                title: "Combined (CE+PE)",
            });

            const pts = [];
            const seenT = new Set();
            for (const p of series) {
                const t = this._toIstEpochSec(p.time);
                if (t && !seenT.has(t)) {
                    seenT.add(t);
                    pts.push({ time: t, value: p.combined_premium, label: p.label });
                }
            }
            pts.sort((a, b) => a.time - b.time);
            areaSeries.setData(pts);

            if (refs.entry_combined) {
                areaSeries.createPriceLine({
                    price: refs.entry_combined,
                    color: "#10b981",
                    lineWidth: 1,
                    lineStyle: LightweightCharts.LineStyle.Dashed,
                    axisLabelVisible: true,
                    title: "Entry",
                });
            }
            if (refs.stop_loss_combined) {
                areaSeries.createPriceLine({
                    price: refs.stop_loss_combined,
                    color: "#ef4444",
                    lineWidth: 1,
                    lineStyle: LightweightCharts.LineStyle.Dashed,
                    axisLabelVisible: true,
                    title: "SL Cap",
                });
            }

            // Signal markers onto nearest timestamp
            const markers = [];
            for (const sig of (data.signals || [])) {
                const matchPt = pts.find(x => x.label === sig.label_time) || pts[pts.length - 1];
                if (matchPt) {
                    const isEntry = sig.type === "SELL";
                    markers.push({
                        time: matchPt.time,
                        position: isEntry ? "aboveBar" : "belowBar",
                        color: isEntry ? "#ef4444" : "#10b981",
                        shape: isEntry ? "arrowDown" : "arrowUp",
                        text: sig.label || sig.type,
                    });
                }
            }
            if (markers.length) {
                markers.sort((a, b) => a.time - b.time);
                areaSeries.setMarkers(markers);
            }
            if (pts.length > 200) {
                const total = pts.length;
                chart.timeScale().setVisibleLogicalRange({
                    from: total - 150,
                    to: total,
                });
            } else {
                chart.timeScale().fitContent();
            }
        } else {
            // Chart.js fallback
            const canvas = document.createElement("canvas");
            host.appendChild(canvas);
            this.fallbackMainChart = new Chart(canvas.getContext("2d"), {
                type: "line",
                data: {
                    labels: series.map(p => p.label),
                    datasets: [{
                        label: "Combined Premium (CE + PE)",
                        data: series.map(p => p.combined_premium),
                        borderColor: "#38bdf8",
                        backgroundColor: "rgba(56, 189, 248, 0.14)",
                        fill: true,
                        tension: 0.2,
                        pointRadius: 1.5,
                    }],
                },
                options: { responsive: true, maintainAspectRatio: false },
            });
        }

        // Sub-chart: CE vs PE individual leg premiums
        const subTitle = document.getElementById("charts-subchart-title");
        if (subTitle) subTitle.innerText = `Option Legs Breakdown (${data.ce_symbol} vs ${data.pe_symbol})`;
        const subCanvas = document.getElementById("charts-subchart-canvas");
        if (subCanvas) {
            this.subChart = new Chart(subCanvas.getContext("2d"), {
                type: "line",
                data: {
                    labels: series.map(p => p.label),
                    datasets: [
                        { label: data.ce_symbol || "CE", data: series.map(p => p.ce_ltp), borderColor: "#60a5fa", tension: 0.2, pointRadius: 0 },
                        { label: data.pe_symbol || "PE", data: series.map(p => p.pe_ltp), borderColor: "#f59e0b", tension: 0.2, pointRadius: 0 },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: "index", intersect: false },
                    scales: { x: { grid: { color: theme.grid } }, y: { grid: { color: theme.grid } } },
                },
            });
        }
    }

    renderOhlcStrategyView(data) {
        const candles = data.candles || [];
        const overlays = data.overlays || {};
        const osc = data.oscillators || {};
        const isPcp = data.template_key === "strat_pcp_pro_v201";
        const isSupertrend = data.template_key === "strat_supertrend_momentum_v1";
        const lastC = candles.length ? candles[candles.length - 1] : null;
        const lastIdx = candles.length - 1;

        // Metrics Strip
        const metricsEl = document.getElementById("charts-metrics-strip");
        if (metricsEl) {
            if (isSupertrend) {
                const stDir = (overlays.supertrend_direction && lastIdx >= 0) ? overlays.supertrend_direction[lastIdx] : 0;
                const stVal = overlays.supertrend && lastIdx >= 0 ? overlays.supertrend[lastIdx] : null;
                const rsiVal = osc.rsi && lastIdx >= 0 ? osc.rsi[lastIdx] : null;
                const atrVal = osc.atr && lastIdx >= 0 ? osc.atr[lastIdx] : (overlays.atr && lastIdx >= 0 ? overlays.atr[lastIdx] : null);
                const r1Val = overlays.pivot_r1 && lastIdx >= 0 ? overlays.pivot_r1[lastIdx] : null;
                const s1Val = overlays.pivot_s1 && lastIdx >= 0 ? overlays.pivot_s1[lastIdx] : null;
                const bbUVal = overlays.bb_upper && lastIdx >= 0 ? overlays.bb_upper[lastIdx] : null;
                const bbLVal = overlays.bb_lower && lastIdx >= 0 ? overlays.bb_lower[lastIdx] : null;
                metricsEl.innerHTML = `
                    <span><strong>${data.strategy_name}</strong> (<code>${data.symbol}</code> • ${data.resolution})</span>
                    <span>Close: <strong>₹${lastC ? lastC.close.toFixed(2) : '-'}</strong></span>
                    ${stVal !== null ? `<span style="color: ${stDir === 1 ? '#10b981' : '#ef4444'}; font-weight: 700;">Supertrend: ₹${stVal.toFixed(2)} (${stDir === 1 ? '▲ LONG' : '▼ SHORT'})</span>` : ""}
                    ${atrVal !== null ? `<span style="color: ${(atrVal >= 15 && atrVal <= 35) ? '#10b981' : '#f59e0b'}; font-weight: 600;">ATR: ${atrVal.toFixed(1)} [15-35]</span>` : ""}
                    ${rsiVal !== null ? `<span style="color: ${rsiVal >= 70 ? '#10b981' : (rsiVal <= 30 ? '#ef4444' : '#38bdf8')}; font-weight: 600;">RSI(14): ${rsiVal.toFixed(1)}</span>` : ""}
                    ${r1Val !== null ? `<span style="color: #a855f7;">R1: ₹${r1Val.toFixed(2)}</span>` : ""}
                    ${s1Val !== null ? `<span style="color: #f59e0b;">S1: ₹${s1Val.toFixed(2)}</span>` : ""}
                    ${(bbUVal !== null && bbLVal !== null) ? `<span style="color: #60a5fa;">BB: [₹${bbLVal.toFixed(2)} - ₹${bbUVal.toFixed(2)}]</span>` : ""}
                    <span>Signals: <strong>${(data.signals || []).length}</strong></span>
                `;
            } else if (isPcp) {
                const td = osc.trend_direction && lastIdx >= 0 ? osc.trend_direction[lastIdx] : 0;
                const adxLast = osc.adx && lastIdx >= 0 ? osc.adx[lastIdx] : null;
                const lsLast = overlays.long_stop && lastIdx >= 0 ? overlays.long_stop[lastIdx] : null;
                const ssLast = overlays.short_stop && lastIdx >= 0 ? overlays.short_stop[lastIdx] : null;
                metricsEl.innerHTML = `
                    <span><strong>${data.strategy_name}</strong> (<code>${data.symbol}</code> • ${data.resolution})</span>
                    <span>Close: <strong>₹${lastC ? lastC.close.toFixed(2) : '-'}</strong></span>
                    <span style="color: ${td === 1 ? '#10b981' : '#ef4444'}; font-weight: 700;">Regime: ${td === 1 ? '▲ BULLISH' : (td === -1 ? '▼ BEARISH' : 'NEUTRAL')}</span>
                    ${lsLast !== null ? `<span style="color: #10b981;">LongStop: ₹${lsLast.toFixed(2)}</span>` : ""}
                    ${ssLast !== null ? `<span style="color: #ef4444;">ShortStop: ₹${ssLast.toFixed(2)}</span>` : ""}
                    ${adxLast !== null ? `<span style="color: #60a5fa;">ADX(14): ${adxLast.toFixed(1)}</span>` : ""}
                    <span>Signals: <strong>${(data.signals || []).length}</strong></span>
                `;
            } else {
                const vwapL = overlays.vwap && lastIdx >= 0 ? overlays.vwap[lastIdx] : null;
                const orHL = overlays.or_high && lastIdx >= 0 ? overlays.or_high[lastIdx] : null;
                const orLL = overlays.or_low && lastIdx >= 0 ? overlays.or_low[lastIdx] : null;
                metricsEl.innerHTML = `
                    <span><strong>${data.strategy_name}</strong> (<code>${data.symbol}</code> • ${data.resolution})</span>
                    <span>Close: <strong>₹${lastC ? lastC.close.toFixed(2) : '-'}</strong></span>
                    ${vwapL !== null ? `<span style="color: #a855f7;">VWAP: ₹${vwapL.toFixed(2)}</span>` : ""}
                    ${orHL !== null ? `<span style="color: #10b981;">OR High: ₹${orHL.toFixed(2)}</span>` : ""}
                    ${orLL !== null ? `<span style="color: #ef4444;">OR Low: ₹${orLL.toFixed(2)}</span>` : ""}
                    <span>Signals: <strong>${(data.signals || []).length}</strong></span>
                `;
            }
        }

        // Legend Strip
        const legendEl = document.getElementById("charts-legend-strip");
        if (legendEl) {
            if (isSupertrend) {
                legendEl.innerHTML = `
                    <span style="color: #10b981;">━ Supertrend (Long)</span>
                    <span style="color: #ef4444;">━ Supertrend (Short)</span>
                    <span style="color: #60a5fa;">┄ BB Upper / Lower</span>
                    <span style="color: #a855f7;">┄ Pivot R1</span>
                    <span style="color: #f59e0b;">┄ Pivot S1</span>
                    <span style="color: #fbbf24;">┄ OR High / Low</span>
                    <span style="color: #94a3b8;">▲ BUY / COVER | ▼ SHORT / SELL</span>
                `;
            } else if (isPcp) {
                legendEl.innerHTML = `
                    <span style="color: #10b981;">━ LongStop</span>
                    <span style="color: #ef4444;">━ ShortStop</span>
                    <span style="color: #f59e0b;">━ ExitLine</span>
                    <span style="color: #38bdf8;">┄ ProfitTarget</span>
                    <span style="color: #94a3b8;">▲ BUY / COVER | ▼ SHORT / SELL</span>
                `;
            } else {
                legendEl.innerHTML = `
                    <span style="color: #a855f7;">━ VWAP</span>
                    <span style="color: #10b981;">┄ OR High</span>
                    <span style="color: #ef4444;">┄ OR Low</span>
                    <span style="color: #38bdf8;">┄ Prior Day H/L</span>
                    <span style="color: #94a3b8;">▲ BUY | ▼ SELL</span>
                `;
            }
        }

        const host = document.getElementById("charts-main-host");
        const theme = this._themeColors();
        if (!host || !candles.length) return;

        if (typeof LightweightCharts !== "undefined") {
            const chart = this._createLwChart(host, theme);
            this.lwChart = chart;

            const candleSeries = chart.addCandlestickSeries({
                upColor: "#10b981",
                downColor: "#ef4444",
                borderVisible: false,
                wickUpColor: "#10b981",
                wickDownColor: "#ef4444",
            });

            const lwCandles = [];
            const idxToTime = new Map();
            let prevT = 0;
            candles.forEach((c, idx) => {
                let t = this._toIstEpochSec(c.time);
                if (!t) return;
                if (t <= prevT) t = prevT + 1;
                prevT = t;
                idxToTime.set(idx, t);
                lwCandles.push({ time: t, open: c.open, high: c.high, low: c.low, close: c.close });
            });
            candleSeries.setData(lwCandles);

            const addOverlayLine = (arr, color, title, lineStyle = LightweightCharts.LineStyle.Solid, lineWidth = 1.5) => {
                if (!arr || !arr.length) return;
                const linePts = [];
                arr.forEach((v, idx) => {
                    const t = idxToTime.get(idx);
                    if (v !== null && v !== undefined && Number.isFinite(v) && t) {
                        linePts.push({ time: t, value: Number(v) });
                    }
                });
                if (linePts.length) {
                    const ls = chart.addLineSeries({ color, lineWidth, lineStyle, title, priceLineVisible: false });
                    ls.setData(linePts);
                }
            };

            if (isSupertrend) {
                addOverlayLine(overlays.supertrend, "#10b981", "Supertrend", LightweightCharts.LineStyle.Solid, 2);
                addOverlayLine(overlays.bb_upper, "#60a5fa", "BB Upper", LightweightCharts.LineStyle.Dotted, 1.5);
                addOverlayLine(overlays.bb_lower, "#60a5fa", "BB Lower", LightweightCharts.LineStyle.Dotted, 1.5);
                addOverlayLine(overlays.pivot_r1, "#a855f7", "Pivot R1", LightweightCharts.LineStyle.Dashed, 1.5);
                addOverlayLine(overlays.pivot_s1, "#f59e0b", "Pivot S1", LightweightCharts.LineStyle.Dashed, 1.5);
                addOverlayLine(overlays.opening_range_high, "#fbbf24", "OR High", LightweightCharts.LineStyle.Dotted, 1);
                addOverlayLine(overlays.opening_range_low, "#fbbf24", "OR Low", LightweightCharts.LineStyle.Dotted, 1);
                addOverlayLine(overlays.consolidation_high, "rgba(226, 232, 240, 0.4)", "Consolidation High", LightweightCharts.LineStyle.Dotted, 1);
                addOverlayLine(overlays.consolidation_low, "rgba(226, 232, 240, 0.4)", "Consolidation Low", LightweightCharts.LineStyle.Dotted, 1);
                addOverlayLine(overlays.long_stop, "#ef4444", "Long SL", LightweightCharts.LineStyle.Dashed, 1.5);
                addOverlayLine(overlays.short_stop, "#ef4444", "Short SL", LightweightCharts.LineStyle.Dashed, 1.5);
                addOverlayLine(overlays.long_target, "#10b981", "Target R:R", LightweightCharts.LineStyle.Dashed, 1.5);
                addOverlayLine(overlays.short_target, "#10b981", "Target R:R", LightweightCharts.LineStyle.Dashed, 1.5);
            } else if (isPcp) {
                addOverlayLine(overlays.long_stop, "#10b981", "LongStop");
                addOverlayLine(overlays.short_stop, "#ef4444", "ShortStop");
                addOverlayLine(overlays.trend_long_stop, "rgba(16, 185, 129, 0.45)", "TrendLong", LightweightCharts.LineStyle.Dotted, 1);
                addOverlayLine(overlays.trend_short_stop, "rgba(239, 68, 68, 0.45)", "TrendShort", LightweightCharts.LineStyle.Dotted, 1);
                addOverlayLine(overlays.exit_line, "#f59e0b", "ExitLine", LightweightCharts.LineStyle.Solid, 2);
                addOverlayLine(overlays.profit_target, "#38bdf8", "Target", LightweightCharts.LineStyle.Dashed, 1);
            } else {
                addOverlayLine(overlays.vwap, "#a855f7", "VWAP", LightweightCharts.LineStyle.Solid, 2);
                addOverlayLine(overlays.or_high, "#10b981", "OR High", LightweightCharts.LineStyle.Dashed, 1.5);
                addOverlayLine(overlays.or_low, "#ef4444", "OR Low", LightweightCharts.LineStyle.Dashed, 1.5);
                addOverlayLine(overlays.prior_day_high, "#38bdf8", "PDH", LightweightCharts.LineStyle.Dotted, 1.5);
                addOverlayLine(overlays.prior_day_low, "#f59e0b", "PDL", LightweightCharts.LineStyle.Dotted, 1.5);
            }

            // Signal markers
            const markers = [];
            for (const sig of (data.signals || [])) {
                const t = idxToTime.get(sig.index);
                if (!t) continue;
                const isUp = sig.type === "BUY" || sig.type === "COVER";
                const color = sig.type === "BUY" ? "#10b981" : (sig.type === "COVER" ? "#38bdf8" : (sig.type === "SHORT" ? "#ef4444" : "#f59e0b"));
                markers.push({
                    time: t,
                    position: isUp ? "belowBar" : "aboveBar",
                    color,
                    shape: isUp ? "arrowUp" : "arrowDown",
                    text: sig.label || sig.type,
                });
            }
            if (markers.length) {
                markers.sort((a, b) => a.time - b.time);
                candleSeries.setMarkers(markers);
            }

            if (lwCandles.length > 200) {
                const total = lwCandles.length;
                chart.timeScale().setVisibleLogicalRange({
                    from: total - 150,
                    to: total,
                });
            } else {
                chart.timeScale().fitContent();
            }
        } else {
            // Chart.js fallback (Close + indicators)
            const canvas = document.createElement("canvas");
            host.appendChild(canvas);
            const datasets = [{
                label: `${data.symbol} Close`,
                data: candles.map(c => c.close),
                borderColor: "#3b82f6",
                tension: 0.1,
                pointRadius: 1.5,
            }];
            if (isPcp) {
                datasets.push({ label: "LongStop", data: overlays.long_stop || [], borderColor: "#10b981", borderWidth: 1.2, pointRadius: 0 });
                datasets.push({ label: "ShortStop", data: overlays.short_stop || [], borderColor: "#ef4444", borderWidth: 1.2, pointRadius: 0 });
            } else {
                datasets.push({ label: "VWAP", data: overlays.vwap || [], borderColor: "#a855f7", borderWidth: 1.5, pointRadius: 0 });
            }
            this.fallbackMainChart = new Chart(canvas.getContext("2d"), {
                type: "line",
                data: { labels: candles.map(c => c.label), datasets },
                options: { responsive: true, maintainAspectRatio: false },
            });
        }

        // Sub-Chart: RSI & ADX for Supertrend OR ADX & JMA(RWI) for PivotChandelier OR ATR for FTX
        const subTitle = document.getElementById("charts-subchart-title");
        const subCanvas = document.getElementById("charts-subchart-canvas");
        if (subCanvas) {
            const labels = candles.map(c => c.label);
            if (isSupertrend) {
                if (subTitle) subTitle.innerText = `Momentum & Volatility Oscillators: RSI(14), ADX(14), and ATR(14) [Entry Range: ${osc.minimum_entry_atr || 15} - ${osc.maximum_entry_atr || 35}]`;
                this.subChart = new Chart(subCanvas.getContext("2d"), {
                    type: "line",
                    data: {
                        labels,
                        datasets: [
                            { label: "RSI(14)", data: osc.rsi || [], borderColor: "#a855f7", yAxisID: "yRsi", tension: 0.2, pointRadius: 0 },
                            { label: "ADX(14)", data: osc.adx || [], borderColor: "#60a5fa", yAxisID: "yAdx", tension: 0.2, pointRadius: 0 },
                            { label: "ATR", data: osc.atr || [], borderColor: "#f59e0b", yAxisID: "yAtr", tension: 0.2, pointRadius: 0 },
                        ],
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        interaction: { mode: "index", intersect: false },
                        scales: {
                            x: { grid: { color: theme.grid } },
                            yRsi: {
                                type: "linear",
                                position: "left",
                                min: 0,
                                max: 100,
                                grid: { color: theme.grid },
                                title: { display: true, text: "RSI (0-100)" }
                            },
                            yAdx: {
                                type: "linear",
                                position: "right",
                                min: 0,
                                max: 80,
                                grid: { drawOnChartArea: false },
                                title: { display: true, text: "ADX" }
                            },
                            yAtr: {
                                type: "linear",
                                position: "right",
                                min: 0,
                                max: 60,
                                grid: { drawOnChartArea: false },
                                title: { display: true, text: "ATR" }
                            },
                        },
                    },
                });
            } else if (isPcp) {
                if (subTitle) subTitle.innerText = `Regime Oscillators: ADX(14) [Threshold ${osc.adx_threshold || 18}] & JMA(RWI High / Low) [Threshold 0.6]`;
                this.subChart = new Chart(subCanvas.getContext("2d"), {
                    type: "line",
                    data: {
                        labels,
                        datasets: [
                            { label: "ADX(14)", data: osc.adx || [], borderColor: "#60a5fa", yAxisID: "yAdx", tension: 0.2, pointRadius: 0 },
                            { label: "JMA(RWI High)", data: osc.jma_rwi_high || [], borderColor: "#10b981", yAxisID: "yRwi", tension: 0.2, pointRadius: 0 },
                            { label: "JMA(RWI Low)", data: osc.jma_rwi_low || [], borderColor: "#ef4444", yAxisID: "yRwi", tension: 0.2, pointRadius: 0 },
                        ],
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        interaction: { mode: "index", intersect: false },
                        scales: {
                            x: { grid: { color: theme.grid } },
                            yAdx: { type: "linear", position: "left", grid: { color: theme.grid }, title: { display: true, text: "ADX" } },
                            yRwi: { type: "linear", position: "right", grid: { drawOnChartArea: false }, title: { display: true, text: "JMA(RWI)" } },
                        },
                    },
                });
            } else {
                if (subTitle) subTitle.innerText = `Structural Volatility: Rolling ATR(20)`;
                this.subChart = new Chart(subCanvas.getContext("2d"), {
                    type: "line",
                    data: {
                        labels,
                        datasets: [
                            { label: "ATR(20)", data: osc.atr || [], borderColor: "#f59e0b", backgroundColor: "rgba(245, 158, 11, 0.1)", fill: true, tension: 0.2, pointRadius: 0 },
                        ],
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        scales: { x: { grid: { color: theme.grid } }, y: { grid: { color: theme.grid } } },
                    },
                });
            }
        }
    }

    renderSignalsTable(data) {
        const tbody = document.getElementById("charts-signals-tbody");
        const countEl = document.getElementById("charts-signals-count");
        if (!tbody) return;

        const rows = [];
        for (const s of (data.signals || [])) {
            rows.push({
                time: s.label_time || s.time || "-",
                source: "SIGNAL",
                side: s.type,
                price: Number(s.price || 0),
                details: s.label || s.type,
            });
        }
        for (const t of (data.executed_trades || [])) {
            rows.push({
                time: t.label_time || t.time || "-",
                source: "EXECUTED TRADE",
                side: t.side,
                price: Number(t.price || 0),
                details: `${t.symbol} • Qty: ${t.quantity} (${t.leg_id || 'MAIN'})`,
            });
        }

        if (countEl) countEl.innerText = `${rows.length} signal(s) / order(s)`;

        if (!rows.length) {
            tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-muted);">No signals or executed trades recorded in visible bar window</td></tr>`;
            return;
        }

        tbody.innerHTML = rows.slice().reverse().map(r => {
            const isBuy = r.side === "BUY" || r.side === "COVER";
            const badgeColor = isBuy ? "var(--accent-green)" : "var(--accent-red)";
            return `
                <tr>
                    <td style="font-family: var(--font-mono);">${r.time}</td>
                    <td><span class="leg-pill" style="font-size: 0.7rem;">${r.source}</span></td>
                    <td style="font-weight: 700; color: ${badgeColor};">${r.side}</td>
                    <td style="font-family: var(--font-mono);">₹${r.price.toFixed(2)}</td>
                    <td style="font-size: 0.8rem;">${r.details}</td>
                </tr>
            `;
        }).join("");
    }
}

window.chartsPageManager = new ChartsPageManager();

