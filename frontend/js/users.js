/**
 * User & Role Management Component (Admin Feature)
 * Allows Admins to create, edit, deactivate, and manage ADMIN and USER accounts.
 */

class UserManager {
    constructor() {
        this.users = [];
        this.currentFilterRole = "";
        this.searchTerm = "";
        this.init();
    }

    init() {
        this.bindEvents();
    }

    bindEvents() {
        // Modal close buttons
        document.querySelectorAll("#modal-manage-users .modal-close-btn").forEach(btn => {
            btn.onclick = () => this.closeUsersModal();
        });
        document.querySelectorAll("#modal-user-form .modal-close-btn").forEach(btn => {
            btn.onclick = () => this.closeUserFormModal();
        });

        // Search & Filter
        const searchInput = document.getElementById("users-search-input");
        if (searchInput) {
            searchInput.oninput = (e) => {
                this.searchTerm = e.target.value.toLowerCase().trim();
                this.renderUsers();
            };
        }

        const roleFilter = document.getElementById("users-role-filter");
        if (roleFilter) {
            roleFilter.onchange = (e) => {
                this.currentFilterRole = e.target.value;
                this.renderUsers();
            };
        }
    }

    async openUsersModal() {
        const modal = document.getElementById("modal-manage-users");
        if (!modal) return;
        modal.classList.add("active");
        await this.loadUsers();
    }

    closeUsersModal() {
        const modal = document.getElementById("modal-manage-users");
        if (modal) modal.classList.remove("active");
    }

    openUserFormModal(user = null) {
        const modal = document.getElementById("modal-user-form");
        if (!modal) return;

        const titleEl = document.getElementById("user-form-title");
        const idInp = document.getElementById("user-form-id");
        const usernameInp = document.getElementById("user-form-username");
        const fullnameInp = document.getElementById("user-form-fullname");
        const roleSel = document.getElementById("user-form-role");
        const emailInp = document.getElementById("user-form-email");
        const pwdInp = document.getElementById("user-form-password");
        const confirmInp = document.getElementById("user-form-confirm-password");
        const activeChk = document.getElementById("user-form-active");
        const errorDiv = document.getElementById("user-form-error");

        if (errorDiv) {
            errorDiv.innerText = "";
            errorDiv.style.display = "none";
        }

        if (user) {
            // Edit mode
            if (titleEl) titleEl.innerText = `Edit User: ${user.username}`;
            if (idInp) idInp.value = user.id;
            if (usernameInp) {
                usernameInp.value = user.username;
                usernameInp.disabled = true; // username immutable on edit
            }
            if (fullnameInp) fullnameInp.value = user.full_name || "";
            if (roleSel) roleSel.value = user.role || "USER";
            if (emailInp) emailInp.value = user.email || "";
            if (pwdInp) {
                pwdInp.value = "";
                pwdInp.placeholder = "Leave blank to keep current password";
                pwdInp.required = false;
            }
            if (confirmInp) {
                confirmInp.value = "";
                confirmInp.placeholder = "Leave blank to keep current password";
                confirmInp.required = false;
            }
            if (activeChk) activeChk.checked = Boolean(user.is_active);
        } else {
            // Add mode
            if (titleEl) titleEl.innerText = "Add New User";
            if (idInp) idInp.value = "";
            if (usernameInp) {
                usernameInp.value = "";
                usernameInp.disabled = false;
            }
            if (fullnameInp) fullnameInp.value = "";
            if (roleSel) roleSel.value = "USER";
            if (emailInp) emailInp.value = "";
            if (pwdInp) {
                pwdInp.value = "";
                pwdInp.placeholder = "Enter password (min 4 characters)";
                pwdInp.required = true;
            }
            if (confirmInp) {
                confirmInp.value = "";
                confirmInp.placeholder = "Confirm password";
                confirmInp.required = true;
            }
            if (activeChk) activeChk.checked = true;
        }

        modal.classList.add("active");
    }

    closeUserFormModal() {
        const modal = document.getElementById("modal-user-form");
        if (modal) modal.classList.remove("active");
    }

    async loadUsers() {
        try {
            const resp = await fetch("/api/users");
            const data = await resp.json();
            if (data.success) {
                this.users = data.users || [];
                this.renderUsers();
            }
        } catch (err) {
            console.error("Failed to load users:", err);
        }
    }

