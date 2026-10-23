/**
 * Ticker Bar Module
 * Displays real-time streaming prices of default instruments:
 * NIFTY, BANKNIFTY, SENSEX, CRUDEOIL, NATURALGAS, GOLD, SILVER (Current Month Future)
 */

class TickerManager {
    constructor() {
        this.trackEl = document.getElementById("ticker-track");
        this.instruments = {
            "NSE:NIFTY": { name: "NIFTY 50", ltp: 24925.0, change: 45.2, change_pct: 0.18 },
            "NSE:BANKNIFTY": { name: "BANKNIFTY", ltp: 51860.0, change: -120.5, change_pct: -0.23 },
            "BSE:SENSEX": { name: "SENSEX", ltp: 81780.0, change: 180.0, change_pct: 0.22 },
            "MCX:CRUDEOIL": { name: "CRUDEOIL FUT", ltp: 6185.0, change: 35.0, change_pct: 0.57 },
            "MCX:NATURALGAS": { name: "NATURALGAS FUT", ltp: 245.8, change: -2.4, change_pct: -0.97 },
            "MCX:GOLD": { name: "GOLD FUT", ltp: 76420.0, change: 150.0, change_pct: 0.20 },
            "MCX:SILVER": { name: "SILVER FUT", ltp: 92550.0, change: 320.0, change_pct: 0.35 }
        };
        this.render();
    }

    render() {
        if (!this.trackEl) return;
        // Duplicate list for infinite smooth marquee effect
        const keys = Object.keys(this.instruments);
        const doubleKeys = [...keys, ...keys];

        this.trackEl.innerHTML = doubleKeys.map(k => {
            const inst = this.instruments[k];
            const isPos = inst.change >= 0;
            const sign = isPos ? "+" : "";
            const cls = isPos ? "pos" : "neg";

            return `
                <div class="ticker-item" id="ticker-${k.replace(':', '_')}">
                    <span class="ticker-name">${inst.name}</span>
                    <span class="ticker-price">${inst.ltp.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
                    <span class="ticker-change ${cls}">${sign}${inst.change_pct.toFixed(2)}%</span>
                </div>
            `;
        }).join("");
    }

    updateTick(tick) {
        if (!tick || !tick.symbol || !tick.ltp) return;
        const sym = tick.symbol;
        if (!this.instruments[sym]) {
            this.instruments[sym] = {
                name: tick.display_name || sym,
                ltp: tick.ltp,
                change: tick.change || 0,
                change_pct: tick.change_pct || 0
            };
            this.render();
        } else {
            const prevLtp = this.instruments[sym].ltp;
            this.instruments[sym].ltp = tick.ltp;
            this.instruments[sym].change = tick.change;
            this.instruments[sym].change_pct = tick.change_pct;

            // Flash animation on DOM
            const domId = `ticker-${sym.replace(':', '_')}`;
            const els = document.querySelectorAll(`[id^="${domId}"]`);
            els.forEach(el => {
                const priceEl = el.querySelector(".ticker-price");
                const chgEl = el.querySelector(".ticker-change");
                if (priceEl) priceEl.innerText = tick.ltp.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
                if (chgEl) {
                    const isPos = tick.change >= 0;
                    chgEl.className = `ticker-change ${isPos ? 'pos' : 'neg'}`;
                    chgEl.innerText = `${isPos ? '+' : ''}${tick.change_pct.toFixed(2)}%`;
                }

                if (tick.ltp > prevLtp) {
                    el.classList.add("flash-up");
                    setTimeout(() => el.classList.remove("flash-up"), 400);
                } else if (tick.ltp < prevLtp) {
                    el.classList.add("flash-down");
                    setTimeout(() => el.classList.remove("flash-down"), 400);
                }
            });
        }
    }
}

window.tickerManager = new TickerManager();
