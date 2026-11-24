/**
 * Trading Holidays Manager (Admin)
 * Dynamic management of exchange trading holidays for NSE, BSE, and MCX.
 * Allows administrators to add, edit, or delete holiday dates as exchange circulars change.
 */

class HolidaysManager {
    constructor() {
        this.holidays = [];
        this.currentYearFilter = "ALL";
        this.init();
    }

    init() {
        const modal = document.getElementById("modal-trading-holidays");
        if (modal) {
            modal.querySelectorAll(".modal-close-btn").forEach(btn => {
                btn.onclick = () => this.closeHolidaysModal();
            });
            modal.onclick = (e) => {
                if (e.target === modal) this.closeHolidaysModal();
            };
        }
    }

    async openHolidaysModal() {
        if (window.authManager && typeof window.authManager.closeUserDropdown === "function") {
            window.authManager.closeUserDropdown();
        }
        const modal = document.getElementById("modal-trading-holidays");
        if (modal) {
            modal.classList.add("active");
            this.closeForm();
            await this.loadHolidays();
        }
    }

    closeHolidaysModal() {
        const modal = document.getElementById("modal-trading-holidays");
        if (modal) {
            modal.classList.remove("active");
            this.closeForm();
        }
    }

    setYearFilter(year) {
        this.currentYearFilter = year;
        document.querySelectorAll(".holiday-year-filter").forEach(btn => {
            if (btn.getAttribute("data-year") === year) {
                btn.classList.add("active");
            } else {
                btn.classList.remove("active");
            }
        });
        this.renderTable();
    }

    async loadHolidays() {
        try {
            const [holResp, statResp] = await Promise.all([
                fetch("/api/holidays"),
                fetch("/api/status/holidays")
            ]);

            if (holResp.ok) {
                this.holidays = await holResp.json();
            }

            if (statResp.ok) {
                const statData = await statResp.json();
                this.renderTodayStatus(statData);
            }

            this.renderTable();
        } catch (err) {
            console.error("[HolidaysManager] Failed to load holidays:", err);
        }
    }

    renderTodayStatus(stat) {
        const statusContainer = document.getElementById("holidays-today-status");
        if (!statusContainer || !stat) return;

        const isNseHol = stat.nse && stat.nse.is_holiday_today;
        const isMcxHol = stat.mcx && stat.mcx.is_holiday_today;

        if (isNseHol) {
            statusContainer.innerHTML = `
                <span class="status-pill status-stopped" style="padding: 0.25rem 0.6rem; font-size: 0.72rem;">
                    🔴 TODAY IS HOLIDAY (NSE): ${stat.nse.holiday_name}
                </span>
            `;
        } else if (isMcxHol) {
            statusContainer.innerHTML = `
                <span class="status-pill status-warning" style="padding: 0.25rem 0.6rem; font-size: 0.72rem;">
                    🟠 MCX: ${stat.mcx.holiday_name}
                </span>
            `;
        } else {
            const openBadge = stat.is_any_market_open
                ? '<span class="status-pill status-running" style="padding: 0.25rem 0.6rem; font-size: 0.72rem;">🟢 Exchange Session Active</span>'
                : '<span class="status-pill status-stopped" style="padding: 0.25rem 0.6rem; font-size: 0.72rem;">⚪ Outside Trading Hours</span>';
            statusContainer.innerHTML = openBadge;
        }
    }