    renderUsers() {
        const tbody = document.getElementById("users-table-tbody");
        const countBadge = document.getElementById("users-total-count");
        if (!tbody) return;

        let filtered = this.users;
        if (this.currentFilterRole) {
            filtered = filtered.filter(u => u.role === this.currentFilterRole);
        }
        if (this.searchTerm) {
            filtered = filtered.filter(u => 
                (u.username && u.username.toLowerCase().includes(this.searchTerm)) ||
                (u.full_name && u.full_name.toLowerCase().includes(this.searchTerm)) ||
                (u.email && u.email.toLowerCase().includes(this.searchTerm))
            );
        }

        if (countBadge) countBadge.innerText = `${filtered.length} Users`;
        tbody.innerHTML = "";

        if (filtered.length === 0) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="6" style="text-align: center; color: var(--text-muted); padding: 2rem;">
                        No users found matching your criteria.
                    </td>
                </tr>
            `;
            return;
        }

        filtered.forEach(u => {
            const tr = document.createElement("tr");

            const roleBadgeClass = u.role === "ADMIN" ? "user-badge-admin" : "user-badge-normal";
            const roleIcon = u.role === "ADMIN" ? "🛡️" : "👤";
            const statusClass = u.is_active ? "status-running" : "status-stopped";
            const statusText = u.is_active ? "ACTIVE" : "INACTIVE";

            tr.innerHTML = `
                <td style="font-weight: 700; font-family: var(--font-mono); color: var(--text-primary);">
                    ${u.username}
                </td>
                <td style="font-weight: 500;">${u.full_name || "-"}</td>
                <td style="color: var(--text-secondary); font-size: 0.8rem;">${u.email || "-"}</td>
                <td>
                    <span class="user-role-badge ${roleBadgeClass}">
                        ${roleIcon} ${u.role}
                    </span>
                </td>
                <td>
                    <span class="status-pill ${statusClass}">${statusText}</span>
                </td>
                <td style="text-align: right; white-space: nowrap;">
                    <button class="action-btn-sm" onclick='window.userManager.editUserById(${u.id})' title="Edit user details or role" style="margin-right: 4px; padding: 3px 8px; font-size: 0.75rem;">
                        ✏️ Edit
                    </button>
                    <button class="action-btn-sm btn-delete-user" onclick='window.userManager.deleteUserById(${u.id}, "${u.username}")' title="Delete user" style="padding: 3px 8px; font-size: 0.75rem; background: rgba(239, 68, 68, 0.15); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.3);">
                        🗑️ Delete
                    </button>
                </td>
            `;
            tbody.appendChild(tr);
        });
    }

    editUserById(id) {
        const user = this.users.find(u => u.id === id);
        if (user) {
            this.openUserFormModal(user);
        }
    }

    async deleteUserById(id, username) {
        if (!confirm(`Are you sure you want to delete user '${username}'? This action cannot be undone.`)) {
            return;
        }

        try {
            const resp = await fetch(`/api/users/${id}`, { method: "DELETE" });
            const data = await resp.json();
            if (data.success) {
                await this.loadUsers();
            } else {
                alert(data.detail || data.message || "Failed to delete user.");
            }
        } catch (err) {
            alert(`Error deleting user: ${err.message}`);
        }
    }

    async submitUserForm() {
        const idInp = document.getElementById("user-form-id");
        const usernameInp = document.getElementById("user-form-username");
        const fullnameInp = document.getElementById("user-form-fullname");
        const roleSel = document.getElementById("user-form-role");
        const emailInp = document.getElementById("user-form-email");
        const pwdInp = document.getElementById("user-form-password");
        const confirmInp = document.getElementById("user-form-confirm-password");
        const activeChk = document.getElementById("user-form-active");
        const errorDiv = document.getElementById("user-form-error");

        const isEdit = Boolean(idInp && idInp.value);
        const userId = idInp ? idInp.value : null;

        const username = usernameInp ? usernameInp.value.trim() : "";
        const fullName = fullnameInp ? fullnameInp.value.trim() : "";
        const role = roleSel ? roleSel.value : "USER";
        const email = emailInp ? emailInp.value.trim() : "";
        const password = pwdInp ? pwdInp.value.trim() : "";
        const confirmPassword = confirmInp ? confirmInp.value.trim() : "";
        const isActive = activeChk ? activeChk.checked : true;

        if (errorDiv) {
            errorDiv.style.display = "none";
            errorDiv.innerText = "";
        }

        const showError = (msg) => {
            if (errorDiv) {
                errorDiv.innerText = msg;
                errorDiv.style.display = "block";
            } else {
                alert(msg);
            }
        };

        if (!isEdit && !username) {
            showError("Username is required.");
            return;
        }

        if (!isEdit && (!password || password.length < 4)) {
            showError("Password must be at least 4 characters.");
            return;
        }

        if (password && password !== confirmPassword) {
            showError("Passwords do not match.");
            return;
        }

        const payload = {
            full_name: fullName,
            role: role,
            email: email,
            is_active: isActive
        };

        if (!isEdit) {
            payload.username = username;
            payload.password = password;
        } else if (password) {
            payload.password = password;
        }

        try {
            const url = isEdit ? `/api/users/${userId}` : `/api/users`;
            const method = isEdit ? "PUT" : "POST";

            const resp = await fetch(url, {
                method: method,
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });

            const data = await resp.json();
            if (resp.ok && data.success) {
                this.closeUserFormModal();
                await this.loadUsers();
            } else {
                showError(data.detail || data.message || "Failed to save user.");
            }
        } catch (err) {
            showError(`Network error: ${err.message}`);
        }
    }
}

// Global instance
window.userManager = new UserManager();

