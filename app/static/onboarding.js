(() => {
  const button = document.querySelector("#invites-button");
  if (!button) return;

  async function json(path, options = {}) {
    const response = await fetch(path, {
      credentials: "same-origin",
      cache: "no-store",
      ...options,
      headers: options.body ? { "Content-Type": "application/json", ...(options.headers || {}) } : options.headers,
    });
    const body = response.status === 204 ? null : await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body?.detail || `Request failed (${response.status})`);
    return body;
  }

  async function syncVisibility() {
    try {
      const me = await json("/api/auth/me");
      button.classList.toggle("hidden", me.role !== "admin");
    } catch { button.classList.add("hidden"); }
  }

  function statusClass(status) {
    return ["active", "pending"].includes(status) ? "good" : ["expired", "revoked"].includes(status) ? "muted" : "";
  }

  async function renderAdmin() {
    const modal = document.querySelector("#modal");
    const content = document.querySelector("#modal-content");
    const [config, listing] = await Promise.all([
      json("/api/admin/capabilities/invitations/config"),
      json("/api/admin/capabilities/invitations"),
    ]);
    const ready = config.tailscale_api_ready && config.public_onboarding_ready && config.private_xoduz_ready;
    content.innerHTML = `
      <p class="eyebrow">ADMIN · REMOTE ACCESS</p>
      <h2>Invite a Xoduz user</h2>
      <p>Create a one-time QR without knowing the person's email. Xoduz asks Tailscale for a member invite with no email address, then Google identifies the person when they sign in.</p>
      <div class="onboarding-readiness ${ready ? "ready" : "needs-config"}">
        <b>${ready ? "QR onboarding is ready" : "One-time setup is required"}</b>
        <span>Tailscale API ${config.tailscale_api_ready ? "✓" : "✕"} · Public setup URL ${config.public_onboarding_ready ? "✓" : "✕"} · Private Xoduz URL ${config.private_xoduz_ready ? "✓" : "✕"}</span>
      </div>
      <form id="invite-user-form" class="invite-user-form">
        <label>Invitation life<select name="expires_hours"><option value="24">24 hours</option><option value="168" selected>7 days</option><option value="720">30 days</option></select></label>
        <details><summary>Manual Tailscale invite fallback</summary><label>Paste invite URL<input name="tailscale_invite_url" type="url" placeholder="https://login.tailscale.com/uinv/…"></label><p class="setting-note">Leave blank when XV12_TAILSCALE_API_TOKEN is configured.</p></details>
        <button class="primary-button" type="submit">Generate QR invitation</button>
        <span id="invite-create-status" class="setting-note"></span>
      </form>
      <div id="invite-result"></div>
      <section class="settings-section"><h3>Invitation history</h3><div id="invite-list" class="invite-list"></div></section>`;

    const list = content.querySelector("#invite-list");
    if (!listing.invitations.length) list.innerHTML = '<p class="setting-note">No invitations have been created yet.</p>';
    listing.invitations.forEach((invite) => {
      const row = document.createElement("article");
      row.className = "invite-history-row";
      const who = invite.claimed_display_name ? `${invite.claimed_display_name}${invite.claimed_email ? ` · ${invite.claimed_email}` : ""}` : "Not claimed";
      const detail = document.createElement("div");
      const statusLabel = document.createElement("strong");
      statusLabel.textContent = String(invite.status || "unknown").replaceAll("_", " ");
      statusLabel.classList.add(statusClass(invite.status));
      const person = document.createElement("span");
      person.textContent = who;
      const expires = document.createElement("small");
      expires.textContent = `Expires ${new Date(invite.expires_at).toLocaleString()}`;
      detail.append(statusLabel, person, expires);
      const actions = document.createElement("div");
      actions.className = "invite-row-actions";
      row.append(detail, actions);
      if (["pending", "expired"].includes(invite.status)) {
        const revoke = document.createElement("button");
        revoke.className = "secondary-button"; revoke.type = "button"; revoke.textContent = "Revoke";
        revoke.addEventListener("click", async () => {
          const result = await json(`/api/admin/capabilities/invitations/${invite.id}`, { method: "DELETE" });
          if (result.warning) alert(result.warning);
          await renderAdmin();
        });
        actions.append(revoke);
      }
      list.append(row);
    });

    content.querySelector("#invite-user-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const status = content.querySelector("#invite-create-status");
      const result = content.querySelector("#invite-result");
      status.textContent = "Creating one-time Tailscale and Xoduz invitation…";
      try {
        const values = Object.fromEntries(new FormData(event.currentTarget));
        const created = await json("/api/admin/capabilities/invitations", {
          method: "POST",
          body: JSON.stringify({
            expires_hours: Number(values.expires_hours || 168),
            tailscale_invite_url: String(values.tailscale_invite_url || "").trim() || null,
          }),
        });
        status.textContent = created.public_onboarding_ready ? "Ready to send." : "Created, but the public onboarding URL still needs configuration.";
        result.innerHTML = `
          <article class="invite-result-card">
            <img alt="Xoduz setup QR code">
            <div><p class="eyebrow">ONE-TIME XODUZ INVITE</p><h3>Send this QR to the user</h3><p>Default X access: <b id="invite-default-access"></b>. The user does not need to give you an email first.</p><code></code><div class="settings-actions"><button id="copy-invite-link" class="secondary-button" type="button">Copy setup link</button></div></div>
          </article>`;
        result.querySelector("img").src = created.qr_url;
        result.querySelector("#invite-default-access").textContent = created.default_access;
        result.querySelector("code").textContent = created.setup_url;
        result.querySelector("#copy-invite-link").addEventListener("click", async () => {
          await navigator.clipboard.writeText(created.setup_url);
          status.textContent = "Setup link copied.";
        });
      } catch (error) {
        status.textContent = error.message;
        result.replaceChildren();
      }
    });
    if (!modal.open) modal.showModal();
  }

  button.addEventListener("click", () => renderAdmin().catch((error) => alert(`User invitation failed: ${error.message}`)));
  document.addEventListener("DOMContentLoaded", () => { syncVisibility(); setTimeout(syncVisibility, 500); setTimeout(syncVisibility, 1500); });
  const appView = document.querySelector("#app-view");
  if (appView) new MutationObserver(syncVisibility).observe(appView, { attributes: true, attributeFilter: ["class"] });
})();
