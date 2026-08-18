(() => {
  const modal = document.getElementById("feedback-modal");
  if (!modal) return;
  document.querySelectorAll("[data-feedback-open]").forEach((button) => button.addEventListener("click", () => {
    modal.showModal();
    modal.querySelector("textarea")?.focus();
  }));
  modal.querySelectorAll("[data-feedback-close]").forEach((button) => button.addEventListener("click", () => modal.close()));
  modal.addEventListener("click", (event) => {
    if (event.target === modal) modal.close();
  });
})();