    renderTable() {
        const tbody = document.getElementById("holidays-table-body");
        if (!tbody) return;

        const todayStr = new Date().toISOString().split("T")[0];

        let list = [...this.holidays];
        if (this.currentYearFilter !== "ALL") {
            const yr = parseInt(this.currentYearFilter);
            list = list.filter(h => {
                const hDate = new Date(h.date);
                return h.year === yr || hDate.getFullYear() === yr;
            });
        }

        if (list.length === 0) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="7" style="text-align: center; padding: 2.5rem 1rem; color: var(--text-muted);">
                        <div style="font-size: 1.5rem; margin-bottom: 0.5rem;">📅</div>
                        No trading holidays found for the selected filter.
                        <div style="margin-top: 0.5rem;">
                            <button class="action-btn-sm" onclick="window.holidaysManager.openAddForm()" style="background: var(--accent-blue); color: white;">+ Add Holiday</button>
                        </div>
                    </td>
                </tr>
            `;
            return;
        }

        let html = "";
        list.forEach(h => {
            const isToday = (h.date === todayStr);
            const rowBg = isToday ? "background: rgba(239, 68, 68, 0.08);" : "";
            const todayBadge = isToday
                ? '<span class="status-pill status-stopped" style="margin-left: 6px; font-size: 0.65rem; padding: 2px 6px;">TODAY</span>'
                : "";

            const exchPill = `<span class="badge" style="background: rgba(59, 130, 246, 0.15); color: #3b82f6; font-size: 0.72rem; padding: 2px 6px; border-radius: 4px; font-weight: 600;">${h.exchange}</span>`;

            let sessionDesc = "Full Day Closed";
            let sessionPillClass = "status-stopped";
            if (h.session === "MORNING_CLOSED") {
                sessionDesc = "Morning Closed (Open 17:00)";
                sessionPillClass = "status-warning";
            }
            const sessionPill = `<span class="status-pill ${sessionPillClass}" style="font-size: 0.7rem; padding: 2px 6px;">${sessionDesc}</span>`;

            const activeToggle = `
                <button class="action-btn-sm" onclick="window.holidaysManager.toggleHolidayActive(${h.id}, ${!h.is_active})" 
                        style="padding: 2px 8px; font-size: 0.72rem; ${h.is_active ? 'background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3);' : 'background: rgba(100, 116, 139, 0.15); color: var(--text-muted);'}">
                    ${h.is_active ? 'Active' : 'Disabled'}
                </button>
            `;

            html += `
                <tr style="border-bottom: 1px solid var(--border-color); ${rowBg}">
                    <td style="padding: 0.65rem 1rem; font-family: var(--font-mono); font-weight: 600; color: var(--text-primary);">
                        ${h.date} ${todayBadge}
                    </td>
                    <td style="padding: 0.65rem 0.75rem; color: var(--text-secondary);">
                        ${h.day || ""}
                    </td>
                    <td style="padding: 0.65rem 0.75rem; font-weight: 600; color: var(--text-primary);">
                        ${h.description}
                    </td>
                    <td style="padding: 0.65rem 0.75rem;">
                        ${exchPill}
                    </td>
                    <td style="padding: 0.65rem 0.75rem;">
                        ${sessionPill}
                    </td>
                    <td style="padding: 0.65rem 0.75rem; text-align: center;">
                        ${activeToggle}
                    </td>
                    <td style="padding: 0.65rem 1rem; text-align: right;">
                        <button class="action-btn-sm" onclick="window.holidaysManager.openEditForm(${h.id})" 
                                style="padding: 3px 8px; font-size: 0.72rem; background: rgba(59, 130, 246, 0.12); color: #3b82f6; border: 1px solid rgba(59, 130, 246, 0.25); margin-right: 4px;" title="Edit Date/Details">
                            ✏️ Edit
                        </button>
                        <button class="action-btn-sm" onclick="window.holidaysManager.deleteHoliday(${h.id}, '${h.description.replace(/'/g, "\\'")}', '${h.date}')" 
                                style="padding: 3px 8px; font-size: 0.72rem; background: rgba(239, 68, 68, 0.12); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.25);" title="Delete Holiday">
                            🗑️
                        </button>
                    </td>
                </tr>
            `;
        });

        tbody.innerHTML = html;
    }

    openAddForm() {
        const container = document.getElementById("holiday-form-container");
        const title = document.getElementById("holiday-form-title");
        const idInput = document.getElementById("holiday-form-id");
        const dateInput = document.getElementById("holiday-input-date");
        const descInput = document.getElementById("holiday-input-desc");
        const exchSelect = document.getElementById("holiday-input-exch");
        const sessSelect = document.getElementById("holiday-input-sess");

        if (container) {
            container.style.display = "block";
            if (title) title.innerText = "Add New Trading Holiday";
            if (idInput) idInput.value = "";
            if (dateInput) {
                dateInput.value = "";
                dateInput.focus();
            }
            if (descInput) descInput.value = "";
            if (exchSelect) exchSelect.value = "ALL";
            if (sessSelect) sessSelect.value = "FULL_DAY";
        }
    }

    openEditForm(id) {
        const h = this.holidays.find(item => item.id === id);
        if (!h) return;

        const container = document.getElementById("holiday-form-container");
        const title = document.getElementById("holiday-form-title");
        const idInput = document.getElementById("holiday-form-id");
        const dateInput = document.getElementById("holiday-input-date");
        const descInput = document.getElementById("holiday-input-desc");
        const exchSelect = document.getElementById("holiday-input-exch");
        const sessSelect = document.getElementById("holiday-input-sess");

        if (container) {
            container.style.display = "block";
            if (title) title.innerText = `Edit Trading Holiday: ${h.description}`;
            if (idInput) idInput.value = h.id;
            if (dateInput) dateInput.value = h.date;
            if (descInput) descInput.value = h.description;
            if (exchSelect) exchSelect.value = h.exchange || "ALL";
            if (sessSelect) sessSelect.value = h.session || "FULL_DAY";
            if (dateInput) dateInput.focus();
        }
    }

    closeForm() {
        const container = document.getElementById("holiday-form-container");
        if (container) container.style.display = "none";
    }

    async handleFormSubmit(e) {
        e.preventDefault();
        const idVal = document.getElementById("holiday-form-id").value;
        const dateVal = document.getElementById("holiday-input-date").value;
        const descVal = document.getElementById("holiday-input-desc").value;
        const exchVal = document.getElementById("holiday-input-exch").value;
        const sessVal = document.getElementById("holiday-input-sess").value;

        if (!dateVal || !descVal) {
            alert("Please provide both Date and Description for the holiday.");
            return;
        }

        const payload = {
            date: dateVal,
            description: descVal,
            exchange: exchVal,
            session: sessVal,
            is_active: true
        };

        try {
            let resp;
            if (idVal) {
                resp = await fetch(`/api/holidays/${idVal}`, {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                });
            } else {
                resp = await fetch("/api/holidays", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                });
            }

            if (resp.ok) {
                this.closeForm();
                await this.loadHolidays();
            } else {
                const err = await resp.json();
                alert(err.detail || "Failed to save holiday");
            }
        } catch (err) {
            console.error("[HolidaysManager] Form save error:", err);
            alert("Network error saving holiday");
        }
    }

    async deleteHoliday(id, desc, date) {
        if (!confirm(`Are you sure you want to remove ${desc} (${date}) from the trading holiday schedule?`)) {
            return;
        }

        try {
            const resp = await fetch(`/api/holidays/${id}`, { method: "DELETE" });
            if (resp.ok) {
                await this.loadHolidays();
            } else {
                const err = await resp.json();
                alert(err.detail || "Failed to delete holiday");
            }
        } catch (err) {
            console.error("[HolidaysManager] Delete error:", err);
            alert("Network error deleting holiday");
        }
    }

    async toggleHolidayActive(id, newActive) {
        try {
            const resp = await fetch(`/api/holidays/${id}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ is_active: newActive })
            });
            if (resp.ok) {
                await this.loadHolidays();
            }
        } catch (err) {
            console.error("[HolidaysManager] Toggle error:", err);
        }
    }
}

// Global Singleton Initialization
window.holidaysManager = new HolidaysManager();
