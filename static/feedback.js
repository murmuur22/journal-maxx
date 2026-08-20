(() => {
  const dismissToast = (toast, delay = 6000) => window.setTimeout(() => {
    toast.classList.add("is-dismissing");
    toast.addEventListener("transitionend", () => toast.remove(), { once: true });
    window.setTimeout(() => toast.remove(), 500);
  }, delay);
  const showToast = (message, isError = false) => {
    const toast = document.createElement("div");
    toast.className = `message${isError ? " error" : ""}`;
    toast.dataset.toast = "";
    toast.setAttribute("role", isError ? "alert" : "status");
    toast.textContent = message;
    document.querySelector("main")?.prepend(toast);
    dismissToast(toast, isError ? 9000 : 6000);
  };
  document.querySelectorAll("[data-toast]").forEach((toast) => dismissToast(toast, toast.classList.contains("error") ? 9000 : 6000));

  const modal = document.getElementById("feedback-modal");
  if (!modal) return;
  const form = modal.querySelector("form");
  document.querySelectorAll("[data-feedback-open]").forEach((button) => button.addEventListener("click", () => {
    modal.showModal();
    modal.querySelector("textarea")?.focus();
  }));
  modal.querySelectorAll("[data-feedback-close]").forEach((button) => button.addEventListener("click", () => modal.close()));
  modal.addEventListener("click", (event) => {
    if (event.target === modal) modal.close();
  });
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector('button[type="submit"], button:not([type])');
    submit.disabled = true;
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.message || "Feedback could not be sent.");
      modal.close();
      form.reset();
      showToast(result.message);
    } catch (error) {
      showToast(error.message || "Feedback could not be sent.", true);
    } finally {
      submit.disabled = false;
    }
  });
})();
