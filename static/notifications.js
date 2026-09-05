(() => {
  const inbox = document.querySelector("[data-notifications]");
  if (!inbox) return;
  const button = inbox.querySelector("button");
  const panel = inbox.querySelector(".notification-panel");
  const close = () => {
    panel.hidden = true;
    button.setAttribute("aria-expanded", "false");
  };
  button.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    button.setAttribute("aria-expanded", String(!panel.hidden));
  });
  document.addEventListener("click", (event) => {
    if (!inbox.contains(event.target)) close();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !panel.hidden) {
      close();
      button.focus();
    }
  });
  inbox.addEventListener("focusout", (event) => {
    if (!inbox.contains(event.relatedTarget)) close();
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) location.reload();
  });
})();
