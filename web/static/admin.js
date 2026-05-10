// Admin panel — runtime configuration UI.
// Vanilla JS, single page, talks to /api/v1/admin/*.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);
const ADMIN = "/api/v1/admin";

// Read the CSRF token cookie issued at login. Non-HttpOnly by design so
// JS can echo it on every state-changing request via X-CSRF-Token. The
// server compares cookie + header via constant-time compare.
function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

async function api(path, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  const headers = {
    "Content-Type": "application/json",
    ...(opts.headers || {}),
  };
  // State-changing methods carry the CSRF token. GET/HEAD don't need it.
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const t = csrfToken();
    if (t) headers["X-CSRF-Token"] = t;
  }
  const r = await fetch(ADMIN + path, {
    credentials: "include",
    headers,
    ...opts,
  });
  if (r.status === 401) {
    showLogin();
    throw new Error("not authenticated");
  }
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try {
      const body = await r.json();
      if (body?.error?.message) msg = body.error.message;
      else if (body?.detail) msg = body.detail;
    } catch (_) { /* not json */ }
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

function showLogin() {
  $("#login-screen").hidden = false;
  $("#admin-shell").hidden = true;
}
function showShell() {
  $("#login-screen").hidden = true;
  $("#admin-shell").hidden = false;
}
function toast(msg, kind = "info") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (kind === "error" ? " error" : "");
  t.hidden = false;
  setTimeout(() => { t.hidden = true; }, 2400);
}

// -------- Tabs --------
$$(".tab").forEach((t) => t.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.remove("active"));
  $$(".panel").forEach((x) => x.classList.remove("active"));
  t.classList.add("active");
  $(`#panel-${t.dataset.tab}`).classList.add("active");
  if (t.dataset.tab === "audit") loadAudit();
}));

// -------- Login --------
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const password = $("#password").value;
  const totp = $("#totp-code").value.trim();
  const errBox = $("#login-error");
  errBox.hidden = true;
  try {
    const body = totp ? { password, totp } : { password };
    const r = await fetch(ADMIN + "/login", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      errBox.textContent = "Invalid credentials.";
      errBox.hidden = false;
      return;
    }
    showShell();
    await loadSettings();
  } catch (e) {
    errBox.textContent = "Network error.";
    errBox.hidden = false;
  }
});

// -------- Logout --------
$("#logout-btn").addEventListener("click", async () => {
  await fetch(ADMIN + "/logout", { method: "POST", credentials: "include" });
  showLogin();
});

// -------- Settings --------
async function loadSettings() {
  try {
    const cats = await api("/settings");
    renderSettings(cats);
  } catch (e) {
    if (e.message !== "not authenticated") toast(e.message, "error");
  }
}

const TYPE_BADGES = { str: "string", int: "int", float: "float", bool: "bool", list: "list", json: "json", secret: "secret" };

function renderSettings(categories) {
  const container = $("#settings-tree");
  container.innerHTML = categories.map(({ category, settings }) => `
    <section class="category">
      <header class="category-header">${escapeHtml(category)}</header>
      ${settings.map((s) => settingRow(s)).join("")}
    </section>
  `).join("");

  // Wire change handlers
  $$(".setting-row").forEach((row) => {
    const input = row.querySelector(".setting-input input, .setting-input select");
    const initial = JSON.stringify(parseValue(input));
    input.addEventListener("input", () => {
      const dirty = JSON.stringify(parseValue(input)) !== initial;
      row.classList.toggle("dirty", dirty);
    });
    input.addEventListener("blur", async () => {
      if (!row.classList.contains("dirty")) return;
      const key = row.dataset.key;
      const isSecret = input.dataset.type === "secret";
      // Empty + secret = "leave the existing key in place." Don't overwrite.
      if (isSecret && input.value === "") {
        row.classList.remove("dirty");
        return;
      }
      try {
        const updated = await api(`/settings/${encodeURIComponent(key)}`, {
          method: "PUT",
          body: JSON.stringify({ value: parseValue(input) }),
        });
        if (isSecret) {
          // Re-render so the input clears and the placeholder shows the new
          // masked form. The raw value never round-trips.
          replaceRow(row, updated);
        } else {
          row.classList.remove("dirty");
          row.querySelector(".setting-meta").innerHTML = metaHtml(updated);
        }
        toast(`saved · ${key}`);
      } catch (e) {
        toast(e.message, "error");
      }
    });

    row.querySelector(".reset-btn").addEventListener("click", async () => {
      const key = row.dataset.key;
      if (!confirm(`Reset ${key} to its default?`)) return;
      try {
        const updated = await api(`/settings/${encodeURIComponent(key)}/reset`, {
          method: "POST",
        });
        replaceRow(row, updated);
        toast(`reset · ${key}`);
      } catch (e) {
        toast(e.message, "error");
      }
    });
  });
}

