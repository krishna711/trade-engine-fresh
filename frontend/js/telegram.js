/**
 * Telegram Notification Settings & Live Test Manager
 * Supports:
 * - 3 Message tiers: Critical (🚨), Warning (⚠️), and Info (ℹ️)
 * - Individual user configuration (Enable/Disable, Custom Bot Token, Numeric Chat ID)
 * - Admin multi-user subscription controls (Admin receives all by default, can mute specific users for admin only)
 * - Direct UI test message dispatcher
 */

class TelegramManager {
    constructor() {
        this.settings = null;
        this.init();
    }

    init() {
        // Modal close buttons
        document.querySelectorAll("#modal-telegram-settings .modal-close-btn").forEach(btn => {
            btn.onclick = () => this.closeTelegramModal();
        });

        // Background overlay close
        const modal = document.getElementById("modal-telegram-settings");
        if (modal) {
            modal.onclick = (e) => {
                if (e.target === modal) this.closeTelegramModal();
            };
        }

        // Live status pill toggle listener
        const enabledToggle = document.getElementById("tg-enabled-toggle");
        if (enabledToggle) {
            enabledToggle.onchange = () => {
                const badge = document.getElementById("tg-status-badge");
                if (badge) {
                    if (enabledToggle.checked) {
                        badge.innerText = "Active";
                        badge.className = "status-pill status-running";
                    } else {
                        badge.innerText = "Disabled";
                        badge.className = "status-pill status-stopped";
                    }
                }
            };
        }
    }

    async openTelegramModal() {
        if (window.authManager) window.authManager.closeUserDropdown();
        const modal = document.getElementById("modal-telegram-settings");
        if (!modal) return;

        this.resetAlert();
        modal.classList.add("active");
        modal.style.display = "";

        await this.loadSettings();
    }

    closeTelegramModal() {
        const modal = document.getElementById("modal-telegram-settings");
        if (modal) modal.classList.remove("active");
    }

    async loadSettings() {
        try {
            const resp = await fetch("/api/telegram/settings");
            if (!resp.ok) throw new Error("Failed to load settings");
            const data = await resp.json();
            this.settings = data;
            this.renderForm();
        } catch (e) {
            this.showAlert("Error loading Telegram settings: " + e.message, false);
        }
    }

    renderForm() {
        if (!this.settings) return;

        const enabledToggle = document.getElementById("tg-enabled-toggle");
        const statusBadge = document.getElementById("tg-status-badge");
        const botTokenInput = document.getElementById("tg-bot-token");
        const chatIdInput = document.getElementById("tg-chat-id");
        const critCheckbox = document.getElementById("tg-notify-critical");
        const warnCheckbox = document.getElementById("tg-notify-warning");
        const infoCheckbox = document.getElementById("tg-notify-info");
        const adminSection = document.getElementById("tg-admin-section");

        if (enabledToggle) enabledToggle.checked = Boolean(this.settings.enabled);
        if (statusBadge) {
            if (this.settings.enabled) {
                statusBadge.innerText = "Active";
                statusBadge.className = "status-pill status-running";
            } else {
                statusBadge.innerText = "Disabled";
                statusBadge.className = "status-pill status-stopped";
            }
        }

        if (botTokenInput) botTokenInput.value = this.settings.bot_token || "";
        if (chatIdInput) chatIdInput.value = this.settings.chat_id || "";

        if (critCheckbox) critCheckbox.checked = this.settings.notify_critical !== false;
        if (warnCheckbox) warnCheckbox.checked = this.settings.notify_warning !== false;
        if (infoCheckbox) infoCheckbox.checked = this.settings.notify_info !== false;

        // Admin Multi-User filtering section
        if (adminSection) {
            if (this.settings.is_admin && Array.isArray(this.settings.users_for_admin)) {
                adminSection.style.display = "block";
                this.renderAdminUsersList(this.settings.users_for_admin);
            } else {
                adminSection.style.display = "none";
            }
        }
    }

