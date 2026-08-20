(() => {
  const timeout = Number(document.body.dataset.sessionTimeout) * 1000;
  const keepaliveUrl = document.body.dataset.sessionKeepalive;
  const warning = document.getElementById("session-warning");
  const logoutForm = document.querySelector('.disconnect-form, .mobile-disconnect-form');
  if (!timeout || !keepaliveUrl || !warning || !logoutForm) return;

  const warningLead = Math.min(120000, timeout / 3);
  let lastActivity = Date.now();
  let lastKeepalive = Date.now();
  let warningTimer;
  let logoutTimer;

  const csrf = logoutForm.querySelector('[name="csrfmiddlewaretoken"]')?.value;
  const keepalive = async (force = false) => {
    if (!force && Date.now() - lastKeepalive < 60000) return;
    lastKeepalive = Date.now();
    try {
      const body = new FormData(); body.set("csrfmiddlewaretoken", csrf);
      const response = await fetch(keepaliveUrl, { method: "POST", body, credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!response.ok) window.location.assign("/login/?expired=1");
    } catch (_) { /* A temporary connection failure should not destroy unsaved work. */ }
  };
  const schedule = () => {
    window.clearTimeout(warningTimer); window.clearTimeout(logoutTimer);
    warningTimer = window.setTimeout(() => { if (!warning.open) warning.showModal(); }, Math.max(0, timeout - warningLead));
    logoutTimer = window.setTimeout(() => logoutForm.requestSubmit(), timeout);
  };
  const activity = () => {
    lastActivity = Date.now();
    if (warning.open) warning.close();
    schedule(); keepalive();
  };
  ["pointerdown", "keydown", "input", "scroll", "touchstart"].forEach((name) => document.addEventListener(name, activity, { passive: true }));
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    if (Date.now() - lastActivity >= timeout) logoutForm.requestSubmit(); else activity();
  });
  warning.querySelector("[data-session-continue]")?.addEventListener("click", () => { activity(); keepalive(true); });
  schedule();
})();
