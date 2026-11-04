/**
 * Authentication and Session Gate Manager
 * Intercepts all API calls to automatically inject Authorization headers,
 * enforces login screen in incognito private mode or unauthenticated visits,
 * and maintains role isolation between ADMIN and USER.
 */

// 1. Global Fetch Interceptor
(function() {
    const originalFetch = window.fetch;
    window.fetch = async function(url, options = {}) {
        options = options || {};
        options.headers = options.headers || {};
        
        const token = localStorage.getItem("fresh_trade_auth_token");
        if (token) {
            if (options.headers instanceof Headers) {
                if (!options.headers.has("Authorization")) {
                    options.headers.set("Authorization", `Bearer ${token}`);
                }
            } else if (Array.isArray(options.headers)) {
                options.headers.push(["Authorization", `Bearer ${token}`]);
            } else {
                if (!options.headers["Authorization"]) {
                    options.headers["Authorization"] = `Bearer ${token}`;
                }
            }
        }

        const response = await originalFetch(url, options);

        // If server returns 401 Unauthorized (and not login endpoint itself)
        if (response.status === 401 && typeof url === "string" && !url.includes("/api/auth/login")) {
            console.warn("[Auth Interceptor] 401 Unauthorized for:", url);
            if (window.authManager) {
                window.authManager.handleUnauthorized();
            }
        }

        return response;
    };
})();


class AuthManager {
    constructor() {
        this.token = localStorage.getItem("fresh_trade_auth_token") || null;
        this.user = null;
        try {
            const rawUser = localStorage.getItem("fresh_trade_user");
            if (rawUser) this.user = JSON.parse(rawUser);
        } catch (e) {
            this.user = null;
        }

        window.authManager = this;
        this.init();
    }

    getToken() {
        return this.token;
    }

    getUser() {
        return this.user;
    }

    isAuthenticated() {
        return Boolean(this.token && this.user);
    }

    init() {
        this.bindEvents();
        this.checkInitialAuthState();
    }

    bindEvents() {
        // Login form submit
        const form = document.getElementById("form-login");
        if (form) {
            form.onsubmit = (e) => {
                e.preventDefault();
                this.handleLoginSubmit();
            };
        }

        // Quick sign-in chips
        const chipAdmin = document.getElementById("chip-login-admin");
        if (chipAdmin) {
            chipAdmin.onclick = () => {
                const u = document.getElementById("login-username");
                const p = document.getElementById("login-password");
                if (u) u.value = "admin";
                if (p) p.value = "admin123";
                this.handleLoginSubmit();
            };
        }

        const chipTrader = document.getElementById("chip-login-trader");
        if (chipTrader) {
            chipTrader.onclick = () => {
                const u = document.getElementById("login-username");
                const p = document.getElementById("login-password");
                if (u) u.value = "trader";
                if (p) p.value = "trader123";
                this.handleLoginSubmit();
            };
        }
    }

    async checkInitialAuthState() {
        const loginOverlay = document.getElementById("view-login");
        const appShell = document.getElementById("app-shell");

        if (!this.token) {
            // Unauthenticated (Incognito or logged out): Strictly lock interface
            if (loginOverlay) loginOverlay.style.display = "flex";
            if (appShell) appShell.style.display = "none";
            return;
        }

        try {
            const resp = await fetch("/api/auth/me");
            if (resp.ok) {
                const data = await resp.json();
                if (data.success && data.user) {
                    this.user = data.user;
                    localStorage.setItem("fresh_trade_user", JSON.stringify(this.user));
                    this.showAuthenticatedUI();
                    return;
                }
            }
            // Token is invalid/expired
            this.handleUnauthorized("Session expired. Please sign in again.");
        } catch (e) {
            console.error("[Auth] Session validation error:", e);
            // In case of network glitch but token exists, show authenticated UI and retry in background
            this.showAuthenticatedUI();
        }
    }