    renderAdminUsersList(users) {
        const tbody = document.getElementById("tg-admin-users-tbody");
        if (!tbody) return;
        tbody.innerHTML = "";

        if (!users || users.length === 0) {
            tbody.innerHTML = `<tr><td colspan="3" style="text-align: center; color: var(--text-muted); padding: 0.75rem;">No other users found.</td></tr>`;
            return;
        }

        users.forEach(u => {
            const isMuted = Boolean(u.is_muted_for_admin);
            const tr = document.createElement("tr");
            tr.innerHTML = `
                <td style="padding: 0.6rem 0.75rem;">
                    <div style="font-weight: 600; color: var(--text-primary); font-size: 0.82rem;">${u.full_name || u.username}</div>
                    <div style="font-size: 0.72rem; color: var(--text-muted); font-family: var(--font-mono);">@${u.username} • <span class="badge ${u.role === 'ADMIN' ? 'role-admin' : 'role-user'}" style="font-size: 0.65rem; padding: 1px 5px;">${u.role}</span></div>
                </td>
                <td style="padding: 0.6rem 0.75rem;">
                    <span class="status-pill ${u.has_telegram ? 'status-running' : 'status-stopped'}" style="font-size: 0.7rem; padding: 2px 8px;">
                        ${u.has_telegram ? '📱 Active' : '⚪ Not Set'}
                    </span>
                </td>
                <td style="padding: 0.6rem 0.75rem; text-align: right;">
                    <label style="cursor: pointer; display: inline-flex; align-items: center; gap: 8px;">
                        <input type="checkbox" class="admin-user-sub-toggle" data-user-id="${u.id}" ${!isMuted ? 'checked' : ''} style="width: 16px; height: 16px; accent-color: var(--accent-blue); cursor: pointer;">
                        <span class="sub-label" style="font-size: 0.78rem; color: ${!isMuted ? 'var(--accent-green, #10b981)' : 'var(--text-muted)'}; font-weight: 600;">
                            ${!isMuted ? 'Subscribed' : 'Muted'}
                        </span>
                    </label>
                </td>
            `;
            tbody.appendChild(tr);
        });

        // Add event listener to toggles to update label dynamically
        tbody.querySelectorAll(".admin-user-sub-toggle").forEach(cb => {
            cb.onchange = (e) => {
                const label = e.target.parentElement.querySelector(".sub-label");
                if (label) {
                    label.innerText = e.target.checked ? "Subscribed" : "Muted";
                    label.style.color = e.target.checked ? "var(--accent-green, #10b981)" : "var(--text-muted)";
                }
            };
        });
    }

    async saveSettings() {
        const saveBtn = document.getElementById("btn-tg-save");
        if (saveBtn) {
            saveBtn.disabled = true;
            saveBtn.innerText = "Saving...";
        }

        try {
            const enabled = document.getElementById("tg-enabled-toggle")?.checked || false;
            const botToken = document.getElementById("tg-bot-token")?.value.trim() || "";
            const chatId = document.getElementById("tg-chat-id")?.value.trim() || "";
            const notifyCritical = document.getElementById("tg-notify-critical")?.checked ?? true;
            const notifyWarning = document.getElementById("tg-notify-warning")?.checked ?? true;
            const notifyInfo = document.getElementById("tg-notify-info")?.checked ?? true;

            const payload = {
                enabled,
                bot_token: botToken,
                chat_id: chatId,
                notify_critical: notifyCritical,
                notify_warning: notifyWarning,
                notify_info: notifyInfo
            };

            // If Admin, collect muted user IDs
            if (this.settings && this.settings.is_admin) {
                const mutedIds = [];
                document.querySelectorAll(".admin-user-sub-toggle").forEach(cb => {
                    if (!cb.checked) {
                        const uid = parseInt(cb.dataset.userId, 10);
                        if (!isNaN(uid)) mutedIds.push(uid);
                    }
                });
                payload.admin_muted_user_ids = mutedIds;
            }

            const resp = await fetch("/api/telegram/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });

            const data = await resp.json();
            if (resp.ok && data.success) {
                this.showAlert("✅ Telegram settings saved successfully!", true);
                await this.loadSettings();
            } else {
                this.showAlert("⚠️ " + (data.detail || data.message || "Failed to save settings"), false);
            }
        } catch (e) {
            this.showAlert("⚠️ Network error: " + e.message, false);
        } finally {
            if (saveBtn) {
                saveBtn.disabled = false;
                saveBtn.innerText = "💾 Save Settings";
            }
        }
    }

    async sendTestNotification() {
        const testBtn = document.getElementById("btn-tg-test");
        if (testBtn) {
            testBtn.disabled = true;
            testBtn.innerText = "Sending Test...";
        }

        const botToken = document.getElementById("tg-bot-token")?.value.trim() || "";
        const chatId = document.getElementById("tg-chat-id")?.value.trim() || "";

        try {
            const resp = await fetch("/api/telegram/test", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    bot_token: botToken,
                    chat_id: chatId,
                    level: "ALL"
                })
            });

            const data = await resp.json();
            if (resp.ok && data.success) {
                this.showAlert("🚀 " + data.message, true);
            } else {
                this.showAlert("❌ " + (data.detail || data.message || "Delivery failed"), false);
            }
        } catch (e) {
            this.showAlert("❌ Connection error: " + e.message, false);
        } finally {
            if (testBtn) {
                testBtn.disabled = false;
                testBtn.innerText = "🧪 Send Test Message";
            }
        }
    }

    showAlert(msg, isSuccess = false) {
        const box = document.getElementById("tg-alert-box");
        if (!box) return;
        box.style.display = "block";
        box.innerText = msg;
        box.style.background = isSuccess ? "rgba(16, 185, 129, 0.15)" : "rgba(239, 68, 68, 0.15)";
        box.style.color = isSuccess ? "#10b981" : "#ef4444";
        box.style.border = `1px solid ${isSuccess ? "rgba(16, 185, 129, 0.35)" : "rgba(239, 68, 68, 0.35)"}`;
    }

    resetAlert() {
        const box = document.getElementById("tg-alert-box");
        if (box) {
            box.style.display = "none";
            box.innerText = "";
        }
    }
}

window.telegramManager = new TelegramManager();
