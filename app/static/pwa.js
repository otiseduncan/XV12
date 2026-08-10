(() => {
  const TOKEN_KEY = "xv12_onboarding_token";
  let deferredInstall = null;
  const params = new URLSearchParams(window.location.search);
  const incoming = params.get("onboard");
  if (incoming && incoming.length >= 20) {
    sessionStorage.setItem(TOKEN_KEY, incoming);
    params.delete("onboard");
    const cleanQuery = params.toString();
    history.replaceState({}, "", `${location.pathname}${cleanQuery ? `?${cleanQuery}` : ""}${location.hash}`);
  }

  function createInstallCard(message = "Install Xoduz for its own application icon and standalone window.") {
    let card = document.querySelector("#xoduz-install-card");
    if (card) return card;
    card = document.createElement("aside");
    card.id = "xoduz-install-card";
    card.className = "xoduz-install-card";
    card.innerHTML = `<img src="/assets/avatar/xoduz-512.png" alt=""><div><strong>Install Xoduz</strong><span>${message}</span></div><button type="button" class="primary-button">Install</button><button type="button" class="icon-button" aria-label="Dismiss">×</button>`;
    card.querySelector(".icon-button").addEventListener("click", () => card.remove());
    card.querySelector(".primary-button").addEventListener("click", async () => {
      if (!deferredInstall) {
        card.querySelector("span").textContent = "Use your browser menu and choose Install app or Add to Home screen. Xoduz's manifest supplies the application icon.";
        return;
      }
      deferredInstall.prompt();
      await deferredInstall.userChoice;
      deferredInstall = null;
      card.remove();
    });
    document.body.append(card);
    return card;
  }

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferredInstall = event;
    if (sessionStorage.getItem(TOKEN_KEY) || params.get("enrollment") === "complete") createInstallCard();
  });
  window.addEventListener("appinstalled", () => document.querySelector("#xoduz-install-card")?.remove());

  async function claimOnboarding() {
    const token = sessionStorage.getItem(TOKEN_KEY);
    if (!token) return;
    let me;
    try {
      const response = await fetch("/api/auth/me", { credentials: "same-origin", cache: "no-store" });
      if (!response.ok) return;
      me = await response.json();
    } catch { return; }
    if (me.role !== "user") {
      sessionStorage.removeItem(TOKEN_KEY);
      return;
    }
    try {
      const response = await fetch("/api/admin/capabilities/invitations/claim", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || `Invitation claim failed (${response.status})`);
      sessionStorage.removeItem(TOKEN_KEY);
      history.replaceState({}, "", "/?enrollment=complete");
      createInstallCard("You're connected. Install Xoduz now so it appears with its own app icon.");
    } catch (error) {
      const status = document.querySelector("#login-status") || document.querySelector("#composer-status");
      if (status) status.textContent = `Onboarding needs attention: ${error.message}`;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    if (params.get("enrollment") === "complete") createInstallCard();
    const invite = sessionStorage.getItem(TOKEN_KEY);
    if (invite) {
      const copy = document.querySelector(".login-copy");
      if (copy) copy.textContent = "Your private Xoduz invitation is ready. Continue with Google to identify this Xoduz account.";
    }
    setTimeout(claimOnboarding, 250);
    setTimeout(claimOnboarding, 1200);
  });
})();