function settingRow(s) {
  return `
    <div class="setting-row" data-key="${escapeHtml(s.key)}">
      <div>
        <div class="setting-key">${escapeHtml(s.key)}</div>
        <div class="setting-desc">${escapeHtml(s.description)}</div>
      </div>
      <div class="setting-input">${inputFor(s)}</div>
      <div class="setting-meta">${metaHtml(s)}</div>
    </div>`;
}

function inputFor(s) {
  if (s.type === "bool") {
    return `
      <select>
        <option value="true" ${s.value ? "selected" : ""}>true</option>
        <option value="false" ${!s.value ? "selected" : ""}>false</option>
      </select>`;
  }
  if (s.type === "secret") {
    // Server returns a masked placeholder ("••••••<last4>") so the raw key
    // never leaves the DB. Render an empty password input — typing rotates
    // the secret; leaving it empty keeps the existing value (the input is
    // disabled until the operator focuses to rotate).
    const placeholder = s.value ? String(s.value) : "(not set)";
    return `<input type="password" value="" placeholder="${escapeHtml(placeholder)}"
                   autocomplete="new-password" data-type="secret"
                   data-secret-empty-keeps-current="1">`;
  }
  if (s.type === "json") {
    const v = typeof s.value === "string" ? s.value : JSON.stringify(s.value);
    return `<input type="text" value="${escapeHtml(v)}" data-type="json"
                   placeholder='{"tenant_hash": "500/minute"}'>`;
  }
  const v = s.type === "list" ? s.value.join(", ") : String(s.value);
  return `<input type="text" value="${escapeHtml(v)}" data-type="${s.type}">`;
}

function parseValue(el) {
  const t = el.dataset.type || (el.tagName === "SELECT" ? "bool" : "str");
  const raw = el.value;
  if (t === "bool") return raw === "true";
  if (t === "int") return parseInt(raw, 10);
  if (t === "float") return parseFloat(raw);
  if (t === "list") return raw.split(",").map((s) => s.trim()).filter(Boolean);
  return raw;
}

function metaHtml(s) {
  const ago = relTime(s.updated_at);
  const by = s.updated_by ? ` by <strong>${escapeHtml(s.updated_by)}</strong>` : "";
  return `
    <div>${TYPE_BADGES[s.type] || s.type}</div>
    <div>updated ${ago}${by}</div>
    <button class="reset-btn" type="button">Reset to default</button>`;
}

function replaceRow(row, s) {
  row.outerHTML = settingRow(s);
  // Re-wire after innerHTML replacement
  loadSettings();
}

function relTime(iso) {
  const t = new Date(iso).getTime();
  const ms = Date.now() - t;
  if (ms < 60_000) return "just now";
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`;
  return new Date(iso).toLocaleDateString();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// -------- Audit --------
async function loadAudit() {
  try {
    const entries = await api("/audit?limit=200");
    const rows = entries.map((e) => `
      <tr>
        <td>${new Date(e.timestamp).toLocaleString()}</td>
        <td>${escapeHtml(e.actor)}</td>
        <td><span class="action-badge action-${escapeHtml(e.action)}">${escapeHtml(e.action)}</span></td>
        <td><code>${escapeHtml(e.setting_key || "—")}</code></td>
        <td class="value-cell">${e.old_value === null || e.old_value === undefined ? "—" : escapeHtml(JSON.stringify(e.old_value))}</td>
        <td class="value-cell">${e.new_value === null || e.new_value === undefined ? "—" : escapeHtml(JSON.stringify(e.new_value))}</td>
        <td>${escapeHtml(e.notes || "")}</td>
      </tr>`).join("");
    $("#audit-table").innerHTML = `
      <thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Key</th><th>Old</th><th>New</th><th>Notes</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="7" class="empty">No audit entries yet</td></tr>`}</tbody>`;
  } catch (e) {
    if (e.message !== "not authenticated") toast(e.message, "error");
  }
}