    async handleLoginSubmit() {
        const usernameInp = document.getElementById("login-username");
        const passwordInp = document.getElementById("login-password");
        const submitBtn = document.getElementById("btn-login-submit");
        const errorAlert = document.getElementById("login-error-alert");

        const username = usernameInp ? usernameInp.value.trim() : "";
        const password = passwordInp ? passwordInp.value.trim() : "";

        if (errorAlert) {
            errorAlert.style.display = "none";
            errorAlert.innerText = "";
        }

        if (!username || !password) {
            this.showLoginError("Please enter both username and password.");
            return;
        }

        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.innerText = "Authenticating...";
        }

        try {
            const resp = await fetch("/api/auth/login", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ username, password })
            });

            const data = await resp.json();

            if (resp.ok && data.success && data.token) {
                this.token = data.token;
                this.user = data.user;
                localStorage.setItem("fresh_trade_auth_token", this.token);
                localStorage.setItem("fresh_trade_user", JSON.stringify(this.user));
                localStorage.setItem("fresh_trade_role", this.user.role);

                this.showAuthenticatedUI();

                // Trigger application live reload
                if (window.app) {
                    window.app.applyRole(this.user.role);
                    window.app.connectWebSocket();
                    window.app.refreshAll();
                }
            } else {
                this.showLoginError(data.detail || data.message || "Invalid credentials. Please try again.");
            }
        } catch (err) {
            this.showLoginError(`Network error: ${err.message}`);
        } finally {
            if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.innerText = "Sign In to Terminal";
            }
        }
    }

    showLoginError(msg) {
        const errorAlert = document.getElementById("login-error-alert");
        if (errorAlert) {
            errorAlert.innerText = `⚠️ ${msg}`;
            errorAlert.style.display = "flex";
        } else {
            alert(msg);
        }
    }

    showAuthenticatedUI() {
        const loginOverlay = document.getElementById("view-login");
        const appShell = document.getElementById("app-shell");

        if (loginOverlay) loginOverlay.style.display = "none";
        if (appShell) appShell.style.display = "block";

        // Update header user profile
        this.updateHeaderProfile();

        // Apply role UI
        if (this.user && window.app) {
            window.app.applyRole(this.user.role);
        }
    }

    updateHeaderProfile() {
        const nameEl = document.getElementById("header-user-name");
        const roleBadge = document.getElementById("header-user-role-badge");
        const avatarEl = document.getElementById("header-user-avatar");
        const roleSwitcher = document.querySelector(".role-switcher");

        if (!this.user) return;

        if (nameEl) nameEl.innerText = this.user.full_name || this.user.username;
        if (roleBadge) {
            roleBadge.innerText = this.user.role;
            roleBadge.className = `header-user-role-badge ${this.user.role === 'ADMIN' ? 'header-role-admin' : 'header-role-user'}`;
        }
        if (avatarEl) {
            avatarEl.innerText = this.user.role === "ADMIN" ? "🛡️" : "👤";
        }

        // Normal users cannot toggle into Admin mode
        if (roleSwitcher) {
            roleSwitcher.style.display = this.user.role === "ADMIN" ? "inline-flex" : "none";
        }
    }

    logout() {
        this.token = null;
        this.user = null;
        localStorage.removeItem("fresh_trade_auth_token");
        localStorage.removeItem("fresh_trade_user");

        // Disconnect WebSocket
        if (window.app && window.app.ws) {
            try {
                window.app.ws.close();
            } catch (e) {}
        }

        // Hide app, show login gate
        const loginOverlay = document.getElementById("view-login");
        const appShell = document.getElementById("app-shell");
        if (loginOverlay) loginOverlay.style.display = "flex";
        if (appShell) appShell.style.display = "none";

        // Call backend logout
        fetch("/api/auth/logout", { method: "POST" }).catch(() => {});
    }

    handleUnauthorized(msg = "Session expired or authentication required.") {
        this.token = null;
        this.user = null;
        localStorage.removeItem("fresh_trade_auth_token");
        localStorage.removeItem("fresh_trade_user");

        const loginOverlay = document.getElementById("view-login");
        const appShell = document.getElementById("app-shell");

        if (loginOverlay) loginOverlay.style.display = "flex";
        if (appShell) appShell.style.display = "none";

        this.showLoginError(msg);
    }
}

// Instantiate immediately so fetch interceptor is live before any other script runs
window.authManager = new AuthManager();