// -------- Password rotation --------
$("#password-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#password-msg");
  msg.className = "";
  msg.textContent = "";
  try {
    await api("/password", {
      method: "POST",
      body: JSON.stringify({
        current_password: $("#current-password").value,
        new_password: $("#new-password").value,
      }),
    });
    msg.className = "ok";
    msg.textContent = "Password updated. You'll need to sign in again on next session.";
    $("#current-password").value = "";
    $("#new-password").value = "";
  } catch (e) {
    msg.className = "err";
    msg.textContent = e.message;
  }
});

// -------- TOTP / MFA --------
async function refreshTotpStatus() {
  try {
    const me = await api("/me");
    const enabled = !!me.totp_required;
    $("#totp-status").textContent = enabled
      ? "MFA is enabled."
      : "MFA is not enabled.";
    $("#totp-setup").hidden = enabled;
    $("#totp-disable").hidden = !enabled;
    $("#totp-setup-panel").hidden = true;
  } catch (e) {
    if (e.message !== "not authenticated") toast(e.message, "error");
  }
}

let _pendingTotpSecret = null;

document.addEventListener("click", async (e) => {
  if (e.target && e.target.id === "totp-start-btn") {
    try {
      const r = await api("/totp/setup", { method: "POST" });
      _pendingTotpSecret = r.secret;
      $("#totp-uri").textContent = r.uri;
      $("#totp-secret").textContent = `secret: ${r.secret}`;
      $("#totp-setup-panel").hidden = false;
    } catch (err) { toast(err.message, "error"); }
  }
  if (e.target && e.target.id === "totp-disable-btn") {
    if (!confirm("Disable MFA? Password-only login will be allowed.")) return;
    try {
      await api("/totp/disable", { method: "POST" });
      toast("MFA disabled");
      await refreshTotpStatus();
    } catch (err) { toast(err.message, "error"); }
  }
});

document.addEventListener("submit", async (e) => {
  if (e.target && e.target.id === "totp-verify-form") {
    e.preventDefault();
    const code = $("#totp-verify-code").value.trim();
    if (!_pendingTotpSecret) { toast("Start setup first", "error"); return; }
    try {
      await api("/totp/verify", {
        method: "POST",
        body: JSON.stringify({ secret: _pendingTotpSecret, code }),
      });
      _pendingTotpSecret = null;
      $("#totp-verify-code").value = "";
      toast("MFA enabled");
      await refreshTotpStatus();
    } catch (err) { toast(err.message, "error"); }
  }
  if (e.target && e.target.id === "gdpr-form") {
    e.preventDefault();
    const customer = $("#gdpr-customer").value.trim();
    const confirmation = $("#gdpr-confirm").value.trim();
    const msg = $("#gdpr-msg");
    msg.className = "";
    msg.textContent = "";
    if (!confirm(`Permanently delete all data for "${customer}"? This is irreversible.`)) return;
    try {
      const r = await api("/gdpr/delete-customer", {
        method: "POST",
        body: JSON.stringify({ customer_name: customer, confirmation }),
      });
      msg.className = "ok";
      msg.textContent = `Deleted ${r.deleted_meetings ?? 0} record(s). Deletion ID: ${r.deletion_id} · customer hash: ${r.customer_hash}`;
      $("#gdpr-customer").value = "";
      $("#gdpr-confirm").value = "";
    } catch (err) {
      msg.className = "err";
      msg.textContent = err.message;
    }
  }
});

// Refresh TOTP status when Account tab opens.
$$(".tab").forEach((t) => t.addEventListener("click", () => {
  if (t.dataset.tab === "account") refreshTotpStatus();
}));

// -------- Boot --------
(async function main() {
  try {
    await api("/me");
    showShell();
    await loadSettings();
    await refreshTotpStatus();
  } catch (_) {
    showLogin();
  }
})();
